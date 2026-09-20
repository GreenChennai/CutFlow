# Incremental — 阶段缓存与增量编排(S10 之外的横切能力)

> **ADR-0013**。一句话:**阶段产物是一等公民,能不能复用由内容 hash 决定,不由人肉记忆决定。**

## 1. 为什么需要它

旧 `compose.md` 的「中间件」一节全文是:

> `06_output/_build/<ratio>/` 下 `seg_*/base/composed/mixed/subtitled` 可复用;**改了字幕只重跑 step6-7(重调 rs_render 会全跑,手改时可复用 mixed.mkv)**。

这就是全部了——产物摆在目录里,能不能复用、复用哪几个,**靠 Agent 每次现场判断**。没有依赖声明,没有脏值计算,没有工具版本记录。于是:

| 编辑动作 | 旧行为 |
|---|---|
| 改一个字幕文字 | 全跑(实测 20min+) |
| 换背景图 | 全跑 |
| 调语句顺序 | 全跑 |
| 换 Logo / 加变体 | 不支持 |

而且**跑完之前你不知道哪几步会命中缓存**,没有任何"为什么慢"的可观测性。

## 2. 状态文件

### `05_ir/pipeline.json`(阶段清单)

```json
{
  "version": 1,
  "slug": "20260910-demo-口播",
  "createdAt": "2026-09-10T14:02:11+08:00",
  "params": {"maxChars": {"9x16": 12, "3x4": 15, "16x9": 22}, "cpsMax": 9},
  "stages": {
    "S1": {
      "status": "done",
      "inputs":  [{"path": "01_materials/a.mp4", "sha1": "9f2c..."}],
      "params":  {"model": "paraformer-zh", "punc": "ct-punc"},
      "tool":    {"rs_align.py": "ab31...", "asr_server": "0.4.2"},
      "outputs": [{"path": "05_ir/wordline.json", "sha1": "77de..."}],
      "ts": "2026-09-10T14:02:11+08:00"
    },
    "S7": {"status": "stale", "staleReason": "S3 输出 hash 变化"}
  }
}
```

- `status`(`_state/S*.json` 落盘口径)`∈ {done, failed, missing, corrupt}`;`stale` / `blocked` 是 `--status` 的实时判定,不落盘。
  **`failed` 已实现(P25-1,原为文档承诺)**:阶段执行失败即落 `{"status":"failed","error":…}`,
  `--status` 显式显示 failed + 原因,其下游全部显示 **blocked**(`--dirty` 本轮不选 blocked,
  先让失败的上游重跑,成功后下轮收敛循环自然接上);重跑成功状态自然回 `done`;
- `params` 记录**当时的参数快照**——这是旧工程可复现的前提(例如竖屏字数从 16 下调到 12 后,旧工程仍按 16 复现);
  - **P12-1 起该机制真实生效**:`rs_run` 每次写状态都会把生效参数落进 `pipeline.json` 的 `params`(首次运行即回填,旧工程缺字段也补一次);
  - 参数来源与优先级:brief.md 显式声明(`每卡字数:` / `CPS:` / `画幅:` / `平台:` 逐行)> pipeline.json 已存值 > 代码默认(`segmentation.MAX_CHARS` + cps 9);
  - **改 brief 里声明的参数 → 字幕(S7)变脏**;`params_of` 仅在完全缺失时回退默认。
- `staleReason` 必须能指到**具体哪个输入变了**;
- **状态文件损坏 ≠ missing**(P15-1):`_state/S*.json` 解析失败判 `corrupt` 并在 `--status` 显式告警,按缺失处理重跑但绝不静默;状态与记账写盘一律「临时文件 + `os.replace`」原子写,中断/掉电不留半截 JSON。

### `_state/S*.json` 与 `06_output/_build/<ratio>/segcache/`(实码口径,P25-1)

阶段级状态在 `_state/S*.json`;**segment 级**缓存在 rs_render 手里,真实落点是
`06_output/_build/<ratio>/segcache/<segKey>.mp4`:`segKey` 按「该段输入(clip 全字段含尾帧扩展)+
文档参数 + 画幅 + 源指纹」内容寻址,**只重渲 key 变了的 seg**,其余 concat 复用;
上限 `SEG_CACHE_KEEP=400` 按 mtime 淘汰,`rs_render --clear-cache` / `--no-cache` 显式管理。

> **缓存粒度 = segment。粗于 segment 没收益,细于 segment 管理成本爆炸。**

(旧文档写的 `_state/seg_S3_0007.json` 从未存在过——段级键一直在 rs_render 的 segcache 里,已改指实码。)

### 2.5 人工阶段的「真实产物标记」与产物落点(P11-1 / P10b-1)

- **S4(动画/信息卡)只认 `03_assets/artboard/manifest.json` 里的 `appliedAt`**(`rs_artboard --apply` 成功时写入)。不再声明 `05_ir/project.json` 当产物——那是 S3 的,曾让 S3 一跑完 S4 就自动 ✓。无卡片的工程用 `rs_run --mark S4` 显式记录"无事可做";
- 人工阶段(S0/S4/S11)无真实标记一律判 **missing**,绝不借上游产物自动 done;
- **交付成片各落独占子目录**:S8 烧录导出 → `06_output/final/final_*.mp4`;S5 品牌变体 → `06_output/branded/成片_*.mp4`(rs_render 的 final 档默认落 `06_output/final/`,rs_brand 经 `--out` 显式指定落点)。从根上消除两阶段 glob 交叠互相打脏;
- **旧工程兼容**:顶层遗留的 `final_*.mp4` / `成片_*.mp4` 不追改历史——`{final_video}` 映射、`rs_verify`/`rs_ingest` 成片清单与 `rs_cleanup` 白名单同时认新子目录与旧顶层落点;首次重跑起产物自动落新目录。

## 3. 缓存键公式(最容易写错的地方)

```
key(stage) = sha1(
      Σ hash(上游产物)            ← 内容,不是 mtime
    + Σ hash(stage 参数快照)
    + Σ hash(本阶段用的脚本文件)   ← rs_*.py 自己的内容 hash
    + 外部服务版本(model / asr_server / tts 引擎)
)
```

**反模式警告**:只 hash 输入文件会导致「**改了代码但缓存命中**」的幽灵 bug——增量系统最常见也最致命的一类故障。`rs_run.py` 会把本阶段脚本的文件 hash 一并计入并在 `--explain` 中展示。

**参数按阶段声明(P12-1)**:只有阶段注册表里声明了 `paramKeys` 的阶段(目前 S7 = `maxChars`/`cpsMax`)把参数计入缓存键;未声明的阶段不因全局参数变化被打脏——**改字幕参数不该触发重转写**。全量参数快照仍存 `pipeline.json` 供复现。S7 的命令行经 `{max_chars}` 占位符真正消费生效参数——brief 改字数,重跑产出的字幕卡就真的不一样。

**`--dirty` 逐轮收敛(P13-1)**:上游重跑会把下游打脏(如 S7 改字 → S8 输入变),只在起点选一次会漏掉"运行中途变脏"的阶段,收敛被拖到下一次调用。`--dirty` 现在逐轮重选直到没有非 done 阶段,单次调用内收敛;`--from/--only` 单轮(列表覆盖全部后续阶段,级联由循环内重评驱动)。

## 4. 命令语义

| 命令 | 语义 |
|---|---|
| `rs_run.py --status` | 打印 S0–S11 状态灯:✓ done / ✗ missing / ⚠ stale(附 staleReason);状态文件损坏显式标 corrupt |
| `rs_run.py --from S3` | 从 S3 起重跑,之前阶段 hash 命中即跳过 |
| `rs_run.py --only S7` | 只跑 S7;**命中已 done 时报错**(`PRECONDITION_FAILED`,非零退出)并提示补 `--force`——静默 cached 会吞掉带外改动(P14-1) |
| `rs_run.py --dirty` | 只重跑 stale 的阶段;连跑两次第二次全 cached(收敛) |
| `rs_run.py --force --from S3` | 强制重跑起点阶段(上游仍走缓存);**`--force` 必须搭配 `--from/--only`,单独使用报错**(P13-1,不再静默 no-op) |
| `rs_run.py --explain S7` | 打印 S7 为什么 stale(哪个输入变了,旧 hash → 新 hash,附脚本 hash 变化) |
| `rs_run.py --mark S7` | 手工标记某阶段为 done(用于人工介入后);与 CutForge 编辑器并发时(`.cutforge/lock` 存在)写不进去会显式报错 |

退出码沿用 `rs_common` 协议:0 成功 / 2 输入错 / 3 依赖缺失 / 4 执行失败。

## 4.5 与编辑器(CutForge)共存(O7-2)

`rs_run` 写任何状态/记账前探测 `.cutforge/lock`(锁协议归 cutforge 管,rs_run 只做存在性探测):编辑器在运行 → **只读降级**——阶段照跑、产物照出,但状态不写盘,并在 stderr、结果消息与 `data.stateReadOnly` 明确告警;`--mark` 写不进去直接 `STATE_READONLY` 非零退出。绝不静默丢账。

## 4.6 备份与清理的诚实口径(P13-1 / P13-2)

- **备份范围 = 每个真正写盘的非 cached 阶段**(含 `--dirty` 级联里被判 stale 的),不只 `--force` 起点——备份移进了 `run_stage`,覆盖前先备份,备不下就中止;
- 删除失败不许掩盖:旧备份清理失败逐项上报(`data.pruneFailed`)并使整次运行非零退出;`rs_cleanup --apply` 同口径,"已释放 N MB"只计真删掉的,失败项在 `data.failed`。

## 4.7 无人值守(--auto)与决策留痕(阶段四 N3/N4)

`rs_run.py --auto` 是**从意图编译入口开始的全程无人值守**(rs_intent compile → rs_run --auto),
不是新状态机:阶段注册表、缓存键、`--from/--only/--dirty` 语义全部不变,它只把「会停下来问人」的
CHECK 换成「自动决策 + 理由留痕」。与 intake 双模式里 `automation`(用户全权,Agent 临场自选)的
区别:--auto 连 Agent 的临场裁决也收归脚本口径,每条落 `05_ir/pipeline.json` 的 `decision_log`:

| CHECK | --auto 的裁决 | 留痕 |
|---|---|---|
| 断句歧义(`ambiguous`) | 取断句 DP 最优,不问 Agent | `auto:S7:ambiguous-auto`;候选本就在 `06_output/segments_candidates.json` 供事后复核 |
| 粗剪 `review` 刀 | **保守保留**(宁可漏删不可错删),`--apply` 照常重算 keep + 时长账,再 remap 出成片空间 wordline | `auto:S2:review-keep` / `auto:S2:auto-apply` / `auto:S2:remap` |
| 人工阶段 S0 | 注册 cmd 本是机械命令(摄取)→ 照常执行 | `rs_run` 结果行 `manualAuto` |
| 人工阶段 S4 | 无 `00_brief/cards.json` → 标记无事可做;有 → artboard 三连(gen-cards --force/export/apply) | `auto:S4:no-cards` / `auto:S4:cards-chain` |
| 人工阶段 S11 | 跑交付对账 + 生成 `06_output/决策说明书.md`;缺封面/占位文案如实留痕不代劳 | `auto:S11:deliverables` |
| S5 无 `variants.json` | 跳过(缺声明宁可漏做不猜),状态保持 missing 如实亮灯 | `auto:S5:skip` |
| L1 目测 | **降级为抽帧留证**(`rs_bench` 网格图 `06_output/L1未人工确认_抽帧留证.png`),不判定不阻断 | `auto:verify:l1-degrade` / `auto:verify:bench` |
| L0 机械自检 | **硬闸不放松**,不过即非零退出 | `auto:verify:l0` |
| L2 最终验收 | **始终归用户,auto 不代劳**(不写 firstCheck) | 运行结果 `l2Note` |

- `decision_log` 条目带稳定 `id`(意图 `intent:<字段>` / 运行时 `auto:<阶段>:<种类>`),
  重跑同 id **覆盖不堆积**;意图条目无时间戳(编译纯函数,同输入字节级复现),
  运行时条目带 `at`;
- S2 的 auto 后置会同步 wordline 时长账(P27-1)——S1 的 outHash 随之刷新并留痕
  (`auto:S1:outHash-refresh`),避免账面三字段变动触发无谓重转写;这是账本维护,
  不是放松护栏:任何带外改写(编辑器/人手)依旧判 stale;
- 交付侧人类可读出口:`rs_ingest.py decisions <工程>`(S11 deliverables 自动附带),
  列「参数快照 / 决策逐条(来源→为什么→推断与否)/ 改一条重跑一段对照表」。

### 参数 → 命令行(阶段四起 S3/S7/S8 消费画幅与字幕样式)

P12-1 的「brief 声明即参数源」延伸到画幅与字幕样式:S3/S8 的 `--ratio {ratio}`、S7 的
`--style {sub_style} --ratio {ratio} --from-wordline {final_wordline}` 进命令行与缓存键
(paramKeys:S3/S8=`ratio`,S7=`maxChars/cpsMax/ratio`)。来源优先级:brief.md 显式声明
(`画幅:` / `平台:` / `风格 token:` 行)> 平台预设(`templates/platforms.json`)>
旧字面缺省(talkshow-bold / 9x16)——未声明任何参数的旧工程命令与从前**逐字节一致**。
`{final_wordline}` 让有粗剪的工程用 remap 后的成片空间 wordline 出字幕(与 S9 对账同一约定;
无 remap 产物的旧工程仍解析到 `05_ir/wordline.json`,行为不变)。S3/S7/S8 的缓存键因此新增
`ratio` 维度:老账首次对账会判一次 stale 重跑,属诚实的参数接管。

## 5. 改造前后代价对照(这是这一机制的全部意义)

| 编辑动作 | 旧 | 新 | 走哪条路 |
|---|---|---|---|
| 改一个字幕文字 | 全跑 20min+ | **秒级** | 只重生成该卡 ASS 事件 + 重叠一次(S7) |
| 改字幕断句(挪一个切点) | 全跑 | **局部 DP 窗口** | 只对受影响卡及邻居重跑切分 DP(±3 卡窗口) |
| 调整语句顺序 | 全跑 | 中等 | S2 CutList 重排 → S3 起重跑,但**不重新转写**(S1 命中) |
| 换背景图 | 全跑 | 中短 | 只重渲 S3/S4 中受影响的 seg |
| 换 BGM | 全跑 | 短 | S6 起重跑(混音段) |
| 换 Logo / 加变体 | 不支持 | **短** | S5 + S10,其余全部命中缓存 |
| 换 TTS 音色 | 全跑 | 长(合理) | S1 起重跑,S0/S2 缓存仍有效 |

## 6. 预期观测

```powershell
python skills/cutflow/scripts/rs_run.py --status
# S0 ✓  S1 ✓  S2 ✓  S3 ⚠ stale (compose.md 改了 reframe 默认值)
# S4 ⚠ stale (S3 变化)  S5 ✗  S6 ✗  S7 ✗  S8 ✗  S9 ✗  S10 ✗

python skills/cutflow/scripts/rs_run.py --explain S3
python skills/cutflow/scripts/rs_run.py --only S7      # 只重做字幕
python skills/cutflow/scripts/rs_run.py --dirty        # 只跑 stale
```

## 7. 手改工作流(一键重建)

> 用户改完东西,**不该需要理解管线**。改哪个文件夹,就跑那个文件夹里的 `rebuild.py`。

```powershell
python skills/cutflow/scripts/rs_run.py --root <工程> --init   # 生成各文件夹的 rebuild.py(一次性)
```

生成物(`rs_run --init` 的 INIT_MAP,共 4 个文件夹 + 工程根;**不是"每个文件夹都有"**):

| 文件 | 从哪起跑 | 改什么时用 |
|---|---|---|
| `04_cut/rebuild.py` | S2 | 手工调 CutList |
| `05_ir/rebuild.py` | S3 | 改 IR / Wordline |
| `03_assets/artboard/rebuild.py` | S4(前接 artboard 导出+回填,一条龙) | 改 artboard 卡片 |
| `06_output/rebuild.py` | **S8** | 改字幕(只重烧录导出,**不重新生成字幕**) |
| `rebuild.py`(工程根) | S0 | 拿不准就全量 |
| `REBUILD.md` | — | 「改了东西跑哪个」速查表 |

每个 rebuild.py 是一个薄壳:实际执行 `rs_run --from <Sx> --force`,真正做的事是——

```
① 备份   每个「真正写盘的非 cached 阶段」跑前自动备份将被覆盖的产物
         → _state/backup/<时间戳>/(保留最近 5 次;备不下就中止,绝不无保险覆盖)
② 级联   --force 只作用于起点阶段,上游一律走缓存;--dirty 路径逐轮收敛
③ 自检   跑到末尾后按 verify_policy 分级自检(首次/画面变过 → L1,否则 L0)
```

(旧文档写的「固定四步:备份→校验→级联→导出」与实现不符,已改上面的实际口径;
**校验**由各阶段脚本自身的输入校验承担——JSON 可解析 / IR validate / guard /
`WORDLINE_MANUAL_EDIT` 手改护栏等,不合法会停住并指出具体位置,绝不带着坏输入往下跑。)

**为什么必须备份**:用户手改的可能是**产物**(如 `subtitles.ass`),而重跑会覆盖它。这是"手改成果不会被一键脚本毁掉"的唯一保险。

**为什么需要 `--force`**:手改产物后该阶段的**输入 hash 没变**,缓存会判定 done 而跳过,改动不生效。`--force` = 忽略**本阶段**的完成状态强制重跑,但上游仍走缓存(这才是"改字幕只要几秒"的关键)。

```powershell
python skills/cutflow/scripts/rs_run.py --root <工程> --rollback          # 还原最近一次备份
python skills/cutflow/scripts/rs_run.py --root <工程> --rollback --at 20260910-2130
```

> ⚠️ **易错点**:哪个阶段会覆盖用户的手改内容?
> - 改 `subtitles.ass` → **S7 会覆盖它**,所以 `06_output/rebuild.py` 从 **S8(烧录导出)** 起跑,只用现有 ass 重新烧录编码;
> - 改 `project.json` → S3 会覆盖,`05_ir/rebuild.py` 从 S3 起跑前已自动备份。

## 8. 门禁与验收

| 检查 | 通过线 |
|---|---|
| 缓存幂等 | 无改动重跑 `--from S3`,全程命中,墙钟 ≤ **2s** |
| 单卡改字 | 端到端 ≤ **10s**(对照:改造前 20min+) |
| stale 可解释 | `--explain` 能指出具体变化的输入(含脚本 hash) |
| 状态完整 | `--status` 覆盖 S0–S11,无未知阶段 |
| 手册命令对拍(P19-1) | SKILL.md/rules 里每条 `rs_*.py` 命令都能被对应 argparse **接受**(`python tests/check_manual_cmds.py` / `pytest tests/test_v19_manual_gate.py`,解析级不执行) |

## 8. 反模式

- ❌ 用 mtime 代替内容 hash —— 一次 `git checkout` 就让全部缓存失效(或更糟:失效不了)。
- ❌ 缓存键不含脚本 hash —— 幽灵 bug。
- ❌ 让阶段粒度细到「每个 filter」—— 12 个阶段是上限,超出就没有可读性了。
- ❌ 在 stale 时静默继续 —— 宁可报错,也不要用过期产物交付。
