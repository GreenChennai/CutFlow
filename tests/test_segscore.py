"""第四册 T4.7 回归:断句质量分 segscore(机械可算、可回归、可比较)。

判据(计划文档 4.2 T4.7):
  · S = w1*标点边界率 + w2*(1-词内切率) + w3*(1-连词收尾率) + w4*节拍达标率
        + w5*(1-违规率),机械可算;
  · 同输入重复运行 S 一致(可回归);
  · rs_subtitle 输出 segscore(随 meta 落盘)。

[数] 判据「20 条真实口播上 S 提升 ≥15%」属实机素材验收,单测覆盖机械性质。

运行:pytest tests/test_segscore.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import segmentation as sg  # noqa: E402
import rs_subtitle as rsub  # noqa: E402


def test_segscore_deterministic_same_input():
    """同输入重复运行 S 一致(可回归基线的资格条件)。"""
    sen = [{"text": "他非常努力地准备但是没有成功", "cuts": [8], "gaps": {}}]
    a = sg.segscore(sen, [1.5, 2.0], 0)
    b = sg.segscore(sen, [1.5, 2.0], 0)
    assert a == b and a["score"] == a["score"]  # 相等且非 NaN


def test_segscore_in_unit_interval():
    sen = [{"text": "先做人群画像，再设计脚本，才是正确顺序", "cuts": [7, 12], "gaps": {}}]
    for viol in (0, 2, 10):
        s = sg.segscore(sen, [1.5, 2.0, 1.0], viol)
        assert 0.0 <= s["score"] <= 1.0, s


def test_segscore_rewards_punct_and_word_boundaries():
    """标点边界 + 非词内切 → 高分;词内切 + 连词收尾 → 低分。"""
    good = sg.segscore([{"text": "先做人群画像，再设计脚本", "cuts": [7], "gaps": {}}],
                       [2.0, 2.0], 0)
    bad = sg.segscore([{"text": "他非常努力地准备但是最后失败", "cuts": [2], "gaps": {}}],
                      [0.2, 6.0], 3)
    assert good["score"] > bad["score"], (good["score"], bad["score"])
    # 分量方向:词内切率拉低 word 分量(切点落在「非常」内部)
    assert bad["components"]["word"] < good["components"]["word"]


def test_segscore_weights_sum_to_one():
    assert abs(sum(sg.SEGSCORE_WEIGHTS.values()) - 1.0) < 1e-9
    assert sg.segscore([{"text": "一句话", "cuts": [], "gaps": {}}], [2.0], 0)["weights"] \
        == sg.SEGSCORE_WEIGHTS


def test_segscore_beat_rate_uses_policy_range():
    """节拍达标率按策略表 beatRangeS 判定(时长在区间内计达标)。"""
    sen = [{"text": "一句话", "cuts": [], "gaps": {}}]
    in_beat = sg.segscore(sen, [2.5], 0)["components"]["beat"]
    out_beat = sg.segscore(sen, [0.2], 0)["components"]["beat"]
    assert in_beat == 1.0 and out_beat == 0.0


def test_segscore_empty_inputs_safe():
    s = sg.segscore([], [], 0)
    assert s["score"] == 0.0 and s["cards"] == 0


# ---------------------------------------------------------------- rs_subtitle 输出

def _wordline(text: str, per: int = 150) -> dict:
    chars = [{"i": i, "ch": ch, "startMs": per * i, "endMs": per * i + per - 30,
              "srcStartMs": per * i, "srcEndMs": per * i + per - 30, "conf": 0.99}
             for i, ch in enumerate(text)]
    return {"source": "test", "chars": chars,
            "sentences": [{"id": 0, "span": [0, len(text)]}],
            "degraded": False, "degradeReasons": []}


def test_events_from_wordline_emits_segscore():
    """rs_subtitle 的 meta 必须带 segscore(score + components + weights)。"""
    text = "他非常努力地准备但是没有成功最后还是失败了"
    events, meta = rsub.events_from_wordline(_wordline(text), 12)
    sc = meta["segscore"]
    assert sc and 0.0 <= sc["score"] <= 1.0
    assert set(sc["components"]) == {"punct", "word", "conj", "beat", "viol"}
    assert sc["cards"] == len(events)


def test_segscore_stable_across_reruns():
    """同 wordline 重跑(含 CLI 同链路)S 一致——回归基线可复现。"""
    text = "这个方案的转化效率比上一版高出了整整一倍还多"
    _, m1 = rsub.events_from_wordline(_wordline(text), 12)
    _, m2 = rsub.events_from_wordline(_wordline(text), 12)
    assert m1["segscore"] == m2["segscore"]
