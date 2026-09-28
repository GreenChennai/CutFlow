# -*- coding: utf-8 -*-
"""第五册 T5.1/T5.2/T5.7:成片及格线评分卡 + 及格线门禁 + 门禁分层诚实。

判据(docs/QC-SCORECARD.md 是维表文档单一出处):
  [机] 五维 100 分制可算:机械可判满分 91 + 人工签核 9;健康样例 ≥75;
  [机] 及格线门禁:构造低分样例,`rs_verify --score` 非零退出(SCORE_FAIL)并指出低分维;
  [机] 判据缺席(无成片等)→ pending 计账,缺席不冒充满分(已判满分 < 机械满分);
  [机] T5.7:collect_l0 每条结论带 scope ∈ mechanical/content;文本报告与
       CLI 消息不得出现无 scope 限定的「通过」(正则扫描);
  [机] T5.6a:B10 内容闸三态归一(pass/fail/degraded/absent),降级显式留痕;
  [机] T5.6b:rs_run._bench_cmd 在 shots.json 在册时走价值选帧档(--shots)。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_align  # noqa: E402
import rs_codes  # noqa: E402
import rs_run  # noqa: E402
import rs_sync  # noqa: E402
import rs_verify  # noqa: E402

# ---------------------------------------------------------------- fixture 构造(小工程,零真实渲染)


def _ass_time(ms: int) -> str:
    s = ms / 1000.0
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def _write_ass(root: Path, final: dict, shift_ms: int = 0) -> None:
    """从 final wordline 出 ASS(逐句一卡,-20/+20ms 提示余量;shift_ms 构造错位样例)。"""
    lines = ["[Script Info]", "ScriptType: v4.00+", "", "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for sent in final["sentences"]:
        a = final["chars"][sent["span"][0]]
        b = final["chars"][sent["span"][1] - 1]
        lines.append(f"Dialogue: 0,{_ass_time(a['startMs'] - 20 + shift_ms)},"
                     f"{_ass_time(b['endMs'] + 20 + shift_ms)},,,0,0,0,,{sent['text']}")
    (root / "06_成片输出" / "subtitles.ass").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mk_project(tmp: Path, slug: str = "评分卡工程") -> Path:
    """健康小工程:源 wordline + cutlist + 重放一致的 final + 与 final 一致的 ass + 交付件。

    无真实成片(ffprobe 解不了的占位 mp4)→ 成片实测类判据显式降级留痕(pending),
    机械可判满分 91 中已判 76,健康样例应 ≥75。
    """
    root = tmp / slug
    for d in ("00_制作简报", "01_原始素材", "04_粗剪决策", "05_时间线工程", "06_成片输出", "成品"):
        (root / d).mkdir(parents=True)
    text = "今天讲下店群运营的关键点还有选品逻辑"          # 18 内容字,两句各 ≤12 字
    chars = [{"i": i, "ch": ch, "startMs": 300 * i, "endMs": 300 * i + 250,
              "srcStartMs": 300 * i, "srcEndMs": 300 * i + 250} for i, ch in enumerate(text)]
    src = {"source": "a.mp4", "chars": chars, "space": "source",
           "srcDurationMs": 300 * len(text) + 600,
           "sentences": [{"id": 0, "span": [0, 10], "text": text[:10]},
                         {"id": 1, "span": [10, 18], "text": text[10:]}]}
    keep = [[0, 300 * 9], [300 * 10, 300 * len(text) + 600]]   # 剪 2.7s-3.0s 气口
    cl = {"source": "a.mp4", "keep": keep, "srcTotalMs": 300 * len(text) + 600,
          "cuts": [], "removedMs": 300}
    (root / "04_粗剪决策" / "cutlist.applied.json").write_text(
        json.dumps(cl, ensure_ascii=False), encoding="utf-8")
    (root / "05_时间线工程" / "wordline.json").write_text(
        json.dumps(src, ensure_ascii=False), encoding="utf-8")
    final = rs_align.remap_wordline(src, keep, cl)
    (root / "05_时间线工程" / "wordline.final.json").write_text(
        json.dumps(final, ensure_ascii=False), encoding="utf-8")
    _write_ass(root, final)
    (root / "06_成片输出" / "final").mkdir()
    (root / "06_成片输出" / "final" / "final_x_916.mp4").write_bytes(b"not a real video")
    (root / "06_成片输出" / "deliverables.md").write_text("# 交付清单", encoding="utf-8")
    (root / "06_成片输出" / "决策说明书.md").write_text("# 决策说明书", encoding="utf-8")
    (root / "成品" / "说明书").mkdir()
    (root / "成品" / "说明书" / "素材归因.md").write_text("# 素材归因", encoding="utf-8")
    return root


def _run_verify(root: Path, *extra: str) -> tuple[int, dict]:
    p = subprocess.run([sys.executable, str(SCRIPTS / "rs_verify.py"), str(root), *extra],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
    assert lines, f"rs_verify 无输出:{(p.stderr or '')[-300:]}"
    return p.returncode, json.loads(lines[-1])


# ---------------------------------------------------------------- T5.7 scope 分层


def test_collect_l0_every_check_has_scope(tmp_path):
    """T5.7:每条 L0 结论必须带 scope ∈ {mechanical, content};分类计数守恒。"""
    root = _mk_project(tmp_path)
    res = rs_verify.collect_l0(root)
    assert res["checks"], "L0 判据不应为空"
    for c in res["checks"]:
        assert c.get("scope") in (rs_verify.SCOPE_MECHANICAL, rs_verify.SCOPE_CONTENT), \
            f"结论缺 scope:{c['name']}"
    assert res["scopeBreakdown"]["mechanical"] + res["scopeBreakdown"]["content"] == len(res["checks"])
    # 成片实测判据(独立对账成片路/成片体检/安全区内容占位/内容闸回读)必须标 content
    for c in res["checks"]:
        if any(k in c["name"] for k in ("语音活动独立对账", "成片体检", "安全区内容占位", "内容闸状态回读")):
            assert c["scope"] == rs_verify.SCOPE_CONTENT, c["name"]


def test_report_marks_carry_scope(tmp_path):
    """T5.7:文本报告里任何 ✓/✗ 都必须带 [机械]/[实测];总判定必须是「机械闸(L0)」;
    不得出现无 scope 限定的「判定:通过」。"""
    root = _mk_project(tmp_path)
    res = rs_verify.collect_l0(root)
    out = root / "06_成片输出"
    rs_verify.write_report(res, out / "verify_report.md")
    md = (out / "verify_report.md").read_text(encoding="utf-8")
    bad = [l for l in md.splitlines()
           if re.search(r"[✓✗]", l) and not re.search(r"\[(机械|实测|人工)\]", l)]
    assert not bad, f"存在无 scope 限定的 ✓/✗ 行:{bad[:3]}"
    verdicts = [l for l in md.splitlines() if l.startswith("- 判定:")]
    assert verdicts and all("机械闸" in v for v in verdicts), verdicts
    assert not any(re.search(r"判定:\*\*通过\*\*", v) for v in verdicts), \
        "禁止无 scope 限定的「判定:通过」"


def test_cli_message_says_mechanical_gate(tmp_path):
    """T5.7:CLI 消息必须是「机械闸通过/未通过(L0)」,不许裸「L0 通过」。"""
    root = _mk_project(tmp_path)
    rc, doc = _run_verify(root)
    msg = doc.get("message", "")
    assert msg.startswith("机械闸"), msg
    assert "(L0" in msg, msg


# ---------------------------------------------------------------- T5.1 五维算分


def test_score_dims_weights_match_doc():
    """维表契约:五维满分合计 100 = 机械 91 + 人工 9;权重与 docs/QC-SCORECARD.md 一致。"""
    expect = {"D1": (35, 0), "D2": (20, 0), "D3": (14, 6), "D4": (12, 3), "D5": (10, 0)}
    root = None  # 权重断言不依赖盘面:用空工程算结构即可
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="qc_weights_")) / "w"
    root.mkdir(parents=True)
    score = rs_verify.build_scorecard(root)
    assert [d["id"] for d in score["dims"]] == ["D1", "D2", "D3", "D4", "D5"]
    for d in score["dims"]:
        mech, human = expect[d["id"]]
        assert d["maxMech"] == mech, (d["id"], d["maxMech"])
        assert d["humanPoints"] == human, (d["id"], d["humanPoints"])
    assert score["mechFull"] == 91 and score["humanPoints"] == 9
    assert score["passLine"] == 75


def test_scorecard_healthy_project_passes(tmp_path):
    """T5.1:健康样例机械可判部分全部得分(76/76),达到及格线。"""
    root = _mk_project(tmp_path)
    res = rs_verify.collect_l0(root)
    assert res["pass"], res["failed"]
    score = rs_verify.build_scorecard(root, l0=res)
    assert score["score"] == score["judgedMax"], "健康样例不应失分"
    assert score["score"] >= score["passLine"] and score["pass"]
    # 判据缺席的 15 分(成片实测类降级)必须显式 pending,不许冒充满分
    assert score["judgedMax"] < score["mechFull"]
    assert score["pendingPoints"] == score["mechFull"] - score["judgedMax"]
    assert score["pending"], "缺席判据必须逐项留痕"


def test_scorecard_pending_items_explicit(tmp_path):
    """缺席计分纪律:无成片时成片实测类计分项 = pending(既不得分也不进已判满分)。"""
    root = _mk_project(tmp_path)
    (root / "06_成片输出" / "final" / "final_x_916.mp4").unlink()
    score = rs_verify.build_scorecard(root)
    pend_dims = {p["dim"] for p in score["pending"]}
    assert "D1" in pend_dims and "D4" in pend_dims, score["pending"]
    for p in score["pending"]:
        assert p["detail"], "pending 项必须带缺席原因"


# ---------------------------------------------------------------- T5.2 及格线门禁


def test_low_score_sample_blocked(tmp_path):
    """T5.2:低分样例(字幕整体平移 1s)→ --score 非零退出 SCORE_FAIL 并指出低分维。"""
    root = _mk_project(tmp_path)
    final = json.loads((root / "05_时间线工程" / "wordline.final.json").read_text(encoding="utf-8"))
    _write_ass(root, final, shift_ms=1000)          # 字幕整体平移 1s(旧闸 check_alignment 必红)
    rc, doc = _run_verify(root, "--score")
    assert rc != 0, "低分样例必须非零退出"
    assert doc.get("code") == "SCORE_FAIL", doc.get("code")
    score = (doc.get("data") or {}).get("score") or {}
    assert score.get("pass") is False and score.get("score", 0) < 75, score.get("score")
    low_ids = [d["id"] for d in score.get("lowDims") or []]
    assert "D1" in low_ids, low_ids
    assert "低分维" in doc.get("message", ""), doc.get("message")
    # 诊断指引必须落进报告(怎么查/怎么修)
    md = (root / "06_成片输出" / "verify_report.md").read_text(encoding="utf-8")
    assert "低分维诊断指引" in md and "怎么查" in md and "怎么修" in md


def test_score_gate_code_registered():
    """SCORE_OK / SCORE_FAIL 必须先进 rs_codes 注册表(T2.13 纪律)。"""
    assert rs_codes.is_registered("SCORE_OK") and rs_codes.is_registered("SCORE_FAIL")


# ---------------------------------------------------------------- T5.6a 内容闸三态


def test_content_gate_tri_state_mapping():
    """B10 三态归一:pass/fail/degraded/absent;降级必须带原因与「绿灯不含内容正确性」提示。"""
    st = rs_sync.content_gate_state({"pass": True, "similarity": 0.98})
    assert st["state"] == "pass" and st["scope"] == "content"
    st = rs_sync.content_gate_state({"pass": False, "problems": ["归一相似度 0.5 < 0.9"]})
    assert st["state"] == "fail" and st["problems"]
    st = rs_sync.content_gate_state({"skipped": "找不到 tools/fun_asr.py"})
    assert st["state"] == "degraded" and "找不到" in st["reason"]
    assert "不包含内容正确性" in st["note"], "降级提示必须写明绿灯边界"
    st = rs_sync.content_gate_state(None)
    assert st["state"] == "absent"


def test_content_gate_readback_from_sync_rows(tmp_path):
    """check_content_gate 回读 sync_rows.json:fail→红;degraded→绿但显式降级留痕。"""
    root = _mk_project(tmp_path)
    rows = root / "06_成片输出" / "sync_rows.json"
    rows.write_text(json.dumps({"rows": [], "summary": {"contentGate": {
        "gate": "B10", "state": "degraded", "scope": "content",
        "reason": "找不到 tools/fun_asr.py", "note": "显式降级"}}}, ensure_ascii=False), encoding="utf-8")
    c = rs_verify.check_content_gate(root)
    assert c["ok"] is True and c.get("degraded") is True and "降级" in c["detail"]
    rows.write_text(json.dumps({"rows": [], "summary": {"contentGate": {
        "gate": "B10", "state": "fail", "scope": "content",
        "problems": ["片头句出现 3 次"]}}}, ensure_ascii=False), encoding="utf-8")
    c2 = rs_verify.check_content_gate(root)
    assert c2["ok"] is False and "片头句" in c2["detail"]
    # 旧版 sync_rows(无 contentGate)→ skipped,不误伤
    rows.write_text("[]", encoding="utf-8")
    c3 = rs_verify.check_content_gate(root)
    assert c3["ok"] is True and c3.get("skipped")


def test_s9_spec_carries_content_gate():
    """T5.6a:S9 装配默认携带内容闸(--audio-content),不是 opt-in。"""
    s9 = next(s for s in rs_run.spec() if s["id"] == "S9")
    assert "--audio-content" in s9["cmd"] and "--video" in s9["cmd"], s9["cmd"]


# ---------------------------------------------------------------- T5.6b 价值选帧证据接入 S9 装配


def test_bench_cmd_value_mode_with_shots(tmp_path):
    """shots.json 在册 → rs_bench 命令带 --shots/--wordline(价值选帧档);缺席回退启发式。"""
    root = _mk_project(tmp_path)
    # 无 shots.json → 启发式档,不带 --shots
    cmd, mode, out = rs_run._bench_cmd(root)
    assert mode == "heuristic" and "--shots" not in cmd
    # shots.json 在册 → 价值档
    (root / "04_粗剪决策" / "shots.json").write_text(
        json.dumps({"shots": [{"index": 0, "startMs": 0, "endMs": 3000, "motionPeakMs": 1500}]}),
        encoding="utf-8")
    cmd2, mode2, _ = rs_run._bench_cmd(root)
    assert mode2 == "value"
    assert "--shots" in cmd2 and str(root / "04_粗剪决策" / "shots.json") in cmd2
    assert "--wordline" in cmd2, cmd2
