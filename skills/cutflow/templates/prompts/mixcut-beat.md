---
id: mixcut-beat
label: 混剪卡点
triggers: [混剪, 卡点, 踩点, 音乐驱动, 高燃, remix]
pack: mixcut-douyin
videoType: 混剪

brief_skeleton:
  videoType: 混剪
  platform: douyin
  ratio: 9x16
  pacing: music
  bgm: cc0
  density: 多
  structure: "强钩子(2拍) → 主歌(渐密) → 副歌(最密) → 定格收尾"

plan_skeleton:
  pacing: music
  cards: none
  cut: {enabled: false}
  sfx: minimal

assets_required: ["BGM 授权源音频", "素材碎片 ≥10 条"]
assets_forbidden: ["静态长镜头", "低对比素材"]

example_prompts:
  - "把这几段游戏录屏做成一条高燃卡点"
  - "音乐驱动的混剪,节奏要快"

acceptance: rules/video-types/混剪.md §5
---

# 混剪卡点(mixcut-beat)

BGM 是时间轴骨架,画面事件跟节拍走;字幕只上歌词钩子/点睛短句。
风格包:`mixcut-douyin`(music 档 / subtitle-white / 钩子卡借 artboard ecommerce-promo)。
用法:`rs_intent.py compile --template mixcut-beat --out <工程> --auto-fill`;BGM 音频在 S0 单独登记(03_创作素材/bgm.mp3,授权源留痕)。
