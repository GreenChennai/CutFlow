# TTS — 配音(音色卡驱动)

## 用法

`rs_tts.py --script 00_brief/文案.txt --out 03_assets/tts [--voice koubo-test] [--speed 1.0]`

- 文案格式:普通多行文本;行首 `【开心】` 指定该句情感。
- 断点续传:manifest.json 已有句子自动跳过;改文案后重跑只合成新句。
- 产出:`tts_XXXX.wav` 序列 + `manifest.json`(句级 start/end,已含 120ms 句间垫)+ `voice_48k_full.wav`。

## 音色卡

- 卡位置:config.tts.voices_dir / `<voice>/card.json`(ref_audio/prompt_text/权重路径)。
- 默认 `koubo-test`;`disabled_voices` 中的音色(Jimi)会拒绝,移出即可启用。
- 切音色=换 --voice,脚本自动 set_weights。

## 引擎管理

- 探活 `http://127.0.0.1:9885`;未运行则后台启动:
  `cd <engine_dir> && runtime/python.exe api_v2.py -a 127.0.0.1 -p 9885`(约 1-2 分钟就绪)。
- 首次合成较慢属正常(ZLUDA);预热:先发一句"热身。"。
- 失败重试 3 次指数退避;仍败检查 ref_audio 路径是否存在(音色卡里是绝对路径)。

## 时间轴用法

manifest.json 的句级 start/end 就是字幕时间轴(rs_subtitle --from-tts 直接用),
也是 IR 里 voice 音频 clip 的摆放依据(通常整条 voice_48k_full.wav 一条轨道,或逐句摆放做卡点)。
