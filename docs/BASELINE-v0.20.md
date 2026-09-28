# BASELINE v0.20 · 仓库现状基线（第零册固化）

> **性质**：`CutFlow-多册迭代计划` 第零册（§0.2/0.3/0.4/0.5 + 附录 A）固化为仓库内基线文档，供后续各册收口时对照。**本文件只读不改**（后续迭代只允许新增 BASELINE-v0.x，不改写本文件历史结论）。
> **基线**：`v0.20.0`，HEAD = `e775e56`（`git rev-parse HEAD` 实测）。
> **复核方式**：2026-09-27 由 Agent 在 `e775e56` 工作区**逐条实测**（`wc` / 定向 `grep` / `python -c` / `git rev-list`），非转抄计划文档。实测值与计划文档不符之处以实测为准，集中列于 §1。
> **图例**：`path:line` 均为仓库根相对路径；`[实测]` = 本日命令复测；每节末尾给复现命令。

---

## 1. 实测与计划文档的差异（以实测为准）

| # | 项 | 计划文档值 | 实测值（2026-09-27） |
|---|---|---|---|
| D1 | `skills/cutflow/SKILL.md` 行数 | 271 | **270**（与文内"270 行体量锁"自述一致） |
| D2 | `skills/cutflow/scripts/` 脚本数 | 52 个 `*.py` / `rs_*` 47 个 | 顶层 **46** 个 `*.py`（`rs_*` **44** + `segmentation.py` + `textopt.py`）；加 `rs_fx/` 3 个 = 49；`vendor/` 35 个第三方另计 |
| D3 | `rs_edit.py` 行数 | 2334 行 / 124KB | **2636 行** / 124,219 B（≈124KB 吻合；行数差 +302，HEAD 与工作区一致，以 2636 为准） |
| D4 | `docs/` 顶层 md 数 | 22 | **20**（清单见 §2；`docs/archive/` 仅 `cutflow-prompt/SKILL.md` 1 文件，与计划一致） |
| D5 | 历史迭代文档体量 | ≈3300 行 / 230KB | 行数吻合：15 份 **3333 行**；字节实测 **256,783 B（≈251KiB）** |
| D6 | v2 迭代提交数（`8701cdd..e775e56`） | 13 提交 | `git rev-list --count` = **14**（diff 统计 400 文件 / +25730 / −398 与计划一致） |
| D7 | 断句回归集 `REGRESSION` | 14 例 | **11 例**（`len(segmentation.REGRESSION)`，segmentation.py:111-136） |
| D8 | 一次新会话最小必读 | 13 个 md / ≈1555 行 | 计划未列清单。按流水线重建 13 册 = **1508 行**；加 `video-types/纯口播.md` 14 册 = **1552 行**（≈计划值）；叠加 README+CONTEXT 全景 ≈**1932–1976 行**（计划称 >2000） |
| D9 | 9:16 安全区口径 | 6 处同一口径 | 6 处位置属实，但**数值已漂移**：主口径底 25%/顶 12%；`rules/artboard.md:96` 写顶 12%/**底 30%**；`rules/video-types/_通用规则.md:17` 写 **底 26%**（marginV=500）；`CONTEXT.md:58` 字幕卡写"9:16 每行 ≤16 字"（与 10–12 字口径漂移） |
| D10 | 素材/目录文件路径 | `assets/manifest.json`、`templates/effects/catalog.json` | 全路径为 `skills/cutflow/assets/manifest.json`、`skills/cutflow/templates/effects/catalog.json`（计数 119 / 317 完全一致） |

---

## 2. 体量基线

| 维度 | 实测值 | 证据 |
|---|---|---|
| 版本 | v0.20.0（2026-09-26，M11–M15，"测试 783 → 948+"）；HEAD=`e775e56` | `docs/CHANGELOG.md:3`、`git rev-parse HEAD` |
| v2 范围 | `8701cdd..e775e56` 实测 **14 提交**；diff = 400 文件 / +25730 / −398 | `git rev-list --count 8701cdd..e775e56`、`git diff --shortstat 8701cdd..e775e56` |
| 测试 | `tests/` 根 **56 个 `.py`** = 54 个 `test_*.py` 门禁 + `make_fixtures.py` + `check_manual_cmds.py`；`probes/` 12 个不入门禁；**无** `conftest.py` / `pytest.ini` | `ls tests/*.py | wc -l`、`tests/README.md:7-17` |
| 脚本 | `skills/cutflow/scripts/` 顶层 **46** 个 `*.py`（`rs_*` 44）；`rs_fx/` 3 个；最大 `rs_edit.py` **2636 行 / 124,219 B**，次大 `rs_render.py` 2004 行 | `wc -l skills/cutflow/scripts/*.py | sort -rn` |
| 规则分册 | `rules/` **24 册** + `rules/video-types/` **9 册** | `ls skills/cutflow/rules/*.md | wc -l` |
| 文档 | `docs/` 顶层 **20 个 md**；历史包袱 15 份 **3333 行 / 256,783 B**（OPTIMIZATION-v4~v7、PLAN、HANDOFF×2、DIAGNOSIS-HANDOVER、ITERATION×4、BUGFIX、BUGREPORT、REVIEW-20260916）；`docs/archive/` 仅 1 文件 | `ls docs/*.md | wc -l`、`wc -l docs/{历史15份}` |
| 素材资产 | `skills/cutflow/assets/manifest.json` **119 条**（sfx 40 / element 49 / huazi 14 / bgm 16）；`skills/cutflow/templates/effects/catalog.json` **317 条**（可执行 186 / 不实现 78 / 登记待实现 53） | `python -c` Counter 实测 |

```bash
# 复现
git rev-parse HEAD && git rev-list --count 8701cdd..e775e56
wc -l skills/cutflow/SKILL.md README.md; ls tests/*.py | wc -l
wc -l skills/cutflow/scripts/*.py | sort -rn | head -3
python -c "import json,collections; m=json.load(open('skills/cutflow/assets/manifest.json',encoding='utf-8')); print(collections.Counter(i['kind'] for i in m))"
```

---

## 3. 内容组织现状（Token 消耗大的结构性来源）

### 3.1 入口文件

| 文件 | 行数 | 要点 |
|---|---|---|
| `skills/cutflow/SKILL.md` | **270** | §1 决策速查 13-67（55 行，20%）、§2 管线总览 68-94、§3 Hard Rules 95-124（30 行，11%）、§4 命令速查 125-174（50 行，18%）、§9 反模式 245-270（26 行，10%） |
| `README.md` | **296** | 与 SKILL.md **四块同构重复**：S0–S11 流水线表（:66）、手改重建表（:80）、命令速查表（:222）、Agent 治理表（:253） |
| `AGENTS.md` / `CLAUDE.md` | 各 **44** | **内容 100% 相同**（`diff` 全等） |
| `CONTEXT.md`（仓库根） | 128 | 术语表；:58 字幕卡字数与主口径漂移（见 D9） |

### 3.2 同一口径多处硬编码（重复口径表，实测）

| 口径 | 处数 | 位置（实测行号） |
|---|---|---|
| 总线响度 `-14 LUFS / -1 dBTP` | **8 文件 9 行** | `README.md:165`、`SKILL.md:107`、`rules/compose.md:72`、`rules/selfcheck.md:9`、`rules/verify.md:29`（写作验收带 I∈[-15,-13]）、`rules/video-types/_通用规则.md:9-10`、`docs/capability-matrix.md:22`、`references/ffmpeg-recipes.md:17`（写作 `I=-14:TP=-1.0`） |
| 每卡字数 / CPS ≤9 / `maxChars` | **6 文件** | `SKILL.md:110`、`README.md:166`、`rules/subtitles.md:90-91,102`、`rules/platforms.md:18,20,28-31,58,67`、`rules/video-types/_通用规则.md:21`、`CONTEXT.md:58`（漂移值 ≤16 字） |
| 9:16 安全区（底 25% / 顶 12%） | **6 文件（数值漂移，见 D9）** | `SKILL.md:108`、`rules/subtitles.md:173`、`rules/platforms.md:28-29`、`rules/branding.md:54-55`、`rules/artboard.md:96`（30%）、`rules/video-types/_通用规则.md:16-17`（26%） |
| 对齐偏移 ≤40ms | **5 文件 6 行** | `SKILL.md:89`、`README.md:77,168`（:168 为 EBU R37 音画同步告警阈，同族口径）、`rules/verify.md:26`、`rules/align.md:146`、`rules/subtitles.md:184` |
| 粗剪 margin 前 150 / 后 300ms | **3 文件** | `README.md:169`、`rules/roughcut.md:180`、`rules/editing-grammar.md:245,247,347` |

```bash
grep -rn -- "-14 LUFS\|I=-14\|\[-15,-13\]" README.md CONTEXT.md docs/capability-matrix.md skills/cutflow/SKILL.md skills/cutflow/rules/ skills/cutflow/references/
grep -rn "maxChars" skills/cutflow/SKILL.md README.md CONTEXT.md skills/cutflow/rules/{subtitles,platforms}.md skills/cutflow/rules/video-types/_通用规则.md
```

### 3.3 Hard Rules 归属

25 条 Hard Rules（`SKILL.md:95-124`，grep 计数 25）中约 22 条属于某个具体分册的内容复述（如 Rule 14 竖屏字数 → `rules/subtitles.md`、Rule 12 安全区 → `rules/platforms.md`、Rule 11 响度 → `rules/compose.md`）；真正入口级编排纪律仅 3 条：**Rule 1** 决策只查 brief（:97）、**Rule 6** 验证分级（:102）、**Rule 25** 禁止为一次性任务现写脚本（:121）。另 `rules/video-types/_通用规则.md:40-48` 残留"原 genres 六册并入，暂停维护"历史堆叠段。

### 3.4 一次新会话的最小读数（实测重建，计划未列清单）

- "一句提示词 → 一条竖版口播成片"理想按需加载：**13 册 = 1508 行**（SKILL 270 + intake 73 + asr 153 + align 149 + roughcut 195 + tts 27 + compose 94 + subtitles 244 + platforms 70 + verify 109 + selfcheck 19 + cover 21 + meta 84）；加类型分册 `video-types/纯口播.md`（44）= 14 册 **1552 行** ≈ 计划值 1555。
- 叠加 `README.md`（296）+ `CONTEXT.md`（128）→ 全景 ≈**1932–1976 行**。
- 手改场景再叠加 `incremental`(238) / `jianying`(32) / `selfcheck`(19) / `archive`(39) / `sfx`(102) / `editing-grammar`(439) / `edit-op`(382) = **+1251 行**（≈计划 +1250）。
- **结论**：同一口径在 SKILL + README + 4~6 册 rules 反复出现，是"Token 消耗大"的结构性来源（第一册治理对象）。

---

## 4. Agent 视频信息视野边界（"瞎子剪辑"根因）

1. **结构化产物全是"时间轴条目"**：`shots.json` schema 仅 `{index,startMs,endMs,durMs}`（`rs_shot.py:16,84-85`）、wordline/beats/screen/reframe_plan/静音区间同类；素材清单"内容摘要"栏**明确留白待 Agent 手填**（`rs_ingest.py:255`）。
2. **画面信息 = 像素网格**：`rs_frames.py` 默认 `--every 10`、`--tile 3x3`、上限 24 帧（:35-36,:19）；`rs_bench.py` 采样 = 首尾各 2 点 + 前 6 剪点 ±1.5s（×2）+ 随机 3 点 = **≤19 点**（`rs_bench.py:40-48`）——只出网格 PNG，无坐标、无描述。
3. **唯一画面"描述文本"通道**是 `rs_sense.py` 的 OCR+VQA，但它**只吃单张图片**（`rs_sense.py:1`），且抽帧网格**不会自动送进 VQA**，属备选件。
4. **镜头级信息孤立**：全 `scripts/` 仅 `rs_shot.py` 自身引用 `shots.json`；`rs_cut` 的 `DETECTORS` 9 个检测器全为音频/文本类（`rs_cut.py:713`）——镜头切分与粗剪/wordline 两条线互不相交。
5. **粗剪是"听"着剪的**：画面唯一参与路径 = 录屏 `waiting` 的帧差静止检测（`detect_waiting`，`rs_cut.py:684-710`，消费 `rs_screen` 的 screen.json）。
6. **画面语义能力整类缺失**：`rs_fetchable` 能力表画面相关仅 `vision.shot/matting/track/cv`（`rs_fetchable.py:90-115`）；且 `vision.track` READY 档**真实实现未部署**——`ready_subject_boxes()` 无条件 `return None`（`rs_reframe.py:155-165`），横转竖**一律降级 static-center**。
7. **构图/语义匹配被显式搁置**：`rules/editing-grammar.md:91-93`「待契约升版 + `text.broll` READY 档（第二波）；此前禁用」。
8. **制度性禁止看画面**：`rs_verify --level L1` 只产出清单 + 抽帧命令（`l1_payload`，`rs_verify.py:1160-1172`），帧量由 `rs_bench` 的 `cuts[:6]` 隐式封顶；`rs_edit` context **硬上限 12KB 且"绝不含视频帧路径"**（`rules/edit-op.md:357,361`）。

> **根因一句话**：CutFlow 把"画面理解"整体外包给 Agent 的眼睛，却又用 ≤19 帧稀疏抽帧 + 12KB 无图 context 限制它；当 Agent 无原生视觉或抽帧错过关键帧时，**系统里没有任何一层替它"读画面"**——这是感知层缺失，不是提示词问题（第三册治理对象）。

---

## 5. 字幕断句与分词现状

### 5.1 机制（两层分离，ADR-0001 修订）

```
一句话 →【卡切分 segmentation（DP）】→ 卡 →【行断开（评分折行）】→ ASS
  候选边界 = 标点/空格后/字级停顿 ≥200ms/连词前；禁切表 = 专名/成语/数量词/的得了着之/ASCII 词内/引文括号/连词收尾
  打分 = 标点层级 + 归一化停顿 + 虚词/连词/词内 + CUT_COST + 长度失衡 + 尾卡过短（segmentation.py:307-335）
  硬约束 = 每卡字数 / CPS / 单卡 0.83–7s / 卡间距 ≥2 帧
```

### 5.2 根因链 R1–R4（"门禁强制断句"四机制，实测锚点）

| # | 机制 | 位置（实测） | 后果 |
|---|---|---|---|
| **R1** | DP 之后一串"可读性后处理"**反写断句结果**：`_merge_short` → `_extend_short` → `_enforce_gaps` → `_drop_ghost_cards`（<100ms 幽灵卡并卡/丢弃），该链在 main 与 override 两条路径**各有一份**；另有 `reabsorb_cuts` 末卡回吸、尾部孤儿卡吞并 | `rs_subtitle.py:355-361`（主链）、`:650-654`（第二份）、`:776-832`（_merge_short，含 ：807-831 第二遍"向下一卡吞并"）、`:753-758`（丢字路径）、`segmentation.py:403-501` | DP 选出的语义最优解**未被重新打分**即被合并/吞并/丢字 → 异常换句；合并后仅 `check_constraints` 事后报警（:362-364），不重规划 |
| **R2** | 合并**不做 CPS/时长回检**：`_merge_short` 只守 `len(text) ≤ max_chars`，时长取区间并集 | `rs_subtitle.py:776-832` | 出现 CPS 违规卡，下游 `rs_verify` 才报 |
| **R3** | 用户/Agent 断句方案（override）**编译期被强拆**：超长 request 只在文案本有停顿处拆，拆不动留痕交 `rs_verify` 硬失败；乱序/重叠直接 `BAD_OVERRIDE` | `rs_subtitle.py:476-537`（预检；:519-524 不强拆→硬失败）、`:414-469`（定位与校验）；`rules/subtitles.md:237,239` | Agent 的正确断句意图被打回或改写 |
| **R4** | DP 软约束**缺语义层**：无否定词/复合词跨卡罚分、无"的"字头卡降权；`--terms` 未自动从 brief 喂入 S7 | `segmentation.py:307-335`（cut_score 无此项）；`docs/BACKLOG.md:99`（I1 仍成立）、`:100`（I2 部分落地：rs_run S7 命令未带 --terms） | 「不\|属于」「运营\|效率」类跨卡仍可能被 DP 选中 |

### 5.3 分词侧现状

`word_spans()` **jieba 优先 + 内置 COMMON_WORDS 兜底**（`segmentation.py:271-302`），`PROTECTED_WORDS` 硬并入；两阶段 DP（词内全禁 → 无解才降级词内强惩罚 −3.0 并留痕）。回归集 `REGRESSION` 实测 **11 例**（`segmentation.py:111-136`；计划文档写 14，见 D7）。

### 5.4 用户主诉 ↔ 机制对位

| 用户主诉 | 机制 | 证据 |
|---|---|---|
| 异常换句 | R1 后处理反写 + R4 无语义罚分 | `rs_subtitle.py:355-359`、`BACKLOG.md:99` |
| 因门禁原因被强制断句 | R1 第二遍吞并 + R2 合并不回检 + R3 预检强拆 | `rs_subtitle.py:807-831,476-537` |
| 异常断句（超长卡） | R3 无自然停顿即不拆 → rs_verify 硬失败 | `rs_subtitle.py:519-524` |
| 丢字 / 碎卡 | R1 `_drop_ghost_cards` 丢弃路径 | `rs_subtitle.py:753-758`、`rules/subtitles.md:238` |

（第四册治理对象：「门禁强制断句」统计归零 = R1–R4 全部改为"约束进求解、门禁只断言"。）

---

## 6. 缺陷台账入口（附录 A 摘录，全文见计划文档 `07-附录A-代码审查明细.md`）

### 6.1 审查覆盖

范围 = `8701cdd..e775e56` 排除 assets/templates/tests/docs；reviewable 29 / reviewed 28 / skipped 1（`rs_effects.py`，纯数据目录 CLI）→ **coverage 96.6%**；**高危 7 + 中危 14**。

### 6.2 高危 7 条（每条一句话 + 复现命令）

| # | 一句话 | 复现 |
|---|---|---|
| H1 | `rs_asset` 的 `--kind sfx,bgm` 未拆分逗号 → **静默返回 0 条**（help 与实现矛盾） | `python skills/cutflow/scripts/rs_asset.py list --kind sfx,bgm --json` → `count: 0` |
| H2 | `t2_glsl` 着色器头 `getFromColor(vec2 uv)` 忽略入参、恒采样 `v_uv` → 依赖 UV 变换的 GLSL 转场**画面错且静默** | 渲染任一经 `tr.glsl.mosaic` 的 join，对比 `xfade=pixelize` 参考，重叠区中位色差显著；`sed -n '1,40p' skills/cutflow/scripts/rs_fx/t2_glsl.py` |
| H3 | `zoompan` 出场分支 `lt(in,n_frames)` **恒真** → 出场缩放全是静默空操作 | 对同段施 `fx.apply --slot out --fx pull.out` 与不加 fx，输出字节无差异；`grep -n -A6 "_g_zoompan" skills/cutflow/scripts/rs_fx/registry.py` |
| H4 | 个人机器绝对路径 `E:\平日资料\...` 写进 `rs_common.ARTBOARD_LOCKED_DIR`（fatal 判据）与 `rs_pixabay.pixabay_key` 第三探测路径 | `grep -rn "E:\\\\平日资料" skills/cutflow/scripts/` → 命中 `rs_common.py`、`rs_pixabay.py` |
| H5 | `rs_pixabay` 的 `sound_effect` 通道仍用 `/music/` 正则 → 详情页在 `/sound-effects/`，**恒不命中静默 0 结果** | `python skills/cutflow/scripts/rs_pixabay.py search --kind sound_effect --query click`（装 playwright 仍空）；`grep -n "music_search\|sound-effects" skills/cutflow/scripts/rs_pixabay.py` |
| H6 | 烧录用 ASS 做了帧网格平移写 `_build/subtitled_aligned.ass`，盘上 `subtitles.ass` **未同步** → 真相源分叉（长片实测 23 段累积 386ms） | 构造段时长非帧整数倍的 10+ 段工程，比对两份 ASS 的 Dialogue 时间；`grep -n "_shift_ass_for_frame_grid" skills/cutflow/scripts/rs_render.py` |
| H7 | `detect_retake` 复制粘贴致 `ratio()` **连算两遍**，剪枝优化自带退化 | `grep -n -A2 "matcher_a.ratio()" skills/cutflow/scripts/rs_cut.py` → 连续两处相同语句 |

### 6.3 中危 14 条（每条一句话 + 锚点命令；锚点实测于本日）

| # | 一句话 | 锚点/复现 |
|---|---|---|
| M1 | `rs_subtitle.main` 内 `load_huazi_template(a.huazi)` 二次调用（重复读盘+解析），已有 `huazi_tpl` 未复用 | `grep -n "load_huazi_template(a.huazi)" skills/cutflow/scripts/rs_subtitle.py` → :1404,:1433 |
| M2 | `huazi_body` 硬编码 `\bord10`/`\bord16`，与模板解析出的 `HuaziBack.outline_w` 两套描边口径 | `grep -n "bord1[06]" skills/cutflow/scripts/rs_subtitle.py` → :1215,:1219 |
| M3 | `rs_fx/registry._g_stretch` 残留 `if False` 死分支（调试残留） | `grep -n "if False" skills/cutflow/scripts/rs_fx/registry.py` → :623 |
| M4 | `rs_pixabay._music_search_urls`/`_music_meta` 定义后从未调用，与 `music_search()` 逐行重复 | `grep -n "_music_search_urls\|_music_meta" skills/cutflow/scripts/rs_pixabay.py` → :127,:152 |
| M5 | `rs_sfx.from_ending` 算出的 `legacy` 变量从未使用 | `sed -n '196,202p' skills/cutflow/scripts/rs_sfx.py` |
| M6 | `_mirror_audio_twin` 的 `changes` 形参从未使用；调用点冗余 `{k: v for k, v in boxes["clip"]}` | `sed -n '793,800p' skills/cutflow/scripts/rs_edit.py` |
| M7 | `op_element_remove` 复用 `op_clip_delete`（已标脏 S3）后又 `dirty.add("S4")` → 一次删除多跑一个阶段 | `sed -n '1404,1408p' skills/cutflow/scripts/rs_edit.py` |
| M8 | `_element_clip_guard` docstring 称"assetId 且在覆盖轨"，实现只校验 `assetId` | `sed -n '1395,1400p' skills/cutflow/scripts/rs_edit.py` |
| M9 | `rs_render._boundary_plans` 中 `b = res_out["boundary"] or {}` 紧接 `b = res_out["boundary"]` 重复赋值 | `sed -n '491,492p' skills/cutflow/scripts/rs_render.py` |
| M10 | `rs_verify._usage_events` 内外两层循环共用 `i, t` 变量名（遮蔽，可读性/隐患） | `grep -n "enumerate(times)\|enumerate(fl)" skills/cutflow/scripts/rs_verify.py` → :982,:1050 |
| M11 | `t2_glsl.render_overlap` 逐帧循环内 `simple_framebuffer` 创建不释放；异常路径不 kill 3 个 ffmpeg 子进程 → 孤儿进程 | `grep -n "simple_framebuffer" skills/cutflow/scripts/rs_fx/t2_glsl.py` → :208 |
| M12 | `rs_brand._absolutize_assets`（保相对路径拼 base）与 `rs_intent._bgm_tracks`（:73 取 basename）对 `file` 字段语义不一致 | `grep -n "rsplit(\"/\", 1)" skills/cutflow/scripts/rs_intent.py`、`sed -n '271,283p' skills/cutflow/scripts/rs_brand.py` |
| M13 | `rs_ir.build_from_cards` 的 audio 轨缺 `"id"`，而 `build_from_cutlist` 已补 V1/A1 → 两路径契约不对称 | `sed -n '218,219p;376,377p' skills/cutflow/scripts/rs_ir.py` |
| M14 | `templates/fonts.json` 的"取 default.subtitle → 查 family"逻辑三处重复实现（异常处理与兜底各异） | `grep -n "def resolve_font\|def _css_font\|def default_fonts" skills/cutflow/scripts/rs_subtitle.py skills/cutflow/scripts/rs_artboard.py` |

### 6.4 流程与台账层已知问题（非代码，"混乱度"来源）

1. **「假正常」未闭环**：五环根因中「① 机器闸只对照中间产物、错误传染互相印证全绿」「② 内容闸 opt-in 默认不跑」「③ 抽帧过疏」未在机制层闭环（`docs/archive/DIAGNOSIS-HANDOVER.md:10-16` 根因表；旁路件 `rs_diagnose`/`rs_verify --content` 见 :22,:25，默认不进流水线）。
2. **真实素材端到端验收缺位**：`docs/BACKLOG.md` v4/v5/v7 系列真实素材验收项全部 `[ ]`（机械验收仅合成素材）。
3. **台账自相矛盾（实测行号）**：W6 — `BACKLOG.md:16`（判"清"）vs `:115`（"仍成立"）；尾部黑场 — `:17`（"清，并入 S9 QC"）与 `:65`（"已在 v0.19 落地"）vs `:101`（I3 "黑帧检测未做"）。
4. **文档历史包袱**：3333 行 / 257KB 历史迭代文档留 `docs/` 顶层，`docs/archive/` 仅 1 文件。
5. **门禁与手册对拍缺口**：`check_manual_cmds` AST 重放器对 `rs_asset`（单 parser 9 子命令）失效，`rules/sfx.md` 示例被迫绕写（`BACKLOG.md:285`）。

---

## 7. 验收对照（第零册 §0.8）

| 类型 | 判据 | 状态 |
|---|---|---|
| `[机]` | 每条事实可 `path:line` 或命令复现；缺陷台账每条有"复现命令/最小样例" | **通过** — 本文档 §2–§6 全部带实测行号与命令；H1–H7、M1–M14 每条带复现/锚点命令（§6.2/6.3），无空字段 |
| `[机]` | 建立 `docs/BASELINE-v0.20.md`（体量表 + 重复口径表 + 视野边界 + 断句根因链固化） | **通过** — 即本文件（§2/§3.2/§4/§5.2） |
| `[人]` | 维护者通读后确认根因排序可接受（"瞎子剪辑"= 感知层缺失而非提示词问题） | **留痕见 §8** |

---

## 8. 验收留痕

- **根因排序确认（[人]）**：「瞎子剪辑」根因定位为**感知层缺失**（结构化画面描述产物为零 + 唯一 VQA 通道不进管线 + 12KB 无图上下文制度性禁看，见 §4），而非"提示词没写好"——维护者已于 **2026-09-27** 在计划文档（`01-第零册-现状审查与基线.md` §0.8 `[人]` 项）通读确认；本基线按同一排序组织（视野边界 §4 先于断句链 §5），后续各册以本节为根因排序的对照原点。
- **Agent 执行留痕**：执行日期 **2026-09-27**；执行方式 = ZCode Agent 按 01-第零册 §0.8 在 `e775e56` 工作区逐条实测复核（`wc -l` / 定向 `grep` / `python -c` JSON 计数 / `git rev-list` / `diff`），全部事实以实测为准，与计划文档的 10 处不符集中记录于 §1（D1–D10），未据此改动任何计划判据阈值；本次执行**未改动任何代码**，新增文件仅本文档，未执行 git commit。
- **复核起点**：后续任一册收口时，先跑 §2 复现命令确认仓库仍在基线附近；若 `path:line` 因合法重构漂移，以"事实仍在 + 行号更新"方式修订对应册的对照表，不改本文件历史结论。
