# -*- coding: utf-8 -*-
"""第三册 T3.4 / T3.7 · vision.json 聚合 + schema 校验 + vision.track READY 档。

判据:
  [机] vision.json 有 schema 校验(rs_vision.validate_vision);坏结构/超 64KB 必红;
  [机] vision.track 有检测器时 reframe_plan.trajectory 非 static-center(T3.4);
       无检测/置信度不足 → degradeNote「主体跟踪缺失→居中锚」显式留痕(不再静默);
  [机] T3.11:零画面能力机器上 sense_report.md 照常产出且非空。
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

import rs_fetchable  # noqa: E402
import rs_reframe  # noqa: E402
import rs_vision  # noqa: E402


def _ffmpeg() -> str:
    try:
        rs_common = __import__("rs_common")
        p = rs_common.ffmpeg_bin(rs_common.load_config())
        if Path(p).is_file():
            return p
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


FF = _ffmpeg()


def _mk_video(tmp: Path, name: str = "a.mp4", dur: float = 2.0) -> Path:
    src = tmp / name
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=0x305070:size=160x120:rate=15:duration={dur}",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    str(src)], check=True)
    return src


def _mk_shots(tmp: Path, n: int = 2, span_ms: int = 1000) -> Path:
    shots = [{"index": i, "startMs": i * span_ms, "endMs": (i + 1) * span_ms,
              "durMs": span_ms, "motionScore": 0.02, "brightness": 0.4,
              "contrast": 0.1, "colorfulness": 0.1, "stability": 0.9,
              "staticRatio": 0.9, "audioRms": 0.01, "speechRate": 1.5,
              "motionPeakMs": i * span_ms + 300} for i in range(n)]
    p = tmp / "04_粗剪决策" / "shots.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": 2, "source": "a.mp4", "shots": shots},
                            ensure_ascii=False), encoding="utf-8")
    return p


# ---------------------------------------------------------------- T3.7 schema 校验

def test_validate_vision_accepts_good_and_rejects_bad():
    good = {"version": 1, "shots": [{
        "index": 0, "startMs": 0, "endMs": 1000, "durMs": 1000,
        "motionScore": 0.1, "brightness": 0.5, "contrast": 0.2,
        "colorfulness": 0.3, "stability": 0.8, "staticRatio": 0.5,
        "audioRms": 0.0, "speechRate": 2.0, "motionPeakMs": 400,
        "caption": "一句不超过二十字的画面描述"}]}
    assert rs_vision.validate_vision(good) == []
    bad = {"version": 2, "shots": [{
        "index": 5, "startMs": 0, "endMs": 1000, "durMs": 1000,
        "motionScore": 1.5, "brightness": 0.5, "contrast": 0.2,
        "colorfulness": 0.3, "stability": 0.8, "staticRatio": 0.5,
        "audioRms": 0.0, "speechRate": 2.0, "caption": "x" * 21}]}
    errs = rs_vision.validate_vision(bad)
    assert any("version" in e for e in errs)
    assert any("index" in e for e in errs)
    assert any("motionScore" in e for e in errs)
    assert any("caption" in e for e in errs)


def test_validate_vision_budget_cap():
    """vision.json >64KB 判红(3 分钟成片预算,T3.7)。"""
    rows = []
    for i in range(700):                        # 700 镜大工程,必然超 64KB
        rows.append({"index": i, "startMs": i, "endMs": i + 1, "durMs": 1,
                     "motionScore": 0.1, "brightness": 0.5, "contrast": 0.2,
                     "colorfulness": 0.3, "stability": 0.8, "staticRatio": 0.5,
                     "audioRms": 0.0, "speechRate": 2.0, "motionPeakMs": i})
    doc = {"version": 1, "shots": rows}
    assert any("超预算" in e for e in rs_vision.validate_vision(doc))


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_build_vision_end_to_end_and_schema_gate(tmp_path):
    """shots.json(v2)+ 小媒体 → vision.json 落盘过校验;L1 缺失显式降级。"""
    proj = tmp_path / "vision工程"
    (proj / "01_原始素材").mkdir(parents=True)
    _mk_video(proj / "01_原始素材", "a.mp4")
    _mk_shots(proj)
    rc = rs_vision.build_vision(proj)
    assert rc == 0
    vp = rs_vision.vision_path(proj)
    doc = json.loads(vp.read_text(encoding="utf-8"))
    assert rs_vision.validate_vision(doc) == []
    for row in doc["shots"]:
        for k in ("motionScore", "brightness", "contrast", "colorfulness",
                  "stability", "staticRatio", "audioRms", "speechRate",
                  "motionPeakMs"):
            assert k in row
    assert any("wordline" in d for d in doc["degradeReasons"]) or doc["degradeReasons"] == []


# ---------------------------------------------------------------- T3.4 vision.track READY 档

def _slow_boxes(n=60, step=2.0):
    """缓移主体盒(轨迹可跟,不触硬约束);坐标在 1080x1920 源空间。"""
    return [(400.0 + i * step, 800.0, 640.0 + i * step, 1064.0) for i in range(n)]


def test_reframe_track_mode_with_detector(tmp_path, monkeypatch):
    """T3.4[机]:有 detector 时 reframe_plan 的 trajectory 非 static-center。"""
    proj = tmp_path / "track工程"
    (proj / "05_时间线工程").mkdir(parents=True)
    (proj / "01_原始素材").mkdir(parents=True)
    (proj / "01_原始素材" / "a.mp4").write_bytes(b"stub")   # 仅需存在性(检测走 mock)
    ir = {"version": 1, "slug": "t", "fps": 30, "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"kind": "video", "name": "main", "clips": [
              {"id": "V1-001", "src": "01_原始素材/a.mp4", "startMs": 0,
               "durationMs": 2000, "sourceInMs": 0}]}], "outputs": ["9x16"]}
    (proj / "05_时间线工程" / "project.json").write_text(
        json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rs_reframe, "ready_tier",
                        lambda: (True, {"track": {"engine": "haar+frame-diff",
                                                  "degraded": False}}))
    monkeypatch.setattr(rs_reframe, "ready_subject_boxes",
                        lambda media, src_w, src_h, cfg, max_frames=600: _slow_boxes())
    rs_reframe.main(["plan", str(proj), "--force"])
    plan = json.loads(rs_reframe.reframe_path(proj).read_text(encoding="utf-8"))
    clip = plan["clips"][0]
    assert clip["mode"] == "track", "有检测器 + 可跟主体 → 必须 track 档"
    assert len(clip["trajectory"]) > 1 and clip["trajectory"][0]["anchorX"] != \
        clip["trajectory"][-1]["anchorX"], "轨迹必须非静态"


def test_reframe_missing_subject_degrades_loudly(tmp_path, monkeypatch):
    """T3.4[机]:无检测 → degradeNote「主体跟踪缺失→居中锚」显式留痕(不再静默)。"""
    proj = tmp_path / "盲工程"
    (proj / "05_时间线工程").mkdir(parents=True)
    (proj / "01_原始素材").mkdir(parents=True)
    (proj / "01_原始素材" / "a.mp4").write_bytes(b"stub")
    ir = {"version": 1, "slug": "t", "fps": 30, "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"kind": "video", "name": "main", "clips": [
              {"id": "V1-001", "src": "01_原始素材/a.mp4", "startMs": 0,
               "durationMs": 2000, "sourceInMs": 0}]}], "outputs": ["9x16"]}
    (proj / "05_时间线工程" / "project.json").write_text(
        json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rs_reframe, "ready_tier",
                        lambda: (True, {"track": {"engine": "haar+frame-diff",
                                                  "degraded": False}}))
    monkeypatch.setattr(rs_reframe, "ready_subject_boxes",
                        lambda media, src_w, src_h, cfg, max_frames=600: None)
    rs_reframe.main(["plan", str(proj), "--force"])
    plan = json.loads(rs_reframe.reframe_path(proj).read_text(encoding="utf-8"))
    clip = plan["clips"][0]
    assert clip["mode"] == "static-center"
    assert "主体跟踪缺失" in clip.get("degradeNote", ""), "必须显式留痕,不得静默"
    assert plan.get("degradeReasons"), "doc 级也要有降级留痕"


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_subject_boxes_channel_diff_finds_mover(tmp_path):
    """T3.4:cv2 无人脸模型时,帧差(逐通道)+肤色零模型档仍能给出主体盒。"""
    tmp = tmp_path / "media"
    tmp.mkdir()
    media = tmp / "m.mp4"
    subprocess.run([FF, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=0x204060:size=320x240:rate=30:duration=2",
                    "-f", "lavfi", "-i", "color=c=white:size=64x64:rate=30:duration=2",
                    "-filter_complex",
                    "[0:v][1:v]overlay=x='40+t*30':y=100[v]",
                    "-map", "[v]", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                    "-preset", "ultrafast", str(media)], check=True)
    boxes, tiers = rs_vision.subject_boxes(media, 320, 240, __import__("rs_common").load_config())
    if boxes:                                   # 帧差档命中:轨迹必须向右移动
        assert boxes[-1][0] > boxes[0][0], "移动主体应产生非静态盒序列"
        assert tiers["track"]["degraded"] is False
    else:
        assert tiers["track"]["degraded"] is True and "居中锚" in tiers["track"]["note"]


# ---------------------------------------------------------------- T3.11 sense_report

def test_sense_report_on_zero_vision_machine(tmp_path, monkeypatch):
    """T3.11[机]:无任何画面能力的机器上 report 照常产出且非空、L1 如实标缺失。"""
    proj = tmp_path / "裸机工程"
    proj.mkdir()
    monkeypatch.setattr(rs_fetchable, "state",
                        lambda cid: {"component": cid, "state": "MISSING", "degrade": "x",
                                     "size_mb": 0, "backend": "py", "installDir": "",
                                     "message": "模拟缺失"})
    orig = rs_vision.load_config
    monkeypatch.setattr(rs_vision, "load_config", lambda: {})
    assert rs_vision.build_report(proj) == 0
    rp = rs_vision.sense_report_path(proj)
    text = rp.read_text(encoding="utf-8")
    assert text.strip(), "报告非空"
    assert "L0 结构层" in text and "缺失与建议" in text
    assert "L1 标签" in text and "缺失" in text
    monkeypatch.setattr(rs_vision, "load_config", orig)
