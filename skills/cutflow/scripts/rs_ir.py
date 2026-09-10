"""IR 校验与生成。
用法:python rs_ir.py validate <project.json>
      python rs_ir.py build --from-cutlist 04_cut/cutlist.applied.json --slug X --out 05_ir/project.json

build 把 CutList 的 keep 区间转成 IR 主轨——**消灭「Agent 手写毫秒」这一整类误差**(rules/compose.md)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit  # noqa: E402
from rs_align import keep_to_segments, map_src_to_final  # noqa: E402

CANVAS = {"9x16": {"width": 1080, "height": 1920}, "16x9": {"width": 1920, "height": 1080}}

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


def build_from_cutlist(cutlist: dict, *, slug: str, ratio: str = "9x16",
                       xfade_ms: int = 8, with_audio: bool = True) -> dict:
    """CutList(keep 区间)→ IR 主视频/音频轨(时间一律经 map_src_to_final 换算)。"""
    keep = [[int(a), int(b)] for a, b in (cutlist.get("keep") or [])]
    if not keep:
        raise ValueError("cutlist 缺少 keep 区间(先跑 rs_cut.py --apply)")
    src = cutlist.get("source") or "01_materials/"
    segs = keep_to_segments(keep)

    video, audio = [], []
    cursor = 0
    for i, (a, b) in enumerate(keep):
        dur = b - a
        clip = {"src": src, "startMs": cursor, "durationMs": dur, "sourceInMs": a}
        if i > 0 and xfade_ms > 0:
            clip["transition"] = {"type": "fade", "ms": int(xfade_ms)}
        video.append(clip)
        if with_audio:
            audio.append({"src": src, "startMs": cursor, "durationMs": dur,
                          "sourceInMs": a, "role": "voice"})
        cursor += dur

    return {
        "version": 1, "slug": slug, "fps": 30, "canvas": dict(CANVAS[ratio]),
        "tracks": [{"kind": "video", "clips": video},
                   {"kind": "audio", "clips": audio}],
        "subtitle": {"ass": "06_output/subtitles.ass", "source": "05_ir/wordline.json"},
        "outputs": [ratio],
        "_meta": {"generatedFrom": "cutlist", "keepSegments": len(keep),
                  "finalDurationMs": cursor,
                  "mappedVia": "rs_align.map_src_to_final",
                  "srcTotalMs": cutlist.get("srcTotalMs"),
                  "removedMs": cutlist.get("removedMs")},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["validate", "build"])
    ap.add_argument("project", nargs="?")
    ap.add_argument("--from-cutlist")
    ap.add_argument("--slug", default="project")
    ap.add_argument("--ratio", default="9x16", choices=["9x16", "16x9"])
    ap.add_argument("--xfade", type=int, default=8)
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()

    if a.command == "build":
        cl_path = Path(a.from_cutlist or "")
        if not cl_path.is_file():
            return emit(False, "NO_CUTLIST", f"cutlist 不存在:{cl_path}", exit_code=2)
        try:
            doc = build_from_cutlist(json.loads(cl_path.read_text(encoding="utf-8")),
                                     slug=a.slug, ratio=a.ratio, xfade_ms=a.xfade,
                                     with_audio=not a.no_audio)
        except (ValueError, KeyError) as exc:
            return emit(False, "BUILD_FAIL", f"生成失败:{exc}", exit_code=2)
        out = Path(a.out or "05_ir/project.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        errs = validate(doc, out.parent.parent if out.parent.name == "05_ir" else out.parent)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        msg = (f"IR 已生成:{doc['_meta']['keepSegments']} 段 / "
               f"{doc['_meta']['finalDurationMs'] / 1000:.1f}s")
        if errs:
            msg += f"(校验 {len(errs)} 个提示:src 文件可能尚未就位)"
        return emit(True, "IR_BUILT", msg, {"path": str(out), **doc["_meta"], "validateErrors": errs})

    p = Path(a.project or "")
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
