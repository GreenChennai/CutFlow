"""视频抽帧:均匀采样网格(旧档)+ 价值选帧(T3.9),供 Agent 目测。

用法:
  rs_frames.py <视频> --out <png> [--every 10] [--tile 3x3] [--scale 270:480]      # 旧档均匀
  rs_frames.py <视频> --out <png> --shots <shots.json> [--wordline <wordline.json>]
                [--vision <vision.json>] [--cap 0] [--tile 4x4]                    # 价值选帧

价值选帧(T3.9):抽帧点 = 镜头中点 + 运动峰值 + 字幕起点前 2 帧 + 人脸出现帧,
硬上限 min(24, 3+镜头数)(--cap 可显式覆盖,0 = 用默认上限)。同样预算下优先解释
「剪点/切镜/说话起点」;--shots 缺失时自动退回旧均匀档并在输出里留痕。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import die, emit, ffmpeg_bin, load_config, media_duration_s, run  # noqa: E402


def grid(video: Path, out_png: Path, every: float, tile: str, scale: str, cfg: dict) -> dict:
    dur = media_duration_s(video, cfg)
    if dur <= 0:
        die(4, "NO_DURATION", "无法读取视频时长")
    n_shots = max(1, min(24, int(dur // every)))
    cols, rows = tile.split("x")
    vf = (f"fps=1/{every},scale={scale},tile={cols}x{rows}")
    p = __import__("rs_common").run(
        [ffmpeg_bin(cfg), "-v", "error", "-i", str(video), "-vf", vf,
         "-frames:v", "1", "-y", str(out_png)], timeout=600)
    if p.returncode != 0 or not out_png.is_file():
        die(4, "FRAMES_FAIL", f"抽帧失败:{(p.stderr or '')[:300]}")
    return {"duration_s": round(dur, 2), "every_s": every, "grid": str(out_png),
            "mode": "uniform",
            "note": f"每格间隔 {every}s,从 0 开始;格子顺序=时间顺序"}


def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        die(2, "BAD_JSON", f"{p} 解析失败:{exc}")


def value_grid(video: Path, out_png: Path, shots_doc: dict, chars: list[dict],
               face_first: dict[int, int] | None, cap: int, tile: str,
               scale: str, cfg: dict) -> dict:
    """价值选帧网格(T3.9):多点定点抽取 → xstack 拼图(与 rs_bench 同引擎)。"""
    import rs_vision  # noqa: PLC0415 — 感知层共享库(第三册新建)
    from rs_bench import _xstack_layout
    shots = shots_doc.get("shots") or []
    if not shots:
        die(2, "NO_SHOTS", "shots.json 无镜头,价值选帧不可用")
    fps = 30.0
    dur = media_duration_s(video, cfg)
    pts = rs_vision.value_frames(shots, chars, fps=fps, face_first_ms=face_first, cap=cap)
    pts = [min(max(p, 0.0), max(dur - 0.05, 0.0)) for p in pts]
    n = len(pts)
    if n == 0:
        die(4, "NO_POINTS", "价值选帧产出 0 点(镜头数据异常)")
    cols, rows = tile.split("x")
    cmd = [ffmpeg_bin(cfg), "-v", "error", "-y"]
    for t in pts:
        cmd += ["-ss", f"{t:.3f}", "-i", str(video)]
    filters = [f"[{i}:v]scale={scale}[s{i}]" for i in range(n)]
    filters.append("".join(f"[s{i}]" for i in range(n))
                   + f"xstack=inputs={n}:layout=" + _xstack_layout(int(cols), int(rows)))
    cmd += ["-filter_complex", ";".join(filters), "-frames:v", "1", str(out_png)]
    p = run(cmd, timeout=600)
    if p.returncode != 0 or not out_png.is_file() or out_png.stat().st_size == 0:
        die(4, "FRAMES_FAIL", f"价值选帧拼图失败:{(p.stderr or '')[-300:]}")
    return {"duration_s": round(dur, 2), "grid": str(out_png), "mode": "value",
            "cap": cap, "points": pts, "n": n,
            "note": f"价值选帧 {n} 点(上限 {cap}):镜头中点/运动峰值/字幕前2帧/人脸帧;"
                    "格子顺序=时间顺序"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--every", type=float, default=10.0)
    ap.add_argument("--tile", default="3x3")
    ap.add_argument("--scale", default="270:480")
    ap.add_argument("--shots", default=None, help="shots.json(给出即走价值选帧,T3.9)")
    ap.add_argument("--wordline", default=None, help="wordline.json(字幕起点前 2 帧选点)")
    ap.add_argument("--vision", default=None, help="vision.json(人脸出现帧选点,L1)")
    ap.add_argument("--cap", type=int, default=0, help="选帧硬上限(0=min(24,3+镜头数))")
    a = ap.parse_args()
    video, out_png = Path(a.video), Path(a.out)
    if not video.is_file():
        return emit(False, "NO_MEDIA", f"视频不存在:{video}", exit_code=2)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    if a.shots:
        shots_doc = _load_json(Path(a.shots))
        chars: list[dict] = []
        if a.wordline and Path(a.wordline).is_file():
            wl = _load_json(Path(a.wordline))
            chars = [c for c in (wl.get("chars") or [])
                     if isinstance(c, dict) and c.get("startMs") is not None]
        face_first: dict[int, int] = {}
        if a.vision and Path(a.vision).is_file():
            vis = _load_json(Path(a.vision))
            for row in vis.get("shots") or []:
                ms = int(row.get("faceFirstMs") or -1)
                if ms > 0:
                    face_first[int(row["index"])] = ms
        cap = a.cap if a.cap > 0 else min(24, 3 + max(len(shots_doc.get("shots") or []), 1))
        data = value_grid(video, out_png, shots_doc, chars, face_first, cap,
                          a.tile, a.scale, cfg)
    else:
        data = grid(video, out_png, a.every, a.tile, a.scale, cfg)
        data["degraded"] = True
        data["note"] += ";未给 --shots,退回均匀抽帧(T3.9 价值选帧需 shots.json)"
    return emit(True, "FRAMES_OK", f"网格图已生成:{data['grid']}", data)


if __name__ == "__main__":
    sys.exit(main())
