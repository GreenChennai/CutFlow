# FFmpeg 配方簿

## 时长裁剪与延长:`-t` 必须放输入侧(B8,v0.12)

```bash
# ✗ -t 是输出选项,限制最终输出时长,tpad 补的帧全被截(安信德工程丢 20.4s 的事故形态)
ffmpeg -i card.mp4 -t 7.88 -vf "tpad=stop_mode=clone:stop_duration=5.6" out.mp4   # 得 7.88s
# ✓ -t 放输入侧,只裁输入,tpad 自由延长
ffmpeg -t 7.88 -i card.mp4 -vf "tpad=stop_mode=clone:stop_duration=5.6" out.mp4   # 得 13.5s
```

冻结帧补长(rs_render `clip.freezeMs`):输入 `-t` 只读到冻结起点,其后 tpad 克隆尾帧。

## 响度

- 人声预归一:`loudnorm=I=-16:TP=-1.5:LRA=11`
- 总线:`loudnorm=I=-14:TP=-1.0:LRA=11`(两遍法更准:先 `-af loudnorm=print_format=json -f null` 取 measured 值再 linear 重跑;v1 单遍够用;final 档已内置双 pass)
- BGM 闪避(v0.12 正确姿势):filtergraph 标签**只能被消费一次**,人声先合成一条总线再 asplit 出侧链与正混两路——

```
[a1][a2]...amix=inputs=N:duration=longest:normalize=0[voice];
[voice]asplit=2[voice_m][voice_d];
[bgraw][voice_d]sidechaincompress=threshold=0.02:ratio=6:attack=60:release=500[bgm];
[voice_m][bgm]amix=inputs=2:duration=first:normalize=0[mix]
```

(旧写法 `[bgraw][a1]sidechaincompress…;[a1][bgm]amix…` 二次消费 `[a1]` → `MIX_FAIL: Stream specifier 'a1' matches no streams`)

## 色彩透传(抄 kbcut)

- 读:ffprobe `color_primaries/color_transfer/color_space/color_range`;
- 写:`-color_primaries <同源> -color_transfer <同源> -color_space <同源>`;
- 10bit 源:`-pix_fmt yuv420p10le -c:v libx265`;SDR 普通源:h264 + yuv420p。
- rs_render v1 统一 yuv420p(h264),bt709 素材直接透传;非 bt709 输入先标定。

## 段级接缝防爆音

每段 `afade=t=in:st=0:d=0.008,afade=t=out:st=<len-0.008>:d=0.008`(8ms;注:rs_render 现行实现是 v0.10 起的帧量化 + concat 端 acrossfade,此配方仅用于手工拼段场景)。

## 无损拼接

段参数完全一致时 concat demuxer + `-c copy`;帧率/像素格式不一致会炸——segment 步已统一。

## 抠像

`colorkey=0x00FF00:0.12:0.08,despill=type=green,format=yuva420p`(叠加前)。
**不要用 `chromakey`**:alpha 在部分构建上全坏(v0.10 实测人物区域 α≈0),已全链改用 `colorkey`(RGB 距离键控)+ 几何腐蚀;详见 rules/archive.md。

## 字幕烧录

`-vf "ass='E\:/path/sub.ass'"`(Windows 冒号必须转义);字幕永远最后叠。

## Windows / Git Bash 引号与路径纪律(v0.12,安信德 #15)

- 路径以 `\` 结尾放进双引号会转义引号:`"...\fonts\"` → `unexpected EOF`。结尾反斜杠去掉或改用 `/`。
- 中文路径 + 复杂引号的内联 `python -c "..."` 极易碎:写成脚本文件再执行,不要内联。
- `/tmp` 等 POSIX 路径对 Windows 版 ffmpeg.exe 无效,输出一律写盘符路径。
- Bash 每条命令 cwd 可能漂移:长命令显式 `cd`。

## HDR→SDR(素材若为 HDR)

`zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p`
