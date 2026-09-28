# -*- coding: utf-8 -*-
"""第三册 T3.9 · 价值选帧替换均匀抽帧。

判据:
  [数] 抽帧点 = 镜头中点 + 运动峰值 + 字幕起点前 2 帧 + 人脸出现帧;
  [数] 硬上限 min(24, 3+镜头数)(--cap 可显式收窄);
  [机] rs_frames / rs_bench 给 --shots 走价值档;缺席回退均匀/启发式并留痕。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_frames  # noqa: E402
import rs_vision  # noqa: E402


def _ffmpeg() -> str:
    try:
        rc = __import__("rs_common")
        p = rc.ffmpeg_bin(rc.load_config())
        if Path(p).is_file():
            return p
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


FF = _ffmpeg()


def _shots(n: int, span_ms: int = 4000, peak: bool = True) -> list[dict]:
    out = []
    for i in range(n):
        out.append({"index": i, "startMs": i * span_ms, "endMs": (i + 1) * span_ms,
                    "durMs": span_ms, "motionScore": 0.05, "brightness": 0.5,
                    "contrast": 0.2, "colorfulness": 0.5, "stability": 0.6,
                    "staticRatio": 0.3, "audioRms": 0.0, "speechRate": 2.0,
                    "motionPeakMs": (i * span_ms + span_ms - 1000) if peak else -1})
    return out


def _chars(n: int, span_ms: int = 4000, offset_ms: int = 500) -> list[dict]:
    return [{"startMs": i * span_ms + offset_ms, "endMs": i * span_ms + offset_ms + 200,
             "ch": "字"} for i in range(n)]


def test_value_frames_points_and_priority():
    """选点覆盖四类来源;中点每镜保底;30fps 下字幕前 2 帧 = 起点前 66.7ms。"""
    shots = _shots(3)
    chars = _chars(3)
    # cap 放宽到 9:3 中点 + 3 峰值 + 3 字幕点全装下(轮次优先级:中点→峰值→字幕)
    pts = rs_vision.value_frames(shots, chars, fps=30.0, cap=9)
    assert pts, "必须产出选点"
    # 中点:2000/6000/10000ms
    for mid in (2.0, 6.0, 10.0):
        assert any(abs(p - mid) < 0.25 for p in pts), f"缺镜中点 {mid}"
    # 运动峰值:3000/7000/11000ms
    for pk in (3.0, 7.0, 11.0):
        assert any(abs(p - pk) < 0.25 for p in pts), f"缺运动峰值 {pk}"
    # 字幕起点前 2 帧(66.7ms):约 0.433s
    assert any(abs(p - (0.5 - 2 / 30.0)) < 0.05 for p in pts), "缺字幕起点前 2 帧"
    # 人脸出现帧(vision.json faceFirstMs)
    pts2 = rs_vision.value_frames(shots, chars, fps=30.0, cap=12,
                                  face_first_ms={0: 1000, 1: 5000})
    assert any(abs(p - 1.0) < 0.25 for p in pts2), "缺人脸出现帧"


def test_value_frames_hard_cap():
    """硬上限 min(24, 3+镜头数);超限按优先级轮次填充即止。"""
    n = 40
    shots = _shots(n, span_ms=1000, peak=True)
    pts = rs_vision.value_frames(shots, _chars(n, span_ms=1000), fps=30.0)
    cap = min(24, 3 + n)
    assert len(pts) <= cap
    assert len(pts) > 0
    # 无峰值/无人脸/无台词:至少每镜中点全部保底(n=40 > cap=24 → 截到 cap)
    plain = _shots(40, peak=False)
    pts2 = rs_vision.value_frames(plain, [], fps=30.0)
    assert len(pts2) == min(24, 43)
    # 显式 --cap 收窄
    pts3 = rs_vision.value_frames(_shots(10), _chars(10), fps=30.0, cap=5)
    assert len(pts3) <= 5


def test_value_frames_near_dedupe_and_determinism():
    """0.25s 窗口内去重;同输入两次调用逐点一致(确定性)。"""
    shots = _shots(4)
    chars = _chars(4, offset_ms=10)     # 字幕起点紧贴镜起点 → 与中点相距远,与峰值近
    a = rs_vision.value_frames(shots, chars, fps=30.0)
    b = rs_vision.value_frames(shots, chars, fps=30.0)
    assert a == b
    for i in range(1, len(a)):
        assert a[i] - a[i - 1] >= 0.25, f"选点过密:{a[i - 1]} vs {a[i]}"


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_rs_frames_and_bench_value_modes(tmp_path):
    """rs_frames/rs_bench 给 --shots 走价值档;rs_frames 缺 --shots 回退均匀并留痕。"""
    tmp = tmp_path / "选帧工程"
    tmp.mkdir()
    media = tmp / "v.mp4"
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=160x120:rate=10:duration=20",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    str(media)], check=True)
    sp = tmp / "shots.json"
    sp.write_text(json.dumps({"schema": 2, "source": "v.mp4", "shots": _shots(5)},
                             ensure_ascii=False), encoding="utf-8")
    wl = tmp / "wordline.json"
    wl.write_text(json.dumps({"chars": _chars(5)}, ensure_ascii=False), encoding="utf-8")
    import rs_common
    cfg = rs_common.load_config()
    # frames 价值档(cap=6)
    data = rs_frames.value_grid(media, tmp / "grid.png", json.loads(sp.read_text(encoding="utf-8")),
                                _chars(5), {}, cap=6, tile="3x3", scale="120:72", cfg=cfg)
    assert data["mode"] == "value" and data["n"] <= 6 and (tmp / "grid.png").is_file()
    # frames 旧档回退留痕
    r = subprocess.run([sys.executable, str(SCRIPTS / "rs_frames.py"), str(media),
                        "--out", str(tmp / "grid2.png")], capture_output=True, text=True,
                       encoding="utf-8")
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["data"]["mode"] == "uniform" and out["data"].get("degraded") is True
    # bench 价值档
    r2 = subprocess.run([sys.executable, str(SCRIPTS / "rs_bench.py"), str(media),
                         "--shots", str(sp), "--wordline", str(wl),
                         "--out", str(tmp / "bench.png")], capture_output=True, text=True,
                        encoding="utf-8")
    out2 = json.loads(r2.stdout.strip().splitlines()[-1])
    assert out2["data"]["mode"] == "value"
    assert len(out2["data"]["points"]) <= min(24, 3 + 5)
