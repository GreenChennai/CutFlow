"""字幕卡切分:约束最优 DP(rules/subtitles.md §4,ADR-0001 修订)。

核心区分(旧版本混为一谈的两层):
  卡切分 segmentation —— 一句话切成几张卡   → 本模块(DP)
  行断开 line break    —— 一张卡内怎么折行 → rs_subtitle.break_line(评分算法,保留)

设计要点:
  · 候选边界 = 强/弱标点 + 字级停顿(gap ≥ 200ms)+ 句法线索;其余位置需承担惩罚
  · 禁切表(专名/成语/数量词+单位/数字+单位/的得了着之后/ASCII 词内)
  · 打分 = 标点层级 + 归一化停顿 + 语义完整性 - 长度失衡 - 尾卡过短
  · 硬约束 = 每卡字数 / 单卡时长 / CPS(有字级时间时校验)
  · 输出 top-N 候选;最优与次优差 < 5% 标记 ambiguous

纯标准库,可单测。用法见 rs_subtitle.py / tests/test_v4.py。
"""
from __future__ import annotations

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
CN_UNITS = "个岁次天年月日时秒分元块毛角米厘斤吨度倍页条第名位件台只张片章节课"
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

_SPACE = " \u3000"
# 词内强惩罚:两阶段 DP 降级后仍切在词内的代价(必须压过一切语义加分)
WORD_CUT_PENALTY = -3.0

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


# ---------------------------------------------------------------- 词边界(v0.8.1,ADR-0020)

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


def word_spans(text: str, terms=(), idioms=DEFAULT_IDIOMS) -> list[tuple[int, int]]:
    """词跨度列表 [(start, end));jieba 优先,降级内置高频词表(ADR-0020)。

    词不得横跨空格;terms/idioms 总是显式并入(专名/行业词即便 jieba 在场也可能被切碎)。
    """
    if not text:
        return []
    spans: set[tuple[int, int]] = set()
    jb = _try_jieba()
    if jb is not None:
        try:
            for _w, s, e in jb.tokenize(text):
                if e - s >= 2:
                    spans.update(r for r in _non_space_runs(s, e, text) if r[1] - r[0] >= 2)
        except Exception:
            jb = None
    if jb is None:
        lexicon = COMMON_WORDS | {t for t in terms if t} | set(idioms)
        spans.update(_lexicon_spans(text, lexicon))
    for t in list(terms) + list(idioms):
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

def cut_score(text: str, pos: int, gap_ms: float = 0.0, max_chars: int = 12,
              preferred: bool = True, in_word: bool = False) -> float:
    """单个切点的分数(越大越好),对应 rules/subtitles.md §4.3。"""
    left, right = text[:pos], text[pos:]
    s = 2.0 * PUNCT_LEVEL.get(left[-1] if left else "", 0.0)
    s += 1.5 * min(max(gap_ms, 0.0) / 500.0, 1.0)
    if left and left[-1] not in TAIL_FUNC:
        s += 0.5                                     # 不以虚词结尾
    if left and left[-1] in _SPACE:
        s += 0.3                                     # 空格收尾 = 词组边界
    if left and left[-1] in NO_TAIL:
        s += NO_TAIL_PENALTY                         # 连词/引导字不得收尾(防切词)
    if right and right[0] in CONJ_HEAD:
        s += 0.5                                     # 连词起首 = 从句边界(BBC:clause boundary)
    if not preferred:
        s -= 0.5                                     # 非候选边界需付出代价
    if in_word:
        s += WORD_CUT_PENALTY                        # 切在词内(仅两阶段降级路径可达)
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
    score = 0.0
    prev = 0
    for c in cuts:
        score += cut_score(text, c, gaps.get(c, 0.0), max_chars, c in preferred, c in in_word)
        score += CUT_COST
        score += _card_penalty(c - prev, max_chars)
        prev = c
    score += _card_penalty(len(text) - prev, max_chars)
    return score


def _dp(text: str, max_chars: int, min_chars: int, forb: set[int],
        gaps: dict[int, float], preferred: set[int], top: int,
        in_word: set[int] | None = None) -> list[list[int]]:
    """返回 top-N 个切点序列(按分排序)。DP 状态 = 位置 → 前 N 优方案。"""
    in_word = in_word if in_word is not None else set()
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
                add = _imbalance(prev_len, L, max_chars) if prev_len else 0.0
                if e == n:
                    add += _card_penalty(L, max_chars)
                else:
                    add += cut_score(text, e, gaps.get(e, 0.0), max_chars,
                                     e in preferred, e in in_word)
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
    """按「每卡字数上限」反查 CPS 上限。

    调用方(如 rs_subtitle)只拿到 max_chars、拿不到比例;写死 `CPS_MAX["9x16"]`
    会在 16x9 / 3x4 下用错口径(见 OPTIMIZATION-v7 #4)。查不到时取最严档。
    """
    for ratio, mc in MAX_CHARS.items():
        if mc == max_chars:
            return float(CPS_MAX.get(ratio, 9.0))
    return float(min(CPS_MAX.values()))


def check_constraints(cards: list[dict], max_chars: int, cps_max: float,
                      dur_range=DUR_RANGE) -> list[str]:
    """硬约束校验(rules/subtitles.md §4.4)。返回违规说明列表,空即全过。"""
    bad: list[str] = []
    for c in cards:
        n_chars = c.get("chars") or len(c["text"].replace(" ", ""))
        if n_chars > max_chars:
            bad.append(f"卡{c['i']} 字数 {n_chars} > {max_chars}")
        if "durMs" in c:
            dur = c["durMs"] / 1000.0
            if dur < dur_range[0] - 1e-6:
                bad.append(f"卡{c['i']} 时长 {dur:.2f}s < {dur_range[0]}s")
            if dur > dur_range[1] + 1e-6:
                bad.append(f"卡{c['i']} 时长 {dur:.2f}s > {dur_range[1]}s")
            if c.get("cps", 0) > cps_max + 1e-6:
                bad.append(f"卡{c['i']} CPS {c['cps']} > {cps_max}")
    for a, b in zip(cards, cards[1:]):
        if "startMs" in a and "startMs" in b and b["startMs"] < a["endMs"]:
            bad.append(f"卡{a['i']}↔{b['i']} 时间重叠")
    return bad


def segment(text: str, max_chars: int = 12, *, min_chars: int = MIN_CHARS,
            gaps: dict[int, float] | None = None, index_map: list[int | None] | None = None,
            char_times: list[dict] | None = None, terms=(), idioms=DEFAULT_IDIOMS,
            top: int = 3, cps_max: float = 9.0, dur_range=DUR_RANGE) -> dict:
    """约束最优卡切分。返回 {plans, ambiguous, cards, violations, degraded}。"""
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
        return {"plans": [], "ambiguous": False, "cards": [], "violations": [],
                "degraded": False, "wordFallback": False}
    gaps = gaps or {}
    if len(text) <= max_chars:
        cards = cards_from_cuts(text, [])
        chosen = _finalize(cards, index_map, char_times, max_chars, cps_max, dur_range)
        return {"plans": [{"score": 0.0, "cuts": [], "cards": chosen}],
                "ambiguous": False, "cards": chosen,
                "violations": check_constraints(chosen, max_chars, cps_max, dur_range),
                "degraded": False, "wordFallback": False}

    forb = forbidden_positions(text, terms, idioms)
    preferred = candidate_positions(text, gaps)
    # 词边界两阶段(ADR-0020):①词内位置强禁切;②无可行解才降级为词内强惩罚并留痕。
    in_word = {i for a, b in word_spans(text, terms=terms, idioms=idioms)
               for i in range(a + 1, b)}
    word_fallback = False
    raw_plans = _dp(text, max_chars, min_chars, forb | in_word, gaps, preferred, max(top, 1))
    if raw_plans == [[]] and in_word:
        word_fallback = True
        raw_plans = _dp(text, max_chars, min_chars, forb, gaps, preferred,
                        max(top, 1), in_word)

    plans = []
    for cuts in raw_plans:
        cards = cards_from_cuts(text, cuts)
        cards, orphan_note = _merge_orphan_tail(cards, max_chars)
        cards = _finalize(cards, index_map, char_times, max_chars, cps_max, dur_range)
        viol = check_constraints(cards, max_chars, cps_max, dur_range) if char_times else \
            [v for v in check_constraints(cards, max_chars, cps_max, dur_range) if "CPS" not in v
             and "时长" not in v and "重叠" not in v]
        if orphan_note:
            viol.append(orphan_note)
        plans.append({"score": round(plan_score(text, cuts, max_chars, gaps, preferred,
                                                in_word if word_fallback else set()), 3),
                      "cuts": cuts, "cards": cards, "violations": viol})

    legal = [p for p in plans if not p["violations"]] or plans
    legal.sort(key=lambda p: -p["score"])
    top_plan = legal[0]
    ambiguous = (len(legal) > 1 and
                 abs(legal[0]["score"] - legal[1]["score"]) / max(1e-6, abs(legal[0]["score"])) < 0.05)
    return {"plans": legal[:top], "ambiguous": ambiguous, "cards": top_plan["cards"],
            "violations": top_plan["violations"], "degraded": bool(top_plan["violations"]),
            "wordFallback": word_fallback}


def _finalize(cards: list[dict], index_map, char_times, max_chars, cps_max, dur_range) -> list[dict]:
    for c in cards:
        c["chars"] = len(c["text"].replace(" ", ""))
    if index_map and char_times:
        _attach_times(cards, index_map, char_times)
    return cards
