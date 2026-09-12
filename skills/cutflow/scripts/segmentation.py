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
     "must_not_split": ("但是",)},
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
            forb.add(i)                      # 标点前不切(标点挂上一卡尾部,防「?关于…」式领头卡)
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
    """候选边界:标点处 + 字级停顿 ≥200ms 处 + 连词前。"""
    gaps = gaps or {}
    cand: set[int] = set()
    for i in range(1, len(text)):
        if text[i - 1] in TRAIL_PUNCT:
            cand.add(i)                      # 标点后切(标点跟上一卡)
        if text[i] in CONJ_HEAD:
            cand.add(i)
        if gaps.get(i, 0.0) >= 200.0:
            cand.add(i)
    return cand


# ---------------------------------------------------------------- 打分

def cut_score(text: str, pos: int, gap_ms: float = 0.0, max_chars: int = 12,
              preferred: bool = True) -> float:
    """单个切点的分数(越大越好),对应 rules/subtitles.md §4.3。"""
    left, right = text[:pos], text[pos:]
    s = 2.0 * PUNCT_LEVEL.get(left[-1] if left else "", 0.0)
    s += 1.5 * min(max(gap_ms, 0.0) / 500.0, 1.0)
    if left and left[-1] not in TAIL_FUNC:
        s += 0.5                                     # 不以虚词结尾
    if left and left[-1] in NO_TAIL:
        s += NO_TAIL_PENALTY                         # 连词/引导字不得收尾(防切词)
    if right and right[0] in CONJ_HEAD:
        s += 0.5                                     # 连词起首 = 从句边界(BBC:clause boundary)
    if not preferred:
        s -= 0.5                                     # 非候选边界需付出代价
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
               preferred: set[int] | None = None) -> float:
    gaps = gaps or {}
    preferred = preferred if preferred is not None else set()
    score = 0.0
    prev = 0
    for c in cuts:
        score += cut_score(text, c, gaps.get(c, 0.0), max_chars, c in preferred)
        score += CUT_COST
        score += _card_penalty(c - prev, max_chars)
        prev = c
    score += _card_penalty(len(text) - prev, max_chars)
    return score


def _dp(text: str, max_chars: int, min_chars: int, forb: set[int],
        gaps: dict[int, float], preferred: set[int], top: int) -> list[list[int]]:
    """返回 top-N 个切点序列(按分排序)。DP 状态 = 位置 → 前 N 优方案。"""
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
                    add += cut_score(text, e, gaps.get(e, 0.0), max_chars, e in preferred)
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
    text = (text or "").strip()
    if not text:
        return {"plans": [], "ambiguous": False, "cards": [], "violations": [], "degraded": False}
    gaps = gaps or {}
    if len(text) <= max_chars:
        cards = cards_from_cuts(text, [])
        chosen = _finalize(cards, index_map, char_times, max_chars, cps_max, dur_range)
        return {"plans": [{"score": 0.0, "cuts": [], "cards": chosen}],
                "ambiguous": False, "cards": chosen,
                "violations": check_constraints(chosen, max_chars, cps_max, dur_range),
                "degraded": False}

    forb = forbidden_positions(text, terms, idioms)
    preferred = candidate_positions(text, gaps)
    raw_plans = _dp(text, max_chars, min_chars, forb, gaps, preferred, max(top, 1))

    plans = []
    for cuts in raw_plans:
        cards = cards_from_cuts(text, cuts)
        cards = _finalize(cards, index_map, char_times, max_chars, cps_max, dur_range)
        viol = check_constraints(cards, max_chars, cps_max, dur_range) if char_times else \
            [v for v in check_constraints(cards, max_chars, cps_max, dur_range) if "CPS" not in v
             and "时长" not in v and "重叠" not in v]
        plans.append({"score": round(plan_score(text, cuts, max_chars, gaps, preferred), 3),
                      "cuts": cuts, "cards": cards, "violations": viol})

    legal = [p for p in plans if not p["violations"]] or plans
    legal.sort(key=lambda p: -p["score"])
    top_plan = legal[0]
    ambiguous = (len(legal) > 1 and
                 abs(legal[0]["score"] - legal[1]["score"]) / max(1e-6, abs(legal[0]["score"])) < 0.05)
    return {"plans": legal[:top], "ambiguous": ambiguous, "cards": top_plan["cards"],
            "violations": top_plan["violations"], "degraded": bool(top_plan["violations"])}


def _finalize(cards: list[dict], index_map, char_times, max_chars, cps_max, dur_range) -> list[dict]:
    for c in cards:
        c["chars"] = len(c["text"].replace(" ", ""))
    if index_map and char_times:
        _attach_times(cards, index_map, char_times)
    return cards
