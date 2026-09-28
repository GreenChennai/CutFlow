# 快照目录:12 个真实工程盘面的状态机结论固化(T5.3b)

每个 `<盘面名>.json` 固化一个构造工程在某一「视频类型 × 改动状态」组合下的:

- `status` —— `rs_run.py --status` 的完整载荷(逐阶段 status/staleReason);
- `explain` —— `rs_run.py --explain Sx` 的逐阶段载荷(status / verdict /
  verdictLabel / 逐组对账明细 compared;`contract` 块盘面无关,不入快照,
  由 `tests/test_stages_contract.py` 另行门禁);
- `rebuild` —— 由此推导的重建决策结论(级联起点 / 必须停 / 降级继续 / 一键入口)。

## 再生方式

```bash
python tests/test_snapshots.py
```

直接运行本文件(不经 pytest)即按 `tests/test_snapshots.py` 里的 `BOARDS`
矩阵重建 12 个工程、重新捕获结论、归一化后覆写本目录 JSON。

## 何时应更新(以及何时不该)

- **应该**:有意修改了 rs_run 状态机 / verdict 映射 / 阶段注册表 /
  `templates/stages.json` 契约,或调整了 `BOARDS` 盘面构造 —— 先审查 12 份
  diff **确认语义变化符合预期**,再再生;
- **不该**:与状态机无关的改动导致快照大面积变化 —— 那说明归一化失效
  (新引入了时间戳/绝对路径/环境量进载荷)或盘面构造被意外污染,先修根因;
- 快照断言「结论」而非「字节」:hash 一律归一化为 `<hash>`,工程根路径
  归一化为 `<ROOT>`,路径分隔符统一为 `/`。

## 盘面矩阵(视频类型 × 改动状态)

| 盘面 | 类型 | 状态 |
|---|---|---|
| 01-koubo-clean | 口播 | 干净盘面(账实相符 12/12) |
| 02-koubo-stale-subtitle | 口播 | 单阶段:手改字幕产物(带外改写) |
| 03-koubo-stale-ir | 口播 | 多阶段:带外改 IR 级联打脏 |
| 04-koubo-fresh-s3 | 口播 | 进行中新盘面(仅到 S3,S4/S5 声明内降级) |
| 05-hunjian-clean | 混剪 | 干净盘面(多素材) |
| 06-hunjian-stale-cutlist | 混剪 | 单阶段:改 cutlist(applied 未动,诚实口径) |
| 07-hunjian-multistale | 混剪 | 多阶段:外删产物 + 外改产物 |
| 08-hunjian-stale-script | 混剪 | 单阶段:工具升级(脚本 hash 变化) |
| 09-vlog-clean-degrade | vlog | 干净盘面 + S5 无变体声明降级 |
| 10-vlog-stale-brief-param | vlog | 改 brief 参数行(单文件多阶段 stale) |
| 11-jiaocheng-clean | 教程 | 干净盘面(卡片计划 + 术语表) |
| 12-jiaocheng-missing-stop | 教程 | 产物缺失 → 必须停(无降级通道) |
