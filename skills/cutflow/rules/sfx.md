# Sfx — 音效自动落点(S6)

> 一句话:**把「手动挂音效」变成「Agent 出草案 + 人审」。音效不是撒得越多越好,是落在正确的位置上。**

## 1. 为什么需要它

旧管线的音效能力是:IR 支持 `src: "assets_sfx:whoosh"` 伪协议 + 仓库 `assets/sfx/` 里 7 个 Mixkit 音效(CC0,见 `assets/sfx/CREDITS.md`)。**全都要人手动决定"在哪里挂什么"**——这在 3 分钟的片子里意味着几十次判断,通常的结果是要么忘了挂,要么挂得杂乱。

自动落点的价值不是"替人省力气",而是**把落点规则显性化**:什么事件配什么音色,写下来就一致了。

## 2. 落点规则表

| 落点类型 | 触发条件 | 音色 |
|---|---|---|
| **转场** | CutList 切点 / 卡片切换点 / 场景变化点 | `whoosh` / `swipe` |
| **强调** | 关键词高亮词出现处(IR.subtitle.highlight 或 artboard 强调卡) | `ding` |
| **列举** | 卡片内出现「第一/第二/第三」序号 | `click` / `pop` |
| **章节** | `markers[]` 的章节标记 | `riser` / `bell` |
| **结尾** | 最后一张卡片 / 片尾 | `bell` |

内置音色(仓库 `assets/sfx/`):`whoosh` `swipe` `pop` `click` `ding` `riser` `bell`。

## 3. 密度约束(防噪)

> **每 15s 内音效 ≤ 2 个。**

超出时按以下优先级保留:

1. 章节/转场(结构性强)
2. 强调(语义相关)
3. 列举
4. 氛围类

被丢弃的候选写入 `06_output/sfx_dropped.md`,可人工捞回——**不静默丢弃**。

## 4. 音量与混音

- 音效峰值**不得掩人声**:走 sidechain(人声触发 ducking),或音效 `gainDb ≤ -12`;
- 所有落点对齐 Wordline 的**锚点字**时间(不是"大概差不多"),相差超过 1 帧视为未对齐;
- 音效素材优先**本地资产**(ADR-0005);缺失时如实上报,不引入在线素材下载。

## 5. 用法

```powershell
# 出草案(不改 IR,只产建议表)
python skills/cutflow/scripts/rs_sfx.py 05_ir/project.json --auto --out 05_ir/sfx_draft.json

# 人工审完后合并进 IR 的 audio.clips[] (role: sfx)
python skills/cutflow/scripts/rs_sfx.py 05_ir/project.json --apply 05_ir/sfx_draft.json
```

草案结构:

```json
{"version": 1,
 "placements": [
   {"atMs": 12400, "src": "assets_sfx:whoosh", "trigger": "cut", "anchorChar": 41,
    "conf": 0.9, "gainDb": -14, "note": "CutList 切点 c001"}
 ],
 "dropped": [{"atMs": 26800, "src": "assets_sfx:ding", "reason": "15s 窗口内已 2 个"}]}
```

## 6. 门禁与验收

| 检查 | 通过线 |
|---|---|
| 密度 | 任意 15s 窗口内音效数 ≤ 2 |
| 落点精度 | 全部对齐锚点字,偏差 ≤ 1 帧 |
| 人声不被掩 | 人声段响度无下降超过 1 LU(sidechain 生效) |
| 可追溯 | 每条音效有 `trigger` 与 `anchorChar`,可反查落点理由 |
| 丢弃可见 | `dropped` 非空时一定有 `sfx_dropped.md` |
