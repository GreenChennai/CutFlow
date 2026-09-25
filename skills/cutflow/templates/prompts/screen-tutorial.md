---
id: screen-tutorial
label: 录屏教程
triggers: [录屏, 软件教程, 操作演示, 屏幕录制, 操作步骤]
pack: screen-tutorial
videoType: screen-recording

brief_skeleton:
  videoType: screen-recording
  platform: bilibili
  ratio: 16x9
  pacing: slow
  bgm: none
  density: 少
  structure: "痛点/成果开场 → 步骤 1..N(每步一个章节卡) → 常见错误 → 合集引导"

plan_skeleton:
  pacing: slow
  cards: chapter
  cut: {enabled: true}
  sfx: minimal
  subtitle: {style: tutorial-clean, maxChars: 22, cpsMax: 9}

assets_required: ["1080p 及以上录屏素材", "讲解原声(降噪后)"]
assets_forbidden: ["低分辨率录屏(文字模糊)", "遮挡操作区的贴片素材"]

example_prompts:
  - "把这段软件录屏做成操作教程"
  - "屏幕录制的教学视频 步骤清楚一点"

acceptance: rules/video-types/录屏教程.md §5
---

# 录屏教程(screen-tutorial)

录屏本体即内容,解说是导航,卡片是路标;节奏跟操作步骤,不跟音乐。
风格包:`screen-tutorial`(slow 档 / tutorial-clean 22 字 / 章节卡借 artboard data-longform)。
用法:`rs_intent.py compile --template screen-tutorial --out <工程> --auto-fill`。
