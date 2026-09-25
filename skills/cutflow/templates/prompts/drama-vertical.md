---
id: drama-vertical
label: 竖屏短剧
triggers: [短剧, 剧情, 反转, 霸总, 爽剧, 竖屏剧]
pack: drama-vertical
videoType: drama

brief_skeleton:
  videoType: drama
  platform: douyin
  ratio: 9x16
  pacing: fast
  bgm: cc0
  density: 少
  structure: "冲突前置(3秒) → 铺垫蓄力 → 反转/爽点 → 悬念收尾"

plan_skeleton:
  pacing: fast
  cards: scene
  cut: {enabled: true}
  sfx: minimal
  subtitle: {style: subtitle-white, maxChars: 14, cpsMax: 9}

assets_required: ["剧情素材(表演/对白清晰)", "配乐授权源(情绪推手)"]
assets_forbidden: ["剧透式预告素材", "无对白字幕的哑剧段(台词是信息主线)"]

example_prompts:
  - "帮我把这段短剧素材剪成高燃竖屏剧"
  - "反转剧情向的竖屏短片"

acceptance: rules/video-types/短剧.md §5
---

# 竖屏短剧(drama-vertical)

强冲突、快翻转、情绪外放;前 3 秒给冲突不给背景,尾卡必带下集悬念。
风格包:`drama-vertical`(fast 档 / subtitle-white 台词 14 字 / 钩子卡借 artboard ecommerce-promo)。
用法:`rs_intent.py compile --template drama-vertical --out <工程> --auto-fill`。
