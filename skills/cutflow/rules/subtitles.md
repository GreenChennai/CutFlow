# Subtitles — 字幕(Netflix 规范 + 抖音红线 + 两层切分)

> **ADR-0001(修订) / ADR-0002**。一句话:**卡与卡之间怎么切(segmentation)和一张卡内怎么折行(line break)是两件事,旧版本把它们混成了一件。**

## 1. 权威依据

- [Netflix 简体中文 Timed Text Style Guide](https://partnerhelp.netflixstudios.com/hc/en-us/articles/215986007-Chinese-Simplified-Timed-Text-Style-Guide):每行 ≤16 字、最多 2 行;**句号/逗号不入屏**(断点用空格);问号/感叹号保留且禁 `!?` 连用;顿号可列举行中、不入行尾;省略号统一 `…`(U+2026);全角引号;一至十用汉字、其余半角数字。
- [BBC Subtitle Guidelines](https://www.bbc.com/accessibility/forproducts/guides/subtitles/)(**断句定论**,原文):*Sentences should be segmented at natural linguistic breaks such that each subtitle forms an integrated linguistic unit. Thus, **segmentation at clause boundaries is to be preferred.** … There is considerable evidence from the psycho-linguistic literature that normal reading is organised into word groups corresponding to syntactic clauses and phrases, and that linguistically coherent segmentation of text can significantly improve readability. **Random segmentation must certainly be avoided.***
- [抖音低质内容判定](https://m.bjnews.com.cn/detail/1716003662168785.html):字幕**无错别字**、**时间轴与画面同步**是硬红线;
- [星图营销平台制作规范](https://www.xingtu.cn/help-center/demander/109176):字幕不得出现促销/导流信息;
- 字幕 onset 应在语音起始后 **1–2 帧**内 —— 这是通过线,不是"差不多就行"。

## 2. 两层分离(核心修订)

| 层 | 问题 | 旧实现 | 新实现 |
|---|---|---|---|
| **卡切分** segmentation | 一句话切成几张卡 | **纯长度驱动**(≤16 字) | **约束最优 DP**(§4) |
| **行断开** line break | 一张卡内怎么折行 | 评分算法(标点+100/空格+90/ASCII 切断 −200/每填一字 −4) | **保留**,补「金字塔形、禁顶行 1–2 字」 |

「滚滚长江东逝水」被切成「滚滚长」/「江东逝水」正是**长度驱动切分**的典型病理:某处刚好到字数上限就硬切,既不认专名(长江),也不认诗句整体性。BBC 的原文就是对它的判决:**Random segmentation must certainly be avoided.**

## 3. 轻改写引擎(textopt.py,默认开启)

`--no-optimize` 可关闭。

1. 删句首 filler(嗯/呃/唉…)与句尾语气字(啊/嘛/呢/吧);
2. 标点:句号丢弃、全半角逗号在卡内转空格、`……`→`…`、禁 `!?` 连用;
3. 卡级切分走 **DP**(§4),ASCII 词内禁切;
4. **卡的时间从 Wordline 聚合**(§5),**已彻底废除「按字符数比例分摊」**。

**铁律**:只动标点/口水词/断句,不改语义不删信息(音频不动,轻改写定义见 ADR-0002)。

## 4. 卡切分算法(约束最优 DP)

### 4.1 候选边界集合(只有这些位置可以切)

- **强标点**:`。！？；`
- **弱标点**:`，、：`
- **字级停顿**:Wordline 中相邻字之间 `gap ≥ 200ms`
- **句法线索**:连词/时间副词之前(「然后」「所以」「但是」「接下来」)

### 4.2 禁止边界(硬约束,直接排除)

| 禁切 | 例子 |
|---|---|
| 专有名词内部 | brief 术语表 + NER 名单:「长江」「抖音」「GPT-SoVITS」 |
| 成语/固定搭配内部 | 常用成语表;至少做 **4 字整体保护** |
| 数量词 + 量词/单位之间 | 「三十五」/「岁」、「三千」/「万」 |
| 数字与单位/百分号/货币之间 | 「9」/「秒」、「¥」/「1999」、「50」/「%」 |
| `的 / 地 / 得 / 了 / 着 / 之` 之后 | 「美丽」/「的风景」 |
| **连词/引导字收尾**(v0.7.0) | 「而 / 但 / 并 / 且 / 或 / 及 / 与 / 则 / 却 / 故 / 因 / 若 / 虽 / 如 / 由 / 然 / 所」结尾:会切开「而被 / 而且 / 但是 / 然后 / 因为 / 所以」 |
| ASCII 词内部 | 「build123」 |

### 4.3 打分函数(越大越好)

```
score(cut, left, right) =
    + 2.0 * 标点层级(。！？=1.0, ；=0.8, ，=0.6, 、=0.4, 无=0.0)
    + 1.5 * 归一化停顿( gapMs / 500ms, clip 到 [0,1] )
    + 0.5 * 不以虚词(TAIL_FUNC)结尾
    - 2.0 * 以连词/引导字(NO_TAIL)结尾        ← 强惩罚,防切词
    + 0.5 * 以连词(CONJ_HEAD)起首            ← 从句边界优先(BBC)
    - 0.5 * 非候选边界
    - 0.8 * 长度失衡惩罚( |chars_left - chars_right| / max_chars )
    - 1.2 * 尾卡过短惩罚( 卡字数 < 4 时线性惩罚 → 防「悬一字」 )
    - 1.0 * 每切一刀的固定代价(CUT_COST)      ← 防为了拿语义加分而切成一片 4 字卡
```

> **v0.7.0 修订(OPTIMIZATION-v7 #2)**:旧打分把「以连词开头」当**缺点**(`not in CONJ_HEAD` 才 +0.5),与本节引用的 BBC「从句边界优先」正好相反,于是 DP 会选出「…店铺违规**而** / 被连带处理…」。现在改为**连词起首加分 + 连词收尾强惩罚**。惩罚而非硬禁切,是为了避免极端句无解(DP 无候选 → 单卡超字数)。同时新增 `CUT_COST`,治"过度切分"(一句被切成多张 4 字卡同样是「断句拉跨」)。

DP 目标:在**所有合法切分方案**中最大化**总分数**,同时满足 §4.4 全部硬约束。

### 4.4 硬约束(不满足即该切分方案非法)

| 约束 | 值 | 依据 |
|---|---|---|
| 每卡字数 | 9:16 **10–12 字**;16:9 **20–22 字** | Netflix 的 16 字是**横屏**标准;竖屏 CJK 屏宽只有约 60%,行业建议 8–10 字 |
| **CPS** | ≤ **9 字/秒**(成人);儿童 ≤7;SDH ≤11 | Netflix 简体中文 |
| 单卡时长 | **[0.83s, 7s]**(>7s 硬失败;见下) | Netflix min 5/6s / max 7s |
| 卡间距 | ≥ **2 帧** | 行业通行 |
| 视觉节拍 | 卡时长宜落 **1.5–3.5s**;>4s 必切,<0.8s 必并 | ADR-0004 的 2–3s 节拍 |

> **最短时长是软约束,对齐精度是硬约束。** 实施中发现两者会冲突:给过短卡硬补时长的同时会和下一卡重叠。故定序为——
> ① `<0.8s` 先**合卡**(与相邻卡合并,前提是不超字数上限);
> ② 合不了则**在有余量时延长后沿**,绝不越过下一卡起点;
> ③ 仍不足则**如实告警**(`sync_report.md` 的"时长过短"栏),但不阻断交付。
> **起点永远不变**——起点决定对齐精度,不能为了凑时长去挪它。

> **竖屏字数为什么下调**:Netflix 16 字是 16:9 标准,`talkshow-bold` 直接用 16 字在 1080×1920 上会顶满安全区。`maxChars` 不写死,由**比例 + 字号**推导。旧工程按 `pipeline.json` 记录的 `maxChars` 复现,不追改。

### 4.5 「节奏感」的正解

节奏**不是靠"断得短"**产生的,是靠**卡时长落在视觉节拍区间(1.5–3.5s)**产生的。「滚滚长江东逝水」作为完整的 7 字诗句,一张卡 2s 念完,符合节拍;切成 3+4 反而破坏语感。

> **长度约束服务于节拍,而不是反过来。**

### 4.6 多假设输出

DP 求最优后返回 **top-3 候选**(按 score 排序)供 Agent 挑选;若最优与次优 score 差 < **5%**,标记 `ambiguous` 并请 Agent 决断——这比"悄悄选一个错的"好得多。

### 4.7 回归测试集(必须固化,这是质量的量化手段)

| 用例 | 期望 | 防护机制 |
|---|---|---|
| `滚滚长江东逝水` | 不得切成「滚滚长」/「江东逝水」 | 专名表 + 尾卡过短惩罚 + 完整句优先 |
| `我今年三十五岁` | 不得切成「我今年三十」/「五岁」 | 数量词 + 单位保护 |
| `我们把那个…那个什么…对,做完了` | 删口水词后重新对齐,不产生空卡 | 轻改写 → Wordline 重聚合 |
| `¥1999 元` | 不切 | 数字 + 单位保护 |
| `用 GPT-SoVITS 做配音` | 不切 ASCII 词 | 已有规则 |

回归集写在 `tests/` 里,批次 C 的验收线是**全绿**。

## 5. 卡的时间来源(废除比例插值)

> 详见 rules/align.md。**这是解决「字幕/声音/画面对不上」的关键。**

| 卡的时间 | 来源 |
|---|---|
| `start` | 该卡**首字**的 `startMs` − 20ms 释放余量 |
| `end` | 该卡**末字**的 `endMs` + 20ms 释放余量 |
| 单字卡 | 最短时长补足到 **0.83s**(Netflix 最短时长) |
| 相邻卡 | 间距 ≥ 2 帧;不足则前卡收早 / 后卡推迟 |

> **释放余量边界(v0.7.0,OPTIMIZATION-v7 #1)**:卡时间锚定在字级戳上,**任何"为凑卡间距/最短时长"的调整都只能在释放余量内进行** ——
> 起点不得晚于首字 `startMs`,终点不得早于末字 `endMs`(否则会切掉末字语音 = 字幕"偏快");
> 为凑最短时长延长后沿时,上限 = 末字 `endMs` + **0.30s**(否则字幕会滞留到停顿里 = "偏慢")。
> 余量耗尽仍不足 2 帧 → **保持字级精确时间**(对齐精度 > 卡间距)。

**明令禁止**:`时長 * len(card) / total_chars` 这类写法(旧 `build_events` 的 optimize 分支)。语速不均时它必错,而且是系统性误差。

## 6. 用法

```
# 首选:从 Wordline 取时(字级精确)
rs_subtitle.py --from-wordline 05_ir/wordline.json --style talkshow-bold --ratio 9x16 --out 06_output

# 兼容:纯文案 TTS(时间 = 合成实长,ffprobe 实测)
rs_subtitle.py --from-tts 03_assets/tts/manifest.json --style tutorial-clean --ratio 9x16 --out 06_output

# 兼容:无 Wordline 的降级路径(句级时间 + 停顿锚点,会在报告里标注 degraded)
rs_subtitle.py --from-transcript 02_sensed/transcript_corrected.json --style subtitle-white --ratio 16x9 --out 06_output
```

- `--from-transcript` 必须用 **Agent 校对后的** `transcript_corrected`(原始 ASR 错字是抖音红线);
- `--segment dp`(默认)/ `--segment length`(回退旧长度驱动,仅用于复现旧工程);
- `--top 3` 输出候选方案到 `06_output/segments_candidates.json`,供 Agent 决断 `ambiguous` 卡。

## 7. 风格

| style | 用途 | 特点 |
|---|---|---|
| talkshow-bold | 口播/绿幕 | 大字居中偏下、粗体黑边、9:16 marginV=500 / 3:4 marginV=400 / 16:9 marginV=120 |
| tutorial-clean | 教程 | 底部半透明底条(BorderStyle=3) |
| subtitle-white | 通用白字黑边 | |

安全区(9:16):底 25%、顶 12% 不放字幕;3:4 用底 18%、顶 10%(小红书);16:9 用底 16%、顶 8%。**Logo 同样不得进入字幕带**。
**平台预设见 `rules/platforms.md`** —— `rs_subtitle --platform <douyin|shipinhao|xiaohongshu|bilibili>` 会一次定下比例/画布/字数/风格,显式 `--style/--ratio/--max-chars` 优先。

## 8. 门禁与验收

| 检查 | 通过线 |
|---|---|
| 断句回归 | §4.7 五条用例全绿 |
| CPS 合规 | 抽样 20 卡,CPS 全部 ≤ 9 字/秒 |
| 竖屏可读性 | 9:16 每卡 ≤ 12 字,无顶行 1–2 字 |
| 单卡时长 | `>7s` 必须为 0(硬失败);`<0.83s` 应尽量为 0,残余项可由 `sync_report.md` 解释 |
| 对齐偏移(起点) | 中位数 ≤ 40ms、95 分位 ≤ 80ms(由 `rs_sync.py` 断言) |
| **对齐偏移(终点)**(v0.7.0) | 中位数 ≤ 60ms、95 分位 ≤ 120ms;**「早退」(终点早于末字 >25ms)= 0**;**「滞留过久」(终点晚于末字 >350ms)= 0** |
| **连词不落卡尾**(v0.7.0) | 抽样 20 句含连词的句子,连词在卡尾的比例 = **0** |
| 错别字 | 0(校对后文本;抖音硬红线) |

## 9. Karaoke 逐字字幕(v0.6.0)

- 开关:`rs_subtitle.py --karaoke`(可加 `--allow-degraded` 让降级 wordline 退回普通卡而不是报错);需 pkg 后端字级时间戳。
- 原理:每字一个 ASS `\kf` 标签(时长=厘秒,取自 `chars[]` 字级时间戳,**字间停顿计入前字**);首字从卡头起唱、末字吃到卡尾,`_kar_text` 产出 `{\kf40}你{\kf60}好` 形态。
- 染色:已唱 primary `&H0000E5FF`(暖黄,**ASS 是 BGR 顺序**,从 RGB 换算后再写)、未唱 secondary 白。
- **纯标点/空白文本段必须跳过**(`_PUNCT_ONLY` 集合):孤立 `。?!` 不成卡。注意不能拿 `_clean_card` 当 skip 判据——它对 `?` 返回 `?` 是设计(保留语气),不是空卡。
- **挂字时序铁律(v0.6.0 实测)**:挂字必须在**必并/合规校验之前**(`events_from_wordline(karaoke=True)` 内置)——`_clean_card` 剥掉的标点会在 `\kf` 显示层经 chars 原样带回,预算若只数清洗文本会漏 1-2 字形(实测冒出 13-14 字卡)。挂字后 `e["text"]` = chars 拼接,与 rs_verify 计数同口径;合并事件必须同步拼接 `chars`,否则 `_kar_text` 丢字。
- **必并线 = MIN_DUR_S(0.83s)**:与单卡时长下限同线。旧 0.8s 线有 0.03s 死区——0.81s 卡既不触发必并、又延不满(下一卡 2 帧间隙就到,延长被 `_enforce_gaps` 收回,L0 硬失败);预算放不下时第二遍**向下一卡吞并**,起点取短卡(对齐精度不动)。
- 实测锚点:625 字 → 77 卡全 `\kf`,无缺 startMs、无标点孤卡、无领头标点卡、无超 12 字形卡(问题 #3/#8/#10/#11 修复后;rs_verify L0 全过)。
