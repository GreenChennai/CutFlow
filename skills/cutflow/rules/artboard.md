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
