# tests/

CutFlow 的测试目录。

## 结构

- `test_*.py` — 各版本回归测试,**参与门禁**(pytest)。
- `check_manual_cmds.py` — 手册命令 ↔ argparse 机械对拍门禁(可独立运行,也被 test_v19 引用)。
- `make_fixtures.py` / `fixtures/` — 测试夹具的生成与实体。
- `probes/` — **调试用探针脚本,不参与门禁**。12 个一次性诊断/探查脚本
  (ASR 探查、对齐基线、卡片检查、SAPI 视图等),仅在排查具体问题时手工运行,
  不进入 CI,也不作为任何验收依据(v0.15 起从 tests/ 根目录归置至此)。

## 文档/结构门禁(第一册新增)

| 文件 | 把守什么 |
|---|---|
| `test_context_budget.py` | 行数预算:SKILL.md ≤200、分册 ≤150(册头「超限理由」可超)、SKILL+README ≤470;AGENTS/CLAUDE 不重复;project-layout.md ↔ rs_paths.STAGE_DIRS 对拍;ir-sample.json 过 rs_ir validate |
| `test_doc_single_source.py` | 数值单一化:五类口径(-14/0.83/maxChars/40ms/150·300)白名单外出现即红;SKILL 引用的 rs_* 全在能力目录 |
| `test_rules_no_dup_paragraphs.py` | 分册间重复段落(相似度 >0.8 且 >3 行)即红 |
| `test_backlog_consistency.py` | BACKLOG.md 同 ID「已清 vs 仍开」结论冲突即红 |

## 运行

```bash
python -m pytest tests -q          # 只跑门禁测试(test_*.py)
python tests/probes/<script>.py    # 手工调试,按各脚本头部说明传参
```

