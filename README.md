<h1 align="center">CutFlow · AI 视频总控技能</h1>

<p align="center">
  <strong>给 Agent 一句提示词,出一条可审计的成片</strong><br>
  提示词经「意图编译」变成结构化参数,再由 S0–S11 机械臂确定性执行:<br>
  转写对齐 → 粗剪 → 合成 → 字幕 → 烧录 → 分级自检 → 封面文案,<br>
  同步产出<b>可在剪映 5.9 继续精修的原生草稿</b>与 <b>CutForge 可继续编辑的工程</b>——每一步可解释、可复现、可回退。
</p>

<p align="center">
  同伴项目 <a href="https://github.com/GreenChennai/cutforge">CutForge</a>(Rust 编辑器内核):CutFlow 管 S0–S11 批量机械臂,CutForge 管"人的手"——时间线交互、标注、AI 可 diff 的编辑;两者读写同一份工程文件,经 <code>rs_editor / rs_notes / rs_oplog / rs_gate</code> 四桥互通。
</p>

---

## 🆕 v0.16–v0.18 · 提示词驱动 + 管线可信大版本

> 本轮把"能用"推进到"像 AI 生图一样给提示词就出片",并把状态机、断句、剪映出口全部换成可机械验收的机制。

- **提示词 → 全自动**:新增**意图编译器** `rs_intent.py`——Agent 只做"读懂提示词、写文案"两件事,校验/补默认/查**风格注册表**全部脚本化(同输入字节级同输出);`rs_run --auto` 无人值守:断句歧义自动裁决留痕、粗剪 review 保守保留、L1 降级为抽帧留证、L0 硬闸不放松、**L2 验收始终归用户**;每个参数的来源(用户明说 / 注册表默认 / 推断)落 `decision_log`,交付附人读**决策说明书**;
- **断句与片尾根治**:时长以 **ffprobe 实测**为准并带"钳制迹象"检测;粗剪 keep 末段自动保底 0.5–0.8s 自然底噪(片尾不再"戛然而止");改 keep 后时长账自动同步;幽灵字符在本体丢弃(`prune-ghost`),二次重映射被拦截;断句"末卡回吸"+ 文件引文/动宾禁切,昨日真实翻车四案例进回归集;
- **状态机诚实**:`failed` 状态、状态与产物原子写、`--only` 命中已办结报错而非静默跳过、`--force` 必须带目标、级联重跑每个写盘阶段先备份、S5/S8 各写独占子目录、与 CutForge 同工程时的只读降级;
- **Token 治理**:**能力目录** `capabilities.json`(30 工具 / 51 条命令,由 argparse 自动生成、防漂移测试把守)+ "手册命令 ↔ argparse"机械对拍门禁(138 条)+ Hard Rule「禁止 Agent 现写剪辑逻辑脚本」;
- **剪映出口换血**:IR→草稿抽出**编译层**(`--dry-run` 打印映射表)+ 草稿结构门禁(帧网格 / 重叠 / 黑场 / 时长和)+ **保护区**(protect,切点不得侵入,侵入即报错)+ [诚实验收说明](skills/cutflow/rules/jianying-verification.md)(已验证 / 未验证 / 拒绝 三类如实写明);
- **与 CutForge 闭环**:编辑器改完盘面 → `.cutforge` 会话摘要 → `rs_run --status` 精确标脏 → `rs_editor.py diff` 人话差异 → `rebuild.py` / `--from S8 --force` 定向重建,只重跑真变了的部分。

## ✨ 它解决什么问题

| | 一般"AI 剪辑" | CutFlow |
|---|---|---|
| 入口 | 每次人肉调参 | 一句提示词,意图编译查表出参数 |
| 时间 | 按字数比例插值,字幕对不上嘴型 | FunASR **字级对齐**,全片唯一真相源 `wordline.json` |
| 剪辑决策 | 黑盒一刀切 | 七类检测器给每刀 `reason + conf`,guard 校验,**宁可漏删不可错删** |
| 断句 | 大模型现编现错 | 约束最优 DP + 词边界 + 禁切表 + 回归集 |
| 出错 | 静默继续 | 失败必响:`failed` 状态 + 非零退出码 + 退出码诚实门禁 |
| 片尾 | 按错误时长硬切,尾音消失 | ffprobe 实测 + 钳制检测 + 片尾保底 |
| 交付 | 一条成片 | 成片 + 字幕 + 封面文案 + 剪映草稿 + **决策说明书**(每个参数为什么是这个值) |

## 🔄 流水线(S0–S11)

```
S0 素材(幕布检测门禁)→ S1 自带 ASR 转写+字级对齐+能量校准 → S2 粗剪(七类检测器+guard+删改稿)
   → S3 基础合成 → S4 动画卡 → S5 品牌变体(branded/) → S6 音效
   → S7 字幕(DP 断句) → S8 烧录导出(final/) → S9 对齐断言+音频内容闸+QC 体检
   → S10 封面+文案 → S11 交付对账(含决策说明书)
   出口:剪映 5.9 原生草稿(编译层+门禁)/ CutForge 编辑器(同一工程文件)
```

- **S8 烧录**只用现有 `subtitles.ass` 重烧,绝不重新生成字幕——手改字幕不会被覆盖;
- **S9 三重机械闸**:①字幕↔Wordline 对齐断言(中位 ≤40ms);②成片音频内容闸(ASR 对账:片头句唯一 / 相似度 ≥0.90 / 无重复段);③QC 体检(黑帧 / 冻结 / VFR / 响度);
- **阶段缓存 + 一键重建**:缓存键含脚本 hash 与 brief 参数,`--status / --only / --from` 只重跑真变了的部分。

### 手改之后怎么重建(不需要 AI 从头跑)

每个阶段文件夹都有 `rebuild.py`(`rs_run.py --init` 生成),固定四步:**备份 → 校验 → 级联 → 导出+自检**:

| 你改了什么 | 运行哪个 |
|---|---|
| 字幕 `06_output/subtitles.ass` | `python 06_output\rebuild.py` |
| IR / Wordline `05_ir/`(手注过单 clip 音频/转场) | ⚠ 改跑 `python 06_output\rebuild.py`(S8 只重烧,不碰 IR) |
| 粗剪决策 `04_cut/cutlist*.json` | `python 04_cut\rebuild.py` |
| artboard 卡片 `03_assets/artboard/` | `python 03_assets\artboard\rebuild.py` |
| CutForge 编辑器改了盘面 | `rs_run.py --status` 看标脏 → `rs_editor.py diff` 看改了什么 → 上表定向重建 |
| 拿不准 | `python rebuild.py`(工程根,全量) |

校验不过会**停住并指出具体行**;跑砸了 `rs_run.py --rollback` 一键还原。

## 🚀 快速开始

### 第 1 步:部署 FFmpeg(约 100MB,自动下载)

```powershell
python tools\fetch_ffmpeg.py
```

### 第 2 步:图形界面填配置(不用手写 JSON!)

双击 `tools\CutFlowConfigEditor.exe`(或从 [Release](https://github.com/GreenChennai/CutFlow/releases) 下载),像填表单一样填好每一项——每项都有中文说明。参数全表见下文。

### 第 3 步:部署自带语音识别

```powershell
python tools\fetch_deps.py asr --pkg     # 精度(~1-2GB):官方 FunASR,字级时间戳,推荐
python tools\fetch_deps.py asr --onnx    # 轻量(~200MB):能转写,无字级时间戳(降级标 degraded)
python tools\fun_asr.py --probe          # 查看状态;未就绪时 rs_align 会自动 --ensure 部署
```

> onnx 导出没有 timestamp 输出(模型导出固有限制),要字级对齐请用 `--pkg`。
> pkg 后端首次运行从 ModelScope 下载 torch 权重(~1GB,一次性)。

### 第 4 步:体检 + 可选模块

```powershell
python skills\cutflow\scripts\rs_doctor.py --report
```

| 命令 | 作用 | 大小 |
|---|---|---|
| `python tools\fetch_deps.py ocr` | OCR 文字识别(RapidOCR) | ~110MB |
| `python tools\fetch_deps.py vqa` | VQA 看图问答(QORA) | ~530MB |

备选件:AI 自带看图能力时不需要;批量处理/无视觉环境才用。

### 第 5 步:开工

对 AI 说人话:

```text
「帮我用这段口播视频做一条抖音竖版,加字幕」
「知识口播,抖音竖屏,快节奏,大字幕——素材在这个文件夹,全自动跑完给我看」
「按 cutlist.applied.json 重出工程,粗剪改了三刀」
「把结尾那句话的重音留给下一卡,重新断句」
「给这条片子出 9:16 和 16:9 两个版本,Logo 用这两张」
「编辑器里我改了几刀,看看改了什么,把成片重出一遍」
```

交付:成片 mp4、`subtitles.ass`+`master.srt`、封面、标题简介 Tag、可编辑的剪映 5.9 草稿、`sync_report.md`(对齐断言 + QC 体检 + 音频内容闸)、**决策说明书**(`--auto` 时)。

## 🎬 视频类型(开放注册表)

| 类型 | 状态 | 说明 |
|---|---|---|
| 纯口播 `talking-head` | ✅ | 素材须已抠像合成背景 |
| 口播+动画 `talking-head+animation` | ✅ | 知识/数据/对比/章节四类卡按 wordline 卡点 |
| 纯动画 `pure-animation` | ✅ | 旁白+场景卡承载信息流 |
| **vlog** | ✅ 新增 | 素材碎片多、节奏自由,BGM + 转场档 |
| **混剪** | ✅ 新增 | 卡点/音乐驱动,复用 ducking + 转场语法 |
| 录屏 / 访谈 / 剧情 / 影评 | 🟡 预留 | 每型一册手册,按需增补(新增类型 = 新增一册 + 一条注册表条目,不改引擎) |

画幅 9:16 / 3:4 / 16:9,**平台字幕预设**(抖音 / 视频号 / 小红书 / B站),多 Logo × 多比例**变体矩阵**;风格 = `videoType × 平台预设 × 画幅 × 节奏档 × 字幕样式 × 卡片模板 × BGM`,由 [registry.json](skills/cutflow/templates/styles/registry.json) 查表出默认值。

## 📐 剪辑水平:口径与出处

CutFlow 的"剪得好"不是玄学,每条数值都有出处(完整索引见 [ITERATION-GUIDE §10](docs/ITERATION-GUIDE-v0.11.md)):

| 口径 | 数值 | 出处 |
|---|---|---|
| 总线响度 | -14 LUFS ±1 / TP ≤ -1 dBTP(双 pass loudnorm) | EBU R128 口径;-14 为 Spotify/YouTube 归一化对齐值(非平台强制) |
| 字幕 | ≤16 字/行、≤2 行、成人 ≤9 CPS、0.83–7s、间距 ≥2 帧 | Netflix 简体中文 Timed Text 规范 |
| 视觉节拍 | 卡时长 1.5–3.5s;镜头/视觉变化 2–3s 窗口 | Cutting(康奈尔)好莱坞 75 年镜头时长实证 |
| A/V 同步 | 超前 ≤40ms / 滞后 ≤60ms 告警 | EBU R37(ATSC IS-191 更严者作参考) |
| 粗剪防护 | margin 前 150/后 300ms;碎刀 <120ms 放弃;碎片 <100ms 并刀 | auto-editor `--margin/--smooth` 精神 |
| punch-in | 1.4x 起步、密度 15s/≤3 处 | Frame.io/r.editors 共识 + Hitchcock 规则(经验值,已标注) |
| 片尾 | 口播结尾保留 0.5–0.8s 自然底噪 | 听觉自然度经验值(ADR-0043) |

找不到权威出处的数值一律标注「经验值」——不把行业传说包装成规范。

## ⚙️ Config 参数全表

配置文件为仓库根 `config.json`(首次从 `config.example.json` 复制,或用图形编辑器生成)。**推荐用图形编辑器修改**。

### 渲染

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `ffmpeg_dir` | FFmpeg 程序目录,剪辑的发动机 | ffmpeg.exe 所在 bin 目录。没有 → `tools\fetch_ffmpeg.py` 自动部署 |

### 语音识别(自带,不依赖外部服务)

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `asr.backend` | 后端选择 | `auto`(默认,精度优先 pkg→onnx)/ `pkg` / `onnx` |
| `asr.models_dir` | 本地模型目录 | `--seed-models` 播种自动回填;默认 `<repo>\models\funasr\` |
| `asr.model` | 识别模型 | 默认 `paraformer-large`,中文最优,保持默认 |

### 声音(配音,可选)

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `tts.url` | GPT-SoVITS 引擎地址 | 默认 `http://127.0.0.1:9885`,由 EchoSmith 技能或 `api_v2.py` 提供 |
| `tts.voices_dir` / `tts.default_voice` / `tts.disabled_voices` | 音色卡目录/默认音色/禁用列表 | EchoSmith 内 `models\voices`(每个子文件夹一张音色卡) |

### 感知本地(备选件)

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `ocr_exe` / `vqa_exe` | OCR / VQA 程序路径 | `fetch_deps.py ocr|vqa` 自动部署回填 |
| `sense.force_local` | 强制本地 OCR/VQA | 默认 `false`(AI 自带视觉优先) |

### 剪映 5.9(草稿直写,可选)

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `jianying59.exe` | 剪映 5.9 主程序 | **必须 5.9 版**(6.0+ 草稿加密,永不写入);装好后关闭自动更新 |
| `jianying59.draft_root` / `root_meta` | 草稿目录/清单文件 | 剪映全局设置里查;用 5.9 打开一次后回填 |

### 扩展

| 参数 | 作用 | 填什么、从哪获取 |
|---|---|---|
| `workdir_root` | 视频工程根目录 | 如 `E:\视频工程`,每工程自动建标准子目录 |
| `proxy` | 网络代理 | 如 `http://127.0.0.1:7890`;没有留空 |
| `artboard_dir` | artboard 技能路径 | 生成片头/封面/动画卡用 |

## 🧰 常用命令速查

> Agent 请先读 [capabilities.json](skills/cutflow/capabilities.json)(机器可读能力目录,由 argparse 自动生成),不读源码。

| 环节 | 命令 |
|---|---|
| 意图编译(提示词入口) | `rs_intent.py compile --brief 00_brief\brief.json --plan 00_brief\plan.json --out 00_brief`(`--dry-run` 打印推断表) |
| 全自动流水线 | `rs_run.py --auto`(无人值守:自动裁决+留痕;L2 验收仍归用户) |
| 阶段状态 / 增量 | `rs_run.py --status` / `--from S3` / `--only S7` / `--explain S7` |
| 一键重建 | `rs_run.py --init`(生成 rebuild.py)/ `--rollback` |
| 分级自检 | `rs_verify.py <工程>` / `--mark-first --result pass` |
| 转写 | `python tools\fun_asr.py <媒体>` / `--probe` |
| 对齐(S1) | `rs_align.py build --media <素材> --out 05_ir\wordline.json`(专名错 → `--terms-file 00_brief\terms.txt` 热词重跑) |
| wordline 平滑 / 时长修正 | `rs_align.py smooth …` / `rs_align.py refresh-durations 05_ir\wordline.json --media <素材>`(只改时长不动字符时间) |
| 粗剪(S2) | `rs_cut.py 05_ir\wordline.json --detect all --media 源 --out 04_cut` → `--apply`(自动同步时长账;`--protect` 标保护区) |
| 按文本裁片 | `rs_cut.py 05_ir\wordline.json --from-text "只想要的引文"`(引文外走 guard) |
| CutList→IR(S3) | `rs_ir.py build --from-cutlist 04_cut\cutlist.applied.json --slug X --out 05_ir\project.json`(`--punch-in-auto` 启用变焦掩饰) |
| 挂动画卡(I7 / O2) | `rs_ir.py add-overlay --manifest 03_assets\artboard\manifest.json --plan 00_brief\cards.json` / `rs_artboard.py gen-cards --from 00_brief\cards.json` |
| 字幕(S7) | `rs_subtitle.py --from-wordline 05_ir\wordline.final.json --platform douyin --out 06_output`(`--override` 复核回灌,余字自动重组) |
| 对齐自检+体检(S9) | `rs_sync.py --wordline 05_ir\wordline.final.json --ass 06_output\subtitles.ass --video 成片.mp4 --audio-content --qc` |
| 封面文案(S10) | `rs_meta.py --wordline ... --brief 00_brief\brief.md --platform douyin,bili` |
| 决策说明书 | `rs_ingest.py decisions <工程>`(每个参数从哪来,改一条重跑一段) |
| 剪映草稿 | `rs_jy_draft.py 05_ir\project.json --name <名>`(`--dry-run` 打印 IR→草稿映射表;只写 5.9 明文草稿) |
| 清理 | `rs_cleanup.py <工程> [--apply]` |
| CutForge 桥:编辑器视图/变更识别 | `rs_editor.py view/timeline/check/diff <工程>`(只读;diff 输出编辑器改动的人话摘要) |
| CutForge 桥:标注 | `rs_notes.py list/stats <工程> [--state open]`(含孤儿统计) |
| CutForge 桥:OpLog 审计 | `rs_oplog.py tail/report <工程> [--actor agent]`(AI 改了什么) |
| CutForge 桥:门禁 | `rs_gate.py M0–M7 --json`(透传 cutforge 侧门禁退出码;**范围以 gate.py 注册表为准**,无里程碑参数报错) |

全部脚本支持 `--json` 协议输出(`{"ok","code","message","data"}`),便于 Agent 消费。

## 🤖 Agent 治理(省 Token,也省踩坑)

| 机制 | 一句话 |
|---|---|
| [capabilities.json](skills/cutflow/capabilities.json) | 能力目录:先查它,不读源码;由 argparse 自动生成,手改会被测试打回 |
| 手册命令对拍门禁 | SKILL/rules 里每条 `rs_*` 命令都能被 argparse 接受,漂移即红(`tests/check_manual_cmds.py`) |
| Hard Rule 25 | 禁止为一次性任务现写剪辑逻辑脚本;可复用的必须升格为官方 `rs_*` 子命令 |
| `rs_verify` 分级验证 | L0 机械自检每次必跑;L1 语义目测首次/画面变更(`--auto` 时降级为抽帧留证);L2 验收归用户 |
| 上下文预算 | 指标与趋势记录见 [docs/context-budget.md](docs/context-budget.md) |

Agent 编排总控见 [skills/cutflow/SKILL.md](skills/cutflow/SKILL.md);领域术语见 [CONTEXT.md](CONTEXT.md);videoType 剪辑手册见 `skills/cutflow/rules/video-types/`;平台预设见 `skills/cutflow/rules/platforms.md`。

## 📁 目录结构

```
CutFlow/
├── skills/cutflow/
│   ├── SKILL.md               # Agent 入口:分工表 + 铁律 + 命令速查(270 行体量锁)
│   ├── capabilities.json      # 能力目录(自动生成,禁手改)
│   ├── CONTEXT.md             # 领域术语表
│   ├── rules/                 # 按需加载的规则分册(intake/subtitles/roughcut/jianying/
│   │                          #   incremental/cover/platforms/artboard/video-types/*)
│   ├── templates/             # styles/registry.json 风格注册表 + platforms.json 平台预设
│   └── scripts/               # rs_* 官方子命令(S0–S11 机械臂 + 四桥 + 意图编译)
├── tests/                     # 全量测试(含手册命令对拍门禁 check_manual_cmds.py)
├── tools/                     # fetch_ffmpeg / fetch_deps / fun_asr / CutFlowConfigEditor
├── docs/
│   ├── adr/                   # 架构决策记录(0001–0044)
│   ├── CHANGELOG.md           # 版本台账
│   ├── BACKLOG.md             # 欠账台账(条条带证据)
│   ├── ITERATION-GUIDE-v0.11.md  # 剪辑水平迭代总纲(权威数值出处索引)
│   └── context-budget.md      # 上下文预算指标与基线
└── config.example.json        # 复制为 config.json(已被 gitignore)
```

## 🙏 致谢

[FunASR](https://github.com/modelscope/FunASR)(MIT,自带 ASR 的模型与推理层来源,见 `tools/asr_vendor/NOTICE.md`)、[pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)(MIT,vendored)、[auto-editor](https://github.com/WyattBlue/auto-editor)(公共领域;margin/smooth 防碎切语义参考)。音效:[Mixkit 免费许可](https://mixkit.free-license/)。字幕规范参考 [Netflix Timed Text Style Guide](https://partnerhelp.netflixstudios.com/) 与 [BBC Subtitle Guidelines](https://www.bbc.co.uk/accessibility/forproducts/guides/subtitles/)。

[jianying-headless](https://github.com/mcncarl/jianying-headless):其许可为个人学习/非商用——本项目**只学习其方法与能力**(计划编译分层、保护区、帧数门禁、诚实验收文档的写法),全部自行实现、自定字段,未复制任何代码或专有结构(ADR-0044;自查口径见 [诚实验收说明](skills/cutflow/rules/jianying-verification.md))。

## 📄 License

MIT
