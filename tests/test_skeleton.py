# -*- coding: utf-8 -*-
"""第三册 T3.3 · skeleton.json 主轨骨架摘要。

判据:
  [数] 3 分钟成片的 skeleton.json ≤2KB(超预算逐级压缩:台词摘要 12→8→删→截行,留痕);
  [机] 行结构 = {i 镜号, d 时长, m 运动档, b 亮度档, t 有无台词, h 台词前 12 字, r 建议角色};
  [机] 角色规则确定性:首镜开场 / 末镜收尾 / 无台词且静止 → 过渡 / 其余主体。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import rs_vision  # noqa: E402


def _shot(i, motion, brightness, start=None, end=None):
    return {"index": i, "startMs": start if start is not None else i * 2000,
            "endMs": end if end is not None else i * 2000 + 2000,
            "durMs": 2000, "motionScore": motion, "brightness": brightness,
            "contrast": 0.1, "colorfulness": 0.1, "stability": 0.9,
            "staticRatio": 0.8, "audioRms": 0.01, "speechRate": 2.0,
            "motionPeakMs": (start or 0) + 500}


def _mk_project(tmp: Path, shots: list[dict], chars: list[dict] | None = None) -> Path:
    proj = tmp
    cut = proj / "04_粗剪决策"
    cut.mkdir(parents=True, exist_ok=True)
    (cut / "shots.json").write_text(
        json.dumps({"schema": 2, "shots": shots}, ensure_ascii=False), encoding="utf-8")
    if chars is not None:
        tl = proj / "05_时间线工程"
        tl.mkdir(parents=True, exist_ok=True)
        (tl / "wordline.json").write_text(
            json.dumps({"chars": chars}, ensure_ascii=False), encoding="utf-8")
    return proj


def test_skeleton_rows_bands_roles(tmp_path):
    """行结构齐备;档位/角色规则确定性;台词摘要取前 12 字。"""
    shots = [_shot(0, 0.0, 0.6),           # 静/偏亮 → 开场
             _shot(1, 0.005, 0.5),         # 静/偏亮,无台词 → 过渡
             _shot(2, 0.10, 0.7),          # 中/亮 → 主体
             _shot(3, 0.20, 0.3)]          # 烈/暗 → 收尾
    chars = [{"startMs": 200, "endMs": 1900, "ch": ch}
             for ch in "这是一个十二个字以上的台词摘要样例"]
    proj = _mk_project(tmp_path / "骨架工程", shots, chars)
    assert rs_vision.build_skeleton(proj) == 0
    doc = json.loads(rs_vision.skeleton_path(proj).read_text(encoding="utf-8"))
    items = doc["items"]
    assert len(items) == 4
    roles = [it["r"] for it in items]
    assert roles[0] == rs_vision.ROLE_OPEN and roles[-1] == rs_vision.ROLE_CLOSE
    assert roles[1] == rs_vision.ROLE_TRANS and roles[2] == rs_vision.ROLE_BODY
    assert items[0]["t"] == 1 and items[0]["h"].startswith("这是一个十二个字")
    assert len(items[0]["h"]) <= rs_vision.SPEECH_HEAD_CHARS
    assert items[2]["m"] == 2 and items[0]["m"] == 0
    size = rs_vision.skeleton_path(proj).stat().st_size
    assert size <= rs_vision.SKEL_BUDGET_BYTES


def test_skeleton_budget_2kb_three_minute(tmp_path):
    """[数] 3 分钟成片(72 镜×2.5s,全带台词摘要)≤2KB;超预算必须压缩并留痕。"""
    shots = [_shot(i, 0.02 + (i % 4) * 0.04, 0.3 + (i % 3) * 0.2)
             for i in range(72)]
    chars = []
    for i in range(72):
        base = i * 2500
        for j, ch in enumerate("每一镜都有整整十二个字的台词摘要内容"):
            chars.append({"startMs": base + j * 25, "endMs": base + j * 25 + 20, "ch": ch})
    proj = _mk_project(tmp_path / "长片工程", shots, chars)
    assert rs_vision.build_skeleton(proj) == 0
    p = rs_vision.skeleton_path(proj)
    assert p.stat().st_size <= 2048, f"skeleton {p.stat().st_size}B 超 2KB"
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc.get("compressed") or doc.get("truncated") or \
        all(len(it.get("h", "")) <= 12 for it in doc["items"])


def test_skeleton_timeline_source_from_ir(tmp_path):
    """有 IR 主轨(sourceInMs)→ 骨架按成片 clip 行(主导镜档案映射)。"""
    shots = [_shot(0, 0.05, 0.6, 0, 10_000), _shot(1, 0.2, 0.4, 10_000, 20_000)]
    proj = _mk_project(tmp_path / "时间线工程", shots, None)
    tl = proj / "05_时间线工程"
    tl.mkdir(exist_ok=True)
    ir = {"version": 1, "slug": "t", "fps": 30, "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"kind": "video", "name": "main", "clips": [
              {"id": "V1-001", "src": "01_原始素材/a.mp4", "startMs": 0,
               "durationMs": 8000, "sourceInMs": 0},
              {"id": "V1-002", "src": "01_原始素材/a.mp4", "startMs": 8000,
               "durationMs": 12000, "sourceInMs": 9000}]}], "outputs": ["9x16"]}
    (tl / "project.json").write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    assert rs_vision.build_skeleton(proj) == 0
    doc = json.loads(rs_vision.skeleton_path(proj).read_text(encoding="utf-8"))
    assert doc["source"] == "timeline"
    assert [it["d"] for it in doc["items"]] == [8000, 12000]
    assert doc["items"][0]["m"] == 1 and doc["items"][1]["m"] == 3


def test_skeleton_no_shots_fails_loud(tmp_path):
    """无 shots/vision → 显式失败(不静默产空骨架)。"""
    proj = tmp_path / "空工程"
    proj.mkdir()
    assert rs_vision.build_skeleton(proj) != 0
