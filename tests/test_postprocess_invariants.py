"""T5.3a 回归扩容:后处理不变式薄弱域(textopt 纯函数层 / segscore 机械性质)。

域分布盘点(2026-09-27,1413 用例基线)显示:断句 DP / 分词金标 / 时间锚 /
sync 对账各域均有成建制回归(test_dp_constraints / test_segboundary_gold /
test_v17_break_tail / test_med_fixes …),唯 textopt 的「轻改写不变式」只有
test_v4/test_v7 里约 2 处间接覆盖。本文件把 ADR-0002 的承诺
「只动标点/口水词/断行,不改语义不删信息」落成机械不变式:

  · 字符守恒:normalize 只做「白名单删除 + 全角数字转写 + 省略号折叠」,
    绝不引入/改写实义字符,顺序不变(子序列关系);
  · 分句还原:split_sentences 丢的只有入屏标点,非标点字符序恒等;
  · 断行体积下界:任何切法的卡数 ≥ ceil(len/maxChars)(体积下界不可绕过);
  · 每卡 ≤ maxChars(策略上界);
  · 零字符丢失:卡片拼合(滤掉被剥标点/空白)== 规范化句(同滤);
  · 降级留痕:DP 失败退回长度算法必须写 degrade,不静默;
  · segscore 机械性质:违规数单增 ⇒ 分数单调不增;记账 cards == 卡时长数。

全部为纯函数断言,零 IO 零 mock(除一处 monkeypatch 验证降级留痕)。
运行:pytest tests/test_postprocess_invariants.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import segmentation as sg  # noqa: E402
import textopt  # noqa: E402

# 允许被改写/删除的字符白名单(与 textopt 模块常量对账,防"白名单偷偷扩")
ALLOWED_DROP = (set(textopt.TAIL_DROP) | set("嗯呃唉哦噢诶哈")     # 句尾标点 + 句首口水词
                | set("啊嘛呢吧啦呗呕噢") | set(" \t\n")           # 句尾语气字 + 空白
                | set("。，,;、::"))                               # 卡级清洗可剥标点
ELLIPSIS_ONLY = {"…"}          # 唯一允许"新增"的字符(省略号折叠的目标)
FULLWIDTH_DIGITS = "０１２３４５６７８９"

# 真实口播语料(短句/长句/带口水词/带标点/带数字/带省略号的典型混合)
CORPUS = [
    "大家好，今天我们讲桌面运维。",
    "嗯，先做人群画像，再设计脚本，才是正确顺序。",
    "这个方案跑了两周……结果还不错！",
    "蓝屏的原因有很多，但是内存条接触不良最常见。",
    "把内存条拔下来，用橡皮擦一擦，再插回去就好了。",
    "关注我，下期讲硬盘坏道的自救方法！",
    "３２１，上车。",
    "吧啦吧啦说了一堆啊，重点只有一个。",
    "先看报错代码，再看日志，最后再动手改配置。",
    "数据不会骗人，但是看数据的人会。",
]


# ---------------------------------------------------------------- normalize_text

@pytest.mark.parametrize("text", CORPUS)
def test_normalize_char_conservation(text):
    """字符守恒:规范化输出必须是「全角数字转写后输入」的子序列,
    且引入的字符只可能是省略号(折叠目标)—— ADR-0002 不改语义的机械面。"""
    mapped = text.translate(textopt.FULLWIDTH_NUM)
    out = textopt.normalize_text(text)
    # 顺序保持:out 是 mapped 的子序列(只删不改不换位)
    it = iter(mapped)
    assert all(ch in it for ch in out), (text, out)
    # 不引入新字符(除省略号折叠)
    assert set(out) - set(mapped) <= ELLIPSIS_ONLY, (text, out)
    # 删除的白名单对账:被删字符必须全部属于可删白名单
    dropped = set(mapped) - set(out)
    assert dropped - ALLOWED_DROP <= {"…"}, (text, sorted(dropped))


@pytest.mark.parametrize("raw, want", [
    ("３２１，上车。", "321，上车。"),          # 全角数字 → 半角(数值不变)
    ("等等。。。真的吗", "等等…真的吗"),        # 中文省略号折叠
    ("等等...真的吗", "等等…真的吗"),           # 半角省略号折叠
    ("什么!!", "什么!"),                        # 重复半角叹号折叠(禁 !? 连用)
    ("嗯，那个我们开始", "那个我们开始"),        # 句首口水词(含跟随逗号)剥除
    ("呃呃好", "好"),                            # 连续口水词剥除
    ("好吧。", "好。"),                          # 句尾语气字轻删(仅标点/结尾前)
    ("去做吧他们", "去做吧他们"),                # 句中语气字不删(非标点/结尾前)
])
def test_normalize_enumerated_rules(raw, want):
    """每条改写规则逐例对账:规则是枚举的,行为必须与枚举一致(不许隐式扩权)。"""
    assert textopt.normalize_text(raw) == want


def test_normalize_digit_value_unchanged():
    """全角→半角只换形不换值:转写前后字符恒等映射(信息不变式的数值面)。"""
    for full, half in textopt.FULLWIDTH_NUM.items():
        assert textopt.normalize_text(chr(full)) == chr(half)


# ---------------------------------------------------------------- split_sentences

@pytest.mark.parametrize("text", CORPUS)
def test_split_sentences_preserves_non_punct_text(text):
    """分句还原:两侧滤掉「可入屏标点集合」后逐字相等 —— 分句只丢标点,不丢字。"""
    keep_out = set("。，,;、::")
    a = "".join(ch for ch in textopt.normalize_text(text) if ch not in keep_out)
    b = "".join(ch for ch in "".join(textopt.split_sentences(text)) if ch not in keep_out)
    assert a == b, (text, a, b)


@pytest.mark.parametrize("text", CORPUS)
def test_split_sentences_no_on_screen_tail_punct(text):
    """Netflix 规则:任何一句的句尾不得带入屏禁用标点(。，,;、:);?!… 保留。"""
    for s in textopt.split_sentences(text):
        assert s and s[-1] not in "。，,;、::", (text, s)


@pytest.mark.parametrize("text", ["", "   ", "。，;;", "。"])
def test_split_sentences_empty_inputs_safe(text):
    """空串/纯禁屏标点输入 → 空表(不出空卡,后续 build_cards 才不会炸)。"""
    assert textopt.split_sentences(text) == []


def test_split_sentences_punct_only_ellipsis_kept_single():
    """纯省略号输入:折叠为单 … 入屏(唯一保留的"无字句"),不出空卡。"""
    assert textopt.split_sentences("。。。") == ["…"]


def test_split_sentences_keeps_qmark_exclaim():
    """?!属于保留标点(入屏),分句在其后切开但标点本身留在卡尾。"""
    parts = textopt.split_sentences("做完了吗!真的做完了?")
    assert parts == ["做完了吗!", "真的做完了?"], parts


# ---------------------------------------------------------------- card_split / build_cards

@pytest.mark.parametrize("text", CORPUS)
@pytest.mark.parametrize("max_chars", [6, 10, 15])
def test_cards_respect_max_chars(text, max_chars):
    """策略上界:任何切法的每张卡都 ≤ maxChars(_clean_card 只会缩短不会加长)。"""
    for s in textopt.split_sentences(text):
        for mode in ("dp", "length"):
            for card in textopt.card_split(s, max_chars, mode=mode):
                assert 0 < len(card) <= max_chars, (s, mode, card)


@pytest.mark.parametrize("text", CORPUS)
@pytest.mark.parametrize("max_chars", [6, 10])
def test_cards_volume_lower_bound(text, max_chars):
    """体积下界:不管什么算法,卡数 ≥ ceil(句长/maxChars) —— 不可绕过的物理约束。"""
    import math
    for s in textopt.split_sentences(text):
        if not s:
            continue
        need = math.ceil(len(s) / max_chars)
        for mode in ("dp", "length"):
            cards = textopt.card_split(s, max_chars, mode=mode)
            assert len(cards) >= need, (s, mode, cards)


@pytest.mark.parametrize("text", CORPUS)
@pytest.mark.parametrize("max_chars", [6, 10, 15])
def test_cards_zero_character_loss(text, max_chars):
    """零字符丢失:全部卡拼合,滤掉「被剥标点/空白」后与规范化句逐字相等。

    这是「断行不改信息」的最强机械面:断行只允许在滤集内做文章。"""
    drop = set("。，,;、:: ")

    def norm(s: str) -> str:
        return "".join(ch for ch in s if ch not in drop)

    for s in textopt.split_sentences(text):
        joined = "".join(textopt.card_split(s, max_chars, mode="dp"))
        assert norm(joined) == norm(s), (s, joined)


def test_dp_degrade_must_leave_trail(tmp_path, monkeypatch):
    """降级留痕铁律:DP 分段失败 → 退回长度算法,但 degrade 必须记录原因,不静默。"""
    def boom(*a, **k):
        raise RuntimeError("注入的 DP 故障")
    monkeypatch.setattr(sg, "segment", boom)
    degrade: list[str] = []
    cards = textopt.card_split("这是一个明显超过限制长度的长句子需要切开处理",
                               8, mode="dp", degrade=degrade)
    assert cards, "退回长度算法也要出卡(字幕环节不许炸)"
    assert any("DP 分段失败" in w and "RuntimeError" in w for w in degrade), degrade


def test_build_cards_preserves_sentence_order():
    """句序与内容保持:全部卡滤掉「被剥标点/空白」后拼合,与逐句规范化拼合逐字相等
    (字幕卡乱序/丢字 = 事故);且 build_cards == 逐句 card_split 的顺序拼接。"""
    sents = textopt.split_sentences("第一句讲背景。第二句讲方法!第三句讲结论。")
    cards = textopt.build_cards(sents, 6)
    assert cards
    drop = set("。，,;、:: ")

    def norm(s: str) -> str:
        return "".join(ch for ch in s if ch not in drop)

    assert norm("".join(cards)) == norm("".join(sents))
    assert cards == [c for s in sents for c in textopt.card_split(s, 6)]


# ---------------------------------------------------------------- segscore 机械性质

def test_segscore_monotone_in_violations():
    """同句同切下,违规数单增 ⇒ 质量分单调不增(惩罚项方向正确)。"""
    sen = [{"text": "先做人群画像再设计脚本才是正确顺序", "cuts": [7, 12], "gaps": {}}]
    durs = [1.5, 2.0, 1.0]
    scores = [sg.segscore(sen, durs, v)["score"] for v in (0, 1, 2, 3)]
    assert scores == sorted(scores, reverse=True), scores


def test_segscore_ledger_counts_cards():
    """记账一致:segscore.cards == 切点数 + 1(卡数与切分方案一一对应,防两头各记各的)。"""
    assert sg.segscore([{"text": "这句话有十个字呀", "cuts": [4], "gaps": {}}],
                       [2.0, 1.0], 0)["cards"] == 2
    assert sg.segscore([{"text": "一句话", "cuts": [], "gaps": {}}],
                       [2.0], 0)["cards"] == 1
    assert sg.segscore([], [], 0)["cards"] == 0


def test_segscore_word_boundary_beats_midword_cut():
    """方向性:词界切分恒优于词内切(同句、同卡数、同违规数的对照实验)。"""
    text = "他非常努力地准备但是最后失败"
    durs, viol = [2.0, 2.0], 0
    boundary = sg.segscore([{"text": text, "cuts": [1], "gaps": {}}], durs, viol)
    midword = sg.segscore([{"text": text, "cuts": [2], "gaps": {}}], durs, viol)
    assert boundary["components"]["word"] == 1.0, boundary
    assert midword["components"]["word"] < 1.0, midword
    assert boundary["score"] > midword["score"]


# ---------------------------------------------------------------- 语料基线(T5.4)

SEG_CORPUS = Path(__file__).resolve().parent / "fixtures" / "seg_corpus.txt"
S_FLOOR = 0.85            # 防退化下限(2026-09-27 实测 0.907;同 segboundary F1 惯例)


def test_segscore_corpus_baseline():
    """20 条语料 DP 断句的 S 分 ≥ 防退化下限(tests/fixtures/seg_corpus.txt;
    复算命令与当前值登记在 docs/METRICS.md,改 DP/分词后必须重算并同步两处)。"""
    corpus = [ln.strip() for ln in SEG_CORPUS.read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.startswith("#")]
    assert len(corpus) == 20, len(corpus)
    plans = [sg.segment(t, 15) for t in corpus]
    sents = [{"text": t, "cuts": p["cuts"], "gaps": {}}
             for t, p in zip(corpus, plans)]
    durs = [len(c["text"]) * 0.18 for p in plans for c in p["cards"]]
    viol = sum(len(p["violations"]) for p in plans)
    r = sg.segscore(sents, durs, viol)
    assert r["score"] >= S_FLOOR, (r["score"], r["components"])
