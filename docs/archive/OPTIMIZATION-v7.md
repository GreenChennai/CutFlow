# CutFlow 优化迭代笔记（OPTIMIZATION-v7）

> 日期：2026-09-12 ｜ 基线：v0.6.0（118 条可收集用例全绿）
> 前置：`OPTIMIZATION-v4.md`（结构化管线）、`OPTIMIZATION-v5.md`（内置 ASR + 分级验证）、`OPTIMIZATION-v6.md`（seg 缓存 / retext / 卡拉OK）
> 性质：**可执行方案笔记**。每项含 问题 / 方案 / 验收标准 / 成本（人日），按 P0/P1/P2 分级。
> 本文只做规划，**不含实施**；用户确认后另行实施。

---

## 0. TL;DR

v0.6.0 把「字级时间戳全链路」打通了（334 字↔334 条、conf 0.95、RTF≈0.08），S0–S11 骨架稳定、118 条用例全绿。但**成片观感**仍有三处系统性欠账，且都不是参数没调好，而是**管线中存在自相矛盾的设计**：

1. **字幕与声音对不上（或快或慢）**：三层原因叠加——
   ① `rs_sync` 的自检**只核对卡片起点、不核对终点**，所以"尾部漂移"在报告里根本不可见；
   ② `rs_subtitle` 为了"卡间距 ≥2 帧 / 最短 0.83s"会**改写卡片后沿**（收早或延长），而这两个动作都会破坏"末字 `endMs`+20ms 才结束"这一时间铁律；
   ③ 纯文案 / TTS 路径在**无字级时间戳时按"句内均分"猜位置**（`rs_align.build_wordline`），这本质就是 `rules/align.md` 明令禁止的"按字符数比例插值"的句内版本。
2. **字幕断句切词**：`segmentation.cut_score` 的两条语义加分方向写反了——**以连词（而/但/并/且…）结尾反而加分、以连词起首反而不加分**，与它自己引用的 BBC「segmentation at clause boundaries is to be preferred」正好相反。于是「…店铺违规**而** / 被连带处理…」这种切法被 DP 选中。
3. **口播废片段没剪掉**：`detect_retake` **只比较相邻两句**、间隔 <15s、相似度 ≥0.80，而口误后重来常跨句、隔得久；加上 `guard` 四项**同时成立**才允许 `remove`（重录刀出点紧邻下一句首字，天然不落静音区），于是**绝大多数重录/中间段被压进 `review`**，真正剪掉的极少；"有画面无语音"的调整段（换提词器/理仪容）根本不在检测范围。

**重构侧**（用户四条调整）：
- 删掉第二个技能组 `cutflow-prompt`（内容归档 `docs/archive/`）；
- 用 **videoType 三类型**（纯口播 / 口播+动画 / 纯动画）**取代**现有 `rules/genres/` 六册类型知识库，并预留扩展占位；
- 明确"**脚本做机械、Agent 做识别校验**"，并落地脚本鲁棒性清单；
- **字幕为重中之重**：建立平台预设档案（抖音 / 视频号 / 小红书 / B站），新增第三画幅 **1080×1440（3:4，小红书）**。

**最推荐先做的三件（收益/成本比最高）**：① 字幕终点校验 + 后沿策略纠正（#1）；② 断句连词评分纠正（#2）；③ 粗剪重录检测跨句化 + guard 按 reason 分档（#3）。

---

## 1. 现状快照（2026-09-12 复核）

| 指标 | 数值 | 备注 |
|---|---|---|
| 版本 | v0.6.0 | `docs/CHANGELOG.md` 顶部 |
| 测试 | 118 条（`def test_` 字面 120，其中 `test_v6.py` 有两对重名函数被 pytest 合并） | test_smoke 9 / test_v4 31 / test_v5 28 / test_v6 52 |
| 管线 | S0–S11 阶段注册表 + 声明式缓存 | `rs_run.py` / `rules/incremental.md` |
| CLI 契约 | `{ok, code, message, data}`；退出码 0/2/3/4 | `rs_common.py::emit/die` |
| 画幅 | 仅 `9x16`（1080×1920）与 `16x9`（1920×1080） | `rs_render.py:30` `RATIO`、`project.schema.json:36-49/100-108` |
| 字幕风格 | `talkshow-bold` / `tutorial-clean` / `subtitle-white` | `rs_subtitle.py:37-56` |
| 每卡字数 | `{"9x16": 12, "16x9": 22}` | `segmentation.py:45` |
| 类型知识 | `rules/genres/` 六册 | 口播知识 / 动画教程 / 新闻采访 / 短剧 / 影视解说 / _通用规则 |
| 技能组 | 两个 | `skills/cutflow/` + `skills/cutflow-prompt/` |

**本次复核确认的"隐藏口径问题"**：
- `rs_subtitle.py:207` 与 `rs_verify.py:190` 都把 CPS 上限**写死为 `CPS_MAX["9x16"]=9.0`**，无论当前是什么比例——加画幅时必须一并改，否则校验口径错。
- `rs_render.py:715-716` 用字符串 `canvas == "1080x1920"` 反推比例，`rs_brand.py:69-70` 直接硬编码两张画布字典——新增画幅会踩到。
- `detect_filler` 第 155–156 行 `if ...: pass` 是死代码。

---

## 2. 候选总表

| # | 候选 | 级别 | 价值 | 成本 | 对应 |
| --- | --- | --- | --- | --- | --- |
| 1 | 字幕↔音频同步三件套（终点校验 / 后沿策略 / 帧对齐 + TTS 真字级） | **P0** | 高 | 1.5–2d | 用户问题① |
| 2 | 断句连词切词修复 + 回归集扩充 | **P0** | 高 | 0.5–1d | 用户问题② |
| 3 | 废片段检出增强（跨句重录 / 段落重来 / 无语音长段 / guard 分档） | **P0** | 高 | 1.5–2d | 用户问题③ |
| 4 | 平台字幕预设档案 + 新增 1080×1440 画幅 | **P0** | 高 | 1–1.5d | 用户调整④ |
| 5 | 删除 `cutflow-prompt` 技能组并归档 + 引用清理 | **P0** | 中 | 0.25d | 用户调整① |
| 6 | videoType 三类型取代 genres + 三类型管线分支 | **P0** | 高 | 1.5–2d | 用户调整② |
| 7 | 脚本鲁棒性强化清单（消灭静默降级） | P1 | 中高 | 1d | 用户调整③ |
| 8 | 新增 ADR-0018（videoType 取代 genres）/ ADR-0019（平台字幕预设） | P1 | 中 | 0.25d | 随 #4/#6 |
| 9 | 补 `rs_ingest`(S0) + `deliverables.md` 自动生成 | P1 | 中 | 0.5–1d | BACKLOG v4/v5 欠账 |
| 10 | TTS 配音强制对齐工具 `rs_dub align` | P1 | 中高 | 1d | #1 的 ④ 依赖固化 |
| 11 | `rs_cut` 死代码清理 + retake 阈值按素材类型分档 | P2 | 低 | 0.25d | BACKLOG |
| 12 | `rs_sync` 增加「卡片 ↔ 动画卡时间窗」重叠检查 | P2 | 中低 | 0.5d | BACKLOG |

---

## 3. P0 详单

### #1 字幕↔音频同步三件套

**问题（三条独立根因，均已定位到代码）**

- **① 自检口径缺口（尾部漂移不可见）**：`rs_sync.py::check_offsets`（70–93 行）只算**起点**偏移，硬编码公式 `expect = (chars[first].startMs - LEAD_MS) / 1000`（第 86 行，`LEAD_MS=20`）。`summarize`（96–123 行）统计的也只是起点 offset 的中位数 / 95 分位；`durMs` 虽算出（第 90 行）但只用于时长区间与 CPS 校验。**全链路没有任何一处校验"字幕终点是否等于末字 `endMs`+20ms"**。`--video` 分支（181–191 行）只比对成片总时长 ±0.5s。→ 后沿怎么漂，报告一律显示"通过"。
- **② 后沿被"间距/最短时长"逻辑改写（偏快 / 偏慢）**：
  - `_enforce_gaps`（`rs_subtitle.py:290-297`）把前卡 `end` 收为 `max(a["start"]+0.2, b["start"]-2帧)`（第 296 行）——**会切进末字的真实语音**（末字本应到 `endMs+20ms`）→ 字幕比声音先消失，观感"偏快"。
  - `_extend_short`（273–287）把 <0.83s 卡的后沿延到"下一卡起点前 66ms"；`_merge_short`（226–270）第一遍并入上一卡时 `prev["end"]=e["end"]`。两卡之间若有长停顿，字幕会**滞留到停顿之后**→ 观感"偏慢 / 对不上"。
  - 注意：起点从未被改（设计如此，起点决定对齐精度），但**终点被改而起点没改 = 时长被改**，这就是"或快或慢"的直接来源。
- **③ TTS / 纯文案路径的句内比例插值（禁区）**：`rs_align.py::build_wordline` 在无字级时间戳时走 else 分支（111–121 行），按 `a = st_ms + span*k/n`（115–116 行）**句内均分**并追加降级原因"字级时间戳缺失:句内按均分估算"（121 行）。`rs_align.py:451` 的 TTS 入口直接写死 `degraded="TTS 路径:句级时间 + 句内均分"`；`rs_subtitle.py:473` 也走同一函数。→ 语速不均时字幕必然错位，且**字幕的粒度仍是"字"，看起来像字级，实际是猜的**。
- ④ 附带：`_kar_text`（370–383）末字结束时间取 `ev_end`（卡尾），而卡尾已被 ② 改写 → 卡拉OK 逐字染色与真实语音错位；ASS 时间直接写小数秒、未做帧对齐（`_ts_ass`）。

**方案**

1. **补终点校验（先做，否则修了也看不见）**：`rs_sync.check_offsets` 同时算终点偏移 `expect_end = (chars[last].endMs + LEAD_MS)/1000`，`summarize` 新增 `medianEndMs` / `p95EndMs`，报告增两行；通过线与起点同档（中位 ≤40ms、95 分位 ≤80ms）。过渡期给 `--legacy-end` 开关只告警不阻断，避免历史工程大面积"未通过"。
2. **后沿策略纠正**：
   - `_enforce_gaps` 改为**只允许在"末字 `endMs`+20ms 之后"收**；若与下一卡间距不足，改为**把下一卡起点推到 ≥2 帧后**（同样帧对齐、且不得越过下一卡自身末字），或**如实标记 `overlap` 违规**——**绝不为凑间距切掉末字语音**。定序：对齐精度 > 卡间距 > 最短时长。
   - `_extend_short` 延长上限 = 该卡末字 `endMs` + 允许释放余量（建议 ≤300ms），**不得无约束延到下一卡前 66ms**。
   - `_merge_short` 合并后 `end` 取"真实末字 endMs+20ms"，不取下一卡起点。
3. **消灭"静默"的句内比例插值**：
   - `build_wordline` 无字级时间戳时置 `charTimingEstimated=True`、逐字打 `estimated` 标，`degradeReasons` 明确"卡内位置为估算(不可当字级用)"；`rs_subtitle` 在 `meta["charTimingEstimated"]` 透出，并继续拒绝用于卡拉OK（逐字染色）。
   - **v0.7.0 落地修订**：原计划"整句一卡"在实施时被实测否决 —— 长句（>max_chars）会直接变成超字数大卡，伤观感且让 L0 字数门禁不过。改为**保留按 `max_chars` 出卡以保可读性**，但卡内位置**不再被当成字级**（显式标注 + 不可用于逐字染色 + 报告点名）。真正的字级时间由 #10 补齐。
   - 新增 `rs_dub align`（复用自带 pkg ASR 对 TTS 音频做强制对齐）→ 把 TTS 音频的**字级时间戳**回填 wordline，`degraded=False`。这是"纯动画 / TTS 视频字幕对轴"的正解，也是 #10 的内容。
4. **karaoke 末字取真值**：`_kar_text` 末字结束时间改用末字 `chars.endMs`；karaoke 模式下后沿改动必须**同步刷新 `chars` 时间**或直接禁用后沿改写。
5. **帧对齐**：ASS 时间按 fps 量化（起点向下、终点向上取整到帧），并在 `rs_sync` 断言"字幕时间与最近帧差 ≤1 帧"。
6. `rs_sync --video` 增加**段级抽样**校验（按 keep 段边界抽样比对），不止总时长。

**验收标准**

| 检查 | 通过线 |
|---|---|
| 起点偏移 | 中位数 ≤40ms、95 分位 ≤80ms（保持） |
| **终点偏移（新增）** | 中位数 ≤60ms、95 分位 ≤120ms（宽于起点：允许可读性延长 ≤0.30s）；**「早退」>25ms = 0**、**「滞留过久」>350ms = 0**（两项硬失败） |
| 老工程过渡 | `rs_sync --legacy-end` 跳过终点门禁（只告警）—— **v0.7.0 已实现** |
| 卡片时长 vs `末字endMs-首字startMs` | 差值 ≤2 帧 |
| 卡拉OK 染色 | 抽 3 行首/尾字与 timestamp 误差 ≤50ms |
| 音画切点一致 | 100% 切点同帧或差 ≤1 帧 |
| TTS 路径 | `degraded=False`（走 `rs_dub align`）或显式标注 `charTimingEstimated` + "卡内位置为估算"，**不得出现"静默当字级"路径** |

**成本**：1.5–2 人日（其中 `rs_dub align` 是 #10，可先行只做报告不自动变速）。

---

### #2 断句连词切词修复

**问题（用户实例已复现）**

基准句：「可能因为其中一家店铺违规而被连带处理最终一同遭殃」

- `segmentation.py:27` `TAIL_FUNC = "的了着地吧呢啊吗嘛"` **不含 `而/但/并/且/或/及/与/则/却/因/若/虽/如/故/即/由`**。→ 以"而"结尾的卡，在 `cut_score`（117–118 行）仍拿到"不以虚词结尾 +0.5"。
- `segmentation.py:119-120` 写的是 `if right[0] not in CONJ_HEAD: s += 0.5`。`CONJ_HEAD`（第 26 行）**已含"而"**，于是**以连词起首反而不加分**，方向与 `rules/subtitles.md §1` 引用的 BBC 原文（*segmentation at clause boundaries is to be preferred / Random segmentation must certainly be avoided*）**正好相反**。
- 叠加效应：在"而"**之后**切（`…违规而` / `被连带处理…`）时，左端以"而"结尾 +0.5、右端以"被"起首 +0.5 → 语义分 +1.0；而在"而"**之前**切只有 +0.5。只要 DP 因停顿（`candidate_positions` 第 102–103 行把"连词前"列为候选）或长度均衡略微偏向后者，**就会选中错误切法**。
- `forbidden_positions`（62–92 行）的 `FORBID_AFTER = "的地得了着之"`（第 31 行）只覆盖 5 字，不足以拦住连词收尾。
- `REGRESSION`（52–57 行）现有 4 条用例（专名 / 数量词 / 货币 / ASCII）**没有连词类用例**。

**方案**

1. **新增"不可收尾"集合** `NO_TAIL`（至少含 `而但并且或及与则却故因若虽如即由`，与 `TAIL_FUNC` 合并判定）。在 `cut_score` 中，左端以 `NO_TAIL` 结尾时给 **−2.0 强惩罚**（而非硬禁切，避免极端句无解导致 DP 退化）。
2. **纠正连词起首方向**：`right[0] in CONJ_HEAD` → **+0.5（从句边界，合法切点）**；非连词起首记 0。即把"从句边界优先"落实为加分，而不是扣分。
3. **扩禁切表**：`forbidden_positions` 纳入连词收尾禁切（与 1 配合，双保险）；补充"助词 + 连词"组合（如"的了着之"后接连词）。
4. **回归集固化**（`segmentation.REGRESSION` + `tests/`，必须全绿）：
   - 「可能因为其中一家店铺违规而被连带处理最终一同遭殃」→ 不得切成 `…违规而` / `被连带处理…`
   - 「他努力了但是没有成功」→ 不得切成 `…了但` / `是没有成功`
   - 「便宜而且好用」/「先洗手然后再吃饭」→ 不得以连词收尾
   - 保留既有 4 条（专名 / 数量词 / 货币 / ASCII）
5. 顺带把 `rules/subtitles.md §4.2/§4.3` 的"禁切表 / 打分函数"文档与代码对齐（文档目前也没写连词收尾）。
6. **v0.7.0 落地补充**：实测中另发现**过度切分**（同一句被 DP 切成一片 4 字卡）也属"断句拉跨"，故新增 `CUT_COST = −1.0`（每刀固定代价）。`forbidden_positions` **未**加连词硬禁切 —— 硬禁会让极端句无解（DP 无候选 → 单卡超字数），改用强惩罚达成同样效果。

**验收标准**

- 新增 4 条连词用例全绿；既有 4 条回归用例保持全绿；
- 抽样 20 句含连词的句子，**连词不在卡尾**的比例 = 100%；
- 不引入新的字数/CPS/时长硬约束违规（DP 无解时降级为"告警 + 惩罚切法"，不崩）。
- 成本：0.5–1 人日。

---

### #3 废片段检出增强

**问题（两类中间片段都没被剪）**

用户场景 A：**口误后从口误处重新开始**（跨句重录）；场景 B：**一段说完后调整仪容 / 换提词器再继续**（有画面、无有效语音）。

- **只比相邻句**：`detect_retake`（`rs_cut.py:188-218`）用 `zip(sents, sents[1:])`（第 193 行）只比较**相邻两句**，且要求间隔 ≤15s（203–204 行）、`SequenceMatcher.ratio ≥ 0.80`（205–207 行）。口误后重来常**跨 2–3 句、间隔 >15s**（尤其说完一段再重录）→ 漏检。
- **只删前一次，不合并多次旧尝试**：即使命中，也只删紧邻的前一次（`in_ms` = 前句首字起点，`out_ms` = 后句首字起点）。若同一段录了 3 次，会残留中间那次。
- **guard 四项同时成立 → 大量降级 review**：`guard`（82–104 行）要求 `inSilence && outSilence && !wordClipped && tailKeep≥60ms`（第 101 行）四项**同时**成立。重录刀的出点 `out_ms = t_b_start`（下一句首字起点，212 行）**天然紧邻语音**，不落静音区 → 即便 `conf≥0.90` 也被 `classify`（113–130 行）降级 `review`。结果是"检出了一些，但真正 `remove` 的极少"，用户观感就是"没剪掉"。
- **无"有画面无语音"长段检测**：`detect_silence`（135–147 行）只基于 wordline **相邻字之间的 gap**（`SILENCE_MIN_MS=600`，两端各留 150ms、剩余 <200ms 跳过）。若调整段里夹了零碎说话（"好，再来一遍"）或 ASR 把它当成文本，就完全不成静音刀。
- **检测器不含 off_topic**：`DETECTORS`（232–233）只有 silence / filler / repetition / retake；`detect_off_topic` 仅能由 `--off-topic` 手工传 chars 下标，`--detect all` 走不到。
- 顺带：`detect_filler` 155–156 行死代码。

**方案**

1. **重录检测跨句化（滑动窗口）**：把 `detect_retake` 改为在**窗口内任意两句 (i<j)** 判定，窗口 = 句数 ≤6 或时间间隔 ≤30s；命中后**删除区间 = 最早旧尝试起点 → 最后一次尝试起点**，**一次合并删除多次旧尝试**（reason 仍 `retake`，note 记"第 1–N 次尝试，保留最后一次"）。阈值分档：口播 0.80 / 短剧 0.86（短剧更怕误删），落到 `--retake-ratio` 与 `brief` 默认值。
2. **新增段落级整段重来 `detect_retake_block`**：以"连续 ≥8 字（或 ≥2 句拼接）内容重复"为判据，删旧块留新块，reason `false_start`。用于"说完一整段觉得不满意，整段重来"。
3. **新增"有画面无语音"长段检测 `detect_dead_air`**：不再只依赖字间 gap，而是读**素材音频能量/VAD**（`rs_cut` 接素材路径，用 ffmpeg 能量探测或复用 ASR 的 VAD 段），标记"无有效语音 ≥1.2s"的中间段；当前后语义单元完整时给较高 `conf`，reason `silence`（长）/`breath`（短）。
4. **guard 按 reason 分档（关键，且不放松安全线）**：
   - `retake` / `false_start`：只硬要求 `!wordClipped` + `tailKeep≥60ms`，`inSilence/outSilence` 降为**告警项**（因为其切点本就紧邻语音）；
   - `silence` / `breath` / `dead_air`：保持静音侧要求；
   - **`wordClipped` 永不放松**（宁可漏删，不可错删的硬线不变）。
   - `classify` 相应改为"按 reason 取 guard 组合"，并在 `cut_report.md` 里分列"硬过项 / 告警项"。
5. **off_topic 接入**：保留 Agent 语义判定为最终裁决，但脚本**自动生成候选**（相邻高相似块 + 自我否定词如"说错了/重新说/这段不算"）列入 `review`，供 Agent 一键批注；不自动 `remove`。
6. 清死代码；审查包为 retake 刀额外附"旧尝试 / 新尝试各 3s"音频，便于 A/B 快速听。

**验收标准**

| 检查 | 通过线 |
|---|---|
| 误删率 | **= 0（硬线，保持）** |
| retake 检出率 | 含 ≥3 处重录的素材 ≥90%（保持，且新增跨句用例） |
| guard 通过率 | 所有 `remove` 刀过其 reason 对应组合 100% |
| 裁剪收益 | 长口播素材时长减少 20–35% |
| **中间段残留（新增）** | 人工抽验：调整/重来片段残留 ≤1 处 / 10 分钟素材 |
| 报告齐全 | `cut_report.md` + 每刀 `review/cXXX.*`（分列硬过项/告警项） |

**成本**：1.5–2 人日（`detect_dead_air` 若需新增音频能量探测，取上限）。

**v0.7.0 落地补充**
- 出点取「最后一次尝试起点 − `TAIL_KEEP_MS`(60ms)」：既留出自然起音，又满足 `tailKeep` 硬项。
- 新增 `_chain_merge`：把同一段话的**连续**重录刀串成一刀 —— 否则两个旧尝试之间会留下几十毫秒碎片（实测 1→2 句、2→3 句各出一刀，合并后才干净）。
- `rhetorical_suspect` 反向保护**只作用于 `silence`/`breath`**：原实现用"其它刀"当语义单元代理，单刀/末刀会被一律降级为 review，反而把重录刀挡住。
- `dead_air` 的音频路径用 ffmpeg `silencedetect`（新增纯函数 `parse_silencedetect` 便于单测），无 `--media` 时退回字间 gap。
- 元话语检测落为 `detect_self_negative`，**只产 `review` 候选**（"再来一遍"也可能是正文内容，不自动删）。
- `dead_air` 的 `reason` 按空档长短分档：< 2×1.2s → `breath`，≥ → `silence`（与静音检测语义一致）。
- 阈值分档只提供 `--retake-ratio` 手动旋钮；**按 `brief.videoType` 自动取默认值**留待 #6 之后（已记 BACKLOG）。
- `GUARD_REQUIRED` 除 `retake`/`false_start` 外，把 `stumble`/`repetition`/`off_topic`/`manual` 一并归入"切点紧邻语音"档 —— 对它们要求落静音区等于永不 `remove`，属同一逻辑；`wordClipped` 仍对所有 reason 硬卡。

---

### #4 平台字幕预设档案 + 新增 1080×1440 画幅

**问题**

- 现有字幕只有 3 个风格（`rs_subtitle.py:37-56`）与 2 个画幅，**没有"平台"概念**；小红书（3:4）根本不在支持范围（全仓库 `rules/` 无 `3:4` 字面量）。
- 画幅是**散落的硬编码**，加一档会牵动多处：
  - `rs_render.py:30` `RATIO = {"9x16": (1080,1920), "16x9": (1920,1080)}`；`--ratio choices`（699 行）；`canvas→ratio` 反推靠字符串比较（715–716 行）；输出命名用 `ratio.replace('x','')`（665–666 行）。
  - `rs_brand.py:69-70` 画布字典硬编码；`logo_rect`（37–53 行）安全区倒是按百分比算（44–45 行，可复用）。
  - `project.schema.json`：`canvas.width/height` 枚举 `[1080,1920]`（36–49 行）、`outputs` 枚举 `["9x16","16x9"]`（100–108 行）——**1080×1440 会同时触犯这两处**。
  - `rs_verify.py:95-105` `_max_chars` 读 `maxChars["9x16"]`、`:190` CPS 写死 9x16、`:238-245` 待目测清单只写 9:16 安全区。
  - `rs_subtitle.py:207` CPS 写死 9x16、`:449` ratio choices、`:462` canvas 推断。

**方案**

1. **新增平台预设档案** `templates/platforms.json` + `rules/platforms.md`，每平台一条：`{ratio, canvas, safeArea:{top,bottom,left,right}, maxChars, cpsMax, style, fontSize, marginV, durationHint, coverSize}`。首发四平台：
   | 平台 | 主比例 | 画布 | 字幕风格 | 备注 |
   |---|---|---|---|---|
   | 抖音 | 9:16 | 1080×1920 | talkshow-bold | 底 25% / 顶 12% 安全区 |
   | 视频号 | 9:16 | 1080×1920 | talkshow-bold | 同抖音，留白略增 |
   | 小红书 | 3:4 | 1080×1440 | talkshow-bold（字号下调） | 竖屏正文，安全区重新标定 |
   | B站 | 16:9 | 1920×1080 | tutorial-clean | 底部字幕条 |
2. **`rs_subtitle` 增 `--platform`**（与 `--ratio/--canvas` 联动，显式参数优先）；`STYLES` 各档补 `3x4` 的 size / margin_v；`--ratio choices` 增 `3x4`。
3. **新增画幅 1080×1440（3:4）**，按上面清单**逐处同步**：
   - `segmentation.MAX_CHARS` 增 `"3x4"`（建议 15–16，按竖屏屏宽介于 9:16 与 16:9 之间，落地时以实拍观感定档）；
   - `CPS_MAX` 增 `3x4`（9.0），并把 `rs_subtitle.py:207`、`rs_verify.py:190` 的**写死 9x16 改为"按当前 ratio 取"**；
   - `rs_render.RATIO` / `--ratio choices` / canvas→ratio 反推（改为查表，不再字符串比较）/ 输出命名；
   - `rs_brand.variant_ir` 画布改查表；`expand_matrix` 无需改；
   - `project.schema.json` 两处枚举；
   - `rs_verify` 待目测清单与安全区文字按画幅给。
4. **竖屏 / 横屏观感统一**：9:16 与 3:4 用大字居中偏下 + 底部安全区；16:9 用底部半透明条；安全区随 `platforms.json` 配置化，不再散落。
5. 附带：`rules/subtitles.md` §7/§8、`rules/compose.md`、`rules/branding.md` 里的比例清单同步为"平台 × 画幅"。

**验收标准**

- 抖音 / 视频号 9:16、小红书 3:4、B站 16:9 **各出一版**，字幕不压脸、不出安全区；
- 每版 CPS ≤9、每卡字数 ≤平台上限、单卡时长落 [0.83s, 7s]；
- `rs_verify` L1 目测清单四画幅通过；
- 变体矩阵（多 Logo × 多画幅）仍能展开，`rs_brand` 安全区判定随画幅变化；
- **L1 目测（Agent 首次）**：三画幅字幕可读性均通过。

**成本**：1–1.5 人日。

---

### #5 删除 cutflow-prompt 技能组并归档

**问题**：用户要"先完善第一个技能，第二个删掉"。`cutflow-prompt` 是独立技能组（Seedance 提示词），触发场景与剪辑独立，当前阶段不再维护。

**方案**

1. 内容归档到 `docs/archive/cutflow-prompt/`（`SKILL.md` + `references/`），保留备查；
2. 删除 `skills/cutflow-prompt/` 目录；
3. 清理引用（**已 grep 全量确认，共 6 处 / 8 行**）：
   | 文件:行 | 处理 |
   |---|---|
   | `tools/install.ps1:15` `@("cutflow","cutflow-prompt")` | 改为只装 `cutflow` |
   | `skills/cutflow/SKILL.md:202` AI 生视频 → 转 `cutflow-prompt` | 改为"只产提示词，转 `docs/archive/cutflow-prompt/`（暂不维护）"或删除该行 |
   | `docs/PLAN.md:29 / 43 / 140 / 172 / 266 / 304` | 标注"v0.7 起停用，见 OPTIMIZATION-v7 #5" |
   | `docs/CHANGELOG.md:175` | **历史记录，保留不改** |
4. `README.md` 无引用（确认），无需改；`tests/` 无引用（确认）。

**验收标准**

- 全仓库（忽略 `*.pyc`）grep `cutflow-prompt`，只应出现在 `docs/CHANGELOG.md`（历史）与 `docs/archive/`（归档路径）中；
- `tools/install.ps1` 只安装 `cutflow`；`rs_doctor` 全绿；
- 归档内容可读、路径在文档中可检索。

**成本**：0.25 人日。

---

### #6 videoType 三类型取代 genres

**问题**：现有 `rules/genres/` 六册（口播知识 / 动画教程 / 新闻采访 / 短剧 / 影视解说 / _通用规则）是"按内容题材"的分类；用户的真实需求是**按成片形态**分类（纯口播 / 口播+动画 / 纯动画），因为形态决定**管线分支**（绿幕抠像、动画密度、声音来源），而题材只影响风格默认值。

**方案**

1. **定义 `videoType`（一级路由）**，写入 `brief.md` 与 `intake.md`：
   ```
   videoType: talking-head | talking-head+animation | pure-animation
   # 预留占位（暂不实现，注释保留）：screen-recording / interview / drama / film-commentary
   ```
2. **新增 `rules/video-types/`**（三级视频类型知识册，取代 `rules/genres/`）：
   | 册 | 管线分支要点 | 承接原 genres |
   |---|---|---|
   | `纯口播.md` | 绿幕抠像（colorkey+despill+cropTopPct）、虚拟背景、字幕优先、**动画密度=少/无** | 口播知识 |
   | `口播+动画.md` | 继承纯口播 + artboard 卡片体系（ADR-0004/0009）；**动画流畅性与贴合性**：节拍 2–3s、转场枚举、卡片与口播语义贴合、避让绿幕人物与字幕带 | 口播知识 + 动画教程（部分） |
   | `纯动画.md` | 无真人画面、场景卡串联、**声音来源二选一（音色卡 TTS / 视频中人物原声）**、背景动态化 | 动画教程 |
   | `_通用规则.md` | 响度 / 安全区 / 字幕三定律 / 节奏 / 混音 / 版权 | 原 _通用规则 原样保留 |
   其余题材（新闻采访 / 短剧 / 影视解说）的**红线段落**并入 `_通用规则.md` 的"类型补充"小节并标注"待 videoType 扩展位启用"，避免信息直接丢失。
3. **删除 `rules/genres/`**（信息先并入三册，再删目录）；`docs/adr/0010-类型剪辑知识库.md` 标注"被 ADR-0018 取代"。
4. **同步更新**：`templates/brief.md:6`（`类型(决定 genres 分册)` → `videoType`）、`rules/intake.md:9`（项 0 类型问卷改为三类型 + 占位）、`SKILL.md:59`（路由表）、`SKILL.md:110`（Hard Rule 17"必读对应分册"）、`README.md:8/202`。
5. 新增 `docs/adr/0018-videoType取代genres.md`（#8）。

**验收标准**

- 三类型各跑一条端到端（纯口播 / 口播+动画 / 纯动画），`brief.videoType` 能正确路由到对应分册与管线分支；
- 纯动画的声音来源二选一都能跑通（TTS 音色卡 / 人物原声提取）；
- 全仓库无 `rules/genres/` 残留引用（除 ADR 历史标注）；
- `tests` 中"顶层 `rules/*.md` 必须登记在 SKILL.md"仍全绿（新增顶层规则文件时同步登记；子目录 `video-types/` 不进 glob，但需在 SKILL.md 索引里可检索）；
- `SKILL.md ≤ 270 行` 纪律守住。

**成本**：1.5–2 人日（含三册撰写与文档同步）。

---

## 4. P1 详单

### #7 脚本鲁棒性强化清单

**问题**：用户要求"大部分操作脚本化、识别校验交 Agent，脚本要很强健"。现状已有 `{ok,code,message,data}` + `degraded` 机制，但仍有**静默吞异常**与口径不一致处：
- `rs_cut.py:380-381` `_extract_clip` 的 `except Exception: pass`（无 ffmpeg 时静默）—— **v0.8.0 已改为留痕**（`review/_DEGRADED.md` + 每刀 md 标注）；
- `textopt.card_split:92-97` 的 `except Exception` 直接回退长度算法，**不记 degraded** —— **v0.8.0 已加 `degrade` 列表**；
- `rs_subtitle`/`rs_verify` 的 CPS 写死 9x16（#4 一并修）；
- `_wordline` 降级文案（`rs_subtitle.py:473`）与 `rs_align.py:451` 不一致。

**方案**：落地"鲁棒性清单"并逐项审计：
1. **显式失败不静默降级**：所有 `except ...: pass` 改为"降级 + 写 `degradeReasons` + 报告可见"；不可恢复的错误用 `die(3/4, CODE, msg)` 带明确提示。
2. 统一退出码 0/2/3/4 与 `--json` 契约（保持）；
3. **幂等 / 可重入**：缓存键含脚本文件 hash（已有，ADR-0013），新脚本必须遵守；
4. **路径 / 编码 / 依赖兜底**：Windows 盘符 ASS 转义（已有）、中文路径、UTF-8 读写、ffmpeg/模型缺失给出可执行修复提示；
5. **输入校验**：区间越界、时间非单调、枚举非法、wordline 与素材不同源 → 明确报错不猜。

**验收**：注入坏输入（缺文件 / 坏 JSON / 区间越界 / 无 ffmpeg）→ 每个脚本给出明确错误码与提示，不崩溃、不静默；审计表列出全部 `except` 点的处置。成本 1 人日。

**v0.8.0 落地补充：全量 `except` 审计结论**

| 处置 | 站点 |
|---|---|
| **改为留痕+报告可见**（会损失质量的那类） | `rs_cut._extract_clip`（→ `review/_DEGRADED.md`）、`textopt.card_split`（→ `degrade` 列表）、`rs_subtitle` 单句 DP 失败（→ `degradeReasons`）、`rs_common.resolve_voice` 坏卡（→ 报错点名） |
| **保留为"读不到就退回默认值"的只读兜底**（不吞质量信息，代码内已带注释） | `rs_align` 转写稿 end 字段解析、`rs_run` pipeline/config 读取、`rs_verify` verify.json/pipeline/画布反查、`rs_common.ensure_utf8`（流无 `reconfigure`）、`rs_artboard.probe_duration_ms`（返回 `None` 本身就是信号） |
| **补充** | `rs_common.ensure_utf8()` 在 `rs_common` **导入即生效** —— 端到端实测抓到 `rs_verify` 的检查名含 `↔`，在 GBK 控制台下 `emit()` 直接 `UnicodeEncodeError` 崩脚本（属同类缺陷） |

### #8 新增 ADR-0018 / ADR-0019

- `docs/adr/0018-videoType取代genres.md`：决策、三类型枚举与扩展位、genres 处置（并入 + 删除）、对 intake/brief/路由的影响、对 ADR-0010 的取代关系。
- `docs/adr/0019-平台字幕预设.md`：四平台预设、1080×1440 画幅、安全区配置化、对 schema / render / brand / verify 的连带改动。
- 成本 0.25 人日，随 #4/#6 一起落。

### #9 补 `rs_ingest`(S0) + `deliverables.md`

BACKLOG v4/v5 已挂两轮的 P1：S0 素材摄取（probe 元数据、重命名、生成 IR 骨架）仍靠手工；交付清单未自动化。方案见 BACKLOG。成本 0.5–1 人日。

### #10 TTS 配音强制对齐 `rs_dub align`

即 #1 的方案 3 的工具化：输入 TTS 音频 + wordline → pkg ASR 强制对齐 → 输出每卡实际起止/偏移/超阈值清单与建议（改稿 / 变速率）；顺带覆盖 BACKLOG"fa-zh 偏移自检"。**这是纯动画 / TTS 视频字幕对轴的第一砖**。成本 1 人日。

---

## 5. P2 详单

### #11 `rs_cut` 死代码清理 + retake 阈值分档
删 `rs_cut.py:155-156` 死代码；`min_ratio` 按素材类型分档（与 #3 合并落地）。0.25 人日。

### #12 `rs_sync` 增加「卡片 ↔ 动画卡时间窗」重叠检查
BACKLOG 原条目的自动化版：字幕卡与 artboard 动画卡的时间窗重叠告警（防止动画压字幕）。0.5 人日。
**落地**：`check_card_overlap()` + `--ir`；**默认只告警**（`--strict-cards` 才判未通过）—— 普通素材轨（口播/绿幕）本该有字幕压在上面，不算重叠。

---

## 6. 建议路线

| 版本 | 范围 | 目标量化 |
|---|---|---|
| **v0.7.0 字幕与粗剪修复** | #1 #2 #3 + #11 | 终点偏移中位 ≤40ms；连词用例全绿；重录 remove 不再被 guard 大面积压制；中间段残留 ≤1/10min |
| **v0.7.1 平台适配** | #4 + #8(0019) | 三画幅（9:16 / 3:4 / 16:9）四平台各出一版，L1 目测通过 |
| **v0.7.2 技能重构** | #5 #6 + #8(0018) | 技能组只剩 `cutflow`；videoType 三类型端到端各一条 |
| **v0.8.0 鲁棒性与对齐** | #7 #9 #10 #12 | 坏输入审计全过；TTS 路径 `degraded=False`；交付清单自动生成 |

**始终让路**：无真实素材时不得用"参数调优"替代素材验收（BACKLOG A3/A4/A5、C5、D5 类欠账，拿到真实项目素材时优先补做）。

---

## 7. 风险与边界

| 风险 | 说明 | 缓解 |
|---|---|---|
| **既有测试锁定项被打破** | `tests/test_v4.py`/`test_v5.py` 锁定：`SKILL.md ≤270 行`；顶层 `rules/*.md` 每个文件名必须出现在 `SKILL.md`；`SKILL.md` 引用的 `rs_*.py`/`segmentation.py`/`fun_asr.py` 必须存在；断句回归集必须全绿；`fa.VAD_MAX_END_SIL ≤ 500` | #6 新增顶层规则文件必须同步登记 SKILL.md 索引；#5 删技能后重跑 doctor 与全量测试 |
| 终点校验一旦加上，历史工程"后沿本来就不准"会大面积失败 | 过渡期 `--legacy-end` 只告警不阻断；新工程强制 | 报告分"阻断 / 告警"两栏 |
| guard 放宽（按 reason 分档）被误解为放松安全线 | 必须**只**把 `inSilence/outSilence` 对 retake 降为告警，`wordClipped` 永不放松 | ADR + `rules/roughcut.md` 明写"硬过项 / 告警项" |
| "禁止比例插值"禁区边界模糊 | `retext` 是**字级锚点区间内插值**（合法）；`build_wordline` 的**句内均分**是禁区（本次消灭）；`rs_dub align` 是锚点兜底 | 在 `rules/align.md §5` 明写三者边界，避免实现走样 |
| 新增 1080×1440 牵动多处硬编码，漏改即静默错 | 已清点：`rs_render.py:30/699/715-716/665-666`、`rs_brand.py:69-70`、`project.schema.json:36-49/100-108`、`rs_subtitle.py:207/449/462`、`rs_verify.py:95-105/190/238-245`、`segmentation.py:45` | 改为"画幅查表 + 平台预设单一事实源"，禁止新增字符量硬编码 |
| 删除 genres 造成类型知识丢失 | 先**并入**三册与 `_通用规则`，再删目录 | ADR-0018 记录对照表；`docs/archive/` 可回滚 |
| 无真实素材，验收无法闭环 | #1–#3 的量化验收需真实素材 | 用 lavfi 合成片锁机械指标（切点/偏移/CPS）；真实观感验收待素材就位 |
| 一次改动面过大 | #1–#6 横跨字幕/粗剪/渲染/schema/文档 | 按 §6 版本切分，每版小步、每步可回滚（`rs_run --rollback` + git tag） |

---

## 8. BACKLOG 联动

**本次规划新增/激活条目**（实施时写入 `docs/BACKLOG.md`）：
- `[ ]` **P0** 字幕终点偏移校验（rs_sync 起点-only → 起点+终点）→ 本笔记 #1
- `[ ]` **P0** 字幕后沿策略纠正（不得切进末字 / 延长设上限 / merge 取真实末字）→ 本笔记 #1
- `[ ]` **P0** 消灭 `build_wordline` 句内均分（改"句级整句卡" + `rs_dub align` 补字级）→ 本笔记 #1/#10
- `[ ]` **P0** 断句连词评分纠正 + 连词回归用例 → 本笔记 #2
- `[ ]` **P0** 粗剪重录跨句/段落级检测 + guard 按 reason 分档 + dead_air 检测 → 本笔记 #3
- `[ ]` **P0** 平台字幕预设档案 + 1080×1440 画幅 → 本笔记 #4
- `[ ]` **P0** 删除 `cutflow-prompt` 技能组并归档 → 本笔记 #5
- `[ ]` **P0** videoType 三类型取代 genres → 本笔记 #6
- `[ ]` **P1** ADR-0018 / ADR-0019 → 本笔记 #8
- `[ ]` **P1** 脚本鲁棒性审计（消灭静默降级）→ 本笔记 #7
- `[ ]` **P2** 清理 rs_cut 死代码 + retake 阈值分档 → 本笔记 #11

**激活既有条目**：
- `[→]` BACKLOG `P2`「粗剪 retake 检测的相似度阈值按素材类型分档」→ 并入本笔记 #3/#11。
- `[→]` BACKLOG `P2`「rs_sync 增加『卡片 ↔ 动画卡时间窗』重叠检查」→ 本笔记 #12。
- `[→]` BACKLOG `P2`「fa-zh 强制对齐的偏移自检工具」→ 并入本笔记 #10。
- `[→]` BACKLOG `P2`「成语/固定搭配词表扩充」→ 并入本笔记 #2 禁切表扩充。

**保持未动的欠账**（需真实素材/会话，实施时按素材就位优先级补做）：
BACKLOG `B5 / C4 / D6 / E5`（v5 验收）、`A3 / A4 / A5 / B4 / B5 / C5 / D5`（v4 验收）、`P1 rs_ingest + deliverables.md`（→ 本笔记 #9）。

---

> 实施提示：本文档一经确认，按 §6 路线逐版本落地；每版本完成后回写 `CHANGELOG.md` 并勾选 §8 中对应条目。
