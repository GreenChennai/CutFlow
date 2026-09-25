# -*- coding: utf-8 -*-
"""v2 M11 · R03/R23/R25/R26 回归:Logo 安全区单一化 + 错误分支 + 子进程超时。

R03:rs_brand.logo_rect / check_safe_area 与 rs_verify 同源(templates/platforms.json
safeArea)——小红书/B站变体此前用抖音口径会越过自家禁区且自家校验器判不出。
R23:rs_brand 的 NO_LOGO/NO_INPUT/LOGO_INVALID、rs_align ASR 空/坏输出分支。
R25/R26:run_verify 与 rs_align ASR 的超时路径(mock TimeoutExpired)。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_align  # noqa: E402
import rs_brand  # noqa: E402
import rs_run  # noqa: E402
import rs_subtitle  # noqa: E402


def _platform_entry(pid: str) -> dict:
    return (rs_subtitle.load_platforms() or {}).get(pid) or {}


# ---------------------------------------------------------------- R03 Logo 安全区

def test_logo_rect_xiaohongshu_not_over_bottom_band():
    """小红书 bottom=0.18:底部锚点 Logo 必须抬到 82% 线上方(旧硬编码 75% 会越界)。"""
    canvas = {"width": 1080, "height": 1920}
    logo = {"src": "x.png", "anchor": "bottomRight", "scale": 0.12,
            "_probe": {"aspect": 2.0, "content": {"aspect": 2.0}}}
    rect = rs_brand.logo_rect(logo, canvas, "9x16", platform="xiaohongshu")
    band = canvas["height"] - int(canvas["height"] * 0.18)
    assert rect["y"] + rect["h"] <= band, \
        f"小红书变体 Logo 越过自家字幕带:{rect}(band={band})"
    assert rect["safeAreaSource"] == "platforms.json:xiaohongshu"
    assert rect["lifted"] is True


def test_logo_rect_bilibili_uses_own_band():
    """B站 bottom=0.16:判据与抖音(0.25)必须不同 —— 双口径回归锚。"""
    canvas = {"width": 1920, "height": 1080}
    logo = {"src": "x.png", "anchor": "bottomRight", "scale": 0.12,
            "_probe": {"aspect": 2.0, "content": {"aspect": 2.0}}}
    r_bili = rs_brand.logo_rect(logo, canvas, "16x9", platform="bilibili")
    r_none = rs_brand.logo_rect(logo, canvas, "16x9", platform="")
    assert r_bili["y"] > r_none["y"], "B站字幕带(16%)应比保守带(25%)更靠下 → Logo 更低"
    assert r_none["safeAreaSource"] == "legacy-bands(平台未声明)"


def test_check_safe_area_same_source():
    """check_safe_area 与 logo_rect 同源:平台声明下不互相打架(R03 的『自家判不出』)。"""
    canvas = {"width": 1080, "height": 1920}
    logo = {"src": "x.png", "anchor": "bottomRight", "scale": 0.12,
            "_probe": {"aspect": 2.0, "content": {"aspect": 2.0}}}
    rect = rs_brand.logo_rect(logo, canvas, "9x16", platform="xiaohongshu")
    assert rs_brand.check_safe_area(rect, canvas, platform="xiaohongshu") == []


# ---------------------------------------------------------------- R23 错误分支

def _capture_argv(fn, argv):
    import contextlib, io
    old = sys.argv
    sys.argv = argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = fn()
    finally:
        sys.argv = old
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip().startswith("{")]
    return code, (json.loads(lines[-1]) if lines else {})


def test_brand_error_branches(tmp_path):
    """NO_LOGO / NO_INPUT / LOGO_INVALID 退出码与码(R23 抽样)。"""
    code, doc = _capture_argv(rs_brand.main,
                              ["rs_brand.py", "--expand", "--logos", "", "--out", str(tmp_path)])
    assert code == 2 and doc["code"] == "NO_LOGO"
    code, doc = _capture_argv(rs_brand.main,
                              ["rs_brand.py", "--expand", "--logos", "a",
                               "--ratios", "9x16", "--durations", "full"])
    assert code == 2 and doc["code"] == "NO_INPUT"


def test_brand_logo_invalid_nonexistent_src(tmp_path):
    """Logo src 不存在 → LOGO_INVALID(R23)。"""
    vp = tmp_path / "variants.json"
    vp.write_text(json.dumps({
        "version": 1,
        "logos": [{"id": "lg1", "src": str(tmp_path / "不存在.png"), "anchor": "topRight",
                   "scale": 0.12, "opacity": 0.9}],
        "ratios": ["9x16"], "durations": ["full"],
        "matrix": [{"id": "v1", "logo": "lg1", "ratio": "9x16", "duration": "full"}]},
        ensure_ascii=False), encoding="utf-8")
    ir = tmp_path / "project.json"
    ir.write_text(json.dumps({"version": 1, "slug": "p", "fps": 30,
                              "canvas": {"width": 1080, "height": 1920},
                              "tracks": [], "outputs": ["9x16"]}), encoding="utf-8")
    code, doc = _capture_argv(rs_brand.main, [
        "rs_brand.py", str(ir), "--variants", str(vp), "--out", str(tmp_path / "out"),
        "--profile", "draft"])
    # Logo src 不可探测 → 变体被跳过并留痕(BRAND_PARTIAL,exit 4;错误文本含路径)
    assert code == 4 and doc["code"] == "BRAND_PARTIAL", doc
    assert any("不存在" in e for e in doc["data"]["errors"]), doc


def test_align_asr_empty_output_branch(tmp_path, monkeypatch):
    """rs_align ASR 空输出 → ASR_NO_OUTPUT 结构化退出(R23)。"""
    class FakeP:
        returncode = 0
        stdout = ""
        stderr = "backend dead"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeP())
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    with pytest.raises(SystemExit) as ei:
        rs_align._from_media(media, tmp_path / "out.json")
    assert ei.value.code == 3


def test_align_asr_timeout_branch(tmp_path, monkeypatch):
    """R26:ASR 超时 → ASR_TIMEOUT 结构化退出(此前直调永久挂起)。"""
    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="fun_asr", timeout=3600)

    monkeypatch.setattr(subprocess, "run", _timeout)
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    with pytest.raises(SystemExit) as ei:
        rs_align._from_media(media, tmp_path / "out.json")
    assert ei.value.code == 3


def test_run_verify_timeout_branch(tmp_path, monkeypatch):
    """R25:run_verify 子进程超时 → 结构化失败(此前 --auto 可能永久挂起)。"""
    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="rs_verify", timeout=1800)

    monkeypatch.setattr(rs_run.subprocess, "run", _timeout)
    ok, msg, data = rs_run.run_verify(tmp_path, "L0")
    assert ok is False and "超时" in msg
