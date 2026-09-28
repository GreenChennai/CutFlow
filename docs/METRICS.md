# METRICS — 度量看板(第五册 T5.4)

> 目的:把"质量趋势"变成**每轮可复算的数字**,而不是模糊感受。
> **更新纪律:每轮迭代收口时在「历史趋势表」追加一行;六项指标每轮都必须有值,
> 评分卡总分在 T5.1/T5.2 交付且 C 组端到端跑通前允许标「待填」——除该列外,
> **缺项即收口不通过**。每项指标必须绑定可复算命令,命令跑不出来 = 指标无效。**

---

## 一、回归规模(T5.3a 复核,2026-09-27 工作树)

- 复算命令:`python -m pytest --collect-only -q 2>/dev/null | grep -c "::"`
- 当前进值:**1596** 用例(v0.21.0 收口时点 2026-09-29 复测,判据 >1200:✓;默认套件 1592,
  4 个为 `e2e` 标记按 marker 排除;第五册 Q 组时点为 1579,数值漂移如实记录)
- 本轮新增(第五册 Q 组):`tests/test_postprocess_invariants.py`(**131**,
  后处理不变式薄弱域)、`tests/test_snapshots.py`(**14**,工程层快照)、
  `tests/test_marker_config.py`(**3**,marker 纪律)、`tests/e2e/`(**4**,e2e 冒烟)

### 域分布(按测试文件归属域;第五册 Q 组时点快照,1579 = 下表合计;收口时点 1596,新增 17 例随新增文件散入对应域,不逐域重算)

| 域 | 用例数 | 代表文件 |
|---|---:|---|
| 契约/治理/预算 | 386 | test_json_contract(166)、test_capabilities(109)、test_fetchable(44)、test_context_budget、test_v20_token_governance、test_perf_budget |
| 断句/分词/字幕 | 320 | test_no_text_rewrite_after_dp(48)、**test_postprocess_invariants(131,本轮新增)**、test_v17_break_tail(28)、test_tokenizers、test_segboundary_gold、test_segscore、test_stylepack |
| 跨域版本回归 | 280 | test_v4/v5/v6/v7/v8/v9/v10/v12/v13、test_med_fixes |
| 流程/状态机/缓存 | 126 | test_pipeline_states(23)、test_v19_manual_gate(35)、**test_snapshots(14,本轮新增)**、test_stages_contract、**test_marker_config(3,本轮新增)** |
| 素材/交付/出口 | 122 | test_deliverables(24)、test_v22_jy_backend(24)、test_drama_commentary(20)、test_sfx_semantics、test_assets |
| 渲染/特效/包装 | 86 | test_artboard_frames(19)、test_effects_usage(16)、test_effects(15)、test_render_parallel(7) |
| 端到端(合成素材) | 65 | test_m8_infra(31)、test_vlog_e2e(15)、**tests/e2e/test_smoke_e2e(4,本轮新增)**、test_mixcut_e2e、test_screen_e2e |
| 感知/画面理解 | 64 | test_matting(14)、test_vision_json、test_skeleton、test_value_frames、test_safe_area_content |
| 剪辑操作/编辑器桥 | 64 | test_edit_op(25)、test_edit_op_m14(20)、test_review_queue、test_editing_grammar |
| 自检/评分卡 | 38 | test_qc_scorecard(12,P 组)、test_v11(QA 闭环)、test_v15_diagnosis、test_ass_grid_single_source、test_false_green(P 组) |
| 意图编译/自动化 | 28 | test_v21_prompt_auto(22)、test_intent_effects |

**薄弱域结论**:断句/分词/时间锚/sync 对账各域均已成建制(专项文件 8–48 例 +
版本回归兜底);唯一明显薄弱的是 **textopt 后处理不变式**(此前仅 2 处间接覆盖),
本轮以 `tests/test_postprocess_invariants.py` 131 例补齐(字符守恒/分句还原/
体积上下界/零字符丢失/降级留痕/segscore 机械性质 + 语料基线)。

---

## 二、六项指标(当前值 + 可复算命令)

| # | 指标 | 当前值(v0.21.0 收口实测,分项注日期) | 门禁/目标 | 复算命令 |
|---|---|---|---|---|
| 1 | 上下文预算(SKILL+README 行数) | **448 行**(SKILL 169 + README 279);v0.21 收口 C 组真实素材全流程任务**实际读入 398 行**(对照名义最小必读 1469/治理前同口径 1552——检索+定点读取代通读) | ≤470 | 门禁 `python -m pytest tests/test_context_budget.py -q`;实测 `wc -l skills/cutflow/SKILL.md README.md` |
| 2 | 断句质量分 S(20 条语料) | **S = 0.907**(45 卡 / 0 违规;分量 punct 0.87 / word 1.0 / conj 1.0 / beat 0.73 / viol 1.0) | 防退化下限 ≥0.85 | 见下方〔复算 2〕;门禁 `python -m pytest tests/test_postprocess_invariants.py::test_segscore_corpus_baseline -q` |
| 3 | 评分卡总分 | **assetA=81 / assetB=77 / assetC=77**(2026-09-28 第二册 C 组真实素材端到端实测,全 ≥75 ✓;五维明细 D1 35/35/35、D2 16/16/16、D3 14/14/14、D4 8/4/4、D5 8/8/8) | ≥75 分(机械可判满分 91;人工 9 分另列签核,不计入机械总分) | `python skills/cutflow/scripts/rs_verify.py <成片工程> --score`;维表与扣分规则见 `docs/QC-SCORECARD.md` |
| 4 | 感知预算(skeleton 文本) | **2011 B**(3 分钟 / 72 镜夹具,压缩留痕后)≤2048 B ✓;零图片路径 | ≤2KB,零图片路径 | `python -m pytest tests/test_skeleton.py tests/test_edit_with_vision.py -q`(门禁);实测口径 = T3.3 夹具经 `rs_vision.build_skeleton`,emit message 自带字节数 |
| 5 | 阶段耗时(e2e 冒烟链) | 主链 S2→S11 墙钟 **≈58s**(收口复测 2026-09-29;第五册时点 ≈41s,机时波动如实记录);S9 对账 47.9s > S8 渲染 4.6s > S7 字幕 0.61s > S0 摄取 0.47s(8s 合成素材,final 档) | 趋势记录;渲染侧另见 test_perf_budget | `python -m pytest tests/e2e/test_smoke_e2e.py -m e2e -q` 后读该工程 `05_时间线工程/pipeline.json` 的 `stages.*.elapsedMs`(工程路径在 pytest 输出/temp 下) |
| 6 | 缓存命中率(冒烟链二次运行) | **8/8 = 100%**(自动可缓存阶段全命中;S0/S4/S11 人工处置、S5 声明跳过不计入;收口复测 2026-09-29 同值) | 二次全命中 = 增量引擎收敛 | `python -m pytest "tests/e2e/test_smoke_e2e.py::test_smoke_idempotent_second_run_all_cached" -m e2e -q` |

〔复算 2〕断句质量分 S(同输入必同分;jieba 确定性;语料 `tests/fixtures/seg_corpus.txt`):

```bash
python -c "import sys; sys.path.insert(0,'skills/cutflow/scripts'); import segmentation as sg; corpus=[l.strip() for l in open('tests/fixtures/seg_corpus.txt',encoding='utf-8') if l.strip() and not l.startswith('#')]; ps=[sg.segment(t,15) for t in corpus]; sents=[{'text':t,'cuts':p['cuts'],'gaps':{}} for t,p in zip(corpus,ps)]; durs=[len(c['text'])*0.18 for p in ps for c in p['cards']]; viol=sum(len(p['violations']) for p in ps); r=sg.segscore(sents,durs,viol); print('S=%.3f cards=%d viol=%d'%(r['score'],r['cards'],viol))"
```

---

## 三、历史趋势表(每轮迭代收口追加一行)

| 日期 | 用例总数 | 上下文预算(行,≤470) | 断句质量分 S(≥0.85) | 评分卡总分(≥75) | 感知预算(B,≤2048) | 阶段耗时(冒烟链主链) | 缓存命中率 |
|---|---|---|---|---|---|---|---|
| 2026-09-28(v0.21.0 多册迭代收口) | 1596(默认套件 1592;第五册 Q 组时点 1579) | 448 ✓ | 0.907 | **81 / 77 / 77 ✓**(assetA/B/C;判满口径 91+人工 9 另列) | 2011 ✓ | ≈58s(S9 47.9s 为最重;第五册时点 ≈41s) | 8/8 = 100% |

> 追加行时:六列全部要有值;评分卡总分在 C 组交付前可写「待填」并在下一轮补,
> 其余任一列缺值 = 该轮收口**不通过**(T5.4 判据)。数值漂移(如用例总数随
> 并行组增长)如实记录,不许为凑数扩底。
