# ASR — 自带语音识别(不需要任何外部服务器)

> **ADR-0015**。一句话:**`python tools/fun_asr.py <视频>` 就能转写,不用先开别的软件。**

## 1. 为什么

旧路径要求先启动 MomentShift 并切到"服务模式",忘了就转写失败;MomentShift 一升级/移动,CutFlow 就残废。现在 CutFlow 自带 ASR 运行器。

## 2. 先看后端状态

```powershell
python tools/fun_asr.py --probe
```

输出会说清三件事:**哪个后端可用 / 有没有字级时间戳 / 缺什么怎么装**。

## 3. 两个后端(能力不同,必须选对)

| 后端 | 实现 | **字级时间戳** | 依赖体积 | 速度 | 何时用 |
|---|---|---|---|---|---|
| **`pkg`** ⭐ | 官方 `funasr` 包(装在本项目自己的 venv) | **✅ 有** | ~1–2GB(torch-cpu) | 中 | 需要字级对齐精度时(推荐) |
| **`onnx`** | `tools/asr_vendor/`(FuASR 裁剪版,MIT) | **❌ 无** | ~200MB | 快(≈12× 实时) | 想先跑起来 / 不想装 torch |
| `server` | HTTP 兼容(config.asr.url) | 取决于服务 | — | 中 | **仅兼容保留**,不再默认 |

> ⚠️ **关键事实(本机实测)**:本地 Paraformer 的 ONNX 导出只有 `logits` + `token_num` **两个输出**,
> **没有 `timestamp`** —— CIF 下采样在导出阶段就把时间轴压掉了,事后无法恢复。
> 所以 `onnx` 后端**拿不到字级时间戳**,Wordline 会被标记 `degraded`。
> **这不是配置问题,是模型导出的固有限制。**

### 3.1 但 onnx 后端有个便宜的大改进

没有字级时间戳 ≠ 只能句级均分。**收紧 VAD 的静音切分阈值**,就能拿到接近句级的时间粒度:

| `max_end_sil` | 段数 | 段中位时长 | 说明 |
|---|---|---|---|
| 800(模型默认) | 5 | 15.9s | 段内均分误差大 |
| **400(本项目默认)** | **27** | **2.1s** | **拐点**,≈每句一段 |
| 300 | 36 | 1.9s | 收益递减,且有切在换气处的风险 |

实测素材:68s 中文口播;VAD 仅占总耗时约 7%(0.43s / 5.7s)。所以**默认就用 400**,不用改。

## 4. 安装

```powershell
# 轻量(约 200MB):numpy + onnxruntime + jieba
python tools/fetch_deps.py asr --onnx

# 精度(再加 torch-cpu + funasr,约 1-2GB):拿到字级时间戳
python tools/fetch_deps.py asr --pkg

# 模型:优先"播种",不要重新下载 237MB
python tools/fetch_deps.py asr --seed-models "<已有的 funasr 模型目录>"
#   默认用目录联接(junction),零拷贝、秒级;失败才复制(加 --copy 强制复制)

# 看状态
python tools/fetch_deps.py asr
```

模型落在 `config.asr.models_dir`(默认 `<repo>/models/funasr/`,**已在 .gitignore**)。
需要 `paraformer-large`(必需)、`fsmn-vad`(必需)、`ct-punc`(强烈建议,断句质量依赖它)。

### 4.1 模型格式与两个后端的关系(实测,容易踩)

| 目录里有什么 | onnx 后端 | pkg(torch 引擎)后端 |
|---|---|---|
| `model_quant.onnx`(播种副本通常是这种) | ✅ 直接用 | ❌ 读不了 → **自动回落 ModelScope 短名** |
| `model.pt` / `model.pb`(torch 权重) | ❌ 读不了 | ✅ 直接用(离线) |

- pkg 后端回落短名(`paraformer-zh` / `fsmn-vad` / `ct-punc`)时,首次运行自动从 ModelScope
  下载 torch 权重到 `~/.cache/modelscope`(~1GB,**一次性**),之后离线可用;
- 播种目录(ONNX)与 pkg 后端互不冲突:它只是"不能加速 pkg",不是错误;
- 判断逻辑在 `fun_asr.py pick()`:目录含 `model.pt`/`model.pb` 才算 torch 可用。

### 4.2 pkg 后端实测结论(2026-09-11,本机,68.9s 中文口播)

- **依赖坑**:funasr 抽 fbank 特征需要 torchaudio(`--pkg` 已一并装;老 venv 手动补
  `pip install torchaudio --index-url https://download.pytorch.org/whl/cpu`);
- **模型**:`paraformer-zh` 短名实际下载 **SeACo-Paraformer**(torch 权重 944MB,
  ModelScope,~19MB/s);fsmn-vad / ct-punc 同批自动下载;
- **字级时间戳**:334 字 ↔ 334 条一一对应。funasr 的 `timestamp` 只覆盖发音字,
  `fun_asr._align_ts_to_text()` 把标点零宽对齐(继承相邻发音字),含标点文本也能用;
- **质量对比**(同素材同参数,`--out` 字幕走同一 DP 断句):

| 指标 | pkg(字级真值) | onnx(降级均分) |
|---|---|---|
| conf 中位数 | **0.95** | 0.40 |
| 字幕卡数 | 35 | 40 |
| 约束违规 | **0** | 1 |
| 断句歧义卡 | **1** | **9** |
| 典型断句 | 干净的录音再加上 / 一点耐心 | 再加上一 / 点耐心和合 ❌ |

- **速度**:推理 RTF 0.074–0.088(≈11–13× 实时);首次运行含模型加载约 40s,热运行更快。

## 5. 用法

```powershell
# 转写(后端自动选:有 pkg 用 pkg,否则 onnx)
python tools/fun_asr.py 01_materials/a.mp4 --out 02_sensed/asr_raw.json

# 强制某后端
python tools/fun_asr.py a.mp4 --backend onnx

# 只调 VAD 粒度(段越细,时间粒度越好)
python tools/fun_asr.py a.mp4 --max-end-sil 300

# 走完整对齐(S1)
python skills/cutflow/scripts/rs_align.py build --media 01_materials/a.mp4 --out 05_ir/wordline.json
```

输出协议:`{"ok","code","message","data"}`,其中 `data.segments = [{start, end, text, timestamp?}]`
可直接喂 `rs_align.build_wordline`。

## 6. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| `NO_BACKEND` | 没装任何后端 | `python tools/fetch_deps.py asr --onnx` |
| `缺模型 paraformer-large` | 模型没播种 | `--seed-models "<目录>"` |
| `DEP_MISSING: onnxruntime` | 解释器不对 | 用 CutFlow 的 ASR venv,或重跑 `--onnx` |
| VAD 未检出语音段 | 素材是纯音乐/噪声,或音量过低 | 检查素材;静音素材本就无内容 |
| 输出**没有** `timestamp` | 用的是 onnx 后端 | 这是预期行为;要字级请装 `--pkg` |
| `ct-punc` 失败 | 缺 jieba 或模型 | 重跑 `--onnx`;不影响转写本身 |

## 7. 纪律

- **不要把 onnx 后端的产出当字级对齐结果用** —— 看 `data.degraded` 与 `capabilities.charTimestamps`;
- **不要在缺 VAD 模型时静默退化为整段均分** —— 直接报错让人装模型;
- `tools/asr_vendor/` 是第三方代码(MIT),改算法前先看 `NOTICE.md`;升级用 `fetch_deps.py asr --update-vendor <目录>`。
