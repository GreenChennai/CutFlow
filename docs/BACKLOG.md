# CutFlow Backlog(自迭代账本)

> 级别:P0 缺陷(必须马上修)/ P1 优化 / P2 弱项 / IDE 新能力想法。
> 每轮自迭代:取最高优先 1–3 项 → 实现 → 验证 → tag `iter-NN` → push。

## 待办

### v5 落地验收清单(2026-09-10,来源 `docs/OPTIMIZATION-v5.md` §8)

**批次 A · 自带 ASR**

- [x] A1 `tools/fun_asr.py` + `--probe` 报后端状态;后端链 pkg→onnx→server
- [x] A2 vendored ONNX 推理层 + NOTICE(MIT/出处/升级方式)
- [x] A3 `fetch_deps.py asr --onnx/--pkg/--seed-models`(目录联接零拷贝)
- [x] A4 `rs_align` 改调自带运行器;**实测不启动任何外部服务即可转写**(68s→335字/5.7s,≈12× 实时)
- [ ] A5 装 `pkg` 后端后验证**字级时间戳**链路(需 `fetch_deps.py asr --pkg`,约 1–2GB;未在此机器上装)

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

- [ ] **P1** 无字级时间戳时的**跨词硬切**(实测「再加上一 / 点耐心」):堆规则解决不了,正解是字级时间戳(pkg 后端或换带 timestamp 的 ONNX 导出);先用 pkg 后端缓解
- [ ] **P1** `rs_render` 接入 seg 级缓存(沿用 v4 批次 B3)
- [ ] **P1** 补 `rs_ingest`(S0)与 `deliverables.md` 生成
- [ ] **P2** `fun_asr` 的 `pkg` 后端在**本机实际安装验证**(torch-cpu + funasr),并确认字级 timestamp 单位(ms vs 10ms 帧)
- [ ] **P2** `rs_artboard` 支持动画卡 `--fps` 与时长从工程导出配置读取
- [ ] **P2** 备份占用可视:`rs_cleanup` 一并清理 `_state/backup/`

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
- [ ] B3 `rs_render` 的 seg 级 hash 接入(结构已现成,待挂清单)
- [ ] B4 单卡字幕重渲路径(只重生成该卡 ASS 事件 + 重叠)→ 端到端 ≤10s
- [ ] B5 无改动重跑 `--from S3` 全程命中 ≤2s(需 B3 完成)

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

- [ ] **P1** 上游联动:MomentShift `asr_server.py` 增补 `char_timestamps=1`,把 Paraformer 原生字级 `timestamp` 透传出来(跨项目,需用户点头后再开分支提 PR);在此之前 CutFlow 走句级降级路径
- [ ] **P1** `rs_render` 按 `pipeline.json` 的 seg 清单做 seg 级缓存(B3)
- [ ] **P1** 补 `rs_ingest`(S0)与 `deliverables.md` 生成,把交付清单自动化
- [ ] **P2** `fa-zh` 强制对齐的偏移自检工具(切片起点回填;FunASR issue #2784)
- [ ] **P2** 成语/固定搭配词表扩充(当前只做 4 字整体保护 + 内置常用表)
- [ ] **P2** `rs_sync` 增加「卡片 ↔ 动画卡时间窗」重叠检查(BACKLOG 原条目的自动化版)
- [ ] **P2** 粗剪 retake 检测的相似度阈值按素材类型分档(短剧 vs 口播)

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
- [ ] **IDEA** 卡拉OK 式逐字字幕(需字级时间戳,当前 FunASR 只有句级;可由 Agent 按字数插值)

## 已完成

- [x] iter-03:P1 转场吞时长的音画漂移风险 → schema 写明语义约定 + rs_render 渲染前警告;clip.fade 字段入 schema

- [x] iter-02:修复 rs_jy_draft 时间单位错误(ms 误作 μs,历史草稿需重新生成);转场+音频淡入淡出实测落盘正确(17.5s/叠化 500ms 挂前段)

- [x] iter-01:P0 install.ps1 PowerShell 5.1 解析炸裂(无 BOM UTF-8 + 中文)→ 改 ASCII 版并实测通过;
- [x] iter-01:P1 渲染器补齐 IR 承诺的 transition(xfade 链 + acrossfade,17.53s→17.03s 实测消耗正确);
- [x] iter-01:P2 config.example.json 补 momentshift_dir 键。

## 已知非阻塞瑕疵(judge 备注)

- 画中画面板顶部带源素材灰色标题条(用户素材固有,非管线问题)
- 滑入动画方向无法从静帧核验(终帧位置已验证合规)
