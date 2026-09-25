# BASELINE v0.19 · 多风格迭代基线报告

> 日期：2026-09-25 · 依据：《CutFlow-多风格迭代方案-v1.md》（桌面，2183 行）
> 本报告是 v0.19 迭代（M0–M10）的冻结基线。迭代期间所有里程碑验收均以此为对照。

## 一、代码基线

| 仓库 | HEAD | 版本 | 工作区状态 |
|---|---|---|---|
| CutFlow | `466a8bc6dfcad1e2a0776d7a49e310c071e7a4e2` | v0.15.2-7-g466a8bc（fix(test): test_v15 的 ASR 守卫升级为真就绪探针） | 干净（仅 .claude/、AGENTS.md、CLAUDE.md 未跟踪） |
| cutforge | `73c32d8f1ab57264c580171939a7ae55e34925d2` | v0.4.0（chore(release): v0.4.0——阶段二编辑器闭环） | 干净（同上） |

## 二、测试基线（实测 2026-09-25）

| 项 | 结果 |
|---|---|
| CutFlow `python -m pytest tests -q` | **457 passed**（413.72s，本地全量） |
| cutforge `cargo test --workspace` | **exit 0 全绿** |
| artboard | 本轮只读引用，不做改动（方案 §A.4） |

## 三、能力基线（审查口径，方案 §1.2）

- videoType 5 键：talking-head / talking-head+animation / pure-animation / vlog / 混剪
- registry.json 7 条 entries；节奏 4 档；平台 4 预设；画幅 3 种
- 粗剪 reason 封闭枚举 9 项；guard 四项永不放松；响度 −14 LUFS ±1
- 31 个官方 rs_* 子命令 / capabilities.json 30 工具条目（自动生成，禁手改）
- vlog / 混剪：注册表与分册层已实现，**引擎层零实现**（方案 §1.3 核心发现）

## 四、结构性欠账基线（方案 §1.4 / §1.7）

- 阶段目录名（00_brief 等 8 个）以字面量散落 15 个脚本 60+ 处，无唯一真相源
- `ensure_workdir` 仍创建已废弃的 `04_ai_prompts` 与 `02_sensed/frames`
- 交付物与中间件在 06_output 顶层混放，无物理交付区
- 欠账台账 18 条（处置见方案 §1.7：12 清 / 4 顺延 / 2 关门）

## 五、迭代后回归判据

任一里程碑结束时必须满足：

1. CutFlow pytest 全绿（允许用例数增加，不允许既有用例转红）；
2. cutforge `cargo test --workspace` 全绿；
3. 四桥冒烟（弱依赖）不转红；
4. `rs_doctor --report` 基础档可用，零可选件环境非 fatal。
