# CutFlow 迭代 v0.10 — 交接文档（Handoff）

> 接手人：下一个 Agent。本文档描述**任务目标**、**已完成改动（含实证与测试）**、**待完成项（可执行命令）**、**关键坑**。
> 仓库真身：`E:\平日资料\GitHub\CutFlow`（技能目录 `skills/cutflow/`，工具 `tools/`，测试 `tests/`，文档 `docs/`）。
> 旧工程样本：`E:\平日资料\20260913-店群拆分收入-纯口播`（用户实测反馈来源，需重渲验收）。

---

## 0. 关键环境约定（先读，避免踩坑）

- **`C:\Users\Velon\.agents\skills\cutflow` 是到 `E:\平日资料\GitHub\CutFlow\skills\cutflow` 的 junction（目录联接）**，不是副本。所有编辑只有单一落点，改一处即全生效，无需同步两份。
- **`search_content` 对中文路径会失效**（ripgrep 在 CJK 路径上静默空结果）。诊断中文目录内容请用 PowerShell `Select-String -Path <绝对路径> -Pattern ...`，或先用 `Get-ChildItem` 拿绝对路径。
- **ffmpeg 可用滤镜**：`colorkey`、`chromakey`、`despill`、`gblur`、`alphaextract`、`morpho`（形态学）、`xfade`、`acrossfade`、`geq`、`scale`、`format`、`fade` 均可用。不要改用不存在的滤镜。
- **Python 双版本**：系统 `python` 是 3.14（无 torch 轮子）；ASR 专用 venv 在 `tools/.venv-asr`（CPython 3.12，torch 2.14 cpu 已装）。`fun_asr.py` 默认走 `.venv-asr`；仓库主脚本/测试跑在系统 python。
- **ASR 模型已预暖完成**：`fun_asr.py` 的 `pkg` 后端首次会触发 `ensure_backend()` 自动从 ModelScope 下载 ~1GB torch 权重（一次性，落 `~/.cache/modelscope`）。已用样本跑通，`charTimestamps: true`。**不要**再让用户手装或“启动 ASR 服务”——那是已废弃的 server 时代话术。

---

## 1. 任务目标（用户四项反馈）

来源工程：`E:\平日资料\20260913-店群拆分收入-纯口播`，用户对 v0.9 成片 `06_output/final_店群拆分收入_916.mp4` 提出：

1. **绿幕边缘差**：人物边缘锯齿严重、有黑边、左下角残留闪烁黑影。
2. **段间衔接闪烁**：任务切换时人物瞬间消失再显示（很快但明显）。
3. **ASR 经常“没启动”**：要求**内置 FunASR**（自带运行器，不依赖外部软件/在线 API）。
4. **品牌 Logo 功能**：识别用户提供的 Logo 真实尺寸（含透明通道/内容包围盒），按排版规则放到四角/顶部/底部等位置。

---

## 2. 已完成改动（代码已落地，`tests/test_v10.py` 22 项全过）

### T1 — 绿幕边缘精修（ADR-0022）
- 文件：`skills/cutflow/scripts/rs_render.py` 函数 `_chroma_fg_chain()`。
- 旧实现：仅 `edgeBlur` 羽化（0.8px），无腐蚀，黑边/锯齿原样保留；`killRects` 硬矩形连人体一起抹（矩形边界随人物动作穿帮 → 店群工程“左下角闪烁黑影”根因）。
- 新实现：`alpha 大 sigma 模糊(gblur) → 偏高频阈值重新硬化(clip((alpha-t)/(1-t)))` 实现腐蚀收缩 → 小 sigma `edgeFeather` 细羽化；`killRects` 默认 `killRectMode=green`（框内**只清绿幕主导像素** `lt(cb,116)*lt(cr,116)`，保护入区人体），`all` 保留旧硬清。
- 参数统一：新增模块常量 `CHROMA_DEFAULTS = {"similarity": 0.15, "blend": 0.12}`，基轨/overlay/schema 三处引用同一真相源。
- overlay 层（compose）现在也吃同一套边缘精修链。

### T2 — 段间衔接 / 交叉溶解（ADR-0023，反馈#2 核心）
- 文件：`rs_render.py` 的 `step_segment()`、`step_concat()`、新增 `_resolve_transitions()`。
- 根因：旧 `_effective_transitions` 在转场 `durMs<1帧` 时**整链弃用转场 → 退回 `-c copy` 硬切**，段边界跳变读感为“人物闪烁消失”。
- 新逻辑：
  - `0 < durMs < 1帧` 的转场**自动提升为 `joinCrossfadeMs`（默认 120ms）交叉溶解**（doc 级 `joinCrossfadeMs` 或 config.render 可覆；`0` 禁用提升）。
  - **尾帧扩展法**：`step_segment` 渲染段 i 时多取 `tails[i]`（= 它与下一段的转场时长）尾帧；`step_concat` 的 xfade `offset = 后段名义起点 sum(qdurs[:i])` → **成片时间与字幕时间零漂移**；末尾 `-t total_s` 裁齐（尾帧只进重叠不外溢）。
  - 帧量化：段边界 `sourceInMs`/`durationMs` 吸附到帧网格（`q_in_ms`/`q_out_ms`），消除半帧散差。
  - 保守回退不变：仅当某衔接点**源间隙放不下尾帧扩展**才整链弃用走 concat（`type=cut/none` 仍显式硬切）。
- `CACHE_VER` 由 `v2` → `v3`（语义变更，防旧缓存幽灵命中）。

### T3 — 内置 FunASR 收尾（ADR-0024，反馈#3 核心）
- **真凶 bug 已修**：`tools/fun_asr.py` 的 `ensure_backend()` 调用了**从未定义的 `any_backend_ready()`** → 每次转写必然 NameError 崩溃，上游只能报“ASR 无输出/未启动”。已补定义 `any_backend_ready()`（遍历 `BACKENDS` 任一就绪即 True），并删除其后一段死代码。
- `rs_doctor.py`：删除探测 `config.asr.url` 的 HTTP server 旧 `_check`（永远“不可达”，误导诊断），改为 `probe_asr_local()`（子进程跑 `fun_asr.py --probe`），hint 指向 `fun_asr.py --ensure` 自动部署。
- `rules/asr.md`：写死“未就绪=自动部署，禁止让用户手装/启动服务”；记录 pkg 后端首次 ModelScope 下载说明。
- `SKILL.md` 硬规则 22：同步上述转场与 ASR 行为描述。

### T4 — 品牌 Logo 真实尺寸（ADR-0025）
- 文件：`skills/cutflow/scripts/rs_brand.py`（已重写）、`rs_render.py` 的 `step_compose()`、`project.schema.json`、`rules/branding.md`。
- 旧坑：`rs_brand.variant_ir` 产 `clip.overlay={x,y,w,h}` 但 `rs_render` 不消费 → Logo 会以 scale 默认值**贴满画布**（潜伏致命）。
- 新能力：
  - `probe_logo()`：读像素宽高 + alpha **内容包围盒**（透明 padding 不算尺寸）。
  - `--analyze <logo.png>` 子命令：输出真实尺寸诊断。
  - `logo_rect()`：按真实宽高比缩放（`scale` 默认占画幅宽 0.12，高度上限 8% 画高，超限按高等比缩宽）；锚点扩为 6 位：`topLeft/topRight/bottomLeft/bottomRight/topCenter/bottomCenter`；bottom 系自动抬升到字幕带上缘（`lifted=true` 留痕）。
  - `variant_ir()`：产出变体轨 clip 带 `overlay={x,y,w,h,opacity}`（绝对像素落点）。
  - `rs_render.step_compose`：消费 `overlay` 绝对定位；支持 `opacity<1`（`colorchannelmixer=aa=`）。

### 测试
- `tests/test_v10.py` 已存在并覆盖 ADR-0022/0023/0024/0025 全部公开 seam，**当前 22 项全过**（运行：`python -m pytest tests/test_v10.py -q`）。

---

## 3. 待完成项（按优先级）

### ⚠️ P0 — 整片实机验收（T6，最关键，尚未做）
代码已改，但**尚未用新管线重渲店群工程并目检**。这是“用户反馈是否已解决”的最终证据。

1. 重渲（会全量从 S0 级联，上游命中缓存很快；`CACHE_VER=v3` 已强制换键）：
   ```powershell
   cd "E:\平日资料\20260913-店群拆分收入-纯口播"
   python rebuild.py
   ```
   或直接 `python "E:\平日资料\GitHub\CutFlow\skills\cutflow\scripts\rs_run.py" --root "E:\平日资料\20260913-店群拆分收入-纯口播" --from S0 --force`。
2. 抽帧目检（参考诊断期脚本风格，用 ffmpeg `-vf fps=` / `-ss -frames:v 1`）：
   - **衔接点**（成片约 4.1s / 15.55s 等原两处硬切点）：逐帧确认**无人物整帧消失、无闪黑**；交叉溶解应平滑过渡。
   - **边缘**：放大人物发丝区，确认**无黑边、无锯齿**。
   - **左下角**：确认绿幕投影残留闪烁黑影被 `green-only killRect` 清除且不伤人体。
3. 若店群工程 IR 自带 `brand`/Logo 轨，顺带验证 Logo 真实比例+落点；若没有，见 P1。
4. 跑 `rs_sync` 对账：中位偏移 ≤40ms，成片时长与 IR `finalDurationMs` 差 ≤1 帧。

### P1 — Logo 端到端验证（T4 验收，反馈#4）
- 需一个带 brand logo 的工程验证。可临时给店群工程 IR 加 `brand.logos` + 变体轨，或新建最小工程。验证：`rs_brand.py --analyze <logo>` 输出透明 bbox 正确；`06_output` 变体成片中 Logo 真实比例、落点符合安全区、不压字幕带。
- 若店群工程本身不验证 Logo，**至少**确认 `test_v10.py` 的 `test_probe_logo_alpha_bbox` 这条 ffmpeg 实机用例通过（它已被 `@skipif` 保护，需 ffmpeg 可用）。

### P2 — 文档补完（T7）
- **写 ADR-0022 / 0023 / 0024 / 0025**（`docs/adr/` 现有编号到 0021，四个 ADR 均未写）。每篇按现有 ADR 体例：背景、决策、影响、回滚。参考本文档第 2 节的“根因→对策”即可落笔。
- **更新 `docs/ITERATION-v0.10-PLAN.md` 末尾状态勾选**：T1/T2/T3/T4 应全打勾，仅“整片验收 / 测试+ADR+CHANGELOG”待办。
- **`CHANGELOG.md` 尚不存在**，在仓库根创建并记录 v0.10 四项改动。
- **`CONTEXT.md`**（`docs/` 下已存在）补充/确认术语：边缘精修、尾帧扩展、joinCrossfadeMs、green-only killRect、Logo 真实尺寸包围盒。

### P3 — 清理
- 删除 `E:\平日资料\GitHub\CutFlow\tmp_flicker/`（上一会话的抠像对照实验残留，7 版 key_v* 等，已无引用）。
- 删除 `E:\平日资料\GitHub\CutFlow\tmp_prewarm/`（本次预暖的样本 wav + 日志；torch 权重已落 `~/.cache/modelscope`，删此目录不影响 ASR）。
- 保留 `tests/__pycache__/` 无妨（或一起清）。

---

## 4. 验收线（完成定义）
- 衔接点逐帧无人物消失/无闪黑；rs_sync 中位偏移 ≤40ms；成片时长与 IR `finalDurationMs` 差 ≤1 帧。
- 发丝边缘无黑边无锯齿；左下角无闪烁残影。
- `rs_align build --media`（或等价 ASR 流程）一条命令出字级 wordline，无任何手工部署步骤。
- Logo 变体成片：真实比例、落点符合安全区、不压字幕带。
- `python -m pytest tests/test_v10.py -q` 全绿（已满足）；四个 ADR 文档 + CHANGELOG 就位；tmp 清理完毕。

---

## 5. 不该动 / 已知边界
- 不要回退 `CACHE_VER=v3` 到旧值（会导致旧缓存被错误命中）。
- `similarity/blend` 默认已统一到 0.15/0.12；若某工程 IR 显式给了更小值（如店群原 blend=0.05），优先 IR，但评估时留意边缘过硬。
- `acrossfade` 用 `curve1=tri:curve2=tri`（三角形交叉淡入淡出），不要换成 exponential（低端噪声大）。
- 不要重新让 `rs_doctor` 探测 HTTP server —— 那是已废弃路径。
- ffmpeg `geq` 在 `yuva444p` 下操作：`alpha(X,Y)` 取 alpha；`cb/cr` 取色度，用于 green-only 判断。已在 `_chroma_fg_chain` 内正确接线。
