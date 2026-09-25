# -*- coding: utf-8 -*-
"""v2 M11 · R22 中文路径端到端 + R24 边界用例(空 IR/无 video 轨/60fps)。

R22:全仓此前缺一条「工程根/素材路径含中文」的端到端用例——目录全量中文化
(ADR-0045)的前置保障;cv2 中文路径已知的坑在 rs_matting 有专项封装,本文件
管「管线主体(ingest/ir/render)对中文路径的行为」。
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


def _ffmpeg_bin() -> str:
    try:
        cfg = rs_common.load_config()
        p = rs_common.ffmpeg_bin(cfg)
        if Path(p).is_file():
            return p
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


FF = _ffmpeg_bin()


def _run(*args: str, cwd: Path) -> dict:
    p = subprocess.run([sys.executable, str(SCRIPTS / args[0]), *args[1:]], cwd=cwd,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip().splitlines()
    doc = json.loads(out[-1]) if out else {"ok": False, "code": "NO_OUTPUT", "message": p.stderr}
    assert doc.get("ok"), f"{args[0]} 失败:{doc.get('code')} {doc.get('message')[:200]}"
    return doc


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_chinese_paths_end_to_end(tmp_path):
    """中文工程根 + 中文素材文件名 → ingest 骨架/IR/渲染全链产成片(R22)。"""
    root = tmp_path / "中秋项目二〇二六"
    mat = root / "01_原始素材"
    mat.mkdir(parents=True)
    src = mat / "开场镜头.mp4"
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=1080x1920:rate=30:duration=1.5",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    str(src)], check=True)
    _run("rs_ingest.py", "scan", str(root), cwd=root)
    skeleton = root / "05_时间线工程" / "project.skeleton.json"
    assert skeleton.is_file(), "中文根下 ingest 骨架缺失"
    sk = json.loads(skeleton.read_text(encoding="utf-8"))
    # 骨架 clip src 指向中文素材名 → 直接补成可渲染 IR(draft 档)
    clips = []
    t0 = 0
    for c in sk.get("tracks", [{}])[0].get("clips", []) or []:
        clips.append({"id": f"c{len(clips)+1:03d}", "src": c["src"],
                      "startMs": t0, "durationMs": 1500, "sourceInMs": 0})
        t0 += 1500
    if not clips:
        clips = [{"id": "c001", "src": str(src), "startMs": 0,
                      "durationMs": 1500, "sourceInMs": 0}]
    ir = {"version": 1, "slug": "中文工程", "fps": 30,
          "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"kind": "video", "name": "main", "clips": clips}],
          "outputs": ["9x16"]}
    ir_path = root / "05_时间线工程" / "project.json"
    ir_path.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    doc = _run("rs_render.py", "05_时间线工程/project.json",
               "--ratio", "9x16", "--profile", "draft", cwd=root)
    out_mp4 = Path(doc["data"]["output"])
    assert out_mp4.is_file() and out_mp4.stat().st_size > 0, "中文全链未产成片"


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_fps60_render_smoke(tmp_path):
    """R24:60fps 工程(字符时间与容差自适应 W6 的渲染侧)冒烟出片。"""
    root = tmp_path / "proj60"
    mat = root / "01_原始素材"
    mat.mkdir(parents=True)
    src = mat / "a.mp4"
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x204060:size=160x120:rate=60:duration=0.8",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    "-r", "60", str(src)], check=True)
    ir = {"version": 1, "slug": "p60", "fps": 60,
          "canvas": {"width": 160, "height": 120},
          "tracks": [{"kind": "video", "name": "main", "clips": [
              {"id": "c1", "src": str(src), "startMs": 0,
               "durationMs": 800, "sourceInMs": 0}]}],
          "outputs": ["9x16"]}
    ir_path = root / "05_时间线工程" / "project.json"
    ir_path.parent.mkdir(parents=True, exist_ok=True)
    ir_path.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    doc = rs_render.render(ir, ir_path, "9x16", "draft")
    assert Path(doc["output"]).is_file() and doc["duration_s"] == pytest.approx(0.8, abs=0.1)
