# Changelog

## v0.8.0 (2026-09-12) — 鲁棒性 · S0 摄取 · 配音对齐 · 动画卡重叠（OPTIMIZATION-v7 #7/#9/#10/#12）

**R1 消灭静默降级(#7)**

- `rs_cut._extract_clip` 改为返回 `bool`;审查包抽音频失败写 `review/_DEGRADED.md`,并在该刀 md 里标注 —— 不再 `except: pass`。
- `textopt.card_split / build_cards` 新增 `degrade` 列表:退回长度算法时记录原因;`rs_subtitle.events_from_wordline` 把**单句 DP 失败**降级为"该句退回长度算法"(不再让一句炸掉整条字幕),原因写进 `degradeReasons`。
- `rs_common.resolve_voice`:坏掉的音色卡不再静默 `continue`,找不到音色时点名"另有 N 张卡读取失败"。
- `rs_common.ensure_utf8()` + `rs_doctor` 报告符号 GBK 安全(修 cp936 下 `--report` 崩溃)。

**R2 S0 素材摄取 + 交付清单(#9)**

- 新增 `rs_ingest.py scan <工程>`:`01_materials/` → probe → `manifest.json` + `MANIFEST.md`(probe 失败写 `failed/unavailable` 并点名,不阻塞),并生成 `05_ir/project.skeleton.json`(**不覆盖**已有 `project.json`)。
- 新增 `rs_ingest.py deliverables <工程>` → `06_output/deliverables.md`(成片清单 / 画幅 / 验证等级 / 粗剪与对齐摘要 / 缺失项点名)。
- `rs_run` 的 S0 登记 `rs_ingest.py`;`SKILL.md` 阶段表与命令表同步。

**R3 配音强制对齐 `rs_dub`(#10)**

- 新增 `rs_dub.py align --wordline … (--audio … | --from-asr …) [--write]`:自带 ASR 转写**配音音频**取真实字级时间戳作为参考轴 → `rs_align.retime_to_reference()` 把目标文本锚上去(equal 区间零漂移)→ 逐句漂移报告 `06_output/dub_report.md`;`--write` 写回并置 `charTimingEstimated=False / degraded=False`。
- **ASR 拿不到字级时间戳时拒绝写回**(`DUB_NO_WORD_TS`,退出码 3)并给出 `--backend pkg` 修复提示 —— TTS 路径不再有"估算当字级"的空子。
- `rs_align` 抽出 `_apply_opcodes`(retext 与 dub 共用)+ 新增 `retime_to_reference()`。

**R4 字幕 ↔ 动画卡重叠检查(#12)**

- `rs_sync` 新增 `check_card_overlap()` 与 `--ir / --strict-cards`:artboard 卡片时间窗压住字幕卡时在报告点名(默认告警,`--strict-cards` 才判未通过);普通素材轨(口播/绿幕)不算重叠。

**质量与验证**

- 测试 152 → **165 全绿**；新增 `tests/test_v7_e2e.py`（lavfi 合成素材端到端：S0 摄取 → S1 对齐 → S2 粗剪(带音频探测) → S3 IR → S7 字幕(3:4 平台预设) → S8 真渲染 → 对齐自检 → L0 自检 → 交付清单；无 ffmpeg 时整模块跳过）。

**R5 复核整改（code-review 两轴）**

- 端到端抓到并修掉：`rs_verify` 检查名含 `↔`，GBK 控制台下 `emit()` 崩脚本 → `rs_common` **导入即** `ensure_utf8()`。
- 文档失真修正：`rules/platforms.md` 的"新画幅改两处"→ **三处**（补 `rs_subtitle.STYLES[*].size/margin_v`，并加一致性用例说明）；`rules/align.md` 的 TTS 条目改为描述现状（句级实测 + 句内估算**显式标注**，字级由 `rs_dub` 补）。
- 代码去重：新增 `rs_common.guard_passed()`（粗剪/自检共用一套 guard 口径，替换 3 处重复的 `okByReason` 兜底）与 `rs_common.p95()`（替换 2 处索引式分位）。
- `rs_dub` 报告不再失真：未加 `--write` 时明写"**未写回**"。
- `detect_dead_air` 补 `reason` 分档（短 `breath` / 长 `silence`）。
- 补齐规格项：`rs_sync --legacy-end`（历史工程终点门禁只告警）；平台预设 `cpsMax` 真正被 `rs_subtitle` 消费（此前是死数据）。
- 测试 165 → **170 全绿**（连词 20 句抽样、`--legacy-end`、平台 `cpsMax` 消费、dead_air 分档）。

## v0.7.2 (2026-09-12) — videoType 三类型取代 genres · 技能组精简（OPTIMIZATION-v7 #5/#6/#8）

**R1 删除第二个技能组(#5)**

- `skills/cutflow-prompt/` 内容归档到 `docs/archive/cutflow-prompt/SKILL.md` 后删除;`tools/install.ps1` 现在只安装 `cutflow`。
- 同步清理引用:`skills/cutflow/SKILL.md` 的"AI 生视频"指引改为指向归档;`docs/PLAN.md` 加停用横幅并标注 §6.3(历史段落不改写);`docs/CHANGELOG.md` 的历史记录保留不动。

**R2 `videoType` 取代 `rules/genres/`(#6)**

- **一级枚举**:`talking-head`(纯口播)/ `talking-head+animation`(口播+动画)/ `pure-animation`(纯动画);**预留扩展位** `screen-recording` / `interview` / `drama` / `film-commentary`(只写注释,不建空文件)。
- 新增 `rules/video-types/` 四册,每册固定结构「管线分支 → 节奏参数表 → 结构模板 → CutFlow 对应 → 红线」:
  - `纯口播.md`:绿幕必抠(`cropTopPct` → `colorkey`+`despill`,禁裸 `chromakey`)、虚拟背景、字幕重中之重、动画密度少/零、粗剪必做;
  - `口播+动画.md`:继承纯口播 + artboard 卡片体系,新增**流畅性硬线**(缓动/200–300ms/禁线性匀速/文字 1s·13 字/最短停留 1.5s)与**贴合性硬线**(卡片时间窗必须落在所解说句子的时间窗内、错位 >1 卡不合格、人物与卡片切换时口播不停);
  - `纯动画.md`:场景卡 + 6s 循环背景,**声音来源二选一**(音色卡 TTS / 视频中人物原声),TTS 无字级戳时标 `charTimingEstimated`;
  - `_通用规则.md`:响度/安全区/字幕三定律/节奏/混音/版权 + **类型补充**(原新闻采访·短剧·影视解说的题材红线,标注"暂停维护")。
- `rules/genres/` 六册**先并入再删除**;同步 `templates/brief.md`(videoType + 声音来源 + 平台预设)、`rules/intake.md` 项 0、`SKILL.md` 路由表与 Hard Rule 17、`README.md`、`CONTEXT.md`(新增"视频类型""平台与画幅"两节)。
- **ADR**:新增 `docs/adr/0018-videoType取代genres.md`、`docs/adr/0019-平台字幕预设.md`;`0010` 顶部标注被 0018 取代。

**质量与验证**

- 测试 146 → **152 全绿**。

## v0.7.1 (2026-09-12) — 平台字幕预设 · 新增 1080×1440（3:4）（OPTIMIZATION-v7 #4）

- 新增 `templates/platforms.json`：抖音 / 视频号 / 小红书 / B站 四平台预设（比例、画布、风格、每卡字数、安全区、封面尺寸、时长倾向）。**字幕规格终于有数据可查**，不再靠人记。
- **画幅单一事实源** `rs_common.RATIOS`（9x16 / **3x4** / 16x9）+ `canvas_for()` / `ratio_for_canvas()`。`rs_render`、`rs_brand`、`rs_ir`、`rs_artboard`、`rs_jy_draft`、`rs_verify` 的硬编码/字符串比较全部收敛为查表。
- **新增画幅 1080×1440（小红书 3:4）**：`segmentation.MAX_CHARS` 增 `3x4=15`（可调）、`CPS_MAX` 增 `3x4`；`rs_subtitle.STYLES` 三档样式补 `3x4` 字号与 `marginV`；`project.schema.json` 的 canvas 枚举加 `1440`、outputs 枚举加 `3x4`；`rs_ir.validate` 画布白名单改查表。
- `rs_subtitle` 新增 **`--platform`**（+ `--max-chars`）：**显式 `--style/--ratio/--canvas/--max-chars` > 平台预设 > 内置默认**；未知平台直接 `BAD_PLATFORM` 报错（不静默退回默认，避免悄悄出一版错规格的片子）；`--style` 默认值改为 None 以便让预设生效；输出 data 增 `ratio/platform/canvas/charTimingEstimated`。
- **修掉 CPS 口径错**：`rs_subtitle` / `rs_verify` 原先无论什么比例都取 `CPS_MAX["9x16"]`，现改用 `segmentation.cps_max_for(max_chars)`；`rs_verify` 从 IR 画布反查比例取 `maxChars`。
- 新增 `rules/platforms.md`（平台预设说明 + 安全区表 + 优先级 + 坑位），并在 `SKILL.md` 路由表/命令表登记；新增硬规则 20（卡时间只在释放余量内调整）与 21（画幅/平台只查表）。
- 测试 139 → **146 全绿**：四平台预设覆盖、比例表一致性（新增画幅防漏改）、CPS 按比例取值、schema 允许 3:4、`--platform` 生效、显式参数优先、未知平台报错。

## v0.7.0 (2026-09-12) — 字幕同步 · 断句连词 · 粗剪废片段（OPTIMIZATION-v7 #1/#2/#3/#11）

针对用户三大成片问题（字幕与声音对不上 / 断句切词 / 口播废片段没剪掉）落地。测试 118 → **137 全绿**（新增 `tests/test_v7.py` 19 项）。基线方案见 `docs/OPTIMIZATION-v7.md`。

**R1 字幕↔音频同步三件套(#1)**

- `rs_sync` 补**终点偏移**校验：`endOffsetMs = 卡尾 − (末字 endMs + 20ms)`；新增两个硬失败项「**早退**（终点早于末字 >25ms = 切掉语音）」与「**滞留过久**（终点晚于末字 >350ms）」，外加终点中位数/95 分位（60/120ms）。报告与 `sync_rows.json` 同步扩展。
- `rs_subtitle._enforce_gaps` 改为**锚点有界**：只在「释放余量」内调整（起点 ≤ 首字 `startMs`、终点 ≥ 末字 `endMs`），余量耗尽仍不足 2 帧 → **保持字级精确时间**（对齐精度 > 卡间距）。不再为凑间距切掉末字语音（"偏快"根因）。
- `_extend_short` 设上限 = 末字 `endMs` + **0.30s**，且不越过下一卡（"字幕滞留到停顿里"根因）。
- `_merge_short` 合并同步锚点；`_kar_text` 末字结束时间改取**末字真实 `endMs`**（卡尾可能被可读性延长）。
- 新增 `snap_events_to_frames`：ASS 时间量化到帧（起点向下、终点向上）；`rs_subtitle --fps / --no-snap`。
- `rs_align.build_wordline` 无字级时间戳时置 `charTimingEstimated=True`、逐字打 `estimated`，`degradeReasons` 明写"卡内位置为估算(不可当字级用)"。**落地修订**：原计划的"整句一卡"实测会让长句超字数、直接伤观感，故**保留按 `max_chars` 出卡**，但卡内位置不再被当成字级（显式标注 + 拒绝用于卡拉OK + 报告点名）；真字级由 #10 `rs_dub align` 补齐。
- `segmentation._relax_gaps` 同样改为锚点有界。
- `CPS_MAX` 取值新增 `segmentation.cps_max_for(max_chars)`，替代 `rs_subtitle` / `rs_verify` 里写死的 `CPS_MAX["9x16"]`（为 #4 多画幅铺路）。

**R2 断句连词切词(#2)**

- `cut_score` 方向纠正：**以连词/引导字（`NO_TAIL`）收尾 −2.0 强惩罚**；**以连词（`CONJ_HEAD`）起首 +0.5**（从句边界优先，与本节引用的 BBC 一致）。旧实现是 `not in CONJ_HEAD` 才加分，方向正好相反 —— 用户实例「…店铺违规**而** / 被连带处理…」即由此产生。
- 新增 `CUT_COST = −1.0`（每刀固定代价）：治"过度切分"（同一句被切成一片 4 字卡同样是断句拉跨）。
- `NO_TAIL` 覆盖 `而但并且或及与则却故因若虽如由然所`；`REGRESSION` 新增 3 条连词用例。
- 用户实例实测修正为：`可能因为其中一家店铺违规 / 而被连带处理最终一同遭殃`。

**R3 粗剪废片段(#3/#11)**

- `detect_retake` 重写：**滑动窗口内任意两句**（句数 ≤6 或间隔 ≤30s）比对；一刀删掉**全部旧尝试**（`_chain_merge` 把连续重录刀串成一刀，不留几十毫秒碎片）；出点取「最后一次尝试起点 − 60ms」留自然起音并满足 `tailKeep`。
- 新增 `detect_retake_block`（整段重来：连续 ≥8 字逐字相同 → `false_start`）。
- 新增 `detect_dead_air`：给 `--media <源素材>` 时用 ffmpeg `silencedetect` 探音频能量（含纯函数 `parse_silencedetect`），否则退回字间 gap（≥1.2s）。
- 新增 `detect_self_negative`（`说错了/再来一遍…` → `off_topic`，**仅 review，不自动删**）。
- **guard 按 `reason` 分档**：`silence/breath/filler` 保持四项全过；`retake/false_start/stumble/repetition/off_topic/manual` 只硬要求「不切断字内音素 + 后留 ≥60ms」。**`wordClipped` 永不放松**。`guard` 新增 `okByReason`/`required`，`classify` 改用它，`cut_report.md` 分列硬过项/告警项，`rs_verify.check_cutlist` 同步。
- `rhetorical_suspect` 反向保护**只作用于 `silence`/`breath`**（重录/整段重来删的是一整段内容，不是修辞停顿）——此前它会把单刀/末刀一律降级为 review。
- 清理 `detect_filler` 死代码；`DETECTORS` 扩为 silence/dead_air/filler/repetition/retake/retake_block/self_negative；`rs_cut` 新增 `--media` / `--retake-ratio`。

**测试与夹具**

- 新增 `tests/test_v7.py`（19 项）：终点偏移/早退/滞留、锚点有界、估算标注、连词不落卡尾、跨句重录、多次旧尝试合并、guard 分档、段落重来、`silencedetect` 解析。
- 修正 `tests/test_v6.py::test_rs_sync_karaoke_ass_sync_ok` 夹具：该 ASS 终点相对其 wordline 末字多出 870ms（旧口径不校验终点才成立），把夹具的字级间隔调为 280ms 使其自洽；测试意图（override 标签剥离 → SYNC_OK）不变。

**文档**

- `rules/subtitles.md`：连词禁切 / 打分函数 / 释放余量边界 / 终点门禁。
- `rules/roughcut.md`：新检测器、guard 分档表、门禁。
- `rules/align.md`：`charTimingEstimated` 坑位。
- `docs/OPTIMIZATION-v7.md`：#1 方案 3 记录落地修订。

**R4 附带（部分 #7 鲁棒性）**

- 新增 `rs_common.ensure_utf8()`：stdout/stderr 切 UTF-8（`errors="replace"` 兜底）；已被重定向/被测试框架替换的流**静默跳过**，绝不因它抛异常。
- `rs_doctor --report` 的符号改为 GBK 安全（`√ / × / △` + `[OK] / [FAIL]`）—— 此前在 cp936 控制台会因 `✓` / `✅` 触发 `UnicodeEncodeError` 直接崩掉报告。

## v0.6.0 (2026-09-11) — seg 缓存 · retext 回灌 · 卡拉OK · JJAV2815 一条龙实测

P0 三件套(OPTIMIZATION-v6.md)+ 真实素材一条龙实测驱动的 11 项修复。测试 68 → **118 全绿**(test_v6.py 50 项)。

**R1 rs_render seg 级缓存(ADR-0013 落地)**

- 内容寻址段缓存:`seg_key = hash(clip 内容指纹 + chroma/bg + fps/画布 + rs_render 脚本哈希)`,落 `06_output/_build/<ratio>/segcache/`,保留最近 3 代(SEG_CACHE_KEEP=3)
- 上层步骤亦有 `step_keys.json` 门禁:只改字幕时 seg 8/8 命中、concat/compose/mix 全跳过,重出片 174s → **54s**
- `--explain` 逐段显示命中/重渲;`--clear-cache` / `--no-cache`

**R2 校对回灌 `rs_align retext`**

- `rs_align retext --take N --file proof.txt [--dry-run]`:SequenceMapper char 级 opcodes 把校对稿对回 wordline;equal 零漂移、replace 区间均分、insert 挤进 [prev.end, next.start];相似度 <0.5 拒绝
- 统计平铺在 `doc["retext"]`(editChars/similarity/inserted/deleted);重跑 resplit + remap 一条龙
- 实测:623→625 字、相似度 94%、句子 23→17,下游字幕/切点全量跟进

**R3 卡拉OK 逐字字幕**

- `rs_subtitle.py --karaoke`(需 pkg 后端字级时间戳,`--allow-degraded` 可降级):每字 ASS `\kf`,字间停顿计入前字;已唱 `&H0000E5FF` 暖黄(BGR)/未唱白
- 实测:625 字 77 卡全 `\kf`,无缺 startMs、无标点孤卡、无领头标点卡、无超 12 字形卡

**R4 JJAV2815 一条龙实测修复(286MB 真实素材,S0–S11 全链路)**

- **coverage 语义修正**:旧公式(字时长和÷末字时间)对真实字级时间戳恒判 ~75%,语义颠倒;改为**跨度覆盖率** =(首字起点→末字终点)÷转写声明区间,<0.99 软警告
- **段 0 视频膨胀**(4.26s→85.33s):ffmpeg git-master 回归,overlay filtergraph 且 `-ss` 0/缺省时输入 `-t` 按「帧数 = t × time_base_den」解释(tb=1/600 ×20);修复 = 段命令输出侧 `-t` 钳制 + 回归测试
- **绿幕人物半透明幽灵**:同构建 chromakey 输出 alpha 全坏(人物 α≈0);修复 = 全部改 **colorkey**;`-vf`+JPG 丢 alpha 会掩盖此 bug
- **字幕领头标点**:DP 候选边界去掉"标点前"并入禁止集(含半角);karaoke attach 标点跟随前字所在卡
- **rs_sync × 卡拉OK**:`parse_ass` 不剥 ASS override 标签(`{\kf28}店…`)→ 与 Wordline 84/84 unmatched → SYNC_FAIL;修复 = 解析时剥 `{...}` 后匹配
- **卡拉OK 字形预算**:挂字前移到必并/合规校验之前(以"显示字形"为唯一口径,`_clean_card` 剥掉的标点会在 `\kf` 层经 chars 带回,曾冒出 13-14 字卡);合并同步拼 `chars`
- **必并死区**:必并线 0.8s → MIN_DUR_S(0.83s)对齐,0.81s 卡不再"既不并也延不满";新增第二遍向下一卡吞并(起点取短卡);84 卡收紧到 77 卡,L0 验证全过
- resplit 孤立标点并入前句;`_PUNCT_ONLY` 显式跳过纯标点卡(`_clean_card("?")` 保留语气是设计,不能当 skip 判据)
- drawtext 中文必须单 face TTF(simhei),msyh*.ttc 多 face 集合丢字形;中文文案走 `textfile=`
- concat 相对路径双重拼接:`render()` 入口 base_dir 绝对化
- 实测战报全文见 `rules/compose.md` §实测战报

**R5 P2 清理**

- `rs_asr.py` 降级为 fun_asr.py 薄壳(标 deprecated);fun_asr `maybe_reexec` 剥 docstring 断言修复

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
