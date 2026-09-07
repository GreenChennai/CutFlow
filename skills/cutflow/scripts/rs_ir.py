"""IR 校验:schema + 语义(时间重叠/文件存在/枚举)。用法:python rs_ir.py validate <project.json>"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit  # noqa: E402

MOTION_IN = {"none", "fadeIn", "slideInLeft", "slideInRight", "scaleIn", "zoomIn"}
MOTION_OUT = {"none", "fadeOut", "slideOutLeft", "slideOutRight"}
TRANSITIONS = {"fade", "wipeleft", "wipeup", "slideleft", "circleopen"}
KINDS = {"video", "audio", "text"}


def validate(doc: dict, base_dir: Path) -> list[str]:
    errs: list[str] = []
    if doc.get("version") != 1:
        errs.append("version 必须为 1")
    canvas = doc.get("canvas", {})
    w, h = canvas.get("width"), canvas.get("height")
    if (w, h) not in ((1080, 1920), (1920, 1080)):
        errs.append(f"canvas 非法:{w}x{h}(仅 1080x1920 / 1920x1080)")
    if doc.get("fps") not in (24, 25, 30, 50, 60):
        errs.append(f"fps 非法:{doc.get('fps')}")

    video_tracks = [t for t in doc.get("tracks", []) if t.get("kind") == "video"]
    if not video_tracks:
        errs.append("至少需要一个 video 轨")

    for ti, track in enumerate(doc.get("tracks", [])):
        kind = track.get("kind")
        if kind not in KINDS:
            errs.append(f"tracks[{ti}].kind 非法:{kind}")
        spans = []
        for ci, clip in enumerate(track.get("clips", [])):
            where = f"tracks[{ti}].clips[{ci}]"
            start, dur = clip.get("startMs"), clip.get("durationMs")
            if not isinstance(start, (int, float)) or start < 0:
                errs.append(f"{where}.startMs 非法:{start}")
                continue
            if not isinstance(dur, (int, float)) or dur <= 0:
                if kind == "video":
                    errs.append(f"{where}.durationMs 非法:{dur}(video 轨必填)")
                    continue
                dur = None  # audio/text 缺省 = 素材全长
            if dur is not None:
                spans.append((start, start + dur))
            if clip.get("src") and kind != "text":
                if clip["src"].startswith("assets_sfx:"):  # 内置音效库伪协议
                    from rs_common import REPO_ROOT
                    exists = (REPO_ROOT / "assets" / "sfx" / (clip["src"].split(":", 1)[1] + ".mp3")).is_file()
                else:
                    pp = Path(clip["src"])
                    exists = (pp if pp.is_absolute() else base_dir / pp).is_file()
                if not exists:
                    errs.append(f"{where}.src 不存在:{clip['src']}")
            motion = clip.get("motion", {})
            if motion.get("in", "none") not in MOTION_IN:
                errs.append(f"{where}.motion.in 非法:{motion.get('in')}")
            if motion.get("out", "none") not in MOTION_OUT:
                errs.append(f"{where}.motion.out 非法:{motion.get('out')}")
            tr = clip.get("transition")
            if tr and tr.get("type") not in TRANSITIONS:
                errs.append(f"{where}.transition.type 非法:{tr.get('type')}")
        spans.sort()
        for a, b in zip(spans, spans[1:]):
            if b[0] < a[1] - 1:
                errs.append(f"tracks[{ti}] 时间重叠:{a} 与 {b}(转场重叠应 ≤1ms 容差)")

    for ti, track in enumerate(doc.get("tracks", [])):
        for ci, clip in enumerate(track.get("clips", [])):
            if track.get("kind") == "text" and not clip.get("text"):
                errs.append(f"tracks[{ti}].clips[{ci}] text 轨缺 text 字段")

    sub = doc.get("subtitle", {})
    if sub.get("ass") and not Path(sub["ass"]).is_absolute() and not (base_dir / sub["ass"]).is_file():
        errs.append(f"subtitle.ass 不存在:{sub['ass']}")
    elif sub.get("ass") and Path(sub["ass"]).is_absolute() and not Path(sub["ass"]).is_file():
        errs.append(f"subtitle.ass 不存在:{sub['ass']}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["validate"])
    ap.add_argument("project")
    a = ap.parse_args()
    p = Path(a.project)
    if not p.is_file():
        return emit(False, "NO_PROJECT", f"IR 文件不存在:{p}", exit_code=2)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return emit(False, "BAD_JSON", f"JSON 解析失败:{exc}", exit_code=2)
    errs = validate(doc, p.parent.parent)  # IR 在 05_ir/ 下,相对路径以工程根为基准
    if errs:
        return emit(False, "IR_INVALID", f"{len(errs)} 个问题(hint:逐条修复后重跑)", {"errors": errs}, exit_code=2)
    return emit(True, "IR_VALID", "IR 校验通过",
                {"slug": doc.get("slug"), "canvas": doc.get("canvas"),
                 "tracks": len(doc.get("tracks", []))})


if __name__ == "__main__":
    sys.exit(main())
