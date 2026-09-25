# Intake — 开剪前的问卷与契约

## 何时问、问什么

**这是开工第 0 步:任何素材进入流水线之前必须先走完本问卷**(grill-me 精神:问清需求与特别注意点再动手)。

自适应:**素材里已有的答案不问**。必问清单(缺什么问什么):

0. **videoType**(决定走哪册 `rules/video-types/` 分册,并决定管线分支):
   `talking-head`(纯口播:真人出镜,素材须**已抠像并合成好背景** + 字幕)/ `talking-head+animation`(口播+部分动画)/ `pure-animation`(纯动画,无真人画面)/ `vlog`(生活记录:素材碎片多,BGM+自由节奏,见该册)/ `混剪`(卡点/音乐驱动:无旁白主线,BGM 是时间轴骨架,见该册)?
   —— 追问式:用户说不清时,给出两三个候选并各配一句示例描述;
   —— **预留扩展位**(暂不实现,勿选):`screen-recording` / `interview` / `drama` / `film-commentary`;
   —— videoType 是**开放注册表**(阶段四起):新增类型 = 新增一册分册 + 一条 `templates/styles/registry.json` 条目,不改引擎。
0a. **绿幕预处理确认**(talking-head 系必问):这段素材**是否已经抠好像、合成好背景**?
   v0.14 起 CutFlow **不做抠像/背景合成**。若用户答"还没有"或在绿幕现场 → 先让用户用剪映/Pr/AE 抠像+合成背景后交付处理完的成片,再开工。
   S0 摄取会自动抽帧检测幕布,命中即阻断(`GREEN_SCREEN_INPUT`);确属误判时让用户说明,记录 `绿幕检测:误判(<原因>)` 到 brief 或跑 `rs_ingest.py green-ok <工程> --reason "…"`。
0b. **特别注意点**:有没有绝不能出错的内容(数据/法务/品牌词)?有没有必须保留/必须剪掉的段落?

1. 成片用途/平台(抖音/B站/视频号/YouTube → 决定默认比例与时长节奏)
2. 比例(9:16 / 16:9 / 双出)
3. 目标时长(粗略区间)
4. 风格(talkshow-bold / tutorial-clean / motion-info / 给参考视频描述)
5. 字幕(开/关;样式;关键词高亮词表)
6. 声音(用原声 / TTS 配音——音色默认 koubo-test,可指定 / 无声+BGM)
7. 片头片尾(不要 / artboard 生成 / 用户提供)
8. 视觉场景/背景(口播类改为"人物已合成的背景是什么";纯文案动画类为"视觉场景如何轮换")
8b. 穿插动画密度(无 / 少=每 30–40s 一张 / 多=每 15–20s 一张;口播类默认 少)
9. BGM(用户提供 / 不要 / Agent 选 CC0 并告知)
10. 专有名词表(人名/产品名,供 ASR 校对)

## 制作端 checklist(v0.12,I6:「素材差算法救不了」,开工前问一句能省一轮返工)

**talking-head 类(素材已由用户预处理的验收线)** —— v0.14 起 CutFlow 不抠像,这些是**用户侧**交付前的自检项:
- [ ] 是否已抠像并合成好最终背景?(未处理 → 直接退回用户,见 ADR-0031)
- [ ] 人物边缘是否有绿边/黑边/半透明残留?(残留 = 不合格,回用户重做)
- [ ] 输出帧率/分辨率与剪辑需求一致?(预处理导出建议常规 mp4、30fps)
- [ ] 若原素材是绿幕但**确属误报**(如纯绿布景、绿色海报),是否已在 brief/`green-ok` 说明放行?

**纯动画类(pure-animation)追加问句**:
- [ ] 比例确认 16:9 还是 9:16?(卡片按画幅导出,返工=全重导)
- [ ] 卡片总数与节奏预期?(≈2s/卡;决定旁白写法)
- [ ] 旁白是 TTS 还是真人原声?(决定 wordline 走哪条入口)
- [ ] 卡片是否已定稿?(定稿才能 scan+export,中途改卡 = 时长全漂)

**环境 checklist**(缺一开工前就补,不进渲染再发现):
- [ ] `WPI_FFMPEG` **与** `ARTBOARD_FFMPEG` 都已设置(artboard 的 MP4 导出走 WPI,只认 `WPI_FFMPEG`,`ARTBOARD_FFMPEG` 无效;安信德 #13)
- [ ] ASR 环境:`python tools/fun_asr.py --probe` 就绪(专名错 → 先补热词重跑,见 rules/asr.md)

## brief.md 契约(templates/brief.md)

问卷答案 + 素材清单 + 决策记录,落 `00_制作简报/brief.md`。**此后一切决策只查 brief,不再问人。**

## 提示词驱动(阶段四,rs_intent 编译)

用户给一句提示词时,Agent 只做**语义解析**两件事:把提示词读成 `brief.json + plan.json`、
写卡片/标题类文案;其余全部确定性编译:

- `rs_intent.py compile --brief <b.json> --plan <p.json> --out <工程>`(`--prompt <file>` 锚定原文)
  校验必填与枚举 → 补默认(**一切推断显式标 inferred**)→ 查风格注册表
  (`templates/styles/registry.json`:风格 = videoType × 平台预设 × 画幅 × 节奏档 × 字幕样式
  × 卡片模板 × BGM 库)→ 落本问卷的既有产物(`00_制作简报/brief.md`、`terms.txt`)与
  `intent_decisions.json`;`--dry-run` 只打印「字段 → 推断值 → 来源」对照表,不落盘;
- 编译是纯函数:同输入必得同一输出;无人值守全程走 `rs_run.py --auto`(决策留痕见
  `rules/incremental.md` §4.7),L2 验收始终归用户。

## 双模式

- `automation`(用户全权):所有开放项按 brief 默认值自选,理由写进 project.md;只在不可逆且高影响时(如两种完全不同的成片方向)才问。
- `companion`(协作):分镜闸门、风格闸门、成片闸门三处确认。

## 素材登记

`01_原始素材/` 收录全部素材(只读);建 `01_原始素材/MANIFEST.md`:文件名/类型/时长/内容一句话(口播视频跑 rs_frames 网格图后补内容摘要)。
