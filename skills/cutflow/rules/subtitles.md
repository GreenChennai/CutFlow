# Subtitles — 字幕(Netflix 规范 + 抖音红线)

## 权威依据(ADR-0001/0002)

- [Netflix 简体中文 Timed Text Style Guide](https://partnerhelp.netflixstudios.com/hc/en-us/articles/215986007-Chinese-Simplified-Timed-Text-Style-Guide):每行 ≤16 字、最多 2 行;**句号/逗号不入屏**(断点用空格);问号/感叹号保留且禁 `!?` 连用;顿号可列举行中、不入行尾;省略号统一 `…`(U+2026);全角引号;一至十用汉字、其余半角数字。
- [抖音低质内容判定](https://m.bjnews.com.cn/detail/1716003662168785.html):字幕**无错别字**、**时间轴与画面同步**是硬红线;
- [星图营销平台制作规范](https://www.xingtu.cn/help-center/demander/109176):字幕不得出现促销/导流信息;
- 口语书面化属剪辑优化:转写 → 删口水词 → 语义断句 → 校对(万兴喵影/剪映智能字幕通行流程)。

## 轻改写引擎(textopt.py,默认开启)

`--no-optimize` 可关闭。规则:
1. 删句首 filler(嗯/呃/唉…)与句尾语气字(啊/嘛/呢/吧);
2. 标点:句号丢弃、全半角逗号在卡内转空格、断行点优先 问叹>逗号>顿号>虚词收尾、禁 `!?` 连用、`……`→`…`;
3. 卡级切分 ≤16 字(9:16)/≤22 字(16:9),ASCII 词内禁切;
4. 时间插值:ASR 句级时间戳按字符数比例分摊到卡。

**铁律**:只动标点/口水词/断行,不改语义不删信息(音频不动,轻改写定义见 ADR-0002)。

## 用法

```
rs_subtitle.py --from-transcript 02_sensed/transcript_corrected.json --style talkshow-bold --ratio 9x16 --out 06_output
rs_subtitle.py --from-tts 03_assets/tts/manifest.json --style tutorial-clean --ratio 9x16 --out 06_output
```

- `--from-transcript` 必须用 **Agent 校对后的** transcript_corrected(原始 ASR 错字是抖音红线);
- `--from-tts` 时间戳即合成实长,天然同步。

## 风格

| style | 用途 | 特点 |
|---|---|---|
| talkshow-bold | 口播/绿幕 | 大字居中偏下、粗体黑边、9:16 marginV=500(底部 25% 安全区之上) |
| tutorial-clean | 教程 | 底部半透明底条(BorderStyle=3) |
| subtitle-white | 通用白字黑边 | |

安全区(9:16):底 25%、顶 12% 不放字幕。
