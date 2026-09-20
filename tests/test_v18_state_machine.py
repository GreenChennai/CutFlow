"""副文档 03 · 阶段三 §4.1「状态机可信」+ §4.4 O7-2 回归:P11/P12/P13/P14/P15/P10b/O7-2。

P11-1  S4 只认 artboard manifest --apply 后的 appliedAt;无标记一律 missing(不再借 S3 产物自动 done)
P12-1  params 落进 pipeline.json 缓存键;brief 声明的参数改了 → S7 变脏;params_of 仅缺失时回退默认
P13-1  --force 无 --from/--only target 报错;备份移进 run_stage(每个写盘的非 cached 阶段都先备份)
P13-2  删除失败不再 ignore_errors 掩盖:rs_run 备份清理失败上报,rs_cleanup 失败项进 failed 且非零退出
P14-1  --only 命中已 done → PRECONDITION_FAILED 非零退出 + 提示补 --force(不再静默 cached)
P15-1  状态/记账原子写(临时文件 + os.replace);状态文件损坏区分 corrupt 并显式告警
P10b-1 S5/S8 各写独占子目录 06_output/branded/、06_output/final/;旧顶层产物兼容不追改
O7-2   写状态前探测 .cutforge/lock;编辑器在运行 → 只读降级 + 明确告警(不崩、不静默)

运行:pytest tests/test_v18_state_machine.py -q
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_artboard  # noqa: E402
import rs_cleanup  # noqa: E402
import rs_ingest  # noqa: E402
import rs_run  # noqa: E402
import rs_verify  # noqa: E402


def _capture(fn, *args, **kw):
    """跑返回 (exit_code, 最后一个 JSON 输出)——rs_* 的 emit 协议。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    doc = json.loads(lines[-1]) if lines else {}
    return code, doc


def _mk_project(tmp_path: Path) -> Path:
    """最小工程:铺 rs_run 记账要碰的目录;产物在用例内按需补。"""
    root = tmp_path / "proj"
    for d in ("00_brief", "01_materials", "04_cut", "05_ir", "06_output", "_state"):
        (root / d).mkdir(parents=True)
    (root / "01_materials" / "a.mp4").write_bytes(b"fake")
    return root


def _stage(sid: str) -> dict:
    return next(s for s in rs_run.spec() if s["id"] == sid)


def _fake_run_ok(cmd, **kw):
    class R:
        returncode = 0
        stdout = "ok"
        stderr = ""
    return R()


def _seed_done(root: Path, sid: str) -> None:
    """按真实 run_stage 的口径给某阶段落一份 done 状态(等价于跑完后写的账)。"""
    st = _stage(sid)
    parts = rs_run.stage_parts(root, st, rs_run.params_of(root), {})
    rs_run.write_state(root, sid, {"status": "done", "key": rs_run.key_of(parts),
                                   "parts": parts,
                                   "outHash": rs_run.outputs_hash(root, st)})


# ================================================================ P11-1 S4 真实产物标记

def test_p11_s4_not_auto_done_by_borrowing_s3_output(tmp_path):
    """P11-1 核心回归:S3 的产物(project.json)在,S4 没有 appliedAt 标记 → 必须 missing。"""
    root = _mk_project(tmp_path)
    (root / "05_ir" / "project.json").write_text("{}", encoding="utf-8")
    r = rs_run.evaluate(root, _stage("S4"))
    assert r["status"] == "missing", "S4 不得再借 S3 产物自动 done"
    assert "appliedAt" in r["staleReason"][0]


def test_p11_s4_done_only_with_applied_at_marker(tmp_path):
    """P11-1:manifest 有 --apply 写入的 appliedAt → S4 done;删掉标记键 → 回到 missing。"""
    root = _mk_project(tmp_path)
    mp = root / "03_assets" / "artboard" / "manifest.json"
    mp.parent.mkdir(parents=True)
    mp.write_text(json.dumps({"version": 1, "items": []}), encoding="utf-8")
    assert rs_run.evaluate(root, _stage("S4"))["status"] == "missing"
    doc = json.loads(mp.read_text(encoding="utf-8"))
    doc["appliedAt"] = "2026-09-20T12:00:00+08:00"
    mp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    assert rs_run.evaluate(root, _stage("S4"))["status"] == "done"


def test_p11_mark_s4_still_works_without_artboard(tmp_path):
    """P11-1:无卡片工程 --mark S4 显式记录后必须稳定 done(不被"产物缺失"翻成 missing)。"""
    root = _mk_project(tmp_path)
    (root / "05_ir" / "project.json").write_text("{}", encoding="utf-8")
    monkey_mark = {"argv": ["rs_run.py", "--root", str(root), "--mark", "S4"]}
    orig = sys.argv
    sys.argv = monkey_mark["argv"]
    try:
        code, doc = _capture(rs_run.main)
    finally:
        sys.argv = orig
    assert code == 0 and doc["code"] == "MARK_OK", doc
    assert rs_run.evaluate(root, _stage("S4"))["status"] == "done"


def test_p11_artboard_apply_stamps_applied_at(tmp_path):
    """P11-1:rs_artboard --apply 成功后在 manifest 写 appliedAt(rs_run 的标记物来源)。"""
    root = _mk_project(tmp_path)
    src = root / "03_assets" / "artboard" / "c1" / "src"
    src.mkdir(parents=True)
    (src / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
    out = root / "03_assets" / "artboard" / "c1" / "export" / "c1.png"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"png")
    manifest = root / "03_assets" / "artboard" / "manifest.json"
    manifest.write_text(json.dumps({"version": 1, "items": [
        {"id": "c1", "project": "03_assets/artboard/c1/src",
         "output": "03_assets/artboard/c1/export/c1.png",
         "kind": "png", "size": [1080, 1920],
         "sourceHash": rs_artboard.hash_source(src)}]}, ensure_ascii=False), encoding="utf-8")
    ir = root / "05_ir" / "project.json"
    ir.write_text(json.dumps({"canvas": {"width": 1080, "height": 1920},
                              "tracks": [{"kind": "video", "clips": [
                                  {"src": "03_assets/artboard/c1/export/c1.png",
                                   "startMs": 0, "durationMs": 2000}]}]}), encoding="utf-8")
    orig = sys.argv
    sys.argv = ["rs_artboard.py", "--root", str(root), str(manifest), "--apply", str(ir)]
    try:
        code, doc = _capture(rs_artboard.main)
    finally:
        sys.argv = orig
    assert code == 0 and doc["code"] == "APPLY_OK", doc
    mf = json.loads(manifest.read_text(encoding="utf-8"))
    assert mf.get("appliedAt"), "apply 成功必须盖 appliedAt(S4 的产物标记)"
    assert rs_run.evaluate(root, _stage("S4"))["status"] == "done"


# ================================================================ P12-1 params 进缓存键

def test_p12_params_backfilled_into_pipeline_json(tmp_path):
    """P12-1:写状态时把生效参数落进 pipeline.json(旧工程缺 params 回填一次);
    params_of 仅在缺失时回退默认。"""
    root = _mk_project(tmp_path)
    assert rs_run.params_of(root) == rs_run.default_params(), "缺失时回退默认"
    _seed_done(root, "S6")
    agg = json.loads((root / "05_ir" / "pipeline.json").read_text(encoding="utf-8"))
    assert agg.get("params", {}).get("cpsMax") == 9, "write_state 必须回填 params"
    assert "maxChars" in agg["params"]


def test_p12_brief_params_override_and_dirty_s7(tmp_path):
    """P12-1 验收:改 brief 里声明的每卡字数 → S7 变脏(reason 指到 maxChars)。"""
    root = _mk_project(tmp_path)
    (root / "05_ir" / "wordline.json").write_text("{}", encoding="utf-8")
    (root / "06_output" / "subtitles.ass").write_text("[Script Info]", encoding="utf-8")
    (root / "00_brief" / "brief.md").write_text("# Brief\n\n- 每卡字数: 12\n",
                                                encoding="utf-8")
    assert rs_run.params_of(root)["maxChars"] == 12
    _seed_done(root, "S7")
    assert rs_run.evaluate(root, _stage("S7"))["status"] == "done"
    # 改 brief:12 → 10
    (root / "00_brief" / "brief.md").write_text("# Brief\n\n- 每卡字数: 10\n",
                                                encoding="utf-8")
    r = rs_run.evaluate(root, _stage("S7"))
    assert r["status"] == "stale", "改 brief 参数后 S7 必须变脏"
    assert any("params 变化 maxChars" in w for w in r["staleReason"]), r["staleReason"]
    # 平台别名映射进快照(证据链),画幅同理
    (root / "00_brief" / "brief.md").write_text("平台: 抖音\n画幅: 9x16\nCPS: 8\n",
                                                encoding="utf-8")
    p = rs_run.params_of(root)
    assert (p["platform"], p["ratio"], p["cpsMax"]) == ("douyin", "9x16", 8.0), p


def test_p12_params_only_dirty_declared_stages(tmp_path):
    """P12-1:未声明 paramKeys 的阶段(S2)不因全局参数变化被打脏——改字幕参数不该触发重转写。"""
    root = _mk_project(tmp_path)
    (root / "05_ir" / "wordline.json").write_text("{}", encoding="utf-8")
    (root / "04_cut" / "cutlist.json").write_text("{}", encoding="utf-8")
    _seed_done(root, "S2")
    doc = json.loads((root / "05_ir" / "pipeline.json").read_text(encoding="utf-8"))
    doc["params"]["cpsMax"] = 7.5                     # 手改生效参数
    (root / "05_ir" / "pipeline.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    assert rs_run.evaluate(root, _stage("S2"))["status"] == "done", "S2 不消费字幕参数"
    # 而 S7(声明了 paramKeys)同全局参数变化必须变脏
    (root / "05_ir" / "wordline.json").write_text("{}", encoding="utf-8")
    (root / "06_output" / "subtitles.ass").write_text("x", encoding="utf-8")
    _seed_done(root, "S7")
    doc = json.loads((root / "05_ir" / "pipeline.json").read_text(encoding="utf-8"))
    doc["params"]["cpsMax"] = 8.5
    (root / "05_ir" / "pipeline.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    assert rs_run.evaluate(root, _stage("S7"))["status"] == "stale"


# ================================================================ P13-1 force 语义与备份范围

def test_p13_force_without_target_is_rejected(tmp_path, monkeypatch):
    """P13-1:--force 无 --from/--only target → FORCE_NEEDS_TARGET 非零退出(不再静默 no-op)。"""
    root = _mk_project(tmp_path)
    monkeypatch.setattr(sys, "argv", ["rs_run.py", "--root", str(root), "--dirty", "--force"])
    code, doc = _capture(rs_run.main)
    assert code == 2 and doc["code"] == "FORCE_NEEDS_TARGET", doc


def test_p13_backup_moves_into_run_stage_for_every_writing_stage(tmp_path, monkeypatch):
    """P13-1:非 cached 阶段(含级联)经 run_stage 跑之前都先备份将覆盖的产物。"""
    root = _mk_project(tmp_path)
    (root / "05_ir" / "wordline.json").write_text("旧字幕上游", encoding="utf-8")
    monkeypatch.setattr(rs_run.subprocess, "run", _fake_run_ok)
    info: dict = {}
    ok, msg = rs_run.run_stage(root, _stage("S1"), info)
    assert ok, msg
    bp = info.get("backup")
    assert bp and Path(bp).is_dir(), "run_stage 必须先备份"
    saved = list(Path(bp).rglob("wordline.json"))
    assert saved and saved[0].read_text(encoding="utf-8") == "旧字幕上游"
    # 无产物可备份时 backup 为 None(首跑常态)
    root2 = _mk_project(tmp_path / "b")
    info2: dict = {}
    ok2, _ = rs_run.run_stage(root2, _stage("S1"), info2)
    assert ok2 and info2["backup"] is None


# ================================================================ P13-2 删除失败诚实

def test_p13_prune_failure_reported_not_swallowed(tmp_path, monkeypatch):
    """P13-2:旧备份清理失败不再 ignore_errors 掩盖——失败项收进 errors。"""
    root = _mk_project(tmp_path)
    bdir = root / "_state" / "backup"
    for i in range(8):
        d = bdir / f"2026010{i}-000000"
        d.mkdir(parents=True)
        (d / "x.txt").write_text("x", encoding="utf-8")
    real_rmtree = shutil.rmtree

    def fake_rmtree(path, *a, **kw):
        if Path(path).name == "20260100-000000":
            raise OSError(" simulated lock")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(shutil, "rmtree", fake_rmtree)
    errors: list[str] = []
    rs_run._prune_backups(root, errors)
    assert len(errors) == 1 and "20260100-000000" in errors[0], errors
    left = [d.name for d in bdir.iterdir() if d.is_dir()]
    assert "20260100-000000" in left and "20260107-000000" in left, left


def test_p13_cleanup_reports_failures_and_honest_freed(tmp_path, monkeypatch):
    """P13-2:rs_cleanup --apply 删失败 → CLEANUP_PARTIAL 非零退出、failed 逐项列出、
    释放量只计真删掉的。"""
    root = _mk_project(tmp_path)
    junk = root / "06_output" / "junk.txt"
    junk.write_text("j" * 1_500_000, encoding="utf-8")          # 1.5MB,可正常删
    build = root / "06_output" / "_build"
    build.mkdir()
    (build / "x.mkv").write_text("b" * 2_000_000, encoding="utf-8")   # 2.0MB,模拟被占用
    real_rmtree = shutil.rmtree

    def fake_rmtree(path, *a, **kw):
        if Path(path).name == "_build":
            raise OSError(" simulated lock")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(shutil, "rmtree", fake_rmtree)
    monkeypatch.setattr(sys, "argv", ["rs_cleanup.py", str(root), "--apply"])
    code, doc = _capture(rs_cleanup.main)
    assert code == 4 and doc["code"] == "CLEANUP_PARTIAL", doc
    fails = doc["data"]["failed"]
    assert any(f["path"].endswith("_build") for f in fails), fails
    assert build.exists(), "删除失败的项必须还在(不许谎报已删)"
    assert not junk.exists()
    assert doc["data"]["freed_mb"] == 1.5, "释放量只计真删掉的,失败项的 2MB 不得计入"
    # 撤掉锁 → 全部删掉,如实报释放量
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    code, doc = _capture(rs_cleanup.main)  # argv 仍在
    assert code == 0 and doc["code"] == "CLEANUP_OK", doc
    assert doc["data"]["size_mb"] == 2.0 and not build.exists()


def test_p13_dirty_rounds_catch_midrun_dirty_spread(tmp_path, monkeypatch):
    """收敛验收的根:上游重跑(S7 改字)把下游(S8 输入)打脏时,--dirty 逐轮重选,
    同一次调用内收敛,不拖到下一次。"""
    root = _mk_project(tmp_path)
    (root / "05_ir" / "wordline.json").write_text("{}", encoding="utf-8")
    (root / "04_cut" / "cutlist.json").write_text("{}", encoding="utf-8")
    (root / "05_ir" / "project.json").write_text("{}", encoding="utf-8")
    (root / "05_ir" / "sfx_draft.json").write_text("{}", encoding="utf-8")
    (root / "06_output" / "subtitles.ass").write_text("旧字幕", encoding="utf-8")
    final = root / "06_output" / "final"
    final.mkdir(parents=True)
    (final / "final_p_916.mp4").write_bytes(b"old render")
    branded = root / "06_output" / "branded"
    branded.mkdir(parents=True)
    (branded / "成片_916_demo_final.mp4").write_bytes(b"brand")
    (root / "06_output" / "sync_report.md").write_text("# old", encoding="utf-8")
    (root / "06_output" / "metadata.json").write_text("{}", encoding="utf-8")
    for sid in ("S1", "S2", "S3", "S5", "S6", "S8", "S9", "S10"):
        _seed_done(root, sid)

    def fake_run(cmd, **kw):
        # 模拟真实生产者:S7 重跑会改写 subtitles.ass(下游 S8 的输入)
        argv = [str(x) for x in cmd]
        if any(x.endswith("rs_subtitle.py") for x in argv):
            (root / "06_output" / "subtitles.ass").write_text("新字幕", encoding="utf-8")
        return _fake_run_ok(cmd, **kw)

    monkeypatch.setattr(rs_run.subprocess, "run", fake_run)
    monkeypatch.setattr(rs_run, "run_verify",
                        lambda root, level: (True, "L0 通过", {"firstCheckDone": True}))
    # 参数变化让 S7 变脏(maxChars 12 → 10)
    (root / "05_ir" / "pipeline.json").write_text(
        json.dumps({"version": 1, "params": {"maxChars": 10, "cpsMax": 9}}, ensure_ascii=False),
        encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_run.py", "--root", str(root), "--dirty"])
    code, doc = _capture(rs_run.main)
    assert code == 0 and doc["code"] == "RUN_OK", doc
    ran = [r["stage"] for r in doc["data"]["results"]
           if not r.get("cached") and not r.get("skipped")]
    assert "S7" in ran and "S8" in ran, f"下游 S8 必须在同一轮收敛内重跑:{ran}"
    # 二次 --dirty:全收敛,无事可跑
    code2, doc2 = _capture(rs_run.main)
    assert code2 == 0
    assert not [r for r in doc2["data"]["results"]
                if not r.get("cached") and not r.get("skipped")]


def test_p12_max_chars_placeholder_reaches_s7_cmd(tmp_path):
    """P12-1:S7 命令行经 {max_chars} 真正消费生效参数(字典/整数两种形态都能解析)。"""
    root = _mk_project(tmp_path)
    cmd = rs_run.build_cmd(root, _stage("S7"))
    assert "--max-chars" in cmd
    i = cmd.index("--max-chars")
    assert cmd[i + 1] == "12", f"默认取 MAX_CHARS[9x16]:{cmd}"
    (root / "00_brief" / "brief.md").write_text("- 每卡字数: 10\n", encoding="utf-8")
    cmd2 = rs_run.build_cmd(root, _stage("S7"))
    assert cmd2[cmd2.index("--max-chars") + 1] == "10", "brief 声明必须直达命令行"


# ================================================================ P14-1 --only 语义

def test_p14_only_done_stage_precondition_failed(tmp_path, monkeypatch):
    """P14-1 验收:--only 命中已 done → PRECONDITION_FAILED 非零退出 + 提示 --force,
    不再静默 cached。"""
    root = _mk_project(tmp_path)
    (root / "06_output" / "subtitles.ass").write_text("[Script Info]", encoding="utf-8")
    _seed_done(root, "S7")
    monkeypatch.setattr(sys, "argv", ["rs_run.py", "--root", str(root), "--only", "S7"])
    code, doc = _capture(rs_run.main)
    assert code == 2 and doc["code"] == "PRECONDITION_FAILED", doc
    assert "--force" in doc["message"]
    assert doc["data"]["stage"] == "S7"


def test_p14_only_stale_stage_runs_rather_than_cached(tmp_path, monkeypatch):
    """P14-1 对照:--only 命中 stale(输入带外变过)→ 真重跑,绝不 cached。"""
    root = _mk_project(tmp_path)
    (root / "05_ir" / "wordline.json").write_text("{}", encoding="utf-8")
    (root / "06_output" / "subtitles.ass").write_text("[Script Info]", encoding="utf-8")
    _seed_done(root, "S7")
    (root / "05_ir" / "wordline.json").write_text("{\n  \"changed\": true\n}", encoding="utf-8")
    monkeypatch.setattr(rs_run.subprocess, "run", _fake_run_ok)
    monkeypatch.setattr(rs_run, "run_verify", lambda root, level: (True, "L0 通过",
                                                                  {"firstCheckDone": True}))
    monkeypatch.setattr(sys, "argv", ["rs_run.py", "--root", str(root), "--only", "S7"])
    code, doc = _capture(rs_run.main)
    assert code == 0 and doc["code"] == "RUN_OK", doc
    entry = next(r for r in doc["data"]["results"] if r["stage"] == "S7")
    assert not entry.get("cached") and entry.get("backup"), entry


# ================================================================ P15-1 原子写与 corrupt

def test_p15_atomic_write_leaves_no_tmp_and_survives_crash_leftover(tmp_path):
    """P15-1:写盘走「临时文件 + os.replace」,不留 .tmp;残留半截 .tmp 不影响读取。"""
    root = _mk_project(tmp_path)
    _seed_done(root, "S6")
    assert rs_run.read_state(root, "S6") is not None
    assert not list((root / "_state").glob("*.tmp")), "原子写不得留下临时文件"
    assert not (root / "05_ir" / "pipeline.json.tmp").exists()
    # 模拟崩溃残留:半截 tmp + 完好正式文件
    (root / "_state" / "S6.json.tmp").write_text('{"status": "do', encoding="utf-8")
    assert rs_run.read_state(root, "S6")["status"] == "done"


def test_p15_corrupt_state_is_flagged_not_silent_missing(tmp_path):
    """P15-1:状态文件损坏 → corrupt 显式告警;其余阶段不受牵连(不误判全量 missing)。"""
    root = _mk_project(tmp_path)
    (root / "05_ir" / "sfx_draft.json").write_text("{}", encoding="utf-8")
    _seed_done(root, "S6")
    (root / "_state" / "S7.json").write_text('{"status": "do', encoding="utf-8")  # 半截
    rec, problem = rs_run.load_state(root, "S7")
    assert rec is None and problem == "corrupt"
    assert rs_run.read_state(root, "S7") is None, "read_state 兼容旧调用"
    r = rs_run.evaluate(root, _stage("S7"))
    assert r["status"] == "corrupt" and "损坏" in r["staleReason"][0]
    assert rs_run.evaluate(root, _stage("S6"))["status"] == "done", "别的阶段不受牵连"
    # --dirty 会把 corrupt 阶段纳入重跑(账坏了只能重做,但要响)
    todo = rs_run.select(root, rs_run.S13, None)
    assert any(s["id"] == "S7" for s in todo)
    # kill/掉电恢复验收:除损坏者外,已写盘的状态全部完好可读(不误判 missing)
    for sid in ("S1", "S2", "S3"):
        (root / "_state" / f"{sid}.json").write_text(
            json.dumps({"status": "done", "parts": {}, "key": "k"}), encoding="utf-8")
    assert all(rs_run.read_state(root, sid) for sid in ("S1", "S2", "S3", "S6"))


# ================================================================ O7-2 编辑器锁只读降级

def test_o72_lock_downgrades_write_state_readonly(tmp_path):
    """O7-2:.cutforge/lock 在 → write_state 只读降级(不写盘、返回原因),不崩、不静默。"""
    root = _mk_project(tmp_path)
    (root / ".cutforge").mkdir()
    (root / ".cutforge" / "lock").write_text("", encoding="utf-8")
    assert rs_run.editor_lock_held(root)
    reason = rs_run.write_state(root, "S6", {"status": "done"})
    assert reason and "只读降级" in reason
    assert not (root / "_state" / "S6.json").exists(), "降级时不得写状态"
    assert not rs_run.editor_lock_held(root / "nonexistent-project"), "无锁工程不受影响"
    # 无锁 → 正常写盘
    (root / ".cutforge" / "lock").unlink()
    assert rs_run.write_state(root, "S6", {"status": "done"}) is None
    assert rs_run.read_state(root, "S6") is not None


def test_o72_mark_under_lock_fails_loud(tmp_path, monkeypatch):
    """O7-2:--mark 写不进状态 → STATE_READONLY 非零退出(否则"做没做过"又没人知道)。"""
    root = _mk_project(tmp_path)
    (root / ".cutforge").mkdir()
    (root / ".cutforge" / "lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_run.py", "--root", str(root), "--mark", "S4"])
    code, doc = _capture(rs_run.main)
    assert code == 4 and doc["code"] == "STATE_READONLY", doc


def test_o72_run_stage_still_runs_under_lock_but_flags_readonly(tmp_path, monkeypatch):
    """O7-2:锁下阶段照跑(产物照出),但结果明确带回 stateReadOnly。"""
    root = _mk_project(tmp_path)
    (root / ".cutforge").mkdir()
    (root / ".cutforge" / "lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(rs_run.subprocess, "run", _fake_run_ok)
    info: dict = {}
    ok, msg = rs_run.run_stage(root, _stage("S1"), info)
    assert ok, msg
    assert info.get("stateSkipped") and "只读降级" in info["stateSkipped"]
    assert not (root / "_state" / "S1.json").exists()


# ================================================================ P10b-1 独占子目录

def test_p10b_final_videos_map_reads_new_and_legacy_locations(tmp_path):
    """P10b-1:{final_video} 同时认新子目录与旧工程顶层遗留(不追改历史产物)。"""
    root = _mk_project(tmp_path)
    (root / "06_output" / "final").mkdir()
    legacy = root / "06_output" / "final_old_916.mp4"
    legacy.write_bytes(b"old")
    old_ts = 1_000_000_000
    os.utime(legacy, (old_ts, old_ts))
    assert [p.name for p in rs_run._final_videos(root)] == ["final_old_916.mp4"]
    fresh = root / "06_output" / "final" / "final_new_916.mp4"
    fresh.write_bytes(b"new")
    assert rs_run._final_videos(root)[-1].name == "final_new_916.mp4", "最新成片按 mtime 取"


def test_p10b_verify_and_deliverables_see_subdir_videos(tmp_path):
    """P10b-1:rs_verify 产物检查 / rs_ingest 交付清单都必须看得到 final/、branded/ 里的成片。"""
    root = _mk_project(tmp_path)
    final = root / "06_output" / "final" / "final_p_916.mp4"
    final.parent.mkdir(parents=True)
    final.write_bytes(b"v")
    chk = rs_verify.check_artifacts(root)
    assert chk["ok"] is True and "final/final_p_916.mp4" in chk["videos"], chk
    res = rs_ingest.build_deliverables(root)
    assert "final/final_p_916.mp4" in res["videos"], res["videos"]
    branded = root / "06_output" / "branded" / "成片_916_logo_final.mp4"
    branded.parent.mkdir(parents=True)
    branded.write_bytes(b"v")
    assert "branded/成片_916_logo_final.mp4" in rs_ingest.build_deliverables(root)["videos"]


def test_p10b_cleanup_whitelists_branded_and_final_dirs(tmp_path):
    """P10b-1:rs_cleanup 不得把 branded/、final/ 独占子目录列进删除名单。"""
    root = _mk_project(tmp_path)
    out = root / "06_output"
    (out / "final").mkdir()
    (out / "final" / "final_p_916.mp4").write_bytes(b"v")
    (out / "branded").mkdir()
    (out / "branded" / "成片_916_logo_final.mp4").write_bytes(b"v")
    (out / "sub_916").mkdir()
    (out / "random_dir").mkdir()
    delete, keep = rs_cleanup.classify(root)
    del_names = [p.name for p in delete]
    keep_names = [p.name for p in keep]
    for must_keep in ("final", "branded", "sub_916"):
        assert must_keep in keep_names, f"{must_keep} 被列入删除(P10b-1):{del_names}"
    assert "random_dir" in del_names, "未登记目录照旧清理"


def test_p10b_render_routes_final_profile_and_brand_pins_out():
    """P10b-1 契约钉:rs_render 的 final 档默认落 06_output/final/ 并支持 --out 显式落点;
    rs_brand 经 --out 把变体成片钉进 --out 目录(预测路径=实际写盘)。"""
    src = (SCRIPTS / "rs_render.py").read_text(encoding="utf-8")
    assert 'base_dir / "06_output" / "final"' in src, "final 档必须落独占子目录"
    assert 'ap.add_argument("--out"' in src, "必须支持 --out 显式落点"
    brand = (SCRIPTS / "rs_brand.py").read_text(encoding="utf-8")
    assert '"--out", str(final)' in brand, "rs_brand 必须把预测路径显式传给 rs_render"


# ================================================================ §4.1 验收:--dirty 收敛 + 备份全程

def test_acceptance_dirty_converges_and_backs_up_every_writing_stage(tmp_path, monkeypatch):
    """§4.1 验收连跑两次 --dirty:第一次全部真跑(每个写盘阶段都有备份),
    第二次全部 cached(收敛);S4 无标记按 missing 纳入但作为人工阶段跳过。"""
    root = _mk_project(tmp_path)
    # 预铺各阶段产物(真实存在物,等价上游已跑完)
    (root / "01_materials" / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "05_ir" / "wordline.json").write_text("{}", encoding="utf-8")
    (root / "04_cut" / "cutlist.json").write_text("{}", encoding="utf-8")
    (root / "04_cut" / "cutlist.applied.json").write_text("{}", encoding="utf-8")
    (root / "05_ir" / "project.json").write_text("{}", encoding="utf-8")
    (root / "05_ir" / "sfx_draft.json").write_text("{}", encoding="utf-8")
    (root / "06_output" / "subtitles.ass").write_text("[Script Info]", encoding="utf-8")
    (root / "06_output" / "sync_report.md").write_text("# sync", encoding="utf-8")
    (root / "06_output" / "metadata.json").write_text("{}", encoding="utf-8")
    branded = root / "06_output" / "branded"
    branded.mkdir()
    (branded / "成片_916_logoA_final.mp4").write_bytes(b"brand")
    final = root / "06_output" / "final"
    final.mkdir()
    (final / "final_proj_916.mp4").write_bytes(b"render")

    monkeypatch.setattr(rs_run.subprocess, "run", _fake_run_ok)
    monkeypatch.setattr(rs_run, "run_verify",
                        lambda root, level: (True, "L0 通过", {"firstCheckDone": True}))
    monkeypatch.setattr(sys, "argv", ["rs_run.py", "--root", str(root), "--dirty"])
    code1, doc1 = _capture(rs_run.main)
    assert code1 == 0 and doc1["code"] == "RUN_OK", doc1
    ran = [r for r in doc1["data"]["results"] if not r.get("skipped")]
    assert len(ran) == 9, f"9 个非人工阶段都应真跑:{doc1['data']['results']}"
    assert not any(r.get("cached") for r in ran)
    # 级联备份:9 个写盘阶段的既有产物都进了备份快照
    bdir = root / "_state" / "backup"
    snaps = sorted(p for p in bdir.rglob("*") if p.is_file() and p.name != "_manifest.json")
    backed_names = {p.name for p in snaps}
    for must in ("wordline.json", "cutlist.json", "project.json", "sfx_draft.json",
                 "subtitles.ass", "成片_916_logoA_final.mp4", "final_proj_916.mp4",
                 "sync_report.md", "metadata.json"):
        assert must in backed_names, f"{must} 没有备份快照:{backed_names}"

    code2, doc2 = _capture(rs_run.main)
    assert code2 == 0 and doc2["code"] == "RUN_OK", doc2
    ran2 = [r for r in doc2["data"]["results"] if not r.get("skipped")]
    assert ran2 == [], "第二次 --dirty 不得再真跑任何阶段(收敛)"
    # --from 视角看同一事实:所有非人工阶段逐个标 cached(全 cached)
    monkeypatch.setattr(sys, "argv", ["rs_run.py", "--root", str(root), "--from", "S0"])
    code3, doc3 = _capture(rs_run.main)
    assert code3 == 0 and doc3["code"] == "RUN_OK", doc3
    entries = doc3["data"]["results"]
    assert sum(1 for r in entries if r.get("cached")) == 9, entries
    assert all(r.get("cached") or r.get("skipped") for r in entries)
