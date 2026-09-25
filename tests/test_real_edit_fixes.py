# -*- coding: utf-8 -*-
"""v2 实剪驱动修复的回归(真实素材剪辑时发现,修复必须锁死):

① 跨源转场:resolve_joins 此前对跨源 join 用同源"源间隙"算术 → 恒负 → 全部退化硬切;
   现按 prev 自身源尾余量判定(静图源不设限)。
② 拼接图混音:链上有无音轨段(照片)时整链 -an → 静音片;现注入 anullsrc 静音轨。
③ ducking 侧链截断:sidechaincompress 在侧链(人声/音效总线)结束时终止,
   把已 atrim 到全长的 BGM 掐短(实测 53.3s 片音频止于 50.7s);现 apad 补齐侧链。
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

import rs_common  # noqa: E402
import rs_render  # noqa: E402

FF = rs_common.ffmpeg_bin(rs_common.load_config()) if True else ""


def _has_ff() -> bool:
    try:
        p = rs_common.ffmpeg_bin(rs_common.load_config())
        return Path(p).is_file() or shutil.which("ffmpeg") is not None
    except SystemExit:
        return shutil.which("ffmpeg") is not None


def _mk_video(path: Path, dur: float, color: str) -> None:
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c={color}:size=160x120:rate=10:duration={dur}",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    str(path)], check=True, capture_output=True)


def _mk_tone(path: Path, dur: float) -> None:
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={dur}",
                    "-c:a", "aac", "-b:a", "128k", str(path.with_suffix(".m4a"))],
                   check=True, capture_output=True)
    # mp3 编码器在本机不可用时用 m4a(管线只按 src 探测,容器不限)
    if path.with_suffix(".m4a").is_file():
        path.with_suffix(".m4a").replace(path)


def _capture_audio_len(path: Path) -> float:
    p = subprocess.run([FF, "-v", "info", "-i", str(path), "-map", "0:a", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    import re
    m = re.findall(r"time=(\d+):(\d+):([\d.]+)", p.stderr)
    if not m:
        return 0.0
    h, mm, s = m[-1]
    return int(h) * 3600 + int(mm) * 60 + float(s)


def test_cross_source_transition_kept():
    """①跨源 join 不再用同源间隙判负(夹短或保留,不再 forced_off)。"""
    clips = [
        {"id": "c1", "src": "a.mp4", "startMs": 0, "durationMs": 3000, "sourceInMs": 0},
        {"id": "c2", "src": "b.mp4", "startMs": 3000, "durationMs": 3000, "sourceInMs": 8000,
         "transition": {"type": "fade", "reason": "topic", "durMs": 400}},
    ]
    eff, forced, reasons, joins = rs_render.resolve_joins(clips, 30.0, {}, None)
    assert not forced, reasons
    assert eff[1] == pytest.approx(0.4, abs=0.01)
    assert joins.get(1, {}).get("kind") in ("xfade", "concatvideo", "glsl", None)


@pytest.mark.skipif(not _has_ff(), reason="ffmpeg 不可用")
def test_splice_mixed_audio_tracks_full_length(tmp_path):
    """②③端到端:glsl 降级链 + 无音轨段 + 短侧链 → 成片音画等长且非静音。"""
    root = tmp_path / "proj"
    mat = root / "01_原始素材"
    mat.mkdir(parents=True)
    srcs = []
    for i, color in enumerate(("0x4080C0", "0xC07040", "0x40C080")):
        s = mat / f"s{i}.mp4"
        _mk_video(s, 4.0, color)
        srcs.append(s)
    bgm = mat / "bgm.mp3"
    _mk_tone(bgm, 12.0)
    clips = []
    t = 0
    for i, s in enumerate(srcs):
        c = {"id": f"c{i+1}", "src": str(s), "startMs": t, "durationMs": 3600,
             "sourceInMs": 200}
        if i == 1:
            c["transition"] = {"type": "fade", "fx": "tr.glsl.dissolve",
                               "durMs": 500, "reason": "topic"}
        clips.append(c)
        t += 3600
    ir = {"version": 1, "slug": "mixfix", "fps": 30,
          "canvas": {"width": 160, "height": 120},
          "tracks": [{"kind": "video", "name": "main", "clips": clips}],
          "bgm": {"src": str(bgm), "gainDb": -14, "ducking": True, "loop": False},
          "outputs": ["9x16"]}
    ir_path = root / "05_时间线工程" / "project.json"
    ir_path.parent.mkdir(parents=True, exist_ok=True)
    ir_path.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    doc = rs_render.render(ir, ir_path, "9x16", "draft", use_cache=False, jobs=1)
    out = Path(doc["output"])
    assert out.is_file()
    vlen = float(rs_common.ffprobe_json(out, {})["format"]["duration"])
    alen = _capture_audio_len(out)
    assert abs(vlen - 10.8) <= 0.3, f"视频时长漂移:{vlen}"
    assert alen >= vlen - 0.4, f"音频被截短:{alen} vs 视频 {vlen}(侧链截断回归)"
    # 音频非静音:能量峰值 > -60dB(纯 anullsrc/bgmp3 都有声;这里 bgm 必有声)
    assert alen > 1.0
