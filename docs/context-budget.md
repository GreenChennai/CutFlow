# 上下文预算与度量(阶段五 T6)

> 目的:把"省 Token"从口号变成可记录的趋势。正确性不降是前提(L0 全绿、e2e 全绿),本文件只管"读得多不多"。

## 指标定义

一次典型任务记三个数(写入该工程 `_state/context_budget.jsonl`,每行一条):

| 字段 | 含义 |
|---|---|
| `task` | 任务标识(日期 + 一句话) |
| `files_read` | 读取的文件数(Read/检索命中后展开的原文均计) |
| `bytes_read` | 读入字节总数 |
| `wasted_reads` | 无效读取次数(读了但没用于结论/产物的文件) |
| `scripts_written` | Agent 现写的一次性脚本数(目标恒为 0,见 Hard Rule 25) |

## 查目录 → 检索 → 定点读(取代通读)

1. **查能力**:先读 `skills/cutflow/capabilities.json`(机器可读、自动生成、禁手改),不读源码;
2. **检索定位**:两仓已入 context-mode 知识库与 GitNexus 图谱,用 `ctx_search` / 图谱查询命中片段,不整份通读 Markdown;
3. **定点读**:只读命中章节与确需修改的文件。

## 基线(2026-09-20 迭代)

本轮为"治理机制建立前"的最后一批大规模通读式工作,记录为基线;其后任务按上表记入各工程 `_state/`,形成趋势对比:

| 波次 | 内容 | 子代理工具调用次数(代理) |
|---|---|---|
| 专项 07 | 断句/片尾 P26–P30 | 148 |
| 阶段二 | CutForge 编辑器闭环 | 210 |
| 阶段三 a/b | 状态机 + 退出码/契约/文档 | 374 |
| 阶段四 | 意图编译器 N1–N6 | 174 |
| 阶段五/六/附录 | 能力目录 + 剪映出口 + ADR 落地 | 199 |

- `scripts_written`:本轮 0(全部走官方 `rs_*` 子命令;`prune-ghost`/`gen-cards`/`add-overlay`/`export-fallback` 已升格)。
- 治理设施:`capabilities.json`(30 工具/51 命令,防漂移门禁在测)、`tests/check_manual_cmds.py`(手册 138 条对拍)、GitNexus 双仓图谱、context-mode 四源索引。
