# CutFlow Backlog(自迭代账本)

> 级别:P0 缺陷(必须马上修)/ P1 优化 / P2 弱项 / IDE 新能力想法。
> 每轮自迭代:取最高优先 1–3 项 → 实现 → 验证 → tag `iter-NN` → push。
> 2026-09-21 v0.18 清账(副文档 03 O8-1):已落地条目收口打勾,"仍成立"条目补当前证据;对照本轮交付(P17–P25、RT 闭环)无已落地仍挂 `[ ]` 的条目。

## 待办

### v0.14(2026-09-16,来源用户要求:抠像移交用户 + 幕布检测)

- [x] **抠像/背景合成移交用户(ADR-0031)**:删除 `chroma`/`background` 全链(render/ir/schema/jy/verify/文档/测试);用户预抠像+合成背景后交付剪辑
- [x] **S0 幕布检测门禁**:`rs_greenscreen.py` 抽帧判据 + `rs_ingest green-ok` 误判放行留痕 + `rs_verify` L0 二次把关
- [x] **批修**:`rs_subtitle` 卡尾标点终点锚(NCLM1605);测试 ffmpeg cfg/skipif 健壮性;`skills/cutflow-prompt/` 遗留副本
- [ ] **检测阈值实测校准(P2)**:`rs_greenscreen` 的 BORDER_FRAC/OVERALL_FRAC/MAX_CV 是经验值,需用真实绿幕/非绿幕素材各跑一批统计误报率/漏报率,必要时按素材类型分档(仍成立:v0.18 时阈值仍为经验常量,未见实测评差)
- [ ] **多段素材联合判定(P2)**:当前逐条素材独立检测;同一工程多条同源素材可合并判据(减少单条抽帧抖动)(仍成立:rs_ingest.scan 仍逐条 detect_media)

### v0.12(2026-09-14,来源 HANDOFF-v0.12-安信德GEO实测迭代与修复)

- [x] **B1–B8 必修批修**:字幕 strip↔index_map 坐标系(致命)/ 中文数字折叠 / ducking asplit / ass 缺失 WARN / L0 纳入 QC / 孤卡合并 / rs_align smooth / `-t` 输入侧陷阱;回归 `tests/test_v12.py`(250 → 281)
- [x] **I7 纯动画 IR 组装器(ADR-0027)**:`rs_ir build --from-cards`(锚点分组/停顿中点/冻结帧补长/护栏照旧)
- [x] **I1 ASR 热词链路(ADR-0028)**:`rs_align --hotwords/--terms-file`;实测默认模型 paraformer-zh 即 SeACo,透传即生效
- [x] **I2 按文本裁片**:`rs_cut --from-text`(引文顺序锚定,引文外走 guard)
- [x] **I6 制作端 checklist**:`rules/intake.md` 绿幕四问 + 纯动画四问 + 环境 checklist
- [x] **W5(部分)**:文本锚定三处重复已抽 `rs_common.content_index/anchor_span`(rs_subtitle 已委托);事件层「必并→延长→间距→校验→meta」管线去重仍开放
- [ ] **I4 说话人分离(P2,暂不做)**:仅 `interview` 类 videoType 需要(预留位),单人口播无收益;做时落点 `fun_asr --spk`(funasr `spk_model="cam++"`,CPU 可跑)→ `--json` 带 speaker → `build_wordline` 已能消费 `seg.get("speaker")`;**勿为单人口播引入 ~1GB 模型成本**(决策性挂起,非欠账)
- [ ] **I5 音频事件(IDEA)**:掌声/笑声/音乐起等事件维度(SenseVoice 方向)可给 `dead_air`/`hesitate` 做"别删"白名单;新模型 ~1GB + 新依赖,违背零第三方依赖克制,不进近期迭代(决策性挂起)
- [x] **Q5 遗留副本核查**:`D:\CutFlow` 复盘引用的副本已确认不存在(Test-Path=False)——**核查完成**;后续若其它盘再发现旧副本,diff 后只合并有测试覆盖的差异,勿整体覆盖

### v0.8.2(2026-09-13,来源 BUGREPORT-20260913-纯口播复测 B1–B10)

- [x] **B1–B10 批修**:见 `docs/BUGFIX-20260913-B1-B10.md`;音频内容闸见 ADR-0021;回归 `tests/test_v9.py`
- [ ] **I1 DP 断句语义单元惩罚**:否定词跨卡(「不|属于」)、复合词跨卡(「经营|主体」「运营|效率」「店群|企业」)罚分;「的」字头卡降权(「的市场版图」「的客流量差异」)—— 影响质量不影响正确性(仍成立:segmentation 现有 NO_TAIL/连词/词内禁切,无语义单元罚分)
- [ ] **I2 --terms 即断词保护表**:brief 术语表强制喂给 `rs_subtitle`;歧义候选里显式展示 terms 命中(部分落地:`rs_subtitle --terms` 断句禁切已可用;brief 自动喂入与候选 terms 命中展示未做,rs_run S7 命令未带 --terms)
- [ ] **I3 rs_cut 尾部黑场检测器**:源尾黑帧目前只靠 L1 目测(仍成立:v0.17 落的是实测片尾时长保底,黑帧检测未做)

### v0.8.1(2026-09-13,来源 v2 重跑实测反馈 + 分词主诉)

- [x] **W1 分词彻底修复(ADR-0020)**:`word_spans`(jieba 优先 + 内置词表兜底)+ 词内强禁切 + 空格强候选 + 两阶段 DP(降级留痕);REGRESSION 增两字词用例;`rs_doctor` jieba 检查;`fetch_deps subtitle`
- [x] **W2 Agent 复核修正闭环**:卡片 `charSpan` + `cards.json` + `rs_subtitle --override`(span→wordline 重建时间,audit 留痕);契约 `rules/subtitles.md` §10
- [x] **B1 rs_render step_mix 音频调度**:先 `atrim` 后 `adelay`(旧链序把 startMs>0 的段裁错,音轨缩到 13.9s);lavfi 实测恢复 ≈6s
- [x] **B2 rs_artboard 三连**:`project` 统一 `/src` 口径 + hash 四处一致(旧版永远误报"已变化");apply 未引用卡片默认跳过+告警(`--strict`/`--only`);挂点匹配改归一化绝对路径
- [x] **B3 junction config 错位**:仓库根优先 + `skills/config.json` 兜底只补缺;新增 `skills/config.example.json`
- [x] **B5 rs_sync 伪重叠**:snap 后碰撞消解 + 重叠容差 1 帧(`--frame-ms`),3ms 级帧取整伪影放行
- [x] **B4 ebur128**:经用户对比确认为 ffmpeg 内置滤镜且输出正常 —— **关闭,不改(决策已执行)**
- [ ] **W3 词表持续扩充**(继承原 P2 条目):新发现的切词案例 → 补 `COMMON_WORDS` + `REGRESSION` 双保险;jieba 覆盖不到的领域词也可走 brief terms(仍成立:词表随案例滚动扩,机制在 segmentation.py)
- [ ] **W4 Agent 复核实跑验证**:在真实工程上跑一遍「cards.json → Agent 审 → override 回灌」闭环(本次只落了机制与单测)(仍成立:机制 + 单测在,真实工程实跑缺)
- [ ] **W5 事件层去重(code-review 两次复核共认)**:`events_from_wordline` 与 `events_from_override` 的「必并→延长→间距→校验→meta」管线几乎逐行重复;字级锚计算(+RELEASE_MS/anchorStart/End)三处重复;main() 的 karaoke 降级三连;可抽共享管线函数(部分落地:文本锚定三处已抽 rs_common.content_index/anchor_span;事件层管线仍重复)
- [ ] **W6 rs_sync 容差按 fps 自适应**:当前默认 34ms ≈ 1 帧@30fps,60fps 素材需手动 `--frame-ms 17`;可从 IR/成片读 fps 自动定容差(仍成立:OVERLAP_TOL_MS 仍为常量默认)

### v7 落地进度(2026-09-12,来源 `docs/OPTIMIZATION-v7.md`)

**v0.7.0 字幕与粗剪修复(已完成,测试 118 → 137 全绿)**

- [x] #1 字幕↔音频同步三件套:`rs_sync` 终点偏移校验(早退 >25ms / 滞留 >350ms 硬失败)+ `rs_subtitle` 锚点有界后沿策略 + 帧对齐 + `charTimingEstimated` 显式标注(不再静默当字级)
- [x] #2 断句连词切词:`NO_TAIL` 收尾强惩罚 + 连词起首加分(从句边界优先)+ `CUT_COST` 治过度切分 + 连词回归用例
- [x] #3 粗剪废片段:跨句重录(滑动窗口 + 多次旧尝试串一刀)、`retake_block`(整段重来)、`dead_air`(音频能量/字间 gap)、`self_negative`(元话语)、**guard 按 reason 分档**
- [x] #11 清理 `rs_cut` 死代码 + 新增 `--media` / `--retake-ratio`

**v0.7.1 – v0.8.0(全部完成,测试 118 → 165 全绿)**

- [x] **P0** #4 平台字幕预设档案(抖音 / 视频号 / 小红书 / B站)+ 新增 **1080×1440(3:4)** 画幅全链路 —— **v0.7.1 已落地**:`templates/platforms.json` + `rs_common.RATIOS` 单一事实源 + `rs_subtitle --platform` + schema/verify/render/brand/ir/artboard/jy 全查表,并修掉 CPS 写死 9x16 的口径错
- [x] **P0** #5 删除 `cutflow-prompt` 技能组并归档到 `docs/archive/cutflow-prompt/` —— **v0.7.2 已落地**
- [x] **P0** #6 videoType 三类型(纯口播 / 口播+动画 / 纯动画)取代 `rules/genres/` 六册 —— **v0.7.2 已落地**（含管线分支、流畅性/贴合性硬线、纯动画双声源、题材红线并入 `_通用规则`）
- [x] **P1** #7 脚本鲁棒性审计(消灭静默降级)—— **v0.8.0 已落地**:`rs_cut` 审查包抽音频失败留痕、`textopt`/`rs_subtitle` DP 降级逐句记录、`resolve_voice` 坏卡点名、`ensure_utf8` + doctor GBK 安全
- [x] **P1** #8 ADR-0018(videoType 取代 genres)/ ADR-0019(平台字幕预设) —— **v0.7.2 已落地**
- [x] **P1** #9 补 `rs_ingest`(S0)+ `deliverables.md` 自动生成 —— **v0.8.0 已落地**
- [x] **P1** #10 `rs_dub align`:TTS 配音强制对齐 —— **v0.8.0 已落地**（真实字级回填 + 漂移报告 + 无字级戳时拒绝写回）
- [x] **P2** #12 `rs_sync` 增加「卡片 ↔ 动画卡时间窗」重叠检查 —— **v0.8.0 已落地**

**v7 验收欠账(需真实素材)**

- [ ] #A1 真实长口播素材端到端:终点偏移达标、中间段残留 ≤1 处/10min、连词不落卡尾(欠账性质:机制有合成素材回归,真实素材实测缺)
- [ ] #A2 真实素材上 `--media` 的 `dead_air` 音频能量探测实测
- [ ] #A3 `rs_cut` 的 `retakeRatio` 按 `brief.videoType` 自动取默认值(当前只有 `--retake-ratio` 手动旋钮)
- [ ] #A4 videoType 三类型各跑一条端到端(现 e2e 只覆盖 `talking-head` + 口播+动画的卡片安全区由 rs_artboard 测试覆盖)
- [ ] #A5 `rs_sync` 补「字幕时间与最近帧差 ≤1 帧」断言与段级抽样(现只校验起点/终点偏移与总时长)(仍成立:rs_sync 断言集未含帧差项)
- [ ] #A6 `platforms.json` 的 `safeArea` 目前只作为 Agent/目测清单的参考,未进脚本硬校验(字幕位置仍由 `STYLES.margin_v` 决定);若要硬校验,需把 marginV 由 safeArea 反推

### v5 落地验收清单(2026-09-10,来源 `docs/OPTIMIZATION-v5.md` §8)

**批次 A · 自带 ASR**

- [x] A1 `tools/fun_asr.py` + `--probe` 报后端状态;后端链 pkg→onnx→server
- [x] A2 vendored ONNX 推理层 + NOTICE(MIT/出处/升级方式)
- [x] A3 `fetch_deps.py asr --onnx/--pkg/--seed-models`(目录联接零拷贝)
- [x] A4 `rs_align` 改调自带运行器;**实测不启动任何外部服务即可转写**(68s→335字/5.7s,≈12× 实时)
- [x] A5 装 `pkg` 后端后验证**字级时间戳**链路 —— **已实测(2026-09-11)**:SeACo-Paraformer 944MB,334 字↔334 条时间戳,conf 0.95,degraded=False

**批次 B · 验证分级**

- [x] B1 `rs_verify.py` L0 聚合自检(IR/Wordline/guard/字幕合规/对齐/产物)
- [x] B2 `_state/verify.json` + 首次/非首次策略;**missing ≠ 变更**
- [x] B3 L1 只出清单与抽帧命令,不自动判定
- [x] B4 输出携带 `verifyLevel` / `firstCheckDone`(有测试锁)
- [ ] B5 真实成片上的 L1 目测清单走一遍(需完整素材链)

**批次 C · 省 Token**

- [x] C1 SKILL.md「谁来做」表
- [x] C2 SKILL.md「读哪个文件(读完就停)」路由表,覆盖全部 rules(有测试锁)
- [x] C3 SKILL.md 体量纪律(≤270 行,有测试锁)
- [ ] C4 实测对比:同一任务下新旧流程的 token 消耗(需真实会话采样)

**批次 D · 一键重建**

- [x] D1 `rs_run --init` 生成各文件夹 `rebuild.py` + `REBUILD.md`
- [x] D2 四步固定流程:备份 → 校验 → 级联 → 导出 + 自检
- [x] D3 `--force` 只作用于起点阶段(有测试锁)
- [x] D4 `--rollback` + 备份保留最近 5 次(有往返测试)
- [x] D5 新增 S8「烧录导出」,手改字幕不被冲掉(有测试锁)
- [ ] D6 真实素材上跑通「手改字幕 → rebuild → 出片」(需完整视频链)

**批次 E · artboard 闭环**

- [x] E1 `manifest.json` + `--scan/--export/--apply`
- [x] E2 尺寸不符报错不拉伸(有测试)
- [x] E3 时长变更平移下游 clip + 标记 stale(有测试)
- [x] E4 `03_assets/artboard/rebuild.py` 专用一条龙
- [ ] E5 用真实 artboard 工程跑一次导出回填(需 artboard 技能就位)

### 已知待办(v5 之后)

- [x] **P1** 无字级时间戳时的**跨词硬切**(实测「再加上一 / 点耐心」)—— **已解决(2026-09-11)**:pkg 字级时间戳落地,坏切分实测变为「干净的录音再加上 / 一点耐心」
- [x] **P1** `rs_render` 接入 seg 级缓存 —— **v0.6.0 已落地(2026-09-11)**:内容寻址段缓存 + step_keys 门禁,JJAV2815 实测改字幕重出片 174s→54s(seg 8/8 命中)
- [x] **P1** 补 `rs_ingest`(S0)与 `deliverables.md` 生成 —— **已落地**(v0.8.0 #9;P18 后 deliverables 还带真对账)
- [x] **P2** `fun_asr` 的 `pkg` 后端在**本机实际安装验证** —— **已实测(2026-09-11)**:torch 2.14 + torchaudio 2.11 cpu(venv `tools/.venv-asr`);timestamp 单位为 ms
- [x] **P2** `rs_artboard` 支持动画卡 `--fps` 与时长从工程导出配置读取 —— **已落地**:export_item 按 manifest item 的 kind/fps 传 `--format MP4 --fps`;时长由 apply 时 ffprobe 实测(probe_duration_ms)回填并平移下游
- [ ] **P2** 备份占用可视:`rs_cleanup` 一并清理 `_state/backup/`(范围收窄:rs_run 已有 BACKUP_KEEP=5 上限 + P13-2 清理失败如实上报;剩余是 rs_cleanup 侧的集中清理/占用展示)
- [→] **P2** 粗剪 retake 相似度阈值**按素材类型分档** → 已部分落地(v0.7.0 加 `--retake-ratio`,短剧可提到 0.86);按 `brief.videoType` 自动取默认值待接(#6)
- [x] **P2** `rs_sync` 增加「卡片 ↔ 动画卡时间窗」重叠检查 → 已随 v7 #12 落地(v0.8.0,rs_sync 卡片重叠检查)
- [x] **P2** `fa-zh` 强制对齐偏移自检 → 已并入 `rs_dub align`(v0.8.0 落地:真实字级回填 + 漂移报告)

### v4 落地验收清单(2026-09-10,来源 `docs/OPTIMIZATION-v4.md` §9)

> 批次 A 全部通过后进 B,B 与 C 可并行,D 依赖 B。

**批次 A · 对齐地基**

- [x] A1 `rs_align.py` 产出 `wordline.json`(字覆盖率 ≥99%、`conf` 中位数 ≥0.8)——**脚本就绪,待真实素材验收**
- [x] A2 字幕↔音频偏移 中位数 ≤40ms、95 分位 ≤80ms —— `rs_sync.py` 已实现并在合成素材上实测 0ms / 3ms
- [ ] A3 音画切点一致:100% 切点同帧或差 ≤1 帧(需真实素材 + 视频链路)
- [ ] A4 粗剪保守性:含 ≥3 处重录的素材 retake 检出 ≥90%、**误删 = 0**
- [ ] A5 粗剪收益:长口播素材时长减少 20–35%

**批次 B · 增量引擎**

- [x] B1 `pipeline.json` + `_state/` + 含脚本 hash 的缓存键(已单测)
- [x] B2 `rs_run.py --status/--from/--only/--dirty/--explain/--mark`
- [x] B3 `rs_render` 的 seg 级 hash 接入 —— v0.6.0 落地(segcache + prune 3 代)
- [ ] B4 单卡字幕重渲路径(只重生成该卡 ASS 事件 + 重叠)→ 端到端 ≤10s(机制已落:segcache(v0.6.0)+ --dirty 收敛(v0.17);≤10s 实测待真实素材)
- [ ] B5 无改动重跑 `--from S3` 全程命中 ≤2s(B3 已完成,实测计时待真实素材)

**批次 C · 断句 v2**

- [x] C1 DP 卡切分 + top-3 候选 + `ambiguous` 标记
- [x] C2 竖屏字数 16 → 12,引入 CPS / 时长 / 间距硬约束
- [x] C3 断句回归 5 用例写成 pytest(全绿)
- [x] C4 行断开保留评分算法 + 补「金字塔形、禁顶行 1–2 字」
- [ ] C5 CPS 合规抽样:20 卡全部 ≤9 字/秒(需真实素材)

**批次 D · 一条龙补齐**

- [x] D1 S5 品牌层 + `variants.json` + 变体渲染脚本(共享中间件)
- [x] D2 S6 音效自动落点(草案 + 密度约束 + dropped 记录)
- [x] D3 S9 `rs_meta.py` 文案生成(含 B站章节时间戳)
- [x] D4 `rs_run --status` 覆盖 S0–S10
- [ ] D5 2 Logo × 2 比例 = 4 成片,总耗时 ≤1.5× 单变体(需真实素材端到端)

### 已知待办(v4 之后)

- [x] **P1** 上游联动:MomentShift `asr_server.py` 增补 `char_timestamps=1` —— **moot(2026-09-11)**:CutFlow 自带 fun_asr pkg 后端直出字级时间戳,不再依赖上游透传
- [x] **P1** `rs_render` 按 `pipeline.json` 的 seg 清单做 seg 级缓存(B3)—— **v0.6.0 已落地**(见上方同条目;真实落点 `06_output/_build/<ratio>/segcache/`,P25-1 文档已改指实码)
- [x] **P1** 补 `rs_ingest`(S0)与 `deliverables.md` 生成,把交付清单自动化 —— **已落地**(v0.8.0 #9;本行与 v0.12 区重复挂账,一并收口)
- [x] **P2** `fa-zh` 强制对齐的偏移自检工具(切片起点回填;FunASR issue #2784)—— 已并入 `rs_dub align`(v0.8.0)
- [x] **P2** 成语/固定搭配词表扩充(当前只做 4 字整体保护 + 内置常用表)—— 并入 **W3 词表持续扩充**(同机制,不再单列)
- [x] **P2** `rs_sync` 增加「卡片 ↔ 动画卡时间窗」重叠检查(BACKLOG 原条目的自动化版)—— 已随 v7 #12 落地
- [x] **P2** 粗剪 retake 检测的相似度阈值按素材类型分档(短剧 vs 口播)—— 部分落地收口:v0.7.0 `--retake-ratio` 可调;按 videoType 自动取默认见上方 #A3

### 原有待办

- [ ] **P1** 卡片时段字幕与卡片文字叠写:卡片显示期间字幕应下沉/半透明/隐藏(ASS 可按时间段改 MarginV/alpha)

- [ ] **P2** rs_render 转场吞时长的自动预扣(目前只警告,音频/字幕 startMs 仍需 Agent 手算)
- [ ] **P2** EchoSmith 音色卡加参考音频时长校验(3-10s 引擎硬限,Jimi 卡曾用 15s+ 参考音频导致 400)
- [ ] **P2** rs_bench 采样点去重与相邻帧合并(首2s/末2s 与剪点±1.5s 常重复)
- [ ] **P2** 纯声音视频(成片 B)背景动态化:静态 PNG 偏单调,可用 artboard 6s 无缝循环 MP4 串联
- [ ] **P3** 真·剪映花字:用户 GUI 挑选花字/贴纸/音效拖入草稿保存后,反解 resource_id 建库(ADR-0005 路线 A)

- [ ] **P2** 关键词高亮字幕:ASS 富文本 span 按 IR.subtitle.highlight 上色(rich_text span 机制)
- [x] iter-02:rs_jy_draft 已写入转场(叠化/向左擦除/向上擦除/左移映射+回退)与 clip.fade 音频淡入淡出
- [ ] **P2** rs_jy_draft 写入位置/缩放关键帧(KeyframeProperty:位置滑动、缩放推拉)
- [ ] **P2** 内置 CC0 BGM 小曲库(3-5 首,带 CREDITS)+ `--bgm auto` 按情绪选曲
- [ ] **P2** MomentShift 上游 bug:asr_server._normalize_wav 直接把 build_extract_audio_cmd 结果(不含 ffmpeg 二进制)喂 subprocess → WinError 2。客户端已绕开;上游修复建议开分支提 PR(跨项目,需用户点头)
- [ ] **P2** 教程 16:9 样片第 5 格头顶留白≈0:需在原片确认未裁头皮(judge 备注)
- [ ] **P3** 剪映 GUI 自动导出时改"导出至"目录(目前用默认 Videos 再归档)
- [x] iter-02:README 已加端到端使用示例
- [ ] **P3** rs_bench 网格加时间码标签(需解决 Windows drawtext fontconfig 依赖,可用 Pillow 事后标注)
- [ ] **IDEA** 双后端能力对齐矩阵文档:哪些 IR 特性 FFmpeg 版有/5.9 版有,交付时展示
- [x] **IDEA** 卡拉OK 式逐字字幕 —— **v0.6.0 已落地(2026-09-11)**:`rs_subtitle --karaoke`,JJAV2815 实测 625 字 84 卡全 \kf;规则见 rules/subtitles.md §9

## 已完成

- [x] iter-03:P1 转场吞时长的音画漂移风险 → schema 写明语义约定 + rs_render 渲染前警告;clip.fade 字段入 schema

- [x] iter-02:修复 rs_jy_draft 时间单位错误(ms 误作 μs,历史草稿需重新生成);转场+音频淡入淡出实测落盘正确(17.5s/叠化 500ms 挂前段)

- [x] iter-01:P0 install.ps1 PowerShell 5.1 解析炸裂(无 BOM UTF-8 + 中文)→ 改 ASCII 版并实测通过;
- [x] iter-01:P1 渲染器补齐 IR 承诺的 transition(xfade 链 + acrossfade,17.53s→17.03s 实测消耗正确);
- [x] iter-01:P2 config.example.json 补 momentshift_dir 键。

## 已知非阻塞瑕疵(judge 备注)

- 画中画面板顶部带源素材灰色标题条(用户素材固有,非管线问题)
- 滑入动画方向无法从静帧核验(终帧位置已验证合规)
