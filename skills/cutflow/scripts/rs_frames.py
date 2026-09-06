"""视频抽帧:均匀采样 + 网格拼图,供 Agent 目测(内容理解/绿幕锚点/自评)。

用法:python rs_frames.py <视频> --out <png> [--every 10] [--tile 3x3] [--scale 270:480]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import die, emit, ffmpeg_bin, load_config, media_duration_s  # noqa: E402


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
            "note": f"每格间隔 {every}s,从 0 开始;格子顺序=时间顺序"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--every", type=float, default=10.0)
    ap.add_argument("--tile", default="3x3")
    ap.add_argument("--scale", default="270:480")
    a = ap.parse_args()
    video, out_png = Path(a.video), Path(a.out)
    if not video.is_file():
        return emit(False, "NO_MEDIA", f"视频不存在:{video}", exit_code=2)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    data = grid(video, out_png, a.every, a.tile, a.scale, load_config())
    return emit(True, "FRAMES_OK", f"网格图已生成:{data['grid']}", data)


if __name__ == "__main__":
    sys.exit(main())
