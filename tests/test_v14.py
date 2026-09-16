r"""v0.14 测试:绿幕检测门禁(ADR-0031)+ 抠像功能移除。

运行: pytest tests/test_v14.py -q
纯标准库合成帧测试不需要 ffmpeg;端到端抽帧检测在有 ffmpeg 时运行,否则跳过。
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

import rs_greenscreen as gs  # noqa: E402
import rs_ingest  # noqa: E402
import rs_verify  # noqa: E402


def _ffmpeg() -> str | None:
    try:
        import rs_common
        cand = rs_common.ffmpeg_bin()
    except SystemExit:
        cand = "ffmpeg"
    return shutil.which(cand) or (cand if Path(cand).is_file() else None)


FFMPEG = _ffmpeg()
needs_ff = pytest.mark.skipif(not FFMPEG, reason="本机没有 ffmpeg,跳过")


# ---------------------------------------------------------------- 帧级判据(纯 Python)

def _frame(w: int, h: int, painter) -> bytes:
    buf = bytearray(w * h * 3)
    for y in range(h):
        for x in range(w):
            r, g, b = painter(x, y)
            i = (y * w + x) * 3
            buf[i], buf[i + 1], buf[i + 2] = r, g, b
    return bytes(buf)


def test_analyze_frame_green_screen_detected():
    w, h = 64, 36

    def painter(x, y):
        # 中间下方一块"人"——其余为均匀绿幕
        if 24 <= x < 40 and 20 <= y < 36:
            return (200, 120, 60)
        return (0, 220, 10)

    res = gs.analyze_frame(_frame(w, h, painter), w, h)
    assert res["borderGreen"] >= gs.BORDER_FRAC
    assert res["greenRatio"] >= gs.OVERALL_FRAC
    decided = gs._decide([res])
    assert decided["detected"] and decided["kind"] == "green"
    assert decided["confidence"] > 0.5


def test_analyze_frame_blue_screen_detected():
    w, h = 64, 36
    res = gs.analyze_frame(_frame(w, h, lambda x, y: (20, 60, 190)), w, h)
    decided = gs._decide([res])
    assert decided["detected"] and decided["kind"] == "blue"


def test_analyze_frame_uniformity_guard_rejects_foliage():
    """非均匀绿色(草地/树叶)不得被判幕布:幕色亮度离散度 cv 超阈值即否。"""
    w, h = 64, 36
    res = gs.analyze_frame(_frame(w, h, lambda x, y: (0, 60 if (x + y) % 2 else 240, 0)), w, h)
    assert res["cvGreen"] > gs.MAX_CV
    assert gs._decide([res])["detected"] is False


def test_analyze_frame_plain_scene_not_flagged():
    w, h = 64, 36
    res = gs.analyze_frame(_frame(w, h, lambda x, y: (((x * 4 + y * 2) % 256),) * 3), w, h)
    assert gs._decide([res])["detected"] is False


def test_decide_empty_frames():
    d = gs._decide([])
    assert d["detected"] is False and d["frames"] == 0


# ---------------------------------------------------------------- 放行留痕

def test_override_roundtrip(tmp_path):
    assert gs.read_override(tmp_path) is None
    p = gs.write_override(tmp_path, "素材是绿色海报,不是绿幕")
    assert p.is_file()
    assert "绿色海报" in gs.read_override(tmp_path)


def test_override_from_brief_marker(tmp_path):
    (tmp_path / "00_brief").mkdir()
    (tmp_path / "00_brief" / "brief.md").write_text(
        "# Brief\n- 绿幕检测:误判(已人工确认,非绿幕)\n", encoding="utf-8")
    assert gs.read_override(tmp_path)


def test_detect_media_missing_file(tmp_path):
    res = gs.detect_media(tmp_path / "ghost.mp4")
    assert res["detected"] is False and res["probe"] in ("missing", "unavailable")


# ---------------------------------------------------------------- S0 门禁

def _fake_video(root: Path, name: str = "a.mp4") -> Path:
    mat = root / "01_materials"
    mat.mkdir(parents=True, exist_ok=True)
    p = mat / name
    p.write_bytes(b"fake")
    return p


def test_scan_blocks_on_green_without_override(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    _fake_video(root)
    monkeypatch.setattr(rs_ingest, "probe_media", lambda p: {
        "probe": "ok", "durationMs": 2000, "width": 1080, "height": 1920,
        "fps": 30.0, "hasAudio": True, "codec": "h264"})
    monkeypatch.setattr(gs, "detect_media", lambda *a, **k: {
        "probe": "ok", "detected": True, "kind": "green", "confidence": 0.82,
        "borderGreen": 0.82, "greenRatio": 0.5, "frames": 6})
    res = rs_ingest.scan(root, "proj", "9x16")
    assert res["ok"] is False and res["code"] == "GREEN_SCREEN_INPUT"
    assert res["flagged"] == ["a.mp4"]
    assert (root / "01_materials" / "GREENSCREEN.md").is_file()
    man = json.loads((root / "01_materials" / "manifest.json").read_text(encoding="utf-8"))
    assert man["items"][0]["greenScreen"]["detected"] is True


def test_scan_passes_after_user_override(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    _fake_video(root)
    monkeypatch.setattr(rs_ingest, "probe_media", lambda p: {
        "probe": "ok", "durationMs": 2000, "width": 1080, "height": 1920,
        "fps": 30.0, "hasAudio": True, "codec": "h264"})
    monkeypatch.setattr(gs, "detect_media", lambda *a, **k: {
        "probe": "ok", "detected": True, "kind": "green", "confidence": 0.82,
        "borderGreen": 0.82, "greenRatio": 0.5, "frames": 6})
    gs.write_override(root, "误判:绿色布景")
    res = rs_ingest.scan(root, "proj", "9x16")
    assert res["ok"] is True and res["code"] == "INGEST_OK"
    man = json.loads((root / "01_materials" / "manifest.json").read_text(encoding="utf-8"))
    assert man["items"][0]["greenScreen"]["overridden"] is True


# ---------------------------------------------------------------- L0 闸

def test_verify_greenscreen_fails_then_passes(tmp_path):
    root = tmp_path / "proj"
    mat = root / "01_materials"
    mat.mkdir(parents=True)
    (mat / "manifest.json").write_text(json.dumps({
        "version": 1, "items": [{"file": "a.mp4",
                                 "greenScreen": {"detected": True, "kind": "green"}}]}),
        encoding="utf-8")
    res = rs_verify.check_greenscreen(root)
    assert res["ok"] is False and res["flagged"] == ["a.mp4"]
    gs.write_override(root, "误判")
    assert rs_verify.check_greenscreen(root)["ok"] is True


def test_verify_greenscreen_skips_legacy_manifest(tmp_path):
    root = tmp_path / "proj"
    mat = root / "01_materials"
    mat.mkdir(parents=True)
    (mat / "manifest.json").write_text(json.dumps(
        {"version": 1, "items": [{"file": "a.mp4", "probe": "ok"}]}), encoding="utf-8")
    res = rs_verify.check_greenscreen(root)
    assert res["ok"] is True and res.get("skipped")


def test_verify_greenscreen_clean_manifest(tmp_path):
    root = tmp_path / "proj"
    mat = root / "01_materials"
    mat.mkdir(parents=True)
    (mat / "manifest.json").write_text(json.dumps({"version": 1, "items": [
        {"file": "a.mp4", "greenScreen": {"detected": False}}]}), encoding="utf-8")
    assert rs_verify.check_greenscreen(root)["ok"] is True


# ---------------------------------------------------------------- 端到端抽帧(需 ffmpeg)

@needs_ff
def test_detect_media_real_green_video(tmp_path):
    media = tmp_path / "green.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=0x00FF00:s=320x240:r=30:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media)], check=True)
    res = gs.detect_media(media, {"ffmpeg_dir": str(Path(FFMPEG).parent)})
    assert res["probe"] == "ok" and res["detected"] and res["kind"] == "green"


@needs_ff
def test_detect_media_real_plain_video(tmp_path):
    media = tmp_path / "plain.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                    "-i", "testsrc=size=320x240:rate=30:d=3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media)], check=True)
    res = gs.detect_media(media, {"ffmpeg_dir": str(Path(FFMPEG).parent)})
    assert res["probe"] == "ok" and res["detected"] is False
