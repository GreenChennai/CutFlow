# Pipeline State — 阶段状态机显式化(T2.11)

> **一句话**:盘面的每种状态都有**确定的三态结论** —— stale 必须重跑、stop 必须停、
> 声明内降级可继续;`rs_run.py --explain Sx` 是唯一判定口,判定依据对照
> `templates/stages.json`(T2.10 契约表,`tools/gen_stages.py` 机械抽取)。
> 本文只写规则;实现在 `rs_run.evaluate / explain_payload`,判据由
> `tests/test_pipeline_states.py`(8 盘面参数化)把守。

## 1. 状态全集与三态判定

`rs_run evaluate()` 产出的 `status ∈ {done, stale, missing, corrupt, failed, blocked}`
映射到三态(rules/incremental.md §2 的状态灯是同一套账):

| status | 灯 | 三态 verdict | 判定规则 |
|---|---|---|---|
| `done` | ✓ | **可继续(keep)** | 输入 hash + 参数快照(paramKeys)+ 脚本 hash + 外部版本 + 产物 outHash 全部对账一致;`--explain` 的 `data.compared` 逐组列出"为什么不算 stale" |
| `stale` | ⚠ | **必须重跑(rerun)** | 任一对账组出现 changed/added/removed,或产物被带外改写(outHash 不符);"为什么 stale"逐条给到具体文件/参数(`staleReason`) |
| `missing` | ✗ | **必须停(stop)** | 无状态账(从未跑 / 产物缺失);人工阶段无真实产物标记(如 S4 缺 `appliedAt`,P11-1)同判。**例外**:契约 `degradePolicy.action=skip` 且缺声明条件成立(S4 无 cards.json / S5 无 variants.json)→ `degrade-continue` |
| `corrupt` | ✗ | **必须停(stop)** | `_内部状态/Sx.json` 解析失败(P15-1)—— 坏账 ≠ 从未跑,先告警;按缺失重跑但绝不静默 |
| `failed` | ✗ | **必须停(stop)** | 上次执行失败(P25-1 落账),原因在 `staleReason`;重跑成功自然回 done |
| `blocked` | ⚠ | **必须停(stop)** | 上游处于 failed —— 不拿坏/缺的上游产物硬跑下游;先修上游 |

**禁止第三种结论**:任何盘面都必须落进上表;"拿不准"= 测试红,不许人肉临时解释
(参数化测试的 8 盘面每盘面一个确定结论,见 §3)。

## 2. 对账维度(`--explain data.compared`)

`stale`/`done` 的判定逐组对账,每组给 changed/added/removed 明细:

| 组 | 计入 | 不计入 |
|---|---|---|
| `inputs` | 阶段注册表声明的输入产物内容 hash(内容,非 mtime) | 未声明的文件(改了不算脏) |
| `tool` | 本阶段 `scripts[]` 文件 hash —— **改了代码缓存必须失效** | 其它脚本的改动 |
| `params` | 仅 `paramKeys` 声明的参数(P12-1:改字幕参数不触发重转写) | 未声明参数的全局变化 |
| `external` | 外部服务版本(asr model/url、detector 版) | — |
| `outputs` | outHash(产物内容指纹,M9-3:带外改写探测) | — |

## 3. 八种典型盘面的确定结论(判据:tests/test_pipeline_states.py)

以下结论全部可在构造盘面上机械复现;表里的"具体阶段"以 S7(字幕)为锚,
其余阶段同构:

| # | 盘面 | 结论(status → verdict) | 依据 |
|---|---|---|---|
| 1 | **改字幕**(手改 `subtitles.ass`) | S7 `stale → rerun`(outHash 带外改写);S8 输入变 → `stale → rerun` | outputs/inputs 内容 hash;正确出路是 `06_成片输出/rebuild.py`(S8 只重烧录,不冲手改) |
| 2 | **改 IR**(带外改 `project.json`) | S3 `stale → rerun`(outHash);S4/S5/S8 等直接消费者 `stale → rerun` | M9-3 + 输入 hash |
| 3 | **改 cutlist**(带外改 `cutlist.json`) | S2 `stale → rerun`;S3 若消费 applied 副本未更新 → 输入变 `stale` | 只改 `cuts[].action` 时改跑 `rs_cut --apply`,勿重跑 S2 detect(rules/incremental.md §7) |
| 4 | **改卡片**(`manifest.json` 被带外改写) | 有账:S4 `stale → rerun`(outHash 带外改写);无账且无 manifest、但 cards.json 已声明:S4 `missing → stop`(无 skip 通道);**无账且无 manifest、cards.json 也缺** → `missing → degrade-continue` | P11-1:S4 只认 --apply 写入的 `appliedAt`;marker 判定在无账分支先行 |
| 5 | **只改脚本**(改本阶段 `scripts[]` 文件) | 该阶段 `stale → rerun`,**其余阶段不动** | tool hash 只进声明它的阶段(幽灵 bug 反模式的第一防线) |
| 6 | **只改 brief**(改正文,不动参数行) | S0(输入含 brief.md)与 S10(输入含 brief.md)`stale → rerun`;S7 **不算 stale**(brief 文本不是 S7 输入) | 若改的是 `每卡字数:` 参数行 → S7 经 paramKeys 变 `stale → rerun`(P12-1) |
| 7 | **外删产物**(删 `subtitles.ass`) | S7 `missing → stop`(产物缺失) | 无产物 = 无从对账,必须重跑 |
| 8 | **外改产物**(改 `subtitles.ass` 内容) | S7 `stale → rerun`(`staleReason` 指明"产物带外改写",含新旧 outHash) | 同 1,但强调带外改写有专门证据链(outHash),与输入变化区分 |

另有两条**声明内降级**盘面(不是 stale,也不许静默):

| 盘面 | 结论 |
|---|---|
| S4 有 `appliedAt` 但 `00_制作简报/cards.json` 从未声明 | `missing → degrade-continue`(--auto 标记"无事可做"并留痕 `auto:S4:no-cards`) |
| S5 的 `variants.json` 不存在 | `missing → degrade-continue`(--auto 跳过并留痕 `auto:S5:skip`;状态保持 missing 如实亮灯) |

## 4. 与降级/超时策略的衔接

- 每阶段的 `degradePolicy`(stop/skip)与 `rerunPolicy`(content-key/marker)
  以 `templates/stages.json` 为唯一机器口径;`--explain` 原样回带(`data.contract`)。
- 判定入口只有两个:`rs_run --status`(全链路灯)与 `rs_run --explain Sx`(单阶段
  三态裁决 + 对账明细)。Agent 与用户一律以它们的输出为准,**禁止读源码猜状态**。

## 5. 门禁与验收

| 检查 | 通过线 |
|---|---|
| 8 盘面 + 2 降级盘面 | `pytest tests/test_pipeline_states.py` 全绿,每盘面结论唯一 |
| 契约一致 | `--explain` 的 `data.contract` 与 stages.json 逐字段一致(`tests/test_stages_contract.py`) |
| 生成器零 diff | `python tools/gen_stages.py --check` 退出 0 |
