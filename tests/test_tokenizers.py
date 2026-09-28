"""第四册 T4.8 回归:分词引擎抽象 + 可插拔(Tokenizer 接口,五实现)。

判据(计划文档 4.2 T4.8):
  · 5 种实现(jieba / pkuseg / LAC / hanlp / 兜底词表)均可被同一组 30 条断句样例驱动;
  · 缺失实现报「降级到 X」留痕,绝不抛错;
  · config 可选(subtitleTokenizer 字段 / rs_subtitle --tokenizer)。

运行:pytest tests/test_tokenizers.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import segmentation as sg  # noqa: E402

# 30 条断句样例:REGRESSION 全集 + 金标抽样(跨口播/电商/财税/教程领域)
GOLD = None
try:
    import json
    _gold_path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "segboundary_gold.json"
    _gold_path = Path(__file__).resolve().parent / "fixtures" / "segboundary_gold.json"
    doc = json.loads(_gold_path.read_text(encoding="utf-8"))
    GOLD = [e["text"] for e in doc["entries"][:20]]
except Exception:  # noqa: BLE001 — 金标缺席时退 REGRESSION,样例数断言会兜底
    GOLD = []

SAMPLES = [r["text"] for r in sg.REGRESSION] + (GOLD or [])
SAMPLES = SAMPLES[:30]


def _valid_spans(text: str, spans) -> bool:
    """跨度合法:界内、不跨空格、宽度 ≥2。"""
    n = len(text)
    for a, b in spans:
        if not (0 <= a < b <= n) or b - a < 2:
            return False
        if any(text[k] in sg._SPACE for k in range(a, b)):
            return False
    return True


def test_sample_pool_has_30_items():
    assert len(SAMPLES) == 30, len(SAMPLES)


def _has_jieba() -> bool:
    import importlib.util
    return importlib.util.find_spec("jieba") is not None


def test_jieba_tokenizer_drives_all_samples():
    tok = sg.get_tokenizer("jieba")
    if _has_jieba():
        assert tok.name == "jieba"
    else:
        # 三态协议:请求 jieba 而组件缺失 → 显式降级到兜底词表引擎,不得静默伪装
        assert tok.name == "lexicon", tok.name
    for text in SAMPLES:
        assert _valid_spans(text, tok.spans(text)), text


def test_lexicon_tokenizer_drives_all_samples():
    tok = sg.get_tokenizer("lexicon")
    for text in SAMPLES:
        assert _valid_spans(text, tok.spans(text)), text


def test_missing_engines_degrade_with_trace():
    """pkuseg/LAC/hanlp 未安装(本机)→ 降级到 jieba/lexicon 并留痕,绝不抛错。"""
    state_backup = dict(sg._ENGINE_STATE)
    sg._ENGINE_STATE.clear()
    try:
        for name in ("pkuseg", "lac", "hanlp"):
            notes: list[str] = []
            tok = sg.get_tokenizer(name, degrade=notes)
            assert tok.name in ("jieba", "lexicon"), (name, tok.name)
            assert any("降级" in n for n in notes), (name, notes)
            assert _valid_spans(SAMPLES[0], tok.spans(SAMPLES[0]))
    finally:
        sg._ENGINE_STATE.clear()
        sg._ENGINE_STATE.update(state_backup)


def test_fake_pkuseg_drives_samples_via_tokens_to_spans(monkeypatch):
    """假 pkuseg(词列表形态)注入 → 走 _tokens_to_spans 锚定,同一组样例可驱动。"""
    class FakeSeg:
        def cut(self, text):  # noqa: ANN001 — 简单单字切分引擎(最差可用形态)
            return list(text)

    # 注入实例的 loader(TOKENIZERS 在 import 时捕获了模块级函数引用)
    monkeypatch.setattr(sg.TOKENIZERS["pkuseg"], "_loader", lambda: FakeSeg())
    state_backup = dict(sg._ENGINE_STATE)
    sg._ENGINE_STATE.pop("pkuseg", None)
    sg.TOKENIZERS["pkuseg"]._model = None
    try:
        notes: list[str] = []
        tok = sg.get_tokenizer("pkuseg", degrade=notes)
        assert tok.name == "pkuseg"
        for text in SAMPLES:
            assert _valid_spans(text, tok.spans(text)), text
    finally:
        sg.TOKENIZERS["pkuseg"]._model = None
        sg._ENGINE_STATE.clear()
        sg._ENGINE_STATE.update(state_backup)


def test_tokens_to_spans_alignment_rules():
    """_tokens_to_spans:token 对不上文本(引擎加空格/改写)就丢弃,不臆测位置。"""
    spans = sg._tokens_to_spans(["我们", "今天", "讲讲", "账号"], "我们今天讲讲账号")
    assert spans == [(0, 2), (2, 4), (4, 6), (6, 8)]
    # 引擎输出带空格的 token → 锚不上(文本里没有)→ 丢弃
    spans2 = sg._tokens_to_spans(["我 们", "今天"], "我们今天")
    assert spans2 == [(2, 4)]
    # 单字 token 被过滤(对断句无意义)
    assert sg._tokens_to_spans(["我", "是", "谁"], "我是谁") == []
    # 空文本安全
    assert sg._tokens_to_spans(["x"], "") == []


def test_unknown_engine_falls_back_to_auto_chain():
    notes: list[str] = []
    tok = sg.get_tokenizer("不存在的引擎", degrade=notes)
    assert tok.name in ("jieba", "lexicon")
    assert any("未知分词引擎" in n for n in notes)


def test_auto_chain_is_jieba_then_lexicon():
    """auto 链 = jieba→lexicon(ADR-0060:无评测不换件,jieba 仍是默认)。"""
    assert sg.AUTO_CHAIN == ("jieba", "lexicon")
    tok = sg.get_tokenizer("auto")
    # auto 链按可用性取首引擎:jieba 在场取 jieba;缺失则落到链尾兜底(降级留痕由 notes 承载)
    assert tok.name == ("jieba" if _has_jieba() else "lexicon"), tok.name


def test_word_spans_engine_param_and_degrade_trace():
    """word_spans(engine=…) 可插拔;降级留痕可由调用方收集。"""
    text = "他非常努力地准备但是最后失败了"
    spans_j = sg.word_spans(text, engine="jieba")
    spans_l = sg.word_spans(text, engine="lexicon")
    assert _valid_spans(text, spans_j) and _valid_spans(text, spans_l)
    assert {text[a:b] for a, b in spans_j} >= {"非常", "准备"}
    notes: list[str] = []
    sg.word_spans(text, engine="hanlp", degrade=notes)
    assert any("降级" in n for n in notes), notes


def test_terms_and_protected_words_always_merged():
    """terms/idioms/PROTECTED_WORDS 与引擎无关地强制并入词跨度。"""
    text = "借款时发生周转归还的义务"
    for engine in ("jieba", "lexicon"):
        words = {text[a:b] for a, b in sg.word_spans(text, engine=engine)}
        assert "周转归还" in words, (engine, words)


def test_segment_accepts_engine_passthrough():
    """segment 透传 engine:降级留痕进 degrade 表,断句结果不因引擎缺失而失败。"""
    text = "小企业会计准则的利润表中只有一个财务费用科目"
    notes: list[str] = []
    res = sg.segment(text, 12, engine="lac", degrade=notes)
    assert res["cards"]
    assert any("降级" in n for n in notes), notes
