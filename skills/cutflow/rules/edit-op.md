# EditOp 语法手册 — 声明式编辑层 `rs_edit`(ADR-0048)

> 面向 Agent 的改片语义与机械契约。**执行器是 `rs_edit.py`,本册是它的唯一语法手册。**
> 原则:**Agent 只做语义工作**(读懂人话 → 写 ops.json);寻址/白名单/值域/帧网格/
> 冲突/留痕全部由脚本机械保证。改片**零渲染**——渲染只在用户说「出片」时发生。

---

## 1. 三层职责与快速上手

| 层 | 子命令 | 输入 | 输出 | 是否落盘/渲染 |
|---|---|---|---|---|
| 投影 | `context` | 工程 | 结构化时间线视图(Markdown,≤12KB;`--json` 出协议 JSON) | ❌ |
| 校验 | `ops-validate` | ops.json | 校验报告(不落盘、不渲染) | ❌ |
| 应用 | `apply --dry-run` | ops.json | **人话差异表** | ❌ |
| 应用 | `apply` | ops.json | IR 变更 + OpLog + 重建建议 | 只写真相源,**零渲染** |
| 撤销 | `undo --last N` | — | OpLog 逆写回 before + 撤销留痕 | 只写真相源 |
| 对比 | `diff --rev a --rev b` | — | 两 rev 的人话差异 | ❌ |

完整链路(验收线 §2.3-B / M4):

```
python skills/cutflow/scripts/rs_edit.py context <工程>
#   ↑ 读 12KB 文本视图,拿到 rev(=baseRev)与 clipId;不抽帧
# ② Agent 语义解析人话 → 写 ops.json(格式见 §2)
python skills/cutflow/scripts/rs_edit.py ops-validate <ops.json>
python skills/cutflow/scripts/rs_edit.py apply <工程> --ops <ops.json> --dry-run
#   ↑ 打印人话差异表,不写盘;给用户确认
python skills/cutflow/scripts/rs_edit.py apply <工程> --ops <ops.json>
#   ↑ 写 IR + OpLog + 输出最小重建建议
python skills/cutflow/scripts/rs_edit.py undo <工程> --last 3      # 反悔
python skills/cutflow/scripts/rs_edit.py diff <工程> --rev 1 --rev 2
```

---

## 2. ops.json 与 EditOp 文法

ops.json 是**包装对象**(推荐,带 baseRev)或裸数组(仅 ops-validate 方便;apply 必须有 baseRev):

```json
{
  "baseRev": 42,
  "requestId": "req-20260925-1",
  "ops": [
    {"op": "clip.trim", "target": "V1-003",
     "after": {"durationMs": 2600},
     "reason": "第 3 段剪短一点", "source": "user"}
  ]
}
```

| 键 | 必填 | 说明 |
|---|---|---|
| `op` | ✅ | 34 个 op 名之一(§4 全表:v1 24 + v2 新增 10) |
| `target` | ✅ | 稳定引用:clipId / `bgm` / `output` / `protect` / `clipA\|clipB` / `t<毫秒>` 锚点 |
| `after` | 视 op | 目标态;**只允许 §4 表内字段**,越界 `BAD_FIELD`(退出码 2) |
| `reason` | ✅ | 人话意图摘要,原样进 OpLog `summary`,`rs_oplog.py report` 直接可读 |
| `source` | ⬜ | `user` / `agent` / `inferred`,缺省 `agent` |
| `before` | ❌ | **由 apply 回填**,Agent 不写(写了会被忽略;留键位是给人工读的) |
| `requestId` | ⬜ | 幂等重放去重键:同 requestId 的重放不会被判为人侧冲突 |

包装层 `baseRev` 必填(或 `--base-rev N`):apply 前置校验,不匹配即
`PRECONDITION_FAILED`(退出码 2),**要求重新 context**——这是防「最后写入者获胜」
的硬闸。`requestId` 用于同一份 ops 重放(幂等验证)时不把自己的旧 Op 当人侧冲突。

**寻址纪律(P22-1)**:禁止数组下标(`clips[3]` → `BAD_ADDRESS`,退出码 2)。
一律用 context 视图第一列的稳定 clipId:原生 `id`(如 `V1-003`)优先;旧 IR 无 id 时
视图给内容寻址回退 id(`cf-<sha1 前 12 位>`,与 rs_editor 桥同机制)。
⚠️ 回退 id 以片段内容为身份:该片段被改后旧 id 失效(按孤儿处理,不会指错)——
连续多轮改同一片段请用原生 id,或每轮重新 context。

---

## 3. apply 的七步(对齐 cutforge `Workspace::apply`)

```
1 校验前置      工程存在、IR 可解析、fps 可读(帧网格基准)
2 取工程锁      .cutforge/edit.lock;被占 → LOCKED(退出码 4,含 pid 与过期接管提示)
               锁持者进程已死或超 300s = 过期,--force 显式接管
3 baseRev 前置  盘面 rev 与声明不符时先做冲突甄别(见下);无冲突 → PRECONDITION_FAILED
4 逐 op 校验    op 名/寻址/白名单/值域/U7 → IR 依赖检查(目标存在/相邻/区间合法)
               → 回填 before;after == before → 幂等短路(不产 Op)
5 冲突即停     与人侧 OpLog 比对(窗口 = rev > baseRev 的既有 Op,任何 actor):
               目标指针前缀相含 → CF-001/CF-002,停止写入(退出码 4)
6 原子写       IR 临时文件+os.replace → 文件级真相源(notes/cutlist)→ 追加 OpLog
               → .cutforge/rev 升号 → bases/<新rev>.json 快照
7 脏传播建议    按改动类输出最小重建建议(§9 映射表);缓存键含上游 hash,失配自动发生
```

**baseRev 与冲突的关系**(第 3/5 步合起来读):baseRev 匹配 → 直接进第 5 步(窗口空,
必无冲突);baseRev 落后 → 窗口里人侧动过的**同一目标**报 CF(停止写入),没撞目标才
按「视图过期」报 PRECONDITION_FAILED。全幂等的重放(baseRev 旧但每条 op 都
after==before)允许短路通过——重放无副作用。

**幂等语义**:同一条 op 重复 apply → `after == before` 短路,rev 不变、OpLog 不增。
插入类 op(add/note.add)没有可比较的旧值,其幂等性由 baseRev 前置 + requestId
重放去重保证——不会重复插入。

---

## 4. EditOp 全表(34 op)

`after` 白名单列 = OP_AFTER 表(脚本与手册同源,漂移即门禁红)。
「写到的 schema 字段」= 映射落点,**全部在 project.schema.json 的 clip/bgm/outputs
白名单内**(v2 M14 起含双仓同步新增的 `fx` / `huazi` / `font` / `assetId` / `motion.inFx`
等字段);ms 类时间写入前吸附帧网格(fps 来自 IR,±0.5ms)。

### 4.1 既有 op(24 支;★ = v2 扩展字段)

| op | target 文法 | after 白名单(值域) | 写到的 schema 字段 | 脏传播 |
|---|---|---|---|---|
| `clip.trim` | clipId | `startMs`(≥0) / `durationMs` (>0) / `sourceInMs`(≥0) | 同名平铺字段 | S3→S8 |
| `clip.move` | clipId | `startMs`(≥0);`ripple`(bool,连带重排同轨后续) | `startMs` | S3→S8 |
| `clip.split` | clipId | `atMs`(须在片段窗内) | 左段 durationMs;右段 startMs/durationMs/sourceInMs(+δ) | S3→S8 |
| `clip.delete` | clipId | —(禁 after) | 删除片段 | S3→S8 |
| `clip.speed` | clipId | `rate` ∈ [0.25, 4] | `speed` | S3→S8 |
| `clip.reframe` | clipId | `anchorY` ∈ [0,1] / `scale` ∈ [0.05,4] | `reframe.anchorY` / `scale` | S3→S8 |
| `clip.motion` | clipId | `in` / `inMs`(≥0) / `out` / `outMs`(≥0),枚举见 schema;★ `inFx` / `outFx`(fxId,v2 §4.1 扩展,声明时优先于枚举) | `motion.*`(含 `motion.inFx` / `motion.outFx`) | S3→S8 |
| `transition.set` | `clipA\|clipB`(须相邻) | `kind`(§7 别名表) / `durMs` (>0);★ `fx`(fxId;与 kind 并存时渲染以 fx 为准并 WARN) | 后段的 `transition.type` / `transition.durMs` / `transition.fx` | S3→S8 |
| `overlay.add` | 任意(挂覆盖轨) | `card` ⊕ `element`(v2:素材库元素,与卡互斥) / `startMs`(≥0) / `durationMs` (>0) / `motion`(对象;元素路径只承载 `{fx}`) | 覆盖轨新 clip:卡经 artboard manifest;元素经素材库 manifest(src+assetId) | S4→S8 |
| `overlay.remove` | clipId(覆盖轨卡) | —(禁 after) | 删除片段 | S3/S4→S8 |
| `sfx.add` | `t<毫秒>` 锚点 | `assetId`(v2 口径,素材库 id) ⊕ `name`(兼容别名;两者并存以 assetId 为准并 WARN) / `gainDb` ∈ [-60,0] | 音频轨新 clip(role=sfx;assetId → `src`+`assetId`,时长取 manifest 实测) | S6→S8 |
| `sfx.remove` | clipId(role=sfx) | —(禁 after) | 删除片段 | S3/S6→S8 |
| `bgm.set` | `bgm` | `src`(工程内须存在) ⊕ `assetId`(v2;并存以 assetId 为准并 WARN) / `gainDb` ∈ [-60,0] / `ducking`(bool) | 顶层 `bgm.src/assetId/gainDb/ducking` | S6→S8 |
| `audio.gain` | clipId | `gainDb` ∈ [-60,0] | `volume`(线性,10^(dB/20),6 位截断) | S6→S8 |
| `freeze.set` | clipId | `freezeMs`(≥1) | `freezeMs` | S3→S8 |
| `subtitle.set` | clipId(字幕轨) | `text`(非空) / `huazi`(v2:`{template, params?}`,挂花字) | `text` / `huazi` | S7→S8 |
| `subtitle.retime` | clipId(字幕轨) | `startMs`(≥0) / `endMs`(→durationMs=end−start) | `startMs` / `durationMs` | S7→S8 |
| `subtitle.highlight` | — | **整支不承诺**(U7,见 §6) | — | — |
| `segment.protect` | `protect` | `startMs` / `endMs`(start<end) / `note` | **cutlist.json** 的 `protect[]`(文件级,不进 project.json) | S2 |
| `keyframe.set` | — | **整支不承诺**(U7,见 §6) | — | — |
| `output.set` | `output` | `ratios[]` ⊆ 9x16/3x4/16x9 | 顶层 `outputs` | 参数源 |
| `note.add` | `t<毫秒>` / clipId / `clipId@<毫秒>` | `text` / `author`(user\|agent) | **notes.json** 新 item(文件级,不进 project.json) | — |
| `style.pacing` | — | **整支不承诺**(U7,见 §6) | — | — |
| `beat.snap` | clipId | `windowMs` ∈ [1,500](缺省 60) | `startMs`(吸附到最近节拍) | S3→S8 |

### 4.2 v2 新增 op(10 支,分册04 §4.2;M14 落地)

| op | target 文法 | after 白名单(值域) | 写到的 schema 字段 | 脏传播 |
|---|---|---|---|---|
| `fx.apply` | clipId | `slot` ∈ in\|out\|combo(必填) / `fx`(fxId,必填) / `params`(对象) | `clip.fx[slot] = {fx, params?}` | S3→S8 |
| `fx.clear` | clipId | `slot`(必填);原无 → 幂等短路 | 删 `clip.fx[slot]`(容器清空即摘除) | S3→S8 |
| `element.add` | 覆盖轨 trackId 或 `overlay`(自动建;禁主轨) | `element`(素材 id,必填) / `startMs`(≥0) / `durationMs` (>0) / `x`,`y` ∈ [0,1](归一化中心点) / `w`,`h`(像素,>0) / `opacity` ∈ [0,1] / `motion{fx}` | 覆盖轨新 clip:`src`+`assetId`(manifest 解析)+`position`/`overlay`/`scale`(几何,见下)+`opacity`+`motion.inFx` | S4→S8 |
| `element.remove` | clipId(元素段,须有 assetId) | —(禁 after) | 删除片段 | S4→S8 |
| `element.retime` | clipId(元素段) | `startMs`(≥0) / `durationMs` (>0) 至少一个 | `startMs` / `durationMs` | S4→S8 |
| `huazi.set` | clipId(字幕轨) | `template`(huazi.* 必填) / `params`(对象) | `clip.huazi = {template, params?}` | S7→S8 |
| `huazi.clear` | clipId | —(禁 after);原无 → 幂等短路 | 删 `clip.huazi` | S7→S8 |
| `font.set` | `project` 或 clipId | `family`(必填) / `scope` ∈ project\|clip\|style(与 target 一致;`style` → OP_UNSUPPORTED,见 §6) | `font.family`(顶层)/ `clip.font.family` | clip→S7;project→全链(OUT) |
| `asset.swap` | clipId(role=sfx 的音频段)或 `bgm` | `assetId`(必填;kind 按被换对象判定并校验一致) | 该 clip 的 `src`+`assetId` / `bgm.src`+`bgm.assetId`(**时间与参数原样保留**) | S6→S8 |
| `effect.glsl.enable` | `project` | `on`(bool,必填) | 顶层 `effects.glsl`(false = T2 全部降级最接近的 T1 近似,渲染端留痕) | S8 |

**element.add 几何语义**(人话「加个箭头指向那里 --x 0.6 --y 0.3」):`x`/`y` 是
**归一化中心点** → `clip.position`;`w`/`h` 给出时 → `clip.overlay` 绝对落点
(以 x/y 为中心折算);都不给 → `scale=0.2` 兜底(元素是贴图,绝不整幅铺满)。

**素材 id 的解析与校验**(element/sfx/bgm/swap 共用):apply 时对
`skills/cutflow/assets/manifest.json`(M12 产物)按 id 查条目 —— manifest 缺失 →
`DEP_MISSING`(退出码 3,先跑素材库入库 `rs_asset.py`);id 不存在 / kind 不符 /
`commercial: false` / 文件缺失 → `BAD_VALUE`(不可商用素材不得入轨,绝不静默)。
id 检索用素材库 `rs_asset.py`(M12 提供的检索入口;此处不以子命令形态书写,免对拍门禁空跑)。

**值域速查**:`rate` 0.25–4 · `gainDb` −60–0 · `scale` 0.05–4 · `anchorY` 0–1 ·
`windowMs` 1–500 · `x`/`y` 0–1(归一化中心点)· `opacity` 0–1 · `w`/`h` > 0(像素)·
所有 `*Ms` ≥ 0(时长类 > 0)。枚举(`motion.in/out`、转场 kind、`slot`、ratios)
与 schema 逐字一致。

**beat.snap 语义**:读 04_粗剪决策/beats.json(M8 混剪能力产物);缺文件报
`BEATS_MISSING`(退出码 3)——**绝不伪造吸附**。最近节拍超出吸附窗 → 不动 + WARN
(禁强制吸附),该 op 幂等无 Op。

**transition.set 的 kind 值域**:schema 枚举 `fade / wipeleft / wipeup / slideleft /
circleopen / cut / none` 直写;别名 `dissolve`→`fade`、`slide`→`slideleft`;
`match` 不在 schema 枚举 → `OP_UNSUPPORTED`。

---

## 5. 人话 → op 映射(Agent 的语义参考)

| 人话 | op |
|---|---|
| 剪短一点 / 剪掉这段 | `clip.trim` / `clip.delete` |
| 从这切一刀 | `clip.split` |
| 挪到前面 | `clip.move` |
| 快点 / 慢放 | `clip.speed` |
| 这里加个转场 | `transition.set` |
| 这里来个甩镜转场 | `transition.set --fx tr.whip.pan`(v2) |
| 画面偏了 / 把人放中间 | `clip.reframe` |
| 这里加个卡片 | `overlay.add` |
| 加个箭头指向那里 | `element.add --element element.arrow.right --x 0.6 --y 0.3`(v2) |
| 这块加个放大入场 | `fx.apply --slot in --fx fx.in.zoom.slight`(v2) |
| 这句话做成花字 | `huazi.set --template huazi.keyword.box`(v2) |
| 换个字体 | `font.set --family smiley-sans --scope project`(v2) |
| 背景音乐换成紧张的 | `bgm.set --assetId bgm.tension.120`(v2) |
| 这个音效换掉 | `asset.swap --assetId sfx.impact.02`(v2) |
| 别用 GLSL,怕慢 | `effect.glsl.enable --on false`(v2) |
| 配乐换掉 / 音乐小一点 | `bgm.set` / `audio.gain` |
| 这句字幕再留久点 | `subtitle.retime` |
| 这个词不能切 | `segment.protect` |
| 卡在鼓点上 | `beat.snap` |
| 出个竖版 | `output.set` |
| 这里标一下 | `note.add` |
| 冻结一下 / 定格 | `freeze.set` |
| 入场淡入 / 出场滑走 | `clip.motion` |

---

## 6. 字段覆盖表(U7 核查结论,诚实条款)

核查依据:CutFlow `skills/cutflow/templates/project.schema.json` 全文 × cutforge
`schemas/project.schema.json` v2 的 `$defs/clip`(两者交集即 rs_edit 可写字段;
cutforge 侧 `additionalProperties: false`,写交集之外的字段会让编辑器拒开工程)。
**v2 M14 起两份 schema 双仓同步新增**:`clip.fx` / `clip.huazi` / `clip.font` /
`clip.assetId` / `motion.inFx` / `motion.outFx` / `transition.fx` / `bgm.assetId` /
顶层 `font` / 顶层 `effects`(cutforge 侧已 `tools/schema_gen.py` 再生成校验器)。

**全量支持(31 op)**:§4 表全部除下列「显式不承诺」者。其中带**映射**的:
`clip.speed.rate`→`speed`、`audio.gain/sfx.add.gainDb`→`volume`(线性换算)、
`transition.set.kind`→`transition.type`(别名表)、`subtitle.retime.endMs`→`durationMs`、
`output.set.ratios`→`outputs`、`overlay.add.card`→覆盖轨 clip 的 `src`(经 manifest)、
`overlay.add.element`/`element.add.element`→覆盖轨 clip 的 `src`+`assetId`(经素材库 manifest)、
`sfx.add/bgm.set/asset.swap 的 assetId`→`src`+`assetId`、`effect.glsl.enable.on`→`effects.glsl`。

**v2 新增字段的下游消费(诚实声明,ADR-0055「警告不算实现」的边界)**:

| 字段 | 消费方 | 状态 |
|---|---|---|
| `transition.fx` | rs_render `_resolve_transitions`:既有 5 种转场 + `tr.cut` 有 `tr.*` 别名**直通生效**;未注册 fxId 回退 `type`(缺省 fade)+ WARN + `fxDegraded` 留痕 | M14 部分生效,M13 注册表全量 |
| `motion.inFx` / `outFx`、`clip.fx` | rs_render:未注册 fxId → 该段按无特效渲染 + WARN + `fxDegraded` 留痕(绝不静默) | 契约 M14 落,渲染 M13 注册表消费 |
| `clip.huazi` / `clip.font` / 顶层 `font` | S7 `rs_subtitle`(字幕/字体真相源;`--huazi` 链) | 契约 M14 落,S7 消费随素材库里程碑接线 |
| `assetId` / `bgm.assetId` | rs_edit(换素材锚/溯源);渲染仍由 `src` 驱动 | M14 生效 |
| 顶层 `effects.glsl` | T2 GLSL 渲染开关(渲染器本体 M13/B3) | 契约 M14 落,M13 消费 |

**显式不承诺——整支 op(3 支,报 `OP_UNSUPPORTED`,退出码 2)**:

| op | 原因 |
|---|---|
| `keyframe.set` | 两份 schema 均无 keyframe 承载字段(§9.1 U7) |
| `subtitle.highlight` | clip 无高亮词字段(ASS 富文本 span 待契约升版,清欠账 #12) |
| `style.pacing` | project.json 无 pacing 字段;节奏档归 M6 风格包参数源,届时补承诺 |

**显式不承诺——字段级(8 个,报 `OP_UNSUPPORTED`)**:
`clip.speed.keepPitch`、`clip.reframe.anchorX`(schema reframe 只有 anchorY)、
`bgm.set.fadeInMs / fadeOutMs`(schema bgm 对象无此二字段)、
`audio.gain.leadMs / lagMs`(J/L-cut 无承载字段)、
`output.set.logos`(logo 归 S5 品牌矩阵)、`output.set.profiles`;
**v2 追加**:`font.set.scope=style`(project.json 无字幕样式容器,样式归 S7 rs_subtitle 契约)。

**纪律**:schema 未覆盖 = 显式拒绝,**绝不静默写 schema 外字段**(五份 schema
`additionalProperties:false` 的端会直接拒载)。扩承诺的路径是先升 schema 契约
(ADR-0034;v2 起两仓同步),再改 rs_edit 的 OP_AFTER/UNSUPPORTED 表并更新本表。

**已知边界(如实声明,非缺陷)**:
- `subtitle.retime` 的 release-margin(起点≤首字、终点≥末字、延长≤+0.30s)由
  S7/S8 重建链与 rs_verify 把守;rs_edit 只做值域与帧网格,不重复实现
  wordline 空间换算。
- `sfx.add` 单点挂载;「≤2 个/15s」密度闸由 rs_sfx 的 `--auto` 档统一裁决。
- 字幕 op 作用于 IR 的 text 轨卡;纯 wordline/ASS 工程(无 text 轨)改卡走
  S7 重建,rs_edit 会报 `BAD_ADDRESS` 说明原因。

---

## 7. 通用约束(所有 op,机械把守)

1. **禁止数组下标寻址**:`"target": "clips[3]"` → `BAD_ADDRESS` 退出码 2;
2. **after 只允许 §4 表字段**:越界 → `BAD_FIELD` 退出码 2(U7 字段优先报 `OP_UNSUPPORTED`);
3. **值域校验**:出界 → `BAD_VALUE` 退出码 2;
4. **帧网格**:所有时间写入吸附到 fps 帧网格(±0.5ms,毫秒取整);
5. **幂等**:`after == before` → 短路,不升 rev、不产 Op;
6. **文件级 Op**:`note.add` → notes.json;`segment.protect` → cutlist.json。
   **绝不写进 project.json**(cutforge 契约;undo 也逆写回各自真相源)。

---

## 8. 冲突码与错误码

**冲突(cutforge CF 码表;冲突 = 停止写入,禁止最后写入者获胜)**:

| 码 | 语义 | 触发 |
|---|---|---|
| `CF-001` | FieldConflict | 窗口内人侧 Op 与本次 op 指进同一子树(JSON Pointer 前缀相含) |
| `CF-002` | DeleteModify | 同上且对方是 delete 类 Op |

窗口 = OpLog 中 `rev > baseRev` 的既有 Op(人机同一条日志,任何 actor 都算;
同 `requestId` 的回声除外)。冲突时 apply 退出码 4,盘面**字节级不变**。

**错误码总表**:

| code | 退出码 | 场景 |
|---|---|---|
| `BAD_OP` / `BAD_ADDRESS` / `BAD_FIELD` / `BAD_VALUE` / `NOT_FOUND` | 2 | op 名/寻址/白名单/值域/目标不存在(请重新 context) |
| `OP_UNSUPPORTED` | 2 | U7:schema 未覆盖的 op/字段,本轮不承诺 |
| `PRECONDITION_FAILED` | 2 | 缺 baseRev / baseRev 过期(重跑 context)/ --ops 缺失 |
| `BEATS_MISSING` | 3 | beat.snap 依赖的 beats.json 缺失(先跑混剪) |
| `DEP_MISSING` | 3 | 缺 IR / 缺 artboard manifest / 缺 cutlist.json |
| `MISSING_SNAPSHOT` | 3 | diff 要的 rev 快照不在 bases/(被 LRU 淘汰) |
| `LOCKED` | 4 | 工程锁被占(含 pid;过期可 `--force` 接管) |
| `CF-001` / `CF-002` | 4 | 冲突即停 |
| `INTERNAL` | 4 | 解析失败等内部错误 |
| 成功码:`CONTEXT_OK` / `VALIDATED` / `DRY_RUN` / `APPLY_OK` / `IDEMPOTENT` / `UNDONE` / `DIFF_OK` | 0 | 幂等短路也是成功(IDEMPOTENT) |

---

## 9. 脏传播与最小重建建议(§5.3.5 映射表)

| 改动类 | 脏传播范围 | apply 输出的建议 |
|---|---|---|
| `clip.*` / `transition.set` / `beat.snap` / `freeze.set` | IR → S3/S8 | `python skills/cutflow/scripts/rs_run.py --from S3 --force` |
| `overlay.*` | S4 产物 | `python skills/cutflow/scripts/rs_run.py --from S4 --force` |
| `bgm.set` / `audio.gain` / `sfx.*` | S6/S8 | `python skills/cutflow/scripts/rs_run.py --only S6` 之后 `--only S8` |
| `subtitle.*` | S7 产物 | `python skills/cutflow/scripts/rs_run.py --from S7 --force` |
| `segment.protect` | 粗剪决策 | `python skills/cutflow/scripts/rs_cut.py <工程> --apply <cutlist>`(不重跑 detect) |
| `output.set` | 参数源 | `python skills/cutflow/scripts/rs_run.py --status` 后按提示 |

缓存键含上游 hash + 参数 + 脚本 hash,IR 变了缓存自动失配——rs_edit 只如实报建议,
不自维护失效逻辑。

---

## 10. OpLog 对齐(cutforge 协议)

- 落点 `.cutforge/oplog/<日>.jsonl`,append-only,按天切分;每条 Op 含
  `op_id`(`op-<n>` 全工程单调)/ `ts` / `actor{kind,id}`(agent 的 id 固定
  `cutflow-rs_edit`,--actor user 则 kind=user)/ `target{file,path}`(JSON Pointer)/
  `op_kind`(set/insert/delete/split/undo/redo)/ `before` / `after` / `base_rev`
  (`rev-N`)/ `rev` / `summary`(= op 的 reason)/ 可选 `request_id`。
- **键名口径**:以 cutforge-io 的**实际序列化**(蛇形 `op_id/op_kind/base_rev`)为准——
  cutforge 仓的 oplog.schema.json 写的是驼峰,与其 Rust 结构体的 serde 命名漂移;
  断键会让 cutforge 触发「半行恢复」把整天日志弃读,故宁从实现。该漂移属 cutforge
  侧债务,两仓对齐时以其修复方向为准。
- `rs_edit.py` 的 apply 与 undo 同时维护 `.cutforge/rev`(当前修订号)与
  `.cutforge/bases/<rev>.json` 快照(cutforge 三路合并的祖先链)。
- 审计:`python skills/cutflow/scripts/rs_oplog.py report <工程>` 按 actor 汇总,
  agent 行直接可读(「AI 改了什么」的答案);`rs_editor.py diff <工程>` 识别人侧改动。

---

## 11. undo / diff 语义

- `undo --last n`:n = **Op 条数**(人机同一条日志,一起逆)。取 OpLog 尾部 n 条,
  **逆序**按 before↔after 互换复原(set 按指针映射;insert 删除;delete 回插;
  split 两段合回原片段,值比对防错位);每逆一条以 `op_kind=undo` 的新 Op 留痕
  (撤销也进审计链);rev +1,新快照落 bases/。cutforge 侧 move/merge 形态暂不逆写,
  报 `INTERNAL` 说明。
- undo 恢复到**值级**状态;字节级复原以 rs_edit 写盘口径(dump_json,indent=1)为前提
  ——人手改过(编辑器 pretty 口径)后 undo 只保值不保排版。
- `diff --rev a --rev b`:数据源 = bases 快照;当前 rev 可直接读盘面。输出人话差异
  (复用 rs_editor.diff_tracks 的配对口径:按「内容身份」src+sourceInMs 配对)。

---

## 12. context 视图(投影层)

- 内容:头部(rev / fps / baseRev)→ 主轨 → 覆盖轨 → 音频轨 → 字幕摘要 → 保护区 →
  可执行手法清单 → **可用特效 → 可用素材**(v2 分册04 §4.4)→ 当前降级项。
  每行:clipId(可寻址)+ 时间窗 + 源/文本 + 当前参数 +
  **可改字段白名单**。**绝不含视频帧路径**(性能守门,`tests/test_perf_budget.py` 把守)。
  「可用特效」读效果目录 `templates/effects/catalog.json`(M13 产物,缺失时显示占位提示)
  的 status=可执行 条目;「可用素材」读 `skills/cutflow/assets/manifest.json`(M12 产物,
  缺失时显示占位提示)按 kind+usage 分组各列前 5 条 —— 两段均防御性读取,库未部署绝不崩 context。
- `--budget 12KB` 硬上限:超预算按「手法清单 → 降级项 → 字幕摘要 → 保护区 → 覆盖轨 →
  主轨行」顺序裁剪,并在「预算裁剪声明」节**列明被裁内容**(不静默截断)。
- `--scope clip|audio|subtitle|project` 只看一类;`--json` 出协议 JSON(markdown 全文
  在 `data.markdown`)。
- 「当前降级项」= capabilities_report 留账 + 已声明描述符;组件部署态经 rs_fetchable
  的 `state(component)` 查询(MISSING/FAILED 成行,READY 不列);取用系统不管的
  阶段内置型能力(仓内 detector 在)不制造噪声;rs_fetchable 不可用时如实标
  「已声明(部署态未知)」——rs_edit 不自己实现组件探测。

---

## 附:reason 的写法(v2 M13/ADR-0059,分册06 §5.2)

`rs_edit` 的 EditOp 强制 `reason` 字段;在 ADR-0059 下它**有评审效力**:

1. **每一处显式转场/效果必须带能一句话解释的 reason**("时间跳了三天" / "观众需要喘口气" / "章节切换")——
   rs_verify EFFECTS_UNMOTIVATED 对无 reason 的显式转场硬红;
2. `reason` 为**空或"好看"类词**(好看/更酷/炫/高级感/牛)视为未解释 → L1 打回、L0 红;
   解释不出来就**改硬切**;
3. 硬切(`transition.set kind=none/cut`)不需要理由 —— 无技巧转场是默认值,不是需要辩护的例外;
4. 风格包 `forbid` 的效果一律不得出现,无论 reason 多好(处方是门禁,不是建议)。

