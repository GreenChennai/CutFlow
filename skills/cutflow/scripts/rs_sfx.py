"""S6 音效自动落点:把「手动挂音效」变成「Agent 出草案 + 人审」(rules/sfx.md)。

用法:
  rs_sfx.py 05_ir/project.json --auto --out 05_ir/sfx_draft.json
  rs_sfx.py 05_ir/project.json --apply 05_ir/sfx_draft.json [--write 05_ir/project.sfx.json]

落点规则:转场=切点/卡片切换;强调=关键词;列举=第一/第二/第三;章节=markers;结尾=末卡。
密度硬约束:**每 15s 内音效 ≤ 2 个**,超出者进 dropped(不静默丢弃)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import REPO_ROOT, emit  # noqa: E402

DENSITY_WINDOW_MS = 15000
DENSITY_MAX = 2
DEFAULT_GAIN_DB = -14
ENUM_WORDS = ("第一", "第二", "第三", "首先", "其次", "最后")
ENDS = "。！？…"
SFX_DIR = REPO_ROOT / "assets" / "sfx"


def from_transitions(ir: dict) -> list[dict]:
    out = []
    for t in ir.get("tracks", []):
        if t.get("kind") != "video":
            continue
        for i, c in enumerate(t.get("clips", [])):
            if i == 0:
                continue
            at = int(c.get("startMs", 0))
            kind = "whoosh" if i % 2 else "swipe"
            out.append({"atMs": at, "src": f"assets_sfx:{kind}", "trigger": "cut",
                        "conf": 0.9, "gainDb": DEFAULT_GAIN_DB,
                        "note": f"片段切换 @{t.get('name') or '主轨'}#{i}"})
    return out


def from_markers(ir: dict) -> list[dict]:
    out = []
    for m in ir.get("markers", []) or []:
        out.append({"atMs": int(m.get("atMs", 0)), "src": "assets_sfx:riser",
                    "trigger": "chapter", "conf": 0.85, "gainDb": DEFAULT_GAIN_DB,
                    "note": f"章节:{m.get('label') or m.get('title') or ''}"})
    return out


def from_enumeration(wordline: dict | None) -> list[dict]:
    if not wordline:
        return []
    chars = wordline.get("chars", [])
    text = "".join(c["ch"] for c in chars)
    out = []
    for w in ENUM_WORDS:
        start = 0
        while True:
            k = text.find(w, start)
            if k < 0:
                break
            out.append({"atMs": int(chars[k]["startMs"]), "src": "assets_sfx:click",
                        "trigger": "enumerate", "conf": 0.8, "gainDb": DEFAULT_GAIN_DB,
                        "anchorChar": k, "note": f"序号「{w}」"})
            start = k + 1
    return out


def from_ending(ir: dict, wordline: dict | None) -> list[dict]:
    total = 0
    for t in ir.get("tracks", []):
        if t.get("kind") == "video":
            for c in t.get("clips", []):
                total = max(total, int(c.get("startMs", 0)) + int(c.get("durationMs", 0)))
    if total:
        return [{"atMs": max(0, total - 800), "src": "assets_sfx:bell", "trigger": "ending",
                 "conf": 0.8, "gainDb": DEFAULT_GAIN_DB, "note": "片尾"}]
    return []


def anchor(wordline: dict | None, placement: dict) -> dict:
    """把落点吸附到最近的锚点字时间(rules/sfx.md §4:偏差 ≤1 帧)。"""
    if not wordline:
        return placement
    chars = wordline.get("chars", [])
    if not chars:
        return placement
    target = int(placement["atMs"])
    best, best_d = None, None
    for c in chars:
        d = abs(int(c["startMs"]) - target)
        if best_d is None or d < best_d:
            best, best_d = c, d
    if best is not None and best_d is not None and best_d <= 500:
        placement = dict(placement, atMs=int(best["startMs"]), anchorChar=best["i"])
    return placement


def enforce_density(placements: list[dict]) -> tuple[list[dict], list[dict]]:
    """每 15s 窗口内 ≤2 个;超出按 章节/转场 > 强调 > 列举 > 氛围 优先级保留。"""
    priority = {"chapter": 0, "cut": 1, "enumerate": 2, "ending": 0, "ambient": 4}
    ordered = sorted(placements, key=lambda p: (p["atMs"], priority.get(p["trigger"], 3)))
    kept: list[dict] = []
    dropped: list[dict] = []
    for p in ordered:
        window = [k for k in kept if p["atMs"] - DENSITY_WINDOW_MS < k["atMs"] <= p["atMs"]]
        if len(window) >= DENSITY_MAX:
            dropped.append({**p, "reason": f"{DENSITY_WINDOW_MS // 1000}s 窗口内已 {DENSITY_MAX} 个"})
        else:
            kept.append(p)
    return kept, dropped


def build_draft(ir: dict, wordline: dict | None) -> dict:
    raw: list[dict] = []
    raw += from_markers(ir)
    raw += from_transitions(ir)
    raw += from_enumeration(wordline)
    raw += from_ending(ir, wordline)
    raw = [anchor(wordline, p) for p in raw]
    kept, dropped = enforce_density(raw)
    missing = sorted({p["src"].split(":", 1)[1] for p in kept
                      if not (SFX_DIR / f"{p['src'].split(':', 1)[1]}.mp3").is_file()})
    return {"version": 1, "placements": kept, "dropped": dropped, "missingAssets": missing}


def apply_draft(ir: dict, draft: dict) -> dict:
    doc = json.loads(json.dumps(ir))
    audio = next((t for t in doc.get("tracks", []) if t.get("kind") == "audio"), None)
    if audio is None:
        audio = {"kind": "audio", "clips": []}
        doc.setdefault("tracks", []).append(audio)
    audio["clips"] = [c for c in audio.get("clips", []) if c.get("role") != "sfx"]
    for p in draft.get("placements", []):
        audio["clips"].append({"src": p["src"], "startMs": int(p["atMs"]), "role": "sfx",
                               "note": p.get("note", ""), "gainDb": p.get("gainDb", DEFAULT_GAIN_DB),
                               "trigger": p.get("trigger")})
    audio["clips"].sort(key=lambda c: int(c.get("startMs", 0)))
    return doc


def write_notes(draft: dict, outdir: Path) -> None:
    if not draft.get("dropped"):
        return
    lines = ["# 被丢弃的音效候选(密度约束)", "",
             f"规则:每 {DENSITY_WINDOW_MS // 1000}s 内音效 ≤ {DENSITY_MAX} 个。", "",
             "| atMs | src | trigger | 原因 |", "|---|---|---|---|"]
    for d in draft["dropped"]:
        lines.append(f"| {d['atMs']} | {d['src']} | {d.get('trigger', '')} | {d['reason']} |")
    (outdir / "sfx_dropped.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ir")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--apply")
    ap.add_argument("--write")
    ap.add_argument("--wordline")
    ap.add_argument("--out", default="05_ir/sfx_draft.json")
    a = ap.parse_args()
    ir = json.loads(Path(a.ir).read_text(encoding="utf-8"))

    if a.apply:
        draft = json.loads(Path(a.apply).read_text(encoding="utf-8"))
        doc = apply_draft(ir, draft)
        dst = Path(a.write or a.ir)
        dst.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        n = len([c for c in doc["tracks"][-1].get("clips", []) if c.get("role") == "sfx"])
        return emit(True, "SFX_APPLIED", f"已写入 {n} 条音效 → {dst}",
                    {"path": str(dst), "count": n})

    wl = json.loads(Path(a.wordline).read_text(encoding="utf-8")) if a.wordline else None
    draft = build_draft(ir, wl)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(draft, ensure_ascii=False, indent=1), encoding="utf-8")
    write_notes(draft, out.parent)
    msg = f"{len(draft['placements'])} 条音效落点(丢弃 {len(draft['dropped'])} 条)"
    if draft["missingAssets"]:
        msg += f";⚠ 缺失音色:{','.join(draft['missingAssets'])}"
    return emit(True, "SFX_DRAFT_OK", msg, {"path": str(out), **draft})


if __name__ == "__main__":
    sys.exit(main())
