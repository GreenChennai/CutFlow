"""字幕:时间轴来源(Wordline / TTS manifest / transcript)→ SRT + ASS(DP 卡切分 + 风格模板)。

用法:
  rs_subtitle.py --from-wordline 05_ir/wordline.json --style talkshow-bold --ratio 9x16 --out 06_output
  rs_subtitle.py --from-tts 03_assets/tts/manifest.json --style tutorial-clean --ratio 9x16 --out 06_output
  rs_subtitle.py --from-transcript 02_sensed/transcript_corrected.json --ratio 16x9 --out 06_output

核心变化(ADR-0001 修订 / ADR-0011):
  · 卡切分走约束最优 DP(segmentation.py),不再是长度驱动
  · **卡的时间从 Wordline 字级时间戳聚合**(首字 startMs-20ms ~ 末字 endMs+20ms),
    彻底废除「按字符数比例插值」
  · 无字级时间时,统一经 rs_align.build_wordline 生成(并在报告里标注 degraded),不另立第二套时间
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rs_common  # noqa: E402
from rs_common import RATIOS, canvas_for, emit  # noqa: E402
import segmentation  # noqa: E402
import textopt  # noqa: E402

PLATFORMS_PATH = Path(__file__).resolve().parents[1] / "templates" / "platforms.json"


def load_platforms() -> dict:
    """读平台预设档案(templates/platforms.json)。缺文件返回空表,不阻塞普通出片。"""
    if not PLATFORMS_PATH.is_file():
        return {}
    try:
        return json.loads(PLATFORMS_PATH.read_text(encoding="utf-8")).get("platforms") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_platform(name: str | None) -> dict:
    """平台名 → 预设;未知名直接报错(不静默退回默认,否则会出一版错规格的片子)。"""
    if not name:
        return {}
    table = load_platforms()
    if name not in table:
        raise KeyError(f"未知平台 {name!r}(可选 {'/'.join(table) or '无'})")
    return dict(table[name])

MAX_CHARS = dict(segmentation.MAX_CHARS)          # 9x16=12 / 16x9=22(竖屏已从 16 下调)
TAIL_STOP = "。!?…;:!?"
MID_STOP = ",,、;:? "
TAIL_FUNC = "的了着地吧呢啊吗嘛"
BAD_END = "我你他她它们这那就都也很不有在和与跟对往朝从被把将"

FRAME_MS = 1000 / 30.0                            # 卡间距 ≥2 帧
MIN_GAP_FRAMES = 2

RELEASE_MS = segmentation.RELEASE_MS             # 卡相对首/末字的时间释放余量
EXTEND_MAX_S = 0.30                               # 为凑最短时长可延长的后沿上限(秒)

_PUNCT_ONLY = set("。,、;::!?!?…,. ;:~·—–-()()《》「」『』【】[]“”‘’\"' ")

STYLES = {
    "talkshow-bold": {
        "font": "Microsoft YaHei", "size": {"9x16": 78, "3x4": 72, "16x9": 64},
        "primary": "&H00FFFFFF", "outline": "&H00101010", "back": "&H80000000",
        "outline_w": 3, "shadow": 1, "margin_v": {"9x16": 500, "3x4": 400, "16x9": 120},
        "align": 2, "bold": 1,
    },
    "tutorial-clean": {
        "font": "Microsoft YaHei", "size": {"9x16": 62, "3x4": 58, "16x9": 56},
        "primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H60000000",
        "outline_w": 2, "shadow": 0, "margin_v": {"9x16": 520, "3x4": 430, "16x9": 90},
        "align": 2, "bold": 0, "border_style": 3,
    },
    "subtitle-white": {
        "font": "Microsoft YaHei", "size": {"9x16": 68, "3x4": 64, "16x9": 58},
        "primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H00000000",
        "outline_w": 2, "shadow": 1, "margin_v": {"9x16": 320, "3x4": 280, "16x9": 110},
        "align": 2, "bold": 1,
    },
}


# ---------------------------------------------------------------- 行断开(保留评分算法)

def score_break(line: str, pos: int) -> int:
    left, right = line[:pos], line[pos:]
    s = 0
    if left and left[-1] in MID_STOP + TAIL_STOP:
        s += 100
    if right and right[0] in MID_STOP:
        s += 90
    if right and right[0] == " ":
        s += 90
    if left and left[-1] in TAIL_FUNC:
        s += 30
    if left and left[-1] in BAD_END:
        s -= 150
    s -= 4 * pos
    return s


def pyramid_fix(lines: list[str]) -> list[str]:
    """金字塔形 + 禁顶行 1–2 字(rules/subtitles.md §2)。仅在安全时搬字。"""
    if len(lines) < 2:
        return lines
    last, prev = lines[-1], lines[-2]
    if len(last) > 2:
        return lines
    move = min(3 - len(last), len(prev) - 3)
    if move <= 0:
        return lines
    seg = prev[-move:]
    if any(c.isascii() and c.isalnum() for c in seg):
        return lines
    if last and last[0].isascii() and last[0].isalnum():
        return lines
    lines = list(lines)
    lines[-2] = prev[:-move].rstrip()
    lines[-1] = (prev[-move:] + last).strip()
    return lines


def _break_rec(line: str, max_chars: int) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    best, best_s = None, None
    for p in range(3, max_chars - 1):
        if len(line) > p + 1 and line[p - 1].isascii() and line[p - 1].isalnum() \
                and line[p].isascii() and line[p].isalnum():
            continue
        s = score_break(line, p)
        if best_s is None or s > best_s:
            best, best_s = p, s
    if best is None:
        best = max_chars
    return [line[:best].rstrip()] + _break_rec(line[best:].lstrip(), max_chars)


def break_line(line: str, max_chars: int, pyramid: bool = True) -> list[str]:
    lines = _break_rec(line, max_chars)
    return pyramid_fix(lines) if pyramid else lines


# ---------------------------------------------------------------- 时间格式

def _ts_srt(sec: float) -> str:
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    s, ms = int(sec % 60), int((sec % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ts_ass(sec: float) -> str:
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


# ---------------------------------------------------------------- Wordline 路径(唯一时间来源)

def _sentence_slices(wl: dict) -> list[tuple[str, list[int], dict]]:
    """把 Wordline 切成 (原始文本, 位置→chars下标 映射, 停顿表)。"""
    chars = wl.get("chars", [])
    sents = wl.get("sentences") or []
    out = []
    if not sents:
        sents = [{"id": 0, "span": [0, len(chars)]}]
    for s in sents:
        a, b = s.get("span", [0, 0])
        a, b = max(0, int(a)), min(int(b), len(chars))
        if b <= a:
            continue
        text = "".join(c["ch"] for c in chars[a:b])
        idxmap = list(range(a, b))
        gaps = {}
        for k in range(1, b - a):
            d = int(chars[a + k]["startMs"]) - int(chars[a + k - 1]["endMs"])
            if d > 0:
                gaps[k] = float(d)
        out.append((text, idxmap, gaps))
    return out


def _to_cards(events: list[dict]) -> list[dict]:
    """事件 → 约束校验用的卡结构(与 events_from_wordline 尾部同口径)。"""
    out = []
    for i, e in enumerate(events):
        n_chars = len(e["text"].replace(" ", ""))
        dur_ms = int(round((e["end"] - e["start"]) * 1000))
        out.append({"i": i, "text": e["text"], "chars": n_chars,
                    "startMs": int(e["start"] * 1000), "endMs": int(e["end"] * 1000),
                    "durMs": dur_ms,
                    "cps": round(n_chars / (dur_ms / 1000.0), 2) if dur_ms else 0.0})
    return out


def _dp_events(wl: dict, max_chars: int, *, terms=(), top: int = 3,
               mode: str = "dp") -> tuple[list[dict], list[dict], list[str], int]:
    """逐句 DP 切分 → 原始事件(未必并/未延长/未间距),附候选与降级留痕。

    events_from_wordline 与 override 的 partial 模式共用(B7:部分替换需要
    DP 分组做基底)。
    """
    events: list[dict] = []
    candidates: list[dict] = []
    seg_degrade: list[str] = []      # 单句 DP 失败 → 退回长度算法,但必须留痕
    word_fb_count = 0                # 词内全禁无可行解、走了词内强惩罚的句数(留痕)
    for text, idxmap, gaps in _sentence_slices(wl):
        if all(ch in _PUNCT_ONLY or not ch.strip() for ch in text):
            continue          # 纯标点句跳过:DP 对它产卡缺 startMs(会以 0.0s 污染排序)
        if mode == "length":
            cards = [{"i": i, "text": c, "start": None, "end": None}
                     for i, c in enumerate(textopt.card_split(text, max_chars, mode="length"))]
            plan = {"cards": cards, "violations": [], "ambiguous": False}
        else:
            try:
                plan = segmentation.segment(text, max_chars, gaps=gaps, index_map=idxmap,
                                            char_times=wl.get("chars"), terms=terms, top=top)
            except Exception as exc:  # noqa: BLE001 — 单句分段失败不该炸掉整条字幕
                seg_degrade.append(f"句「{text[:12]}」DP 分段失败,退回长度算法"
                                   f"({type(exc).__name__}: {exc})")
                plan = {"cards": [{"i": i, "text": c, "startMs": None, "endMs": None}
                                  for i, c in enumerate(textopt.card_split_length(text, max_chars))],
                        "violations": [], "ambiguous": False, "plans": []}
        word_fb_count += 1 if plan.get("wordFallback") else 0
        candidates.append({"sentence": text, "ambiguous": plan.get("ambiguous", False),
                           "plans": [{"score": p["score"], "cards": [c["text"] for c in p["cards"]]}
                                     for p in plan.get("plans", [])]})
        for c in plan["cards"]:
            cleaned = textopt._clean_card(c["text"])
            if not cleaned:
                continue
            if c.get("startMs") is None:            # length 模式:退化为句内均分(仅复现旧工程)
                ev = {"start": 0.0, "end": 0.0, "text": cleaned, "degraded": True}
            else:
                ev = {"start": c["startMs"] / 1000.0, "end": c["endMs"] / 1000.0, "text": cleaned}
                # 字级锚点:后沿/起点调整只能在释放余量内做(见 _enforce_gaps / _extend_short)
                ev["anchorStart"] = c.get("anchorStartMs", c["startMs"] + RELEASE_MS) / 1000.0
                ev["anchorEnd"] = c.get("anchorEndMs", c["endMs"] - RELEASE_MS) / 1000.0
            if c.get("charSpan"):
                # Agent 复核定位用:卡 ↔ wordline 内容字全局索引(ADR-0020)
                ev["charSpan"] = list(c["charSpan"])
            events.append(ev)
    return events, candidates, seg_degrade, word_fb_count


def events_from_wordline(wl: dict, max_chars: int, *, terms=(), top: int = 3,
                         mode: str = "dp", karaoke: bool = False,
                         cps_max: float | None = None) -> tuple[list[dict], dict]:
    """Wordline → 字幕事件。卡时间 = 首字/末字时间戳聚合(align.md §4)。

    `wl.charTimingEstimated`(无字级时间戳)时,卡内位置是**估算**的:仍按 max_chars
    出卡以保证可读性,但在 `degradeReasons` 里显式标注"卡内位置为估算",并由
    `meta["charTimingEstimated"]` 告知上游 —— 真正的字级时间由 `rs_dub align`(#10)补齐。
    """
    events, candidates, seg_degrade, word_fb_count = _dp_events(
        wl, max_chars, terms=terms, top=top, mode=mode)

    events.sort(key=lambda e: e["start"])
    kar_attached = 0
    if karaoke:
        # dev-jj2815 实测:必须**先挂字再排卡**。_clean_card 剥掉的标点会在卡拉OK
        # 显示层经 chars 原样带回(「能动性,」显示 5 字形),必并/合规校验若只数
        # 清洗文本会漏掉这笔预算 → ASS 里冒出 13-14 字卡。挂字后把文本刷新为
        # chars 拼接,合并预算与 rs_verify 从此同口径(显示字形,含标点)。
        kar_attached = _apply_karaoke_chars(events, wl)
    events, merged_short = _merge_short(events, max_chars)
    extended = _extend_short(events)
    _enforce_gaps(events)
    # 约束校验必须在**可读性调整之后**做,否则报的是已经不存在的问题
    final_cards = _to_cards(events)
    violations = segmentation.check_constraints(
        final_cards, max_chars, cps_max or segmentation.cps_max_for(max_chars))
    reasons = list(wl.get("degradeReasons", [])) + seg_degrade
    estimated = bool(wl.get("charTimingEstimated"))
    if estimated and not any("估算" in r for r in reasons):
        reasons.append("卡内位置为估算(无字级时间戳),建议 rs_dub align 补字级")
    if word_fb_count:
        reasons.append(f"{word_fb_count} 句词内全禁无可行解,按词内强惩罚切分(ADR-0020 留痕)")
    meta = {"degraded": bool(wl.get("degraded")) or any(e.get("degraded") for e in events),
            "degradeReasons": reasons,
            "charTimingEstimated": estimated,
            "violations": violations, "candidates": candidates,
            "mergedShort": merged_short, "extendedShort": extended,
            "karaokeAttached": kar_attached,
            "wordFallbackSentences": word_fb_count,
            "ambiguous": sum(1 for c in candidates if c["ambiguous"])}
    return events, meta


def _content_index(chars: list[dict]) -> tuple[str, list[int]]:
    """内容字串 S + 每个内容字位置 → raw chars 下标(B7 文本锚定用)。

    v0.12 起委托 rs_common.content_index(与 rs_cut --from-text / rs_ir --from-cards
    同一实现,消三处重复);口径仍是标点(PUNCT_WS)+ 空白剔除。
    """
    return rs_common.content_index(chars)


def _normalize_card_text(t: str) -> str:
    """卡文本去标点/空白 → 内容字(B7:按内容定位,绝不做算术偏移)。"""
    return rs_common.content_text(t)


def _resolve_override_requests(ov: dict, chars: list[dict], s: str, idx: list[int]
                               ) -> list[dict]:
    """override 卡 → 内容字区间请求(递增、不重叠)。

    三种定位(B7,BUGREPORT-20260913):
      {"span": [a, b]}                旧契约:raw chars 内容字全局索引;
      {"text": "……"}                  新:去标点后在 S 上**顺序锚定**;
      {"textPrefix": "…", "textSuffix": "…"}  新:前缀定位起点、后缀定位终点(中间吞并)。
    按"内容字数"做算术偏移必然切错位(raw 索引含标点/空格条目),一律走锚定。
    """
    import bisect
    requests: list[dict] = []
    cursor = 0
    for it in (ov.get("cards") or []):
        note = it.get("note") if isinstance(it, dict) else None
        if not isinstance(it, dict):
            raise ValueError(f"override cards 元素应为对象:{it!r}")
        if "span" in it:
            try:
                a, b = int(it["span"][0]), int(it["span"][1])
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ValueError(f"override span 非法:{it!r}") from exc
            if not (0 <= a < b <= len(chars)):
                raise ValueError(f"override span 越界:[{a},{b}),chars 总数 {len(chars)}")
            ca, cb = bisect.bisect_left(idx, a), bisect.bisect_left(idx, b)
        elif "text" in it:
            t = _normalize_card_text(str(it["text"]))
            if not t:
                raise ValueError(f"override text 归一化后为空:{it!r}")
            pos = s.find(t, cursor)
            if pos < 0:
                raise ValueError(f"override text 无法在 wordline 内容串中锚定(必须按原文顺序):"
                                 f"「{t[:14]}…」(游标 {cursor}/{len(s)})")
            ca, cb = pos, pos + len(t)
            cursor = cb
        elif "textPrefix" in it:
            pre = _normalize_card_text(str(it.get("textPrefix") or ""))
            suf = _normalize_card_text(str(it.get("textSuffix") or ""))
            if not pre or not suf:
                raise ValueError(f"override textPrefix/textSuffix 均不能为空:{it!r}")
            pos = s.find(pre, cursor)
            if pos < 0:
                raise ValueError(f"override textPrefix 无法锚定:「{pre[:14]}…」(游标 {cursor})")
            end = s.find(suf, pos + len(pre))
            if end < 0:
                raise ValueError(f"override textSuffix 无法在前缀之后锚定:「{suf[:14]}…」")
            ca, cb = pos, end + len(suf)
            cursor = cb
        else:
            raise ValueError(f"override 卡缺定位字段(需要 span / text / textPrefix+textSuffix):{it!r}")
        requests.append({"content": [ca, cb], "note": note})
    for r1, r2 in zip(requests, requests[1:]):
        if r2["content"][0] < r1["content"][1]:
            raise ValueError(f"override 区间重叠/乱序:{r1['content']} 与 {r2['content']}"
                             f"(契约见 rules/subtitles.md §10.3,不做静默重排)")
    return requests


def _event_from_content_range(ca: int, cb: int, chars: list[dict], idx: list[int]) -> dict | None:
    """内容字区间 [ca, cb) → 字幕事件(时间唯一真相源 = wordline 字级锚)。

    卡尾标点至多带一个(防「。，」连挂);纯标点/空 span 返回 None。
    """
    if cb <= ca:
        return None
    first = idx[ca]
    last = idx[cb - 1]
    content_last = last   # 终点锚 = 最后一个内容字(卡尾可带标点显示,但标点在重映射后
    if last + 1 < len(chars):   # 可能吸收停顿宽度,不能当终点锚;否则 rs_sync 的
        nxt = chars[last + 1]["ch"]   # 「滞留过久」闸必炸 — 2026-09-15 NCLM1605 实测)
        if not nxt.strip() or nxt in segmentation.PUNCT_WS:
            last += 1          # 卡尾标点至多带一个
    cleaned = textopt._clean_card("".join(c["ch"] for c in chars[first:last + 1]))
    if not cleaned:
        return None
    return {"start": max(0.0, (chars[first]["startMs"] - RELEASE_MS) / 1000.0),
            "end": (chars[content_last]["endMs"] + RELEASE_MS) / 1000.0,
            "text": cleaned,
            "anchorStart": chars[first]["startMs"] / 1000.0,
            "anchorEnd": chars[content_last]["endMs"] / 1000.0,
            "charSpan": [first, last + 1]}


def events_from_override(wl: dict, override: dict, max_chars: int, *,
                         karaoke: bool = False,
                         cps_max: float | None = None) -> tuple[list[dict], dict]:
    """Agent 复核修正(ADR-0020 / B7 扩展):按 override 从 wordline 重建卡片。

    时间唯一真相源仍是 wordline:起点 = 首字 startMs − 20ms、终点 = 末字 endMs + 20ms,
    重建后照常走必并/延长/间距/帧对齐与硬约束校验。

    覆盖模式:
      **full** —— override 区间覆盖全部内容字(旧行为):整表重建,不用 DP;
      **partial** —— 只覆盖一部分:以 DP 分组为基底,被 override 区间压住的 DP 卡
      被替换,其余沿用 DP 结果。微调一张卡不再需要重给全部 span(B7)。
    """
    import bisect
    chars = wl.get("chars") or []
    if not chars:
        raise ValueError("wordline 没有 chars,无法按 override 重建")
    s, idx = _content_index(chars)
    requests = _resolve_override_requests(override, chars, s, idx)
    if not requests:
        raise ValueError("override 没有有效卡片")

    covered = 0
    for ca, cb in (r["content"] for r in requests):
        covered += cb - ca
    full_mode = covered >= len(s)

    if full_mode:
        events = []
        for r in requests:
            ev = _event_from_content_range(r["content"][0], r["content"][1], chars, idx)
            if ev:
                events.append(ev)
        override_mode = "full"
    else:
        base, _cand, _deg, _wfb = _dp_events(wl, max_chars)
        if any("charSpan" not in e for e in base):
            raise ValueError("partial override 需要 DP 事件携带 charSpan(降级 wordline 不支持,"
                             "请改用全量 span 覆盖)")
        kept: list[tuple[int, dict]] = []      # (content 起点, 事件)
        for e in base:
            ra, rb = e["charSpan"]
            j0, j1 = bisect.bisect_left(idx, ra), bisect.bisect_left(idx, rb)
            if any(not (j1 <= ca or cb <= j0) for ca, cb in
                   (r["content"] for r in requests)):
                continue                        # 被 override 压住的 DP 卡 → 替换
            kept.append((j0, e))
        replaced: list[tuple[int, dict]] = []
        for r in requests:
            ev = _event_from_content_range(r["content"][0], r["content"][1], chars, idx)
            if ev:
                replaced.append((r["content"][0], ev))
        events = [e for _, e in sorted(kept + replaced, key=lambda t: t[0])]
        override_mode = "partial"

    events.sort(key=lambda e: e["start"])
    kar_attached = _apply_karaoke_chars(events, wl) if karaoke else 0
    events, merged = _merge_short(events, max_chars)
    extended = _extend_short(events)
    _enforce_gaps(events)
    final_cards = _to_cards(events)
    violations = segmentation.check_constraints(
        final_cards, max_chars, cps_max or segmentation.cps_max_for(max_chars))
    audit = [{"span": list(e["charSpan"]), "text": e["text"],
              "startMs": int(round(e["start"] * 1000)), "endMs": int(round(e["end"] * 1000)),
              "chars": len(e["text"].replace(" ", ""))}
             for e in events if "charSpan" in e]
    meta = {"degraded": bool(wl.get("degraded")),
            "degradeReasons": list(wl.get("degradeReasons") or []),
            "charTimingEstimated": bool(wl.get("charTimingEstimated")),
            "violations": violations, "candidates": [],
            "mergedShort": merged, "extendedShort": extended,
            "karaokeAttached": kar_attached, "wordFallbackSentences": 0,
            "ambiguous": 0,
            "overrideApplied": True, "overrideCards": len(requests),
            "overrideMode": override_mode, "audit": audit}
    return events, meta


MIN_DUR_S = 0.83


def _join(a: str, b: str) -> str:
    sep = " " if (a and b and a[-1].isascii() and a[-1].isalnum()
                  and b[0].isascii() and b[0].isalnum()) else ""
    return a.rstrip() + sep + b.lstrip()


def _merge_short(events: list[dict], max_chars: int,
                 min_dur: float = MIN_DUR_S) -> tuple[list[dict], int]:
    """<0.83s 必并(rules/subtitles.md §4.4):与相邻卡合并,前提是合并后不超字数上限。

    读得完是节奏问题,但**说出来的时间是对的**——所以宁可合卡,不去改时间。
    dev-jj2815 实测:必并线曾是 0.8s,与 DUR_MIN=0.83s 之间有 0.03s 死区——
    0.81s 卡既不触发必并,又因下一卡 2 帧间隙就到、延长被 _enforce_gaps 收回
    (「成为电商」0.81s)。改为与 MIN_DUR_S 同线;第一遍向上一卡并,预算放
    不下的第二遍向下一卡吞(起点取短卡,说出来的时间不动)。
    """
    out: list[dict] = []
    merged = 0
    for e in events:
        if out:
            prev = out[-1]
            text = _join(prev["text"], e["text"])
            short = (prev["end"] - prev["start"] < min_dur) or (e["end"] - e["start"] < min_dur)
            if short and len(text.replace(" ", "")) <= max_chars:
                prev["end"] = e["end"]
                prev["text"] = text
                if "anchorEnd" in e:
                    prev["anchorEnd"] = e["anchorEnd"]      # 合并后占的是后一卡的时间
                if "charSpan" in e:
                    prev["charSpan"] = [min(prev["charSpan"][0], e["charSpan"][0]),
                                        max(prev["charSpan"][1], e["charSpan"][1])] \
                        if "charSpan" in prev else list(e["charSpan"])
                if e.get("chars"):
                    # 卡拉OK:合并文本必须同步合并逐字时间,否则 _kar_text 丢字
                    prev["chars"] = (prev.get("chars") or []) + e["chars"]
                merged += 1
                continue
        out.append(dict(e))
    # 第二遍:仍过短的卡(上一卡预算放不下)向下一卡吞并
    i = 0
    while i < len(out) - 1:
        e = out[i]
        if e["end"] - e["start"] >= min_dur:
            i += 1
            continue
        nxt = out[i + 1]
        text = _join(e["text"], nxt["text"])
        if len(text.replace(" ", "")) <= max_chars:
            nxt["text"] = text
            nxt["start"] = e["start"]
            if "anchorStart" in e:
                nxt["anchorStart"] = e["anchorStart"]      # 起点取短卡(对齐精度)
            if "charSpan" in e:
                nxt["charSpan"] = [min(e["charSpan"][0], nxt["charSpan"][0]),
                                   max(e["charSpan"][1], nxt["charSpan"][1])] \
                    if "charSpan" in nxt else list(e["charSpan"])
            if e.get("chars"):
                nxt["chars"] = (e.get("chars") or []) + (nxt.get("chars") or [])
            out.pop(i)
            merged += 1          # 不 i+=1:吞并后的卡可能仍短,继续尝试
        else:
            i += 1
    return out, merged


def _extend_short(events: list[dict], min_dur: float = MIN_DUR_S) -> int:
    """仍有余量时,把过短卡的后沿延到最短时长——**绝不超过锚点上限**。

    上限 = min(末字 endMs + EXTEND_MAX_S, 下一卡起点 − 2 帧)。延长只是让字多在
    屏上停一会(可读性),不能一路延到下一卡导致字幕滞留到停顿里。
    """
    min_gap = MIN_GAP_FRAMES * FRAME_MS / 1000.0
    n = 0
    for i, e in enumerate(events):
        if e["end"] - e["start"] >= min_dur:
            continue
        want = e["start"] + min_dur
        if "anchorEnd" in e:
            want = min(want, e["anchorEnd"] + EXTEND_MAX_S)
        if i + 1 < len(events):
            want = min(want, events[i + 1]["start"] - min_gap)
        if want > e["end"]:
            e["end"] = want
            n += 1
    return n


def _enforce_gaps(events: list[dict], fps: float = 30.0,
                  min_gap_frames: int = MIN_GAP_FRAMES) -> int:
    """卡间距 ≥2 帧;**但对齐精度优先**——只在释放余量内调整,绝不动字级锚点。

    释放余量 = 卡起点与其首字 startMs 之差、卡终点与其末字 endMs 之差(各 ≤RELEASE_MS)。
    调整顺序:① 推迟后卡起点(最多到其首字锚点);② 收早前卡终点(最多到其末字锚点)。
    余量耗尽仍不足 2 帧 → 保持字级精确时间(宁可间距紧,不可音画错位)。返回仍不足项数。
    """
    min_gap = min_gap_frames * (1000.0 / fps) / 1000.0
    tight = 0
    for a, b in zip(events, events[1:]):
        need = min_gap - (b["start"] - a["end"])
        if need <= 1e-9:
            continue
        if "anchorStart" in b:
            b["start"] += min(need, max(0.0, b["anchorStart"] - b["start"]))
            need = min_gap - (b["start"] - a["end"])
        if need > 1e-9 and "anchorEnd" in a:
            a["end"] -= min(need, max(0.0, a["end"] - a["anchorEnd"]))
            need = min_gap - (b["start"] - a["end"])
        if need > 1e-9:
            tight += 1
    # 防御:任何情况下不得重叠(锚点保证 ≥0;无锚点事件走这里兜底)
    for a, b in zip(events, events[1:]):
        if b["start"] < a["end"]:
            b["start"] = a["end"]
    return tight


def _finalize_events(events: list[dict], fps: float = 30.0) -> None:
    """B5 落盘兜底:正时长、单调、无重叠(就在释放余量/防御语义内,绝不动锚点)。"""
    events.sort(key=lambda e: e["start"])
    frame = 1.0 / fps if fps and fps > 0 else 1 / 30.0
    for e in events:
        if e["end"] <= e["start"]:
            e["end"] = e["start"] + frame
    _enforce_gaps(events, fps)


def snap_events_to_frames(events: list[dict], fps: float = 30.0) -> int:
    """把字幕时间量化到帧:起点向下取整、终点向上取整(渲染只认帧)。

    v0.8.1:snap 之后追加**碰撞消解**——起点 floor / 终点 ceil 会让相邻卡产生
    ≤1 帧的伪重叠(用户实测 3ms 级)。锚点有余量时消解之;余量耗尽则保留伪重叠
    (切割点吸附伪影,不可见),由 rs_sync 的 1 帧容差放行。

    返回被调整的事件数。fps ≤ 0 时不处理。
    """
    if not fps or fps <= 0:
        return 0
    frame = 1.0 / fps
    n = 0
    for e in events:
        s = math.floor(round(e["start"] / frame, 6)) * frame
        t = math.ceil(round(e["end"] / frame, 6)) * frame
        if s < 0:
            s = 0.0
        if t <= s:
            t = s + frame
        if abs(s - e["start"]) > 1e-9 or abs(t - e["end"]) > 1e-9:
            e["start"], e["end"] = s, t
            n += 1
    # 碰撞消解:① 后卡起点推迟(最多到其 anchorStart);② 前卡终点收早(最多到其 anchorEnd);
    # 两者都会破坏对齐精度时,保持伪重叠——不可见,rs_sync 以 1 帧容差放行。
    for a, b in zip(events, events[1:]):
        if b["start"] >= a["end"] - 1e-9:
            continue
        if "anchorStart" in b and b["anchorStart"] >= a["end"] - 1e-9:
            b["start"] = a["end"]
        elif "anchorEnd" in a and a["anchorEnd"] <= b["start"] + 1e-9:
            a["end"] = b["start"]
    return n


# ---------------------------------------------------------------- 兼容入口

def build_events(entries: list[dict], max_chars: int, per_span_s: float = 4.0,
                 optimize: bool = True, terms=(), top: int = 3) -> list[dict]:
    """entries: [{start_s, end_s, text}] → 事件列表。

    统一经 rs_align.build_wordline 建时间轴再聚合(不另立第二套时间);
    optimize=False 时退回逐句断行 + 句内均分(仅复现旧工程)。
    """
    if not optimize:
        out = []
        for e in entries:
            dur = e["end_s"] - e["start_s"]
            lines = break_line(e["text"].replace("\n", " ").strip(), max_chars)
            per = dur / max(1, len(lines))
            for i, ln in enumerate(lines):
                out.append({"start": e["start_s"] + i * per,
                            "end": e["start_s"] + (i + 1) * per, "text": ln})
        return out
    import rs_align
    wl = rs_align.build_wordline(entries, "inline", degraded="内存构建(无字级时间戳)")
    events, _ = events_from_wordline(wl, max_chars, terms=terms, top=top)
    return events


# ---------------------------------------------------------------- 输出

def write_srt(events: list[dict], path: Path) -> None:
    body = "".join(f"{i}\n{_ts_srt(e['start'])} --> {_ts_srt(e['end'])}\n{e['text']}\n\n"
                   for i, e in enumerate(events, 1))
    path.write_text(body, encoding="utf-8")


def write_ass(events: list[dict], path: Path, style_name: str, ratio: str, canvas: str,
              karaoke: bool = False) -> None:
    """karaoke=True 时生成逐字卡拉OK:Primary=已唱色(黄),Secondary=未唱色(白),
    每字一个 \\kf 标签(时长=厘秒,取自字级时间戳;字间停顿计入前字)。"""
    st = STYLES.get(style_name) or STYLES["subtitle-white"]
    w, h = canvas.split("x")
    play_res = f"PlayResX: {w}\nPlayResY: {h}"
    margin_r, margin_l = 60, 60
    if karaoke:
        st = dict(st)
        st["primary"] = "&H0000E5FF"          # 已唱:暖黄(BGR)
        secondary = "&H00FFFFFF"              # 未唱:白
    else:
        secondary = "&H000000FF"
    header = f"""[Script Info]
Title: CutFlow subtitles
ScriptType: v4.00+
WrapStyle: 0
{play_res}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{st['font']},{st['size'][ratio]},{st['primary']},{secondary},{st['outline']},{st['back']},{st['bold']},0,0,0,100,100,0,0,{st.get('border_style', 1)},{st['outline_w']},{st['shadow']},{st['align']},{margin_l},{margin_r},{st['margin_v'][ratio]},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    if karaoke:
        lines = "".join(f"Dialogue: 0,{_ts_ass(e['start'])},{_ts_ass(e['end'])},Main,,0,0,0,,{_kar_text(e)}\n"
                        for e in events)
    else:
        lines = "".join(f"Dialogue: 0,{_ts_ass(e['start'])},{_ts_ass(e['end'])},Main,,0,0,0,,{e['text']}\n"
                        for e in events)
    path.write_text(header + lines, encoding="utf-8")


def _kar_text(e: dict) -> str:
    """事件 → \\kf 逐字文本。首字从卡头起唱,末字吃到卡尾;字间停顿计入前字时长。"""
    chs = e.get("chars") or []
    if not chs:
        return e["text"]
    ev_start = int(round(e["start"] * 1000))
    ev_end = int(round(e["end"] * 1000))
    parts = []
    for k, c in enumerate(chs):
        cur = c["startMs"] if k > 0 else min(c["startMs"], ev_start)
        if k + 1 < len(chs):
            nxt = chs[k + 1]["startMs"]
        else:
            # 末字吃到末字真实结束时刻(而非卡尾)——卡尾可能因可读性被延长
            nxt = min(ev_end, int(chs[-1].get("endMs", ev_end)))
        cs = max(1, int(round((nxt - cur) / 10.0)))
        parts.append(f"{{\\kf{cs}}}{c['ch']}")
    return "".join(parts)


def attach_karaoke_chars(events: list[dict], wl: dict) -> int:
    """把 wordline 字级时间按「卡起点分界」分配到事件(卡拉OK 用)。

    用起点分界而不是 [start,end] 窗口:卡间距压缩后尾部字符不丢。
    """
    import bisect
    chars = wl.get("chars") or []
    for e in events:
        e["chars"] = []
    if not chars or not events:
        return 0
    starts = [e["start"] for e in events]
    n = 0
    prev_k: int | None = None
    for c in chars:
        is_punct = all(ch in _PUNCT_ONLY for ch in c["ch"] if ch.strip())
        ctr = (c["startMs"] + c["endMs"]) / 2000.0
        if is_punct and prev_k is not None:
            # dev-jj2815 实测:retext 给插入标点分了 gap 中段时间(如 ?[4.41,4.59]),
            # 按时间中心 bisect 会把尾标点划给下一卡 → 出现「?关于…」式领头标点。
            # 标点是上一字的余音,一律跟随前字所在卡。
            k, ok = prev_k, True
        else:
            k = bisect.bisect_right(starts, ctr) - 1
            ok = 0 <= k < len(events) and ctr <= events[k]["end"] + 0.5
        if ok:
            events[k]["chars"].append(c)
            prev_k = k
            n += 1
        else:
            prev_k = None
    return n


def _apply_karaoke_chars(events: list[dict], wl: dict) -> int:
    """挂字 + 用 chars 拼接刷新显示文本(卡拉OK 的预算/校验基准 = 显示字形)。

    dev-jj2815 实测:_clean_card 剥掉的标点会在 \\kf 显示层原样重现,文本若
    停留在清洗态,必并预算与 rs_verify 计数都会比实际显示少 1-2 字形。
    """
    n = attach_karaoke_chars(events, wl)
    if n:
        for e in events:
            if e.get("chars"):
                e["text"] = "".join(c["ch"] for c in e["chars"])
    return n


def _karaoke_ready(wl: dict) -> tuple[bool, str]:
    """卡拉OK 只认真实字级时间戳;降级 wordline 一律拒绝(防静默插值回禁区)。"""
    if wl.get("degraded"):
        return False, "wordline 是降级时间(无字级时间戳)"
    if not (wl.get("chars") or []):
        return False, "wordline 没有 chars"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-wordline")
    ap.add_argument("--from-tts")
    ap.add_argument("--from-transcript")
    ap.add_argument("--style", default=None, help="字幕样式(默认按平台预设,再退回 subtitle-white)")
    ap.add_argument("--ratio", default=None, choices=list(RATIOS), help="画幅比例(默认按平台预设)")
    ap.add_argument("--canvas", default=None, help="如 1080x1920,默认按比例/平台预设推断")
    ap.add_argument("--platform", default=None,
                    help="平台预设:douyin / shipinhao / xiaohongshu / bilibili(见 templates/platforms.json)")
    ap.add_argument("--max-chars", dest="max_chars", type=int, default=None, help="覆盖每卡字数上限")
    ap.add_argument("--out", required=True)
    ap.add_argument("--terms", default="", help="专有名词表(逗号分隔),断句禁切用")
    ap.add_argument("--top", type=int, default=3, help="输出前 N 个切分候选")
    ap.add_argument("--segment", default="dp", choices=["dp", "length"])
    ap.add_argument("--no-optimize", action="store_true", help="关闭轻改写(默认开启)")
    ap.add_argument("--karaoke", action="store_true",
                    help="逐字卡拉OK字幕(\\kf 染色;需 pkg 后端字级时间戳的 wordline)")
    ap.add_argument("--allow-degraded", dest="allow_degraded", action="store_true",
                    help="卡拉OK 但 wordline 降级时,降级为普通字幕而不是报错")
    ap.add_argument("--override", default=None,
                    help="Agent 复核修正文件(subtitles_override.json):按 span / text / "
                         "textPrefix+textSuffix 从 wordline 重建卡片;支持部分替换(未提及卡沿用 DP),"
                         "仅支持 --from-wordline")
    ap.add_argument("--fps", type=float, default=30.0, help="帧率(字幕时间量化到帧)")
    ap.add_argument("--no-snap", dest="no_snap", action="store_true",
                    help="不做帧对齐,保留亚帧精度")
    a = ap.parse_args()

    # 优先级:显式参数 > 平台预设 > 内置默认(见 templates/platforms.json)
    try:
        preset = resolve_platform(a.platform)
    except KeyError as exc:
        return emit(False, "BAD_PLATFORM", str(exc), exit_code=2)
    ratio = a.ratio or preset.get("ratio") or "9x16"
    if ratio not in RATIOS:
        return emit(False, "BAD_RATIO", f"未知比例 {ratio}(可选 {'/'.join(RATIOS)})", exit_code=2)
    style = a.style or preset.get("style") or "subtitle-white"
    if a.canvas:
        canvas = a.canvas
    elif preset.get("canvas"):
        canvas = f"{preset['canvas'][0]}x{preset['canvas'][1]}"
    else:
        w, h = canvas_for(ratio)
        canvas = f"{w}x{h}"
    terms = tuple(t.strip() for t in a.terms.split(",") if t.strip())
    max_chars = a.max_chars or preset.get("maxChars") or MAX_CHARS[ratio]

    if a.from_wordline:
        wl = json.loads(Path(a.from_wordline).read_text(encoding="utf-8"))
    elif a.from_tts:
        man = json.loads(Path(a.from_tts).read_text(encoding="utf-8"))
        entries = [{"start_s": s["start_s"], "end_s": s["end_s"], "text": s["text"],
                    "timestamp": s.get("timestamp") or s.get("chars")}
                   for s in man.get("sentences", [])]
        wl = _wordline(entries, a.from_tts, "TTS 路径:句级时间(+ 字级若可用)")
    elif a.from_transcript:
        tr = json.loads(Path(a.from_transcript).read_text(encoding="utf-8"))
        entries = [{"start": float(s["start"]),
                    "end": float(s.get("end") or (float(s["start"]) + 3.0)),
                    "text": s["text"], "timestamp": s.get("timestamp") or s.get("chars")}
                   for s in tr.get("segments", [])]
        wl = _wordline(entries, a.from_transcript, "transcript 句级时间(未字级对齐)")
    else:
        return emit(False, "NO_SOURCE", "需要 --from-wordline / --from-tts / --from-transcript", exit_code=2)

    # 卡拉OK判定必须先于建卡:挂字要在必并/合规校验之前做(预算口径=显示字形)
    karaoke = bool(a.karaoke)
    kar_note = ""
    if karaoke and not a.from_wordline:
        return emit(False, "KARAOKE_NEEDS_WORDLINE", "--karaoke 仅支持 --from-wordline", exit_code=2)
    if karaoke:
        ok, why = _karaoke_ready(wl)
        if not ok:
            if a.allow_degraded:
                karaoke = False
                kar_note = f"卡拉OK 已降级为普通字幕({why})"
            else:
                return emit(False, "KARAOKE_NEEDS_WORD_TS",
                            why + ";用 pkg 后端重建 wordline,或加 --allow-degraded 降级为普通字幕",
                            exit_code=2)

    if a.override:
        # Agent 复核修正(ADR-0020):时间真相源仍是 wordline,只动文本分组
        if not a.from_wordline:
            return emit(False, "OVERRIDE_NEEDS_WORDLINE",
                        "--override 仅支持 --from-wordline(需要字级时间真相源)", exit_code=2)
        try:
            ov = json.loads(Path(a.override).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return emit(False, "BAD_OVERRIDE_FILE", f"读取 override 失败:{exc}", exit_code=2)
        if not isinstance(ov, dict) or not isinstance(ov.get("cards"), list):
            return emit(False, "BAD_OVERRIDE_FILE",
                        'override 结构应为 {"cards":[{"span":[a,b] | "text":"…" | '
                        '"textPrefix":"…","textSuffix":"…","note":"..."}]}', exit_code=2)
        try:
            events, meta = events_from_override(wl, ov, max_chars, karaoke=karaoke,
                                                cps_max=preset.get("cpsMax"))
        except ValueError as exc:
            return emit(False, "BAD_OVERRIDE", str(exc), exit_code=2)
        if karaoke and not meta.get("karaokeAttached"):
            karaoke = False
            kar_note = "卡拉OK 降级:字级时间未覆盖任何字幕卡"
    elif a.no_optimize:
        events = build_events([{"start_s": c["startMs"] / 1000, "end_s": c["endMs"] / 1000,
                                "text": c["ch"]} for c in wl.get("chars", [])],
                              max_chars, optimize=False)
        meta = {"degraded": True, "degradeReasons": ["--no-optimize 关闭轻改写"], "violations": [],
                "candidates": [], "ambiguous": 0}
        if karaoke and not _apply_karaoke_chars(events, wl):
            karaoke = False
            kar_note = "卡拉OK 降级:字级时间未覆盖任何字幕卡"
    else:
        events, meta = events_from_wordline(wl, max_chars, terms=terms, top=a.top,
                                            mode=a.segment, karaoke=karaoke,
                                            cps_max=preset.get("cpsMax"))
        if karaoke and not meta.get("karaokeAttached"):
            karaoke = False
            kar_note = "卡拉OK 降级:字级时间未覆盖任何字幕卡"
    if kar_note:
        meta["degradeReasons"] = list(meta.get("degradeReasons") or []) + [kar_note]
    if not a.no_snap:
        meta["snapped"] = snap_events_to_frames(events, a.fps)
        # B3(BUGREPORT-20260913):snap 的 start floor / end ceil 会把相邻卡推回
        # 30–40ms 重叠(rs_sync 容差 1 帧 → 判 FAIL)。锚点释放余量内再收一次间距;
        # 余量耗尽仍重叠的留给 rs_sync 帧容差(对齐精度优先,Hard Rule 20)。
        meta["postSnapGaps"] = _enforce_gaps(events, a.fps)
    # B5(BUGREPORT-20260913)落盘防御:cards.json 与 ass 由**同一份 events** 写出,
    # 时间必须单调、正时长、无重叠;任何上游调整(必并/延长/间距/帧对齐)后在此兜底,
    # 不得把倒挂/漂移的时间写进审计件。
    _finalize_events(events, a.fps or 30.0)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    write_srt(events, out / "master.srt")
    write_ass(events, out / "subtitles.ass", style, ratio, canvas, karaoke=karaoke)

    if meta["candidates"]:
        (out / "segments_candidates.json").write_text(
            json.dumps(meta["candidates"], ensure_ascii=False, indent=1), encoding="utf-8")

    # Agent 复核输入:卡 ↔ charSpan(wordline 内容字全局索引);改 span 后用 --override 回灌
    cards_json = [{"i": i, "text": e["text"], "charSpan": e.get("charSpan"),
                   "startMs": int(round(e["start"] * 1000)), "endMs": int(round(e["end"] * 1000))}
                  for i, e in enumerate(events)]
    (out / "cards.json").write_text(
        json.dumps({"maxChars": max_chars, "ratio": ratio,
                    "finalTimes": True,   # B5:时间 = 最终 ass 同源快照(必并/延长/间距/帧对齐之后)
                    "cards": cards_json},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    msg = f"{len(events)} 条字幕事件(卡切分 {a.segment},每卡 ≤{max_chars} 字)"
    if meta.get("overrideApplied"):
        msg += f";override 重建 {meta['overrideCards']} 卡"
    if meta["ambiguous"]:
        msg += f";{meta['ambiguous']} 句切分歧义(见 segments_candidates.json)"
    if meta["violations"]:
        msg += f";⚠ {len(meta['violations'])} 项约束违规"
    if meta["degraded"]:
        msg += ";⚠ 降级模式:" + ";".join(meta["degradeReasons"][:2])

    return emit(True, "SUBTITLE_OK", msg,
                {"srt": str(out / "master.srt"), "ass": str(out / "subtitles.ass"),
                 "cards": str(out / "cards.json"),
                 "style": style, "ratio": ratio, "platform": a.platform,
                 "canvas": canvas, "count": len(events),
                 "overrideApplied": bool(meta.get("overrideApplied")),
                 "maxChars": max_chars, "ambiguous": meta["ambiguous"],
                 "violations": meta["violations"][:20], "degraded": meta["degraded"],
                 "charTimingEstimated": bool(meta.get("charTimingEstimated")),
                 "degradeReasons": meta["degradeReasons"]})


def _wordline(entries: list[dict], source: str, reason: str) -> dict:
    import rs_align
    return rs_align.build_wordline(entries, source, degraded=reason)


if __name__ == "__main__":
    sys.exit(main())
