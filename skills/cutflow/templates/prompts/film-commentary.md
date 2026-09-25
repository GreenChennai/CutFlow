---
id: film-commentary
label: 影视解说
triggers: [影视解说, 解说, 影评, 电影解读, 拉片]
pack: ""
videoType: talking-head

brief_skeleton:
  videoType: talking-head
  platform: douyin
  ratio: 9x16
  pacing: fast
  bgm: provided
  density: 少
  structure: "悬念钩子(不剧透) → 剧情脉络(快剪) → 关键反转 → 锐评收尾"

plan_skeleton:
  pacing: fast
  cards: none
  cut: {enabled: true}
  sfx: minimal
  subtitle: {style: subtitle-white, maxChars: 14, cpsMax: 9}

assets_required: ["解说稿原声或 TTS", "影视素材(注意版权边界,见 _通用规则.md §版权)"]
assets_forbidden: ["整段无解说的原片搬运", "剧透式开头"]

example_prompts:
  - "把这部电影剪成一条解说"
  - "影视解说 三分钟讲完一部悬疑片"

acceptance: rules/video-types/影视解说.md §6
---

# 影视解说(film-commentary)

解说轨是主线,原片是插图;版权边界(合理使用四要素)是本模板第一红线。
风格包:无专属包(编译时按 `talking-head` 缺省条目 knowledge-talkshow-douyin 回退,pack 参数留痕为回退口径)。
用法:`rs_intent.py compile --template film-commentary --out <工程> --auto-fill`。
