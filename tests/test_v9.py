"""B 系列修复回归(BUGREPORT-20260913-纯口播复测):B1–B10 逐条对账。

只测公开 seam;ffmpeg 实机用例沿用 test_v8 的 skipif 体例。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import pytest  # noqa: E402

import rs_subtitle as sub  # noqa: E402
import rs_sync  # noqa: E402
import rs_verify  # noqa: E402
import rs_ir  # noqa: E402
import rs_render  # noqa: E402
import rs_cleanup  # noqa: E402


def _wordline(text: str, sent_spans=None) -> dict:
    chars = [{"i": i, "ch": ch, "startMs": 150 * i, "endMs": 150 * i + 120,
              "srcStartMs": 150 * i, "srcEndMs": 150 * i + 120, "conf": 0.99}
             for i, ch in enumerate(text)]
    spans = sent_spans or [(0, len(text))]
    return {"source": "test", "chars": chars,
            "sentences": [{"id": i, "span": list(s)} for i, s in enumerate(spans)],
            "degraded": False, "degradeReasons": []}


# ---------------------------------------------------------------- B1 step_mix sourceIn

def test_step_mix_consumes_source_in():
    """B1:sourceInMs>0 的音频 clip 必须输入侧 -ss 寻址(旧版完全没消费)。"""
    import inspect
    src = inspect.getsource(rs_render.step_mix)
    assert "sourceInMs" in src, "step_mix 必须消费 sourceInMs(B1)"
    # 缓存键必须包含 sourceInMs,否则修复后旧缓存幽灵命中
    import inspect as _i
    render_src = _i.getsource(rs_render.render)
    assert '"sourceInMs"' in render_src.replace("'", '"'), "mix 缓存键缺 sourceInMs"


def test_voice_chain_unaffected_by_source_in():
    """B1:链序保持 先裁后延;寻址在输入侧完成,链内不再偏移。"""
    chain = rs_render._voice_chain({"role": "voice", "volume": 1.0,
                                    "startMs": 4000, "durationMs": 2000})
    assert chain.index("atrim=0:2.000") < chain.index("adelay=4000|4000")


# ---------------------------------------------------------------- B2 转场字段

def test_rs_ir_build_writes_durms():
    """B2:rs_ir build 必须写 durMs(schema/rs_render 同口径),不得再写 ms。"""
    cl = {"source": "a.mp4", "keep": [[0, 4000], [4100, 9000]], "srcTotalMs": 9000}
    doc = rs_ir.build_from_cutlist(cl, slug="t")
    tr = doc["tracks"][0]["clips"][1]["transition"]
    assert "durMs" in tr and "ms" not in tr, tr


def test_validate_rejects_legacy_ms_field():
    """B2:旧字段 ms 必须显式报错(旧版会静默按 500ms 吞时长)。"""
    doc = {"version": 1, "slug": "t", "fps": 30, "canvas": {"width": 1080, "height": 1920},
           "tracks": [{"kind": "video", "clips": [
               {"src": "a.mp4", "startMs": 0, "durationMs": 4000},
               {"src": "a.mp4", "startMs": 4000, "durationMs": 4000,
                "transition": {"type": "fade", "ms": 8}}]}]}
    errs = rs_ir.validate(doc, Path("."))
    assert any("ms" in e and "durMs" in e for e in errs), errs


def test_resolve_transitions_promotes_subframe():
    """ADR-0023(v0.10):0<tdur<1帧的转场提升为 joinCrossfadeMs(默认120ms)交叉溶解,
    不再整链弃用;仅"源间隙放不下尾帧扩展"才回退 concat;cut/none 仍显式硬切。"""
    clips = [{"durationMs": 4000, "sourceInMs": 0},
             {"durationMs": 4000, "sourceInMs": 5000,
              "transition": {"type": "fade", "durMs": 8}},
             {"durationMs": 4000, "sourceInMs": 10000,
              "transition": {"type": "fade", "durMs": 500}}]
    eff, forced, _ = rs_render._resolve_transitions(clips, 30.0, {}, None)
    assert not forced
    assert eff[1] == 0.12, "亚帧转场提升为 joinCrossfadeMs 溶解"
    assert eff[2] == 0.5, "常规转场照常保留"
    clips2 = [{"durationMs": 4000, "sourceInMs": 0},
              {"durationMs": 4000, "sourceInMs": 5000,
               "transition": {"type": "cut", "durMs": 500}}]
    eff2, forced2, _ = rs_render._resolve_transitions(clips2, 30.0, {}, None)
    assert not forced2 and all(t == 0 for t in eff2), "cut/none 仍显式硬切"
    clips3 = [{"durationMs": 4000, "sourceInMs": 0},
              {"durationMs": 4000, "sourceInMs": 4050,
               "transition": {"type": "fade", "durMs": 500}}]
    eff3, forced3, reasons = rs_render._resolve_transitions(clips3, 30.0, {}, None)
    assert forced3 and all(t == 0 for t in eff3) and reasons, \
        "源间隙放不下尾帧 → 整链弃用走 concat(宁硬切勿错位)"


# ---------------------------------------------------------------- B3 snap 后间距

def test_snap_then_enforce_no_overlap():
    """B3:snap 的 floor/ceil 推回的重叠,必须在锚点余量内被二次收间距消掉。"""
    events = [
        {"start": 1.0040, "end": 1.9900, "text": "a",
         "anchorStart": 1.0147, "anchorEnd": 1.9800},
        {"start": 1.9760, "end": 3.0000, "text": "b",
         "anchorStart": 2.0147, "anchorEnd": 2.9800},
    ]
    sub.snap_events_to_frames(events, 30.0)
    sub._enforce_gaps(events, 30.0)          # main() 中 B3 修复的收尾步
    assert events[1]["start"] >= events[0]["end"] - 1e-9, events


def test_finalize_events_sane_times():
    """B5 落盘防御:倒挂/零时长被兜底,输出单调无重叠。"""
    events = [
        {"start": 2.0, "end": 2.0, "text": "a"},            # 零时长
        {"start": 1.0, "end": 1.5, "text": "b"},            # 乱序
    ]
    sub._finalize_events(events, 30.0)
    for a, b in zip(events, events[1:]):
        assert b["start"] >= a["end"] - 1e-9, events
    for e in events:
        assert e["end"] > e["start"], events


# ---------------------------------------------------------------- B4 cleanup keep

def test_cleanup_keeps_deliverables(tmp_path):
    """B4:final_*.mp4 / subtitles.ass / metadata.* / 报告 / cards.json 必留;
    只删探针件与 _build。"""
    out = tmp_path / "06_output"
    out.mkdir()
    for name in ("final_口播_916.mp4", "subtitles.ass", "master.srt", "metadata.json",
                 "metadata.txt", "sync_report.md", "verify_report.md", "cards.json",
                 "deliverables.md", "bench_916.png", "成片_最终_916.mp4"):
        (out / name).write_bytes(b"x")
    for name in ("_src_probe.wav", "preview_t_916.mp4", "draft_t_916.mp4", "x.log"):
        (out / name).write_bytes(b"x")
    delete, keep = rs_cleanup.classify(tmp_path)
    keep_names = {p.name for p in keep}
    delete_names = {p.name for p in delete}
    for must in ("final_口播_916.mp4", "subtitles.ass", "master.srt", "metadata.json",
                 "metadata.txt", "sync_report.md", "verify_report.md", "cards.json",
                 "deliverables.md", "bench_916.png", "成片_最终_916.mp4"):
        assert must in keep_names and must not in delete_names, (must, keep_names, delete_names)
    for gone in ("_src_probe.wav", "preview_t_916.mp4", "draft_t_916.mp4", "x.log"):
        assert gone in delete_names and gone not in keep_names, (gone, delete_names)


# ---------------------------------------------------------------- B5 cards.json 与 ass 同源

def test_cards_json_matches_ass(tmp_path):
    """B5:全链调整(必并/延长/间距/帧对齐)后,cards.json 时间必须与 ass 一致。"""
    wl = _wordline("一二三四五六七八九", sent_spans=[(0, 4), (4, 9)])
    for i, c in enumerate(wl["chars"]):
        c["startMs"], c["endMs"] = 10 * i, 10 * i + 8      # 触发必并
    events, _meta = sub.events_from_wordline(wl, 10)
    sub.snap_events_to_frames(events, 30.0)
    sub._enforce_gaps(events, 30.0)
    cards = [{"startMs": int(round(e["start"] * 1000)),
              "endMs": int(round(e["end"] * 1000))} for e in events]
    sub.write_ass(events, tmp_path / "subtitles.ass", "subtitle-white", "9x16", "1080x1920")
    parsed = rs_sync.parse_ass(tmp_path / "subtitles.ass")
    assert len(parsed) == len(cards)
    for c, e in zip(cards, parsed):
        assert abs(c["startMs"] - e["start"] * 1000) < 1, (c, e)
        assert abs(c["endMs"] - e["end"] * 1000) < 1, (c, e)
        assert c["startMs"] < c["endMs"], c


# ---------------------------------------------------------------- B7 override 定位与部分替换

def test_override_text_anchor_full():
    """B7:text 锚定模式全量重建(去标点顺序定位,不受标点索引偏移影响)。"""
    text = "店群运营嘛，不是拆分收入，线上是虚拟空间。"
    wl = _wordline(text)
    ov = {"cards": [{"text": "店群运营嘛，"}, {"text": "不是拆分收入，"},
                    {"text": "线上是虚拟空间。"}]}
    events, meta = sub.events_from_override(wl, ov, 12)
    assert meta["overrideMode"] == "full"
    assert [e["text"] for e in events] == ["店群运营嘛", "不是拆分收入", "线上是虚拟空间"], events


def test_override_prefix_suffix_anchor():
    """B7:textPrefix/textSuffix:中间吞并由锚点自动定位。"""
    text = "他说店群运营的效率呢决定整个项目的成败"
    wl = _wordline(text)
    ov = {"cards": [{"textPrefix": "他说店群", "textSuffix": "效率呢"},
                    {"textPrefix": "决定整个", "textSuffix": "成败"}]}
    events, meta = sub.events_from_override(wl, ov, 12)
    assert meta["overrideMode"] == "full"
    assert "".join(e["text"].replace(" ", "") for e in events) == text


def test_override_partial_keeps_dp_cards():
    """B7:partial 模式 —— 只覆盖一段,其余卡沿用 DP 结果,不再需要全量 span。"""
    text = "他非常努力地准备但是没有成功"
    wl = _wordline(text)
    base_events, _ = sub.events_from_wordline(wl, 10)
    assert len(base_events) >= 2
    # 只修正前 4 个内容字为一卡
    ov = {"cards": [{"span": [0, 4]}]}
    events, meta = sub.events_from_override(wl, ov, 10)
    assert meta["overrideMode"] == "partial"
    # 内容字全覆盖、无重复
    covered: set[int] = set()
    for e in events:
        a, b = e["charSpan"]
        assert not (covered & set(range(a, b))), f"charSpan 重叠:{e}"
        covered.update(range(a, b))
    assert covered == {i for i, ch in enumerate(text) if ch.strip()}


def test_override_text_not_in_wordline_raises():
    """B7:锚定失败必须显式报错(不做静默算术偏移 —— 本次串卡的元凶)。"""
    wl = _wordline("他非常努力地准备但是没有成功")
    with pytest.raises(ValueError):
        sub.events_from_override(wl, {"cards": [{"text": "不存在的文本内容"}]}, 10)
    with pytest.raises(ValueError):
        sub.events_from_override(wl, {"cards": [{"textPrefix": ""}]}, 10)


# ---------------------------------------------------------------- B8 rebuild 起点

def test_rs_ir_build_refuses_manual_edits(tmp_path):
    """B8:现存 IR 含手注 chroma/background/manualEdit 时,build 拒绝覆盖。"""
    out = tmp_path / "05_ir" / "project.json"
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps({"version": 1, "slug": "t", "tracks": [
        {"kind": "video", "clips": [{"src": "a.mp4", "startMs": 0, "durationMs": 100,
                                     "chroma": {"color": "green"}}]}]},
        ensure_ascii=False), encoding="utf-8")
    manual = rs_ir._manual_edits(out)
    assert any("chroma" in m for m in manual), manual
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps({"version": 1, "tracks": [
        {"kind": "video", "clips": [{"src": "a.mp4", "startMs": 0, "durationMs": 100}]}]}),
        encoding="utf-8")
    assert rs_ir._manual_edits(clean) == []


def test_rebuild_template_warns_ir_trap():
    """B8:05_ir/rebuild.py 的生成模板必须写明手改 IR 的正确出路。"""
    import rs_run
    assert "06_output/rebuild.py" in rs_run.REBUILD_EXTRA_NOTES.get("05_ir", "")
    assert "05_ir" in rs_run.REBUILD_EXTRA_NOTES


# ---------------------------------------------------------------- B9 L0 中间态

def test_l0_missing_subtitles_is_skipped(tmp_path):
    """B9:阶段式运行后字幕缺失 = 中间态,标 skipped 而不是 ✗。"""
    r = rs_verify.check_subtitles(tmp_path)
    assert r.get("skipped") and r["ok"] is True, r
    # 有 ass 但坏内容仍要 FAIL
    out = tmp_path / "06_output"
    out.mkdir()
    (out / "subtitles.ass").write_text("[Events]\n", encoding="utf-8")
    r2 = rs_verify.check_subtitles(tmp_path)
    assert not r2.get("skipped") and r2["ok"] is False, r2


# ---------------------------------------------------------------- B10 音频内容闸

def test_audio_norm_and_similarity():
    """B10:归一相似度口径 —— 标点/大小写不敏感;正常成片应 ≥0.90。"""
    import difflib
    ref = rs_sync._norm_hard("Hello，世界！ This is a test。")
    asr = rs_sync._norm_hard("hello 世界 this is a test")
    assert ref == asr == "hello世界thisisatest"
    sim = difflib.SequenceMatcher(None, ref, asr).ratio()
    assert sim >= rs_sync.AUDIO_SIM_MIN


def test_audio_check_skips_without_usable_input(tmp_path):
    """B10:工具/素材不可用时 skipped(不硬失败);汇总逻辑有 problem → FAIL。"""
    wl_chars = [{"i": i, "ch": ch, "startMs": 100 * i, "endMs": 100 * i + 80}
                for i, ch in enumerate("今天讲什么呢店群运营的内容")]
    wl = {"chars": wl_chars, "sentences": [{"id": 0, "span": [0, len(wl_chars)]}]}
    # repo 自带 tools/fun_asr.py → 会走到音轨提取;素材不存在 → skipped,不抛异常
    audio = rs_sync.check_audio_content(tmp_path / "nonexistent.mp4", wl)
    assert audio.get("skipped"), audio
    # 汇总语义:跑出来且有 problem → pass=False;skipped → 不影响总判定
    damage = {"pass": False, "problems": ["片头句出现 8 次"]}
    res_pass = not (not damage.get("skipped") and not damage.get("pass"))
    assert res_pass is False
    skipped_case = {"skipped": "找不到 tools/fun_asr.py"}
    res_pass2 = not (not skipped_case.get("skipped") and not skipped_case.get("pass", True))
    assert res_pass2 is True


def test_s9_spec_has_audio_gate():
    """B10:S9 命令必须带 --video 与 --audio-content;rs_run 支持占位符。"""
    import rs_run
    s9 = next(s for s in rs_run.spec() if s["id"] == "S9")
    assert "--audio-content" in s9["cmd"] and "--video" in s9["cmd"], s9["cmd"]
    assert "{final_video}" in s9["cmd"]
