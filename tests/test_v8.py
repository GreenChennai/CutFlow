"""v0.8.1 回归:词边界切分 / Agent override / step_mix 链序 / artboard 口径 / rs_sync 伪重叠。

对齐 test_v4~v7 体例;seam 只测公开接口:
  segmentation.word_spans / segmentation.segment / REGRESSION
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import pytest  # noqa: E402

import segmentation as sg  # noqa: E402
import rs_subtitle as sub  # noqa: E402


def _wordline(text: str, sent_spans: list[tuple[int, int]] | None = None) -> dict:
    """构造带真实字级时间戳的 wordline(时间唯一真相源)。150ms/字,防触发必并。"""
    chars = [{"i": i, "ch": ch, "startMs": 150 * i, "endMs": 150 * i + 120,
              "srcStartMs": 150 * i, "srcEndMs": 150 * i + 120, "conf": 0.99}
             for i, ch in enumerate(text)]
    spans = sent_spans or [(0, len(text))]
    return {"source": "test", "chars": chars,
            "sentences": [{"id": i, "span": list(s)} for i, s in enumerate(spans)],
            "degraded": False, "degradeReasons": []}


def _cuts_of(text: str, max_chars: int = 12, terms=()) -> list[int]:
    res = sg.segment(text, max_chars, terms=terms)
    assert res["plans"], f"无可行切分方案:{text}"
    return list(res["plans"][0]["cuts"])


def _assert_words_intact(text: str, cuts: list[int], terms=()) -> None:
    """任何词跨度都不得被切点拦腰截断。"""
    cut_set = set(cuts)
    for a, b in sg.word_spans(text, terms=terms):
        if b - a < 2:
            continue
        for i in range(a + 1, b):
            assert i not in cut_set, f"词「{text[a:b]}」在位置 {i} 被切开(cuts={sorted(cut_set)})"


# ---------------------------------------------------------------- 1. 词边界

def test_word_spans_covers_common_two_char_words():
    """词表兜底也必须认得高频两字词(『非常』割裂是本次修的主诉)。"""
    text = "他非常努力地准备但是最后失败了"
    words = {text[a:b] for a, b in sg.word_spans(text)}
    for w in ("非常", "努力", "准备", "但是", "最后", "失败"):
        assert w in words, (w, words)


def test_word_spans_merges_terms_and_idioms():
    """调用方 terms 与内置成语必须并入词跨度。"""
    text = "这个方案便宜"
    words = {text[a:b] for a, b in sg.word_spans(text, terms=("方案",))}
    assert "方案" in words, words
    text2 = "我们守株待兔成功了"
    words2 = {text2[a:b] for a, b in sg.word_spans(text2)}
    assert "守株待兔" in words2, words2


def test_word_spans_never_spans_across_space():
    """词跨度不得横跨空格——否则会禁止空格后的合法切点。"""
    text = "处理 最终一同遭殃"
    for a, b in sg.word_spans(text):
        seg = text[a:b]
        assert " " not in seg and "\u3000" not in seg, (a, b, seg)


def test_segment_never_splits_two_char_word():
    """规格验收:两字词不跨卡(『非常』不得切成 非|常)。"""
    text = "他非常努力地准备但是没有成功最后还是失败了"
    assert len(text) > 12, "用例本身必须超过 max_chars 才会触发切分"
    cuts = _cuts_of(text)
    assert cuts, "句子超长必须被切分"
    _assert_words_intact(text, cuts)


def test_segment_user_example_words_intact_and_space_candidate():
    """用户原诉例:『…违规而被连带处理 最终一同遭殃』——空格是强候选,词不跨卡。"""
    text = "可能因为其中一家店铺违规而被连带处理 最终一同遭殃"
    k = text.index(" ")
    assert k in sg.forbidden_positions(text), "不得切在空格之前(空格挂上一卡尾)"
    assert k + 1 in sg.candidate_positions(text), "空格之后必须是强候选边界"
    cuts = _cuts_of(text)
    _assert_words_intact(text, cuts)


def test_word_internal_cut_only_when_infeasible(monkeypatch):
    """两阶段 DP:词内全禁无可行解时才降级为词内强惩罚,且必须留痕 wordFallback。"""
    text = "他说的是一段特别特别长的内容中间毫无停顿也毫无标点"
    monkeypatch.setattr(sg, "word_spans", lambda t, **k: [(0, len(t))])
    idx = list(range(len(text)))
    ct = [{"startMs": 100 * i, "endMs": 100 * i + 80} for i in range(len(text))]
    res = sg.segment(text, 8, index_map=idx, char_times=ct)
    assert res["cards"], "降级路径也必须能出卡,不能无解"
    assert res.get("wordFallback") is True, res
    # 降级后时间锚仍须完整(每卡都有字级时间)
    assert all("startMs" in c and "anchorEndMs" in c for c in res["cards"]), res["cards"]


def test_word_fallback_not_triggered_when_feasible():
    """词内全禁可解时,不得走降级。"""
    res = sg.segment("他非常努力地准备但是没有成功", 10)
    assert res["cards"], res
    assert res.get("wordFallback") is False, res


def test_regression_set_covers_two_char_words():
    """『非常』类两字词用例必须固化进回归集,防止以后又被改回去。"""
    texts = [r["text"] for r in sg.REGRESSION]
    assert any("非常" in t for t in texts), texts


def test_regression_all_cases_words_intact():
    """回归集逐条:词跨度完整 + must_not_split 在某一张卡内完整出现。"""
    for r in sg.REGRESSION:
        terms = r.get("terms", ())
        cuts = _cuts_of(r["text"], terms=terms)
        _assert_words_intact(r["text"], cuts, terms=terms)
        cards = [c["text"] for c in sg.segment(r["text"], 12, terms=terms)["cards"]]
        for term in r.get("must_not_split", ()):
            assert any(term in "".join(c.split()) for c in cards), (r["text"], term, cards)


# ---------------------------------------------------------------- 2. Agent 复核 override 闭环

def test_events_from_wordline_carry_charspan():
    """卡片输出必须带 charSpan(wordline 内容字全局索引),Agent 复核靠它定位。"""
    text = "他非常努力地准备但是没有成功"
    events, _ = sub.events_from_wordline(_wordline(text), 10)
    assert events, "必须产出卡片"
    covered: set[int] = set()
    for e in events:
        span = e.get("charSpan")
        assert span and span[0] < span[1], e
        covered.update(range(span[0], span[1]))
    # 内容字全覆盖、无重复(标点/空格除外)
    expect = {i for i, ch in enumerate(text) if ch.strip()}
    assert expect <= covered, (sorted(expect - covered),)


def test_charspan_survives_merge_short():
    """必并合卡后 charSpan 必须同步合并(时间锚与文本分组保持一致)。

    句内相邻卡合并必然超字数上限,所以用「短句 1 + 短句 2」触发必并:
    每字 10ms → 4 字卡只有 40ms < 0.83s,与后句 5 字卡合并(合计 9 ≤ 10)。
    """
    wl = _wordline("一二三四五六七八九", sent_spans=[(0, 4), (4, 9)])
    for i, c in enumerate(wl["chars"]):
        c["startMs"], c["endMs"] = 10 * i, 10 * i + 8
    events, meta = sub.events_from_wordline(wl, 10)
    assert meta["mergedShort"] >= 1, meta
    assert len(events) == 1, events
    assert events[0]["charSpan"] == [0, 9], events[0]
    covered: set[int] = set()
    for e in events:
        a, b = e["charSpan"]
        assert not (covered & set(range(a, b))), f"charSpan 重叠:{e}"
        covered.update(range(a, b))


def test_override_rebuilds_cards_from_spans():
    """override 只动 span:时间从 wordline 字级锚重建,审计记录变更。"""
    text = "他非常努力地准备但是没有成功"
    wl = _wordline(text)
    events, _ = sub.events_from_wordline(wl, 10)
    assert len(events) >= 2, events
    # Agent 修正:把前两张卡合成一张
    first_two = [events[0]["charSpan"], events[1]["charSpan"]]
    span = [first_two[0][0], first_two[1][1]]
    override = {"cards": [{"span": span, "note": "修复切词"}]
                + [{"span": e["charSpan"]} for e in events[2:]]}
    out, meta = sub.events_from_override(wl, override, 10)
    assert meta["overrideApplied"] is True
    assert meta["overrideCards"] == len(override["cards"])
    assert len(out) == len(events) - 1, (out, events)
    # 时间唯一真相源仍是 wordline:起点 ≤ 首字 startMs,终点 ≥ 末字 endMs
    e0 = out[0]
    assert e0["start"] * 1000 <= wl["chars"][span[0]]["startMs"] + 1e-6
    assert e0["end"] * 1000 >= wl["chars"][span[1] - 1]["endMs"] - 1e-6
    # 审计:每张卡记录 span/text/时间
    assert len(meta["audit"]) == len(out)
    assert meta["audit"][0]["span"] == span


def test_override_rejects_bad_spans():
    """span 非法(越界/倒置/重叠/乱序)必须显式报错,不静默吞掉、不静默重排。"""
    wl = _wordline("他非常努力地准备但是没有成功")
    with pytest.raises(ValueError):
        sub.events_from_override(wl, {"cards": [{"span": [5, 3]}]}, 10)
    with pytest.raises(ValueError):
        sub.events_from_override(wl, {"cards": [{"span": [0, 8]}, {"span": [6, 12]}]}, 10)
    with pytest.raises(ValueError):
        sub.events_from_override(wl, {"cards": [{"span": [0, 999]}]}, 10)
    with pytest.raises(ValueError):
        # 乱序但不重叠:契约要求递增,不做静默 sort
        sub.events_from_override(wl, {"cards": [{"span": [7, 12]}, {"span": [0, 6]}]}, 10)


# ---------------------------------------------------------------- 3. step_mix 音频调度

def test_voice_chain_atrim_before_adelay():
    """Bug #1:必须先裁后延——adelay 之后 atrim 会把 startMs>0 的段裁错。"""
    import rs_render
    chain = rs_render._voice_chain({"role": "voice", "volume": 1.0,
                                    "startMs": 4000, "durationMs": 2000})
    assert "adelay=4000|4000" in chain, chain
    assert "atrim=0:2.000" in chain, chain
    assert chain.index("atrim=0:2.000") < chain.index("adelay=4000|4000"), chain
    # 无 durationMs 时不得出现 atrim
    chain2 = rs_render._voice_chain({"role": "voice", "volume": 0.8, "startMs": 0})
    assert not any(c.startswith("atrim=") for c in chain2), chain2
    assert "volume=0.8" in chain2, chain2


def _has_ffmpeg() -> bool:
    try:
        import rs_common
        cand = rs_common.ffmpeg_bin()
    except SystemExit:
        cand = "ffmpeg"
    return Path(cand).is_file() or shutil.which(cand) is not None


@pytest.mark.skipif(not _has_ffmpeg(), reason="本机没有 ffmpeg,跳过")
def test_step_mix_multi_voice_keeps_full_length(tmp_path):
    """Bug #1 实测:两段 startMs>0 的人声混音后音轨必须 ≈ max(startMs+durationMs)。

    旧 bug(先延后裁)会把音轨缩到 durationMs(≈2s)。
    """
    import json as _json
    import rs_common
    import rs_render
    cfg = rs_common.load_config()
    ff = rs_common.ffmpeg_bin(cfg)
    # 底片:8s 视频 + 8s 音
    src = tmp_path / "src.mp4"
    subprocess.run([ff, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000",
                    "-t", "8", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    # 两段人声(各 2s 正弦)
    v1, v2 = tmp_path / "v1.wav", tmp_path / "v2.wav"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi",
                    "-i", "sine=frequency=880:sample_rate=48000:duration=2", str(v1)], check=True)
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi",
                    "-i", "sine=frequency=1320:sample_rate=48000:duration=2", str(v2)], check=True)
    doc = {"fps": 30, "bgm": {},
           "tracks": [{"kind": "audio", "clips": [
               {"src": str(v1), "startMs": 1000, "durationMs": 2000, "role": "voice", "volume": 1.0},
               {"src": str(v2), "startMs": 4000, "durationMs": 2000, "role": "voice", "volume": 1.0},
           ]}]}
    out = rs_render.step_mix(doc, src, tmp_path, tmp_path, cfg)
    # mkv 流可能缺 duration 元数据 → 抽出音轨转 wav 再探测(wav 必有 duration)
    wav = tmp_path / "mix_probe.wav"
    subprocess.run([ff, "-y", "-v", "error", "-i", str(out), "-vn",
                    "-c:a", "pcm_s16le", str(wav)], check=True)
    info = rs_common.ffprobe_json(wav, cfg)
    dur = float(info["format"]["duration"])
    assert 5.5 <= dur <= 6.8, f"混音音轨应 ≈6s(最后一段 4000+2000),实测 {dur:.2f}s"


# ---------------------------------------------------------------- 4. rs_artboard 三连

def _mk_card(root: Path, name: str = "cardA", with_export: bool = False) -> None:
    src = root / name / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "main.art").write_text(f"id={name}\n", encoding="utf-8")
    if with_export:
        exp = root / name / "export"
        exp.mkdir(parents=True, exist_ok=True)
        (exp / f"{name}.png").write_bytes(b"PNG")


def _manifest(tmp_path: Path, root: Path, ids: tuple[str, ...] = ("cardA", "cardB")) -> dict:
    items = [{"id": i, "project": f"{i}/src", "sourceHash": "h",
              "output": f"{i}/export/{i}.png", "kind": "png", "size": [1080, 1920]}
             for i in ids]
    return {"version": 1, "items": items}


def test_scan_writes_project_with_src_suffix(tmp_path):
    """scan 的 project 字段必须带 /src 后缀(export 的 --source 要吃 src 目录)。"""
    import rs_artboard as ab
    root = tmp_path / "art"
    _mk_card(root)
    doc = ab.scan(root, "x")
    it = doc["items"][0]
    assert it["project"] == "cardA/src", it
    assert it["output"] == "cardA/export/cardA.png", it


def test_changed_items_hash_matches_scan(tmp_path):
    """hash 口径必须一致:scan 记的 hash 与 changed/export 算的同一口径。"""
    import rs_artboard as ab
    root = tmp_path / "art"
    _mk_card(root, with_export=True)
    doc = ab.scan(root, "x")
    assert doc["items"], doc
    assert ab.changed_items(doc, root) == [], "scan 后无改动,changed 必须为空(旧版永远误报)"
    (root / "cardA" / "src" / "main.art").write_text("id=cardA\nv2\n", encoding="utf-8")
    assert [it["id"] for it in ab.changed_items(doc, root)] == ["cardA"]


def test_legacy_manifest_without_src_suffix_still_works(tmp_path):
    """旧清单(project 不带 /src)必须继续可用。"""
    import rs_artboard as ab
    root = tmp_path / "art"
    _mk_card(root, with_export=True)
    doc = {"version": 1, "items": [{"id": "cardA", "project": "cardA",
                                    "sourceHash": ab.hash_source(root / "cardA" / "src"),
                                    "output": "cardA/export/cardA.png", "kind": "png",
                                    "size": [1080, 1920]}]}
    assert ab.changed_items(doc, root) == []
    assert ab._src_dir(root, doc["items"][0]) == root / "cardA" / "src"


def test_apply_skips_unreferenced_and_matches_absolute(tmp_path):
    """默认:未引用卡片跳过+告警(不硬停);匹配容忍 绝对/相对/反斜杠。"""
    import rs_artboard as ab
    root = tmp_path / "proj"
    root.mkdir()
    _mk_card(root, "cardA", with_export=True)
    _mk_card(root, "cardB")
    doc = _manifest(root, root)
    ir = {"canvas": {"width": 1080, "height": 1920}, "tracks": [
        {"kind": "video", "clips": [
            # 绝对路径 + 反斜杠:旧版字符串全等匹配不到
            {"src": str((root / "cardA" / "export" / "cardA.png").resolve()),
             "startMs": 0, "durationMs": 2000}]}]}
    new_ir, issues, changes, skipped = ab.apply_to_ir(doc, ir, root)
    assert issues == [], issues
    assert [s["id"] for s in skipped] == ["cardB"], skipped
    assert any(c["id"] == "cardA" for c in changes), changes
    assert new_ir["tracks"][0]["clips"][0]["durationMs"] == 2000


def test_apply_strict_keeps_hard_fail(tmp_path):
    """--strict:未引用卡片恢复硬失败。"""
    import rs_artboard as ab
    root = tmp_path / "proj"
    root.mkdir()
    _mk_card(root, "cardA", with_export=True)
    _mk_card(root, "cardB")
    doc = _manifest(root, root)
    ir = {"canvas": {"width": 1080, "height": 1920}, "tracks": [
        {"kind": "video", "clips": [{"src": "cardA/export/cardA.png",
                                     "startMs": 0, "durationMs": 2000}]}]}
    _, issues, _, skipped = ab.apply_to_ir(doc, ir, root, strict=True)
    assert issues and any("没挂进 IR" in s for s in issues), issues
    assert skipped == []


def test_apply_only_filters_items(tmp_path):
    """--only 过滤:只有选中卡片参与校验,未选中的不算 missing(多变体不必拆 manifest)。"""
    import rs_artboard as ab
    root = tmp_path / "proj"
    root.mkdir()
    _mk_card(root, "cardA", with_export=True)
    _mk_card(root, "cardB")
    doc = _manifest(root, root)
    ir = {"canvas": {"width": 1080, "height": 1920}, "tracks": [
        {"kind": "video", "clips": [{"src": "cardA/export/cardA.png",
                                     "startMs": 0, "durationMs": 2000}]}]}
    _, issues, _, skipped = ab.apply_to_ir(doc, ir, root, only={"cardA"})
    assert issues == [] and skipped == []


def test_load_artboard_config_repo_root_wins(tmp_path):
    """config 查找:仓库根优先,skills/config.json 兜底只补缺(junction 错位修复)。"""
    import rs_artboard as ab
    repo = tmp_path / "repo.json"
    local = tmp_path / "skills.json"
    repo.write_text('{"a": 1, "artboard_dir": "from-repo"}', encoding="utf-8")
    local.write_text('{"artboard_dir": "from-skills", "b": 2}', encoding="utf-8")
    cfg = ab._load_artboard_config(repo, local)
    assert cfg["artboard_dir"] == "from-repo", cfg     # 仓库根优先(单一事实源)
    assert cfg["a"] == 1 and cfg["b"] == 2, cfg        # 兜底只补缺
    cfg2 = ab._load_artboard_config(tmp_path / "nope.json", local)
    assert cfg2["artboard_dir"] == "from-skills", cfg2  # 仓库根没有 → 兜底
    assert ab._load_artboard_config(tmp_path / "x.json", tmp_path / "y.json") == {}


# ---------------------------------------------------------------- 5. rs_sync 伪重叠(Bug #5)

def test_snap_resolves_subframe_collision():
    """帧取整碰撞:锚点有余量时必须消解,不能把 ≤1 帧重叠留给下游。"""
    events = [
        {"start": 1.0000, "end": 1.9900, "text": "a",
         "anchorStart": 1.0000, "anchorEnd": 1.9900},
        {"start": 1.9800, "end": 3.0000, "text": "b",
         "anchorStart": 2.0100, "anchorEnd": 2.9800},
    ]
    sub.snap_events_to_frames(events, 30.0)
    assert events[1]["start"] >= events[0]["end"], events


def test_snap_leaves_invisible_artifact_when_no_room():
    """锚点无余量时保持 ≤1 帧伪重叠(对齐精度优先),交由 rs_sync 容差放行。"""
    events = [
        {"start": 1.0000, "end": 1.9900, "text": "a",
         "anchorStart": 1.0000, "anchorEnd": 1.9900},
        {"start": 1.9800, "end": 3.0000, "text": "b",
         "anchorStart": 1.9800, "anchorEnd": 2.9800},
    ]
    sub.snap_events_to_frames(events, 30.0)
    overlap = events[0]["end"] - events[1]["start"]
    assert 0 < overlap <= 1.1 / 30.0, events


def test_sync_overlap_tolerance_frame_rounding():
    """rs_sync:3ms 级帧取整伪重叠不算 FAIL(容差 1 帧);50ms 真重叠仍 FAIL。"""
    import rs_sync
    events = [
        {"start": 1.0, "end": 2.0, "text": "a"},
        {"start": 1.997, "end": 3.0, "text": "b"},          # 3ms 伪重叠
    ]
    res = rs_sync.summarize([], events)
    assert res["overlaps"] == 0, res
    assert res["overlapTolMs"] >= 30, res
    big = [
        {"start": 1.0, "end": 2.0, "text": "a"},
        {"start": 1.95, "end": 3.0, "text": "b"},           # 50ms 真重叠
    ]
    res2 = rs_sync.summarize([], big)
    assert res2["overlaps"] == 1, res2
    # 容差可参数化:放宽到 60ms 后 50ms 也不再计
    res3 = rs_sync.summarize([], big, overlap_tol_ms=60.0)
    assert res3["overlaps"] == 0, res3
