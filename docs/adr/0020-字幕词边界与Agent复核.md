# ADR-0020:字幕词边界硬约束与 Agent 复核 override 闭环

状态:已采纳 ｜ 日期:2026-09-13（v0.8.1）

## 背景

用户实测(v2 重跑):两字词仍会被拆到上下两卡——「非常」切成 非|常,割裂感严重。

根因核验:`segmentation.py` **没有任何词边界概念**。切点候选只有标点/连词/≥200ms 停顿三类信号;词保护只有 12 个内置四字成语 + brief 传入的 terms;校对稿的空格(人工标注的词组边界)在卡切分层完全不消费。v0.7.0 的连词修复只治「而/但/并」类收尾,治不了任意两字词被拦腰切断。

## 决策

1. **词边界层**(`segmentation.word_spans()`):返回词跨度 `[(start,end))`;**jieba 优先**(延迟 import、失败静默降级),缺了降级**内置高频词表**(COMMON_WORDS,最长匹配)+ terms/idioms 显式并入。词**永不横跨空格**。
2. **词内位置 = 强禁切 + 两阶段 DP**:
   - ①词内位置与既有禁切表合并后跑 DP;
   - ②仅当全禁**无可行解**(极端长句在 max_chars 内放不下任何完整词边界)才降级为**词内强惩罚 −3.0**(WORD_CUT_PENALTY)重跑,并在结果留痕(`wordFallback` / `wordFallbackSentences` + degradeReasons)——宁可如实留痕,不可静默切词;
   - 仍无解才走既有单卡超字数报错路径。
3. **空格升级为切分信号**:空格**前禁切**(空格挂上一卡尾)、空格**后 = 强候选边界**、空格收尾 +0.3。
4. **Agent 复核修正闭环**(用户明确要求「Agent 再过一遍结果修正」):
   - `rs_subtitle` 卡片输出补 **`charSpan`**(wordline 内容字全局索引 `[a,b)`),并落盘 **`cards.json`** 供 Agent 审;
   - 新增 **`--override subtitles_override.json`**:Agent 只动 span(合并/拆分/微调边界),**时间永远从 wordline 字级锚重建**(时间唯一真相源不变),重跑必并/延长/间距/帧对齐与硬约束校验,`meta.audit` 逐卡留痕;
   - span 非法(越界/倒置/重叠)显式 `BAD_OVERRIDE` 报错(exit 2),不静默吞;
   - 契约与 Agent 检查清单写入 `rules/subtitles.md` §10。
5. **jieba 为可选依赖**:不装也能出片(内置词表兜底);`tools/fetch_deps.py subtitle` 安装到当前解释器;`rs_doctor` 增非致命检查。jieba 初始化日志**必须在重定向的 stdout 里**(否则污染 rs_* 的 `--json` 契约)。

## 后果

- 「两字词不跨卡」成为可测试的硬验收(§8 门禁 + `tests/test_v8.py` 词完整性断言);新发现的切词案例按「补词表 + 补 REGRESSION 用例」双保险处理;
- 词表覆盖不全的冷门词仍可能被切(jieba 缺席时)——这是如实降级,不是造假;jieba 在场时基本消除;
- override 让 Agent 修正**可机读、可回放、可审计**,且不破坏时间真相源;代价是多一个文件契约(rules §10);
- 极端输入(无标点无空格超长句)允许词内切但必须留痕。

## 关联

- `segmentation.py`(word_spans/COMMON_WORDS/两阶段 DP)、`rs_subtitle.py`(charSpan/cards.json/--override/events_from_override)、`rs_doctor.py`(jieba 检查)、`tools/fetch_deps.py subtitle`
- `rules/subtitles.md` §4.1/§4.2/§4.3/§4.7/§8/§10
- 依赖 ADR-0001(字幕规范)、ADR-0011(Wordline 时间源);延续 OPTIMIZATION-v7 #2(连词修复)
