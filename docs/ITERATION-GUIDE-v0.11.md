# CutFlow 剪辑水平迭代指南(v0.11 方向)

> 读者:接手 CutFlow 的 Agent / 贡献者。本文回答三个问题:**现在到哪了**(§1-2)、**有什么问题**(§3,本次 review 实证)、**往哪走最划算**(§4-9,含权威数值基准与开源可抄清单)。
> 数值凡有规范/文献出处必标来源;找不到权威出处的明确标「经验值」。**修订纪律:引用本文数值时先读 §10 的来源,别凭记忆转述。**
> 生成:2026-09-14,v0.10.1 验收之后。前置:ADR-0022/0023/0024/0025、CHANGELOG v0.10/v0.10.1。

---

## 1. 一句话现状

CutFlow 已经把「**工程正确性**」做厚:字级时间真相源、声明式缓存、三级验证、机械闸门。但「**剪辑判断力**」仍是词典+相似度启发式,「**镜头语言**」基本为零(无 punch-in、无 B-roll、转场语法只有 fade)。下一阶段的主线是:**把省下的 20-35% 时长变成"更好看",而不只是"更短"**。

### 能力边界(截至 v0.10.1)

| 已具备 | 缺口 |
|---|---|
| FunASR 字级对齐(内置自举,`--ensure` 零手工) | 无说话人分离;无能量/VAD 辅助 filler 检测 |
| 粗剪七类检测器(silence/filler/stumble/retake/false_start/dead_air/off_topic)+ guard 三档 | 词典小而硬编码;无每说话人自适应;jump cut 无掩饰手段 |
| 边缘精修(腐蚀+羽化+green-only killRect,v0.10.1 字节域修复) | 无 matte 质量自检(如 alpha 均值探针);拍摄端 checklist 未固化 |
| 尾帧扩展交叉溶解(零漂移,ADR-0023) | 转场语法单一:所有亚帧转场一律 120ms dissolve,无章节级转场区分 |
| 字幕 DP 断句+词边界+卡拉OK+Agent override | 已贴 Netflix 中文规范;竖屏字号/位置是经验值 |
| L0 机械闸 + S9 音频内容闸 + rs_sync 三对齐 | 无黑帧/冻结帧/VFR/响度自动检测(§8 可全部机械化为 L0) |
| Logo 真实尺寸+六锚点+v0.10.1 padding 预裁剪 | 变体只能最后一步分叉;无片头尾板自动化 |

---

## 2. 本轮 review 结论(为什么敢说上面的话)

对 SKILL.md、19 册 rules、24 个脚本、tests/ 的走查 + 店群工程整片验收,新抓到并已修复 5 个真 Bug(细节见 CHANGELOG v0.10.1 / ADR-0022 修订):

1. **geq 字节域**(致命):滤镜字符串测试全绿,渲染出来全片人物透明。教训已经固化成规矩:**滤镜链改动必须配"真跑 ffmpeg + 断言像素"的回归**,字符串断言不算数。
2. **S1 选中 manifest.json**(致命):Windows Path 排序大小写不敏感,`manifest.json` 排在 CJK 素材前。凡"目录里挑第一个文件"的代码都要过一遍这个坑。
3. **S9 源空间 wordline**(假失败):`wordline.json` 恒为源空间,对账成片时长必差粗剪量;现用 `{final_wordline}`(优先 `wordline.final.json`,与 rs_verify 同一约定)。
4. **`--video` 契约漂移**:文档说选项、argparse 只认位置参数。**spec/register/argparse/文档四处说的是同一件事,改一处必须 grep 其余三处。**
5. **Logo padding 稀释**:e2e 差分实测 scale=12% 实际视觉 3.7%。已修(`crop_to_content` 预裁剪),e2e 复测 12.0% ±1px。

流程层结论:**v0.10 的四个特性全部有单测,但"单测绿"与"成片对"之间隔着一整条渲染链**。P0 整片验收不可省——这次抓到的 4 个问题没有一个能被单测抓到。

---

## 3. 待办问题清单(按优先级,均经实证)

### P1(影响成片质量,下一迭代就做)

| # | 问题 | 证据 | 修法 |
|---|---|---|---|
| 1 | jump cut 无掩饰:粗剪每个切点 = 裸跳切(或统一 120ms 溶解) | ADR-0023 全部切点同质化;§5 转场语法 | punch-in 交替 + 分级转场策略(§5) |
| 2 | 粗剪检测器词典硬编码,口癖因人而异 | `rs_cut.py` FILLERS 词典 ~10 词;店群「就是说」类漏检靠 retake 兜底 | 词表外置 + 工程级自适应(§4.2) |
| 3 | L0 无黑帧/冻结/静音/VFR 检测 | 本次若非抽帧目检,人物消失要到用户手里才暴露 | ffmpeg 自带检测器机械闸化(§8) |
| 4 | 响度闸未闭环:硬规则 11 写了 -14 LUFS/-1 dBTP,但管线无 loudnorm 步、S9 不验 | `rs_render step_mix` 无响度滤镜;sync_report 无响度字段 | 双 pass loudnorm + 报告字段(§8.2) |
| 5 | matte 质量无自检:绿幕拍差时只能靠人眼 | v0.10.1 全透明事故的兄弟场景:alpha 均值异常照样成片 | seg 级 alpha 探针(§8.3) |

### P2(打磨与体验)

- `06_output/rebuild.py` 依赖 `rs_run --init` 生成,新工程/旧工程容易缺失(店群工程就没有,而 IR_MANUAL_EDITS 报错指名道姓让它当逃生门)。修法:`rs_run` 在 `evaluate` 阶段发现缺失自动补生成,或 `--init` 挂进 S0。
- 规则文档有两处 v0.10 前旧描述未同步:`rules/roughcut.md` §8「段间 8ms afade」、`rules/compose.md`「切点处自动带 transition 或 8ms afade」——现在 8ms 会被提升为 120ms 溶解(ADR-0023)。改文档时顺手核对全部「8ms」字样。
- 拍摄端 checklist 未固化(brief 阶段就该挡住的绿幕问题:离幕距离、布光均匀、衣角/头发与背景对比)。素材差时算法救不了(§7.3),要在 `rules/intake.md` 加验收问句。

---

## 4. 粗剪智能化(剪辑判断力的核心)

### 4.1 可直接抄:auto-editor 的四参数

auto-editor(WyattBlue,Unlicense;GitHub 4k+ star,社区最活跃的自动粗剪器)的防碎切设计,与 CutFlow 的 CutList 模型天然兼容:

| auto-editor | 默认 | 抄进 rs_cut 的落点 |
|---|---|---|
| `--margin` | 0.2s(可 `0.3s,1.5s` 前后不对称) | 已有等价物:切点 ±150ms 留白 + tailKeepMs ≥60ms。**吸收不对称语义**:后留白可大于前留白,给呼吸感 |
| `--smooth mincut/minclip` | 0.2s / 0.1s | **新增**:连续 remove 刀间隔 <0.2s 合并、<0.1s 的保留碎片并入邻段——消灭毫秒级碎切(与 `_chain_merge(tol=300ms)` 精神一致,阈值量化到刀级) |
| `--edit audio:threshold` | 归一化音量 4% / `-19dB` 量级 | 现在 silenceDb=-32 偏严;把阈值参数暴露到 CutList `detector.params` 并在报告中记录实际值 |
| `--transition dissolve:DUR[:MIN-CUT]` | 被删间隔 <1s 的切点跳过溶解 | **直接对应 ADR-0023**:溶解只给"间隙大的真切换",小间隙跳切保持硬切+音频短交叉淡变 |

jumpcutter(carykh)的教训也要记:假设 30fps 导致 24fps 素材音画漂移(Issue #144)——CutFlow 已显式探测帧率,别回退。

### 4.2 检测器升级路径(按性价比排序)

1. **词表外置**:`FILLERS`/`REPEAT_WORDS`/元话语词典移到 `templates/fillers.json`,brief 阶段允许用户补自己的口癖;报告按词统计命中,迭代有抓手。
2. **能量辅助 filler**:词典法抓不到"无字的迟疑"(拖长音、半开放音节)。ffmpeg `silencedetect` 已在 dead_air 用;对 0.3-1.2s 的亚阈值段(有声但能量骤降)打 `hesitate` 候选(review 档),与字级词典结果融合。**只进 review 不自动删**,守住"宁可漏删"硬线。
3. **retake 相似度升级**:现在 `difflib` 序列比(0.80 阈值)。中文字面相似但语义不同的误检,叠加字级时长差做第二判据(重录通常更慢)——低成本的启发式,先于任何模型方案。
4. **(远期)说话人分离**:多人访谈 videoType 预留位;FunASR 有 VAD/SAD 能力可探,不要引入新依赖。

### 4.3 文本化编辑契约(对齐 Descript / Premiere TBE)

CutFlow 已具备做「按文本删」的全部底座(字级 wordline + CutList 双向映射)。缺的只是交互契约:**CutList 的每刀补 `text` 字段(刀口前后各 12 字)**,审查包 md 里给人看文本而不是只有时间;`rs_cut --off-topic` 已收文本区间,补反向输出:wordline 上高亮全部 remove/review 区间成一份「可读的删改稿」。这是把「听 30 个 3 秒」进一步变成「读 30 行文字」。

---

## 5. 镜头语言:punch-in 与转场语法(§3-P1#1 的解法)

### 5.1 依据

- **注意力窗口**:镜头时长 2-3 秒窗口有实证(Cutting,康奈尔,75 年好莱坞统计 + Wiley《The two- to three-second time window of shot durations in movies》);ASL 从 1930s ~12s 缩到 2010s ~2.5s。CutFlow 的"视觉节拍 2-3s"(ADR-0004)有据,可继续当硬指标。
- **punch-in 量级**:职业剪辑师共识至少 **20%**,Frame.io 建议 **40-50%**(100%→140-150%)构图差异才算"另一个镜头"——两值均经验值,取中:切点两侧构图差 ≥25%。
- **Hitchcock 规则**("画面大小=叙事重要性"):punch-in 只给重点句,不做无意义变焦。落点:粗剪刀口若落在 `emphasis`(brief 关键句)上 → 该切点右侧用 punch-in;否则硬切。
- **转场语法**(Reisz《The Technique of Film Editing》确立、Dmytryk 七规则第一条"没有积极理由不要切"):**同时空连续动作 = 硬切;dissolve = 时间省略/换场**。口播同场景内插 dissolve 会暗示"时间跳跃"并拖节奏——这是"溶解是拖延"说法的学理来源。

### 5.2 落地:转场三级策略(改 `_resolve_transitions` 与 rs_ir)

| 衔接类型 | 判定 | 处理 |
|---|---|---|
| **同段内跳切**(粗剪 remove 刀产生,源间隙小) | gap < 阈值(建议 1s,对齐 auto-editor) | **硬切 + 音频 10-30ms constant power 交叉淡变**(藏爆音;auto-editor/passim 共识) |
| **话题/章节切换**(brief 章节边界、或源间隙 ≥1s 的真切换) | gap ≥ 阈值或章节标记 | 现行 120ms 交叉溶解(ADR-0023),可配章节卡 |
| **显式声明** | IR `transition.type` 任意合法值 | 按 IR,不覆盖(用户主权) |

配套:rs_ir `--from-cutlist` 生成主轨时按上表打 `transition.reason`(`jumpcut`/`topic`/`explicit`),渲染端按 reason 选策略。**默认值变化要在 CHANGELOG 里喊**——这改变所有新工程的成片观感。

### 5.3 punch-in 实现(新 step,渐进)

- 第一步(纯 FFmpeg,无新依赖):粗剪刀口处把段 i 的右半窗用 `scale=w*1.4:h*1.4` + 居中 crop 交替,anchorY 保持人物头部;帧量化进 ADR-0023 的段框架(等效于"段内变焦",不动时间模型)。
- 判据先行:只对 `remove` 刀且**下一句 ∈ brief 强调句**生效;全片 punch-in 密度上限(经验值:≥15s 一次),多则失去强调意义。
- 验收:切点抽帧前后构图差 ≥25%;人物头部位置漂移 <5% 画高(anchorY 生效);rs_sync 时间零漂移不回退。

---

## 6. 结构层:钩子与留存(有出处的部分才机械化)

- **Hook 3 秒**:TikTok 官方 Best Practice 把黄金窗口压到前 2-3 秒;行业留存基准(**经验值**,营销工具方数据):3 秒留存 ≥65-70% 为强钩子,15 秒 ≥60% 健康。社区 500+ 样本分析:静态开场掉 ~30% 观众。
- 可机械化的两件事:
  1. **首镜头运动量探针**:开头 3s 内画面运动能量(帧差)低于阈值 → L0 告警「疑似静态开场」;
  2. **ASL 统计进报告**:成片平均镜头/视觉变化间隔落 2-4s → ✓;>5s → 告警(直接算,无新依赖)。
- 不可机械化的部分(信息密度、转化结构)交给 Agent 按 videoType 分册做 L1 语义审,写进 `rules/video-types/_通用规则.md`,别试图做成闸。
- B-roll hold 时长 2-5s(常规)/ 6-10s(情绪段)——经验值,未来做 `screen-recording`/`drama` 类型时的插入节奏参考。

---

## 7. 绿幕质量:算法边界与拍摄端

### 7.1 算法能做什么(现状 + 小步)

专业 keyer(Ultra Key/KeyLight/Resolve)的通用序:**matte 先干净 → choke 收缩 → soften 羽化 → despill 收尾**。CutFlow 的链(腐蚀→羽化→despill)顺序吻合。FFmpeg colorkey/chromakey 是纯阈值比较器,**没有发丝级 matte 能力**——算法端到此为止,别试图在 FFmpeg 里复刻 KeyLight。

新增一件小事:**seg 级 alpha 探针**(§3-P1#5):渲染段时对 alpha 平面跑 `signalstats`,人物占比(α>200 的像素比例)落在 [<1% 或 >70%] → 段缓存打 `matte_suspect` 标记进 report。成本一个 pass,能把"全透明"这类事故从"人眼看成片"提前到"渲染时告警"。

### 7.2 拍摄端 checklist(进 rules/intake.md,brief 阶段挡)

- 人物离幕 ≥1.5m(防绿溢+阴影);绿幕布光均匀(幕面亮度差 <10%,经验值);服装/头发与幕色有对比;4K 或 1080p 均可但快门 1/50+(防运动模糊吃掉抠像边缘)。
- brief 问句:「绿幕是否有大面积反光/褶皱?人物是否穿绿色?」——答 yes 先改拍摄,再进管线。

### 7.3 参数口径

`CHROMA_DEFAULTS`(0.15/0.12)与各软件数值**不通用也不必通用**;边缘质量的主 knobs 顺序:先 `cropTopPct` 构图、再 similarity、再 shrink/feather——别一上来调 similarity。

---

## 8. QA 管线:把 L0 从"对账"升级为"体检"

### 8.1 ffmpeg 自带检测器(全部可机械判,零新依赖)

| 检查 | 滤镜 | 判据建议 | 出处/口径 |
|---|---|---|---|
| 黑帧 | `blackdetect(d=0.1,pix_th=0.10)` | 成片内黑帧 ≥0.3s → FAIL(片头尾黑场白名单) | ffmpeg 一手 |
| 冻结 | `freezedetect(n=-60dB,d=2)` | ≥2s 冻结 → FAIL(口播不应有) | ffmpeg 一手 |
| 静音 | `silencedetect(n=-30dB,d=2)` | 尾部垫静音外 ≥2s → WARN | ffmpeg 一手 |
| VFR 混帧 | `ffprobe r_frame_rate vs avg_frame_rate` | 不一致 → FAIL(jumpcutter 血案) | ffmpeg 一手 |
| A/V 同步 | 已有 rs_sync;容差口径补注 | 音频超前 ≤40ms / 滞后 ≤60ms 报警 | **EBU R37**;ATSC IS-191 为 -15/+45ms,取更宽者当 FAIL、更严者当 WARN |
| 响度 | `loudnorm` 双 pass | 见 §8.2 | EBU R128 |

落点:全部进 `rs_sync --video`(或新 `rs_qc.py`),结果进 sync_report 固定字段;**检测器不可用 → skipped 留痕**(ADR-0021 的失败语义,别破坏)。

### 8.2 响度闸闭环(把硬规则 11 从口号变闸门)

- 目标:**集成 -14 LUFS ±1,True Peak ≤ -1 dBTP**。注:-14 是 Spotify/YouTube 归一化点对齐的**行业对齐值,非平台强制规范**(Production Expert:流媒体根本没有统一响度标准;归一化只降不升,真正要管的是 TP 余量)。Netflix 最严(-27 LKFS 对白门控),本技能面向抖音/B站,不取 Netflix 值。
- 实现:`step_mix` 后加双 pass `loudnorm=I=-14:TP=-1.0:LRA=11`(measured 回填 + `linear=true`),`print_format=json` 的测量值写进 sync_report;S9 对账时校验字段存在且达标。单 pass loudnorm 会破坏动态,禁用。
- 成本:双 pass 要多解一遍音轨,段级缓存摊平后可接受;preview/draft 跳过。

### 8.3 验收线新增(进 rules/verify.md L0 表)

黑帧=0(白名单外)/ 冻结=0 / VFR=0 / 响度报告字段齐全且 TP≤-1 / matte_suspect=0。全部机械可判,塞进 L0 不侵占 L1/L2。

---

## 9. 实施排课(建议迭代顺序)

| 轮次 | 内容 | 涉及 | 验收线 |
|---|---|---|---|
| **R1 QA 闭环** | §8 全部检测器 + 响度双 pass + alpha 探针 | `rs_sync.py`/`rs_render.py`/`rules/verify.md` | 店群工程 sync_report 新字段齐全;人为注入黑帧/冻结能被 FAIL |
| **R2 转场语法** | §5.2 三级策略 + reason 字段 + 音频短交叉淡变 | `rs_ir.py`/`rs_render.py`/ADR-0023 修订 | 跳切点无溶解有淡变;章节点有溶解;时间零漂移不回退;测试补像素级 |
| **R3 punch-in** | §5.3(判据+密度上限先行) | `rs_ir.py`/`rs_render.py`/schema | 构图差 ≥25%;rs_sync 零漂移;≤3 处/片 |
| **R4 粗剪智能** | §4.1 四参数 + §4.2 词表外置 + hesitate | `rs_cut.py`/`templates/fillers.json` | 店群源素材复跑:误删率仍=0,检出率报告对比 |
| **R5 文本化** | §4.3 CutList text 字段 + 删改稿 | `rs_cut.py`/审查包 | 一份可读删改稿;按文本能反向定位刀口 |

排课理由:R1 最便宜且直接防住本次"全透明"类事故;R2/R3 是用户观感最大的单点;R4/R5 在 R2 之后做,避免检测器阈值和掩饰手段互相耦合着调。

---

## 10. 来源索引(权威性分级)

**规范/一手文档**
- EBU R128(响度)https://tech.ebu.ch/publications/r128;EBU R37(A/V 容差)https://tech.ebu.ch/publications/r037
- Netflix Timed Text(通用/简体中文/时序)https://partnerhelp.netflixstudios.com/hc/en-us/articles/215758617 ・ /215986007 ・ /360051554394;Netflix Sound Mix(Partner Help)
- FFmpeg Filters(chromakey/colorkey/despill/loudnorm/blackdetect/freezedetect/geq 域约定)https://ffmpeg.org/ffmpeg-filters.html
- auto-editor https://github.com/WyattBlue/auto-editor 及 https://auto-editor.com/ref/options
- jumpcutter 及 Issue #144 https://github.com/carykh/jumpcutter
- Premiere Text-Based Editing(官方)https://helpx.adobe.com/premiere/desktop/edit-projects/edit-video-using-text-based-editing/overview-of-text-based-editing.html;Ultra Key(官方)
- YouTube 编码推荐 https://support.google.com/youtube/answer/1722171

**研究/实证**
- Cutting 等,镜头时长与注意力窗口:https://pmc.ncbi.nlm.nih.gov/articles/PMC3485803/;https://onlinelibrary.wiley.com/doi/full/10.1002/pchj.384
- Murch《In the Blink of an Eye》六原则;Dmytryk《On Film Editing》七规则;Reisz《The Technique of Film Editing》(转场语法)

**行业经验值(无权威规范,采用需注明)**
- -14 LUFS 目标值(Spotify/YouTube 归一化点对齐);punch-in 20%/40-50%;hook 留存 65-70%/60%;B-roll hold 2-5s;B站二压线(社区实测 https://www.bilibili.com/opus/65107112889212237);抖音/B站无公开响度与字幕规范

**内部既定(与本文冲突时以 ADR 为准)**
- ADR-0002(轻改写)、ADR-0004(视觉节拍)、ADR-0009(安全区)、ADR-0012(粗剪决策表)、ADR-0016(三级验证)、ADR-0021(S9 内容闸)、ADR-0022/0023/0024/0025(v0.10 四件套)
