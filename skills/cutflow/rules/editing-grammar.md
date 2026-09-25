# 剪辑手法库(editing-grammar,方案 §5.9,M8 逐条转录)

> 本文件是**给 Agent 读的规范**:每条手法给出 手法名 / 适用类型 / 可执行落点(命令 +
> 子命令)/ 关键参数与默认值 / 出处与分级 / 验收判据 / 禁忌。落点命令必须真实存在
> (M8 落点核查,见文末核查表);还不存在的落点如实标 ⏳,不许编造命令,也不许
> "先写文档后补实现"(诚实纪律,同能力描述符门禁)。
>
> **四级分级纪律**(沿 `ITERATION-GUIDE-v0.11.md` §10):
> - 【规范】规范/一手文档 —— EBU R128/R37、Netflix Timed Text、FFmpeg Filters、
>   auto-editor、Adobe 官方文档、行业通用连续剪辑规范;
> - 【研究】研究/实证 —— Cutting 镜头时长、Murch《In the Blink of an Eye》、
>   Dmytryk、Reisz、Eisenstein/Kuleshov;
> - 【经验】行业经验值(无权威规范,采用需注明)—— punch-in 比例、hook 留存、
>   B-roll hold、平台适配值、卡点从业者共识;
> - 【内部】内部既定 —— ADR 编号。
>
> **Agent 使用顺序纪律**:做任何剪辑决策前先读手法 14(Murch 六原则)—— 它是
> 排序所有其他手法的元规则:情感 51% / 故事 23% / 节奏 10% / 视线追踪 7% /
> 二维画面平面 5% / 三维空间 4%。

---

## A. 转场与镜头语法(手法 1–7)

### 1. 硬切(hard cut)

- **适用类型**:全类型。
- **可执行落点**:`rs_edit.py apply <工程> --ops ops.json`,op `transition.set`,
  target `clipA|clipB`,`after: {"kind": "none"}`。
- **关键参数/默认值**:`kind=none`(无转场即硬切);显式 `{"kind": "cut"}` 同义。
- **出处与分级**:【研究】Reisz《The Technique of Film Editing》。
- **验收判据**:无 0.3s 内连续双转场(禁忌表第 3 条,机械可查);硬切点落在
  keep 边界或拍点上。
- **禁忌**:卡点型里 0.3s 内双硬切(观感碎裂)。

### 2. 交叉溶解(dissolve)

- **适用类型**:全类型。
- **可执行落点**:op `transition.set`,`after: {"kind": "dissolve", "durMs": 300}`。
- **关键参数/默认值**:300ms(方案给定值);op 层 `dissolve` 别名映射 schema
  `fade`(rs_edit TRANSITION_KIND)。
- **出处与分级**:【研究】Reisz/Dmytryk。
- **验收判据**:溶解只出现在「时间过去了」的真切换;同段内跳切不用溶解
  (rs_ir ADR-0026 三级语法已固化:间隙 <1s 亚帧软切、≥1s 300ms 溶解)。
- **禁忌**:全片连用溶解(糖水感);溶解盖住拍点。

### 3. 转场三级语法

- **适用类型**:全类型。
- **可执行落点**:`rs_ir.py build --from-cutlist …` 自动(无参数);规则:
  源间隙 <1s → 1 帧软切(渲染端 xfade,视觉即硬切);≥1s → 300ms 交叉溶解。
- **关键参数/默认值**:`--xfade 8`(亚帧软切 8ms,可调)。
- **出处与分级**:【内部】ADR-0026。
- **验收判据**:IR 相邻 clip 的 transition 字段与间隙规则一致(机械可查)。
- **禁忌**:手改 transition.type 绕过三级语法而不留痕。

### 4. 亚帧转场提升

- **适用类型**:全类型。
- **可执行落点**:rs_render 内置(自动,无开关):0 < durMs < 1 帧的转场提升为
  `joinCrossfadeMs`(120ms),消除 alpha 单帧 pop 与音频爆音。
- **关键参数/默认值**:120ms。
- **出处与分级**:【内部】ADR-0023。
- **验收判据**:成片转场处逐帧无单帧透明混叠;音频无爆音。
- **禁忌**:为"省事"把亚帧转场直接删掉(会露出硬接爆音)。

### 5. 尾帧扩展零漂移

- **适用类型**:全类型。
- **可执行落点**:rs_render 内置(段尾不足一帧时按帧网格补齐,时间轴总长
  零漂移)。
- **关键参数/默认值**:帧网格来自 IR fps。
- **出处与分级**:【内部】ADR-0023。
- **验收判据**:成片总时长 = IR `_meta.finalDurationMs`(±1 帧);rs_sync 时长断言过。
- **禁忌**:绕过 rs_render 手动拼段(时间账必漂)。

### 6. 动作切(cut on action)

- **适用类型**:短剧 / vlog。
- **可执行落点**:⏳ 第二波——`clip.split` 只收显式 `atMs`;「动作峰值」定位依赖
  `rs_shot.py` 镜头切分产物(`04_粗剪决策/shots.json`)与逐帧运动量检测,
  M8 第一波先由 Agent 读 shots.json 后用显式 `atMs` 拆(峰值 ±2 帧内人工/语义定)。
- **关键参数/默认值**:峰值 ±2 帧。
- **出处与分级**:【研究】Reisz。
- **验收判据**:切点落在动作进行中而非动作结束后(抽帧 L1 目测);无半帧闪烁。
- **禁忌**:在动作完全静止后切(顿挫感);误差超过 ±2 帧仍声称动作切。

### 7. 匹配剪辑(match cut)

- **适用类型**:短剧 / 解说。
- **可执行落点**:❌ 本轮不承诺——`transition.set --kind match` 会得到
  `OP_UNSUPPORTED`(U7 诚实条款:schema 转场枚举未含 match)。构图/语义相似度
  匹配待契约升版 + `text.broll` READY 档(第二波);此前禁用该 op,不要试探。
- **关键参数/默认值**:—。
- **出处与分级**:【研究】Reisz/Eisenstein。
- **验收判据**:—(未承诺;强行手改 IR 写非法枚举会被 rs_ir validate 拦下)。
- **禁忌**:手改 project.json 塞 schema 外的 transition.type(rs_ir validate 报提示、
  rs_render 静默落默认,双输)。

## B. 声音先行与连续性(手法 8–14)

### 8. J-cut / L-cut(声音先行/滞后)

- **适用类型**:短剧 / 解说 / vlog。
- **可执行落点**:❌ 字段落点不承诺——`audio.gain` 只收 `gainDb`;
  `leadMs`/`lagMs` 在 UNSUPPORTED_FIELDS(U7:schema 无 J/L-cut 字段)。
  ⏳ 第二波(契约升版)。当前替代:`clip.move` 把下一 clip 的 startMs 提前/
  滞后(画面侧实现同效果,须自检时长账)。
- **关键参数/默认值**:150–500ms(方案给定域)。
- **出处与分级**:【规范】Dmytryk 亦述,行业通用。
- **验收判据**:声画错位量 ∈ 150–500ms;不吞下一句首字。
- **禁忌**:位移超过 500ms(观众能感知错版);静默手改 IR。

### 9. 视线匹配(eyeline match)

- **适用类型**:短剧 / 访谈。
- **可执行落点**:L1 检查项 + op `note.add`(`after: {"text": "…视线方向待复核",
  "author": "agent"}`)留痕。
- **关键参数/默认值**:无参数;纯人工/L1 判定。
- **出处与分级**:【规范】连续剪辑(行业通用连续剪辑规范)。
- **验收判据**:对话正反打视线方向相对;note 全部关闭后才交付。
- **禁忌**:视线不接还交付(连续性事故)。

### 10. 30 度原则

- **适用类型**:短剧。
- **可执行落点**:⏳ 机位差检查需要 IR 具备机位元数据(当前 schema 无);
  `rs_edit.py ops-validate` 现阶段只把守 op 白名单/值域层。M8 口径:
  相邻镜头机位差 ≥30° 由 Agent 选材时语义保证 + L1 抽查,`note.add` 留痕。
- **关键参数/默认值**:≥30°。
- **出处与分级**:【规范】连续剪辑。
- **验收判据**:相邻两镜跳轴/跳机位处无"跳剪感"投诉点(抽帧 L1)。
- **禁忌**:同机位同景别相邻硬接(即自跳剪)。

### 11. 180 度轴线

- **适用类型**:短剧 / 访谈。
- **可执行落点**:同手法 10(L1 + `note.add` 留痕;机位元数据待第二波)。
- **关键参数/默认值**:对话双方保持轴线一侧。
- **出处与分级**:【规范】连续剪辑。
- **验收判据**:对话戏内人物左右关系不翻转(抽帧 L1)。
- **禁忌**:越轴硬切不补偿(插入中性镜/轴线运动镜是唯一豁免)。

### 12. 建立镜头/切出/插入

- **适用类型**:全类型。
- **可执行落点**:op `overlay.add`(`after: {"card", "startMs", "durationMs"}`)
  挂卡片;op `clip.move`(`after: {"startMs", "ripple": true}`)调序把建立镜头
  放段首。
- **关键参数/默认值**:ripple=true 时后续 clip 自动顺移。
- **出处与分级**:【规范】连续剪辑。
- **验收判据**:场景/章节开头有建立镜(或章节卡);插入素材时长 ∈ 卡区间。
- **禁忌**:插入素材无语义关联(纯凑数 B-roll)。

### 13. 蒙太奇冲突(Kuleshov/Eisenstein)

- **适用类型**:短剧 / 混剪。
- **可执行落点**:`rs_edit.py` 序列编排组合(clip.move / clip.split / clip.trim;
  镜头排序与对位是 Agent 语义工作,命令只落时间轴)。
- **关键参数/默认值**:无固定参数;对位关系(画面 A × 画面 B = 新含义)由 Agent 判定。
- **出处与分级**:【研究】Eisenstein/Kuleshov。
- **验收判据**:并置产生的语义与解说/主题一致(L1 + 交付说明);镜头顺序有因果/对位设计。
- **禁忌**:随机拼接冒充蒙太奇。

### 14. Murch 六原则排序

- **适用类型**:全类型。
- **可执行落点**:**剪辑决策优先级**(非命令):情感 51% / 故事 23% / 节奏 10% /
  视线追踪 7% / 二维画面平面 5% / 三维空间 4%。Agent 在一切取舍冲突时按此排序。
- **关键参数/默认值**:上述百分比即默认权重。
- **出处与分级**:【研究】Murch《In the Blink of an Eye》。
- **验收判据**:决策说明(`成片输出/决策说明书.md`)里的取舍理由能对应到六原则。
- **禁忌**:为"节奏好看"牺牲情感/故事(本末倒置)。

## C. 节奏与卡点(手法 15–17)

### 15. 卡点(beat sync)

- **适用类型**:混剪。
- **可执行落点**:`rs_beat.py detect <音频> --out <工程>`(或工程模式无参)产出
  `04_粗剪决策/beats.json` → op `beat.snap`(target clipId,
  `after: {"windowMs": 60}`)吸附切点。
- **关键参数/默认值**:吸附窗 60ms(硬性 ≤60ms 才吸附);**吸不到 = 不动 + WARN**
  (rs_edit BEATS_MISSING / 超窗不动,禁强制吸附,ADR-0047/0049)。
- **出处与分级**:【经验】卡点混剪从业者共识。
- **验收判据**:副歌段抽查 10 个重拍,画面切换对齐 ≥8 个(±60ms 内算对齐,方案
  §5.5.1);beats.json 的 confidence 已记录(降级时 degraded=true)。
- **禁忌**:强制吸附(超窗仍吸)= 制造假卡点(禁忌表第 7 条);beats.json 缺失时
  凭感觉摆切点冒充卡点。

### 16. 视觉节拍

- **适用类型**:口播+动画(及所有需要画面节奏的类型)。
- **可执行落点**:`templates/styles/registry.json` 的
  `pacingTiers.visualBeatSec`(fast 2–3s / normal 2–5s / slow 3–5s / music 1–2s);
  风格包 params.yaml `visualBeatSec` 是参数真身(ADR-0051),经 `rs_intent compile`
  并入 resolved。
- **关键参数/默认值**:2–3s(方案给定,fast 档)。
- **出处与分级**:【研究】Cutting 等(注意力窗口)。
- **验收判据**:同屏静置时长 ≤ visualBeatSec 上界(抽帧可查);卡片/动画出入场
  与节拍窗一致。
- **禁忌**:全片一个画面撑到底(注意力流失);把 visualBeatSec 当硬切指令逐帧卡。

### 17. 前 3 秒钩子

- **适用类型**:全类型。
- **可执行落点**:`rs_meta.py`(封面/标题)与 `rs_intent.py compile` 的结构原型
  (brief.structure 首段 HOOK);混剪型 §5.5.1 加码:开头 1–2 个镜头必须踩重拍
  (`rs_beat` + `beat.snap` 落地)。
- **关键参数/默认值**:前 3s(平台留存共识);混剪开头踩重拍。
- **出处与分级**:【经验】平台留存共识。
- **验收判据**:首帧非黑场/非 logo;前 3s 出现钩子信息(vlog §5.5.2、短剧 §5.5.3
  分册各自的加码判据)。
- **禁忌**:片头先出频道 logo 淡入(留存杀手)。

## D. 修饰与掩饰(手法 18–23)

### 18. punch-in 推近

- **适用类型**:口播 / 教程。
- **可执行落点**:op `clip.reframe`(`after: {"scale": 1.4}`;注意 schema 只有
  `anchorY` 无 `anchorX`——U7)。自动形态:`rs_ir.py build --punch-in-auto`
  (真剪辑点移除 ≥1.2s 后切更紧构图)。
- **关键参数/默认值**:1.4x 起步、密度 15s/≤3 处(R3 启发式,PUNCH_MIN_GAP_MS)。
- **出处与分级**:【经验】Frame.io/r.editors 共识 + Hitchcock 规则(README 已标注经验值)。
- **验收判据**:同一处 punch-in 不与硬切重叠;全片 ≤3 处;构图变紧但主体未被切破
  (reframe_plan.json 无 REFRAME_CLIP_SUBJECT)。
- **禁忌**:高频 punch-in(抖);scale 大于 1.6 仍当"轻微推近"(已是变焦)。

### 19. jump cut 掩饰三选一

- **适用类型**:口播。
- **可执行落点**(三选一,`rs_ir` 建议 + `rs_edit` 执行):
  ① op `transition.set` kind=dissolve;② op `overlay.add` 插 B-roll;
  ③ op `clip.reframe` 推近。
- **关键参数/默认值**:溶解 300ms(手法 2);B-roll hold 1.5–3s(经验);推近 1.4x(手法 18)。
- **出处与分级**:【规范】auto-editor/jumpcutter(掩饰是这类工具的配套实践)。
- **验收判据**:跳切处三选一落实(抽帧 L1);同一跳切不叠三种。
- **禁忌**:裸跳切连发不掩饰(口播观感碎)。

### 20. 保护式留白(margin)

- **适用类型**:口播。
- **可执行落点**:`rs_cut.py <wordline> --detect all`(内置,无参数):
  前留白 150ms / 后留白 300ms(`MARGIN_IN_MS/MARGIN_OUT_MS`,不对称——后留白
  给呼吸感)。
- **关键参数/默认值**:150ms / 300ms。
- **出处与分级**:【规范】auto-editor `--margin` 精神。
- **验收判据**:粗剪 keep 边界不贴字(字首字尾有留白);无半字被切(wordline guard)。
- **禁忌**:margin 设 0(字贴刀口);把 margin 当音量淡出用。

### 21. 碎刀防护(smooth)

- **适用类型**:口播。
- **可执行落点**:`rs_cut.py`(内置 `smooth_cuts`):<120ms 的刀整体放弃;
  相邻刀间 <100ms 的保留碎片并刀(「宁可漏删」:放弃即 keep,绝不因平滑多删)。
- **关键参数/默认值**:SMOOTH_MINCUT_MS=120 / SMOOTH_MINCLIP_MS=100。
- **出处与分级**:【规范】auto-editor `--smooth` 精神。
- **验收判据**:cutlist 无 <120ms 的孤立刀;keep 无 <100ms 碎片。
- **禁忌**:关掉 smooth 追求"剪得狠"(碎刀是爆音发生器)。

### 22. 片尾保底底噪

- **适用类型**:口播。
- **可执行落点**:`rs_cut.py --tail-reserve-ms 650`(建议区间 500–800ms)。
- **关键参数/默认值**:650ms(TAIL_RESERVE_MS)。
- **出处与分级**:【内部】ADR-0043。
- **验收判据**:成片尾部有 0.5–0.8s 自然底噪不截断(听感收尾);无戛然而止。
- **禁忌**:尾余量 <500ms(底噪截断有"被掐断"感)。

### 23. 冻结帧补长

- **适用类型**:纯动画。
- **可执行落点**:`rs_ir.py build --from-cards …`(卡片比旁白短 →
  `clip.freezeMs` 渲染端 tpad 冻结补长,出场动画开始前定格);手工形态 op
  `freeze.set`(`after: {"freezeMs": …}`)。
- **关键参数/默认值**:CARD_FREEZE_RESERVE_MS 预留(rs_ir 内置);freezeMs ≥1。
- **出处与分级**:【内部】ADR-0027。
- **验收判据**:卡时长 + 冻结 = 旁白段时长(时长账平);定格发生在出场动画前。
- **禁忌**:冻结在入场动画上(看起来像卡顿)。

## E. 素材与录屏(手法 24–25)

### 24. Ken Burns 推近静图

- **适用类型**:vlog / 解说。
- **可执行落点**:artboard `animation.md` §十(**有限次数**——借格纪律,一次
  1–2 个原子);静图经 `rs_artboard.py gen-frames` 出卡,再 `rs_artboard.py <manifest> --apply <project.json>` 挂 overlay 轨。
- **关键参数/默认值**:scale 1→1.08(artboard 分册给定)。
- **出处与分级**:【规范】artboard 分册。
- **验收判据**:缓动为 decelerate/accelerate(禁匀速,禁忌表第 4 条);静图不动区
  无抖动;单条 ≤2 处 Ken Burns。
- **禁忌**:全片每张图都 Ken Burns(廉价感);匀速位移。

### 25. 静音压缩(waiting)

- **适用类型**:录屏 / 教程。
- **可执行落点**:M8 落地:`rs_screen.py analyze <视频> --waiting --out <工程>`
  (或工程模式无参)→ `04_粗剪决策/screen.json` 的 `waiting[]`
  (判据:≥2000ms 无视觉变化 **且** 无语音;视觉变化=降采样帧差,无语音=复用
  rs_cut 的 silencedetect 口径)。消费方式(第二波):按 waiting 清单加速 2–8×
  或删除——当前用 `rs_edit clip.split`(在 waiting 边界拆)+ `clip.speed` 实现,
  `rs_cut --detect waiting` 检测器接入待第二波(现阶段 rs_cut 未知检测器会
  BAD_DETECTOR 拒绝,不臆测)。
- **关键参数/默认值**:WAITING_MIN_MS=2000;变速 2–8×。
- **出处与分级**:【经验】教程从业者共识。
- **验收判据**(§5.5.4):等待/输入废段压缩后总时长较源减少 ≥30%;压缩边界无
  跳帧闪烁。
- **禁忌**:把"讲解停顿"(有信息量的等待)也压掉——waiting 只是候选,语义取舍
  归 Agent/人;压缩后不查边界闪烁。

---

## 禁忌表(负面清单,逐条给出处)

| # | 禁忌 | 理由 | 分级 | 机检/落点 |
|---|---|---|---|---|
| 1 | 按字符数比例插值推时间 | 所有对齐问题的元凶 | 【内部】Hard Rule 2 | 全仓禁令:时间锚定一律走 rs_common.content_index/anchor_span(内容字下标),tests 守门 |
| 2 | 全片一个常数做 disfluency 阈值 | 越长的句子越易含不流畅(Shriberg 1994:10–13 词句子 50% 概率含 disfluency) | 【研究】Shriberg 1994 | rs_cut 检测器按词长/停顿上下文取阈值,无全局常数 |
| 3 | 0.3s 内连续双转场 | 卡点型观感碎裂 | 【经验】混剪共识 | 混剪 §5.5.1 验收清单机械可查(IR 相邻 transition 间隔) |
| 4 | 匀速位移做动画 | 匀速 = 死 | 【规范】迪士尼十二法则/artboard `animation.md` §三 | artboard 卡片出入场只用 decelerate/accelerate |
| 5 | 高频频闪(>3 次/秒明暗反转) | 光敏性癫痫风险(WCAG 2.3.1 同口径) | 【规范】WCAG 2.3.1 | 交付前 L1;rs_sync QC 抽帧可提示 |
| 6 | 拉伸素材适配画幅 | 变形是最廉价的破绽 | 【内部】artboard 桥「尺寸不符报错不拉伸」 | rs_artboard --apply 尺寸不符即停;rs_reframe 裁切窗比恒等于目标比(crop_window) |
| 7 | 强制卡点吸附(超窗仍吸) | 制造假卡点 | 【内部】ADR-0047 | rs_edit beat.snap 超窗不动 + WARN;BEATS_MISSING 拒绝伪造 |

---

## M8 落点核查表(第一波实测,第二波对接用)

| 手法 | 方案落点 | 现状 |
|---|---|---|
| 1 硬切 / 2 溶解 | transition.set | ✅ 既有 op(kind=none/cut/dissolve→fade,durMs) |
| 3 三级语法 / 4 亚帧提升 / 5 尾帧补齐 | rs_ir / rs_render 内置 | ✅ 既有(ADR-0026/0023) |
| 6 动作切 | clip.split --at action-peak | ⏳ 第二波(需 shots.json 运动峰;现用显式 atMs) |
| 7 匹配剪辑 | transition.set --kind match | ❌ OP_UNSUPPORTED(U7,schema 枚举未含) |
| 8 J/L-cut | audio.gain --lead/--lag | ❌ UNSUPPORTED_FIELDS(U7);临时替代 clip.move |
| 9 视线匹配 | note.add | ✅ 既有 op(L1 判定) |
| 10 30 度 / 11 轴线 | ops-validate --lint | ⏳ 机位元数据缺;现 L1 + note.add |
| 12 建立/插入 | overlay.add / clip.move | ✅ 既有 op |
| 13 蒙太奇 | 序列编排 | ✅ 组合既有 op(Agent 语义) |
| 14 Murch | 决策优先级 | ✅ 文档纪律 |
| 15 卡点 | beat.snap + rs_beat | ✅ M8 落地(beats.json 毫秒口径;60ms 窗) |
| 16 视觉节拍 | registry pacingTiers | ✅ 数据既有(风格包 params.yaml 真身) |
| 17 前 3 秒钩子 | rs_meta / rs_intent | ✅ 既有 |
| 18 punch-in | clip.reframe --scale | ✅ 既有 op(scale 域 0.05–4.0;anchorX 为 U7 拒绝项)+ rs_ir --punch-in-auto |
| 19 jump cut 掩饰 | 三选一 | ✅ 组合既有 op |
| 20 margin / 21 smooth / 22 tail-reserve | rs_cut 内置 | ✅ 既有(150/300ms;120/100ms;650ms) |
| 23 冻结补长 | rs_ir --from-cards / freeze.set | ✅ 既有(ADR-0027) |
| 24 Ken Burns | artboard 借格 | ✅ artboard 分册(有限次数) |
| 25 waiting | rs_cut --detect waiting | ⏳ rs_cut 检测器待第二波;**M8 已落 rs_screen --waiting**(screen.json.waiting) |
