"""CutFlow v0.16 迭代回归:BUGREPORT P9 / P10 / P16 修复对拍。

P9  rs_run S1 post 步骤脚本名拼重 → S1 永远失败,能量校准从未生效。
P10 S5/S6 声明产物与实际产物不符,且 S5/S8 同 glob 互相打脏、--dirty 永不收敛。
P16 rs_cleanup --apply 删掉 06_output/rebuild.py 与 _variants/,手册重建链断。

运行:pytest tests/test_v16_iteration.py -q
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_cleanup  # noqa: E402
import rs_run  # noqa: E402


def _mk_project(tmp_path: Path) -> Path:
    """最小工程:只铺 rs_run 记账要碰的目录与占位素材(不触发真实子进程)。"""
    root = tmp_path / "proj"
    for d in ("00_brief", "01_materials", "04_cut", "05_ir", "06_output", "_state"):
        (root / d).mkdir(parents=True)
    (root / "01_materials" / "a.mp4").write_bytes(b"fake")
    return root


def _stage(sid: str) -> dict:
    return next(s for s in rs_run.spec() if s["id"] == sid)


# ---------------------------------------------------------------- P9

def test_s1_post_argv_has_single_script_name(tmp_path, monkeypatch):
    """S1 post 必须以 rs_align.py 为脚本名恰好出现一次(P9 曾拼出两个)。"""
    root = _mk_project(tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)

        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return R()

    monkeypatch.setattr(rs_run.subprocess, "run", fake_run)
    ok, msg = rs_run.run_stage(root, _stage("S1"))
    assert ok, msg
    assert len(calls) == 2, f"应有主命令+post 两次调用:{calls}"
    post = calls[1]
    # argv[1] 是脚本绝对路径,argv[2:] 起是参数;脚本名只允许出现一次
    assert post[1].endswith("rs_align.py"), post
    assert sum(1 for tok in post if str(tok).endswith("rs_align.py")) == 1, post
    assert "calibrate" in post, post


def test_post_declares_script_name_like_cmd():
    """契约:st["post"] 与 st["cmd"] 同构(首元素=脚本名),且脚本真实存在。"""
    for st in rs_run.spec():
        post = st.get("post") or []
        if not post:
            continue
        assert post[0].endswith(".py"), f"{st['id']} post 首元素须是脚本名:{post}"
        assert (SCRIPTS / post[0]).is_file(), f"{st['id']} post 脚本不存在:{post[0]}"


def test_no_post_concat_in_source():
    """防复发:P9 的拼接写法不允许回来。"""
    src = (SCRIPTS / "rs_run.py").read_text(encoding="utf-8")
    assert '[st["cmd"][0]] + st["post"]' not in src


# ---------------------------------------------------------------- P10 / P10b-1

def test_s5_s6_s8_output_globs_match_reality_and_are_disjoint():
    # P10b-1(副文档03 §4.1)更新:S5/S8 各落独占子目录,从根上消除 glob 交叠
    s5, s6, s8 = _stage("S5"), _stage("S6"), _stage("S8")
    assert s5["outputs"] == ["06_output/branded/成片_*.mp4"]   # rs_brand --out 06_output/branded
    assert s6["outputs"] == ["05_ir/sfx_draft.json"]           # 与 S6 cmd 落点同点
    assert s8["outputs"] == ["06_output/final/final_*.mp4"]    # rs_render final 档独占子目录
    s5_set, s8_set = set(s5["outputs"]), set(s8["outputs"])
    assert not s5_set & s8_set, "S5/S8 产物 glob 交叠会互相打脏(--dirty 永不收敛)"


def test_s5_s8_states_converge_no_mutual_stale(tmp_path):
    """S5、S8 各自落账后,重评对方必须仍是 done(此前同 glob 导致互相改 outHash)。"""
    root = _mk_project(tmp_path)
    # P10b-1:两阶段产物各落独占子目录
    branded = root / "06_output" / "branded"
    final = root / "06_output" / "final"
    branded.mkdir(parents=True)
    final.mkdir(parents=True)
    (branded / "成片_916_logoA_final.mp4").write_bytes(b"brand")
    (final / "final_proj_916.mp4").write_bytes(b"render")
    for sid in ("S5", "S8"):
        st = _stage(sid)
        parts = rs_run.stage_parts(root, st, rs_run.params_of(root), {})
        rs_run.write_state(root, sid, {"status": "done", "key": rs_run.key_of(parts),
                                       "parts": parts,
                                       "outHash": rs_run.outputs_hash(root, st)})
    assert rs_run.evaluate(root, _stage("S5"))["status"] == "done"
    assert rs_run.evaluate(root, _stage("S8"))["status"] == "done"


def test_s6_done_when_draft_exists(tmp_path):
    """S6 声明产物=命令落点后,evaluate 不再恒 missing。"""
    root = _mk_project(tmp_path)
    (root / "05_ir" / "sfx_draft.json").write_text("{}", encoding="utf-8")
    st = _stage("S6")
    parts = rs_run.stage_parts(root, st, rs_run.params_of(root), {})
    rs_run.write_state(root, "S6", {"status": "done", "key": rs_run.key_of(parts),
                                    "parts": parts,
                                    "outHash": rs_run.outputs_hash(root, st)})
    assert rs_run.evaluate(root, st)["status"] == "done"


# ---------------------------------------------------------------- P16

def test_cleanup_keeps_rebuild_scripts():
    assert rs_cleanup._out_keep("rebuild.py")
    assert rs_cleanup._out_keep("REBUILD.md")
    assert rs_cleanup._out_keep("verify_report.md")
    # 探针件仍删
    assert not rs_cleanup._out_keep("_probe.mp4")
    assert not rs_cleanup._out_keep("dev-x.png")


def test_cleanup_classify_keeps_rebuild_and_variants(tmp_path):
    root = _mk_project(tmp_path)
    out = root / "06_output"
    (out / "rebuild.py").write_text("# rebuild", encoding="utf-8")
    (out / "_variants").mkdir()
    (out / "_variants" / "v.json").write_text("{}", encoding="utf-8")
    (out / "sub_916").mkdir()
    (out / "junk.txt").write_text("x", encoding="utf-8")
    delete, keep = rs_cleanup.classify(root)
    keep_names = [p.name for p in keep]
    del_names = [p.name for p in delete]
    for must_keep in ("rebuild.py", "_variants", "sub_916"):
        assert must_keep in keep_names, f"{must_keep} 被列入删除(P16):{del_names}"
    assert "junk.txt" in del_names


def test_cleanup_apply_preserves_rebuild(tmp_path):
    root = _mk_project(tmp_path)
    out = root / "06_output"
    (out / "rebuild.py").write_text("# rebuild", encoding="utf-8")
    (out / "junk.txt").write_text("x", encoding="utf-8")
    delete, _ = rs_cleanup.classify(root)
    for d in delete:
        if d.is_dir():
            import shutil
            shutil.rmtree(d)
        elif d.is_file():
            d.unlink()
    assert (out / "rebuild.py").is_file(), "rs_cleanup --apply 后 rebuild.py 必须仍在(P16)"
    assert not (out / "junk.txt").exists()
