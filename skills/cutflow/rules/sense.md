# Sense — 感知层:ASR / OCR / VQA / 校对

## ASR(口播视频 → 文字)

1. `rs_asr.py <媒体> --out 02_sensed`:自动抽 16k wav → FunASR 服务 → 句级时间戳分段。
2. 服务不可达时按序拉起:
   a. `python tools/start_asr.py`(无头直启,PATH 含 ffmpeg);
   b. 打包版 GUI(MomentShift.exe)用 computer-use 点"服务模式";
   c. 失败则告知用户,先做不依赖转写的环节。
3. **校对(必做)**:逐句读 transcript_raw.md,修错字/口误/标点,专有名词对照 brief 术语表;
   产出 `02_sensed/transcript_corrected.md`(格式同 raw:每行 `[ss.s] 说话人N: 文本`),并写 `transcript.json`
   (同 raw 结构,文本已修正)。下游字幕/剪映草稿一律用 corrected 版。
4. 校对只改文本不动时间戳;某句明显漏识别→合并到下一句并注明。

## OCR / VQA(图片)

- `rs_sense.py <图> --out 02_sensed`:OCR(文字原样)+ VQA(中文描述)合并。
- 用途:素材理解、给图复刻(转 artboard)、分镜表图片内容提取。

## 抽帧(视频内容理解/重构图锚点)

- `rs_frames.py <视频> --out <png> --every 10`:网格图,Agent 目测。
- 绿幕重构图:看网格图选 anchorY(人物头部离顶比例);全权模式自选记录,协作模式出 2 张对比让用户挑。
