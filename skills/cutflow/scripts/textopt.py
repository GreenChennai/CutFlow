"""字幕文本轻改写引擎(ADR-0002):Netflix 简体中文规范 + 抖音红线。

规则可枚举、可单测;只动标点/口水词/断行,不改语义不删信息,音频不动。
依据:Netflix Chinese (Simplified) Timed Text Style Guide;抖音低质判定(无错别字/时间轴同步)。
"""
from __future__ import annotations

import re

# Netflix 规则:句号/逗号不入屏(逗号在断点转空格);?!保留;顿号仅行中;U+2026;禁 !? 连用
TAIL_DROP = "。，,;"  # 句号/全半角逗号/分号不入屏(Netflix)  # 句尾丢弃
FILLER_HEAD = re.compile(r"^[嗯呃唉哦噢诶哈]+[，,]?\s*")
FILLER_TAIL_AH = re.compile(r"[啊嘛呢吧啦呗呕噢](?=[。,,!?…]|$)")  # 句尾语气字(轻删)
SPACE_AFTER_SPLIT = " "
DOUBLE_PUNCT = re.compile(r"([!?])\1+")
ELLIPSIS = re.compile(r"。。。+|……|\.\.\.+")
FULLWIDTH_NUM = str.maketrans("０１２３４５６７８９", "0123456789")
BAD_END = "我你他她它们这那就都也很不有在和与跟对往朝从被把将一二三四五六七八九十百千万"
TAIL_FUNC = "的了着地吧呢啊吗嘛"


def normalize_text(text: str) -> str:
    """标点与口语规范化(不跨语义)。返回可直接断行的单句文本。"""
    t = text.strip()
    t = t.translate(FULLWIDTH_NUM)
    t = ELLIPSIS.sub("…", t)
    t = DOUBLE_PUNCT.sub(r"\1", t)
    t = FILLER_HEAD.sub("", t)
    t = FILLER_TAIL_AH.sub("", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def split_sentences(text: str) -> list[str]:
    """先规范化,再按 ?!?…。;: 分句,剥句尾句号/逗号(Netflix:标点不入屏)。"""
    t = normalize_text(text.strip())
    parts = re.split(r"(?<=[!?…。;::])\s*|(?<=。)\s*", t)
    out = []
    for p in parts:
        p = p.strip()
        while p and p[-1] in "。，,;::":
            p = p[:-1].rstrip()
        if p:
            out.append(p)
    return out


def card_split_length(sentence: str, max_chars: int) -> list[str]:
    """【旧算法,仅用于复现旧工程】单句 → 字幕卡(≤max_chars):长度驱动的贪心断行。"""
    if len(sentence) <= max_chars:
        return [_clean_card(sentence)]
    best, best_s = None, None
    for pos in range(3, min(len(sentence) - 1, max_chars)):
        left, right = sentence[:pos], sentence[pos:]
        if left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum():
            continue
        s = 0
        if left[-1] in "，，、;:,;:":
            s += 100
        if left[-1] == " ":
            s += 95
        if left[-1] in "!?…":
            s += 120
        if right[0] in "与之而或但及和":
            s += 20
        if left and left[-1] in BAD_END:
            s -= 150
        if left and left[-1] in TAIL_FUNC:
            s += 30
        if left and left[-1] in "的了是在和与把被对从向于也就都更最而即Each们":
            s += 35
        s -= 4 * abs(pos - len(sentence) // 2)
        if best_s is None or s > best_s:
            best, best_s = pos, s
    if best is None:
        best = max_chars
    head = _clean_card(sentence[:best])
    tail = sentence[best:].lstrip("，，、;:,;: ")
    return ([head] if head else []) + card_split_length(tail, max_chars)


def card_split(sentence: str, max_chars: int, mode: str = "dp",
               gaps: dict | None = None, terms=()) -> list[str]:
    """单句 → 字幕卡。默认走**约束最优 DP**(rules/subtitles.md §4 / segmentation.py)。

    mode="length" 回退旧长度驱动算法(仅用于复现旧工程)。
    """
    if mode == "length" or len(sentence) <= max_chars:
        if len(sentence) <= max_chars:
            return [_clean_card(sentence)]
        return card_split_length(sentence, max_chars)
    try:
        import segmentation
        plan = segmentation.segment(sentence, max_chars, gaps=gaps or {}, terms=terms)
        cards = [_clean_card(c["text"]) for c in plan["cards"]]
    except Exception:  # noqa: BLE001 — 任何异常都退回长度算法,不让字幕环节炸掉
        return card_split_length(sentence, max_chars)
    return [c for c in cards if c]



def _clean_card(card: str) -> str:
    """卡级清洗:句尾句号/逗号/顿号丢弃(Netflix),保留 ?!…。"""
    card = card.strip()
    while card and card[-1] in TAIL_DROP + "、::":
        card = card[:-1].rstrip()
    card = card.replace("，，", " ").replace("，", " ").replace(",,", " ").replace(",", " ")
    card = re.sub(r"\s{2,}", " ", card)
    return card.strip()


def build_cards(sentences: list[str], max_chars: int, mode: str = "dp",
                terms=()) -> list[str]:
    """句列表 → 卡列表(保持顺序)。"""
    cards: list[str] = []
    for s in sentences:
        cards.extend(card_split(s, max_chars, mode=mode, terms=terms))
    return [c for c in cards if c]
