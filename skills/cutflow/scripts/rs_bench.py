"""自评抽帧:渲染产物 → 关键点网格图(首2s/尾2s/剪点±1.5s/随机3点),供 Agent 目测。

用法:python rs_bench.py <成片.mp4> --ir <project.json> --out <png>
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import die, emit, ffmpeg_bin, ffprobe_json, load_config, media_duration_s, run  # noqa: E402


def video_stream_duration(video: Path, cfg: dict) -> float:
    """视频流时长(B6,BUGREPORT-20260913):容器 duration 常被音频垫尾撑长,
    按它布点会让尾部采样零帧 → xstack 空输出假成功。流 duration 缺失时退
    nb_frames/帧率,再退容器时长。"""
    info = ffprobe_json(video, cfg)
    vs = next((s for s in info.get("streams", []) if s["codec_type"] == "video"), None)
    if vs is None:
        die(4, "BENCH_NO_VIDEO", f"没有视频流:{video}")
    try:
        d = float(vs.get("duration") or 0)
        if d > 0:
            return d
    except (TypeError, ValueError):
        pass
    try:
        num, den = str(vs.get("r_frame_rate") or "0/1").split("/")
        nf = float(vs.get("nb_frames") or 0)
        if nf > 0 and float(num) > 0:
            return nf * float(den) / float(num)
    except (ValueError, ZeroDivisionError):
        pass
    return media_duration_s(video, cfg)


def sample_points(duration: float, ir: dict | None, seed: int = 7) -> list[float]:
    pts = [1.0, 2.0, duration - 2.0, duration - 1.0]
    if ir:
        cuts = [c["startMs"] / 1000 for t in ir.get("tracks", []) if t["kind"] == "video"
                for c in t["clips"][1:]]
        for c in cuts[:6]:
            pts += [max(0.5, c - 1.5), min(duration - 0.5, c + 1.5)]
    rng = random.Random(seed)
    pts += [rng.uniform(2, max(2.1, duration - 2)) for _ in range(3)]
    return sorted(p for p in pts if 0 <= p <= duration)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--ir", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cols", type=int, default=4)
    a = ap.parse_args()
    video = Path(a.video)
    if not video.is_file():
        return emit(False, "NO_MEDIA", f"成片不存在:{video}", exit_code=2)
    cfg = load_config()
    dur = video_stream_duration(video, cfg)
    ir = json.loads(Path(a.ir).read_text(encoding="utf-8")) if a.ir else None
    pts = sample_points(dur, ir)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [ffmpeg_bin(cfg), "-v", "error", "-y"]
    for i, t in enumerate(pts):
        cmd += ["-ss", f"{t:.2f}", "-i", str(video)]
    n = len(pts)
    cols = a.cols
    rows = (n + cols - 1) // cols
    filters = [f"[{i}:v]scale=270:-2[s{i}]" for i in range(n)]
    filters.append("".join(f"[s{i}]" for i in range(n)) + f"xstack=inputs={n}:layout=" + _xstack_layout(cols, rows))
    cmd += ["-filter_complex", ";".join(filters), "-frames:v", "1", str(out)]
    p = run(cmd, timeout=600)
    if p.returncode != 0:
        die(4, "BENCH_FAIL", f"抽帧失败:{(p.stderr or '')[-400:]}")
    # B6:rc=0 不代表成功 —— 尾部点无帧时 xstack 可能空输出假成功,必须验产物
    if not out.is_file() or out.stat().st_size == 0:
        die(4, "BENCH_EMPTY",
            f"抽帧空输出(视频流 {dur:.2f}s,采样点 {[round(t, 2) for t in pts]});"
            "疑似采样点越界或流异常,拒绝假成功(BUGREPORT B6)")
    return emit(True, "BENCH_OK", f"{n} 个采样点已拼图", {"grid": str(out), "duration_s": round(dur, 2),
                                                       "points": [round(t, 2) for t in pts]})


def _xstack_layout(cols: int, rows: int) -> str:
    """xstack 布局(等尺寸输入):x=前面各列宽度之和,y=上面各行高度之和。"""
    positions = []
    for r in range(rows):
        for c in range(cols):
            x = "0" if c == 0 else "+".join(f"w{i}" for i in range(c))
            y = "0" if r == 0 else "+".join(f"h{i}" for i in range(r))
            positions.append(f"{x}_{y}")
    return "|".join(positions)


if __name__ == "__main__":
    sys.exit(main())
