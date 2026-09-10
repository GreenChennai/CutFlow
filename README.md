# CutFlow — AI 视频制作技能库

**素材进,成片出。** 让 AI Agent 端到端制作视频:接收口播视频 / 文案 / 剧本分镜 / 图片,经
「转写对齐 → 粗剪 → 合成 → 剪辑 → 字幕 → 烧录 → 自检 → 封面文案」产出成片与平台物料,
并同步生成**可在剪映 5.9 继续精修的草稿工程**。

- 支持:口播绿幕 / 纯文案动画 / 剧本分镜 / 图片素材;9:16 与 16:9;多 Logo × 多比例变体
- 六类视频各有剪辑手册:`skills/cutflow/rules/genres/`(口播知识 / 动画教程 / 新闻采访 / 短剧 / 影视解说 / 通用规则)
- **自带语音识别**,不依赖任何外部服务;脚本纯 Python 标准库 + FFmpeg,零第三方依赖

---

## 这套流水线解决什么(v0.4 / v0.5 核心机制)

| 机制 | 解决的问题 | 一句话 |
|---|---|---|
| **Wordline 字级对齐** | 字幕/声音/画面对不上 | 全片时间只有一个真相源 `wordline.json`,任何模块不得自行算时间,禁止按字数比例插值 |
| **CutList 粗剪决策表** | 口误/重录/废片段剪不掉 | 三路检测器给出每一刀的 `reason + 置信度 + guard 三重校验`,宁可漏删不可错删,配审查包(每刀 3 秒音频) |
| **阶段缓存 + 一键重建** | 改一个字要重跑 20 分钟 | 缓存键含脚本 hash,`rs_run --status/--only/--from` 只重跑真变了的部分;每个文件夹有 `rebuild.py`,手改后双击级联重建 |
| **分级验证** | 每次都全检太费时间 | L0 机械自检每次秒级跑;L1 语义目测仅首次或画面变更;L2 验收归用户。输出必带 `verifyLevel` |
| **DP 字幕断句** | "滚滚长/江东逝水"式断错 | 约束最优 DP + 禁切表(词内/虚词后/数量词),竖屏 10–12 字/卡,CPS ≤9,Netflix 时长规范 |
| **品牌变体矩阵** | 一条视频要出多平台多 Logo 版 | `Logo × 比例` 一次编排,共享上游缓存,多变体只多付渲染钱 |
| **artboard 闭环** | 改张卡片图要全链重跑 | 卡片工程 ↔ 产物 ↔ IR 挂点记录在 `manifest.json`,改完 `rebuild.py` 直接导出合成 |

设计决策见 `docs/adr/`(0001–0017),迭代分析见 `docs/OPTIMIZATION-v4.md`(对齐/粗剪/增量/一条龙)与 `docs/OPTIMIZATION-v5.md`(自带 ASR/分级验证/一键重建/artboard 闭环)。

---

## 新手五步上手(每步一条命令)

### 第 1 步:部署 FFmpeg(约 100MB,自动下载)

```powershell
python tools\fetch_ffmpeg.py
```

自动下载 → 解压到 `E:\Tools\ffmpeg` → 写进 config。已完成过会跳过。

### 第 2 步:图形界面填配置(不用手写 JSON!)

双击 `tools\CutFlowConfigEditor.exe`(或从 [Release](https://github.com/GreenChennai/CutFlow/releases) 下载),像填表单一样填好每一项——每项都有中文说明告诉你**填什么、去哪找**。填完点【保存】。

> 手写 JSON 容易少个逗号报错,推荐永远用这个工具改配置。

### 第 3 步:部署自带语音识别(按需二选一)

```powershell
python tools\fetch_deps.py asr --onnx   # 轻量(~200MB):能转写,无字级时间戳
python tools\fetch_deps.py asr --pkg    # 精度(~1-2GB):官方 FunASR,有字级时间戳,推荐
```

已有模型副本可零拷贝播种:`python tools\fetch_deps.py asr --seed-models "<目录>"`。
查看状态:`python tools\fun_asr.py --probe`。

> ⚠️ 本机实测:Paraformer 的 ONNX 导出**没有 timestamp 输出**(导出阶段已压掉时间轴),
> 所以 `onnx` 后端只有 VAD 段级时间,Wordline 会标 `degraded`;要字级对齐精度请装 `--pkg`。

### 第 4 步:体检 + 可选模块

```powershell
python skills\cutflow\scripts\rs_doctor.py --report
```

打印环境自检报告:哪些 ✓ 就绪、哪些 ✗ 缺失、缺了怎么补。备选件:

| 命令 | 作用 | 大小 |
|---|---|---|
| `python tools\fetch_deps.py ocr` | OCR 文字识别(RapidOCR,免安装) | ~110MB |
| `python tools\fetch_deps.py vqa` | VQA 看图问答(QORA,Rust 引擎免 Python) | ~530MB |

**这两个是备选件**:AI 自带看图能力时不需要下载;批量处理图片或无视觉环境时才用。
配音(GPT-SoVITS)服务由 EchoSmith 提供,见下方参数表。

### 第 5 步:开工

对 AI 说人话即可,例如:**"帮我用这段口播视频做一条抖音竖版,加字幕和动画卡"**。Agent 会先问你几个问题(类型/平台/风格…),然后走流水线直到交付:

`06_output\成片A_口播精修_竖版_最终.mp4`、`封面.png`、`字幕_成片A.srt`、标题简介 Tag、可编辑的剪映 5.9 草稿。

---

## 管线总览(S0–S11)

```
S0 素材 ─► S1 转写+字级对齐 ─► S2 粗剪 ─► S3 基础合成 ─► S4 动画信息 ─► S5 品牌(Logo 变体)
  │          │wordline.json      │cutlist     │base          │composed     │branded/
  └──────────┴───────────────────┴────────────┴──────────────┴─────────────┤
                                   下游全部共享 map(t_src)→t_final          ▼
   S11 交付 ◄─ S10 封面+文案 ◄─ S9 自评断言 ◄─ S8 烧录导出 ◄─ S7 字幕 ◄─ S6 音效
```

- **S8 烧录导出**只用现有 `subtitles.ass` 重新烧录,**绝不重新生成字幕**——这是"手改字幕不会被覆盖"的保证。
- 每个阶段产物走声明式缓存:`rs_run.py --status` 看哪些阶段新鲜/过期,`--only S7` 只重跑字幕。

### 手改之后怎么重建(不需要 AI 从头跑)

每个阶段文件夹里都有 `rebuild.py`(`rs_run.py --init` 生成),固定四步:**备份 → 校验 → 级联 → 导出+自检**:

| 你改了什么 | 运行哪个 |
|---|---|
| 字幕 `06_output/subtitles.ass` | `python 06_output\rebuild.py` |
| IR / Wordline `05_ir/` | `python 05_ir\rebuild.py` |
| 粗剪决策 `04_cut/cutlist*.json` | `python 04_cut\rebuild.py` |
| artboard 卡片 `03_assets/artboard/` | `python 03_assets\artboard\rebuild.py` |
| 拿不准 | `python rebuild.py`(工程根,全量) |

校验不过会**停住并指出具体行**;跑砸了 `rs_run.py --rollback` 一键还原。

---

## Config 参数全表

配置文件为仓库根的 `config.json`(首次从 `config.example.json` 复制,或直接用图形编辑器生成)。**推荐用图形编辑器修改**。

### 渲染

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `ffmpeg_dir` | FFmpeg 程序目录,剪辑的发动机 | ffmpeg.exe 所在 bin 目录。没有 → 运行 `tools\fetch_ffmpeg.py` 自动部署 |

### 语音识别(自带,不再依赖外部服务)

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `asr.backend` | 后端选择 | `auto`(默认,精度优先 pkg→onnx→server)/ `pkg` / `onnx` / `server` |
| `asr.models_dir` | 本地模型目录 | `tools\fetch_deps.py asr --seed-models` 自动回填;默认 `<repo>\models\funasr\` |
| `asr.model` | 识别模型 | 默认 `paraformer-large`,中文最优,保持默认 |
| `asr.url` | HTTP 兼容服务地址(仅 `server` 后端用) | 默认 `http://127.0.0.1:8000`;不配则该后端不可用 |

> ASR 部署与模型播种见上文第 3 步;`pkg` 后端首次运行会从 ModelScope 下载 torch 权重(~1GB,一次性)。

### 声音(配音)

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `tts.url` | GPT-SoVITS 引擎地址 | 默认 `http://127.0.0.1:9885`,由 EchoSmith 技能或 `api_v2.py` 提供 |
| `tts.engine_dir` | 引擎目录(含 api_v2.py、runtime) | EchoSmith 技能内 `models\GPT-SoVITS` |
| `tts.voices_dir` | 音色卡目录 | EchoSmith 内 `models\voices`(每个子文件夹一张音色卡) |
| `tts.default_voice` | 默认音色 | 音色卡目录名或权重名 |
| `tts.disabled_voices` | 禁用音色 | 暂不可用的音色名,逗号分隔(如权重文件缺失时) |

### 感知本地(备选件,见 ADR-0008)

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `ocr_exe` | OCR 文字识别程序路径 | 运行 `tools\fetch_deps.py ocr` 自动部署并回填 |
| `vqa_exe` | VQA 看图问答引擎(qor08b.exe) | 运行 `tools\fetch_deps.py vqa` 自动部署并回填 |
| `vqa_python` + `vqa_cli` | VQA 本地项目模式(开发者) | 指向本地 VQA 仓库的 venv python 与 qora_cli.py;普通用户不用填 |
| `sense.force_local` | 强制用本地 OCR/VQA | 默认 `false`(AI 自带视觉优先)。纯文本模型环境/批量处理时改 `true` |

### 剪映 5.9(草稿直写,可选)

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `jianying59.exe` | 剪映 5.9 主程序 | `JianyingPro.exe` 完整路径。**必须 5.9 版**(6.0+ 草稿加密,无法直写);5.9 装好后关闭自动更新 |
| `jianying59.draft_root` | 草稿保存目录 | 剪映全局设置里查 |
| `jianying59.root_meta` | 草稿清单文件 | 同目录下 `root_meta_info.json`;用 5.9 打开一次剪映后回填 |

### 扩展

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `workdir_root` | 视频工程根目录 | 如 `E:\视频工程`,每个工程自动建标准子目录 |
| `proxy` | 网络代理 | 如 `http://127.0.0.1:7890`;没有留空 |
| `artboard_dir` | artboard 技能路径 | 生成片头/封面/动画卡用;按 artboard 技能安装位置填 |

---

## 常用命令速查

| 环节 | 命令 |
|---|---|
| 阶段状态 / 增量 | `rs_run.py --status` / `--from S3` / `--only S7` / `--explain S7` |
| 一键重建 | `rs_run.py --init`(生成 rebuild.py)/ `--rollback` |
| 分级自检 | `rs_verify.py <工程>` / `--mark-first --result pass` |
| 转写 | `python tools\fun_asr.py <媒体> --out 02_sensed\asr_raw.json` |
| 对齐(S1) | `rs_align.py build --media <素材> --out 05_ir\wordline.json` |
| 粗剪(S2) | `rs_cut.py 05_ir\wordline.json --detect all --out 04_cut` → `--apply` |
| 字幕(S7) | `rs_subtitle.py --from-wordline 05_ir\wordline.json --out 06_output` |
| 对齐自检 | `rs_sync.py --wordline ... --ass 06_output\subtitles.ass` |
| 文案(S10) | `rs_meta.py --wordline ... --brief 00_brief\brief.md --platform douyin,bili` |
| 剪映草稿 | `rs_jy_draft.py 05_ir\project.json --name <名>` |
| 清理 | `rs_cleanup.py <工程> [--apply]` |

全部脚本支持 `--json` 协议输出(`{"ok","code","message","data"}`),便于 Agent 消费。

---

## 架构(一图)

```
素材 → S1 自带ASR转写+字级对齐(Wordline唯一真相源) → S2 粗剪(CutList决策表+guard)
     → S3-S6 合成(TTS音色卡/artboard动画卡/Logo变体/音效落点)
     → 剪辑:毫秒级 IR ─┬→ FFmpeg 七步管线直出成片(字幕最后叠)
     │                  └→ 剪映 5.9 明文草稿(可编辑工程 + GUI 自动导出)
     → S7 字幕(DP断句+CPS硬约束) → S8 烧录 → S9 对齐断言 → S10 封面文案 → S11 交付
     全程:阶段缓存(增量重跑) + rebuild.py(手改级联重建) + 分级验证(L0/L1/L2)
```

领域术语见 `CONTEXT.md`,六类视频剪辑手册见 `skills/cutflow/rules/genres/`,Agent 编排总控见 `skills/cutflow/SKILL.md`。

## 致谢

[FunASR](https://github.com/modelscope/FunASR)(MIT,自带 ASR 的模型与推理层来源,见 `tools/asr_vendor/NOTICE.md`)、[pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)(MIT)、[jianying-editor-skill](https://github.com/luoluoluo22/jianying-editor-skill)、[kbcut](https://github.com/starboom/kbcut)、[video-use](https://github.com/browser-use/video-use)、[lingji-cut](https://github.com/yoqu/lingji-cut)、[Generative-Media-Skills](https://github.com/SamurAIGPT/Generative-Media-Skills)、[hyperframes](https://github.com/heygen-com/hyperframes)、[make-prompt-seedance2](https://github.com/liangdabiao/make-prompt-seedance2)。音效:[Mixkit 免费许可](https://mixkit.free-license/)。字幕规范参考 [Netflix Timed Text Style Guide](https://partnerhelp.netflixstudio.com/) 与 [BBC Subtitle Guidelines](https://www.bbc.co.uk/accessibility/forproducts/guides/subtitles/)。

## License

MIT
