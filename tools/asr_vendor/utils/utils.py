"""funasr_onnx 工具集裁剪版（只保留 Paraformer 需要的能力）。

相对上游 ``funasr_onnx/utils/utils.py`` 的改动：
- 移除 ``import yaml``（PyYAML），``read_yaml`` 改走同包 ``yaml_light``
- 移除注释掉的 torch 代码；``pad_list`` 修正为 numpy 写法（``x.shape[0]``）
- 保留 jieba 相关分词函数（jieba 是声明的运行时依赖）
"""

from __future__ import annotations

import functools
import logging
import re
import warnings
from pathlib import Path
from typing import Any, Iterable, NamedTuple

import jieba
import numpy as np
from onnxruntime import (
    GraphOptimizationLevel,
    InferenceSession,
    SessionOptions,
    get_available_providers,
    get_device,
)

from .yaml_light import read_yaml as read_yaml  # noqa: PLC0414

logger_initialized: dict[str, bool] = {}


def pad_list(xs: list[np.ndarray], pad_value: int, max_len: int = None) -> np.ndarray:
    """把不等长数组按 ``pad_value`` 补齐为 ``(batch, max_len)``。"""
    n_batch = len(xs)
    if max_len is None:
        max_len = max(x.shape[0] for x in xs)
    pad = (np.zeros((n_batch, max_len)) + pad_value).astype(np.int32)
    for i in range(n_batch):
        pad[i, : xs[i].shape[0]] = xs[i]
    return pad


class TokenIDConverter:
    """token 列表 ↔ id 转换（paraformer 字符级词表）。"""

    def __init__(self, token_list: list | str):
        self.token_list = token_list
        self.unk_symbol = token_list[-1]
        self.token2id = {v: i for i, v in enumerate(self.token_list)}
        self.unk_id = self.token2id[self.unk_symbol]

    def get_num_vocabulary_size(self) -> int:
        return len(self.token_list)

    def ids2tokens(self, integers: np.ndarray | Iterable[int]) -> list[str]:
        if isinstance(integers, np.ndarray) and integers.ndim != 1:
            raise TokenIDConverterError(f"Must be 1 dim ndarray, but got {integers.ndim}")
        return [self.token_list[i] for i in integers]

    def tokens2ids(self, tokens: Iterable[str]) -> list[int]:
        return [self.token2id.get(i, self.unk_id) for i in tokens]


class CharTokenizer:
    """字符级 tokenizer（与 funasr_onnx 一致）。"""

    def __init__(
        self,
        symbol_value: Path | str | Iterable[str] = None,
        space_symbol: str = "<space>",
        remove_non_linguistic_symbols: bool = False,
    ):
        self.space_symbol = space_symbol
        self.non_linguistic_symbols = self.load_symbols(symbol_value)
        self.remove_non_linguistic_symbols = remove_non_linguistic_symbols

    @staticmethod
    def load_symbols(value: Path | str | Iterable[str] = None) -> set:
        if value is None:
            return set()
        if isinstance(value, Iterable[str]):
            return set(value)
        file_path = Path(value)
        if not file_path.exists():
            logging.warning("%s doesn't exist.", file_path)
            return set()
        with file_path.open("r", encoding="utf-8") as f:
            return set(line.rstrip() for line in f)

    def text2tokens(self, line: str | list) -> list[str]:
        tokens = []
        while len(line) != 0:
            for w in self.non_linguistic_symbols:
                if line.startswith(w):
                    if not self.remove_non_linguistic_symbols:
                        tokens.append(line[: len(w)])
                    line = line[len(w) :]
                    break
            else:
                t = line[0]
                if t == " ":
                    t = "<space>"
                tokens.append(t)
                line = line[1:]
        return tokens

    def tokens2text(self, tokens: Iterable[str]) -> str:
        tokens = [t if t != self.space_symbol else " " for t in tokens]
        return "".join(tokens)

    def __repr__(self):
        return (
            f"{self.__class__.__name__}("
            f'space_symbol="{self.space_symbol}"'
            f'non_linguistic_symbols="{self.non_linguistic_symbols}"'
            f")"
        )


class Hypothesis(NamedTuple):
    """Hypothesis data type."""

    yseq: np.ndarray
    score: float | np.ndarray = 0
    scores: dict[str, float | np.ndarray] = dict()
    states: dict[str, Any] = dict()

    def asdict(self) -> dict:
        """Convert data to JSON-friendly dict."""
        return self._replace(
            yseq=self.yseq.tolist(),
            score=float(self.score),
            scores={k: float(v) for k, v in self.scores.items()},
        )._asdict()


class TokenIDConverterError(Exception):
    pass


class ONNXRuntimeError(Exception):
    pass


class OrtInferSession:
    """ONNX Runtime 推理会话（CPU 优先，与 funasr_onnx 一致）。

    v0.8.5 新增 ``provider`` 参数：显式指定推理后端（``"cpu"`` / ``"cuda"`` /
    ``None``）。None 沿用旧逻辑（device_id != -1 且环境是 GPU 才尝试 CUDA）；
    显式 ``"cuda"`` 时若 CUDAExecutionProvider 不可用则自动回退 CPU（策略由
    调用方用 ``core.hardware.asr_inference_device`` 保证，这里只是兜底）。
    GPU 会话不设 intra_op_num_threads（交给 CUDA 流调度），CPU 会话照常设置。
    """

    def __init__(self, model_file, device_id=-1, intra_op_num_threads=4, provider=None):
        device_id = str(device_id)
        sess_opt = SessionOptions()
        sess_opt.log_severity_level = 4
        sess_opt.enable_cpu_mem_arena = False
        sess_opt.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL

        cuda_ep = "CUDAExecutionProvider"
        cuda_provider_options = {
            "device_id": device_id,
            "arena_extend_strategy": "kNextPowerOfTwo",
            "cudnn_conv_algo_search": "EXHAUSTIVE",
            "do_copy_in_default_stream": "true",
        }
        cpu_ep = "CPUExecutionProvider"
        cpu_provider_options = {
            "arena_extend_strategy": "kSameAsRequested",
        }

        if provider == "cuda":
            # 显式 GPU：可用才用，不可用回退 CPU（不抛错，推理照常）
            if cuda_ep in get_available_providers():
                ep_list = [(cuda_ep, cuda_provider_options)]
            else:
                ep_list = []
                warnings.warn(
                    f"{cuda_ep} is not avaiable for current env, the inference part is "
                    f"automatically shifted to be executed under {cpu_ep}.\n",
                    RuntimeWarning,
                )
        elif provider == "cpu":
            ep_list = []
        else:
            # 旧行为：device_id != -1 且 onnxruntime 自报 GPU 才尝试 CUDA
            ep_list = []
            if device_id != "-1" and get_device() == "GPU" and cuda_ep in get_available_providers():
                ep_list = [(cuda_ep, cuda_provider_options)]

        # CPU 会话设置线程数；CUDA 会话不设（避免与 GPU 流调度冲突）
        if not ep_list or ep_list[0][0] != cuda_ep:
            sess_opt.intra_op_num_threads = intra_op_num_threads
        ep_list.append((cpu_ep, cpu_provider_options))

        self._verify_model(model_file)
        self.session = InferenceSession(model_file, sess_options=sess_opt, providers=ep_list)

        if provider == "cuda" and cuda_ep not in self.session.get_providers():
            warnings.warn(
                f"{cuda_ep} is not avaiable for current env, the inference part is "
                f"automatically shifted to be executed under {cpu_ep}.\n",
                RuntimeWarning,
            )

    def __call__(self, input_content: list[np.ndarray], run_options=None) -> np.ndarray:
        input_dict = dict(zip(self.get_input_names(), input_content))
        try:
            return self.session.run(self.get_output_names(), input_dict, run_options)
        except Exception as e:
            raise ONNXRuntimeError("ONNXRuntime inferece failed.") from e

    def get_input_names(self):
        return [v.name for v in self.session.get_inputs()]

    def get_output_names(self):
        return [v.name for v in self.session.get_outputs()]

    @staticmethod
    def _verify_model(model_path):
        model_path = Path(model_path)
        if not model_path.exists():
            raise FileNotFoundError(f"{model_path} does not exists.")
        if not model_path.is_file():
            raise FileExistsError(f"{model_path} is not a file.")


def split_to_mini_sentence(words: list, word_limit: int = 20):
    assert word_limit > 1
    if len(words) <= word_limit:
        return [words]
    sentences = []
    length = len(words)
    sentence_len = length // word_limit
    for i in range(sentence_len):
        sentences.append(words[i * word_limit : (i + 1) * word_limit])
    if length % word_limit > 0:
        sentences.append(words[sentence_len * word_limit :])
    return sentences


def code_mix_split_words(text: str):
    words = []
    segs = text.split()
    for seg in segs:
        current_word = ""
        for c in seg:
            if len(c.encode()) == 1:
                current_word += c
            else:
                if len(current_word) > 0:
                    words.append(current_word)
                    current_word = ""
                words.append(c)
        if len(current_word) > 0:
            words.append(current_word)
    return words


def isEnglish(text: str) -> bool:
    return bool(re.search("^[a-zA-Z']+$", text))


def join_chinese_and_english(input_list) -> str:
    line = ""
    for token in input_list:
        if isEnglish(token):
            line = line + " " + token
        else:
            line = line + token
    return line.strip()


def code_mix_split_words_jieba(seg_dict_file: str):
    jieba.load_userdict(seg_dict_file)

    def _fn(text: str):
        input_list = text.split()
        token_list_all = []
        langauge_list = []
        token_list_tmp = []
        language_flag = None
        for token in input_list:
            if isEnglish(token) and language_flag == "Chinese":
                token_list_all.append(token_list_tmp)
                langauge_list.append("Chinese")
                token_list_tmp = []
            elif not isEnglish(token) and language_flag == "English":
                token_list_all.append(token_list_tmp)
                langauge_list.append("English")
                token_list_tmp = []

            token_list_tmp.append(token)

            if isEnglish(token):
                language_flag = "English"
            else:
                language_flag = "Chinese"

        if token_list_tmp:
            token_list_all.append(token_list_tmp)
            langauge_list.append(language_flag)

        result_list = []
        for token_list_tmp, language_flag in zip(token_list_all, langauge_list):
            if language_flag == "English":
                result_list.extend(token_list_tmp)
            else:
                seg_list = jieba.cut(join_chinese_and_english(token_list_tmp), HMM=False)
                result_list.extend(seg_list)
        return result_list

    return _fn


@functools.lru_cache
def get_logger(name="funasr_onnx"):
    """Initialize and get a logger by name（与 funasr_onnx 一致）。"""
    logger = logging.getLogger(name)
    if name in logger_initialized:
        return logger
    for logger_name in logger_initialized:
        if name.startswith(logger_name):
            return logger
    formatter = logging.Formatter(
        "[%(asctime)s] %(name)s %(levelname)s: %(message)s", datefmt="%Y/%m/%d %H:%M:%S"
    )
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    logger.addHandler(sh)
    logger_initialized[name] = True
    logger.propagate = False
    logging.basicConfig(level=logging.ERROR)
    return logger
