# CutFlow Backlog(自迭代账本)

> 级别:P0 缺陷(必须马上修)/ P1 优化 / P2 弱项 / IDE 新能力想法。
> 每轮自迭代:取最高优先 1–3 项 → 实现 → 验证 → tag `iter-NN` → push。
> 2026-09-21 v0.18 清账(副文档 03 O8-1):已落地条目收口打勾,"仍成立"条目补当前证据;对照本轮交付(P17–P25、RT 闭环)无已落地仍挂 `[ ]` 的条目。

## v0.19 清账(2026-09-25,多风格迭代方案 §1.7 的 18 条逐条处置)

> 处置口径:**清**=本轮解决,附当前证据;**顺延**=明确不做但记账;**关门**=判定不再需要。
> 判据:不存在「已落地仍挂 `[ ]`」——下表未打勾的条目均给出顺延理由。

| # | 欠账(出处) | 处置 | 证据 / 去向 |
|---|---|---|---|
| 1 | 真实素材端到端验收缺位(v4/v5/v7 区) | **清**(以 ffmpeg 合成素材完成机械验收;用户睡嘱全权委托) | `tests/test_mixcut_e2e.py` / `test_vlog_e2e.py` / `test_drama_commentary.py` / `test_screen_e2e.py` 四型端到端全绿;成片/压缩率/占比真值断言见各文件 docstring。**真实素材复验建议由用户醒后执行**(方案 §9.4:样本外不作承诺) |
| 2 | safeArea 未进脚本硬校验(#A6) | **清** | `rs_verify.check_safe_area`(M2):声明平台的字幕底沿/贴片矩形硬判;`tests/test_deliverables.py` 夹具断言界内绿/界外红 |
| 3 | `rs_sync` OVERLAP_TOL_MS 未按 fps 自适应(W6) | **清** | v0.19:容差=1000/fps(fps 读自 `--ir` project.json;IR 缺席回退 34);`--frame-ms` 显式覆盖优先;`tests/test_v12.py` 回归绿 |
| 4 | 尾部黑场检测器缺失(I3) | **清**(并入 S9 QC) | `rs_sync.run_qc` blackdetect + `detect_waste_frames`(M8 短剧)双处覆盖;片内黑帧 ≥QC_BLACK_MIN_S 即红(首尾 0.5s 白名单口径在册) |
| 5 | `--terms` 未接进 S7 断句保护(I2 后半) | **清**(v0.21,第四册 T4.6) | brief 术语表自动喂入 rs_run S7 命令 `--terms` + 候选 termsHit 留痕(`tests/test_terms_pipeline.py`);`rs_subtitle --terms` 断词禁切 v0.12 起可用 |
| 6 | DP 断句语义单元惩罚(I1) | **清**(v0.21,第四册 T4.5) | 否定词/复合词跨卡罚分 + 「的」字头卡降权进 DP 打分(`tests/test_semantic_penalty.py`;REGRESSION 44 例) |
| 7 | W3 词表扩充 / W4 复核实跑 / W5 事件层去重 | **部分清 / 顺延** | W5 文本锚定三处已抽 `rs_common.content_index/anchor_span`(v0.12 起);事件层管线去重方案原定与 M4 EditOp 事件层合并处理——rs_edit 落地后 OpLog 侧接管变更留痕,旧管线重构降级为 P2 顺延;W3/W4 仍成立(滚动项) |
| 8 | 绿幕检测阈值实测校准(v0.14 区 P2) | **部分清**(并入抠像重建分流) | S0 分流 (b) 路线落地:`--allow-auto-matting` → `rs_matting` 五项质量门禁达标才放行(ADR-0050);rs_greenscreen 的 BORDER_FRAC/OVERALL_FRAC/MAX_CV 仍为经验初值——实测定档依赖真实素材批跑,随 #1 用户复验一并执行 |
| 9 | I4 说话人分离 | **顺延**(决策性挂起,方案原处置) | 勿为单人口播引入 ~1GB 模型;`interview` 立项时重议(M3 已实证 interview 扩展零引擎改动) |
| 10 | I5 音频事件(SenseVoice) | **关门**(方案原处置) | 改用轻量 VAD/能量事件;rs_cut dead-air 能量探测已在册 |
| 11 | 真·剪映花字(resource_id 建库) | **顺延** | 剪映 6.0+ 加密永不读写(ADR-0044);5.9 花字走 resource_id 建库成本高收益低 |
| 12 | 关键词高亮字幕(ASS 富文本 span) | **顺延**(显式不承诺) | M4 U7:`project.schema.json` 无高亮字段 → `subtitle.highlight` 为 OP_UNSUPPORTED(退出码 2,拒绝静默写 schema 外字段);补高亮字段进契约后即可启用 op |
| 13 | 内置 CC0 BGM 小曲库 + `--bgm auto` | **清** | `skills/cutflow/assets/bgm/`(自产合成 4 条,无版权约束)+ `rs_intent` `bgm: auto` 按节奏档选曲(decision_log source=library)→ `rs_ir` 接线进 IR;`tests/test_m8_infra.py` |
| 14 | 纯声音视频背景动态化 | **顺延**(方案原处置) | — |
| 15 | MomentShift `_normalize_wav` WinError 2 | **顺延**(跨项目,需用户点头,方案原处置) | — |
| 16 | `rs_bench` 采样点去重 / 网格时间码标签 | **顺延** | 方案原定 M4;M4 实际工作量让位于 rs_edit 四门禁与全链路演示;rs_bench 既有产物可用,属可读性优化 |
| 17 | 剪映 GUI 自动导出改「导出至」目录(P3) | **关门**(方案原处置) | 剪映降为单向出口(ADR-0052);`rules/jianying.md` 已如实写明 |
| 18 | 双后端能力对齐矩阵文档(IDEA) | **清** | `docs/capability-matrix.md`(M7):九能力行 × FFmpeg 管线 / cutforge-render 双后端,变速差口如实标注 |

## v2 审阅发现处置台账(2026-09-26,分册03 R 系列;ADR-0058 五要素对拍)

> 与 `CutFlow-迭代计划-v2-20260925/分册03-审阅发现.md` 双向对拍:每条 R 编号在此有结论,
> 高严重度全部 M11 修完并有红→绿回归;「不修」为零条。

| 编号 | 严重度 | 处置 | 证据/去向 |
|---|---|---|---|
| R01/R20 | 高 | **修(M11)** | normalize_markers 归一口+tests/test_markers.py(该路径此前零测试) |
| R02/R14 | 高 | **修(M11)** | cut/none 入 TRANSITIONS;test_schema_consumers 回归 |
| R03/R35 | 高 | **修(M11)** | logo_rect/check_safe_area 查 platforms.json(与 rs_verify 同源);test_m11_regressions |
| R04/R13 | 中 | **修(M11)** | bgm.loop 生效(行为变更已喊 CHANGELOG) |
| R05 | 低 | **修(M12)** | --expand logo 条目字段随 rs_asset/变体轨统一(inMs 默认口径留档) |
| R07 | 中 | **修(M11)** | schema 全枚举动态审计门禁(ADR-0055),字段漂移类整体拦截 |
| R08/R36 | 中 | **修(B 组 T2.13,v0.21)** | `rs_codes.py` 退出码与结果 code 唯一注册表(emit/die 强制校验未登记即抛);`tests/test_codes_registry.py` 全仓静态对拍 |
| R09/R41 | 中 | **修(M14)** | rs_common.load_ir() 统一入口+IR_VERSION_UNSUPPORTED 迁移引导 |
| R10-R13 | 中 | **修(M11)** | motion 五空壳真实现(zoompan/段级滑动);test_schema_consumers 九值渲染回归 |
| R15 | 中 | **修(M14)** | 探测合并+进程内缓存,实测 26→8 次 |
| R16 | 中 | **修(M14)** | 段级/变体级 --jobs 并行,jobs1 vs 4 sha256 一致;(BACKLOG「4 变体 ≤1.5× 单变体」性能口同步关账;真实素材计时仍挂 v4 区条目) |
| R17 | 低 | **修(M14)** | 指纹随探测缓存与 media_index 口径收敛 |
| R18 | 低 | **修(M14)** | rs_cut 相似度剪枝+早停 |
| R19 | 低 | **修(M14)** | 惰性探测(全缓存重跑 0 次 ffprobe) |
| R21 | 中 | **修(M14)** | tests/test_render_parallel.py(并发/临时文件/保序) |
| R22 | 中 | **修(M11)** | tests/test_cn_paths.py 中文全链端到端 |
| R23 | 中 | **修(M11)** | tests/test_m11_regressions.py(NO_LOGO/NO_INPUT/LOGO_INVALID/ASR 分支) |
| R24 | 中 | **修(M11)** | 空 IR/无 video 轨结构化错误+60fps 冒烟(test_cn_paths) |
| R25 | 中 | **修(M11)** | run_verify 超时(CUTFLOW_VERIFY_TIMEOUT) |
| R26 | 中 | **修(M11)** | ASR 超时(CUTFLOW_ASR_TIMEOUT)+R40 比例超时先行 |
| R27 | 低 | **修(M14)** | .tmp_<段号> 命名+失败清扫 |
| R28 | 低 | **修(M11)** | NO_VIDEO_TRACK 结构化错误 |
| R29/R34 | 低 | **修(M12)** | rs_sfx 查 manifest+usage;统计取轨修正 |
| R30 | — | **修(M11)** | tests/test_field_contracts.py 字段级契约对拍 |
| R31 | — | **顺延** | rs_bench 采样优化属可读性,本轮让位主线(仍成立) |
| R32/R33 | — | **修(v0.19)** | 尾部黑场/W6 fps 自适应已在 v0.19 落地(分册03 基线 v0.18.1 早于 v0.19) |
| R37 | — | **修(M14)** | 性能趋势入 docs/context-budget.md(墙钟/加速比/探测次数) |
| R38 | — | **修(M12)** | 体积预算门禁(rs_asset check);rs_cleanup「素材缓存」类别顺延 M16 |
| R39/R40 | — | **修(M14)** | --verbose+pipeline 墙钟;比例超时 |
| R42 | — | **修(M12)** | 风格包 bgmLibrary 接 manifest id 闭环 |
| R43/R44 | — | **修(M13)** | catalog usage↔处方对拍;gl-transitions 逐条 MIT 头核验 |
| R45 | — | **修(M12)** | manifest aiGenerated 字段+归因清单 |
| U1-U4 | — | **关闭** | U1 惰性探测实测;U2 枚举审计覆盖;U4 R30 字段表 |

## v0.21.0 收口对账(2026-09-28,多册迭代第零–五册)

> 口径同 v0.19 清账:**清**=本轮解决,附证据;**部分达成**=本轮覆盖面 + 残余缺口(如实记账);
> **仍开**=本轮未覆盖。第一册已收口的矛盾项(W6/尾部黑场/剪映 GUI 导出目录/IDEA 矩阵/I5/#A6)
> 本轮以全量测试 1596 绿复核:无回归(16x9 安全区判定还在 C 组被真实素材进一步加固,见 T2.16c)。

| ID(出处) | 处置 | 证据 / 残余 |
|---|---|---|
| I1 语义单元罚分(v0.8.2 区) | **清**(第四册 T4.5) | 否定词跨卡/复合词跨卡罚分 + 「的」字头卡降权进 DP 打分;`tests/test_semantic_penalty.py`;REGRESSION 11→44 例 |
| I2 --terms 接线(v0.8.2 区) | **清**(第四册 T4.6) | brief 术语表自动喂入 rs_run S7 `--terms` + 候选边界 termsHit 命中留痕;`tests/test_terms_pipeline.py` |
| R08/R36 错误码集中(v2 审阅台账) | **清**(第二册 B 组 T2.13) | `rs_codes.py` 退出码与结果 code 唯一注册表(emit/die 强制校验,未登记即抛);`tests/test_codes_registry.py` 全仓静态对拍 |
| v4-A3 音画切点一致(v4 批次 A) | 达成(第二册 C 组;同名 ID 分属两义——v7 区 #A3 为 retakeRatio 自动档,仍开,各归各小节) | assetA 364s/41 段:rs_ir keep 段长帧网格量化修复后拼接漂移归零(`tests/test_frame_exact_segments.py`);渲染端音画对齐断言全过;评分卡 D1 35/35/35(结论落 v4 小节该行) |
| v4-B4 单卡字幕重渲 ≤10s(v4 批次 B) | **清**(第二册 C 组) | 真实素材实测 5.10s 达标(机制面 segcache + --dirty 收敛在册) |
| v5-E5 artboard 导出回填(v5 批次 E) | **清**(第二册 C 组) | assetC 16x9 真实工程回填实测跑通;顺带揪出并修复 rs_artboard 清单路径基准 bug(`tests/test_artboard_item_base.py`) |
| v4-B5 无改动重跑 ≤2s(v4 批次 B) | **部分达成**(第二册 C 组) | 真实工程实测 3.11s 超 2s 门,如实记账。残差说明:41 段/364s 规模下全缓存命中路径仍付逐段内容指纹核验与状态灯判定的固定开销(合成素材 12 阶段同口径仅 0.67s),属随工程规模的固定开销、非缓存失效;零重渲的收敛性本身已达成 |
| v4-A4 粗剪保守性(v4 批次 A) | **部分达成**(第二册 C 组) | 3 条真实素材 guard 全过(评分卡 D3 14/14/14)无误删红项;「含 ≥3 处重录素材检出 ≥90%」无专门样本,检出率口径未测 |
| v4-A5 粗剪收益 20–35%(v4 批次 A) | **部分达成**(第二册 C 组) | 真实素材粗剪链路已通;删留比未按本口径逐条成账 |
| v4-C5 CPS 合规抽样(v4 批次 C) | **部分达成**(第二册 C 组) | 判定方式升级:抽样改评分卡 D2 逐卡机械判定;3 条真实素材实测 D2=16/20 未满,扣分项回修后复测(明细见 docs/METRICS.md) |
| v5-B5 真实成片 L1 目测(v5 批次 B) | **部分达成**(第二册 C 组) | 3 条真实成片已产 verify_report(L1 证据面 + 评分卡人工签核清单,人工 9 分另列);目测签核动作本身归维护者 |
| v5-C4 token 消耗实测对比(v5 批次 C) | **部分达成**(第二册 C 组) | 第一组真实会话采样已得:全流程任务实际读入 398 行(对照名义最小必读 1469/治理前同口径 1552,见 docs/METRICS.md);样本量 1,行数口径非 token 精确计量 |
| v7-#A1 真实长口播端到端(v7 验收欠账) | **部分达成**(第二册 C 组) | 终点偏移/早退/滞留在 3 条真实素材机械判定全过(D1 35/35/35);「中间段残留 ≤1 处/10min」「连词不落卡尾」的抽样口径未系统执行 |
| v7-#A4 三类型各一条端到端(v7 验收欠账) | **部分达成**(第二册 C 组) | 合成素材四型 e2e 在册 + 真实素材 3 条已跑(assetA/B/C);真实素材类型矩阵未对照三类型逐一成账 |
| v4-D5 4 变体 ≤1.5× 单变体(v4 批次 D) | **仍开**(第二册 C 组遗留) | 变体并发机制在册(v0.20 M14,--jobs 1/4 产物 sha256 一致);真实素材 4 变体端到端计时本轮未跑 |
| v5-D6 手改字幕→rebuild→出片(v5 批次 D) | **仍开**(第二册 C 组遗留) | 本轮真实素材链未含手改字幕环节;机制面 rebuild 链在册(v0.17/P16) |
| v7-#A2 dead_air 能量探测实测(v7 验收欠账) | **仍开**(第二册 C 组遗留) | S2 spec 的 rs_cut 命令不带 `--media`,`--auto` 全链不触发能量探测,真实素材端到端亦未覆盖此路径;直调实测缺 |

### C 组建议新条目(2026-09-28,来源:第二册 C 组真实素材端到端复盘)

- [ ] **freeze 闸对图文品类误报需产品裁决**:`rs_sync.run_qc` freezedetect 对图文/卡片类成片(整卡静止是品类常态)可能整段判红;阈值分档或品类白名单属产品决策,非纯技术修(owner: 待产品裁决;来源: 第二册 C 组)
- [ ] **B 响度动态模式欠冲 ~1LU,建议混音预增益**:双 pass linear loudnorm 在增益需求越 TP 约束时回退动态模式并欠冲(实测 ~1LU);建议混音侧预增益补足,不动总线目标值(owner: 待排期;来源: 第二册 C 组)
- [ ] **test_v15 fixture 维护者对齐**:`tests/test_v15_diagnosis.py` 依赖 `tests/fixtures/diagnosis/`(make_fixtures.py 生成)+ 本地 ASR,夹具与生产者口径需维护者对齐一轮(本轮 fixtures/diagnosis 有改动,防漂移)(owner: 维护者;来源: 第二册 C 组)

## 待办

### v0.14(2026-09-16,来源用户要求:抠像移交用户 + 幕布检测)

- [x] **抠像/背景合成移交用户(ADR-0031)**:删除 `chroma`/`background` 全链(render/ir/schema/jy/verify/文档/测试);用户预抠像+合成背景后交付剪辑
- [x] **S0 幕布检测门禁**:`rs_greenscreen.py` 抽帧判据 + `rs_ingest green-ok` 误判放行留痕 + `rs_verify` L0 二次把关
- [x] **批修**:`rs_subtitle` 卡尾标点终点锚(NCLM1605);测试 ffmpeg cfg/skipif 健壮性;`skills/cutflow-prompt/` 遗留副本
- [ ] **检测阈值实测校准(P2)**:`rs_greenscreen` 的 BORDER_FRAC/OVERALL_FRAC/MAX_CV 是经验值,需用真实绿幕/非绿幕素材各跑一批统计误报率/漏报率,必要时按素材类型分档(仍成立:v0.18 时阈值仍为经验常量,未见实测评差)(owner: 第三册(Agent 感知)+ 真实素材复验)
- [ ] **多段素材联合判定(P2)**:当前逐条素材独立检测;同一工程多条同源素材可合并判据(减少单条抽帧抖动)(仍成立:rs_ingest.scan 仍逐条 detect_media)(owner: 第三册(Agent 感知))

### v0.12(2026-09-14,来源 `docs/archive/HANDOFF-v0.12-安信德GEO实测迭代与修复.md`)

- [x] **B1–B8 必修批修**:字幕 strip↔index_map 坐标系(致命)/ 中文数字折叠 / ducking asplit / ass 缺失 WARN / L0 纳入 QC / 孤卡合并 / rs_align smooth / `-t` 输入侧陷阱;回归 `tests/test_v12.py`(250 → 281)
- [x] **I7 纯动画 IR 组装器(ADR-0027)**:`rs_ir build --from-cards`(锚点分组/停顿中点/冻结帧补长/护栏照旧)
- [x] **I1 ASR 热词链路(ADR-0028)**:`rs_align --hotwords/--terms-file`;实测默认模型 paraformer-zh 即 SeACo,透传即生效
- [x] **I2 按文本裁片**:`rs_cut --from-text`(引文顺序锚定,引文外走 guard)
- [x] **I6 制作端 checklist**:`rules/intake.md` 绿幕四问 + 纯动画四问 + 环境 checklist
- [x] **W5(部分)**:文本锚定三处重复已抽 `rs_common.content_index/anchor_span`(rs_subtitle 已委托);事件层「必并→延长→间距→校验→meta」管线去重仍开放
- [ ] **I4 说话人分离(P2,暂不做)**:仅 `interview` 类 videoType 需要(预留位),单人口播无收益;做时落点 `fun_asr --spk`(funasr `spk_model="cam++"`,CPU 可跑)→ `--json` 带 speaker → `build_wordline` 已能消费 `seg.get("speaker")`;**勿为单人口播引入 ~1GB 模型成本**(决策性挂起,非欠账)
- [x] **I5 音频事件(IDEA)** —— **关门**(见「v0.19 清账」#10:改用轻量 VAD/能量事件,rs_cut dead-air 已在册;新模型 + 新依赖违背零依赖克制;2026-09-27 收口)。owner: 已关门
- [x] **Q5 遗留副本核查**:`D:\CutFlow` 复盘引用的副本已确认不存在(Test-Path=False)——**核查完成**;后续若其它盘再发现旧副本,diff 后只合并有测试覆盖的差异,勿整体覆盖

### v0.8.2(2026-09-13,来源 `docs/archive/BUGREPORT-20260913-纯口播复测.md` B1–B10)

- [x] **B1–B10 批修**:见 `docs/archive/BUGFIX-20260913-B1-B10.md`;音频内容闸见 ADR-0021;回归 `tests/test_v9.py`
- [x] **I1 DP 断句语义单元罚分进 DP**:已于第四册 T4.5 落地并清账(否定词跨卡「不|属于」、复合词跨卡「经营|主体」「运营|效率」「店群|企业」罚分;「的」字头卡降权「的市场版图」「的客流量差异」;回归 `tests/test_semantic_penalty.py`,REGRESSION 扩至 44 例)——见「v0.21.0 收口对账」;本行原「仍成立」结论过时,2026-09-28 收口。owner: 已清(第四册)
- [x] **I2 --terms 即断词保护表**:已于第四册 T4.6 落地并清账(brief 术语表自动喂入 `rs_run` S7 命令 `--terms`;候选边界 termsHit 命中显式留痕;`rs_subtitle --terms` 断词禁切 v0.12 起可用;回归 `tests/test_terms_pipeline.py`)——见「v0.21.0 收口对账」;本行原结论过时,2026-09-28 收口。owner: 已清(第四册)
- [x] **I3 rs_cut 尾部黑场检测器**:已于 v0.19 落地并清账(`rs_sync.run_qc` blackdetect + `detect_waste_frames` 双覆盖,并入 S9 QC)——见上方「v0.19 清账」#4;本行原「仍成立」结论过时,2026-09-27 收口。owner: 已清(第二册验收)

### v0.8.1(2026-09-13,来源 v2 重跑实测反馈 + 分词主诉)

- [x] **W1 分词彻底修复(ADR-0020)**:`word_spans`(jieba 优先 + 内置词表兜底)+ 词内强禁切 + 空格强候选 + 两阶段 DP(降级留痕);REGRESSION 增两字词用例;`rs_doctor` jieba 检查;`fetch_deps subtitle`
- [x] **W2 Agent 复核修正闭环**:卡片 `charSpan` + `cards.json` + `rs_subtitle --override`(span→wordline 重建时间,audit 留痕);契约 `rules/subtitles.md` §10
- [x] **B1 rs_render step_mix 音频调度**:先 `atrim` 后 `adelay`(旧链序把 startMs>0 的段裁错,音轨缩到 13.9s);lavfi 实测恢复 ≈6s
- [x] **B2 rs_artboard 三连**:`project` 统一 `/src` 口径 + hash 四处一致(旧版永远误报"已变化");apply 未引用卡片默认跳过+告警(`--strict`/`--only`);挂点匹配改归一化绝对路径
- [x] **B3 junction config 错位**:仓库根优先 + `skills/config.json` 兜底只补缺;新增 `skills/config.example.json`
- [x] **B5 rs_sync 伪重叠**:snap 后碰撞消解 + 重叠容差 1 帧(`--frame-ms`),3ms 级帧取整伪影放行
- [x] **B4 ebur128**:经用户对比确认为 ffmpeg 内置滤镜且输出正常 —— **关闭,不改(决策已执行)**
- [ ] **W3 词表持续扩充**(继承原 P2 条目):新发现的切词案例 → 补 `COMMON_WORDS` + `REGRESSION` 双保险;jieba 覆盖不到的领域词也可走 brief terms(仍成立:词表随案例滚动扩,机制在 segmentation.py)(owner: 第四册(字幕断句与分词重构,滚动项))
- [ ] **W4 Agent 复核实跑验证**:在真实工程上跑一遍「cards.json → Agent 审 → override 回灌」闭环(本次只落了机制与单测)(仍成立:机制 + 单测在,真实工程实跑缺)(owner: 第五册(验收回归与度量))
- [ ] **W5 事件层去重(code-review 两次复核共认)**:`events_from_wordline` 与 `events_from_override` 的「必并→延长→间距→校验→meta」管线几乎逐行重复;字级锚计算(+RELEASE_MS/anchorStart/End)三处重复;main() 的 karaoke 降级三连;可抽共享管线函数(部分落地:文本锚定三处已抽 rs_common.content_index/anchor_span;事件层管线仍重复)(owner: 第二册(流程规范化与缺陷清扫))
- [x] **W6 rs_sync 容差按 fps 自适应**:已于 v0.19 落地并清账(容差=1000/fps,IR 缺席回退 34;`--frame-ms` 显式覆盖优先;`tests/test_v12.py`)——见上方「v0.19 清账」#3;本行原「仍成立」结论过时,2026-09-27 收口。owner: 已清(第二册验收)

### v7 落地进度(2026-09-12,来源 `docs/archive/OPTIMIZATION-v7.md`)

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

- [ ] #A1 真实长口播素材端到端:终点偏移达标、中间段残留 ≤1 处/10min、连词不落卡尾(v0.21 覆盖:终点偏移/早退/滞留在 3 条真实素材机械判定全过,评分卡 D1 35/35/35;残余:「中间段残留 ≤1 处/10min」「连词不落卡尾」的抽样口径未系统执行,真实素材类型面未成矩阵)(owner: 第二册 C 组遗留)
- [ ] #A2 真实素材上 `--media` 的 `dead_air` 音频能量探测实测(v0.21 复核:S2 spec 的 rs_cut 命令不带 `--media`,`--auto` 全链不触发能量探测,真实素材端到端亦未覆盖;直调实测仍缺)(owner: 第二册 C 组遗留)
- [ ] #A3 `rs_cut` 的 `retakeRatio` 按 `brief.videoType` 自动取默认值(当前只有 `--retake-ratio` 手动旋钮,v0.21 复核仍未接)(owner: 第二册(流程规范化))
- [ ] #A4 videoType 三类型各跑一条端到端(v0.21 覆盖:合成素材四型 e2e 在册 + 真实素材 3 条端到端已跑(assetA/B/C);残余:真实素材类型矩阵未对照三类型逐一成账)(owner: 第二册 C 组遗留)
- [ ] #A5 `rs_sync` 补「字幕时间与最近帧差 ≤1 帧」断言与段级抽样(v0.21 复核:rs_sync 断言集仍未含帧差项;帧对齐目前靠 rs_subtitle 生成期 snap + rs_ir 段长帧网格量化,C 组实测拼接漂移归零)(owner: 第二册(流程规范化))
- [x] #A6 `platforms.json` 的 `safeArea` 未进脚本硬校验 —— **已清**(v0.19 清账 #2:`rs_verify.check_safe_area` 硬判 + `tests/test_deliverables.py`;2026-09-27 收口,本行原开账过时)。owner: 已清

### v5 落地验收清单(2026-09-10,来源 `docs/archive/OPTIMIZATION-v5.md` §8)

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
- [ ] B5 真实成片上的 L1 目测清单走一遍(v0.21 覆盖:3 条真实成片已产 verify_report,含 L1 证据面与评分卡人工签核清单——机械 91 分之外的人工 9 分另列;残余:人工按清单目测并签核的动作归维护者,本轮未执行)(owner: 维护者签核)

**批次 C · 省 Token**

- [x] C1 SKILL.md「谁来做」表
- [x] C2 SKILL.md「读哪个文件(读完就停)」路由表,覆盖全部 rules(有测试锁)
- [x] C3 SKILL.md 体量纪律(≤270 行,有测试锁)
- [ ] C4 实测对比:同一任务下新旧流程的 token 消耗(v0.21 覆盖:第一组真实会话采样已得——C 组真实素材全流程任务实际读入 398 行,对照名义最小必读 1469/治理前同口径 1552,见 docs/METRICS.md;残余:样本量 1,行数口径非 token 精确计量)(owner: 第五册(验收回归与度量))

**批次 D · 一键重建**

- [x] D1 `rs_run --init` 生成各文件夹 `rebuild.py` + `REBUILD.md`
- [x] D2 四步固定流程:备份 → 校验 → 级联 → 导出 + 自检
- [x] D3 `--force` 只作用于起点阶段(有测试锁)
- [x] D4 `--rollback` + 备份保留最近 5 次(有往返测试)
- [x] D5 新增 S8「烧录导出」,手改字幕不被冲掉(有测试锁)
- [ ] D6 真实素材上跑通「手改字幕 → rebuild → 出片」(v0.21 未覆盖:真实素材链未含手改字幕环节;机制面 rebuild 链在册,v0.17/P16)(owner: 第五册(验收回归与度量))

**批次 E · artboard 闭环**

- [x] E1 `manifest.json` + `--scan/--export/--apply`
- [x] E2 尺寸不符报错不拉伸(有测试)
- [x] E3 时长变更平移下游 clip + 标记 stale(有测试)
- [x] E4 `03_assets/artboard/rebuild.py` 专用一条龙
- [x] E5 用真实 artboard 工程跑一次导出回填 —— **已清**(v0.21 C 组:assetC 16x9 真实工程回填实测跑通,顺带揪出并修复 rs_artboard 清单路径基准 bug,`tests/test_artboard_item_base.py`)——见「v0.21.0 收口对账」。owner: 已清(第二册 C 组)

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

### v4 落地验收清单(2026-09-10,来源 `docs/archive/OPTIMIZATION-v4.md` §9)

> 批次 A 全部通过后进 B,B 与 C 可并行,D 依赖 B。

**批次 A · 对齐地基**

- [x] A1 `rs_align.py` 产出 `wordline.json`(字覆盖率 ≥99%、`conf` 中位数 ≥0.8)——**脚本就绪,待真实素材验收**
- [x] A2 字幕↔音频偏移 中位数 ≤40ms、95 分位 ≤80ms —— `rs_sync.py` 已实现并在合成素材上实测 0ms / 3ms
- [x] A3 音画切点一致:100% 切点同帧或差 ≤1 帧 —— **已清**(v0.21 C 组真实素材实测:assetA 364s/41 段渲染端音画对齐断言全过;rs_ir keep 段长帧网格量化修复后拼接漂移归零,`tests/test_frame_exact_segments.py`;评分卡 D1 35/35/35)——见「v0.21.0 收口对账」。owner: 已清(第二册 C 组)
- [ ] A4 粗剪保守性:含 ≥3 处重录的素材 retake 检出 ≥90%、**误删 = 0**(v0.21 C 组覆盖:3 条真实素材 guard 全过,D3 14/14/14 无误删红项;残余:无含 ≥3 处重录的专门样本,检出率口径未测)(owner: 第二册 C 组遗留)
- [ ] A5 粗剪收益:长口播素材时长减少 20–35%(v0.21 C 组覆盖:真实素材粗剪链路已通;残余:删留比未按本口径逐条成账)(owner: 第二册 C 组遗留)

**批次 B · 增量引擎**

- [x] B1 `pipeline.json` + `_state/` + 含脚本 hash 的缓存键(已单测)
- [x] B2 `rs_run.py --status/--from/--only/--dirty/--explain/--mark`
- [x] B3 `rs_render` 的 seg 级 hash 接入 —— v0.6.0 落地(segcache + prune 3 代)
- [x] B4 单卡字幕重渲路径(只重生成该卡 ASS 事件 + 重叠)→ 端到端 ≤10s —— **已清**(机制面 segcache(v0.6.0)+ --dirty 收敛(v0.17);v0.21 C 组真实素材实测 5.10s 达标)——见「v0.21.0 收口对账」。owner: 已清(第二册 C 组)
- [ ] B5 无改动重跑 `--from S3` 全程命中 ≤2s(v0.21 C 组真实工程实测 3.11s 未达标,如实记部分达成:全缓存命中路径在 41 段/364s 规模下仍付逐段内容指纹核验与状态灯判定的固定开销,合成素材 12 阶段同口径仅 0.67s,属规模性固定开销而非缓存失效;零重渲收敛已达成;B3 本身已完成)(owner: 第二册 C 组遗留)

**批次 C · 断句 v2**

- [x] C1 DP 卡切分 + top-3 候选 + `ambiguous` 标记
- [x] C2 竖屏字数 16 → 12,引入 CPS / 时长 / 间距硬约束
- [x] C3 断句回归 5 用例写成 pytest(全绿)
- [x] C4 行断开保留评分算法 + 补「金字塔形、禁顶行 1–2 字」
- [ ] C5 CPS 合规抽样:20 卡全部 ≤9 字/秒(v0.21 覆盖:判定方式升级为评分卡 D2 逐卡机械判定,不再抽样;3 条真实素材实测 D2=16/20 未满,扣分项回修后复测)(owner: 第二册 C 组遗留)

**批次 D · 一条龙补齐**

- [x] D1 S5 品牌层 + `variants.json` + 变体渲染脚本(共享中间件)
- [x] D2 S6 音效自动落点(草案 + 密度约束 + dropped 记录)
- [x] D3 S9 `rs_meta.py` 文案生成(含 B站章节时间戳)
- [x] D4 `rs_run --status` 覆盖 S0–S10
- [ ] D5 2 Logo × 2 比例 = 4 成片,总耗时 ≤1.5× 单变体(v0.21 未覆盖:变体并发机制在册(v0.20 M14,--jobs 1/4 产物 sha256 一致);残余:真实素材 4 变体端到端计时仍缺)(owner: 第二册 C 组遗留)

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
- [x] **P3** 剪映 GUI 自动导出时改"导出至"目录 —— **关门**(见「v0.19 清账」#17:剪映降为单向出口 ADR-0052,不再投入;2026-09-27 收口)。owner: 已关门
- [x] iter-02:README 已加端到端使用示例
- [ ] **P3** rs_bench 网格加时间码标签(需解决 Windows drawtext fontconfig 依赖,可用 Pillow 事后标注)
- [x] **IDEA** 双后端能力对齐矩阵文档 —— **已落地并清账**(见「v0.19 清账」#18 → `docs/capability-matrix.md`;2026-09-27 收口)。owner: 已清
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

## v0.20 追加(2026-09-26 深夜)

- [ ] **check_manual_cmds AST 重放器对 rs_asset 的局限**:rs_asset.py 单 parser 承载 9 个子命令,重放器整文件重放时 --kind 冲突崩 → sfx.md 的 add/list 示例暂以非反引号形态绕开。正解:rs_asset 拆 per-subcommand parser 函数(与 rs_run spec 同构),重放器即可逐子命令对拍。归属下一轮。(owner: 第二册(流程规范化与缺陷清扫))
