"""CutFlow v5 回归测试:自带 ASR / 验证分级 / 一键重建与备份 / artboard 闭环 / 省 Token 纪律。

对应 docs/OPTIMIZATION-v5.md §8 验收线。
运行:pytest tests/ -q
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
TOOLS = REPO / "tools"
sys.path.insert(0, str(SCRIPTS))

import rs_align  # noqa: E402
import rs_artboard  # noqa: E402
import rs_run  # noqa: E402
import rs_subtitle as rsub  # noqa: E402
import rs_verify  # noqa: E402


# ---------------------------------------------------------------- R1 自带 ASR

def _load_fun_asr():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fun_asr", TOOLS / "fun_asr.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fun_asr_declares_backend_capabilities():
    """能力矩阵必须显式声明:onnx 后端没有字级时间戳(实测结论,不可含糊)。"""
    fa = _load_fun_asr()
    assert fa.CAPS["onnx"]["charTimestamps"] is False
    assert fa.CAPS["pkg"]["charTimestamps"] is True
    assert set(fa.BACKENDS) == {"pkg", "onnx", "server"}


def test_fun_asr_backend_order_prefers_accuracy():
    """auto 的选择顺序必须是 精度(pkg) → 轻量(onnx) → 兜底(server)。"""
    src = (TOOLS / "fun_asr.py").read_text(encoding="utf-8")
    assert 'for name in ("pkg", "onnx", "server")' in src


def test_fun_asr_vad_threshold_is_tightened():
    """VAD 阈值必须比模型默认(800)紧,否则段长 15s+,段内均分误差大。"""
    fa = _load_fun_asr()
    assert fa.VAD_MAX_END_SIL <= 500, "默认 VAD 静音阈值应收紧到能给出句级粒度"


def test_fun_asr_reports_degraded_for_onnx():
    src = (TOOLS / "fun_asr.py").read_text(encoding="utf-8")
    assert "degradeReasons" in src and "charTimestamps" in src


def test_pkg_backend_never_feeds_onnx_dir_to_torch(tmp_path, monkeypatch):
    """pkg(torch 引擎)只认 model.pt/model.pb;ONNX 导出目录必须回落 modelscope 短名。

    实测背景:本地播种的 paraformer-large 只有 model_quant.onnx —— 喂给 torch 引擎必报错。
    """
    fa = _load_fun_asr()
    md = tmp_path / "funasr"
    (md / "paraformer-large").mkdir(parents=True)
    (md / "paraformer-large" / "model_quant.onnx").write_bytes(b"x")
    monkeypatch.setattr(fa, "models_dir", lambda: md)
    assert fa.pick("paraformer-large") == "paraformer-zh", "ONNX 目录不能喂给 torch 引擎"
    (md / "paraformer-large" / "model.pb").write_bytes(b"x")
    assert fa.pick("paraformer-large") == str(md / "paraformer-large"), "有 torch 权重就该用本地"
    assert fa.pick("fsmn-vad") == "fsmn-vad" and fa.pick("ct-punc") == "ct-punc"


def test_rs_align_calls_bundled_asr_not_http():
    """rs_align 必须调自带运行器,不得再直接请求外部服务器。"""
    src = (SCRIPTS / "rs_align.py").read_text(encoding="utf-8")
    assert "fun_asr.py" in src
    assert "urllib.request" not in src.split("def _from_media")[1].split("def ")[0]


def test_vendor_notice_and_license_kept():
    notice = TOOLS / "asr_vendor" / "NOTICE.md"
    assert notice.is_file(), "vendored 代码必须带 NOTICE(许可与出处)"
    text = notice.read_text(encoding="utf-8")
    assert "MIT" in text and "FunASR" in text
    assert (TOOLS / "asr_vendor" / "paraformer_bin.py").is_file()


# ---------------------------------------------------------------- R2 验证分级

def _mk_project(tmp_path: Path, with_ass: bool = True) -> Path:
    root = tmp_path / "proj"
    (root / "05_ir").mkdir(parents=True)
    (root / "06_output").mkdir(parents=True)
    wl = rs_align.build_wordline(
        [{"start": 0.0, "end": 3.0, "text": "大家好,今天我们来讲桌面运维。"},
         {"start": 3.4, "end": 6.2, "text": "先看蓝屏,蓝屏是最常见的问题。"}], "a.mp4")
    (root / "05_ir" / "wordline.json").write_text(json.dumps(wl, ensure_ascii=False),
                                                  encoding="utf-8")
    if with_ass:
        events, _ = rsub.events_from_wordline(wl, 12)
        (root / "06_output").mkdir(exist_ok=True)
        rsub.write_ass(events, root / "06_output" / "subtitles.ass", "subtitle-white",
                       "9x16", "1080x1920")
    return root


def test_verify_l0_passes_on_clean_project(tmp_path):
    root = _mk_project(tmp_path)
    res = rs_verify.collect_l0(root)
    assert res["pass"] is True, res["failed"]
    assert res["level"] == "L0"


def test_verify_l0_catches_broken_subtitles(tmp_path):
    root = _mk_project(tmp_path, with_ass=False)
    res = rs_verify.collect_l0(root)
    assert res["pass"] is False
    assert any("字幕" in x for x in res["failed"])


def test_verify_l1_is_review_manifest_not_autojudgement():
    l1 = rs_verify.l1_payload(REPO)
    assert l1["needsAgentReview"] is True
    assert l1["checklist"] and "判定权" in l1["note"]


def test_verify_state_roundtrip(tmp_path):
    root = _mk_project(tmp_path)
    assert rs_verify.first_check_done(root) is False
    st = rs_verify.load_verify(root)
    st["firstCheck"] = {"done": True, "at": rs_verify.now(), "level": "L1", "result": "pass"}
    rs_verify.save_verify(root, st)
    assert rs_verify.first_check_done(root) is True


def test_verify_policy_first_then_l0(tmp_path):
    root = _mk_project(tmp_path)
    level, why = rs_run.verify_policy(root)
    assert level == "L1" and "首次" in why          # 首次 → L1
    st = rs_verify.load_verify(root)
    st["firstCheck"] = {"done": True}
    rs_verify.save_verify(root, st)
    level, why = rs_run.verify_policy(root)
    assert level == "L0", why                        # 非首次且画面未变 → 只 L0


def test_picture_change_forces_l1(tmp_path):
    root = _mk_project(tmp_path)
    st = rs_verify.load_verify(root)
    st["firstCheck"] = {"done": True}
    rs_verify.save_verify(root, st)
    # 画面阶段"曾经做过、现在失效"→ 必须提示 L1(单一真相在 _state/S*.json)
    state = root / "_state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "S4.json").write_text(json.dumps(
        {"status": "done", "key": "old", "parts": {"inputs": {"a": "1"}, "tool": {},
                                                   "params": {}, "external": {}}}), encoding="utf-8")
    level, why = rs_run.verify_policy(root)
    assert level == "L1" and "S4" in why
    # 只是"没跑过"(从未记录)不算画面变更
    (state / "S4.json").unlink()
    level, why = rs_run.verify_policy(root)
    assert level == "L0", why
    assert rs_verify.picture_changed(root) == []


def test_verify_output_contract_fields():
    """输出契约:任何交付输出必须携带 verifyLevel 与 firstCheckDone。"""
    src = (SCRIPTS / "rs_run.py").read_text(encoding="utf-8")
    assert '"verifyLevel": level' in src and '"firstCheckDone": first_done' in src


# ---------------------------------------------------------------- R3 省 Token 纪律

def test_skill_md_has_router_covering_all_rules():
    skill = (REPO / "skills/cutflow/SKILL.md").read_text(encoding="utf-8")
    rules = sorted(p.name for p in (REPO / "skills/cutflow/rules").glob("*.md"))
    missing = [r for r in rules if r not in skill]
    assert not missing, f"路由表/索引未覆盖:{missing}"


def test_skill_md_has_who_does_what_table():
    skill = (REPO / "skills/cutflow/SKILL.md").read_text(encoding="utf-8")
    assert "谁来做" in skill and "只读这一个" in skill
    assert "一次读两个以上规则文件" in skill, "必须有『只读一个文件』的硬纪律"


def test_skill_md_size_discipline():
    skill = (REPO / "skills/cutflow/SKILL.md").read_text(encoding="utf-8")
    assert len(skill.splitlines()) <= 270, "SKILL.md 过胖;细节应下沉到 rules/"


# ---------------------------------------------------------------- R4 一键重建

def test_init_rebuild_generates_scripts(tmp_path):
    root = _mk_project(tmp_path)
    made = rs_run.init_rebuild(root)
    assert "06_output/rebuild.py" in made and "05_ir/rebuild.py" in made
    assert (root / "REBUILD.md").is_file()
    # 06_output 的脚本必须从"烧录导出"段起跑,否则会冲掉手改字幕
    body = (root / "06_output" / "rebuild.py").read_text(encoding="utf-8")
    assert '"--from", "S8"' in body and "--force" in body


def test_artboard_rebuild_is_specialized(tmp_path):
    root = _mk_project(tmp_path)
    (root / "03_assets" / "artboard").mkdir(parents=True)
    rs_run.init_rebuild(root)
    body = (root / "03_assets" / "artboard" / "rebuild.py").read_text(encoding="utf-8")
    assert "rs_artboard.py" in body and "--export" in body and "--apply" in body


def test_backup_and_rollback_roundtrip(tmp_path):
    root = _mk_project(tmp_path)
    ass = root / "06_output" / "subtitles.ass"
    original = ass.read_text(encoding="utf-8")
    st = next(s for s in rs_run.spec() if s["id"] == "S7")
    bp = rs_run.backup_paths(root, st)
    assert bp is not None and bp.is_dir()
    ass.write_text("被改坏了", encoding="utf-8")
    ok, msg = rs_run.rollback(root)
    assert ok, msg
    assert ass.read_text(encoding="utf-8") == original


def test_backup_prunes_to_keep_limit(tmp_path):
    root = _mk_project(tmp_path)
    st = next(s for s in rs_run.spec() if s["id"] == "S7")
    bdir = root / "_state" / "backup"
    for i in range(8):
        d = bdir / f"2026010{i}-000000"
        d.mkdir(parents=True)
        (d / "x.txt").write_text("x", encoding="utf-8")
    rs_run.backup_paths(root, st)
    left = [d for d in bdir.iterdir() if d.is_dir()]
    assert len(left) <= rs_run.BACKUP_KEEP, left


def test_force_only_applies_to_start_stage():
    src = (SCRIPTS / "rs_run.py").read_text(encoding="utf-8")
    assert 'forced = (forced_id is not None and st["id"] == forced_id)' in src


def test_s8_is_burn_and_export_not_regenerate():
    s8 = next(s for s in rs_run.spec() if s["id"] == "S8")
    assert s8["cmd"][0] == "rs_render.py"
    assert all("rs_subtitle" not in x for x in s8["cmd"])


# ---------------------------------------------------------------- R5 artboard 闭环

def test_artboard_hash_detects_source_change(tmp_path):
    proj = tmp_path / "card" / "src"
    proj.mkdir(parents=True)
    (proj / "index.html").write_text("<h1>a</h1>", encoding="utf-8")
    h1 = rs_artboard.hash_source(proj)
    (proj / "index.html").write_text("<h1>b</h1>", encoding="utf-8")
    assert rs_artboard.hash_source(proj) != h1


def test_artboard_scan_finds_cards(tmp_path):
    (tmp_path / "card_a" / "src").mkdir(parents=True)
    (tmp_path / "card_a" / "src" / "index.html").write_text("x", encoding="utf-8")
    doc = rs_artboard.scan(tmp_path)
    assert len(doc["items"]) == 1 and doc["items"][0]["id"] == "card_a"


def test_artboard_apply_rejects_size_mismatch(tmp_path):
    root = _mk_project(tmp_path)
    out = root / "03_assets" / "artboard" / "c1" / "export" / "c1.png"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"png")
    doc = {"version": 1, "items": [{"id": "c1", "project": "03_assets/artboard/c1/src",
                                    "output": "03_assets/artboard/c1/export/c1.png",
                                    "kind": "png", "size": [1920, 1080]}]}
    ir = {"canvas": {"width": 1080, "height": 1920},
          "tracks": [{"kind": "video", "clips": [
              {"src": "03_assets/artboard/c1/export/c1.png", "startMs": 0, "durationMs": 2000}]}]}
    _, issues, _ = rs_artboard.apply_to_ir(doc, ir, root)
    assert any("尺寸" in i for i in issues), issues


def test_artboard_duration_change_shifts_downstream_and_marks_stale():
    clips = [{"startMs": 0, "durationMs": 3000}, {"startMs": 3000, "durationMs": 2000}]
    n = rs_artboard.shift_track(clips, 0, 2000)
    assert n == 1 and clips[1]["startMs"] == 5000
    assert "S7" in rs_artboard.stale_stages([{"oldDurationMs": 3000}])
    assert rs_artboard.stale_stages([{"path": "x"}]) == ["S4", "S5", "S8"]


# ---------------------------------------------------------------- 文档防漂移

def test_skill_referenced_scripts_exist():
    import re
    skill = (REPO / "skills/cutflow/SKILL.md").read_text(encoding="utf-8")
    for name in sorted(set(re.findall(r"(rs_[a-z_]+\.py|segmentation\.py|fun_asr\.py)", skill))):
        hit = (SCRIPTS / name).is_file() or (TOOLS / name).is_file()
        assert hit, f"SKILL.md 引用了不存在的脚本:{name}"
