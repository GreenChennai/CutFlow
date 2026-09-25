# 风格包:Vlog 旅拍·抖音竖屏(`vlog-douyin`)

> ADR-0051 风格包;registry 同名条目的参数真身。

## 调性

生活流、情绪骨架、轻干预。BGM 承担情绪,碎片镜头自然落位;不做信息卡轰炸,人设比单条更重要。卡片视觉借 artboard `xhs-cover`(暖底贴纸手账感),片头目的地卡与旅拍气质同源。

## 适用

- videoType:`vlog`(旅拍/日常/碎片记录,无连续口播稿);
- 平台/画幅:抖音 9x16,30–90s 完播优先;
- 不适用:有主线解说的知识内容(去 knowledge-talkshow-douyin)、音乐驱动的素材混排(去 mixcut-douyin)。

## 禁则(≥5 条,每条带理由)

- **禁环境音段出字幕** —— 理由:vlog.md §3:字幕只跟人声对白,纯 BGM/环境音段出字幕=空转字幕,机检会红,观众也觉得莫名其妙;
- **禁默认加信息卡**(density 无却私加 stat/compare)—— 理由:vlog 的信息卡只做地名/时间戳角标与订阅引导,信息卡轰炸破坏生活流感(本包 cards.density=无);
- **禁花哨转场连用** —— 理由:vlog.md §2:硬切为主、场景大切换才用 fade,转场特技连用会把"记录感"变成" engineered 感";
- **禁 BGM 盖过人声段**(ducking 失效仍硬铺)—— 理由:BGM 是情绪骨架不是主角,人声段 ducking 已由 rs_render 固化,人工抬增益必须显式改 IR 留痕;
- **禁黑场起手** —— 理由:开场放最漂亮的空镜或结果前置(前 3 秒定去留),黑场/Logo 起手直接劝退;
- **禁横屏素材拉伸上竖屏** —— 理由:必须走 reframe 锚点(reframe.anchorY 由 Agent 定),拉伸变形是 L1 目测必红项。

## 参数出处速查

| 字段 | 值 | 出处 |
|---|---|---|
| pacing | normal(卡 1500–5000ms,节拍 2–5s) | [内部] registry.pacingTiers.normal + vlog.md §2 |
| bgm | 开(-18dB,人声段 ducking) | [内部] vlog.md §2 |
| subtitle | talkshow-bold / 12 字 / CPS 9 / 仅人声 | [内部] vlog.md §3 + platforms.douyin |
