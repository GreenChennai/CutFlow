# Roughcut — 粗剪与 CutList(S2)

> **ADR-0012**。一句话:**粗剪的产物不是一段新视频,而是一张可读、可改、可审查的决策表(`cutlist.json`)。**

## 1. 为什么需要它

旧管线里 `SKILL.md §1` 的路由是 `口播视频 → rs_asr → 校对 → 字幕`——**没有 cut 这一步**。原始素材被当作"已经拍好的成片"直接进入合成;`cutlist` / `retake` / `filler` / `silence` 这些词在整个仓库零出现。这不是参数没调好,是**阶段不存在**。

直接后果:
- 一段 30 分钟的口播,含 8 分钟口误/重录/长停顿,人只能靠耳朵一句句听——AI 帮不上忙;
- 素材越啰嗦,后面 S7 字幕、S6 音效、S3 渲染的成本全部按原始时长计费。

**粗剪是整条管线里性价比最高的一步**:典型口播素材可裁掉 **20–35%** 时长,且后面每一步都变便宜。

## 2. 决策表结构(`04_粗剪决策/cutlist.json`)

```json
{
  "version": 1,
  "source": "01_原始素材/JJAV2815.MP4",
  "detector": {"version": "cutflow-1.0", "params": {"silenceDb": -32, "minSilenceMs": 600}},
  "cuts": [
    {"id": "c001", "inMs": 12400, "outMs": 13980, "reason": "retake",
     "conf": 0.94, "action": "remove", "note": "第2次尝试,保留后一次(c002)",
     "guard": {"inSilence": true, "outSilence": true, "wordClipped": false, "tailKeepMs": 120}},
    {"id": "c002", "inMs": 26400, "outMs": 26900, "reason": "filler",
     "conf": 0.71, "action": "review", "note": "「那个…那个」疑似口吃"}
  ],
  "keep": [[0, 12400], [13980, 26400], [26900, 45200]],
  "removedMs": 4180, "srcTotalMs": 45200
}
```

- `keep` 由 `cuts` 中 `action == "remove"` 的区间**自动推导**,不得手写;`rs_cut.py` 的 `--apply` 会重算并校验连续性(必须严格覆盖 `[0, srcTotalMs]` 且不重叠)。
- `removedMs / srcTotalMs` 用于验收:裁剪比应落在 **20–35%**(口播长素材);偏离过大时如实上报,不硬凑。

### 2.1 keep 末段终点保底(P26-3,副文档 07)

keep 末段终点 = `max(末字 endMs + 尾余量, ffprobe 实测时长)`,**口播结尾留 0.5–0.8s 自然底噪是不割裂的最低要求**(20260920 实测:keep 终点被钉在 ASR 钳制出的错误总时长上,末字衰减 + 0.7s 底噪全被切掉,成片在字尾瞬间硬停)。

- 尾余量默认 **650ms**(`rs_cut --tail-reserve-ms`,建议区间 500–800);
- 给了 `--media` 时以 ffprobe 实测为唯一真相(物理上限);无实测时退回 `max(记录总时长, 末字+尾余量)`——记录值被字尾钳制时也能自动多留一个尾余量;
- 保底触发时 cutlist 写入 `tail` 块(`reserveMs / measuredMs / recordedTotalMs / keepEndMs`)留痕,`srcTotalMs` 同步为有效源时长。

### 2.2 时长账自动同步(P27,副文档 07)

**`rs_cut.py` 的 `--apply` 是时长账同步的单一入口**:重算 keep 之后自动把工程 wordline(`05_时间线工程/wordline.json`,或 `--wordline` 显式指定)的三个时长字段改平——`srcDurationMs = cutlist.srcTotalMs`、`removedMs = cutlist.removedMs`、`finalDurationMs = src − removed`——**不再需要手改 wordline 两个字段**。

- **恒等式**(P27-2):`srcDurationMs − removedMs == finalDurationMs`,任一环节写盘后校验;`rs_ir build --from-cutlist` 在 S3 入口把「改 keep 但 wordline 未同步」判为 `DURATION_LEDGER` 硬失败并给出修复命令,不等 `rs_sync` 事后挂红叉;
- **手改护栏**(B8 同源):wordline 带 `manualEdit` 痕迹时 `--apply` 拒绝自动改写(`WORDLINE_MANUAL_EDIT`),确认放弃手改加 `--force`,或只改时长用 `rs_align.py refresh-durations --media <素材>`。

## 3. `reason` 是封闭枚举

合法值(封闭枚举,共 10 项):

`silence` / `breath` / `filler` / `false_start` / `retake` / `stumble` / `repetition` / `off_topic` / `manual` / `waiting`

> M8 新增 `waiting`(录屏教程等待段,方案 §5.5.4 screen.compress):≥2.0s 无视觉变化且无语音 → 加速 2–8× 或删除;见 §4 检测器表。

> `rs_cut.py` 对未知 `reason` 直接报错(退出码 2)。枚举的意义是**可统计、可审计、可回归**:每轮迭代能看「retake 检出率」「filler 误删率」怎么变。

## 4. 三路检测器(并行跑,融合出 CutList)

| 检测器 | 输入 | 判据 | reason |
|---|---|---|---|
| **静音** | 音频能量 + VAD | 能量 < **-32 dB** 且持续 ≥ **600 ms** | `silence` / `breath` |
| **口头禅** | 字级转写 | 词典:`嗯/呃/啊/那个/这个/就是说/然后就是/就…然后` | `filler` |
| **口吃/重复** | 字级转写 | 相邻窗口字序列近似重复(编辑距离比 ≥ 0.8,间隔 < 3s) | `stumble` / `repetition` |
| **重录** | 字级转写 | **滑动窗口内任意两句**(句数 ≤6 或间隔 ≤30s)相似度 ≥ 0.8 **且** B 更完整 → **一刀删掉全部旧尝试,留最后一次**(v0.7.0 起跨句) | `retake` |
| **整段重来** | 字级转写 | 连续 ≥ **8 字逐字相同**且再次出现 → 删旧块留新块 | `false_start` |
| **无语音长段** | 素材音频能量(`silencedetect`)或字间 gap | 无有效语音 ≥ **1.2s**(调整仪容 / 换提词器) | `silence` / `breath` |
| **元话语** | 字级转写 | 词典:`说错了/重新说/再来一遍/这段不算/重来一遍` | `off_topic`(仅 `review`,不自动删) |
| **等待段(录屏)** | `rs_screen` 的 `screen.json`(优先)或 `--media` 内联帧差+静音检测 | ≥ **2.0s** 无视觉变化且无语音;区间内有转写字即整段不删(防误删人声) | `waiting` |
| 跑题段落(可选) | Wordline + Agent 语义 | Agent 读转写判断「说跑题了/自我否定」`--off-topic 起-止` | `off_topic` |

> **v0.7.0 修订(OPTIMIZATION-v7 #3)**:旧实现只比**相邻两句**、间隔 <15s,而"说完一段/调整后再重来"常跨 2–3 句、隔得更久 → 大量漏检;且每次只删紧邻前一次(同一段录三次会残留中间那次)。现在按滑动窗口两两比对,并把连续重录刀**串成一刀**(避免两个旧尝试之间留几十毫秒碎片),出点取「最后一次尝试起点 − 60ms」留出自然起音。
> **`dead_air`**:给 `--media <源素材>` 时用 ffmpeg `silencedetect` 探测音频能量(能抓到"ASR 却把静音写成了文本"的情况);不给则退回字间 gap(只认 ≥1.2s 空档)。

### 4.1 阈值随句长自适应

学术依据:不流畅(disfluency)= 自我纠正 / 重复 / 填充停顿;Shriberg 1994(Switchboard)发现 **10–13 词的句子有 50% 概率含不流畅**——**句子越长越容易含 disfluency**。所以阈值不能全片一个常数:

- 所在句长 > 25 字时,`filler` 阈值从 0.85 放宽到 **0.75**(更容易被标为 `review`,而不是更容易被删);
- 放宽只影响**进审查包的比例**,不影响 `remove` 的门槛——**宁可多审,不可错删**。

### 4.2 人几乎总把好的版本说在最后

重录检测的默认策略:**保留后一次完整尝试,切掉较早的**。这条启发式来自行业工具(Vidpal 等)的通行做法,但必须过 §5 的 guard。

## 5. `guard` 三重校验(防误删的核心)

**任何一刀不通过即不执行,自动降级为 `action: "review"`,并在 `note` 里写明未过的项。**

**v0.7.0:guard 按 `reason` 分档(OPTIMIZATION-v7 #3)。** 重录/整段重来的切点本就紧邻语音,若坚持要求它落在静音区 = 永远无法 `remove`(用户观感就是"废片段没剪掉")。故:

| `reason` | 硬过项 | 说明 |
|---|---|---|
| `silence` / `breath` / `filler` | 静音区 + 不切断字内音素 + 后留 ≥60ms(全四项) | 不变 |
| `retake` / `false_start` / `stumble` / `repetition` / `off_topic` / `manual` / `waiting` | **不切断字内音素 + 后留 ≥60ms** | 静音两侧降为**告警项**(不阻断);`waiting` 段按判据无语音,inSilence/outSilence 天然满足,防误删靠检测期「区间内有转写字即整段放弃」 |

> **`wordClipped`(不切断字内音素)在任何 `reason` 下都是硬过项,永不放松** —— 这是"宁可漏删,不可错删"的底线。
> `guard` 同时给出 `ok`(四项全过,保守口径)与 `okByReason`(该 reason 的硬过项);`classify` 用后者,`cut_report.md` 分列「硬过项 / 告警项」。

| 校验 | 判据 | 依据 |
|---|---|---|
| `inSilence` / `outSilence` | 切点前后 ± **120ms** 内存在静音(VAD 或能量判据) | auto-editor `--margin` 的"留白防削字"精神 |
| `wordClipped` | 该时刻**不落在任何一个字的 `[startMs, endMs]` 内部** | Wordline 字级时间戳 |
| `tailKeepMs` | 切点后保留 ≥ **60ms** 释放余量 | Descript 的 **"Avoid harsh cuts"**(分析周边音频,会削到相邻词就跳过) |

### 5.1 反向保护(防止把修辞停顿删掉)

删掉某段后,若其**前后两个语义单元的间隔从 > 700ms 降到 < 200ms**,说明这一刀删掉的是一个有表达作用的停顿 → 标记 `rhetorical_pause_suspect`,**强制 `review`**。

### 5.2 protect 保护区(阶段六 J2:guard 第四条)

`cutlist.protect` 是**被明确"必须保留发音"的词的有效发音区间**清单(单位与 keep 一致,ms,左闭右开):

```json
"protect": [{"startMs": 1200, "endMs": 1580, "note": "术语「蓝屏」必须完整"}]
```

- **guard 加第四条:切点不得侵入 protect 区**(remove 区间与任一 protect 区相交即冲突;触边不算侵入,半开区间判定)。
- **优先级 protect > 既有 guard 三项**:guard 不过尚可降级 `review`(留稿待人审),protect 冲突**即报错而非降级**(`PROTECT_INTRUDED`,退出码 2)——protect 的语义是"审也不许切",必须显式解决冲突:改刀,或撤区。
- 双闸:检测/构建期(`build_cutlist`,审 remove+review 候选)与 `--apply`/`finalize`(审真正执行的 remove,防人工改刀绕过)都复验。
- CLI:`rs_cut.py 05_时间线工程/wordline.json --detect all --out 04_粗剪决策 --protect 1200-1580,2000-2800`;protect 区随 cutlist 落盘(`cl["protect"]`),可审计、可复验。
- 与专项 07 共存:protect 只**收紧**可删区间,不改 `derive_keep`/时长账/尾余量保底——被保护区天然落在 keep 里。

## 6. `conf` 三级 → `action` 三态

| conf | action | 行为 |
|---|---|---|
| ≥ **0.90** | `remove` | 自动执行(仍须过 guard) |
| **0.60 – 0.90** | `review` | 进审查包,等 Agent/用户批 |
| < **0.60** | `keep` | 不动 |

> 这三档是「防粗剪不合格」的核心机制。**硬线:宁可漏删,不可错删。** 一条素材的错删是不可恢复的内容损失,漏删只是多花几秒。

## 7. 审查包(`04_粗剪决策/review/`)——把「听 20 分钟」变成「听 30 个 3 秒」

每个 `review` 刀生成:

| 文件 | 内容 |
|---|---|
| `cXXX.wav` | 切点**前 1.5s + 后 1.5s** 音频 |
| `cXXX.png` | 切点帧(可选,视觉确认) |
| `cXXX.md` | 一行说明:`reason` / `conf` / `note` / guard 结果 |

Agent(或用户)可以**只听音频**就完成 approve / reject——这是粗剪从「AI 猜」变成「人机协作」的关键形态。

产出汇总 `04_粗剪决策/cut_report.md`:

```markdown
# 粗剪报告 · <工程名>
- 源时长 45.2s → 保留 41.0s(裁掉 9.2%)
- 自动执行 remove:2 刀 / 待审 review:3 刀 / 未动 keep:41 刀
- 按 reason:retake 2、filler 3
- 可疑项:rhetorical_pause_suspect 1(见 c007)
```

## 8. 执行方式

**只产 CutList + 渲染 keep 片段序列,绝不整段重编码。** 这既是性能要求,也是「改一刀只重渲受影响片段」的基础。

```powershell
rs_cut.py 05_时间线工程/wordline.json --detect all --out 04_粗剪决策
rs_cut.py --review-pack 04_粗剪决策/cutlist.json
# 人工批注:把 cutlist.json 复制为 cutlist.final.json,改 action / 删条目
rs_cut.py --apply 04_粗剪决策/cutlist.final.json --render
```

`--apply` 负责:重算 `keep` → **自动同步 wordline 时长账**(`srcDurationMs/removedMs/finalDurationMs`,P27,见 §2.2)→ 供 `rs_align.py remap` 与 rs_ir.py 的 build --from-cutlist 生成 IR 主轨。

### 剪后衔接(必须保留)

每一刀都会在静止机位上产生 jump cut,并因 room tone 被切掉而显得突兀。v0.10 起 rs_ir 写的 8ms 亚帧转场会被 rs_render **自动提升为 120ms 交叉溶解**(joinCrossfadeMs,ADR-0023,尾帧扩展保证零漂移)——溶解只该表达"时间/话题切换",同段内跳切的掩饰应优先 punch-in/B-roll(见 docs/ITERATION-GUIDE-v0.11.md §5)。

## 8.5 v0.11 增强(R4/R5,ITERATION-GUIDE §4)

- **词表外置**:`templates/fillers.json`(fillers/repeat_words/self_negative),brief 阶段补用户口癖;`rs_cut` 每次运行读取,报告按词统计命中。
- **margin 不对称**:静音切点前留 150ms / 后留 300ms(auto-editor `--margin` 不对称语义)——后留白给呼吸感。
- **防碎切(smooth)**:相邻刀间隙 <100ms 的保留碎片并刀;<120ms 的碎刀整体放弃(亚音素剪切只添错删风险;宁可漏删)。
- **hesitate 检测器**(需 `--media`):0.3–1.2s 无字段能量谷 → review(conf 0.62);谷里有字保守不切。
- **R5 文本化**:每刀带 `text`(前后 1.2s 上下文);`cut_report.md` 尾部产出**删改稿**——全文按 ~~remove~~/**review**/keep 分行,先读稿再听审。

## 9. 门禁与验收

| 检查 | 通过线 |
|---|---|
| **误删率** | **= 0**(硬线) |
| **protect 侵入**(J2) | **= 0**:`remove`/`review` 候选切点不落入任何 protect 区;冲突必须报错解决,不得降级放行 |
| retake 检出率 | 含 ≥3 处重录的测试素材上 ≥ 90%(含**跨句**重录) |
| guard 通过率 | 所有 `remove` 刀过其 `reason` 对应硬过项(100%);`wordClipped` 永不放松 |
| 裁剪收益 | 长口播素材时长减少 20–35% |
| 中间段残留(v0.7.0 新增) | 人工抽验:调整/重来片段残留 ≤1 处 / 10 分钟素材 |
| 报告齐全 | `cut_report.md`(分列硬过项/告警项)+ 每刀 `review/cXXX.*` 存在 |
