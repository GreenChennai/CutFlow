"""S2 粗剪:三路检测器 → CutList(可读可改可审查的决策表,ADR-0012)。

用法:
  rs_cut.py 05_ir/wordline.json --detect all --out 04_cut
  rs_cut.py 05_ir/wordline.json --review-pack 04_cut/cutlist.json
  rs_cut.py --apply 04_cut/cutlist.final.json

设计铁律:**宁可漏删,不可错删。**
  conf ≥ 0.90 → remove(仍须过 guard 三重校验)
  0.60–0.90  → review(进审查包)
  < 0.60     → keep(不动)
guard = 切点在静音区 / 不切断字内音素 / 后留 ≥60ms;任一不过 → 降级 review。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit  # noqa: E402

DETECTOR_VERSION = "cutflow-1.0"
REASONS = {"silence", "breath", "filler", "false_start", "retake",
           "stumble", "repetition", "off_topic", "manual"}
CONF_REMOVE, CONF_REVIEW = 0.90, 0.60
SILENCE_MIN_MS = 600          # 静音判定:VAD 间隔 ≥600ms
TAIL_KEEP_MS = 60             # 切点后释放余量(Descript "Avoid harsh cuts")
NEAR_SILENCE_MS = 120         # 切点前后多远内有静音算"落在静音区"
RHETORIC_ORIG_MS, RHETORIC_AFTER_MS = 700, 200

FILLERS: dict[str, float] = {
    "嗯": 0.72, "呃": 0.72, "啊": 0.70, "诶": 0.70, "哦": 0.70, "唉": 0.70,
    "那个": 0.82, "这个": 0.80, "就是说": 0.86, "然后就是": 0.86, "就然后": 0.80,
}
REPEAT_WORDS = ("我", "你", "他", "她", "我们", "你们", "那个", "这个", "然后", "就是")


# ---------------------------------------------------------------- 工具

def _find_all(hay: str, needle: str) -> list[tuple[int, int]]:
    out, start = [], 0
    while True:
        k = hay.find(needle, start)
        if k < 0:
            return out
        out.append((k, k + len(needle)))
        start = k + 1


def _text_and_map(wl: dict) -> tuple[str, list[int]]:
    """把 wordline.chars 拼成字符串,并给出 字符串下标 → chars 下标 的映射。"""
    chars = wl.get("chars", [])
    text = "".join(c["ch"] for c in chars)
    return text, list(range(len(chars)))


def _span_ms(chars: list[dict], a: int, b: int) -> tuple[int, int]:
    a = max(0, min(a, len(chars) - 1))
    b = max(a, min(b, len(chars)))
    if b <= a:
        b = a + 1
    return int(chars[a]["startMs"]), int(chars[b - 1]["endMs"])


def _gap_windows(chars: list[dict]) -> list[dict]:
    """相邻字之间的空隙(用于静音检测与 guard)。"""
    out = []
    for i, (a, b) in enumerate(zip(chars, chars[1:])):
        ms = int(b["startMs"]) - int(a["endMs"])
        if ms > 0:
            out.append({"after": i, "startMs": int(a["endMs"]), "endMs": int(b["startMs"]), "ms": ms})
    return out


# ---------------------------------------------------------------- guard

def guard(cut: dict, chars: list[dict], gaps: list[dict]) -> dict:
    """三重校验(rules/roughcut.md §5)。返回 {inSilence,outSilence,wordClipped,tailKeepMs,ok}。"""
    in_ms, out_ms = int(cut["inMs"]), int(cut["outMs"])
    in_sil = any(abs(in_ms - g["startMs"]) <= NEAR_SILENCE_MS or
                 abs(in_ms - g["endMs"]) <= NEAR_SILENCE_MS or
                 (g["startMs"] - NEAR_SILENCE_MS <= in_ms <= g["endMs"] + NEAR_SILENCE_MS)
                 for g in gaps) or _is_edge(chars, in_ms)
    out_sil = any((g["startMs"] - NEAR_SILENCE_MS <= out_ms <= g["endMs"] + NEAR_SILENCE_MS)
                  for g in gaps) or _is_edge(chars, out_ms)

    def clipped(t: int) -> bool:
        return any(int(c["startMs"]) + 1 < t < int(c["endMs"]) - 1 for c in chars)

    word_clipped = clipped(in_ms) or clipped(out_ms)
    tail = 10 ** 9
    for c in chars:
        if int(c["startMs"]) >= out_ms:
            tail = int(c["startMs"]) - out_ms
            break
    ok = bool(in_sil and out_sil and not word_clipped and tail >= TAIL_KEEP_MS)
    return {"inSilence": bool(in_sil), "outSilence": bool(out_sil),
            "wordClipped": bool(word_clipped), "tailKeepMs": int(min(tail, 10 ** 9)),
            "ok": ok}


def _is_edge(chars: list[dict], t: int) -> bool:
    if not chars:
        return False
    return t <= int(chars[0]["startMs"]) + NEAR_SILENCE_MS or t >= int(chars[-1]["endMs"]) - NEAR_SILENCE_MS


def classify(cut: dict) -> dict:
    """按 conf 三级定 action;guard 不过则降级 review(绝不放宽)。"""
    g = cut.get("guard") or {}
    conf = float(cut.get("conf", 0.0))
    if conf >= CONF_REMOVE and g.get("ok"):
        action = "remove"
    elif conf >= CONF_REVIEW:
        action = "review"
    else:
        action = "keep"
    if conf >= CONF_REMOVE and not g.get("ok"):
        action = "review"
        failed = [k for k in ("inSilence", "outSilence", "wordClipped") if not g.get(k)]
        if g.get("tailKeepMs", 0) < TAIL_KEEP_MS:
            failed.append("tailKeepMs")
        cut["note"] = (cut.get("note", "") + f" [guard 未过:{','.join(failed)}]").strip()
    cut["action"] = action
    return cut


# ---------------------------------------------------------------- 检测器

def detect_silence(wl: dict, min_ms: int = SILENCE_MIN_MS) -> list[dict]:
    chars, cuts = wl.get("chars", []), []
    for g in _gap_windows(chars):
        if g["ms"] < min_ms:
            continue
        in_ms = g["startMs"] + 150          # 两端各留 150ms(auto-editor --margin 精神)
        out_ms = g["endMs"] - 150
        if out_ms - in_ms < 200:
            continue
        cuts.append({"inMs": in_ms, "outMs": out_ms,
                     "reason": "breath" if g["ms"] < 900 else "silence",
                     "conf": 0.95, "note": f"{g['ms']}ms 停顿"})
    return cuts


def detect_filler(wl: dict) -> list[dict]:
    text, cmap = _text_and_map(wl)
    chars, out = wl.get("chars", []), []
    for word, conf in FILLERS.items():
        for a, b in _find_all(text, word):
            if a == 0 and len(text) > len(word) and text[b:b + 1] not in "，。！？…":
                pass
            in_ms, out_ms = _span_ms(chars, cmap[a], cmap[b - 1] + 1)
            if out_ms - in_ms <= 0 or out_ms - in_ms > 1500:
                continue
            if b < len(text) and text[b] in "，。！？…":
                out_ms += 120               # 把紧随的口语标点一并带走
            out.append({"inMs": in_ms, "outMs": min(out_ms, int(chars[-1]["endMs"])),
                        "reason": "filler", "conf": conf, "note": f"口头禅「{word}」"})
    return _dedupe(out)


def detect_repetition(wl: dict) -> list[dict]:
    text, cmap = _text_and_map(wl)
    chars, out = wl.get("chars", []), []
    for k in (1, 2, 3):
        i = 0
        while i + 2 * k <= len(text):
            if text[i:i + k] == text[i + k:i + 2 * k] and text[i:i + k].strip():
                frag = text[i:i + k]
                if k == 1 and frag not in REPEAT_WORDS:
                    i += 1
                    continue
                a, b = cmap[i], cmap[i + 2 * k - 1] + 1
                in_ms, out_ms = _span_ms(chars, a, b)
                out.append({"inMs": in_ms, "outMs": out_ms, "reason": "stumble",
                            "conf": 0.88 if k > 1 else 0.70, "note": f"重复「{frag}{frag}」"})
                i += 2 * k
            else:
                i += 1
    return _dedupe(out)


def detect_retake(wl: dict, max_gap_ms: int = 15000, min_ratio: float = 0.80) -> list[dict]:
    """重录:相邻两句高度相似且后者更完整 → 删前者、留后者(人总把好的说在最后)。"""
    sents = wl.get("sentences") or []
    chars = wl.get("chars", [])
    out = []
    for a, b in zip(sents, sents[1:]):
        ta, tb = a.get("text", ""), b.get("text", "")
        if not ta or not tb:
            continue
        sa, ea = a.get("span", [0, 0])
        sb, eb = b.get("span", [0, 0])
        if not (ea > sa and eb > sb):
            continue
        t_a_end = int(chars[min(ea, len(chars)) - 1]["endMs"])
        t_b_start = int(chars[min(sb, len(chars) - 1)]["startMs"])
        if t_b_start - t_a_end > max_gap_ms:
            continue
        ratio = SequenceMatcher(None, ta, tb).ratio()
        if ratio < min_ratio:
            continue
        more_complete = len(tb) >= len(ta) and tb[-1:] in "。！？…" or len(tb) > len(ta)
        if not more_complete:
            continue
        in_ms = int(chars[min(sa, len(chars) - 1)]["startMs"])
        out_ms = t_b_start
        if out_ms - in_ms < 200:
            continue
        out.append({"inMs": in_ms, "outMs": out_ms, "reason": "retake",
                    "conf": round(min(0.97, 0.72 + ratio * 0.3), 3),
                    "note": f"第 {a['id'] + 1} 次尝试(相似度 {ratio:.2f}),保留后一次"})
    return _dedupe(out)


def detect_off_topic(wl: dict, spans: list[list[int]] | None = None) -> list[dict]:
    """跑题段落:由 Agent 语义判断后通过 --off-topic '起-止,起-止' 传入(不自动猜)。"""
    chars = wl.get("chars", [])
    out = []
    for a, b in spans or []:
        in_ms, out_ms = _span_ms(chars, int(a), int(b))
        out.append({"inMs": in_ms, "outMs": out_ms, "reason": "off_topic",
                    "conf": 0.90, "note": "Agent 判定跑题/自我否定"})
    return out


DETECTORS = {"silence": detect_silence, "filler": detect_filler,
             "repetition": detect_repetition, "retake": detect_retake}


# ---------------------------------------------------------------- 融合

def _dedupe(cuts: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in sorted(cuts, key=lambda x: (x["inMs"], x["outMs"])):
        key = (c["inMs"], c["outMs"], c["reason"])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def merge_overlaps(cuts: list[dict]) -> list[dict]:
    """重叠区间取并集(保留置信度最高者的 reason)。"""
    if not cuts:
        return []
    ordered = sorted(cuts, key=lambda c: (c["inMs"], c["outMs"]))
    merged: list[dict] = [dict(ordered[0])]
    for c in ordered[1:]:
        last = merged[-1]
        if c["inMs"] <= last["outMs"] + 40:
            if c["outMs"] > last["outMs"]:
                last["outMs"] = c["outMs"]
            if c["conf"] > last["conf"]:
                last["reason"], last["conf"] = c["reason"], c["conf"]
            last["note"] = f"{last.get('note', '')}; {c.get('note', '')}".strip("; ")
        else:
            merged.append(dict(c))
    return merged


def rhetorical_suspect(cut: dict, cuts: list[dict]) -> bool:
    """反向保护:删掉后前后语义单元间隔从 >700ms 塌到 <200ms → 疑似修辞停顿。"""
    before = max([c["outMs"] for c in cuts if c["outMs"] <= cut["inMs"]] or [0])
    after = min([c["inMs"] for c in cuts if c["inMs"] >= cut["outMs"]] or [cut["outMs"]])
    inner = cut["outMs"] - cut["inMs"]
    remaining = (cut["inMs"] - before) + (after - cut["outMs"])
    original = inner + remaining
    return original > RHETORIC_ORIG_MS and remaining < RHETORIC_AFTER_MS


def build_cutlist(wl: dict, cuts: list[dict], params: dict | None = None) -> dict:
    chars = wl.get("chars", [])
    gaps = _gap_windows(chars)
    merged = merge_overlaps(cuts)
    for i, c in enumerate(merged):
        c["id"] = f"c{i + 1:03d}"
        c["guard"] = guard(c, chars, gaps)
        if c["reason"] not in REASONS:
            raise ValueError(f"未知 reason:{c['reason']}")
        c.setdefault("note", "")
        classify(c)
        if rhetorical_suspect(c, merged):
            c["action"] = "review"
            c["note"] = (c["note"] + " [rhetorical_pause_suspect]").strip()
    removed = [c for c in merged if c["action"] == "remove"]
    total = int(wl.get("srcDurationMs") or (int(chars[-1]["endMs"]) if chars else 0))
    keep = derive_keep(removed, total)
    return {"version": 1, "source": wl.get("source", ""),
            "detector": {"version": DETECTOR_VERSION, "params": params or {}},
            "cuts": merged, "keep": keep,
            "removedMs": sum(c["outMs"] - c["inMs"] for c in removed),
            "srcTotalMs": total}


def derive_keep(remove_cuts: list[dict], total: int) -> list[list[int]]:
    keep, cursor = [], 0
    for c in sorted(remove_cuts, key=lambda x: x["inMs"]):
        a, b = max(0, int(c["inMs"])), min(total, int(c["outMs"]))
        if a > cursor:
            keep.append([cursor, a])
        cursor = max(cursor, b)
    if cursor < total:
        keep.append([cursor, total])
    return [k for k in keep if k[1] - k[0] > 0]


# ---------------------------------------------------------------- 输出

def write_report(cl: dict, path: Path) -> None:
    cuts = cl["cuts"]
    total = cl["srcTotalMs"] or 1
    by_reason: dict[str, int] = {}
    for c in cuts:
        by_reason[c["reason"]] = by_reason.get(c["reason"], 0) + 1
    removes = [c for c in cuts if c["action"] == "remove"]
    reviews = [c for c in cuts if c["action"] == "review"]
    suspects = [c for c in cuts if "rhetorical_pause_suspect" in c.get("note", "")]
    lines = [
        f"# 粗剪报告 · {Path(cl.get('source', '')).name or '工程'}",
        "",
        f"- 源时长 {total / 1000:.1f}s → 保留 {(total - cl['removedMs']) / 1000:.1f}s"
        f"(裁掉 {cl['removedMs'] / total:.1%})",
        f"- 自动执行 remove:{len(removes)} 刀 / 待审 review:{len(reviews)} 刀 / "
        f"未动 keep:{len(cuts) - len(removes) - len(reviews)} 刀",
        f"- 按 reason:" + "、".join(f"{k} {v}" for k, v in sorted(by_reason.items())),
        f"- 可疑项:rhetorical_pause_suspect {len(suspects)}"
        + ("(见 " + ", ".join(c["id"] for c in suspects) + ")" if suspects else ""),
        "",
        "## 刀目明细",
        "",
        "| id | 入点 | 出点 | reason | conf | action | guard | 说明 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in cuts:
        g = c["guard"]
        gs = "✓" if g["ok"] else f"✗({','.join(k for k in ('inSilence', 'outSilence', 'wordClipped') if not g[k])})"
        lines.append(f"| {c['id']} | {c['inMs']} | {c['outMs']} | {c['reason']} | {c['conf']} | "
                     f"{c['action']} | {gs} | {c.get('note', '')} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_review_pack(cl: dict, outdir: Path, src: str | None = None) -> int:
    """每刀一个 md(有 ffmpeg 且给了源素材时附切点前后各 1.5s 音频)。"""
    rev = outdir / "review"
    rev.mkdir(parents=True, exist_ok=True)
    n = 0
    for c in cl["cuts"]:
        if c["action"] != "review":
            continue
        n += 1
        body = [f"# {c['id']}", "",
                f"- reason: `{c['reason']}`  conf: {c['conf']}  action: {c['action']}",
                f"- 区间: {c['inMs']}–{c['outMs']} ms({(c['outMs'] - c['inMs']) / 1000:.2f}s)",
                f"- guard: {json.dumps(c['guard'], ensure_ascii=False)}",
                f"- 说明: {c.get('note', '')}", "",
                "> 听 `%s.wav`(切点前后各 1.5s),approve 则把 action 改为 remove 并重跑 `--apply`。" % c["id"]]
        (rev / f"{c['id']}.md").write_text("\n".join(body) + "\n", encoding="utf-8")
        if src:
            _extract_clip(Path(src), rev / f"{c['id']}.wav", c["inMs"], c["outMs"])
    return n


def _extract_clip(src: Path, dst: Path, in_ms: int, out_ms: int) -> None:
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from rs_common import ffmpeg_bin, load_config, run
        start = max(0, in_ms - 1500) / 1000
        dur = (out_ms - in_ms + 3000) / 1000
        p = run([ffmpeg_bin(load_config()), "-y", "-v", "error", "-ss", f"{start:.3f}",
                 "-t", f"{dur:.3f}", "-i", str(src), "-vn", str(dst)])
        if p.returncode != 0 or not dst.is_file():
            dst.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass  # 无 ffmpeg 时只留 md,不阻塞


def finalize_cutlist(cl: dict) -> dict:
    """按当前 action 重算 keep(apply 路径)。"""
    total = int(cl["srcTotalMs"])
    removes = [c for c in cl["cuts"] if c["action"] == "remove"]
    cl["keep"] = derive_keep(removes, total)
    cl["removedMs"] = sum(c["outMs"] - c["inMs"] for c in removes)
    # 校验:keep 有序、不重叠、覆盖到片尾(段间空隙即被剪掉的区间,允许存在)
    cursor = 0
    for a, b in cl["keep"]:
        if a < cursor:
            raise ValueError(f"keep 区间重叠:a={a} < 上一段末尾 {cursor}")
        if b <= a:
            raise ValueError(f"keep 空区间:{a}-{b}")
        cursor = b
    if cursor != total:
        raise ValueError(f"keep 未覆盖到片尾:cursor={cursor} total={total}")
    return cl


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wordline", nargs="?")
    ap.add_argument("--detect", default="all")
    ap.add_argument("--out", default="04_cut")
    ap.add_argument("--review-pack", dest="review_pack")
    ap.add_argument("--apply")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--off-topic", dest="off_topic", default="",
                    help="Agent 判定的跑题段落,格式 '起-止,起-止'(chars 下标)")
    ap.add_argument("--min-silence-ms", type=int, default=SILENCE_MIN_MS)
    a = ap.parse_args()

    if a.review_pack:
        cl = json.loads(Path(a.review_pack).read_text(encoding="utf-8"))
        outdir = Path(a.review_pack).parent
        n = write_review_pack(cl, outdir, cl.get("source") or None)
        return emit(True, "REVIEW_PACK_OK", f"审查包 {n} 条 → {outdir / 'review'}", {"count": n})

    if a.apply:
        p = Path(a.apply)
        cl = json.loads(p.read_text(encoding="utf-8"))
        src_total = int(cl.get("srcTotalMs") or 0)
        for c in cl["cuts"]:
            if c["reason"] not in REASONS:
                return emit(False, "BAD_REASON", f"{c.get('id')} 的 reason 非法:{c['reason']}", exit_code=2)
            if src_total and not (0 <= c["inMs"] < c["outMs"] <= src_total):
                return emit(False, "BAD_RANGE", f"{c.get('id')} 区间越界:{c['inMs']}–{c['outMs']}", exit_code=2)
        try:
            finalize_cutlist(cl)
        except ValueError as exc:
            return emit(False, "KEEP_INCONSISTENT", str(exc), exit_code=4)
        dst = p.with_name("cutlist.applied.json")
        dst.write_text(json.dumps(cl, ensure_ascii=False, indent=1), encoding="utf-8")
        msg = (f"已应用:{len([c for c in cl['cuts'] if c['action'] == 'remove'])} 刀 remove,"
               f"保留 {len(cl['keep'])} 段 / {(src_total - cl['removedMs']) / 1000:.1f}s")
        if a.render:
            msg += ";下一步:rs_align.py remap <wordline> --cutlist " + str(dst)
        return emit(True, "CUT_APPLIED", msg, {"path": str(dst), "keep": cl["keep"],
                                               "removedMs": cl["removedMs"]})

    if not a.wordline:
        return emit(False, "NO_INPUT", "需要 <wordline.json> 或 --apply/--review-pack", exit_code=2)
    wl = json.loads(Path(a.wordline).read_text(encoding="utf-8"))
    names = list(DETECTORS) if a.detect in ("all", "") else [s.strip() for s in a.detect.split(",")]
    cuts: list[dict] = []
    counts: dict[str, int] = {}
    for name in names:
        fn = DETECTORS.get(name)
        if not fn:
            return emit(False, "BAD_DETECTOR", f"未知检测器:{name}(可选 {list(DETECTORS)})", exit_code=2)
        got = fn(wl)
        counts[name] = len(got)
        cuts.extend(got)
    if a.off_topic:
        spans = []
        for part in a.off_topic.split(","):
            lo, _, hi = part.partition("-")
            if lo.strip().isdigit() and hi.strip().isdigit():
                spans.append([int(lo), int(hi)])
        cuts.extend(detect_off_topic(wl, spans))

    params = {"silenceMinMs": a.min_silence_ms, "tailKeepMs": TAIL_KEEP_MS,
              "confRemove": CONF_REMOVE, "confReview": CONF_REVIEW}
    try:
        cl = build_cutlist(wl, cuts, params)
    except ValueError as exc:
        return emit(False, "BAD_REASON", str(exc), exit_code=2)

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "cutlist.json").write_text(json.dumps(cl, ensure_ascii=False, indent=1), encoding="utf-8")
    write_report(cl, outdir / "cut_report.md")

    total = cl["srcTotalMs"] or 1
    removes = [c for c in cl["cuts"] if c["action"] == "remove"]
    reviews = [c for c in cl["cuts"] if c["action"] == "review"]
    msg = (f"检出 {len(cl['cuts'])} 刀(remove {len(removes)} / review {len(reviews)}),"
           f"预计裁掉 {cl['removedMs'] / total:.1%}")
    return emit(True, "CUT_OK", msg, {"cutlist": str(outdir / "cutlist.json"),
                                      "report": str(outdir / "cut_report.md"),
                                      "detectors": counts, "remove": len(removes),
                                      "review": len(reviews), "removedMs": cl["removedMs"],
                                      "srcTotalMs": cl["srcTotalMs"], "keep": cl["keep"]})


if __name__ == "__main__":
    sys.exit(main())

