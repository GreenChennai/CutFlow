# 风格包:教程·B站横屏(`tutorial-bilibili`)

> ADR-0051 风格包;registry 同名条目的参数真身。

## 调性

慢节奏、章节感、讲解优先。给「来学东西」的 B站观众:步骤推进、图表说话、卡片做章节骨架。卡片视觉借 artboard `data-longform`(报告型版式,ECharts 图表语言),标题卡与章节卡和正片教程气质一致。

## 适用

- videoType:`talking-head`(口播讲解型教程;纯演示无讲解去 screen-tutorial);
- 平台/画幅:B站 16x9(3–10min 教程档);
- 不适用:抖音快知识(去 knowledge-talkshow-douyin)、录屏操作演示(去 screen-tutorial)。

## 禁则(≥5 条,每条带理由)

- **禁 BGM 压过讲解声**(显式开 BGM 时 gainDb 高于 -20)—— 理由:教程的信息密度在讲解词里,BGM 抢声等于让人重听一遍;slow 档 -20dB 是底线;
- **禁节奏快过 slow 档**(卡时长 <2.5s 或节拍 <3s)—— 理由:观众要跟操作,快节奏只适合娱乐内容,教程快了=看不懂=弃剧;
- **禁每卡超过 22 字 / CPS 超 9** —— 理由:22 字是横屏单屏上限,CPS≤9 全库红线;教程术语多,超字数必然跳读;
- **禁跳步骤**(章节卡编号断档)—— 理由:教程的可信度来自完整步骤链,断一章观众就要暂停去猜;
- **禁卡片挡演示主体**(画面中上部操作区)—— 理由:观众眼睛在操作区,卡片入侵即遮挡关键信息,安全区机检也过不了;
- **禁字幕样式换档**(中途换 subtitle token)—— 理由:教程时长长,样式漂移会被观众感知为"拼接感",且破坏 talkshow/tutorial 样式体系的一致性。

## 参数出处速查

| 字段 | 值 | 出处 |
|---|---|---|
| pacing | slow(卡 2500–7000ms,节拍 3–5s) | [内部] registry.pacingTiers.slow |
| bgm | 关(-20dB 仅显式开时生效) | [内部] registry.pacingTiers.slow;[经验] 讲解优先 |
| subtitle | tutorial-clean / 22 字 / CPS 9 | [内部] registry 条目 style + platforms.bilibili |
