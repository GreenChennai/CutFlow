# Sense — 感知层:ASR / 三级画面感知 / 校对

## ASR(口播视频 → 文字)

1. `rs_asr.py <媒体> --out 02_转写与校对`:自动抽 16k wav → FunASR 服务 → 句级时间戳分段。
   **句级只是过渡产物**——紧接着必须做字级对齐(S1 第二步),否则时间精度不够用。
2. 服务不可达时按序拉起:
   a. `python tools/start_asr.py`(无头直启,PATH 含 ffmpeg);
   b. 打包版 GUI(MomentShift.exe)用 computer-use 点"服务模式";
   c. 失败则告知用户,先做不依赖转写的环节。
3. `rs_align.py build --media <素材> --out 05_时间线工程/wordline.json`:取 **Paraformer 原生字级时间戳(timestamp 字段)** 建 Wordline
   (唯一真相源)。若上游 MomentShift 未透传字级字段,自动降级为「句级 + 停顿锚点」模式,并在
   `data.degraded` 与报告中标注,**不静默降级**。详见 rules/align.md。
4. **校对(必做)**:逐句读 transcript_raw.md,修错字/口误/标点,专有名词对照 brief 术语表;
   产出 `02_转写与校对/transcript_corrected.md`(格式同 raw:每行 `[ss.s] 说话人N: 文本`),并写 `transcript.json`
   (同 raw 结构,文本已修正)。下游字幕/剪映草稿一律用 corrected 版。
5. **校对后必须重聚合**(修订旧规则「只改文本不动时间戳」):用字级锚点把校订文本重新对齐到
   `wordline.json` 的 `chars`——未改动的连续片段沿用原字时间戳,新增字取相邻字区间均分(仅此一处
   允许均分且区间 ≤3 字),删除字把其时间并给相邻字;某句明显漏识别 → 合并到下一句并在
   `sentences[].note` 注明且 `conf` 置 0。**改文本不改时间=旧版本对齐问题的元凶。**

## 三级画面感知预算(T3.8,核心口径)

Agent 读画面的每一分钱都走**分级预算**,像素数据在脚本层算成结构化字段,**绝不进 context**:

| 级 | 内容 | 依赖 | 默认 | 开法 | 超预算行为 |
|---|---|---|---|---|---|
| **L0 结构** | 逐镜档案(motionScore/亮度/对比/彩色度/稳定度/audioRms/speechRate)+ 镜头切分 + skeleton.json + vision.json | 仅 ffmpeg(零模型零网络) | **开** | 无需(管线默认) | 不适用(秒级机械计算) |
| **L1 标签** | hasFace/faceFirstMs/safeAreaOccupancy + reframe 主体锚 | 本地模型:cv2(Haar 人脸+帧差粗框,vision.cv/vision.sense/vision.track) | **开** | 无需;组件缺失→显式降级留痕 | 缺失即降级,产物照出,`degradeReasons` 留痕 |
| **L2 描述** | VLM 逐镜一句 ≤20 字中文描述 + 主体/场景/构图标签 | 本机 VQA 组件(fetch_deps vqa,约 630MB) | **关** | `rs_sense.py shots --tier L2 --budget-sec 120` 显式开 | **停并留痕**(`stopped="budget"`,完成 x/y 记账)——不静默降级 |

机械纪律(验收口径):
1. `--tier L0` **零网络零模型零解码**:纯读 shots.json 结构(代码路径保证,测试断言);
2. `--tier L2` 不给预算不放行:预算默认 120s,超限立即停,`tiers.L2.note` 写明「超限停,完成 x/y」;
3. 任何一级缺失都必须在产物 `degradeReasons`/`sense_report.md` 里**写明缺什么、影响哪些决策、怎么补**;
4. L2 描述并入 `vision.json` 的 `caption/tags` 字段(单一产物),经 schema 校验后落盘,超 20 字即校验红。

## 画面感知产物链(哪些命令产什么)

```
rs_shot.py detect <素材> --out <工程>        # 04_粗剪决策/shots.json v2(切分 + L0 逐镜档案)
rs_vision.py vision   <工程>                 # 05_时间线工程/vision.json(L0+L1 聚合,schema 校验)
rs_vision.py skeleton <工程>                 # 05_时间线工程/skeleton.json(骨架摘要,≤2KB)
rs_sense.py shots --vision … --tier L2       # 逐镜 VLM 描述 → 并回 vision.json(L2,默认关)
rs_vision.py report   <工程>                 # 05_时间线工程/sense_report.md(能力三态+降级建议)
```

- **Agent 剪辑前唯一必读画面摘要 = skeleton.json**:一行一镜(镜号/时长/运动档/亮度档/有无台词/
  台词前 12 字/建议角色),3 分钟成片 ≤2KB;要单镜细节查 vision.json,不读原图。
- `rs_edit.py context --with-vision`:改片时注入文本化画面摘要(skeleton 一行/镜 + 镜内描述),
  仍无图片路径,12KB 硬上限不变。
- 抽帧一律走**价值选帧**(`rs_frames.py <视频> --out <png> --shots <shots.json>` /
  `rs_bench.py <成片.mp4> --ir <project.json> --shots <shots.json> --out <png>`):
  镜头中点 + 运动峰值 + 字幕起点前 2 帧 + 人脸出现帧,硬上限 `min(24, 3+镜头数)`;
  不再均匀撒帧(均匀档保留为 `--shots` 缺席时的兼容回退,输出里留痕)。

## OCR / VQA(单图,备选件)

- `rs_sense.py <图> --out 02_转写与校对`:OCR(文字原样)+ VQA(中文描述)合并。
- 用途:素材理解、给图复刻(转 artboard)、分镜表图片内容提取;镜头级批量描述走上面的 L2 通道。

## 抽帧(视频内容理解/重构图锚点)

- 价值选帧网格:`rs_frames.py <视频> --out <png> --shots <shots.json> [--wordline …] [--vision …]`。
- 重构图锚点:看网格图选 anchorY(人物头部离顶比例);全权模式自选记录,协作模式出 2 张对比让用户挑。

## 备选策略(ADR-0008,重要)

**Agent 自带视觉能力时,优先自己直接看图**;本地 OCR/VQA 是备选件,只在以下情况调用:
1. 批量图片处理(几十张以上);
2. 运行环境没有视觉模型(纯 API 文本模型);
3. 用户明确要求;
4. config.sense.force_local = true。

部署(缺才有必要):`python tools/fetch_deps.py ocr`(110MB)/ `python tools/fetch_deps.py vqa`(630MB,Rust 引擎免 Python);自动写回 config。状态查看:`python tools/fetch_deps.py`(无参)。
