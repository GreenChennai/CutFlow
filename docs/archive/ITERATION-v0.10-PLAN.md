# 迭代方案 v0.10(接手 v0.9,用户实测四项反馈)

> 上一轮 `ITERATION-v0.9-PLAN.md` 完成度:T1 初版(edgeBlur/killRects)✅、T2 首帧 setpts 修复 ✅(v7 实测)、
> T3 fun_asr --ensure 自举 ✅、T4 未动。本轮针对用户对 `20260913-店群拆分收入-纯口播` 成片的四项反馈继续。

## 用户反馈 → 根因 → 对策

| # | 反馈 | 根因(已实证) | 对策 |
|---|---|---|---|
| 1 | 边缘锯齿+黑边+左下角闪烁黑影 | colorkey alpha 近二值,仅 0.8px 羽化,无腐蚀收缩;本工程 blend=0.05 过硬;killRect 硬矩形连人体一起抹,矩形边界穿帮 | **T1 边缘精修**:alpha 腐蚀(blur+重阈值)→ 细羽化;killRect 改 green-only(框内只清绿幕主导像素);统一默认 similarity .15 / blend .12 |
| 2 | 段间切换人物瞬间闪烁消失再显示 | 转场 durMs=8 <1帧 → B2 规则整链弃用 → 硬切跳变;人物位置瞬移+抠像 alpha 单帧 pop 读感为"闪烁" | **T2 交叉溶解**:0<tdur<1帧提升为 joinCrossfadeMs(默认120ms);**尾帧扩展法**保时间模型(见 ADR-0023);帧量化段边界 |
| 3 | ASR 经常"没有启动" | pkg 后端首次需从 ModelScope 拉 ~1GB torch 权重(本地模型是 ONNX 导出),拉取慢/失败即"未启动";rs_doctor 还在探测已废弃的 HTTP server,误导诊断 | **T3 自举收尾**:rs_doctor 改本地 probe;rules/asr.md 写死"未就绪=自动部署,禁止让用户手装";预暖 torch 权重;清理 ensure_backend 死代码 |
| 4 | Logo 品牌标识(真实大小+排版规则+四角/顶底) | rs_brand 的 variant_ir 产 clip.overlay{},rs_render 不消费 → Logo 会贴满画布(潜伏致命);probe_logo 从未被调用;logo_rect 假定方形;anchor 只有 5 位 | **T4 Logo 真实尺寸**:analyze 子命令(ffprobe+alpha 内容包围盒);logo_rect 按真实宽高比;anchor 扩 6 位;topCenter/bottomCenter;variant_ir 改产 scale/position/opacity;rs_render 支持 opacity |

## 实施顺序

1. **T2+T1**(`rs_render.py`、`templates/project.schema.json`):边缘精修链 + 帧量化 + 转场提升 + 尾帧扩展 xfade;CACHE_VER→v3。
2. **T3**(`rs_doctor.py`、`rules/asr.md`、`tools/fun_asr.py` 小修):doctor 本地 probe;ASR 预暖(pkg 权重落盘)。
3. **T4**(`rs_brand.py`、`rs_render.py` step_compose、schema、`rules/branding.md`):真实尺寸+排版+opacity 渲染。
4. **整片验收**:店群工程 `06_output/rebuild.py` 重渲 → 抽 7 衔接点+边缘+Logo 帧目测;rs_sync 对账。
5. **测试与文档**:tests/test_v10.py;ADR-0022/0023/0024/0025;CONTEXT.md 术语;CHANGELOG。

## 验收线

- 衔接点逐帧无人物消失/无闪黑;rs_sync 中位偏移 ≤40ms;成片时长与 IR finalDurationMs 差 ≤1 帧。
- 发丝边缘无黑边无锯齿;左下角无闪烁残影(绿幕投影被 green-only killRect 清除且不伤人体)。
- `rs_align build --media` 一条命令出字级 wordline(无需任何手工部署步骤)。
- Logo 变体成片:Logo 真实比例、落点符合安全区、不压字幕带。

## 状态记录

- [x] 方案成文(本文档)
- [x] T1 边缘精修(ADR-0022)
- [x] T2 交叉溶解(ADR-0023)
- [x] T3 ASR 自举收尾 + 预暖(ADR-0024)
- [x] T4 Logo 真实尺寸(ADR-0025)
- [x] 整片验收(v0.10.1:抓 5 真 Bug 已修——geq 字节域全透明/S1 manifest/S9 wordline 域/--video 契约/Logo padding;抽帧 14/14 人物在、溶解平滑、左下角干净;sync 全绿中位 18ms、时长差 1 帧、音频闸 0.973;rebuild.py 已 --init 补齐)
- [x] 测试(test_v10.py 22 项全绿)+ ADR×4 + CHANGELOG
