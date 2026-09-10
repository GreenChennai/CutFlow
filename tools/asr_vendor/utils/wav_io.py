"""标准 PCM WAV 读取（替代 librosa，仅标准库 + numpy）。

``funasr_onnx`` 全包只有一行用到了 librosa：::

    waveform, _ = librosa.load(path, sr=fs)

MomentShift 的 ASR 流水线（``core/asr_worker``）保证喂给引擎的音频是
16kHz 单声道 ``pcm_s16le``，因此这里只支持标准 RIFF/WAVE：

- 编码：PCM 16-bit（fmt=1, bits=16）或 IEEE float32（fmt=3, bits=32）
- 采样率：16000（不是 16k 时报人话错误，引导用 ffmpeg 归一化）
- 声道：任意（多声道取平均合成单声道，与 ``librosa.load(mono=True)`` 一致）

非 wav / 损坏 / 不支持的编码 → 抛 :class:`WavError`，``message`` 可直接展示。
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

EXPECTED_SAMPLE_RATE = 16000


class WavError(ValueError):
    """WAV 文件无法读取；``message`` 为可直接展示给用户的人话。"""


def _parse_chunks(data: bytes) -> tuple[tuple[int, int, int, int], bytes]:
    """遍历 RIFF 块，返回 ``(fmt 字段, data 字节)``。"""
    fmt: tuple[int, int, int, int] | None = None
    pcm: bytes | None = None
    pos = 12
    total = len(data)
    while pos + 8 <= total:
        cid = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = data[pos + 8 : pos + 8 + size]
        if cid == b"fmt ":
            if size < 16:
                raise WavError("WAV 格式块不完整")
            audio_format, num_channels, sample_rate, _byte_rate, _block_align, bits = (
                struct.unpack("<HHIIHH", body[:16])
            )
            fmt = (audio_format, num_channels, sample_rate, bits)
        elif cid == b"data":
            pcm = body
        pos += 8 + size + (size & 1)  # 块按 2 字节对齐
    if fmt is None or pcm is None:
        raise WavError("WAV 缺少 fmt 或 data 块")
    return fmt, pcm


def _probe_header(path: str) -> tuple[int, int, int, int] | None:
    """只读 RIFF 头（前 44+ 字节），返回 ``(audio_format, channels, rate, bits)``。

    返回 None 表示「无法确认是 16k 单声道 PCM16/float32 wav」（文件缺失 / 非
    wav / 头损坏 / 编码非 16bit-PCM 或 32bit-float 等）。
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with open(p, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if len(head) < 44 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return None
    try:
        (audio_format, num_channels, sample_rate, bits), _ = _parse_chunks(head)
    except WavError:
        return None
    return audio_format, num_channels, sample_rate, bits


def wav_needs_normalization(path: str) -> bool:
    """该 wav 是否需要先归一化成 16k 单声道 PCM16（v0.8.5 功能 3）。

    - 16k 单声道 PCM16 / float32 → False（直接读取）
    - 其它采样率 / 多声道 / 非 PCM 编码 / 无法确认 → True（交给 ffmpeg 重编码）

    Returns:
        True 表示需要先经 ffmpeg 归一化；False 表示可直接 ``load_wav``。
    """
    probe = _probe_header(path)
    if probe is None:
        return True
    audio_format, num_channels, sample_rate, bits = probe
    if sample_rate != EXPECTED_SAMPLE_RATE:
        return True
    if num_channels != 1:
        return True
    if not ((audio_format == 1 and bits == 16) or (audio_format == 3 and bits == 32)):
        return True
    return False


def load_wav(path: str) -> np.ndarray:
    """读取 16k 音频并归一为单声道 float32 ``[-1, 1]``。

    Args:
        path: 本地 .wav 文件路径。

    Returns:
        shape ``(n_samples,)`` 的 float32 波形。

    Raises:
        WavError: 文件缺失、非 WAV、编码不支持、采样率不是 16k。
    """
    p = Path(path)
    if not p.is_file():
        raise WavError(f"音频文件不存在：{path}")
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise WavError(f"读取音频失败：{exc}") from exc

    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise WavError("不是有效的 WAV 文件（缺少 RIFF/WAVE 头）")

    try:
        (audio_format, num_channels, sample_rate, bits), pcm = _parse_chunks(data)
    except struct.error as exc:
        raise WavError(f"WAV 文件损坏：{exc}") from exc

    if sample_rate != EXPECTED_SAMPLE_RATE:
        raise WavError(
            f"仅支持 16kHz 音频（当前 {sample_rate}Hz）；请先用「提取音频」或 ffmpeg 重采样"
        )

    if audio_format == 1 and bits == 16:
        elem = 2
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        scale = 32768.0
    elif audio_format == 3 and bits == 32:
        elem = 4
        samples = np.frombuffer(pcm, dtype="<f4").astype(np.float32)
        scale = 1.0
    else:
        raise WavError(
            f"不支持的 WAV 编码（format={audio_format}, bits={bits}）；仅支持 16-bit PCM / 32-bit float"
        )

    usable = (len(pcm) // (elem * num_channels)) * (elem * num_channels)
    if usable != len(pcm):
        samples = np.frombuffer(pcm[:usable], dtype=samples.dtype).astype(np.float32)
    if num_channels > 1:
        samples = samples.reshape(-1, num_channels).mean(axis=1)
    if scale != 1.0:
        samples = samples / scale
    return samples.astype(np.float32)
