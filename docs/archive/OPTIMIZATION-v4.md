# CutFlow 优化提案 v4 — 从「一次性出片」到「可增量 · 可对齐 · 一条龙」

> **审查对象**：CutFlow v0.3.0（仓库 `GreenChennai/CutFlow`，2026-09-09 快照）
> **审查范围**：`skills/cutflow/SKILL.md`、`rules/*`、`rules/genres/*`、`scripts/rs_*.py`、`docs/adr/0001~0010`、`docs/BACKLOG.md`、`docs/PLAN.md`
> **产出日期**：2026-09-10
> **方法**：逐条把 5 个用户痛点回溯到架构层面的根因（而非表层功能缺失），再对照影视后期行业规范与开源实现给出可落地方案
> **性质**：提案，不含代码改动；落地拆为 4 个批次，可独立验收

---

## 0. TL;DR

**五条抱怨不是五个功能缺失，而是同一个地基问题：CutFlow 现在是一条「线性一次性」管线。**

它的隐含假设是——素材已经是干净的、时间是只有一个真相的、产物是最终态的。这三条假设全都不成立，于是：

| # | 用户抱怨 | 架构级根因 | 一句话方案 | 收益量级 |
|---|---|---|---|---|
| P1 | 粗剪不合格，口误/废弃片段识别不到 | **管线里根本没有「粗剪」这个阶段**。`SKILL.md §1` 的路由是 `口播视频 → rs_asr → 校对 → 字幕`，原始画面直接被当成成品片段写进 IR | 新增 **S2 粗剪**：三路检测器（静音/口头禅/重录）融合 → 产出可读可改的 **CutList** → 带三重安全校验执行 | 长视频裁掉 20–35% 时长，且不用人肉听 |
| P2 | 字幕/声音/画面对不上 | **三个时间源互不隶属**：画面用 IR 手写 `sourceInMs`、声音用 TTS 名义累加、字幕用「ASR 句级时间戳按字数比例插值」。插值本身就是系统性误差；`sense.md` 又规定「校对只改文本不动时间戳」，删了字却不改时间 → 误差被放大；一旦剪辑，三者各自漂移 | 建 **Wordline（字级对齐时间轴）** 作唯一真相源，下游全部用同一个 `map(t_src) → t_final` 重映射函数派生；新增 `rs_sync` 做机器断言（偏移中位数/95 分位） | 字幕 onset 误差从 **±300~800ms → ≤±80ms** |
| P3 | 各阶段不可编辑，改背景要全跑 20 分钟 | **阶段产物不是一等公民**。`compose.md` 现在的原话是「改了字幕只重跑 step6-7……**手改时可复用 mixed.mkv**」——靠人肉记忆复用中间件，没有声明式缓存与依赖图 | **Stage Manifest + 内容寻址缓存**：`rs_run.py --from S3 --only S7`，hash 决定跳过；粒度到 segment | 改一个字幕：**20min → 秒级**；换背景：只重渲受影响 seg |
| P4 | 字幕断句不合理，「滚滚长/江东逝水」 | **做的其实是「行内换行」，不是「卡切分」**。`textopt.card_split` 的评分是断行评分；而卡切分在 `subtitles.md` 里只是「≤16 字」的**长度驱动**切分——长度驱动必然切断语义 | 两层分离；卡切分改为 **约束最优切分（DP/Viterbi）**，边界候选=标点+字级停顿+句法线索，硬约束=CPS/时长/字数，输出 top-3 候选 | 断句错误率可通过**回归测试集**量化收敛到 0 |
| P5 | 不够一条龙 | 阶段表缺失，且缺 S5 品牌（Logo 变体）、S6 音效自动落点、S9 文案生成（标题/简介/Tag）三块 | 建立 **S0–S10 阶段注册表**，直接对齐用户原话的阶段顺序；每阶段带门禁与可增量性 | 一次编排出全部交付物，含多 Logo 变体与平台文案 |

**最关键的判断**：P3（增量）与 P2（对齐）必须**先做**。P2 不做，P1/P4 的效果都建立在流沙上（你剪得再准，时间轴还是漂的）；P3 不做，P1/P4 每调一次都要重跑 20 分钟，迭代成本高到没人愿意调。

**成本判断（好消息）**：`rs_render` 已经是「逐段提取 → concat」的 segment 化结构（`PLAN.md §7.2`），IR 也已经是一份合格的简化版 EDL。增量改造**不是重写**，是给已有的 segment 加上 hash 清单。

---

## 1. 现状审计

### 1.1 现有能力盘点（v0.3.0 真实状态）

| 层 | 已具备 | 文件 |
|---|---|---|
| 契约 | intake 问卷 → `brief.md` 单一契约；automation/companion 双模式 | `rules/intake.md` |
| 感知 | FunASR **句级**转写 + Agent 校对；OCR/VQA 备选；抽帧网格 | `rules/sense.md`、`rs_asr.py` |
| 合成 | GPT-SoVITS 音色卡 TTS（断点续传）；artboard 桥 | `rules/tts.md`、`rs_tts.py` |
| 剪辑 | 毫秒级 IR（`project.json`）+ 双后端（FFmpeg 直出 / 剪映 5.9 草稿） | `rules/compose.md`、`rs_render.py`、`rs_jy_draft.py` |
| 字幕 | textopt 轻改写（Netflix 简化中文规范）+ **断行**评分 + ASS 样式模板 | `textopt.py`、`rules/subtitles.md` |
| 自评 | ffprobe 断言 + 抽帧网格目测 ≤3 轮 | `rules/selfcheck.md` |
| 类型知识 | 六册 genres（口播/动画教程/新闻采访/短剧/影视解说/通用） | `rules/genres/` |

这是一份**相当扎实**的底子。下面五条抱怨，全部落在它的**空白与耦合**处，而不是能力不足处。

### 1.2 逐条根因定位

#### P1 · 粗剪不合格 → 管线无粗剪阶段

`SKILL.md §1` 的管线总览里，口播视频的路由是：

```
口播视频 ──→ rs_asr 转写 → 你校对 → rs_subtitle 字幕
```

**没有 cut 这一步。** 原始素材被当作已经拍好的成片，直接进入感知与合成。用户说的「口误/废弃片段夹杂」在系统里没有任何表达位置——IR 的 `video.clips[]` 只有 `src/startMs/durationMs/sourceInMs`，它是一个**已经决定好的时间线**，不是**待决定的**。

进一步看：`cutlist`、`retake`、`filler`、`silence` 这些词在整个仓库（除 `textopt.py` 里删句首语气词那一条外）**零出现**。这不是参数没调好，是阶段不存在。

#### P2 · 三对齐失效 → 三个独立时间源 + 比例插值

把时间信息的来源列出来，问题一目了然：

| 元素 | 时间来源 | 精度/误差 |
|---|---|---|
| 画面 | IR 里手写的 `startMs` / `durationMs` / `sourceInMs` | 人（Agent）决定，无校验 |
| 原声 | 与画面同源，跟随 IR 段 | 相对可靠 |
| 字幕（口播） | ASR **句级** `[12.3s]` | 句边界 ±300~800ms |
| 字幕（TTS） | `manifest.json` 各句 wav 时长**累加** | 累加漂移 + 无句内位置 |
| 字幕内部切分 | 「按字符数比例分摊」（`subtitles.md` 第 4 条） | **系统性误差源**：语速不均时，按字数分摊必错 |

再叠加两个放大器：

1. `sense.md §3` 规定校对「**只改文本不动时间戳**」——删掉口水词、修正错字后字符数变了，而第 4 条又按**新字符数**比例分摊到**旧时间戳**上。
2. 一旦 S2 粗剪落地，时间轴整体重映射，上表每一行都要各自重算一遍——这必然出现「字幕对了画面错、画面对了字幕错」的此消彼长。

**结论：只要存在多于一个时间源，「对上」就只能靠巧合。** 这不是精度问题，是架构问题。

#### P3 · 不可编辑 → 无声明式缓存，靠人肉复用中间件

`rules/compose.md` 的「中间件」一节原文：

> `06_output/_build/<ratio>/` 下 `seg_*/base/composed/mixed/subtitled` 可复用；**改了字幕只重跑 step6-7（重调 rs_render 会全跑，手改时可复用 mixed.mkv）**。

这就是全部了：产物摆在目录里，能不能复用、复用哪几个，**靠 Agent 每次现场判断**。没有依赖声明，没有脏值计算，没有工具版本记录。所以：

- 改背景 → 改 IR → `rs_render` 全跑（用户实测 20min+）
- 改一个字幕 → 理论上只需 ASS 事件重生成 + 一次 overlay，实际因为「重调 rs_render 会全跑」而全跑
- 调语句顺序 → 因为字幕/动画/音效时间都嵌在 IR 里 → 全跑

**并且没有任何"为什么慢"的可观测性**：跑完之前你不知道哪几步会命中缓存。

#### P4 · 断句不合理 → 做的是断行，不是切分

`subtitles.md` 第 3 条写的是「**卡级切分** ≤16 字（9:16）/≤22 字（16:9），ASCII 词内禁切」，第 14 条评分规则「标点+100 / 空格+90 / 句尾虚词+50 / ASCII 切断−200 / 每填一字−4」——这套评分的作用域是**一行怎么折**（line break），而卡与卡之间怎么切（segmentation），实现上是**纯长度**的。

「滚滚长江东逝水」被切成「滚滚长」/「江东逝水」正是长度驱动的典型病理：某处刚好到 16 字上限就硬切，既不认专名（长江），也不认诗句整体性。

第二个问题是**没有 CPS 约束**。CutFlow 的约束只有「≤N 字」，但一个字少的一卡和一卡字多的卡，屏幕停留时间可能一样——可读性由 `字数 / 秒` 决定，不是由字数决定。

#### P5 · 不够一条龙 → 阶段表缺失 + 三块空白

把用户原话的阶段顺序与现状对照：

| 用户要的阶段 | CutFlow 现状 | 缺口 |
|---|---|---|
| 基础素材 | ✅ `01_materials` + MANIFEST | — |
| 粗剪处理 | ❌ **不存在** | P1 |
| 基础合成 | ✅ IR + `rs_render` step1-4 | — |
| 动画/其他信息添加 | ⚠️ 有 artboard 桥与 ADR-0004 卡片体系，但靠 Agent 手动塞进 IR | 无自动落点 |
| Logo 添加（**多 Logo 分别导出**） | ❌ **完全不存在** | 无品牌层、无变体矩阵 |
| 音效添加 | ⚠️ `assets_sfx:` 伪协议，7 个 Mixkit 音效，**手动挂** | 无自动落点 |
| 字幕合成 | ✅ | — |
| 封面合成 | ✅ `rules/cover.md` | 未进阶段表 |
| 文案生成（标题+简介+Tag） | ❌ **完全不存在** | 无 |

所以「不够一条龙」的准确表述是：**中间缺两段（粗剪、品牌），尾巴缺两段（封面/文案未纳入编排），中间三段的自动落点靠手工**。

---

## 2. 权威依据（不在真空中设计）

### 2.1 字幕：分句规则与硬参数

**BBC Subtitle Guidelines** 对「怎么切」有明确定论（原文）：

> Sentences should be segmented at natural linguistic breaks such that each subtitle forms an integrated linguistic unit. Thus, **segmentation at clause boundaries is to be preferred.** … There is considerable evidence from the psycho-linguistic literature that normal reading is organised into word groups corresponding to syntactic clauses and phrases, and that linguistically coherent segmentation of text can significantly improve readability. **Random segmentation must certainly be avoided.**
> —— <https://www.bbc.com/accessibility/forproducts/guides/subtitles/>

这一条直接否决了 CutFlow 现在的长度驱动切分，也是 P4 方案的理论基础。

**Netflix 简体中文 Timed Text Style Guide**（CutFlow 已在 ADR-0001 引用，但只取了其中一部分）：

| 参数 | Netflix 简体中文 | CutFlow 现状 | 判定 |
|---|---|---|---|
| 每行字数 | **16 字** | 9:16 = 16 ✅ / 16:9 = 22 | 9:16 见下 |
| 最多行数 | 2 | 2 ✅ | — |
| 阅读速度 | 成人 **≤9 字/秒**，儿童 ≤7；SDH ≤11 | **无此约束** | ❌ 缺 |
| 最短时长 | 5/6 秒（≈0.83s） | 无 | ❌ 缺 |
| 最长时长 | 7 秒 | 无 | ❌ 缺 |
| 卡间距 | 2 帧 | 无 | ❌ 缺 |
| 标点 | 句号/逗号不入屏，用空格；?! 保留 | ✅ | — |
| 断行 | **金字塔形（底部较宽），避免顶行只剩一两个字** | 部分（有虚词收尾加分） | ⚠️ 不完整 |

> **竖屏 16 字的问题**：Netflix 的 16 字是 16:9 横屏标准。竖屏 9:16 屏宽只有约 60%，多份行业汇总把 CJK 竖屏每行建议值定在 **8–10 字**（<https://subhero.io/blog/subtitle-standards-guide>）。CutFlow 的 `talkshow-bold` 直接用 16 字，在 1080×1920 上会顶满安全区。**建议 9:16 改 10–12 字**，并在 `rs_subtitle` 里把 `maxChars` 做成按比例 + 字号推导，而不是写死。

### 2.2 对齐：字级时间戳是完全可得的

这是本次审查中**性价比最高的一个发现**：

**FunASR Paraformer 原生输出字符级时间戳**，一次调用即可（<https://funasr.com/en/blog/speech-to-text-timestamps-python.html>）：

```python
model = AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc")
res = model.generate(input="audio.wav")
res[0]["text"]      # "欢 迎 大 家 来 体 验"
res[0]["timestamp"] # [[880,1120],[1120,1360],[1380,1540],...]  逐字 [start_ms, end_ms]
```

行业侧对照：WhisperX 用 wav2vec2 强制对齐把时间戳做到 **±50ms**，而 Whisper 原生段级时间戳误差是 **±500ms**（<https://github.com/m-bain/whisperX>）。

**CutFlow 现状**：走 MomentShift 的 `POST /v1/audio/transcriptions` with `structured=1`，拿回来的是 `[12.3s] 说话人0: 台词` 的**句级**格式（`PLAN.md §2.1`）。也就是说——**引擎本来有字级能力，被服务封装层丢掉了，然后客户端又用「按字数比例插值」把丢掉的信息猜回来**。这是一个纯粹的信息浪费。

**行动项**：向 MomentShift `asr_server.py` 增补 `char_timestamps=1`（把 Paraformer 的 `timestamp` 字段透传出来），或 CutFlow 侧旁路直取。这与 BACKLOG 中已有的「MomentShift 上游 `_normalize_wav` bug → 开分支提 PR」是同一类动作。

**已知坑（必须写进 ADR）**：FunASR `fa-zh` 强制对齐模型在 VAD 切片上存在**系统性偏移**——issue #2784 实测偏移约 1.3s，原因是它在 VAD 段内做对齐但返回的相对时间未加回切片起点（<https://github.com/modelscope/FunASR/issues/2784>）。所以：**优先用 Paraformer 原生 `timestamp`，谨慎使用 `fa-zh`；若使用必须做切片起点回填 + 偏移自检**。

### 2.3 粗剪：三路检测器的行业实现

| 能力 | 权威实现 | 关键机制 | 可抄什么 |
|---|---|---|---|
| 删死气（silence） | **auto-editor**（WyattBlue，Unlicense） | 基于音频响度的 "first pass"；`--edit audio:threshold=4%`（默认）；`--margin 0.2s` 前后留白防削字 | ①**导出剪辑决策给 NLE（`--export premiere/resolve/final-cut-pro`）而不是直接渲染** —— 这正是 CutFlow 需要的「可审查的 CutList」形态；②`--when-active/--when-silent` 的动作分离模型 |
| 删口头禅 | **Descript** | 转写驱动；四种处理：Delete / **Delete and replace with gap** / Ignore / Remove from transcript only；**Avoid harsh cuts**（分析周边音频，跳过会削到相邻词的填充词） | ①填充词不是只有「删」一种动作；②「会削到邻词就跳过」这条安全策略应成为 CutFlow 的硬校验 |
| 重录/口误 | Vidpal 等工具的做法 | 找时间上邻近的**重复或近似重复短语**，保留**最后一次**完整尝试，切掉较早的 | 「人几乎总是把好的版本说在最后」+「重录前留 1 秒停顿给检测器边界」 |
| 不流畅检测（学术） | Google Research：disfluency = self-correction / repetition / filled pauses（<https://research.google/blog/identifying-disfluencies-in-natural-speech/>）；Shriberg 1994（Switchboard）：10–13 词的句子有 **50%** 概率含不流畅 | BERT-base 逐 token 二分类；小模型 3.1M 参数可本地跑 | 句子越长越容易有 disfluency → **检测阈值应按句长自适应**，而不是全片一个常数 |

**剪后处理（同样重要）**：每一刀都会在静止机位上产生 jump cut，并因为 room tone 被切掉而显得突兀。通行做法是——切点做几毫秒 audio crossfade、或用交替 punch-in / b-roll 覆盖。CutFlow 的 `rs_render` 已有「段间 8ms afade」，这一条**已经做对了**，方案里要明确保留并写进校验。

### 2.4 剪辑交接：把 IR 对齐 OTIO 语义

**OpenTimelineIO（OTIO）** 是 Pixar 发起、现由 Academy Software Foundation 托管的剪辑交接格式，定位是「a modern Edit Decision List (EDL) that also includes an API」（<https://opentimeline.io/>）。

它的能力矩阵（<https://opentimelineio.readthedocs.io/en/v0.15/tutorials/feature-matrix.html>）与 CutFlow IR 对照：

| OTIO 概念 | CutFlow IR | 建议 |
|---|---|---|
| Tracks / Clips | `tracks[].clips[]` ✅ | 语义已对齐 |
| Gaps / Filler | ❌ 无（空白=没有 clip） | **建议引入显式 Gap**——粗剪后 keep 区间之间的空隙需要被表达 |
| Transitions | `clip.transition` ✅（挂前片段） | 与 OTIO 一致 |
| Markers | `markers[]` ✅ | 已有，可用于 HOOK/章节/音效锚点 |
| Nesting | ❌ 无 | 暂不需要（YAGNI） |
| Adapters（FCP7/FCPX/EDL/AAF/Kdenlive） | ❌ 无 | **远期**：一旦 IR 对齐 OTIO 语义，`rs_jy_draft` 之外可以再挂一个 `rs_otio` 适配器，把工程交给 DaVinci Resolve 精修 |

**结论**：不必引入 OTIO 依赖（项目坚持零第三方），但**语义要对齐**——这样"导出给别的 NLE"从"重写"降级为"写个映射"。

### 2.5 增量构建：内容寻址缓存

这不是影视行业概念，是构建系统的成熟范式（Nix / Bazel / ccache）：**缓存键 = hash(输入内容 + 参数 + 工具版本)**，命中即复用产物。

CutFlow 已经天然适配这个范式，因为：

1. `rs_render` 已经是 segment 化（`seg_*/base/composed/mixed/subtitled`）；
2. IR 是声明式的；
3. `scripts/` 全部纯标准库 → 工具版本可用文件 hash 表达。

所以增量不是新架构，是**给现有中间件目录加一份带 hash 的清单**。

### 2.6 一条龙：后期流程的行业阶段命名

影视后期的通行阶段划分（offline edit → online/conform → VFX & graphics → sound design & mix → grade → master → deliverables）给了「一条龙」一个成熟骨架。CutFlow 的 S0–S10 直接借用这个骨架，同时保留用户原话的阶段名（见 §3.6）。

---

## 3. 目标架构 v4

### 3.1 一张图：从「直线」到「带缓存的 DAG」

**现状（直线，任何一处改动 → 全跑）**

```
素材 ─► ASR(句级) ─► 人肉校对 ─┬─► 字幕(按字数插值) ──┐
                              │                      ├─► IR(手写时间) ─► FFmpeg 全量渲染 ─► 自评
                              └─► 人工挑片段 ─────────┘
```

**v4（阶段化 DAG，箭头旁是内容 hash；改 S7 只重跑 S7→S10）**

```
S0 素材 ──► S1 转写+字级对齐 ──► S2 粗剪 ──► S3 基础合成 ──► S4 动画信息 ──► S5 品牌
   │ w0        │ w1                 │ w2          │ w3            │ w4          │ w5
   │           │                    │             │               │             │
   │        wordline.json        cutlist.json   base/         composed/      branded/
   │                                                                             │
   └──────────────────────────── 全部下游共享 map(t_src)→t_final ────────────────┤
                                                                                 ▼
                                        S10 交付 ◄── S9 封面+文案 ◄── S8 自评 ◄── S7 字幕 ◄── S6 音效
                                        (变体矩阵)      metadata      assert      subtitled    mixed
```

### 3.2 机制 A · Wordline：字级对齐时间轴（唯一真相源）

**新增 `05_ir/wordline.json`**，全片每一个字一条记录：

```json
{
  "version": 1,
  "source": "01_materials/JJAV2815.MP4",
  "space": "final",
  "fps": 30,
  "chars": [
    {"i": 0, "ch": "大", "startMs": 880,  "endMs": 1120, "srcStartMs": 880,  "conf": 0.97},
    {"i": 1, "ch": "家", "startMs": 1140, "endMs": 1360, "srcStartMs": 1140, "conf": 0.95}
  ],
  "gaps": [{"after": 41, "ms": 320, "kind": "silence"}],
  "sentences": [{"id": 0, "span": [0, 41], "punc": "。"}]
}
```

**关键设计**：

1. **双坐标**：`startMs`（成片坐标，`space: final`）与 `srcStartMs`（源素材坐标，`space: source`）。这是让「剪辑后仍能对齐」的唯一办法。
2. **三个入口统一落到同一结构**：
   - 口播：Paraformer 原生 `timestamp`（字符级）
   - 纯文案 TTS：合成逐句落盘后 **ffprobe 实测时长**（不是估算），句内用字级对齐模型或 TTS 引擎给出的字级信息
   - 兜底：`fa-zh`（必须做切片起点回填，见 §2.2 的坑）
3. **`conf` 字段**：Paraformer 有字级置信度；低置信字在字幕校对时优先展示给 Agent。
4. **下游一律派生，禁止第二套时间**：
   - 字幕卡的时间 = 其首字 `startMs` 与末字 `endMs`（±20ms 释放余量），**彻底废除「按字数比例插值」**
   - 动画卡入点 = 其挂靠的语义锚点所在字的时间
   - 音效入点 = 锚点字的时间
   - Logo 入场 = S3 起点
5. **重映射函数**：`rs_align.map(t_src) → t_final`，由 CutList 生成的分段线性映射。**所有下游模块必须调它**，不得各自计算。

### 3.3 机制 B · CutList：粗剪决策表（一等产物）

**新增 `04_cut/cutlist.json`**：

```json
{
  "version": 1,
  "source": "01_materials/JJAV2815.MP4",
  "detector": {"version": "cutflow-1.0", "params": {"silenceDb": -32, "minSilenceMs": 600}},
  "cuts": [
    {"id": "c001", "inMs": 12400, "outMs": 13980, "reason": "retake",
     "conf": 0.94, "action": "remove", "note": "第2次尝试，保留后一次(c002)",
     "guard": {"inSilence": true, "outSilence": true, "wordClipped": false}},
    {"id": "c002", "inMs": 26400, "outMs": 26900, "reason": "filler",
     "conf": 0.71, "action": "review", "note": "「那个…那个」，疑似口吃"}
  ],
  "keep": [[0, 12400], [13980, 26400], [26900, 45200]],
  "removedMs": 4180, "srcTotalMs": 45200
}
```

**设计要点**：

1. **`reason` 是封闭枚举**，可统计可审计：`silence` / `breath` / `filler` / `false_start` / `retake` / `stumble` / `repetition` / `off_topic` / `manual`。
2. **`conf` 分级 + `action` 三态**（这是防「粗剪不合格」的核心）：
   - `conf ≥ 0.90` → `action: remove`，自动执行
   - `0.60 ≤ conf < 0.90` → `action: review`，进审查包
   - `conf < 0.60` → `action: keep`，不动
3. **`guard` 三重校验**——任何一刀不通过就不执行，并降级为 `review`：

   | 校验 | 判据 | 依据 |
   |---|---|---|
   | 切点落在静音区 | VAD/能量在该点前后 ±120ms 内有静音 | auto-editor 的 `--margin` 精神 |
   | 不切断词内音素 | 该时刻不落在任何一个字的 `[startMs, endMs]` 内部 | Wordline 字级时间戳 |
   | 不削邻字 | 切点后保留 ≥60ms 释放余量 | Descript 的 "Avoid harsh cuts" |

4. **审查包（`04_cut/review/`）**：每一刀生成一个 `cXXX.wav`（切点前后各 1.5s）+ 一张抽帧 `cXXX.png` + 一行说明，Agent（或用户）可以只听音频就完成 approve/reject。**把「听 20 分钟整片」变成「听 30 个 3 秒片段」。**
5. **反向保护**——防止把修辞停顿删掉：若删除某段后，其前后两个语义单元的间隔从 >700ms 降到 <200ms，则标记 `rhetorical_pause_suspect` 并强制 `review`。

### 3.4 机制 C · Stage Manifest：声明式增量

**新增 `05_ir/pipeline.json` + `_state/` 目录**：

```json
{
  "version": 1,
  "stages": {
    "S1": {
      "status": "done",
      "inputs":  [{"path": "01_materials/a.mp4", "sha1": "9f2c..."}],
      "params":  {"model": "paraformer-zh", "punc": "ct-punc"},
      "tool":    {"rs_align.py": "ab31...", "asr_server": "0.4.2"},
      "outputs": [{"path": "05_ir/wordline.json", "sha1": "77de..."}],
      "ts": "2026-09-10T14:02:11+08:00"
    },
    "S7": { "status": "stale", "staleReason": "S3 输出 hash 变化" }
  }
}
```

**命令设计**：

| 命令 | 语义 |
|---|---|
| `rs_run.py --status` | 打印 S0–S10 状态灯：✓ done / ✗ missing / ⚠ stale（附 staleReason） |
| `rs_run.py --from S3` | 从 S3 起重跑，之前阶段 hash 命中即跳过 |
| `rs_run.py --only S7` | 只跑 S7（前提：其输入 hash 未变） |
| `rs_run.py --dirty` | 只重跑 stale 的阶段 |
| `rs_run.py --explain S7` | 打印 S7 为什么 stale（哪个输入变了） |

**粒度到 segment**：`_state/seg_S3_0007.json` 记录每个 seg 输入的 hash。**改字幕只影响 S7** → 只重新生成 ASS 事件 + 重叠一次，秒级完成；**改背景影响 S3/S4 的若干 seg** → 只重渲这些 seg，然后 concat 复用其余。

> 与现状对比：`compose.md` 那句「手改时可复用 mixed.mkv」从**人肉判断**升级为**声明式缓存**。这是 P3 的全部答案。

### 3.5 机制 D · Variants：一次编辑，多产物

**新增 `05_ir/variants.json`**：

```json
{
  "version": 1,
  "logos": [
    {"id": "brandA", "src": "03_assets/logo_a.png", "anchor": "topRight", "scale": 0.12, "opacity": 0.9, "inMs": 0, "outMs": null},
    {"id": "brandB", "src": "03_assets/logo_b.png", "anchor": "topRight", "scale": 0.12}
  ],
  "ratios": ["9x16", "16x9"],
  "durations": ["full"],
  "matrix": [
    {"id": "A_9x16", "logo": "brandA", "ratio": "9x16"},
    {"id": "B_9x16", "logo": "brandB", "ratio": "9x16"}
  ]
}
```

**渲染策略**：共享中间件，只分叉最后一步。
`base/` `composed/` `mixed/` `subtitled/` 各算一次 → 各变体只做 logo overlay + encode。**2 个 Logo × 2 比例 = 4 个成片，成本约等于 1.3 个。**

**后端能力矩阵**（BACKLOG 已有此 IDEA，此处正式提出）：variants 里每个变体标记 `backends: ["ffmpeg", "jianying59"]`；剪映 5.9 无对应字段的特性（如 `chroma`）自动降级并在交付说明中标注。

### 3.6 机制 E · 阶段注册表（一条龙骨架）

直接对应用户原话的阶段顺序，每阶段给出门禁与可增量性：

| 阶段 | 名称（用户原话） | 主要脚本 | 产物 | 门禁 | 可增量 |
|---|---|---|---|---|---|
| **S0** | 基础素材 | `rs_ingest` | `01_materials/` + `MANIFEST.md` + `brief.md` | 素材可解码、时长可读 | — |
| **S1** | 转写与字级对齐 | `rs_align` | `wordline.json`、`transcript_corrected.md` | 字级覆盖率 ≥99%、`conf` 中位数 ≥0.8 | 换素材才重跑 |
| **S2** | **粗剪处理** | `rs_cut` | `cutlist.json`、`review/` | 所有 `remove` 刀 guard 三项全过 | 只重算受影响刀 |
| **S3** | 基础合成 | `rs_render(step1-4)` | `base/`、`composed/` | IR validate + reframe 锚点确认 | seg 级 |
| **S4** | 动画/其他信息添加 | `rs_anim`（artboard 桥 + 自动落点） | `composed/` | 全部卡片过安全区（ADR-0009） | 卡片级 |
| **S5** | **Logo 添加** | `rs_brand` | `branded/` | logo 不压安全区/不遮字幕 | 变体级 |
| **S6** | **音效添加** | `rs_sfx`（自动落点 + 人工审） | `mixed/` | 音效峰值不掩人声（sidechain 生效） | 音效级 |
| **S7** | 字幕合成 | `rs_subtitle(卡切分 v2)` | `subtitled/` | 断句回归集全绿、CPS 全卡 ≤9 | 卡级 |
| **S8** | 自评 | `rs_bench` + `rs_sync` | `bench_*.png`、`sync_report.md` | 断言全过；对齐偏移中位数 ≤40ms、95 分位 ≤80ms | — |
| **S9** | 封面合成 + **文案生成** | `rs_cover`、`rs_meta` | `cover.png`、`metadata.md/json` | 封面过安全区；标题符合平台字数 | 独立 |
| **S10** | 交付 | `rs_deliver` | 变体矩阵产物 + `deliverables.md` | 清单齐全、`rs_cleanup` dry-run 过 | — |

> **「一条龙」不等于「一口气」**：阶段门禁 + 可中断恢复。用户可以在 S2 停下来改 CutList，也可以在 S7 停下来改一个字，都不用重跑上游。

---

## 4. 五痛点专项方案

### 4.1 P1 · 粗剪

**检测器矩阵（并行跑，融合出 CutList）**

| 检测器 | 输入 | 判据 | reason |
|---|---|---|---|
| 静音 | 音频能量 + VAD | 能量 < -32dB 且持续 ≥600ms | `silence` / `breath` |
| 口头禅 | 字级转写 | 词典：嗯/呃/啊/那个/这个/就是说/然后就是/就…然后 | `filler` |
| 口吃/重复 | 字级转写 | 相邻窗口内字序列近似重复（编辑距离比 ≥0.8，间隔 <3s） | `stumble` / `repetition` |
| **重录** | 字级转写 | 窗口 A 与窗口 B 相似度 ≥0.8 **且** 间隔 <15s **且** B 更完整（字数 ≥ A 且结尾更接近句末）→ 删 A 留 B | `retake` / `false_start` |
| 句内段落作废（可选） | Wordline + Agent 语义 | Agent 读转写判断某段「说跑题了/自我否定」 | `off_topic` |

**阈值自适应**：按 §2.3 的学术依据，长句更容易含 disfluency → 检测阈值随所在句长线性放宽（句长 >25 字时，`filler` 阈值可从 0.85 降到 0.75）。

**执行**：只产 CutList + 渲染 keep 片段序列。**绝不整段重编码**——这既是性能要求，也是「改一刀只重渲受影响片段」的基础。

**验收指标**：
- 在一条含 ≥3 处重录的测试素材上，`retake` 检出率 ≥90%，误删率 = 0（这是硬线：**宁可漏删不可错删**）
- 交付物含 `cut_report.md`：删了多少秒 / 每刀理由 / 可疑项清单

### 4.2 P2 · 三对齐

**根因重述**：不是精度不够，是**时间源不止一个**。

**方案：单向派生链 + 机器断言**

```
src 域                    final 域
wordline(src)  ──map()──► wordline(final) ──┬──► 字幕卡（首/末字时间戳聚合）
                                             ├──► 动画卡入点
cutlist.json ──生成──►  map(t_src)→t_final   ├──► 音效入点
                                             └──► Logo / 章节 / 封面抽帧点
```

**四条具体改动**：

1. **废除比例插值**。字幕卡时间 = 首字 `startMs` 与末字 `endMs`。若一卡只有一个字，最短时长补足到 0.83s（Netflix 最短时长要求）。
2. **TTS 路径用时长的实测值**。`rs_tts` 逐句落盘后 ffprobe 读实际时长写进 manifest（现已接近，明确为硬规则）；句内字级位置由对齐模型给出，不再「按字数摊」。
3. **剪辑采用音视频联合 CutList**。口播素材的音与画同源，CutList 一刀同时作用于两者 → 天然同步，不存在「音频剪了画面没剪」。
4. **新增 `rs_sync` 自校验（这是解决「AI 剪完还得人工调」的关键）**：

   | 检查 | 方法 | 通过线 |
   |---|---|---|
   | 字幕 ↔ 音频 | 对每张卡，取其在成片音频中的时间窗，跑轻量对齐/能量起点检测，统计偏移分布 | 中位数 ≤40ms，95 分位 ≤80ms |
   | 音频 ↔ 画面 | 在每个 CutList 切点，比对音频能量突变帧与画面场景变化帧 | 同帧或差 ≤1 帧 |
   | 卡片 ↔ 字幕带 | 字幕卡时间窗与 S4 动画卡时间窗的重叠检查 | 重叠时字幕按 BACKLOG 方案下沉/降 alpha |
   | 总时长 | ffprobe vs CutList 推算 | ±0.5s（已有） |

   失败即自动修正（平移 `+offset`）或标记，把「人工对轴」变成「阈值告警 + 一键修正」。

**行业对照**：字幕 onset 应在语音起始后 1–2 帧内（<https://subhero.io/blog/subtitle-standards-guide>）。这是通过线，不是"差不多就行"。

### 4.3 P3 · 各阶段可编辑

**改造前后代价对照（这是最能说明问题的一张表）**

| 编辑动作 | 现状 | v4 | 说明 |
|---|---|---|---|
| 改一个字幕文字 | 全跑（20min+） | **秒级** | 只重生成该卡 ASS 事件 + 重叠一次（S7） |
| 改字幕断句（挪一个切点） | 全跑 | **局部 DP 窗口** | 只对受影响的卡及其邻居重跑切分 DP（±3 卡窗口） |
| 调整语句顺序 | 全跑 | 中等 | S2 CutList 重排 → S3 起重跑（但**不重新转写**，S1 命中缓存） |
| 换背景图 | 全跑 | 中短 | 只重渲 S3/S4 中受影响的 seg |
| 换 BGM | 全跑 | 短 | S6 起重跑（混音段） |
| 换 Logo / 加 Logo 变体 | 不支持 | **短** | S5 + S10，其余全部命中缓存 |
| 换 TTS 音色 | 全跑 | 长（合理） | S1 起重跑，但 S0/S2 缓存有效 |

**命令示例**：

```powershell
python skills/cutflow/scripts/rs_run.py --status
# S0 ✓  S1 ✓  S2 ✓  S3 ⚠ stale (compose.md 改了 reframe 默认值)
# S4 ⚠ stale (S3 变化)  S5 ✗  ...  S10 ✗

python skills/cutflow/scripts/rs_run.py --from S3 --explain
python skills/cutflow/scripts/rs_run.py --only S7      # 只重做字幕
python skills/cutflow/scripts/rs_run.py --dirty        # 只跑 stale
```

**反模式警告**：hash 必须涵盖**工具文件 hash + 参数 + 上游产物 hash** 三者。只 hash 输入文件会导致「改了代码但缓存命中」的幽灵 bug——这是增量系统最常见也最致命的一类故障。

### 4.4 P4 · 字幕断句

**先分离两层**（现状把它们混在一起了）：

| 层 | 问题 | 现状 | v4 |
|---|---|---|---|
| **卡切分** segmentation | 一句话切成几张卡 | 长度驱动（≤16 字） | **约束最优 DP** |
| **行断开** line break | 一张卡内怎么折行 | 已有评分算法 ✅ | 保留，但补充「金字塔形、禁顶行 1–2 字」 |

**卡切分算法（DP / 加权有限状态）**

1. **候选边界集合**（只有这些位置可以切）：
   - 强标点：`。！？；`
   - 弱标点：`，、：`
   - **字级停顿**：Wordline 中相邻字之间 `gap ≥ 200ms`
   - 句法线索：连词/时间副词之前（「然后」「所以」「但是」「接下来」）
2. **禁止边界**（硬约束，直接排除）：
   - 专有名词内部（brief 术语表 + NER 名单：「长江」「抖音」「GPT-SoVITS」）
   - 成语/固定搭配内部（可维护一个常用成语表，或至少做 4 字整体保护）
   - 数量词 + 量词/单位之间（「三十五」/「岁」、「三千」/「万」）
   - 数字与单位、百分号、货币符号之间（「9」/「秒」、「¥」/「199」）
   - `的 / 地 / 得 / 了 / 着 / 之` 之后
   - ASCII 词内部（已有）
3. **打分函数**（越大越好）：

   ```
   score(cut) =
       + 2.0 * 标点层级(。！？=1.0, ；=0.8, ，=0.6, 、=0.4, 无=0.0)
       + 1.5 * 归一化停顿( gapMs / 500ms , clip 到 [0,1] )
       + 1.0 * 语义完整性(不以虚词结尾 +0.5, 不以连词开头 +0.5)
       - 0.8 * 长度失衡惩罚( |chars_a - chars_b| / max_chars )
       - 1.2 * 尾卡过短惩罚( 卡字数 < 4 时线性惩罚 → 防「悬一字」)
   ```

4. **硬约束**（不满足即该切分方案非法）：

   | 约束 | 值 | 依据 |
   |---|---|---|
   | 每卡字数 | 9:16 **10–12 字**（从 16 下调）；16:9 20–22 字 | Netflix 16 字是横屏；竖屏 CJK 建议 8–10 字 |
   | CPS | ≤ **9 字/秒**（成人）；儿童 ≤7 | Netflix 简体中文 |
   | 单卡时长 | **[0.83s, 7s]** | Netflix min 5/6s / max 7s |
   | 卡间距 | ≥ 2 帧 | 行业通行 |
   | 视觉节拍 | 卡时长宜落 **1.5–3.5s**；>4s 必切，<0.8s 必并 | ADR-0004 的 2–3s 节拍 |

5. **多假设输出**：DP 求最优后，返回 **top-3 候选**（按 score 排序）供 Agent 挑选；若最优解与次优解 score 差 <5%，标记 `ambiguous` 并请 Agent 决断——这比"悄悄选一个错的"好得多。
6. **回归测试集（必须固化，这是质量的量化手段）**：

   | 用例 | 期望 | 防护机制 |
   |---|---|---|
   | `滚滚长江东逝水` | 不得切成「滚滚长」/「江东逝水」 | 专名表 + 尾卡过短惩罚 + 完整句优先 |
   | `我今年三十五岁` | 不得切成「我今年三十」/「五岁」 | 数量词+单位保护 |
   | `我们把那个…那个什么…对，做完了` | 删口水词后重新对齐，不产生空卡 | 轻改写 → Wordline 重聚合 |
   | `¥1999 元` | 不切 | 数字+单位保护 |
   | `用 GPT-SoVITS 做配音` | 不切 ASCII 词 | 已有规则 |

**「节奏感」的正解**：节奏不是靠"断得短"产生的，是靠**卡时长落在视觉节拍区间**（1.5–3.5s）产生的。「滚滚长江东逝水」作为完整的 7 字诗句，一张卡 2s 念完，符合节拍；切成 3+4 反而破坏语感。**长度约束服务于节拍，而不是反过来。**

### 4.5 P5 · 一条龙

见 §3.6 阶段表。此处只补充**三块空白的具体设计**：

**S5 · 品牌层（Logo）**
- 新增 `03_assets/branding/logos/`，每个 Logo 一个条目（`id/path/anchor/scale/opacity/safeArea`）
- 落点：`anchor ∈ {topLeft, topRight, bottomLeft, bottomRight, watermark}`；**默认避开字幕带**（9:16 底部 25%，ADR-0009）
- 变体：`variants.json` 的 `logos[]` × `ratios[]` 笛卡尔积 → 共享中间件
- 可选：片头/片尾板也归 S5（artboard 生成）

**S6 · 音效自动落点（把"手挂"变成"建议 + 审"）**

| 落点类型 | 触发条件 | 音色 |
|---|---|---|
| 转场 | CutList 切点 / 卡片切换点 | whoosh / swipe |
| 强调 | 关键词高亮词出现处 | ding |
| 列举 | 卡片内出现「第一/第二/第三」序号 | click |
| 章节 | `markers[]` 的章节标记 | riser / bell |
| 结尾 | 最后一张卡片 / 片尾 | bell |

规则：**每 15s 内音效 ≤2 个**（防噪）；Agent 出草案进 IR 的 `audio.clips[]`（`role: sfx`），用户在审查包里可逐条删。

**S9 · 文案生成**

新增 `rs_meta.py`：输入 `wordline` + `brief` + `cut_report`，输出 `metadata.json` + `metadata.md`：

| 平台 | 标题 | 简介 | Tag |
|---|---|---|---|
| 抖音 | ≤30 字，前 8 字含钩子 | ≤55 字 | 5 个话题（# 开头） |
| B站 | ≤80 字 | 分段简介 + 时间戳章节（由 `markers[]` 生成） | 10 个 tag |
| 视频号 | ≤22 字 | ≤120 字 | 3–5 个 |

关键：**章节时间戳直接从 `markers[]` 生成**——因为 markers 本来就跟着 Wordline 走，剪辑后自动正确。这是"一条龙"真正省事的地方：文案不再是另起一套时间。

---

## 5. 数据契约（新增文件一览）

```
<工程>/
├── 00_brief/brief.md                     （已有）
├── 01_materials/                          （已有）
├── 02_sensed/transcript_corrected.md      （已有）
├── 04_cut/                                ★ 新增
│   ├── cutlist.json                       ★ 粗剪决策表
│   ├── cut_report.md                      ★ 人读报告
│   └── review/cXXX.{wav,png}              ★ 审查包
├── 05_ir/
│   ├── project.json                       （已有，IR）
│   ├── wordline.json                      ★ 字级对齐时间轴（唯一真相源）
│   ├── variants.json                      ★ 变体矩阵
│   └── pipeline.json                      ★ 阶段清单
├── _state/S*.json, seg_*.json             ★ 缓存状态
└── 06_output/
    ├── final_<variantId>.mp4              ★ 变体产物
    ├── metadata.json / metadata.md        ★ 标题/简介/Tag
    ├── sync_report.md                     ★ 对齐自检报告
    └── deliverables.md                    ★ 交付清单
```

**CutList → IR 的桥**：`rs_ir.py build --from-cutlist 04_cut/cutlist.json` 自动生成 `project.json` 的 video/audio 主轨（keep 区间 + map 后的时间），Agent 只需补 overlay/字幕/音效。**这一步把「手写毫秒」的活彻底消掉。**

---

## 6. CLI 设计汇总

| 命令 | 语义 | 幂等 | 阶段 |
|---|---|---|---|
| `rs_align.py <媒体>` | 转写 + 字级对齐 → wordline.json | ✅ | S1 |
| `rs_cut.py <wordline> --detect all` | 检测 → cutlist.json（草案） | ✅ | S2 |
| `rs_cut.py --review-pack` | 生成审查包 | ✅ | S2 |
| `rs_cut.py --apply cutlist.final.json` | 生成 keep 区间 + 渲染序列 | ✅ | S2 |
| `rs_ir.py build --from-cutlist ...` | CutList → IR 主轨 | ✅ | S3 |
| `rs_brand.py --variants variants.json` | Logo 变体渲染 | ✅ | S5 |
| `rs_sfx.py --auto` | 音效自动落点（草案） | ✅ | S6 |
| `rs_subtitle.py --segment dp` | DP 卡切分（新） | ✅ | S7 |
| `rs_sync.py <成片> --wordline ...` | 三对齐自检 | ✅ | S8 |
| `rs_meta.py --platform douyin` | 标题/简介/Tag 生成 | ✅ | S9 |
| `rs_run.py --status / --from / --only / --dirty / --explain` | 增量编排 | ✅ | 全 |

全部沿用现有 CLI 协议：`--json` 输出 `{ok, code, message, data}`，退出码 0/2/3/4。

---

## 7. 落地路线（4 批次，可独立验收）

### 批次 A · 对齐地基（最高杠杆，先做）— 体量 L

1. `rs_align`：取 Paraformer 原生字级 `timestamp`（含 MomentShift 上游扩展）；产出 `wordline.json`
2. `rs_cut` 的最小版：**只做静音 + 填充词**（先不做 retake），产出 CutList + 审查包 + guard 三项校验
3. `map(t_src) → t_final` 重映射函数（`rs_align` 内）
4. `rs_subtitle` 改为**从 Wordline 聚合卡片时间**，删除比例插值
5. `rs_sync` 最小版：字幕 ↔ 音频偏移统计

**验收**：用 JJAV2815 跑通，`sync_report.md` 显示字幕偏移中位数 ≤40ms；CutList 的 `remove` 刀 guard 通过率 100%。

> 为什么先做这个：P1/P4 的效果全建立在"时间是对的"之上。这一步不做，后面所有优化都在漂移的地基上打磨。

### 批次 B · 增量引擎 — 体量 M

1. `pipeline.json` + `_state/` 的 hash 计算（含工具文件 hash）
2. `rs_run.py --status / --from / --only / --dirty / --explain`
3. `rs_render` 的 seg 级 hash 接入（结构已现成，只需挂清单）
4. 单卡字幕重渲路径（只重生成该卡 ASS 事件 + 重叠）

**验收**：改一个字幕 → 端到端墙钟时间 ≤10s；`--status` 能正确解释 stale 原因；重复跑 `--from S3` 在无改动时全部命中缓存（≤2s）。

### 批次 C · 断句 v2 — 体量 M

1. DP 卡切分 + top-3 候选 + `ambiguous` 标记
2. 竖屏字数从 16 下调到 10–12，并引入 CPS/时长/间距硬约束
3. 回归测试集（§4.4 表格 5 条）写成 pytest
4. `textopt` 的断行算法保留，补「金字塔形、禁顶行 1–2 字」

**验收**：回归集全绿；随机抽 20 张卡，CPS 全部 ≤9；「滚滚长江东逝水」类专名用例 0 误切。

### 批次 D · 一条龙补齐 — 体量 M–L

1. S5 品牌层 + `variants.json` + 变体渲染
2. S6 音效自动落点（草案 + 审查）
3. S9 `rs_meta.py` 文案生成（含 B站章节时间戳）
4. `rs_run --status` 覆盖 S0–S10；`deliverables.md` 生成

**验收**：一条素材 → 2 Logo × 2 比例 = 4 个成片 + 封面 + 文案，全流程 1 条命令编排；总成本 ≤1.5 倍单变体耗时。

**依赖关系**：批次 A 是 B/C/D 的前提（无 Wordline 则增量无从判断脏值，DP 也没有韵律边界）；B 与 C 可并行；D 依赖 B（变体共享中间件）。

---

## 8. 风险与反模式

| 风险 | 影响 | 对策 |
|---|---|---|
| 粗剪误删（最严重） | 删掉不可恢复的内容 | 三级置信度 + guard 三重校验 + **宁可漏删不可错删** + 审查包强制过目 |
| `fa-zh` 系统性偏移 | 字级时间全偏 ~1.3s | 优先 Paraformer 原生 timestamp；用 fa-zh 必须回填 VAD 切片起点 + 偏移自检（FunASR issue #2784） |
| hash 不含工具版本 | 「改了代码但缓存命中」幽灵 bug | 缓存键 = 输入 hash + 参数 + **脚本文件 hash** + 外部服务版本 |
| 增量把管线拆碎 | 100 个小脚本，可读性崩溃 | 阶段边界按「可独立验证」划分，保持 ≤12 个阶段；每阶段单一入口脚本 |
| 断句 DP 过度工程 | 规则堆到没人敢改 | 硬约束 ≤8 条，评分项 ≤6 项；一切靠回归测试集约束，不靠直觉 |
| 剪映后端能力不对齐 | 变体在剪映里缺字段 | 变体矩阵标记 `backends`；缺失特性降级并在交付说明标注（BACKLOG 已有 IDEA） |
| 竖屏字数下调引发返工 | 旧工程字幕变多卡 | **仅在批次 C 生效**；旧工程 `pipeline.json` 记录当时的 `maxChars` 参数，保证可复现 |
| 一次做太多 | 每批都半成品 | 每批独立验收 + 独立 tag；批次 A 未通过验收不进 B |

**要避免的反模式（写进 SKILL.md 反模式一节）**：

- ❌ 不要用「按字符数比例插值」推导任何时间——这是当前所有对齐问题的元凶
- ❌ 不要在任何下游模块里自己算时间——一律调 `map()`
- ❌ 不要让增量缓存的粒度粗于 segment——粗了没收益，细了管理成本爆炸
- ❌ 不要为了通过验收而放宽 guard，收紧检测器阈值即可

---

## 9. 验收清单（可直接抄进 BACKLOG）

| # | 验收项 | 通过线 | 批次 |
|---|---|---|---|
| 1 | 字级对齐产出 | `wordline.json` 字覆盖率 ≥99%，`conf` 中位数 ≥0.8 | A |
| 2 | 字幕↔音频偏移 | 中位数 ≤40ms，95 分位 ≤80ms | A |
| 3 | 音画切点一致 | 100% 切点同帧或差 ≤1 帧 | A |
| 4 | 粗剪保守性 | 含 ≥3 处重录的素材：retake 检出 ≥90%，**误删 = 0** | A |
| 5 | 粗剪收益 | 长口播素材时长减少 20–35% | A |
| 6 | 单卡改字耗时 | 端到端 ≤10s（对照：改造前 20min+） | B |
| 7 | 缓存幂等 | 无改动重跑 `--from S3`，全程命中，≤2s | B |
| 8 | stale 可解释 | `--explain` 能指出具体变化的输入 | B |
| 9 | 断句回归 | §4.4 五条用例全绿 | C |
| 10 | CPS 合规 | 抽样 20 卡，CPS 全部 ≤9 字/秒 | C |
| 11 | 竖屏可读性 | 9:16 每卡 ≤12 字，无顶行 1–2 字 | C |
| 12 | Logo 变体 | 2 Logo × 2 比例 = 4 成片，总耗时 ≤1.5× 单变体 | D |
| 13 | 文案产出 | 含标题/简介/Tag，B站含章节时间戳且章节时间与成片一致 | D |
| 14 | 一条龙 | S0–S10 全绿，`deliverables.md` 齐全 | D |

---

## 10. 参考来源

**字幕规范**
- Netflix Chinese (Simplified) Timed Text Style Guide — <https://backlothelp.netflix.com/hc/en-us/articles/215986007-Simplified-Chinese-PRC-Timed-Text-Style-Guide>
- BBC Subtitle Guidelines（分句/断句/WPM/定位）— <https://www.bbc.com/accessibility/forproducts/guides/subtitles/>
- Subtitle Standards Guide: Netflix, BBC & Amazon（CJK 与竖屏参数汇总）— <https://subhero.io/blog/subtitle-standards-guide>
- Subtitle Reading Speed — CPS & WPM Standards — <https://fixsubtitles.com/guides/subtitle-reading-speed>
- Characters-Per-Second Limits by Language — <https://www.versely.studio/blog/characters-per-second-limits-by-language>

**语音对齐**
- WhisperX: word-level timestamps via forced alignment（±50ms vs 段级 ±500ms）— <https://github.com/m-bain/whisperX>
- FunASR: Speech-to-Text with Word/Character-Level Timestamps（Paraformer 原生字符时间戳）— <https://funasr.com/en/blog/speech-to-text-timestamps-python.html>
- FunASR issue #2784: `fa-zh` 强制对齐时间戳系统性偏移 — <https://github.com/modelscope/FunASR/issues/2784>
- WhisperX 技术报告 — <https://arxiv.org/abs/2303.00747>

**粗剪 / 不流畅检测**
- Auto-Editor（死气删除；导出 NLE 决策而非渲染）— <https://github.com/WyattBlue/auto-editor>
- Descript: Remove Filler Words（四种处理 + Avoid harsh cuts）— <https://help.descript.com/script-editing/filler-words>
- Google Research: Identifying Disfluencies in Natural Speech — <https://research.google/blog/identifying-disfluencies-in-natural-speech/>
- Awesome Disfluency Detection（论文索引）— <https://github.com/pariajm/awesome-disfluency-detection>

**剪辑交接**
- OpenTimelineIO — <https://opentimeline.io/>
- OTIO Feature Matrix — <https://opentimelineio.readthedocs.io/en/v0.15/tutorials/feature-matrix.html>
- SMPTE 258M-2004 (CMX3600 EDL) — <http://xmil.biz/EDL-X/CMX3600.pdf>

**项目内部依据**
- `skills/cutflow/SKILL.md`、`rules/{sense,subtitles,compose,intake}.md`
- `docs/adr/0001~0010`、`docs/BACKLOG.md`、`docs/PLAN.md`

---

## 附：本提案与现有 ADR 的关系

| 提案项 | 动作 | 影响 |
|---|---|---|
| Wordline 作唯一真相源 | **新增 ADR-0011** | 修订 ADR-0002（轻改写的时间处理）与 `sense.md §4`（"只改文本不动时间戳"需改为"改文本并按字级锚点重聚合"） |
| CutList 与粗剪阶段 | **新增 ADR-0012** | 修订 `SKILL.md §1` 管线总览（插入 S2）、新增 `rules/roughcut.md` |
| 阶段缓存与增量编排 | **新增 ADR-0013** | 改写 `compose.md` 的「中间件」一节（人肉 → 声明式） |
| 竖屏字数 16 → 10–12 | **修订 ADR-0001** | 需说明依据（Netflix 16 字为横屏标准，竖屏按屏宽比例下调） |
| 卡切分 DP | **修订 ADR-0001/0002** | 卡切分与行断开两层分离 |
| Logo 变体矩阵 | **新增 ADR-0014（或并入 ADR-0007）** | 与工程归档命名规范联动，产物命名需含 variantId |
