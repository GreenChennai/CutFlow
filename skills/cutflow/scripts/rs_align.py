"""S1 字级对齐:产出 wordline.json(全片时间的唯一真相源,ADR-0011)。

用法:
  rs_align.py build --from-transcript 02_sensed/transcript_corrected.json --out 05_ir/wordline.json
  rs_align.py build --from-tts 03_assets/tts/manifest.json --out 05_ir/wordline.json
  rs_align.py build --media 01_materials/a.mp4 --out 05_ir/wordline.json
  rs_align.py remap 05_ir/wordline.json --cutlist 04_cut/cutlist.json --out 05_ir/wordline.final.json

三条入口统一落到同一数据结构;取不到字级时间戳时降级为「句级均分」并**显式标注 degraded**。
本模块同时导出 map_src_to_final():全部下游唯一允许的时间换算函数。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import die, emit, load_config  # noqa: E402

VERSION = 1
GAP_MIN_MS = 150          # 相邻字间隔超过此值记为一个 gap(供断句用)
MIN_COVERAGE = 0.99       # 门禁:时间跨度覆盖率(见 time_coverage)


def time_coverage(chars: list[dict], src_dur: int) -> float:
    """时间跨度覆盖率 = (首字起点→末字终点) ÷ 转写声明区间。

    2026-09-11 dev-jj2815 实测修正:旧公式(字时长和 ÷ 末字时间)把词间停顿
    全算成"未覆盖"——真实字级时间戳必然 ~75%,降级均分反而 100%,语义颠倒。
    字间停顿是语音常态,不算未覆盖;该指标只应对 ASR 漏转写开头/结尾敏感。
    """
    hard = [c for c in chars if str(c.get("ch", "")).strip()]
    if not hard or src_dur <= 0:
        return 0.0
    span = max(c["srcEndMs"] for c in hard) - min(c["srcStartMs"] for c in hard)
    return round(max(0.0, min(1.0, span / src_dur)), 4)
MIN_CONF_MEDIAN = 0.8     # 门禁:conf 中位数


# ---------------------------------------------------------------- 时间换算

def keep_to_segments(keep: list[list[int]]) -> list[dict]:
    """keep 区间 → 分段线性映射段(供 map_src_to_final 使用)。"""
    segs, acc = [], 0
    for src_in, src_out in keep:
        segs.append({"srcIn": int(src_in), "srcOut": int(src_out), "finalIn": acc})
        acc += int(src_out) - int(src_in)
    return segs


def map_src_to_final(t_src_ms: float, segments: list[dict]) -> float:
    """源域时间 → 成片域时间。**全下游唯一允许的时间换算入口。**

    落在被删除区间的时刻 → 吸附到下一个 keep 区间起点(不插值穿透),保证单调不减。
    """
    if not segments:
        return float(t_src_ms)
    t = float(t_src_ms)
    if t <= segments[0]["srcIn"]:
        return float(segments[0]["finalIn"])
    for seg in segments:
        if seg["srcIn"] <= t < seg["srcOut"]:
            return float(seg["finalIn"] + (t - seg["srcIn"]))
    nxt = [s for s in segments if s["srcIn"] > t]
    if nxt:
        return float(nxt[0]["finalIn"])
    last = segments[-1]
    return float(last["finalIn"] + (last["srcOut"] - last["srcIn"]))


# ---------------------------------------------------------------- 构建

def _strip_speaker(text: str) -> str:
    import re
    return re.sub(r"^\s*说话人\d+\s*[:：]\s*", "", text or "").strip()


def build_wordline(segments: list[dict], source: str, *, fps: int = 30,
                   degraded: str | None = None, space: str = "source") -> dict:
    """segments: [{start, end, text, timestamp?}] → wordline。

    有 char-level timestamp(每字 [start_ms, end_ms])时用真值;否则句内均分并标 degraded。
    """
    chars: list[dict] = []
    sentences: list[dict] = []
    degrade_reasons = [degraded] if degraded else []
    char_timing_estimated = False     # 无字级时间戳 → 字时间是句内估算,不可当字级用

    for si, seg in enumerate(segments):
        raw_text = seg.get("text") or ""
        text = _strip_speaker(raw_text) if seg.get("strip_speaker", True) else raw_text
        try:
            st_ms = float(seg.get("start", seg.get("start_s", 0))) * 1000.0
            en_ms = float(seg.get("end", seg.get("end_s", 0)) or 0) * 1000.0
        except (TypeError, ValueError):
            st_ms, en_ms = 0.0, 0.0
        if en_ms <= st_ms:
            en_ms = st_ms + max(1000.0, 120 * len(text))

        ts = seg.get("timestamp") or seg.get("char_timestamps") or seg.get("chars")
        idxs: list[int] = []
        if isinstance(ts, list) and len(ts) == len(text) and text:
            for ch, pair in zip(text, ts):
                a, b = int(pair[0]), int(pair[1])
                chars.append({"i": len(chars), "ch": ch, "startMs": a, "endMs": max(b, a + 20),
                              "srcStartMs": a, "srcEndMs": max(b, a + 20),
                              "conf": round(float(seg.get("conf", 0.95)), 3)})
                idxs.append(len(chars) - 1)
        else:
            n = max(len(text), 1)
            span = en_ms - st_ms
            for k, ch in enumerate(text):
                a = int(round(st_ms + span * k / n))
                b = int(round(st_ms + span * (k + 1) / n))
                chars.append({"i": len(chars), "ch": ch, "startMs": a, "endMs": max(b, a + 20),
                              "srcStartMs": a, "srcEndMs": max(b, a + 20), "conf": 0.40,
                              "estimated": True})
                idxs.append(len(chars) - 1)
            if text:
                char_timing_estimated = True
                degrade_reasons.append("字级时间戳缺失:句内按均分估算(不可当字级用)")

        punc = ""
        for ch in reversed(text):
            if ch in "。！？；，、：!?;:,…":
                punc = ch
                break
        sentences.append({"id": si, "span": [idxs[0], idxs[-1] + 1] if idxs else [0, 0],
                          "punc": punc, "text": text})

    hard = [c for c in chars if c["ch"].strip()]
    confs = [c["conf"] for c in hard] or [0.0]
    seg_end = 0
    for s in segments:                        # 转写声明的时间轴终点(含结尾静音)
        try:
            seg_end = max(seg_end, int(round(float(s.get("end", s.get("end_s", 0)) or 0) * 1000)))
        except (TypeError, ValueError):
            pass
    src_dur = max(max((c["srcEndMs"] for c in chars), default=0), seg_end)
    coverage = time_coverage(hard, src_dur)

    return {
        "version": VERSION,
        "source": source,
        "space": space,
        "fps": fps,
        "chars": chars,
        "gaps": compute_gaps(chars),
        "sentences": sentences,
        "speakers": sorted({s.get("speaker") for s in segments if s.get("speaker")}),
        "srcDurationMs": src_dur,
        "finalDurationMs": src_dur,
        "stats": {"charCount": len(hard), "coverage": round(coverage, 4),
                  "confMedian": round(statistics.median(confs), 3)},
        "degraded": bool(degrade_reasons),
        "degradeReasons": sorted(set(degrade_reasons)),
        # 字级时间是"句内均分估算"而非真实时间戳 → 下游只能按句级用,不得细分到字
        "charTimingEstimated": char_timing_estimated,
    }


def compute_gaps(chars: list[dict], min_ms: int = GAP_MIN_MS) -> list[dict]:
    """相邻字间隔 ≥ min_ms 记为一个 gap(断句的候选边界来源之一)。"""
    gaps = []
    for a, b in zip(chars, chars[1:]):
        d = b["startMs"] - a["endMs"]
        if d >= min_ms:
            kind = "silence" if d >= 600 else "pause"
            gaps.append({"after": a["i"], "ms": int(d), "kind": kind})
    return gaps


# ---------------------------------------------------------------- 重映射

def remap_wordline(wl: dict, keep: list[list[int]], cutlist: dict | None = None) -> dict:
    """按 CutList 的 keep 区间把 wordline 从 source 域重映射到 final 域。"""
    segs = keep_to_segments(keep)
    out = json.loads(json.dumps(wl))
    for c in out["chars"]:
        c["srcStartMs"] = c["startMs"]
        c["srcEndMs"] = c["endMs"]
        s = map_src_to_final(c["startMs"], segs)
        e = map_src_to_final(c["endMs"], segs)
        c["startMs"], c["endMs"] = int(round(s)), int(round(max(e, s + 20)))
    # 保证单调
    prev = 0
    for c in out["chars"]:
        if c["startMs"] < prev:
            c["startMs"] = prev
        if c["endMs"] <= c["startMs"]:
            c["endMs"] = c["startMs"] + 20
        prev = c["startMs"]
    out["space"] = "final"
    out["finalDurationMs"] = sum(int(b) - int(a) for a, b in keep)
    out["gaps"] = compute_gaps(out["chars"])
    if cutlist:
        out["cutlist"] = cutlist.get("source") or cutlist.get("version")
    out["stats"] = dict(out.get("stats") or {}, monotonic=True,
                        finalDurationMs=out["finalDurationMs"])
    return out


# ---------------------------------------------------------------- 校对回灌(v0.6.0)

SENT_END = "。！？；!?"
SENT_ALL = SENT_END + "，、：,:…"
GAP_SENT_MS = 600          # 静音 gap 达到此值也切句
INSERT_CONF = 0.85         # 插入/替换字的置信度(低于真实锚点,高于模糊线)


def _spread(text: str, a: int, b: int, conf: float) -> list[dict]:
    """把一段文本摊到 [a, b] 毫秒内(锚定真实区间内部的局部摊派,非整段比例缩放)。"""
    n = max(len(text), 1)
    span = max(b - a, n)
    out = []
    for k, ch in enumerate(text):
        s = a + span * k // n
        e = a + span * (k + 1) // n
        out.append({"i": 0, "ch": ch, "startMs": int(s), "endMs": int(max(e, s + 1)),
                    "srcStartMs": int(s), "srcEndMs": int(max(e, s + 1)), "conf": conf})
    return out


def _apply_opcodes(ref: list[dict], new_text: str, sm, conf: float) -> tuple[list[dict], dict]:
    """把 `new_text` 的时间锚到 `ref` 上(retext 回灌 / 配音强制对齐共用的核心)。

    - equal 区间:原锚点原样保留(**时间零漂移**)
    - replace:新字在旧区间 [首字start, 末字end] 内均分(有真实锚点兜底)
    - delete:锚点一并删除
    - insert:在相邻两字的 [prev.end, next.start] 区间内均分;区间过窄则压缩单字时长,
      **绝不平移既有锚点**(保住与音频的真实对应)
    """
    durs = sorted(max(c["endMs"] - c["startMs"], 1) for c in ref)
    med = durs[len(durs) // 2]
    out: list[dict] = []
    stats = {"replace": 0, "delete": 0, "insert": 0, "editChars": 0}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(ref[i1:i2])
        elif tag == "delete":
            stats["delete"] += i2 - i1
            stats["editChars"] += i2 - i1
        elif tag == "replace":
            stats["replace"] += 1
            stats["editChars"] += (i2 - i1) + (j2 - j1)
            a = ref[i1]["startMs"]
            b = max(ref[i2 - 1]["endMs"], a + 40 * (j2 - j1))
            out.extend(_spread(new_text[j1:j2], a, b, conf))
        elif tag == "insert":
            stats["insert"] += 1
            stats["editChars"] += j2 - j1
            text = new_text[j1:j2]
            if out and i2 < len(ref):            # 中间插入:[prev.end, next.start]
                a = out[-1]["endMs"]
                b = ref[i2]["startMs"]
                if b < a:
                    b = a
                out.extend(_spread(text, a, b, conf))
            elif out:                            # 末尾插入
                a = out[-1]["endMs"]
                out.extend(_spread(text, a, a + med * len(text), conf))
            else:                                # 开头插入
                f = ref[i2] if i2 < len(ref) else ref[-1]
                a = max(0, f["startMs"] - med * len(text))
                out.extend(_spread(text, a, f["startMs"], conf))
    prev = 0
    for c in out:                                # 单调化(保险):起点不减,终点至少 +1ms
        if c["startMs"] < prev:
            c["startMs"] = prev
        if c["endMs"] <= c["startMs"]:
            c["endMs"] = c["startMs"] + 1
        prev = c["startMs"]
    for k, c in enumerate(out):
        c["i"] = k
    return out, stats


def retime_to_reference(ref_chars: list[dict], text: str, *, conf: float = INSERT_CONF,
                        min_ratio: float = 0.5) -> tuple[list[dict], dict]:
    """把 `text` 的每个字锚到 `ref_chars` 的**真实**字级时间戳上(TTS 配音强制对齐,#10)。

    与 `retext_wordline` 同一套 opcode 逻辑,区别只在方向:
    retext 是"拿校对稿去改已有锚点";本函数是"拿真实锚点去给目标文本定时间"。
    """
    from difflib import SequenceMatcher
    if not ref_chars:
        raise ValueError("参考时间轴没有 chars,无法对齐")
    orig = "".join(c["ch"] for c in ref_chars)
    sm = SequenceMatcher(None, orig, text, autojunk=False)
    if sm.ratio() < min_ratio:
        raise ValueError(f"配音转写与目标文本相似度过低({sm.ratio():.0%}),疑似拿错音频/文本")
    out, stats = _apply_opcodes(ref_chars, text, sm, conf)
    stats["similarity"] = round(sm.ratio(), 4)
    return out, stats


def retext_wordline(wl: dict, new_text: str) -> tuple[dict, dict]:
    """把校对后的文本回灌到 **source 域** wordline:改动锚定在字级时间戳上。

    逻辑见 `_apply_opcodes`;相似度 <0.5 直接拒绝(疑似拿错文件)。
    """
    from difflib import SequenceMatcher
    chars = wl.get("chars") or []
    if not chars:
        raise ValueError("wordline 没有 chars,无法回灌")
    orig = "".join(c["ch"] for c in chars)
    sm = SequenceMatcher(None, orig, new_text, autojunk=False)
    if sm.ratio() < 0.5:
        raise ValueError(f"校对稿与原稿相似度过低({sm.ratio():.0%}),疑似拿错文件")
    out, stats = _apply_opcodes(chars, new_text, sm, INSERT_CONF)

    doc = dict(wl)
    doc["chars"] = out
    doc["sentences"] = _resplit_sentences(out)
    doc["gaps"] = compute_gaps(out)
    hard = [c for c in out if c["ch"].strip()]
    src_dur = max(int(wl.get("srcDurationMs") or 0),
                  max((c["srcEndMs"] for c in out), default=0))
    import statistics
    doc["stats"] = {"charCount": len(hard),
                    "coverage": time_coverage(out, src_dur),
                    "confMedian": round(statistics.median([c["conf"] for c in hard]), 3)}
    doc["degraded"] = bool(wl.get("degraded"))
    doc["srcDurationMs"] = max(src_dur, out[-1]["endMs"] if out else 0)
    doc["retext"] = {"similarity": round(sm.ratio(), 4), **stats,
                     "chars": f"{len(orig)}->{len(new_text)}"}
    return doc, stats


def _resplit_sentences(chars: list[dict]) -> list[dict]:
    """校对后字数已变,旧 span 作废:按强标点/静音 gap 重切句(供 DP 断句用)。"""
    sents: list[dict] = []
    a = 0
    last_punc = ""
    for i, c in enumerate(chars):
        if c["ch"] in SENT_ALL:
            last_punc = c["ch"]
        nxt = chars[i + 1] if i + 1 < len(chars) else None
        gap = (nxt["startMs"] - c["endMs"]) if nxt else 0
        if nxt is None or c["ch"] in SENT_END or gap >= GAP_SENT_MS:
            text = "".join(x["ch"] for x in chars[a:i + 1])
            body = any(x["ch"] not in SENT_ALL for x in chars[a:i + 1])
            if body:
                sents.append({"id": len(sents), "span": [a, i + 1],
                              "punc": last_punc if last_punc in SENT_ALL else "", "text": text})
            elif sents:
                # dev-jj2815 实测:零宽继承时间的孤立标点(如"?")在 gap 切句后
                # 不得单独成句 —— 单标点句会让下游 DP 产卡缺 startMs,污染首卡
                sents[-1]["span"][1] = i + 1
                sents[-1]["text"] += text
                if last_punc in SENT_ALL:
                    sents[-1]["punc"] = last_punc
            a = i + 1
            last_punc = ""
    return sents



def _load_segments(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(doc, list):
        return doc
    return doc.get("segments") or doc.get("sentences") or []


def _from_media(media: Path, cfg: dict, backend: str = "auto",
                max_end_sil: int = 0) -> tuple[list[dict], dict]:
    """调 **CutFlow 自带** ASR(tools/fun_asr.py)取转写 —— 不需要任何外部服务器。

    返回值: (segments, meta),meta 含 backend / capabilities / degraded / degradeReasons。
    """
    import subprocess
    runner = Path(__file__).resolve().parents[3] / "tools" / "fun_asr.py"
    if not runner.is_file():
        die(3, "NO_ASR_RUNNER", f"找不到自带 ASR 运行器:{runner}")
    cmd = [sys.executable, str(runner), str(media), "--json", "--backend", backend]
    if max_end_sil:
        cmd += ["--max-end-sil", str(max_end_sil)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip()
    if not out:
        die(3, "ASR_NO_OUTPUT",
            f"自带 ASR 无输出(exit {p.returncode}):{(p.stderr or '')[-300:]}。"
            f"先跑 `python tools/fun_asr.py --probe` 看后端状态,"
            f"或用 `python tools/fetch_deps.py asr --onnx` 部署")
    try:
        doc = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        die(4, "ASR_BAD_JSON", f"自带 ASR 输出无法解析:{out[:200]}")
    if not doc.get("ok"):
        die(3, "ASR_FAILED", doc.get("message") or "自带 ASR 失败",
            {"code": doc.get("code"), "detail": doc.get("data")})
    data = doc.get("data") or {}
    meta = {"backend": data.get("backend"), "capabilities": data.get("capabilities") or {},
            "elapsedSec": data.get("elapsedSec"), "segmentCount": data.get("segmentCount"),
            "asrDegraded": bool(data.get("degraded")),
            "asrDegradeReasons": data.get("degradeReasons") or []}
    return data.get("segments") or [], meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="build", choices=["build", "remap", "retext"])
    ap.add_argument("source", nargs="?", help="remap/retext 时为 wordline 路径")
    ap.add_argument("--from-transcript")
    ap.add_argument("--from-tts")
    ap.add_argument("--media")
    ap.add_argument("--backend", default="auto", choices=["auto", "pkg", "onnx", "server"],
                    help="自带 ASR 后端;auto=精度优先(pkg→onnx→server)")
    ap.add_argument("--max-end-sil", dest="max_end_sil", type=int, default=0,
                    help="VAD 静音切分阈值 ms(0=用工具默认 400)")
    ap.add_argument("--cutlist")
    ap.add_argument("--src", help="覆盖 wordline.source;--from-transcript 时用它指向真实素材路径"
                                  "(否则下游 IR 会把转写稿当成视频源)")
    ap.add_argument("--text", help="retext:校对后的纯文本文件(标点可有可无)")
    ap.add_argument("--out", required=False)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="retext:只报告编辑摘要,不写文件")
    a = ap.parse_args()
    out = Path(a.out) if a.out else None

    if a.command == "retext":
        if not a.source or not a.text:
            return emit(False, "NO_INPUT", "retext 需要:<wordline.json> --text <校对稿.txt>", exit_code=2)
        wl = json.loads(Path(a.source).read_text(encoding="utf-8"))
        if wl.get("space") == "final":
            return emit(False, "NOT_SOURCE_SPACE",
                        "该 wordline 已在 final 域(粗剪后);请回灌到 source 域 wordline 后再重跑 remap", exit_code=2)
        new_text = Path(a.text).read_text(encoding="utf-8")
        new_text = " ".join(new_text.split())          # 压缩换行/多余空白
        try:
            doc, stats = retext_wordline(wl, new_text)
        except ValueError as exc:
            return emit(False, "RETEXT_REJECT", str(exc), exit_code=2)
        msg = (f"校对回灌:{stats['replace']} 处替换 / {stats['delete']} 字删除 / "
               f"{stats['insert']} 处插入,字符 {doc['retext']['chars']},"
               f"相似度 {doc['retext']['similarity']:.0%}(equal 区间时间零漂移)")
        if a.dry_run:
            return emit(True, "RETEXT_DRYRUN", msg + "(dry-run,未写盘)",
                        {"stats": stats, "charCount": doc["stats"]["charCount"],
                         "sentences": len(doc["sentences"])})
        out = out or Path(a.source)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return emit(True, "RETEXT_OK", f"{msg} → {out}",
                    {"path": str(out), "stats": stats, **doc["stats"],
                     "sentences": len(doc["sentences"]),
                     "hint": "下一步:rs_run --mark S1(记录新状态)→ --from S2 级联"})

    if a.command == "remap":
        if not a.source or not a.cutlist:
            return emit(False, "NO_INPUT", "remap 需要:<wordline.json> --cutlist <cutlist.json>", exit_code=2)
        if not a.out:
            return emit(False, "NO_INPUT", "remap 需要 --out", exit_code=2)
        wl = json.loads(Path(a.source).read_text(encoding="utf-8"))
        cut = json.loads(Path(a.cutlist).read_text(encoding="utf-8"))
        keep = cut.get("keep") or []
        if not keep:
            return emit(False, "NO_KEEP", "cutlist 缺少 keep 区间(先跑 rs_cut.py --apply)", exit_code=2)
        doc = remap_wordline(wl, keep, cut)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return emit(True, "REMAP_OK", f"重映射完成:{out}", {"space": doc["space"],
                    "finalDurationMs": doc["finalDurationMs"], "chars": len(doc["chars"])})

    source, segments, degraded = "", [], None
    asr_meta: dict = {}
    if a.from_transcript:
        p = Path(a.from_transcript)
        segments, source = _load_segments(p), (a.src or str(p))
    elif a.from_tts:
        p = Path(a.from_tts)
        man = json.loads(p.read_text(encoding="utf-8"))
        segments = [{"start": s["start_s"], "end": s["end_s"], "text": s["text"],
                     "timestamp": s.get("timestamp") or s.get("chars")}
                    for s in man.get("sentences", [])]
        source = str(p)
        degraded = "TTS 路径:句级时间 + 句内均分"
    elif a.media:
        media = Path(a.media)
        if not media.is_file():
            return emit(False, "NO_MEDIA", f"素材不存在:{media}", exit_code=2)
        cfg = load_config()
        source = str(media)
        segments, asr_meta = _from_media(media, cfg, a.backend, a.max_end_sil)
        if asr_meta.get("asrDegraded"):
            degraded = "；".join(asr_meta.get("asrDegradeReasons")
                                or ["自带 ASR 标注为降级"]) + \
                       f"(后端 {asr_meta.get('backend')})"
    else:
        return emit(False, "NO_INPUT", "需要 --from-transcript / --from-tts / --media 之一", exit_code=2)

    if not a.out:
        return emit(False, "NO_INPUT", "build 需要 --out", exit_code=2)

    doc = build_wordline(segments, source, fps=a.fps, degraded=degraded)
    doc["asr"] = asr_meta
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    st = doc["stats"]
    msg = (f"Wordline 已生成:{st['charCount']} 字,时间跨度覆盖率 {st['coverage']:.1%},"
           f"conf 中位数 {st['confMedian']}")
    if st["coverage"] < MIN_COVERAGE:
        msg += (f" ⚠ 覆盖率 {st['coverage']:.1%} < {MIN_COVERAGE:.0%}:"
                "检查 ASR 是否漏转写开头/结尾(字间停顿不算未覆盖)")
    if asr_meta.get("backend"):
        msg = f"[ASR {asr_meta['backend']}] " + msg
    if doc["degraded"]:
        msg += f" ⚠ 降级模式:{';'.join(doc['degradeReasons'])}"
    return emit(True, "ALIGN_OK", msg, {"path": str(out), **st, "degraded": doc["degraded"],
                                        "degradeReasons": doc["degradeReasons"],
                                        "asr": asr_meta})


if __name__ == "__main__":
    sys.exit(main())
