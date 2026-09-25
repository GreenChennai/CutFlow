---
id: short-video-knowledge
label: 知识口播·抖音竖屏
triggers: [知识口播, 口播, 科普, 知识, 大字幕, 抖音, 竖屏, 快节奏]
pack: knowledge-talkshow-douyin
videoType: talking-head

brief_skeleton:
  videoType: talking-head
  platform: douyin
  ratio: 9x16
  pacing: fast
  bgm: none
  density: 少
  structure: "钩子(结论前置) → 正文要点(每点一个金句) → CTA(关注/下期)"

plan_skeleton:
  pacing: fast
  cards: info
  cut: {enabled: true}
  sfx: minimal
  subtitle: {style: talkshow-bold, maxChars: 12, cpsMax: 9}

assets_required: ["口播原声或 TTS 稿", "绿幕/虚拟背景素材(talking-head 必备)"]
assets_forbidden: ["无授权 BGM", "与口播无关的水印素材"]

example_prompts:
  - "把这篇科普文章做成一条抖音知识口播"
  - "知识类口播 要快节奏大字报那种"

acceptance: rules/video-types/纯口播.md §5
---

# 知识口播·抖音竖屏(short-video-knowledge)

给「一句话讲清一个知识点」的抖音口播。开场 3 秒给结论,正文金句化,大字幕是第二音轨。
风格包:`knowledge-talkshow-douyin`(fast 档 / talkshow-bold / 卡片借 artboard tech-kv)。
用法:`rs_intent.py compile --template short-video-knowledge --out <工程> --auto-fill`,再补 `--brief`/`--plan` 覆盖骨架值。
