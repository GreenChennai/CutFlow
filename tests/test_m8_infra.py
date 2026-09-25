# -*- coding: utf-8 -*-
"""M8 第一波(多风格基础设施)门禁:五大能力脚本 + 手法库 + BGM 曲库 + 描述符登记。

覆盖面(方案 §5.5.1 混剪 / §5.5.2 vlog / §5.5.4 录屏 / §5.9 手法库 / §6.2 性能预算):
· rs_beat   降级档 onset-energy 真实跑通(ffmpeg 合成点击轨,bpm/拍点/留痕/schema)
            + READY 档 mock(beatnet 推理)+ beat-only 降级 + beats.json 与 rs_edit
            beat.snap 的毫秒口径互通(既有测试不回归);
· rs_shot   降级档 frame-diff 真实跑通(三段合成视频,切点/镜头/留痕)+ MIN_SHOT_MS
            并镜单测 + READY 档 mock(scenedetect);
· rs_reframe static-center 真实跑通(裁切不拉伸:窗比恒等于目标比)+ 主体保护硬约束
            (REFRAME_CLIP_SUBJECT 违反降级留痕)+ 平滑限速真数学 + 缓存跳过;
· rs_broll  keyword-match 真实跑通(文件名/OCR 命中,确定性排序)+ READY 档 mock;
· rs_screen waiting 检测真实跑通(≥2.0s 无视觉变化且无语音)+ 降级留痕(state mock)
            + READY 档 mock(光标轨迹→点击点);
· 挂载集成:ADR-0047 挂载器无参执行 detector(工程模式)→ 产物落盘 → status ok;
· BGM 曲库(清欠账 #13):manifest 完备性 + bgm=auto 编译选曲(source=library 留痕,
            字节级可复现)+ rs_ir 接线(IR.bgm 绝对路径 + bgmFrom);
· rs_artboard 探测位修复(真包直接命中,无需镜像桥)+ registry/pack capabilities 对拍;
· 性能冒烟(§6.2):3 分钟音频降级档 ≤0.02× 实时;60s 视频降级档 ≤0.05× 实时。

运行:pytest tests/test_m8_infra.py -q(ffmpeg 不可用时整模块 SKIP)
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
BGM_DIR = REPO / "skills" / "cutflow" / "assets" / "bgm"
sys.path.insert(0, str(SCRIPTS))

import rs_paths  # noqa: E402
import rs_common  # noqa: E402
import rs_fetchable  # noqa: E402
import rs_beat  # noqa: E402
import rs_shot  # noqa: E402
import rs_reframe  # noqa: E402
import rs_broll  # noqa: E402
import rs_screen  # noqa: E402
import rs_intent  # noqa: E402
import rs_run  # noqa: E402
import rs_edit  # noqa: E402
import rs_artboard  # noqa: E402
import rs_stylepack  # noqa: E402


# ---------------------------------------------------------------- 环境与合成输入

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
pytestmark = pytest.mark.skipif(not FF, reason="ffmpeg 不可用(合成输入必需)")


def _run_ff(*args: str, timeout: int = 300) -> None:
    p = subprocess.run([FF, "-v", "error", "-y", *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    assert p.returncode == 0, f"ffmpeg 合成失败:{p.stderr[-300:]}"


def _synth_clicks(path: Path, seconds: float = 6.0) -> Path:
    """120 BPM 点击轨(0.5s 周期指数衰减正弦)—— 节拍检测的真值输入。"""
    _run_ff("-f", "lavfi", "-i",
            f"aevalsrc=0.8*sin(880*2*PI*t)*exp(-20*mod(t\\,0.5)):d={seconds}:s=8000",
            "-c:a", "pcm_s16le", str(path))
    return path


def _synth_scenes(path: Path, parts: list[tuple[float, str]]) -> Path:
    """按 (时长秒, 颜色) 顺序拼接纯色段 —— 镜头切分/waiting 检测的真值输入。"""
    ins: list[str] = []
    fc: list[str] = []
    for i, (dur, color) in enumerate(parts):
        ins += ["-f", "lavfi", "-i",
                f"color=c={color}:size=320x240:rate=10:duration={dur:g}"]
        fc.append(f"[{i}:v]")
    _run_ff(*ins, "-filter_complex", "".join(fc)
            + f"concat=n={len(parts)}:v=1:a=0[out]", "-map", "[out]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))
    return path


def _mk_project(tmp_path: Path, tag: str, *, with_ir: bool = False) -> Path:
    """最小工程:阶段目录 + 视频素材 + manifest(与 rs_ingest probe 字段同口径)。"""
    root = tmp_path / tag
    for key in ("brief", "materials", "sensed", "assets", "cut", "timeline",
                "output", "state"):
        (root / rs_paths.p(key)).mkdir(parents=True, exist_ok=True)
    media = _synth_scenes(root / rs_paths.p("materials") / "a.mp4",
                          [(1.0, "gray"), (1.0, "black"), (1.0, "white")])
    (root / rs_paths.manifest_json(root)).write_text(json.dumps({
        "version": 1, "items": [
            {"file": "a.mp4", "probe": "ok", "width": 320, "height": 240,
             "durationMs": 3000, "hasAudio": False, "ocr": "海边日落 航拍素材"}]},
        ensure_ascii=False), encoding="utf-8")
    if with_ir:
        ir = {"version": 1, "slug": tag, "fps": 30,
              "canvas": {"width": 1080, "height": 1920},
              "tracks": [{"id": "V1", "kind": "video", "name": "main", "clips": [
                  {"id": "V1-001", "src": rs_paths.rel(root, "materials", "a.mp4"),
                   "startMs": 0, "durationMs": 3000, "sourceInMs": 0}]}],
              "outputs": ["9x16"]}
        rs_paths.project_json(root).write_text(rs_edit.dump_json(ir), encoding="utf-8")
    return root


def _capture(fn, *args, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip().startswith("{")]
    doc = json.loads(lines[-1]) if lines else {}
    return code, doc


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ================================================================ §1 rs_beat

def test_beat_degraded_detect_and_schema(tmp_path):
    """降级档真实跑通:点击轨 → bpm≈120、拍点落 0.5s 网格;degraded 留痕齐。"""
    proj = tmp_path / "proj"
    rs_paths.ensure(proj)
    clicks = _synth_clicks(tmp_path / "clicks.wav", 6.0)
    code, doc = _capture(rs_beat.main, ["detect", str(clicks), "--out", str(proj)])
    assert code == 0 and doc["code"] == "BEATS_OK", doc
    beats_doc = _read(rs_beat.beats_path(proj))
    assert beats_doc["unit"] == "ms"
    assert abs(beats_doc["bpm"] - 120.0) <= 1.0, beats_doc["bpm"]
    assert beats_doc["beats"], "点击轨必须出拍点"
    assert all(isinstance(b, int) for b in beats_doc["beats"])
    heads = beats_doc["beats"][:4]
    assert all(abs((heads[i + 1] - heads[i]) - 500) <= 60 for i in range(len(heads) - 1)), heads
    # 降级留痕(beatnet 未部署是本环境事实,非 mock)
    assert beats_doc["degraded"] is True
    assert beats_doc["degradeReason"] == "onset-energy"
    assert beats_doc["missingComponent"] == "beatnet"
    assert beats_doc["engine"] == "onset-energy"
    assert 0.0 <= beats_doc["confidence"] <= 1.0


def test_beat_beats_json_feeds_beat_snap(tmp_path):
    """beats.json(ms)→ rs_edit beat.snap 吸附成功 —— 两脚本口径互通(防回归)。"""
    proj = tmp_path / "proj"
    clips = [{"id": f"V1-{i + 1:03d}", "src": rs_paths.rel(proj, "materials", "a.mp4"),
              "startMs": i * 4000, "durationMs": 4000, "sourceInMs": i * 4000}
             for i in range(2)]
    ir = {"version": 1, "slug": "p", "fps": 30, "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"id": "V1", "kind": "video", "name": "main", "clips": clips},
                     {"id": "A1", "kind": "audio", "clips": []}],
          "outputs": ["9x16"]}
    for key in ("materials", "cut", "timeline"):
        (proj / rs_paths.p(key)).mkdir(parents=True, exist_ok=True)
    rs_paths.project_json(proj).write_text(rs_edit.dump_json(ir), encoding="utf-8")
    (proj / "notes.json").write_text(rs_edit.dump_json({"version": 1, "items": []}),
                                     encoding="utf-8")
    (proj / rs_paths.p("cut") / "cutlist.json").write_text(rs_edit.dump_json(
        {"version": 1, "source": "fixture", "detector": "rs_cut", "cuts": [],
         "keep": [], "removedMs": 0, "srcTotalMs": 8000, "script": "rs_cut"}),
        encoding="utf-8")
    _synth_clicks(tmp_path / "clicks.wav", 6.0)
    code, _ = _capture(rs_beat.main, ["detect", str(tmp_path / "clicks.wav"),
                                      "--out", str(proj)])
    assert code == 0
    (proj / rs_paths.p("cut") / "beats.json").write_text(
        json.dumps(_read(rs_beat.beats_path(proj))), encoding="utf-8")
    doc0 = _read(rs_paths.project_json(proj))
    doc0["tracks"][0]["clips"][1]["startMs"] = 4005     # 距 4000ms 拍点 5ms
    rs_paths.project_json(proj).write_text(rs_edit.dump_json(doc0), encoding="utf-8")
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps({"baseRev": 0, "ops": [
        {"op": "beat.snap", "target": "V1-002", "after": {"windowMs": 60},
         "reason": "卡点"}]}, ensure_ascii=False), encoding="utf-8")
    code2, doc2 = _capture(rs_edit.main, ["apply", str(proj), "--ops", str(ops)])
    assert code2 == 0 and doc2["code"] == "APPLY_OK", doc2
    doc1 = _read(rs_paths.project_json(proj))
    assert doc1["tracks"][0]["clips"][1]["startMs"] == 4000, "拍点 4000ms 应吸中"


def test_beat_downbeat_degrades_beat_only(tmp_path):
    """--downbeat 缺 madmom → downbeats 空表 + tiers.downbeat beat-only 留痕。"""
    proj = tmp_path / "proj"
    rs_paths.ensure(proj)
    clicks = _synth_clicks(tmp_path / "clicks.wav", 4.0)
    code, _ = _capture(rs_beat.main, ["detect", str(clicks), "--out", str(proj),
                                      "--downbeat", "--stems"])
    doc = _read(rs_beat.beats_path(proj))
    assert code == 0
    assert doc["downbeats"] == []
    assert doc["tiers"]["downbeat"]["degradeReason"] == "beat-only"
    assert doc["tiers"]["stems"]["available"] is False
    assert doc["tiers"]["stems"]["note"], "混音检测处置必须给人话说明"


def test_beat_ready_tier_mock(tmp_path, monkeypatch):
    """READY 档(mock 推理,绝不真下载):engine=beatnet、downbeats 来自驱动、零降级。"""
    proj = tmp_path / "proj"
    rs_paths.ensure(proj)
    clicks = _synth_clicks(tmp_path / "clicks.wav", 3.0)

    def fake_state(cid: str) -> dict:
        base = {"component": cid, "state": "READY", "degrade": "", "size_mb": 0,
                "backend": "venv-dsp", "installDir": "", "message": ""}
        return base

    def fake_infer(py, media, out_json, mode, timeout=900):
        out_json.write_text(json.dumps({
            "bpm": 128.0, "meter": 4, "beats": [0, 469, 938, 1406],
            "downbeats": [0, 1875]}), encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(rs_fetchable, "state", fake_state)
    monkeypatch.setattr(rs_fetchable, "venv_python", lambda backend: Path(sys.executable))
    monkeypatch.setattr(rs_beat, "_venv_infer", fake_infer)
    code, _ = _capture(rs_beat.main, ["detect", str(clicks), "--out", str(proj),
                                      "--downbeat"])
    doc = _read(rs_beat.beats_path(proj))
    assert code == 0 and doc["engine"] == "beatnet" and doc["degraded"] is False
    assert doc["bpm"] == 128.0 and doc["downbeats"] == [0, 1875]
    assert doc["tiers"]["beats"]["degraded"] is False


@pytest.mark.perf
def test_beat_perf_budget_three_minutes(tmp_path):
    """§6.2:降级档 3 分钟音频 ≤0.02× 实时(≈3.6s)。"""
    proj = tmp_path / "proj"
    rs_paths.ensure(proj)
    clicks = _synth_clicks(tmp_path / "long.wav", 180.0)
    t0 = time.perf_counter()
    code, _ = _capture(rs_beat.main, ["detect", str(clicks), "--out", str(proj)])
    elapsed = time.perf_counter() - t0
    assert code == 0
    assert elapsed <= 0.02 * 180, f"降级档超预算:{elapsed:.2f}s > 3.6s"


# ================================================================ §2 rs_shot

def test_shot_degraded_detect_and_schema(tmp_path):
    """降级档真实跑通:三段纯色拼接 → 2 个切点、3 个镜头;留痕齐。"""
    proj = _mk_project(tmp_path, "shotp")
    media = proj / rs_paths.p("materials") / "a.mp4"
    code, doc = _capture(rs_shot.main, ["detect", str(media), "--out", str(proj)])
    assert code == 0 and doc["code"] == "SHOTS_OK"
    shots = _read(rs_shot.shots_path(proj))
    assert shots["engine"] == "frame-diff" and shots["degraded"] is True
    assert shots["degradeReason"] == "frame-diff" and shots["missingComponent"] == "scenedetect"
    assert len(shots["shots"]) == 3, shots["shots"]
    bounds = [s["startMs"] for s in shots["shots"][1:]]
    assert all(abs(b - 1000) <= SAMPLE_TOL_MS or abs(b - 2000) <= SAMPLE_TOL_MS
               for b in bounds), bounds
    assert shots["shots"][0]["startMs"] == 0 and shots["shots"][-1]["endMs"] >= 2900
    assert [t["kind"] for t in shots["transitions"]] == ["cut"] * len(shots["transitions"])
    for s in shots["shots"]:
        assert s["durMs"] >= rs_shot.MIN_SHOT_MS


SAMPLE_TOL_MS = 300            # 10fps 降采样 + 容差(切点粒度 ±100ms,留 CI 余量)


def test_shot_min_shot_merge():
    """MIN_SHOT_MS:过短镜头并入前一镜(纯函数单测,免 ffmpeg)。"""
    shots = rs_shot.cuts_to_shots([1000, 1150, 3000], 6000)
    # [1000,1150) 150ms < MIN_SHOT_MS → 并入前一镜([0,1000)→[0,1150)),下一镜起点 1150
    assert [s["startMs"] for s in shots] == [0, 1150, 3000], "150ms 镜头应并入前一镜"
    assert all(s["durMs"] >= rs_shot.MIN_SHOT_MS for s in shots)


def test_shot_ready_tier_mock(tmp_path, monkeypatch):
    """READY 档(mock scenedetect 子进程):engine=scenedetect、零降级。"""
    proj = tmp_path / "proj"
    rs_paths.ensure(proj)
    media = _synth_scenes(tmp_path / "v.mp4", [(1, "black"), (1, "white"), (1, "black")])

    def fake_state(cid: str) -> dict:
        return {"component": cid, "state": "READY", "degrade": "", "size_mb": 0,
                "backend": "py", "installDir": "", "message": ""}

    def fake_ready(media_p, out_json):
        out_json.write_text(json.dumps([[0.0, 1.0], [1.0, 2.0]]), encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(rs_fetchable, "state", fake_state)
    monkeypatch.setattr(rs_shot, "_ready_shots", fake_ready)
    code, _ = _capture(rs_shot.main, ["detect", str(media), "--out", str(proj)])
    doc = _read(rs_shot.shots_path(proj))
    assert code == 0 and doc["engine"] == "scenedetect" and doc["degraded"] is False
    assert [s["startMs"] for s in doc["shots"]] == [0, 1000, 2000], doc["shots"]


@pytest.mark.perf
def test_shot_perf_budget_sixty_seconds(tmp_path):
    """§6.2:降级档帧差切分 ≤0.05× 实时(60s 素材 ≤3s,比例外推 10 分钟 ≤30s)。"""
    proj = tmp_path / "proj"
    rs_paths.ensure(proj)
    parts = [(2.0, "black" if i % 2 == 0 else "white") for i in range(30)]  # 60s / 29 切
    media = _synth_scenes(tmp_path / "long.mp4", parts)
    t0 = time.perf_counter()
    code, _ = _capture(rs_shot.main, ["detect", str(media), "--out", str(proj)])
    elapsed = time.perf_counter() - t0
    assert code == 0
    assert elapsed <= 0.05 * 60, f"降级档超预算:{elapsed:.2f}s > 3s"


# ================================================================ §3 rs_reframe

def test_reframe_static_center_no_stretch(tmp_path):
    """降级档真实跑通:static-center + 裁切窗比恒等于目标比(裁切不拉伸)。"""
    proj = _mk_project(tmp_path, "rfp", with_ir=True)
    code, doc = _capture(rs_reframe.main, ["plan", str(proj)])
    assert code == 0
    plan = _read(rs_reframe.reframe_path(proj))
    assert plan["engine"] == "static-center" and plan["degraded"] is True
    assert plan["degradeReason"] == "static-center"
    clip = plan["clips"][0]
    assert clip["clipId"] == "V1-001" and clip["mode"] == "static-center"
    win = clip["cropWindow"]
    assert abs(win["w"] / win["h"] - 9 / 16) < 1e-6, "裁切窗比必须等于 9:16(不拉伸)"
    assert win["x0"] >= 0 and win["y0"] >= 0
    assert win["x0"] + win["w"] <= clip["srcWidth"] + 1e-6
    assert clip["scale"] == rs_reframe.DEFAULT_SCALE
    assert clip["trajectory"] and clip["violations"] == []


def test_reframe_crop_window_pure():
    """纯几何:居中夹紧 + 越界收缩(免 ffmpeg 直测)。"""
    win = rs_reframe.crop_window(1920, 1080, 960, 540, 1.0, 9 / 16)
    assert abs(win["w"] / win["h"] - 9 / 16) < 1e-6
    assert win["x0"] == round((1920 - win["w"]) / 2, 2)
    # 窄源(竖 1000x4000)进宽目标 16:9:全高窗超源宽 → 以源宽为限收缩
    win2 = rs_reframe.crop_window(1000, 4000, 500, 2000, 1.0, 16 / 9)
    assert win2["w"] == 1000.0 and abs(win2["h"] - 1000 * 9 / 16) < 1e-6
    assert abs(win2["w"] / win2["h"] - 16 / 9) < 1e-6, "收缩后窗比仍须等于目标比"


def test_reframe_subject_violation_downgrades(tmp_path, monkeypatch):
    """硬约束 REFRAME_CLIP_SUBJECT:跟踪轨迹切破主体 → 整 clip 降级 + violations 留痕。"""
    proj = _mk_project(tmp_path, "rfv", with_ir=True)
    boxes = [(10.0, 10.0, 300.0, 230.0)] * 30       # 320x240 源里占大半画面 → 9:16 窗必切破
    monkeypatch.setattr(rs_reframe, "ready_tier",
                        lambda: (True, {"track": {"engine": "bytetrack", "degraded": False}}))
    monkeypatch.setattr(rs_reframe, "ready_subject_boxes", lambda media, max_frames=600: boxes)
    code, _ = _capture(rs_reframe.main, ["plan", str(proj), "--force"])
    assert code == 0
    clip = _read(rs_reframe.reframe_path(proj))["clips"][0]
    assert clip["mode"] == "static-center", "切破主体必须退回居中"
    assert any(v["code"] == "REFRAME_CLIP_SUBJECT" for v in clip["violations"])
    assert clip["degradeNote"]


def test_reframe_track_path_and_rate_limit(tmp_path, monkeypatch):
    """READY 档(主体盒 mock,轨迹数学真跑):mode=track + 限速 ≤ MAX_SHIFT_PX_PER_SEC/帧。"""
    proj = _mk_project(tmp_path, "rft", with_ir=True)
    boxes = [(40.0 + i * 1.0, 100.0, 60.0 + i * 1.0, 140.0) for i in range(90)]
    monkeypatch.setattr(rs_reframe, "ready_tier",
                        lambda: (True, {"track": {"engine": "bytetrack", "degraded": False}}))
    monkeypatch.setattr(rs_reframe, "ready_subject_boxes", lambda media, max_frames=600: boxes)
    code, _ = _capture(rs_reframe.main, ["plan", str(proj), "--force"])
    assert code == 0
    plan = _read(rs_reframe.reframe_path(proj))
    clip = plan["clips"][0]
    assert clip["mode"] == "track" and len(clip["trajectory"]) >= 60
    max_step = rs_reframe.MAX_SHIFT_PX_PER_SEC / 30.0
    for a, b in zip(clip["trajectory"], clip["trajectory"][1:]):
        step = ((b["anchorX"] - a["anchorX"]) ** 2 + (b["anchorY"] - a["anchorY"]) ** 2) ** 0.5
        assert step <= max_step + 1e-6, f"锚点超速:{step} > {max_step}"


def test_reframe_smooth_track_rate_unit():
    """滑窗中位数 + 限速纯函数:尖峰被压平(免 ffmpeg 直测)。"""
    pts = [(100.0, 100.0)] * 10 + [(400.0, 100.0)] + [(100.0, 100.0)] * 10
    out = rs_reframe.smooth_track(pts, fps=30.0)
    assert len(out) == len(pts)
    max_step = rs_reframe.MAX_SHIFT_PX_PER_SEC / 30.0
    for a, b in zip(out, out[1:]):
        assert ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 <= max_step + 1e-6


def test_reframe_cached_skip(tmp_path):
    """产物新于 IR → 跳过重算(挂载器三连跑不重复做功)。"""
    proj = _mk_project(tmp_path, "rfc", with_ir=True)
    code1, _ = _capture(rs_reframe.main, ["plan", str(proj)])
    code2, doc2 = _capture(rs_reframe.main, ["plan", str(proj)])
    assert code1 == 0 and code2 == 0 and doc2["code"] == "REFRAME_CACHED"


# ================================================================ §4 rs_broll

def test_broll_keyword_match_deterministic(tmp_path):
    """降级档真实跑通:文件名/OCR 命中加权;排序全序确定;degraded 留痕。"""
    proj = _mk_project(tmp_path, "brp")
    man = rs_paths.manifest_json(proj)
    doc = _read(man)
    doc["items"] += [
        {"file": "beach_sunset.mp4", "probe": "ok", "ocr": "海边航拍"},
        {"file": "office.mp4", "probe": "ok"},
    ]
    man.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    code, out = _capture(rs_broll.main, ["match", str(proj), "--query", "海边 航拍 日落"])
    assert code == 0
    res = _read(rs_broll.broll_path(proj))
    assert res["degraded"] is True and res["degradeReason"] == "keyword-match"
    assert res["missingComponent"] == "clip"
    files = [m["file"] for m in res["matches"]]
    assert "a.mp4" in files and "beach_sunset.mp4" in files and "office.mp4" not in files
    top = res["matches"][0]
    assert top["source"] == "keyword-match" and top["score"] >= 2
    # 确定性:同输入两次 → 字节级同 matches
    _capture(rs_broll.main, ["match", str(proj), "--query", "海边 航拍 日落"])
    assert _read(rs_broll.broll_path(proj))["matches"] == res["matches"]


def test_broll_tokenize_and_empty_query(tmp_path):
    """tokenizer:CJK 二字滑窗 + 西文小写;空 query → 空匹配不猜。"""
    toks = rs_broll.tokenize("海边航拍 Sunset")
    assert "海边" in toks and "边航" in toks and "sunset" in toks
    proj = _mk_project(tmp_path, "bre")
    code, _ = _capture(rs_broll.main, ["match", str(proj), "--query", "  "])
    res = _read(rs_broll.broll_path(proj))
    assert code == 0 and res["matches"] == [] and res.get("note")


def test_broll_ready_tier_mock(tmp_path, monkeypatch):
    """READY 档(mock clip 子进程):engine=clip、零降级。"""
    proj = _mk_project(tmp_path, "brr")
    monkeypatch.setattr(rs_fetchable, "state", lambda cid: {
        "component": cid, "state": "READY", "degrade": "", "size_mb": 0,
        "backend": "py", "installDir": "", "message": ""})

    def fake_rank(query, items, out_json):
        out_json.write_text(json.dumps(
            [{"file": "a.mp4", "score": 0.97, "matched": [], "source": "clip"}]),
            encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(rs_broll, "_clip_rank", fake_rank)
    code, _ = _capture(rs_broll.main, ["match", str(proj), "--query", "海边"])
    res = _read(rs_broll.broll_path(proj))
    assert code == 0 and res["engine"] == "clip" and res["degraded"] is False
    assert res["matches"][0]["score"] == 0.97


# ================================================================ §5 rs_screen

def test_screen_waiting_detection_real(tmp_path):
    """waiting 真实跑通:1s 花 + 3s 黑 + 1s 白、无音轨 → waiting [1000,4000) 3000ms。"""
    proj = _mk_project(tmp_path, "scw")
    media = proj / rs_paths.p("materials") / "a.mp4"
    _synth_scenes(media, [(1.0, "gray"), (3.0, "black"), (1.0, "white")])
    code, _ = _capture(rs_screen.main, ["analyze", str(media), "--waiting",
                                        "--out", str(proj)])
    assert code == 0
    doc = _read(rs_screen.screen_path(proj))
    waiting = doc["waiting"]
    assert len(waiting) == 1, waiting
    w = waiting[0]
    assert abs(w["startMs"] - 1000) <= 600 and abs(w["endMs"] - 4000) <= 600
    assert w["ms"] >= rs_screen.WAITING_MIN_MS
    assert doc["waitingTotalMs"] >= rs_screen.WAITING_MIN_MS
    assert doc["tiers"]["waiting"]["degraded"] is False


def test_screen_schema_and_sections(tmp_path):
    """产物 schema:cursorTrace/clickPoints/keyHints/redactBoxes/zoomPlan 各节齐。"""
    proj = _mk_project(tmp_path, "scs")
    media = proj / rs_paths.p("materials") / "a.mp4"
    code, _ = _capture(rs_screen.main, ["analyze", str(media), "--out", str(proj),
                                        "--cursor", "--zoom", "--keys", "--redact",
                                        "--waiting"])
    assert code == 0
    doc = _read(rs_screen.screen_path(proj))
    for key in ("cursorTrace", "clickPoints", "keyHints", "redactBoxes", "waiting",
                "zoomPlan", "tiers"):
        assert key in doc, f"screen.json 缺 {key}"
    assert doc["tiers"]["redact"].get("note"), "redact 必须带 L1 提醒"


def test_screen_degraded_when_cv_missing(tmp_path, monkeypatch):
    """缺组件(state mock → MISSING)→ 各节按 §5.5.4 降级 + 顶层 degraded 留痕。"""
    proj = _mk_project(tmp_path, "scd")
    media = proj / rs_paths.p("materials") / "a.mp4"

    def fake_state(cid: str) -> dict:
        return {"component": cid, "state": "MISSING", "degrade": "none", "size_mb": 60,
                "backend": "py", "installDir": "", "message": "缺模块(测试注入)"}

    monkeypatch.setattr(rs_fetchable, "state", fake_state)
    code, _ = _capture(rs_screen.main, ["analyze", str(media), "--out", str(proj),
                                        "--cursor", "--zoom", "--keys", "--redact"])
    doc = _read(rs_screen.screen_path(proj))
    assert code == 0
    assert doc["degraded"] is True and doc["degradeReason"] == "none"
    assert doc["missingComponent"] == "opencv"
    assert set(doc["degradedSections"]) == {"cursor", "zoom", "keys", "redact"}
    assert doc["cursorTrace"] == [] and doc["keyHints"] == []
    assert doc["zoomPlan"][0]["mode"] == "static-zoom"


def test_screen_ready_cursor_and_clicks(tmp_path, monkeypatch):
    """READY 档(轨迹 mock,点击数学真跑):滞留 ≥400ms 后快移 → 点击点 + zoomPlan。"""
    proj = _mk_project(tmp_path, "scr")
    media = proj / rs_paths.p("materials") / "a.mp4"
    trace = [{"tMs": 0, "xPct": 50.0, "yPct": 50.0},
             {"tMs": 500, "xPct": 50.2, "yPct": 50.1},
             {"tMs": 520, "xPct": 60.0, "yPct": 30.0}]
    monkeypatch.setattr(rs_screen, "ready_cursor_trace", lambda m, c: trace)
    code, _ = _capture(rs_screen.main, ["analyze", str(media), "--out", str(proj),
                                        "--cursor", "--zoom"])
    doc = _read(rs_screen.screen_path(proj))
    assert code == 0
    assert doc["clickPoints"] and doc["clickPoints"][0]["tMs"] == 500
    assert doc["zoomPlan"][0]["scale"] == rs_screen.ZOOM_FACTOR


# ================================================================ §6 挂载集成(ADR-0047)

def test_mount_runs_detector_and_records_ok(tmp_path):
    """挂载器无参执行 detector(工程模式):vlog S2 挂 vision.shot → shots.json → ok。"""
    proj = _mk_project(tmp_path, "mount")
    d = proj / rs_paths.p("brief")
    d.mkdir(parents=True, exist_ok=True)
    (d / "intent_decisions.json").write_text(json.dumps({
        "version": 1, "kind": "cutflow-intent-decisions",
        "resolved": {"videoType": "vlog",
                     "capabilities": ["vision.shot", "vision.track", "vision.reframe"]}},
        ensure_ascii=False), encoding="utf-8")
    st = {"id": "S2", "name": "粗剪处理", "scripts": ["rs_cut.py"]}
    info: dict = {}
    ok, msg = rs_run.run_stage_capabilities(proj, st, info)
    assert ok, msg
    entry = info["capabilities"][0]
    assert entry["id"] == "vision.shot" and entry["status"] == "ok"
    assert entry["degraded"] is False
    assert rs_shot.shots_path(proj).is_file()


def test_mount_undeployable_reports_degraded(tmp_path, monkeypatch):
    """产物缺失 = 能力未部署 → 降级留痕(此处以 state mock 令 detector 走 NO_MEDIA 分支)。"""
    proj = _mk_project(tmp_path, "mount2")
    # 清空素材目录并移走 manifest → 工程模式无素材 → 挂载器按描述符降级留痕
    for f in (proj / rs_paths.p("materials")).iterdir():
        f.unlink()
    d = proj / rs_paths.p("brief")
    (d / "intent_decisions.json").write_text(json.dumps({
        "version": 1, "kind": "cutflow-intent-decisions",
        "resolved": {"videoType": "vlog", "capabilities": ["vision.shot"]}},
        ensure_ascii=False), encoding="utf-8")
    ok, msg = rs_run.run_stage_capabilities(
        proj, {"id": "S2", "name": "x", "scripts": ["rs_cut.py"]}, {})
    rep = _read(rs_run.capabilities_report_path(proj))
    entry = rep["stages"]["S2"][0]
    assert entry["degraded"] is True and entry["trace"] == "shotDegraded"
    assert "message" in entry and ok


# ================================================================ §7 BGM 曲库与 --bgm auto(清欠账 #13)

def test_bgm_library_manifest_complete():
    """曲库完备性:4 条、15–45s、文件在盘、pacingFit 覆盖四档、自产声明。"""
    man = _read(BGM_DIR / "manifest.json")
    assert man["version"] == 1 and "无版权" in man["license"]
    tracks = man["tracks"]
    assert 3 <= len(tracks) <= 5
    fits: set[str] = set()
    for t in tracks:
        assert 15 <= t["durationSec"] <= 45, t["file"]
        assert (BGM_DIR / t["file"]).is_file(), t["file"]
        assert 60 <= t["bpm"] <= 200 and t["name"] and t["mood"]
        fits |= set(t["pacingFit"])
    assert {"music", "fast", "normal", "slow"} <= fits, "四档必须全有曲可选"


def test_bgm_auto_compile_picks_from_library(tmp_path):
    """bgm=auto:music 档选中 pulse_rhythm;decision source=library;两次编译字节级一致。"""
    brief = tmp_path / "b.json"
    plan = tmp_path / "p.json"
    brief.write_text(json.dumps({"videoType": "混剪", "pacing": "music", "bgm": "auto",
                                 "title": "t", "platform": "douyin"},
                                ensure_ascii=False), encoding="utf-8")
    plan.write_text("{}", encoding="utf-8")
    outs = []
    for i in range(2):
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / "rs_intent.py"), "compile",
             "--brief", str(brief), "--plan", str(plan), "--out", str(tmp_path / f"o{i}")],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert r.returncode == 0, r.stdout + r.stderr
        outs.append((tmp_path / f"o{i}" / rs_paths.p("brief") / "intent_decisions.json")
                    .read_text(encoding="utf-8"))
    assert outs[0] == outs[1], "bgm=auto 编译必须字节级可复现"
    doc = json.loads(outs[0])
    bgm = doc["resolved"]["bgm"]
    assert bgm["mode"] == "auto" and bgm["enabled"] is True
    assert bgm["src"] == "skills/cutflow/assets/bgm/pulse_rhythm_140bpm.mp3"
    pick_dec = [x for x in doc["decisions"] if x["id"] == "intent:bgm.pick"]
    assert pick_dec and pick_dec[0]["source"] == "library", "选曲必须 source=library 留痕"
    # 非循环档 → 曲库首条兜底(确定性)
    pick = rs_intent.bgm_library_pick("music")
    assert pick and pick["file"] == "pulse_rhythm_140bpm.mp3"
    assert rs_intent.bgm_library_pick("slow")["file"] == "ambient_calm_90bpm.mp3"


def test_bgm_auto_wires_into_ir(tmp_path):
    """rs_ir 接线:resolved.bgm.src(库选)→ IR.bgm 绝对路径 + _meta.bgmFrom=library。"""
    root = tmp_path / "proj"
    for key in ("brief", "materials", "cut", "timeline", "output", "state",
                "sensed", "assets"):
        (root / rs_paths.p(key)).mkdir(parents=True, exist_ok=True)
    (root / rs_paths.p("brief") / "intent_decisions.json").write_text(json.dumps({
        "version": 1, "kind": "cutflow-intent-decisions",
        "resolved": {"bgm": {"mode": "auto", "enabled": True, "gainDb": -12,
                             "src": "skills/cutflow/assets/bgm/pulse_rhythm_140bpm.mp3"}}},
        ensure_ascii=False), encoding="utf-8")
    (root / rs_paths.p("cut") / "cutlist.applied.json").write_text(json.dumps({
        "version": 1, "source": rs_paths.rel(root, "materials", "a.mp4"),
        "keep": [[0, 2000]], "cuts": [], "removedMs": 0, "srcTotalMs": 2000}),
        encoding="utf-8")
    shutil.copy2(REPO / "tests" / "fixtures" / "diagnosis" / "source.mp4",
                 root / rs_paths.p("materials") / "a.mp4") if \
        (REPO / "tests" / "fixtures" / "diagnosis" / "source.mp4").is_file() else \
        _synth_scenes(root / rs_paths.p("materials") / "a.mp4", [(2.0, "black")])
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "rs_ir.py"), "build",
         "--from-cutlist", str(root / rs_paths.p("cut") / "cutlist.applied.json"),
         "--slug", "proj", "--out", str(rs_paths.project_json(root))],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stdout + r.stderr
    ir = _read(rs_paths.project_json(root))
    assert ir["bgm"]["src"] == str(BGM_DIR / "pulse_rhythm_140bpm.mp3")
    assert Path(ir["bgm"]["src"]).is_file(), "IR.bgm 必须是存在的绝对路径(渲染端可消费)"
    assert ir["_meta"]["bgmFrom"] == "intent-library"
    assert ir["bgm"]["gainDb"] == -12 and ir["bgm"]["ducking"] is True


# ================================================================ §8 探测位修复 / 登记对拍

def test_artboard_probe_hits_real_pack():
    """M8 修复:rs_artboard 直接读 skills/cutflow/templates/styles/packs/(免镜像桥)。"""
    css, warns = rs_artboard.load_style_pack("mixcut-douyin")
    assert css and "/* pack:mixcut-douyin */" in css, "真包 frames.css 必须直接命中"
    assert warns == []
    css2, warns2 = rs_artboard.load_style_pack("no-such-pack")
    assert css2 == "" and any("stylePackMissing" in w for w in warns2)


def test_registry_and_pack_capabilities_consistent():
    """registry.videoTypes capabilities ⊆ 已登记描述符;packs capabilities 与 registry 同步。"""
    reg = rs_intent.load_registry()
    descs = rs_run.load_capability_descriptors()
    for vt, meta in reg["videoTypes"].items():
        for cid in meta.get("capabilities") or []:
            assert cid in descs, f"{vt} 声明了未登记能力 {cid}"
    assert set(reg["videoTypes"]["混剪"]["capabilities"]) == {
        "qc.black-frame", "audio.beat", "audio.downbeat", "audio.stem"}
    assert {"vision.shot", "vision.track", "vision.reframe"} <= set(
        reg["videoTypes"]["vlog"]["capabilities"])
    assert set(reg["videoTypes"]["screen-recording"]["capabilities"]) == {
        "screen.cursor", "screen.zoom", "screen.keys", "screen.redact"}
    assert {"text.broll", "sub.pair", "drama.hook"} <= set(
        reg["videoTypes"]["drama"]["capabilities"]), "drama 第二波已登记(sub.pair/drama.hook/text.broll)"
    for slug_dir in sorted(rs_stylepack.PACKS_DIR.iterdir()):
        if not slug_dir.is_dir() or slug_dir.name.startswith("_"):
            continue
        pack = rs_stylepack.load_pack(slug_dir.name)
        vt = pack["params"]["videoType"]
        expected = reg["videoTypes"].get(vt, {}).get("capabilities") or []
        assert sorted(pack["params"]["capabilities"]) == sorted(expected), \
            f"{slug_dir.name}: pack capabilities 必须与 registry.videoTypes.{vt} 同步"


def test_editing_grammar_doc_complete():
    """手法库:25 条逐条在册 + 禁忌表 7 条 + 落点核查表(命令可解析性归 manual-gate 门禁)。"""
    doc = (REPO / "skills" / "cutflow" / "rules" / "editing-grammar.md").read_text(
        encoding="utf-8")
    assert "### 25. 静音压缩(waiting)" in doc
    for i in range(1, 26):
        assert f"### {i}." in doc, f"手法 {i} 缺条目"
    grades = ("【规范】", "【研究】", "【经验】", "【内部】")
    for g in grades:
        assert g in doc, f"分级 {g} 必须有用例"
    assert doc.count("| 【") >= 7, "禁忌表 7 条分级必须逐条在册"
    assert "REFRAME_CLIP_SUBJECT" in doc and "M8 落点核查表" in doc


# ================================================================ §9 工程模式(挂载器契约)直跑

def test_project_mode_no_args_writes_artifacts(tmp_path):
    """五脚本无参(cwd=工程根)→ 各产物落盘 —— 挂载器/第二波 Agent 的默认入口。"""
    proj = _mk_project(tmp_path, "nomode", with_ir=True)
    for script in ("rs_beat.py", "rs_shot.py", "rs_screen.py", "rs_reframe.py",
                   "rs_broll.py"):
        r = subprocess.run([sys.executable, str(SCRIPTS / script)], cwd=str(proj),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300,
                           env={**os.environ, "PYTHONPATH": str(SCRIPTS)})
        assert r.returncode == 0, f"{script} 工程模式失败:{r.stdout[-200:]}{r.stderr[-200:]}"
    assert rs_beat.beats_path(proj).is_file()
    assert rs_shot.shots_path(proj).is_file()
    assert rs_screen.screen_path(proj).is_file()
    assert rs_reframe.reframe_path(proj).is_file()
    assert rs_broll.broll_path(proj).is_file()
    beats = _read(rs_beat.beats_path(proj))
    assert beats["unit"] == "ms" and "beats" in beats
