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
MIN_COVERAGE = 0.99       # 门禁:字覆盖率
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
                              "srcStartMs": a, "srcEndMs": max(b, a + 20), "conf": 0.40})
                idxs.append(len(chars) - 1)
            if text:
                degrade_reasons.append("字级时间戳缺失:句内按均分估算")

        punc = ""
        for ch in reversed(text):
            if ch in "。！？；，、：!?;:,…":
                punc = ch
                break
        sentences.append({"id": si, "span": [idxs[0], idxs[-1] + 1] if idxs else [0, 0],
                          "punc": punc, "text": text})

    hard = [c for c in chars if c["ch"].strip()]
    confs = [c["conf"] for c in hard] or [0.0]
    src_dur = max((c["srcEndMs"] for c in chars), default=0)
    covered = sum(c["srcEndMs"] - c["srcStartMs"] for c in hard)
    coverage = (covered / src_dur) if src_dur else 0.0

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


# ---------------------------------------------------------------- 入口

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
    ap.add_argument("command", nargs="?", default="build", choices=["build", "remap"])
    ap.add_argument("source", nargs="?", help="remap 时为 wordline 路径")
    ap.add_argument("--from-transcript")
    ap.add_argument("--from-tts")
    ap.add_argument("--media")
    ap.add_argument("--backend", default="auto", choices=["auto", "pkg", "onnx", "server"],
                    help="自带 ASR 后端;auto=精度优先(pkg→onnx→server)")
    ap.add_argument("--max-end-sil", dest="max_end_sil", type=int, default=0,
                    help="VAD 静音切分阈值 ms(0=用工具默认 400)")
    ap.add_argument("--cutlist")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=int, default=30)
    a = ap.parse_args()
    out = Path(a.out)

    if a.command == "remap":
        if not a.source or not a.cutlist:
            return emit(False, "NO_INPUT", "remap 需要:<wordline.json> --cutlist <cutlist.json>", exit_code=2)
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
        segments, source = _load_segments(p), str(p)
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

    doc = build_wordline(segments, source, fps=a.fps, degraded=degraded)
    doc["asr"] = asr_meta
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    st = doc["stats"]
    msg = (f"Wordline 已生成:{st['charCount']} 字,覆盖率 {st['coverage']:.1%},"
           f"conf 中位数 {st['confMedian']}")
    if asr_meta.get("backend"):
        msg = f"[ASR {asr_meta['backend']}] " + msg
    if doc["degraded"]:
        msg += f" ⚠ 降级模式:{';'.join(doc['degradeReasons'])}"
    return emit(True, "ALIGN_OK", msg, {"path": str(out), **st, "degraded": doc["degraded"],
                                        "degradeReasons": doc["degradeReasons"],
                                        "asr": asr_meta})


if __name__ == "__main__":
    sys.exit(main())
