# CutFlow — AI 视频制作技能库

**素材进,成片出。** 让 AI Agent 端到端制作视频:接收口播视频 / 文案 / 剧本分镜 / 图片,经
「转写对齐 → 粗剪 → 合成 → 剪辑 → 字幕 → 烧录 → 体检自检 → 封面文案」产出成片与平台物料,
并同步生成**可在剪映 5.9 继续精修的草稿工程**。

- **视频类型**:`talking-head`(纯口播绿幕)/ `talking-head+animation`(口播+动画卡)/ `pure-animation`(纯动画),每类有专属管线手册;画幅 9:16 / 3:4 / 16:9,**平台字幕预设**(抖音 / 视频号 / 小红书 / B站),多 Logo × 多比例**变体矩阵**
- **自带语音识别**(FunASR 字级对齐),不依赖任何外部服务/在线 API;脚本纯 Python 标准库 + FFmpeg,零第三方依赖
- **不止剪短,还剪好看**:粗剪去口误之外,v0.11 内置成片体检(QC 闸)、转场三级语法、punch-in 变焦掩饰、文本化删改稿——数值口径全部标注权威出处(EBU R128 / Netflix 规范 / 注意力窗口研究)

---

## 核心机制(每一件都解决一个真实事故)

| 机制 | 解决的问题 | 一句话 |
|---|---|---|
| **Wordline 字级对齐** | 字幕/声音/画面对不上 | 全片时间只有一个真相源 `wordline.json`,任何模块不得自行算时间,禁止按字数比例插值 |
| **CutList 粗剪决策表** | 口误/重录/废片段剪不掉 | 七类检测器(静音/口头禅/口吃/重录/整段重来/死空气/迟疑)给出每刀 `reason + conf + guard 三重校验`,宁可漏删不可错删 |
| **文本化删改稿**(v0.11) | 审查粗剪要逐条听 3 秒音频 | 每刀带文本上下文,报告产出全文删改稿(~~删~~/**待审**/保留)——机器粗剪,人**读稿**精修(对齐 Descript / Premiere 文本化编辑) |
| **转场三级语法**(v0.11) | 所有切点一个面孔 | 源间隙 <1s=跳切→**1 帧软切**(吃掉姿态跳变与爆音);≥1s=真切换→**300ms 溶解**(Reisz 语法:dissolve 表达"时间过去了") |
| **punch-in 变焦**(v0.11) | jump cut 观感生硬 | 真剪辑点后自动 1.4x 变焦交替构图(密度 15s/≤3 处;Hitchcock 规则:紧构图只给重点) |
| **成片体检 QC**(v0.11) | 黑帧/冻结/音画漂移/响度超标要人眼看 | `rs_sync --qc` 机械闸:黑帧≥0.3s / 冻结≥2.5s / VFR 混帧 / 响度(I -14 LUFS ±1、TP -1 dBTP,双 pass loudnorm)直接 FAIL;绿幕段 **matte 探针**在渲染期抓"整片人物透明"类事故 |
| **阶段缓存 + 一键重建** | 改一个字要重跑 20 分钟 | 缓存键含脚本 hash,`rs_run --status/--only/--from` 只重跑真变了的部分;每文件夹一个 `rebuild.py`,手改后级联重建 |
| **分级验证** | 每次都全检太费时间 | L0 机械自检每次秒级;L1 语义目测仅首次或画面变更;L2 验收归用户。输出必带 `verifyLevel` |
| **DP 字幕断句** | "滚滚长/江东逝水"式断错 | 约束最优 DP + 词边界硬约束(两字词不跨卡)+ 禁切表,竖屏 10–12 字/卡,CPS ≤9,Netflix 时长规范 |
| **品牌变体矩阵** | 一条视频要多平台多 Logo 版 | Logo 真实尺寸(alpha 内容包围盒,透明 padding 不算)+ 6 锚点排版,自动避开字幕带;`Logo × 比例` 共享上游缓存 |

设计决策见 [docs/adr/](docs/adr/)(0001–0026);剪辑水平迭代总纲见
[docs/ITERATION-GUIDE-v0.11.md](docs/ITERATION-GUIDE-v0.11.md)(权威数值出处索引也在那里);
实测战报见 `skills/cutflow/rules/compose.md`。

---

## 新手五步上手(每步一条命令)

### 第 1 步:部署 FFmpeg(约 100MB,自动下载)

```powershell
python tools\fetch_ffmpeg.py
```

### 第 2 步:图形界面填配置(不用手写 JSON!)

双击 `tools\CutFlowConfigEditor.exe`(或从 [Release](https://github.com/GreenChennai/CutFlow/releases) 下载),像填表单一样填好每一项——每项都有中文说明。参数全表见本页末尾。

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

对 AI 说人话:**"帮我用这段口播视频做一条抖音竖版,加字幕"**。Agent 走 S0→S11 流水线,交付:
成片 mp4、`subtitles.ass`+`master.srt`、封面、标题简介 Tag、可编辑的剪映 5.9 草稿、`sync_report.md`(对齐断言 + QC 体检 + 音频内容闸)。

---

## 管线总览(S0–S11)

```
S0 素材 ─► S1 转写+字级对齐 ─► S2 粗剪 ─► S3 基础合成 ─► S4 动画信息 ─► S5 品牌(Logo 变体)
  │          │wordline.json      │cutlist     │base          │composed     │branded/
  └──────────┴───────────────────┴────────────┴──────────────┴─────────────┤
                                   下游全部共享 map(t_src)→t_final          ▼
   S11 交付 ◄─ S10 封面+文案 ◄─ S9 体检+断言 ◄─ S8 烧录导出 ◄─ S7 字幕 ◄─ S6 音效
```

- **S8 烧录**只用现有 `subtitles.ass` 重烧,**绝不重新生成字幕**——手改字幕不会被覆盖。
- **S9 是三重机械闸**:①字幕↔Wordline 对齐断言(中位 ≤40ms);②成片音频内容闸(ASR 对账:片头句唯一/相似度 ≥0.90/无重复段);③QC 体检(黑帧/冻结/VFR/响度)。
- 每个绿幕段渲染时跑 **matte 探针**(alpha 前景占比 <1% 或 >70% 告警)——"全片人物透明"这类事故在渲染期被抓,而不是交付后。

### 手改之后怎么重建(不需要 AI 从头跑)

每个阶段文件夹都有 `rebuild.py`(`rs_run.py --init` 生成),固定四步:**备份 → 校验 → 级联 → 导出+自检**:

| 你改了什么 | 运行哪个 |
|---|---|
| 字幕 `06_output/subtitles.ass` | `python 06_output\rebuild.py` |
| IR / Wordline `05_ir/`(手注过 chroma/背景) | ⚠ 改跑 `python 06_output\rebuild.py`(S8 只重烧,不碰 IR) |
| 粗剪决策 `04_cut/cutlist*.json` | `python 04_cut\rebuild.py` |
| artboard 卡片 `03_assets/artboard/` | `python 03_assets\artboard\rebuild.py` |
| 拿不准 | `python rebuild.py`(工程根,全量) |

校验不过会**停住并指出具体行**;跑砸了 `rs_run.py --rollback` 一键还原。

---

## 剪辑水平:口径与出处

CutFlow 的"剪得好"不是玄学,每条数值都有出处(完整索引见 [ITERATION-GUIDE §10](docs/ITERATION-GUIDE-v0.11.md)):

| 口径 | 数值 | 出处 |
|---|---|---|
| 总线响度 | -14 LUFS ±1 / TP ≤ -1 dBTP(双 pass loudnorm) | EBU R128 口径;-14 为 Spotify/YouTube 归一化对齐值(非平台强制) |
| 字幕 | ≤16 字/行、≤2 行、成人 ≤9 CPS、0.83–7s、间距 ≥2 帧 | Netflix 简体中文 Timed Text 规范 |
| 视觉节拍 | 卡时长 1.5–3.5s;镜头/视觉变化 2–3s 窗口 | Cutting(康奈尔)好莱坞 75 年镜头时长实证 |
| A/V 同步 | 超前 ≤40ms / 滞后 ≤60ms 告警 | EBU R37(ATSC IS-191 更严者作参考) |
| 粗剪防护 | margin 前 150/后 300ms;碎刀 <120ms 放弃;碎片 <100ms 并刀 | auto-editor `--margin/--smooth` 精神 |
| punch-in | 1.4x 起步、密度 15s/≤3 处 | Frame.io/r.editors 共识 + Hitchcock 规则(经验值,已标注) |

找不到权威出处的数值一律标注「经验值」——不把行业传说包装成规范。

---

## Config 参数全表

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

---

## 常用命令速查

| 环节 | 命令 |
|---|---|
| 阶段状态 / 增量 | `rs_run.py --status` / `--from S3` / `--only S7` / `--explain S7` |
| 一键重建 | `rs_run.py --init`(生成 rebuild.py)/ `--rollback` |
| 分级自检 | `rs_verify.py <工程>` / `--mark-first --result pass` |
| 转写 | `python tools\fun_asr.py <媒体>` / `--probe` |
| 对齐(S1) | `rs_align.py build --media <素材> --out 05_ir\wordline.json`(专名错 → `--terms-file 00_brief\terms.txt` 热词重跑) |
| wordline 平滑 | `rs_align.py smooth 05_ir\wordline.json --out 05_ir\wordline.final.json`(标点零宽+重叠钳制) |
| 粗剪(S2) | `rs_cut.py 05_ir\wordline.json --detect all --media 源 --out 04_cut` → `--apply` |
| 按文本裁片 | `rs_cut.py 05_ir\wordline.json --from-text "只想要的引文"`(引文外走 guard) |
| CutList→IR(S3) | `rs_ir.py build --from-cutlist 04_cut\cutlist.applied.json --slug X --out 05_ir\project.json`(`--punch-in-auto` 启用变焦掩饰) |
| 纯动画组装(I7) | `rs_ir.py build --from-cards 03_assets\artboard\manifest.json --anchors 00_brief\cards.json --wordline 05_ir\wordline.json --voice 03_assets\vo\voice.wav --slug X --ratio 16x9 --out 05_ir\project.json`(锚点分组/停顿中点/冻结帧补长) |
| 字幕(S7) | `rs_subtitle.py --from-wordline 05_ir\wordline.final.json --platform douyin --out 06_output` |
| 对齐自检+体检(S9) | `rs_sync.py --wordline 05_ir\wordline.final.json --ass 06_output\subtitles.ass --video 成片.mp4 --audio-content --qc` |
| 封面文案(S10) | `rs_meta.py --wordline ... --brief 00_brief\brief.md --platform douyin,bili` |
| 剪映草稿 | `rs_jy_draft.py 05_ir\project.json --name <名>` |
| 清理 | `rs_cleanup.py <工程> [--apply]` |

全部脚本支持 `--json` 协议输出(`{"ok","code","message","data"}`),便于 Agent 消费。

## 架构(一图)

```
素材 → S1 自带ASR转写+字级对齐(Wordline 唯一真相源) → S2 粗剪(七类检测器+guard+删改稿)
     → S3-S6 合成(IR:转场三级语法/punch-in/chroma绿幕/TTS音色卡/artboard卡/Logo变体/音效)
     → 剪辑双出:FFmpeg 七步管线(段缓存/matte探针/双pass响度/字幕最后叠)
                  + 剪映 5.9 明文草稿(可编辑工程)
     → S7 字幕(DP断句+词边界+CPS) → S8 烧录 → S9 对齐断言+音频闸+QC体检 → S10 封面文案 → S11 交付
     全程:声明式阶段缓存 + rebuild.py 手改级联重建 + 分级验证(L0/L1/L2)
```

领域术语见 [CONTEXT.md](CONTEXT.md);videoType 剪辑手册见 `skills/cutflow/rules/video-types/`;平台预设见 `skills/cutflow/rules/platforms.md`;Agent 编排总控见 [skills/cutflow/SKILL.md](skills/cutflow/SKILL.md)。

## 致谢

[FunASR](https://github.com/modelscope/FunASR)(MIT,自带 ASR 的模型与推理层来源,见 `tools/asr_vendor/NOTICE.md`)、[pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)(MIT)、[jianying-editor-skill](https://github.com/luoluoluo22/jianying-editor-skill)、[kbcut](https://github.com/starboom/kbcut)、[video-use](https://github.com/browser-use/video-use)、[lingji-cut](https://github.com/yoqu/lingji-cut)、[Generative-Media-Skills](https://github.com/SamurAIGPT/Generative-Media-Skills)、[hyperframes](https://github.com/heygen-com/hyperframes)、[auto-editor](https://github.com/WyattBlue/auto-editor)(公共领域;margin/smooth 防碎切语义参考)。音效:[Mixkit 免费许可](https://mixkit.free-license/)。字幕规范参考 [Netflix Timed Text Style Guide](https://partnerhelp.netflixstudios.com/) 与 [BBC Subtitle Guidelines](https://www.bbc.co.uk/accessibility/forproducts/guides/subtitles/)。

## License

MIT
