# Changelog

## v0.5.0 (2026-09-10) — 自主 · 分级 · 省 Token · 可手改 · 闭环

针对用户 5 条新需求迭代。新增 ADR-0015~0017,阶段表扩为 **S0–S11**。

**R1 自带 FunASR,砍掉外在服务器(ADR-0015)**

- 新增 `tools/fun_asr.py`:一条命令转写,**不需要任何外部服务器**;后端自动选择 `pkg`(官方 funasr,有字级时间戳)→ `onnx`(轻量)→ `server`(仅兼容)
- **vendored** FuASR 裁剪版 ONNX 推理层到 `tools/asr_vendor/`(MIT,带 NOTICE 记录出处与升级方式)
- **实测关键结论**:本地 Paraformer ONNX 导出**只有 `logits` + `token_num`,没有 `timestamp`**(CIF 在导出阶段压掉了时间轴)→ onnx 后端拿不到字级时间戳,产出显式标 `degraded`
- **实测免费改善**:VAD 静音阈值 800→**400ms**,68s 口播从 5 段(中位 15.9s)变成 **27 段(中位 2.1s)**,接近句级粒度;整体约 **12× 实时**
- 模型**播种**而非下载:`fetch_deps.py asr --seed-models` 优先目录联接(零拷贝);`models/` 已 gitignore
- `rs_align.py` 改为调自带运行器;HTTP 降为兜底
- **pkg 后端实测通过(2026-09-11)**:字级时间戳 334 字 ↔ 334 条一一对应(标点零宽对齐进 `_align_ts_to_text`);
  同素材对比 onnx 降级路径 —— conf 0.95 vs 0.40、断句歧义卡 1 vs 9、违规 0 vs 1,
  「再加上一/点耐心」式断错消失;推理 RTF ≈0.08(11× 实时)。修复三处:`pick()` 不再把 ONNX 目录喂给 torch 引擎、
  `--pkg` 补装 torchaudio(fbank 必需)、probe 前置 fbank 检查

**R2 检查分级:首次全检,之后只跑代码自检(ADR-0016)**

- 新增 `rs_verify.py` + `_state/verify.json`:L0 机械自检(每次,秒级)/ L1 语义自检(首次 + 画面构图变更,Agent 看图)/ L2 人工验收(用户触发)
- **L1 只产出"待目测清单 + 抽帧命令",不自动判定** —— 判定权在 Agent/用户
- `missing`(从未跑过)**不算**画面变更;只有 `stale/failed` 才算
- **输出契约**:任何交付输出必须带 `verifyLevel` 与 `firstCheckDone`,缺失即视为未验证

**R3 省 Token:脚本优先 + 只读一个规则文件**

- SKILL.md 重写:新增「**谁来做**」表(判据:给定输入必得同一输出 → 脚本;需判断/创造/审美 → Agent)与「**读哪个文件(读完就停)**」路由表(覆盖全部 18 个 rules)
- 硬纪律:禁止一次读两个以上规则文件;SKILL.md 体量上限(有测试锁)

**R4 手动改阶段 + 一键重建(ADR-0016)**

- `rs_run.py --init`:在每个阶段文件夹生成 `rebuild.py` + 根目录 `REBUILD.md` 速查表
- 每个脚本固定四步:**备份 → 校验 → 级联 → 导出 + 自检**;校验不过**停住并指出具体行**
- `--force` 只作用于**起点阶段**,上游走缓存(这是"改字幕只要几秒"的前提)
- `--rollback` 还原备份(保留最近 5 次);`_state/backup/<ts>/`
- **新增 S8「烧录导出」阶段**:手改字幕走 S8(用现有 ass 重新烧录),**不会重新生成字幕把用户改动冲掉**

**R5 artboard 闭环(ADR-0017)**

- 新增 `rs_artboard.py` + `03_assets/artboard/manifest.json`(工程 ↔ 产物 ↔ IR 挂点 的唯一映射表)
- `--scan` / `--export`(按 `sourceHash` 只重导变了的)/ `--apply`(尺寸校验 + 时长变更自动平移下游 clip)
- **尺寸不符直接报错、不拉伸**;未挂进 IR 的卡片会被点名
- `03_assets/artboard/rebuild.py` 一条龙:导出 → 回填 → 从 S4 级联

**质量与验证**

- 测试 40 → **67 条全绿**(新增 `tests/test_v5.py`:后端能力声明、自带 ASR 不依赖 HTTP、VAD 阈值、验证分级与策略、备份/回滚、`--force` 语义、S8 不重新生成字幕、artboard hash/尺寸/时长传播、SKILL.md 路由覆盖与体量纪律)
- 真实素材端到端:68s 中文口播 → 自带 ASR(335 字/5.7s)→ Wordline → 40 张字幕卡,仅 1 张低于软性最短时长(合并会超字数上限,按设计降级为告警)

## v0.4.0 (2026-09-10) — 从「一次性出片」到「可增量 · 可对齐 · 一条龙」

针对用户提出的 5 条痛点(粗剪不合格 / 三对齐失效 / 各阶段不可编辑 / 断句不合理 / 不够一条龙)做架构级重构。按 `docs/OPTIMIZATION-v4.md` 落地,新增 ADR-0011~0014。

**P1 粗剪不合格 → 新增 S2 粗剪阶段(ADR-0012)**

- 旧管线**根本没有 cut 这一步**;现新增 `rules/roughcut.md` + `rs_cut.py`
- 三路检测器(静音 / 口头禅 / 口吃重复 / 重录)融合 → **CutList 决策表**(`reason` 9 项封闭枚举 + `conf` 三级 → `action` 三态)
- **guard 三重校验**(切点在静音区 / 不切断字内音素 / 后留 ≥60ms),不过即降级 review,绝不放宽
- **审查包**:每刀切点前后各 1.5s 音频 + 说明,「听 20 分钟整片」→「听 30 个 3 秒片段」
- 反向保护 `rhetorical_pause_suspect`;硬线:**宁可漏删,不可错删**

**P2 三对齐失效 → Wordline 字级对齐为唯一真相源(ADR-0011)**

- 根因是**三个独立时间源** + 「按字符数比例插值」;新增 `rules/align.md` + `rs_align.py`
- 产出 `05_ir/wordline.json`(双坐标:`final` 域 / `source` 域);导出唯一时间换算入口 `map_src_to_final()`
- 字幕卡时间 = 首字/末字时间戳聚合(**彻底废除比例插值**);首尾标点不计入时间跨度
- 新增 `rs_sync.py` 对齐断言:偏移中位数 ≤40ms、95 分位 ≤80ms,并给出可一键平移的系统偏差(合成素材实测 0ms / 3ms)
- 修订 `sense.md`:「校对只改文本不动时间戳」→「改文本后按**字级锚点重聚合**」

**P3 各阶段不可编辑 → 阶段清单与内容寻址缓存(ADR-0013)**

- 新增 `rules/incremental.md` + `rs_run.py`:缓存键 = 上游产物 hash + 参数快照 + **脚本文件 hash** + 外部服务版本
- `--status / --from / --only / --dirty / --explain / --mark`;`--explain` 能指出具体是哪个输入或脚本变了
- 改写 `compose.md` 的「中间件」一节:从**人肉判断复用**升级为**声明式缓存**
- 改一个字幕:20min+ → 秒级;换背景:只重渲受影响 seg

**P4 断句不合理 → 卡切分与行断开两层分离(ADR-0001 修订)**

- 新增 `segmentation.py`:**约束最优 DP** 卡切分(候选边界 / 禁切表 / 打分函数 / 硬约束)+ top-3 候选 + `ambiguous` 标记
- 禁切表:专名、成语/固定搭配、数量词+量词、数字+单位、货币+数字、`的得了着之`之后、**ASCII token 内**(含 `GPT-SoVITS` / `v2.1.0` 这类连字符 token)
- 硬约束:竖屏每卡 **16 → 12 字**、CPS ≤9 字/秒、单卡 0.83–7s、卡间距 ≥2 帧;`<0.8s 必并`、`>4s 必切`
- 行断开保留评分算法并补「金字塔形、禁顶行 1–2 字」
- 断句回归测试集 5 用例固化进 `tests/`

**P5 不够一条龙 → S0–S10 阶段注册表 + 三块空白补齐(ADR-0014)**

- 补齐 **S5 品牌**(`rules/branding.md` + `rs_brand.py`:Logo 变体矩阵,共享中间件,2 Logo × 2 比例 ≈ 1.3× 成本,默认避开字幕带)
- 补齐 **S6 音效**(`rules/sfx.md` + `rs_sfx.py`:转场/强调/列举/章节/结尾五类落点,密度 ≤2 个/15s,丢弃项留痕)
- 补齐 **S9 文案**(`rules/meta.md` + `rs_meta.py`:抖音/B站/视频号标题·简介·Tag 规范,**B站章节时间戳由 markers 自动生成**);封面正式纳入编排
- `rs_ir.py` 新增 `build --from-cutlist`:CutList → IR 主轨,**消灭「Agent 手写毫秒」这一整类误差**
- SKILL.md 重写为阶段注册表;新增反模式一节(5 条架构级 + 操作级)

**质量与验证**

- 测试 9 → **40 条**(新增 `tests/test_v4.py`:断句回归、禁切表、CPS/时长约束、guard 三重校验、重映射单调性、缓存键含脚本 hash、变体安全区、音效密度、文案截断与章节、对齐自检、文档防漂移)
- 完整链路冒烟通过:S1 对齐 → S2 粗剪 → apply → remap → S3 生成 IR → S7 字幕(DP) → S8 对齐自检 → S6 音效 → S9 文案

## v0.3.0 (2026-09-09) — 工程化与类型知识库迭代

12 条用户反馈全落地:

- **开工前提问环节**(SKILL 第 0.5 步):companion 模式逐项问,automation 模式按 genres 分册默认值自查;brief 增加类型与动画密度字段
- **工程归档规范**(ADR-0007):目录 `<YYYYMMDD>-<中文标题>-<类型>`;产物中文化命名;`rs_cleanup.py` 完工清理(dry-run 默认);旧工程已迁移
- **Config 图形编辑器**:tools/config_gui.py(tkinter 零依赖)+ PyInstaller onefile exe(CutFlowConfigEditor.exe,12MB);分组表单/中文说明/路径浏览/frozen 路径
- **小白教程**:README 重写(新手五步 + Config 参数全表:作用/填什么/去哪获取)
- **动画安全区**(ADR-0009):视频卡内容带垂直 250–1290px,底部 576px 字幕带留白;四卡全部重做;背景升级 6s 无缝循环动画(clip.loop 支持);纯动画视频场景全覆盖
- **类型剪辑知识库**(ADR-0010):rules/genres/ 六册(口播/动画教程/新闻采访/短剧/影视解说/通用),全部带权威来源(广电总局/BBC/NBCU/MD3/AES/YouTube 官方)
- **依赖发行为**(ADR-0008):Release v0.3-dependencies 提供 ocr-module.zip(113MB)/vqa-module.zip(529MB,相对路径修复版);tools/fetch_deps.py 一键部署;tools/fetch_ffmpeg.py 多镜像一键部署
- **感知备选策略**:Agent 视觉优先,本地 OCR/VQA 兜底(config.sense.force_local);rs_sense 支持 vqa_exe 直连模式
- **自动封面**:rules/cover.md 固化抽帧+抠像+合成三路线,产物为标准交付物


## v0.2.0 (2026-09-08) — 质量精进迭代

十条用户反馈全部落地,成片质量从"链路通"升级到"可观看":

- **字幕轻改写引擎 textopt.py**(ADR-0001/0002):Netflix 简体中文规范(句号/逗号不入屏、?!保留、断行 ≤16 字)+ 抖音红线(无错别字/时间轴同步);删口水词;语义断行;ASCII 词禁切;时间轴按字符插值。9 单测覆盖。
- **绿幕合成管线**(ADR-0003):`chroma.cropTopPct` 裁顶部非绿幕区(实测素材白墙占 0-9.5%)→ chromakey+despill → artboard 虚拟演播室背景;修 overlay **中心点定位**语义(此前卡片/人物下坠半屏)与 overlay fade 时间基准。
- **穿插动画体系**(ADR-0004):知识/对比/章节/观点四类 artboard 动画卡(MP4 25fps),2-3 秒视觉节拍原则;实测对比卡/风控卡/定义卡/AI 卡全屏插入正确节拍。
- **成片 B:Jimi 纯声音动画视频**(ADR-0006):EchoSmith 已合并为 Skill,Jimi 重训权重(e12+s552)全程配音 51 句 152s;修复 Jimi 卡过期 sovits 路径与超长参考音频(3-10s 硬限)。
- **趣味素材**(ADR-0005):Mixkit 免费音效 7 个入库(assets/sfx + CREDITS),IR 支持 `assets_sfx:` 伪协议;剪映云端素材确认不可离线用,本地字体花字感为替代。
- **真人封面**:抽帧 → rembg 抠像(isnet)→ PIL 去绿边/裁杂物 → artboard 合成标题+人物+播放键封面。
- **rs_doctor --report**:人读环境自检报告(分组/就绪度/修复提示),12 项检查。
- 修复:rs_asr 行内时间戳解析、rs_ir assets_sfx 校验、Jimi 首句"电子"→"电商"(音频+字幕同步重合成)。
- 验收:judge 两轮,成片 A/B 全部通过(成片 A 167.6s,成片 B 152.5s,均 1080×1920@30)。


## v0.1.0 (2026-09-07)

首个交付版本。核心链路本机实测通过:

- **cutflow 总控技能**:intake 问卷 → brief.md 契约;感知(FunASR `structured=1` 句级转写 + OCR/VQA + Agent 校对);合成(GPT-SoVITS 音色卡配音,默认 koubo-test,断点续传;artboard 片头/封面桥);剪辑(毫秒级 IR → FFmpeg 七步管线:统一帧率/逐段提取 8ms afade/concat/overlay 含绿幕抠像/混音 ducking + loudnorm -14 LUFS/ASS 字幕最后叠);自评(ffprobe 断言 + rs_bench 抽帧网格目测 ≤3 轮)。
- **剪映 5.9 双通道**:rs_jy_draft 直写明文草稿(draft_content.json + draft_meta_info.json + root_meta_info.json 注册);computer-use GUI 自动导出(Ctrl+E)。
- **cutflow-prompt 技能**:Seedance 2.0 方法论的 AI 首帧图提示词 + 首帧标注式 5-10s i2v 提示词。
- 验收:2 风格 × 2 比例样片全部通过视觉裁决(2 轮修复:画中画按画幅适配、字幕条安全区、整词断行)。

### iter-01 (2026-09-07)

- fix: install.ps1 在 PowerShell 5.1 下解析失败(无 BOM UTF-8 含中文)→ 改 ASCII 版,实测通过
- feat: 渲染器补齐 transition(xfade 链 + acrossfade;时长消耗 0.5s 实测正确)
- chore: config.example.json 补 momentshift_dir

### iter-02 (2026-09-07)

- fix: rs_jy_draft 时间单位错误(ms 误作 μs)——此前生成的草稿片段时长缩短 1000 倍,已修正并重生成验证
- feat: 5.9 草稿写入转场(fade→叠化/wipeleft→向左擦除/slideleft→左移,未映射回退叠化并警告)与 clip.fade 音频淡入淡出
- docs: README 增加端到端使用示例

### iter-03 (2026-09-07)

- fix(P1): 转场吞时长导致的音频/字幕时间轴漂移风险——schema 写明"使用转场时 startMs 需预扣转场消耗"的约定,rs_render 渲染前主动警告
- chore: project.schema.json 补 clip.fade 字段(音频淡入淡出)

### iter-04 (2026-09-08)

- fix: 断行评分统一收敛到 textopt.card_split(BAD_END 含数字防"一/个"切断、虚词收尾加分、候选范围 ≤max_chars);
- fix: Jimi 首句"电子运营"→"电商运营"(TTS 音频+manifest 时间轴+整轨重拼接+字幕同步);
- 验收: judge 终验两片 pass,遗留项(悬"一"字)已全分辨率帧闭环。
