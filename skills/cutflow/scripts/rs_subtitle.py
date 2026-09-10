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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit  # noqa: E402
import segmentation  # noqa: E402
import textopt  # noqa: E402

MAX_CHARS = dict(segmentation.MAX_CHARS)          # 9x16=12 / 16x9=22(竖屏已从 16 下调)
TAIL_STOP = "。!?…;:!?"
MID_STOP = ",,、;:? "
TAIL_FUNC = "的了着地吧呢啊吗嘛"
BAD_END = "我你他她它们这那就都也很不有在和与跟对往朝从被把将"

FRAME_MS = 1000 / 30.0                            # 卡间距 ≥2 帧
MIN_GAP_FRAMES = 2

STYLES = {
    "talkshow-bold": {
        "font": "Microsoft YaHei", "size": {"9x16": 78, "16x9": 64},
        "primary": "&H00FFFFFF", "outline": "&H00101010", "back": "&H80000000",
        "outline_w": 3, "shadow": 1, "margin_v": {"9x16": 500, "16x9": 120},
        "align": 2, "bold": 1,
    },
    "tutorial-clean": {
        "font": "Microsoft YaHei", "size": {"9x16": 62, "16x9": 56},
        "primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H60000000",
        "outline_w": 2, "shadow": 0, "margin_v": {"9x16": 520, "16x9": 90},
        "align": 2, "bold": 0, "border_style": 3,
    },
    "subtitle-white": {
        "font": "Microsoft YaHei", "size": {"9x16": 68, "16x9": 58},
        "primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H00000000",
        "outline_w": 2, "shadow": 1, "margin_v": {"9x16": 320, "16x9": 110},
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


def events_from_wordline(wl: dict, max_chars: int, *, terms=(), top: int = 3,
                         mode: str = "dp") -> tuple[list[dict], dict]:
    """Wordline → 字幕事件。卡时间 = 首字/末字时间戳聚合(align.md §4)。"""
    events: list[dict] = []
    candidates: list[dict] = []
    for text, idxmap, gaps in _sentence_slices(wl):
        if not text.strip():
            continue
        if mode == "length":
            cards = [{"i": i, "text": c, "start": None, "end": None}
                     for i, c in enumerate(textopt.card_split(text, max_chars, mode="length"))]
            plan = {"cards": cards, "violations": [], "ambiguous": False}
        else:
            plan = segmentation.segment(text, max_chars, gaps=gaps, index_map=idxmap,
                                        char_times=wl.get("chars"), terms=terms, top=top)
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
            events.append(ev)

    events.sort(key=lambda e: e["start"])
    events, merged_short = _merge_short(events, max_chars)
    extended = _extend_short(events)
    _enforce_gaps(events)
    # 约束校验必须在**可读性调整之后**做,否则报的是已经不存在的问题
    final_cards = []
    for i, e in enumerate(events):
        n_chars = len(e["text"].replace(" ", ""))
        dur_ms = int(round((e["end"] - e["start"]) * 1000))
        final_cards.append({"i": i, "text": e["text"], "chars": n_chars,
                            "startMs": int(e["start"] * 1000), "endMs": int(e["end"] * 1000),
                            "durMs": dur_ms,
                            "cps": round(n_chars / (dur_ms / 1000.0), 2) if dur_ms else 0.0})
    violations = segmentation.check_constraints(final_cards, max_chars, segmentation.CPS_MAX["9x16"])
    meta = {"degraded": bool(wl.get("degraded")) or any(e.get("degraded") for e in events),
            "degradeReasons": wl.get("degradeReasons", []),
            "violations": violations, "candidates": candidates,
            "mergedShort": merged_short, "extendedShort": extended,
            "ambiguous": sum(1 for c in candidates if c["ambiguous"])}
    return events, meta


MIN_DUR_S = 0.83


def _join(a: str, b: str) -> str:
    sep = " " if (a and b and a[-1].isascii() and a[-1].isalnum()
                  and b[0].isascii() and b[0].isalnum()) else ""
    return a.rstrip() + sep + b.lstrip()


def _merge_short(events: list[dict], max_chars: int, min_dur: float = 0.8) -> tuple[list[dict], int]:
    """<0.8s 必并(rules/subtitles.md §4.4):与相邻卡合并,前提是合并后不超字数上限。

    读得完是节奏问题,但**说出来的时间是对的**——所以宁可合卡,不去改时间。
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
                merged += 1
                continue
        out.append(dict(e))
    return out, merged


def _extend_short(events: list[dict], min_dur: float = MIN_DUR_S) -> int:
    """仍有余量时,把过短卡的后沿延到最短时长——**绝不越过下一卡的起点**。"""
    min_gap = MIN_GAP_FRAMES * FRAME_MS / 1000.0
    n = 0
    for i, e in enumerate(events):
        if e["end"] - e["start"] >= min_dur:
            continue
        ceiling = (events[i + 1]["start"] - min_gap) if i + 1 < len(events) else None
        want = e["start"] + min_dur
        if ceiling is not None:
            want = min(want, ceiling)
        if want > e["end"]:
            e["end"] = want
            n += 1
    return n


def _enforce_gaps(events: list[dict]) -> None:
    """卡间距 ≥2 帧;在满足间距的前提下尽量保住最短时长(收早优先,不推迟后卡起点)。"""
    min_gap = MIN_GAP_FRAMES * FRAME_MS / 1000.0
    for a, b in zip(events, events[1:]):
        if b["start"] - a["end"] >= min_gap:
            continue
        floor = a["start"] + 0.2
        a["end"] = max(floor, b["start"] - min_gap)


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


def write_ass(events: list[dict], path: Path, style_name: str, ratio: str, canvas: str) -> None:
    st = STYLES.get(style_name) or STYLES["subtitle-white"]
    w, h = canvas.split("x")
    play_res = f"PlayResX: {w}\nPlayResY: {h}"
    margin_r, margin_l = 60, 60
    header = f"""[Script Info]
Title: CutFlow subtitles
ScriptType: v4.00+
WrapStyle: 0
{play_res}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{st['font']},{st['size'][ratio]},{st['primary']},&H000000FF,{st['outline']},{st['back']},{st['bold']},0,0,0,100,100,0,0,{st.get('border_style', 1)},{st['outline_w']},{st['shadow']},{st['align']},{margin_l},{margin_r},{st['margin_v'][ratio]},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = "".join(f"Dialogue: 0,{_ts_ass(e['start'])},{_ts_ass(e['end'])},Main,,0,0,0,,{e['text']}\n"
                    for e in events)
    path.write_text(header + lines, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-wordline")
    ap.add_argument("--from-tts")
    ap.add_argument("--from-transcript")
    ap.add_argument("--style", default="subtitle-white")
    ap.add_argument("--ratio", default="9x16", choices=["9x16", "16x9"])
    ap.add_argument("--canvas", default=None, help="如 1080x1920,默认按比例推断")
    ap.add_argument("--out", required=True)
    ap.add_argument("--terms", default="", help="专有名词表(逗号分隔),断句禁切用")
    ap.add_argument("--top", type=int, default=3, help="输出前 N 个切分候选")
    ap.add_argument("--segment", default="dp", choices=["dp", "length"])
    ap.add_argument("--no-optimize", action="store_true", help="关闭轻改写(默认开启)")
    a = ap.parse_args()

    canvas = a.canvas or {"9x16": "1080x1920", "16x9": "1920x1080"}[a.ratio]
    terms = tuple(t.strip() for t in a.terms.split(",") if t.strip())
    max_chars = MAX_CHARS[a.ratio]

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

    if a.no_optimize:
        events = build_events([{"start_s": c["startMs"] / 1000, "end_s": c["endMs"] / 1000,
                                "text": c["ch"]} for c in wl.get("chars", [])],
                              max_chars, optimize=False)
        meta = {"degraded": True, "degradeReasons": ["--no-optimize 关闭轻改写"], "violations": [],
                "candidates": [], "ambiguous": 0}
    else:
        events, meta = events_from_wordline(wl, max_chars, terms=terms, top=a.top,
                                            mode=a.segment)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    write_srt(events, out / "master.srt")
    write_ass(events, out / "subtitles.ass", a.style, a.ratio, canvas)

    if meta["candidates"]:
        (out / "segments_candidates.json").write_text(
            json.dumps(meta["candidates"], ensure_ascii=False, indent=1), encoding="utf-8")

    msg = f"{len(events)} 条字幕事件(卡切分 {a.segment},每卡 ≤{max_chars} 字)"
    if meta["ambiguous"]:
        msg += f";{meta['ambiguous']} 句切分歧义(见 segments_candidates.json)"
    if meta["violations"]:
        msg += f";⚠ {len(meta['violations'])} 项约束违规"
    if meta["degraded"]:
        msg += ";⚠ 降级模式:" + ";".join(meta["degradeReasons"][:2])

    return emit(True, "SUBTITLE_OK", msg,
                {"srt": str(out / "master.srt"), "ass": str(out / "subtitles.ass"),
                 "style": a.style, "canvas": canvas, "count": len(events),
                 "maxChars": max_chars, "ambiguous": meta["ambiguous"],
                 "violations": meta["violations"][:20], "degraded": meta["degraded"],
                 "degradeReasons": meta["degradeReasons"]})


def _wordline(entries: list[dict], source: str, reason: str) -> dict:
    import rs_align
    return rs_align.build_wordline(entries, source, degraded=reason)


if __name__ == "__main__":
    sys.exit(main())
