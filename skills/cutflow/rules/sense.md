# Sense — 感知层:ASR / OCR / VQA / 校对

## ASR(口播视频 → 文字)

1. `rs_asr.py <媒体> --out 02_sensed`:自动抽 16k wav → FunASR 服务 → 句级时间戳分段。
   **句级只是过渡产物**——紧接着必须做字级对齐(S1 第二步),否则时间精度不够用。
2. 服务不可达时按序拉起:
   a. `python tools/start_asr.py`(无头直启,PATH 含 ffmpeg);
   b. 打包版 GUI(MomentShift.exe)用 computer-use 点"服务模式";
   c. 失败则告知用户,先做不依赖转写的环节。
3. `rs_align.py <媒体> --out 05_ir/wordline.json`:取 **Paraformer 原生字级 `timestamp`** 建 Wordline
   (唯一真相源)。若上游 MomentShift 未透传字级字段,自动降级为「句级 + 停顿锚点」模式,并在
   `data.degraded` 与报告中标注,**不静默降级**。详见 rules/align.md。
4. **校对(必做)**:逐句读 transcript_raw.md,修错字/口误/标点,专有名词对照 brief 术语表;
   产出 `02_sensed/transcript_corrected.md`(格式同 raw:每行 `[ss.s] 说话人N: 文本`),并写 `transcript.json`
   (同 raw 结构,文本已修正)。下游字幕/剪映草稿一律用 corrected 版。
5. **校对后必须重聚合**(修订旧规则「只改文本不动时间戳」):用字级锚点把校订文本重新对齐到
   `wordline.json` 的 `chars`——未改动的连续片段沿用原字时间戳,新增字取相邻字区间均分(仅此一处
   允许均分且区间 ≤3 字),删除字把其时间并给相邻字;某句明显漏识别 → 合并到下一句并在
   `sentences[].note` 注明且 `conf` 置 0。**改文本不改时间=旧版本对齐问题的元凶。**

## OCR / VQA(图片)

- `rs_sense.py <图> --out 02_sensed`:OCR(文字原样)+ VQA(中文描述)合并。
- 用途:素材理解、给图复刻(转 artboard)、分镜表图片内容提取。

## 抽帧(视频内容理解/重构图锚点)

- `rs_frames.py <视频> --out <png> --every 10`:网格图,Agent 目测。
- 绿幕重构图:看网格图选 anchorY(人物头部离顶比例);全权模式自选记录,协作模式出 2 张对比让用户挑。


## 备选策略(ADR-0008,重要)

**Agent 自带视觉能力时,优先自己直接看图**;本地 OCR/VQA 是备选件,只在以下情况调用:
1. 批量图片处理(几十张以上);
2. 运行环境没有视觉模型(纯 API 文本模型);
3. 用户明确要求;
4. config.sense.force_local = true。

部署(缺才有必要):`python tools/fetch_deps.py ocr`(110MB)/ `python tools/fetch_deps.py vqa`(630MB,Rust 引擎免 Python);自动写回 config。状态查看:`python tools/fetch_deps.py`(无参)。
