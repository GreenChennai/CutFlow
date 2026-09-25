# artboard 桥 — 图形素材(片头/片尾/封面/信息卡/小动画)

> **ADR-0017**。闭环目标:**改完图 → 一键出新片**,不需要 Agent 重新找轨道、改路径、算时间。

## 闭环三件套

```
artboard 工程(src/)  ──export──►  产物(png/mp4)  ──apply──►  IR overlay 轨  ──►  S4 起级联
        ▲                                        ▲
   source_hash 变了才重导                  尺寸/时长校验
```

清单 `03_创作素材/artboard/manifest.json` 是**唯一映射表**:工程 ↔ 产物 ↔ IR 挂点(`usedIn`)。

```powershell
python skills/cutflow/scripts/rs_artboard.py gen-cards --from 00_制作简报/cards.json --out 03_创作素材/artboard/manifest.json
# 或:手工 scaffold 的既有卡片工程用 --scan 登记
python skills/cutflow/scripts/rs_artboard.py --scan 03_创作素材/artboard --out 03_创作素材/artboard/manifest.json
python skills/cutflow/scripts/rs_artboard.py 03_创作素材/artboard/manifest.json --export
python skills/cutflow/scripts/rs_artboard.py export-fallback 03_创作素材/artboard/manifest.json   # 主引擎不可用时的兜底
python skills/cutflow/scripts/rs_artboard.py 03_创作素材/artboard/manifest.json --apply 05_时间线工程/project.json
python 03_创作素材/artboard/rebuild.py     # 一条龙:导出+回填+从 S4 级联
```

- **卡片计划 JSON(00_制作简报/cards.json)是 gen-cards 与 add-overlay 的共用输入**:内容字段
  (`id`/`template`(info|stat|section)/`title`/`lines`/`accent`/`kicker`)归 `gen-cards`,
  时间窗字段(`card|id`/`startMs`/`durationMs`/`motion`)归 `rs_ir.py add-overlay`,一份计划两处消费;
- **`--export` 按 `source_hash` 判断**,没改的卡片不重导(内容寻址);hash 与 manifest 的 `project` 字段统一按 **`<卡片>/src` 目录**算(v0.8.1;旧清单不带 `/src` 也兼容);
  **P6-2:产物不在盘也重导** —— skip 判定 = 「源码未变 **且** 产物存在」,首次 scan 后不会假 EXPORT_SKIP 漏导;
- **export-fallback(升格自 artboard 技能的 export_fallback.py)**:Playwright + 系统 Edge/Chrome
  截图导 PNG,与主引擎同一张清单、同一套产物落点,导完同样回写 sourceHash;
  用途:Kiln 主引擎不可用/导不出时;`--only id1,id2`、`--scale 2`、`--transparent` 可选;
- **尺寸不符直接报错、不拉伸**:导出尺寸必须等于画幅(9:16=1080×1920 / 16:9=1920×1080);
- **时长变化会传播**:动画卡改长了,`--apply` 自动平移后续 clip,并给出需重跑的下游阶段(时长变 → S4–S9;仅路径变 → S4/S5/S8);
- **未被 IR 引用的卡片(v0.8.1)**:默认**跳过+告警**(`APPLY_OK` 的 `skipped` 列表,不静默丢弃也不硬停);`--strict` 恢复硬失败;`--only id1,id2` 只校验选中的卡片——多变体/多场景不必再为每次 apply 拆 manifest。IR 里的挂点路径写**工程根相对**或绝对都行(按归一化绝对路径匹配);
- artboard 目录取自 `config.artboard_dir`;config 查找**仓库根优先、`skills/config.json` 兜底**(junction 安装下两者都能读到);缺失会给人话错误,不会半路炸。

## 何时用

- 片头/片尾板(静态 PNG 或 ≤15s MP4 动画)、封面、信息卡/数据卡、章节转场卡。
- 素材落 `03_创作素材/artboard/`,以静图或视频 clip 进 IR 的 overlay 轨。

## 调用(走 artboard 技能本体)

1. 预检:`python <artboard>/scripts/preflight.py`(ffmpeg 已回填,应无 WARN);
2. 脚手架:`scaffold.py <slug> --size <品类> --fonts <字体>`——**落盘位置由 `config.studio_dir` 决定,与 cwd 无关**;要落进当前工程目录用 `ARTBOARD_STUDIO` 环境变量指定(v0.12 补记);
3. 导出:`export.py --source <proj>/src --output <proj>/export/o.png --width 1080 --height 1920`
   (固定高度必须带 --height;动图 `--format MP4 --fps 25`,GIF `--format GIF`);
4. 9:16 用 1080x1920,16:9 用 1920x1080(slide 品类);
5. **导出 MP4 的环境变量:`WPI_FFMPEG` 才是有效项**(`ARTBOARD_FFMPEG` 无效——artboard 的 MP4 导出走 WPI;两个都设,rules/intake.md 环境 checklist 同款,安信德 #13)。

## 口播+动画挂轨:rs_ir add-overlay(T1-1 升格,原 _apply_overlay.py / 上一版 O2)

S4 的人工装配(手写 overlay 轨 JSON → apply → 记 manualEdit)**一条命令官方化**:

```powershell
python skills/cutflow/scripts/rs_ir.py add-overlay 05_时间线工程/project.json `
    --manifest 03_创作素材/artboard/manifest.json --plan 00_制作简报/cards.json
```

- `--plan` 时间窗表:`[{"card":"c01","startMs":3200,"durationMs":1800,"motion":{"in":"fadeIn","inMs":400}}]`
  (与 gen-cards 共用一份 cards.json;按 startMs 升序自动排;重叠、缺产物、白名单外字段一律硬失败);
- 自动:新增 `name=overlay` 的 video 轨(只写 schema 允许的字段,P8)→ 回写 manifest `usedIn`
  (track/clipIndex/startMs/durationMs)→ 标 IR `_meta.manualEdit`(S3 重建护栏认得这次手改);
- 同名轨已存在默认报错(防重复挂轨);确认整体替换加 `--replace`,或 `--track-name` 另挂一层;
- IR 若带 CutForge 编辑痕迹(`schemaVersion`)默认拒绝——编辑器工程走编辑器挂轨,显式 `--force` 才绕过。

## 场景卡 → 纯动画成片(I7,v0.12)

纯动画工程(pure-animation)的卡片定稿导出后,**不要手写 IR、不要每个工程重写组装脚本**:

```powershell
python skills/cutflow/scripts/rs_ir.py build --from-cards 03_创作素材/artboard/manifest.json `
    --anchors 00_制作简报/cards.json --wordline 05_时间线工程/wordline.json `
    --voice 03_创作素材/vo/voice.wav --slug <slug> --ratio 16x9 --out 05_时间线工程/project.json
```

- `--anchors` 分组表:`[{"card":"c01-x","match":"句首词|备选词"}]`,锚词**优先取句首词**;
- 卡片↔旁白 = 字符级锚点扫描(命中消费、标点继承、同卡相邻合并),组间边界 = 停顿中点;
- 卡比旁白短 → 自动写 `freezeMs`(冻结帧补长,出场动画前定格);
- 详见 ADR-0027 与 rules/video-types/纯动画.md。

## 设计约定

- 风格与视频风格 token 一致(看 brief 的 colors/typography);
- 动图约束:2-6s 无缝循环、仅 transform/opacity、终态须仍是合格静态海报;
- **单一外层统一出场、内层只挂入场**(安信德 #7):同一元素挂两个动画类(`.so` 出场 + `.count`/`.stamp` 入场)后者覆盖前者 → 元素不入场常驻 / 永不退场;计数器元素内容必须为空(由 JS 填充);
- **定宽网格先算总宽再定起点**(安信德 #8):如 4×380px + 3×34px 间距 = 1622px,画布 1920 → 起点 x=149(149+1622=1771 ✓);起点 x=320 会溢出切边(320+1622=1942 > 1920);
- **Mode S 录制时序**(ADR-0012,v0.12 补全):录制从页面加载 ~1.7–1.9s 才开始 → 入场动画 delay 必须从 `--t0: 2.0s` 起算;导出片长 ≈ 时间轴 −1.8s;单卡 `--max-wait` ≤15s;
- 产出回填 01_原始素材/MANIFEST.md。


## 视频卡安全区(ADR-0009,硬约束)

穿插动画卡(9:16 1080×1920)布局必须:
- 顶部 230px(12%)、底部 576px(30%,字幕带)、左右 118px(8%)之内不放任何内容;
- 核心内容置于垂直 250–1290px 中央带;装饰性元素(大数字/光斑)可越界但透明度低;
- 导出后自检:叠加 9:16 字幕带目测无叠压。

## 素材复用(用户额外素材一律走 artboard)

| 需求 | artboard 脚本 |
|---|---|
| 字体 | fetch_font.py(下载/安装字体到工程 fonts/) |
| 图片素材 | fetch_asset.py(图库 key 源/爬虫,爬虫图自动加 版权风险- 前缀) |
| 抠像 | cutout.py(rembg isnet 本地;--quality high 需联网下 BiRefNet) |
| 插画 | 插画包(open-doodles 等,见 assets) |
| 图标/二维码 | qr.py;图标走 fetch_asset iconfont 源 |
| VQA 读图 | scripts/vqa.py(config vqa_path) |

字数纪律:卡片文字 ≤3 组元素,单行 ≤14 字,停留 ≥1.5s/13 字符(MD3/legibility 规范)。

## 六类场景卡 gen-frames(M7,ADR-0017 扩展)

artboard「模式 S 视频场景卡」的 CutFlow 产品化:一条命令出**片头/片尾/标题/章节/数据/对比**卡,
生成 → 机检 → 导出 → 挂轨全链。

```powershell
python skills/cutflow/scripts/rs_artboard.py gen-frames <工程根> --kind opener,outro,title,section,stat,compare [--pack <slug>] [--ratio 9x16]
```

- **输入**:`00_制作简报/cards.json`(与 gen-cards/add-overlay 同一份),条目新增 `kind` 字段
  (`opener` 片头 1.5–3.0s / `outro` 片尾 2.0–4.0s(别名 `endcard`) / `title` 标题卡与解说句对齐 /
  `section` 章节卡 1.5–2.5s / `stat` 数据卡 ≤8s / `compare` 对比卡 ≤8s);
  `durationMs` 给出时按「导出片长 ≈ 持住 + 1.6s」反推持住并按档位夹限(夹限必 WARN);
- **五段式硬约束(违反即废,artboard animation.md §八/§十二)**:前置静置 ≥2.0s(`--t0`) /
  入场 0.5–0.8s decelerate / 持住(与解说句对齐) / 出场 0.4–0.6s accelerate / 收尾静置 ≥0.3s;
  **产物全 finite 禁 infinite**;**入场与出场分属两层嵌套元素**(外层 `.si` 只挂入场、内层 `.so`
  只挂出场;同元素双动画 = 出场 backwards fill 压住入场 → 画面从 0s 常驻);
  产物 `data-five-seg` 属性可供机检/测试断言;compare 卡 = §十「前后对比切换」(态 A 持住 45% 处
  0.3s 退场 → 态 B 入场,标签取 kicker 的「错/对」式写法);
- **安全区机检联动**:gen-frames 自动跑 artboard `check_overflow.py <卡>/src --safe-area <画幅>`
  (9x16 / 3x4 / 16x9;9:16 = 左右 184px / 顶 230px / 底 576px 标准档),
  **`ok:false` 即整条命令失败**(SAFE_CHECK_FAILED),不导出、不登记 manifest;
- **导出**:`export.py --format MP4 --fps 25 --max-wait <五段总和+1.5s>`;导出成功回写 sourceHash,
  失败时 HTML 与机检结果留盘、manifest 留空 hash 待 `--export` 补导(EXPORT_FAILED 非零退出);
  重跑幂等:源码未变且产物在盘 → 跳过导出(P6-2 同口径);手改保护同 gen-cards(CARD_EXISTS,--force 放行);
- **挂轨**:导出后 IR(`05_时间线工程/project.json`)存在即自动回填——按 **ffprobe 探测时长**入轨、
  后续 clip 自动平移、报告需重跑的下游阶段(时长变 → S4–S9);尺寸不符报错不拉伸(APPLY_ISSUES);
  IR 不存在则不做挂轨(gen-frames 不发明时间窗),先用 add-overlay / 纯动画组装器排定挂点再跑;
- **风格包(M6 前缺省回退)**:`--pack <slug>` 探测 `templates/styles/packs/<slug>/`(ADR-0051 规划位),
  命中且含 `frames.css` 则注入样式;缺失即回退**内置默认卡片规格 + WARN `stylePackMissing`**,行为可复现;
- **双引擎路径**:主路径 Kiln(探测序:config `kiln_cli_exe` → `VellumBench/target/release/kiln-cli.exe`
  → `VellumBench/dist/Kiln-noGUI-CLI.exe`,`rs_artboard.py --probe` 可查);Kiln 不可用时产物导出
  走 `export-fallback`(Playwright + 系统 Edge/Chrome,**仅 PNG**——场景卡 MP4 没有浏览器兜底,
  引擎不可用只能诚实报 EXPORT_FAILED);
- **MP4 导出的环境变量坑(安信德 #13,升级口径)**:Kiln 车道要求 **ffmpeg 在 PATH**
  (`ffmpeg -version` 可探测);WPI/浏览器车道认 **`WPI_FFMPEG`**;**`ARTBOARD_FFMPEG` 对 MP4
  导出无效**(只是 artboard preflight 的配置键)。两者都不满足时 gen-frames 的 warnings 会带提示;
- **Kiln 车道片长实测(与 WPI 时代口径的差异)**:WPI 实时采样「导出片长 ≈ 五段总和 − 1.8s」;
  **Kiln 浏览器逐帧采样无提前停,片长 ≈ `--max-wait` 本身**(2026-09 实测 6/6 张均为 max_wait±0.02s),
  挂轨以 ffprobe 探测为准(既有语义,零漂移),节奏预算勿再按 −1.8s 估算。
