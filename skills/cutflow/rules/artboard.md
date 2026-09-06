# artboard 桥 — 图形素材(片头/片尾/封面/信息卡/小动画)

## 何时用

- 片头/片尾板(静态 PNG 或 ≤15s MP4 动画)、封面、信息卡/数据卡、章节转场卡。
- 素材落 `03_assets/artboard/`,以静图或视频 clip 进 IR 的 overlay 轨。

## 调用(走 artboard 技能本体)

1. 预检:`python <artboard>/scripts/preflight.py`(ffmpeg 已回填,应无 WARN);
2. 脚手架:`scaffold.py <slug> --size <品类> --fonts <字体>`;
3. 导出:`export.py --source <proj>/src --output <proj>/export/o.png --width 1080 --height 1920`
   (固定高度必须带 --height;动图 `--format MP4 --fps 25`,GIF `--format GIF`);
4. 9:16 用 1080x1920,16:9 用 1920x1080(slide 品类)。

## 设计约定

- 风格与视频风格 token 一致(看 brief 的 colors/typography);
- 动图约束:2-6s 无缝循环、仅 transform/opacity、终态须仍是合格静态海报;
- 产出回填 01_materials/MANIFEST.md。
