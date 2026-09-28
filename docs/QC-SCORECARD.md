# 成片及格线评分卡(QC-SCORECARD,T5.1/T5.2)

> 把"成片是否达标"从模糊感受变成**可重复打分**的评分卡:五维 100 分制,
> 机械可判部分由 `rs_verify.py <工程根> --score` 自动算分,半机械维输出待人工项清单。
> 本文是**维表与扣分规则的文档单一出处**;代码实现在 `rs_verify.build_scorecard`。

## 0. 计分纪律(先读这个)

1. **机械可判满分 = 91,人工签核 = 9,合计 100。** 半机械维(③④)的 `[人]` 项
   不计入机械总分,单列"人工签核项"清单,交付前逐条确认。
2. **判据缺席(判据 skipped/降级)的分值既不得分、也不进"已判满分"分母** ——
   缺席不许冒充满分,这是假正常闭环(REVIEW-20260916)在计分面上的落点。
   输出面永远写 `总分 / 已判满分`,并附未判定项清单。
3. **每条结论带 scope**(T5.7):`机械` = 产物自洽;`实测` = 成片实测;
   `人工` = 须人工判定。L0 总判定只能表述为"机械闸通过(L0)",
   不等于"内容正确/观感合格"。
4. 阈值数值不在此复述(数值单一化纪律):同步线见 `rules/verify.md` §2
   与 `rs_sync.MEDIAN_MAX/P95_MAX`;单卡时长档见 `rules/subtitles.md` §8 与
   `rs_sync.DUR_MIN/DUR_MAX`;每卡字数/CPS 上限见 `templates/subtitle-policy.json`;
   响度目标见 `rules/compose.md` 与 `rs_render.LOUDNESS_TARGET`。

## 1. 五维维表

| 维度 | 满分 | 类型 | 计分项(分值) | 客观锚点 | 扣分规则 |
|---|---|---|---|---|---|
| ① 声画与字幕同步 | 35 | 机械 | 对齐起点中位达标(12);起点 95 分位达标(5);终点早退=0(8);滞留过久=0(3);未匹配卡=0(2);**Wordline 推导链重放一致·独立对账产物路(2)**;**字幕↔成片语音活动一致·独立对账成片路(3)** | `rs_sync.check_offsets` 偏移表;重放对账 `check_replay_remap`;成片实测 `check_subtitle_speech` | 任一项不达标该计分项计 0 分;独立对账判据缺席(无成片/探测降级)记未判定 |
| ② 字幕可读性 | 20 | 机械 | CPS 全达标(8);字数合规(6);无孤卡(内容字 <4 的卡,4);无时间重叠(2) | ASS 逐卡机械重算(评分卡自解析,不复用 check 内部结构);CPS/字数档 = subtitle-policy | 逐项判 0/满分;字数类违规看 violations 中"字数"条目;重叠含 summary.overlaps 与 violations"时间重叠" |
| ③ 剪辑连贯与节奏 | 20 | 半机械 | 剪点 guard 全过(8,机械);卡时长节拍达标率(6,机械连续计分:`round(6 × (1 − 短卡占比))`);剪点上下文语义连贯人工复核(4,人);整体节奏观感(2,人) | `check_cutlist`(guard=刀在静音区);短卡 = 时长低于必并线;人工项配 rs_diagnose D3a/D3b 工具面 | guard 未过 → 该项 0;节拍达标率按比例得分;人工项不进机械总分,进签核清单 |
| ④ 画面安全与观感 | 15 | 半机械 | 安全区几何(4,机械);安全区内容占位 SAFE_AREA_CONTENT(4,机械·实测);成片体检黑帧/冻结/VFR/响度(4,机械·实测);主体裁切/压脸/观感目测(3,人) | `check_safe_area`(平台 safeArea 几何);`check_safe_area_content`(第三册 T3.5 判据);`check_qc`(R1 体检档) | 任一判据不达标该计分项计 0;平台未声明/无成片 → 未判定(显式留痕);人工项进签核清单 |
| ⑤ 交付完整性 | 10 | 机械 | 成片产物存在(3);字幕产物+交付清单 deliverables.md(3);素材归因(说明书/素材归因.md,2);决策说明书(决策说明书.md,2) | `check_artifacts` + 交付目录文件存在性(rs_ingest deliverables / decisions 的产物) | 缺一件该计分项计 0 —— 本维没有"缺席即未判定"通道,交付时点必须齐全 |

## 2. 及格线门禁(T5.2)

- 命令:`python skills/cutflow/scripts/rs_verify.py <工程根> --score`。
- **总分 < 75(机械可判部分)→ 退出码 4(code=SCORE_FAIL)**,与 verifyLevel 缺失
  同等强度:不得宣称"完成"。
- 拦截输出必须包含:**总分/已判满分、低分维清单(逐维失分与失分项)、逐维诊断指引**
  ("怎么查/怎么修",写入 verify_report.md 评分卡小节)。
- 机械闸(L0)本身有红项时走 VERIFY_FAIL(先修红项再谈分数);
  `--score` 在同一轮 L0 结论上复用算分,不二次跑判据。

### 逐维诊断指引(拦截时输出)

| 维度 | 怎么查 | 怎么修 |
|---|---|---|
| D1 | sync_report.md 偏移表;rs_diagnose D1(成片 ASR 独立对轴);verify_report「独立对账」两项 | 整体偏移走 rs_align calibrate;字幕错位重跑 S7/S8;音频被替换回 S1 重转写 |
| D2 | sync_report.md 的 CPS/时长/孤卡明细 | 调 brief 每卡字数/CPS 重跑 S7;手改字幕工程走 06_成片输出/rebuild.py |
| D3 | cutlist guard 标红刀;rs_diagnose D3a 剪点上下文 | rs_cut 重切或改 action 后 `rs_cut.py --apply`,再从 S3 级联 |
| D4 | verify_report 安全区/体检小节;rs_bench 抽帧网格 | 卡片/Logo 落位 rs_artboard --apply 后级联;黑帧/冻结回查素材与拼接 |
| D5 | rs_ingest deliverables 缺失清单 | 补 metadata/清单、归因与决策说明书(rs_ingest decisions) |

## 3. 内容闸三态(T5.6a)

S9 装配**默认携带** `--audio-content`(B10 音频内容闸)。其结论以三态落账
(`sync_rows.json → summary.contentGate`,rs_verify 经 `check_content_gate` 回读):

| state | 语义 | 对总分/门禁的影响 |
|---|---|---|
| `pass` | 成片 ASR 与 wordline 对账一致 | 正常计分 |
| `fail` | 对账不一致(相似度低/片头句重复/重复段) | 内容闸红 → 机械闸红 |
| `degraded` | 真跑不了(无模型/无服务/无成片)——**显式降级留痕,不是通过** | 不翻红,但 L0 输出与报告必须带"降级留痕"标注与"绿灯不包含内容正确性"提示 |
| `absent` | 未请求 --audio-content 的独立直调(同样显式留痕) | 同 degraded 的可见性要求 |

禁止把 degraded 表述成干净的通过;补跑 ASR 对账或人工抽检后才可宣称完成。

## 附录 A:独立对账双路设计(T5.6c)

REVIEW-20260916 根因 1:全部对照闸都在"对照中间产物",管线内部错误会同时传染
wordline/ass/成片,产物互证全绿。闭环方案 = 对"内容正确性"建立**第二条推导路径**,
两路不一致即红:

### A.1 产物路:`check_replay_remap`(Wordline 推导链重放对账)

- **路 A(既有)**:ass ↔ wordline.final(工程产物互证,`check_alignment`)。
- **路 B(新增,独立重推导)**:从 `wordline.json`(源域)+ `cutlist.applied.json`
  的 keep 区间,重放 `rs_align.remap_wordline` 纯函数,与盘上
  `wordline.final.json` 逐字对拍(chars 时间戳/句子结构/finalDurationMs)。
- 单侧篡改源 wordline、final wordline 或 cutlist 任一而未同步重映射 → 重放不一致 → 红。
- 三处一起改 = "重新剪了一版",不在拦截语义内(那走全流程重跑)。

### A.2 成片路:`check_subtitle_speech`(字幕↔成片语音活动独立对账)

- **路 B(新增,成片实测)**:对成片音轨跑 `silencedetect` 实测语音活动区间
  (`rs_sync.speech_intervals`),不经任何工程中间产物,与字幕时间轴互推:
  1. **字幕是否压着语音说**(硬红):卡起点落在实测静默区的占比 ≥ 30%
     (字幕整体平移/烧录未同步的特征;`CARD_SILENCE_RATIO`);
  2. **字幕是否覆盖全部台词**(硬红):语音段起点(片头 0.5s/片尾 1.5s 白名单外)
     无字幕卡临近的占比 ≥50% 且 ≥2 处(整体缺卡/替换特征);
     语音**时长**覆盖率 <70% 仅告警 —— 机械口径无 ASR 分不清人声与底噪/配乐/垫尾,
     硬红误伤率不可接受(v0.22 端到端实测教训);
  3. **剪点是否在停顿处**(WARN 级):成片拼接点(cutlist keep 拼接位置,
     `final_joins_s`)落在实测语音活动内 → 留痕告警不翻红
     (配乐会填满停顿,硬红误伤率不可接受;线索交给人工与 rs_diagnose D3a)。
- 两路结论(产物路:字幕与 wordline 一致;成片路:字幕与成片实测语音一致)
  不一致即红 —— 这正是"两路互证"的门禁化。
- 探测不可用(无 ffmpeg/解码失败)→ `degraded` 显式留痕,绝不静默放行。

### A.3 三个"错误传染但旧闸全绿"样例的红判据(测试:test_false_green.py)

| 样例 | 传染路径 | 旧闸为何全绿 | 改造后红判据 |
|---|---|---|---|
| ① 源 wordline 被篡改,cutlist/成片未动 | 篡改只落在 wordline.json | 对齐闸读 final 域(ass↔final 依然一致);粗剪 guard 只看 cutlist 自身 | **产物路**:remap(篡改源, cutlist) ≠ 盘上 final → 重放对账红 |
| ② 成片域 wordline 被整体平移并重出字幕,成片未重烧 | 篡改同步落在 wordline.final 与 ass | ass↔wordline 互证一致;wordline 单调;guard 不涉及 | **成片路**:卡起点整体落入实测静默区 ≥30%、台词覆盖 <70% → 独立对账红(产物路重放对账同红:推导链已断) |
| ③ 成片音频被替换(静音/他轨) | 错误直接落在成片,产物没变 | 所有产物间闸不受影响,全绿 | **成片路**:实测语音活动缺失/覆盖率 <70% → 独立对账红 |

## 附录 B:价值选帧证据接入 S9 装配(T5.6b)

- 根因③(抽帧过疏)已由第三册 T3.9 价值选帧解决;第五册把它的**产物接进 S9 装配**:
  `rs_run._bench_cmd` 在 `04_粗剪决策/shots.json` 在册时给 `rs_bench` 追加
  `--shots`(镜头中点+运动峰值+字幕起点前 2 帧,硬上限 min(24, 3+镜头数)),
  并喂 `wordline.final.json`;缺席回退启发式档并留痕。
- `--auto` 的抽帧留证(`bench_evidence`)消费同一命令构造,决策留痕带 `mode=value|heuristic`,
  链路可审计;网格图仍只是 L1 证据,不构成目测判定。

## 复算命令

```bash
# 评分卡(含 L0 全量判据 + 五维算分 + 及格线门禁)
python skills/cutflow/scripts/rs_verify.py <工程根> --score

# 单判据复算(独立对账两路)
python - <<'PY'
import sys; from pathlib import Path
sys.path[:0] = [r"skills/cutflow/scripts"]
import rs_verify
root = Path("<工程根>")
print(rs_verify.check_replay_remap(root))
print(rs_verify.check_subtitle_speech(root))
PY
```
