---
name: cutflow
description: AI 视频制作总控技能:接收口播视频/文案/剧本分镜/图片,经自带 ASR(字级对齐)→粗剪(CutList)→合成(TTS/artboard/品牌/音效)→剪辑(FFmpeg直出+剪映5.9草稿)→字幕→烧录导出→分级自检→封面与文案,产出成片与平台物料;支持 9:16/16:9 与多 Logo 变体,支持手工改阶段后一键重建。当用户想要:做视频、剪视频、口播视频、教程视频、动画视频、把文案变成视频、给视频加字幕、粗剪去口误去重录、绿幕抠像、配音、生成剪映草稿、出 AI 视频提示词、生成封面与标题简介 Tag 时使用。
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
| 粗剪检测 + guard + CutList | **脚本** | `rs_cut` |
| 断句 DP + 硬约束 + 卡时间 | **脚本** | `segmentation` / `rs_subtitle` |
| 渲染、混音、编码、变体、烧录 | **脚本** | `rs_render` / `rs_brand` |
| L0 机械自检 | **脚本** | `rs_verify` |
| 封面抽帧与合成 | **脚本** | `rules/cover.md` |
| 术语校对、口水词、语义改写 | **Agent** | 脚本做不了 |
| 断句歧义裁决(`ambiguous`) | **Agent** | 需要语感 |
| 卡片文案与设计意图 | **Agent** | 需要创意 |
| 标题 / 简介 / Tag | **Agent**(脚本只校验+截断+生成章节) | `rs_meta` |
| L1 语义自检(看图) | **Agent**,仅在首次或用户要求时 | `rules/verify.md` |
| L2 最终验收 | **用户** | 不得代劳 |

### 1.2 读哪个文件(读完就停)

**规则文件按需加载。不是当前任务,不要读。**

| 任务 | 只读这一个 |
|---|---|
| 开工问卷、brief 契约 | `rules/intake.md` |
| 转写 / 自带 ASR / 模型 | `rules/asr.md` |
| 字级对齐 / 重映射 / Wordline | `rules/align.md` |
| 粗剪 / CutList / guard | `rules/roughcut.md` |
| 字幕断句 / CPS / 卡片 | `rules/subtitles.md` |
| 感知与校对(OCR/VQA/抽帧) | `rules/sense.md` |
| 配音 TTS | `rules/tts.md` |
| IR 与渲染 / 中间件 | `rules/compose.md` |
| 阶段缓存 / 增量 / 一键重建 | `rules/incremental.md` |
| 检查分级 / 自检 / 验收 | `rules/verify.md` |
| Logo 与变体矩阵 | `rules/branding.md` |
| 音效自动落点 | `rules/sfx.md` |
| 标题简介 Tag / 章节 | `rules/meta.md` |
| 封面 | `rules/cover.md` |
| artboard 图形素材 | `rules/artboard.md` |
| 剪映草稿双通道 | `rules/jianying.md` |
| 工程归档与命名 | `rules/archive.md` |
| 自评闭环细节 | `rules/selfcheck.md` |
| 类型节奏默认值 | `rules/genres/<类型>.md`(**仅一册**) |

> **禁止**:一次读两个以上规则文件(用户明确要求交叉说明时除外)。

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
| **S0** | 基础素材 | `rs_doctor` | `01_materials/` + `brief.md` | 素材可解码 |
| **S1** | 转写与字级对齐 | `rs_align`(→`tools/fun_asr.py`) | `05_ir/wordline.json` | 覆盖率 ≥99% |
| **S2** | 粗剪处理 | `rs_cut` | `04_cut/cutlist.json` | remove 刀 guard 全过 |
| **S3** | 基础合成 | `rs_ir build` + `rs_render` | `seg_*/base/` | IR validate |
| **S4** | 动画/信息卡 | artboard 桥(`rs_artboard`) | `composed/` | 卡片过安全区 |
| **S5** | 品牌(Logo 变体) | `rs_brand` | `branded/<id>/` | 不压字幕带 |
| **S6** | 音效 | `rs_sfx` | `mixed/` | ≤2 个 / 15s |
| **S7** | 字幕 | `rs_subtitle` | `06_output/subtitles.ass` | 回归集全绿、CPS ≤9 |
| **S8** | **烧录导出** | `rs_render` | `06_output/final_*.mp4` | 用**现有 ass**,不重新生成字幕 |
| **S9** | 自评与对齐断言 | `rs_sync` + `rs_verify` | `sync_report.md` | 偏移中位数 ≤40ms |
| **S10** | 封面与文案 | 抽帧 + `rs_meta` | `cover.png`、`metadata.json` | 平台字数合规 |
| **S11** | 交付 | `rs_cleanup [--apply]` | 变体成片 + `deliverables.md` | 清单齐全 |

---

## 3. Hard Rules(会静默失败,违反必炸)

1. **一切决策只查 brief.md**;automation 模式不打断用户,自行选择并记录理由。
2. **`wordline.json` 是时间的唯一真相源**。任何模块不得自行算时间,一律调 `rs_align.map_src_to_final()`。**禁止「按字符数比例插值」**。
3. **粗剪宁可漏删不可错删**。`conf ≥0.90` 才 `remove`,且 guard 三项(静音切点 / 不切断字内音素 / 后留 ≥60ms)必须全过,否则降级 `review`。
4. **阶段产物走声明式缓存**。缓存键 = 上游 hash + 参数 + **脚本文件 hash**。改一个字幕先 `rs_run --status`,再 `--only/--from`,不要盲目重跑。
5. **自带 ASR,不依赖外部服务器**。默认 `tools/fun_asr.py`;`onnx` 后端**没有字级时间戳**(模型导出固有限制),产出必须标 `degraded`,不得当字级用。
6. **验证分级**:每次产出跑 L0;**L1 仅在首次或画面构图变更时**;L2 由用户触发。**任何交付输出必须带 `verifyLevel` 与 `firstCheckDone`**,缺失即视为未验证。
7. **手改后走一键重建**:改哪个文件夹就跑那个文件夹的 `rebuild.py`(先备份 → 校验 → 级联 → 导出)。**不要手动去调 `rs_render`**。
8. ASR 原始输出**必须经你校对**后才能用;校对改文本后**按字级锚点重聚合**(见 `rules/align.md`),不得沿用旧时间戳插值。
9. TTS 长文**先全量合成落盘**再进时间线;逐句时长以 ffprobe **实测值**为准(禁止估算累加)。
10. 渲染:统一帧率 → 逐段提取 → concat → 合成 → 混音 → **字幕最后叠** → 编码(rs_render 已固化,勿绕过)。
11. 总线响度 -14 LUFS / -1 dBTP;人声先行归一。
12. 9:16 安全区:底部 25%、顶部 12% 不放字幕/关键信息;**Logo 默认避开字幕带**。
13. 写剪映草稿前确认剪映未运行;只动 5.9,绝不碰 11.3。
14. **竖屏(9:16)每卡 10–12 字、CPS ≤9 字/秒、单卡 0.83–7s**;旧工程按当时 `maxChars` 复现,不追改。
15. 素材/中间件/git:`01_materials` 只读;大文件与 `models/` 不进 git;工程目录 `<YYYYMMDD>-<中文标题>-<类型>`;产物中文命名并带 variantId;测试件用 `dev-` 前缀,交付前 `rs_cleanup` 必删。
16. 感知备选:Agent 自带视觉优先自己看图;OCR/VQA 仅在批量/无视觉/`force_local` 时用。
17. 开工必读 brief.类型 对应的 `rules/genres/` 分册(**仅一册**)。

---

## 4. 命令速查(全部支持 `--json`)

| 环节 | 命令 |
|------|------|
| 体检 | `rs_doctor.py --report` |
| **阶段状态 / 增量** | `rs_run.py --status` / `--from S3` / `--only S7` / `--dirty` / `--explain S7` |
| **一键重建** | `rs_run.py --init`(生成 rebuild.py)/ `--from S8 --force` / `--rollback` |
| **分级自检** | `rs_verify.py <工程>` / `--level L1` / `--mark-first` |
| **自带 ASR** | `python tools/fun_asr.py <媒体> [--backend onnx\|pkg]` / `--probe` |
| **ASR 部署** | `python tools/fetch_deps.py asr [--onnx\|--pkg\|--seed-models D]` |
| 转写 + 对齐(S1) | `rs_align.py build --media <素材> --out 05_ir/wordline.json` |
| 重映射 | `rs_align.py remap 05_ir/wordline.json --cutlist 04_cut/cutlist.applied.json --out ...` |
| 粗剪(S2) | `rs_cut.py 05_ir/wordline.json --detect all --out 04_cut` / `--apply ...` |
| CutList→IR | `rs_ir.py build --from-cutlist 04_cut/cutlist.applied.json --slug X --out 05_ir/project.json` |
| IR 校验 | `rs_ir.py validate 05_ir/project.json` |
| 渲染 | `rs_render.py 05_ir/project.json --ratio 9x16 --profile final` |
| 品牌变体 | `rs_brand.py --expand --logos a,b --ratios 9x16,16x9 --out 05_ir/variants.json` |
| 音效落点 | `rs_sfx.py 05_ir/project.json --auto --wordline 05_ir/wordline.json` |
| 字幕 | `rs_subtitle.py --from-wordline 05_ir/wordline.json --style talkshow-bold --ratio 9x16 --out 06_output` |
| **artboard 闭环** | `rs_artboard.py 03_assets/artboard/manifest.json --export\|--apply` |
| 对齐自检 | `rs_sync.py --wordline ... --ass 06_output/subtitles.ass --out 06_output` |
| 抽帧目测 | `rs_bench.py <成片> --ir 05_ir/project.json --out 06_output/bench.png` |
| 文案 | `rs_meta.py --wordline ... --brief 00_brief/brief.md --platform douyin,bili` |
| 剪映草稿 | `rs_jy_draft.py 05_ir/project.json --name <名>` |
| 抽帧 / 感知 | `rs_frames.py` / `rs_sense.py` |
| 配音 | `rs_tts.py --script 文案.txt --out 03_assets/tts` |
| 清理 | `rs_cleanup.py <工程> [--apply]` |

---

## 5. 改了东西怎么办(手工编辑工作流)

每个阶段文件夹里都有 `rebuild.py`(`rs_run.py --init` 生成):

| 你改了什么 | 运行哪个 |
|---|---|
| 字幕 `06_output/subtitles.ass` | `python 06_output/rebuild.py` |
| IR / Wordline `05_ir/` | `python 05_ir/rebuild.py` |
| 粗剪决策 `04_cut/cutlist*.json` | `python 04_cut/rebuild.py` |
| artboard 卡片 `03_assets/artboard/` | `python 03_assets/artboard/rebuild.py` |
| 拿不准 | `python rebuild.py`(全量) |

每个脚本固定四步:**备份 → 校验 → 级联(`--force` 只作用于起点,上游走缓存)→ 导出 + 自检**。
校验不过会**停住并指出具体行**;跑砸了可 `rs_run.py --rollback` 还原。

---

## 6. IR 最小样例(schema 见 `templates/project.schema.json`)

```json
{"version":1,"slug":"demo","fps":30,"canvas":{"width":1080,"height":1920},
 "tracks":[
  {"kind":"video","clips":[{"src":"01_materials/a.mp4","startMs":0,"durationMs":12000,
    "sourceInMs":3000,"reframe":{"anchorY":0.35},"motion":{"in":"fadeIn","inMs":400}}]},
  {"kind":"audio","clips":[{"src":"06_output/_build/sfx/whoosh.wav","startMs":800,"role":"sfx"}]}],
 "bgm":{"src":"03_assets/bgm.mp3","gainDb":-18,"ducking":true},
 "subtitle":{"ass":"06_output/subtitles.ass","source":"05_ir/wordline.json"},
 "outputs":["9x16"]}
```

风格 token:`templates/styles/`(talkshow-bold / tutorial-clean / motion-info)。

---

## 7. 工程目录契约(★ 为 v4/v5 新增)

```
<工程>/
├── 00_brief/brief.md
├── 01_materials/                      只读
├── 02_sensed/                         转写与校对
├── 03_assets/                         ★artboard/manifest.json
├── 04_cut/                            ★cutlist.json / cut_report.md / review/ / ★rebuild.py
├── 05_ir/                             project.json / ★wordline.json / variants.json / pipeline.json / ★rebuild.py
├── _state/                            S*.json / ★verify.json / ★backup/<ts>/
├── 06_output/                         ★final_*.mp4 / subtitles.ass / metadata.* / sync_report.md / ★rebuild.py
└── ★rebuild.py                        全量重建
```

---

## 8. 剪映 5.9 双通道 / AI 生视频边界

- **草稿直写**(主):`rs_jy_draft` → 用户获得可编辑工程;绿幕叠加无对应字段会警告。
- **GUI 自动导出**(辅,computer-use):控件锚点见 `references/jianying-gui-anchors.md`(11.3 草稿加密,永不操作)。
- 变体标记 `backends`;剪映缺特性(如 chroma)自动降级并在交付说明标注。
- **AI 生视频**:只产提示词(首帧图 + 5–10s i2v),转 `cutflow-prompt` 技能;**绝不调用任何生图/生视频 API**。

---

## 9. 反模式

**架构级**

- ❌ 用「按字符数比例插值」推导任何时间 —— 所有对齐问题的元凶。
- ❌ 在下游模块里自己算时间 —— 一律调 `map_src_to_final()`。
- ❌ 缓存键不含脚本文件 hash —— 会出现"改了代码但缓存命中"的幽灵 bug。
- ❌ 为通过验收而放宽 guard —— 应收紧检测器阈值,而不是松开保护。
- ❌ 把 `onnx` 后端的产出当字级对齐结果用。

**Token / 协作级**

- ❌ **在能用脚本的地方让 Agent 代劳**(逐字校对、语义润色除外)。
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
