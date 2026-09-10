# Copyright FunASR (https://github.com/alibaba-damo-academy/FunASR). All Rights Reserved.
#  MIT License  (https://opensource.org/licenses/MIT)
#
# 裁剪说明（MomentShift v0.8.4 内置版，相对 funasr_onnx 0.4.2 的 paraformer_bin.py）：
# - 移除 librosa：音频加载改用同包 ``utils/wav_io``（标准 PCM16/float32 wav，
#   16k 单声道；非 wav 或异常报人话错误）
# - 移除 modelscope / funasr 自动下载与导出（模型必须已存在于本地目录，
#   下载走 MomentShift 的 ``core/funasr_download``）
# - 移除 ContextualParaformer / SeacoParaformer / 时间戳绘图：本引擎只用
#   Paraformer（paraformer-large-onnx 是 2 输出：logits + token_num）
# - ``config.yaml`` 解析改用 ``utils/yaml_light``（不依赖 PyYAML）；
#   词表支持 tokens.json → config 内联 token_list → tokens.txt 三种来源
#
# 保留的公开接口：``Paraformer(model_dir, batch_size, device_id, quantize,
# intra_op_num_threads)`` + ``__call__(wav_path) -> [{"preds": str}, ...]``。

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .utils.frontend import WavFrontend
from .utils.postprocess_utils import sentence_postprocess
from .utils.utils import (
    CharTokenizer,
    Hypothesis,
    ONNXRuntimeError,
    OrtInferSession,
    TokenIDConverter,
    get_logger,
    read_yaml,
)
from .utils.wav_io import load_wav

logging = get_logger()

_CONFIG_NAMES = ("config.yaml", "config.yml", "asr.yaml")
_CMVN_NAMES = ("am.mvn", "vad.mvn")


def _resolve_config_path(model_dir: Path) -> Path:
    for name in _CONFIG_NAMES:
        p = model_dir / name
        if p.is_file():
            return p
    raise FileNotFoundError(f"模型缺少配置文件（{' / '.join(_CONFIG_NAMES)}）")


def _resolve_cmvn_path(model_dir: Path) -> Path:
    for name in _CMVN_NAMES:
        p = model_dir / name
        if p.is_file():
            return p
    raise FileNotFoundError(f"模型缺少 CMVN 文件（{' / '.join(_CMVN_NAMES)}）")


def _load_token_list(model_dir: Path, config: dict) -> list:
    """词表来源优先级：tokens.json → config 内联 token_list → tokens.txt。"""
    p = model_dir / "tokens.json"
    if p.is_file():
        with open(p, encoding="utf-8") as f:
            token_list = json.load(f)
        if isinstance(token_list, list) and token_list:
            return token_list
    inline = config.get("token_list")
    if isinstance(inline, list) and inline:
        return inline
    p = model_dir / "tokens.txt"
    if p.is_file():
        lines = [ln.rstrip("\r\n") for ln in p.read_text(encoding="utf-8").splitlines()]
        lines = [ln for ln in lines if ln != ""]
        if lines:
            return lines
    raise FileNotFoundError("模型缺少词表（tokens.json / tokens.txt / config 内联 token_list）")


class Paraformer:
    """
    Author: Speech Lab of DAMO Academy, Alibaba Group
    Paraformer: Fast and Accurate Parallel Transformer for Non-autoregressive
    End-to-End Speech Recognition
    https://arxiv.org/abs/2206.08317
    """

    def __init__(
        self,
        model_dir: str | Path = None,
        batch_size: int = 1,
        device_id: str | int = "-1",
        quantize: bool = False,
        intra_op_num_threads: int = 4,
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

        config_path = _resolve_config_path(model_dir)
        cmvn_path = _resolve_cmvn_path(model_dir)
        config = read_yaml(str(config_path))
        if not config:
            raise ValueError(f"配置文件解析为空：{config_path}")
        token_list = _load_token_list(model_dir, config)

        self.converter = TokenIDConverter(token_list)
        self.tokenizer = CharTokenizer()
        self.frontend = WavFrontend(cmvn_file=str(cmvn_path), **config.get("frontend_conf", {}))
        self.ort_infer = OrtInferSession(
            str(model_file), device_id, intra_op_num_threads=intra_op_num_threads
        )
        self.batch_size = batch_size
        model_conf = config.get("model_conf") or {}
        self.pred_bias = int(model_conf.get("predictor_bias", 0) or 0)
        self.language = config.get("lang")

    def __call__(self, wav_content: str | np.ndarray | list[str], **kwargs) -> list:
        waveform_list = self.load_data(wav_content, self.frontend.fs)
        waveform_nums = len(waveform_list)
        asr_res = []
        for beg_idx in range(0, waveform_nums, self.batch_size):
            end_idx = min(waveform_nums, beg_idx + self.batch_size)
            feats, feats_len = self.extract_feat(waveform_list[beg_idx:end_idx])
            try:
                outputs = self.infer(feats, feats_len)
                am_scores, valid_token_lens = outputs[0], outputs[1]
            except ONNXRuntimeError:
                logging.warning("input wav is silence or noise")
                continue
            preds = self.decode(am_scores, valid_token_lens)
            for pred in preds:
                text, _ = sentence_postprocess(pred)
                asr_res.append({"preds": text})
        return asr_res

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

    def infer(self, feats: np.ndarray, feats_len: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self.ort_infer([feats, feats_len])

    def decode(self, am_scores: np.ndarray, token_nums: int) -> list[str]:
        return [
            self.decode_one(am_score, token_num)
            for am_score, token_num in zip(am_scores, token_nums)
        ]

    def decode_one(self, am_score: np.ndarray, valid_token_num: int) -> list[str]:
        yseq = am_score.argmax(axis=-1)
        score = am_score.max(axis=-1)
        score = np.sum(score, axis=-1)

        # pad with mask tokens to ensure compatibility with sos/eos tokens
        # asr_model.sos:1  asr_model.eos:2
        yseq = np.array([1] + yseq.tolist() + [2])
        hyp = Hypothesis(yseq=yseq, score=score)

        # remove sos/eos and get results
        last_pos = -1
        token_int = hyp.yseq[1:last_pos].tolist()

        # remove blank symbol id, which is assumed to be 0
        token_int = list(filter(lambda x: x not in (0, 2), token_int))

        # Change integer-ids to tokens
        token = self.converter.ids2tokens(token_int)
        token = token[: valid_token_num - self.pred_bias]
        return token
