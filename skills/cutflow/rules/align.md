# Align — 字级对齐与 Wordline(S1)

> **ADR-0011**。一句话:**全片每一句话的每一个字,只有一份时间;这份时间写在 `wordline.json`,别处一律派生。**

## 1. 为什么需要它(问题的根因)

旧管线里时间有**三个互不隶属的来源**:

| 元素 | 旧时间来源 | 误差量级 |
|---|---|---|
| 画面 | 人(Agent)手写 IR `sourceInMs` | 无校验 |
| 字幕(口播) | ASR **句级**时间戳 + **按字数比例插值** | ±300–800ms | 
| 字幕(TTS) | 各句 wav 时长**累加** | 累加漂移,句内无位置 |

再叠加两个放大器:① 校对「只改文本不动时间戳」,删字后字符数变了却仍按新字数摊到旧时间戳;② 一旦粗剪,时间轴整体重映射,三者各自重算 → 必然出现「字幕对了画面错、画面对了字幕错」。

**结论:只要存在多于一个时间源,「对上」只能靠巧合。** Wordline 就是把这个数量压回 1。

## 2. 数据结构(`05_ir/wordline.json`)

```json
{
  "version": 1,
  "source": "01_materials/JJAV2815.MP4",
  "space": "final",
  "fps": 30,
  "sampleRate": 16000,
  "srcDurationMs": 45200,
  "finalDurationMs": 41020,
  "chars": [
    {"i": 0, "ch": "大", "startMs": 880,  "endMs": 1120, "srcStartMs": 880,  "srcEndMs": 1120, "conf": 0.97},
    {"i": 1, "ch": "家", "startMs": 1140, "endMs": 1360, "srcStartMs": 1140, "srcEndMs": 1360, "conf": 0.95}
  ],
  "gaps": [{"after": 41, "ms": 320, "kind": "silence"}],
  "sentences": [{"id": 0, "span": [0, 41], "punc": "。", "text": "欢迎大家来体验"}],
  "speakers": ["说话人0"]
}
```

### 关键字段语义

| 字段 | 含义 | 约束 |
|---|---|---|
| `space` | 坐标域:`source`(源素材)或 `final`(成片) | 粗剪前是 `source`,S2 之后重映射为 `final` |
| `startMs` / `endMs` | **成片域**时间,字幕/动画/音效/Lo 唯一的取时来源 | 单调不减,`endMs > startMs` |
| `srcStartMs` / `srcEndMs` | **源域**时间,粗剪重映射的锚 | 用于 `map()` 的输入 |
| `conf` | 字级置信度(Paraformer 提供) | 低置信字在校对时优先展示给 Agent |
| `gaps[].kind` | `silence` / `breath` / `pause` | 停顿是断句候选边界(见 rules/subtitles.md) |
| `sentences[].span` | 句 = `chars` 的下标区间 `[起, 止)` | 句边界来自 ASR 标点模型 |

## 3. 三条入口路径(统一落到同一结构)

| 素材 | 字级来源 | 说明 |
|---|---|---|
| **口播视频**(首选) | **FunASR Paraformer 原生 `timestamp`** | 一次调用即得逐字 `[[880,1120],...]`;`res[0]["text"]` 与 `res[0]["timestamp"]` 逐字对应 |
| **纯文案 TTS** | 逐句 wav **ffprobe 实测**时长 → 句级真实区间;字级由 **`rs_dub align`** 强制对齐补上 | 实测时长可用;句内位置**不许当字级用** —— 未对齐前标 `charTimingEstimated`(见 §5) |
| **兜底** | `fa-zh` 强制对齐 | ⚠️ **慎用**,见 §5 |

### 3.1 取 Paraformer 原生字级时间戳(性价比最高的一步)

```python
from funasr import AutoModel
model = AutoModel(model="paraformer-zh", vad_model="fsmn-vad", punc_model="ct-punc")
res = model.generate(input="audio.wav")
res[0]["text"]       # "欢 迎 大 家 来 体 验"
res[0]["timestamp"]  # [[880,1120],[1120,1360],[1380,1540], ...]  逐字 [start_ms, end_ms]
```

**现状缺口**:CutFlow 经 MomentShift `POST /v1/audio/transcriptions`(带 `structured=1`)拿回的是句级 `[12.3s] 说话人0: 台词`——**引擎本来有字级能力,被服务封装层丢掉了,客户端又用比例插值把丢掉的信息猜回来**。这是纯信息浪费。

**行动项**(与 BACKLOG 上游 PR 同类):向 MomentShift `asr_server.py` 增补 `char_timestamps=1`,把 Paraformer 的 `timestamp` 字段透传出来;CutFlow 侧 `rs_align.py` 优先消费该字段,取不到时自动降级为**句级 + 停顿锚点**模式并在 `data.degraded` 中标注(附降级原因),**不静默降级**。

### 3.2 校对后必须重聚合(修订旧规则)

旧规则「校对只改文本不动时间戳」**已废除**。正确流程:

1. Agent 逐句校对(改错字/删口水词/补漏字);
2. 用**字级锚点**把校订文本重新对齐到 `chars`:文本中未被改动的连续片段沿用原字时间戳;新增字取相邻字区间均分(仅此一处允许均分,且区间 ≤ 3 字);删除字直接把其时间并给相邻字;
3. 某句明显漏识别 → 合并到下一句并在 `sentences[].note` 注明,该句 `conf` 置 `0`;
4. 输出 `transcript_corrected.md`(人读)+ `wordline.json`(机器用),两者必须同源同版本。

## 4. 重映射函数(唯一允许的时间换算)

由 CutList 的 `keep` 区间生成**分段线性映射**,`rs_align.py` 导出:

```python
def map_src_to_final(t_src_ms: float, segments: list[dict]) -> float:
    """segments: [{"srcIn":0,"srcOut":12400,"finalIn":0}, ...] 按 srcIn 升序。
    落在被删区间的时间 → 吸附到该区间起点的成片位置(避免产生非法时间);"""
```

规则:

- **所有下游模块必须调它**(字幕/动画/音效/Logo/章节 marker),不得各自计算;
- 落在被删除区间内的 `src` 时间,**吸附**到该删除段对应的 `final` 位置,不得插值穿透;
- 映射结果必须单调不减;`rs_sync.py` 会抽样断言这一点。

## 5. 已知坑(必须遵守)

| 坑 | 后果 | 对策 |
|---|---|---|
| **`fa-zh` 系统性偏移** | 字级时间整体偏 ~1.3s | FunASR issue #2784:`fa-zh` 在 VAD 段内对齐,返回的相对时间**未加回切片起点**。故:①优先 Paraformer 原生 `timestamp`;②必须用 `fa-zh` 时,回填 VAD 切片起点 + 跑偏移自检(与原生结果比对,中位偏移 >200ms 即判定不可用) |
| 比例插值 | 语速不均时必错 | 已明令禁止;`rs_subtitle.py` 不再提供该路径 |
| 句级时间戳当字级用 | 句内位置全靠猜 | 必须走字级对齐;降级模式需在报告中标注 |
| 工具版本漂移 | 换模型后时间全变 | 缓存键含 `model` / `punc` / 服务版本(ADR-0013) |
| **旧覆盖公式语义颠倒** | 真实字级时间戳恒判 ~75% 不达标(词间停顿全算"未覆盖"),降级均分反而 100%——好坏倒挂 | v0.6.0 起 `rs_align.py` 用**跨度覆盖率** =(首字起点→末字终点)÷转写声明区间,只对漏转写敏感;<0.99 输出 ⚠ 软警告 |
| **retext 补标点孤立成句** | 校对插入的标点零宽继承邻字时间 → 与前字 gap 巨大 → gap 切句 → 单标点成卡缺 startMs → 排最前污染首卡 | `_resplit_sentences` 把孤立标点并入前句;`rs_subtitle` 侧用 `_PUNCT_ONLY` 显式跳过纯标点文本(不能拿 `_clean_card` 当 skip 判据,它设计上保留 ?! 语气) |
| retext 统计字段位置 | 测试按 `doc["retext"]["stats"]["editChars"]` 断言 KeyError | 统计**平铺**在 `doc["retext"]` 下(如 `editChars`/`similarity`),没有嵌套 stats 层 |
| **无字级时间戳时"卡内位置"是估算的** | 被当成字级用 → 逐字染色/终点校验失去意义 | v0.7.0(#1)起 `build_wordline` 置 `charTimingEstimated=True`、逐字打 `estimated` 标,`degradeReasons` 明写"卡内位置为估算(不可当字级用)";卡拉OK 显式拒绝;正解是 `rs_dub align` 做强制对齐(#10) |
| **估算时间仍会造成卡内漂移** | 只有句级时间准 → 句内快慢靠运气 | 这是**已知且有意的折中**:句级整句卡会伤长句可读性,所以在 `max_chars` 内出卡并**显式标注**;真正的字级必须走 `rs_dub align`(译文:L1 目测清单会把"卡内位置为估算"列为待确认项) |

## 6. 门禁与验收

| 检查 | 通过线 |
|---|---|
| 跨度覆盖率 | ≥ 99%((首字起点→末字终点)÷ 转写声明区间,`rs_align.time_coverage`;仅对漏转写敏感,<0.99 软警告) |
| `conf` 中位数 | ≥ 0.8 |
| 单调性 | `startMs` 严格单调不减,无负时长 |
| 字幕↔音频偏移(`rs_sync`) | 中位数 ≤ 40ms,95 分位 ≤ 80ms |
| 音画切点一致 | 100% 切点同帧或差 ≤ 1 帧 |

未过门禁**不得进入 S3**;降级模式须在 `sync_report.md` 与 `deliverables.md` 中显式标注。
