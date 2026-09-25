---
id: vlog-daily
label: Vlog 旅拍日常
triggers: [vlog, Vlog, VLOG, 旅拍, 生活记录, 日常, 碎片]
pack: vlog-douyin
videoType: vlog

brief_skeleton:
  videoType: vlog
  platform: douyin
  ratio: 9x16
  pacing: normal
  bgm: cc0
  density: 无
  structure: "最美空镜/结果前置 → 碎片叙事(时间或地点线) → 下期钩子"

plan_skeleton:
  pacing: normal
  cards: none
  cut: {enabled: true}
  sfx: minimal
  subtitle: {style: talkshow-bold, maxChars: 12, cpsMax: 9}

assets_required: ["多条素材碎片(空镜/对话/特写)", "BGM 授权源音频"]
assets_forbidden: ["拉伸变形的横屏素材(须走 reframe 锚点)", "盖过人声段的 BGM"]

example_prompts:
  - "把国庆旅拍的碎片剪成一条 vlog"
  - "日常生活记录 加个情绪 BGM"

acceptance: rules/video-types/vlog.md §5
---

# Vlog 旅拍日常(vlog-daily)

生活流碎片拼接,BGM 承担情绪骨架,字幕只跟人声对白,默认无信息卡。
风格包:`vlog-douyin`(normal 档 / talkshow-bold 仅人声 / 片头借 artboard xhs-cover)。
用法:`rs_intent.py compile --template vlog-daily --out <工程> --auto-fill`。
