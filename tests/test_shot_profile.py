# -*- coding: utf-8 -*-
"""第三册 T3.1 · 镜头档案 v2(shots.json 逐镜数值档案)。

判据:
  [机] 字段齐备且全数值可复算(motionScore/brightness/contrast/colorfulness/
       stability/staticRatio/audioRms/speechRate/motionPeakMs);
  [数] 对同一素材两次运行**逐字节一致**(确定性,整数累加保证);
  [附] T3.2 下游:rs_cut 的 shots 检测器产出 reason=shot_change 候选;
       rs_ir 转场 reason 按镜头关系选(跨镜 shot-change / 同镜 jumpcut)。
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

import rs_cut  # noqa: E402
import rs_ir  # noqa: E402
import rs_shot  # noqa: E402


def _ffmpeg() -> str:
    try:
        cfg = __import__("rs_common").load_config()
        p = __import__("rs_common").ffmpeg_bin(cfg)
        if Path(p).is_file():
            return p
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


FF = _ffmpeg()
PROFILE_FIELDS = ("motionScore", "brightness", "contrast", "colorfulness",
                  "stability", "staticRatio", "audioRms", "speechRate", "motionPeakMs")


def _mk_scene_video(tmp: Path, name: str = "a.mp4") -> Path:
    """3s 三场景(黑/白/彩条):frame-diff 检测器必有 2 个切点。"""
    src = tmp / name
    subprocess.run([FF, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:size=192x144:rate=30:duration=1",
                    "-f", "lavfi", "-i", "color=c=white:size=192x144:rate=30:duration=1",
                    "-f", "lavfi", "-i", "smptebars=size=192x144:rate=30:duration=1",
                    "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1[v]",
                    "-map", "[v]", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                    "-preset", "ultrafast", str(src)], check=True)
    return src


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_shot_profile_fields_complete_and_deterministic(tmp_path):
    """T3.1:v2 档案字段齐备、值域合法;两次运行产物逐字节一致。"""
    proj = tmp_path / "档案工程"
    mat = proj / "01_原始素材"
    mat.mkdir(parents=True)
    src = _mk_scene_video(mat)
    argv = ["detect", str(src), "--out", str(proj)]
    assert rs_shot.main(argv) == 0
    doc = json.loads(rs_shot.shots_path(proj).read_text(encoding="utf-8"))
    assert doc.get("schema") == 2, "v2 镜头档案必须带 schema=2"
    assert len(doc["shots"]) >= 2
    for s in doc["shots"]:
        for k in PROFILE_FIELDS:
            assert k in s, f"缺 v2 档案字段 {k}"
        for k in ("motionScore", "brightness", "contrast", "colorfulness",
                  "stability", "staticRatio", "audioRms"):
            assert isinstance(s[k], (int, float)) and 0.0 <= s[k] <= 1.0, f"{k} 越界:{s[k]}"
    # 黑场镜亮度≈0、白场镜亮度≈1(数值可信性抽查)
    by_b = sorted(doc["shots"], key=lambda s: s["brightness"])
    assert by_b[0]["brightness"] < 0.1, "黑场镜亮度应接近 0"
    assert by_b[-1]["brightness"] > 0.85, "白场镜亮度应接近 1"
    # 确定性:重跑(--force)逐字节一致
    before = rs_shot.shots_path(proj).read_bytes()
    assert rs_shot.main(argv + ["--force"]) == 0
    assert rs_shot.shots_path(proj).read_bytes() == before, "两次运行必须逐字段一致"


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_shot_profile_audio_and_speech(tmp_path):
    """T3.1:audioRms 吃真实音轨;speechRate 吃 wordline 字级区间求交。"""
    proj = tmp_path / "语音工程"
    mat = proj / "01_原始素材"
    mat.mkdir(parents=True)
    src = mat / "withaudio.mp4"
    subprocess.run([FF, "-v", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=15:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-map", "0:v", "-map", "1:a", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-c:a", "aac", "-preset", "ultrafast",
                    str(src)], check=True)
    # wordline:前 1s 每毫秒 4 字/秒,后 1s 无字
    chars = [{"startMs": i * 250, "endMs": i * 250 + 200, "ch": "字"}
             for i in range(0, 4)]
    wl = proj / "05_时间线工程"
    wl.mkdir(parents=True)
    (wl / "wordline.json").write_text(json.dumps({"chars": chars}, ensure_ascii=False),
                                      encoding="utf-8")
    assert rs_shot.main(["detect", str(src), "--out", str(proj)]) == 0
    doc = json.loads(rs_shot.shots_path(proj).read_text(encoding="utf-8"))
    s0 = doc["shots"][0]
    assert s0["audioRms"] > 0.01, "有音轨的镜头 audioRms 必须非 0"
    assert s0["speechRate"] > 0, "镜内有字的 speechRate 必须非 0"
    assert doc["profile"]["engine"] == "ffmpeg-framestats"


# ---------------------------------------------------------------- T3.2 下游(纯函数,免 ffmpeg)

def _wl_with_gap() -> dict:
    """两段台词,中间 800ms 停顿恰好横跨 2000ms 镜头边界。"""
    chars = []
    t = 0
    for text in ("大家好今天讲镜头", "然后我们看第二个话题"):
        for ch in text:
            chars.append({"startMs": t, "endMs": t + 200, "ch": ch})
            t += 200
        t += 800                                  # 镜头边界处的大停顿
    return {"chars": chars, "srcDurationMs": 6000}


def _shots_two() -> list[dict]:
    return [{"index": 0, "startMs": 0, "endMs": 2000, "durMs": 2000},
            {"index": 1, "startMs": 2000, "endMs": 6000, "durMs": 4000}]


def test_shot_change_detector_candidates(tmp_path):
    """T3.2:rs_cut shots 检测器 —— 镜边界 ∩ 语音停顿 → reason=shot_change 候选。"""
    proj = tmp_path / "cut工程"
    (proj / "04_粗剪决策").mkdir(parents=True)
    (proj / "05_时间线工程").mkdir(parents=True)
    sp = proj / "04_粗剪决策" / "shots.json"
    sp.write_text(json.dumps({"schema": 2, "shots": _shots_two()}, ensure_ascii=False),
                  encoding="utf-8")
    wl_p = proj / "05_时间线工程" / "wordline.json"
    wl_p.write_text(json.dumps(_wl_with_gap(), ensure_ascii=False), encoding="utf-8")
    cuts = rs_cut.detect_shot_change(_wl_with_gap(), wl_path=str(wl_p))
    assert cuts, "边界+停顿必须产出候选"
    c = cuts[0]
    assert c["reason"] == "shot_change" and c["conf"] < rs_cut.CONF_REMOVE
    assert c["inMs"] < 2000 < c["outMs"], "候选区间必须覆盖镜边界"
    assert "shot_change" in rs_cut.REASONS and "shots" in rs_cut.DETECTORS
    # shots.json 缺失 → 空候选(留痕由 CLI params 承担)
    assert rs_cut.detect_shot_change(_wl_with_gap(), wl_path=str(tmp_path / "无.json")) == []


def test_ir_transition_reason_by_shot_relation(tmp_path):
    """T3.2:rs_ir 转场 reason 按镜头关系选择;产物过 project.schema.json。"""
    import jsonschema
    proj = tmp_path / "ir工程"
    (proj / "04_粗剪决策").mkdir(parents=True)
    (proj / "04_粗剪决策/shots.json").write_text(
        json.dumps({"shots": _shots_two()}, ensure_ascii=False), encoding="utf-8")
    # 跨镜:前段止于镜 0(1500),后段起于镜 1(2200)→ shot-change
    ir = rs_ir.build_from_cutlist(
        {"source": "01_原始素材/a.mp4", "keep": [[0, 1500], [2200, 5500]],
         "srcTotalMs": 6000}, slug="t", project_root=proj)
    assert ir["tracks"][0]["clips"][1]["transition"]["reason"] == "shot-change"
    # 同镜内跳切(1000→1500 都在镜 0)→ jumpcut 原语义
    ir2 = rs_ir.build_from_cutlist(
        {"source": "01_原始素材/a.mp4", "keep": [[100, 800], [1500, 5500]],
         "srcTotalMs": 6000}, slug="t", project_root=proj)
    assert ir2["tracks"][0]["clips"][1]["transition"]["reason"] == "jumpcut"
    schema = json.loads((REPO / "skills/cutflow/templates/project.schema.json")
                        .read_text(encoding="utf-8"))
    jsonschema.validate(ir, schema)
