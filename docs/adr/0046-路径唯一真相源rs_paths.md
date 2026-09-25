# ADR-0046: 路径唯一真相源 `rs_paths.py`，禁止目录字面量

日期: 2026-09-25 · 状态: 已采纳（待落地） · 关联: ADR-0019（`RATIOS` 唯一真相源体例）、ADR-0045

## 背景

`rs_common.RATIOS` 已经是「比例 → 宽高」的唯一真相源并被 Hard Rule 21 保护。**阶段目录却没有同类机制**，导致 §1.4a 的 60+ 处散落。

## 决策

1. 新增 `skills/cutflow/scripts/rs_paths.py`，提供：
   - `STAGE_DIRS: dict[str, str]` —— 逻辑键 → 中文目录名（唯一真相源）。
   - `LEGACY_ALIASES: dict[str, str]` —— 旧英文名 → 逻辑键（迁移与过渡期读取用）。
   - `p(key) -> str` / `root(project) -> Path` / `pipeline_json(project)` 等**访问函数**。
   - `READONLY` / `NEVER_CLEAN` 等语义常量。
2. **全仓禁止再出现阶段目录字符串字面量**；新能力只能通过 `rs_paths` 取路径。
3. 过渡期提供 `resolve(project, key)`：优先新名，**新名不存在而旧名存在时返回旧名并 WARN**（保证旧工程可直接跑，不必立刻迁移）。

## 取舍

- 付出：一次性改造 15 个脚本；多一层间接。
- 换来：日后改目录名 = 改**一个常量**；路径相关 bug 面收敛；文档与代码可机械对拍。

## 被否决的替代

- **在 `rs_common` 里加常量**：否决——`rs_common` 已有 CLI 协议／配置／ffmpeg 等职责，路径独立成模块更清爽，且便于门禁只白名单一个文件。
- **用环境变量注入路径**：否决——不可审计、不可复现，与「给定输入必得同一输出」冲突。

## 影响

- `capabilities.json` 新增 `rs_paths.py` 条目（作为库工具，标注 `library: true`，不占命令配额）。
- 迁移工具、`rs_cleanup`、`rebuild.py` 模板（`ROOT = parents[depth]` 逻辑）全部改走 `rs_paths`。

## 四问

| 问 | 答 |
|---|---|
| 旧工程 | `resolve()` 自动兼容读；跑一次 `migrate_paths.py` 即转正。 |
| 门禁 | `tests/test_paths_gate.py` 的字面量扫描（见 ADR-0045）+ `rs_paths.py check --root <工程>`。 |
| 验收 | 全仓扫描 0 命中；`rs_run --status` 在新旧工程上都正确。 |
| 回滚 | 纯新增模块，回滚即删文件 + 还原字面量（由迁移脚本的 `--rollback` 一并处理）。 |
