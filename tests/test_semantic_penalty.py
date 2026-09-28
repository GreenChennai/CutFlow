"""第四册 T4.5 回归:I1 语义单元罚分进 DP 打分。

判据(计划文档 4.2 T4.5 / BACKLOG v0.8.2 I1):
  · 否定词跨卡(「不|属于」)罚分;
  · 复合词跨卡(「经营|主体」「运营|效率」「店群|企业」)罚分;
  · 「的」字头卡降权(「的市场版图」);
  · 新增 REGRESSION ≥8 例(含 BACKLOG I1 全部案例)全绿。

运行:pytest tests/test_semantic_penalty.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import segmentation as sg  # noqa: E402


def cards(text: str, max_chars: int = 12, terms=()) -> list[str]:
    return [c["text"] for c in sg.segment(text, max_chars, terms=terms)["cards"]]


# ---------------------------------------------------------------- 否定词跨卡

def test_negation_not_separated_when_alternative_exists():
    """「不属于」不得被 DP 拆成「…不 | 属于…」——存在合法替代时罚分必须生效。"""
    text = "这些内容根本不属于合规的范围里面"
    for _ in range(3):
        res = sg.segment(text, 12)
        assert res["cards"]
        cuts = set(res["cuts"])
        p = text.find("不属于")
        assert p + 1 not in cuts, (cards(text), "否定词「不」被甩到下一卡")


def test_neg_tail_penalty_in_cut_score():
    """cut_score 机制级:否定词收尾的切点分数必须显著低于替代切点。"""
    text = "他说不属于这个团队的人不能进来"
    p_bad = text.find("不属于") + 1          # 不 | 属于
    p_ok = text.find("他说") + len("他说")   # 他说 | …
    s_bad = sg.cut_score(text, p_bad)
    s_ok = sg.cut_score(text, p_ok)
    assert s_bad < s_ok, (s_bad, s_ok)


# ---------------------------------------------------------------- 复合词跨卡

def test_compound_words_not_split():
    """BACKLOG I1 案例:经营主体 / 运营效率 / 店群企业 不得跨卡。"""
    cases = [
        ("店群企业的运营效率决定了经营主体的收益",
         ("店群企业", "运营效率", "经营主体")),
        ("提高运营效率的核心是优化流量结构", ("运营效率",)),
        ("平台算法正在改写流量的市场版图", ("市场版图",)),
        ("这种做法直接影响了店群企业的整体评分", ("店群企业",)),
    ]
    for text, words in cases:
        got = cards(text)
        for w in words:
            assert any(w in c.replace(" ", "") for c in got), (text, w, got)


def test_semantic_penalty_weaker_than_word_penalty():
    """罚分强度分层:词内切(−3.0)> 复合词/否定词(−2.0)> 「的」字头(−1.5)。"""
    assert sg.WORD_CUT_PENALTY < sg.SEMANTIC_PENALTY < sg.DE_HEAD_PENALTY < 0
    assert sg.NEG_TAIL_PENALTY == sg.SEMANTIC_PENALTY


def test_semantic_bad_positions_cover_compounds():
    """机制级:SEMANTIC_COMPOUNDS 的内部位置全部进罚分集合。"""
    text = "店群企业的运营效率"
    bad = sg.semantic_bad_positions(text)
    assert all(i in bad for i in range(1, 4)), bad      # 店群企业 内部(1..3)
    assert all(i in bad for i in range(6, 9)), bad      # 运营效率 内部(6..8)


# ---------------------------------------------------------------- 「的」字头卡降权

def test_no_card_starts_with_de_when_alternative_exists():
    """「的市场版图」式「的」字头卡:存在合法替代时 DP 必须绕开。"""
    texts = [
        "平台算法正在改写流量的市场版图格局",
        "三家门店的客流量差异其实非常明显",
        "统一了客流量差异的分析口径之后结论清晰了",
    ]
    for text in texts:
        res = sg.segment(text, 12)
        starts = [c["text"].lstrip()[:1] for c in res["cards"][1:]]
        assert all(s not in sg.DE_HEAD for s in starts), (text, [c["text"] for c in res["cards"]])


def test_de_head_penalty_in_cut_score():
    """cut_score 机制级:「的」字头切点分数低于替代切点。"""
    text = "流量的市场版图正在被改写"
    p_bad = text.find("的")                   # 切在「的」之前 → 「的」字头卡
    p_ok = text.find("版图") + len("版图")    # 替代切点:名词边界,卡首非「的」
    assert sg.cut_score(text, p_bad) < sg.cut_score(text, p_ok)


# ---------------------------------------------------------------- REGRESSION 扩容

def test_regression_expanded_to_at_least_40():
    """T4.5 判据:回归集 ≥40 例(14 现状口径 + I1 8 例 + terms + 金标抽样)。"""
    assert len(sg.REGRESSION) >= 40, len(sg.REGRESSION)


def test_regression_contains_all_i1_backlog_cases():
    """BACKLOG I1 列出的全部案例必须固化进回归集。"""
    joined = " ".join(r["text"] for r in sg.REGRESSION)
    for w in ("不属于", "经营主体", "运营效率", "店群企业", "市场版图", "客流量差异"):
        assert w in joined, w


def test_regression_all_cases_green():
    """回归集逐条全绿:must_not_split 完整 + 词跨度不被切。"""
    for r in sg.REGRESSION:
        terms = r.get("terms", ())
        res = sg.segment(r["text"], 12, terms=terms)
        got = ["".join(c["text"].split()) for c in res["cards"]]
        for term in r.get("must_not_split", ()):
            assert any(term in g for g in got), (r["text"], term, got)
        cut_set = set(res["cuts"])
        for a, b in sg.word_spans(r["text"], terms=terms):
            if b - a < 2:
                continue
            assert not (cut_set & set(range(a + 1, b))), (r["text"], r["text"][a:b])
