# Compose — IR 与渲染

## IR 书写(templates/project.schema.json)

- 毫秒单位;canvas 1080x1920 或 1920x1080;fps 30 默认。
- tracks[0] 必须是主视频轨(顺序拼接);后续 video 轨是画中画/信息卡(支持 png/jpg 静图 + chroma 绿幕)。
- 音频轨 clip 用 role 区分 voice/sfx;bgm 顶层字段(ducking 自动闪避)。
- motion 枚举:fadeIn/fadeOut/slideInLeft/slideInRight/zoomIn(基轨 zoom 退化为 fadeIn 已警告)。
- transition 挂前一片段(fade/wipeleft/wipeup/slideleft/circleopen)。
- reframe.anchorY:比例转换时人物锚点(0=贴顶)。
- **`subtitle.source` 应指向 `05_ir/wordline.json`**(不再是句级 transcript);旧工程可指 manifest.json。
- **显式 Gap**:粗剪后 keep 区间之间的空隙用 `{"kind":"gap","durationMs":N}` 表达,不再靠"没有 clip"隐式表示(语义对齐 OTIO 的 Gaps / Filler)。

## IR 不再手写(关键改动)

主视频/音频轨由 **CutList 自动生成**,消除「Agent 手写毫秒」这一整类误差:

```
04_cut/cutlist.json ──► rs_ir.py build --from-cutlist ──► 05_ir/project.json(主轨)
                                                              │
Agent 只需补:overlay 卡片 / 字幕 / 音效 / Logo(全部从 Wordline 取时)
```

```powershell
python skills/cutflow/scripts/rs_ir.py build --from-cutlist 04_cut/cutlist.json `
    --slug 20260910-demo-口播 --ratio 9x16 --out 05_ir/project.json
```

- keep 区间 → `clips[]`,时间由 `map_src_to_final()` 换算(`sourceInMs` 仍指源素材位置);
- 切点处自动带 `transition` 或 8ms afade(由 `--xfade ms` 控制,默认 8)。

## 流程

1. `rs_ir.py validate` 全绿才渲染;
2. `rs_render.py <ir> --ratio 9x16 --profile final`(迭代期用 `--profile preview` 提速);
3. 双出:对 outputs 里每个比例各渲一次(第二比例记得带 reframe);
4. 多变体:见 rules/branding.md,`rs_brand.py` 在最后一步分叉,**共享中间件**。

## 中间件与增量(改写旧「人肉复用」)

**旧写法**:`06_output/_build/<ratio>/` 下 `seg_*/base/composed/mixed/subtitled` 可复用;改了字幕只重跑 step6-7(重调 rs_render 会全跑,**手改时可复用 mixed.mkv**)。

**新写法(ADR-0013)**:产物复用**由声明式缓存决定,不由人肉记忆决定**。

| 目录 | 对应阶段 | 增量粒度 |
|---|---|---|
| `_build/<ratio>/seg_*/base/` | S3 基础合成 | segment |
| `_build/<ratio>/seg_*/composed/` | S4 动画信息 | 卡片 |
| `_build/<ratio>/branded/<variantId>/` | S5 品牌 | 变体 |
| `_build/<ratio>/mixed/` | S6 音效 | 单条音效 |
| `_build/<ratio>/subtitled/` | S7 字幕 | 单卡 |

**操作前先问缓存,不要凭记忆**:

```powershell
python skills/cutflow/scripts/rs_run.py --status      # 哪几步 stale
python skills/cutflow/scripts/rs_run.py --only S7     # 只重做字幕(秒级)
python skills/cutflow/scripts/rs_run.py --explain S3  # 为什么 stale
```

**反模式**:直接重调 `rs_render.py` 会按当前 hash 全量重算——**想省时间就先 `--status` 再 `--from/--only`**。细节见 rules/incremental.md。

## 渲染后自检

`rs_doctor` 语义校验 + **`rs_sync.py` 三对齐断言** + `rs_bench.py <成片> --ir <ir> --out 06_output/bench_<ratio>.png` → 目测:

黑帧 / 绿幕残留 / 字幕压脸或出安全区 / 跳变 / 信息卡错位 / Logo 压字幕。修复 ≤3 轮,仍败上报。
