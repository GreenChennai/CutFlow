"""FunASR 标点恢复 CT-Transformer（精简移植自 funasr_onnx 0.4.2）。

v0.8.11：CPU 可用的标点恢复（Paraformer 转写结果无标点，本模块加上）。

移植要点：
- 去掉原模块的 ``modelscope.snapshot_download`` / ``funasr.AutoModel.export``
  自动下载与 onnx 导出（用户自行把模型放 ``tools/funasr/ct-punc/``）；
- import 改用同包 ``funasr.utils.utils``（已移植版含 TokenIDConverter/
  OrtInferSession/read_yaml 等）；
- ``librosa`` 未引用（标点模型输入是文本，不走音频）。

原模块版权：FunASR (https://github.com/alibaba-damo-academy/FunASR)，MIT License。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .utils.utils import (
    ONNXRuntimeError,
    OrtInferSession,
    TokenIDConverter,
    code_mix_split_words,
    code_mix_split_words_jieba,
    get_logger,
    read_yaml,
    split_to_mini_sentence,
)

logging = get_logger("funasr.punc")


class CT_Transformer:
    """CT-Transformer 标点恢复（FunASR 官方）。"""

    def __init__(
        self,
        model_dir: str | Path,
        intra_op_num_threads: int = 4,
    ):
        model_dir = Path(model_dir)
        if not model_dir.is_dir():
            raise FileNotFoundError(f"标点模型目录不存在：{model_dir}")

        # 模型文件：优先 int8（量化），其次 fp32
        model_file = model_dir / ("model_quant.onnx" if (model_dir / "model_quant.onnx").is_file() else "model.onnx")
        if not model_file.is_file():
            raise FileNotFoundError(f"标点模型文件不存在：{model_file}")
        config_file = model_dir / "config.yaml"
        if not config_file.is_file():
            raise FileNotFoundError(f"标点配置不存在：{config_file}")
        token_file = model_dir / "tokens.json"
        if not token_file.is_file():
            raise FileNotFoundError(f"标点词表不存在：{token_file}")

        config = read_yaml(str(config_file))
        with open(token_file, encoding="utf-8") as f:
            token_list = json.load(f)

        self.converter = TokenIDConverter(token_list)
        self.ort_infer = OrtInferSession(
            str(model_file), device_id="-1", intra_op_num_threads=intra_op_num_threads
        )
        self.punc_list = config["model_conf"]["punc_list"]
        self.period = 0
        for i in range(len(self.punc_list)):
            if self.punc_list[i] == ",":
                self.punc_list[i] = "，"
            elif self.punc_list[i] == "?":
                self.punc_list[i] = "？"
            elif self.punc_list[i] == "。":
                self.period = i
        jieba_usr_dict_path = model_dir / "jieba_usr_dict"
        if jieba_usr_dict_path.exists():
            self.seg_jieba = True
            self.code_mix_split_words_jieba = code_mix_split_words_jieba(str(jieba_usr_dict_path))
        else:
            self.seg_jieba = False

    def infer(self, feats: np.ndarray, feats_len: np.ndarray):
        return self.ort_infer([feats, feats_len])

    def __call__(self, text: list | str, split_size: int = 20):
        """对文本加标点。

        Args:
            text: 字符串或词列表。
            split_size: 每批最大字数。

        Returns:
            ``(加标点文本, 标点 id 序列)``。
        """
        if self.seg_jieba:
            split_text = self.code_mix_split_words_jieba(text)
        else:
            split_text = code_mix_split_words(text)
        split_text_id = self.converter.tokens2ids(split_text)
        mini_sentences = split_to_mini_sentence(split_text, split_size)
        mini_sentences_id = split_to_mini_sentence(split_text_id, split_size)
        assert len(mini_sentences) == len(mini_sentences_id)

        cache_sent: list = []
        cache_sent_id: list = []
        new_mini_sentence = ""
        new_mini_sentence_punc: list[int] = []
        cache_pop_trigger_limit = 200
        for mini_sentence_i in range(len(mini_sentences)):
            mini_sentence = mini_sentences[mini_sentence_i]
            mini_sentence_id = mini_sentences_id[mini_sentence_i]
            mini_sentence = cache_sent + mini_sentence
            mini_sentence_id = np.array(cache_sent_id + mini_sentence_id, dtype="int32")
            data = {
                "text": mini_sentence_id[None, :],
                "text_lengths": np.array([len(mini_sentence_id)], dtype="int32"),
            }
            try:
                outputs = self.infer(data["text"], data["text_lengths"])
                y = outputs[0]
                punctuations = np.argmax(y, axis=-1)[0]
                assert punctuations.size == len(mini_sentence)
            except ONNXRuntimeError:
                logging.warning("标点推理失败，跳过")
                return text, []

            # 寻找最后一个句号/问号作为缓存切分点
            if mini_sentence_i < len(mini_sentences) - 1:
                sentence_end = -1
                last_comma_index = -1
                for i in range(len(punctuations) - 2, 1, -1):
                    if (
                        self.punc_list[punctuations[i]] == "。"
                        or self.punc_list[punctuations[i]] == "？"
                    ):
                        sentence_end = i
                        break
                    if last_comma_index < 0 and self.punc_list[punctuations[i]] == "，":
                        last_comma_index = i
                if (
                    sentence_end < 0
                    and len(mini_sentence) > cache_pop_trigger_limit
                    and last_comma_index >= 0
                ):
                    sentence_end = last_comma_index
                    punctuations[sentence_end] = self.period
                cache_sent = mini_sentence[sentence_end + 1 :]
                cache_sent_id = mini_sentence_id[sentence_end + 1 :].tolist()
                mini_sentence = mini_sentence[0 : sentence_end + 1]
                punctuations = punctuations[0 : sentence_end + 1]

            new_mini_sentence_punc += [int(x) for x in punctuations]
            words_with_punc = []
            for i in range(len(mini_sentence)):
                if i > 0:
                    if (
                        len(mini_sentence[i][0].encode()) == 1
                        and len(mini_sentence[i - 1][0].encode()) == 1
                    ):
                        mini_sentence[i] = " " + mini_sentence[i]
                words_with_punc.append(mini_sentence[i])
                if self.punc_list[punctuations[i]] != "_":
                    words_with_punc.append(self.punc_list[punctuations[i]])
            new_mini_sentence += "".join(words_with_punc)
            new_mini_sentence_out = new_mini_sentence
            new_mini_sentence_punc_out = new_mini_sentence_punc
            if mini_sentence_i == len(mini_sentences) - 1:
                if new_mini_sentence[-1] == "，" or new_mini_sentence[-1] == "、":
                    new_mini_sentence_out = new_mini_sentence[:-1] + "。"
                    new_mini_sentence_punc_out = new_mini_sentence_punc[:-1] + [self.period]
                elif new_mini_sentence[-1] != "。" and new_mini_sentence[-1] != "？":
                    new_mini_sentence_out = new_mini_sentence + "。"
                    new_mini_sentence_punc_out = new_mini_sentence_punc[:-1] + [self.period]
        return new_mini_sentence_out, new_mini_sentence_punc_out