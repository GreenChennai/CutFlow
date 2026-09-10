# NOTICE — tools/asr_vendor/

本目录是**第三方代码的本地副本(vendored)**,不是 CutFlow 原创代码。

## 出处

| 项 | 内容 |
|---|---|
| 上游项目 | **FunASR** — <https://github.com/alibaba-damo-academy/FunASR> |
| 许可证 | **MIT License** |
| 中间来源 | MomentShift `src/momentshift/core/funasr/`(FunASR 的裁剪版,裁剪自 `funasr_onnx` 0.4.2) |
| 复制日期 | 2026-09-10 |
| 复制范围 | `__init__.py`、`paraformer_bin.py`、`vad_bin.py`、`punc_bin.py`、`utils/`(共 137KB / 约 2771 行) |
| 未复制 | `sensevoice_bin.py`、`spk_bin.py`(CutFlow 不需要多语种与说话人嵌入) |

## 上游已做的裁剪(沿用,未再改动)

- 移除 `librosa` → 音频加载改用同包 `utils/wav_io`(仅 16k 单声道 PCM16/float32 wav);
- 移除 `modelscope` / `funasr` 自动下载与导出 → 模型**必须预先存在于本地目录**;
- `config.yaml` 解析改用 `utils/yaml_light`(不依赖 PyYAML);
- 词表支持 `tokens.json` → config 内联 `token_list` → `tokens.txt` 三种来源。

## CutFlow 侧的改动

仅两处,均为**包装层**,不改上游算法:

1. `__init__.py` 的注释改为说明 CutFlow 的用途与依赖;
2. 无其他修改。推理逻辑与上游逐行一致。

## 运行依赖

```
numpy
onnxruntime        # CPU 版即可
jieba              # 标点模型分词(ct-punc)
```

用 `python tools/fetch_deps.py asr --onnx` 自动创建 CutFlow 自己的 venv 并安装。

## 为什么不直接用 pip 装 `funasr`

官方 `funasr` 包依赖 PyTorch(约 1–2GB)。本目录提供的是**纯 ONNX Runtime** 路线
(约 200MB 依赖),代价是:

> ⚠️ **本目录的 Paraformer ONNX 导出只有 `logits` + `token_num` 两个输出,没有 `timestamp`。**
> CIF 下采样在导出阶段已把时间轴压掉,事后无法恢复。
> **因此 `onnx` 后端拿不到字级时间戳**,只能给到 VAD 段边界,Wordline 会被标记 `degraded`。
> 需要字级时间戳请用 `pkg` 后端(`tools/fetch_deps.py asr --pkg`,安装官方 funasr 包)。

## 升级上游

本副本不会自动跟随上游。升级方式:

```powershell
# 从新的 MomentShift 或 funasr_onnx 版本重新复制,并更新本文件的"复制日期"
python tools/fetch_deps.py asr --update-vendor "<新的 funasr 目录>"
```
