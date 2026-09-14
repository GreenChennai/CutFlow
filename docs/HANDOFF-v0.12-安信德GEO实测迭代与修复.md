# HANDOFF v0.12 — 安信德 GEO 纯动画实测：必修复 Bug 与迭代立项

> **读者**：接手 CutFlow 的下一个 Agent。本文只讲三件事：**哪些是必须修的错**（§2）、**哪些是新能力立项**（§3-4）、**怎么算做完**（§6）。
> 决策依据来自一次真实工程实测（16:9 纯动画 19 张场景卡，158.93s 成片），不是推演。
> 来源：`C:\Users\Velon\Desktop\安信德GEO视频制作BUG复盘-20260914.md`；仓库真身 `E:\平日资料\GitHub\CutFlow`。
> 生成：2026-09-14。**基线**：commit `d70a1e7`（v0.11.1），`pytest tests -q` = 250 passed。
> 纪律：本文的每条"修法"都给了**落点文件:行**与**回归测试名**；改任何契约字段，四处同改（schema / 脚本 / rules / README+SKILL）。

---

## 0. 先读这六条（本轮实测的环境与事实核对结论）

1. **`search_content`（ripgrep）在中文路径上静默返回 0 结果**。本次已再次踩中（`E:\平日资料\...` 下所有检索全空）。诊断中文目录内容请用
   `Get-ChildItem <路径> -Recurse -File | Select-String -Pattern '...'`，或先 `Get-ChildItem` 拿绝对路径再 `read_file`。
2. **唯一落点**：`C:\Users\Velon\.agents\skills\cutflow` 是指向 `E:\平日资料\GitHub\CutFlow\skills\cutflow` 的 **Junction**。改仓库即生效，**不要**再往别处复制一份改。
3. **Python 双版本**：系统 `python` 3.14（无 torch 轮子）；ASR 走 `tools/.venv-asr`（3.12 + torch 2.14 cpu），`fun_asr.py` 自动切。仓库主脚本与 `pytest` 跑系统 python。
4. **大依赖装不动先看 `site-packages` mtime 是否在动**；国内直连失败用代理/镜像，别硬等（本次 `pip install funasr` 卡了 50 分钟，开代理后秒级完成）。
5. **改上游代码必须跑回归**：`python -m pytest tests -q`。当前基线 250 passed，**其中 0 个失败**——任何新增红都是你引入的。
6. **不要 commit / push**，除非用户明确要求。这些改动需用户先在真工程上复跑验收。

---

## 1. ⚠️ 关键发现：复盘里"已上游修复"的两处，**不在本仓库**

这是接手第一件要处理的事。bug 复盘（§五「本次改动/新增文件清单」）声称已改：

| 复盘声称 | 声称路径 | 仓库实际核对结果 |
|---|---|---|
| segmentation `strip()` 与 `index_map` 同步裁剪 | `D:\CutFlow\skills\cutflow\scripts\segmentation.py` | ❌ **本仓库无此修复**。`segmentation.py:477` 仍是 `text = (text or "").strip()`，`index_map` 未同步裁剪。`D:\CutFlow` **当前不存在**（`Test-Path` = False） |
| rs_sync `_norm_hard` 中文数字折叠 | `D:\CutFlow\skills\cutflow\scripts\rs_sync.py` | ❌ **本仓库无此修复**。`rs_sync.py:294-297` 只做 lower + 去标点，无任何数字折叠 |
| Git 状态 | — | `git status --porcelain` **干净**，`git log` 顶部为 `d70a1e7 fix(v0.11.1)`。即：**没有任何未提交改动** |

**结论与动作**：
- 复盘里的"已上游修复"是**在另一个副本（`D:\CutFlow`）上改的，且该副本已不在**，修复已丢失（或从未落到本仓库）。**必须按 §2 的 B1/B2 重新实现并配回归测试**，否则下次同类工程一定复现。
- 若 `D:\` 或其它盘还能找到 `CutFlow` 副本：先 `git --no-pager diff --stat` 对比，只把**有测试覆盖**的差异手工合并进本仓库；**不要整体覆盖**（本仓库已是 v0.11.1，副本可能更旧）。

> 复盘里其余 14 条与仓库的核对结果已逐条并入 §2，其中 **#1 ducking、#4 字幕静默跳过是确凿的上游未修 bug**，#5/#6/#7/#8/#13/#15 属工程级/交叉技能（见 §4）。

---

## 2. 必修 Bug（按优先级；每条含落点、修法、回归测试名、验收）

> 通例：每条修完，`docs/CHANGELOG.md` 加一行"现象 → 根因 → 修法 → 测试数变化"；涉及**渲染滤镜链**的必须真跑 ffmpeg 断言像素（v0.10.1 geq 字节域事故的教训：字符串断言不算数）。

### B1（致命·字幕对齐）`segment()` 的 `strip()` 与 `index_map` 坐标系不同步

- **落点**：`skills/cutflow/scripts/segmentation.py:472-477`（`segment()` 入口）
- **现象**：字幕**系统性提前收字**（终点偏移中位 -170ms、59/68 卡早退、个别 -680~-1420ms）。同一卡在 ass / cards.json / wordline 三处时间不一致，末字『点』的锚落到了前一个字『人』上。
- **根因**：`text` 被 `.strip()` 剥掉句首空白，位置整体左移；但 `index_map`（句内位置 → chars 下标）仍是未 strip 的，此后一切按位取值都**系统性偏移 1**。
- **修法**：

  ```python
  raw_text = text or ""
  text = raw_text.strip()
  if index_map and (len(raw_text) != len(text)):
      lead_n = len(raw_text) - len(raw_text.lstrip())
      trail_n = len(raw_text) - len(raw_text.rstrip())
      index_map = index_map[lead_n:len(index_map) - trail_n]
  ```

  注意：**原复盘只裁了句首（`lead_n`），漏了句尾**。校对稿句尾带全角空格 `\u3000` 同样会偏移，必须双侧对称裁剪（末尾切片用 `len(index_map) - trail_n`，不要用 `lead_n + len(text)`，那在末尾空白场景下不等价）。
- **回归测试**：`tests/test_v12.py::test_segment_strip_keeps_index_map_aligned`——构造带前导空格 / 前导+尾随全角空格的句子 + `index_map` + `char_times`，断言卡片 `charSpan` 首末下标与实际字一致（不是 ±1）。
- **验收**：安信德工程复跑，rs_sync 终点早退 **0**、终点中位 ≤40ms（复盘修后实测：起点 p95 290→30ms、终点中位 170→20ms）。
- **副产**：断句会更贴语义（复盘实测卡数 68→79、节奏 ≈2s/卡）——这是预期变化，`CHANGELOG` 要写明"本修复会改变断句结果"。

### B2（高·内容闸误报）`rs_sync._norm_hard` 不做中文数字折叠

- **落点**：`skills/cutflow/scripts/rs_sync.py:294-297`（`_norm_hard`）
- **现象**：`--audio-content` 判 FAIL，相似度 **0.888 < 0.90**。人工对账显示差异几乎全是良性：`1500`↔`一千五百`、`10800`↔`一万零八百`、`2980`↔`两千九百八十`、`4.3`↔`四点三`（另有 `I/i` 大小写、BGM 下同音字 `投/头`、`喂/为`）。
- **修法**：`_norm_hard` 增中文数字 → 阿拉伯折叠，**两种读法都要**：
  - 值读：`两千九百八十` → `2980`、`一千五百` → `1500`、`一万零八百` → `10800`
  - 位读：`四九八零` → `4980`
  - 小数：`四点三` → `4.3`
  - 字母 lower 已有，保留。
- **🚫 禁止**：为了让闸通过去下调 `AUDIO_SIM_MIN`（`rs_sync.py:289`）。SKILL.md §9 反模式明写"为通过验收而放宽 guard"。修归一化，不修阈值。
- **回归测试**：`test_norm_hard_folds_cn_numbers`（表驱动，覆盖上列全部样例）+ `test_audio_content_sim_improves_with_fold`（构造 ref 阿拉伯数字 / asr 中文数字的差异文本，断言折叠后 ratio ≥0.94）。
- **验收**：0.888 → ≥0.94；`python -m pytest tests -k sync -q` 全绿。

### B3（致命·混音）BGM ducking 滤镜图标签被消费两次

- **落点**：`skills/cutflow/scripts/rs_render.py:775-784`（`step_mix`）
- **现象**：`MIX_FAIL: Stream specifier 'a1' matches no streams`。生成的 filtergraph：

  ```
  [1:a]...[a1]; [2:a]...[bgraw];
  [bgraw][a1]sidechaincompress=...[bgm];   ← [a1] 第一次消费
  [a1][bgm]amix=inputs=2...[mix]           ← [a1] 第二次消费 → 报错
  ```

  ffmpeg 的 filtergraph 标签**只能被消费一次**。本次工程靠 IR 里 `"bgm": {"ducking": false}` 绕开，**bug 仍在**。
- **修法**（比复盘建议的更正确一点）：不要只给 `[a1]` 加 `asplit`——那只 duck 第 1 段人声。应先把**全部人声**合成一条总线，再 split：

  ```
  [1:a]chain[a1]; [2:a]chain[a2]; ...  ; [a1][a2]...amix=inputs=N:normalize=0[voice];
  [bgraw][voice]asplit=2  ← 不对
  [voice]asplit=2[voice_m][voice_d];
  [bgraw][voice_d]sidechaincompress=threshold=0.02:ratio=6:attack=60:release=500[bgm];
  [voice_m][bgm]amix=inputs=2:duration=first:normalize=0[mix]
  ```

  保留 `duration=first` 语义（以人声长度为准）；`ducking=false` 分支保持不动。
- **回归测试**：`test_mix_ducking_graph_has_asplit`（字符串层面的快速护栏）**＋ 一条实机用例**（1 段人声 + BGM + `ducking:true` → 真跑 `step_mix`，断言 returncode=0 且输出时长 ≈ 人声时长）。学 v0.10.1：滤镜链改动必须真跑。
- **验收**：`ducking:true` 的 IR 能直接渲出成片（不再需要 `ducking:false` 绕开）；安信德工程把 IR 改回 `ducking:true` 复跑一次确认。

### B4（高·静默降级）`step_subtitle` 在 `ass` 缺失时无声跳过

- **落点**：`skills/cutflow/scripts/rs_render.py:800-803`

  ```python
  ass = doc.get("subtitle", {}).get("ass")
  if not ass:
      return src          # ← 无日志、无警告
  ```

- **现象**：第一版"成功渲染"的成片**没有字幕**，靠 L1 抽帧才被发现。IR 只写了 `subtitle.source` 没写 `subtitle.ass`。
- **修法**（二选一，推荐两者都做）：
  1. `step_subtitle` 里 `ass` 缺失但 `doc["subtitle"].get("source")` 存在 → 追加显式 WARN（复用渲染 `warnings` 列表）：`"IR 有 subtitle.source 但无 subtitle.ass → 本次不会烧录字幕"`；
  2. `rules/compose.md:11` 现在写的是"**`subtitle.source` 应指向 `05_ir/wordline.json`**"——只提 source 不提 ass，正是这次踩坑的诱因。改写为：「**`subtitle.ass` 才是烧录字段**（缺失即不烧字幕）；`subtitle.source` 仅作溯源」。`rs_ir.build_from_cutlist` 产出的 IR（`rs_ir.py:200`）两个字段都有，保持。
- **回归测试**：`test_render_warns_when_ass_missing`（`ass` 缺、`source` 在 → warnings 非空且**不含**静默路径）。
- **验收**：人为去掉 IR 的 `ass`，渲染输出的 `warnings` 必须能看到这条。

### B5（中·契约漂移）QC 体检在 4 处文档/代码里说法不一致

- **现状核对**：

  | 位置 | 是否含 QC |
  |---|---|
  | `rules/verify.md:27`（L0 判据表） | ✅ 列为 L0 判据 |
  | `rs_run.py:86-91`（S9 spec） | ✅ `--qc` 已注册 |
  | `README.md:185`（命令速查） | ✅ 写了 `--qc` |
  | `SKILL.md:142`（命令速查） | ❌ **漏了 `--qc`** |
  | `rs_verify.py:247-248`（`L0_CHECKS`） | ❌ **不含 QC**——只跑 `rs_verify` 的路径永远不体检 |

- **判定**：rules 说它是 L0，实现里 `rs_verify` 却不跑。**spec / register / argparse / 文档四处说的是同一件事，改一处必须 grep 其余三处**（v0.10 已将此写成流程教训，这次又踩）。
- **修法**：`rs_verify.py` 内成片存在时调用 `rs_sync.run_qc`（或新增 `--qc/--no-qc`，默认开），QC 结果进 `verify_report.md` 与 L0 输出；同时给 `SKILL.md:142` 的命令补 `--qc`。
- **回归测试**：`test_verify_l0_includes_qc_when_video_present`（成片在 → checks 里出现 QC 项；QC 不可用 → `skipped` 留痕，不硬失败，守住 ADR-0021 的失败语义）。

### B6（中·断句）长破折句上的 3 字孤卡

- **落点**：`segmentation.py:296-299`（`_card_penalty`，`length < 4` 罚 −1.2×缺额）
- **现象**：切出 3 字孤卡『说谁好』。
- **修法**（保守，不破坏对齐精度）：保持 `MIN_CHARS = 2` 不变，新增**后处理"孤卡合并"**：尾卡（末卡）字数 <4 且与前卡合并 ≤ `max_chars` → 并入前卡；合并不了就在 `violations` 里显式记 `orphan-card`（**不静默**）。人工通道已有：`rs_subtitle --override` 支持 `textPrefix+textSuffix` 定位合并（B7，v0.8.2）。
- **回归测试**：`test_no_three_char_orphan_card_on_dash_sentence`（长破折句用例进 `segmentation.REGRESSION`）；`test_orphan_card_reported_in_violations`（合并不了时不静默）。
- **验收**：安信德工程 79 卡重跑，无 3 字孤卡；卡数变化写进 CHANGELOG。

### B7（中·上游补能力）把"wordline 平滑"从工程级脚本收编为 `rs_align smooth`

- **现状**：`rs_align.py` 自身在写字级时间时有保底 `endMs = max(b, a + 20)`（`:108`、`:118`），所以**正常链路不会出零宽字**。本次的 `147 个字的 endMs<=startMs`（rs_verify L0 硬失败，`rs_verify.py:151`）来自用户**自写**的 `_smooth_wordline.py`：它把标点设成零宽以满足"标点不占时长"，又违反单调门禁（要求严格 `endMs > startMs`）。
- **同类**：retext 插入标点带 240ms、个别字符 1.14s / 互相重叠（`rs_subtitle.py:741` 附近有相关注释）——都要靠事后平滑兜底。
- **修法**：新增 `rs_align smooth`（或 `retext_wordline` 内置开关），把复盘里自写的两条规则**上游化**：
  1. **插入标点零宽**：`endMs = startMs + 1`（+1ms 恰好满足严格单调，且不占显示时长）；
  2. **内容字重叠钳制**：相邻字 `endMs > next.startMs` → 收到 `next.startMs`；不足 1ms 的间隙让位给对齐精度。
  命令示例：`rs_align.py smooth 05_ir/wordline.json --out 05_ir/wordline.final.json`。
- **回归测试**：`test_align_smooth_punct_zero_width_monotonic`（折叠后无 `endMs<=startMs`、`startMs` 单调不减）+ `test_align_smooth_clamps_overlap`。
- **收益**：纯动画 / 配音工程不再需要每个工程重写一遍易错脚本——这正是 §3-I7 的前提。

### B8（工程级 → 上游文档）两条 ffmpeg/DX 陷阱写进 references

- **`-t` 放输出侧会把 tpad 补的帧又截掉**（本次丢 20.4s：10 张补长卡各短一截，`视频流 138.77s ≠ 预期 159.17s`）：

  ```bash
  # ✗ -t 是输出选项,限制最终输出时长,tpad 补的帧全被截
  ffmpeg -i card.mp4 -t 7.88 -vf "tpad=stop_mode=clone:stop_duration=5.6" out.mp4   # 得 7.88s
  # ✓ -t 放输入侧,只裁输入,tpad 自由延长
  ffmpeg -t 7.88 -i card.mp4 -vf "tpad=stop_mode=clone:stop_duration=5.6" out.mp4   # 得 13.5s
  ```

  落点：`skills/cutflow/references/ffmpeg-recipes.md`（补正确/错误对照）。
- **门禁报警必须查，不许当警告忽略**：这次正是 rs_render 的**音画对齐断言**（成片时长 vs IR `finalDurationMs`）抓到了丢时长。落点：`rules/selfcheck.md` 补一句"渲染后时长断言 ≠ 通过即停下"。

---

## 3. 迭代立项（新能力）

### I7（★本轮最大立项）纯动画 IR 组装器上游化，消灭"每个工程重写一遍脚本"

- **证据**：安信德工程是 `pure-animation`（16:9 / 19 卡），但 `rs_ir.py` 只有 `build --from-cutlist`（`rs_ir.py:231-245`，choices = `validate` / `build`）——**纯动画没有 IR 组装入口**。于是该工程自写了三个脚本：

  | 自写脚本 | 干什么 | 由此产生的 bug |
  |---|---|---|
  | `_gen_cards.py` | 19 张场景卡生成（共享 HUD + 五段式骨架） | #7 CSS 动画类冲突、#8 网格溢出（见 §4） |
  | `_build_ir.py` | 字符级锚点分组 + 停顿中点切卡 + 冻结帧补长 + BGM 裁齐 | #6 手改产物被生成器覆盖、#9 锚点扫描三连、#5 `-t` 位置错 |
  | `_smooth_wordline.py` | 标点零宽 + 重叠钳制 | #11/#12 单调门禁（→ B7 上游化） |

- **落点**：`rs_ir.py build --from-cards`（或独立 `rs_cards.py`），输入 = `03_assets/artboard/manifest.json` + `05_ir/wordline.json` + 卡片↔旁白分组表，输出 = 完整 IR（主轨 = 场景卡串联，音频轨 = 旁白/原声，`subtitle.ass` 必须写全）。
- **必须内置的四条逻辑**（全部来自本次实测，别再让下一个工程重推一遍）：
  1. **卡片↔旁白分组 = 字符级锚点扫描**：命中关键词即切组；**命中后消费 `len(k)` 个字符**（防「安信德GEO优化系统」被当作「交给安信德GEO优化系统」的后缀重复触发）；标点继承当前组；同卡相邻组合并；**锚点优先取句首词**（句中锚会让上一张卡拖尾、下一张卡过短）。**不要用 `if gi == g: break`**——`g` 从 `gi` 起步时第一轮恒真，锚点永不前进（本次 bug #9 的真凶），要用 `hit` 标志。
  2. **组内停顿中点切卡**（旁白比卡长时）；**冻结帧补长**（`-t` 在输入侧，见 B8）。
  3. **生成式产物不得手改**：所有可调参数固化进命令/配置，`_manual_edits()`（`rs_ir.py:210`）的护栏要覆盖这条新路径（`IR_MANUAL_EDITS` + `--force` 语义照旧）。
  4. **与 `rs_artboard --apply` 同一套挂点匹配**（归一化绝对路径，`rules/artboard.md:25`），避免出现两套路径比较口径。
- **复用**：分组/定位的锚定算法与 `rs_subtitle` 的 override 定位（B7 的 `text` / `textPrefix+textSuffix` 文本锚定，v0.8.2）是同族问题 → **抽到 `rs_common` 共享**（同时消掉 BACKLOG W5 指出的三处重复）。
- **验收**：用安信德工程同构的最小工程（3 卡 + 3 段旁白），一条命令出 IR，`rs_ir validate` 全绿、rs_render 出片、rs_sync 通过；卡时间与人工核对一致。
- **ADR**：写 `docs/adr/0027-纯动画IR组装器.md`（背景 / 决策 / 影响 / 回滚），并把 `rules/video-types/纯动画.md:40` 的"场景 = artboard 卡"补上这条命令。

### I1 热词链路（借鉴 FunClip，直接治"专有名词全错"）

- **现状**：`tools/fun_asr.py` **已经有** `--hotwords`（`:556`）且 `CAPS` 声明 pkg 后端支持热词（`:193-195`）；pkg 路径已把 `hotword` 传进 funasr（`:435-436`）。**但 `rs_align.py` 完全不透传**（grep 无 hotword）。
- **现象**：onnx 降级后端下专有名词全错（投喂→位、SEO→ICO、新闻源→西闻源）——复盘已写明"字幕/对齐用途必须 pkg 后端"，但 pkg 下专名仍需热词。
- **落点**：`rs_align.py` 增 `--hotwords`（或 `--terms-file`），来源约定为 `00_brief/brief.md` 的术语表 / `templates/terms.json`；wordline 顶层记 `asr: {backend, hotwords}` 留痕；`rules/asr.md` 写明"专名错 → 先补热词重跑，不要手工改字"。
- **⚠️ 待验证事实（别想当然）**：热词增强是 **SeACo-Paraformer** 的能力，而当前默认模型是 `paraformer-large` → `HUB_ALIAS` 映到 `paraformer-zh`（`fun_asr.py:241-242`）。**先实测默认模型下 `--hotwords` 是否真的生效**（拿「安信德 GEO」这类生造词对比开/关识别结果）；不生效则给 config `asr.model` 增加 SeACo 档并写进 `rules/asr.md`，别只写"已支持"。
- **回归测试**：`test_align_passes_hotwords_to_asr`（monkeypatch runner 断言命令行含 `--hotwords`）。

### I2 `rs_cut --from-text`：按文本裁片（借鉴 FunClip 的"文字稿里选段落"）

- **现状**：CutFlow 有 R5 文本化删改稿（"读稿精修"），但缺**正向入口**——用户说"我只要『第三步』到『第四步』"时，只能手算时间。
- **落点**：`rs_cut.py --from-text "<引文>"`（或 `--from-text-file`）：在 wordline 上顺序锚定引文 → 生成 keep/remove 区间 → **走现有 guard**（宁可漏删）+ 留痕 `_meta.fromText`。
- **复用**：锚定算法同 I7 第 4 条（下沉到 `rs_common`）。
- **回归测试**：`test_cut_from_text_builds_keep_intervals`。

### I4 说话人分离（P2，FunClip 借鉴）— 先只落字段来源

- **现状**：`wordline` 顶层**已有** `speakers` 字段（`rs_align.py:153`），但没有 diarization 来源；`CAPS` 里 `speaker` 只有已废弃的 server 后端为 True（`:195`）。
- **判定**：**现在不做**。用途只有 `interview` 类 videoType（SKILL.md 硬规则 17 里的预留位）+ 单人口播无收益。要做时的落点：`fun_asr --spk`（funasr `spk_model="cam++"`，CPU 可跑），`--json` 输出带 speaker，`build_wordline` 已能消费 `seg.get("speaker")`。
- **动作**：进 `docs/BACKLOG.md`（标 P2，注明"仅 interview 需要，勿为单人口播引入模型成本"）。

### I5 音频事件（P3，SenseVoice）— 记一笔就够

掌声/笑声/音乐起等事件维度可给 `dead_air` / `hesitate` 检测器做"别删"的白名单（FunClip 用 SenseVoice 提供情绪与音频事件）。**风险**：新模型 ~1GB + 新依赖，违背"零第三方依赖"的克制。**明确不进本轮**，只进 BACKLOG 的 IDEA。

### I6 制作端 checklist（把"素材差算法救不了"固化到 brief 阶段）

- 落点：`rules/intake.md` —— 绿幕问句（离幕 ≥1.5m / 幕面亮度差 <10% / 服装发色对比 / 快门 1/50+）；纯动画问句（是否 16:9 / 卡片总数与节奏 / 旁白是 TTS 还是原声 / 卡片是否已定稿）。
- 理由：本次 #10（onnx 无字级时间戳）、#13（WPI_FFMPEG）、#15（Git Bash 路径）全是**开工前问一句就能避免**的。

---

## 4. 交叉技能条目（artboard / 环境）——**不要在本仓库改**，只在本仓库留引用

本次有 6 条 bug 属于 **artboard 技能本体**或环境，不是 CutFlow 的代码缺陷。处理原则：**CutFlow 只在自己的 rules 里留 checklist 与引用**，改 artboard 需另开仓库迭代（越界改别的仓库会让两个技能互相污染）。

| 来源 | 内容 | 本仓库落点（只写规则，不改实现） |
|---|---|---|
| #7 | 同一元素挂两个动画类（`.so` 出场 + `.count`/`.stamp` 入场）后者覆盖前者 → 元素不入场常驻 / 永不退场 | `rules/artboard.md` 设计约定补：**单一外层统一出场、内层只挂入场**；计数器元素内容必须为空 |
| #8 | c06 平台矩阵 4×380px + 3×34 间距 = 1622px，起点 x=320 → 右缘 1942 > 1920 被切 | `rules/artboard.md` 补：**定宽网格先算总宽再定起点**（149 + 1622 = 1771 ✓） |
| #13 | artboard 的 MP4 导出实际走 WPI，只认环境变量 **`WPI_FFMPEG`**（`ARTBOARD_FFMPEG` 无效） | `rules/intake.md` 环境 checklist：两个都设；`docs/` 已有记录则交叉引用 |
| #16 | Mode S 录制从页面加载 ~1.7–1.9s 才开始 → 入场 delay 必须从 `--t0: 2.0s` 起算；导出片长 ≈ 时间轴 −1.8s；单卡 `--max-wait` ≤15s | `rules/artboard.md`（已提 ADR-0012，**核对是否写全**；本次仍被当 bug 排查，说明没写到位） |
| — | `scaffold.py` 落盘位置由 `config.studio_dir` 决定，**与 cwd 无关**；要进工程目录用 `ARTBOARD_STUDIO` | `rules/artboard.md` 调用段补一句 |
| #15 | Windows 路径以 `\` 结尾放进双引号会转义引号（`"...\fonts\"` → `unexpected EOF`）；中文路径 + 复杂引号的内联 python 建议写脚本文件 | `rules/archive.md` 或 `references/ffmpeg-recipes.md` 补"Windows/ Git Bash 引号与路径纪律" |

---

## 5. 收尾要求（文档与账本，四处同改）

1. **ADR**（`docs/adr/`，现有编号到 0026）：
   - `0027-纯动画IR组装器.md`（I7）
   - 可选 `0028-ASR热词链路.md`（I1，若实测默认模型不吃热词、需要切 SeACo，则**必须**写）
2. **`CONTEXT.md`（只放术语，不放实现）** ——建议补：**场景卡串联**、**卡片↔旁白分组**、**冻结帧补长**、**热词表**、**孤卡**。每条一句话，别写实现细节。
3. **`docs/CHANGELOG.md`**：新增 `v0.12` 段，逐条"现象 → 根因 → 修法 → 测试数变化（250 → N）"；**默认值/断句结果变化必须喊**（B1 会改断句、B6 会改卡数）。
4. **`docs/BACKLOG.md`**：把 I2/I4/I5 与本轮未做的项挂进去（沿用 P0/P1/P2/IDEA 分级）。
5. **`SKILL.md` / `README.md` / `rules/*`**：契约类改动四处同改（B5 就是反例）。SKILL.md 有 **≤270 行**的体量纪律与"路由表覆盖全部 rules"的测试锁，加内容前先看 `tests/test_v5.py` 里的相关用例。
6. **测试**：新增用例进 `tests/test_v12.py`（沿用 v4~v11 一版一文件的惯例）；涉及 ffmpeg 的用例照 v0.10.1/v0.11 的写法真跑并断言像素或时长。

---

## 6. 验收线（完成定义）

- `python -m pytest tests -q` 全绿，且**用例数 > 250**（每条 B* 都有回归）。
- **安信德工程复跑**（`D:\artboard-studio\20260914-安信德GEO品牌宣传-pure-animation`，或同构最小工程）：
  - rs_sync：**终点早退 0**、终点中位 ≤40ms、起点 p95 ≤80ms；
  - `--audio-content` 相似度 ≥0.94 且判定通过（**没有**改 `AUDIO_SIM_MIN`）；
  - IR 改回 `"ducking": true` 后能直接渲出成片；
  - 成片字幕**确实烧上了**（抽 2–3 帧：`ffmpeg -ss t -frames:v 1`），且渲染 `warnings` 为空；
  - `rs_verify` 的输出里**能看到 QC 项**，`sync_report.md` 的 QC 段非空；
  - 断句无 3 字孤卡。
- 纯动画新路径：3 卡最小工程用 §3-I7 的一条命令产出 IR 并出片。
- 文档：ADR-0027 落盘；CHANGELOG v0.12；CONTEXT.md 术语补充；BACKLOG 更新；SKILL/README/rules 四处口径一致。

---

## 7. 不该动 / 已知边界

- **不要**为过闸调 `AUDIO_SIM_MIN`（0.90）、`END_TOL_MS`（25）、`MEDIAN_MAX`（40）等阈值——本次的问题全在**归一化与坐标系**，不在阈值。
- **不要**回退 `CACHE_VER`（当前 `v4`，`rs_render.py:37`）；滤镜链/时间语义变更时**必须** `+1`。
- **不要**把 `MIN_CHARS` 调到 4 来"顺手治孤卡"——会制造超字数降级路径（`_dp` 无解）。用后处理合并 + 显式留痕（B6）。
- **不要**用 `chromakey`（alpha 在部分构建上全坏，v0.10 已改用 `colorkey` + 几何腐蚀）；`:775-784` 附近改动后务必真跑渲染。
- **不要**动 `rs_align` 的 `max(b, a + 20)` 保底（它是单调门禁的第一道护栏）；平滑只在 `smooth` 子命令里做。
- **不要**在 artboard 仓库做本文件的 §4 条目（那是另一个技能的事）。
- 现有 `rules/artboard.md`、`rules/compose.md` 里有 v0.10 之前的旧描述（如"8ms afade"），顺手核对全部「8ms」字样（ITERATION-GUIDE §3-P2 已记过一次）。

---

## 8. 待用户拍板的开放决策（先按推荐值执行，用户否决再改）

| # | 问题 | 推荐 |
|---|---|---|
| Q1 | B1 的修复会**改变断句结果**（卡数 68→79 一类），是否接受"修 bug 换观感"？ | **接受**。早退字幕是硬伤，卡数变化本身是修复的副产；CHANGELOG 必须写明 |
| Q2 | I7 纯动画组装器是**新脚本**还是 `rs_ir` 的新子命令？ | **`rs_ir build --from-cards`**：IR 生成只有一处出口，`_manual_edits` 护栏与 validate 天然复用 |
| Q3 | B3 ducking 修好后，历史工程里 `ducking:false` 的 IR 要不要批量改回 `true`？ | **不批量改**：IR 是工程产物，谁需要闪避谁改；只在 `rules/compose.md` 说明"现在可以开" |
| Q4 | I4 说话人分离要不要现在做？ | **不做**，只进 BACKLOG（单人口播无收益，多一个 ~1GB 模型） |
| Q5 | 本次是否需要顺手把 `D:\CutFlow` 残留副本清理/归档？ | **先确认再动**：若副本还在，diff 后合并有测试覆盖的部分；确认无用再删 |
