# -*- coding: utf-8 -*-
"""v2 M13 · 效果使用率门禁(ADR-0059/分册06 §9.3 九判据)+ context「该用的特效」段。

全部机械可算:用合成工程(intent_decisions.json 的 effectsPrescription + IR)驱动
rs_verify.check_effects_usage,逐判据给触发样例;再验 rs_edit context 的新节
(必备/进度/缺口/禁用)与无处方时的防御性提示。

运行:pytest tests/test_effects_usage.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_edit  # noqa: E402
import rs_paths  # noqa: E402
import rs_verify  # noqa: E402

PRESCRIPTION = {
    "transition": {"min": 1, "max_per_8s": 1,
                   "prefer": ["tr.fade.black", "tr.dissolve.cross"],
                   "forbid": ["tr.glitch.slice", "tr.fade.fast"]},
    "in": {"min": 1, "max": 12, "prefer": ["in.fade.up"]},
    "out": {"min": 0, "max": 12, "prefer": ["out.fade.up"]},
    "sfx": {"min": 0, "max": 20, "per15s": 2, "noRepeatWithinMs": 10000},
    "flashy_max": 3,
    "require_reason": True,
}


def _mk_project(tmp_path: Path, clips: list[dict], prescription: dict | None,
                sfx: list[dict] | None = None) -> Path:
    root = tmp_path / "proj"
    (root / rs_paths.p("brief")).mkdir(parents=True)
    (root / rs_paths.p("timeline")).mkdir(parents=True)
    resolved = {} if prescription is None else {"effectsPrescription": prescription}
    (root / rs_paths.p("brief") / "intent_decisions.json").write_text(
        json.dumps({"resolved": resolved}, ensure_ascii=False), encoding="utf-8")
    tracks = [{"id": "V1", "kind": "video", "name": "main", "clips": clips}]
    if sfx:
        tracks.append({"id": "A1", "kind": "audio", "name": "audio", "clips": sfx})
    (root / rs_paths.p("timeline") / "project.json").write_text(
        json.dumps({"version": 1, "slug": "t", "fps": 30,
                    "canvas": {"width": 1080, "height": 1920},
                    "tracks": tracks, "outputs": ["9x16"]}, ensure_ascii=False),
        encoding="utf-8")
    return root


def _clip(i: int, dur: int, **kw) -> dict:
    c = {"id": f"V1-{i + 1:03d}", "src": f"01_原始素材/a{i % 2}.mp4",
         "startMs": i * dur, "durationMs": dur, "sourceInMs": i * (dur + 400)}
    c.update(kw)
    return c


def _codes(res: dict) -> list[str]:
    return [v.split(":")[0] for v in res.get("violations") or []]


# ================================================================ 启用条件

def test_no_prescription_skips(tmp_path):
    """无处方 → skipped 留痕 NO_PRESCRIPTION(不误伤旧工程,ADR-0059 四问)。"""
    root = _mk_project(tmp_path, [_clip(0, 1200), _clip(1, 1200)], None)
    res = rs_verify.check_effects_usage(root)
    assert res["ok"] and res.get("skipped")
    assert "NO_PRESCRIPTION" in res["skipped"]


def test_no_ir_skips(tmp_path):
    root = tmp_path / "empty"
    (root / rs_paths.p("brief")).mkdir(parents=True)
    (root / rs_paths.p("brief") / "intent_decisions.json").write_text(
        json.dumps({"resolved": {"effectsPrescription": PRESCRIPTION}}), encoding="utf-8")
    res = rs_verify.check_effects_usage(root)
    assert res["ok"] and res.get("skipped")


# ================================================================ ①② UNUSED

def test_unused_transitions_red(tmp_path):
    """判据 1:显式转场数 < 处方 min → EFFECTS_UNUSED(「声明了却零使用」= 红)。"""
    clips = [_clip(0, 1200, transition={"type": "cut"}),
             _clip(1, 1200, transition={"type": "cut"}),
             _clip(2, 1200, transition={"fx": "tr.fade.black", "durMs": 300,
                                        "reason": "章节切换"})]
    root = _mk_project(tmp_path, clips,
                       {**PRESCRIPTION, "transition": {**PRESCRIPTION["transition"],
                                                       "min": 2}})
    res = rs_verify.check_effects_usage(root)
    assert "EFFECTS_UNUSED" in _codes(res)
    # 同一工程 min=1 → 绿(少用且用得准,§5.3)
    clips_ok = [_clip(0, 1200, motion={"inFx": "in.fade.up"}),
                _clip(1, 1200, transition={"type": "cut"}),
                _clip(2, 1200, transition={"fx": "tr.fade.black", "durMs": 300,
                                           "reason": "章节切换"})]
    root2 = _mk_project(tmp_path / "ok", clips_ok, PRESCRIPTION)
    res2 = rs_verify.check_effects_usage(root2)
    assert res2["ok"], res2["violations"]


def test_unused_in_out_red(tmp_path):
    """判据 2:入场/出场 < min → EFFECTS_UNUSED。"""
    clips = [_clip(0, 1200), _clip(1, 1200, motion={"inFx": "zoompan.in.slight"})]
    root = _mk_project(tmp_path, clips,
                       {**PRESCRIPTION, "in": {"min": 2, "prefer": ["in.fade.up"]},
                        "out": {"min": 0, "prefer": []}})
    res = rs_verify.check_effects_usage(root)
    assert "EFFECTS_UNUSED" in _codes(res)


# ================================================================ ④⑤ OVERUSED

def test_overused_flashy_hard_fail(tmp_path):
    """判据 5:花哨类 > flashy_max → EFFECTS_OVERUSED(硬失败)。"""
    clips = []
    flashy = ["tr.whip.pan", "tr.punch.zoom", "tr.glitch.slice", "tr.flash.zoom"]
    for i in range(6):
        tr = {"fx": flashy[i % 4], "durMs": 250, "reason": f"节奏块切换{i}"} \
            if 1 <= i else None
        c = _clip(i, 3000, transition=tr)
        if i == 0:
            c.pop("transition", None)
        clips.append(c)
    root = _mk_project(tmp_path, clips, {**PRESCRIPTION, "flashy_max": 3})
    res = rs_verify.check_effects_usage(root)
    assert "EFFECTS_OVERUSED" in _codes(res)
    # ok 样例:花哨类不相邻(join1/join3,间隔 6s)→ 不触连续判据
    clips4 = []
    for i in range(4):
        c = _clip(i, 3000)
        if i == 1:
            c["transition"] = {"fx": "tr.whip.pan", "durMs": 250, "reason": "节奏块 1"}
        if i == 3:
            c["transition"] = {"fx": "tr.punch.zoom", "durMs": 250, "reason": "情绪爆发"}
        clips4.append(c)
    clips4[0]["motion"] = {"inFx": "in.fade.up"}
    root2 = _mk_project(tmp_path / "ok", clips4, {**PRESCRIPTION, "flashy_max": 3})
    res2 = rs_verify.check_effects_usage(root2)
    assert "EFFECTS_OVERUSED" not in _codes(res2), res2["violations"]


def test_overused_consecutive_flashy(tmp_path):
    """判据 5:花哨类连续两处 → EFFECTS_OVERUSED(不得连续)。"""
    clips = [_clip(0, 2000, motion={"inFx": "in.fade.up"})]
    for i in range(1, 4):
        clips.append(_clip(i, 2000, transition={"fx": "tr.whip.pan", "durMs": 250,
                                                "reason": "甩镜串接"}))
    root = _mk_project(tmp_path, clips,
                       {**PRESCRIPTION, "out": {"min": 0, "prefer": []}})
    res = rs_verify.check_effects_usage(root)
    assert "EFFECTS_OVERUSED" in _codes(res)
    assert any("连续" in v for v in res["violations"])


def test_density_caps_are_advisory(tmp_path):
    """判据 4 密度上限(片长÷10、每 8s ≤1)为【告警档】:不硬失败但必须报告。

    最小偏离(报告已列):管线 ADR-0026 三级语法会在去气口边界自动产生 topic 溶解,
    密度超标可能反映素材结构而非剪辑滥用,硬失败会误伤全部管线工程。"""
    clips = []
    for i in range(4):
        c = _clip(i, 800)
        if i == 0:
            c["motion"] = {"inFx": "in.fade.up"}
        if i > 0:
            c["transition"] = {"type": "fade", "durMs": 300, "reason": "章节切换"}
        clips.append(c)
    root = _mk_project(tmp_path, clips,
                       {**PRESCRIPTION, "out": {"min": 0, "prefer": []}})
    res = rs_verify.check_effects_usage(root)
    assert res["ok"], res["violations"]
    assert any("EFFECTS_OVERUSED" in w and "告警" in w for w in res.get("warnings") or [])


# ================================================================ ⑥ REPEATED

def test_repeated_within_10s(tmp_path):
    """判据 6:同一效果 10s 内重复 → EFFECTS_REPEATED。"""
    clips = [_clip(0, 2500, motion={"inFx": "in.fade.up"})]
    for i in (1, 2):
        clips.append(_clip(i, 2500, transition={"fx": "tr.fade.black", "durMs": 400,
                                                "reason": "时间跳跃"}))
    root = _mk_project(tmp_path, clips,
                       {**PRESCRIPTION, "out": {"min": 0, "prefer": []}})
    res = rs_verify.check_effects_usage(root)
    assert "EFFECTS_REPEATED" in _codes(res)


# ================================================================ ⑦ UNMOTIVATED

def test_unmotivated_empty_reason(tmp_path):
    """判据 7:显式转场无 reason → EFFECTS_UNMOTIVATED。"""
    clips = [_clip(0, 1500, motion={"inFx": "in.fade.up"}),
             _clip(1, 1500, transition={"fx": "tr.fade.black", "durMs": 400})]
    root = _mk_project(tmp_path, clips,
                       {**PRESCRIPTION, "out": {"min": 0, "prefer": []}})
    res = rs_verify.check_effects_usage(root)
    assert "EFFECTS_UNMOTIVATED" in _codes(res)


def test_unmotivated_pretty_word(tmp_path):
    """判据 7:reason =「好看」类词视为未解释 → 红(§5.2 三条规矩)。"""
    clips = [_clip(0, 1500, motion={"inFx": "in.fade.up"}),
             _clip(1, 1500, transition={"fx": "tr.fade.black", "durMs": 400,
                                        "reason": "这里加个转场比较好看"})]
    root = _mk_project(tmp_path, clips,
                       {**PRESCRIPTION, "out": {"min": 0, "prefer": []}})
    res = rs_verify.check_effects_usage(root)
    assert "EFFECTS_UNMOTIVATED" in _codes(res)


def test_jumpcut_soft_cut_not_explicit(tmp_path):
    """ADR-0026 三级语法:jumpcut/亚帧软切是视觉硬切,不计显式转场、不要求 reason。"""
    clips = [_clip(0, 1500),
             _clip(1, 1500, transition={"type": "fade", "durMs": 33,
                                        "reason": "jumpcut"})]
    root = _mk_project(tmp_path, clips, PRESCRIPTION)
    res = rs_verify.check_effects_usage(root)
    assert res["ok"] or all(c != "EFFECTS_UNMOTIVATED" for c in _codes(res))
    assert res["counts"]["transitions"] == 0


# ================================================================ ③ SFX_DENSITY

def test_sfx_density(tmp_path):
    """判据 3:15s 窗口 >2 个音效 → SFX_DENSITY(分册01 §4.1 硬约束)。"""
    clips = [_clip(0, 12000)]
    sfx = [{"src": "assets_sfx:whoosh_01", "startMs": 1000 + i * 2000,
            "durationMs": 1000, "role": "sfx"} for i in range(3)]
    root = _mk_project(tmp_path, clips, PRESCRIPTION, sfx=sfx)
    res = rs_verify.check_effects_usage(root)
    assert "SFX_DENSITY" in _codes(res)


# ================================================================ ⑧ FLASH_UNSAFE

def test_flash_unsafe(tmp_path):
    """判据 8:任一秒明暗反转 >3 次 → FLASH_UNSAFE(WCAG 2.3.1,安全底线)。

    6 段 300ms 素材 + 每个衔接点 fade.fast → 1s 窗口内 4 次闪变(0.3/0.6/0.9/1.2s)。"""
    clips = [_clip(0, 300, motion={"inFx": "in.fade.up"})]
    for i in range(1, 6):
        clips.append(_clip(i, 300, transition={"fx": "tr.fade.fast", "durMs": 200,
                                               "reason": "频闪强调"}))
    root = _mk_project(tmp_path, clips,
                       {**PRESCRIPTION, "out": {"min": 0, "prefer": []}})
    res = rs_verify.check_effects_usage(root)
    assert "FLASH_UNSAFE" in _codes(res), res["violations"]


# ================================================================ 全绿样例

def test_healthy_project_green(tmp_path):
    """处方满足 + 无滥用 → 绿,且 counts 进留痕(进度可见)。"""
    clips = [_clip(0, 2000),
             _clip(1, 2000, transition={"fx": "tr.fade.black", "durMs": 400,
                                        "reason": "章节切换(时间跳跃)"},
                   motion={"inFx": "in.fade.up"})]
    sfx = [{"src": "assets_sfx:whoosh_01", "startMs": 500,
            "durationMs": 1000, "role": "sfx"}]
    root = _mk_project(tmp_path, clips, PRESCRIPTION, sfx=sfx)
    res = rs_verify.check_effects_usage(root)
    assert res["ok"], res["violations"]
    assert res["counts"]["transitions"] == 1 and res["counts"]["in"] == 1


# ================================================================ context 该用的特效段

def test_context_fxplan_section(tmp_path):
    """rs_edit context:「本工程该用的特效」段如实显示进度与缺口(分册06 §9.4)。"""
    clips = [_clip(0, 1200), _clip(1, 1200)]
    root = _mk_project(tmp_path, clips, PRESCRIPTION)
    text, info = rs_edit.build_context(root, "project", 24 * 1024)
    assert "本工程该用的特效" in text
    assert "已用:过渡 0/1" in text and "⚠ 缺口" in text
    assert "tr.glitch.slice" in text or "forbid" in text or "禁用" in text
    # 加上转场与入场 → 缺口消失
    clips2 = [_clip(0, 1200),
              _clip(1, 1200, transition={"fx": "tr.fade.black", "durMs": 400,
                                         "reason": "章节切换"},
                    motion={"inFx": "in.fade.up"})]
    root2 = _mk_project(tmp_path / "ok", clips2, PRESCRIPTION)
    text2, _ = rs_edit.build_context(root2, "project", 24 * 1024)
    assert "缺口:无" in text2


def test_context_fxplan_without_prescription(tmp_path):
    """无处方 → 防御性提示(不生效声明,绝不假装门禁存在)。"""
    root = _mk_project(tmp_path, [_clip(0, 1200)], None)
    text, _ = rs_edit.build_context(root, "project", 24 * 1024)
    assert "effects_prescription" in text and "不生效" in text
