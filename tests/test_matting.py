# -*- coding: utf-8 -*-
"""M9 抠像重建与质量门禁(ADR-0050)测试:五项指标合成夹具判定 + S0 分流 + IR matte 块。

覆盖面(方案 §7.1 门禁 15):
· 指标计算器:干净/抖动/毛边/硬二值 四组合成 alpha 序列 → 判定方向正确;
· gate 全流程:引擎 READY(mock)→ PASS exit0 / FAIL exit4 + MATTE_QUALITY_FAIL;
  引擎 MISSING → blocked + MATTE_ENGINE_MISSING + missingComponent(懒加载三态);
· WARN 档:jitterRate 略超理想值但 IoU 达标 → warn(交付说明须标注);
· rs_ir add-matte:pass/warn 入 IR,fail 拒绝(MATTE_QUALITY_FAIL);chroma 仍被拒;
· rs_render matte 预合成:alphamerge+overlay 真跑,前景/背景像素真值;alpha 缺失回退;
· rs_ingest scan --allow-auto-matting 分流:S0 (b) 路径 PASS 放行 / FAIL 阻断;
· CACHE_VER == v9(matte 渲染语义变更,ADR-0050;v8 已被 M8 reframe 消费)。
"""
from __future__ import annotations

import contextlib
import io
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
import rs_ingest  # noqa: E402
import rs_ir  # noqa: E402
import rs_jy_draft  # noqa: E402
import rs_matting  # noqa: E402
import rs_paths  # noqa: E402
import rs_render  # noqa: E402

try:
    import cv2  # noqa: F401
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


def _ffmpeg_bin() -> str:
    try:
        cfg = rs_common.load_config()
        p = rs_common.ffmpeg_bin(cfg)
        if Path(p).is_file():
            return p
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


import rs_common  # noqa: E402

FF = _ffmpeg_bin()


def _run_ff(*args: str) -> None:
    p = subprocess.run([FF, "-v", "error", "-y", *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300)
    assert p.returncode == 0, f"ffmpeg 合成失败:{p.stderr[-300:]}"


def _capture(fn, *args, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip().startswith("{")]
    return code, (json.loads(lines[-1]) if lines else {})


def _capture_argv(fn, argv: list[str]):
    old = sys.argv
    sys.argv = argv
    try:
        return _capture(fn)
    finally:
        sys.argv = old


def _disc_alpha(w: int, h: int, cx: int, cy: int, r: int, hard: bool = False):
    """反锯齿圆盘 alpha(0-1 float);hard=True 时二值化(测 transparencySpread 退化)。"""
    yy, xx = np.mgrid[0:h, 0:w].astype(float)
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    a = np.clip(r - d + 0.5, 0.0, 1.0)          # 1px 羽化
    if hard:
        a = (a > 0.5).astype(float)
    return a


def _write_alpha_dir(d: Path, alphas: list) -> int:
    """写 alpha PNG 序列,返回张数(cv2.imwrite 对中文路径不可靠 → imencode+write_bytes)。"""
    d.mkdir(parents=True, exist_ok=True)
    for i, a in enumerate(alphas):
        ok, buf = cv2.imencode(".png", (a * 255).astype("uint8"))
        assert ok, f"PNG 编码失败:{d}"
        (d / f"alpha_{i:05d}.png").write_bytes(buf.tobytes())
    return len(alphas)


def _gray_video(path: Path, frames: int = 6, w: int = 320, h: int = 240,
                color: str = "0x808080") -> Path:
    _run_ff("-f", "lavfi", "-i", f"color=c={color}:size={w}x{h}:rate=10:duration={frames / 10:g}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path))
    return path


@pytest.mark.skipif(not HAS_CV2, reason="opencv/numpy 不可用(指标计算器必需)")
class TestMetrics:
    """五项指标在合成夹具上的判定方向(方案门禁 15 ①)。"""

    def test_clean_disc_passes(self):
        alphas = [_disc_alpha(320, 240, 160, 120, 60) for _ in range(6)]
        m = {"temporalIoU": 1.0, "jitterRate": 0.0, "haloRatio": 0.0,
             "detailRetention": 1.0, "transparencySpread": 0.0}
        m.update(rs_matting.temporal_metrics(alphas))
        m["transparencySpread"] = min(rs_matting.transparency_spread(a) for a in alphas)
        assert m["temporalIoU"] >= rs_matting.MATTE_IOU_MIN
        assert m["jitterRate"] <= rs_matting.MATTE_JITTER_MAX
        assert m["transparencySpread"] > rs_matting.MATTE_TRANSPARENCY_MIN
        verdict, _, _ = rs_matting.verdict_of(m, fps=30.0, use_gpu=True)
        assert verdict == "pass"

    def test_jitter_fails(self):
        alphas = [_disc_alpha(320, 240, 160, 120, r) for r in (60, 42, 60, 42, 60, 42)]
        m = rs_matting.temporal_metrics(alphas)
        assert m["temporalIoU"] < rs_matting.MATTE_IOU_MIN, m
        verdict, _, _ = rs_matting.verdict_of(
            {**m, "haloRatio": 0.0, "detailRetention": 1.0, "transparencySpread": 0.01},
            fps=30.0, use_gpu=True)
        assert verdict == "fail"

    def test_halo_fails(self):
        frame = np.full((240, 320, 3), (80, 80, 80), np.uint8)
        # 边缘带画绿环(幕色渗出):半径 60±3 的环
        cv2.circle(frame, (160, 120), 60, (60, 220, 60), 6)
        a = _disc_alpha(320, 240, 160, 120, 60)
        ratio = rs_matting.halo_ratio(frame, a, (60, 220, 60))
        assert ratio > rs_matting.MATTE_HALO_MAX, ratio

    def test_hard_binary_alpha_fails(self):
        a = _disc_alpha(320, 240, 160, 120, 60, hard=True)
        spread = rs_matting.transparency_spread(a)
        assert spread < rs_matting.MATTE_TRANSPARENCY_MIN
        verdict, _, _ = rs_matting.verdict_of(
            {"temporalIoU": 1.0, "jitterRate": 0.0, "haloRatio": 0.0,
             "detailRetention": 1.0, "transparencySpread": spread},
            fps=30.0, use_gpu=True)
        assert verdict == "fail"

    def test_warn_level_for_mild_jitter(self):
        """WARN 级实证:jitterRate 略超但 IoU 达标 → warn(不阻断,交付说明标注)。"""
        verdict, warnings, _ = rs_matting.verdict_of(
            {"temporalIoU": 0.991, "jitterRate": rs_matting.MATTE_JITTER_MAX * 2,
             "haloRatio": 0.0, "detailRetention": 1.0, "transparencySpread": 0.01},
            fps=30.0, use_gpu=True)
        assert verdict == "warn" and "jitterRate" in warnings


@pytest.mark.skipif(not FF or not HAS_CV2, reason="ffmpeg/opencv 不可用")
class TestGate:
    """gate 全流程:引擎 mock(不真下载),判定与退出码按 ADR-0050 三态。"""

    def _mk(self, tmp_path: Path) -> Path:
        root = tmp_path / "proj"
        for key in ("00_制作简报", "01_原始素材", "05_时间线工程"):
            (root / key).mkdir(parents=True)
        return root

    def test_pass_flow(self, tmp_path, monkeypatch):
        root = self._mk(tmp_path)
        src = _gray_video(tmp_path / "a.mp4", frames=6)
        monkeypatch.setattr(rs_matting, "_engine_state",
                            lambda e: {"state": "READY", "component": "vision.matting",
                                       "size_mb": 480, "backend": "venv-torch"})
        alphas = [_disc_alpha(320, 240, 160, 120, 60) for _ in range(6)]
        monkeypatch.setattr(rs_matting, "_run_engine",
                            lambda s, o, e, mf: _write_alpha_dir(o, alphas))
        code, doc = rs_matting.gate(root, src, max_frames=6)
        assert code == 0 and doc["verdict"] == "pass", doc
        q = json.loads((rs_paths.of(root, "timeline") / "matte" / "quality.json")
                       .read_text(encoding="utf-8"))
        assert q["verdict"] == "pass" and q["metrics"]["temporalIoU"] >= 0.985

    def test_fail_flow_blocks(self, tmp_path, monkeypatch):
        root = self._mk(tmp_path)
        src = _gray_video(tmp_path / "a.mp4", frames=6)
        monkeypatch.setattr(rs_matting, "_engine_state",
                            lambda e: {"state": "READY", "component": "vision.matting"})
        alphas = [_disc_alpha(320, 240, 160, 120, r) for r in (60, 42, 60, 42, 60, 42)]
        monkeypatch.setattr(rs_matting, "_run_engine",
                            lambda s, o, e, mf: _write_alpha_dir(o, alphas))
        code, doc = rs_matting.gate(root, src, max_frames=6)
        assert code == 4 and doc["verdict"] == "fail", doc

    def test_engine_missing_blocks(self, tmp_path, monkeypatch):
        root = self._mk(tmp_path)
        src = _gray_video(tmp_path / "a.mp4", frames=6)
        monkeypatch.setattr(rs_matting, "_engine_state",
                            lambda e: {"state": "MISSING", "component": "vision.matting",
                                       "size_mb": 480, "message": "venv 未创建"})
        code, doc = rs_matting.gate(root, src)
        assert code == 4 and doc["verdict"] == "blocked"
        assert doc["missingComponent"] == "vision.matting"
        assert "rs_fetchable install" in doc["suggestions"][0]


@pytest.mark.skipif(not FF or not HAS_CV2, reason="ffmpeg/opencv 不可用")
class TestWiring:
    """IR matte 块 + 渲染预合成 + S0 分流 + 剪映降级标注 + CACHE_VER。"""

    def test_cache_ver_v9(self):
        # v10:M13 fxId 注册表(fx 声明/转场边界副效进段内容)再次失效旧 segcache
        # (语义变更必须失效旧 segcache;v9 前史见 ADR-0050)
        assert rs_render.CACHE_VER == "v10", "渲染语义变更必须失效旧 segcache"

    def test_verify_l0_matte_check(self, tmp_path):
        """rs_verify L0 抠像判据:无报告 skipped;fail 红;warn 绿但留提示。"""
        import rs_verify
        root = tmp_path / "proj"
        (root / rs_paths.p("timeline") / "matte").mkdir(parents=True)
        assert rs_verify.check_matte(root).get("skipped"), "无报告必须 skipped"
        q = root / rs_paths.p("timeline") / "matte" / "quality.json"
        q.write_text(json.dumps({"verdict": "fail", "suggestions": ["换背景"]}),
                     encoding="utf-8")
        chk = rs_verify.check_matte(root)
        assert chk["ok"] is False and "换背景" in chk["detail"]
        q.write_text(json.dumps({"verdict": "warn", "metrics": {}}), encoding="utf-8")
        chk2 = rs_verify.check_matte(root)
        assert chk2["ok"] is True and "边缘质量一般" in chk2.get("note", "")

    def test_ir_add_matte_and_chroma_still_rejected(self, tmp_path):
        root = tmp_path / "proj"
        (root / rs_paths.p("timeline")).mkdir(parents=True)
        src_dir = root / rs_paths.p("materials")
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "a.mp4").write_bytes(b"")          # validate 校验 src 存在性
        ir = root / rs_paths.p("timeline") / "project.json"
        doc = {"version": 1, "slug": "p", "fps": 30,
               "canvas": {"width": 1080, "height": 1920},
               "tracks": [{"kind": "video", "name": "main", "clips": [
                   {"id": "c0001", "src": "01_原始素材/a.mp4", "startMs": 0,
                    "durationMs": 1000, "sourceInMs": 0}]}],
               "outputs": ["9x16"]}
        ir.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        qpath = root / "quality.json"
        qpath.write_text(json.dumps({"verdict": "pass", "engine": "rvm",
                                     "metrics": {"temporalIoU": 0.99}}), encoding="utf-8")
        code, out = _capture_argv(rs_ir.main, [
            "rs_ir.py", "add-matte", str(ir), "--clip", "c0001",
            "--quality", str(qpath), "--matte-bg", "03_创作素材/背景/bg.png"])
        assert code == 0 and out["code"] == "MATTE_APPLIED", out
        saved = json.loads(ir.read_text(encoding="utf-8"))
        matte = saved["tracks"][0]["clips"][0]["matte"]
        assert matte["engine"] == "rvm" and matte["quality"]["verdict"] == "pass"
        # schema 白名单:带 matte 的 IR 校验零错误
        errs = rs_ir.validate(saved, root)
        assert not errs, errs
        # fail 判定拒绝写入(不达标不启用)
        qpath.write_text(json.dumps({"verdict": "fail", "engine": "rvm"}), encoding="utf-8")
        code2, out2 = _capture_argv(rs_ir.main, [
            "rs_ir.py", "add-matte", str(ir), "--clip", "c0001", "--quality", str(qpath)])
        assert code2 == 4 and out2["code"] == "MATTE_QUALITY_FAIL"
        # chroma/background 仍被拒(ADR-0031 语义不复活)
        bad = {**doc, "tracks": [{"kind": "video", "name": "main", "clips": [
            {**doc["tracks"][0]["clips"][0], "chroma": {"key": "green"}}]}]}
        assert rs_ir.validate(bad, root), "chroma 必须仍然报错"

    def test_render_matte_precompose(self, tmp_path):
        """alphamerge+overlay 真跑:前景绿盘留在绿底上,角落露出红背景;alpha 缺失回退 None。"""
        root = tmp_path / "proj"
        (root / rs_paths.p("timeline")).mkdir(parents=True)
        src = tmp_path / "src.mp4"
        _run_ff("-f", "lavfi", "-i", "color=c=green:size=320x240:rate=10:duration=0.8",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src))
        bg = tmp_path / "bg.png"
        cv2.imwrite(str(bg), np.full((240, 320, 3), (60, 60, 220), np.uint8))
        adir = root / rs_paths.p("timeline") / "matte" / "alpha"
        _write_alpha_dir(adir, [_disc_alpha(320, 240, 160, 120, 60) for _ in range(9)])
        build = tmp_path / "build"
        build.mkdir()
        clip = {"matte": {"engine": "rvm", "alphaDir": "05_时间线工程/matte/alpha",
                          "bg": {"src": str(bg), "mode": "cover"}}}
        pr = {"path": str(src), "type": "video", "width": 320, "height": 240}
        warnings: list[str] = []
        out = rs_render.matte_precompose(clip, src, pr, build, root, 10.0, {}, warnings)
        assert out is not None and out.is_file(), warnings
        cap = cv2.VideoCapture(str(out))
        ok, frame = cap.read()
        cap.release()
        assert ok
        center, corner = frame[120, 160].tolist(), frame[10, 10].tolist()
        # 限位 YUV:纯绿解码 G≈127;前景=中心绿,角落=背景红
        assert center[1] >= 110 and center[0] < 60 and center[2] < 60, f"中心应为前景绿色:{center}"
        assert corner[2] > 150 and corner[1] < 120, f"角落应为背景红色:{corner}"
        # alpha 缺失 → WARN 回退(按未抠像渲染,不臆测)
        clip_bad = {**clip, "matte": {"engine": "rvm", "alphaDir": "05_时间线工程/nope"}}
        w2: list[str] = []
        assert rs_render.matte_precompose(clip_bad, src, pr, build, root, 10.0, {}, w2) is None
        assert w2 and "alphaDir 缺失" in w2[0]

    def test_ingest_auto_matting分流(self, tmp_path, monkeypatch):
        """S0 (b) 分流:gate PASS 放行(autoMattingApplied 留痕),FAIL 阻断 MATTE_QUALITY_FAIL。"""
        root = tmp_path / "proj"
        (root / rs_paths.p("materials")).mkdir(parents=True)
        (root / rs_paths.p("brief")).mkdir(parents=True)
        (root / rs_paths.p("brief") / "brief.md").write_text("# Brief\n- 类型:口播\n", encoding="utf-8")
        _gray_video(root / rs_paths.p("materials") / "a.mp4", frames=6, color="0x00C800")
        monkeypatch.setattr(rs_ingest.rs_greenscreen, "detect_media",
                            lambda p: {"detected": True, "color": "green"})
        calls = []

        def fake_gate(root_, src, **kw):
            calls.append(src.name)
            return 0, {"verdict": "pass", "engine": "rvm", "metrics": {}, "suggestions": []}

        monkeypatch.setattr(rs_ingest.rs_matting, "gate", fake_gate)
        code, doc = _capture_argv(rs_ingest.main, [
            "rs_ingest.py", "scan", str(root), "--allow-auto-matting"])
        assert code == 0 and doc["ok"], doc
        assert calls == ["a.mp4"]
        item = next(i for i in doc["data"]["items"] if i["file"] == "a.mp4")
        assert item["greenScreen"]["autoMattingApplied"] is True
        # FAIL → 阻断
        monkeypatch.setattr(rs_ingest.rs_matting, "gate",
                            lambda r, s, **kw: (4, {"verdict": "fail", "engine": "rvm",
                                                    "metrics": {}, "suggestions": ["x"]}))
        code2, doc2 = _capture_argv(rs_ingest.main, [
            "rs_ingest.py", "scan", str(root), "--allow-auto-matting"])
        assert code2 == 2 and doc2["code"] == "MATTE_QUALITY_FAIL", doc2

    def test_jy_draft_matte_degradation(self):
        """剪映侧 matte 无映射 → warnings + degradedCapabilities 如实留痕。"""
        doc = {"fps": 30, "canvas": {"width": 1080, "height": 1920},
               "tracks": [{"kind": "video", "name": "main", "clips": [
                   {"id": "c1", "src": "01_原始素材/a.mp4", "startMs": 0,
                    "durationMs": 1000, "sourceInMs": 0, "matte": {"engine": "rvm"}}]}]}
        warnings: list[str] = []
        degraded = rs_jy_draft._matte_degradations(doc, warnings)
        assert degraded == ["matte"] and "无草稿映射" in warnings[0]
        assert rs_jy_draft._matte_degradations({"tracks": []}, warnings) == []
