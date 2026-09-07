---
name: cutflow
description: AI 视频制作总控技能:接收口播视频/文案/剧本分镜/图片,经感知(ASR/OCR/VQA)→合成(TTS/artboard/AI提示词)→剪辑(FFmpeg直出+剪映5.9草稿)→自评流水线产出成片,支持 9:16 与 16:9。当用户想要:做视频、剪视频、口播视频、教程视频、动画视频、把文案变成视频、给视频加字幕、绿幕抠像、配音、生成剪映草稿、出 AI 视频提示词时使用。
---

# CutFlow — AI 视频制作总控

一句话:素材进 → 成片出,同时交付可在剪映 5.9 继续精修的草稿工程。**你是编排者,脚本是机械臂;所有决策看 brief,所有产出过自评。**

## 0. 开工前置

1. 跑 `python skills/cutflow/scripts/rs_doctor.py` 读 JSON:致命项全绿才开工;非致命(ASR/TTS 服务)可按 §R 拉起。
2. 首次使用先读本项目记忆 `project.md`(工作目录下,若有)。

## 1. 管线总览(按素材类型路由)

```
素材分类 → intake 问卷 → brief.md(契约,此后不再问)
   ├─ 口播视频 ──→ rs_asr 转写 → 你校对 → rs_subtitle 字幕
   ├─ 纯文案 ───→ 你分句 → rs_tts 配音(koubo-test 默认) → 字幕=合成实长
   ├─ 剧本分镜 ──→ 你解析为 Shot 表 → 逐镜找素材/生成提示词
   ├─ 图片 ────→ rs_sense(OCR+VQA) → 理解后入时间线
   └─ AI 素材缺口 → 转 skills/cutflow-prompt 产提示词(不调任何生成 API)
→ 写 IR(05_ir/project.json)→ rs_ir validate
→ rs_render(FFmpeg 直出,两比例可选)
→ rs_jy_draft(剪映 5.9 草稿,交付可编辑工程)
→ rs_bench 自评 → 目测修复(≤3 轮) → 交付
```

## 2. Hard Rules(会静默失败,违反必炸)

1. **一切决策只查 brief.md**;automation 模式不再打断用户,自行选择并记录理由。
2. ASR/OCR 原始输出**必须经你校对**后才能用(错字/口误/标点),产出 `transcript_corrected.md`。
3. TTS 长文**先全量合成落盘**再进时间线;句间 120ms 垫已由 rs_tts 处理。
4. 渲染:统一帧率 → 逐段提取 → concat → 合成 → 混音 → **字幕最后叠** → 编码(rs_render 已固化,勿绕过)。
5. 总线响度 -14 LUFS / -1 dBTP;人声先行归一。
6. 9:16 安全区:底部 25%、顶部 12% 不放字幕/关键信息。
7. 不可逆构图决策(重构图锚点/风格二选一)先 `rs_frames` 出网格图目测;全权模式自选并写进 project.md。
8. 写剪映草稿前确认剪映未运行(rs_jy_draft 已内置检测);只动 5.9,绝不碰 11.3。
9. 渲染产物必过自评:rs_doctor 断言 + rs_bench 目测,≤3 轮,仍败如实上报。
10. 素材/中间件/git:01_materials 只读;大文件不进 git。

## 3. 命令速查(scripts/,全部支持 --json)

| 环节 | 命令 |
|------|------|
| 体检 | `rs_doctor.py` |
| 转写 | `rs_asr.py <媒体> --out 02_sensed` |
| 图片理解 | `rs_sense.py <图> --out 02_sensed` |
| 抽帧 | `rs_frames.py <视频> --out <png> --every 10` |
| 配音 | `rs_tts.py --script 文案.txt --out 03_assets/tts [--voice koubo-test]` |
| 字幕轻改写 | rs_subtitle 默认开启(Netflix 规范);`--no-optimize` 关闭 |
| 音效 | IR audio clip `src: "assets_sfx:whoosh"`(仓库 assets/sfx/) |
| 字幕 | `rs_subtitle.py --from-tts <manifest> --style talkshow-bold --ratio 9x16 --out 06_output` |
| IR 校验 | `rs_ir.py validate 05_ir/project.json` |
| 渲染 | `rs_render.py 05_ir/project.json --ratio 9x16 --profile final` |
| 剪映草稿 | `rs_jy_draft.py 05_ir/project.json --name <名> --subtitles 03_assets/tts/manifest.json` |
| 自评抽帧 | `rs_bench.py <成片> --ir 05_ir/project.json --out 06_output/bench.png` |

## 4. IR 最小样例(完整 schema 见 templates/project.schema.json)

```json
{"version":1,"slug":"demo","fps":30,"canvas":{"width":1080,"height":1920},
 "tracks":[
  {"kind":"video","clips":[{"src":"01_materials/a.mp4","startMs":0,"durationMs":12000,
    "sourceInMs":3000,"reframe":{"anchorY":0.35},"motion":{"in":"fadeIn","inMs":400}}]},
  {"kind":"audio","clips":[{"src":"03_assets/tts/tts_0001.wav","startMs":0,"role":"voice"}]}],
 "bgm":{"src":"03_assets/bgm.mp3","gainDb":-18,"ducking":true},
 "subtitle":{"ass":"06_output/subtitles.ass","source":"03_assets/tts/manifest.json"},
 "outputs":["9x16"]}
```

风格 token:见 templates/styles/(talkshow-bold 口播大字 / tutorial-clean 教程 / motion-info 动画)。

## 5. 剪映 5.9 双通道

- **草稿直写**(主):rs_jy_draft → 用户获得可编辑工程;绿幕叠加无对应字段会警告,该类项目建议以 FFmpeg 版为准。
- **GUI 自动导出**(辅,computer-use):启动 5.9 → 首页进草稿 → 点导出 → 确认分辨率帧率 → 等产物。控件锚点见 references/jianying-gui-anchors.md(11.3 草稿加密,永不操作)。

## 6. AI 生视频边界

只产提示词(首帧图 + 首帧标注式 5-10s i2v,可选尾帧),转 `cutflow-prompt` 技能;**绝不调用任何生图/生视频 API**。

## 7. 详细规则(rules/,按需加载)

intake.md 问卷契约 / sense.md 感知与校对 / tts.md 配音 / subtitles.md 字幕 / compose.md IR 与渲染 / jianying.md 剪映双通道 / artboard.md 图形素材桥 / selfcheck.md 自评闭环。

## 8. 反模式(踩过的坑)

- 不要手写 filtergraph 一步到位渲染全片——中间件可断点续渲、可定位失败段。
- 不要相信 ASR 专有名词;brief 里让用户/上下文提供术语表。
- 不要在剪映运行时写它的草稿目录。
- 不要为凑时长硬塞低质片段;宁可如实上报素材不足。
- 忘了 ass 文件路径转义(Windows 盘符冒号)会让字幕静默丢失——rs_render 已处理,手改时注意。
