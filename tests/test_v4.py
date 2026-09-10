"""CutFlow v4 回归测试:断句 DP / Wordline 重映射 / guard 三重校验 / 增量缓存键 / 变体矩阵。

对应 docs/OPTIMIZATION-v4.md §9 验收清单与 rules/*.md 的门禁项。
运行:pytest tests/ -q
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_align  # noqa: E402
import rs_cut  # noqa: E402
import rs_meta  # noqa: E402
import rs_run  # noqa: E402
import rs_sfx  # noqa: E402
import rs_sync  # noqa: E402
import rs_subtitle as rsub  # noqa: E402
import segmentation as sg  # noqa: E402
import textopt  # noqa: E402


# ---------------------------------------------------------------- 夹具

def make_wordline(segs, source="a.mp4"):
    return rs_align.build_wordline(segs, source)


SEGS = [
    {"start": 0.0, "end": 3.0, "text": "大家好,今天我们来讲桌面运维。"},
    {"start": 3.4, "end": 6.2, "text": "先看蓝屏,蓝屏是最常见的问题。"},
    {"start": 6.2, "end": 9.4, "text": "嗯,那个,我们先把内存条拔下来。"},
    {"start": 10.6, "end": 13.0, "text": "我们我们再来检查一下硬盘。"},
]


def cards_texts(text, max_chars=12, terms=()):
    return [c["text"] for c in sg.segment(text, max_chars, terms=terms)["cards"]]


def assert_term_intact(text, cards, term):
    """专名/固定搭配不得被切断:必须存在某张卡完整包含该词。"""
    for i in range(len(text)):
        if text.startswith(term, i):
            end = i + len(term)
            assert any("".join(c.split()).find(term) >= 0 for c in cards), \
                f"「{term}」被切断:{cards}"


# ---------------------------------------------------------------- P4 断句回归(5 用例)

def test_regression_poem_proper_noun_not_split():
    """① 滚滚长江东逝水:不得切成「滚滚长」/「江东逝水」。"""
    text = "话说天下大势分久必合长江西来东逝水浪花淘尽英雄"
    cards = cards_texts(text, 12, terms=("长江",))
    assert all(len(c.replace(" ", "")) <= 12 for c in cards), cards
    assert_term_intact(text, cards, "长江")


def test_regression_poem_short_is_single_card():
    """整句 7 字诗:一张卡,不切。"""
    assert cards_texts("滚滚长江东逝水", 12, terms=("长江",)) == ["滚滚长江东逝水"]


def test_regression_number_unit_not_split():
    """② 我今年三十五岁:数量词 + 单位之间禁切。"""
    text = "我今年三十五岁然后我今年三十五岁我们又聊了别的事情"
    assert 3 in sg.forbidden_positions("三十五岁"), "「五」与「岁」之间必须禁切"
    cards = cards_texts(text, 12)
    assert all(len(c.replace(" ", "")) <= 12 for c in cards), cards


def test_regression_currency_not_split():
    """④ ¥1999 元:货币与数字之间禁切。"""
    text = "这套设备要 ¥1999 元然后我们再看看别的方案"
    forb = sg.forbidden_positions(text)
    at = text.index("¥") + 1
    assert at in forb, f"货币符与数字之间应禁切:{at}"
    cards = cards_texts(text, 12)
    assert_term_intact(text, cards, "¥1999")


def test_regression_ascii_word_not_split():
    """⑤ 用 GPT-SoVITS 做配音:ASCII 词内禁切。"""
    text = "我们用 GPT-SoVITS 做配音然后导出成片"
    cards = cards_texts(text, 12)
    assert any("GPT-SoVITS" in c for c in cards), cards


def test_regression_filler_removed_no_empty_card():
    """③ 删口水词后重新对齐,不产生空卡。"""
    cards = textopt.build_cards(["嗯,那个,我们把那个…那个什么…对,做完了。"], 12)
    assert cards and all(c.strip() for c in cards), cards


def test_forbidden_after_particle_and_ascii():
    s = "美丽的风景 v2026 build1"
    forb = sg.forbidden_positions(s)
    assert 3 in forb, "「的」之后禁切"
    idx = s.index("build1")
    for i in range(idx + 1, idx + len("build1")):
        assert i in forb, f"ASCII token 内部({i})禁切"
    v = s.index("v2026")
    assert v + 1 in forb and v + 4 in forb, "ASCII token 内部禁切(v2.1.0/v2026 类)"


# ---------------------------------------------------------------- P4 硬约束

def test_ambiguous_flag_and_top_candidates():
    out = sg.segment("话说天下大势分久必合合久必分浪花淘尽英雄人物", 12, top=3)
    assert len(out["plans"]) >= 2
    assert isinstance(out["ambiguous"], bool)


def test_check_constraints_detects_cps_and_duration():
    cards = [{"i": 0, "text": "一二三四五六七八", "chars": 8, "startMs": 0, "endMs": 1000,
              "durMs": 1000, "cps": 8.0},
             {"i": 1, "text": "一二三四五六七八", "chars": 8, "startMs": 1000, "endMs": 1500,
              "durMs": 500, "cps": 16.0}]
    bad = sg.check_constraints(cards, 12, 9.0)
    assert any("CPS" in b for b in bad)
    assert any("时长" in b for b in bad)


def test_wordline_card_time_comes_from_char_timestamps():
    wl = make_wordline(SEGS[:1])
    chars = wl["chars"]
    text = "".join(c["ch"] for c in chars)
    cards = sg.segment(text, 12, gaps={}, index_map=list(range(len(chars))),
                       char_times=chars)["cards"]
    assert cards[0]["startMs"] <= chars[0]["startMs"]
    last_content = max(c["endMs"] for c in chars if c["ch"] not in sg.PUNCT_WS)
    assert cards[-1]["endMs"] >= last_content
    # 首卡时间必须来自首字,而不是靠"句长 / 字数"摊出来
    assert cards[0]["startMs"] == max(0, chars[0]["startMs"] - 20)


# ---------------------------------------------------------------- P1 粗剪

def test_cutlist_detects_and_keeps_consistent():
    wl = make_wordline(SEGS)
    cuts = []
    for fn in rs_cut.DETECTORS.values():
        cuts.extend(fn(wl))
    cl = rs_cut.build_cutlist(wl, cuts, {})
    assert cl["cuts"], "至少应检出静音/口头禅/重复"
    rs_cut.finalize_cutlist(cl)                       # keep 覆盖 [0,total] 且不重叠
    assert cl["keep"][0][0] == 0
    assert cl["keep"][-1][1] == cl["srcTotalMs"]
    for c in cl["cuts"]:
        assert c["reason"] in rs_cut.REASONS
        assert c["action"] in ("remove", "review", "keep")


def test_guard_three_checks_and_downgrade():
    wl = make_wordline(SEGS)
    chars = wl["chars"]
    gaps = rs_cut._gap_windows(chars)
    # 落在字内部 → wordClipped,必须降级
    inside = int(chars[2]["startMs"]) + (int(chars[2]["endMs"]) - int(chars[2]["startMs"])) // 2
    g = rs_cut.guard({"inMs": inside, "outMs": inside + 400}, chars, gaps)
    assert g["wordClipped"] is True and g["ok"] is False
    cut = {"inMs": inside, "outMs": inside + 400, "reason": "filler", "conf": 0.95}
    rs_cut.classify(cut)
    assert cut["action"] == "review", "guard 未过不得 remove"


def test_derive_keep_and_finalize_guardrails():
    total = 10000
    keep = rs_cut.derive_keep([{"inMs": 3000, "outMs": 4000}], total)
    assert keep == [[0, 3000], [4000, 10000]]
    # finalize 必须按 action 重算 keep(手写的过期 keep 会被纠正),并校验覆盖完整
    cl = {"srcTotalMs": total, "keep": [[6000, total]],
          "cuts": [{"inMs": 5000, "outMs": 6000, "action": "remove", "reason": "silence",
                    "guard": {}, "conf": 0.95, "id": "c001"}]}
    rs_cut.finalize_cutlist(cl)
    assert cl["keep"] == [[0, 5000], [6000, 10000]]
    assert cl["removedMs"] == 1000
    # 空 keep(整片被删)时 derive_keep 返回空,不应产出非法区间
    assert rs_cut.derive_keep([{"inMs": 0, "outMs": total}], total) == []


def test_merge_overlaps_unions_ranges():
    merged = rs_cut.merge_overlaps([
        {"inMs": 1000, "outMs": 2000, "reason": "filler", "conf": 0.7},
        {"inMs": 1500, "outMs": 2500, "reason": "silence", "conf": 0.95}])
    assert len(merged) == 1 and merged[0]["outMs"] == 2500
    assert merged[0]["reason"] == "silence"


def test_unclosed_reason_rejected():
    wl = make_wordline(SEGS)
    with pytest.raises(ValueError):
        rs_cut.build_cutlist(wl, [{"inMs": 0, "outMs": 100, "reason": "乱写", "conf": 0.99}], {})


# ---------------------------------------------------------------- P2 重映射

def test_map_src_to_final_monotonic_and_snap():
    keep = [[0, 9150], [10050, 12000]]
    segs = rs_align.keep_to_segments(keep)
    assert rs_align.map_src_to_final(0, segs) == 0
    assert rs_align.map_src_to_final(11000, segs) == 10100
    # 被删区间的时刻 → 吸附到下一个 keep 起点,不穿透、不插值
    assert rs_align.map_src_to_final(9500, segs) == 9150
    vals = [rs_align.map_src_to_final(t, segs) for t in range(0, 12000, 50)]
    assert vals == sorted(vals), "映射必须单调不减"


def test_remap_wordline_produces_final_space():
    wl = make_wordline(SEGS)
    keep = [[0, 9150], [10050, 12000]]
    out = rs_align.remap_wordline(wl, keep, {"version": 1})
    assert out["space"] == "final"
    assert out["finalDurationMs"] == 11100
    starts = [c["startMs"] for c in out["chars"]]
    assert starts == sorted(starts)
    assert all(c["endMs"] > c["startMs"] for c in out["chars"])
    # 源域信息保留(下游用它做反查)
    assert out["chars"][0]["srcStartMs"] == wl["chars"][0]["startMs"]


def test_wordline_stats_and_degradation_flag():
    wl = make_wordline(SEGS)
    assert wl["stats"]["charCount"] > 0
    assert wl["degraded"] is True and wl["degradeReasons"]
    assert wl["gaps"], "应识别出停顿"


# ---------------------------------------------------------------- P3 增量

def test_cache_key_changes_with_tool_hash(tmp_path):
    base = {"inputs": {"a": "1"}, "params": {"m": 1}, "tool": {"rs_subtitle.py": "aaa"},
            "external": {}}
    changed_tool = json.loads(json.dumps(base))
    changed_tool["tool"]["rs_subtitle.py"] = "bbb"      # 改了脚本 → 缓存必须失效
    changed_input = json.loads(json.dumps(base))
    changed_input["inputs"]["a"] = "2"
    changed_params = json.loads(json.dumps(base))
    changed_params["params"]["m"] = 2
    k = rs_run.key_of(base)
    assert k != rs_run.key_of(changed_tool)
    assert k != rs_run.key_of(changed_input)
    assert k != rs_run.key_of(changed_params)
    assert k == rs_run.key_of(json.loads(json.dumps(base))), "同样的输入必须得到同样的键"


def test_stage_parts_records_script_hash():
    root = REPO
    st = next(s for s in rs_run.spec() if s["id"] == "S7")
    parts = rs_run.stage_parts(root, st, {"maxChars": 12}, {"detector": "x"})
    assert "rs_subtitle.py" in parts["tool"], "缓存键必须含脚本文件 hash"
    assert parts["tool"]["rs_subtitle.py"] == rs_run.sha1_file(SCRIPTS / "rs_subtitle.py")


def test_diff_parts_reports_reason():
    old = {"inputs": {"a": "1"}, "tool": {"x": "1"}, "params": {}, "external": {}}
    new = {"inputs": {"a": "2"}, "tool": {"x": "1"}, "params": {}, "external": {}}
    why = rs_run.diff_parts(old, new)
    assert why and "inputs 变化 a" in why[0]


def test_stage_registry_covers_s0_to_s11():
    ids = [s["id"] for s in rs_run.spec()]
    assert ids == [f"S{i}" for i in range(12)]
    assert all(s["name"] for s in rs_run.spec())
    # 「手改字幕」的落点必须是不重新生成字幕的烧录导出段
    s8 = next(s for s in rs_run.spec() if s["id"] == "S8")
    assert s8["cmd"][0] == "rs_render.py", "S8 必须是烧录导出(用现有 ass),否则会冲掉手改字幕"


def test_status_on_missing_project(tmp_path):
    st = next(s for s in rs_run.spec() if s["id"] == "S1")
    r = rs_run.evaluate(tmp_path, st)
    assert r["status"] == "missing"


# ---------------------------------------------------------------- 变体矩阵 / 音效 / 文案 / 对齐自检

def test_variant_matrix_expand_and_safe_area():
    import rs_brand
    matrix = rs_brand.expand_matrix(["a", "b"], ["9x16", "16x9"])
    assert len(matrix) == 4
    canvas = {"width": 1080, "height": 1920}
    rect = rs_brand.logo_rect({"scale": 0.12, "anchor": "topRight"}, canvas, "9x16")
    assert rs_brand.check_safe_area(rect, canvas) == [], "默认落点不得进字幕带"
    bad = rs_brand.logo_rect({"scale": 0.12, "anchor": "bottomRight"}, canvas, "9x16")
    assert bad["y"] + bad["h"] <= int(canvas["height"] * 0.75) + 1


def test_sfx_density_limit():
    places = [{"atMs": t, "src": "assets_sfx:ding", "trigger": "enumerate", "conf": 0.8,
               "gainDb": -14} for t in (0, 2000, 4000, 20000)]
    kept, dropped = rs_sfx.enforce_density(places)
    assert len(kept) == 3 and len(dropped) == 1, (kept, dropped)
    assert "窗口" in dropped[0]["reason"]


def test_meta_truncates_and_builds_chapters(tmp_path):
    wl = make_wordline([{"start": i * 4.0, "end": i * 4.0 + 3.0,
                         "text": f"这是第{i}段讲解内容,持续一段时间。"} for i in range(20)])
    spec = rs_meta.PLATFORM_SPECS["douyin"]
    p = rs_meta.build_platform("douyin", spec, wl, title="标题" * 30, desc="简介" * 80,
                               tags=[f"t{i}" for i in range(9)])
    assert len(p["title"]) <= spec["titleMax"]
    assert len(p["desc"]) <= spec["descMax"]
    assert len(p["tags"]) == spec["tags"][1]
    assert p["warnings"]
    b = rs_meta.build_platform("bili", rs_meta.PLATFORM_SPECS["bili"], wl,
                               title="t", desc="d", tags=["a"])
    assert b["chapters"] and b["chapters"][0]["ts"] == "00:00"
    assert all(c["ts"].count(":") == 1 for c in b["chapters"])


def test_sync_offset_pass_and_fail(tmp_path):
    wl = rs_align.remap_wordline(make_wordline(SEGS), [[0, 13000]], {"version": 1})
    events, _ = rsub.events_from_wordline(wl, 12)
    rsub.write_ass(events, tmp_path / "ok.ass", "talkshow-white" if False else "subtitle-white",
                   "9x16", "1080x1920")
    parsed = rs_sync.parse_ass(tmp_path / "ok.ass")
    rows = rs_sync.check_offsets(parsed, wl)
    res = rs_sync.summarize(rows, parsed)
    assert res["pass"] is True, res
    assert res["medianMs"] <= 40

    shifted = [dict(e, start=e["start"] + 0.5, end=e["end"] + 0.5) for e in events]
    res2 = rs_sync.summarize(rs_sync.check_offsets(shifted, wl), shifted)
    assert res2["pass"] is False
    assert res2["biasMs"] >= 400, "500ms 整体平移应被识别为系统偏差"


def test_subtitle_from_wordline_uses_no_proportional_interpolation():
    """回归防线:wordline 路径下,卡时间必须来自字级时间戳(不再按字数比例摊)。"""
    wl = rs_align.remap_wordline(make_wordline(SEGS), [[0, 13000]], {"version": 1})
    events, meta = rsub.events_from_wordline(wl, 12)
    assert events
    chars = wl["chars"]
    for e in events:
        assert e["start"] * 1000 >= chars[0]["startMs"] - 1
    first = events[0]
    assert abs(first["start"] * 1000 - (chars[0]["startMs"] - 20)) <= 60, first


def test_pyramid_fix_avoids_one_char_top_line():
    lines = rsub.break_line("今天天气很好,我们一起去公园散步然后吃午饭吧", 12)
    assert all(len(l) <= 12 for l in lines)
    assert len(lines[-1]) >= 3, lines


def test_skills_docs_reference_new_scripts():
    """SKILL.md 里出现的 rs_*.py 必须真实存在(防文档漂移)。"""
    import re
    skill = (REPO / "skills/cutflow/SKILL.md").read_text(encoding="utf-8")
    for name in sorted(set(re.findall(r"(rs_[a-z_]+\.py|segmentation\.py)", skill))):
        assert (SCRIPTS / name).is_file(), f"SKILL.md 引用了不存在的脚本:{name}"


def test_rules_files_exist_and_indexed():
    """rules/ 下的规则文件必须都在 SKILL.md §8 索引里(防新增规则被遗忘)。"""
    skill = (REPO / "skills/cutflow/SKILL.md").read_text(encoding="utf-8")
    for md in sorted((REPO / "skills/cutflow/rules").glob("*.md")):
        assert md.name in skill, f"rules/{md.name} 未在 SKILL.md §8 索引"
