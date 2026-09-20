# ADR-0044: 对 jianying-headless 只学其方法与能力,不复制代码(许可约束)

日期: 2026-09-20 · 状态: 已确认(2026-09-20 用户拍板:只学其方法与能力,具体代码不抄) · 关联: ADR-0038(渲染后端与剪映 5.9 能力对等边界)、ADR-0036(ARL-1.0 弱传染协议)、同伴仓 cutforge `docs/adr/0008-jianying-headless只学方法与能力不复制代码.md`(同一决策在 CutForge 侧的落点)

## 背景

jianying-headless 项目证明了"程序化生成剪映草稿"这条路可行,其工程方法(计划编译分层、protect 保护区、RMS 辅助选切点、剪后重转写对账、独立副本编辑、帧数严格门禁、版本/签名/哈希核对、诚实验收文档)对本项目有直接参考价值。但其许可为 **Personal Learning and Non-Commercial Use**:禁止 republish / mirror / package / market 派生作品,商业使用需作者书面授权;且其仅支持 Apple Silicon macOS + 剪映 11.5,与本项目(Windows + 剪映 5.9)不同源——直接抄代码既不合法,也不兼容。

## 决策

1. **学习其方法与能力,一律自行实现、自定字段名**:上述八项方法逐项落地时独立设计、独立命名;**不复制其源码,不照搬其专有字段与结构**。
2. **Windows 侧剪映出口继续走 pyJianYingDraft(5.9 明文草稿)**,不引入 jianying-headless 的任何代码或依赖;剪映 6.0+/11.3+ 加密草稿维持"永不读写"铁律。
3. **合规边界写进诚实验收文档**:每次借鉴其方法落地,在验收文档中显式标注"方法来自 jianying-headless 的纪律,实现与字段为本项目自有"。
4. 若确需更深借鉴(超出方法层面的引用),先按其许可条款取得作者书面授权,未取得前不做。

## 取舍

- 放弃"直接移植一套成熟实现"的短期便利(重写八项方法各有实现成本)。
- 换来:合法合规(规避派生作品禁止条款)、跨平台可行(不被其 macOS-only 前提绑架)、字段与 IR 契约自洽(不被外来结构污染 schema 真相源)。

## 被否决的替代

- **移植其代码**:否决——许可不符(Personal Learning and Non-Commercial)且平台不符(macOS/剪映 11.5 vs Windows/剪映 5.9)。
- **照搬其字段名与草稿结构**:否决——仍属派生,且会把外部字段语义引入本项目以 schema 为唯一契约真相源的体系(ADR-0034)。

## 后果

- 借鉴清单逐项标注"要 / 不要 + 自行实现落点";后续任何对 jianying-headless 的新引用都必须先过本 ADR 的第 4 条。
- 本决策为跨仓决策,CutForge 侧同文落一份(见关联),两仓互相引用。

## 落地证据

- `skills/cutflow/rules/jianying-verification.md`:诚实验收文档,「已验证 / 未验证 / 拒绝」三分写法;**方法声明行明文标注**(ADR-X-05:只学方法,不抄内容/字段/代码);**"拒绝"区**列出明确不做项(剪映 11.3+ 加密草稿、原生无头引擎导出、复合片段/嵌套草稿、动效关键帧写草稿、BGM ducking 写草稿),每项写明原因。
- **字段名自查对照**:剪映出口的草稿编译层(`skills/cutflow/scripts/rs_jy_draft.py`,IR → 草稿计划)为自行实现;IR→草稿的能力映射与字段口径由本项目自定义(`skills/cutflow/rules/jianying.md` 能力映射节),不经 jianying-headless 的字段命名。
- 阶段六合规落地物:编译层 + 保护区 + 草稿门禁(jianying-verification.md「2026-09-20:编译层 + 保护区 + 草稿门禁」节,J1–J6,测试 `tests/test_v22_jy_backend.py`)。
- 唯一剪映相关第三方依赖仍是 pyJianYingDraft(MIT,vendored 于 `skills/cutflow/scripts/vendor/pyJianYingDraft/`,NOTICE 已声明)。
