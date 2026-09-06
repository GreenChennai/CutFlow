"""字幕:时间轴来源(transcript/tts manifest)→ SRT + ASS(智能断行 + 风格模板)。

用法:python rs_subtitle.py --from-tts <manifest.json> --style talkshow-bold --ratio 9x16 --out <目录>
      python rs_subtitle.py --from-transcript <transcript_raw.json> --style tutorial-clean --ratio 16x9 --out <目录>
断行:评分算法移植 kbcut breakLine(标点+100/空格+90/句尾虚词+50/ASCII 切断-200/每填一字-4)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import die, emit  # noqa: E402

MAX_CHARS = {"9x16": 16, "16x9": 22}
TAIL_STOP = "。!?…;:!?"
MID_STOP = ",,、;:? "
# 句尾虚词:可作断点收尾(轻微加分)
TAIL_FUNC = "的了着地吧呢啊吗嘛"
# 坏收尾:代词/副词/连词收尾会把"我们"这类词拆断(重罚)
BAD_END = "我你他她它们这那就都也很不有在和与跟对往朝从被把将"

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
    s -= 4 * pos  # 越靠后越均匀
    return s


def break_line(line: str, max_chars: int) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    first = line[:max_chars]
    best, best_s = None, None
    for p in range(3, max_chars - 1):
        # ASCII 字母数字连写(英文/数字/版本号)视为不可切断
        if line[p - 1].isascii() and line[p - 1].isalnum() and line[p].isascii() and line[p].isalnum():
            continue
        s = score_break(line, p)
        if best_s is None or s > best_s:
            best, best_s = p, s
    if best is None:  # 无合法断点 → 硬切
        best = max_chars
    return [line[:best].rstrip()] + break_line(line[best:].lstrip(), max_chars)


def _ts_srt(sec: float) -> str:
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    s, ms = int(sec % 60), int((sec % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ts_ass(sec: float) -> str:
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_events(entries: list[dict], max_chars: int, per_span_s: float = 4.0) -> list[dict]:
    """entries: [{start_s, end_s, text}] → 断行后的事件列表。单条超时自动均分。"""
    events = []
    for e in entries:
        dur = e["end_s"] - e["start_s"]
        lines = break_line(e["text"].replace("\n", " ").strip(), max_chars)
        if dur > per_span_s and len(lines) > 1:
            span = dur / len(lines)
            for i, ln in enumerate(lines):
                events.append({"start": e["start_s"] + i * span,
                               "end": e["start_s"] + (i + 1) * span, "text": ln})
        else:
            per = dur / len(lines)
            for i, ln in enumerate(lines):
                events.append({"start": e["start_s"] + i * per,
                               "end": e["start_s"] + (i + 1) * per, "text": ln})
    return events


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
    ap.add_argument("--from-tts")
    ap.add_argument("--from-transcript")
    ap.add_argument("--style", default="subtitle-white")
    ap.add_argument("--ratio", default="9x16", choices=["9x16", "16x9"])
    ap.add_argument("--canvas", default=None, help="如 1080x1920,默认按比例推断")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    canvas = a.canvas or {"9x16": "1080x1920", "16x9": "1920x1080"}[a.ratio]
    entries: list[dict] = []
    if a.from_tts:
        man = json.loads(Path(a.from_tts).read_text(encoding="utf-8"))
        entries = [{"start_s": s["start_s"], "end_s": s["end_s"], "text": s["text"]}
                   for s in man["sentences"]]
    elif a.from_transcript:
        tr = json.loads(Path(a.from_transcript).read_text(encoding="utf-8"))
        entries = [{"start_s": s["start"], "end_s": s["end"], "text": s["text"]}
                   for s in tr["segments"]]
    else:
        return emit(False, "NO_SOURCE", "需要 --from-tts 或 --from-transcript", exit_code=2)

    events = build_events(entries, MAX_CHARS[a.ratio])
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    write_srt(events, out / "master.srt")
    write_ass(events, out / "subtitles.ass", a.style, a.ratio, canvas)
    return emit(True, "SUBTITLE_OK", f"{len(events)} 条字幕事件",
                {"srt": str(out / "master.srt"), "ass": str(out / "subtitles.ass"),
                 "style": a.style, "canvas": canvas})


if __name__ == "__main__":
    sys.exit(main())
