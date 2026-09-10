"""Kaldi 兼容 fbank 前处理（纯 numpy，替代 kaldi_native_fbank）。

``funasr_onnx`` 的 ``utils/frontend.py`` 依赖编译扩展 ``kaldi_native_fbank``
（knf）。MomentShift 运行时只允许 numpy/onnxruntime/jieba，因此这里用 numpy
复刻 knf 1.22.3 的**离线** Fbank 计算（``snip_edges=true``），逐条对齐上游：

- 帧提取：25ms 窗 / 10ms 移；不足整帧不产帧（HTK 式）
- 每帧顺序：去直流 → 预加重(0.97) → 加窗（Hamming：``0.54-0.46cos``）
- FFT：补零到 2 的幂（400 → 512），``numpy.fft.rfft`` 功率谱
- Mel 滤波器组：``num_fft_bins = padded/2``，``mel = 1127*ln(1+f/700)``，
  ``low_freq=20``、``high_freq=nyquist``，三角窗，``log(max(energy, eps))``

与上游唯一**有意差异**：``dither`` 置 0。knf 的 dither 用 C ``rand()`` 生成
逐帧随机高斯噪声（跨平台不可复现），幅度 ~1 个 int16 样本单位（相对 32768
约 90dB 以下），对识别结果影响可忽略；置 0 让推理完全确定、可测试。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np


def _padded_window_size(frame_length: int, round_to_power_of_two: bool = True) -> int:
    """FFT 补零长度：向上取到 2 的幂（knf 默认 ``round_to_power_of_two=true``）。"""
    if not round_to_power_of_two:
        return frame_length
    n = 1
    while n < frame_length:
        n <<= 1
    return n


def _mel_scale(freq: np.ndarray) -> np.ndarray:
    """Kaldi Mel 刻度：``1127 * ln(1 + f/700)``。"""
    return 1127.0 * np.log(1.0 + np.asarray(freq, dtype=np.float64) / 700.0)


def _mel_filterbank(
    fs: int, padded: int, n_mels: int, low_freq: float = 20.0, high_freq: float = 0.0
) -> np.ndarray:
    """构造 Kaldi 风格三角 Mel 滤波器组（稀疏矩阵 ``(n_mels, padded/2)``）。"""
    num_fft_bins = padded // 2
    fft_bin_width = fs / padded
    nyquist = 0.5 * fs
    high = nyquist + high_freq if high_freq <= 0 else high_freq
    mel_low = _mel_scale(low_freq)
    mel_high = _mel_scale(high)
    mel_delta = (mel_high - mel_low) / (n_mels + 1)
    bin_mels = _mel_scale(fft_bin_width * np.arange(num_fft_bins))

    weight = np.zeros((n_mels, num_fft_bins), dtype=np.float64)
    for b in range(n_mels):
        left = mel_low + b * mel_delta
        center = mel_low + (b + 1) * mel_delta
        right = mel_low + (b + 2) * mel_delta
        idx = np.nonzero((bin_mels > left) & (bin_mels < right))[0]
        if idx.size == 0:
            continue
        m = bin_mels[idx]
        w = np.where(
            m <= center,
            (m - left) / (center - left),
            (right - m) / (right - center),
        )
        weight[b, idx] = w
    return weight


def compute_fbank(
    waveform: np.ndarray,
    fs: int = 16000,
    window_type: str = "hamming",
    n_mels: int = 80,
    frame_length_ms: float = 25.0,
    frame_shift_ms: float = 10.0,
    dither: float = 0.0,
    preemph_coeff: float = 0.97,
    remove_dc_offset: bool = True,
    low_freq: float = 20.0,
    high_freq: float = 0.0,
    round_to_power_of_two: bool = True,
) -> np.ndarray:
    """波形（int16 尺度，即已乘 ``2^15``）→ log-mel fbank 特征矩阵。

    Args:
        waveform: 1D float32 波形，**int16 尺度**（与 knf 输入约定一致）。
        其余参数对齐 :class:`WavFrontend`。

    Returns:
        shape ``(num_frames, n_mels)`` 的 float32 特征。
    """
    wave = np.asarray(waveform, dtype=np.float32)
    frame_length = int(round(fs * frame_length_ms / 1000))
    frame_shift = int(round(fs * frame_shift_ms / 1000))
    padded = _padded_window_size(frame_length, round_to_power_of_two)

    if wave.size < frame_length:
        return np.zeros((0, n_mels), dtype=np.float32)
    num_frames = 1 + (wave.size - frame_length) // frame_shift

    # 窗函数（与 knf GetWindow 一致）：hamming / hanning / povey
    a = 2.0 * np.pi / (frame_length - 1)
    idx = np.arange(frame_length)
    win = np.zeros(padded, dtype=np.float64)
    if window_type == "povey":
        # 0.85 次幂的升余弦窗（kaldi 默认，CAM++ 说话人模型训练用）
        win[:frame_length] = np.power(0.5 - 0.5 * np.cos(a * idx), 0.85)
    elif window_type == "hanning":
        win[:frame_length] = 0.5 - 0.5 * np.cos(a * idx)
    else:  # hamming（默认，与 WavFrontend/ASR 模型一致）
        win[:frame_length] = 0.54 - 0.46 * np.cos(a * idx)

    mel = _mel_filterbank(fs, padded, n_mels, low_freq, high_freq)
    eps = np.finfo(np.float32).eps

    feats = np.empty((num_frames, n_mels), dtype=np.float32)
    for f in range(num_frames):
        seg = wave[f * frame_shift : f * frame_shift + frame_length].copy()
        if dither != 0.0:
            # 确定性高斯抖动（分布对齐 knf 的 RandGauss*dither，种子随帧号固定）
            rng = np.random.default_rng(seed=f)
            seg = seg + rng.standard_normal(frame_length).astype(np.float32) * dither
        if remove_dc_offset:
            seg = seg - seg.mean()
        if preemph_coeff != 0.0:
            seg[1:] -= preemph_coeff * seg[:-1]
            seg[0] -= preemph_coeff * seg[0]
        frame = np.zeros(padded, dtype=np.float32)
        frame[:frame_length] = seg
        frame = frame * win
        spec = np.fft.rfft(frame)
        power = spec.real * spec.real + spec.imag * spec.imag
        mel_energy = mel @ power[: padded // 2]
        feats[f] = np.log(np.maximum(mel_energy, eps))
    return feats


class WavFrontend:
    """Conventional frontend structure for ASR（与 funasr_onnx 同名同接口）。"""

    def __init__(
        self,
        cmvn_file: str = None,
        fs: int = 16000,
        window: str = "hamming",
        n_mels: int = 80,
        frame_length: int = 25,
        frame_shift: int = 10,
        lfr_m: int = 1,
        lfr_n: int = 1,
        dither: float = 0.0,
        **kwargs,
    ) -> None:
        self.fs = int(fs)
        self.window = window
        self.n_mels = int(n_mels)
        self.frame_length = float(frame_length)
        self.frame_shift = float(frame_shift)
        self.lfr_m = int(lfr_m)
        self.lfr_n = int(lfr_n)
        self.dither = float(dither)
        self.cmvn_file = cmvn_file
        if self.cmvn_file:
            self.cmvn = load_cmvn(self.cmvn_file)

    def fbank(self, waveform: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """波形（[-1,1] float32）→ log-mel 特征（与 funasr_onnx 一致先乘 2^15）。"""
        wave = np.asarray(waveform, dtype=np.float32) * (1 << 15)
        feat = compute_fbank(
            wave,
            fs=self.fs,
            window_type=self.window,
            n_mels=self.n_mels,
            frame_length_ms=self.frame_length,
            frame_shift_ms=self.frame_shift,
            dither=self.dither,
        )
        feat_len = np.array(feat.shape[0]).astype(np.int32)
        return feat, feat_len

    def lfr_cmvn(self, feat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """LFR 拼接 + CMVN 归一化。"""
        if self.lfr_m != 1 or self.lfr_n != 1:
            feat = self.apply_lfr(feat, self.lfr_m, self.lfr_n)
        if self.cmvn_file:
            feat = self.apply_cmvn(feat)
        feat_len = np.array(feat.shape[0]).astype(np.int32)
        return feat, feat_len

    @staticmethod
    def apply_lfr(inputs: np.ndarray, lfr_m: int, lfr_n: int) -> np.ndarray:
        """Low-Frame-Rate 拼接（与 funasr_onnx 逐行一致）。"""
        lfr_inputs = []
        t = inputs.shape[0]
        t_lfr = int(np.ceil(t / lfr_n))
        left_padding = np.tile(inputs[0], ((lfr_m - 1) // 2, 1))
        inputs = np.vstack((left_padding, inputs))
        t = t + (lfr_m - 1) // 2
        for i in range(t_lfr):
            if lfr_m <= t - i * lfr_n:
                lfr_inputs.append((inputs[i * lfr_n : i * lfr_n + lfr_m]).reshape(1, -1))
            else:
                num_padding = lfr_m - (t - i * lfr_n)
                frame = inputs[i * lfr_n :].reshape(-1)
                for _ in range(num_padding):
                    frame = np.hstack((frame, inputs[-1]))
                lfr_inputs.append(frame)
        return np.vstack(lfr_inputs).astype(np.float32)

    def apply_cmvn(self, inputs: np.ndarray) -> np.ndarray:
        """用 am.mvn 做均值方差归一化。"""
        frame, dim = inputs.shape
        means = np.tile(self.cmvn[0:1, :dim], (frame, 1))
        vars = np.tile(self.cmvn[1:2, :dim], (frame, 1))
        return (inputs + means) * vars


@lru_cache
def load_cmvn(cmvn_file: str | Path) -> np.ndarray:
    """读取 kaldi ``am.mvn`` 文件 → shape ``(2, dim)`` 的 numpy 数组。"""
    path = Path(cmvn_file)
    if not path.exists():
        raise FileNotFoundError("cmvn file not exits")
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    means_list: list[str] = []
    vars_list: list[str] = []
    for i in range(len(lines)):
        line_item = lines[i].split()
        if line_item and line_item[0] == "<AddShift>":
            line_item = lines[i + 1].split()
            if line_item and line_item[0] == "<LearnRateCoef>":
                add_shift_line = line_item[3 : (len(line_item) - 1)]
                means_list = list(add_shift_line)
                continue
        elif line_item and line_item[0] == "<Rescale>":
            line_item = lines[i + 1].split()
            if line_item and line_item[0] == "<LearnRateCoef>":
                rescale_line = line_item[3 : (len(line_item) - 1)]
                vars_list = list(rescale_line)
                continue
    means = np.array(means_list).astype(np.float64)
    vars = np.array(vars_list).astype(np.float64)
    return np.array([means, vars])
