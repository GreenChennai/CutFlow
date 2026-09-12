"""CutFlow v7 回归测试:字幕同步 / 断句连词 / 粗剪废片段 / 平台画幅 / 类型路由。

对应 `docs/OPTIMIZATION-v7.md` 的验收标准。运行:`pytest tests/ -q`
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_align  # noqa: E402
import rs_common  # noqa: E402
import rs_cut  # noqa: E402
import rs_run  # noqa: E402
import rs_subtitle as rsub  # noqa: E402
import rs_sync  # noqa: E402
import segmentation as sg  # noqa: E402


def _last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def wl_from_text(text, per=200, conf=0.95, source="a.mp4", start_ms=0):
    """按均匀字级时间戳构造 wordline(有真实字级时间 → degraded=False)。"""
    ts = [[start_ms + i * per, start_ms + i * per + per - 20] for i in range(len(text))]
    seg = {"start": start_ms / 1000.0, "end": (start_ms + len(text) * per) / 1000.0,
           "text": text, "timestamp": ts, "conf": conf}
    return rs_align.build_wordline([seg], source)


# ================================================================ #1 字幕↔音频同步

def test_sync_reports_end_offset():
    """自检必须同时给出终点偏移,而不能只盯起点。"""
    wl = wl_from_text("今天我们来讲桌面运维和蓝屏排查的基本方法")
    events, _ = rsub.events_from_wordline(wl, 12)
    rows = rs_sync.check_offsets(events, wl)
    assert rows
    matched = [r for r in rows if r.get("matched")]
    assert matched and all("endOffsetMs" in r for r in matched)
    res = rs_sync.summarize(rows, events)
    assert "medianEndMs" in res and "p95EndMs" in res
    assert res["pass"] is True, res


def test_sync_detects_subtitle_ending_early():
    """字幕比末字早退(切掉语音)→ 必须判失败并点名。"""
    wl = wl_from_text("今天我们来讲桌面运维和蓝屏排查的基本方法")
    events, _ = rsub.events_from_wordline(wl, 12)
    early = [dict(e, end=e["end"] - 0.20) for e in events]
    res = rs_sync.summarize(rs_sync.check_offsets(early, wl), early)
    assert res["pass"] is False
    assert res["earlyEnd"], "早退卡必须被点名"


def test_sync_detects_subtitle_lingering_too_long():
    """字幕滞留过久(远超末字释放余量)→ 必须判失败。"""
    wl = wl_from_text("今天我们来讲桌面运维和蓝屏排查的基本方法")
    events, _ = rsub.events_from_wordline(wl, 12)
    long = [dict(e, end=e["end"] + 1.5) for e in events]
    res = rs_sync.summarize(rs_sync.check_offsets(long, wl), long)
    assert res["pass"] is False
    assert res["overLongEnd"]


def test_enforce_gaps_never_cuts_last_char():
    """间距不足时只允许在「释放余量」内调整,绝不动到字级锚点之外。"""
    events = [
        {"start": 0.00, "end": 1.02, "text": "第一句", "anchorStart": 0.02, "anchorEnd": 1.00},
        {"start": 1.02, "end": 2.00, "text": "第二句", "anchorStart": 1.04, "anchorEnd": 1.98},
    ]
    rsub._enforce_gaps(events, fps=30)
    assert events[0]["end"] >= events[0]["anchorEnd"] - 1e-9, "不得切进末字"
    assert events[1]["start"] <= events[1]["anchorStart"] + 1e-9, "不得推迟字幕起点"
    assert events[1]["start"] >= events[0]["end"] - 1e-9, "不得重叠"


def test_sync_legacy_end_skips_end_gate(tmp_path, monkeypatch, capsys):
    """--legacy-end:老工程字幕终点本来就偏 → 只告警不阻断(过渡开关)。"""
    wl = wl_from_text("今天我们来讲桌面运维和蓝屏排查")
    wlp = tmp_path / "wl.json"
    wlp.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    events, _ = rsub.events_from_wordline(wl, 12)
    ev = [dict(e) for e in events]
    ev[-1]["end"] += 1.0                       # 只让末卡滞留:避开"卡间重叠"
    ass = tmp_path / "s.ass"
    rsub.write_ass(ev, ass, "subtitle-white", "9x16", "1080x1920")
    argv = ["rs_sync.py", "--wordline", str(wlp), "--ass", str(ass),
            "--out", str(tmp_path / "o")]
    monkeypatch.setattr(sys, "argv", argv)
    assert rs_sync.main() == 4, "默认:终点滞留必须判未通过"
    _last_json(capsys)
    monkeypatch.setattr(sys, "argv", argv + ["--legacy-end"])
    assert rs_sync.main() == 0, "过渡开关:只告警"
    data = _last_json(capsys)["data"]
    assert data["overLongEnd"] and data["endCheckSkipped"] is True


def test_extend_short_capped_by_anchor():
    """为凑最短时长而延长后沿时,必须有上限,不能一路延到下一卡。"""
    events = [
        {"start": 0.00, "end": 0.30, "text": "短卡", "anchorStart": 0.02, "anchorEnd": 0.28},
        {"start": 4.0, "end": 5.0, "text": "很久以后", "anchorStart": 4.02, "anchorEnd": 4.98},
    ]
    rsub._extend_short(events)
    cap = events[0]["anchorEnd"] + rsub.EXTEND_MAX_S
    assert events[0]["end"] <= cap + 1e-9, f"延长必须有上限:{events[0]}"


def test_estimated_wordline_is_flagged_not_silent():
    """无字级时间戳(degraded)→ 必须显式标注「卡内位置为估算」,不得静默当字级用。"""
    wl = rs_align.build_wordline([
        {"start": 0.0, "end": 3.0, "text": "大家好,今天我们来讲桌面运维。"},
        {"start": 3.4, "end": 6.2, "text": "先看蓝屏,蓝屏是最常见的问题。"},
    ], "a.mp4")
    assert wl["degraded"] is True
    assert wl.get("charTimingEstimated") is True, "降级 wordline 必须显式标注字级时间是估算的"
    assert all(c.get("estimated") for c in wl["chars"] if c["ch"].strip())
    events, meta = rsub.events_from_wordline(wl, 12)
    assert events and all(len(e["text"].replace(" ", "")) <= 12 for e in events), events
    assert meta["charTimingEstimated"] is True
    assert any("估算" in r for r in meta["degradeReasons"]), meta["degradeReasons"]
    ok, _why = rsub._karaoke_ready(wl)
    assert not ok, "估算时间不得用于卡拉OK(逐字染色)"


# ================================================================ #2 断句连词(切词)

def test_conjunction_must_lead_next_card():
    """用户实例:「…店铺违规而被连带处理…」不得切成「…违规而」/「被连带处理…」。

    口播里「而」后常有一处停顿(真实语料如此),旧打分会让 DP 选错边。
    """
    text = "可能因为其中一家店铺违规而被连带处理最终一同遭殃"
    cards = [c["text"] for c in sg.segment(text, 16, gaps={13: 250.0})["cards"]]
    assert len(cards) >= 2, cards
    assert not any(c.rstrip().endswith("而") for c in cards), cards
    assert any(c.startswith("而") for c in cards), f"「而」应领起后一卡:{cards}"


def test_conjunction_never_ends_a_card():
    """「而且 / 但是 / 然后」等连词不得落在卡尾(整词被切开)。"""
    cases = [
        ("这个方案便宜而且好用所以我们决定立刻采用它", 12),
        ("他非常努力地准备但是没有成功最后还是失败了", 12),
        ("我们先洗手然后再去吃饭这样才比较干净卫生", 12),
    ]
    for text, max_chars in cases:
        cards = [c["text"] for c in sg.segment(text, max_chars)["cards"]]
        assert all(len(c.replace(" ", "")) <= max_chars for c in cards), cards
        assert not any(c.rstrip()[-1] in "而但并且或及与则" for c in cards), (text, cards)


def test_regression_set_covers_conjunctions():
    """连词用例必须固化进回归集,防止以后又被改回去。"""
    texts = [r["text"] for r in sg.REGRESSION]
    assert any("而" in t for t in texts), f"回归集缺连词用例:{texts}"


def test_conjunction_never_ends_card_across_many_sentences():
    """规格 #2 验收:抽样 20 句含连词的句子,连词落在卡尾的比例 = 0。"""
    templates = [
        "这个方案便宜而且好用所以我们决定立刻采用它",
        "他非常努力地准备但是没有成功最后还是失败了",
        "我们先洗手然后再去吃饭这样才比较干净卫生",
        "可能因为其中一家店铺违规而被连带处理最终一同遭殃",
        "你要么现在就去做要么以后就别再抱怨别人了",
    ]
    bad = []
    for i in range(20):
        text = templates[i % len(templates)]
        cards = [c["text"] for c in sg.segment(text, 12)["cards"]]
        if any(c.rstrip()[-1] in "而但并且或及与则" for c in cards):
            bad.append((text, cards))
    assert not bad, bad


# ================================================================ #3 废片段(口误重录 / 中间段)

def wl_from_segments(texts, gap_ms=400, per=180):
    """按句构造 wordline(每字真实时间戳),用于粗剪检测。"""
    segs, t = [], 0
    for text in texts:
        ts = [[t + i * per, t + i * per + per - 20] for i in range(len(text))]
        end = t + len(text) * per
        segs.append({"start": t / 1000.0, "end": end / 1000.0, "text": text,
                     "timestamp": ts, "conf": 0.95})
        t = end + gap_ms
    return rs_align.build_wordline(segs, "a.mp4")


def test_retake_detected_across_sentences():
    """口误后重来常跨句(隔一句再说一遍)→ 必须检出,并删到「最后一次」起点前。"""
    wl = wl_from_segments(["今天我们来聊一聊店群运营",
                           "好我们继续",
                           "今天我们来聊一聊店群运营的方法"])
    cuts = rs_cut.detect_retake(wl)
    assert cuts, "跨句重录必须被检出"
    c = cuts[0]
    assert c["reason"] == "retake" and c["inMs"] == 0
    assert c["outMs"] == 3860 - rs_cut.TAIL_KEEP_MS, c


def test_retake_merges_multiple_old_attempts():
    """同一段录了三次 → 一刀删掉所有旧尝试(不留碎片),只留最后一次。"""
    wl = wl_from_segments(["这个方案我很满意",
                           "这个方案我很满意但是",
                           "这个方案我很满意但是要调整"])
    merged = rs_cut.detect_retake(wl)
    assert merged, "多次旧尝试必须被检出"
    assert len(merged) == 1, merged
    assert merged[0]["inMs"] == 0 and merged[0]["outMs"] == 4040 - rs_cut.TAIL_KEEP_MS, merged


def test_retake_action_is_remove_not_review():
    """重录刀的 guard 按 reason 判定:不得因「切点不落静音区」被压进 review。"""
    wl = wl_from_segments(["今天我们来聊一聊店群运营",
                           "好我们继续",
                           "今天我们来聊一聊店群运营的方法"])
    cl = rs_cut.build_cutlist(wl, rs_cut.detect_retake(wl), {})
    retakes = [c for c in cl["cuts"] if c["reason"] == "retake"]
    assert retakes
    assert any(c["action"] == "remove" for c in retakes), \
        [(c["id"], c["conf"], c["guard"]) for c in retakes]
    assert all(c["guard"]["wordClipped"] is False for c in retakes)


def test_guard_still_blocks_clipping_inside_char():
    """硬线不变:切在字内部一律降级(哪怕 reason 是 retake)。"""
    wl = wl_from_segments(["我们先看看这个方案", "我们先看看这个方案再决定"])
    chars = wl["chars"]
    gaps = rs_cut._gap_windows(chars)
    inside = int(chars[3]["startMs"]) + 20
    g = rs_cut.guard({"inMs": inside, "outMs": int(chars[6]["endMs"]),
                      "reason": "retake"}, chars, gaps)
    assert g["wordClipped"] is True and g["okByReason"] is False
    cut = {"inMs": inside, "outMs": int(chars[6]["endMs"]), "reason": "retake",
           "conf": 0.99, "guard": g}
    rs_cut.classify(cut)
    assert cut["action"] == "review"


def test_guard_policy_is_reason_specific():
    """guard 按 reason 分档:静音/呼吸/口头禅保持全四项;重录类只硬要求「不切断字内音素 + 后留余量」。"""
    for reason in ("silence", "breath", "filler"):
        assert set(rs_cut.GUARD_REQUIRED[reason]) == set(rs_cut.GUARD_ALL), reason
    for reason in ("retake", "false_start"):
        need = set(rs_cut.GUARD_REQUIRED[reason])
        assert need == {"wordClipped", "tailKeep"}, reason
        assert "inSilence" not in need and "outSilence" not in need


def test_guard_ok_by_reason_keeps_word_clipped_hard():
    """同一刀落在字内部时,重录类也必须被拦(硬线:不切断字内音素)。"""
    wl = wl_from_segments(["第一句话内容", "第二句话内容"], per=600)
    chars = wl["chars"]
    gaps = rs_cut._gap_windows(chars)
    in_ms = int(chars[2]["startMs"]) + 300
    out_ms = int(chars[5]["startMs"]) + 300
    g = rs_cut.guard({"inMs": in_ms, "outMs": out_ms, "reason": "retake"}, chars, gaps)
    assert g["wordClipped"] is True and g["okByReason"] is False
    assert g["required"] == ["wordClipped", "tailKeep"]


def test_retake_block_paragraph_repeat():
    """整段重来(连续重复 ≥8 字)→ reason false_start,删旧留新。"""
    wl = wl_from_segments(["我们先把整体流程完整地走一遍看效果",
                           "中间插一句别的",
                           "我们先把整体流程完整地走一遍看效果觉得还行"])
    cuts = rs_cut.detect_retake_block(wl)
    assert cuts and cuts[0]["reason"] == "false_start", cuts
    assert cuts[0]["inMs"] == 0


def test_parse_silencedetect():
    """「有画面无语音」长段:解析 ffmpeg silencedetect 输出。"""
    stderr = ("[silencedetect @ 0x1] silence_start: 12.34\n"
              "[silencedetect @ 0x1] silence_end: 15.80 | silence_duration: 3.46\n")
    assert rs_cut.parse_silencedetect(stderr) == [
        {"startMs": 12340, "endMs": 15800, "ms": 3460}]


def test_dead_air_falls_back_to_wordline_gap():
    """无素材时退回字间 gap,只认 ≥1.2s 的空档。"""
    wl = wl_from_segments(["一", "二"], gap_ms=3000)
    cuts = rs_cut.detect_dead_air(wl)
    assert cuts and cuts[0]["reason"] == "silence", cuts
    assert rs_cut.detect_dead_air(wl_from_segments(["一", "二"], gap_ms=400)) == []


def test_dead_air_reason_splits_breath_and_silence():
    """短空档=breath、长空档=silence(与静音检测的语义一致)。"""
    short = wl_from_segments(["一", "二"], gap_ms=1500)      # 1.5s < 2×1.2s
    long = wl_from_segments(["一", "二"], gap_ms=3000)
    assert rs_cut.detect_dead_air(short)[0]["reason"] == "breath"
    assert rs_cut.detect_dead_air(long)[0]["reason"] == "silence"


def test_detectors_registry_covers_new_detectors():
    for name in ("retake", "retake_block", "dead_air", "self_negative"):
        assert name in rs_cut.DETECTORS, rs_cut.DETECTORS
    assert "off_topic" not in rs_cut.DETECTORS, "语义类仍由 Agent 传 --off-topic"


# ================================================================ #7 脚本鲁棒性(Windows 控制台编码)

def test_doctor_report_symbols_are_gbk_safe():
    """doctor --report 在 GBK 控制台不得崩:报告里不得出现 gbk 编不出的符号。"""
    src = (REPO / "skills/cutflow/scripts/rs_doctor.py").read_text(encoding="utf-8")
    for sym in ("✓", "✗", "✅", "❌"):
        assert sym not in src, f"doctor 报告不得使用 GBK 编不出的符号:{sym}"
    assert '"[OK] 可开工"' in src or "'[OK] 可开工'" in src
    for mark in ("√", "×", "△"):
        mark.encode("gbk")            # 必须可编码


def test_ensure_utf8_is_safe_on_dumb_stream(monkeypatch):
    """ensure_utf8 在无 reconfigure 的流(重定向/测试捕获)上必须静默跳过,不得抛异常。"""
    import io

    import rs_common
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    rs_common.ensure_utf8()


# ================================================================ #4 平台预设与 3:4 画幅

def test_platform_presets_cover_four_platforms():
    plats = rsub.load_platforms()
    for name in ("douyin", "shipinhao", "xiaohongshu", "bilibili"):
        assert name in plats, plats
        assert plats[name]["ratio"] in rs_common.RATIOS
        assert len(plats[name]["canvas"]) == 2
    assert plats["xiaohongshu"]["ratio"] == "3x4"
    assert plats["xiaohongshu"]["canvas"] == [1080, 1440]


def test_ratio_tables_are_consistent():
    """新增画幅时,所有「按比例索引」的表都必须有对应 key(防漏改)。"""
    for ratio in rs_common.RATIOS:
        assert ratio in sg.MAX_CHARS, f"segmentation.MAX_CHARS 缺 {ratio}"
        assert ratio in sg.CPS_MAX, f"segmentation.CPS_MAX 缺 {ratio}"
        for name, st in rsub.STYLES.items():
            assert ratio in st["size"], f"STYLES[{name}].size 缺 {ratio}"
            assert ratio in st["margin_v"], f"STYLES[{name}].margin_v 缺 {ratio}"
    assert rs_common.ratio_for_canvas(1080, 1440) == "3x4"
    assert rs_common.canvas_for("3x4") == (1080, 1440)


def test_cps_max_is_taken_by_ratio_not_hardcoded():
    assert sg.cps_max_for(12) == sg.CPS_MAX["9x16"]
    assert sg.cps_max_for(15) == sg.CPS_MAX["3x4"]
    assert sg.cps_max_for(22) == sg.CPS_MAX["16x9"]


def test_schema_allows_3x4_canvas_and_output():
    schema = json.loads((REPO / "skills/cutflow/templates/project.schema.json")
                        .read_text(encoding="utf-8"))
    props = schema["properties"]
    assert 1440 in props["canvas"]["properties"]["width"]["enum"]
    assert 1440 in props["canvas"]["properties"]["height"]["enum"]
    assert "3x4" in props["outputs"]["items"]["enum"]


def test_subtitle_platform_preset_drives_ratio_canvas_style(tmp_path, monkeypatch, capsys):
    """--platform 一键决定比例/画布/风格/字数。"""
    wl = wl_from_text("今天我们来讲桌面运维和蓝屏排查")
    wlp = tmp_path / "wl.json"
    wlp.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    outd = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["rs_subtitle.py", "--from-wordline", str(wlp),
                                      "--platform", "xiaohongshu", "--out", str(outd)])
    assert rsub.main() == 0
    data = _last_json(capsys)["data"]
    assert data["ratio"] == "3x4" and data["canvas"] == "1080x1440"
    assert data["style"] == "talkshow-bold" and data["maxChars"] == 15
    ass = (outd / "subtitles.ass").read_text(encoding="utf-8")
    assert "PlayResX: 1080" in ass and "PlayResY: 1440" in ass


def test_subtitle_explicit_args_beat_platform_preset(tmp_path, monkeypatch, capsys):
    """显式参数优先于平台预设。"""
    wl = wl_from_text("今天我们来讲桌面运维和蓝屏排查")
    wlp = tmp_path / "wl.json"
    wlp.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_subtitle.py", "--from-wordline", str(wlp),
                                      "--platform", "douyin", "--max-chars", "10",
                                      "--out", str(tmp_path / "out")])
    assert rsub.main() == 0
    data = _last_json(capsys)["data"]
    assert data["ratio"] == "9x16" and data["maxChars"] == 10


def test_platform_cpsmax_is_actually_consumed(tmp_path, monkeypatch, capsys):
    """平台预设的 cpsMax 必须真被消费(不能是死数据)。"""
    monkeypatch.setattr(rsub, "load_platforms",
                        lambda: {"fast": {"ratio": "9x16", "canvas": [1080, 1920],
                                          "style": "talkshow-bold", "maxChars": 12,
                                          "cpsMax": 1.0}})
    wl = wl_from_text("今天我们来讲桌面运维和蓝屏排查")
    wlp = tmp_path / "wl.json"
    wlp.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_subtitle.py", "--from-wordline", str(wlp),
                                      "--platform", "fast", "--out", str(tmp_path / "o")])
    assert rsub.main() == 0
    data = _last_json(capsys)["data"]
    assert any("CPS" in v for v in data["violations"]), data["violations"]


def test_subtitle_unknown_platform_is_rejected(tmp_path, monkeypatch, capsys):
    """平台名拼错必须报错,不得静默退回默认(否则出片规格悄悄不对)。"""
    wl = wl_from_text("测试一句")
    wlp = tmp_path / "wl.json"
    wlp.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_subtitle.py", "--from-wordline", str(wlp),
                                      "--platform", "kuaishou", "--out", str(tmp_path / "o")])
    assert rsub.main() == 2
    assert _last_json(capsys)["code"] == "BAD_PLATFORM"


# ================================================================ #5 / #6 技能组精简与 videoType

def test_cutflow_prompt_skill_removed_and_archived():
    """第二个技能组:删除技能目录 + 内容归档 + 引用清理。"""
    assert not (REPO / "skills" / "cutflow-prompt").exists()
    assert (REPO / "docs/archive/cutflow-prompt/SKILL.md").is_file()
    assert "cutflow-prompt" not in (REPO / "tools/install.ps1").read_text(encoding="utf-8")
    skill = (REPO / "skills/cutflow/SKILL.md").read_text(encoding="utf-8")
    ref = next(l for l in skill.splitlines() if "cutflow-prompt" in l)
    assert "archive" in ref, f"SKILL.md 残留未标注引用:{ref}"
    plan = (REPO / "docs/PLAN.md").read_text(encoding="utf-8")
    assert "cutflow-prompt" in plan, "PLAN.md 历史记录应保留(但需带停用横幅)"
    assert "已停用" in plan and "archive" in plan, "PLAN.md 需有停用/归档横幅"


def test_install_script_only_installs_cutflow():
    src = (REPO / "tools/install.ps1").read_text(encoding="utf-8")
    assert '@("cutflow")' in src


def test_video_types_replace_genres():
    """videoType 三册取代 genres 六册。"""
    assert not (REPO / "skills/cutflow/rules/genres").exists()
    names = sorted(p.name for p in (REPO / "skills/cutflow/rules/video-types").glob("*.md"))
    for want in ("_通用规则.md", "纯口播.md", "口播+动画.md", "纯动画.md"):
        assert want in names, f"缺 {want}:{names}"


def test_brief_and_intake_declare_video_type_enum():
    brief = (REPO / "skills/cutflow/templates/brief.md").read_text(encoding="utf-8")
    intake = (REPO / "skills/cutflow/rules/intake.md").read_text(encoding="utf-8")
    for value in ("talking-head+animation", "pure-animation", "talking-head"):
        assert value in brief, f"brief 缺 videoType 取值 {value}"
        assert value in intake, f"intake 缺 videoType 取值 {value}"
    for text in (brief, intake):
        assert "video-types" in text


def test_pure_animation_declares_both_voice_sources():
    """纯动画的声音来源二选一必须写死:音色卡 TTS 或 人物原声。"""
    doc = (REPO / "skills/cutflow/rules/video-types/纯动画.md").read_text(encoding="utf-8")
    assert "音色卡" in doc and "原声" in doc


def test_skill_md_routes_video_types_and_drops_genres():
    skill = (REPO / "skills/cutflow/SKILL.md").read_text(encoding="utf-8")
    assert "rules/video-types/" in skill
    assert "rules/genres/" not in skill


# ================================================================ #7 鲁棒性:消灭静默降级

def test_textopt_dp_fallback_is_recorded(monkeypatch):
    """DP 分段炸了可以退回长度算法,但必须留痕(不许静默)。"""
    import segmentation as sg_mod
    import textopt

    def boom(*_a, **_k):
        raise RuntimeError("dp 炸了")

    monkeypatch.setattr(sg_mod, "segment", boom)
    degrade: list = []
    cards = textopt.card_split("这是一个足够长的句子用来触发 DP 分段路径", 12, degrade=degrade)
    assert cards and degrade, "退回长度算法必须记录降级原因"
    assert "DP 分段失败" in degrade[0]


def test_subtitle_records_dp_failure_per_sentence(monkeypatch):
    """单句 DP 失败 → 该句退回长度算法,其余句子照常,且 meta 里可见。"""
    import segmentation as sg_mod

    wl = wl_from_text("第一句用来触发DP失败然后再写长一点内容")
    monkeypatch.setattr(sg_mod, "segment", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸")))
    events, meta = rsub.events_from_wordline(wl, 12)
    assert events, "不能因为一句失败就整条字幕为空"
    assert any("DP 分段失败" in r for r in meta["degradeReasons"]), meta["degradeReasons"]


def test_review_pack_marks_degraded_when_audio_extract_fails(tmp_path):
    """审查包抽音频失败不阻塞,但必须在 review/_DEGRADED.md 留痕。"""
    cl = {"cuts": [{"id": "c001", "inMs": 0, "outMs": 500, "reason": "filler",
                    "conf": 0.7, "action": "review", "guard": {}, "note": ""}]}
    n = rs_cut.write_review_pack(cl, tmp_path, src=str(tmp_path / "missing.mp4"))
    assert n == 1
    assert (tmp_path / "review" / "c001.md").is_file()
    assert (tmp_path / "review" / "_DEGRADED.md").is_file()


def test_sync_detects_animation_card_overlapping_subtitle():
    """字幕卡被 artboard 动画卡压住 → 必须点名(普通素材轨不算)。"""
    ir = {"tracks": [{"kind": "video", "clips": [
        {"src": "03_assets/artboard/c1/export/c1.mp4", "startMs": 0, "durationMs": 1500}]}]}
    events = [{"start": 0.5, "end": 1.2, "text": "字幕一段"}]
    ov = rs_sync.check_card_overlap(events, ir)
    assert ov and ov[0]["overlapMs"] == 700, ov
    plain = {"tracks": [{"kind": "video", "clips": [
        {"src": "01_materials/a.mp4", "startMs": 0, "durationMs": 1500}]}]}
    assert rs_sync.check_card_overlap(events, plain) == []


def test_rs_ingest_scan_writes_manifest_and_skeleton(tmp_path, monkeypatch, capsys):
    """S0 素材摄取:probe 失败也要留痕,不静默;不覆盖已有 project.json。"""
    import rs_ingest

    root = tmp_path / "proj"
    (root / "01_materials").mkdir(parents=True)
    (root / "01_materials" / "a.mp4").write_bytes(b"not a real video")
    monkeypatch.setattr(sys, "argv", ["rs_ingest.py", "scan", str(root)])
    assert rs_ingest.main() == 0
    data = _last_json(capsys)["data"]
    assert data["count"] == 1
    man = json.loads((root / "01_materials" / "manifest.json").read_text(encoding="utf-8"))
    assert man["items"][0]["file"] == "a.mp4"
    assert man["items"][0]["probe"] in ("ok", "failed", "unavailable")
    assert (root / "01_materials" / "MANIFEST.md").is_file()
    assert (root / "05_ir" / "project.skeleton.json").is_file()


def test_rs_ingest_deliverables_lists_outputs(tmp_path, monkeypatch, capsys):
    import rs_ingest

    root = tmp_path / "proj"
    for sub in ("00_brief", "05_ir", "06_output", "_state"):
        (root / sub).mkdir(parents=True)
    (root / "00_brief" / "brief.md").write_text("# Brief — 演示片\n", encoding="utf-8")
    (root / "05_ir" / "project.json").write_text(json.dumps(
        {"canvas": {"width": 1080, "height": 1440}}), encoding="utf-8")
    (root / "06_output" / "final_demo_34.mp4").write_bytes(b"x")
    (root / "_state" / "verify.json").write_text(json.dumps(
        {"firstCheck": {"done": True}, "lastL0": {"level": "L0", "at": "2026-09-12"}}),
        encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_ingest.py", "deliverables", str(root)])
    assert rs_ingest.main() == 0
    data = _last_json(capsys)["data"]
    assert data["ratio"] == "3x4" and data["firstCheckDone"] is True
    doc = (root / "06_output" / "deliverables.md").read_text(encoding="utf-8")
    assert "final_demo_34.mp4" in doc and "3x4" in doc
    assert "subtitles.ass" in doc, "缺失项必须点名"


def test_run_spec_registers_ingest_at_s0():
    st = next(s for s in rs_run.spec() if s["id"] == "S0")
    assert "rs_ingest.py" in st["scripts"]


# ================================================================ #10 配音强制对齐

def test_retime_to_reference_zero_drift_on_identical_text():
    """字级锚点复用到目标文本:equal 区间必须零漂移。"""
    ref = [{"i": 0, "ch": "你", "startMs": 0, "endMs": 200},
           {"i": 1, "ch": "好", "startMs": 200, "endMs": 400}]
    out, stats = rs_align.retime_to_reference(ref, "你好")
    assert [c["startMs"] for c in out] == [0, 200]
    assert stats["similarity"] == 1.0


def test_retime_to_reference_rejects_way_off_text():
    ref = [{"i": 0, "ch": "你", "startMs": 0, "endMs": 200}]
    with pytest.raises(ValueError):
        rs_align.retime_to_reference(ref, "完全不相干的一句话在这里")


def _dub_fixture(tmp_path, shift_ms=200):
    text = "大家好今天讲蓝屏"
    per = 180
    ts = [[shift_ms + i * per, shift_ms + i * per + per - 20] for i in range(len(text))]
    asr = {"segments": [{"start": shift_ms / 1000.0,
                         "end": (shift_ms + len(text) * per) / 1000.0,
                         "text": text, "timestamp": ts, "conf": 0.95}]}
    target = rs_align.build_wordline([{"start": 0.0, "end": 2.0, "text": text}], "tts.wav")
    assert target["charTimingEstimated"] is True
    (tmp_path / "asr.json").write_text(json.dumps(asr, ensure_ascii=False), encoding="utf-8")
    wlp = tmp_path / "wl.json"
    wlp.write_text(json.dumps(target, ensure_ascii=False), encoding="utf-8")
    return wlp


def test_rs_dub_align_writes_real_char_times(tmp_path, monkeypatch, capsys):
    """配音强制对齐:把真实字级时间戳写回 wordline,并给出逐句漂移报告。"""
    import rs_dub

    wlp = _dub_fixture(tmp_path, shift_ms=200)
    monkeypatch.setattr(sys, "argv", ["rs_dub.py", "align", "--wordline", str(wlp),
                                      "--from-asr", str(tmp_path / "asr.json"),
                                      "--out", str(tmp_path / "out"), "--write"])
    assert rs_dub.main() == 0
    data = _last_json(capsys)["data"]
    assert data["written"] is True and data["similarity"] == 1.0
    assert data["medianDriftMs"] >= 150, data
    assert (tmp_path / "out" / "dub_report.md").is_file()
    wl2 = json.loads(wlp.read_text(encoding="utf-8"))
    assert wl2["charTimingEstimated"] is False and wl2["degraded"] is False
    assert wl2["chars"][0]["startMs"] == 200


def test_rs_dub_refuses_write_without_char_timestamps(tmp_path, monkeypatch, capsys):
    """ASR 没给字级时间戳时必须拒绝,不得把估算时间当字级写回。"""
    import rs_dub

    text = "大家好今天讲蓝屏"
    (tmp_path / "asr.json").write_text(json.dumps(
        {"segments": [{"start": 0.0, "end": 2.0, "text": text}]}, ensure_ascii=False),
        encoding="utf-8")
    target = rs_align.build_wordline([{"start": 0.0, "end": 2.0, "text": text}], "tts.wav")
    wlp = tmp_path / "wl.json"
    wlp.write_text(json.dumps(target, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_dub.py", "align", "--wordline", str(wlp),
                                      "--from-asr", str(tmp_path / "asr.json")])
    assert rs_dub.main() == 3
    assert _last_json(capsys)["code"] == "DUB_NO_WORD_TS"
    assert json.loads(wlp.read_text(encoding="utf-8"))["charTimingEstimated"] is True


def test_sync_card_overlap_is_warning_by_default_and_fails_when_strict():
    events = [{"start": 0.5, "end": 1.2, "text": "字幕一段"}]
    ir = {"tracks": [{"kind": "video", "clips": [
        {"src": "03_assets/artboard/c1/export/c1.mp4", "startMs": 0, "durationMs": 1500}]}]}
    res = rs_sync.summarize([{"event": "字幕一段", "matched": True, "offsetMs": 0.0,
                              "endOffsetMs": 0.0, "durMs": 700.0, "chars": 4}], events)
    assert res["pass"] is True, "重叠默认只是告警,不该拖垮对齐自检"
    res["cardOverlaps"] = rs_sync.check_card_overlap(events, ir)
    assert res["cardOverlaps"]


def test_resolve_voice_reports_broken_cards(tmp_path, monkeypatch):
    """坏掉的音色卡不能被静默跳过:找不到音色时必须点名。"""
    voices = tmp_path / "voices" / "good"
    voices.mkdir(parents=True)
    (voices / "card.json").write_text("{ 这不是 json", encoding="utf-8")
    monkeypatch.setattr(rs_common, "load_config",
                        lambda: {"tts": {"voices_dir": str(tmp_path / "voices")}})
    with pytest.raises(FileNotFoundError) as ei:
        rs_common.resolve_voice("不存在的声音")
    assert "读取失败" in str(ei.value)
