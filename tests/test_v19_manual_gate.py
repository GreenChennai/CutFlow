"""副文档 03 · 阶段三 §4.2/§4.3/§4.4 + 副文档 02 变更识别闭环 回归(P17–P25、O8、RT-2/3/4)。

P17-1  封面名统一 封面.png:rs_common.COVER_PNG 唯一常量,清理/交付清单/文档共用
P18-1  deliverables 真对账:缺封面/字幕/元数据 → DELIVERABLES_INCOMPLETE 非零退出
P19-1  手册命令 ↔ argparse 机械对拍门禁:SKILL/rules 每条 rs_* 命令都能被 argparse 接受
P20-1  rs_gate 无参 → PRECONDITION_FAILED 退出 2;里程碑口径三处文档统一「以 gate.py 注册表为准」
P21-1  doctor 桥探针提为 fatal;rs_artboard --probe 真检配置/目录/导出脚本
P22-1  timeline 回退 id 内容寻址(cf-…),不再数组下标派号;displayId 仅人读
P23-1  四桥错误码 ⊆ CutForge 5.4 码表(lib.rs CODES 机械对拍),表外码退役
P24-1  子进程 UTF-8 显式编码全覆盖;阶段子进程可配置超时且超时明确报错
P25-1  failed 状态实现:失败落盘、--status 可见、下游 blocked、--dirty 先修上游
RT-2   --status 读 .cutforge/session-summary.json(缺失静默跳过)
RT-3   rs_editor.py diff:session-summary 优先,否则 bases/ 基线对比,人话 + JSON
O8-1   BACKLOG 无「已落地仍挂 [ ]」条目(抽查关键条目)

运行:pytest tests/test_v19_manual_gate.py -q
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "tests"))

import check_manual_cmds as gate  # noqa: E402
import rs_cleanup  # noqa: E402
import rs_common  # noqa: E402
import rs_editor  # noqa: E402
import rs_ingest  # noqa: E402
import rs_run  # noqa: E402

CUTFORGE_LIB = Path(os.environ.get(
    "CUTFORGE_REPO", str(REPO.parent / "cutforge"))) / "cutforge-mcp" / "src" / "lib.rs"


def _capture(fn, *args, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    doc = json.loads(lines[-1]) if lines else {}
    return code, buf.getvalue(), doc


def _mk_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    for d in ("00_制作简报", "01_原始素材", "03_创作素材", "04_粗剪决策", "05_时间线工程", "06_成片输出", "_内部状态"):
        (root / d).mkdir(parents=True)
    (root / "01_原始素材" / "a.mp4").write_bytes(b"fake")
    return root


def _stage(sid: str) -> dict:
    return next(s for s in rs_run.spec() if s["id"] == sid)


# ================================================================ P17-1 封面名统一

def test_p17_cover_constant_is_single_source():
    assert rs_common.COVER_PNG == "封面.png"
    # 清理白名单派生自同一常量(封面 stem 前缀 + 精确名)
    assert rs_common.COVER_PNG.removesuffix(".png") in rs_cleanup.KEEP_PREFIX
    assert rs_common.COVER_PNG in rs_cleanup.OUT_KEEP_EXACT
    # 交付对账与 SKILL 文档口径一致
    ingest_src = (SCRIPTS / "rs_ingest.py").read_text(encoding="utf-8")
    assert "COVER_PNG" in ingest_src
    skill = (REPO / "skills/cutflow/SKILL.md").read_text(encoding="utf-8")
    assert "`封面.png`、`metadata.json`" in skill
    assert "`cover.png`" not in skill


def test_p17_cleanup_keeps_chinese_cover(tmp_path):
    root = tmp_path / "proj"
    (root / "06_成片输出").mkdir(parents=True)
    (root / "06_成片输出" / "封面.png").write_bytes(b"png")
    delete, keep = rs_cleanup.classify(root)
    assert not delete and any(p.name == "封面.png" for p in keep)


# ================================================================ P18-1 deliverables 真对账

def _complete_project(root: Path) -> None:
    out = root / "06_成片输出"
    (out / "final" / "final_a_916.mp4").parent.mkdir(parents=True, exist_ok=True)
    (out / "final" / "final_a_916.mp4").write_bytes(b"v")
    (out / "branded" / "成片_9x16_logoA_final.mp4").parent.mkdir(parents=True, exist_ok=True)
    (out / "branded" / "成片_9x16_logoA_final.mp4").write_bytes(b"v")
    for name in ("subtitles.ass", "master.srt", rs_common.COVER_PNG, "sync_report.md"):
        (out / name).write_text("x", encoding="utf-8")
    (out / "metadata.json").write_text(json.dumps(
        {"version": 1, "platforms": {"douyin": {"title": "t"}, "bili": {"title": "b"}}},
        ensure_ascii=False), encoding="utf-8")
    (root / "05_时间线工程" / "variants.json").write_text(json.dumps(
        {"matrix": [{"id": "logoA_9x16", "logo": "logoA", "ratio": "9x16"}]}, ensure_ascii=False),
        encoding="utf-8")


def test_p18_missing_cover_and_srt_must_fail(tmp_path, monkeypatch):
    """§4.2 验收判据:缺封面/缺字幕时 deliverables 非零。"""
    root = _mk_project(tmp_path)
    (root / "06_成片输出" / "final_a.mp4").write_bytes(b"v")
    res = rs_ingest.build_deliverables(root)
    assert res["ok"] is False and res["code"] == "DELIVERABLES_INCOMPLETE"
    missing = " ".join(res["missing"])
    assert rs_common.COVER_PNG in missing and "master.srt" in missing
    assert "subtitles.ass" in missing and "metadata.json" in missing
    # main() 契约:非零退出(4),清单文件照常生成并点名缺失项
    monkeypatch.setattr(sys, "argv", ["rs_ingest.py", "deliverables", str(root)])
    code, out, env = _capture(rs_ingest.main)
    assert code == 4 and env["ok"] is False
    d = (root / "06_成片输出" / "deliverables.md").read_text(encoding="utf-8")
    assert "缺失项" in d and "- ⚠" in d, "清单必须点名缺失项"


def test_p18_complete_project_passes(tmp_path):
    root = _mk_project(tmp_path)
    _complete_project(root)
    res = rs_ingest.build_deliverables(root)
    assert res["ok"] is True and res["code"] == "DELIVERABLES_OK", res["missing"]
    assert res["missing"] == []


def test_p18_variant_matrix_reconciled_against_files(tmp_path):
    """变体成片 ↔ variants.json 矩阵逐条对账:缺一条即不齐,并点名变体 id。"""
    root = _mk_project(tmp_path)
    _complete_project(root)
    vj = root / "05_时间线工程" / "variants.json"
    doc = json.loads(vj.read_text(encoding="utf-8"))
    doc["matrix"].append({"id": "logoB_16x9", "logo": "logoB", "ratio": "16x9"})
    vj.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    res = rs_ingest.build_deliverables(root)
    assert res["ok"] is False
    assert any("logoB_16x9" in m for m in res["missing"]), res["missing"]


# ================================================================ P19-1 手册命令对拍门禁

def test_p19_manual_gate_all_green():
    """§4.3 验收判据:SKILL/rules 中每条 rs_* 命令都能被对应 argparse 接受。"""
    rows, failed = gate.run_gate()
    assert len(rows) >= 100, f"对拍扫描覆盖异常:仅 {len(rows)} 条(扫描器坏了?)"
    bad = [r for r in rows if not r["ok"]]
    assert not bad, "手册命令对拍失败:" + ";".join(
        f"{r['doc']}:{r['line']} {r['script']} {' '.join(r['tokens'])}" for r in bad)


def test_p19_gate_rejects_the_original_p19_bug():
    """防复发自证:当初翻车的 `rs_notes.py tail` 必须仍被门禁拒绝。"""
    ok, note = gate.validate_one("rs_notes.py", ["tail", "P"])
    assert ok is False


def test_p19_gate_catches_missing_required_arg():
    ok, _ = gate.validate_one("rs_meta.py", ["--wordline", "P"])
    assert ok is False, "--out 是 rs_meta 必需参数,缺了必须被对拍抓住"


def test_p19_gate_ast_replay_uses_real_argparse(tmp_path):
    """重放器与脚本源码同源:choices/required 等约束必须生效。"""
    ok, _ = gate.validate_one("rs_ingest.py", ["frobnicate", "P"])
    assert ok is False, "choices 之外的子命令必须被拒"
    ok, _ = gate.validate_one("rs_verify.py", ["P", "--level", "L9"])
    assert ok is False, "choices 之外的取值必须被拒"


# ================================================================ P20-1 rs_gate 无参 / 口径统一

def test_p20_gate_without_milestone_exits_2(monkeypatch):
    import rs_gate
    monkeypatch.setattr(sys, "argv", ["rs_gate.py"])
    code, _, doc = _capture(rs_gate.main)
    assert code == 2
    assert doc["ok"] is False and doc["code"] == "PRECONDITION_FAILED"
    assert "gate.py 注册表" in doc["message"] and "M0–M7" in doc["message"]


def test_p20_milestone_range_unified_across_docs():
    needle = "gate.py 注册表"
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    skill = (REPO / "skills/cutflow/SKILL.md").read_text(encoding="utf-8")
    changelog = (REPO / "docs/CHANGELOG.md").read_text(encoding="utf-8")
    for name, text in (("README", readme), ("SKILL", skill), ("CHANGELOG", changelog)):
        assert needle in text, f"{name} 未统一「以 {needle} 为准」口径"
        assert "M0–M7" in text or "M0–M4" in text, name
    assert "M0…M3" not in readme, "README 旧的 M0…M3 口径还在"
    assert "`rs_gate.py M0 --json`" not in skill or "M0–M7" in skill


# ================================================================ P21-1 doctor fatal / artboard probe

def test_p21_artboard_probe_checks_config_dir_and_script(tmp_path, monkeypatch):
    import rs_artboard
    monkeypatch.setattr(sys, "argv", ["rs_artboard.py", "--probe"])
    monkeypatch.setattr(rs_artboard, "_load_artboard_config", lambda: {})
    code, _, doc = _capture(rs_artboard.main)
    assert code == 3 and doc["code"] == "DEP_MISSING" and "artboard_dir" in doc["message"]

    empty = tmp_path / "artboard"
    empty.mkdir()
    monkeypatch.setattr(rs_artboard, "_load_artboard_config",
                        lambda: {"artboard_dir": str(empty)})
    code, _, doc = _capture(rs_artboard.main)
    assert code == 3 and "导出脚本" in doc["message"]

    (empty / "scripts").mkdir()
    (empty / "scripts" / "export.py").write_text("# probe\n", encoding="utf-8")
    code, _, doc = _capture(rs_artboard.main)
    assert code == 0 and doc["ok"] is True and doc["data"]["bridge"] == "artboard"


def test_p21_doctor_bridge_probes_are_fatal():
    """四桥 + artboard 桥探针必须 fatal(桥断链不许再报「可后补」假绿)。"""
    src = (SCRIPTS / "rs_doctor.py").read_text(encoding="utf-8")
    for name in ("rs_editor.py", "rs_notes.py", "rs_oplog.py", "rs_gate.py", "rs_artboard.py"):
        assert f'"{name}"' in src, f"doctor 探针循环缺 {name}"
    assert "fatal=True" in src
    m = re.search(r'checks\.append\(_check\(f"桥探针:\{name\}", ok, detail, fatal=(\w+)', src)
    assert m and m.group(1) == "True", "桥探针未提为 fatal"


# ================================================================ P22-1 fallback id 内容寻址

def _ir_without_ids() -> dict:
    return {"version": 1, "tracks": [
        {"kind": "video", "name": "main", "clips": [
            {"src": "01_原始素材/a.mp4", "startMs": 0, "durationMs": 12000, "sourceInMs": 3000},
            {"src": "01_原始素材/a.mp4", "startMs": 12000, "durationMs": 8000, "sourceInMs": 15000},
        ]},
        {"kind": "video", "name": "overlay", "clips": [
            {"src": "03_创作素材/artboard/c1/export/c1.png", "startMs": 3200, "durationMs": 1800},
        ]},
    ]}


def test_p22_fallback_ids_are_content_addressed_and_anchorable():
    rows = rs_editor.timeline(_ir_without_ids())
    assert rows, "必须有行"
    for r in rows:
        assert r["id"] and r["id"] != "null", "不得输出空 id"
        assert r["id"].startswith("cf-") and r["idSource"] == "fallback"
        assert r["anchorable"] is True, "内容寻址 id 重排稳定,可作锚点"
        assert r["displayId"] in ("V1#1", "V1#2", "V2#1"), "displayId 仅人读标签"


def test_p22_ids_stable_under_reorder_not_subscript():
    """锚点契约核心:S3 重建后片段重排,id 必须跟着内容走而不是跟着下标走。"""
    ir = _ir_without_ids()
    before = {rs_editor.content_id(c) for t in ir["tracks"] for c in t["clips"]}
    ir["tracks"][0]["clips"].reverse()          # 重排
    after = {rs_editor.content_id(c) for t in ir["tracks"] for c in t["clips"]}
    assert before == after, "内容没变,重排不该换 id(数组下标派号做不到)"


def test_p22_native_ids_take_precedence():
    ir = {"tracks": [{"kind": "video", "clips": [
        {"id": "V1-001", "src": "a.mp4", "startMs": 0, "durationMs": 100}]}]}
    (row,) = rs_editor.timeline(ir)
    assert row["id"] == "V1-001" and row["idSource"] == "native" and row["anchorable"] is True


def test_p22_cutforge_bridge_smoke_stays_green():
    """同伴仓冒烟断言(id 非 null / idSource fallback→content / track 非空)不得被破坏。"""
    rows = rs_editor.timeline(_ir_without_ids())
    assert all(r.get("id") for r in rows)
    assert all(r.get("track") for r in rows)
    assert any(r.get("idSource") == "fallback" for r in rows)


# ================================================================ P23-1 桥错误码对拍

# 对拍范围:四桥 = CutForge 协议面(错误码 ⊆ lib.rs CODES);
# rs_artboard 的失败码走规格 §3 的另一条路:「显式把桥码登记进对拍」(见 _ARTBOARD_REGISTERED)。


def _failure_codes(script: Path) -> set[str]:
    """AST 收集 emit(False, "CODE"/die(..., "CODE") 的失败码(字面量)。"""
    tree = ast.parse(script.read_text(encoding="utf-8"))
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in ("emit", "die"):
            continue
        args = node.args
        if len(args) >= 2 and isinstance(args[0], ast.Constant) and args[0].value is False:
            if isinstance(args[1], ast.Constant) and isinstance(args[1].value, str):
                codes.add(args[1].value)
    return codes


def test_p23_bridge_failure_codes_subset_of_cutforge_table():
    """桥码与 CutForge 5.4 码表一致性机械对拍:失败码必须都在 lib.rs CODES 里。"""
    allowed = {"PRECONDITION_FAILED", "DEP_MISSING", "INTERNAL"}
    forge_codes: set[str] | None = None
    if CUTFORGE_LIB.is_file():
        m = re.search(r"CODES: &\[&str\] = &\[(.*?)\]", CUTFORGE_LIB.read_text(encoding="utf-8"),
                      re.S)
        assert m, "lib.rs CODES 表解析失败"
        forge_codes = set(re.findall(r'"([A-Z_]+)"', m.group(1)))
    for name in ("rs_editor.py", "rs_notes.py", "rs_oplog.py", "rs_gate.py"):
        codes = _failure_codes(SCRIPTS / name)
        assert codes, f"{name} 没收集到失败码(扫描器坏了?)"
        assert codes <= allowed, f"{name} 出现未登记的失败码: {codes - allowed}"
        if forge_codes is not None:
            assert codes <= forge_codes, f"{name} 失败码不在 CutForge 5.4 码表: {codes - forge_codes}"


_ARTBOARD_REGISTERED = {"NO_MANIFEST", "NO_IR", "NO_ARTBOARD", "NO_ACTION", "APPLY_ISSUES",
                        "DEP_MISSING",  # DEP_MISSING 来自 --probe(P21-1)
                        "NO_PLAN", "BAD_PLAN", "CARD_EXISTS",  # T1-1 gen-cards(副文档 05)
                        # M7 gen-frames:计划无场景卡 / --kind 非法 / 安全区机检未过 / MP4 导出失败
                        "NO_FRAMES", "BAD_KIND", "SAFE_CHECK_FAILED", "EXPORT_FAILED"}


def test_p23_artboard_codes_registered_no_drift():
    """rs_artboard 走规格 §3 另一条路:「显式把桥码登记进对拍」—— 集合严格相等防漂移。"""
    codes = _failure_codes(SCRIPTS / "rs_artboard.py")
    assert codes == _ARTBOARD_REGISTERED, f"rs_artboard 失败码漂移: {codes ^ _ARTBOARD_REGISTERED}"


def test_p23_legacy_bridge_codes_retired():
    for name in ("rs_editor.py", "rs_notes.py", "rs_oplog.py", "rs_gate.py"):
        codes = _failure_codes(SCRIPTS / name)
        assert not ({"USAGE", "NO_ENV"} & codes), f"{name} 仍在用 5.4 表外码"


# ================================================================ P24-1 编码与超时

def _py_files() -> list[Path]:
    out = []
    for base in ("skills/cutflow/scripts", "tools", "tests"):
        for p in (REPO / base).glob("*.py"):
            if "__pycache__" not in p.parts:
                out.append(p)
    return sorted(out)


def test_p24_no_bare_text_true_without_encoding():
    """全仓排查:subprocess 捕获文本必须显式 UTF-8(简体 Windows 失败信息不乱码)。"""
    import io as _io
    import tokenize as _tok

    offenders = []
    # PEP 701(3.12+):f-string 拆成 FSTRING_* token,同样按字符串剔除
    fstr = {getattr(_tok, n) for n in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
            if hasattr(_tok, n)}
    for p in _py_files():
        # tokenize 剥掉注释/字符串/文档串,只扫真实代码 —— 文档与注解里的提法不误报
        bare = []
        try:
            for tk in _tok.generate_tokens(_io.StringIO(p.read_text(encoding="utf-8")).readline):
                if tk.type in (_tok.COMMENT, _tok.STRING) or tk.type in fstr:
                    continue
                bare.append(tk.string)
        except _tok.TokenError:
            continue
        code = "\n".join(bare)
        for m in re.finditer(r"text=True", code):
            window = code[m.start():m.start() + 200]
            if "encoding=" not in window.split(")")[0]:
                offenders.append(str(p.relative_to(REPO)))
    assert not offenders, f"裸 text=True(缺 encoding): {offenders}"


def test_p24_stage_timeout_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("CUTFLOW_STAGE_TIMEOUT_SEC", raising=False)
    assert rs_run.stage_timeout(_stage("S7")) == rs_run.DEFAULT_STAGE_TIMEOUT_SEC
    assert rs_run.stage_timeout(_stage("S1")) > rs_run.DEFAULT_STAGE_TIMEOUT_SEC, \
        "长 ASR 阶段必须按阶段放宽"
    monkeypatch.setenv("CUTFLOW_STAGE_TIMEOUT_SEC", "120")
    assert rs_run.stage_timeout(_stage("S1")) == 120, "环境变量统一调大/调小"


def test_p24_timeout_is_loud_not_silent(tmp_path, monkeypatch):
    root = _mk_project(tmp_path)
    monkeypatch.setenv("CUTFLOW_STAGE_TIMEOUT_SEC", "1")
    proc, err = rs_run._run_subprocess(
        [sys.executable, "-c", "import time; time.sleep(30)"], root, _stage("S7"))
    assert proc is None
    assert "超时" in err and "CUTFLOW_STAGE_TIMEOUT_SEC" in err


# ================================================================ P25-1 failed 状态

def test_p25_stage_failure_records_failed_blocks_downstream(tmp_path):
    """failed 状态「实现它」:S0 跑失败 → 落盘 failed → --status 可见 → 下游 blocked →
    --dirty 本轮只选失败上游,不拿坏账硬跑下游。"""
    root = _mk_project(tmp_path)
    (root / "01_原始素材" / "a.mp4").unlink()
    (root / "01_原始素材").rmdir()                     # 让 S0 真实失败(NO_MATERIALS)
    ok, msg = rs_run.run_stage(root, _stage("S0"), {})
    assert ok is False
    rec = rs_run.read_state(root, "S0")
    assert rec and rec["status"] == "failed" and rec.get("error")
    assert rs_run.evaluate(root, _stage("S0"))["status"] == "failed"
    # --status:失败可见 + 下游 blocked
    code, out, doc = _capture(rs_run.cmd_status, root)
    assert "failed" in out and "blocked" in out
    statuses = {d["id"]: d["status"] for d in doc["data"]["stages"]}
    assert statuses["S0"] == "failed"
    assert statuses["S1"] == "blocked" and statuses["S8"] == "blocked"
    # --dirty 只选失败上游(blocked 不进本轮)
    picked = [s["id"] for s in rs_run.select(root, rs_run.S13, None)]
    assert "S0" in picked and "S1" not in picked
    # pipeline.json 汇总账同样如实
    agg = json.loads((root / "05_时间线工程" / "pipeline.json").read_text(encoding="utf-8"))
    assert agg["stages"]["S0"]["status"] == "failed"


def test_p25_failed_clears_after_successful_rerun(tmp_path):
    root = _mk_project(tmp_path)
    (root / "06_成片输出" / "subtitles.ass").write_text("[Events]", encoding="utf-8")
    rs_run.write_state(root, "S7", {"status": "failed", "error": "上次炸了"})
    assert rs_run.evaluate(root, _stage("S7"))["status"] == "failed"
    parts = rs_run.stage_parts(root, _stage("S7"), rs_run.params_of(root), {})
    rs_run.write_state(root, "S7", {"status": "done", "key": rs_run.key_of(parts),
                                    "parts": parts, "outHash": rs_run.outputs_hash(root, _stage("S7"))})
    assert rs_run.evaluate(root, _stage("S7"))["status"] in ("done", "stale")


# ================================================================ RT-2 --status 读会话摘要

def _write_summary(root: Path, ops: list[dict], rev=(2, 4)) -> None:
    cf = root / ".cutforge"
    cf.mkdir(parents=True, exist_ok=True)
    (cf / "session-summary.json").write_text(json.dumps({
        "kind": "cutforge-session-summary", "startedAt": "t0", "updatedAt": "t1",
        "revFrom": rev[0], "revTo": rev[1], "userOpCount": len(ops), "ops": ops,
    }, ensure_ascii=False), encoding="utf-8")


def test_rt2_status_surfaces_editor_session(tmp_path):
    root = _mk_project(tmp_path)
    _write_summary(root, [{"opKind": "set", "target": {"file": "project.json",
                                                       "path": "/tracks/0/clips/0/durationMs"},
                           "before": 100, "after": 200, "summary": "改了时长"}])
    code, out, doc = _capture(rs_run.cmd_status, root)
    assert "编辑器会话" in out and "rs_editor.py diff" in out
    assert doc["data"]["editorSession"]["revFrom"] == 2
    assert doc["data"]["editorSession"]["userOpCount"] == 1


def test_rt2_status_silent_without_summary(tmp_path):
    root = _mk_project(tmp_path)
    code, out, doc = _capture(rs_run.cmd_status, root)
    assert "editorSession" not in doc["data"]
    assert "编辑器会话" not in out
    # 损坏/非摘要文件同样静默跳过(绝不因它崩)
    cf = root / ".cutforge"
    cf.mkdir()
    (cf / "session-summary.json").write_text("{broken", encoding="utf-8")
    code, out, doc = _capture(rs_run.cmd_status, root)
    assert "editorSession" not in doc["data"]


# ================================================================ RT-3 rs_editor diff

def test_rt3_diff_prefers_session_summary(tmp_path, capsys):
    root = _mk_project(tmp_path)
    (root / "05_时间线工程" / "project.json").write_text("{}", encoding="utf-8")
    _write_summary(root, [
        {"opKind": "set", "target": {"file": "project.json",
                                     "path": "/tracks/0/clips/1/durationMs"},
         "before": 8000, "after": 9500, "summary": "延长第2段"}], rev=(3, 5))
    code, _, doc = _capture(rs_editor.cmd_diff, root)
    assert code == 0 and doc["code"] == "DIFF_SESSION"
    assert doc["data"]["source"] == "session-summary"
    assert "第2" in doc["data"]["human"][0] and "8000 → 9500" in doc["data"]["human"][0]


def test_rt3_diff_falls_back_to_base_snapshot(tmp_path):
    """无摘要时拿 .cutforge/bases/ 最新 rev 快照对比 —— 人话差异 + 机器可读 JSON。"""
    root = _mk_project(tmp_path)
    proj = _ir_without_ids()
    (root / "05_时间线工程" / "project.json").write_text(
        json.dumps(proj, ensure_ascii=False), encoding="utf-8")
    bases = root / ".cutforge" / "bases"
    bases.mkdir(parents=True)
    old = json.loads(json.dumps(proj))
    old["tracks"][1]["clips"] = []                      # V2 的 overlay 卡是后来加的
    (bases / "7.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    code, _, doc = _capture(rs_editor.cmd_diff, root)
    assert code == 0 and doc["code"] == "DIFF_BASE" and doc["data"]["baseRev"] == 7
    human = "\n".join(doc["data"]["human"])
    assert "overlay 卡" in human and "新增" in human, human
    assert doc["data"]["diff"]["added"], human


def test_rt3_diff_reports_removed_and_changed(tmp_path):
    root = _mk_project(tmp_path)
    old = _ir_without_ids()
    new = json.loads(json.dumps(old))
    removed = new["tracks"][0]["clips"].pop(0)          # V1 第1段被删
    new["tracks"][0]["clips"][0]["durationMs"] = 9999   # 第(原2)段被改时长
    (root / "05_时间线工程" / "project.json").write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    bases = root / ".cutforge" / "bases"
    bases.mkdir(parents=True)
    (bases / "12.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    code, _, doc = _capture(rs_editor.cmd_diff, root)
    human = "\n".join(doc["data"]["human"])
    assert "被删" in human
    assert "durationMs" in human and "9999" in human
    assert "8000" in json.dumps(doc["data"]["diff"], ensure_ascii=False)
    assert removed["src"]


def test_rt3_diff_without_sources_is_clean_not_crash(tmp_path):
    root = _mk_project(tmp_path)
    (root / "05_时间线工程" / "project.json").write_text("{}", encoding="utf-8")
    code, _, doc = _capture(rs_editor.cmd_diff, root)
    assert code == 0 and doc["code"] == "NO_BASELINE"


def test_rt3_diff_missing_ir_is_dep_missing(tmp_path):
    root = _mk_project(tmp_path)
    code, _, doc = _capture(rs_editor.cmd_diff, root)
    assert code == 3 and doc["code"] == "DEP_MISSING"


def test_rt3_diff_registered_in_skill_and_editor_choices():
    skill = (REPO / "skills/cutflow/SKILL.md").read_text(encoding="utf-8")
    assert "`rs_editor.py diff <工程>`" in skill
    assert "变更识别闭环" in skill


# ================================================================ O8-1 BACKLOG 抽查

def test_o8_backlog_no_delivered_items_left_open():
    text = (REPO / "docs/BACKLOG.md").read_text(encoding="utf-8")
    delivered = [
        "补 `rs_ingest`(S0)与 `deliverables.md` 生成",
        "seg 清单做 seg 级缓存(B3)",
        "「卡片 ↔ 动画卡时间窗」重叠检查(BACKLOG 原条目的自动化版)",
        "支持动画卡 `--fps`",
    ]
    for needle in delivered:
        for line in text.splitlines():
            if needle in line:
                assert line.strip().startswith("- [x]"), f"已落地仍挂 [ ]: {line.strip()[:80]}"
                break
        else:
            assert False, f"BACKLOG 找不到条目: {needle}"


# ================================================================ 对拍门禁防复发(汇总线)

def test_gate_script_is_referenced_from_rules():
    inc = (REPO / "skills/cutflow/rules/incremental.md").read_text(encoding="utf-8")
    assert "check_manual_cmds.py" in inc, "对拍门禁必须写进文档(rules/incremental.md §8)"
