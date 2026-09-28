"""字幕卡切分:约束最优 DP(rules/subtitles.md §4,ADR-0001 修订)。

核心区分(旧版本混为一谈的两层):
  卡切分 segmentation —— 一句话切成几张卡   → 本模块(DP)
  行断开 line break    —— 一张卡内怎么折行 → rs_subtitle.break_line(评分算法,保留)

设计要点:
  · 候选边界 = 强/弱标点 + 字级停顿(gap ≥ 200ms)+ 句法线索;其余位置需承担惩罚
  · 禁切表(专名/成语/数量词+单位/的得了着之/ASCII 词内)
  · 打分 = 标点层级 + 归一化停顿 + 语义完整性(否定词/复合词/「的」字头,I1)+ CUT_COST
            - 长度失衡 - 尾卡过短 - 最短时长缺口(T4.1a)
  · 硬约束 = 每卡字数 / 词内禁切(两阶段) / 幽灵卡禁止边界(T4.1b);时长/CPS 为软约束,
            由带权惩罚承载,违反项照常进 check_constraints 报告(不阻断)
  · 输出 top-N 候选;最优与次优差 < 5% 标记 ambiguous
  · 第四册一句话原则:DP 是唯一的断句决策者;文本分组只能在 DP(含求解器内部的
    边界修复 reabsorb/orphan)里定,rs_subtitle 的事件层后处理只许调时间。

纯标准库,可单测。用法见 rs_subtitle.py / tests/test_v4.py。
"""
from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------- 常量表

PUNCT_LEVEL = {"。": 1.0, "！": 1.0, "？": 1.0, "；": 0.8, "，": 0.6, "、": 0.4}
STRONG_PUNCT = "。！？；"
WEAK_PUNCT = "，、："
# dev-jj2815 实测:校对稿常混入半角标点(?,),不收进来会出现"标点领头卡"
# (如「?关于店群运营」)——标点必须挂在上一卡尾部,任何位置都不得在标点前切。
TRAIL_PUNCT = STRONG_PUNCT + WEAK_PUNCT + ",.?!;:"
CONJ_HEAD = "然所但而并因如虽接下首其另例同此"
# 连词/引导字:只能领起从句,**不能收尾**。以它们收尾 = 把「而被/而且/然后」这类
# 固定搭配切开(用户实例:「…店铺违规而 | 被连带处理…」)。见 OPTIMIZATION-v7 #2。
NO_TAIL = "而但并且或及与则却故因若虽如由然所"
NO_TAIL_PENALTY = -2.0      # 强惩罚而非硬禁切:避免极端句无解(DP 无候选 → 单卡超字数)
CUT_COST = -1.0             # 每切一刀的固定代价:防 DP 为了拿语义加分而把一个句子
                            # 切成一片 4 字卡(过度切分同样是「断句拉跨」)
TAIL_FUNC = "的了着地吧呢啊吗嘛"
CN_DIGITS = "零一二两三四五六七八九十百千万"
# 「号」(P30-4 文件引文:〔2003〕158号 / N号文件 的「数字+号」同属数量结构,禁切)
CN_UNITS = "个岁次天年月日时秒分元块毛角米厘斤吨度倍页条第名位件台只张片章节课号"
CURRENCY = "¥$€£"
FORBID_AFTER = "的地得了着之"
ELLIPSIS = "…"
# ASCII 词内字符(含连字符/下划线/点等 token 内合法符号,如 GPT-SoVITS / v2.1.0)
ASCII_TOKEN = "._-+#&/@"
# 卡首尾的非内容字符:计算卡时间时要剥掉,否则前导标点会把卡片起点提前
PUNCT_WS = "。，、；：,;:…!?！？ \u3000「」“”\"'()（）"

# 常用成语/固定搭配:内部禁切(可被调用方扩充)
DEFAULT_IDIOMS = (
    "一心一意", "三心二意", "四面八方", "五湖四海", "七上八下", "十全十美",
    "画蛇添足", "守株待兔", "刻舟求剑", "塞翁失马", "青出于蓝", "水到渠成",
)

# P30-4 保护词表(副文档 07:20260920 NCLM1605 工程实测教训)。
# 这些搭配 jieba/兜底词表经常拆开(周转|归还、资金|往来、费用|科目),
# 拆开即「动宾被斩断/专名被切碎」——与 terms 同等强度,**始终**并入词跨度
# (word_spans 的 jieba 分支也不例外),词内位置因此在 DP 第一阶段全禁切。
PROTECTED_WORDS = (
    "周转归还", "资金往来", "财务费用科目", "财务费用", "利润表", "高度重视",
)

# 文件引文括号(P30-4):〔2003〕158号 / 《…》——半括号挂卡首(「〕158号文件」)
# 是 20260920 实测事故形态;括号内侧一律禁切。
CITE_OPEN = "〔[《〈「『【([“\"‘'"
CITE_CLOSE = "〕]》〉」』】)]”\"’'"

_SPACE = " \u3000"
# 词内强惩罚:两阶段 DP 降级后仍切在词内的代价(必须压过一切语义加分)
WORD_CUT_PENALTY = -3.0

# I1 语义单元罚分(T4.5,BACKLOG v0.8.2 I1):
NEG_TAIL = "不没无非别未莫勿"
NEG_TAIL_PENALTY = -2.0     # 否定词收尾 = 否定词与被否定对象跨卡(「不|属于」)
DE_HEAD = "的地得了着"
DE_HEAD_PENALTY = -1.5      # 「的」字头卡降权(「的市场版图」「的客流量差异」)
# 复合词跨卡罚分:jieba 常拆开的搭配(「经营|主体」「运营|效率」「店群|企业」);
# 词表治理工具(tools/wordlist_patch.py)产出的新案例优先补进这里。
SEMANTIC_COMPOUNDS = (
    "经营主体", "运营效率", "店群企业", "不属于", "市场版图", "客流量差异",
    "经营范畴", "流量结构", "转化效率", "合规范围",
)
SEMANTIC_PENALTY = -2.0

# 高频词表:仅当 jieba 不可用时的兜底切词(最长匹配,2–4 字)。
# 覆盖口播叙事常用词;新发现的切词案例应优先补进这里(rules/subtitles.md §4.2)。
COMMON_WORDS = frozenset("""
这个 这些 这样 那个 那些 那样 我们 你们 他们 她们 自己 大家 什么 怎么 为什么
因为 所以 但是 而且 或者 虽然 如果 那么 于是 然而 并且 只要 只有 无论 不管
非常 特别 尤其 真的 其实 就是 只是 还是 还有 也是 都是 不会 可以 应该 必须
可能 也许 大概 大约 稍微 略微 几乎 差点 一直 仍然 依然 突然 忽然 渐渐 慢慢
已经 曾经 正在 刚刚 马上 立刻 立即 赶紧 赶快 终于 最后 最终 首先 其次 另外
此外 例如 比如 准备 开始 结束 完成 实现 解决 处理 使用 通过 经过 关于 由于
根据 按照 遵守 违规 违法 店铺 商家 平台 规则 规定 要求 内容 信息 数据 文件
文档 视频 音频 字幕 配音 录音 录制 拍摄 剪辑 导出 输出 输入 上传 下载 制作
生成 创建 删除 修改 更新 检查 测试 验证 确认 选择 设置 调整 优化 提升 增加
减少 保存 成功 失败 错误 问题 原因 结果 效果 影响 情况 状态 过程 方法 方式
方案 步骤 流程 系统 工具 功能 性能 质量 水平 标准 类型 种类 类别 版本 便宜
昂贵 好用 难用 简单 复杂 方便 快速 重要 主要 次要 普通 特殊 常见 罕见 普遍
少数 多数 部分 全部 整个 所有 一些 一点 一同 一起 努力 认真 仔细 用心 用力
尽力 尽量 全力 大力 遭殃 连带 损失 赔偿 处罚 罚款 封号 限流 曝光 流量 推荐
关注 粉丝 点赞 收藏 评论 转发 播放 观看 收看 打开 关闭 启动 停止 暂停 继续
恢复 时间 日期 今天 昨天 明天 上午 下午 晚上 早上 中午 现在 之后 以后 以前
当时 目前 当前 后来 最近 近期 长期 短期 暂时 永久 经常 偶尔 有时 总是 从来
软件 硬件 电脑 手机 平板 相机 麦克 摄像 灯光 背景 场景 画面 镜头 特写 全景
近景 远景 特效 转场 动画 贴纸 文字 字体 颜色 大小 位置 方向 速度 强度 亮度
音量 音质 音效 节奏 感觉 感受 体验 经验 知道 了解 明白 理解 觉得 认为 以为
相信 怀疑 猜测 猜想 判断 分析 思考 考虑 研究 探索 发现 发明 创造 设计 规划
安排 计划 组织 管理 协调 沟通 交流 聊天 说话 讲话 讲解 说明 解释 介绍 分享
教学 学习 复习 练习 模仿 跟读 阅读 书写 记录 记住 忘记 熟悉 陌生 喜欢 讨厌
希望 想要 需要 追求 拥有 失去 得到 获得 提供 给予 帮助 支持 反对 同意 拒绝
接受 回应 回复 回答 提问 询问 请教 打听 通知 告诉 提醒 警告 建议 意见 看法
观点 态度 立场 里面 外面 上面 下面 前面 后面 左边 右边 中间 旁边 附近 到处
毕竟 反正 干脆 压根 根本 完全 彻底 干净 整齐 清晰 明显 显然
当然 确实 果然 居然 竟然 哪里 哪个 哪些 什么样 没什么 一样 不一样
一家 其中 当中 内部 外部 局部 整体 单独 独立 共同 彼此
""".split())

# 每卡字数(2026-09 起:竖屏从 16 下调到 10–12,依据见 rules/subtitles.md §4.4)
# 3x4 = 小红书竖屏正文:屏宽介于 9:16 与 16:9 之间,平台预设取 15(见 templates/platforms.json)
MAX_CHARS = {"9x16": 12, "3x4": 15, "16x9": 22}
CPS_MAX = {"9x16": 9.0, "3x4": 9.0, "16x9": 9.0}
DUR_RANGE = (0.83, 7.0)      # Netflix 最短 5/6s,最长 7s
MIN_CHARS = 2
RELEASE_MS = 20           # 卡片相对首/末字的时间释放余量(align.md §4)

# T4.4 约束分级策略表(templates/subtitle-policy.json)——硬/软阈值的唯一数据真相源,
# segmentation / rs_subtitle / rs_verify 三处共读(读口全部走 load_policy 派生函数)。
# 下列内置常量 = 策略表缺失/坏档时的回退档,数值必须与策略表一致(test_subtitle_policy 对拍)。
#   硬 = 超每卡字数 / 卡时间重叠 / 切字内(降级除外)/ 幽灵卡边界;
#   软 = CPS / 最短·最长时长 / 卡间距 / 视觉节拍 —— 违反只报告 + 给替代方案,不阻断交付。
BEAT_RANGE_S = (1.5, 3.5)    # 视觉节拍(软):卡时长宜落 1.5–3.5s(ADR-0004)
GHOST_MIN_MS = 100           # 幽灵卡线(硬):内容字有效时长低于此值的卡禁止由 DP 产出
MIN_GAP_FRAMES = 2           # 卡间距(软):≥2 帧
MIN_DUR_S = DUR_RANGE[0]     # 最短时长(软):DP 内以带权惩罚承载(T4.1a)
MIN_DUR_PENALTY = -3.0       # 短卡惩罚(按缺口比例缩放,须压过普通语义加分)

# 约束分级策略表的唯一读口(T4.4);缺文件/坏档 → 内置回退常量,绝不臆测。
POLICY_PATH = Path(__file__).resolve().parents[1] / "templates" / "subtitle-policy.json"
_POLICY_CACHE: dict | None = None


def load_policy(force: bool = False) -> dict:
    """读 subtitle-policy.json(带缓存);返回原始文档,缺失时返回 {}(调用方回退常量)。"""
    global _POLICY_CACHE
    if _POLICY_CACHE is not None and not force:
        return _POLICY_CACHE
    pol: dict = {}
    try:
        doc = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        if isinstance(doc, dict):
            pol = doc
    except (OSError, json.JSONDecodeError, ValueError):
        pol = {}
    _POLICY_CACHE = pol
    return pol


def policy_constraints(policy: dict | None = None) -> tuple[dict, dict]:
    """(hard, soft) 两级约束;策略表缺字段时逐项回退内置常量(同源数值)。"""
    pol = policy if policy is not None else load_policy()
    cons = pol.get("constraints") if isinstance(pol.get("constraints"), dict) else {}
    hard = dict(cons.get("hard") or {})
    soft = dict(cons.get("soft") or {})
    hard.setdefault("maxChars", dict(MAX_CHARS))
    soft.setdefault("cpsMax", dict(CPS_MAX))
    soft.setdefault("durRangeS", list(DUR_RANGE))
    soft.setdefault("minGapFrames", MIN_GAP_FRAMES)
    soft.setdefault("beatRangeS", list(BEAT_RANGE_S))
    soft.setdefault("minChars", MIN_CHARS)
    soft.setdefault("ghostMinMs", GHOST_MIN_MS)
    return hard, soft


def max_chars_for(ratio: str) -> int:
    """每卡字数上限(硬约束):策略表 → 内置 MAX_CHARS 回退。rs_subtitle/rs_verify 共读。"""
    hard, _ = policy_constraints()
    table = hard.get("maxChars") or MAX_CHARS
    v = table.get(ratio)
    return int(v) if isinstance(v, int) else int(MAX_CHARS.get(ratio, MAX_CHARS["9x16"]))


def cps_max_for_ratio(ratio: str) -> float:
    """CPS 上限(软约束):策略表 → 内置 CPS_MAX 回退。"""
    _, soft = policy_constraints()
    table = soft.get("cpsMax") or CPS_MAX
    v = table.get(ratio, CPS_MAX.get(ratio, 9.0))
    return float(v)


def dur_range() -> tuple[float, float]:
    """单卡时长区间(软约束):策略表 → DUR_RANGE 回退。"""
    _, soft = policy_constraints()
    dr = soft.get("durRangeS") or DUR_RANGE
    return (float(dr[0]), float(dr[1]))


def beat_range() -> tuple[float, float]:
    """视觉节拍区间(软约束)。"""
    _, soft = policy_constraints()
    br = soft.get("beatRangeS") or BEAT_RANGE_S
    return (float(br[0]), float(br[1]))


def ghost_min_ms() -> int:
    """幽灵卡线(硬约束):策略表 hard → soft → 内置常量。"""
    hard, soft = policy_constraints()
    v = hard.get("ghostMinMs") or soft.get("ghostMinMs")
    return int(v) if v else int(GHOST_MIN_MS)

# 断句回归测试集(rules/subtitles.md §4.7)
REGRESSION = (
    {"text": "滚滚长江东逝水", "terms": ("长江",), "must_not_split": ("长江",)},
    {"text": "我今年三十五岁", "terms": (), "must_not_split": ("三十五",)},
    {"text": "这套设备要 ¥1999 元", "terms": (), "must_not_split": ("¥1999",)},
    {"text": "用 GPT-SoVITS 做配音", "terms": (), "must_not_split": ("GPT-SoVITS",)},
    # 连词不得收尾(OPTIMIZATION-v7 #2):「而」必须领起后一卡
    {"text": "可能因为其中一家店铺违规而被连带处理最终一同遭殃", "terms": (),
     "must_not_split": ("而被",)},
    {"text": "这个方案便宜而且好用所以我们决定立刻采用它", "terms": (),
     "must_not_split": ("而且", "所以")},
    {"text": "他非常努力地准备但是没有成功最后还是失败了", "terms": (),
     "must_not_split": ("非常", "但是", "最后", "失败")},
    # B6(v0.12):破折/短语收尾的 3 字孤卡必须并入前卡(安信德:『说谁好』孤卡)
    {"text": "这个方案真的非常不错所以我们最终决定采用它了说谁好", "terms": (),
     "must_not_split": ("说谁好",)},
    # P30-5(副文档 07):20260920 NCLM1605 工程四案例固化,保证不再复现
    # 案例 #1/#2:「中」被甩到下一卡、「科目」被拆走 —— 长度驱动的边界落点
    {"text": "小企业会计准则的利润表中只有一个财务费用科目", "terms": ("利润表中",),
     "must_not_split": ("利润表中", "财务费用科目")},
    # 案例 #3:动宾「周转归还」被拆开、文件引文「〔2003|〕158」半括号挂卡首
    {"text": "借款时发生纳税年度内周转归还依据财税〔2003〕158号文件的规定", "terms": (),
     "must_not_split": ("周转归还", "〔2003〕", "158号")},
    # 案例 #4:动宾/名词搭配「资金往来」被拆走
    {"text": "规范股东与公司之间的资金往来务必高度重视", "terms": (),
     "must_not_split": ("资金往来", "高度重视")},
    # ------------------------------------------------ 第四册 T4.5(I1 语义单元)8 例
    {"text": "这些内容根本不属于合规的经营范围", "terms": (),
     "must_not_split": ("不属于", "经营范围")},
    {"text": "店群企业的运营效率决定了经营主体的收益", "terms": (),
     "must_not_split": ("店群企业", "运营效率", "经营主体")},
    {"text": "平台算法正在改写流量的市场版图", "terms": (),
     "must_not_split": ("市场版图",)},
    {"text": "三家门店的客流量差异非常明显", "terms": (),
     "must_not_split": ("客流量差异",)},
    {"text": "没有经营主体资质就无法开通小店", "terms": ("经营主体",),
     "must_not_split": ("经营主体",)},
    {"text": "提高运营效率的核心是优化流量结构", "terms": (),
     "must_not_split": ("运营效率", "流量结构")},
    {"text": "账号没有违反规则却被限流了半个月", "terms": (),
     "must_not_split": ("限流",)},
    {"text": "这种做法直接影响了店群企业的整体评分", "terms": ("店群企业",),
     "must_not_split": ("店群企业",)},
    # ------------------------------------------------ 第四册 T4.6(I2 terms 接线)4 例
    {"text": "安信德GEO优化服务已经正式上线了", "terms": ("安信德", "GEO优化"),
     "must_not_split": ("安信德", "GEO优化")},
    {"text": "小规模纳税人的增值税申报流程有新变化", "terms": ("增值税", "小规模纳税人"),
     "must_not_split": ("增值税", "小规模纳税人")},
    {"text": "用GPT-SoVITS克隆声音需要先准备语料", "terms": ("GPT-SoVITS",),
     "must_not_split": ("GPT-SoVITS",)},
    {"text": "抖音小店和抖店的保证金政策并不相同", "terms": ("抖店",),
     "must_not_split": ("抖店",)},
    # ------------------------------------------------ 第四册 T4.9 金标抽样 + 领域扩容
    {"text": "今天这期视频我们来讲讲账号定位的三个方法", "terms": (),
     "must_not_split": ("账号定位",)},
    {"text": "很多新手商家一上来就急着去投流量", "terms": (), "must_not_split": ()},
    {"text": "其实真正的重点是把内容本身做好", "terms": (), "must_not_split": ()},
    {"text": "先做人群画像再去设计脚本才是正确的顺序", "terms": (), "must_not_split": ()},
    {"text": "纳税人在办理汇算清缴时应当准备好这些材料", "terms": ("汇算清缴",),
     "must_not_split": ("汇算清缴",)},
    {"text": "企业所得税的汇算清缴截止到五月三十一日", "terms": ("企业所得税", "汇算清缴"),
     "must_not_split": ("企业所得税", "汇算清缴")},
    {"text": "专用发票和普通发票的抵扣方式完全不一样", "terms": ("专用发票", "普通发票"),
     "must_not_split": ("专用发票", "普通发票")},
    {"text": "开具发票时必须填写正确的纳税人识别号", "terms": ("纳税人识别号",),
     "must_not_split": ("纳税人识别号",)},
    {"text": "小企业会计准则下不需要报送现金流量表", "terms": ("小企业会计准则", "现金流量表"),
     "must_not_split": ("小企业会计准则", "现金流量表")},
    {"text": "净利润等于收入总额减去各项成本费用", "terms": ("成本费用",),
     "must_not_split": ("成本费用",)},
    {"text": "逾期申报会产生滞纳金还会影响信用等级", "terms": ("滞纳金", "信用等级"),
     "must_not_split": ("滞纳金", "信用等级")},
    {"text": "我们先把素材导入时间线再做粗剪决策", "terms": (), "must_not_split": ()},
    {"text": "字幕的断句要跟着语音的停顿走而不是随机切", "terms": (), "must_not_split": ()},
    {"text": "导出之前务必检查响度和真峰这两项指标", "terms": (), "must_not_split": ()},
    {"text": "每张字幕卡的时长最好控制在一秒半到三秒半之间", "terms": (), "must_not_split": ()},
    {"text": "这个功能需要先把模型下载到本地才能离线使用", "terms": (), "must_not_split": ()},
    {"text": "直播间的人气上来了转化率却一直上不去", "terms": ("转化率",),
     "must_not_split": ("转化率",)},
    {"text": "选品的核心逻辑是先看需求再看供给", "terms": (), "must_not_split": ()},
    {"text": "老客户复购的成本远远低于拉新的成本", "terms": ("复购", "拉新"),
     "must_not_split": ("复购", "拉新")},
    {"text": "做口播视频最重要的就是把话说清楚", "terms": ("口播",),
     "must_not_split": ("口播",)},
    {"text": "发布时间对流量池的突破有明显影响", "terms": ("流量池",),
     "must_not_split": ("流量池",)},
)


# ---------------------------------------------------------------- 禁切表

def forbidden_positions(text: str, terms=(), idioms=DEFAULT_IDIOMS) -> set[int]:
    """返回不允许切分的位置集合(pos == 在 text[pos-1] 与 text[pos] 之间切)。"""
    n = len(text)
    forb: set[int] = set()
    for i in range(1, n):
        a, b = text[i - 1], text[i]
        if a.isascii() and b.isascii() and (a.isalnum() or a in ASCII_TOKEN) \
                and (b.isalnum() or b in ASCII_TOKEN):
            forb.add(i)                      # ASCII token 内(GPT-SoVITS / v2.1.0 / build123)
        if a in FORBID_AFTER:
            forb.add(i)                      # 的/地/得/了/着/之 之后
        if b in CN_UNITS and (a.isdigit() or a in CN_DIGITS):
            forb.add(i)                      # 数量词 + 量词/单位
        if a in CURRENCY and (b.isdigit() or b in CN_DIGITS):
            forb.add(i)                      # 货币符号 + 数字
        if a.isdigit() and b in "%‰°":
            forb.add(i)                      # 数字 + 百分号/度数
        if b in TRAIL_PUNCT:
            forb.add(i)                      # 标点前不切(标点挂上一卡尾,防「?关于…」式领头卡)
        if b in _SPACE:
            forb.add(i)                      # 空格前不切:空格挂上一卡尾(校对稿的天然词组分隔)
        # P30-4 文件引文:括号内侧禁切(「〔2003 | 〕158号」半括号挂卡首是实测事故形态)
        if b in CITE_CLOSE or a in CITE_OPEN:
            forb.add(i)
    for t in list(terms) + list(idioms):
        if not t:
            continue
        start = 0
        while True:
            k = text.find(t, start)
            if k < 0:
                break
            for i in range(k + 1, k + len(t)):
                forb.add(i)                  # 词条内部
            start = k + 1
    return forb


def candidate_positions(text: str, gaps: dict[int, float] | None = None) -> set[int]:
    """候选边界:标点处 + 空格后 + 字级停顿 ≥200ms 处 + 连词前。"""
    gaps = gaps or {}
    cand: set[int] = set()
    for i in range(1, len(text)):
        if text[i - 1] in TRAIL_PUNCT:
            cand.add(i)                      # 标点后切(标点跟上一卡)
        elif text[i - 1] in _SPACE:
            cand.add(i)                      # 空格后切 = 词组边界(「被连带处理 最终…」)
        if text[i] in CONJ_HEAD:
            cand.add(i)
        if gaps.get(i, 0.0) >= 200.0:
            cand.add(i)
    return cand


# ---------------------------------------------------------------- 词边界(v0.8.1,ADR-0020;T4.8 引擎抽象)

_jieba_mod = None
_jieba_checked = False


def _try_jieba():
    """延迟加载 jieba(可选依赖);失败只降级,绝不抛错——脚本鲁棒性铁律。

    initialize() 会往 stdout 打「Building prefix dict…」日志,污染 rs_* 的
    --json 契约——必须在重定向的 stdout 里初始化,并把日志级别压到 ERROR。
    """
    global _jieba_mod, _jieba_checked
    if not _jieba_checked:
        _jieba_checked = True
        try:
            import contextlib
            import io
            import logging
            import jieba
            jieba.setLogLevel(logging.ERROR)
            with contextlib.redirect_stdout(io.StringIO()):
                jieba.initialize()
            _jieba_mod = jieba
        except Exception:
            _jieba_mod = None
    return _jieba_mod


def jieba_available() -> bool:
    """jieba 是否可用(rs_doctor 自检用)。首次调用会触发词典加载。"""
    return _try_jieba() is not None


class MissingDependency(Exception):
    """分词引擎的可选依赖缺失(调用方捕获后降级,绝不抛出模块外)。"""


def _tokens_to_spans(tokens: list[str], text: str) -> list[tuple[int, int]]:
    """引擎分词结果(词列表)→ 文本位置跨度 [(start,end))(仅 ≥2 字)。

    贪心对齐:token 依序在文本上游标匹配;对不上(引擎加了空格/改写)就丢弃
    该 token——绝不臆测位置。空白 token 跳过。
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for tok in tokens or []:
        if not tok:
            continue
        k = text.find(tok, cursor)
        if k < 0 and cursor == 0:
            k = text.find(tok)
        if k < 0:
            continue
        spans.extend(r for r in _non_space_runs(k, k + len(tok), text) if r[1] - r[0] >= 2)
        cursor = k + len(tok)
    return sorted(set(spans))


class Tokenizer:
    """分词引擎接口(T4.8):spans(text) → 词跨度列表。

    实现约定:
      · 可用性三态:unchecked → ready / missing(lazy 加载,缺失不抛错只降级);
      · spans 只给**引擎自己的**词跨度;terms/idioms/PROTECTED_WORDS 的强制并入
        在 word_spans 统一层做(所有引擎同一口径);
      · 缺 ≥2 字的 token 对断句无意义,由 _tokens_to_spans / _lexicon_spans 过滤。
    """
    name = "base"

    def spans(self, text: str) -> list[tuple[int, int]]:
        raise NotImplementedError


class LexiconTokenizer(Tokenizer):
    """内置高频词表兜底(最长匹配,2–4 字):永远可用,是所有降级链的终点。"""
    name = "lexicon"

    def spans(self, text: str) -> list[tuple[int, int]]:
        return _lexicon_spans(text, COMMON_WORDS)


class JiebaTokenizer(Tokenizer):
    name = "jieba"

    def spans(self, text: str) -> list[tuple[int, int]]:
        jb = _try_jieba()
        if jb is None:
            raise MissingDependency("jieba")
        out: list[tuple[int, int]] = []
        try:
            for _w, s, e in jb.tokenize(text):
                if e - s >= 2:
                    out.extend(r for r in _non_space_runs(s, e, text) if r[1] - r[0] >= 2)
        except Exception as exc:  # noqa: BLE001 — 引擎运行期故障按缺失处理
            raise MissingDependency(f"jieba:{exc}") from exc
        return sorted(set(out))


def _load_pkuseg():
    """pkuseg 懒加载(三态;未安装抛 ImportError → get_tokenizer 降级)。"""
    import pkuseg
    return pkuseg.pkuseg()


def _load_lac():
    from LAC import LAC
    return LAC(mode="seg")


def _load_hanlp():
    import hanlp
    return hanlp.load(hanlp.pretrained.tok.FINE_ELECTRA_SMALL_ZH)


class _ExternalTok(Tokenizer):
    """pkuseg / LAC / hanlp 的公共壳:cut() 词列表 → _tokens_to_spans。"""

    def __init__(self, name: str, loader):
        self.name = name
        self._loader = loader
        self._model: object | None = None

    def _ensure(self):
        if self._model is None:
            self._model = self._loader()
        return self._model

    def spans(self, text: str) -> list[tuple[int, int]]:
        model = self._ensure()
        words = model.cut(text)
        return _tokens_to_spans(list(words), text)


class PkusegTokenizer(_ExternalTok):
    name = "pkuseg"


class LACTokenizer(_ExternalTok):
    name = "lac"


class HanlpTokenizer(_ExternalTok):
    name = "hanlp"


# 引擎注册表:name → 实例(auto 链按 T4.9 评测结论默认 jieba 优先,详见 ADR-0060)
TOKENIZERS: dict[str, Tokenizer] = {
    "jieba": JiebaTokenizer(),
    "pkuseg": PkusegTokenizer("pkuseg", _load_pkuseg),
    "lac": LACTokenizer("lac", _load_lac),
    "hanlp": HanlpTokenizer("hanlp", _load_hanlp),
    "lexicon": LexiconTokenizer(),
}
AUTO_CHAIN = ("jieba", "lexicon")          # engine=auto 的降级链
_ENGINE_STATE: dict[str, str] = {}         # 三态留痕:unchecked → ready / missing


def get_tokenizer(name: str | None = None, *, degrade: list | None = None) -> Tokenizer:
    """engine 名 → Tokenizer 实例;缺失即降级并把「降级到 X」写进 degrade 留痕表。

    name 取值:auto(默认链 jieba→lexicon)/ jieba / pkuseg / lac / hanlp / lexicon。
    config.json 的 subtitleTokenizer 字段由调用方(rs_subtitle)读出后传入。
    """
    name = (name or "auto").strip().lower()
    if name in ("", "auto"):
        chain = AUTO_CHAIN
    elif name in TOKENIZERS:
        chain = (name, "jieba", "lexicon") if name not in AUTO_CHAIN else (name,) + \
            tuple(x for x in AUTO_CHAIN if x != name)
    else:
        if degrade is not None:
            degrade.append(f"未知分词引擎 {name!r},降级到默认链")
        chain = AUTO_CHAIN
    tried: list[str] = []
    for cand in chain:
        if cand in tried:
            continue
        tried.append(cand)
        tok = TOKENIZERS[cand]
        state = _ENGINE_STATE.get(cand, "unchecked")
        if state == "ready":
            return tok
        if state == "missing":
            continue
        try:
            if isinstance(tok, JiebaTokenizer):
                ok = _try_jieba() is not None
            elif cand == "lexicon":
                ok = True
            else:
                tok._ensure()
                ok = True
        except Exception:  # noqa: BLE001 — ImportError/模型加载失败一律按缺失
            ok = False
        _ENGINE_STATE[cand] = "ready" if ok else "missing"
        if ok:
            if degrade is not None and tried[:-1]:
                degrade.append(f"分词引擎 {tried[0]} 不可用,降级到 {cand}")
            return tok
        if degrade is not None and cand == chain[0] and cand != "lexicon":
            degrade.append(f"分词引擎 {cand} 不可用(未安装),降级到 "
                           f"{'lexicon' if cand == 'jieba' else 'jieba/lexicon'}")
    return TOKENIZERS["lexicon"]


def _non_space_runs(s: int, e: int, text: str) -> list[tuple[int, int]]:
    """跨度内的非空白连续段——词永远不得横跨空格(空格 = 词组边界)。"""
    runs: list[tuple[int, int]] = []
    cur: int | None = None
    for i in range(s, e):
        if text[i] in _SPACE:
            if cur is not None:
                runs.append((cur, i))
                cur = None
        elif cur is None:
            cur = i
    if cur is not None:
        runs.append((cur, e))
    return runs


def _lexicon_spans(text: str, lexicon: frozenset[str]) -> list[tuple[int, int]]:
    """内置词表最长匹配兜底:ASCII 连续段整体成词,中文 4→3→2 字贪心。"""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _SPACE:
            i += 1
            continue
        if ch.isascii() and (ch.isalnum() or ch in ASCII_TOKEN):
            j = i + 1
            while j < n and text[j].isascii() and (text[j].isalnum() or text[j] in ASCII_TOKEN):
                j += 1
            spans.append((i, j))
            i = j
            continue
        for L in (4, 3, 2):
            if i + L <= n and text[i:i + L] in lexicon:
                spans.append((i, i + L))
                i += L
                break
        else:
            i += 1
    return spans


def word_spans(text: str, terms=(), idioms=DEFAULT_IDIOMS, *,
               engine: str | None = None, degrade: list | None = None,
               tokenizer: Tokenizer | None = None) -> list[tuple[int, int]]:
    """词跨度列表 [(start, end));分词引擎可插拔(T4.8),缺依赖即降级留痕(ADR-0020)。

    词不得横跨空格;terms/idioms 总是显式并入(专名/行业词即便引擎在场也可能被切碎)。
    P30-4:PROTECTED_WORDS(动宾搭配/专名,如「周转归还」「资金往来」)同样**始终**并入
    —— 引擎对这类搭配常拆成两个词,不强制并跨度就会重演 20260920 的甩字事故。
    engine=None 时按调用方 config 选(auto=jieba→lexicon);tokenizer 可直接注入实例(测试用)。
    """
    if not text:
        return []
    spans: set[tuple[int, int]] = set()
    tok = tokenizer or get_tokenizer(engine, degrade=degrade)
    try:
        for s, e in tok.spans(text):
            spans.update(r for r in _non_space_runs(s, e, text) if r[1] - r[0] >= 2)
    except Exception:  # noqa: BLE001 — 引擎运行期故障 → 兜底词表(降级必须可见)
        if degrade is not None:
            degrade.append(f"分词引擎 {tok.name} 运行失败,降级到 lexicon 兜底词表")
        spans.update(_lexicon_spans(text, COMMON_WORDS | {t for t in terms if t} | set(idioms)))
    for t in list(terms) + list(idioms) + list(PROTECTED_WORDS):
        if not t:
            continue
        start = 0
        while True:
            k = text.find(t, start)
            if k < 0:
                break
            spans.update(r for r in _non_space_runs(k, k + len(t), text) if r[1] - r[0] >= 2)
            start = k + 1
    return sorted(spans)


# ---------------------------------------------------------------- 打分

def semantic_bad_positions(text: str, compounds=SEMANTIC_COMPOUNDS) -> set[int]:
    """I1 复合词跨卡罚分位置:切在这些位置 = 把固定语义单元拆到两张卡(T4.5)。

    「经营主体」「运营效率」这类搭配 jieba 常拆成两个词,词内禁切拦不住——
    在此以**带权罚分**(而非硬禁切)进 DP:有别的合法切点时 DP 绕开,极端句
    无解时不至于整体无解。
    """
    bad: set[int] = set()
    for t in compounds:
        if not t:
            continue
        start = 0
        while True:
            k = text.find(t, start)
            if k < 0:
                break
            for i in range(k + 1, k + len(t)):
                bad.add(i)
            start = k + 1
    return bad


def cut_score(text: str, pos: int, gap_ms: float = 0.0, max_chars: int = 12,
              preferred: bool = True, in_word: bool = False,
              sem_bad: bool = False) -> float:
    """单个切点的分数(越大越好),对应 rules/subtitles.md §4.3(含 I1 语义罚分)。"""
    left, right = text[:pos], text[pos:]
    s = 2.0 * PUNCT_LEVEL.get(left[-1] if left else "", 0.0)
    s += 1.5 * min(max(gap_ms, 0.0) / 500.0, 1.0)
    if left and left[-1] not in TAIL_FUNC:
        s += 0.5                                     # 不以虚词结尾
    if left and left[-1] in _SPACE:
        s += 0.3                                     # 空格收尾 = 词组边界
    if left and left[-1] in NO_TAIL:
        s += NO_TAIL_PENALTY                         # 连词/引导字不得收尾(防切词)
    if left and left[-1] in NEG_TAIL:
        s += NEG_TAIL_PENALTY                        # 否定词收尾 = 「不|属于」跨卡(I1)
    if right and right[0] in DE_HEAD:
        s += DE_HEAD_PENALTY                         # 「的」字头卡降权(「的市场版图」,I1)
    if right and right[0] in CONJ_HEAD:
        s += 0.5                                     # 连词起首 = 从句边界(BBC:clause boundary)
    if not preferred:
        s -= 0.5                                     # 非候选边界需付出代价
    if in_word:
        s += WORD_CUT_PENALTY                        # 切在词内(仅两阶段降级路径可达)
    if sem_bad:
        s += SEMANTIC_PENALTY                        # 复合词跨卡(「经营|主体」,I1)
    return s


def _card_penalty(length: int, max_chars: int) -> float:
    if length < 4:
        return -1.2 * (4 - length)                   # 尾卡过短惩罚(防「悬一字」)
    return 0.0


def _imbalance(left_len: int, right_len: int, max_chars: int) -> float:
    return -0.8 * abs(left_len - right_len) / max(1, max_chars)


# ---------------------------------------------------------------- DP

def plan_score(text: str, cuts: list[int], max_chars: int,
               gaps: dict[int, float] | None = None,
               preferred: set[int] | None = None,
               in_word: set[int] | None = None) -> float:
    gaps = gaps or {}
    preferred = preferred if preferred is not None else set()
    in_word = in_word if in_word is not None else set()
    sem = semantic_bad_positions(text)
    score = 0.0
    prev = 0
    for c in cuts:
        score += cut_score(text, c, gaps.get(c, 0.0), max_chars, c in preferred, c in in_word,
                           c in sem)
        score += CUT_COST
        score += _card_penalty(c - prev, max_chars)
        prev = c
    score += _card_penalty(len(text) - prev, max_chars)
    return score


def _card_dur_ms(pos_times: list[tuple[int, int]] | None, s: int, e: int) -> float:
    """卡 [s,e) 的**内容字有效时长** ms(T4.1:字级时间进 DP)。pos_times 为 None → inf。"""
    if not pos_times:
        return float("inf")
    return max(0.0, float(pos_times[e - 1][1] - pos_times[s][0]))


def _dp(text: str, max_chars: int, min_chars: int, forb: set[int],
        gaps: dict[int, float], preferred: set[int], top: int,
        in_word: set[int] | None = None, sem_bad: set[int] | None = None,
        pos_times: list[tuple[int, int]] | None = None,
        min_dur_s: float = MIN_DUR_S, ghost_min_ms_: int = GHOST_MIN_MS,
        ghost_ban: bool = False) -> list[list[int]]:
    """返回 top-N 个切点序列(按分排序)。DP 状态 = 位置 → 前 N 优方案。

    T4.1 约束进 DP(有字级时间 pos_times 时):
      · 最短时长 → **卡级带权惩罚**(按缺口比例缩放,上限 MIN_DUR_PENALTY):
        有合法替代切分时 DP 主动绕开短卡;极端句无解时不至于整体无解(软约束不阻断);
      · 幽灵卡(内容字有效时长 < ghost_min_ms)→ **禁止边界**(整句单卡除外):
        求解阶段直接排除,替代旧「P28-2 先出卡再并/丢」的保险(ghost_ban 开关供降级重试)。
    """
    in_word = in_word if in_word is not None else set()
    sem_bad = sem_bad if sem_bad is not None else set()
    n = len(text)
    allowed = [i for i in range(1, n) if i not in forb]
    positions = sorted({0, n} | set(allowed))

    # states[pos] = [(score, tuple(cuts), prev_card_len)]
    states: dict[int, list[tuple[float, tuple[int, ...], int]]] = {0: [(0.0, (), 0)]}
    for s in positions:
        if s not in states:
            continue
        for sc, cuts, prev_len in states[s]:
            for e in positions:
                if e <= s:
                    continue
                L = e - s
                if L > max_chars:
                    break
                if e != n and L < min_chars:
                    continue
                dur_ms = _card_dur_ms(pos_times, s, e)
                if ghost_ban and pos_times and not (s == 0 and e == n) \
                        and dur_ms < ghost_min_ms_:
                    continue                     # 幽灵卡禁止边界(T4.1b,硬排除)
                add = _imbalance(prev_len, L, max_chars) if prev_len else 0.0
                if pos_times and dur_ms / 1000.0 < min_dur_s:
                    deficit = 1.0 - dur_ms / 1000.0 / min_dur_s
                    add += MIN_DUR_PENALTY * max(0.0, min(1.0, deficit))  # T4.1a 带权惩罚
                if e == n:
                    add += _card_penalty(L, max_chars)
                else:
                    add += cut_score(text, e, gaps.get(e, 0.0), max_chars,
                                     e in preferred, e in in_word, e in sem_bad)
                    add += CUT_COST
                    add += _card_penalty(L, max_chars)
                bucket = states.setdefault(e, [])
                bucket.append((sc + add, cuts + (e,), L))
                bucket.sort(key=lambda t: -t[0])
                del bucket[top:]
    if n not in states:
        return [[]]
    out = []
    for sc, cuts, _ in states[n]:
        out.append(list(cuts))
    return out or [[]]


# ---------------------------------------------------------------- 对外接口

def reabsorb_cuts(text: str, cuts: list[int], max_chars: int, *,
                  forb: set[int] | None = None, preferred: set[int] | None = None,
                  terms=(), idioms=DEFAULT_IDIOMS) -> tuple[list[int], int]:
    """P30-1 末卡回吸(副文档 07):把被字数墙逼出的边界调整到语义完整处。

    治「…利润表 | 中 只有一个…」式甩字(20260920 NCLM1605 实测)。只学
    jianying-headless「把边界调整到低能量处」的方法,规则自定,两类触发:

      A. 切点把词跨度拦腰截断(仅词内降级路径可达)→ 整词回吸进上一卡
         (边界移到词尾);词尾放不下 → 整词推给下一卡(边界移到词首)。
      B. 切点不是候选边界(无标点/空格/停顿/连词信号)且下一卡首字是**单字词**
         (如「利润表|中」的「中」)且上一卡已顶近字数上限 → 回吸该字。

    硬边界:新边界必须 ≤max_chars、不在禁切表、不在任何词跨度内部、
    下一卡剩余 ≥MIN_CHARS;且回吸**只吞整词**,永不制造新的词内切点。
    返回 (新切点表, 回吸次数)。无变化时原表返回。
    """
    if not cuts:
        return cuts, 0
    forb = forb if forb is not None else forbidden_positions(text, terms, idioms)
    preferred = preferred if preferred is not None else candidate_positions(text)
    spans = word_spans(text, terms=terms, idioms=idioms)

    def _in_word(p: int) -> bool:
        return any(a < p < b for a, b in spans)

    def _is_single_char_word(p: int) -> bool:
        """位置 p 上的字是**单字词**:没有任何 ≥2 字词跨度覆盖它
        (word_spans 只收 ≥2 字跨度,单字词靠覆盖性判定)。"""
        return not any(a <= p < b for a, b in spans)

    n = len(text)
    out: list[int] = []
    moved = 0
    q = 0                       # 上一边界(当前卡的起点)
    for k, p in enumerate(cuts):
        r = cuts[k + 1] if k + 1 < len(cuts) else n
        newp = p
        if _in_word(p):
            # 触发 A:切在词内 → 整词回吸进上一卡;放不下则整词推给下一卡
            span = next((a, b) for a, b in spans if a < p < b)
            a, b = span
            if b - q <= max_chars and b not in forb and not _in_word(b) and r - b >= MIN_CHARS:
                newp = b
            elif q <= a and a > 0 and a not in forb and not _in_word(a) and r - a <= max_chars:
                newp = a
        else:
            # 触发 B:非候选边界 + 下一卡首字是单字词(如「利润表|中」的「中」)
            # + 上一卡顶近字数墙 → 回吸一字(不碰连词领起字)
            if (_is_single_char_word(p) and p not in preferred
                    and p + 1 - q <= max_chars and (p + 1) not in forb
                    and not _in_word(p + 1) and r - (p + 1) >= MIN_CHARS
                    and text[p] not in NO_TAIL and text[p] not in CONJ_HEAD):
                newp = p + 1
        if newp != p:
            moved += 1
        out.append(newp)
        q = newp
    # 防御:单调去重(回吸后两边界重合 → 后者吞并)
    dedup: list[int] = []
    for p in out:
        if not dedup or p > dedup[-1]:
            dedup.append(p)
    return dedup, moved


def cards_from_cuts(text: str, cuts: list[int]) -> list[dict]:
    spans, prev = [], 0
    for c in cuts:
        spans.append((prev, c))
        prev = c
    spans.append((prev, len(text)))
    return [{"i": i, "start": a, "end": b, "text": text[a:b]}
            for i, (a, b) in enumerate(spans) if b > a]


def _merge_orphan_tail(cards: list[dict], max_chars: int) -> tuple[list[dict], str | None]:
    """B6(v0.12)孤卡合并:末卡 <4 字(_card_penalty 的「过短」线,如破折句切出的
    3 字孤卡)且并入前卡后 ≤ max_chars → 并入前卡;并不下 → 显式留痕 orphan-card
    (不静默;人工通道 rs_subtitle --override textPrefix+textSuffix 可合并)。
    MIN_CHARS 保持 2 不变——调到 4 会在 _dp 制造超字数无解路径。只动文本跨度,
    时间在 _finalize 里按合并后的首末字重新锚定,对齐精度不受影响。
    """
    if len(cards) < 2:
        return cards, None
    tail = cards[-1]
    tail_n = len(tail["text"].replace(" ", ""))
    if tail_n >= 4:
        return cards, None
    prev = cards[-2]
    if len(prev["text"].replace(" ", "")) + tail_n > max_chars:
        return cards, (f"orphan-card:末卡「{tail['text']}」仅 {tail_n} 字且并入前卡超 "
                       f"{max_chars} 字上限(可 rs_subtitle --override 合并)")
    prev["end"] = tail["end"]
    prev["text"] = prev["text"] + tail["text"]
    cards.pop()
    for k, c in enumerate(cards):
        c["i"] = k
    return cards, None


def _attach_times(cards: list[dict], index_map: list[int | None],
                  char_times: list[dict]) -> None:
    """把卡的时间锚到首末字(align.md §4):start = 首字 startMs - 20ms,end = 末字 endMs + 20ms。

    首尾的标点/空白不计入(否则前导逗号会把卡片起点提前 ~200ms,在与 rs_sync 对照时表现为偏移)。
    """
    for card in cards:
        raw = card["text"]
        lead = len(raw) - len(raw.lstrip(PUNCT_WS))
        trail = len(raw) - len(raw.rstrip(PUNCT_WS))
        lo = card["start"] + lead
        hi = max(lo + 1, card["end"] - trail)
        idxs = [index_map[p] for p in range(lo, hi)
                if index_map and p < len(index_map) and index_map[p] is not None]
        if not idxs or not char_times:
            continue
        first, last = min(idxs), max(idxs)
        first, last = max(0, min(first, len(char_times) - 1)), max(0, min(last, len(char_times) - 1))
        card["charSpan"] = [min(idxs), max(idxs) + 1]
        # 字级锚点:卡时间的唯一合法边界(见 _relax_gaps)。起点 ≤ 首字 startMs,终点 ≥ 末字 endMs。
        card["anchorStartMs"] = int(char_times[first]["startMs"])
        card["anchorEndMs"] = int(char_times[last]["endMs"])
        card["startMs"] = max(0, char_times[first]["startMs"] - RELEASE_MS)
        card["endMs"] = char_times[last]["endMs"] + RELEASE_MS
    _relax_gaps(cards)


def _relax_gaps(cards: list[dict], min_gap_ms: int = 66) -> None:
    """相邻卡不得重叠,间距 ≥2 帧;**但对齐精度优先**。

    只在「释放余量」内调整:后卡起点最多推迟到其首字 startMs,前卡终点最多提前到其
    末字 endMs。余量耗尽仍不足 2 帧 → **保持字级精确时间**(宁可间距紧,不可音画错位)。

    最短时长(0.83s)不在这里补:补时长会制造重叠。可读性调整放在 rs_subtitle
    的事件层(先「必并」合卡,再在有余量时延长)。
    """
    prev = None
    for card in cards:
        if "startMs" not in card:
            continue
        if prev is not None:
            need = min_gap_ms - (card["startMs"] - prev["endMs"])
            if need > 0:
                room_b = int(card.get("anchorStartMs", card["startMs"] + RELEASE_MS)) - card["startMs"]
                take = min(need, max(0, room_b))
                card["startMs"] += take
                need -= take
                if need > 0:
                    room_a = prev["endMs"] - int(prev.get("anchorEndMs", prev["endMs"] - RELEASE_MS))
                    prev["endMs"] -= min(need, max(0, room_a))
            prev["durMs"] = prev["endMs"] - prev["startMs"]
            prev["cps"] = round(prev["chars"] / (prev["durMs"] / 1000.0), 2) if prev["durMs"] else 0.0
        card["durMs"] = card["endMs"] - card["startMs"]
        card["cps"] = round(card["chars"] / (card["durMs"] / 1000.0), 2) if card["durMs"] else 0.0
        prev = card


def cps_max_for(max_chars: int) -> float:
    """按「每卡字数上限」反查 CPS 上限(策略表共读,见 cps_max_for_ratio)。

    调用方(如 rs_subtitle)只拿到 max_chars、拿不到比例;写死 `CPS_MAX["9x16"]`
    会在 16x9 / 3x4 下用错口径(见 OPTIMIZATION-v7 #4)。查不到时取最严档。
    """
    hard, soft = policy_constraints()
    mc_table = hard.get("maxChars") or MAX_CHARS
    cps_table = soft.get("cpsMax") or CPS_MAX
    for ratio, mc in mc_table.items():
        if mc == max_chars:
            return float(cps_table.get(ratio, 9.0))
    return float(min(cps_table.values()) if cps_table else 9.0)


def check_constraints(cards: list[dict], max_chars: int, cps_max: float,
                      dur_range=None) -> list[str]:
    """硬/软约束校验(rules/subtitles.md §4.4,T4.4 策略表)。返回违规说明列表,空即全过。

    时长区间缺省走策略表(软约束档);违反项由调用方分级:字数/重叠 = 硬,
    CPS/时长 = 软(rs_verify 的 L0 分级同一结论)。"""
    dr = dur_range if dur_range else dur_range_policy()
    bad: list[str] = []
    for c in cards:
        n_chars = c.get("chars") or len(c["text"].replace(" ", ""))
        if n_chars > max_chars:
            bad.append(f"卡{c['i']} 字数 {n_chars} > {max_chars}")
        if "durMs" in c:
            dur = c["durMs"] / 1000.0
            if dur < dr[0] - 1e-6:
                bad.append(f"卡{c['i']} 时长 {dur:.2f}s < {dr[0]}s")
            if dur > dr[1] + 1e-6:
                bad.append(f"卡{c['i']} 时长 {dur:.2f}s > {dr[1]}s")
            if c.get("cps", 0) > cps_max + 1e-6:
                bad.append(f"卡{c['i']} CPS {c['cps']} > {cps_max}")
    for a, b in zip(cards, cards[1:]):
        if "startMs" in a and "startMs" in b and b["startMs"] < a["endMs"]:
            bad.append(f"卡{a['i']}↔{b['i']} 时间重叠")
    return bad


def dur_range_policy() -> tuple[float, float]:
    """check_constraints 的时长缺省档(策略表 soft.durRangeS)。"""
    return dur_range()


# ---------------------------------------------------------------- 断句质量分(T4.7)

SEGSCORE_WEIGHTS = {"punct": 0.30, "word": 0.25, "conj": 0.15, "beat": 0.20, "viol": 0.10}


def segscore(sentences: list[dict], card_durs_s: list[float] | None = None,
             n_violations: int = 0, weights: dict | None = None) -> dict:
    """断句质量分(T4.7):S = w1*标点边界率 + w2*(1-词内切率) + w3*(1-连词收尾率)
    + w4*节拍达标率 + w5*(1-违规率) ∈ [0,1]。机械可算、同输入必同分(可回归)。

    sentences: [{"text": 句原文, "cuts": [切点], "gaps": {pos: ms}}](DP 每句输出);
    card_durs_s:最终卡时长秒表(节拍项);n_violations:最终违规条数。
    """
    w = dict(SEGSCORE_WEIGHTS)
    if weights:
        w.update(weights)
    n_cuts = n_punct = n_wordcut = 0
    n_cards = n_conjtail = 0
    for sen in sentences or []:
        text = sen.get("text") or ""
        cuts = sen.get("cuts") or []
        gaps = sen.get("gaps") or {}
        spans = word_spans(text)
        for c in cuts:
            n_cuts += 1
            prev = text[c - 1] if c - 1 < len(text) else ""
            if prev in TRAIL_PUNCT or prev in _SPACE or gaps.get(c, 0.0) >= 200.0:
                n_punct += 1                     # 标点/词组空格/字级停顿级边界
            if any(a < c < b for a, b in spans):
                n_wordcut += 1                   # 词内切(降级路径才会出现)
        for card in cards_from_cuts(text, cuts):
            n_cards += 1
            tail = card["text"].rstrip(PUNCT_WS + _SPACE)
            if tail and tail[-1] in NO_TAIL:
                n_conjtail += 1
    if not n_cards:
        return {"score": 0.0, "components": {}, "weights": w, "cards": 0}
    punct_rate = n_punct / n_cuts if n_cuts else 1.0
    word_rate = 1.0 - (n_wordcut / n_cuts if n_cuts else 0.0)
    conj_rate = 1.0 - n_conjtail / n_cards
    br = beat_range()
    if card_durs_s:
        beat_rate = sum(1 for d in card_durs_s if br[0] <= d <= br[1]) / len(card_durs_s)
    else:
        beat_rate = 1.0
    viol_rate = 1.0 - min(1.0, n_violations / n_cards)
    comp = {"punct": round(punct_rate, 4), "word": round(word_rate, 4),
            "conj": round(conj_rate, 4), "beat": round(beat_rate, 4),
            "viol": round(viol_rate, 4)}
    score = sum(w[k] * comp[k] for k in comp)
    return {"score": round(score, 4), "components": comp, "weights": w, "cards": n_cards}


# ---------------------------------------------------------------- 断句上下文审计(T4.14)

def audit_boundaries(cards_text: list[str], terms=(), idioms=DEFAULT_IDIOMS) -> list[dict]:
    """「异常换句」断句上下文审计(T4.14):检查每处卡边界是否切断固定搭配/动宾/引文。

    机械可判的三类嫌疑(全部可解释,报告直达条目):
      A 词跨度横跨边界(jieba/词表/PROTECTED/复合词都算)→ 固定搭配/动宾被斩断;
      B 否定词/把字介词收尾(「不」「把」「将」「对」)→ 与宾语跨卡;
      C 引文括号失衡或半括号挂卡首(「〕158号文件」式,20260920 实测事故形态)。
    返回 [{"between": [i, i+1], "left": 卡i, "right": 卡i+1, "reasons": [...]}]。
    """
    issues: list[dict] = []
    for k in range(len(cards_text) - 1):
        left_raw, right_raw = cards_text[k], cards_text[k + 1]
        left = left_raw.rstrip().rstrip(PUNCT_WS + _SPACE)
        right = right_raw.lstrip().lstrip(PUNCT_WS + _SPACE)
        if not left or not right:
            continue
        reasons: list[str] = []
        pair = left + right
        cut = len(left)
        for a, b in word_spans(pair, terms=terms, idioms=idioms):
            if a < cut < b:
                reasons.append(f"词/固定搭配「{pair[a:b]}」被切断(动宾或专名跨卡)")
                break
        if left[-1] in NEG_TAIL:
            reasons.append(f"否定词「{left[-1]}」收尾,被否定的对象落在下一卡")
        if left[-1] in NO_TAIL:
            reasons.append(f"连词「{left[-1]}」收尾(「而|被」式固定搭配被拆开)")
        if left[-1] in "把将对朝往跟同和与":
            reasons.append(f"介词/把字「{left[-1]}」收尾,宾语被推到下一卡")
        if right[0] in CITE_CLOSE:
            reasons.append(f"引文半括号挂卡首「{right[:4]}…」(引文被切断)")
        for op, cl in (("《", "》"), ("〔", "〕"), ("“", "”"), ("「", "」")):
            if left.count(op) > left.count(cl):
                reasons.append(f"引文「{op}…{cl}」被卡边界切断")
                break
        if reasons:
            issues.append({"between": [k, k + 1], "left": left_raw, "right": right_raw,
                           "reasons": reasons})
    return issues


def segment(text: str, max_chars: int = 12, *, min_chars: int = MIN_CHARS,
            gaps: dict[int, float] | None = None, index_map: list[int | None] | None = None,
            char_times: list[dict] | None = None, terms=(), idioms=DEFAULT_IDIOMS,
            top: int = 3, cps_max: float = 9.0, dur_range=DUR_RANGE,
            engine: str | None = None, degrade: list | None = None) -> dict:
    """约束最优卡切分。返回 {plans, ambiguous, cards, cuts, violations, degraded, …}。

    T4.1:有字级时间(index_map+char_times)时,最短时长以带权惩罚、幽灵卡以禁止
    边界直接进 DP——DP 直接产出合法解,不再依赖下游"先出卡再并/丢"的后处理。
    字级信息缺失(pos_times 建不出来)时这些约束缺席,由 rs_subtitle 的降级后处理
    承接(必须留痕)。
    """
    raw_text = text or ""
    text = raw_text.strip()
    # B1(v0.12):strip 剥掉句首/尾空白后,index_map 必须同步裁剪,否则一切按位
    # 取值(卡内位置 → chars 下标 → 字级时间)系统性偏移——实测 rs_sync 终点
    # 中位 -170ms、59/68 卡早退。必须**双侧对称**裁剪(校对稿句尾全角空格同样
    # 致命);末尾切片用 len(index_map)-trail_n,不用 lead_n+len(text)
    # (句中含连续空白时两者不等价)。
    if index_map and len(raw_text) != len(text):
        lead_n = len(raw_text) - len(raw_text.lstrip())
        trail_n = len(raw_text) - len(raw_text.rstrip())
        index_map = list(index_map[lead_n:len(index_map) - trail_n])
    if not text:
        return {"plans": [], "ambiguous": False, "cards": [], "cuts": [],
                "violations": [], "degraded": False, "wordFallback": False,
                "timeAware": False, "semanticHits": []}
    gaps = gaps or {}
    dr = dur_range if dur_range else DUR_RANGE
    # 字级时间 → DP 位置表(T4.1):位置 p 的字 ↔ raw chars 下标 ↔ (startMs, endMs)。
    pos_times: list[tuple[int, int]] | None = None
    if index_map and char_times:
        pts: list[tuple[int, int]] = []
        ok = True
        for p in range(len(text)):
            ci = index_map[p] if p < len(index_map) else None
            if ci is None or ci < 0 or ci >= len(char_times):
                ok = False
                break
            pts.append((int(char_times[ci]["startMs"]), int(char_times[ci]["endMs"])))
        pos_times = pts if ok else None
    if len(text) <= max_chars:
        cards = cards_from_cuts(text, [])
        chosen = _finalize(cards, index_map, char_times, max_chars, cps_max, dr)
        return {"plans": [{"score": 0.0, "cuts": [], "cards": chosen}],
                "ambiguous": False, "cards": chosen, "cuts": [],
                "violations": check_constraints(chosen, max_chars, cps_max, dr),
                "degraded": False, "wordFallback": False,
                "timeAware": pos_times is not None, "semanticHits": []}

    forb = forbidden_positions(text, terms, idioms)
    preferred = candidate_positions(text, gaps)
    sem_bad = semantic_bad_positions(text)
    # 词边界两阶段(ADR-0020):①词内位置强禁切;②无可行解才降级为词内强惩罚并留痕。
    in_word = {i for a, b in word_spans(text, terms=terms, idioms=idioms,
                                        engine=engine, degrade=degrade)
               for i in range(a + 1, b)}
    word_fallback = False
    ghost_fb = False
    gmin = ghost_min_ms()
    gban = pos_times is not None
    raw_plans = _dp(text, max_chars, min_chars, forb | in_word, gaps, preferred,
                    max(top, 1), sem_bad=sem_bad, pos_times=pos_times, ghost_ban=gban)
    if raw_plans == [[]] and in_word:
        word_fallback = True
        raw_plans = _dp(text, max_chars, min_chars, forb, gaps, preferred,
                        max(top, 1), in_word, sem_bad=sem_bad, pos_times=pos_times,
                        ghost_ban=gban)
    if raw_plans == [[]] and gban:
        # 幽灵卡禁切导致整体无解(pathological 字级时间,如整句坍缩)→ 放开禁切重解
        # 并留痕:约束缺席必须可见,不许静默(第四册退路表:DP 无解显式降级)。
        ghost_fb = True
        if degrade is not None:
            degrade.append("幽灵卡禁切导致 DP 无解,放开该约束重解(字级时间异常,请复核)")
        raw_plans = _dp(text, max_chars, min_chars, forb | in_word, gaps, preferred,
                        max(top, 1), sem_bad=sem_bad, pos_times=pos_times, ghost_ban=False)
        if raw_plans == [[]] and in_word:
            word_fallback = True
            raw_plans = _dp(text, max_chars, min_chars, forb, gaps, preferred,
                            max(top, 1), in_word, sem_bad=sem_bad, pos_times=pos_times,
                            ghost_ban=False)

    plans = []
    for cuts in raw_plans:
        # P30-1 末卡回吸:DP 出解后把「字数墙甩字」边界调整到语义完整处(先于成卡,
        # 孤卡合并/时间锚定都在回吸后的边界上做)。
        cuts, _absorbed = reabsorb_cuts(text, cuts, max_chars, forb=forb,
                                        preferred=preferred, terms=terms, idioms=idioms)
        cards = cards_from_cuts(text, cuts)
        cards, orphan_note = _merge_orphan_tail(cards, max_chars)
        cards = _finalize(cards, index_map, char_times, max_chars, cps_max, dr)
        viol = check_constraints(cards, max_chars, cps_max, dr) if char_times else \
            [v for v in check_constraints(cards, max_chars, cps_max, dr) if "CPS" not in v
             and "时长" not in v and "重叠" not in v]
        if orphan_note:
            viol.append(orphan_note)
        plans.append({"score": round(plan_score(text, cuts, max_chars, gaps, preferred,
                                                in_word if word_fallback else set()), 3),
                      "cuts": cuts, "cards": cards, "violations": viol})

    legal = [p for p in plans if not p["violations"]] or plans
    legal.sort(key=lambda p: -p["score"])
    top_plan = legal[0]
    # I1 语义命中留痕:最优解仍压着语义罚分切(极端句绕不开)→ 供 review queue(T4.12)
    hits = []
    n_len = len(text)
    for c in top_plan["cuts"]:
        if c >= n_len:
            continue                    # 回吸把边界推到句尾的防御(等价于无此切点)
        if text[c - 1] in NEG_TAIL:
            hits.append({"type": "negTail", "pos": c, "word": text[c - 1]})
        if c in sem_bad:
            hits.append({"type": "compound", "pos": c})
        if text[c] in DE_HEAD:
            hits.append({"type": "deHead", "pos": c, "word": text[c]})
    ambiguous = (len(legal) > 1 and
                 abs(legal[0]["score"] - legal[1]["score"]) / max(1e-6, abs(legal[0]["score"])) < 0.05)
    return {"plans": legal[:top], "ambiguous": ambiguous, "cards": top_plan["cards"],
            "cuts": top_plan["cuts"],
            "violations": top_plan["violations"], "degraded": bool(top_plan["violations"]),
            "wordFallback": word_fallback, "ghostFallback": ghost_fb,
            "timeAware": pos_times is not None, "semanticHits": hits}


def _finalize(cards: list[dict], index_map, char_times, max_chars, cps_max, dur_range) -> list[dict]:
    for c in cards:
        c["chars"] = len(c["text"].replace(" ", ""))
    if index_map and char_times:
        _attach_times(cards, index_map, char_times)
    return cards
