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
from rs_common import emit, guard_passed  # noqa: E402

DETECTOR_VERSION = "cutflow-1.1"
REASONS = {"silence", "breath", "filler", "false_start", "retake",
           "stumble", "repetition", "off_topic", "manual"}
CONF_REMOVE, CONF_REVIEW = 0.90, 0.60
SILENCE_MIN_MS = 600          # 静音判定:VAD 间隔 ≥600ms
DEAD_AIR_MIN_MS = 1200        # "有画面无语音"长段(调整仪容/换提词器)
TAIL_KEEP_MS = 60             # 切点后释放余量(Descript "Avoid harsh cuts")
NEAR_SILENCE_MS = 120         # 切点前后多远内有静音算"落在静音区"
RHETORIC_ORIG_MS, RHETORIC_AFTER_MS = 700, 200

# guard 按 reason 分档(OPTIMIZATION-v7 #3):
#   「重录/整段重来」的切点本就紧邻语音,要求它落在静音区 = 永远无法 remove;
#   但 `wordClipped`(不切断字内音素)永不放松 —— 这是"宁可漏删不可错删"的底线。
GUARD_ALL = ("inSilence", "outSilence", "wordClipped", "tailKeep")
GUARD_REQUIRED: dict[str, tuple[str, ...]] = {
    "retake": ("wordClipped", "tailKeep"),
    "false_start": ("wordClipped", "tailKeep"),
    "stumble": ("wordClipped", "tailKeep"),
    "repetition": ("wordClipped", "tailKeep"),
    "off_topic": ("wordClipped", "tailKeep"),
    "manual": ("wordClipped", "tailKeep"),
    "silence": GUARD_ALL, "breath": GUARD_ALL, "filler": GUARD_ALL,
}
# 应被剪掉的"元话语":口播人员要求重来的话,不该出现在成片里(review 候选,不自动删)
SELF_NEGATIVE = ("说错了", "重新说", "再来一遍", "这段不算", "重来一遍", "不对不对",
                 "我们重新来过", "重录一下")

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
    g = {"inSilence": bool(in_sil), "outSilence": bool(out_sil),
         "wordClipped": bool(word_clipped), "tailKeepMs": int(min(tail, 10 ** 9))}
    reason = cut.get("reason") or ""
    g["required"] = list(GUARD_REQUIRED.get(reason, GUARD_ALL))
    # ok = 四项全过(保守口径,保留给调用方参考);okByReason = 该 reason 的硬过项
    g["ok"] = bool(in_sil and out_sil and not word_clipped and tail >= TAIL_KEEP_MS)
    g["okByReason"] = _guard_pass(g, reason)
    return g


def _guard_pass(g: dict, reason: str) -> bool:
    """按 reason 取硬过项判定 guard。`wordClipped`(不切断字内音素)任何 reason 下都硬。"""
    if g.get("wordClipped"):
        return False
    need = GUARD_REQUIRED.get(reason, GUARD_ALL)
    if "tailKeep" in need and g.get("tailKeepMs", 0) < TAIL_KEEP_MS:
        return False
    return all(g.get(k) for k in need if k not in ("wordClipped", "tailKeep"))


def _is_edge(chars: list[dict], t: int) -> bool:
    if not chars:
        return False
    return t <= int(chars[0]["startMs"]) + NEAR_SILENCE_MS or t >= int(chars[-1]["endMs"]) - NEAR_SILENCE_MS


def classify(cut: dict) -> dict:
    """按 conf 三级定 action;guard 不过则降级 review(绝不放宽)。

    guard 判定用**该 reason 的硬过项**(`okByReason`)——重录类不再因"切点不落静音区"被压进 review。
    """
    g = cut.get("guard") or {}
    conf = float(cut.get("conf", 0.0))
    passed = guard_passed(g)
    if conf >= CONF_REMOVE and passed:
        action = "remove"
    elif conf >= CONF_REVIEW:
        action = "review"
    else:
        action = "keep"
    if conf >= CONF_REMOVE and not passed:
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


def _more_complete(new: str, old: str) -> bool:
    """后者更完整(人总把好的说在最后)。"""
    if len(new) > len(old):
        return True
    return len(new) >= len(old) and new.rstrip()[-1:] in "。！？…"


def _sentence_spans(wl: dict) -> list[dict]:
    """句 → {id, text, startMs, endMs}。粗剪检测间通用。"""
    sents = wl.get("sentences") or []
    chars = wl.get("chars", [])
    out = []
    for s in sents:
        a, b = s.get("span", [0, 0])
        a, b = max(0, int(a)), min(int(b), len(chars))
        if b <= a:
            continue
        out.append({"id": s.get("id", len(out)),
                    "text": s.get("text") or "".join(c["ch"] for c in chars[a:b]),
                    "startMs": int(chars[a]["startMs"]),
                    "endMs": int(chars[b - 1]["endMs"])})
    return out


def _chain_merge(cuts: list[dict], tol_ms: int = 300) -> list[dict]:
    """把同一段话的连续重录刀串成一刀 —— 否则两个旧尝试之间会留下几十毫秒的碎片。"""
    if not cuts:
        return []
    ordered = sorted(cuts, key=lambda c: (c["inMs"], c["outMs"]))
    out = [dict(ordered[0])]
    for c in ordered[1:]:
        last = out[-1]
        if c["inMs"] <= last["outMs"] + tol_ms:
            last["outMs"] = max(last["outMs"], c["outMs"])
            last["note"] = f"{last.get('note', '')}; {c.get('note', '')}".strip("; ")
        else:
            out.append(dict(c))
    return out


def detect_retake(wl: dict, max_gap_ms: int = 30000, min_ratio: float = 0.80,
                  max_sents: int = 6) -> list[dict]:
    """重录:**滑动窗口内任意两句**高度相似且后者更完整 → 删 [最早旧尝试, 最后一次尝试)。

    v0.7.0(OPTIMIZATION-v7 #3):旧实现只比相邻两句,而"说完一段/调整后再重来"常跨
    2–3 句、间隔更久 → 大量漏检;且每次只删紧邻前一次,同一段录三次会残留中间那次。
    现在:窗口 = 句数 ≤max_sents 或时间间隔 ≤max_gap_ms;命中后**一刀删掉全部旧尝试**。
    出点取「最后一次尝试起点 − TAIL_KEEP_MS」,留出自然起音,同时满足 tailKeep 硬项。
    """
    spans = _sentence_spans(wl)
    out: list[dict] = []
    for i, a in enumerate(spans):
        if not a["text"]:
            continue
        best = -1
        for j in range(i + 1, min(len(spans), i + 1 + max_sents)):
            b = spans[j]
            if not b["text"]:
                continue
            if b["startMs"] - a["endMs"] > max_gap_ms:
                break
            if not _more_complete(b["text"], a["text"]):
                continue
            if SequenceMatcher(None, a["text"], b["text"]).ratio() < min_ratio:
                continue
            best = j                       # 取窗口内**最后一次**相似尝试
        if best < 0:
            continue
        in_ms = a["startMs"]
        out_ms = max(in_ms + 200, spans[best]["startMs"] - TAIL_KEEP_MS)
        ratio = SequenceMatcher(None, a["text"], spans[best]["text"]).ratio()
        out.append({"inMs": in_ms, "outMs": out_ms, "reason": "retake",
                    "conf": round(min(0.97, 0.72 + ratio * 0.3), 3),
                    "note": f"第 {i + 1}→{best + 1} 句重录(相似度 {ratio:.2f}),保留最后一次"})
    return _dedupe(_chain_merge(out))


def detect_retake_block(wl: dict, min_chars: int = 8, max_gap_ms: int = 60000) -> list[dict]:
    """段落级整段重来:同一段话(连续 ≥min_chars 字)**原样**再说一遍 → 删旧留新。

    用于"说完一整段觉得不满意,整段重来"。判据取保守的**逐字相同**,宁可漏删不可错删。
    """
    text, cmap = _text_and_map(wl)
    chars = wl.get("chars", [])
    n = len(text)
    if n < 2 * min_chars or not chars:
        return []
    out: list[dict] = []
    k = 0
    while k + min_chars <= n:
        best_pos, best_len = -1, 0
        for p in range(k + 1, n - min_chars + 1):
            L = 0
            while (k + L < p) and (p + L < n) and text[k + L] == text[p + L]:
                L += 1
            if L > best_len:
                best_pos, best_len = p, L
        if best_len < min_chars:
            k += 1
            continue
        in_ms = int(chars[cmap[k]]["startMs"])
        out_ms = max(in_ms + 200, int(chars[cmap[best_pos]]["startMs"]) - TAIL_KEEP_MS)
        if out_ms - in_ms >= 200 and out_ms - in_ms <= max_gap_ms:
            out.append({"inMs": in_ms, "outMs": out_ms, "reason": "false_start",
                        "conf": 0.92,
                        "note": f"整段重来「{text[k:k + min(min_chars, 12)]}…」({best_len} 字),保留后一次"})
        k = best_pos + best_len            # 跳过已匹配区间,避免同一处反复出刀
    return _dedupe(out)


_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def parse_silencedetect(stderr: str) -> list[dict]:
    """解析 ffmpeg `silencedetect` 的输出 → [{startMs, endMs, ms}]。"""
    out, cur = [], None
    for line in stderr.splitlines():
        m = _SIL_START.search(line)
        if m:
            cur = float(m.group(1))
            continue
        m = _SIL_END.search(line)
        if m and cur is not None:
            end = float(m.group(1))
            start_ms, end_ms = int(cur * 1000), int(end * 1000)
            if end_ms > start_ms:
                out.append({"startMs": start_ms, "endMs": end_ms, "ms": end_ms - start_ms})
            cur = None
    return out


def _probe_silence(media: Path, db: float, min_ms: int) -> list[dict] | None:
    """用 ffmpeg 能量探测静音段。工具不可用时返回 None(调用方退回字间 gap)。"""
    try:
        from rs_common import ffmpeg_bin, load_config, run
        p = run([ffmpeg_bin(load_config()), "-hide_banner", "-nostats", "-i", str(media),
                 "-af", f"silencedetect=noise={db}dB:d={min_ms / 1000.0:.3f}",
                 "-f", "null", "-"])
        return parse_silencedetect(p.stderr or "")
    except Exception:  # noqa: BLE001 — 无 ffmpeg / 解码失败 → 由调用方退回 gap
        return None


def detect_dead_air(wl: dict, media: str | None = None, min_ms: int = DEAD_AIR_MIN_MS,
                    db: float = -35.0) -> list[dict]:
    """「有画面无语音」长段(调整仪容 / 换提词器):优先按素材音频能量探测。

    给了 media 且 ffmpeg 可用 → `silencedetect` 能量探测(能抓到 ASR 却把静音写成了
    文本的情况);否则退回 wordline 的字间 gap,只认 ≥min_ms 的空档。
    """
    spans: list[dict] = []
    if media:
        spans = _probe_silence(Path(media), db, min_ms) or []
    if not spans:
        spans = [g for g in _gap_windows(wl.get("chars", [])) if g["ms"] >= min_ms]
    out = []
    for g in spans:
        in_ms, out_ms = int(g["startMs"]) + 120, int(g["endMs"]) - 120
        if out_ms - in_ms < 200:
            continue
        out.append({"inMs": in_ms, "outMs": out_ms,
                    "reason": "breath" if g["ms"] < min_ms * 2 else "silence",
                    "conf": 0.95,
                    "note": f"无有效语音 {g['ms']}ms(疑似调整仪容/换提词器)"})
    return _dedupe(out)


def detect_self_negative(wl: dict, max_len_ms: int = 4000) -> list[dict]:
    """口播里的"元话语"(说错了/再来一遍…)—— 成片里不该出现;只进 review,不自动删。"""
    text, cmap = _text_and_map(wl)
    chars = wl.get("chars", [])
    out = []
    for phrase in SELF_NEGATIVE:
        for a, b in _find_all(text, phrase):
            in_ms = int(chars[cmap[a]]["startMs"])
            out_ms = int(chars[cmap[b - 1]]["endMs"])
            if out_ms - in_ms > max_len_ms:
                continue
            out.append({"inMs": in_ms, "outMs": out_ms, "reason": "off_topic", "conf": 0.85,
                        "note": f"疑似元话语「{phrase}」,人工/Agent 确认后删"})
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


DETECTORS = {"silence": detect_silence, "dead_air": detect_dead_air,
             "filler": detect_filler, "repetition": detect_repetition,
             "retake": detect_retake, "retake_block": detect_retake_block,
             "self_negative": detect_self_negative}


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
        # 反向保护只针对"停顿类"刀:重录/整段重来删掉的是一整段重复内容,不是修辞停顿
        if c["reason"] in ("silence", "breath") and rhetorical_suspect(c, merged):
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
        need = g.get("required") or list(GUARD_ALL)
        passed = guard_passed(g)
        hard_bad = [k for k in ("inSilence", "outSilence", "wordClipped") if k in need and not g.get(k)]
        if "tailKeep" in need and g.get("tailKeepMs", 0) < TAIL_KEEP_MS:
            hard_bad.append("tailKeepMs")
        warn = [k for k in ("inSilence", "outSilence") if k not in need and not g.get(k)]
        gs = ("✓硬过" if passed else "✗" + ",".join(hard_bad)) + \
             (f" 告警:{','.join(warn)}" if warn else "")
        lines.append(f"| {c['id']} | {c['inMs']} | {c['outMs']} | {c['reason']} | {c['conf']} | "
                     f"{c['action']} | {gs} | {c.get('note', '')} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_review_pack(cl: dict, outdir: Path, src: str | None = None) -> int:
    """每刀一个 md(有 ffmpeg 且给了源素材时附切点前后各 1.5s 音频)。

    抽音频失败**不阻塞**,但会把失败项记进 `review/_DEGRADED.md`(不再静默吞掉)。
    """
    rev = outdir / "review"
    rev.mkdir(parents=True, exist_ok=True)
    n, failed = 0, []
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
        if src and not _extract_clip(Path(src), rev / f"{c['id']}.wav", c["inMs"], c["outMs"]):
            failed.append(c["id"])
            body.append("")
            body.append("> ⚠ 切点音频抽取失败(无 ffmpeg / 解码失败):只能看上面的区间自行判断。")
        (rev / f"{c['id']}.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    if failed:
        (rev / "_DEGRADED.md").write_text(
            "# 审查包降级说明\n\n以下刀未能抽出切点音频(无 ffmpeg / 素材解码失败):\n\n"
            + "\n".join(f"- {i}" for i in failed) + "\n\n"
            "修复:`--media <源素材>` 指向可解码文件,或先跑 `rs_doctor --report` 查 ffmpeg。\n",
            encoding="utf-8")
    else:
        (rev / "_DEGRADED.md").unlink(missing_ok=True)
    return n


def _extract_clip(src: Path, dst: Path, in_ms: int, out_ms: int) -> bool:
    """切点前后各 1.5s 抽成 wav。返回是否成功(失败不抛,由调用方记降级)。"""
    try:
        from rs_common import ffmpeg_bin, load_config, run
        start = max(0, in_ms - 1500) / 1000
        dur = (out_ms - in_ms + 3000) / 1000
        p = run([ffmpeg_bin(load_config()), "-y", "-v", "error", "-ss", f"{start:.3f}",
                 "-t", f"{dur:.3f}", "-i", str(src), "-vn", str(dst)])
        if p.returncode != 0 or not dst.is_file():
            dst.unlink(missing_ok=True)
            return False
        return True
    except Exception:  # noqa: BLE001 — 无 ffmpeg / 配置缺失:降级留痕,不阻塞
        dst.unlink(missing_ok=True)
        return False


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
    ap.add_argument("--media", help="源素材路径(给 dead_air 做音频能量探测;不给则退回字间 gap)")
    ap.add_argument("--retake-ratio", dest="retake_ratio", type=float, default=0.80,
                    help="重录相似度阈值(口播 0.80;怕误删的类型可提到 0.86)")
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
        if name == "dead_air":
            got = fn(wl, media=a.media)
        elif name == "retake":
            got = fn(wl, min_ratio=a.retake_ratio)
        else:
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
              "retakeRatio": a.retake_ratio, "confRemove": CONF_REMOVE,
              "confReview": CONF_REVIEW}
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

