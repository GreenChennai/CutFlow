# FFmpeg 配方簿

## 响度

- 人声预归一:`loudnorm=I=-16:TP=-1.5:LRA=11`
- 总线:`loudnorm=I=-14:TP=-1.0:LRA=11`(两遍法更准:先 `-af loudnorm=print_format=json -f null` 取 measured 值再 linear 重跑;v1 单遍够用)
- BGM 闪避:`[bgm][voice]sidechaincompress=threshold=0.02:ratio=6:attack=60:release=500`

## 色彩透传(抄 kbcut)

- 读:ffprobe `color_primaries/color_transfer/color_space/color_range`;
- 写:`-color_primaries <同源> -color_transfer <同源> -color_space <同源>`;
- 10bit 源:`-pix_fmt yuv420p10le -c:v libx265`;SDR 普通源:h264 + yuv420p。
- rs_render v1 统一 yuv420p(h264),bt709 素材直接透传;非 bt709 输入先标定。

## 段级接缝防爆音

每段 `afade=t=in:st=0:d=0.008,afade=t=out:st=<len-0.008>:d=0.008`(8ms)。

## 无损拼接

段参数完全一致时 concat demuxer + `-c copy`;帧率/像素格式不一致会炸——segment 步已统一。

## 抠像

`chromakey=0x00FF00:0.12:0.08,despill=type=green,format=yuva420p`(叠加前)。

## 字幕烧录

`-vf "ass='E\:/path/sub.ass'"`(Windows 冒号必须转义);字幕永远最后叠。

## HDR→SDR(素材若为 HDR)

`zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p`
