# -*- coding: utf-8 -*-
"""第三册 T3.8 · 三级感知预算门禁(T3.6 L2 描述通道附带)。

判据:
  [机] --tier L0 零网络零模型零解码(代码路径爆破断言);
  [机] --tier L2 超预算**停并留痕**(stopped="budget",完成 x/y 记账,不静默降级);
  [机] L2 默认关:tier 缺省 L1;VLM 缺失 → 显式降级留痕,CI 无 VLM 全绿。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_sense  # noqa: E402


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
N_SHOTS = 6


def _mk_shots(tmp: Path, n: int = N_SHOTS) -> Path:
    shots = [{"index": i, "startMs": i * 1000, "endMs": (i + 1) * 1000,
              "durMs": 1000, "motionScore": 0.05, "brightness": 0.5,
              "contrast": 0.1, "colorfulness": 0.2, "stability": 0.8,
              "staticRatio": 0.5, "audioRms": 0.05, "speechRate": 2.0,
              "motionPeakMs": i * 1000 + 500} for i in range(n)]
    p = tmp / "shots.json"
    p.write_text(json.dumps({"schema": 2, "source": "a.mp4", "shots": shots},
                            ensure_ascii=False), encoding="utf-8")
    return p


def _args(tmp: Path, tier: str, budget: float = 120.0) -> object:
    a = type("A", (), {})()
    a.shots = True
    a.vision = str(tmp / "shots.json")
    a.media = str(tmp / "a.mp4")
    a.out = str(tmp / "out")
    a.tier = tier
    a.budget_sec = budget
    a.max_shots = 0
    a.force = True
    return a


def test_tier_l0_zero_model_zero_decode(tmp_path, monkeypatch, capsys):
    """--tier L0:解码/VQA 全部爆破仍通过 —— 证明零模型零解码。"""
    sp = _mk_shots(tmp_path)
    a = _args(tmp_path, "L0")

    def boom(*args, **kwargs):
        raise AssertionError("L0 不得触解码/模型")

    monkeypatch.setattr(rs_sense, "vqa_image", boom)
    monkeypatch.setattr(rs_sense, "_extract_frame", boom)
    import rs_vision
    monkeypatch.setattr(rs_vision, "l1_shot_fields", boom)
    rc = rs_sense.cmd_shots(a)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["data"]["tier"] == "L0" and out["data"]["shotCount"] == N_SHOTS
    assert out["data"]["tiers"]["L0"]["state"] == "READY"


def test_tier_l2_default_off_vlm_missing_degrades(tmp_path, monkeypatch, capsys):
    """L2 默认关(缺省 L1);VLM 组件缺失 → 显式降级留痕,退出码 0(CI 全绿)。"""
    sp = _mk_shots(tmp_path)
    a = _args(tmp_path, "L2")
    a.media = str(tmp_path / "不存在.mp4")      # 无素材:显式降级路径
    monkeypatch.setattr(rs_sense, "load_config", lambda: {})   # 无 vqa 组件
    rc = rs_sense.cmd_shots(a)
    assert rc == 0, "缺失降级不失败(CI 无 VLM 全绿)"
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert any(d.startswith("L2") for d in out["data"]["degradeReasons"])
    assert "缺失" in out["data"]["tiers"]["L2"]["note"]


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_tier_l2_budget_overrun_stops_loudly(tmp_path, monkeypatch, capsys):
    """--tier L2 超预算:停并留痕(stopped=budget,完成 x/y),不静默。"""
    tmp = tmp_path / "预算工程"
    tmp.mkdir()
    _mk_shots(tmp)
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=gray:size=96x72:rate=10:duration=6",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    str(tmp / "a.mp4")], check=True)
    a = _args(tmp, "L2", budget=0.55)
    monkeypatch.setattr(rs_sense, "load_config", lambda: {"vqa_exe": "fake"})
    monkeypatch.setattr(rs_sense, "_extract_frame",
                        lambda media, t_ms, png, cfg: png.write_bytes(b"p") or True)

    def slow_vqa(img, txt, cfg, prompt):
        time.sleep(0.25)
        return "一人对镜头讲解 近景\n主体:人 场景:室内 构图:近景"

    monkeypatch.setattr(rs_sense, "vqa_image", slow_vqa)
    rc = rs_sense.cmd_shots(a)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    data = out["data"]
    assert data["stopped"] == "budget", "预算超限必须停"
    assert 0 < len(data["captions"]) < N_SHOTS, "完成数必须小于总镜数(真停了)"
    assert "超限" in data["tiers"]["L2"]["note"], "留痕必须写明超限"
    assert any("预算超限停" in d for d in data["degradeReasons"])
    # 每条描述 ≤20 字(T3.6 判据)
    for cap in data["captions"].values():
        assert len(cap["caption"]) <= 20


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_tier_l1_local_tags_with_explicit_face_degrade(tmp_path, monkeypatch, capsys):
    """L1(默认档):安全带占用照常产出;无人脸模型 → hasFace=False + 显式留痕。"""
    tmp = tmp_path / "L1工程"
    tmp.mkdir()
    _mk_shots(tmp)
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=gray:size=96x72:rate=10:duration=6",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    str(tmp / "a.mp4")], check=True)
    a = _args(tmp, "L1")
    monkeypatch.setattr(rs_sense, "load_config", lambda: {})   # 无 vqa(L2 通道无关)
    rc = rs_sense.cmd_shots(a)
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    data = out["data"]
    l1_state = data["tiers"]["L1"]["state"]
    if l1_state == "READY":
        assert len(data["l1"]) == N_SHOTS
        for v in data["l1"].values():
            assert "safeAreaOccupancy" in v
            assert v.get("hasFace") is False  # 本测试无人脸模型/无人脸 → 显式 False
    else:
        # 三态协议:本地标签组件(cv2 等)缺失 → L1 整体 MISSING,必须显式留痕而非静默
        assert l1_state == "MISSING", l1_state
        note = json.dumps(data["tiers"]["L1"], ensure_ascii=False)
        assert "缺" in note or "missing" in note.lower() or "degrade" in note.lower(), note
