# ADR-0048: 声明式编辑层 `rs_edit` —— 自然语言改片的确定性通道

日期: 2026-09-25 · 状态: 已采纳（待落地） · 关联: ADR-0033（文件为真相源 + OpLog）、ADR-0041（禁止现写脚本）、ADR-0040（意图编译，本 ADR 为其对称物）

## 背景

用户要「用自然语言快速指导 Agent 修改视频」。现状只有三条路，全都不可接受：

1. **手改 JSON**（`project.json` / `cutlist.json`）——曾动坏契约（P3 教训，ADR-0041 背景段有记录）。
2. **重跑阶段**——改一个字幕也要跑 S7→S8，昂贵且不精确。
3. **进 CutForge 编辑器**——人有界面，但 Agent 没有通道，人机不同线。

而 cutforge 已提供**正确的语义模型**：`ClipPatch`（声明式变更）+ 撤销栈 + OpLog（`baseRev` + `actor`）+ 三路合并 + 冲突码 CF-001~CF-006。缺的是**面向自然语言的、可被 Agent 消费的编译层与投影层**。

## 决策

1. **新增 `rs_edit.py`**，作为 IR 的唯一声明式编辑入口。三层职责：

| 层 | 子命令 | 说明 |
|---|---|---|
| **投影**（给 Agent 读） | `rs_edit.py context <工程> [--json] [--scope clip\|project\|subtitle\|audio] [--budget 12KB]` | 产出**结构化时间线视图**：可寻址对象清单（稳定 `clipId` + 文本 + 时间窗 + 当前参数 + 可改字段白名单）。**不含视频帧。** |
| **校验** | `rs_edit.py ops-validate <ops.json>` | 校验 op 名、寻址、字段白名单、值域；**不落盘、不渲染**。 |
| **应用** | `rs_edit.py apply <工程> --ops ops.json [--dry-run] [--actor agent\|user]`<br>`rs_edit.py undo <工程> --last <n>`<br>`rs_edit.py diff <工程> --rev <a> --rev <b>` | 确定性 patch + OpLog 追加 + 脏传播 + 可撤销；`--dry-run` **打印人话差异表而不写盘**。 |

2. **EditOp 契约**（完整语法见附录 B）：

```python
class EditOp(TypedDict):
    op: str        # clip.trim | clip.move | clip.split | clip.delete | clip.speed
                   # clip.reframe | clip.motion | transition.set | overlay.add | overlay.remove
                   # sfx.add | sfx.remove | bgm.set | audio.gain | freeze.set
                   # subtitle.set | subtitle.retime | subtitle.highlight
                   # segment.protect | keyframe.set | output.set | note.add | style.pacing
                   # beat.snap
    target: str    # 稳定引用：clipId / trackId / anchor(kind+ref+tMs) / noteId
    before: dict   # 应用前快照（撤销用，apply 时由引擎回填）
    after: dict    # 目标态（只允许 schema 白名单字段）
    reason: str    # 意图摘要（人话，进 OpLog）
    source: str    # user | agent | inferred
```

3. **硬约束**：
   - **禁止数组下标寻址**。一律用稳定 `clipId` 或锚点 `{kind, ref, tMs}`（cutforge 已有 `cf-<sha1>` 内容寻址回退机制，桥已实现）。
   - **幂等**：同一 EditOp 重复 apply → `before == after` 短路，不升 rev、不产 Op（对齐 cutforge `Workspace::apply` 第 3 步）。
   - **确定性**：同一 `(工程 rev, ops 序列)` 必得**字节级同一 IR**。
   - **只写 schema 白名单字段**（五份 schema `additionalProperties:false`）；越界即 `BAD_FIELD` 退出码 2。
4. **与 cutforge 对齐**：`apply` 同时写 `.cutforge/oplog/<日>.jsonl`（`baseRev` + `actor`）；由 `rs_oplog.py` 审计；人与 AI 的改动在**同一条 OpLog** 上，`actor` 区分。冲突时**显式报错停止写入**（CF-001~006），禁止「最后写入者获胜」。
5. **脏传播联动**：`apply` 后按 cutforge `stage.rs` 的脏传播语义标脏，并输出**最小重建建议**（如「仅字幕受影响 → `06_成片输出/rebuild.py`」）。

## 取舍

- 付出：新契约层（EditOp 语法、寻址、冲突语义）；需与 cutforge schema 对齐。
- 换来：改片**毫秒级、可审计、可撤销、可增量**；Agent 不必抽帧看视频（**性能的关键**）；人机同一条 OpLog。

## 被否决的替代

- **直接让 Agent 调 cutforge 的 38 个 MCP 工具**：否决为**唯一**面——受 cutforge 能力边界限制（关键词／蒙版零实现、旋转无契约字段），且 CutFlow 侧的字幕／artboard／品牌等能力无对应工具。**保留为可选执行后端**（能力允许时透传）。
- **生成 FFmpeg 命令**：否决——不可审计、不可撤销、无法增量，等于把债埋进提示词。
- **在 `project.json` 上做 JSON Patch（RFC 6902）**：否决——路径依赖数组下标，天然不稳定；且无法表达「语义意图」与「守卫」。

## 影响

- `project.schema.json` 需确认**已覆盖**全部 EditOp 目标字段；缺的字段（如 `speed` / `keyframe`）**要么走 cutforge 已支持契约，要么明确不承诺**（见 §9 未核实项）。
- 新增 `rules/edit-op.md`（EditOp 语法手册）；`SKILL.md` §1.1 加一行「改片 → `rs_edit`」。
- `rs_run --status` 增加 `editOps` 来源标注。

## 四问

| 问 | 答 |
|---|---|
| 旧工程 | 纯新增子命令，不改 IR 契约，旧工程可直接用；`context` 对无 `clipId` 的旧 IR 走内容寻址回退（桥已有）。 |
| 门禁 | ①`tests/test_edit_op.py`：**幂等**（同 op 两次 → rev 不变）、**确定性**（同 ops 序列 → 字节级同 IR）、**寻址**（下标寻址必须报错）、**白名单**（越界字段报错）；②`rs_caps.py check` 收录 `rs_edit` 全部子命令；③手册命令对拍。 |
| 验收 | §2.3-B：一句自然语言 → `context` → `ops.json` → `apply --dry-run` 出人话差异 → `apply` → `undo` 复原；**全程零渲染**。 |
| 回滚 | `rs_edit undo --last N`（OpLog 逆写回 `before`）；`rs_run --rollback` 兜底到阶段备份。 |
