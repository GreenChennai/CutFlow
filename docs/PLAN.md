# CutFlow — AI 视频制作技能库 · 方案笔记(v2)

> 状态:**方案 v2 完成,等待用户"开工"指令**。
> 本笔记由 grill-me/grilling 自查流程产出(用户全权委托,未向用户提问,所有决策由 Agent 自查并记录理由)。
> 开工后严格按本笔记执行;与笔记冲突时以现场验证结果为准并回写本笔记。
> 最终交付:GitHub 上传(GreenChennai/CutFlow)→ 进入自迭代循环(每轮自动 push)→ 保存资料 → **电脑保持运行,不关机**(用户凌晨 4:00 有其他定时任务)。

**修订记录**
- v1(2026-09-06):初版,名 ReelSmith,剪映走纯 GUI 辅助,TTS 用 Jimi。
- v2(2026-09-07,按用户四条修改):
  1. 剪映 5.9(E:\Jianying\5.9JianyingPro,5.9.0.11632)已备好 → 剪映路线升级为"**明文草稿直写 + GUI 自动导出**"(5.9 草稿为明文,schema 已被 pyJianYingDraft/jianying-editor-skill 验证);11.3 保留为用户日常版本,不用于自动化。
  2. 更名 **CutFlow**(用户要求不与既有项目命名风格一致)。
  3. Jimi 暂不可用 → TTS 测试/演示音色改用 **koubo-test**(即 EchoSmith\models\voices\口播声线\card.json,权重 koubo-test-e1.ckpt / koubo-test_e1_s33.pth,已核实);音色做成 brief/config 可配,Jimi 可用后一键切换。
  4. 新增 **M8 自迭代循环**:初始交付后持续"审计→改→推",每轮自动 push GitHub,不询问;用户可用 git tag/revert 回滚。
- v2.1(2026-09-07):完成任务后**不关机**,电脑保持运行待机(用户凌晨 4:00 有其他定时任务要跑);所有流程中的关机步骤移除,改为"停服务 + 保存资料 + 保持运行"。

---

## 0. 一句话定位

**CutFlow**:一个让 AI Agent 端到端制作视频的技能库——接收口播视频/文案/剧本分镜/图片等任意素材,经"感知→合成→剪辑→自评"流水线直接产出成片,并可同时交付**可编辑的剪映 5.9 工程**;AI 生视频环节只产出提示词(首帧图提示词 + 首帧标注式 5–10s 图生视频提示词),由用户拿去外部工具生成素材回流。

---

## 1. Grilling 自查记录(设计树决策表)

| # | 分支 | 问题 | 决策 | 理由 |
|---|------|------|------|------|
| Q1 | 定位 | 单技能还是技能库? | 单仓库 **两个技能**:`cutflow`(总控+剪辑管线)+ `cutflow-prompt`(AI 生视频提示词,可独立触发) | 剪辑与提示词生成的触发场景常独立;hyperframes/jianying-editor-skill 证明"入口路由 + 域规则文件"的单技能形态最稳 |
| Q2 | 剪辑引擎 | 剪映怎么用? | **FFmpeg 直出为成片主线**;**剪映 5.9 双通道**:①IR → 5.9 明文草稿(draft_info.json)编译器,交付可编辑工程;②computer-use GUI 自动导出(5.9 弹窗少、控件锚点已被 jianying-editor-skill 验证)。11.3 草稿加密且为用户日常版,不碰不自动化 | 本机已备 5.9;draft 直写能力 = 用户拿到手就能继续人工精修,价值远超纯 GUI;FFmpeg 保底确定性出片 |
| Q3 | 渲染栈 | 引入 Remotion / HyperFrames 吗? | **v1 不引入**,roadmap。动画能力由 artboard(HTML→GIF/MP4,≤15s)+ FFmpeg 覆盖 | Remotion 许可 ≥4 人公司收费;HyperFrames 栈重;artboard 已覆盖 80% 动效需求 |
| Q4 | 中间表示 | Agent 怎么描述一部视频? | 自研 **毫秒级 IR `project.json`**:tracks[video/audio/text] + clips{src,startMs,durationMs,sourceInMs,position,scale,motion 枚举…} + subtitle 全局样式 + markers;封闭枚举;同一 IR 编译两个后端:`rs_render.py`(FFmpeg 直出)与 `rs_jy_draft.py`(5.9 草稿) | 毫秒对 LLM 友好;封闭枚举可校验;一份时间线两种交付,后端可插拔 |
| Q5 | ASR | FunASR 服务未运行怎么办? | 三级预案:①python 直启 asr_server(纯标准库)→②computer-use 操作打包版 GUI 点"服务模式"→③上报不阻塞;`POST /v1/audio/transcriptions`(multipart: file/model/structured=1)句级时间戳 | asr_server 由 GUI 启动,直启未验证;句级时间戳够字幕对轴 |
| Q6 | ASR 质量 | 转写错字/口误? | **硬规则:ASR/OCR 输出必须经 Agent 校对**,产出 transcript_corrected.md,保留 raw 对照 | 用户明确要求 |
| Q7 | TTS | 纯文案怎么配音?用什么音色? | 直调 GPT-SoVITS api_v2(127.0.0.1:9885),音色卡驱动(读 card.json 自动装配);**测试/演示默认 koubo-test(口播声线卡)**;Jimi 待可用后在 config/brief 切换;预热+逐句落盘+断点续传;32k mono→48k | 用户指定 koubo-test 代替 Jimi 测试;音色卡机制天然支持多音色切换 |
| Q8 | AI 生视频边界 | 自己调生图/生视频 API 吗? | **不调**。只产出:①首帧(可选尾帧)图片提示词;②基于首帧、标注画面角色的 5–10s i2v 提示词。方法论照搬 make-prompt-seedance2 | 用户明确"只生成提示词" |
| Q9 | 绿幕 | 合成能力? | FFmpeg `chromakey`+`despill`;9:16 重构图锚点 = 抽帧网格 + Agent 目测选点(全权模式自选并记录) | JJAV2815 是绿幕,正好验收 |
| Q10 | 字幕 | 样式与断行? | ASS 烧录 + 样式模板;中文断行评分算法(移植 kbcut breakLine);平台安全区(9:16 底部 25%/顶部 12%);关键词高亮 v1.5 | video-use 铁律:字幕最后叠 |
| Q11 | 音频 | BGM 与混音? | 用户提供或 CC0;人声 loudnorm → BGM sidechaincompress 闪避 → 总线 loudnorm -14 LUFS / -1 dBTP | 平台通行目标 |
| Q12 | 比例 | 出片规格? | 1080×1920 与 1920×1080,单出或双出;全片统一帧率 | 用户明确 |
| Q13 | Intake | 开剪前问什么? | 内置轻量问卷(rules/intake.md):素材/平台/比例/时长/风格/字幕/声音(音色可指定 koubo-test)/BGM/片头片尾/绿幕背景;落 **brief.md 单一契约**;automation(全权)/companion(闸门确认)双模式 | hyperframes BRIEF.md 精神 |
| Q14 | 工作目录 | 工程放哪? | `E:\平日资料\视频工程\<slug>\` 固定结构 + project.md 会话记忆 | 素材与工程分离 |
| Q15 | 安装 | 技能装哪? | 仓库为事实源,用户级 junction(C:\Users\Velon\.agents\skills\cutflow、cutflow-prompt);装前查同名遮蔽;装后更新 E:\GCissue\Agent Skill 速查手册 | 用户既定约定 |
| Q16 | 仓库 | 公开还是私有? | **Public**;config.json gitignore,example 用占位;素材/模型/测试视频不入库 | 展示库,无密钥,可随时转私有 |
| Q17 | 验收 | 怎么算做完? | ①JJAV2815 → ≥2 风格 × 双比例真渲染过 judge;②纯文案 → koubo-test TTS → 成片端到端;③剪映 5.9 草稿能在 5.9 中打开且可导出;④artboard 片头/封面;⑤提示词样例;⑥doctor 全绿 | 5.9 草稿可打开是 Q2 的直接验收 |
| Q18 | 收尾 | 最后做什么? | 停服务 → push → 更新手册/记忆 → 进入 M8;**全程不关机**(v2.1) | 用户最终步骤 + v2.1 修订:凌晨 4:00 有定时任务,须保持运行 |
| Q19 | **v2** | 更名 | **CutFlow**(repo GreenChennai/CutFlow),避免与 EchoSmith 系命名风格一致 | 用户要求 |
| Q20 | **v2** | 剪映 5.9 已备 | 见 Q2;剪映自动化对象 = 5.9(禁自动更新) | 用户备好 5.9 |
| Q21 | **v2** | Jimi 不可用 | 测试/演示音色 = **koubo-test**(口播声线卡);架构上音色全卡驱动 | 用户指定 |
| Q22 | **v2** | 交付后怎么办? | **M8 自迭代循环**:审计(缺陷/优化/弱项)→ 实现 → 验证 → commit + tag `iter-NN` + push,每轮必推,不询问;会话退出条件见 §11 M8;用户可随时 `git tag`/`revert` 回滚 | 用户明确授权持续自动迭代 |

---

## 2. 调研摘要

### 2.1 本机事实(已核实)

| 依赖 | 状态 | 关键事实 |
|------|------|----------|
| 剪映 5.9(自动化目标) | ✅ 已备 | E:\Jianying\5.9JianyingPro(5.9.0.11632 + JianyingPro.exe);**草稿明文 JSON,pyJianYingDraft 体系验证可写**;使用须防自动更新(v2 新增注意事项);草稿目录开工时首次启动确认(默认 C:\Users\Velon\AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft\) |
| 剪映 11.3(用户日常) | ✅ 在装 | E:\Jianying\JianyingPro(11.3.0.14362);**6.0+ 草稿加密(protocol 183),不做任何自动化,不碰草稿** |
| FunASR 服务 | ⚠️ 未运行 | MomentShift(src\momentshift\core\asr_server.py,纯标准库):`POST /v1/audio/transcriptions` multipart(file, model=paraformer-large, structured=1)→ `{"text":"[0.6s] 说话人0: …"}` 句级时间戳+说话人;`GET /health`;由 GUI"服务模式"启动;打包版 dist\MomentShift\MomentShift.exe;模型已就位(tools\funasr\) |
| TTS(GPT-SoVITS) | ⚠️ 未运行 | 引擎 EchoSmith\models\GPT-SoVITS\api_v2.py(`runtime\python.exe api_v2.py -a 127.0.0.1 -p 9885`);音色卡在 EchoSmith\models\voices\{口播声线,冒烟声线,全链路声线,Jimi}\card.json;**koubo-test = 口播声线卡**(koubo-test-e1.ckpt / koubo-test_e1_s33.pth,ref=datasets\koubo-test\clips\clip_002.wav+prompt_text);`POST /tts` JSON{text, text_lang:"zh", ref_audio_path, prompt_text, text_split_method:"cut5", speed_factor} → 32k mono wav;ZLUDA 首句可达 10min,之后 19–29s/句 |
| OCR | ✅ | E:\平日资料\GitHub\OCR\dist\OCR.exe:`OCR.exe 图片.png [-o out.txt] [--box]` |
| VQA | ✅ | E:\平日资料\GitHub\VQA\venv\Scripts\python.exe qora_cli.py 图片 --prompt "…" [-o out.txt] |
| artboard | ✅ | E:\平日资料\GitHub\.agents\skills\artboard:HTML/CSS→PNG/GIF/MP4/PDF;export.py 固定高度必须 --height;动图 ≤15s/fps∈{10,20,25,50};config.json ffmpeg 字段空(部署后回填) |
| FFmpeg | ❌ 待部署 | 7z:C:\Users\Velon\Downloads\ffmpeg-2026-07-30-git-2ae2413488-full_build.7z;系统无 ffmpeg;7-Zip 26.00 在 "C:\Program Files\7-Zip\7z.exe" |
| Node / git / gh | ✅ | node v24.19.0;git 2.55.0;gh 已登录 **GreenChennai** |
| 测试素材 | ✅ | E:\平日资料\视频素材-口播\JJAV2815.MP4(286MB 绿幕口播) |

### 2.2 参考项目要点(已克隆至 E:\平日资料\GitHub\其他开源项目参考\)

| 项目 | 形态 | 核心机制 | 抄什么 |
|------|------|----------|--------|
| **jianying-editor-skill** | Agent Skill + vendored pyJianYingDraft | **5.9 明文草稿生成 + uiautomation 自动导出**(与我们 5.9 场景完全同构,升级为首要参考) | ①草稿 schema 权威(track/segment/material、extra_material_refs、文本 content 富文本 JSON、transform 半画布单位、check_flag 位掩码、转场挂前一片段、关键帧 curveType);②CLI 统一 JSON 协议{ok,code,message,data};③desc_matcher 控件锚点(GetPropertyValue 30159 full description,如 MainWindowTitleBarExportBtn)+ app_status 状态机;④特效名 CSV+同义词容错;⑤6.0+ 弹窗干扰论断(反向确认 5.9 是自动化甜点) |
| **kbcut** | Agent Skill(零依赖) | FFmpeg 段级重剪 + Whisper + HTML 模板 | ①render_recut.py 色彩透传+编码自选+8ms afade;②captions.cjs 中文断行评分;③frame.md 风格即数据;④踩坑→硬校验维护模式 |
| **lingji-cut** | Electron 工作台 | 毫秒 IR → Remotion | ①types.ts IR(毫秒/像素/封闭枚举/全局字幕样式+高亮/autoResegment);②file-first 契约(edit-lock/heartbeat/TTL/edit-result 回传);③MCP+CLI+Agent 三通道 |
| **video-use** | 单技能 + 6 helpers | "LLM 读视频不看视频",EDL + FFmpeg | ①EDL 契约;②render.py 铁律(HDR tonemap/统一帧率/逐段提取→concat/两遍 loudnorm -14 LUFS/安全区);③Hard Rules 与品味分离;④self-eval ≤3 轮;⑤project.md 会话记忆 |
| **Generative-Media-Skills** | Core/Library 生态 | muapi 原语 + 60 配方 | ①seedance-2:Director Brief 六段式、@引用角色分配表、时间分段+单拍规则;②SKILL.md 模板(Inputs 表/Done Criteria/Failure Modes/Trigger Keywords);③原语层与配方层分离 |
| **make-prompt-seedance2** | 提示词工程库 | Seedance 2.0 方法论 | ①四段公式【风格/时间轴/声音/参考】+ 10s 五段节奏;②`@图片1 作为首帧`/`@图片2 作为尾帧`/`@图片3 角色形象参考`(图≤9/视频≤3/音频≤3,不支持写实真人脸上传);③写意图不写细节+单镜头单动作;④真人感九维度;⑤7 段交付包装+5% 微调法+废片排查表 |
| **hyperframes** | HeyGen 总控台(Apache 2.0) | HTML/GSAP→Chrome 逐帧+FFmpeg | ①BRIEF.md 单一契约 + run-shape(automation/companion);②STORYBOARD `## Frame N` 分发块;③双闸门 + 词表引用制 |
| Remotion(仅调研) | React 视频引擎 | Chromium+FFmpeg | v1 不引入(许可/栈重),roadmap |

---

## 3. 总体架构

```
                          ┌─────────────────────────────────────────────┐
                          │  cutflow SKILL.md(总控:路由/编排/闸门)       │
                          │  intake 问卷 → brief.md(单一契约)            │
                          └──────┬──────────────────────────────────────┘
        素材(口播视频/文案/       │  感知             合成              剪辑
        剧本分镜/图片)────────────┤
                                 ▼                ▼                ▼
   ┌────────────────────────────────────────────────────────────────────────┐
   │ 02_sensed           03_assets                       05_ir/project.json │
   │ transcript_corrected  tts_*.wav(koubo-test/音色卡)   (毫秒 IR,封闭枚举)│
   │ ocr/vqa/frames        artboard 片头/片尾/封面/小动画        │            │
   │                       ai_prompts(首帧+i2v 提示词)     ┌─────┴──────┐     │
   │                                                     ▼            ▼     │
   │                              rs_render(FFmpeg 直出)  rs_jy_draft(5.9 草稿)│
   │                              统一帧率→逐段提取→concat  draft_info.json   │
   │                              →绿幕→overlay→混音       可编辑工程交付     │
   │                              (ducking+loudnorm)       +GUI 自动导出     │
   │                              →ASS 字幕最后叠→双比例                     │
   │                              →自评(ffprobe 断言+抽帧目测 ≤3 轮)         │
   └────────────────────────────────────────────────────────────────────────┘
                                 06_output/final_9x16.mp4 | final_16x9.mp4 | master.srt
                                           | cover.png | jianying_draft/(5.9 工程)
```

分层:**scripts/ 原语层**(纯机械,统一 JSON 输出)+ **rules/ 配方层**(领域知识)+ **templates/**(brief/IR/风格)。一个 IR,两个后端(FFmpeg 成片 / 剪映 5.9 工程)。

### 渲染铁律(Hard Rules)

1. 混帧率素材先统一目标 fps(默认 30)再 concat;
2. 逐段提取(段间 8ms afade)→ 无损 concat → 后期滤镜;
3. 色彩元数据透传(ffprobe 读 primaries/transfer/space/range 原样回写,10bit→hevc+p010le);
4. 绿幕先抠像,字幕永远最后叠;
5. 人声 loudnorm → BGM sidechaincompress → 总线 loudnorm -14 LUFS / -1 dBTP;
6. 断行过评分算法;9:16 安全区:底 25%/顶 12%;
7. TTS 长文先全量合成落盘再进时间线;
8. 剪映运行期间禁止触碰其草稿目录;只对 **5.9** 做自动化,11.3 不碰;5.9 安装禁止自动更新;
9. 渲染产物过 rs_doctor 断言 + 抽帧目测,≤3 轮,仍败上报;
10. 不可逆构图决策先出对比图;全权模式 Agent 自选并记录理由到 project.md。

---

## 4. 输入与 Intake

### 4.1 素材类型与路由

| 用户给的 | 感知动作 | 产出 |
|----------|----------|------|
| 口播视频(普通/绿幕) | FunASR 转写(structured=1)+ 关键帧 OCR 交叉校对 | transcript_corrected.md(句级时间戳) |
| 纯文案脚本 | Agent 分句 + TTS(音色按 brief,默认 koubo-test) | tts_*.wav + 实长时间戳 |
| 完整剧本/分镜表 | Agent 解析为分镜列表 | storyboard.md(`## Shot N` 块) |
| 图片 | OCR.exe + qora_cli | ocr_*/vqa_* 文本 |
| AI 生成素材需求 | cutflow-prompt | 首帧图提示词 + 5–10s i2v 提示词(可选尾帧) |

### 4.2 Intake 问卷(rules/intake.md,自适应)

必问(缺什么问什么):用途/平台?比例?时长?风格?字幕(开关/样式)?声音(**原声/指定音色(默认 koubo-test)/无声+BGM**)?片头片尾?绿幕背景?BGM?
不问:已有答案的;automation 模式按 brief 默认值自选并记录。
产出 `00_brief/brief.md` 单一契约,此后一切决策只查 brief。

---

## 5. 感知层(scripts/rs_asr.py、rs_sense.py)

- `rs_asr.py <媒体>`:探活 /health → 未运行打印启动指引(三级预案)→ POST(structured=1)→ 解析 `[12.3s] 说话人0: …` → `02_sensed/transcript_raw.json`。
- 校对(Agent 步骤):逐句修错字/口误/标点,专有名词可在 brief 声明;产出 transcript_corrected.md。
- `rs_sense.py <图片>`:并行 OCR + VQA,合并 JSON{text, description}。
- `rs_frames.py <视频> --every 10s --grid`:抽帧网格图(内容理解/绿幕锚点/自评共用)。

## 6. 生成层

### 6.1 TTS(scripts/rs_tts.py,音色卡驱动)

- 引擎管理:探 9885 → 未运行则后台拉起 api_v2.py,轮询就绪,首发预热短句(吃掉 10min 冷启动)。
- 音色装配:config.json voices_dir 指向 EchoSmith\models\voices;按 brief/config 选卡(默认 **koubo-test = 口播声线**;Jimi 卡已存在但权重暂不可用,标注 disabled,可用后取消标注即可切换);读 card.json → ref/prompt_text/aux。
- 合成:按【情感】前缀分句,逐句 POST /tts(cut5)落盘 + manifest 断点续传;超时 1800s。
- 后处理:32k mono → FFmpeg 重采样 48k,响度归一留到渲染总线。

### 6.2 artboard 桥(rules/artboard.md)

- 用途:片头/片尾板(静态或 ≤15s MP4 动画)、封面、信息卡、章节卡。
- 调用按 artboard SKILL.md(preflight→scaffold→export;9:16 用 `--width 1080 --height 1920`)。
- 开工动作:ffmpeg 部署后**回填 artboard config.json ffmpeg 路径**。

### 6.3 cutflow-prompt 技能

- 输入:画面构想/剧本分镜/图片;输出七段包装(路线理由/图片理解摘要/理解确认/完整提示词代码块/素材建议@顺序/使用提示/迭代建议)。
- 产两种提示词:①首帧图提示词(真人感九维度词库;可选尾帧=最终状态,首尾帧"生长/对比"写法);②i2v 提示词(四段公式 + 5–10s 时间分段 + `@图片1 作为首帧` + 角色形象标注照图写实 + 单镜头单动作 + 显式音频指令)。
- 硬约束:主体描述与首帧图照实一致;负面提示词自然语言结尾;知名角色写 Figure N 不写名字。

---

## 7. 剪辑层

### 7.1 项目 IR(templates/project.schema.json)

```jsonc
{
  "version": 1, "slug": "demo", "fps": 30,
  "canvas": {"width": 1080, "height": 1920},
  "tracks": [
    {"kind": "video", "clips": [{
        "src": "01_materials/JJAV2815.MP4", "startMs": 0, "durationMs": 12000,
        "sourceInMs": 3000, "speed": 1.0, "volume": 1.0,
        "chroma": {"color": "green", "similarity": 0.12, "blend": 0.08, "despill": true},
        "position": {"x": 0.5, "y": 0.5}, "scale": 1.0,
        "motion": {"in": "fadeIn", "inMs": 400},
        "reframe": {"anchorY": 0.35}
    }]},
    {"kind": "audio", "clips": [{"src": "03_assets/tts_001.wav", "startMs": 0, "role": "voice"}]},
    {"kind": "text", "style": "subtitle_bold_center", "highlight": {"words": ["免费"], "bg": "#FFD400"}}
  ],
  "bgm": {"src": "...", "gainDb": -18, "ducking": true},
  "markers": [{"ms": 0, "label": "HOOK"}, {"ms": 12000, "label": "正文"}]
}
```

- `rs_ir.py validate`:schema + 枚举 + 时间重叠检查;非法值回 `{ok:false, code, hint}`。

### 7.2 后端一:FFmpeg 渲染器(scripts/rs_render.py)

```
1. probe:ffprobe 全素材,统一 fps/像素格式决策(色彩透传)
2. segment:逐 clip 提取(裁切/reframe/chroma key/速度/音量,段间 8ms afade)
3. concat:无损拼接
4. compose:画中画/信息卡/片头片尾 overlay(xfade 枚举: fade/wipe/zoom)
5. mix:人声 loudnorm → BGM sidechaincompress → amix → 总线 loudnorm -14 LUFS/-1 dBTP
6. subtitle:ASS 烧录(最后一步)
7. encode:双比例输出(H.264 high, yuv420p, CRF19 + AAC 192k)
```

质量三档:final(CRF19)/ preview(CRF26 半分辨率)/ draft(片段)。

### 7.3 后端二:剪映 5.9 草稿(scripts/rs_jy_draft.py + rules/jianying.md)

- **草稿生成**:IR → 5.9 明文 `draft_info.json`(+draft_meta_info 同套)。实现选型:优先 vendor pyJianYingDraft(开工核对其许可证,MIT 类则直接用;否则按 references/draft-schema-5.9.md 自研轻量生成器,仅覆盖 IR 能力集:视频/音频/文本字幕/转场/淡入淡出/位置缩放关键帧子集)。
- 能力映射:canvas→canvas_config;clip 时长/裁切→target_timerange+source_timerange(微秒=毫秒×1000);chroma 在 5.9 无对应则跳过并标记"绿幕版请用 FFmpeg 直出";文本→text 段(content 富文本 JSON,样式映射 subtitle 模板);转场→前片段 add_transition。
- **草稿落位**:5.9 草稿目录(开工首次启动确认,默认 User Data\...\com.lveditor.draft);每次写入前确认 5.9 未运行(硬规则 8)。
- **GUI 自动导出(computer-use)**:启动 5.9 → 首页进入草稿 → 点导出 → 分辨率/帧率确认(控件锚点参考 desc_matcher:MainWindowTitleBarExportBtn/ExportSharpnessInput/ExportOkBtn,开工实测重建)→ 等待产物(默认超时 20min)→ 产物归位 06_output。5.9 弹窗少,该链路被 jianying-editor-skill 验证过;失败降级:草稿已交付,用户手动导出。
- **交付意义**:用户拿到 06_output 成片之外,还有一个能在 5.9 里继续精修的工程。

### 7.4 字幕(scripts/rs_subtitle.py)

- transcript_corrected → SRT + ASS;样式模板:口播大字(居中偏下/粗体/描边+底衬)、教程底部条(半透明背景)、白字黑边通用。
- 中文断行评分(标点+100/空格+90/句尾虚词+50/ASCII 切断−200/每填一字−4);9:16 单条 ≤16 字、16:9 ≤22 字,超长自动再切。
- TTS 稿时间戳 = 合成实长累加;口播稿 = ASR 句级时间戳。

### 7.5 自评闭环(rules/selfcheck.md)

渲染完自动:①rs_doctor 断言(时长±0.5s/分辨率/fps/音轨/响度);②rs_bench 抽帧网格(首2s/尾2s/剪点±1.5s/随机3点)→ Agent 目测:黑帧?字幕压脸/出安全区?跳变?绿幕残留?③修复→重渲(≤3 轮)→仍败上报并列证据。

---

## 8. 风格与模板(styles/)

风格即数据(kbcut frame.md 精神):每风格一个 YAML(colors/typography/subtitle 样式/片头片尾模板/转场集/安全区)+ template.ass。

| 起步风格 | 适用 | 要点 |
|----------|------|------|
| `talkshow-bold` | 口播/绿幕 | 虚拟背景+大字居中+关键词高亮+BGM 闪避 |
| `tutorial-clean` | 教程 | 底部字幕条+信息卡插槽+步骤编号 |
| `motion-info` | 动画信息流 | artboard 场景卡串联+旁白+转场枚举 |

---

## 9. 仓库与工程结构

```
E:\平日资料\GitHub\CutFlow\              ← git 仓库(github.com/GreenChennai/CutFlow,public)
├── skills/
│   ├── cutflow/
│   │   ├── SKILL.md                     # 总控:触发描述/路由/管线编排/硬规则
│   │   ├── rules/                       # intake.md sense.md tts.md subtitles.md
│   │   │                                # compose.md jianying.md artboard.md selfcheck.md
│   │   ├── scripts/                     # rs_doctor rs_asr rs_sense rs_frames rs_tts
│   │   │                                # rs_subtitle rs_ir rs_render rs_jy_draft rs_bench
│   │   ├── templates/                   # brief.md project.schema.json styles/*.yaml *.ass
│   │   └── references/                  # draft-schema-5.9.md ffmpeg-recipes.md jianying-gui-anchors.md
│   └── cutflow-prompt/
│       ├── SKILL.md
│       └── references/                  # seedance 方法论提炼
├── docs/PLAN.md(本笔记) architecture.md BACKLOG.md CHANGELOG.md
├── tools/install.ps1                    # ffmpeg 部署+junction+config 生成+doctor
├── tests/                               # pytest smoke:IR 校验/断行评分/草稿生成快照
├── config.example.json                  # 占位;config.json gitignore
└── .gitignore                           # 素材/模型/config.json/06_output/中间件
```

- CLI 协议:rs_*.py 支持 `--json`,输出 `{ok, code, message, data}`;退出码 0 成功/2 输入错/3 依赖缺失/4 执行失败。
- config.json:ffmpeg_dir / jianying59_exe(E:\Jianying\5.9JianyingPro\JianyingPro.exe)/ jianying_draft_root / momentshift_asr_url / gptsovits{url, engine_dir, voices_dir, default_voice:"koubo-test"} / ocr_exe / vqa_python / artboard_dir / workdir_root。

## 10. 视频工程工作目录

```
E:\平日资料\视频工程\<slug>\
├── brief.md
├── 01_materials/          # 用户素材(只读)
├── 02_sensed/             # transcript_raw/corrected、ocr、vqa、frames/
├── 03_assets/             # tts_*.wav+manifest、artboard 导出、cc0 bgm
├── 04_ai_prompts/         # 首帧/i2v 提示词
├── 05_ir/project.json     # IR(唯一时间线事实源)
├── 06_output/             # final_*.mp4 preview.mp4 master.srt cover.png jianying_draft/
└── project.md             # 会话记忆(冷启动先读)
```

---

## 11. 里程碑(开工执行顺序)

| 里程碑 | 内容 | 验收 |
|--------|------|------|
| **M0 环境** | 7z 解压 ffmpeg → E:\Tools\ffmpeg(PATH+回填 artboard config);FunASR 三级预案拉起验证;GPT-SoVITS 拉起+**koubo-test** 预热合成;**5.9 剪映首启冒烟:草稿目录确认+控件树摸底(11.3 不碰)**;git init | doctor 全绿;ASR 转 JJAV2815 前 60s;koubo-test 合成一句 |
| **M1 骨架** | 仓库结构+SKILL.md 路由+CLI 协议+config+doctor | doctor --json 全绿 |
| **M2 感知** | rs_asr/rs_sense/rs_frames + 校对流程 | JJAV2815 全片转写+校对稿 |
| **M3 合成** | rs_tts(音色卡/预热/断点续传)+ rs_subtitle(断行+ASS 模板) | 纯文案→koubo-test 语音→带时间戳字幕 |
| **M4 渲染** | IR+schema+rs_render 七步管线+rs_bench 自评 | **JJAV2815 → ≥2 风格 × 双比例样片,judge 过审** |
| **M5 集成** | artboard 桥+cutflow-prompt 技能+intake/brief | 端到端:纯文案→TTS→片头→成片;提示词样例 |
| **M6 剪映 5.9** | rs_jy_draft(IR→明文草稿)+ computer-use 自动导出 | **5.9 打开草稿无误 + 自动导出一次真实跑通**(失败降级:草稿交付+人工指引) |
| **M7 首次交付** | README+install.ps1+junction+速查手册+记忆更新 → gh repo create(GreenChennai/CutFlow,public)& push | push 成功 |
| **M8 自迭代** | 持续循环,见下 | 每轮 push + tag |

### M8 自迭代循环(用户授权:不询问,持续优化,每轮推送)

**每轮流程(一个迭代 = 一次完整循环):**
1. **审计**——三个信息源:
   - 固定自检:rs_doctor 全绿?pytest smoke 全过?既有样片自评遗留问题?文档(README/SKILL 触发词/规则)完整性?
   - 弱项扫描:能力矩阵对照(用户需求十项 vs 现实现;参考项目有而我们缺的:如关键词高亮字幕、自动 resegment、CC0 BGM 库扩充、双后端能力对齐);
   - 使用回放:重跑端到端演练,记录卡点。
2. **记账**:发现全部写入 docs/BACKLOG.md(P0 缺陷/P1 优化/P2 弱项/新能力,含验证方式);
3. **实现**:取最高优先 1–3 项;
4. **验证**:smoke 必跑;动了渲染/草稿线则重渲样片对比;
5. **发布**:约定式提交 → tag `iter-NN` → **push origin main(每轮必推,不询问)** → CHANGELOG 追加。

**会话退出条件(达成后停服务 → 确认 push → 资料保存 → 电脑保持运行,不关机):**
- P0/P1 清空且连续一轮审计无新 P0/P1;或
- 会话时间预算用尽(目标 4–6 小时);或
- 连续 2 轮无实质可合并改进(防过度工程)。

**回滚支持**:每轮 tag `iter-NN` + 约定式提交;用户可 `git checkout iter-03` 或 `git revert`。
**跨会话可持续**:BACKLOG.md 即状态,用户下次说"继续迭代"即恢复循环。

---

## 12. 风险与预案

| 风险 | 影响 | 预案 |
|------|------|------|
| FunASR 无法无 GUI 拉起 | 转写阻塞 | MomentShift.exe + computer-use 点"服务模式";不行则先开发其他模块 |
| GPT-SoVITS 冷启动 10min | M3 慢 | 预热放后台;开发用短句;TTS 全量先行(铁律 7) |
| 5.9 草稿字段与 vendored 库版本不完全匹配 | M6 草稿打不开 | 5.9 实测回写 references/draft-schema-5.9.md;自研生成器按实测校准;快照测试兜底 |
| 5.9 GUI 锚点漂移 | 自动导出失败 | 降级交付草稿+人工指引;每次成功操作回写 jianying-gui-anchors.md |
| 5.9 误触发自动更新破坏自动化环境 | 自动化失效 | 开工即查更新设置并禁用;只用 5.9JianyingPro 目录,绝不混用 11.3 |
| 大文件渲染慢 | M4 耗时 | 开发用 30s 截段(draft 档);preview 档迭代 |
| ASR 句级精度不足 | 字幕漂 | 校对阶段微调;TTS 稿用实长时间戳 |
| 大素材误入 git | 仓库污染 | .gitignore 严格;提交前 git status 过目 |
| 自迭代跑偏/过度工程 | 质量抖动 | 退出条件 + "连续 2 轮无实质改进即停";每轮只动 1–3 项;push 前测试必过 |
| push 失败(网络) | 迭代未上云 | 本地 commit 不丢,恢复后补推;关机前 push 确认 |

## 13. 现场验证清单(开工第一小时)

- [ ] `"/c/Program Files/7-Zip/7z.exe" x <7z> -oE:\Tools\ffmpeg -y` + ffmpeg -version;回填 artboard config;preflight 复跑
- [ ] FunASR 直启尝试 → 不行则 MomentShift.exe + computer-use;curl /health + 真实转写一段
- [ ] GPT-SoVITS:后台起 api_v2.py → **koubo-test(口播声线卡)** 合成"测试一句"→ 时长/采样率确认
- [ ] **5.9 剪映:确认禁自动更新 → 启动 → 建空草稿 → 定位草稿目录与 draft_info.json 明文性 → computer-use 读控件树记锚点 → 关闭**(11.3 不碰)
- [ ] JJAV2815 前 60s:ffprobe + 抽帧看绿幕纯度 → 裁 30s 测试段
- [ ] gh 确认 GreenChennai;git init CutFlow

## 14. 交付物清单(最终)

1. GitHub 公开仓库 **GreenChennai/CutFlow**(双技能 + 文档 + 安装脚本 + BACKLOG/CHANGELOG,不含任何素材/模型);
2. 本地:技能 junction 生效、doctor 全绿、GCissue 速查手册已更新、记忆已更新;
3. 样片:E:\平日资料\视频工程\ 下 JJAV2815 衍生 ≥4 条(2 风格 × 2 比例)+ 1 条 koubo-test 配音端到端样片 + master.srt + 封面 + 可在 5.9 打开的剪映工程;
4. **M8 自迭代循环已运转若干轮**(每轮已 push + tag);
5. 全部服务停止(GPT-SoVITS/FunASR 等引擎)、资料保存;**电脑保持运行,不执行关机、不注销、不休眠阻断后台任务**(用户凌晨 4:00 有其他定时任务要跑)。
