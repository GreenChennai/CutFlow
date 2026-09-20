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

副文档 07(P26/P27):
  · keep 末段终点保底 = max(末字 endMs + 尾余量, ffprobe 实测时长)——口播结尾
    0.5–0.8s 自然底噪不截断(尾余量默认 650ms,--tail-reserve-ms;给了 --media
    就以实测为唯一真相)。
  · --apply 是时长账同步的单一入口:自动改平 wordline 的 srcDurationMs/removedMs/
    finalDurationMs(src − removed == final,写盘即校验);wordline 带 manualEdit
    手改痕迹时拒绝自动改写(--force 显式越过)。
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
import rs_common  # noqa: E402
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

# ---- v0.11 R4(ITERATION-GUIDE §4):词表外置 + margin 不对称 + 防碎切 ----
MARGIN_IN_MS, MARGIN_OUT_MS = 150, 300   # 后留白 > 前留白,给呼吸感(auto-editor --margin 不对称语义)
SMOOTH_MINCUT_MS = 120    # <120ms 的刀整体放弃:亚音素级剪切人耳难辨,只添错删风险
SMOOTH_MINCLIP_MS = 100   # 相邻刀之间 <100ms 的保留碎片并入刀内(残段只会是爆音)
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# ---- P26-3(副文档 07):keep 末段终点保底 ----
# 「口播结尾留 0.5–0.8s 自然底噪,是不割裂的最低要求」(20260920 NCLM1605:
# keep 末段被钉在 ASR 钳制出的错误总时长上,末字衰减尾音 + 0.7s 底噪全被切掉)。
TAIL_RESERVE_MS = 650     # 口播尾余量默认 0.65s(建议区间 500–800;--tail-reserve-ms 可调)
CLAMP_SIGNATURE_TOL_MS = 40   # 「记录总时长 ≈ 末字 endMs」的钳制签名容差(≈1 帧 @24fps)

# ---- J2(副文档 06):protect 保护区 ----
# 「必须保留发音」的词的有效发音区间(单位与 keep 一致,ms,左闭右开)。
# guard 加第四条:切点(remove 区间)不得侵入 protect 区;优先级 protect >
# 既有 guard 三项 —— guard 不过尚可降级 review,protect 冲突**即报错**而非降级
# (降级 = 留在稿子里等人审,protect 的语义是"审也不许切",故必须显式解决冲突:
# 要么改刀,要么撤区)。
PROTECT_CODE = "PROTECT_INTRUDED"


def normalize_protect(raw) -> list[dict]:
    """把 --protect "a-b,c-d" 或 cutlist.protect 归一成 [{"startMs","endMs","note"?}]。

    非法区间(负数/倒置)直接 ValueError —— protect 是硬约束,宁可报错不可猜。
    """
    zones: list[dict] = []
    if not raw:
        return zones
    items = raw if isinstance(raw, list) else str(raw).split(",")
    for it in items:
        if isinstance(it, dict):
            a, b = int(it.get("startMs", -1)), int(it.get("endMs", -1))
            note = str(it.get("note", ""))
        else:
            s = str(it).strip()
            if not s:
                continue
            lo, sep, hi = s.partition("-")
            if not (lo.strip().lstrip("+").isdigit() and hi.strip().lstrip("+").isdigit()):
                raise ValueError(f"protect 区间非法(应为 起ms-止ms):{s}")
            a, b = int(lo), int(hi)
            note = ""
        if a < 0 or b <= a:
            raise ValueError(f"protect 区间非法(需 0 ≤ start < end):{a}-{b}")
        z = {"startMs": a, "endMs": b}
        if note:
            z["note"] = note
        zones.append(z)
    return zones


def protect_violations(cuts: list[dict], protects,
                       actions: tuple[str, ...] = ("remove", "review")) -> list[str]:
    """刀区间与 protect 区的冲突清单(半开区间相交判定;触边不算侵入)。

    actions 缺省审 remove+review(构建期:review 正是 guard 的降级去处 —— protect
    优先级高于 guard,冲突**不许降级**,必须报错);--apply / finalize 传
    ("remove",) 只审真正会执行的刀(review 此时= 不删,批准后重跑 apply 仍会过闸)。
    """
    zones = normalize_protect(protects)
    if not zones:
        return []
    out = []
    for c in cuts:
        if c.get("action") not in actions:
            continue
        for z in zones:
            if max(int(c["inMs"]), z["startMs"]) < min(int(c["outMs"]), z["endMs"]):
                note = z.get("note") or ""
                out.append(f"{c.get('id', '?')} [{c['inMs']}–{c['outMs']}ms] 侵入 protect 区 "
                           f"[{z['startMs']}–{z['endMs']}ms]{('（' + note + '）') if note else ''}")
                break
    return out


def _assert_no_protect_intrusion(cuts: list[dict], protects,
                                 actions: tuple[str, ...] = ("remove", "review")) -> None:
    bad = protect_violations(cuts, protects, actions)
    if bad:
        raise ValueError(f"{PROTECT_CODE}:切点侵入 protect 区(优先级高于 guard,冲突即报错"
                         f"而非降级;请改刀或撤区):{'；'.join(bad)}")


def probe_duration_ms(media, cfg: dict | None = None) -> int | None:
    """ffprobe 实测媒体时长 ms;失败返回 None(调用方退回记录值,不阻塞)。"""
    try:
        from rs_common import media_duration_s
        d = float(media_duration_s(media, cfg))
        return int(round(d * 1000)) if d > 0 else None
    except SystemExit:
        return None
    except Exception:  # noqa: BLE001 — 无 ffprobe/坏文件:显式降级
        return None


def tail_keep_end_ms(chars: list[dict], recorded_total: int, measured_ms: int | None,
                     reserve_ms: int = TAIL_RESERVE_MS,
                     recorded_is_measured: bool = False) -> int:
    """keep 末段终点保底(P26-3):`max(末字 endMs + 尾余量, 实测时长)`。

    - 有 ffprobe 实测 → 实测时长即物理上限与唯一真相(末字衰减 + 底噪完整保留);
      实测异常地短于末字(坏数据)时至少保住末字。
    - 无实测、但记录值本就是 ffprobe 产物(`durationProvenance == "ffprobe"`)→
      信任记录值:真实媒体末尾已含全部尾音,外推反而越界。
    - 无实测且记录值来自 ASR 链路 → 仅在「钳制疑点形态」(记录总时长与末字 endMs
      重合,20260920 事故签名)下外推一个尾余量,不在字尾瞬间硬停。
    """
    hard = [c for c in chars if str(c.get("ch", "")).strip()]
    last_end = max((int(c["endMs"]) for c in hard), default=0)
    floor = last_end + max(0, int(reserve_ms))
    if measured_ms and int(measured_ms) > 0:
        return max(int(measured_ms), min(last_end, int(measured_ms)))
    if recorded_is_measured:
        return int(recorded_total or 0)
    if int(recorded_total or 0) - last_end <= CLAMP_SIGNATURE_TOL_MS:
        return max(int(recorded_total or 0), floor)     # 记录值被字尾钳制的疑点形态
    return int(recorded_total or 0)


def load_lexicon() -> dict:
    """词表外置:templates/fillers.json 优先,缺省回退内建(永不因缺文件而崩)。
    口癖因人而异;brief 阶段允许用户补词,报告按词统计命中。"""
    try:
        d = json.loads((TEMPLATES_DIR / "fillers.json").read_text(encoding="utf-8"))
        return {
            "fillers": {str(k): float(v) for k, v in (d.get("fillers") or {}).items()}
            or dict(FILLERS),
            "repeat": tuple(d.get("repeat_words") or REPEAT_WORDS),
            "self_negative": tuple(d.get("self_negative") or SELF_NEGATIVE),
        }
    except (OSError, json.JSONDecodeError, ValueError):
        return {"fillers": dict(FILLERS), "repeat": REPEAT_WORDS,
                "self_negative": SELF_NEGATIVE}


LEXICON = load_lexicon()


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

def detect_silence(wl: dict, min_ms: int = SILENCE_MIN_MS,
                   margin_in_ms: int = MARGIN_IN_MS,
                   margin_out_ms: int = MARGIN_OUT_MS) -> list[dict]:
    chars, cuts = wl.get("chars", []), []
    for g in _gap_windows(chars):
        if g["ms"] < min_ms:
            continue
        in_ms = g["startMs"] + margin_in_ms   # 后留白 > 前留白(auto-editor --margin 不对称语义)
        out_ms = g["endMs"] - margin_out_ms
        if out_ms - in_ms < 200:
            continue
        cuts.append({"inMs": in_ms, "outMs": out_ms,
                     "reason": "breath" if g["ms"] < 900 else "silence",
                     "conf": 0.95, "note": f"{g['ms']}ms 停顿"})
    return cuts


def detect_filler(wl: dict) -> list[dict]:
    text, cmap = _text_and_map(wl)
    chars, out = wl.get("chars", []), []
    for word, conf in LEXICON["fillers"].items():
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
                if k == 1 and frag not in LEXICON["repeat"]:
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
    for phrase in LEXICON["self_negative"]:
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


def smooth_cuts(cuts: list[dict]) -> list[dict]:
    """v0.11 R4 防碎切(auto-editor --smooth 精神,ITERATION-GUIDE §4.1):
    ①相邻刀之间 <SMOOTH_MINCLIP_MS 的保留碎片并入后一刀(残段只会是爆音);
    ②合并后仍 <SMOOTH_MINCUT_MS 的刀整体放弃(亚音素剪切人耳难辨,只添错删风险;
    「宁可漏删」——放弃即 keep,永不因平滑而多删)。"""
    if not cuts:
        return []
    ordered = sorted(cuts, key=lambda c: (c["inMs"], c["outMs"]))
    merged: list[dict] = [dict(ordered[0])]
    for c in ordered[1:]:
        last = merged[-1]
        if c["inMs"] - last["outMs"] < SMOOTH_MINCLIP_MS:
            last["outMs"] = max(last["outMs"], c["outMs"])
            if c["conf"] > last["conf"]:
                last["reason"], last["conf"] = c["reason"], c["conf"]
            last["note"] = f"{last.get('note', '')}; smooth合并".strip("; ")
        else:
            merged.append(dict(c))
    kept = [c for c in merged if c["outMs"] - c["inMs"] >= SMOOTH_MINCUT_MS]
    dropped = len(merged) - len(kept)
    if dropped and kept:
        kept[0]["note"] = (kept[0].get("note", "") +
                           f" [smooth:放弃 {dropped} 刀 <{SMOOTH_MINCUT_MS}ms 碎刀]").strip()
    return kept


def detect_hesitate(wl: dict, media: str | None = None,
                    min_ms: int = 300, max_ms: int = 1200, db: int = -25) -> list[dict]:
    """亚阈值停顿(hesitate):能量谷 0.3–1.2s、谷内无任何字 —— 拖长音/迟疑。
    需要 --media(能量探测);只进 review(conf 0.62),守住「宁可漏删」。"""
    if not media:
        return []
    chars = wl.get("chars", [])
    out = []
    for g in (_probe_silence(Path(media), db, min_ms) or []):
        if g["ms"] > max_ms:
            continue
        in_ms, out_ms = int(g["startMs"]) + 80, int(g["endMs"]) - 80
        if out_ms - in_ms < 150:
            continue
        if any(c["endMs"] > in_ms and c["startMs"] < out_ms for c in chars):
            continue          # 谷里有字 = 可能是轻声/ASR 漏字,保守不切
        out.append({"inMs": in_ms, "outMs": out_ms, "reason": "silence",
                    "conf": 0.62,
                    "note": f"hesitate:无字段能量谷 {g['ms']}ms(亚阈值停顿,只进 review)"})
    return _dedupe(out)


DETECTORS = {"silence": detect_silence, "dead_air": detect_dead_air,
             "filler": detect_filler, "repetition": detect_repetition,
             "retake": detect_retake, "retake_block": detect_retake_block,
             "self_negative": detect_self_negative, "hesitate": detect_hesitate}


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
    for c in cuts:
        if c["reason"] not in REASONS:
            raise ValueError(f"未知 reason:{c['reason']}")
    merged = smooth_cuts(merge_overlaps(cuts))
    for i, c in enumerate(merged):
        c["id"] = f"c{i + 1:03d}"
        c["guard"] = guard(c, chars, gaps)
        c.setdefault("note", "")
        classify(c)
        # 反向保护只针对"停顿类"刀:重录/整段重来删掉的是一整段重复内容,不是修辞停顿
        if c["reason"] in ("silence", "breath") and rhetorical_suspect(c, merged):
            c["action"] = "review"
            c["note"] = (c["note"] + " [rhetorical_pause_suspect]").strip()
        # v0.11 R5:每刀带前后 1.2s 文本上下文 —— 审查从「听 30 个 3 秒」变「读 30 行」
        c["text"] = _context_text(chars, c)
    # J2 guard 第四条:切点不得侵入 protect 区。优先级 protect > guard 三项:
    # guard 不过会降级 review,protect 冲突**报错**(降级 = 留稿待人审,而 protect
    # 的语义是"审也不许切")。
    protects = normalize_protect((params or {}).get("protect"))
    _assert_no_protect_intrusion(merged, protects)
    removed = [c for c in merged if c["action"] == "remove"]
    total_rec = int(wl.get("srcDurationMs") or (int(chars[-1]["endMs"]) if chars else 0))
    # P26-3 keep 末段终点保底:总时长以实测/末字+尾余量兜底,不在字尾瞬间硬停
    reserve = int((params or {}).get("tailReserveMs") or TAIL_RESERVE_MS)
    measured = int((params or {}).get("measuredMs") or 0)
    rec_is_probe = bool((params or {}).get("recordedIsMeasured"))
    total = tail_keep_end_ms(chars, total_rec, measured or None, reserve, rec_is_probe)
    keep = derive_keep(removed, total)
    cl = {"version": 1, "source": wl.get("source", ""),
          "detector": {"version": DETECTOR_VERSION, "params": params or {}},
          "cuts": merged, "keep": keep,
          "removedMs": sum(c["outMs"] - c["inMs"] for c in removed),
          "srcTotalMs": total}
    if protects:
        cl["protect"] = protects
    if total != total_rec:
        cl["tail"] = {"reserveMs": reserve,
                      "measuredMs": measured or None,
                      "recordedTotalMs": total_rec, "keepEndMs": total,
                      "note": "keep 末段终点保底(P26-3):末字衰减尾音+自然底噪不截断"}
    cl["script"] = _script_marks(chars, merged, total)
    return cl


def _context_text(chars: list[dict], cut: dict, ctx_ms: int = 1200) -> str:
    """刀口前后各 1.2s 的文本上下文(R5)。"""
    return "".join(c["ch"] for c in chars
                   if cut["inMs"] - ctx_ms < c["endMs"] and c["startMs"] < cut["outMs"] + ctx_ms)


def _script_marks(chars: list[dict], cuts: list[dict], total: int,
                  line_chars: int = 42) -> list[dict]:
    """R5 删改稿:全文按 keep/remove/review 分行标注 —— 机器粗剪、人读稿精修
    (对齐 Descript / Premiere 文本化编辑的心智,ITERATION-GUIDE §4.3)。"""
    if not chars:
        return []
    marks = []
    for c in chars:
        act = "keep"
        for cut in cuts:
            if cut["action"] == "keep":
                continue
            if max(cut["inMs"], c["startMs"]) < min(cut["outMs"], c["endMs"]):
                if cut["action"] == "remove" or act == "keep":
                    act = cut["action"]
        marks.append((act, c["ch"]))
    lines, buf, cur = [], [], marks[0][0]
    for act, ch in marks + [("END", "")]:
        if act != cur or len(buf) >= line_chars:
            if buf:
                lines.append({"action": cur, "text": "".join(buf)})
            buf = []
            cur = act
        if act != "END":
            buf.append(ch)
    return lines


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


def self_wordline_default(cutlist_path: Path) -> Path | None:
    """P27-1:由 cutlist 路径推断工程 wordline(<root>/04_cut/cutlist.json → <root>/05_ir/wordline.json)。

    cutlist 不在标准工程布局里(无父父目录或无 05_ir)时返回 None,调用方跳过同步。
    """
    root = cutlist_path.parent.parent
    cand = root / "05_ir" / "wordline.json"
    return cand if cand.parent.is_dir() else None


# ---------------------------------------------------------------- I2:按文本裁片(v0.12)

def cut_from_text(wl: dict, quote: str) -> dict:
    """`--from-text` 正向入口:用户说「我只要『第三步』到『第四步』」时不再手算时间。

    在 wordline 内容串上**顺序锚定**引文(rs_common.anchor_span,与字幕 override /
    rs_ir --from-cards 同一实现)→ keep = 引文区间;区间外两刀 reason=manual 走
    **现有 guard**(宁可漏删:guard 不过自动降级 review,绝不硬切)。
    cutlist.fromText 留痕(引文/chars 区间/毫秒区间),下游可审计。
    """
    chars = wl.get("chars", [])
    if not chars:
        raise ValueError("wordline 没有 chars,无法按文本裁片")
    s, idx = rs_common.content_index(chars)
    ca, cb = rs_common.anchor_span(s, idx, quote)
    a_ms = int(chars[ca]["startMs"])
    b_ms = int(chars[cb - 1]["endMs"])
    total = int(wl.get("srcDurationMs") or int(chars[-1]["endMs"]))
    cuts = []
    if a_ms > 0:
        # 出点向前借 TAIL_KEEP_MS 作释放余量,但**绝不越过前一字 endMs**(wordClipped
        # 是 guard 底线,永不放松):字间 gap ≥60ms → 出点落 gap 内直达 remove;
        # gap 不足 → tailKeep 不过自动降级 review(宁可漏删,不硬切)。
        prev_end = int(chars[ca - 1]["endMs"]) if ca > 0 else 0
        cuts.append({"inMs": 0, "outMs": max(prev_end, a_ms - TAIL_KEEP_MS), "reason": "manual",
                     "conf": 0.95, "note": f"from-text:引文之前(锚「{quote[:10]}…」)"})
    if b_ms < total:
        cuts.append({"inMs": b_ms, "outMs": total, "reason": "manual", "conf": 0.95,
                     "note": f"from-text:引文之后(锚至「…{quote[-10:]}」)"})
    cl = build_cutlist(wl, cuts, {"mode": "from-text"})
    cl["fromText"] = {"quote": quote, "charsSpan": [ca, cb], "ms": [a_ms, b_ms]}
    return cl


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
    # v0.11 R5 删改稿:全文按 keep/remove/review 分行 —— 人读稿精修替代逐条听审
    script = cl.get("script") or []
    if script:
        lines += ["## 删改稿(R5:按文本审,不改时间)", "",
                  "> ~~删除线~~ = 自动执行 remove;**加粗** = 待审 review;正文 = 保留。", ""]
        for seg in script:
            t = seg["text"]
            if seg["action"] == "remove":
                lines.append(f"- ~~{t}~~")
            elif seg["action"] == "review":
                lines.append(f"- **{t}**  ←待审")
            else:
                lines.append(f"- {t}")
        lines.append("")
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
    # J2:protect 是最后一道闸 —— 人工把 review 改成 remove 也逃不过;侵入即报错
    _assert_no_protect_intrusion(cl.get("cuts", []), cl.get("protect"), actions=("remove",))
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
    ap.add_argument("--wordline", dest="wordline_path", default=None,
                    help="P27-1:--apply 时要同步时长账的 wordline 路径"
                         "(缺省自动发现 <cutlist>/../05_ir/wordline.json)")
    ap.add_argument("--force", dest="force", action="store_true",
                    help="P27-1:wordline 带手工编辑痕迹(manualEdit)时仍强制同步")
    ap.add_argument("--tail-reserve-ms", dest="tail_reserve_ms", type=int, default=TAIL_RESERVE_MS,
                    help="P26-3 口播尾余量 ms(默认 650;建议区间 500–800;"
                         "keep 末段终点保底 = max(末字 endMs+余量, 实测时长))")
    ap.add_argument("--protect", dest="protect", default="",
                    help="J2 保护区:「起ms-止ms,起ms-止ms」(与 keep 同单位,ms)——"
                         "被明确「必须保留发音」的区间;切点(remove)侵入即报错而非降级"
                         "(优先级高于 guard 三项,rules/roughcut.md §5.2)")
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--off-topic", dest="off_topic", default="",
                    help="Agent 判定的跑题段落,格式 '起-止,起-止'(chars 下标)")
    ap.add_argument("--min-silence-ms", type=int, default=SILENCE_MIN_MS)
    ap.add_argument("--media", help="源素材路径(给 dead_air 做音频能量探测;不给则退回字间 gap)")
    ap.add_argument("--retake-ratio", dest="retake_ratio", type=float, default=0.80,
                    help="重录相似度阈值(口播 0.80;怕误删的类型可提到 0.86)")
    ap.add_argument("--from-text", dest="from_text", default="",
                    help="I2 按文本裁片:只保留引文区间(顺序锚定 wordline),"
                         "如 --from-text \"第三步……第四步\";区间外走 guard 可降级 review")
    ap.add_argument("--from-text-file", dest="from_text_file", default="",
                    help="同 --from-text,引文从文件读(UTF-8,支持 # 注释行)")
    a = ap.parse_args()

    if a.from_text or a.from_text_file:
        if not a.wordline:
            return emit(False, "NO_INPUT", "--from-text 需要 <wordline.json>", exit_code=2)
        quote = a.from_text
        if a.from_text_file:
            tf = Path(a.from_text_file)
            if not tf.is_file():
                return emit(False, "NO_QUOTE_FILE", f"引文文件不存在:{tf}", exit_code=2)
            lines = [ln for ln in tf.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
            quote = " ".join(lines).strip()
        if not quote.strip():
            return emit(False, "NO_QUOTE", "引文为空", exit_code=2)
        wl = json.loads(Path(a.wordline).read_text(encoding="utf-8"))
        try:
            cl = cut_from_text(wl, quote)
        except ValueError as exc:
            return emit(False, "ANCHOR_FAIL", str(exc), exit_code=2)
        outdir = Path(a.out)
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "cutlist.json").write_text(json.dumps(cl, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
        write_report(cl, outdir / "cut_report.md")
        keep_ms = sum(b - x for x, b in cl["keep"])
        return emit(True, "CUT_FROM_TEXT",
                    f"引文锚定 chars{cl['fromText']['charsSpan']} → "
                    f"保留 {keep_ms / 1000:.1f}s / {len(cl['keep'])} 段;"
                    f"引文外 {len([c for c in cl['cuts'] if c['action'] == 'remove'])} 刀自动删、"
                    f"{len([c for c in cl['cuts'] if c['action'] == 'review'])} 刀待审",
                    {"cutlist": str(outdir / "cutlist.json"),
                     "report": str(outdir / "cut_report.md"), **cl["fromText"],
                     "keep": cl["keep"], "removedMs": cl["removedMs"]})

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
        # J2:protect 冲突在此显式报错(不用等 finalize 的 ValueError 才露面),
        # 让错误码/清单直接可读;此时 review = 不删,故只审真正会执行的 remove;
        # finalize 内还有同闸兜底防程序化调用绕过。
        bad_protect = protect_violations(cl["cuts"], cl.get("protect"), actions=("remove",))
        if bad_protect:
            return emit(False, PROTECT_CODE,
                        "切点侵入 protect 区(--apply 拒绝;改刀或撤区,冲突即报错而非降级):"
                        + "；".join(bad_protect),
                        {"violations": bad_protect}, exit_code=2)
        try:
            finalize_cutlist(cl)
        except ValueError as exc:
            return emit(False, "KEEP_INCONSISTENT", str(exc), exit_code=4)
        dst = p.with_name("cutlist.applied.json")
        dst.write_text(json.dumps(cl, ensure_ascii=False, indent=1), encoding="utf-8")
        # P27-1:--apply 是时长账同步的单一入口——改了 keep 边界,wordline 的
        # srcDurationMs/removedMs/finalDurationMs 在此自动改平,不再需要手改两字段
        # (20260920 教训:不同步则 rs_sync 总时长断言事后挂红叉)。
        sync_info: dict = {"wordline": None}
        wl_path = Path(a.wordline_path) if a.wordline_path else \
            self_wordline_default(p)
        if wl_path and wl_path.is_file():
            wl = json.loads(wl_path.read_text(encoding="utf-8"))
            if wl.get("manualEdit") and not a.force:
                return emit(False, "WORDLINE_MANUAL_EDIT",
                            f"{wl_path} 带手工编辑痕迹(manualEdit),拒绝自动改写时长账;"
                            "确认放弃手改请加 --force,或只改时长用 "
                            "rs_align.py refresh-durations <wordline> --media <素材>", exit_code=2)
            before = {"srcDurationMs": wl.get("srcDurationMs"),
                      "removedMs": wl.get("removedMs"),
                      "finalDurationMs": wl.get("finalDurationMs")}
            from rs_common import duration_ledger_error, sync_wordline_durations
            wl2 = sync_wordline_durations(wl, int(cl["srcTotalMs"]), int(cl["removedMs"]))
            ledger = duration_ledger_error(wl2)
            if ledger:
                return emit(False, "DURATION_LEDGER", f"同步后校验失败:{ledger}", exit_code=4)
            wl_path.write_text(json.dumps(wl2, ensure_ascii=False, indent=1), encoding="utf-8")
            sync_info = {"wordline": str(wl_path), "before": before,
                         "after": {"srcDurationMs": wl2["srcDurationMs"],
                                   "removedMs": wl2["removedMs"],
                                   "finalDurationMs": wl2["finalDurationMs"]},
                         "changed": before != {"srcDurationMs": wl2["srcDurationMs"],
                                               "removedMs": wl2["removedMs"],
                                               "finalDurationMs": wl2["finalDurationMs"]}}
        elif wl_path:
            sync_info = {"wordline": None, "note": f"未找到 wordline({wl_path}),时长账未同步"}
        msg = (f"已应用:{len([c for c in cl['cuts'] if c['action'] == 'remove'])} 刀 remove,"
               f"保留 {len(cl['keep'])} 段 / {(cl['srcTotalMs'] - cl['removedMs']) / 1000:.1f}s")
        if sync_info.get("wordline"):
            msg += f";wordline 时长账已同步({sync_info['wordline']})"
        if a.render:
            msg += ";下一步:rs_align.py remap <wordline> --cutlist " + str(dst)
        return emit(True, "CUT_APPLIED", msg, {"path": str(dst), "keep": cl["keep"],
                                               "removedMs": cl["removedMs"],
                                               "srcTotalMs": cl["srcTotalMs"],
                                               "wordlineSync": sync_info})

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

    try:
        protects = normalize_protect(a.protect)          # J2:先验区间形状,坏区间快速失败
    except ValueError as exc:
        return emit(False, "BAD_PROTECT", str(exc), exit_code=2)
    params = {"silenceMinMs": a.min_silence_ms, "tailKeepMs": TAIL_KEEP_MS,
              "retakeRatio": a.retake_ratio, "confRemove": CONF_REMOVE,
              "confReview": CONF_REVIEW,
              "tailReserveMs": a.tail_reserve_ms,
              "recordedIsMeasured": wl.get("durationProvenance") == "ffprobe",
              "protect": protects}
    # P26-3:给了 --media 就顺手 ffprobe 实测,keep 末段终点保底以实测为唯一真相
    if a.media and Path(a.media).is_file():
        measured = probe_duration_ms(a.media)
        if measured:
            params["measuredMs"] = measured
        else:
            params["measuredProbeFailed"] = True
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
    if cl.get("tail"):
        t = cl["tail"]
        msg += (f";keep 末段保底至 {t['keepEndMs']}ms"
                f"(实测 {t['measuredMs']} / 记录 {t['recordedTotalMs']},尾余量 {t['reserveMs']}ms)")
    return emit(True, "CUT_OK", msg, {"cutlist": str(outdir / "cutlist.json"),
                                      "report": str(outdir / "cut_report.md"),
                                      "detectors": counts, "remove": len(removes),
                                      "review": len(reviews), "removedMs": cl["removedMs"],
                                      "srcTotalMs": cl["srcTotalMs"], "keep": cl["keep"],
                                      "tail": cl.get("tail")})


if __name__ == "__main__":
    sys.exit(main())

