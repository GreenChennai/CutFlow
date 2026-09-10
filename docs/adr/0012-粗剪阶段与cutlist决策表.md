# ADR-0012:新增粗剪阶段与 CutList 决策表

状态:已采纳 ｜ 日期:2026-09-10

## 背景

用户反馈「粗剪不合格,比如一段口播视频,里面如果有口误/废弃片段夹杂,目前的 Skill 很难识别到并筛选出来剪掉」。

审计发现:`SKILL.md §1` 的路由是 `口播视频 → rs_asr → 校对 → 字幕`,**没有 cut 这一步**;`cutlist` / `retake` / `filler` / `silence` 这些词在整个仓库零出现。这不是参数没调好,是**阶段不存在**——原始素材被当作"已经拍好的成片"直接进入感知与合成。

代价:30 分钟的口播里含 8 分钟口误/重录/长停顿,人只能靠耳朵一句句听;而且素材越啰嗦,后面 S7 字幕、S6 音效、S3 渲染的成本全部按原始时长计费。

## 决策

1. **新增 S2 粗剪阶段**,插在 ASR 之后、基础合成之前(**不是**先合成再剪);
2. 粗剪的产物不是一段新视频,而是**一张可读、可改、可审查的决策表** `04_cut/cutlist.json`;
3. `reason` 为**封闭枚举**(9 项):`silence` / `breath` / `filler` / `false_start` / `retake` / `stumble` / `repetition` / `off_topic` / `manual`;未知值直接报错(退出码 2)——保证可统计、可审计、可回归;
4. **`conf` 三级 → `action` 三态**:
   - `conf ≥ 0.90` → `remove`(自动执行,仍须过 guard)
   - `0.60 ≤ conf < 0.90` → `review`(进审查包)
   - `conf < 0.60` → `keep`(不动)
5. **`guard` 三重校验**,任一不过即降级 `review`(绝不放宽):① 切点前后 ±120ms 内有静音;② 不落在任何字的 `[startMs, endMs]` 内部;③ 切点后保留 ≥60ms 释放余量(Descript 的 "Avoid harsh cuts");
6. **反向保护**:删掉某段后若前后语义单元间隔从 >700ms 塌到 <200ms,标记 `rhetorical_pause_suspect` 并强制 `review`;
7. **审查包** `04_cut/review/cXXX.{wav,png,md}`:切点前后各 1.5s 音频 + 说明,把「听 20 分钟整片」变成「听 30 个 3 秒片段」;
8. `keep` 区间**由 cuts 自动推导**,不得手写;`rs_cut.py --apply` 校验 keep 有序、不重叠、覆盖到片尾;
9. 执行方式:**只产 CutList + 渲染 keep 片段序列,绝不整段重编码**;
10. 阈值随句长自适应(长句更易含 disfluency),但**放宽只影响进审查包的比例,不降低 `remove` 门槛**;
11. 重录策略:**保留后一次完整尝试,切掉较早的**(人总把好的说在最后)。

## 后果

- 长口播素材可裁掉 **20–35%** 时长,且后面每一步都变便宜;
- 硬线是**宁可漏删,不可错删**:错删是不可恢复的内容损失,漏删只多花几秒;
- 每一刀都可能产生 jump cut 与 room-tone 断裂 —— `rs_render` 的**段间 8ms afade 明确保留**并纳入 S8 校验;
- 新增依赖:粗剪质量依赖 Wordline 字级时间戳(ADR-0011),故 S2 必须在 S1 之后;
- 与行业实现对齐:auto-editor 的「导出剪辑决策给 NLE 而不是直接渲染」、Descript 的四种填充词处理与 "Avoid harsh cuts"、Google Research 的 disfluency 三类划分。

## 关联

- `rules/roughcut.md`、`docs/OPTIMIZATION-v4.md` §3.3 / §4.1
- 依赖 ADR-0011(Wordline)
