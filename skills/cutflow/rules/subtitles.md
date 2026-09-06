# Subtitles — 字幕

## 用法

```
rs_subtitle.py --from-tts 03_assets/tts/manifest.json --style talkshow-bold --ratio 9x16 --out 06_output
rs_subtitle.py --from-transcript 02_sensed/transcript.json --style tutorial-clean --ratio 16x9 --out 06_output
```

产出 `master.srt` + `subtitles.ass`;IR.subtitle.ass 指向 ass,渲染时最后烧录。

## 断行

- 评分断行(标点+100/空格+90/句尾停顿+50/ASCII 切断-200),9:16 单条 ≤16 字、16:9 ≤22 字。
- 单句超 4s 自动按断行数均分时间。

## 风格

| style | 用途 | 特点 |
|---|---|---|
| talkshow-bold | 口播/绿幕 | 大字居中偏下、粗体黑边、9:16 marginV=500(避开底部 25% 安全区) |
| tutorial-clean | 教程 | 底部半透明底条(BorderStyle=3) |
| subtitle-white | 通用白字黑边 | |

## 安全区(9:16)

底部 25%(平台 UI/文案区)、顶部 12% 不放字幕;关键词高亮(v1.5)在 ASS 里用富文本 span,当前版本可用 `highlight` IR 字段预留。
