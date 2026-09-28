---
name: cutflow
description: AI 视频制作总控技能:接收口播/混剪/vlog/短剧解说/录屏等多风格视频、文案或分镜,经自带 ASR(字级对齐)→粗剪(CutList)→合成(TTS/artboard/品牌/音效)→剪辑(FFmpeg直出+剪映5.9草稿)→字幕→烧录导出→分级自检→封面文案,产出成片与平台物料;支持 9:16/16:9 与多 Logo 变体,支持自然语言改片(rs_edit)与手工改阶段后一键重建。**抠像默认由用户预处理**(S0 检测绿幕/蓝幕并阻断;显式 --allow-auto-matting 可走 ADR-0050 五项质量门禁,达标才放行)。当用户想要:做视频、剪视频、口播视频、教程视频、混剪卡点、vlog、影视解说、录屏教程、把文案变成视频、给视频加字幕、粗剪去口误、配音、生成剪映草稿、出提示词、生成封面与标题简介 Tag 时使用。
---

# CutFlow — AI 视频制作总控

一句话:素材进 → 成片与平台物料出,全程**可中断、可增量、可审查、可手改**。
**你是编排者,脚本是机械臂;决策看 brief,时间看 Wordline,判定看验证级别。**

---

## 1. 决策速查(先看这里,能省掉 80% 的无效读取)

### 1.1 谁来做

> **判据:能写成「给定输入必得同一输出」的 → 脚本;需要判断/创造/审美的 → Agent。**

| 产物 / 动作 | 谁做 | 备注 |
|---|---|---|
| 环境体检、依赖部署、模型播种 | **脚本** | `rs_doctor` / `fetch_deps` |
| 转写、字级对齐 | **脚本** | `tools/fun_asr.py` / `rs_align` |
| 配音强制对齐(TTS 补字级) | **脚本** | `rs_dub` |
| 粗剪检测 + guard + CutList | **脚本** | `rs_cut` |
| 断句 DP + 硬约束 + 卡时间 | **脚本** | `segmentation` / `rs_subtitle` |
| 渲染、混音、编码、变体、烧录 | **脚本** | `rs_render` / `rs_brand` |
| L0 机械自检 | **脚本** | `rs_verify` |
| 封面抽帧与合成 | **脚本** | `rules/cover.md` |
| 术语校对、口水词、语义改写 | **Agent** | 脚本做不了 |
| 提示词语义解析(brief.json/plan.json) | **Agent** | 唯二语义工作之一;校验/补默认/查表/落盘归 `rs_intent` 编译 |
| 自然语言改片(声明式编辑) | **脚本**(Agent 只写 ops.json) | `rs_edit`;语法 → `rules/edit-op.md` |
| 断句歧义裁决(`ambiguous`) | **Agent** | 需要语感 |
| 卡片文案与设计意图 | **Agent** | 需要创意 |
| 标题 / 简介 / Tag | **Agent**(脚本只校验+截断+生成章节) | `rs_meta` |
| L1 语义自检(看图) | **Agent**,仅在首次或用户要求时 | `rules/verify.md` |
| L2 最终验收 | **用户** | 不得代劳 |

### 1.2 读哪个文件(读完就停)

**规则文件按需加载。不是当前任务,不要读;一册读完就停,一个任务最多读两册。**
**越界即停:不在本表的分册不要读。** 确认有没有某能力/命令、旗标与缺省(**不读源码**),先查
`skills/cutflow/capabilities.json`(机器可读能力目录,`rs_caps.py generate` 再生成);
目录不够用时跑 `python skills/cutflow/scripts/rs_caps.py search <关键词>`(按命令名/用途/关键词检索)。

| 任务 | 只读这一个(读完就停) | 本册给你的判据(输出什么才算过) |
|---|---|---|
| 开工问卷、brief 契约、videoType 选择 | `rules/intake.md` | brief.md 就绪:必问项无缺、绿幕四问过 |
| 转写 / 自带 ASR / 模型部署 | `rules/asr.md` | transcript 产出;onnx 产出标 degraded,不当字级用 |
| 字级对齐 / 重映射 / Wordline / 校对回填 | `rules/align.md` | 覆盖率 ≥99%、startMs 单调;时间只出自 wordline |
| 粗剪 / CutList / guard / 保护区 | `rules/roughcut.md` | 误删 = 0;guard 硬过项全过;裁剪比落 20–35% |
| 自然语言改片(ops.json 语法) | `rules/edit-op.md` | ops.json 过 `--dry-run` 人话差异表再 apply |
| 字幕断句 / 卡片 / 折行 / override | `rules/subtitles.md` | 回归集全绿;字数/CPS/时长/间距硬约束全过 |
| 平台预设 / 画幅适配 / 安全区 | `rules/platforms.md` | 平台参数查表命中;字幕不出安全区 |
| 感知与校对(OCR/VQA/抽帧) | `rules/sense.md` | corrected 版转写就绪;降级必须留痕 |
| 配音 TTS | `rules/tts.md` | manifest 就绪;时长以 ffprobe 实测 |
| IR 与渲染 / 中间件 / 转场 | `rules/compose.md` | `rs_ir validate` 全绿;渲染只走既有管线 |
| 阶段缓存 / 增量 / 一键重建 / --auto | `rules/incremental.md` | 缓存命中合理;裁决进 decision_log |
| 阶段状态机三态 / --explain 判据 | `rules/pipeline-state.md` | 8 盘面结论唯一;stale 可解释到具体输入(rules 判据,机器口在 stages.json) |
| 检查分级 / 自检 / 验收 | `rules/verify.md` | 输出带 `verifyLevel` 与 `firstCheckDone` |
| Logo 与变体矩阵 | `rules/branding.md` | 变体出齐;Logo 不压字幕带 |
| 音效自动落点 | `rules/sfx.md` | 密度 ≤2 个/15s;dropped 留痕 |
| 标题简介 Tag / 章节 | `rules/meta.md` | 平台字数合规(脚本校验+截断) |
| 封面 | `rules/cover.md` | 封面尺寸/安全区合规 |
| artboard 图形素材 / 动画卡 | `rules/artboard.md` | 卡片过安全区机检;sourceHash 回写 |
| 剪映草稿双通道(含诚实验收说明) | `rules/jianying.md` + `rules/jianying-verification.md` | 草稿门禁过;验收说明如实三类 |
| 双向编辑闭环与剪映单向出口 | `rules/editing-roundtrip.md` | 变更识别闭环走完;越界改动被标脏 |
| 工程归档 / 命名 / 清理 | `rules/archive.md` | 交付清单齐全;dev- 试算件清零 |
| 自评闭环细节 | `rules/selfcheck.md` | 三层自评结论写入 project.md |
| 依赖懒加载 / 部署降级 | `rules/deps-lazy.md` | 缺失必降级留痕;`--no-fetch` 绝不触网 |
| 类型节奏/管线分支默认值 | `rules/video-types/<videoType>.md`(**仅一册**) | 该类型节奏与红线逐条过 |
| 剪辑手法库(25 条+禁忌,剪辑决策前读) | `rules/editing-grammar.md` | 手法带出处分级与禁忌,不发明参数 |

> CutForge 四桥(`rs_editor` / `rs_notes` / `rs_oplog` / `rs_gate`)命令全表在 capabilities.json;
> 门禁里程碑范围 **M0–M7,以 gate.py 注册表为准**(无里程碑参数报错)。全部命令细节以
> `capabilities.json` + `<script> --help` 为准——SKILL 不再维护手写命令表(单一真相源,第一册 T1.2)。

---

## 2. 管线总览(S0–S11)

```
S0 素材 ─► S1 转写+字级对齐 ─► S2 粗剪 ─► S3 基础合成 ─► S4 动画信息 ─► S5 品牌
  │          │wordline.json      │cutlist     │base          │composed     │branded
  └──────────┴───────────────────┴────────────┴──────────────┴─────────────┤
                                   下游全部共享 map(t_src)→t_final          ▼
   S11 交付 ◄─ S10 封面+文案 ◄─ S9 自评 ◄─ S8 烧录导出 ◄─ S7 字幕 ◄─ S6 音效
```

| 阶段 | 名称 | 主要脚本 | 产物 | 门禁 |
|---|---|---|---|---|
| **S0** | 基础素材 | `rs_ingest` + `rs_doctor` | `01_原始素材/manifest.json` + `brief.md` | 素材可解码;**未处理幕布检测(ADR-0031)** |
| **S1** | 转写与字级对齐 | `rs_align`(→`tools/fun_asr.py`) | `05_时间线工程/wordline.json` | 覆盖率 ≥99% |
| **S2** | 粗剪处理 | `rs_cut` | `04_粗剪决策/cutlist.json` | remove 刀 guard 全过 |
| **S3** | 基础合成 | `rs_ir build` + `rs_render` | `seg_*/base/` | IR validate |
| **S4** | 动画/信息卡 | artboard 桥(`rs_artboard`) | `composed/` | 卡片过安全区 |
| **S5** | 品牌(Logo 变体) | `rs_brand` | `06_成片输出/成片_*.mp4` | 不压字幕带 |
| **S6** | 音效 | `rs_sfx` | `05_时间线工程/sfx_draft.json` | ≤2 个 / 15s |
| **S7** | 字幕 | `rs_subtitle` | `06_成片输出/subtitles.ass` | 回归集全绿、CPS ≤9 |
| **S8** | **烧录导出** | `rs_render` | `06_成片输出/final_*.mp4` | 用**现有 ass**,不重新生成字幕 |
| **S9** | 自评与对齐断言 | `rs_sync` + `rs_verify` | `sync_report.md` | 对齐断言/成片音频内容闸/QC 体检(判据见 rules/verify.md) |
| **S10** | 封面与文案 | 抽帧 + `rs_meta` | `封面.png`、`metadata.json` | 平台字数合规 |
| **S11** | 交付 | `rs_cleanup [--apply]` | 变体成片 + `deliverables.md` | 清单齐全 |

工程目录契约 → `references/project-layout.md`(唯一真相源 `rs_paths.py`);IR 样例 →
`references/ir-sample.json`(schema 见 `templates/project.schema.json`)。

---

## 3. 入口铁律(会静默失败,违反必炸;阶段专业铁律已下沉到 §1.2 对应分册)

1. **一切决策只查 brief.md**;automation 模式不打断用户,自行选择并记录理由;`--auto` 无人值守同理且更进一步——从意图编译入口起全程自动、每条裁决写 `05_时间线工程/pipeline.json` 的 decision_log、L1 降级抽帧留证;它是入口与留痕约定,不是新状态机。
2. **验证分级**:每次产出跑 L0;**L1 仅在首次或画面构图变更时**;L2 由用户触发。**任何交付输出必须带 `verifyLevel` 与 `firstCheckDone`**,缺失即视为未验证。
3. **禁止为一次性任务现写剪辑逻辑脚本**。凡「给定输入必得同一输出」且可能复用的(批量生成、装配挂轨、导出兜底、清理对账),必须升格为官方 `rs_*` 子命令并进能力目录(改完跑 `rs_caps.py generate` 再生成 `skills/cutflow/capabilities.json`);语义、创作、审美类的一次性判断仍归 Agent,不在此列。
4. **越界即停(不可违反的边界)**:**L2 最终验收归用户**,Agent 不得代劳,未验收不得宣称完成;不在 §1.2 表内的规则分册不要读;**AI 生视频只产提示词**(首帧图 + 5–10s i2v),原 cutflow-prompt 技能组已停用并归档到 `docs/archive/cutflow-prompt/`,**绝不调用任何生图/生视频 API**。

---

## 4. 改了东西怎么办(手工编辑工作流)

先 `rs_run.py --status` 看哪个阶段 stale(带 `staleReason`),再按位置选重建入口(`rs_run.py --init` 生成各 rebuild.py):

| 你改了什么 | 运行哪个 |
|---|---|
| 字幕 `06_成片输出/subtitles.ass` | `python 06_成片输出/rebuild.py` |
| IR / Wordline `05_时间线工程/` | `python 05_时间线工程/rebuild.py`;⚠ **手改过 `05_时间线工程/project.json`**(手注单 clip 音频/转场)→ 改跑 `python 06_成片输出/rebuild.py`(S8 只用现有 ass,不碰 IR) |
| 粗剪决策 action 改动 `04_粗剪决策/cutlist.json` | `python <scripts>/rs_cut.py --apply 04_粗剪决策/cutlist.json`(重算 keep;**不要**重跑 S2 detect——会冲掉 action 编辑) |
| artboard 卡片 `03_创作素材/artboard/` | `python 03_创作素材/artboard/rebuild.py`(一条龙:只重导源码变了的卡 → 回填 IR → S4 级联) |
| 拿不准 | `python rebuild.py`(根目录,全量) |

rebuild.py 的实际行为:`--from <Sx> --force` 起跑,**每个真正写盘的阶段跑前自动备份**到 `_内部状态/backup/`(留最近 5 次),末端按策略跑 L0/L1 自检;输入校验由各阶段脚本承担(不合法会停住并指出具体位置);跑砸了 `rs_run.py --rollback` 还原。

---

## 5. 编辑器改了什么(变更识别闭环 RT)

人在 CutForge 里改完盘面,CutFlow 侧按此链路接住(顺序固定):**`rs_run.py --status`**(outHash
护栏把带外改写标 stale;有会话摘要则末尾列「rev 区间 + 人工 Op」)→ **`rs_editor.py diff <工程>`**
(有摘要按 Op 逐条人话;无摘要拿 `.cutforge/bases/` 最新基线对比盘面,如「V2 轨新增 1 个 overlay 卡
3.2–5.0s」)→ **定向重建**(只动字幕走 `06_成片输出/rebuild.py`;动 IR/卡片走对应 rebuild.py;拿不准
`rs_run.py --from S3 --force`,上游走缓存)→ **重跑 S9 自检**(`rs_run.py --verify-full`,画面变过必 L1)。何时用哪条:看「脏没脏」用 --status;要「改了什么」用 diff;要「接着出片」走后两步。

---

## 6. 反模式

**架构级**

- ❌ 用「按字符数比例插值」推导任何时间 —— 所有对齐问题的元凶。
- ❌ 在下游模块里自己算时间 —— 一律调 `map_src_to_final()`。
- ❌ 缓存键不含脚本文件 hash —— 会出现"改了代码但缓存命中"的幽灵 bug。
- ❌ 为通过验收而放宽 guard —— 应收紧检测器阈值,而不是松开保护。
- ❌ 把 `onnx` 后端的产出当字级对齐结果用。

**Token / 协作级**

- ❌ **在能用脚本的地方让 Agent 代劳**(逐字校对、语义润色除外;为一次性任务现写剪辑逻辑脚本同禁 —— 入口铁律 3)。
- ❌ **一次读两个以上规则文件**。
- ❌ **Agent 主动跑 L1 目测**(用户没要求时)—— 费时间费 Token,还剥夺用户验收权。
- ❌ **在没有备份的情况下跑 `rebuild.py`**。
- ❌ **`verifyLevel` 缺失还宣称"完成"**。
- ❌ 手动调 `rs_render` 绕过阶段缓存。

**操作级**

- 不要手写 filtergraph 一步到位渲染全片。
- 不要相信 ASR 专有名词;brief 提供术语表。
- 不要跳过 S2 —— 粗剪省下的 20–35% 时长是最便宜的收益。
- 不要在剪映运行时写它的草稿目录。
- 忘了 ass 路径转义(Windows 盘符冒号)会让字幕静默丢失 —— rs_render 已处理。
