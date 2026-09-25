# 双后端能力对齐矩阵 — CutFlow FFmpeg 管线 vs cutforge-render

> **清账**:本文件清「双后端能力对齐矩阵」IDEA 欠账(M7 验收线之一,方案 §7.3)。
> **口径**:CutFlow 侧逐行对照 `skills/cutflow/scripts/rs_render.py` 实码与既有对拍记录写判;
> cutforge 侧**不自行判**,对齐其仓库唯一真相源 `cutforge/docs/capability-matrix.json`
> (V2 · M11 版,2026-09-19;status 只认实码 + 对拍夹具,见 cutforge ADR-0003)。
> **金标准**(ADR-0037):成片渲染以 CutFlow 的 FFmpeg 管线为 Golden 基准,
> cutforge-render 为第二后端;两后端消费**同一份 IR 工程**(ADR-0038 能力对等边界)。

## 矩阵(九能力行 × 两后端)

| # | 能力 | CutFlow FFmpeg 管线 | cutforge-render | 说明与证据 |
|---|---|---|---|---|
| 1 | 转场 | **支持** | **支持** | FFmpeg:xfade 链 + 段尾帧扩展,offset=后段名义起点 → 时间零漂移(v0.10 ADR-0023;三级语法 ADR-0026,`rs_render.step_concat`)。cutforge:M11 parity xfade 链夹具(尾帧扩展零漂移,4.000s 无吞切)。 |
| 2 | 字幕烧录 | **支持** | **支持** | FFmpeg:ASS `subtitles` 滤镜**最后叠**(SKILL 铁律),词级 wordline 时间轴(ADR-0011/0020),三画幅预设(`rs_subtitle.STYLES`,ADR-0019)。cutforge:ASS 烧录链(M6 字幕对拍:字幕↔wordline↔成片偏移中位 0.0ms ≤40ms)。 |
| 3 | BGM 混音 | **支持** | **支持** | FFmpeg:`amix` 总线 + **sidechaincompress ducking**(全人声先合总线再 asplit 出侧链与混音两路——修过 `MIX_FAIL` 流说明符坑,`rs_render.step_mix`);BGM gainDb;片尾保底自然底噪(ADR-0043)。cutforge:M11 ducking 夹具(sidechain on/off 能量差可测)。 |
| 4 | 音效(落点/混入) | **支持** | **支持** | FFmpeg:`rs_sfx` 自动落点(转场/强调/列举/章节/结尾,密度门禁每 15s ≤2)+ **先 atrim 后 adelay** 正确时序(v0.11 修 0 秒炸响)。cutforge:M8 P0 修复 adelay 落点 + render_matrix_fixture 起播断言。 |
| 5 | 画幅变体 | **支持** | **支持** | FFmpeg:9x16 / 3x4 / 16x9 单一真相源 `rs_common.RATIOS`(OPTIMIZATION-v7 #4),渲染按 canvas。cutforge:M8 P0 修复缓存键并入 canvas+fps(修复前跨画幅缓存污染),render_matrix_fixture 分辨率断言。 |
| 6 | 卡片 overlay(artboard 卡) | **支持** | **支持** | FFmpeg:`rs_render.step_compose` overlay 轨,`clip.overlay` 绝对像素/归一化定位(ADR-0025),slide 线性滑入;上游闭环 = gen-cards/gen-frames → export → `--apply` 按**探测时长**挂轨 + `usedIn`/`manualEdit` 护栏(ADR-0017,M7 起含六类场景卡)。cutforge:M11 overlay 夹具(绝对像素 + 时间窗);卡片时长以 ffprobe 探测为准(同 IR 语义)。 |
| 7 | 冻结帧补长 | **支持** | **支持** | FFmpeg:`freezeMs` + `tpad=stop_mode=clone`,**-t 只读到冻结起点(输入侧)**(B8 教训:输出侧 -t 会把补帧整段截掉);纯动画必需(ADR-0027)。cutforge:tpad clone 尾帧(render/lib.rs segment 步)。 |
| 8 | 变速 0.25–4x | **部分** | **支持** | FFmpeg:IR 契约有 `speed`,段提取按 `take_s = 时长/speed` 多取/少取源料(`rs_render.step_segment`);但段内**无 setpts/atempo 重定时**,变速≠1 的输出时长语义未建、无对拍夹具(默认 speed=1 不受影响;剪映 5.9 出口 `rs_jy_draft` 原生透传 speed,不受影响)。cutforge:M11 parity 变速夹具(setpts + atempo 链,单实例 0.5–2.0 链式分解到 0.25–4)。 |
| 9 | 响度 | **支持** | **支持** | FFmpeg:总线 loudnorm **-14 LUFS** / 人声 -16(EBU R128;final 档双 pass,先测量后回填,`rs_render.step_loudness_measure`/`step_encode`),TP -1.0,段间 8ms afade。cutforge:M6 响度对拍:总线 -14.02 vs Golden -14.00 LUFS(差 0.02 LU ≤0.5)。 |

## 两行边界备注

- **裁剪/排序、位置缩放(punch-in)**:两后端均支持(FFmpeg:`punchIn` factor 中心裁切 + anchorY 纵向偏置,v0.11 R3;cutforge:M11 punch-in 夹具),不在本表九行内,注记备查。
- **变速是唯一的两后端差口**:FFmpeg 管线判「部分」是**诚实口径**——契约字段在、取料逻辑在,但缺重定时滤镜与对拍证据;补齐路径 = `rs_render` 段内加 `setpts=PTS*/speed` + `atempo`(音频),并补与 cutforge 的时长保持语义对拍(建议挂 M8 手法库或 M10 收敛账)。

## 维护纪律

1. 本表是**消费视图**:cutforge 侧行值以 `cutforge/docs/capability-matrix.json` 再生成为准,
   那边改版后本表必须同步(改版日期写进上表引言)。
2. CutFlow 侧行值只认 `rs_render.py` 实码与 `tests/` 对拍证据;新增能力先改实码+夹具,再改本表。
3. 状态四值:支持 / 部分 / 不支持 / 不适用;判「支持」必须能指出证据位置。
