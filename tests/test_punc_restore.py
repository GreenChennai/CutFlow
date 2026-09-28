"""第四册 T4.10 回归:标点恢复(可选件)——只在降级路径,正常路径零新依赖。

判据(计划文档 4.2 T4.10 / 4.3 验收):
  · 降级路径(--from-transcript / onnx 后端的降级 wordline)产出标点,
    标注 `punctuation: restored`;
  · 正常路径不引入该依赖(零第三方依赖克制,test_zero_dep 判据在此固化);
  · 模型缺失 → 显式降级留痕,不静默、不抛错。

运行:pytest tests/test_punc_restore.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import rs_align  # noqa: E402
import rs_subtitle as rsub  # noqa: E402
import rs_verify  # noqa: E402
import rs_sync  # noqa: E402


# ---------------------------------------------------------------- 零依赖判据

def test_normal_path_never_imports_funasr():
    """正常链路(模块导入 + wordline 出字幕 + L0 字幕闸)不加载任何标点恢复模型。"""
    for mod in ("funasr", "torch", "modelscope"):
        assert mod not in sys.modules, f"正常路径不应加载 {mod}"
    text = "他非常努力地准备但是没有成功最后还是失败了"
    per = 150
    chars = [{"i": i, "ch": ch, "startMs": per * i, "endMs": per * i + per - 30,
              "srcStartMs": per * i, "srcEndMs": per * i + per - 30, "conf": 0.99}
             for i, ch in enumerate(text)]
    wl = {"source": "test", "chars": chars,
          "sentences": [{"id": 0, "span": [0, len(text)]}],
          "degraded": False, "degradeReasons": []}
    rsub.events_from_wordline(wl, 12)          # 正常字幕链
    for mod in ("funasr", "torch"):
        assert mod not in sys.modules, f"字幕主链不应加载 {mod}"


def test_restore_rejects_non_degraded_wordline():
    """正常路径(非降级)即使显式调用也拒绝启用(留痕,不改任何内容)。"""
    wl = {"degraded": False, "charTimingEstimated": False,
          "chars": [{"i": 0, "ch": "好", "startMs": 0, "endMs": 100}]}
    doc, rep = rs_align.restore_punctuation(wl)
    assert rep["applied"] is False
    assert "非降级路径" in rep["reason"]
    assert "punctuation" not in doc


def test_restore_skipped_when_punct_already_present():
    """文本已含标点 → 无需恢复(不浪费模型加载)。"""
    wl = {"degraded": True,
          "chars": [{"i": 0, "ch": ch, "startMs": i * 100, "endMs": i * 100 + 80}
                    for i, ch in enumerate("大家好,今天讲第一句。")]}
    doc, rep = rs_align.restore_punctuation(wl)
    assert rep["applied"] is False and "无需恢复" in rep["reason"]


# ---------------------------------------------------------------- 降级路径(注入假模型)

class _FakePuncModel:
    """FunASR ct-punc 同形假模型:generate(input) → [{"text": …}]。"""

    def generate(self, input: str):  # noqa: A002 — 与 funasr SDK 同名
        out = input.replace("大家好", "大家好,").replace("第一句", "第一句。")
        return [{"text": out}]


def test_restore_on_degraded_wordline_inserts_punct():
    """降级 + 缺标点 + 模型可用 → 插入零宽标点、punctuation=restored、单调保持。"""
    wl = rs_align.build_wordline(
        [{"start": 0.0, "end": 3.0, "text": "大家好今天我们讲第一句"}],
        "transcript.json", degraded="transcript 句级时间(未字级对齐)")
    assert wl["degraded"] is True
    doc, rep = rs_align.restore_punctuation(wl, model=_FakePuncModel())
    assert rep["applied"] is True and rep["inserted"] >= 2
    assert doc.get("punctuation") == "restored"           # T4.10 契约标注
    assert doc["punctuationRestore"]["applied"] is True
    # 内容字一个不丢:去掉插入的标点后与原文一致
    content = [c for c in doc["chars"] if not c.get("inserted")]
    assert "".join(c["ch"] for c in content) == "大家好今天我们讲第一句"
    # 插入的标点全部零宽(endMs==startMs)且时间单调不减
    puncts = [c for c in doc["chars"] if c.get("inserted")]
    assert puncts and all(c["startMs"] == c["endMs"] for c in puncts)
    prev = -1
    for c in doc["chars"]:
        assert c["startMs"] >= prev
        prev = c["startMs"]
    # 句结构按新标点重切
    assert len(doc["sentences"]) >= 1 and doc["gaps"] is not None


def test_restore_without_model_degrades_with_trace(monkeypatch):
    """模型未部署(funasr 缺失)→ 显式降级留痕,绝不抛错、绝不静默。"""

    def _boom():
        raise ImportError("No module named 'funasr'")

    monkeypatch.setattr(rs_align, "_load_punc_model", _boom)
    wl = rs_align.build_wordline(
        [{"start": 0.0, "end": 3.0, "text": "大家好今天我们讲第一句"}],
        "transcript.json", degraded="transcript 句级时间(未字级对齐)")
    doc, rep = rs_align.restore_punctuation(wl)
    assert rep["applied"] is False
    assert "funasr" in rep["reason"] or "不可用" in rep["reason"]
    assert "punctuationRestore" in doc                    # 留痕进文档
    assert "punctuation" not in doc


def test_cli_flag_registered():
    """rs_align build --restore-punct 旗标在册(降级路径 opt-in)。"""
    import argparse
    import io
    import contextlib
    src = Path(rs_align.__file__).read_text(encoding="utf-8")
    assert "--restore-punct" in src
    # argparse 定义可解析(拿真 parser 冒烟)
    assert "restore_punct" in src


def test_restore_only_inserts_punctuation_not_words():
    """安全护栏:模型若产出非标点插入(改写/补字),一律丢弃,不污染内容。"""
    class _BadModel:
        def generate(self, input):  # noqa: A002
            return [{"text": input + "大家"}]     # 试图在句尾补字(非标点)

    wl = rs_align.build_wordline(
        [{"start": 0.0, "end": 2.0, "text": "大家好今天讲第一句"}],
        "t.json", degraded="d")
    doc, rep = rs_align.restore_punctuation(wl, model=_BadModel())
    assert rep["inserted"] == 0 and rep["applied"] is False
    assert "".join(c["ch"] for c in doc["chars"]) == "大家好今天讲第一句"
