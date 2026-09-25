# 风格包:知识口播·抖音竖屏(`knowledge-talkshow-douyin`)

> ADR-0051 风格包;registry 同名条目的参数真身。改参数 = 改这里,registry 只存索引。

## 调性

快节奏、大字幕、强结论。给「3 秒决定去留」的抖音知识受众:开场一句话承诺,正文金句化,每 2–3 秒一个视觉节拍。卡片视觉借 artboard `tech-kv`(深底玻璃拟态,克制、有科技感),片头尾与正片大字幕同源对齐。

## 适用

- videoType:`talking-head`(纯口播;绿幕/虚拟背景按 rules/video-types/纯口播.md);
- 平台/画幅:抖音 9x16(小红书 3:4 变体走 `knowledge-xiaohongshu` 条目,不归本包);
- 不适用:剧情类、混剪类、慢教程(分别去 drama-vertical / mixcut-douyin / tutorial-bilibili)。

## 禁则(≥5 条,每条带理由)

- **禁 BGM 盖过人声**(gainDb 高于 -12 仍全速铺底)—— 理由:口播的信息载体是人声,BGM 抢频段直接掉完播;本包 BGM 默认关,显式开也不得越过 fast 档 -18dB;
- **禁每卡超过 12 字 / CPS 超 9** —— 理由:9:16 竖屏一屏 12 字是可读上限,CPS≤9 是全库字幕三定律红线,超了必有人跟不上;
- **禁 0.3s 内连续双转场** —— 理由:口播观众对转场特技零容忍,连闪两次即"廉价感",掉粉点;
- **禁黑场/Logo 起手** —— 理由:前 3 秒定去留,黑场等于劝退;钩子必须是结论或冲突;
- **禁卡片压字幕带**(底部 30% 安全区)—— 理由:大字幕是本风格的识别度所在,卡片入侵字幕带会互相打架,机检(check_overflow --safe-area)也过不了;
- **禁无字幕** —— 理由:抖音大量静音刷屏场景,大字幕=第二音轨,关字幕等于砍掉一半信息。

## 参数出处速查

| 字段 | 值 | 出处 |
|---|---|---|
| pacing | fast(卡 900–3500ms,节拍 2–3s) | [内部] registry.pacingTiers.fast |
| bgm | 关(-18dB 仅显式开时生效) | [内部] registry.pacingTiers.fast;[经验] 口播人声优先 |
| subtitle | talkshow-bold / 12 字 / CPS 9 | [内部] registry 条目 style + platforms.douyin |
