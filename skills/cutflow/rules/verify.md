# Verify — 分级自检与验收

> **ADR-0016**。一句话:**首次全检;之后只跑机器能判定的部分;要不要亲眼验收,你说了算。**

## 1. 为什么分级

第一次做片子,任何环节都可能是错的,值得逐项过一遍。但**改一个字幕不该等于重跑验收**——那既慢又费 Token,而且用户本来就要亲眼看一遍成品。所以把"检查"拆成三层,各归其主:

| 级别 | 名称 | 谁执行 | 成本 | 何时跑 | 判据 |
|---|---|---|---|---|---|
| **L0** | 机械自检 | 纯脚本 | 秒级 | **每一次**产出后 | 见 §2 |
| **L1** | 语义自检 | Agent(需看图) | 分钟级 + Token | **首次**;以及画面构图变更时 | 见 §4 |
| **L2** | 人工验收 | **用户** | 人的时间 | **由用户显式触发** | — |

**归属铁律**:L1 的**触发权**在首次,L2 的**所有权**在用户。Agent **不得主动**发起 L1,除非用户说"帮我检查一下"。

## 2. L0 判据(纯脚本,`rs_verify.py`)

| 检查 | 通过线 |
|---|---|
| IR 可解析且校验通过 | `rs_ir.validate` 无错误 |
| Wordline 存在且单调 | `startMs` 单调不减、无 `endMs ≤ startMs` |
| 粗剪 guard 全过 | 所有 `action=remove` 的刀 guard.ok = true;keep 完整覆盖源时长 |
| 字幕合规 | 每卡字数 ≤ 上限、CPS ≤ 9、单卡时长 ≤ 7s、卡间不重叠 |
| 字幕↔Wordline 对齐 | 偏移中位数 ≤ 40ms,95 分位 ≤ 80ms |
| 产物存在 | 至少一个成片 |

```powershell
python skills/cutflow/scripts/rs_verify.py <工程根>            # L0
python skills/cutflow/scripts/rs_verify.py <工程根> --level L1 # L0 + 待目测清单
python skills/cutflow/scripts/rs_verify.py <工程根> --status   # 只看状态
```

## 3. 状态文件 `_state/verify.json`

```json
{"version": 1,
 "firstCheck": {"done": true, "at": "2026-09-10T21:30:00+08:00", "level": "L1", "result": "pass"},
 "lastL0": {"at": "...", "level": "L0", "result": "pass"},
 "userReview": null}
```

判定:

```
needL0 = 每次产出后 —— 永远为真
needL1 = !firstCheck.done                // 首次
      || S3/S4/S5 任一被重跑             // 画面构图变了
needL2 = 用户显式要求
```

**"画面变了"不靠 Agent 猜**:`rs_run --verify` 比对阶段状态,`S3/S4/S5` 任一非 done 即判定画面变更。

## 4. L1 怎么做(脚本只出清单,判定权在 Agent)

`rs_verify.py --level L1` **不自动判定**,它产出:

- 待目测清单(黑帧/绿幕残留/字幕压脸/卡片安全区/Logo 压字幕/转场跳变);
- `rs_bench` 抽帧命令(逐成片)。

Agent 看到清单后:跑抽帧命令 → 自己看图 → 给出结论。**脚本不替人做审美判断。**

## 5. 输出契约(硬规则)

任何一次交付输出**必须**携带:

| 字段 | 含义 |
|---|---|
| `verifyLevel` | 本次实际到哪一级(`L0` / `L1`) |
| `firstCheckDone` | 首次全检是否已完成 |

> **缺失即视为未验证,不得宣称"完成"。**

这条是为了防一个具体的坏结果:「之后不用检查了」被实现成「之后什么都不检查」,而界面还显示一个漂亮的 ✓。

## 6. 工作流

```
首次产出:
  rs_run → 跑完全部阶段 → rs_verify(L1)
    → 有失败? 修 → 重跑 → 再验   ← 这个循环只在首次存在,直到通过
    → 通过 → rs_verify --mark-first --result pass

后续编辑:
  改字幕 → 06_output/rebuild.py → 只跑 L0(秒级)
    → 输出里写明 "verifyLevel: L0"
    → 用户自己看一遍(不需要 Agent 参与)

用户要求检查:
  "帮我检查一下" → rs_run --verify --verify-full → L1 清单 + 抽帧 → Agent 看图给结论
```

## 7. 反模式

- ❌ 静默跳过检查还报"✓ 完成" —— 输出不带 `verifyLevel` 就是未验证;
- ❌ Agent 主动跑 L1(用户没要求) —— 费时间费 Token,而且用户的验收权被剥夺;
- ❌ 把 L0 的"通过"说成"成片没问题" —— L0 只保证机械正确性,不保证好看;
- ❌ 画面没变却强迫用户重看 —— 只重跑 L0 即可。
