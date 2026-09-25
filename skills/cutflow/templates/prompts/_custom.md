---
id: custom
label: 定制场景(用户自填)
triggers: [自定义, 定制]
pack: ""
videoType: talking-head

brief_skeleton:
  videoType: talking-head
  platform: douyin
  ratio: 9x16
  pacing: normal
  bgm: none
  density: 少
  structure: "钩子 → 正文(要点…) → CTA"

plan_skeleton:
  pacing: normal
  cards: none
  cut: {enabled: true}
  sfx: auto

assets_required: ["(按需填写)"]
assets_forbidden: ["(按需填写)"]

example_prompts:
  - "(写一句你的真实场景描述,作为 triggers 命中样例)"

acceptance: rules/video-types/纯口播.md §5
---

# 定制化骨架(_custom.md)—— 用户自己填

这是「提示词定制化框架」的落点(方案 §5.11):想定制整个场景模板时,**直接复制本文件**为
`templates/prompts/<你的-id>.md` 并照抄改值,零代码。

## 六步(与风格包六步同构)

1. 复制本文件为 `<你的-id>.md`(id 小写连字符,与文件名一致);
2. 改 front matter:`id` / `label` / `triggers`(模糊描述命中词,≥2 个)/ `pack`(可空)/ `videoType`;
3. 改 `brief_skeleton` / `plan_skeleton`(值必须能过 `rs_intent validate`:枚举与红线见 registry/platforms);
4. 补 `assets_required` / `assets_forbidden` / `example_prompts` / `acceptance`;
5. 校验:`rs_intent.py template show <你的-id>` 能全文打印、`template match` 能命中;
6. 编译:`rs_intent.py compile --template <你的-id> --out <工程> --auto-fill`。

## 门禁

- 新模板必须通过 `_schema.json` 的结构校验(必填键与类型);
- `example_prompts` 至少 1 条,且 `template match` 对它命中本模板(回归锚)。
