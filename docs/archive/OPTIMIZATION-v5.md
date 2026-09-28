# CutFlow 优化提案 v5 — 自主 · 分级 · 省 Token · 可手改 · 闭环

> **触发**：用户提出 5 条新需求(自带 ASR / 检查分级 / 省 Token / 手动改阶段 / artboard 闭环),全权委托。
> **方法**:先审问(grill),再设计。每条需求先回答三个问题——**它真正要什么 / 会在哪里坏 / 证据在哪**,然后才给方案。
> **产出日期**:2026-09-10 ｜ **基线**:CutFlow v0.4.0(ADR-0011~0014 已落地)
> **性质**:提案 + 落地清单。本文件写完后按批次实施。

---

## 0. TL;DR

| # | 需求 | 真正要的东西 | 最大陷阱 | 结论 |
|---|---|---|---|---|
| R1 | 自带 FunASR,不依赖 MomentShift 服务 | **一条命令就能转写**,不需要先开另一个软件 | 实测发现本地 ONNX 版 Paraformer **根本没有 timestamp 输出** → 字级对齐拿不到 | 双后端:官方 `funasr` 包(有字级)/ 轻量 ONNX(无字级)。**必须诚实分级**,不能假装 ONNX 就能给字级 |
| R2 | 首次检查,之后只跑代码自检 | **改字幕不该等于重跑验收** | 静默不检查 → 用户以为一切正常 | 三级验证 `L0 机械`(每次) / `L1 语义`(首次 + 构图变更) / `L2 人工`(用户触发)。**每次输出必须声明本次到了哪级** |
| R3 | 省 Token,脚本优先 | **别再让 Agent 干脚本的活** | 规则文件互相引用 → 一读一大串 | SKILL.md 内联「谁来做」表 + 「只读一个文件」路由表 + 体量纪律 |
| R4 | 手动改阶段后一键重建 | **改完不用懂管线,点一下就行** | 一键脚本**覆盖掉用户手改的成果** | `rebuild.py` + `--force` + **先备份后校验再级联**,失败即停 |
| R5 | artboard 改完一键出片 | **设计师改图 → 视频自动更新** | 卡片时长变了会打乱整条时间轴 | manifest 记录工程↔产物↔尺寸;时长变更自动触发下游重算 |

**一句话**:v4 解决「时间对不对」,v5 解决「**谁来做、做多少、改完怎么办**」。

---

## 1. R1 · 自带 FunASR,砍掉外在服务器

### 1.1 它真正要什么

用户原话:「使用自带 Funasr 脚本部分,不依赖 MomentShift 这种外在的 Funasr 服务器」。

拆开看,痛点是三个,不是一个:

1. **启动成本** —— 想转写必须先开 MomentShift 并切到"服务模式",忘了就失败;
2. **耦合风险** —— MomentShift 升级/移动/卸载 → CutFlow 直接残废;
3. **心理所有权** —— "我的视频工具为什么要靠另一个软件的服务器?"

所以真正的验收标准不是"能转写",而是:**在一个只装了 CutFlow 的机器上,`python tools/fun_asr.py <视频>` 直接出文本。**

### 1.2 证据:先把"能不能"测清楚(本机实测,2026-09-10)

不测就设计 = 赌博。实测结论如下。

**① ONNX 模型签名(用 onnxruntime 直接读图)**

| 模型 | 输入 | 输出 |
|---|---|---|
| `paraformer-large/model_quant.onnx`(237MB) | `speech[B, T, 560]`、`speech_lengths[B]` | **`logits[B, L, 8404]`、`token_num`** |
| `fsmn-vad/model_quant.onnx` | `speech[1, T, 400]` + 4 个 cache | `logits`、`out_cache0..3` |
| `ct-punc/model_quant.onnx` | `inputs[B, T]`、`text_lengths[B]` | `logits[B, L, 6]` |

**关键发现:Paraformer 的 ONNX 导出只有 2 个输出,没有 `timestamp`。**
CIF 下采样已经把时间轴压掉了 —— 输出序列长度 `L` 是**字符数**,不是帧数,**时间信息在导出阶段就丢了,事后无法恢复**。

> 这一条直接否决了「只做 ONNX 就能拿到字级时间戳」的设想。如果不测就写代码,会做出一版永远降级的 Wordline。

**② 模型落盘位置与体量**

```
MomentShift/tools/funasr/
├── paraformer-large/      model_quant.onnx 237MB + config.yaml + am.mvn
├── paraformer-large-fp32/
├── fsmn-vad/              model_quant.onnx + vad.mvn + vad.yaml
├── ct-punc/               model_quant.onnx + tokens.json
└── (cam++ / sensevoice-small / fun-asr-nano / whisper-* …)
```

**③ 依赖现状**

| 解释器 | onnxruntime | numpy | torch | funasr |
|---|---|---|---|---|
| MomentShift `.venv` | 1.28.0 | 2.5.1 | ✗ | ✗ |
| 托管 3.13.12 | ✗ | ✗ | ✗ | ✗ |
| 系统 3.14 | ✗ | ✗ | ✗ | ✗ |

→ **CutFlow 目前没有任何一个能跑 ASR 的解释器**。必须自建。

**④ 代码体量**:MomentShift 的 ONNX 推理层(FuASR 裁剪版, MIT)共 **339KB / 3182 行**,外部依赖仅 `numpy` + `onnxruntime` + `jieba`(标点用)。

### 1.3 决策:双后端 + 自建 venv + 模型播种

**不是二选一,而是明确分工:**

| 后端 | 实现 | 字级时间戳 | 依赖 | 速度 | 定位 |
|---|---|---|---|---|---|
| **`pkg`** ⭐ | 官方 `funasr` 包(本项目自建 venv) | **✅ 原生 `timestamp`** | torch-cpu + funasr(约 1–2GB) | 中 | **精度优先**,Wordline 不降级 |
| **`onnx`** | 自带 vendored ONNX 推理层 | ❌ 仅 VAD 段边界 | numpy + onnxruntime(约 200MB) | 快(CPU int8) | 轻量,Wordline 标 `degraded` |
| `server` | 现有 HTTP 路径 | 取决于服务 | — | 中 | **仅兼容保留**,不再默认 |

**为什么两个都留**:
- Wordline(ADR-0011)的**字级**能力只有 `pkg` 能给。如果只留 ONNX,v4 的核心成果会在 R1 上被削掉一半;
- 但 `pkg` 要拉 1–2GB。用户可能只想"先跑起来看看" → ONNX 是合理的第一步;
- 两者产出**同构**的中间数据(都落到 wordline.json),下游完全无感。

**后端选择链**(`tools/fun_asr.py` 自动决策,不靠用户记):

```
1. config.asr.backend 显式指定 → 用它(不存在则报错,不静默回退)
2. 未指定 → pkg 就绪? 用 pkg :  onnx 就绪? 用 onnx :  server 可达? 用 server : 报错并给出安装命令
```

### 1.4 模型从哪来(不重新下载 237MB)

用户机器上**已经有**全套模型。设计「**播种(seed)**」而不是「下载」:

```powershell
# 从任意现有位置播种到 CutFlow 自己的模型目录(默认只建软链,失败则复制)
python tools/fetch_deps.py asr --seed-models "E:\平日资料\GitHub\MomentShift\tools\funasr"
# 或纯下载(无本地副本时)
python tools/fetch_deps.py asr --download
```

- 模型落在 `config.asr.models_dir`(默认 `<repo>/models/funasr/`),**在 .gitignore 里**;
- 播种优先用**目录联接(junction)**,零拷贝、秒级;不支持则退回复制并提示体积;
- **模型是数据不是服务** —— 复用磁盘上已有的模型文件**不构成**对 MomentShift 的运行时依赖;CutFlow 不 import 它的任何代码。

### 1.5 陷阱清单(设计时就要绕开)

| 陷阱 | 后果 | 对策 |
|---|---|---|
| 把 ONNX 当"有字级时间戳" | Wordline 永远降级而无人察觉 | 后端能力写进返回体(`capabilities.charTimestamps`),`rs_align` 据此决定是否标 `degraded`;SKILL.md 明写 |
| 播种用复制 → 磁盘翻倍 | 237MB × N | 优先 junction/symlink,复制需 `--copy` 显式确认 |
| 自建 venv 与主解释器混淆 | 调错 python,pip 装到全局 | venv 路径固定 `tools/.venv-asr/`;所有调用走绝对路径;`fetch_deps.py` 负责创建 |
| 长音频一次性喂 ONNX | 爆内存 | 沿用 60s 分段 + 段间叠加窗口(与现有服务端语义一致) |
| 无 VAD 模型时 | 拿不到任何边界 | 明确报"需 fsmn-vad";不得静默退化为整段均分 |
| 语言/热词 | 专有名词错 | `pkg` 后端支持热词表;`onnx` 后端不支持 → 能力矩阵里写明 |

### 1.6 交付物

```
tools/
├── fun_asr.py            ★ CutFlow 自带 ASR CLI(--json 输出,后端自动选择)
├── asr_vendor/           ★ vendored ONNX 推理层(FuASR 裁剪版,MIT,保留 NOTICE)
│   ├── NOTICE.md         出处/许可证/裁剪说明/与上游差异
│   ├── paraformer_bin.py / vad_bin.py / punc_bin.py / utils/
├── fetch_deps.py         ← 增补 asr 模块(venv / pip / 播种 / 状态)
skills/cutflow/
├── scripts/rs_align.py   ← 改为调用 tools/fun_asr.py,HTTP 降为兜底
└── rules/asr.md          ★ 自带 ASR 的使用与排障(按需读)
```

---

## 2. R2 · 检查分级:首次全检,之后只跑代码自检

### 2.1 它真正要什么

用户原话:「首次剪辑的时候才运行检查,检查完毕→有问题→修复→检查→没问题→完成;之后用户有要求修改,比如说修改某个字幕,修改之后也不用再次检查,只运行代码部分的自检即可,剩下的实际检查交给用户来完成,触发用户要求你检查」。

翻译成工程语言:

- 存在一个**收敛循环**(检查→修复→再检查),它只在**第一次产出成片**时跑,直到通过;
- 之后的编辑是**增量**的,只跑**机器能自动判定的**那部分;
- **语义判断(好不好看)的所有权归用户**,Agent 不得主动代劳——因为那既慢又费 token,而且用户本来就要亲眼过一遍。

### 2.2 三级验证的定义(这是本节的骨架)

| 级别 | 名称 | 谁执行 | 成本 | 何时跑 | 判据 |
|---|---|---|---|---|---|
| **L0** | 机械自检 | 纯脚本 | 秒级 | **每一次**产出后 | ffprobe 可解码、IR validate、ASS 可解析、CPS ≤9、字数 ≤上限、卡不重叠、对齐偏移中位数 ≤40ms、粗剪 guard 全过、字幕文本与 Wordline 一致 |
| **L1** | 语义自检 | Agent(需看图) | 分钟级 + token | **首次**;以及**改动触及画面构图时** | rs_bench 抽帧网格目测:黑帧/绿幕残留/字幕压脸/卡片错位/Logo 压字幕 |
| **L2** | 人工验收 | 用户 | 人的时间 | **由用户显式触发** | 用户看完说行/不行 |

**归属铁律**:L1 的所有权在 Agent,但**触发权在首次**;L2 的所有权在用户。Agent **不得主动**发起 L1,除非用户说"帮我检查一下"。

### 2.3 状态与判定

`_state/verify.json`:

```json
{
  "version": 1,
  "firstCheck": {"done": true, "at": "2026-09-10T21:30:00+08:00", "level": "L1",
                 "result": "pass", "rounds": 2, "artifactHash": "9f2c..."},
  "lastL0": {"at": "...", "level": "L0", "result": "pass", "artifactHash": "77de..."},
  "userReview": {"requested": false, "at": null}
}
```

判定规则:

```
needL0 = 每次产出后 —— 永远为真
needL1 = (!firstCheck.done)                          // 首次
      || (本次改动触及画面轨道: 背景/卡片/Logo/转场/重构图/比例)
needL2 = 用户显式要求
```

**"触及画面"的判定不靠 Agent 猜**:由 `rs_run --verify` 比对本次变更的阶段集合——
`S3/S4/S5` 任一被重跑 → 画面变了 → 提示 L1;只重跑 `S7/S9` → 字幕/文案变了 → 只 L0。

### 2.4 这条需求最危险的地方

**「之后不用检查」很容易被实现成「之后不检查」。** 两者的区别是:

- 前者:仍在跑 L0,只是不跑需要看图的 L1 —— 而且**输出里明确写着**"本次验证级别 L0";
- 后者:静默什么都不做,用户看到一个漂亮的"✓ 完成",但字幕可能已经超字数、时间已经重叠。

所以硬规则:**任何一次交付输出,必须携带 `verifyLevel` 与 `firstCheckDone` 字段**;缺失即视为未验证,不得宣称完成。

### 2.5 交付物

- `rules/verify.md` —— 三级定义 + 何时跑 + 输出契约
- `skills/cutflow/scripts/rs_verify.py` —— L0 聚合自检(**一个脚本跑完全部机械判据**,输出人读报告 + `--json`)
- `rs_run.py --verify [--full]` —— 按状态决定跑哪级
- SKILL.md Hard Rule 增补:L1 不得主动跑

---

## 3. R3 · 省 Token:脚本优先 + 只读当前任务的规则

### 3.1 它真正要什么

用户原话:「节省Token,能够用脚本跑的就全用脚本跑,不能的才Agent接管,主Skill里面标注,不是当前的任务不要读其他子Markdown,以节省Token」。

两个机制,一句话:**把"读什么"和"谁来做"变成查表,而不是靠 Agent 临场判断。**

### 3.2 机制 A · 「谁来做」表(内联进 SKILL.md)

| 产物/动作 | 谁做 | 依据 |
|---|---|---|
| 环境体检、依赖安装 | **脚本** | 确定性 |
| 转写、字级对齐 | **脚本** | 确定性 |
| 粗剪检测 + guard + CutList | **脚本** | 确定性 |
| 断句 DP + 硬约束 | **脚本** | 确定性 |
| 时段/字数/CPS/对齐自检 | **脚本** | 确定性 |
| 渲染、混音、编码、变体 | **脚本** | 确定性 |
| 封面抽帧与合成 | **脚本** | 确定性 |
| 文案(标题/简介/Tag) | **Agent**(脚本只校验+截断+生成章节) | 需要写作 |
| 断句歧义裁决(`ambiguous`) | **Agent** | 需要语感 |
| 术语校对、口水词处理 | **Agent** | 需要语义 |
| 卡片文案与设计意图 | **Agent** | 需要创意 |
| L1 语义自检(看图) | **Agent**(首次/构图变更) | 需要视觉 |
| L2 最终验收 | **用户** | 需要品味与责任 |

**判据**:能写成"给定输入必得同一输出"的 → 脚本;需要判断/创造/审美 → Agent。

### 3.3 机制 B · 路由表(内联进 SKILL.md)

```
任务 → 只读这一个文件,读完就停
────────────────────────────────────────
开工前问卷、brief 契约        → rules/intake.md
转写/自带 ASR/模型              → rules/asr.md
字级对齐 / 重映射               → rules/align.md
粗剪 / CutList / guard          → rules/roughcut.md
字幕断句 / CPS                  → rules/subtitles.md
IR 与渲染 / 中间件             → rules/compose.md
阶段缓存 / 增量 / rebuild       → rules/incremental.md
检查分级 / 自检 / 验收          → rules/verify.md
Logo 与变体                     → rules/branding.md
音效落点                        → rules/sfx.md
标题简介 Tag / 章节             → rules/meta.md
封面                            → rules/cover.md
artboard 图形素材              → rules/artboard.md
配音 TTS                        → rules/tts.md
剪映草稿                        → rules/jianying.md
工程归档命名                    → rules/archive.md
类型节奏默认值                  → rules/genres/<类型>.md(仅一册)
────────────────────────────────────────
禁止:一次读两个以上规则文件(除非用户明确要求交叉说明)
```

### 3.4 机制 C · 体量纪律(防止 SKILL.md 自己变成负担)

- **SKILL.md 只放"每次都需要的"**:阶段表、硬规则、命令速查、路由表、反模式;
- **参数细节留在规则文件**:SKILL.md 只写默认值摘要 + "详见 X";
- 目标:SKILL.md ≤ 260 行;超了就往下沉;
- 已有测试防漂移(rules 必须被索引),**再加一条**:路由表必须覆盖 `rules/` 下全部文件。

### 3.5 陷阱

| 陷阱 | 后果 | 对策 |
|---|---|---|
| 内联太多 → SKILL.md 巨无霸 | 每次都读一大坨,反而更费 | 体量上限 + 只内联"决策所需最小集" |
| 路由表与 rules 不同步 | 新规则没人读 | 单测强制覆盖 |
| 脚本化过度 | 把需要判断的事也塞给脚本,产出变差 | 「谁来做」表的判据写死;脚本只做确定性工作 |
| Agent 图省事跳过规则 | 参数拍脑袋 | 硬规则:该阶段**必须先读对应规则**再动手 |

---

## 4. R4 · 手动改阶段 + 文件夹内一键重建

### 4.1 它真正要什么

用户原话:「允许用户手动修改阶段,比如,用户觉得某一处字幕不好,手动更改了,那么在那个文件夹里面应该有一个Python脚本,能够一键启动并检查,然后连带触发其他脚本,最终重新合成导出成完成视频」。

关键诉求:**用户不需要理解管线,只需要知道"我在哪个文件夹改的,就点那个文件夹里的脚本"。**

### 4.2 设计:`rebuild.py` 生成器 + 强制起点语义

`rs_run.py --init` 在每个阶段文件夹写入一个薄壳脚本:

```
<工程>/
├── 04_cut/rebuild.py          → 从 S2 起重跑(改完 cutlist 用)
├── 05_ir/rebuild.py           → 从 S3 起重跑(改完 IR / wordline 用)
├── 03_assets/artboard/rebuild.py → artboard 导出 + 从 S4 起重跑(见 R5)
├── 06_output/rebuild.py       → 从 S7 起重跑(改完字幕用)★ 最常用
└── rebuild.py                 → 全量
```

每个 `rebuild.py` 的行为固定为四步,**顺序不可换**:

```
1) 备份   把将被覆盖的产物复制到 _state/backup/<时间戳>/
2) 校验   跑该阶段与下游的机械校验(JSON 可解析 / IR validate / ASS 可解析 / guard)
          ── 不通过就【停下】并打印具体错误行,绝不带着坏输入往下跑
3) 级联   从指定阶段起重跑(--from),上游命中缓存;本次强制忽略该阶段自身缓存(--force)
4) 交付   跑到 S10,输出成片与 rs_verify 报告 + 本次验证级别
```

**为什么必须备份**:用户手改的可能是**产物**(如 `subtitles.ass`),重跑会覆盖它。备份是"手改成果不会被一键脚本毁掉"的唯一保险。

**为什么需要 `--force`**:手改产物后,该阶段的**输入 hash 没变**,缓存会判定它 done 而跳过——用户改的东西不会生效。`--force` 表示"忽略本阶段的完成状态,强制重跑",但**上游仍走缓存**(这才是"改字幕只要几秒"的关键)。

### 4.3 校验失败时的人话输出(重要)

```
✗ 06_output/rebuild.py 已停止
  原因:subtitles.ass 第 143 行的结束时间早于开始时间
        Dialogue: 0,0:01:12.50,0:01:11.20,Main,...
  已备份: _state/backup/20260910-2130/
  下一步:改回该行后重新运行本脚本;或运行  `python rebuild.py --rollback` 还原备份
```

### 4.4 陷阱

| 陷阱 | 后果 | 对策 |
|---|---|---|
| 一键脚本覆盖手改产物 | 用户白干 | **先备份**;`--rollback` 可还原 |
| 手改产生非法结构 | 下游雪崩式报错 | **先校验后级联**,校验不过立即停 |
| 用户改了上游却点了下游脚本 | 改动不生效 | 校验阶段做"产物 hash vs 上一轮记录"比对,发现上游产物被手改则**提示改点哪个脚本** |
| `--force` 被滥用 → 每次都全跑 | 失去增量意义 | `--force` 只作用于**指定阶段**,上游一律走缓存 |
| 生成物进仓库 | 污染 | `rebuild.py` 是工程内生成物,`.gitignore` 已覆盖工程目录 |

### 4.5 交付物

- `rs_run.py`:新增 `--init`、`--force`、`--rollback`、备份逻辑
- `rules/incremental.md`:增补"手改工作流"一节
- SKILL.md:命令速查加 `--init`

---

## 5. R5 · artboard 闭环:改完图 → 一键出新片

### 5.1 它真正要什么

用户原话:「海报接入的Skill artboard,要能够用户/AI手动修改之后能够用脚本触发导出→合成视频一条龙完成,快速高效」。

现状:`rules/artboard.md` 只说了"怎么用 artboard 脚本导出",**没有回填机制**——导出完还是 Agent 手动把 PNG/MP4 塞进 IR。用户改一张卡片,Agent 要重新找轨道、改路径、重算时间。

### 5.2 设计:manifest 作为唯一映射表

`03_assets/artboard/manifest.json`:

```json
{
  "version": 1,
  "items": [
    {"id": "card_definition",
     "project": "03_assets/artboard/card_definition/src/index.html",
     "source_hash": "ab31...",
     "output": "03_assets/artboard/card_definition/export/card_definition.png",
     "kind": "png", "size": [1080, 1920],
     "usedIn": [{"track": 1, "clipIndex": 0, "startMs": 12000, "durationMs": 3000}]}
  ]
}
```

- **`source_hash`** —— artboard 工程的源码 hash。改没改图,**比 hash 就知道**,不用 Agent 看图;
- **`usedIn`** —— 产物在 IR 里挂在哪。闭环的最后一公里;
- **`size`** —— 导出尺寸必须与 IR canvas 匹配(9:16 1080×1920 / 16:9 1920×1080),不匹配就报错而不是拉伸。

### 5.3 一键脚本 `rs_artboard.py`

```powershell
# ① 导出:只重导出源码变了的卡片(内容寻址,秒级)
python skills/cutflow/scripts/rs_artboard.py 03_assets/artboard/manifest.json --export

# ② 应用:把新产物写回 IR 的对应 overlay clip(尺寸/时长校验)
python skills/cutflow/scripts/rs_artboard.py 03_assets/artboard/manifest.json --apply 05_ir/project.json

# ③ 一条龙:导出 + 应用 + 从 S4 起重跑 + 出片(就是 rebuild.py 干的事)
python 03_assets/artboard/rebuild.py
```

`03_assets/artboard/rebuild.py` 生成内容 = `rs_artboard --export --apply` + `rs_run --from S4 --force`。

### 5.4 最麻烦的一点:时长变了怎么办

动画卡从 3s 改成 5s → 后面所有 clip 的 `startMs` 都要平移 → 字幕/音效/Logo 全部要重算。

**不硬扛,交给已有的机制**:`--apply` 发现卡片时长变化时,**重算 IR 主轨起点并标记 S4/S5/S6/S7 stale**,由 `rs_run` 级联重跑。因为字幕时间来自 Wordline(`map_src_to_final`),而 Wordline 不受卡片时长影响 → **字幕时间自动正确**,只需重渲染,不需要重对齐。

> 这正是 v4 里"单一时间源"设计开始还利息的地方。

### 5.5 陷阱

| 陷阱 | 后果 | 对策 |
|---|---|---|
| 导出尺寸与画幅不符 | 画面拉伸/黑边 | manifest 记 size,`--apply` 校验,不符即停 |
| artboard 技能缺失/路径变 | 脚本炸在中间 | 起手 preflight,给人话错误 + config 指引 |
| 重导出全部卡片 | 慢 | 按 `source_hash` 内容寻址,只导变了的 |
| 时长变更未传播 | 后续时间全错 | `--apply` 检测时长差异 → 标记下游 stale |
| 用户直接改图片而不改工程 | hash 不变,改动被忽略 | 生成物与工程同时 hash;或提供 `--apply --assume-changed` |

---

## 6. CLI 变更汇总

| 命令 | 语义 | 新增/变更 |
|---|---|---|
| `tools/fun_asr.py <媒体> --json` | **自带 ASR**,后端自动选择 | ★ 新增 |
| `tools/fetch_deps.py asr [--seed-models D] [--pkg\|--onnx]` | ASR venv + 依赖 + 模型播种 | ★ 新增 |
| `rs_align.py build --media X` | 改为调 `tools/fun_asr.py`(不再需要服务器) | ← 变更 |
| `rs_verify.py <工程> [--level L0\|L1] [--json]` | 分级自检,输出报告 | ★ 新增 |
| `rs_run.py --verify [--full]` | 按状态决定跑哪级 | ★ 新增 |
| `rs_run.py --init` | 在阶段文件夹生成 `rebuild.py` | ★ 新增 |
| `rs_run.py --from S7 --force` | 强制重跑该阶段,上游走缓存 | ★ 新增 |
| `rs_run.py --rollback [--at T]` | 还原最近一次备份 | ★ 新增 |
| `rs_artboard.py <manifest> --export\|--apply` | artboard 导出与应用闭环 | ★ 新增 |

---

## 7. 数据契约新增

```
<工程>/
├── _state/verify.json              ★ 验证状态(首次/L0/人工)
├── _state/backup/<时间戳>/         ★ 手改前的备份
├── 03_assets/artboard/manifest.json ★ artboard 工程↔产物↔IR 挂点
├── 04_cut/rebuild.py               ★ 生成物
├── 05_ir/rebuild.py                ★ 生成物
├── 06_output/rebuild.py            ★ 生成物
└── rebuild.py                      ★ 全量重建
```

---

## 8. 落地批次与验收

| 批次 | 内容 | 验收线 |
|---|---|---|
| **A · 自带 ASR** | `fun_asr.py` + `asr_vendor/` + `fetch_deps asr` + `rs_align` 改造 | 不启动任何外部服务,`fun_asr.py <视频>` 直接出文本;`pkg` 后端出字级 timestamp,`onnx` 后端明确标 `degraded` |
| **B · 验证分级** | `rs_verify.py` + `_state/verify.json` + `rs_run --verify` | 首次跑 L1 后有收敛循环;改字幕重跑仅 L0 且**输出声明验证级别**;不存在静默跳过 |
| **C · 省 Token** | SKILL.md 路由表 + 谁来做表 + 体量纪律 | SKILL.md ≤260 行;路由表覆盖全部 rules(单测);单次任务最多读 1 个规则文件 |
| **D · 一键重建** | `rs_run --init/--force/--rollback` + 备份与校验 | 手改 `subtitles.ass` → 点 `06_output/rebuild.py` → 备份存在 → 校验 → 出片;故意写坏一行则**停住并给出可读错误** |
| **E · artboard 闭环** | `rs_artboard.py` + manifest + `03_assets/artboard/rebuild.py` | 改 artboard 源码 → 点脚本 → 只重导该卡片 → IR 更新 → 出片;尺寸不符时报错不拉伸 |

**依赖关系**:A 独立;B 依赖 A(要先有能跑的产出);C 是文档层,可并行;D 依赖 B(要输出验证级别);E 依赖 D(`rebuild.py` 机制)。

---

## 9. 风险与反模式

| 风险 | 影响 | 对策 |
|---|---|---|
| ONNX 后端被当成有字级时间 | Wordline 永久降级而无人知 | 能力矩阵进返回体 + SKILL 明写 + `rs_verify` 检查 `degraded` 标志 |
| 「不检查」被实现成「静默不检查」 | 交付劣质成片 | 输出必须带 `verifyLevel`;缺失即未验证 |
| 一键脚本覆盖手改 | 用户数据丢失 | 先备份 + `--rollback` |
| 备份无限增长 | 磁盘被吃 | 保留最近 5 次,`rs_cleanup` 一并清理 |
| 路由表形同虚设 | 又回到"读一大堆" | 单测强制覆盖 + SKILL 硬规则 |
| vendored 代码与上游脱节 | 修不了上游 bug | NOTICE 记录出处与版本;`--update-vendor` 提示手动同步 |

**写进 SKILL.md 的反模式**:

- ❌ 不要在能用脚本的地方让 Agent 代劳(逐字校对除外)
- ❌ 不要一次读两个以上规则文件
- ❌ 不要在没有备份的情况下跑 `rebuild.py`
- ❌ 不要在 `verifyLevel` 缺失时宣称"完成"
- ❌ 不要把 ONNX 后端的产出当字级对齐结果用

---

## 10. 参考与依据

- 本机实测:onnxruntime 1.28 读取 `paraformer-large/model_quant.onnx` 输出签名(§1.2)
- FunASR ONNX 导出(`funasr_onnx` / FuASR, MIT License)— <https://github.com/alibaba-damo-academy/FunASR>
- MomentShift `core/funasr/`(FuASR 裁剪版, MIT)— 本机 `E:\平日资料\GitHub\MomentShift`
- `docs/OPTIMIZATION-v4.md`(v4 基线)、`docs/adr/0011~0014`

---

## 11. 实施中发现并修正的设计问题(写文档时没想到的)

> 这一节是给未来的自己看的:**下面每一条都是"纸面设计看起来没问题、一跑就露馅"的典型。**

### 11.1 没有「烧录导出」阶段 → 手改字幕会被冲掉(最严重)

原设计里 `06_output/rebuild.py` 从 **S7(字幕生成)** 起跑。而 S7 会**重新生成 `subtitles.ass`** —— 用户手改的字幕直接没了。

**修正**:新增独立阶段 **S8「烧录导出」**(`rs_render`,只用现有 ass 重新烧录编码),阶段表扩为 **S0–S11**。`06_output/rebuild.py` 从 S8 起跑。并加测试锁死:`S8 的 cmd 必须是 rs_render,且不得含 rs_subtitle`。

> 教训:**"哪个脚本会覆盖用户改过的东西"必须在设计阶段就逐阶段问一遍。** 备份能兜住,但兜不如不覆盖。

### 11.2 `missing` 被当成"画面变更" → 永远触发 L1

`verify_policy` 最初把"S3/S4/S5 状态 != done"都算作画面变更。结果:工程里从没跑过的阶段(状态 `missing`)也被判为"变了",`--verify` 永远走 L1。

**修正**:只有 **"做过且失效"(`stale`/`failed`)** 才算变更;`missing` 不算。并为"两套真相"(rs_run 的 live 求值 vs pipeline.json 快照)建立单一实现:`rs_verify.picture_changed()` 委托给 `rs_run`,不再各写一份。

### 11.3 VAD 默认阈值太高 → 段长 15s+,段内均分误差大

模型默认 `max_end_silence_time=800ms`,68s 口播只切出 **5 段**,中位时长 **15.9s**。没有字级时间戳时,段内只能均分 → 15s 的均分误差很可观。

**修正**:默认收紧到 **400ms** → **27 段 / 中位 2.1s**,接近句级。实测 300ms 收益递减(36 段 / 1.9s),且可能切在换气处。VAD 只占总耗时 7%,这个改善几乎免费。

> 教训:**"模型默认值"是给通用场景的,不一定是给你的场景的。** 实测一次就能拿到 7 倍的分段粒度。

### 11.4 已确认但**未解决**的局限:无字级时间戳时的跨词硬切

实测输出里出现了 `再加上一 / 点耐心` 这类切法:VAD 段内没有标点,DP 在字数上限处硬切。**靠堆断句规则解决不了**(没有候选边界可用)。

**正解是字级时间戳**(`pkg` 后端)。已记入 BACKLOG,并在 ADR-0015 里写明这条局限。

### 11.5 缓存键的自我实现陷阱

`evaluate()` 最初把**记录里的 params/external** 回传给 `stage_parts()`,于是这两项永远与自身相等 —— 只有 inputs/tool 真正参与比对。这不是 bug(参数快照本来就该冻结),但会让人误以为"参数变了也会 stale"。**修正**:在 `rules/incremental.md` 写明 `params` 是**快照**,改参数要显式 `--force` 或改 pipeline.json。

### 11.6 阶段表变更的连锁成本

加一个 S8 就要改:阶段表、SKILL.md、`INIT_MAP`、测试里的 `range(11)→range(12)`、verify 的阶段集合。**这印证了 v4 里定的"阶段边界按可独立验证划分,上限 12 个"是有代价的约束** —— 不加必要阶段会出 11.1 那种事故,加多了维护成本上升。当前 12 段是合理的。

