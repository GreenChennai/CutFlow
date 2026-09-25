# videoType = `vlog` · 生活记录(Vlog)

> 调研 2026-09-20(副文档 04 · 阶段四 N5 开放注册表首批新增)。来源分级:【官方】抖音创作者中心 / YouTube Creators;【权威】NN/g 视频可用性;【经验】vlog 从业者共识。
> brief 已填的值优先;本册只在 brief 缺省时兜底。注册表条目:`vlog-douyin`(templates/styles/registry.json)。
> M8 第二波起,横转竖自动重构与镜头切分已落地为可执行链路(见 §5 手册),不再靠 Agent 心算锚点。

## 1. 素材特征(与口播类的根本差异)

- **碎片多**:一段 vlog 常有十几条短碎片(运镜空镜/对话/特写),没有连续"口播稿";转写覆盖率不是硬闸,时间轴以**片段拼接**为主;
- **原声嘈杂**:环境音/风噪/背景人声大,ASR 置信度普遍偏低 → 粗剪 `review` 刀会显著变多,`--auto` 下按「宁可漏删」全保留;
- **无提词器口误**:粗剪的主要对象是**废镜头**(晃动/失焦/重复走位),不是口误重录——`retake` 检测器在本型基本不触发,属正常;
- 画面比例常混杂(横屏素材进竖屏工程),重构由 `rs_reframe plan` 自动完成、`rs_render` 渲染时自动消费(见 §5);人工仅在自动锚不满意时用 `rs_edit clip.reframe` 兜底。

## 2. 节奏策略

| 参数 | 缺省值 | 说明 |
|---|---|---|
| 视觉节拍 | 2–5s(注册表 `normal` 档) | 场景切换即节拍,不强行加卡 |
| 卡时长目标 | 1.5–5s | 一镜一事件,碎片自然落位 |
| BGM | **开**,增益 -18dB(条目 `bgmLibrary` 缺省) | vlog 的情绪骨架;人声段 ducking 已由 rs_render 固化 |
| 转场语法档 | `fade`(talkshow-bold 模板) | 硬切为主,场景大切换用 fade;**禁**花哨转场连用 |
| 语速 | 240–280 字/分钟(旁白/字幕语感) | 无旁白时只管字幕 CPS |
| 单条时长 | 30–90s(平台预设 durationHint 兜底) | 完播优先 |

- **前 3 秒定去留**通用:开场放最漂亮的空镜或结果前置,禁黑场起手;
- 结尾 3–5s:口播类 CTA 换成**下期预告/系列钩子**(vlog 观众订阅靠人设不靠单条)。

## 3. 字幕与卡片要点

- 字幕:`talkshow-bold`,只上**人声对白**(环境音/纯 BGM 段不出字幕);时间轴仍以 `wordline.json` 为唯一真相源,嘈杂段先补热词重跑 ASR 再出字幕;**无人声工程不出字幕轨**(IR 不写 subtitle.ass,渲染端不烧、不产出 ass);
- 每卡 ≤12 字(9:16)、CPS ≤9,同全库红线;对白含方言/口头语时保原话,不"书面化改写"过头;
- 卡片:**默认无卡**(`cards.density=无`)——vlog 的信息卡只做两件事:地名/时间戳角标、收尾订阅引导;要做也走 artboard 标准安全区(底部 30% 留字幕带);
- BGM 与人声抢段落时,优先保人声;无人声段落 BGM 可抬 2–3dB(在 IR 的 `bgm.gainDb` 显式改,不静默)。

## 4. 管线分支(本类型特有)

| 环节 | 做法 |
|---|---|
| S0 摄取 | 多素材逐条 probe + 幕布检测;碎片命名建议带序号(`a01_walk.mp4`) |
| S1 转写 | 主讲人声轨优先;环境音段允许低覆盖,**不得**当字级真值(onnx 降级同全库纪律);无连续口播稿时允许整段跳过 |
| S2 粗剪 | 检测器以 `dead_air`(碎片间空转)为主;`--auto` 下 review 刀全保留留痕;切点真值来自 `rs_shot detect`(§5.1) |
| S3 组装 | 多 clip 拼接 + `rs_reframe plan` 自动重构;横屏素材上竖屏画布不再手算锚(§5.2) |
| S6 音效 | 转场 whoosh 降频使用(≤2 个/15s 同全库闸);vlog 更依赖 BGM 而非音效 |
| S10 文案 | 标题口语化、第一人称;Tag 带 地点/场景 类目词 |

## 5. 横转竖自动重构 · 执行手册(M8 已落地)

### 5.1 何时跑什么

| 步骤 | 命令 | 产物 | 时机 |
|---|---|---|---|
| 镜头切分 | `rs_shot.py detect <视频> --out <工程根>`(工程模式:无参自动选素材;`--deep` 请求深度档) | `04_粗剪决策/shots.json` | S2 粗剪前,逐条素材各跑一次;切点真值供粗剪/转场决策 |
| 重构方案 | `rs_reframe.py plan [工程根] --ratio 9x16` | `05_时间线工程/reframe_plan.json` | **S3 IR 定稿之后**、渲染之前;IR 再变更后加 `--force` 重算 |

- `shots.json`:`{shots[{index,startMs,endMs,durMs}], transitions[{atMs,kind}], engine, degraded}`;降级档 `frame-diff`(ffmpeg 场景滤镜,±100ms 粒度)已实测可用;
- `reframe_plan.json`:主轨每 clip 一条 `{clipId, src, srcWidth, srcHeight, mode, anchorX, anchorY, scale, cropWindow, trajectory[], violations[]}`,坐标一律**源像素**;
- 引擎双档:`vision.track` READY(bytetrack)→ `mode=track` 逐帧主体轨迹(滑窗中位数平滑 + 限速,防「跟着人乱甩」);降级 → `static-center` 居中裁切,`degraded` 如实留痕。

### 5.2 plan 怎么被渲染消费(rs_render 已内置,无需手工干预)

- 渲染时自动读工程根下 `reframe_plan.json`:命中的 clip 走 **crop(裁切窗)→ scale(目标画幅)**;
- **裁切不拉伸**:裁切窗比例必须恒等于目标画幅比,不等即报 `REFRAME_RATIO` 拒渲(继承 artboard 桥「尺寸不符报错不拉伸」精神);
- `mode=track` 且轨迹 ≥2 关键帧 → 渲染侧按时间**线性插值**平移(平滑/限速已在 plan 侧做完);
- `violations` 非空(`REFRAME_CLIP_SUBJECT`,主体被切破)→ 该 clip **回退 static-center** 并在渲染 warnings 留痕;
- plan 缺失/坏档/源画幅或轨迹时长与 IR 对不上(过期)→ 自动退回旧 `cover_crop` 档并留痕,不阻塞渲染;plan 内容进段缓存键,方案一改该段必重渲。

### 5.3 人工改锚点:rs_edit clip.reframe(人工 > 自动)

- 对自动锚不满意:`rs_edit.py apply <工程根> --ops <ops.json>` 用 `clip.reframe`(`anchorY` 0–1 + `scale`)显式改;
- IR clip 带 `reframe` 字段时,**渲染跳过该 clip 的自动 plan**(人工优先,渲染 warnings 留痕);
- 想回自动:删掉该 clip 的 `reframe` 字段,再 `rs_reframe.py plan <工程根> --force` 重算。

### 5.4 转场 fade 规则(注册表 normal 档)

- **硬切为主**:碎片拼接默认不加 transition(无损 concat);大场景切换才 `{"type":"fade","durMs":300,"reason":"topic"}`;
- 同段内跳切用 `reason:"jumpcut"`(渲染端 1 帧软切,只吃姿态 pop 与爆音);
- **禁**花哨转场连用:wipe/slide/circleopen 本型不用;0.3s 内双转场同混剪红线。

### 5.5 BGM ducking 与人声优先

- IR:`"bgm": {"src": <绝对路径>, "gainDb": -18, "ducking": true}`(增益全库最低档,曲库授权源见 `assets/bgm/manifest.json`);
- 渲染端 sidechaincompress:有人声 clip 时 BGM 自动 ducking;**人声与 BGM 抢段落时优先保人声**;
- 无人声段想抬 BGM 2–3dB:直接改 IR `bgm.gainDb`(显式改,不静默;无人声时 ducking 自动不生效,BGM 按增益直混);
- 字幕只跟人声:无人声工程**不出字幕轨**(IR 不写 subtitle.ass),环境音/纯 BGM 段绝不出空转字幕。

## 6. 该型验收清单(方案 §5.5.2;标注「机械」的项已落 `tests/test_vlog_e2e.py`)

- [ ] `shots.json` 存在;镜头边界与人工抽查一致率 ≥90%(机械等价:fixture 切点真值 8/8 对拍);
- [ ] 横屏素材进竖屏工程**无拉伸变形**(机械:成片 1080x1920 + 已知几何方块像素抽样宽=高);`reframe` 锚点未切掉主体(机械:plan 窗包含主体包络 + 成片主体完整在框内);
- [ ] 前 3 秒有画面钩子(机械:首 3s 抽帧亮度均值/方差断言,非黑场非 logo);
- [ ] BGM 与人声不打架(机械:ducking 配置在 IR、成片有音轨;有人声 ducking 效果由 rs_render sidechaincompress 固化);
- [ ] 字幕只跟人声、无环境音空转字幕(机械:无人声工程 IR 无 subtitle.ass 且无 ass 产物);嘈杂段专名无误(热词表生效,L1);
- [ ] 碎片拼接无半帧闪烁(机械:无损 concat 整数帧段长 + 渲染 B2 对齐断言零漂移)、无重复动作残留(粗剪 review 保守保留的刀,人工确认该删的删,L1);
- [ ] 结尾有下期钩子/订阅引导,且不压字幕带(机械:IR 收尾 overlay 卡贯穿片尾 + 成片卡面像素可见);
- [ ] BGM 用授权源(机械:CC0/自产曲库 manifest 在册对拍)。
