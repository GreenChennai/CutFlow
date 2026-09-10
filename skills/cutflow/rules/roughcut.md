# Roughcut — 粗剪与 CutList(S2)

> **ADR-0012**。一句话:**粗剪的产物不是一段新视频,而是一张可读、可改、可审查的决策表(`cutlist.json`)。**

## 1. 为什么需要它

旧管线里 `SKILL.md §1` 的路由是 `口播视频 → rs_asr → 校对 → 字幕`——**没有 cut 这一步**。原始素材被当作"已经拍好的成片"直接进入合成;`cutlist` / `retake` / `filler` / `silence` 这些词在整个仓库零出现。这不是参数没调好,是**阶段不存在**。

直接后果:
- 一段 30 分钟的口播,含 8 分钟口误/重录/长停顿,人只能靠耳朵一句句听——AI 帮不上忙;
- 素材越啰嗦,后面 S7 字幕、S6 音效、S3 渲染的成本全部按原始时长计费。

**粗剪是整条管线里性价比最高的一步**:典型口播素材可裁掉 **20–35%** 时长,且后面每一步都变便宜。

## 2. 决策表结构(`04_cut/cutlist.json`)

```json
{
  "version": 1,
  "source": "01_materials/JJAV2815.MP4",
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

- `keep` 由 `cuts` 中 `action == "remove"` 的区间**自动推导**,不得手写;`rs_cut.py --apply` 会重算并校验连续性(必须严格覆盖 `[0, srcTotalMs]` 且不重叠)。
- `removedMs / srcTotalMs` 用于验收:裁剪比应落在 **20–35%**(口播长素材);偏离过大时如实上报,不硬凑。

## 3. `reason` 是封闭枚举

合法值(封闭枚举,共 9 项):

`silence` / `breath` / `filler` / `false_start` / `retake` / `stumble` / `repetition` / `off_topic` / `manual`

> `rs_cut.py` 对未知 `reason` 直接报错(退出码 2)。枚举的意义是**可统计、可审计、可回归**:每轮迭代能看「retake 检出率」「filler 误删率」怎么变。

## 4. 三路检测器(并行跑,融合出 CutList)

| 检测器 | 输入 | 判据 | reason |
|---|---|---|---|
| **静音** | 音频能量 + VAD | 能量 < **-32 dB** 且持续 ≥ **600 ms** | `silence` / `breath` |
| **口头禅** | 字级转写 | 词典:`嗯/呃/啊/那个/这个/就是说/然后就是/就…然后` | `filler` |
| **口吃/重复** | 字级转写 | 相邻窗口字序列近似重复(编辑距离比 ≥ 0.8,间隔 < 3s) | `stumble` / `repetition` |
| **重录** | 字级转写 | 窗口 A、B 相似度 ≥ 0.8 **且** 间隔 < 15s **且** B 更完整(字数 ≥ A 且更接近句末)→ **删 A 留 B** | `retake` / `false_start` |
| 跑题段落(可选) | Wordline + Agent 语义 | Agent 读转写判断「说跑题了/自我否定」 | `off_topic` |

### 4.1 阈值随句长自适应

学术依据:不流畅(disfluency)= 自我纠正 / 重复 / 填充停顿;Shriberg 1994(Switchboard)发现 **10–13 词的句子有 50% 概率含不流畅**——**句子越长越容易含 disfluency**。所以阈值不能全片一个常数:

- 所在句长 > 25 字时,`filler` 阈值从 0.85 放宽到 **0.75**(更容易被标为 `review`,而不是更容易被删);
- 放宽只影响**进审查包的比例**,不影响 `remove` 的门槛——**宁可多审,不可错删**。

### 4.2 人几乎总把好的版本说在最后

重录检测的默认策略:**保留后一次完整尝试,切掉较早的**。这条启发式来自行业工具(Vidpal 等)的通行做法,但必须过 §5 的 guard。

## 5. `guard` 三重校验(防误删的核心)

**任何一刀不通过即不执行,自动降级为 `action: "review"`,并在 `note` 里写明未过的项。**

| 校验 | 判据 | 依据 |
|---|---|---|
| `inSilence` / `outSilence` | 切点前后 ± **120ms** 内存在静音(VAD 或能量判据) | auto-editor `--margin` 的"留白防削字"精神 |
| `wordClipped` | 该时刻**不落在任何一个字的 `[startMs, endMs]` 内部** | Wordline 字级时间戳 |
| `tailKeepMs` | 切点后保留 ≥ **60ms** 释放余量 | Descript 的 **"Avoid harsh cuts"**(分析周边音频,会削到相邻词就跳过) |

### 5.1 反向保护(防止把修辞停顿删掉)

删掉某段后,若其**前后两个语义单元的间隔从 > 700ms 降到 < 200ms**,说明这一刀删掉的是一个有表达作用的停顿 → 标记 `rhetorical_pause_suspect`,**强制 `review`**。

## 6. `conf` 三级 → `action` 三态

| conf | action | 行为 |
|---|---|---|
| ≥ **0.90** | `remove` | 自动执行(仍须过 guard) |
| **0.60 – 0.90** | `review` | 进审查包,等 Agent/用户批 |
| < **0.60** | `keep` | 不动 |

> 这三档是「防粗剪不合格」的核心机制。**硬线:宁可漏删,不可错删。** 一条素材的错删是不可恢复的内容损失,漏删只是多花几秒。

## 7. 审查包(`04_cut/review/`)——把「听 20 分钟」变成「听 30 个 3 秒」

每个 `review` 刀生成:

| 文件 | 内容 |
|---|---|
| `cXXX.wav` | 切点**前 1.5s + 后 1.5s** 音频 |
| `cXXX.png` | 切点帧(可选,视觉确认) |
| `cXXX.md` | 一行说明:`reason` / `conf` / `note` / guard 结果 |

Agent(或用户)可以**只听音频**就完成 approve / reject——这是粗剪从「AI 猜」变成「人机协作」的关键形态。

产出汇总 `04_cut/cut_report.md`:

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
rs_cut.py 05_ir/wordline.json --detect all --out 04_cut
rs_cut.py --review-pack 04_cut/cutlist.json
# 人工批注:把 cutlist.json 复制为 cutlist.final.json,改 action / 删条目
rs_cut.py --apply 04_cut/cutlist.final.json --render
```

`--apply` 负责:重算 `keep` → 写回 `wordline.json`(`space: final`)→ 供 `rs_ir.py build --from-cutlist` 生成 IR 主轨。

### 剪后衔接(必须保留)

每一刀都会在静止机位上产生 jump cut,并因 room tone 被切掉而显得突兀。`rs_render` 已有**段间 8ms afade**——**明确保留,并写入 S8 校验**;需要更自然的场合用交替 punch-in / b-roll 覆盖。

## 9. 门禁与验收

| 检查 | 通过线 |
|---|---|
| **误删率** | **= 0**(硬线) |
| retake 检出率 | 含 ≥3 处重录的测试素材上 ≥ 90% |
| guard 通过率 | 所有 `remove` 刀三项全过(100%) |
| 裁剪收益 | 长口播素材时长减少 20–35% |
| 报告齐全 | `cut_report.md` + 每刀 `review/cXXX.*` 存在 |
