"""第四册 T4.1 回归:约束(最短时长带权惩罚 + 幽灵卡禁止边界)统一进 DP。

判据(计划文档 4.2 T4.1):构造 ≥20 个「必并 / 幽灵卡」样例,
**DP 直接产出合法解的比例 ≥90%**(旧契约里这些全靠 rs_subtitle 后处理并卡/吞并,
即根因 R1 的「门禁强制断句」来源)。

运行:pytest tests/test_dp_constraints.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import segmentation as sg  # noqa: E402


def make_char_times(text: str, per: int = 140, collapse: set[int] | None = None,
                    pause_at: dict[int, int] | None = None) -> list[dict]:
    """构造字级时间:per ms/字;collapse 集合内的字坍缩为 5ms(幽灵区);
    pause_at = {字下标: 额外停顿 ms}。"""
    out: list[dict] = []
    t = 0
    for i, _ch in enumerate(text):
        if pause_at and i in pause_at:
            t += pause_at[i]
        d = 5 if (collapse and i in collapse) else per
        out.append({"startMs": t, "endMs": t + d})
        t += d
    return out


# 组 A(10 例):短卡诱导——句首强标点/短停顿会把老 DP 引向「前 4-5 字」边界,
# 直切必出 <0.83s 短卡;合法解只存在于中段无信号处(要付非候选代价)。
SHORT_CARD_CASES = [
    "好，今天这三款爆品的链接都放在评论区里了",
    "嗯，昨天直播间在线人数直接冲破了五千大关",
    "对，这个方法我用了三年从来没有失过手今天分享",
    "哈，很多新手商家一上来就急着投流量全打水漂",
    "好，我们先把素材导入时间线再去做粗剪的决策",
    "嗯，账号的定位决定了系统给你推送什么样的人群",
    "好，先把模板下载到本地然后才能离线使用它",
    "哦，导出之前务必检查响度和真峰这两个指标",
    "诶，先把字幕的断句跟着语音停顿走别随机切",
    "喂，时间线上的每一段都可以单独调整速度和音量",
]

# 组 B(5 例):幽灵卡诱导——中段字级时间坍缩(<100ms 级),切进坍缩区即幽灵卡;
# DP 必须绕开(禁止边界),而不是先出卡再靠 P28-2 保险并/丢。
GHOST_CASES = [
    ("这个功能上线之后大家的反馈都非常好留存很高", {10, 11, 12}),
    ("我们测了三个月的数据发现转化率翻了一倍还多", {9, 10, 11}),
    ("平台算法正在改写整个流量市场的分配规则和逻辑", {10, 11, 12}),
    ("没有经营主体资质就无法开通小店这条是硬规定", {9, 10, 11}),
    ("字幕的断句要跟着语音的停顿走而不是随机切分", {12, 13, 14}),
]

# 组 C(7 例):自然长句——每字 130ms,老 DP 倾向按标点一切到底产短卡;
# 新 DP 应给出全程 ≥0.83s 且 CPS ≤9 的切分。
NATURAL_CASES = [
    "这个方案的转化效率比上一版高出了整整一倍还多",
    "先把人群画像做扎实再谈投放才是正经的运营思路",
    "直播间的人气起来了但是转化率一直上不去很头疼",
    "老客户复购的成本远远低于拉新的成本这笔账要会算",
    "我们把素材导入时间线之后先做粗剪再做精修调色",
    "导出之前务必检查响度和真峰两项指标都达标了",
    "发布时间对流量池的突破有明显影响别忽略这个细节",
]


def _legal(res: dict) -> bool:
    """「DP 直接产出合法解」= 选中方案不带字数/时长/CPS/重叠违规。"""
    bad = [v for v in res["violations"]
           if any(k in v for k in ("字数", "时长", "CPS", "重叠"))]
    return not bad


def _run(text: str, per: int = 140, **kw) -> dict:
    ct = make_char_times(text, per=per, **kw)
    return sg.segment(text, 12, index_map=list(range(len(text))), char_times=ct)


def test_t41_dp_direct_legal_rate_at_least_90pct():
    """T4.1 判据:22 个必并/幽灵卡样例,DP 直接合法解 ≥90%(实测目标 100%)。"""
    results = [(t, _run(t)) for t in SHORT_CARD_CASES]
    results += [(t, _run(t, per=160, collapse=c)) for t, c in GHOST_CASES]
    results += [(t, _run(t, per=130)) for t in NATURAL_CASES]
    assert len(results) >= 20
    ok = [(t, r) for t, r in results if _legal(r)]
    rate = len(ok) / len(results)
    assert rate >= 0.90, f"DP 直接合法解比例 {rate:.0%} < 90%:" + "".join(
        f"\n  ✗ {t} cuts={r['cuts']} viol={r['violations']}"
        for t, r in results if not _legal(r))


def test_t41_ghost_cards_never_in_chosen_plan():
    """T4.1(b):选中方案的每张卡内容字有效时长必须 ≥100ms(幽灵区被绕开)。"""
    for text, collapse in GHOST_CASES:
        res = _run(text, per=160, collapse=collapse)
        for c in res["cards"]:
            span = c.get("anchorEndMs", 0) - c.get("anchorStartMs", 0)
            assert span >= sg.GHOST_MIN_MS, (text, c)


def test_t41_short_cards_avoided_when_legal_alternative_exists():
    """T4.1(a):存在合法替代切分时,DP 不得产出 <0.83s 短卡(带权惩罚生效)。"""
    for text in SHORT_CARD_CASES:
        res = _run(text)
        durs = [c["durMs"] / 1000.0 for c in res["cards"] if "durMs" in c]
        assert durs, text
        assert all(d >= sg.MIN_DUR_S - 1e-6 for d in durs), (text, durs, res["cuts"])


def test_t41_time_aware_flag_and_degraded_absence():
    """字级时间在 → timeAware=True;缺失 → 约束缺席(timeAware=False,不臆测)。"""
    text = "这个方案的转化效率比上一版高出了整整一倍还多"
    res = _run(text)
    assert res["timeAware"] is True
    res2 = sg.segment(text, 12)          # 无 index_map/char_times
    assert res2["timeAware"] is False
    assert res2["cards"], "无字级时间也必须能出卡"


def test_t41_ghost_ban_infeasible_degrades_with_trace():
    """幽灵禁切导致整体无解(pathological 字级时间)→ 放开禁切重解 + 显式留痕,不静默。"""
    text = "这一句每一个字的时间都坍缩成了几乎为零的极端形态"
    ct = make_char_times(text, per=5)            # 整句坍缩:任何切分都是幽灵卡
    degrade: list[str] = []
    res = sg.segment(text, 12, index_map=list(range(len(text))),
                     char_times=ct, degrade=degrade)
    assert res["cards"], "放开禁切后必须能出卡"
    assert res.get("ghostFallback") is True
    assert any("幽灵卡" in d for d in degrade), degrade


def test_t41_semantic_hits_reported_for_forced_cuts():
    """I1 罚分位置被极端约束逼着切时,segment 必须带出 semanticHits(供 review queue)。"""
    text = "他说不属于这个部门的人一律不能进入机房重地"
    ct = make_char_times(text, per=120)
    monkey_len = text
    res = sg.segment(monkey_len, 6, index_map=list(range(len(monkey_len))),
                     char_times=ct)              # max_chars=6 逼出多刀,必压语义单元
    assert isinstance(res.get("semanticHits"), list)


def test_t41_penalty_does_not_break_plain_punctuation_split():
    """普通带标点句不受罚分影响:仍按标点切,不因时长惩罚放弃标点边界。"""
    text = "先做人群画像，再设计脚本，才是正确的顺序和方法论"
    res = _run(text)
    assert res["cards"], res
    assert _legal(res)
    cuts = set(res["cuts"])
    punct_bounds = {i + 1 for i, ch in enumerate(text[:-1])
                    if ch in sg.TRAIL_PUNCT and ch not in sg._SPACE}
    assert cuts & punct_bounds, (cuts, punct_bounds)
