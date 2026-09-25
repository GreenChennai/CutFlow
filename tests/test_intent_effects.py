# -*- coding: utf-8 -*-
"""v2 M13 · 效果计划生成(ADR-0059/分册06 §9.2,门禁表 #3)。

rs_intent compile 在风格包处方齐备时产出 effectsPlan 并进 decision_log
(source=prescription);处方缺席时不产(防御性缺省,旧工程行为不变)。

运行:pytest tests/test_intent_effects.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_intent  # noqa: E402
import rs_stylepack  # noqa: E402

REGISTRY = rs_intent.load_registry()
BRIEF = {"videoType": "混剪", "title": "测试卡点",
         "structure": "HOOK → 正文(卡点段)→ 高潮 → CTA"}
PLAN: dict = {}


def _write_and_compile(tmp_path: Path, brief: dict, pack_slug: str | None):
    inp = tmp_path / "in"
    inp.mkdir(parents=True, exist_ok=True)
    (inp / "brief.json").write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
    (inp / "plan.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "proj"
    if pack_slug is None:
        orig = rs_stylepack.load_pack
        rs_stylepack.load_pack = lambda slug: None        # 模拟风格包缺失
        try:
            rc = rs_intent.main.__wrapped__ if False else None
            args = ["compile", "--brief", str(inp / "brief.json"),
                    "--plan", str(inp / "plan.json"), "--out", str(out)]
            sys.argv = ["rs_intent.py", *args]
            code = rs_intent.main()
        finally:
            rs_stylepack.load_pack = orig
        assert code == 0
    else:
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "rs_intent.py"), "compile",
             "--brief", str(inp / "brief.json"), "--plan", str(inp / "plan.json"),
             "--out", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=REPO)
        assert r.returncode == 0, r.stdout[-400:] + r.stderr[-400:]
    doc = json.loads((out / rs_paths_brief(out) / "intent_decisions.json")
                     .read_text(encoding="utf-8"))
    return doc


def rs_paths_brief(out: Path) -> Path:
    import rs_paths  # noqa: PLC0415
    return out / rs_paths.p("brief")


def test_effects_plan_present_with_prescription(tmp_path):
    """处方齐备 → resolved.effectsPlan 产出且 decision_log source=prescription。"""
    doc = _write_and_compile(tmp_path, BRIEF, "mixcut-douyin")
    r = doc["resolved"]
    pres = r.get("effectsPrescription")
    assert pres and {"transition", "in", "out", "sfx", "flashy_max",
                     "require_reason"} <= set(pres)
    plan = r.get("effectsPlan")
    assert plan, "处方齐备必须产出 effects_plan(效果开工前规划)"
    assert plan["transitions"], "混剪处方 transition.min=2 → 草案必须给章节转场建议"
    assert plan["transitions"][0]["reason"], "草案每条建议带叙事理由(分册06 §1.1)"
    assert plan["requireReason"] is True
    src = [d for d in doc["decisions"] if d["source"] == "prescription"]
    assert len(src) >= 2, "处方与计划必须进 decision_log(source=prescription)"


def test_effects_plan_prefers_executable_fxids(tmp_path):
    """草案 prefer 首选必须【可执行】:登记待实现的 fxId 被跳过,绝不推荐做不出的。"""
    doc = _write_and_compile(tmp_path, BRIEF, "mixcut-douyin")
    plan = doc["resolved"]["effectsPlan"]
    sys.path.insert(0, str(SCRIPTS))
    import rs_effects
    for group in ("in", "out"):
        for item in plan.get(group) or []:
            assert rs_effects.is_executable(item["fx"]), \
                f"effects_plan 推荐了不可执行 fxId:{item['fx']}"
    for t in plan.get("transitions") or []:
        assert rs_effects.is_executable(t["fx"]), f"推荐了不可执行转场:{t['fx']}"


def test_effects_plan_structure_aware(tmp_path):
    """草案按纸面结构分段:分段越多章节转场建议越多(确定性:同输入同输出)。"""
    doc = _write_and_compile(tmp_path, BRIEF, "mixcut-douyin")
    n1 = len(doc["resolved"]["effectsPlan"]["transitions"][0]["atChapters"])
    brief2 = {**BRIEF, "structure": "HOOK → A → B → C → D → CTA"}
    doc2 = _write_and_compile(tmp_path / "more", brief2, "mixcut-douyin")
    n2 = len(doc2["resolved"]["effectsPlan"]["transitions"][0]["atChapters"])
    assert n2 > n1, f"结构分段应驱动章节数({n1} → {n2})"


def test_talking_head_flashy_zero(tmp_path):
    """纯口播处方:flashy_max=0(少用也是处方的合法出口,分册06 §5.3/§7.1)。"""
    brief = {**BRIEF, "videoType": "talking-head",
             "structure": "HOOK → 正文 → CTA"}
    doc = _write_and_compile(tmp_path, brief, "knowledge-talkshow-douyin")
    pres = doc["resolved"]["effectsPrescription"]
    assert pres["flashy_max"] == 0
    for f in pres["transition"]["forbid"]:
        assert "whip" in f or "punch" in f or "flash" in f or "glitch" in f \
            or "fade.fast" in f or "rotate" in f or "grays" in f


def test_no_pack_no_effects_plan(tmp_path, monkeypatch):
    """风格包缺失 → 无处方无计划(防御性缺省,回退路径字节级一致)。"""
    doc = _write_and_compile(tmp_path, BRIEF, None)
    assert "effectsPrescription" not in doc["resolved"]
    assert "effectsPlan" not in doc["resolved"]
    assert not [d for d in doc["decisions"] if d["source"] == "prescription"]


def test_build_effects_plan_pure_function():
    """build_effects_plan 纯函数:登记待实现的 prefer 被跳过(诚实纪律)。"""
    pres = {"transition": {"min": 1, "prefer": ["tr.morph.cut"]},
            "in": {"min": 1, "prefer": ["in.stagger"]},   # 登记待实现
            "out": {}, "sfx": {}}
    plan = rs_intent.build_effects_plan(pres, BRIEF)
    assert plan["transitions"] == [], "morph.cut 未落地,不得进推荐"
    assert plan["in"] == [], "in.stagger 未落地,不得进推荐"
