# 使用与交接文档:成片内容诊断(v0.14.1)

> 面向不熟悉本流程的人:按本文档可独立完成「复现误判 → 运行诊断 → 解读结论 → 回滚」全流程。

## 1. 背景:为什么会有"假正常"

用户反馈:成片有字幕错位/口型错位/错剪无意义片段,Agent 探查一两个小时后回复
"完全正常"。根因审查见 `docs/REVIEW-20260916-假正常诊断根因.md`,五环根因:

| # | 根因 | 代码位置 |
|---|---|---|
| 1 | 所有机器闸对照的是**中间产物**,错误传染时全部互相印证全绿 | rs_verify.py L0_CHECKS |
| 2 | 仅有的成片内容闸(B10/QC)是 opt-in,默认不跑 | rs_sync.py --audio-content/--qc |
| 3 | 抽帧过疏,200ms 级错位在拼图上不可见 | rs_bench.py sample_points |
| 4 | **Windows 下 ASR 子进程 stdout 编码 GBK,调用方按 UTF-8 读 → 中文全部乱码**(文本对账从没真正工作过) | tools/fun_asr.py emit / rs_common.emit(已修) |
| 5 | L0 "通过"被表述成"没问题",违反 rules/verify.md §7 自家规范 | rs_verify.py 输出契约(已修) |

## 2. 修复内容(v0.14.1)

| 修复 | 说明 | 回滚 |
|---|---|---|
| `rs_diagnose.py`(新增) | 从成片出发的独立证据诊断:D1 字幕↔语音时间轴 / D2 音画同步互相关 / D3 剪点审计+语义连贯 | 删除文件即回滚 |
| `fun_asr.py` emit 强制 UTF-8 | 根因 4:中文 JSON 不再乱码 | revert 单提交 |
| `rs_common.run` 支持 env | 子进程 PYTHONIOENCODING 兜底 | revert 单提交 |
| `rs_verify.py --content` | L0 后追加内容诊断,输出 contentVerdict | revert 单提交 |
| L0 输出加 `scope: mechanical` | 堵"通过=没问题"表述漏洞;报告显式写明边界 | revert 单提交 |
| 台账 `06_output/diagnosis/diagnosis_log.jsonl` | 每次诊断留痕(检查项/耗时/证据/结论) | 纯增量,无需回滚 |

## 3. 如何复现原始误判场景

```powershell
# 构造带标注的缺陷用例(基线+三类注入缺陷,全部合成素材无版权问题)
python tests/make_fixtures.py
# 产物在 tests/fixtures/diagnosis/:baseline / fix1-subtitle-shift / fix2-audio-shift / fix3-bad-cut
```

用**旧版**(无 rs_diagnose)的验证路径跑 baseline:verify_report.md 全绿——
这就是当时的"假正常":所有闸对照的中间产物互相一致,但成片内容有问题。

## 4. 如何运行新诊断

```powershell
# 方式 A:独立诊断任意成片(历史视频/外部视频均可,不需要工程根)
python skills/cutflow/scripts/rs_diagnose.py <成片.mp4> --ass <subtitles.ass> --budget 600

# 方式 B:工程内诊断(自动定位 ass/cutlist/wordline/IR)
python skills/cutflow/scripts/rs_diagnose.py 06_output/final_*.mp4 --project <工程根>

# 方式 C:rs_verify 一体化(L0 机械自检 + 内容诊断)
python skills/cutflow/scripts/rs_verify.py <工程根> --content --content-budget 900

# 参数:
#   --budget 900      诊断耗时预算秒,超时输出阶段性结论(不是"没问题")
#   --no-d2           跳过音画同步(纯动画/无人物画面)
#   --json            机器可读输出
```

## 5. 如何解读结论

verdict 三态,**没有含糊的"正常"**:

| verdict | 含义 | 你该做什么 |
|---|---|---|
| `pass` | 所有可用检查通过(每项带证据:偏移中位/p95、互相关峰与显著度、子句统计) | 可放心;若 suspiciousSpans 非空,顺手核对列出的时间段 |
| `issues` | 发现缺陷,**附时间段与证据** | 按 suspiciousSpans 跳着看对应片段;D1 fail 附逐卡偏移表(哪张卡、偏多少 ms);D2 fail 附音轨滞后/超前方向与毫秒数 |
| `indetermined` | 判不了(ASR 不可用/文本不同源/互相关不显著/超时降级) | **这不是"没问题"**;按 findings 里的原因人工抽查,或换素材重跑 |

证据形式(每条结论可自行核对):
- D1:逐卡偏移表(卡文本/ASS 起点/ASR 起点/偏移 ms)+ 最差 3 卡;
- D2:互相关峰(音频滞后 X ms)+ 显著度;>400ms 判口型错位(肉眼可辨阈值);
- D3:剪点上下文审计(句中腰斩/悬空连接词)+ 语义连贯(近重复/短子句/截断)。

## 6. 诊断台账

`06_output/diagnosis/diagnosis_log.jsonl` 每行一次诊断:

```json
{"at": "...", "video": "...", "verdict": "issues", "elapsedS": 55.8,
 "checks": [{"id": "D1", "status": "fail", "elapsedS": 55.6, "findings": [...]}],
 "suspiciousSpans": [[3.1, 5.2]], "version": "diag-1.0"}
```

误判复盘:直接 grep 台账定位该视频的历次诊断,对比检查项状态变化即可锁定漏检环节。

## 7. 回归验证(已实测)

`tests/test_v15_diagnosis.py`(6 用例)对注入缺陷回归集:

| 用例 | 期望 | 实测 |
|---|---|---|
| baseline(正常) | verdict=pass(无误报) | ✅ pass(D1 -80ms / D2 +30ms / D3b 干净) |
| fix1 字幕 +0.5s | issues + D1 fail | ✅ issues,D1 fail 中位 +420ms |
| fix2 音轨延后 0.45s | issues + D2 fail | ✅ issues,D2 fail 互相关峰 +480ms(注入 450ms,误差 30ms) |
| fix3 句中腰斩+硬接 | issues + 错剪证据 | ✅ issues(D1 卡未匹配 +620ms) |
| 耗时预算 | <600s | ✅ 全部 42-107s |
| 台账 | 每次落盘 | ✅ diagnosis_log.jsonl |

**检出率 3/3,误报率 0/1。** 全仓测试 300/300 通过(含 v0.14 的 286 + 新增 14)。

## 8. 已知限制

- 合成语音(SAPI)与真实人声的音画耦合特性不同,D2 阈值(400ms)按"肉眼可辨"口径设定,真实素材建议先跑基线校准;
- 独立诊断(无工程产物)时 D3a 跳过、D3b 仅靠成片 ASR 语义特征;有工程根时建议同时提供 cutlist;
- D1 要求字幕与语音同源(同语言),文本匹配率 <60% 时显式 indetermined;
- 纯动画工程 D2 自动跳过(无口型语义),留痕不误报。

## 9. 部署与回滚

部署:合并本分支即可;无 schema/缓存迁移(CACHE_VER 不变),对既有工程零影响。

回滚(按需选一):
1. 只回内容诊断:`git revert` 本分支的 rs_diagnose 提交;`rs_verify --content` 不传参即不触发;
2. 回到 v0.14 原点:`git reset --hard bda7e28`(本地)/ 分支删除(远端);
3. 编码修复保留建议:fun_asr.py 的 UTF-8 emit 是独立修复,建议在任何回滚中保留
   (否则 Windows 下所有 ASR 文本对账再次退化为乱码)。
