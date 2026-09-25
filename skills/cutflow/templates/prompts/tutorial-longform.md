---
id: tutorial-longform
label: 教程·B站横屏
triggers: [教程, B站, bilibili, 教学, 深度, 步骤]
pack: tutorial-bilibili
videoType: talking-head

brief_skeleton:
  videoType: talking-head
  platform: bilibili
  ratio: 16x9
  pacing: slow
  bgm: none
  density: 少
  structure: "痛点开场 → 步骤 1..N(每步一个章节卡) → 效果展示 → 三连引导"

plan_skeleton:
  pacing: slow
  cards: chapter
  cut: {enabled: true}
  sfx: minimal
  subtitle: {style: tutorial-clean, maxChars: 22, cpsMax: 9}

assets_required: ["口播讲解原声", "演示画面或成品对照"]
assets_forbidden: ["盖过讲解声的 BGM", "低清晰度演示素材"]

example_prompts:
  - "做一个 B站 的 PS 入门教程"
  - "深度教学视频 步骤讲清楚那种"

acceptance: rules/video-types/纯口播.md §5
---

# 教程·B站横屏(tutorial-longform)

慢节奏章节式教程,步骤推进、图表说话,观众要跟操作。
风格包:`tutorial-bilibili`(slow 档 / tutorial-clean / 章节卡借 artboard data-longform)。
用法:`rs_intent.py compile --template tutorial-longform --out <工程> --auto-fill`。
