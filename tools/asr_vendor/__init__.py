# Copyright FunASR (https://github.com/alibaba-damo-academy/FunASR). All Rights Reserved.
#  MIT License  (https://opensource.org/licenses/MIT)
#
# MomentShift 内置 FunASR 推理（裁剪自 funasr_onnx 0.4.2）。
# v0.8.4：Paraformer 字级中文模型；依赖收敛为 numpy/onnxruntime/jieba。
# v0.8.5：新增 SenseVoiceSmall（多语种+标点）、FSMN-VAD（语音分段）、
# CAM++（说话人嵌入）——结构化输出管线。
#
# 注意：这里**不**在包级导入任何推理器（paraformer_bin / sensevoice_bin /
# vad_bin / spk_bin 都会拉 onnxruntime）。应用启动只 import 本包（或子模块
# utils.*）时不应加载 onnxruntime；推理器由 ``core.funasr_engine._get_model``
# 在首次推理时按需延迟导入。
"""MomentShift 内置 FunASR 推理子包（推理器按需延迟导入）。"""
