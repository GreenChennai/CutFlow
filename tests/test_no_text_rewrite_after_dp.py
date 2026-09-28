"""第四册 T4.2 回归:后处理一律「只许放宽、不许重排」。

判据(计划文档 4.2 T4.2 / 4.3 验收):断言「后处理前后 cards[].text 拼接 ==
DP 输出文本拼接」——有真实字级时间的工程,rs_subtitle 事件层只许在释放余量内
调时间;文本被改(合并/吞并/丢弃)即红。字级信息缺失/降级才允许旧可读性后处理,
且必须留痕(T4.1c)。

运行:pytest tests/test_no_text_rewrite_after_dp.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import pytest  # noqa: E402

import segmentation as sg  # noqa: E402
import rs_subtitle as rsub  # noqa: E402


def _wordline(text: str, sent_spans=None, per: int = 150) -> dict:
    """真实字级时间戳 wordline(per ms/字,卡片不会触发估算降级)。"""
    chars = [{"i": i, "ch": ch, "startMs": per * i, "endMs": per * i + per - 30,
              "srcStartMs": per * i, "srcEndMs": per * i + per - 30, "conf": 0.99}
             for i, ch in enumerate(text)]
    spans = sent_spans or [(0, len(text))]
    return {"source": "test", "chars": chars,
            "sentences": [{"id": i, "span": list(s)} for i, s in enumerate(spans)],
            "degraded": False, "degradeReasons": []}


def _content(s: str) -> str:
    """去标点/空白后的内容字(与 rs_common.content_text 同口径的轻量版)。"""
    return "".join(ch for ch in s if ch.strip()
                   and ch not in segmentation_PUNCT_WS)


segmentation_PUNCT_WS = sg.PUNCT_WS


@pytest.mark.parametrize("case", sg.REGRESSION)
def test_event_layer_never_rewrites_dp_grouping(case):
    """T4.2:REGRESSION 全集上,事件层产出的卡 = DP 的卡(文本分组零改写)。"""
    text = case["text"]
    wl = _wordline(text)
    events, meta = rsub.events_from_wordline(wl, 12, terms=case.get("terms", ()))
    assert meta["postProcess"] == "time-only", meta["degradeReasons"]
    assert meta["mergedShort"] == 0, "有字级时间时禁止任何必并合并"
    assert meta["ghostCards"] == {"merged": 0, "dropped": []}
    # 拼接不变:事件内容字拼接 == wordline 内容字拼接(标点可能被 _clean_card 剥掉)
    got = _content("".join(e["text"] for e in events))
    want = _content("".join(c["ch"] for c in wl["chars"]))
    assert got == want, (case["text"], got, want)
    # 分组不变:事件文本序列 == 「同一字级时间输入」的 DP 卡序列
    # (T4.1 后 DP 会消费字级时间与停顿表,故对照必须喂同一份 index_map/char_times/gaps)
    per = 150
    ct = [{"startMs": per * i, "endMs": per * i + per - 30} for i in range(len(text))]
    gaps = {k: float(ct[k]["startMs"] - ct[k - 1]["endMs"])
            for k in range(1, len(text)) if ct[k]["startMs"] > ct[k - 1]["endMs"]}
    plan = sg.segment(text, 12, terms=case.get("terms", ()),
                      index_map=list(range(len(text))), char_times=ct, gaps=gaps)
    dp_cards = [_content(c["text"]) for c in plan["cards"]]
    ev_cards = [_content(e["text"]) for e in events]
    assert ev_cards == dp_cards, (case["text"], ev_cards, dp_cards)


def test_postprocessors_cannot_change_text_on_known_time_path(monkeypatch):
    """T4.2 机制断言:已知时间路径根本不调用会改文本的后处理。"""
    calls = {"merge": 0, "ghost": 0}

    def _no_merge(events, max_chars, min_dur=rsub.MIN_DUR_S):
        calls["merge"] += 1
        return events, 0

    def _no_ghost(events, max_chars, min_ms=rsub.GHOST_MIN_MS):
        calls["ghost"] += 1
        return events, 0, []

    monkeypatch.setattr(rsub, "_merge_short", _no_merge)
    monkeypatch.setattr(rsub, "_drop_ghost_cards", _no_ghost)
    wl = _wordline("他非常努力地准备但是没有成功最后还是失败了")
    rsub.events_from_wordline(wl, 12)
    assert calls == {"merge": 0, "ghost": 0}, calls


def test_short_card_becomes_unsatisfied_not_merged():
    """T4.3:字级时间真实但卡确实过短(延长余量耗尽)→ unsatisfied + 替代方案,不吞并。"""
    wl = _wordline("一二三四五六七八九", sent_spans=[(0, 4), (4, 9)], per=10)
    events, meta = rsub.events_from_wordline(wl, 10)
    # 旧契约这里会静默并成一张卡;新契约:分组保持 DP 原样
    assert len(events) == 2, [e["text"] for e in events]
    assert meta["postProcess"] == "time-only"
    issues = {u["issue"] for u in meta["unsatisfied"]}
    assert issues & {"shortCard", "ghostCard"}, meta["unsatisfied"]
    for u in meta["unsatisfied"]:
        assert u["suggestion"], "替代方案字段必填"
    # 全部进 violations 且带建议
    assert any("未自动" in v for v in meta["violations"]), meta["violations"]
    # charSpan 分区保持连续不重叠(不丢字)
    covered: set[int] = set()
    for e in events:
        a, b = e["charSpan"]
        assert not (covered & set(range(a, b)))
        covered.update(range(a, b))
    assert covered == set(range(9))


def test_degraded_wordline_keeps_legacy_postprocess_with_trace():
    """T4.1(c):字级信息缺失/降级 → 保留旧可读性后处理,但必须留痕。"""
    text = "这一段话从头到尾没有任何停顿符号所以被均匀切分成了碎片"
    wl = _wordline(text)
    wl["charTimingEstimated"] = True
    wl["degraded"] = True
    wl["degradeReasons"] = ["字级时间戳缺失:句内按均分估算(不可当字级用)"]
    events, meta = rsub.events_from_wordline(wl, 12)
    assert meta["postProcess"] == "legacy-readability"
    assert any("保留旧可读性后处理" in r for r in meta["degradeReasons"]), meta["degradeReasons"]
    got = _content("".join(e["text"] for e in events))
    want = _content(text)
    assert got == want, "即便降级路径,内容字也不得丢失"


def test_override_path_time_only_on_known_time():
    """override 回灌(有字级时间)同样走 time-only:合并计数为 0。"""
    text = "依据财税〔2003〕158号文件的规定办理退税流程"
    wl = _wordline(text)
    ov = {"cards": [{"textPrefix": "依据财税", "textSuffix": "办理退税流程"}]}
    events, meta = rsub.events_from_override(wl, ov, 12)
    assert meta["overrideMode"] == "full"
    assert meta["mergedShort"] == 0, meta
    assert meta["postProcess"] == "time-only"
    got = _content("".join(e["text"] for e in events))
    assert got == _content(text)
