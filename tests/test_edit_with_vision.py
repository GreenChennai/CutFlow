# -*- coding: utf-8 -*-
"""第三册 T3.10 · rs_edit context --with-vision(画面准入)。

判据:
  [机] --with-vision 输出仍 ≤12KB(预算硬上限由既有裁剪引擎机械保证);
  [机] 注入文本化画面摘要:skeleton 一行/镜(镜号/档位/台词)+ 该 clip 镜内描述;
  [机] 仍然不含图片路径;
  [机] T3.2 附:context 视频行显示「该 clip 属于第几镜、镜头长短」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import rs_edit  # noqa: E402

BUDGET_12KB = 12 * 1024


def _mk_project(tmp: Path) -> Path:
    proj = tmp / "改片工程"
    for d in ("04_粗剪决策", "05_时间线工程"):
        (proj / d).mkdir(parents=True, exist_ok=True)
    shots = {"schema": 2, "source": "a.mp4",
             "shots": [{"index": 0, "startMs": 0, "endMs": 2000, "durMs": 2000,
                        "motionScore": 0.01, "brightness": 0.5, "contrast": 0.1,
                        "colorfulness": 0.2, "stability": 0.9, "staticRatio": 0.8,
                        "audioRms": 0.05, "speechRate": 2.0, "motionPeakMs": -1},
                       {"index": 1, "startMs": 2000, "endMs": 6000, "durMs": 4000,
                        "motionScore": 0.10, "brightness": 0.6, "contrast": 0.2,
                        "colorfulness": 0.3, "stability": 0.5, "staticRatio": 0.2,
                        "audioRms": 0.06, "speechRate": 2.5, "motionPeakMs": 3000}],
             "transitions": []}
    (proj / "04_粗剪决策" / "shots.json").write_text(
        json.dumps(shots, ensure_ascii=False), encoding="utf-8")
    ir = {"version": 1, "slug": "改片", "fps": 30, "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"kind": "video", "name": "main", "clips": [
              {"id": "V1-001", "src": "01_原始素材/a.mp4", "startMs": 0,
               "durationMs": 2000, "sourceInMs": 0},
              {"id": "V1-002", "src": "01_原始素材/a.mp4", "startMs": 2000,
               "durationMs": 4000, "sourceInMs": 2000}]}], "outputs": ["9x16"]}
    (proj / "05_时间线工程" / "project.json").write_text(
        json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    return proj


def _drop_vision_products(proj: Path, captions=(True, True)) -> None:
    tl = proj / "05_时间线工程"
    skel = {"version": 1, "source": "timeline", "shotCount": 2, "legend": "x",
            "budgetBytes": 2048,
            "items": [{"i": 0, "d": 2000, "m": 0, "b": 2, "t": 1,
                       "h": "大家好今天讲镜", "r": 0},
                      {"i": 1, "d": 4000, "m": 2, "b": 2, "t": 1,
                       "h": "然后我们看第二", "r": 1}]}
    (tl / "skeleton.json").write_text(json.dumps(skel, ensure_ascii=False), encoding="utf-8")
    vis = {"version": 1, "source": "a.mp4", "shotsSource": "shots.json", "shotCount": 2,
           "shots": [
               {"index": 0, "startMs": 0, "endMs": 2000, "durMs": 2000,
                "motionScore": 0.01, "brightness": 0.5, "contrast": 0.1,
                "colorfulness": 0.2, "stability": 0.9, "staticRatio": 0.8,
                "audioRms": 0.05, "speechRate": 2.0, "motionPeakMs": -1,
                **({"caption": "一人对镜头讲解 近景"} if captions[0] else {})},
               {"index": 1, "startMs": 2000, "endMs": 6000, "durMs": 4000,
                "motionScore": 0.10, "brightness": 0.6, "contrast": 0.2,
                "colorfulness": 0.3, "stability": 0.5, "staticRatio": 0.2,
                "audioRms": 0.06, "speechRate": 2.5, "motionPeakMs": 3000,
                **({"caption": "桌面产品展示 中景"} if captions[1] else {})}],
           "tiers": {}, "degradeReasons": []}
    (tl / "vision.json").write_text(json.dumps(vis, ensure_ascii=False), encoding="utf-8")


def test_context_shot_labels_for_video_rows(tmp_path):
    """T3.2:主轨视频行显示「镜N(时长)」;缺 shots.json 时不显示(不报错)。"""
    proj = _mk_project(tmp_path)
    text, _ = rs_edit.build_context(proj, "project", BUDGET_12KB)
    assert "镜0(2.0s)" in text and "镜1(4.0s)" in text
    proj2 = tmp_path / "无镜工程"
    (proj2 / "05_时间线工程").mkdir(parents=True)
    (proj2 / "05_时间线工程" / "project.json").write_text(
        (proj / "05_时间线工程" / "project.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    text2, _ = rs_edit.build_context(proj2, "project", BUDGET_12KB)
    assert "镜0" not in text2


def test_with_vision_rows_and_budget(tmp_path):
    """--with-vision:每 clip 一行画面事实(档位/台词/镜内描述);输出 ≤12KB。"""
    proj = _mk_project(tmp_path)
    _drop_vision_products(proj)
    text, info = rs_edit.build_context(proj, "project", BUDGET_12KB, with_vision=True)
    assert info["bytes"] <= BUDGET_12KB, f"--with-vision 输出 {info['bytes']}B 超 12KB"
    row = next(l for l in text.splitlines()
               if l.startswith("| V1-001") and "近景" in l)
    assert "镜0" in row and "静" in row and "大家好今天讲镜" in row
    row2 = next(l for l in text.splitlines()
                if l.startswith("| V1-002") and "中景" in l)
    assert "中景" in row2
    assert "http" not in text and ".png" not in text and ".jpg" not in text, \
        "画面摘要不得携带任何图片路径"


def test_with_vision_without_products_shows_howto(tmp_path):
    """无画面产物 → 显式提示怎么补(不静默空节)。"""
    proj = _mk_project(tmp_path)
    text, info = rs_edit.build_context(proj, "project", BUDGET_12KB, with_vision=True)
    assert info["bytes"] <= BUDGET_12KB
    assert "尚无画面产物" in text and "rs_shot.py detect" in text


def test_with_vision_budget_squeeze_trims_with_note(tmp_path):
    """预算紧张时 vision 节按优先级整节裁掉并在裁剪声明中留痕(12KB 判据的下界行为)。"""
    proj = _mk_project(tmp_path)
    _drop_vision_products(proj)
    text, info = rs_edit.build_context(proj, "project", 600, with_vision=True)
    assert info["bytes"] <= 600
    assert "画面摘要(视觉)" in "".join(info["trimmed"]) or "镜0" in text
