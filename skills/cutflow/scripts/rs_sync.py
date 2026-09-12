"""S8 三对齐自检:字幕 ↔ Wordline ↔ 成片(rules/align.md §6、rules/subtitles.md §8)。

用法:
  rs_sync.py --wordline 05_ir/wordline.json --ass 06_output/subtitles.ass --out 06_output [--video 成片.mp4]

检查项与通过线:
  字幕 ↔ 音频/Wordline 偏移   中位数 ≤40ms,95 分位 ≤80ms
  卡片 ↔ 卡片 时间重叠        0
  单卡时长 [0.83s, 7s]、CPS ≤9
  成片总时长 vs Wordline        ±0.5s(给了 --video 时用 ffprobe 实测)

把「人工对轴」变成「阈值告警 + 一键修正」:失败项给出建议平移量。
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit, p95  # noqa: E402

MEDIAN_MAX, P95_MAX = 40, 80          # ms(起点偏移)
END_MEDIAN_MAX, END_P95_MAX = 60, 120  # ms(终点偏移;允许可读性延长,故放宽)
END_TOL_MS = 25                       # 终点早于末字超过此值 → 切掉语音(硬失败)
END_MAX_RELEASE_MS = 350              # 终点晚于末字超过此值 → 滞留过久(硬失败)
DUR_MIN, DUR_MAX = 0.83, 7.0          # s
CPS_MAX = 9.0
LEAD_MS = 20                          # 卡片时间 = 首字 startMs - 20ms(align.md §4)
STRIP = "。，、；：,;:…!?！？ \u3000「」“”\"'()（）"
_OVERRIDE = re.compile(r"\{[^}]*\}")  # ASS override 标签(如 {\kf28}、{\c&H..&})


def ass_time(s: str) -> float:
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def parse_ass(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        if len(parts) < 10:
            continue
        # dev-jj2815 实测:卡拉OK字幕行文是 {\kf28}店{\kf14}群… 形式,不剥
        # override 标签会与 Wordline 纯文本对不上(84/84 unmatched → SYNC_FAIL)。
        text = _OVERRIDE.sub("", parts[9]).replace("\\N", " ").strip()
        out.append({"start": ass_time(parts[1]), "end": ass_time(parts[2]), "text": text})
    return out


def norm(s: str) -> str:
    return "".join(ch for ch in s if ch not in STRIP)


def wordline_index(wl: dict) -> tuple[str, list[int]]:
    """去掉标点后的纯文本 + 每个下标对应的 chars 下标。"""
    chars = wl.get("chars", [])
    buf, idx = [], []
    for c in chars:
        if c["ch"] in STRIP:
            continue
        buf.append(c["ch"])
        idx.append(c["i"])
    return "".join(buf), idx


def check_offsets(events: list[dict], wl: dict) -> list[dict]:
    """按文本顺序匹配每条字幕卡到 wordline,计算偏移(ms)。"""
    text, cmap = wordline_index(wl)
    chars = wl.get("chars", [])
    cursor, rows = 0, []
    for e in events:
        t = norm(e["text"])
        if not t:
            continue
        k = text.find(t, cursor)
        if k < 0:
            k = text.find(t)
        if k < 0:
            rows.append({"event": e["text"][:14], "matched": False})
            continue
        first = cmap[k]
        last = cmap[min(k + len(t) - 1, len(cmap) - 1)]
        expect = (int(chars[first]["startMs"]) - LEAD_MS) / 1000.0
        expect_end = (int(chars[last]["endMs"]) + LEAD_MS) / 1000.0
        rows.append({"event": e["text"][:14], "matched": True,
                     "actual": round(e["start"], 3), "expected": round(max(0.0, expect), 3),
                     "offsetMs": round((e["start"] - max(0.0, expect)) * 1000, 1),
                     "endOffsetMs": round((e["end"] - expect_end) * 1000, 1),
                     "durMs": round((e["end"] - e["start"]) * 1000, 1),
                     "chars": len(t)})
        cursor = k + len(t)
    return rows


def summarize(rows: list[dict], events: list[dict], legacy_end: bool = False) -> dict:
    """`legacy_end=True`:跳过终点门禁(只告警)—— 给"终点本来就偏"的历史工程过渡用。"""
    offs = [abs(r["offsetMs"]) for r in rows if r.get("matched")]
    unmatched = [r for r in rows if not r.get("matched")]
    median = statistics.median(offs) if offs else 0.0
    p95_ms = p95(offs)
    bias = statistics.median([r["offsetMs"] for r in rows if r.get("matched")]) if offs else 0.0

    # 终点偏移:只盯两个真实故障——「比末字早退」(切掉语音)与「滞留过久」(字幕赖着不走)
    end_offs = [r["endOffsetMs"] for r in rows if r.get("matched") and "endOffsetMs" in r]
    end_abs = [abs(x) for x in end_offs]
    median_end = statistics.median(end_abs) if end_abs else 0.0
    p95_end = p95(end_abs)
    bias_end = statistics.median(end_offs) if end_offs else 0.0
    early_end = [f"{r['event']} {r['endOffsetMs']:.0f}ms"
                 for r in rows if r.get("matched") and r.get("endOffsetMs", 0) < -END_TOL_MS]
    over_end = [f"{r['event']} +{r['endOffsetMs']:.0f}ms"
                for r in rows if r.get("matched") and r.get("endOffsetMs", 0) > END_MAX_RELEASE_MS]

    overlaps, bad_dur, short_dur, bad_cps = 0, [], [], []
    for a, b in zip(events, events[1:]):
        if b["start"] < a["end"] - 1e-3:
            overlaps += 1
    for r in rows:
        if not r.get("matched"):
            continue
        dur = r["durMs"] / 1000.0
        if dur > DUR_MAX + 1e-6:
            bad_dur.append(f"{r['event']} {dur:.2f}s")          # >7s:必须切,硬失败
        elif dur < DUR_MIN - 1e-6:
            short_dur.append(f"{r['event']} {dur:.2f}s")        # <0.83s:软告警(优先"必并")
        if r["durMs"] > 0:
            cps = r["chars"] / (r["durMs"] / 1000.0)
            if cps > CPS_MAX + 1e-6:
                bad_cps.append(f"{r['event']} {cps:.1f}")
    return {"chars": len(offs), "medianMs": round(median, 1), "p95Ms": round(p95_ms, 1),
            "biasMs": round(bias, 1), "unmatched": unmatched, "overlaps": overlaps,
            "medianEndMs": round(median_end, 1), "p95EndMs": round(p95_end, 1),
            "biasEndMs": round(bias_end, 1),
            "earlyEnd": early_end, "overLongEnd": over_end,
            "badDuration": bad_dur, "shortDuration": short_dur, "badCps": bad_cps,
            "endCheckSkipped": bool(legacy_end),
            "pass": bool(offs and median <= MEDIAN_MAX and p95_ms <= P95_MAX
                         and (legacy_end or (median_end <= END_MEDIAN_MAX
                                             and p95_end <= END_P95_MAX
                                             and not early_end and not over_end))
                         and not overlaps
                         and not unmatched and not bad_dur and not bad_cps)}


def check_card_overlap(events: list[dict], ir: dict | None,
                       min_overlap_ms: int = 1) -> list[dict]:
    """字幕卡 ↔ artboard 动画卡 时间窗重叠(动画压字幕)。

    只看**卡片类**片段(artboard 产物):src 含 `artboard`、或 clip 标了 `card`/名字以 `card_` 开头。
    普通素材轨(口播/绿幕)本就该有字幕压在上面,不算重叠。
    """
    if not ir:
        return []
    windows = []
    for tr in ir.get("tracks", []):
        if tr.get("kind") != "video":
            continue
        for cl in tr.get("clips", []):
            src = str(cl.get("src", ""))
            is_card = ("artboard" in src.replace("\\", "/")
                       or bool(cl.get("card")) or Path(src).name.startswith("card_")
                       or str(tr.get("name", "")).startswith("card"))
            if not is_card:
                continue
            start = int(cl.get("startMs", 0))
            windows.append({"name": Path(src).name or "card",
                            "startMs": start,
                            "endMs": start + int(cl.get("durationMs", 0))})
    out = []
    for e in events:
        es, ee = int(round(e["start"] * 1000)), int(round(e["end"] * 1000))
        for w in windows:
            ov = min(ee, w["endMs"]) - max(es, w["startMs"])
            if ov >= min_overlap_ms:
                out.append({"event": e["text"][:14], "card": w["name"], "overlapMs": ov,
                            "cardWindow": [w["startMs"], w["endMs"]],
                            "eventWindow": [es, ee]})
    return out


def write_report(res: dict, path: Path, video_check: dict | None) -> None:
    lines = ["# 三对齐自检报告(sync_report)", "",
             "| 检查 | 结果 | 通过线 | 判定 |", "|---|---|---|---|",
             f"| 字幕↔Wordline 偏移中位数 | {res['medianMs']} ms | ≤{MEDIAN_MAX} ms | "
             f"{'✓' if res['medianMs'] <= MEDIAN_MAX else '✗'} |",
             f"| 偏移 95 分位 | {res['p95Ms']} ms | ≤{P95_MAX} ms | "
             f"{'✓' if res['p95Ms'] <= P95_MAX else '✗'} |",
             f"| **终点偏移中位数** | {res.get('medianEndMs', 0)} ms | ≤{END_MEDIAN_MAX} ms | "
             f"{'跳过(--legacy-end)' if res.get('endCheckSkipped') else ('✓' if res.get('medianEndMs', 0) <= END_MEDIAN_MAX else '✗')} |",
             f"| **终点偏移 95 分位** | {res.get('p95EndMs', 0)} ms | ≤{END_P95_MAX} ms | "
             f"{'跳过' if res.get('endCheckSkipped') else ('✓' if res.get('p95EndMs', 0) <= END_P95_MAX else '✗')} |",
             f"| **字幕早退(切掉语音)** | {len(res.get('earlyEnd', []))} | 0 | "
             f"{'跳过' if res.get('endCheckSkipped') else ('✓' if not res.get('earlyEnd') else '✗')} |",
             f"| **字幕滞留过久** | {len(res.get('overLongEnd', []))} | 0 | "
             f"{'跳过' if res.get('endCheckSkipped') else ('✓' if not res.get('overLongEnd') else '✗')} |",
             f"| 系统偏差(可一键平移) | {res['biasMs']} ms | — | — |",
             f"| 字幕↔动画卡重叠 | {len(res.get('cardOverlaps', []))} | 0(建议) | "
             f"{'✓' if not res.get('cardOverlaps') else '⚠'} |",
             f"| 卡片时间重叠 | {res['overlaps']} | 0 | {'✓' if not res['overlaps'] else '✗'} |",
             f"| 未匹配字幕 | {len(res['unmatched'])} | 0 | {'✓' if not res['unmatched'] else '✗'} |",
             f"| 单卡时长 >7s | {len(res['badDuration'])} | 0 | {'✓' if not res['badDuration'] else '✗'} |",
             f"| 单卡时长 <0.83s(软告警) | {len(res.get('shortDuration', []))} | 0 | "
             f"{'✓' if not res.get('shortDuration') else '⚠'} |",
             f"| CPS 超限 | {len(res['badCps'])} | 0 | {'✓' if not res['badCps'] else '✗'} |", ""]
    if abs(res["biasMs"]) > MEDIAN_MAX:
        lines += [f"**建议修正**:全部字幕整体平移 `{-res['biasMs']:.0f} ms`"
                  f"(或检查 Wordline 与成片是否同源)。", ""]
    if video_check:
        lines += ["## 成片总时长", "",
                  f"- ffprobe 实测 {video_check['actual']:.2f}s / Wordline 推算 "
                  f"{video_check['expected']:.2f}s(差 {video_check['diff']:+.2f}s)",
                  f"- 判定:{'✓' if video_check['pass'] else '✗'} (±0.5s)", ""]
    if res.get("earlyEnd"):
        lines += ["## 字幕早退明细(字幕在末字说完前消失 → 会切掉语音)", ""] + \
                 [f"- {x}" for x in res["earlyEnd"][:20]] + [""]
    if res.get("overLongEnd"):
        lines += ["## 字幕滞留过久明细(超过末字释放余量)", ""] + \
                 [f"- {x}" for x in res["overLongEnd"][:20]] + [""]
    if res["badCps"]:
        lines += ["## CPS 超限明细", ""] + [f"- {x}" for x in res["badCps"][:20]] + [""]
    if res["badDuration"]:
        lines += ["## 时长过长明细(>7s,必须切)", ""] + [f"- {x}" for x in res["badDuration"][:20]] + [""]
    if res.get("shortDuration"):
        lines += ["## 时长过短明细(<0.83s,建议按「必并」合卡)", ""] + \
                 [f"- {x}" for x in res["shortDuration"][:20]] + [""]
    if res.get("cardOverlaps"):
        lines += ["## 字幕 ↔ 动画卡时间窗重叠(动画压字幕)", ""] + \
                 [f"- {c['event']} ↔ {c['card']} 重叠 {c['overlapMs']}ms" for c in res["cardOverlaps"][:20]] + [""]
    if res["unmatched"]:
        lines += ["## 未匹配字幕(文本与 Wordline 不一致,可能是校对未重聚合)", ""] + \
                 [f"- {r['event']}" for r in res["unmatched"][:20]] + [""]
    lines.append(f"**总判定**:**{'通过' if res['pass'] else '未通过'}**")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", nargs="?")
    ap.add_argument("--wordline", required=True)
    ap.add_argument("--ass", required=True)
    ap.add_argument("--out", default="06_output")
    ap.add_argument("--ir", default=None,
                    help="05_ir/project.json:检查字幕与 artboard 动画卡时间窗是否重叠")
    ap.add_argument("--strict-cards", dest="strict_cards", action="store_true",
                    help="把「字幕被动画卡压住」也算未通过(默认只告警)")
    ap.add_argument("--legacy-end", dest="legacy_end", action="store_true",
                    help="跳过终点门禁(只告警):给终点本来就偏的历史工程过渡用")
    a = ap.parse_args()

    wl = json.loads(Path(a.wordline).read_text(encoding="utf-8"))
    ass = Path(a.ass)
    if not ass.is_file():
        return emit(False, "NO_ASS", f"ASS 不存在:{ass}", exit_code=2)
    events = parse_ass(ass)
    if not events:
        return emit(False, "NO_EVENTS", "ASS 中没有 Dialogue 事件", exit_code=2)

    rows = check_offsets(events, wl)
    res = summarize(rows, events, legacy_end=a.legacy_end)
    res["cardOverlaps"] = []
    if a.ir:
        try:
            res["cardOverlaps"] = check_card_overlap(
                events, json.loads(Path(a.ir).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            res["cardOverlapError"] = f"IR 读取失败:{exc}"
        if a.strict_cards and res["cardOverlaps"]:
            res["pass"] = False

    video_check = None
    if a.video:
        from rs_common import media_duration_s
        try:
            actual = media_duration_s(a.video)
            expected = (wl.get("finalDurationMs") or wl.get("srcDurationMs") or 0) / 1000.0
            diff = actual - expected
            video_check = {"actual": round(actual, 3), "expected": round(expected, 3),
                           "diff": round(diff, 3), "pass": abs(diff) <= 0.5}
        except SystemExit:
            video_check = None

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    write_report(res, outdir / "sync_report.md", video_check)
    (outdir / "sync_rows.json").write_text(json.dumps({"rows": rows, "summary": res,
                                                      "video": video_check},
                                                      ensure_ascii=False, indent=1), encoding="utf-8")
    msg = (f"偏移中位数 {res['medianMs']}ms / 95 分位 {res['p95Ms']}ms;"
           f"{'通过' if res['pass'] else '未通过'}")
    return emit(res["pass"], "SYNC_OK" if res["pass"] else "SYNC_FAIL", msg,
                {"report": str(outdir / "sync_report.md"), **res, "video": video_check},
                exit_code=0 if res["pass"] else 4)


if __name__ == "__main__":
    sys.exit(main())
