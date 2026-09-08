# CutFlow — AI 视频制作技能库

**素材进,成片出。** 让 AI Agent 端到端制作视频:接收口播视频 / 文案 / 剧本分镜 / 图片,经「感知 → 合成 → 剪辑 → 自评」产出成片,并同步生成**可在剪映 5.9 继续精修的草稿工程**。

- 支持:口播绿幕 / 纯文案动画 / 剧本分镜 / 图片素材;9:16 与 16:9
- 六类视频各有剪辑手册:`skills/cutflow/rules/genres/`(口播知识 / 动画教程 / 新闻采访 / 短剧 / 影视解说 / 通用规则)
- 脚本纯 Python 标准库 + FFmpeg,无需安装任何第三方库

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

### 第 3 步:体检

```powershell
python skills\cutflow\scripts\rs_doctor.py --report
```

打印环境自检报告:哪些 ✓ 就绪、哪些 ✗ 缺失、缺了怎么补。按提示补齐到全绿。

### 第 4 步:可选模块按需部署

| 命令 | 作用 | 大小 |
|---|---|---|
| `python tools\fetch_deps.py ocr` | OCR 文字识别(RapidOCR,免安装) | ~110MB |
| `python tools\fetch_deps.py vqa` | VQA 看图问答(QORA,Rust 引擎免 Python) | ~530MB |
| `python tools\fetch_deps.py` | 查看部署状态 | — |

**这两个是备选件**:AI 自带看图能力时不需要下载;批量处理图片或无视觉环境时才用。语音识别(FunASR)与配音(GPT-SoVITS)服务由 MomentShift / EchoSmith 提供,见下方参数表。

### 第 5 步:开工

对 AI 说人话即可,例如:**"帮我用这段口播视频做一条抖音竖版,加字幕和动画卡"**。Agent 会先问你几个问题(类型/平台/风格…),然后走流水线直到交付:

`06_output\成片A_口播精修_竖版_最终.mp4`、`封面.png`、`字幕_成片A.srt`、可编辑的剪映 5.9 草稿。

---

## Config 参数全表

配置文件为仓库根的 `config.json`(首次从 `config.example.json` 复制,或直接用图形编辑器生成)。**推荐用图形编辑器修改**;手改 JSON 时注意逗号与引号。

### 渲染

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `ffmpeg_dir` | FFmpeg 程序目录,剪辑的发动机 | ffmpeg.exe 所在 bin 目录。没有 → 运行 `tools\fetch_ffmpeg.py` 自动部署 |

### 感知服务(联网服务,本机起)

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `asr.url` | 语音识别服务地址(口播转字幕) | 默认 `http://127.0.0.1:8000`。由 MomentShift「服务模式」或 `tools\start_asr.py` 提供 |
| `asr.model` | 识别模型 | 默认 `paraformer-large`,中文最优,保持默认 |
| `momentshift_dir` | MomentShift 项目路径 | start_asr.py 会用到;按实际安装位置填 |

### 感知本地(备选件,见 ADR-0008)

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `ocr_exe` | OCR 文字识别程序路径 | 运行 `tools\fetch_deps.py ocr` 自动部署并回填;或手动解压 Release 包后填 OCR.exe 完整路径 |
| `vqa_exe` | VQA 看图问答引擎(qor08b.exe) | 运行 `tools\fetch_deps.py vqa` 自动部署并回填 |
| `vqa_python` + `vqa_cli` | VQA 本地项目模式(开发者) | 指向本地 VQA 仓库的 venv python 与 qora_cli.py;普通用户不用填 |
| `sense.force_local` | 强制用本地 OCR/VQA | 默认 `false`(AI 自带视觉优先)。纯文本模型环境/批量处理时改 `true` |

### 声音(配音)

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `tts.url` | GPT-SoVITS 引擎地址 | 默认 `http://127.0.0.1:9885`,由 EchoSmith 技能或 `api_v2.py` 提供 |
| `tts.engine_dir` | 引擎目录(含 api_v2.py、runtime) | EchoSmith 技能内 `models\GPT-SoVITS` |
| `tts.voices_dir` | 音色卡目录 | EchoSmith 内 `models\voices`(每个子文件夹一张音色卡) |
| `tts.default_voice` | 默认音色 | `koubo-test`(口播声线)或 `Jimi`;填音色卡目录名或权重名 |
| `tts.disabled_voices` | 禁用音色 | 暂不可用的音色名,逗号分隔(如权重文件缺失时) |

### 剪映 5.9(草稿直写,可选)

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `jianying59.exe` | 剪映 5.9 主程序 | `JianyingPro.exe` 完整路径。**必须 5.9 版**(6.0+ 草稿加密,无法直写);5.9 装好后关闭自动更新 |
| `jianying59.draft_root` | 草稿保存目录 | 剪映全局设置里查;默认 `C:\Users\<你>\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft` |
| `jianying59.root_meta` | 草稿清单文件 | 同目录下 `root_meta_info.json`;用 5.9 打开一次剪映后回填 |

### 扩展

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `workdir_root` | 视频工程根目录 | 如 `E:\视频工程`,每个工程自动建标准子目录 |
| `proxy` | 网络代理 | 如 `http://127.0.0.1:7890`;没有留空 |
| `artboard_dir` | artboard 技能路径 | 生成片头/封面/动画卡用;按 artboard 技能安装位置填 |

---

## 架构(一图)

```
素材 → 感知(FunASR 转写+Agent校对 / OCR+VQA备选) → 合成(音色卡配音 / artboard 片头封面动画)
     → 剪辑:毫秒级 IR ─┬→ FFmpeg 七步管线直出成片(字幕轻改写/绿幕虚拟背景/穿插动画/音效)
                        └→ 剪映 5.9 明文草稿(可编辑工程 + GUI 自动导出)
     → 自评(ffprobe 断言 + 抽帧目测 ≤3 轮) → 封面 → 清理 → 交付
```

设计决策见 `docs/adr/`(0001~0010),领域术语见 `CONTEXT.md`,六类视频剪辑手册见 `skills/cutflow/rules/genres/`。

## 致谢

[pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)(MIT)、[jianying-editor-skill](https://github.com/luoluoluo22/jianying-editor-skill)、[kbcut](https://github.com/starboom/kbcut)、[video-use](https://github.com/browser-use/video-use)、[lingji-cut](https://github.com/yoqu/lingji-cut)、[Generative-Media-Skills](https://github.com/SamurAIGPT/Generative-Media-Skills)、[hyperframes](https://github.com/heygen-com/hyperframes)、[make-prompt-seedance2](https://github.com/liangdabiao/make-prompt-seedance2)。音效:[Mixkit 免费许可](https://mixkit.free-license/)。

## License

MIT
