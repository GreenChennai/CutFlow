"""纯 Python 的 SentencePiece 解码（替代 ``sentencepiece`` 库，零新增依赖）。

背景（v0.8.5）：SenseVoiceSmall 的 ONNX 输出是 BPE token id 序列，官方
``funasr_onnx`` 用 ``sentencepiece`` 的 ``DecodeIds`` 还原文本。MomentShift 的
运行时依赖收敛为 numpy/onnxruntime/jieba，**不新增 sentencepiece**。但
SenseVoice 的 ONNX 仓库同时提供 ``tokens.json`` —— 它就是 SentencePiece 词表
（``piece`` 字符串按 id 顺序排列的纯列表，见 ``haixuantao/SenseVoiceSmall-onnx``
的 ``tokens.json``，25055 项）。因此解码 = 查表 + 复刻 ``DecodeIds`` 的拼接规则：

- ``<unk>``(0) / ``<s>``(1) / ``</s>``(2) 及 ``<OOV>`` 是 CONTROL/UNKNOWN 类型，
  sentencepiece 解码时**跳过**（不会出现在输出里）；
- ``<|zh|>`` 这类 ``<|...|>`` 特殊 token 是 USER_DEFINED 类型，解码时**保留**
  原文 —— ``rich_transcription_postprocess`` 需要它们在文本里才能替换成 emoji /
  事件标记；
- ``▁``（U+2581）是 sentencepiece 的词首空格标记：拼接时换成空格，并去掉
  ``DecodeIds`` 产物开头的多余空格（首个 ``▁`` 不产生前导空格）。

本模块只做「id → 文本」，与句级后处理（``postprocess_utils`` 里的
``rich_transcription_postprocess``）解耦，便于离屏单测。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

# sentencepiece 解码时跳过的 CONTROL / UNKNOWN 表面形式
_SKIP_PIECES = frozenset({"<unk>", "<s>", "</s>", "<OOV>"})
_SPACE_SYMBOL = "\u2581"  # ▁


class SentencepieceDecodeError(ValueError):
    """词表缺失 / 无法解析；``message`` 可直接展示。"""


def load_vocab(model_dir: str | Path) -> list[str]:
    """从模型目录加载 SenseVoice 词表（``tokens.json``，纯列表）。

    Args:
        model_dir: SenseVoice 模型目录（含 ``tokens.json``）。

    Returns:
        按 id 排列的 piece 列表；``vocab[i]`` 即 token id ``i`` 的 piece 文本。

    Raises:
        SentencepieceDecodeError: ``tokens.json`` 缺失或不是非空列表。
    """
    path = Path(model_dir) / "tokens.json"
    if not path.is_file():
        raise SentencepieceDecodeError(
            f"SenseVoice 缺少词表 tokens.json（{path}）；请重新下载模型"
        )
    try:
        vocab = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SentencepieceDecodeError(f"读取词表失败：{exc}") from exc
    if not isinstance(vocab, list) or not vocab:
        raise SentencepieceDecodeError(f"词表格式错误（应为非空列表）：{path}")
    return list(vocab)


def _join_pieces(pieces: Iterable[str]) -> str:
    """复刻 sentencepiece ``DecodePieces``：▁ → 空格，去掉前导空格。"""
    out = ""
    for piece in pieces:
        if piece == _SPACE_SYMBOL:
            out += " "
        elif piece.startswith(_SPACE_SYMBOL):
            out += " " + piece[len(_SPACE_SYMBOL) :]
        else:
            out += piece
    # sentencepiece 解码第一个 ▁ 不产生前导空格（DecodeIds 的既定行为）
    return out.lstrip(" ")


def decode_ids(ids: Iterable[int], vocab: list[str]) -> str:
    """token id 序列 → 文本（等价于 ``sentencepiece.DecodeIds``）。

    Args:
        ids: 模型输出的 token id 序列（已过滤 blank 后仍可能含特殊 token）。
        vocab: :func:`load_vocab` 返回的词表。

    Returns:
        还原后的原始文本（含 ``<|...|>`` 特殊 token 原文；未做 emoji 替换）。
    """
    pieces: list[str] = []
    vocab_len = len(vocab)
    for i in ids:
        if i < 0 or i >= vocab_len:
            continue  # 越界按 unknown 跳过（sentencepiece 用 unk 表面，这里直接丢弃）
        piece = vocab[i]
        if piece in _SKIP_PIECES:
            continue
        pieces.append(piece)
    return _join_pieces(pieces)
