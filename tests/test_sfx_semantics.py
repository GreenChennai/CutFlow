# -*- coding: utf-8 -*-
"""M12 音效语义门禁(分册01 §4.1/§8 + R29/R34):

①转场语义:from_transitions 不再奇偶交替 —— 同类型转场同音色;方向映射到变体;
  纯硬切不撒声;jumpcut → 轻 swipe;旧 IR(无 transition 字段)行为相容;
②强调/列举接线:ding(关键词上屏)与 pop(第N项)真的能被产出(缺陷 C 清零);
③密度硬约束:每 15s ≤2(既有)+ 同一音效 10s 不重复(新增,独立函数不破坏旧口径);
④BGM 让位:混剪类(videoType=mixcut)转场音效默认让位;
⑤素材检索走 manifest:src 全部可经 rs_asset.resolve_sfx_ref 落盘解析;
⑥markers 字段统一:from_markers 落在 {ms,label} 的 ms(test_markers 的草案级复验)。

运行:pytest tests/test_sfx_semantics.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_asset  # noqa: E402
import rs_sfx  # noqa: E402


def _ir_with_transitions(transitions: list[dict | None]) -> dict:
    clips = [{"src": "a.mp4", "startMs": 0, "durationMs": 3000}]
    for i, tr in enumerate(transitions):
        clips.append({"src": f"c{i}.mp4", "startMs": 3000 * (i + 1),
                      "durationMs": 3000, "transition": tr})
    return {"tracks": [{"kind": "video", "name": "main", "clips": clips}]}


def _wordline(text: str, step_ms: int = 200) -> dict:
    return {"chars": [{"ch": ch, "i": i, "startMs": 500 + i * step_ms,
                       "endMs": 480 + i * step_ms + step_ms}
                      for i, ch in enumerate(text)]}


# ================================================================ ① 转场语义(缺陷 B)

def test_same_transition_type_yields_same_sound():
    """三次 fade → 同一音色(奇偶交替废止;同输入必得同输出)。"""
    ir = _ir_with_transitions([{"type": "fade"}, {"type": "fade"}, {"type": "fade"}])
    srcs = [p["src"] for p in rs_sfx.from_transitions(ir)]
    assert len(srcs) == 3 and len(set(srcs)) == 1, srcs


def test_transition_direction_maps_to_distinct_variants():
    """方向映射:左滑 → 横扫变体(04),上擦 → 上行变体(02),fade → 01。"""
    ir = _ir_with_transitions([{"type": "fade"}, {"type": "slideleft"},
                               {"type": "wipeup"}])
    by_at = {p["atMs"]: p["src"] for p in rs_sfx.from_transitions(ir)}
    assert by_at[3000].endswith("whoosh")          # 组首条(01)→ 裸组名
    assert by_at[6000].endswith("sfx.whoosh.04")   # 左滑 → 横扫变体(全 id 引用)
    assert by_at[9000].endswith("sfx.whoosh.02")   # 上擦 → 上行变体


def test_hard_cut_gets_no_sound_jumpcut_gets_swipe():
    ir = _ir_with_transitions([{"type": "cut"}, {"type": "none"},
                               {"type": "cut", "reason": "topic"}])
    pls = rs_sfx.from_transitions(ir)
    by_at = {p["atMs"]: p for p in pls}
    assert 3000 not in by_at and 6000 not in by_at, "纯硬切不撒声"
    assert 9000 in by_at and by_at[9000]["src"].endswith("swipe"), "topic 硬切 → 轻 swipe"


def test_legacy_ir_without_transition_still_gets_cuts():
    """旧 IR(无 transition 字段)→ 轻转场音效,行为相容但不再奇偶交替。"""
    ir = _ir_with_transitions([None, None, None, None])
    srcs = [p["src"] for p in rs_sfx.from_transitions(ir)]
    assert len(srcs) == 4 and len(set(srcs)) == 1, "无转场信息时同一音色(非 whoosh/swipe 交替)"


# ================================================================ ② ding / pop 接线(缺陷 C)

def test_enumeration_produces_pop_for_ordinals_and_click_for_connectives():
    wl = _wordline("第一点,首先看图,其次对比")
    srcs = {(p["src"], p["note"][:4]) for p in rs_sfx.from_enumeration(wl)}
    assert any(s.endswith("pop") for s, _ in srcs), "第N项必须产 pop"
    assert any(s.endswith("click") for s, _ in srcs), "连接词必须产 click"


def test_emphasis_produces_ding_for_keywords():
    wl = _wordline("蓝屏是最常见的问题")
    pls = rs_sfx.from_emphasis(wl, ["蓝屏"])
    assert pls and all(p["src"].endswith("ding") for p in pls)
    assert pls[0]["atMs"] == 500, "关键词上屏对齐首字锚点"
    assert rs_sfx.from_emphasis(wl, []) == [], "无关键词不产强调音"


def test_keywords_from_ir_sfx_config_flow_into_draft():
    ir = {"sfx": {"keywords": ["蓝屏"]},
          "tracks": [{"kind": "video", "name": "main", "clips": [
              {"src": "a.mp4", "startMs": 0, "durationMs": 2000}]}]}
    draft = rs_sfx.build_draft(ir, _wordline("蓝屏来了"))
    assert any(p["trigger"] == "emphasize" for p in draft["placements"]), \
        "IR sfx.keywords 必须触发强调落点"


# ================================================================ ③ 密度硬约束

def test_density_window_15s_max2_unchanged():
    places = [{"atMs": t, "src": "assets_sfx:ding", "trigger": "enumerate"}
              for t in (0, 2000, 4000, 20000)]
    kept, dropped = rs_sfx.enforce_density(places)
    assert len(kept) == 3 and len(dropped) == 1
    assert "窗口" in dropped[0]["reason"]


def test_same_sound_not_repeated_within_10s():
    places = [{"atMs": t, "src": "assets_sfx:ding", "trigger": "enumerate"}
              for t in (0, 4000, 11000)]
    kept, dropped = rs_sfx.enforce_repeat(places)
    assert len(kept) == 2 and len(dropped) == 1
    assert "已用过" in dropped[0]["reason"]
    # 不同音色互不干扰
    places2 = [{"atMs": 0, "src": "assets_sfx:ding", "trigger": "enumerate"},
               {"atMs": 500, "src": "assets_sfx:pop", "trigger": "enumerate"}]
    kept2, _ = rs_sfx.enforce_repeat(places2)
    assert len(kept2) == 2


def test_build_draft_applies_both_constraints():
    ir = {"markers": [{"ms": 500, "label": "章"}],
          "tracks": [{"kind": "video", "name": "main", "clips": [
              {"src": "a.mp4", "startMs": 0, "durationMs": 60000},
              {"src": "b.mp4", "startMs": 30000, "durationMs": 30000}]}]}
    wl = _wordline("第一" * 30)          # 大量列举 → 密度/重复双闸起作用
    draft = rs_sfx.build_draft(ir, wl)
    assert draft["dropped"], "密度/重复约束必须产生丢弃留痕"
    ats = [p["atMs"] for p in draft["placements"]]
    ats.sort()
    for a, b in zip(ats, ats[1:]):        # 全局排布后 15s 窗口 ≤2 仍成立
        window = [x for x in ats if a < x <= a + 15000]
        assert len(window) <= 2, f"{a} 起 15s 窗口内 {len(window)} 个"


# ================================================================ ④ BGM 让位(混剪)

def test_mixcut_transitions_yield_to_bgm():
    ir = {"_meta": {"videoType": "mixcut"}}
    ir["tracks"] = _ir_with_transitions([{"type": "fade"}])["tracks"]
    draft = rs_sfx.build_draft(ir, None)
    assert all(p["trigger"] != "cut" for p in draft["placements"]), "混剪转场音效让位"
    assert any("让位" in d["reason"] for d in draft["dropped"]), "让位必须留痕(可人工捞回)"


# ================================================================ ⑤ manifest 检索 + 可解析

def test_all_draft_srcs_resolve_on_disk():
    ir = {"markers": [{"ms": 4000, "label": "章节"}],
          "tracks": [{"kind": "video", "name": "main", "clips": [
              {"src": "a.mp4", "startMs": 0, "durationMs": 9000},
              {"src": "b.mp4", "startMs": 9000, "durationMs": 3000,
               "transition": {"type": "slideleft"}}]}]}
    wl = _wordline("第一,首先讲原理")
    draft = rs_sfx.build_draft(ir, wl, keywords=["原理"])
    assert draft["placements"], "草案必须非空"
    assert not draft["missingAssets"], f"缺失音色:{draft['missingAssets']}"
    for p in draft["placements"]:
        ref = p["src"].split(":", 1)[1]
        path = rs_asset.resolve_sfx_ref(ref)
        assert path is not None and path.is_file(), f"{p['src']} 无法落盘解析"


def test_markers_semantic_keeps_riser_and_ms_anchor():
    """R01 保持:章节音效 = riser,落在 markers.ms(test_markers 的草案级复验)。"""
    ir = {"markers": [{"ms": 4200, "label": "第二节"}]}
    pls = rs_sfx.from_markers(ir)
    assert pls and pls[0]["src"] == "assets_sfx:riser"
    assert pls[0]["atMs"] == 4200, "章节音效必须落在 ms(不得回落 0ms)"


# ================================================================ ⑥ apply 计数口径(R29)

def test_apply_counts_written_sfx_not_last_track(tmp_path):
    """R29:apply 统计数「写入的 sfx 条数」,不再数 tracks[-1](overlay 轨在尾时旧口径会虚报)。"""
    import json
    ir = {"version": 1,
          "tracks": [{"kind": "video", "name": "main", "clips": [
              {"src": "a.mp4", "startMs": 0, "durationMs": 5000}]},
              {"kind": "audio", "name": "music", "clips": []}]}
    ir_path = tmp_path / "project.json"
    ir_path.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    draft = {"version": 2, "placements": [
        {"atMs": 0, "src": "assets_sfx:whoosh", "trigger": "cut", "gainDb": -14},
        {"atMs": 2000, "src": "assets_sfx:ding", "trigger": "emphasize", "gainDb": -14}],
        "dropped": [], "missingAssets": []}
    draft_path = tmp_path / "draft.json"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    out_path = tmp_path / "out.json"
    import subprocess
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "rs_sfx.py"), str(ir_path),
         "--apply", str(draft_path), "--write", str(out_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout + r.stderr
    import json as _json
    lines = [ln for ln in r.stdout.splitlines() if ln.strip().startswith("{")]
    doc = _json.loads(lines[-1])
    assert doc["data"]["count"] == 2, "写入条数必须实数(audio 轨不在尾部也不影响)"
