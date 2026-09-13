# ADR-0022:绿幕边缘精修(alpha 腐蚀收缩 + green-only killRect)

状态:已采纳(v0.10.1 修订:geq 字节域) ｜ 日期:2026-09-14(v0.10,用户反馈#1)

## 背景

店群工程实测:人物发丝边缘锯齿严重、有黑边、左下角残留闪烁黑影。v0.9 的抠像后处理只有 `edgeBlur` 羽化(0.8px):`colorkey` 产出的 alpha 近二值,锯齿与暗色残边(黑边)原样保留。同时 `killRects` 是**硬矩形全清**:框内 alpha 一律置 0,矩形边界随人物动作进出画面而穿帮 —— "左下角闪烁黑影"的根因正是矩形边界在人物移动时露出又被遮住。另有隐性坑:`similarity/blend` 默认值散落三处(基轨/overlay/schema),某处改动即互相打架;店群 IR 显式 `blend=0.05` 过硬,加剧边缘锯齿。

## 决策

1. **边缘精修链 = 腐蚀收缩 → 细羽化**(`_chroma_fg_chain()`):alpha 先大 sigma 高斯模糊(`gblur`,sigma=`edgeShrink` 默认 1.2),再用偏高频阈值重新硬化 `clip((alpha-t)/(1-t), 0, 1)`(`edgeShrinkT` 默认 0.55)—— 模糊把边界往内推,硬化恢复近二值但边界整体收缩(收缩量 ≈ sigma×系数),黑边/锯齿被裁掉;随后小 sigma `edgeFeather` 细羽化找回平滑过渡。滤镜序:`format=yuva444p → gblur → geq(重算 alpha) → gblur(羽化)`。
2. **killRect 默认 green-only**(`killRectMode=green`):框内只清**绿幕主导像素**,判据 `lt(cb,116)*lt(cr,116)`(despill 后残留绿 cb/cr 双低;纯绿 cb≈44/cr≈21,半中和 ≈86/75;中性灰/人体 cb≈cr≈128)—— 入区人体不被误清。`all` 保留 v0.9 硬清语义,供纯背景区使用。
3. **默认参数统一真相源**:模块常量 `CHROMA_DEFAULTS = {similarity: 0.15, blend: 0.12}`,基轨/overlay/schema 三处引用同一常量;IR 显式值仍优先(不破坏既有工程),但新工程默认获得更稳的边缘。
4. **overlay 层同链**:compose 合成的 Logo/卡片等前景同样吃 `_chroma_fg_chain()`,不再出现基轨精修而 overlay 粗糙的双标。

## 后果

- 发丝黑边/锯齿显著收敛;killRect 不再连人一起抹,矩形边界穿帮(闪烁黑影)消失;
- `geq` 全帧重算 alpha 有渲染成本(单段 +数秒级);`edgeShrink=0` 可跳过腐蚀只留羽化;
- 旧工程 IR 若显式给了过硬的 blend(如 0.05),优先 IR —— 评估边缘时优先调 IR 而非改默认值;
- green-only 判据阈值 116 是 despill 后色度分布的经验分界;极端偏色服装(青绿色系)理论上可能被误清,遇到时用 `killRectMode=all` + 手工缩小矩形绕开人体。

## 修订(v0.10.1,店群整片验收发现)

初版腐蚀表达式按 geq 归一化假设写成 `clip((alpha-t)/(1-t),0,1)`。整片验收时发现**全片人物消失**(成片只剩背景+字幕,音轨正常):geq 的 `alpha/cb/cr(X,Y)` 返回的是**原始 0-255 字节值**,表达式结果也按字节写入——(255−0.55)/0.45 ≫ 1 被 clip 成 1,恒输出字节 1,全帧透明。真凶不是滤镜选择而是**域约定**:修复后表达式换算到字节域 `clip((alpha−t×255)/((1−t)×255)×255,0,255)`(killRect 的 `lt(cb,116)` 恰好一直按字节域写,因祸得福没病)。教训固化为两条:

1. **geq 像素域 = 字节域**:凡在 geq 表达式里比较/计算像素值,一律以 0-255 为口径;参数域(如 edgeShrinkT∈[0,1])必须在生成表达式时换算。
2. **合成类特性必须有"渲染一条合成帧"的实机回归**:`test_v10.py::test_chroma_chain_edge_shrink_and_green_killrect` 只断言了滤镜字符串——字符串对、语义错,静默产出全透明成片;现补 `test_chroma_chain_preserves_opaque_subject`(真跑 ffmpeg:不透明人体保持不透明/矩形内绿幕清除/矩形外不动),此后一切滤镜链改动至少配一条像素级断言。

## 关联

- `rs_render.py`:`_chroma_fg_chain()`、`CHROMA_DEFAULTS`、`step_segment()` 基轨链、`step_compose()` overlay 链
- `templates/project.schema.json`:`edgeShrink`/`edgeShrinkT`/`edgeFeather`/`killRectMode` 字段
- `tests/test_v10.py`:边缘链断言;`CACHE_VER` v2→v3(渲染语义变更,防旧缓存幽灵命中)
- 用户反馈来源:`E:\平日资料\20260913-店群拆分收入-纯口播`
