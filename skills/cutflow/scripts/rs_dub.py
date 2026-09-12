"""TTS 配音强制对齐(OPTIMIZATION-v7 #10)。

一句话:把"句级 + 句内估算"的 TTS 时间轴,换成**真实字级时间戳**。

用法:
  rs_dub.py align --wordline 05_ir/wordline.json --audio 03_assets/tts/all.wav [--out 06_output] [--write]
  rs_dub.py align --wordline 05_ir/wordline.json --from-asr 02_sensed/tts_asr.json [--write]

原理(与 ADR-0011 一致,不是新时间源):
  · 用自带 ASR 对**配音音频**转写,拿到真实字级时间戳(参考轴);
  · 用 `rs_align.retime_to_reference()` 把**目标文本**锚到参考轴上(equal 区间零漂移);
  · 输出每卡漂移报告;**ASR 拿不到字级时间戳时拒绝写回**,并给出修复提示(不静默降级)。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rs_align  # noqa: E402
import rs_common  # noqa: E402
from rs_common import emit, p95  # noqa: E402

DRIFT_WARN_MS = 300          # 首字漂移超过此值 → 该句标 needs_review


def _load_segments(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(doc, list):
        return doc
    return doc.get("segments") or doc.get("sentences") or []


def _asr_segments(audio: Path, backend: str) -> tuple[list[dict], dict]:
    """跑自带 ASR(tools/fun_asr.py)拿配音音频的转写(含字级时间戳,若后端支持)。"""
    cfg = rs_common.load_config()
    try:
        return rs_align._from_media(audio, cfg, backend, 0)          # noqa: SLF001 — 同仓复用
    except SystemExit:
        return [], {"asrDegraded": True, "asrDegradeReasons": ["自带 ASR 执行失败(见上一行输出)"]}


def drift_report(target: dict, new_chars: list[dict]) -> dict:
    """逐句首字漂移(真实 − 估计)。位置对齐:`new_chars[i]` ↔ `target.chars[i]`。"""
    chars = target.get("chars") or []
    rows, drifts = [], []
    for s in target.get("sentences") or []:
        a, b = s.get("span", [0, 0])
        a = max(0, min(int(a), len(chars) - 1))
        if b <= a or a >= len(new_chars):
            continue
        est, real = int(chars[a]["startMs"]), int(new_chars[a]["startMs"])
        d = real - est
        drifts.append(abs(d))
        rows.append({"sentence": (s.get("text") or "")[:16], "estStartMs": est,
                     "realStartMs": real, "driftMs": d,
                     "needsReview": abs(d) > DRIFT_WARN_MS})
    return {"rows": rows,
            "medianDriftMs": round(statistics.median(drifts), 1) if drifts else 0.0,
            "p95DriftMs": round(p95(drifts), 1),
            "needsReview": [r for r in rows if r["needsReview"]]}


def write_report(rep: dict, stats: dict, path: Path, written: bool = False) -> None:
    lines = ["# 配音强制对齐报告(rs_dub)", "",
             f"- 文本相似度:{stats.get('similarity')}",
             f"- 首字漂移中位数:{rep['medianDriftMs']} ms ｜ 95 分位:{rep['p95DriftMs']} ms",
             f"- 需人工确认(>{DRIFT_WARN_MS}ms):{len(rep['needsReview'])} 句", "",
             "| 句 | 估计起点 | 真实起点 | 漂移 | 判定 |", "|---|---|---|---|---|"]
    for r in rep["rows"]:
        lines.append(f"| {r['sentence']} | {r['estStartMs']} | {r['realStartMs']} | "
                     f"{r['driftMs']:+d} ms | {'⚠ 需确认' if r['needsReview'] else '✓'} |")
    tail = "> 已把真实字级时间戳写回 wordline(`charTimingEstimated=False`)。" if written else \
           "> **未写回**(本次没加 `--write`):上面只是漂移报告,wordline 仍是估算时间。"
    lines += ["", tail,
              "> 漂移大不代表错:它说明 TTS 实际语速与目标文本的句级估算不一致 —— 下游字幕改按真实时间走。", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="align", choices=["align"])
    ap.add_argument("--wordline", required=True, help="目标 wordline(通常是 TTS 句级 + 句内估算)")
    ap.add_argument("--audio", help="配音音频(整条);由自带 ASR 转写取真实字级时间戳")
    ap.add_argument("--from-asr", dest="from_asr", help="已有的 ASR 结果 json(离线/已验证)")
    ap.add_argument("--backend", default="auto", choices=["auto", "pkg", "onnx", "server"])
    ap.add_argument("--out", default="06_output")
    ap.add_argument("--write", action="store_true", help="把真实字级时间戳写回 --wordline")
    a = ap.parse_args()

    if not a.audio and not a.from_asr:
        return emit(False, "NO_SOURCE", "需要 --audio <配音音频> 或 --from-asr <json>", exit_code=2)
    wlp = Path(a.wordline)
    if not wlp.is_file():
        return emit(False, "NO_WORDLINE", f"wordline 不存在:{wlp}", exit_code=2)
    target = json.loads(wlp.read_text(encoding="utf-8"))

    asr_meta: dict = {}
    if a.from_asr:
        p = Path(a.from_asr)
        if not p.is_file():
            return emit(False, "NO_ASR", f"ASR 结果不存在:{p}", exit_code=2)
        segments, source = _load_segments(p), str(p)
    else:
        audio = Path(a.audio)
        if not audio.is_file():
            return emit(False, "NO_AUDIO", f"配音音频不存在:{audio}", exit_code=2)
        segments, asr_meta = _asr_segments(audio, a.backend)
        source = str(audio)
    if not segments:
        return emit(False, "ASR_FAILED",
                    "自带 ASR 未返回任何片段;先跑 `python tools/fun_asr.py --probe` 看后端状态",
                    {"asr": asr_meta}, exit_code=3)

    ref = rs_align.build_wordline(segments, source)
    if ref.get("charTimingEstimated"):
        return emit(False, "DUB_NO_WORD_TS",
                    "配音转写没有字级时间戳(onnx/server 后端固有限制),无法强制对齐;"
                    "改用 pkg 后端重转(`--backend pkg`),或先 `python tools/fetch_deps.py asr --pkg`",
                    {"degradeReasons": ref.get("degradeReasons")}, exit_code=3)

    text = "".join(c["ch"] for c in (target.get("chars") or []))
    if not text:
        return emit(False, "NO_TARGET_TEXT", "目标 wordline 没有 chars", exit_code=2)
    try:
        new_chars, stats = rs_align.retime_to_reference(ref["chars"], text)
    except ValueError as exc:
        return emit(False, "DUB_REJECT", str(exc), exit_code=2)

    rep = drift_report(target, new_chars)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    written = False
    write_report(rep, stats, out / "dub_report.md", written=a.write)
    if a.write:
        doc = dict(target)
        doc["chars"] = new_chars
        doc["sentences"] = rs_align._resplit_sentences(new_chars)     # noqa: SLF001 — 同仓复用
        doc["gaps"] = rs_align.compute_gaps(new_chars)
        doc["degraded"] = False
        doc["charTimingEstimated"] = False
        doc["degradeReasons"] = []
        doc["dub"] = {"similarity": stats.get("similarity"), **rep,
                      "source": source}
        wlp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        written = True

    msg = (f"配音对齐:相似度 {stats.get('similarity')},首字漂移中位数 {rep['medianDriftMs']}ms,"
           f"{len(rep['needsReview'])} 句需确认")
    if written:
        msg += " → 已写回字级时间戳"
    return emit(True, "DUB_OK", msg,
                {"similarity": stats.get("similarity"), "medianDriftMs": rep["medianDriftMs"],
                 "p95DriftMs": rep["p95DriftMs"], "needsReview": rep["needsReview"],
                 "written": written, "report": str(out / "dub_report.md")})


if __name__ == "__main__":
    rs_common.ensure_utf8()
    sys.exit(main())
