# BugReport — 纯口播全链路复测(2026-09-13)· 交上游 Agent 检测修复

> 复测工程:`E:\平日资料\视频工程\20260913-口播分句测试-口播绿幕`(素材 JJAV2815.MP4,1080×1920@30,167.6s 绿幕口播)
> 复测范围:S0 摄取 → S1 pkg 字级对齐+retext → S2 粗剪(7 刀,167.6→139.2s)→ S3 IR+手注 chroma → S7 字幕(71 卡)→ S8 烧录 → S9 sync。
> 结果:字级对齐/粗剪 guard/字幕合规/ass↔wordline 对齐全部达标;但**首版成片音频内容损坏**(用户实测:片头问句反复出现、全程音画错位)→ 已定位 B1 并用绕过法修复重出。
> 本报告按严重度排序,每条含:文件:位置 / 机制 / 本工程绕过 / 建议修法。
> ✅ **修复记录(v0.8.2,B1–B10 逐条):见 `docs/BUGFIX-20260913-B1-B10.md` 与 `docs/adr/0021-S9成片音频内容闸.md`;回归 `tests/test_v9.py`。**

---

## B1【致命·音画】step_mix 忽略音频 clip 的 sourceInMs —— 每段人声从源文件 0s 开始取

- **位置**:`scripts/rs_render.py` → `step_mix()`(L498–540)与 `_voice_chain()`(L481)
- **机制**:step_mix 对每个人声 clip 只做 `cmd += ["-i", 源文件]`(**无 -ss 输入寻址**),`_voice_chain` 产 `aresample→aformat→loudnorm→volume→atrim=0:durationMs→adelay=startMs`。`atrim=0:dur` 从**整份源文件**的 0s 取前 dur 毫秒——`sourceInMs` 完全没被消费。于是 keep[1](源 5.04s 起)在时间轴 4.26s 处**又播一遍源 0–11.5s(含片头问句)**,keep[2] 在 15.7s 处再播源 0–29.6s……8 段 `amix=duration=longest` 相互叠加:用户听到"片头问句反复出现、内容重叠、全程音画错位"。
- **佐证**:v0.8.1 的修复(先 atrim 后 adelay)只治了"adelay 后裁流"的旧伤(当时实测音轨缩到 13.9s);sourceIn 缺失是**同函数里的第二个洞**,多 clip 人声场景必炸。单 clip(整段无粗剪)工程不会触发,所以部分测试没暴露。
- **本工程绕过(已验证)**:按 `cutlist.applied.json` 的 keep 区间预抽完整人声 `03_assets/voice_full.wav`(139.245s,与 IR 时长精确一致),IR 音频轨换单 clip `{src, startMs:0, durationMs:139245, role:"voice"}`——单 clip 下 sourceIn 缺省为 0,链条语义正确。
- **建议修法**:输入寻址 `["-ss", sourceInMs/1000, "-i", src]`(与 step_segment 的视频路径一致),或链首 `atrim=start=(sourceInMs/1000):end=(sourceInMs+durationMs)/1000,asetpts=PTS-STARTPTS` 后再走现有 atrim/adelay;补一条多段人声的回归测试(非 0 sourceIn)。
- **流程教训**:这是 v2 重跑(2026-09-13 早上)已知坑,绕过法已沉淀,但复测时未核对已知坑清单直接信任 rs_render——**已知坑绕过应在 SKILL.md 或 step_mix 报错信息中显式提示**,不能只靠记忆。

## B2【致命·渲染】转场字段名不一致:rs_ir 写 "ms",schema/rs_render 认 "durMs"

- **位置**:`scripts/rs_ir.py:147` 写 `{"type":"fade","ms":int(xfade_ms)}`;`templates/project.schema.json` transition 定义与 `step_concat()`(`tr.get("durMs", 500)`)都认 `durMs`
- **机制①**:"ms" 是未知字段 → 默认 **500ms** 生效,7 处转场每处吞 0.5s,xfade offset 按 IR 理论时长累积 → 成片视频流 135.73s vs 音频 139.30s,后半段音画渐进错位、尾帧冻结 3.5s。
- **机制②**:即使字段名修对,`durMs:8`(<1 帧,33.3ms@30fps)会让 xfade 链直接坍缩——实测输出只剩末段 943 帧(31.4s)。
- **本工程绕过**:删全部 `clip.transition`,走 step_concat 的无损 concat 分支(跳切风格本就不需要混合转场)。
- **建议修法**:rs_ir 改写 `durMs`;step_concat 对 `tdur < 1帧` 直接弃用转场走 concat(或 clamp 到 1 帧并同步修 offset);补"转场吞时长的音画对齐"回归断言(视频流时长 vs IR finalDurationMs 差 ≤1 帧)。

## B3【高】rs_subtitle 帧吸附在 enforce_gaps 之后 → 卡片重叠超标

- **位置**:`scripts/rs_subtitle.py`:`_enforce_gaps()`(L438)先保证卡间距 ≥2 帧(只在释放余量内调),`snap_events_to_frames()`(L467)随后 start 向下取整/end 向上取整,把相邻卡推回 **30–40ms 重叠**;rs_sync 容差 34ms(≈1 帧)→ 判 FAIL(本次 4 对)。
- **本工程绕过**:`--no-snap`(字级精确时间优先,符合 Hard Rule 20 精神),rs_sync 中位 5ms / 零重叠全绿。
- **建议修法**:snap 之后重跑一次 `_enforce_gaps`;或 rs_sync 容差改为"≤1 帧"整帧语义并与 fps 联动。

## B4【高】rs_cleanup 的 keep 规则会删交付物

- **位置**:`scripts/rs_cleanup.py`(dry-run 实测)
- **机制**:对含成片的正式工程 dry-run,删除名单包含 `final_*.mp4`、`subtitles.ass`、`metadata.*`、`sync_report.md`、`verify_report.md`、`cards.json`,keep 只剩 `master.srt`——直接违反 ADR-0007 必留清单(成片/字幕/封面/brief/转写校对稿)。
- **建议修法**:keep 规则加:06_output 下 `final_*.mp4`、`subtitles.ass`、`master.srt`、`metadata.*`、`*report*.md` 默认必留;`06_output/_build` 与 `_src_*`/`_*` 探针件才进删除名单。

## B5【中】cards.json(审计)时间与最终 ass 漂移,甚至倒挂

- **机制**:必并(_merge_short)/延长(_extend_short)/防御调整发生在事件层,`cards.json` 落的是调整前时间:同一张卡 cards.json 0.24s vs ass 1.24s;"平台嘛"卡 cards.json `38.51→38.34`(倒挂)而 ass 830ms 正常。任何下游对账/交付物若以 cards.json 为准都会错。
- **本工程绕过**:交付 txt 从 ass Dialogue 派生(剥 override 标签)。
- **建议修法**:cards.json 在全链调整完成后回写最终时间(或改名为 cards_draft.json 并在文件头注明"非最终")。

## B6【中】rs_bench 尾部采样假成功(空输出仍报 OK)

- **机制**:`sample_points()` 按容器时长(duration=139.3s)布点,但视频流常短于容器(音频垫尾)→ 尾部点(137.3/138.3)零帧,`xstack` 等不齐输入 → **空输出且 rc=0**,BENCH_OK 照报,PNG 不落盘。
- **建议修法**:按 `ffprobe -select_streams v:0` 的流时长布点;编码后校验产物存在且非空,否则 die。
- **附注**:N 个采样点在 ceil(N/cols)×cols 布局的最后空位是黑色——是网格留空不是黑帧,别误判。

## B7【中】override(Agent 断句裁决)span 语义易错且只有全量替换

- **机制**:`subtitles_override.json` 的 span 是 **wordline.chars 原始索引(含标点/空格条目)**,按"内容字数"做算术偏移必然切错位(本次第一版 override 产出"套人马 而线上嘛是虚拟空间运"这类串卡);且 cards 全量替换,微调一张卡要重给全部 span。
- **本工程绕过**:文本锚定法——DP 卡文本去标点后在内容字串 `S` 上顺序定位,`assert ''.join(归一化卡)==S` 后映射回原始索引;卡尾标点至多带一个(防"。，")。
- **建议修法**:提供文本锚定模式(`{"cards":[{"text前缀","text后缀"}]}` 由工具自己定位);或支持部分替换(未提及的卡沿用 DP 结果)。

## B8【低】rebuild.py 级联起点陷阱:手改 IR 会被 S3 重生成冲掉

- **机制**:`05_ir/rebuild.py` = `rs_run --from S3 --force`,强制重跑 `rs_ir build --from-cutlist` → **重生成 project.json**,手注的 chroma/background/转场修正/单 clip 音频全丢。
- **建议修法**:05_ir/rebuild.py 的文案与 SKILL §5 表应写明:手改 IR 后重渲染用 `06_output/rebuild.py`(从 S8,只用现有 ass,不碰 IR);或 rs_ir build 检测到现存 project.json 含手注字段(_meta.chromaInjected 等)时拒绝覆盖。

## B9【低】rs_run 阶段后的全量 L0 在中间态必然误报

- **机制**:`rs_run --only S2` 等阶段跑完即跑全量 L0,此时字幕/IR 未生成,"字幕合规 ✗" 必现——流程噪音,易误导为失败。
- **建议修法**:L0 按"已完成阶段的产物域"裁剪检查项,未涉及项标"未涉及"而非 ✗。

## B10【低·验证盲区】无"成片音频内容"机械闸——B1 级损坏全绿通过

- **机制**:本次首版成片音频内容重叠损坏,但时长正常(139.3s)、L0 六项过、rs_sync 全绿——因为所有闸只对账 ass↔wordline 与时长,**没有任何检查验证成片音轨内容**。rs_sync 有 `video` 字段但未接。
- **建议修法(本次已人工验证可行)**:交付前对成片音轨跑自带 ASR,与 wordline 文本 diff:①片头句出现次数=1;②归一化相似度 ≥ 阈值;③无重复段。可作为 S9 的新检查项(素材已是文本真相源,成本一次 ASR)。

---

## 迭代建议(非 bug,断句质量)

1. **DP 断句语义单元惩罚**:否定词跨卡("不|属于"被判最优,正确断法"店群运营|不属于拆分收入"排第三)、复合词跨卡("经营|主体""运营|效率""店群|企业")——建议:跨卡的词对若构成高频搭配/否定结构则罚分;"的"字头卡("的市场版图""的客流量差异")降权。
2. **--terms 即断词保护表**:补"经营主体"后该句立刻出现保词候选——brief 术语表应强制喂给 rs_subtitle,并在歧义候选里显式展示 terms 命中。
3. **rs_cut 可加尾部黑场检测器**(本次源尾无黑场,但 red line 清单里"黑帧"目前只靠 L1 目测)。

## 本次修复后状态(供上游回归对照)

- 成片:`06_output/final_口播分句测试_916.mp4`(139.3s,视频流 139.27s/音频 139.30s,Δ1 帧)
- 音频为绕过结构:IR 音频轨 = 单 clip `03_assets/voice_full.wav`(139.245s,按 keep 预抽)
- 自检:L0 六项过;sync 中位 5ms / p95 25ms / 零重叠;L1 网格目测过(`06_output/bench_916.png`)
- **成片音轨 ASR 内容校验(B10 建议闸的手工执行,通过)**:对修复后成片音轨重跑 ASR——片头问句出现 **1 次**(修复前每段重复);归一相似度 **0.9800**(vs 校对稿 574 字,片侧 575 字);仅 2 处一至二字差异("四套"被二次 ASR 听成"是讨/思考",AAC 重编码噪声,非内容问题);语序与结尾一致。**该检查建议按 B10 固化为 S9 新闸**。

## 附:本次时序账(三次渲染的差异,便于对照复现)

| 渲染 | 转场 | 音频 | 结果 |
|---|---|---|---|
| 第1次 | rs_ir "ms":8 → 实际 500ms xfade | 8 clip 直混 | 视频被吞 3.27s(135.7s),音频内容损坏(每段重片头) |
| 第2次 | durMs:8(字段修正) | 8 clip 直混 | 8ms<1帧 → xfade 坍缩,视频只剩末段 31.4s |
| 第3次(交付) | 无转场,无损 concat | 单 clip voice_full.wav | 视频 139.27s / 音频 139.30s ✓,内容校验 ✓ |
