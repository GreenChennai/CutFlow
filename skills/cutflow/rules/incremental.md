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

- `status ∈ {done, stale, missing, failed}`;
- `params` 记录**当时的参数快照**——这是旧工程可复现的前提(例如竖屏字数从 16 下调到 12 后,旧工程仍按 16 复现);
- `staleReason` 必须能指到**具体哪个输入变了**。

### `_state/S*.json` 与 `_state/seg_*.json`

阶段级状态 + **segment 级**状态。粒度规则:

> **缓存粒度 = segment。粗于 segment 没收益,细于 segment 管理成本爆炸。**

`_state/seg_S3_0007.json` 记录该 seg 的全部输入 hash;只重渲 hash 变了的 seg,其余 concat 复用。

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

## 4. 命令语义

| 命令 | 语义 |
|---|---|
| `rs_run.py --status` | 打印 S0–S10 状态灯:✓ done / ✗ missing / ⚠ stale(附 staleReason) |
| `rs_run.py --from S3` | 从 S3 起重跑,之前阶段 hash 命中即跳过 |
| `rs_run.py --only S7` | 只跑 S7(前提:其输入 hash 未变,否则报错并提示用 `--from`) |
| `rs_run.py --dirty` | 只重跑 stale 的阶段 |
| `rs_run.py --explain S7` | 打印 S7 为什么 stale(哪个输入变了,旧 hash → 新 hash,附脚本 hash 变化) |
| `rs_run.py --mark S7` | 手工标记某阶段为 done(用于人工介入后) |

退出码沿用 `rs_common` 协议:0 成功 / 2 输入错 / 3 依赖缺失 / 4 执行失败。

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

生成物:

| 文件 | 从哪起跑 | 改什么时用 |
|---|---|---|
| `04_cut/rebuild.py` | S2 | 手工调 CutList |
| `05_ir/rebuild.py` | S3 | 改 IR / Wordline |
| `03_assets/artboard/rebuild.py` | S4(前接 artboard 导出+回填) | 改 artboard 卡片 |
| `06_output/rebuild.py` | **S8** | 改字幕(只重烧录导出,**不重新生成字幕**) |
| `rebuild.py` | S0 | 拿不准就全量 |
| `REBUILD.md` | — | 「改了东西跑哪个」速查表 |

每个脚本固定四步,**顺序不可换**:

```
① 备份   将被覆盖的产物 → _state/backup/<时间戳>/(保留最近 5 次)
② 校验   JSON 可解析 / IR validate / ASS 可解析 / guard
         —— 不通过就【停下】并指出具体行,绝不带着坏输入往下跑
③ 级联   --from <Sx> --force;--force 只作用于起点,上游一律走缓存
④ 交付   跑到末尾 + 分级自检(L0/L1 按策略)
```

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
| 状态完整 | `--status` 覆盖 S0–S10,无未知阶段 |

## 8. 反模式

- ❌ 用 mtime 代替内容 hash —— 一次 `git checkout` 就让全部缓存失效(或更糟:失效不了)。
- ❌ 缓存键不含脚本 hash —— 幽灵 bug。
- ❌ 让阶段粒度细到「每个 filter」—— 12 个阶段是上限,超出就没有可读性了。
- ❌ 在 stale 时静默继续 —— 宁可报错,也不要用过期产物交付。
