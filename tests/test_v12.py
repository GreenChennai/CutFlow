"""v0.12 迭代回归:安信德 GEO 纯动画实测必修批(B1-B8)+ 立项(I1/I2/I7)。

对账:docs/HANDOFF-v0.12-安信德GEO实测迭代与修复.md §2/§3/§6。
ffmpeg 实机用例沿用 test_v8/test_v10/test_v11 的 skipif 体例;只测公开 seam。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "cutflow" / "scripts"))

import pytest  # noqa: E402

import rs_align  # noqa: E402
import rs_common  # noqa: E402
import rs_cut  # noqa: E402
import rs_ir  # noqa: E402
import rs_render  # noqa: E402
import rs_subtitle  # noqa: E402
import rs_sync  # noqa: E402
import rs_verify  # noqa: E402
import segmentation as sg  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or str(REPO / ".." / "Tools" / "ffmpeg" / "bin" / "ffmpeg.exe")
CFG = {"ffmpeg_dir": str(Path(FFMPEG).parent)} if FFMPEG else {}


def _gen(path: Path, args: list[str]) -> Path:
    p = subprocess.run([FFMPEG, "-y", "-v", "error", *args], capture_output=True, text=True)
    if p.returncode != 0:
        pytest.skip(f"lavfi 不可用:{p.stderr[-120:]}".encode("ascii", "replace").decode())
    return path


def _wordline(text: str, step_ms: int = 300, dur_ms: int = 250) -> dict:
    """逐字符对应的 wordline(标点/空白也算 chars 条目——_sentence_slices 契约)。"""
    chars = [{"i": i, "ch": ch, "startMs": i * step_ms, "endMs": i * step_ms + dur_ms}
             for i, ch in enumerate(text)]
    return {"chars": chars, "srcDurationMs": len(text) * step_ms, "source": "a.mp4"}


# ================================================================ B1 strip ↔ index_map

def test_segment_strip_keeps_index_map_aligned():
    """B1:text 被 strip 剥掉句首/尾空白后,index_map 必须双侧同步裁剪,
    否则 charSpan/字级时间系统性偏移 1(安信德:59/68 卡终点早退)。"""
    body = "今天天气真不错呀,我们出去玩一会儿吧吧吧"
    for raw in ("\u3000" + body + "\u3000", "  " + body, body + "\u3000", "\u3000" + body):
        chars = [{"i": i, "ch": ch, "startMs": i * 120, "endMs": i * 120 + 100}
                 for i, ch in enumerate(raw)]
        res = sg.segment(raw, 12, index_map=list(range(len(raw))), char_times=chars)
        first, last = res["cards"][0], res["cards"][-1]
        i_body0 = raw.index(body[0])
        i_body_last = raw.rindex(body[-1])
        # 首卡 charSpan 必须锚到正文首字(不是被剥掉的空白),末卡锚到正文末字
        assert first["charSpan"][0] == i_body0, (raw, first["charSpan"])
        assert first["anchorStartMs"] == chars[i_body0]["startMs"], (raw, first)
        assert last["charSpan"][1] == i_body_last + 1, (raw, last["charSpan"])
        assert last["anchorEndMs"] == chars[i_body_last]["endMs"], (raw, last)
        # 无空白时零偏移(哨兵:修复本身不得移动正常句的坐标)
        assert sg.segment(body, 12, index_map=list(range(len(body))),
                          char_times=_wordline(body)["chars"])["cards"][0]["charSpan"][0] == 0


# ================================================================ B2 中文数字折叠

@pytest.mark.parametrize("src,want", [
    ("一千五百", "1500"), ("一万零八百", "10800"), ("两千九百八十", "2980"),
    ("四点三", "4.3"), ("四九八零", "4980"), ("十五", "15"), ("一百零三", "103"),
    ("零点五", "0.5"), ("十万", "100000"), ("SEO", "seo"), ("1500", "1500"),
    ("这一点很好", "这一点很好"),           # 「点」单独出现不折叠
])
def test_norm_hard_folds_cn_numbers(src, want):
    """B2:值读/位读/小数三种读法都要折叠(0.888<0.90 误报的根源全在这三类)。"""
    assert rs_sync._norm_hard(src) == want


def test_audio_content_sim_improves_with_fold():
    """B2:ref 阿拉伯 / ASR 中文数字的良性差异,折叠后必须 ≥0.94(不许调阈值)。"""
    import difflib
    ref = "预算一千五百元,增长四点三个点,编号四九八零,总计一万零八百"
    asr = "预算1500元,增长4.3个点,编号4980,总计10800"
    folded = difflib.SequenceMatcher(None, rs_sync._norm_hard(ref),
                                     rs_sync._norm_hard(asr)).ratio()
    assert folded >= 0.94, folded


# ================================================================ B3 ducking asplit

def test_mix_ducking_graph_has_asplit():
    """B3 字符串护栏:ducking 滤镜图必须 asplit 出两路人声,标签各只消费一次。"""
    import inspect
    src = inspect.getsource(rs_render.step_mix)
    assert "asplit=2" in src, "ducking 路径缺 asplit(标签二次消费 = MIX_FAIL a1)"
    assert "[voice_m][bgm]amix" in src
    # 旧事故形态不得回归:sidechaincompress 直接吃 amix 输入标签
    assert "sidechaincompress=threshold=0.02:ratio=6:attack=60:release=500[bgm]" in src


@pytest.mark.skipif(not Path(FFMPEG).is_file(), reason="ffmpeg 不可用")
def test_mix_ducking_renders(tmp_path):
    """B3 实机:1 段人声 + BGM + ducking:true → step_mix 必须 returncode=0 且
    输出时长 ≈ 人声时长(修复前报 `Stream specifier 'a1' matches no streams`)。"""
    voice = _gen(tmp_path / "voice.mp4",
                 ["-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                  "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=30:d=2",
                  "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                  "-shortest", str(tmp_path / "voice.mp4")])
    bgm = _gen(tmp_path / "bgm.m4a",
               ["-f", "lavfi", "-i", "sine=frequency=220:duration=10",
                "-c:a", "aac", str(tmp_path / "bgm.m4a")])
    doc = {"fps": 30, "bgm": {"src": str(bgm), "gainDb": -18, "ducking": True},
           "tracks": [{"kind": "video", "clips": []},
                      {"kind": "audio", "clips": [{"src": str(voice), "startMs": 0,
                                                   "durationMs": 2000, "role": "voice"}]}]}
    out = rs_render.step_mix(doc, voice, tmp_path, tmp_path, CFG)
    d = rs_common.media_duration_s(out, CFG)
    assert abs(d - 2.0) < 0.3, f"输出时长应≈人声 2s,实测 {d}"


# ================================================================ B4 ass 缺失 WARN

def test_render_warns_when_ass_missing():
    """B4:IR 只有 subtitle.source 没有 subtitle.ass → 渲染必须显式 WARN,不许静默。"""
    doc = {"subtitle": {"source": "05_ir/wordline.json"}}
    warnings: list[str] = []
    out = rs_render.step_subtitle(doc, Path("in.mp4"), Path("."), {}, warnings)
    assert out == Path("in.mp4"), "无 ass 时不得产出新文件"
    assert any("subtitle.ass" in w and "不会烧录字幕" in w for w in warnings), warnings
    # source 也没有(有意不烧字幕):不告警
    warnings2: list[str] = []
    rs_render.step_subtitle({"subtitle": {}}, Path("in.mp4"), Path("."), {}, warnings2)
    assert not warnings2


# ================================================================ B5 rs_verify 纳入 QC

def test_verify_l0_includes_qc_when_video_present(tmp_path, monkeypatch):
    """B5:成片存在 → L0 checks 必须出现 QC 项(rules/verify.md 早已列为 L0 判据);
    QC 不可用 → skipped 留痕,不硬失败(ADR-0021 失败语义)。"""
    (tmp_path / "06_output").mkdir()
    (tmp_path / "06_output" / "final_x_169.mp4").write_bytes(b"not a real video")
    monkeypatch.setattr(rs_sync, "run_qc",
                        lambda video, cfg=None: (_ for _ in ()).throw(
                            SystemExit(3)))          # 模拟 ffprobe 缺失 die()
    c = rs_verify.check_qc(tmp_path)
    assert c.get("skipped"), "工具不可用必须 skipped 留痕"
    assert c["ok"] is True, "闸缺席不硬失败"

    monkeypatch.setattr(rs_sync, "run_qc",
                        lambda video, cfg=None: {"pass": False, "checks": {
                            "black": {"pass": False, "rule": "片中黑帧"}}})
    c2 = rs_verify.check_qc(tmp_path)
    assert c2["ok"] is False and "black" in c2["detail"]
    assert "成片体检" in c2["name"]
    assert any(getattr(fn, "__name__", "") == "check_qc" for fn in rs_verify.L0_CHECKS)


# ================================================================ B6 孤卡

def test_no_three_char_orphan_card_on_dash_sentence():
    """B6:破折/短语收尾切出的 3 字孤卡必须并入前卡(REGRESSION 固化)。"""
    res = sg.segment("这个方案真的非常不错所以我们最终决定采用它了说谁好", 12)
    cards = [c["text"].replace(" ", "") for c in res["cards"]]
    assert all(len(c) >= 4 for c in cards), cards
    assert not any("orphan" in v for v in res["violations"]), res["violations"]


def test_orphan_card_reported_in_violations():
    """B6:末卡过短且并入前卡超上限 → violations 显式记 orphan-card,不静默。"""
    n = sg._merge_orphan_tail([{"i": 0, "start": 0, "end": 22, "text": "一二三四五六七八九十一二三四五六七八九十"},
                               {"i": 1, "start": 22, "end": 25, "text": "说谁好"}], 22)
    assert n[1] and "orphan-card" in n[1], n
    assert len(n[0]) == 2, "合不下时不得强行合并"
    merged = sg._merge_orphan_tail([{"i": 0, "start": 0, "end": 18, "text": "一二三四五六七八九十"},
                                    {"i": 1, "start": 18, "end": 21, "text": "说谁好"}], 22)
    assert merged[1] is None and len(merged[0]) == 1
    assert merged[0][0]["text"] == "一二三四五六七八九十说谁好"


def test_regression_set_covers_orphan_tail():
    """B6 用例固化进回归集(防以后又被改回去)。"""
    texts = [r["text"] for r in sg.REGRESSION]
    assert any("说谁好" in t for t in texts), texts


# ================================================================ B7 rs_align smooth

def test_align_smooth_punct_zero_width_monotonic():
    """B7:标点零宽(end=start+1)、无 endMs<=startMs、startMs 单调不减。"""
    wl = _wordline("你好,世界。再见!", 200, 180)
    doc, stats = rs_align.smooth_wordline(wl)
    chars = doc["chars"]
    assert stats["zeroWidthPunct"] == 3
    for c in chars:
        assert int(c["endMs"]) > int(c["startMs"]), c
    starts = [int(c["startMs"]) for c in chars]
    assert starts == sorted(starts)
    assert doc["smooth"]["applied"] is True
    # 内容字时间不动(零漂移)
    assert chars[0]["startMs"] == 0 and chars[1]["endMs"] == 380


def test_align_smooth_clamps_overlap():
    """B7:相邻内容字重叠 → 前字 endMs 收到后字 startMs。"""
    wl = {"chars": [
        {"i": 0, "ch": "今", "startMs": 0, "endMs": 500},
        {"i": 1, "ch": "天", "startMs": 300, "endMs": 900},     # 重叠 200ms
        {"i": 2, "ch": "好", "startMs": 900, "endMs": 900},     # 零宽病态
    ], "gaps": [], "sentences": []}
    doc, stats = rs_align.smooth_wordline(wl)
    a, b, c = doc["chars"]
    assert int(a["endMs"]) <= int(b["startMs"]), (a, b)
    assert int(c["endMs"]) > int(c["startMs"]), "最小宽度保底必须修复零宽字"
    assert stats["clampedOverlap"] >= 1 and stats["minWidthFixed"] == 1


# ================================================================ I1 热词透传

def test_align_passes_hotwords_to_asr(tmp_path, monkeypatch):
    """I1:--hotwords 必须透传到自带 ASR 命令行(默认模型 paraformer-zh=SeACo,
    实测吃热词;tools/fun_asr.py name_maps 映射即证据)。"""
    captured = {}

    def fake_run(media, cfg, backend="auto", max_end_sil=0, hotwords=""):
        captured["cmd_hotwords"] = hotwords
        return [{"start": 0.0, "end": 1.0, "text": "安信德"}], {"backend": "pkg"}

    monkeypatch.setattr(rs_align, "_from_media", fake_run)
    media = tmp_path / "a.wav"
    media.write_bytes(b"")
    out = tmp_path / "wl.json"
    argv = ["rs_align.py", "build", "--media", str(media), "--hotwords", "安信德 GEO优化",
            "--out", str(out)]
    monkeypatch.setattr(sys, "argv", argv)
    rs_align.main()
    assert captured["cmd_hotwords"] == "安信德 GEO优化"
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["asr"]["hotwords"] == "安信德 GEO优化", "wordline.asr.hotwords 必须留痕"


def test_align_terms_file_merges_hotwords(tmp_path, monkeypatch):
    """I1:--terms-file(每行一词,# 注释)与 --hotwords 合并去重。"""
    captured = {}

    def fake_run(media, cfg, backend="auto", max_end_sil=0, hotwords=""):
        captured["hw"] = hotwords
        return [{"start": 0.0, "end": 1.0, "text": "x"}], {}

    monkeypatch.setattr(rs_align, "_from_media", fake_run)
    terms = tmp_path / "terms.txt"
    terms.write_text("# 术语表\n安信德\nGEO优化\n", encoding="utf-8")
    media = tmp_path / "a.wav"
    media.write_bytes(b"")
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "build", "--media", str(media),
                                      "--hotwords", "安信德", "--terms-file", str(terms),
                                      "--out", str(tmp_path / "wl.json")])
    rs_align.main()
    assert captured["hw"] == "安信德 GEO优化"


# ================================================================ I2 rs_cut --from-text

def test_cut_from_text_builds_keep_intervals():
    """I2:引文顺序锚定 → keep=引文区间;guard 全过则两刀直达 remove。"""
    chars = [{"i": i, "ch": ch, "startMs": i * 300, "endMs": i * 300 + 180}
             for i, ch in enumerate("今天我们讲第三步的具体操作然后是第四步的收尾工作")]
    chars.append({"i": len(chars), "ch": "。", "startMs": len(chars) * 300,
                  "endMs": len(chars) * 300 + 180})
    wl = {"chars": chars, "srcDurationMs": len(chars) * 300, "source": "a.mp4"}
    cl = rs_cut.cut_from_text(wl, "第三步的具体操作然后是第四步")
    a_ms, b_ms = cl["fromText"]["ms"]
    assert cl["keep"] == [[a_ms - 60, b_ms]], cl["keep"]   # 前刀借 60ms 释放余量
    assert all(c["action"] == "remove" for c in cl["cuts"])
    assert cl["fromText"]["quote"] == "第三步的具体操作然后是第四步"
    # 乱序引文必须拒绝(绝不模糊匹配)
    with pytest.raises(ValueError, match="顺序锚定|无法"):
        rs_cut.cut_from_text(wl, "第四步然后第三步")
    # 窄 gap(20ms):出点借不进 gap → 前刀保守 review(宁可漏删)
    chars2 = [{"i": i, "ch": ch, "startMs": i * 200, "endMs": i * 200 + 180}
              for i, ch in enumerate("今天我们讲第三步的具体操作然后是第四步的收尾工作")]
    chars2.append({"i": len(chars2), "ch": "。", "startMs": len(chars2) * 200,
                   "endMs": len(chars2) * 200 + 180})
    cl2 = rs_cut.cut_from_text({"chars": chars2, "srcDurationMs": len(chars2) * 200,
                                "source": "a"}, "第三步的具体操作然后是第四步")
    assert any(c["action"] == "review" for c in cl2["cuts"])


# ================================================================ I7 纯动画 IR 组装器

def test_assign_card_groups_consumes_anchor():
    """I7:锚点命中后消费 len(k) 个字——「交给安信德GEO优化系统」不得让
    「安信德GEO优化系统」锚点在原处重复触发(bug #9 家族)。"""
    text = "现在交给安信德GEO优化系统就够了最后说说安信德GEO优化系统的优势"
    chars = [{"i": i, "ch": ch, "startMs": i * 150, "endMs": i * 150 + 120}
             for i, ch in enumerate(text)]
    groups = rs_ir.assign_card_groups(chars, [
        {"card": "c05", "match": "交给安信德"},
        {"card": "c19", "match": "安信德GEO优化系统"}])
    assert [g["card"] for g in groups] == ["c05", "c19"]
    assert groups[1]["startMs"] // 150 >= text.rindex("最后说说"), "c19 必须命中独立出现的第二处"
    # 单处出现(被前缀吞掉)→ c19 空组 → 显式报错
    with pytest.raises(ValueError, match="未命中"):
        rs_ir.assign_card_groups(chars[:17], [
            {"card": "c05", "match": "交给安信德"},
            {"card": "c19", "match": "安信德GEO优化系统"}])


def test_assign_card_groups_merges_adjacent_same_card():
    """I7:同卡多条锚点相邻自动合并(c16「八成客户」「先布局」);不相邻 → 报错。"""
    text = "很多流量涌进现在八成客户先布局最后说说优势"
    chars = [{"i": i, "ch": ch, "startMs": i * 150, "endMs": i * 150 + 120}
             for i, ch in enumerate(text)]
    groups = rs_ir.assign_card_groups(chars, [
        {"card": "c01", "match": "涌进"},
        {"card": "c16", "match": "八成客户"},
        {"card": "c16", "match": "先布局"},
        {"card": "c20", "match": "优势"}])
    assert [g["card"] for g in groups] == ["c01", "c16", "c20"], groups
    with pytest.raises(ValueError, match="不相邻"):
        rs_ir.assign_card_groups(chars, [
            {"card": "c16", "match": "涌进"},
            {"card": "c01", "match": "八成客户"},
            {"card": "c16", "match": "优势"}])


@pytest.mark.skipif(not Path(FFMPEG).is_file(), reason="ffmpeg 不可用")
def test_build_from_cards_end_to_end(tmp_path):
    """I7 验收:3 卡 + 3 段旁白最小工程,一条 build_from_cards 出 IR 且 validate 全绿;
    卡时间 = 停顿中点边界,字幕两字段写全。"""
    (tmp_path / "cards").mkdir()
    (tmp_path / "06_output").mkdir()
    (tmp_path / "06_output" / "subtitles.ass").write_text("[Script Info]", encoding="utf-8")
    for i in (1, 2, 3):
        _gen(tmp_path / "cards" / f"c0{i}.mp4",
             ["-f", "lavfi", "-i", "testsrc2=s=160x90:r=30:d=1", "-c:v", "libx264",
              "-preset", "ultrafast", str(tmp_path / "cards" / f"c0{i}.mp4")])
    manifest = {"items": [{"id": f"c0{i}", "output": f"cards/c0{i}.mp4", "kind": "mp4"}
                          for i in (1, 2, 3)]}
    text = "第一部分讲背景第二部分讲方法第三部分讲总结"
    chars = [{"i": i, "ch": ch, "startMs": i * 300, "endMs": i * 300 + 250}
             for i, ch in enumerate(text)]
    wl = {"chars": chars, "srcDurationMs": len(chars) * 300, "source": "vo.wav"}
    doc = rs_ir.build_from_cards(
        manifest, wl, [{"card": "c01", "match": "第一部分"},
                       {"card": "c02", "match": "第二部分"},
                       {"card": "c03", "match": "第三部分"}],
        slug="t", ratio="16x9", voice_ms=len(chars) * 300, base_dir=tmp_path)
    errs = rs_ir.validate(doc, tmp_path)
    assert not errs, errs
    clips = doc["tracks"][0]["clips"]
    assert len(clips) == 3
    assert clips[0]["durationMs"] == clips[0]["startMs"] + clips[1]["durationMs"] - clips[1]["startMs"] \
        or clips[1]["startMs"] == clips[0]["durationMs"], "卡首尾相接无缝"
    assert doc["subtitle"]["ass"] == "06_output/subtitles.ass" and doc["subtitle"]["source"], \
        "B4 教训:subtitle.ass 必须写全"
    assert doc["_meta"]["generatedFrom"] == "cards"
    # 卡路径写工程根相对(与 rs_artboard --apply 的归一化挂点匹配同口径)
    assert all(not Path(c["src"]).is_absolute() for c in clips)


@pytest.mark.skipif(not Path(FFMPEG).is_file(), reason="ffmpeg 不可用")
def test_freeze_frame_extends_segment(tmp_path):
    """I7 渲染端:freezeMs → 段长 = durationMs(-t 在输入侧,B8 教训)。
    CACHE_VER 已 +1:freezeMs 必须进 seg 缓存键。"""
    card = _gen(tmp_path / "card.mp4",
                ["-f", "lavfi", "-i", "testsrc2=s=160x90:r=30:d=1",
                 "-c:v", "libx264", "-preset", "ultrafast", str(tmp_path / "card.mp4")])
    doc = {"fps": 30, "tracks": [{"kind": "video", "clips": [
        {"src": str(card), "startMs": 0, "durationMs": 2000, "sourceInMs": 0,
         "freezeMs": 500}]}]}
    build = tmp_path / "build"
    build.mkdir()
    segs, _, _ = rs_render.step_segment(doc, "16x9", build, tmp_path, {}, [], use_cache=False)
    d = rs_common.media_duration_s(segs[0], CFG)
    assert d >= 1.9, f"冻结补长后段长应≈2s,实测 {d}"
    # freezeMs 入缓存键:同 clip 改 freezeMs 必须换键
    k0 = rs_render.seg_key(doc["tracks"][0]["clips"][0], doc, 1920, 1080, "fp")
    doc["tracks"][0]["clips"][0]["freezeMs"] = 900
    k1 = rs_render.seg_key(doc["tracks"][0]["clips"][0], doc, 1920, 1080, "fp")
    assert k0 != k1


# ================================================================ 锚定共享(W5 同族)

def test_content_index_shared_with_override():
    """rs_subtitle 的文本锚定已委托 rs_common:口径统一,三处实现归一。"""
    chars = [{"i": 0, "ch": "，"}, {"i": 1, "ch": "今"}, {"i": 2, "ch": "天"},
             {"i": 3, "ch": "，"}, {"i": 4, "ch": "好"}]
    s, idx = rs_common.content_index(chars)
    assert s == "今天好" and idx == [1, 2, 4]
    assert rs_subtitle._content_index(chars) == (s, idx)
    assert rs_subtitle._normalize_card_text("，今天，") == "今天"
    assert rs_common.anchor_span(s, idx, "今天") == (1, 3)
    with pytest.raises(ValueError):
        rs_common.anchor_span(s, idx, "天今")
