# -*- coding: utf-8 -*-
"""第三册 T3.5 · 安全区内容占位判据(SAFE_AREA_CONTENT)。

判据:
  [机] 对「人脸/内容恰在字幕带」的构造样例报红;对正常样例不误报;
  [机] 平台未声明 / 尚无成片 → skipped 显式留痕(ADR-0021,不误伤);
  [机] 判据注册进 L0_CHECKS,collect_l0 可跑通。
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

import rs_verify  # noqa: E402


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


def _mk_project(tmp: Path) -> Path:
    proj = tmp / "安全区工程"
    (proj / "05_时间线工程").mkdir(parents=True)
    (proj / "06_成片输出").mkdir(parents=True)
    (proj / "05_时间线工程" / "pipeline.json").write_text(
        json.dumps({"params": {"platform": "douyin"}}, ensure_ascii=False),
        encoding="utf-8")
    return proj


def _render(proj: Path, name: str, overlay_y: str, box: str, skin: bool) -> None:
    bg = "color=c=0x204060:size=270x480:rate=30:duration=2"
    fg = (f"color=c=0xF0BE96:size={box}:rate=30:duration=2" if skin
          else f"color=c=white:size={box}:rate=30:duration=2")
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i", bg,
                    "-f", "lavfi", "-i", fg, "-filter_complex",
                    f"[0:v][1:v]overlay=x=35:y={overlay_y}[v]",
                    "-map", "[v]", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                    "-preset", "ultrafast", str(proj / "06_成片输出" / name)],
                   check=True)


def _only_video(proj: Path, keep: str) -> None:
    out = proj / "06_成片输出"
    for p in out.iterdir():
        p.rename(proj.parent / p.name)
    (proj.parent / keep).rename(out / keep)


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_safe_area_content_red_for_face_in_subtitle_band(tmp_path):
    """构造样例:肤色块(人脸代理)恰在底 25% 字幕带 → 报红。"""
    proj = _mk_project(tmp_path)
    _render(proj, "bad.mp4", "370", "200x90", skin=True)
    r = rs_verify.check_safe_area_content(proj)
    assert r["ok"] is False, "内容恰在字幕带必须报红"
    assert r["bottomContentRatio"] >= rs_verify.SAFE_AREA_CONTENT_FRAMES
    assert any("SAFE_AREA_CONTENT" in v for v in r["violations"])


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_safe_area_content_green_for_normal_sample(tmp_path):
    """正常样例:主体在中部、带内纯净 → 不误报;顶部带同样绿。"""
    proj = _mk_project(tmp_path)
    _render(proj, "good.mp4", "160", "160x120", skin=True)
    r = rs_verify.check_safe_area_content(proj)
    assert r["ok"] is True, f"正常样例不得误报:{r.get('detail')}"
    assert r["bottomContentRatio"] < rs_verify.SAFE_AREA_CONTENT_FRAMES
    assert r["topContentRatio"] < rs_verify.SAFE_AREA_CONTENT_FRAMES


def test_safe_area_content_skips_without_platform_or_video(tmp_path):
    """平台未声明 / 无成片 → skipped 显式留痕(判据缺席不算失败也不算通过假绿)。"""
    proj = tmp_path / "无平台工程"
    (proj / "06_成片输出").mkdir(parents=True)
    r = rs_verify.check_safe_area_content(proj)
    assert r["ok"] is True and r.get("skipped")
    proj2 = tmp_path / "无成片工程"
    (proj2 / "05_时间线工程").mkdir(parents=True)
    (proj2 / "06_成片输出").mkdir(parents=True)
    (proj2 / "05_时间线工程" / "pipeline.json").write_text(
        json.dumps({"params": {"platform": "douyin"}}, ensure_ascii=False), encoding="utf-8")
    r2 = rs_verify.check_safe_area_content(proj2)
    assert r2["ok"] is True and "尚无成片" in r2["skipped"]


def test_safe_area_content_registered_in_l0():
    """判据已注册进 L0_CHECKS(机械自检全量名单)。"""
    assert rs_verify.check_safe_area_content in rs_verify.L0_CHECKS
