# ADR-0018:videoType 三类型取代 genres 六册

状态:已采纳 ｜ 日期:2026-09-12（v0.7.2）

## 背景

用户要求:「视频剪辑部分可以制作**三种类型**的视频(更多数量可以先留空占位):①纯口播(注意人物绿幕与台词字幕);②口播+部分动画(在①基础上注意动画的**流畅性**与**贴合性**);③纯动画(声音可用模型合成 / 只用视频中人物的口播语音)」;同时要求「两个技能组先完善第一个,第二个删掉」。

原 ADR-0010 的 `rules/genres/` 是**按题材**分的六册(口播知识 / 动画教程 / 新闻采访 / 短剧 / 影视解说 / 通用)。但用户真正需要的是**按成片形态**分类,因为:

- 形态决定**管线分支**:要不要绿幕抠像(colorkey+despill)、动画密度是"少/零"还是"2–3s 一张"、声音从哪来(原声 vs 音色卡 TTS);
- 题材只决定**风格默认值**(节奏/字幕字数/红线),不能表达"这条片子走哪条渲染路";
- 六册里"口播知识"与"动画教程"其实是一种形态的两种密度,而"新闻采访/短剧/影视解说"是题材红线,与形态正交。

旧结构下,Agent 拿到 brief 得自己从题材反推管线;`SKILL.md` 也只能说"必读对应分册",说不清"读了要改什么管线"。

## 决策

1. **`videoType` 成为一级路由**(写入 `brief.md` 与 `rules/intake.md` 第 0 项),封闭枚举:
   - `talking-head`(纯口播)
   - `talking-head+animation`(口播+部分动画)
   - `pure-animation`(纯动画)
   - **预留扩展位**(注释保留、暂不实现):`screen-recording` / `interview` / `drama` / `film-commentary`
2. **新增 `rules/video-types/`** 取代 `rules/genres/`,每类型一册 + `_通用规则.md`。每册固定结构:
   `## 1. 管线分支(本类型特有)` → `## 2. 节奏参数表` → `## 3. 结构模板` → `## 4. CutFlow 对应` → `## 5. 红线`。
   三类型各自的管线要点:
   - **纯口播**:绿幕必抠(`cropTopPct` → `colorkey`+`despill`,裸 `chromakey` 在本机构建上输出半透明人像,禁用)、虚拟背景、字幕重中之重、动画密度"少或零"、粗剪必做、声音原声优先;
   - **口播+动画**:继承纯口播全部要求,另加 artboard 卡片体系;**流畅性硬线**(入场 ease-out/退场 ease-in/屏内 ease-in-out、动效 200–300ms、禁线性匀速、文字 1s/13 字、最短停留 1.5s)与**贴合性硬线**(画面必须图解旁白正在说的那句、卡片时间窗必须落在其解说的句子时间窗内、错位 >1 卡即不合格、人物与卡片切换时口播不停);
   - **纯动画**:无真人画面,场景卡 + 6s 循环背景;**声音来源二选一(必在 brief 声明)**:音色卡 TTS(`rs_tts`)或视频中人物原声(抽轨重采样);TTS 无字级时间戳时标 `charTimingEstimated`,不得当字级用。
3. **信息先并入再删除**:题材类红线(新闻采访的"硬新闻不配乐/不改语义"、短剧的"3s 一钩子/分类分层审核"、影视解说的"版权四要素")收进 `rules/video-types/_通用规则.md` 的「类型补充」小节并标注"暂停维护",`rules/genres/` 随后删除。
4. **同步更新**:`templates/brief.md`(类型 → `videoType` + 声音来源)、`rules/intake.md` 项 0、`SKILL.md` 路由表与 Hard Rule 17、`README.md`、`CONTEXT.md`(术语表新增 videoType 与平台/画幅两节)。
5. **第二个技能组**:`skills/cutflow-prompt/` 归档到 `docs/archive/cutflow-prompt/` 后删除;`tools/install.ps1` 只安装 `cutflow`;`docs/PLAN.md` 加停用横幅(历史段落不改写)。

## 后果

- brief 一填 `videoType`,Agent 就能确定"走哪条管线 + 读哪一册",不再从题材反推;
- 类型知识从 6 册降为 4 份文件,信息无净损失(题材红线有归档位);
- 扩展新类型只需:加 `videoType` 枚举值 + 加一册 + 在 `intake.md` 项 0 登记。**约定:暂不实现的枚举值只写注释,不建空文件**,避免出现"读了发现是空壳"的册子;
- **`videoType` 刻意不进代码分支**:它决定"Agent 读哪一册 + brief 里写哪些默认参数"(动画密度、声音来源、要不要绿幕抠像都落成 brief/IR 字段),脚本侧不做 `if videoType == ...` 判断 —— 这样新增类型只动文档与 brief,不动管线代码;
- 代价:`videoType` 与"平台/画幅"是两个正交维度(形态 vs 发布规格),别混为一谈——平台参数在 `rules/platforms.md`(ADR-0019);
- 代价:`brief` 的旧字段名"类型"被 `videoType` 取代,历史工程的 brief 需人工映射。

## 关联

- `rules/video-types/{纯口播,口播+动画,纯动画,_通用规则}.md`、`rules/intake.md`、`templates/brief.md`、`SKILL.md`、`CONTEXT.md`
- 取代 ADR-0010(六册题材分类);保留 ADR-0003(绿幕管线)、ADR-0004(穿插动画视觉节拍)、ADR-0006(双声源/成片 B)、ADR-0009(视频卡安全区)
- `docs/OPTIMIZATION-v7.md` #5 / #6
