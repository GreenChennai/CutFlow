# tests/

CutFlow 的测试目录。

## 结构

- `test_*.py` — 各版本回归测试,**参与门禁**(pytest)。
- `make_fixtures.py` / `fixtures/` — 测试夹具的生成与实体。
- `probes/` — **调试用探针脚本,不参与门禁**。12 个一次性诊断/探查脚本
  (ASR 探查、对齐基线、卡片检查、SAPI 视图等),仅在排查具体问题时手工运行,
  不进入 CI,也不作为任何验收依据(v0.15 起从 tests/ 根目录归置至此)。

## 运行

```bash
python -m pytest tests -q          # 只跑门禁测试(test_*.py)
python tests/probes/<script>.py    # 手工调试,按各脚本头部说明传参
```
