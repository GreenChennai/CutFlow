"""第四册 T4.9 回归:分词金标评测(允许切点 F1)+「无评测不换件」决策基线。

判据(计划文档 4.2 T4.9 / 4.3 验收):
  · 金标 `tests/fixtures/segboundary_gold.json` 300–500 条领域内允许切点
    (含 BACKLOG 已知案例与 rules/subtitles.md §4.7 的 14 例词界);
  · 每个引擎给出边界 F1;选定引擎(jieba,ADR-0060)F1 ≥ 防退化下限;
  · 引擎缺失 → 降级留痕;同输入 F1 可复现(防退化基线)。

决策记录:docs/adr/0060-分词引擎无评测不换件.md(2026-09-27:jieba F1 0.876,
无可达 +0.03 的可用替代引擎,保留 jieba)。

运行:pytest tests/test_segboundary_gold.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import segmentation as sg  # noqa: E402

GOLD_PATH = Path(__file__).resolve().parent / "fixtures" / "segboundary_gold.json"
F1_FLOOR = 0.75            # jieba 防退化下限(2026-09-27 实测 0.876;ADR-0060)


def load_gold() -> list[dict]:
    doc = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    return doc["entries"]


def engine_allowed(text: str, tok: sg.Tokenizer) -> set[int] | None:
    """引擎 → 「允许切点」集合 = 非词内位置(引擎认为此处切不伤词)。"""
    try:
        spans = tok.spans(text)
    except Exception:
        return None
    inner: set[int] = set()
    for a, b in spans:
        inner.update(range(a + 1, b))
    return {p for p in range(1, len(text)) if p not in inner}


def boundary_f1(gold: set[int], got: set[int]) -> tuple[float, float, float]:
    tp = len(gold & got)
    prec = tp / len(got) if got else 0.0
    rec = tp / len(gold) if gold else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return prec, rec, f1


def eval_engine(entries: list[dict], name: str) -> dict | None:
    """对金标全集评测一个引擎;引擎缺失(降级路径也拿不到跨度)→ None。"""
    tok = sg.get_tokenizer(name)
    prec_sum = rec_sum = f1_sum = 0.0
    n = 0
    for e in entries:
        got = engine_allowed(e["text"], tok)
        if got is None:
            return None
        p, r, f = boundary_f1(set(e["allowed"]), got)
        prec_sum += p
        rec_sum += r
        f1_sum += f
        n += 1
    if not n:
        return None
    return {"engine": name, "precision": round(prec_sum / n, 4),
            "recall": round(rec_sum / n, 4), "f1": round(f1_sum / n, 4), "n": n}


# ---------------------------------------------------------------- 金标本体

def test_gold_set_size_and_validity():
    """金标 300–500 条;allowed 位置合法且不违反领域禁切表。"""
    entries = load_gold()
    assert 300 <= len(entries) <= 500, len(entries)
    for e in entries:
        text = e["text"]
        forb = sg.forbidden_positions(text)
        assert all(0 < p < len(text) for p in e["allowed"]), e
        assert not (set(e["allowed"]) & forb), (text, e["allowed"] & forb)


def test_gold_covers_known_cases():
    """已知案例(P30-5 / rules §4.7 词界)必须在金标文本中受保护。"""
    entries = load_gold()
    texts = " ".join(e["text"] for e in entries)
    for w in ("周转归还", "资金往来", "财务费用科目", "利润表", "高度重视"):
        assert w in texts, w


# ---------------------------------------------------------------- 引擎 F1 评测

def test_jieba_f1_above_regression_floor():
    """选定引擎 jieba 的边界 F1 ≥ 防退化下限(无评测不换件的另一半:不悄悄掉分)。"""
    res = eval_engine(load_gold(), "jieba")
    assert res is not None
    assert res["f1"] >= F1_FLOOR, res


def test_lexicon_fallback_below_jieba():
    """兜底词表 F1 低于 jieba(印证 jieba 作为默认引擎的评测依据,ADR-0060)。"""
    jieba = eval_engine(load_gold(), "jieba")
    lex = eval_engine(load_gold(), "lexicon")
    assert lex is not None and jieba is not None
    assert lex["f1"] < jieba["f1"], (lex, jieba)


def test_missing_engines_report_degradation():
    """pkuseg/LAC/hanlp 缺席 → get_tokenizer 降级留痕(评测脚本可感知「降级到 X」)。"""
    state_backup = dict(sg._ENGINE_STATE)
    sg._ENGINE_STATE.clear()
    try:
        for name in ("pkuseg", "lac", "hanlp"):
            notes: list[str] = []
            tok = sg.get_tokenizer(name, degrade=notes)
            assert any("降级" in n for n in notes), (name, notes)
            assert tok.name in ("jieba", "lexicon")
    finally:
        sg._ENGINE_STATE.clear()
        sg._ENGINE_STATE.update(state_backup)


def test_fake_engine_driven_on_30_samples(monkeypatch):
    """非 jieba 实现(注入形态)可被同一组 30 条样例驱动并算出 F1。"""
    entries = load_gold()[:30]

    class PairSeg:
        """假引擎:两字一组切分(系统性错切的对照组)。"""

        def cut(self, text):  # noqa: ANN001
            return [text[i:i + 2] for i in range(0, len(text), 2)]

    monkeypatch.setattr(sg.TOKENIZERS["lac"], "_loader", lambda: PairSeg())
    state_backup = dict(sg._ENGINE_STATE)
    sg._ENGINE_STATE.pop("lac", None)
    sg.TOKENIZERS["lac"]._model = None
    try:
        tok = sg.get_tokenizer("lac")
        assert tok.name == "lac"
        n = 0
        f1_sum = 0.0
        for e in entries:
            got = engine_allowed(e["text"], tok)
            assert got is not None
            _, _, f1 = boundary_f1(set(e["allowed"]), got)
            f1_sum += f1
            n += 1
        assert n == 30
        assert f1_sum / n < 1.0        # 两字切分必然有切词损失(评测口径有效)
    finally:
        sg.TOKENIZERS["lac"]._model = None
        sg._ENGINE_STATE.clear()
        sg._ENGINE_STATE.update(state_backup)


def test_f1_reproducible_across_reruns():
    """同输入重跑 F1 一致(防退化基线可复现)。"""
    a = eval_engine(load_gold(), "jieba")
    b = eval_engine(load_gold(), "jieba")
    assert a == b


def test_decision_record_exists():
    """换件决策必须落进决策记录(ADR-0060),并写明当前默认引擎与 F1。"""
    adr = Path(__file__).resolve().parents[1] / "docs" / "adr" / "0060-分词引擎无评测不换件.md"
    assert adr.is_file(), "缺少 ADR-0060 决策记录"
    text = adr.read_text(encoding="utf-8")
    assert "无评测不换件" in text and "jieba" in text
