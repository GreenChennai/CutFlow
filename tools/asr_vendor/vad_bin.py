# Copyright FunASR (https://github.com/alibaba-damo-academy/FunASR). All Rights Reserved.
#  MIT License  (https://opensource.org/licenses/MIT)
#
# 裁剪说明（MomentShift v0.8.5 内置版，相对 funasr_onnx 0.4.2 的 vad_bin.py）：
# - 移除 librosa / pydub：音频加载改用同包 ``utils/wav_io``
# - 移除 modelscope / funasr 自动下载与导出（模型必须已存在于本地目录）
# - 移除 ``Fsmn_vad_online``（在线流式变体，本应用只用离线整段 VAD）
# - ``__call__`` 的分块循环按离线语义重写为 while 循环（上游对整段 feats_len
#   为 shape-(1,) 数组的 ``int()`` / ``min()`` 用法已废弃且有歧义）
#
# 保留的公开接口：``Fsmn_vad(model_dir, batch_size, device_id, quantize,
# intra_op_num_threads, provider)`` + ``__call__(wav_path) -> [[start_ms, end_ms],
# ...]``（语音段起止毫秒，相对整段音频）。

from __future__ import annotations

from pathlib import Path

import numpy as np

from .utils.e2e_vad import E2EVadModel
from .utils.frontend import WavFrontend
from .utils.utils import ONNXRuntimeError, OrtInferSession, get_logger, read_yaml
from .utils.wav_io import load_wav

logging = get_logger()

_CONFIG_NAMES = ("config.yaml", "config.yml", "vad.yaml")
_CMVN_NAMES = ("am.mvn", "vad.mvn")
# FSMN-VAD 模型是 10ms 帧移，160 样本/帧 @16k（上游硬编码，保持一致）
_FRAME_SAMPLES = 160


class Fsmn_vad:
    """
    Author: Speech Lab of DAMO Academy, Alibaba Group
    Deep-FSMN for Large Vocabulary Continuous Speech Recognition
    https://arxiv.org/abs/1803.05030
    """

    def __init__(
        self,
        model_dir: str | Path = None,
        batch_size: int = 1,
        device_id: str | int = "-1",
        quantize: bool = False,
        intra_op_num_threads: int = 4,
        max_end_sil: int = None,
        provider: str | None = None,
        **kwargs,
    ):
        model_dir = Path(model_dir)
        if not model_dir.is_dir():
            raise FileNotFoundError(f"模型目录不存在：{model_dir}")

        model_file = model_dir / ("model_quant.onnx" if quantize else "model.onnx")
        if not model_file.is_file():
            raise FileNotFoundError(
                f"模型文件缺失：{model_file.name}（quantize={quantize}，目录 {model_dir}）"
            )

        config_path = None
        for name in _CONFIG_NAMES:
            p = model_dir / name
            if p.is_file():
                config_path = p
                break
        if config_path is None:
            raise FileNotFoundError(f"模型缺少配置文件（{' / '.join(_CONFIG_NAMES)}）")

        cmvn_file = None
        for name in _CMVN_NAMES:
            p = model_dir / name
            if p.is_file():
                cmvn_file = p
                break
        if cmvn_file is None:
            raise FileNotFoundError(f"模型缺少 CMVN 文件（{' / '.join(_CMVN_NAMES)}）")

        config = read_yaml(str(config_path))
        if not config:
            raise ValueError(f"配置文件解析为空：{config_path}")

        frontend_conf = dict(config.get("frontend_conf") or {})
        frontend_conf["cmvn_file"] = str(cmvn_file)
        self.frontend = WavFrontend(**frontend_conf)
        self.ort_infer = OrtInferSession(
            str(model_file),
            device_id,
            intra_op_num_threads=intra_op_num_threads,
            provider=provider,
        )
        self.batch_size = batch_size
        model_conf = config.get("model_conf") or {}
        self.vad_scorer_config = model_conf
        self.max_end_sil = (
            max_end_sil if max_end_sil is not None else model_conf.get("max_end_silence_time", 800)
        )
        self.encoder_conf = config.get("encoder_conf") or {}

    def prepare_cache(self, in_cache: list | None = None) -> list:
        if in_cache is None:
            in_cache = []
        if len(in_cache) > 0:
            return in_cache
        fsmn_layers = self.encoder_conf["fsmn_layers"]
        proj_dim = self.encoder_conf["proj_dim"]
        lorder = self.encoder_conf["lorder"]
        for _ in range(fsmn_layers):
            cache = np.zeros((1, proj_dim, lorder - 1, 1)).astype(np.float32)
            in_cache.append(cache)
        return in_cache

    def __call__(self, audio_in: str | np.ndarray | list[str], **kwargs) -> list:
        waveform_list = self.load_data(audio_in, self.frontend.fs)
        waveform_nums = len(waveform_list)
        segments: list = []
        for beg_idx in range(0, waveform_nums, self.batch_size):
            vad_scorer = E2EVadModel(self.vad_scorer_config)
            end_idx = min(waveform_nums, beg_idx + self.batch_size)
            waveform = waveform_list[beg_idx:end_idx]
            feats, feats_len = self.extract_feat(waveform)
            waveform = np.array(waveform)
            in_cache = self.prepare_cache()
            try:
                total_frames = int(feats_len[0])
                step = int(min(total_frames, 6000))
                t_offset = 0
                while t_offset < total_frames:
                    cur_step = min(step, total_frames - t_offset)
                    is_final = (t_offset + cur_step) >= total_frames
                    feats_package = feats[:, t_offset : t_offset + cur_step, :]
                    waveform_package = waveform[
                        :,
                        t_offset
                        * _FRAME_SAMPLES : min(
                            waveform.shape[-1], (int(t_offset + cur_step) - 1) * _FRAME_SAMPLES + 400
                        ),
                    ]
                    inputs = [feats_package]
                    inputs.extend(in_cache)
                    scores, out_caches = self.infer(inputs)
                    in_cache = out_caches
                    segments_part = vad_scorer(
                        scores,
                        waveform_package,
                        is_final=is_final,
                        max_end_sil=self.max_end_sil,
                        online=False,
                    )
                    if segments_part:
                        for batch_num in range(self.batch_size):
                            segments.extend(segments_part[batch_num])
                    t_offset += cur_step
            except ONNXRuntimeError:
                logging.warning("input wav is silence or noise")
                segments = []
        return segments

    def load_data(self, wav_content: str | np.ndarray | list[str], fs: int = None) -> list:
        if isinstance(wav_content, np.ndarray):
            return [wav_content]
        if isinstance(wav_content, str):
            return [load_wav(wav_content)]
        if isinstance(wav_content, (list, tuple)):
            return [load_wav(path) for path in wav_content]
        raise TypeError(f"The type of {wav_content} is not in [str, np.ndarray, list]")

    def extract_feat(self, waveform_list: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        feats, feats_len = [], []
        for waveform in waveform_list:
            speech, _ = self.frontend.fbank(waveform)
            feat, feat_len = self.frontend.lfr_cmvn(speech)
            feats.append(feat)
            feats_len.append(feat_len)

        feats = self.pad_feats(feats, np.max(feats_len))
        feats_len = np.array(feats_len).astype(np.int32)
        return feats, feats_len

    @staticmethod
    def pad_feats(feats: list[np.ndarray], max_feat_len: int) -> np.ndarray:
        def pad_feat(feat: np.ndarray, cur_len: int) -> np.ndarray:
            pad_width = ((0, max_feat_len - cur_len), (0, 0))
            return np.pad(feat, pad_width, "constant", constant_values=0)

        feat_res = [pad_feat(feat, feat.shape[0]) for feat in feats]
        return np.array(feat_res).astype(np.float32)

    def infer(self, feats: list) -> tuple[np.ndarray, np.ndarray]:
        outputs = self.ort_infer(feats)
        scores, out_caches = outputs[0], outputs[1:]
        return scores, out_caches
