# Meta — 标题 / 简介 / Tag 生成(S9)

> 一句话:**文案不再另起一套时间。章节时间戳直接从 `markers[]` 生成,而 markers 跟着 Wordline 走——剪辑后自动正确。**

## 1. 为什么需要它

旧管线的末尾是「自动封面 → 中文化命名 → 交付」。用户拿到的是一条成片和一个封面,但**发布前还得自己想标题、写简介、编 Tag、数章节时间**——最费脑子的一步反而没有工具支持。

## 2. 输入与输出

**输入**:`05_ir/wordline.json`(正文与时间)、`00_brief/brief.md`(类型/平台/受众/术语表)、`04_cut/cut_report.md`(知道了删了什么,避免标题承诺内容没有的东西)。

**输出**:

- `06_output/metadata.json`(机器用,可直接投喂发布工具)
- `06_output/metadata.md`(人读,复制粘贴用)

```json
{
  "version": 1,
  "platforms": {
    "douyin": {
      "title": "3步搞定桌面运维排障,新手也能上手",
      "desc": "从蓝屏到网络不通,一套流程走完。",
      "tags": ["#桌面运维", "#IT运维", "#电脑维修", "#蓝屏", "#职场技能"]
    },
    "bili": {
      "title": "桌面运维实战:3步排查蓝屏与网络故障(附流程图)",
      "desc": "...",
      "chapters": [{"atMs": 0, "title": "开场"}, {"atMs": 42000, "title": "第一章:蓝屏排查"}],
      "tags": ["桌面运维", "IT支持", "运维", "..."]
    }
  }
}
```

## 3. 各平台规范

| 平台 | 标题 | 简介 | Tag |
|---|---|---|---|
| **抖音** | ≤ **30 字,前 8 字含钩子** | ≤ **55 字** | 5 个话题(`#` 开头) |
| **B站** | ≤ **80 字** | 分段简介 + **时间戳章节** | 10 个 tag |
| **视频号** | ≤ **22 字** | ≤ **120 字** | 3–5 个 |

**通用要求**:

- 标题承诺的内容必须在片中真的出现(看过 `cut_report.md` 后写);
- 禁用促销/导流措辞(星图规范);
- 术语按 brief 术语表统一(「统信 UOS」不写成「UOS 系统」)。

## 4. 章节时间戳(这一条最省事)

B站章节格式:

```
00:00 开场
00:42 第一章:蓝屏排查
02:15 第二章:网络不通
```

时间**从 `markers[]` 生成**:markers 挂靠在 Wordline 的锚点字上,而 Wordline 的 `final` 坐标已经过 `map_src_to_final()`——所以**粗剪、调序、换 TTS 之后,章节时间自动跟着变**,不需要重新数。

`rs_meta.py` 只需把 `markers[].atMs` 格式化为 `mm:ss`。

## 5. 用法

```powershell
python skills/cutflow/scripts/rs_meta.py --wordline 05_ir/wordline.json `
    --brief 00_brief/brief.md --cut-report 04_cut/cut_report.md `
    --platform douyin,bili --out 06_output
```

- 只要 `--platform` 里包含 `bili`,就会带 `chapters`;
- 无 `markers[]` 时章节降级为「按句群自动分章」并按每章 ≥30s 合并,且在 `metadata.md` 顶部标注「章节为自动切分,建议人工核」;
- 标题/简介由 **Agent 本人**撰写(不再接外部 LLM API),`rs_meta.py` 负责**校验 + 截断 + 落盘 + 生成章节**。

## 6. 门禁与验收

| 检查 | 通过线 |
|---|---|
| 字数合规 | 各平台标题/简介/tag 数全部在限内(超限自动截断并警告) |
| 章节一致 | `chapters[].atMs` 与成片实际章节位置一致(由 `rs_sync.py` 抽样比对) |
| 承诺一致 | 标题提到的要点在片中确有出现(`cut_report.md` 交叉核对) |
| 术语一致 | 无 brief 术语表之外的写法(抽样人工核) |
| 双格式 | `metadata.json` 与 `metadata.md` 同时产出 |
