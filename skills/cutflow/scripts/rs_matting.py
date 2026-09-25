#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rs_matting.py —— 抠像重建与五项质量门禁(ADR-0050,部分取代 ADR-0031)。

设计要点(方案 §5.6):
· 引擎主选 RVM(时序引导,venv-torch ~480MB,懒加载 ADR-0049);rembg 逐帧备选;
  旧键控链路(chromakey/despill)不复用——天花板已被 ADR-0031 论证。
· **先判质量再决定是否启用**:不达标直接阻断,绝不产烂片;速度是门禁指标之一。
· 五项客观指标(全部机械可判):
    1 temporalIoU        相邻帧 alpha(>0.5 二值)IoU 中位数 ≥ MATTE_IOU_MIN
    2 jitterRate         |ΔIoU| > MATTE_JITTER_STEP 的帧占比 ≤ MATTE_JITTER_MAX
    3 haloRatio          alpha 边缘带内「残留幕色」(绿占优)像素占比 ≤ MATTE_HALO_MAX
    4 detailRetention    边缘带高频能量比(合成帧/原帧 Laplacian 方差) ≥ MATTE_DETAIL_MIN
    5 transparencySpread alpha 中间态(0.05,0.95)像素占比 > 0(防硬二值剪贴画感)
    吞吐 fps: 1080p ≥ MATTE_FPS_GPU(GPU)/ ≥ MATTE_FPS_CPU(CPU 降分辨率档)
· 三态: PASS(启用)/ WARN(可用,交付说明标注边缘质量一般)/ FAIL(阻断 MATTE_QUALITY_FAIL)。
· 引擎缺失: 阻断 MATTE_ENGINE_MISSING(vision.matting 的 degrade=none(gate)——不启用即
  回到 ADR-0031 默认路径「用户预处理」,不是程序故障)。
· IR matte 块(不复用已删的 chroma/background 语义): rs_ir add-matte 写入 clip 级:
  {"engine","quality":{verdict,metrics},"bg":{"src","mode"},"cacheVer"}。

子命令:
  gate <工程> --source <视频> [--engine rvm|rembg] [--bg <背景图>] [--report] [--json]
  extract <工程> --source <视频> [--engine ...]           只产 alpha,不判定
  check <工程>                                            读 quality.json 复述判定(工程模式可用)

工程模式(无参,cwd=工程根,rs_run 能力挂载器契约):对 manifest 标记幕布的素材逐条 gate,
汇总写 05_时间线工程/matte/quality.json。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rs_common  # noqa: E402
import rs_fetchable  # noqa: E402
import rs_paths  # noqa: E402

# ---------------------------------------------------------------- 门禁阈值(ADR-0050 初值,实测定档)
MATTE_IOU_MIN = 0.985        # 时间一致性:相邻帧 IoU 中位数
MATTE_JITTER_STEP = 0.05     # 抖动计数步长(|ΔIoU| 超此值记一次抖动)
MATTE_JITTER_MAX = 0.02      # 抖动帧占比上限(2%)
MATTE_HALO_MAX = 0.015       # 边缘 3px 环带残留幕色占比上限(1.5%)
MATTE_DETAIL_MIN = 0.75      # 发丝/细结构高频保留率下限
MATTE_TRANSPARENCY_MIN = 0.0005   # alpha 中间态像素最小占比(防硬二值)
MATTE_FPS_GPU = 12.0         # 1080p GPU 吞吐下限
MATTE_FPS_CPU = 1.5          # CPU 降分辨率档吞吐下限
MATTE_HALO_BAND_PX = 3       # 边缘环带宽度(px)
MATTE_MIN_FRAMES = 3         # 参与判定的最少帧数

ENGINE_MISSING = "MATTE_ENGINE_MISSING"
QUALITY_FAIL = "MATTE_QUALITY_FAIL"
QUALITY_WARN = "MATTE_QUALITY_WARN"
QUALITY_PASS = "MATTE_QUALITY_PASS"

# WARN 级(不阻断):这些指标差一点仍可用,交付说明标注「边缘质量一般」
WARN_METRICS = ("jitterRate",)


# ---------------------------------------------------------------- 指标计算器(纯函数,合成夹具可测)

def _binmask(alpha):
    """alpha(0-1 浮点)→ 0/1 二值掩码。"""
    return (alpha > 0.5).astype("uint8")


def _iou(a, b) -> float:
    inter = ((a > 0) & (b > 0)).sum()
    union = ((a > 0) | (b > 0)).sum()
    return float(inter) / float(union) if union else 1.0


def temporal_metrics(alphas: list) -> dict:
    """指标 1/2:相邻帧 IoU 中位数 + 抖动帧占比。"""
    if len(alphas) < 2:
        return {"temporalIoU": 1.0, "jitterRate": 0.0}
    ious = [_iou(_binmask(alphas[i]), _binmask(alphas[i + 1]))
            for i in range(len(alphas) - 1)]
    med = sorted(ious)[len(ious) // 2] if ious else 1.0
    jitters = sum(1 for i in range(1, len(ious))
                  if abs(ious[i] - ious[i - 1]) > MATTE_JITTER_STEP)
    return {"temporalIoU": round(med, 4), "jitterRate": round(jitters / len(ious), 4)}


def _edge_band(alpha, band_px: int = MATTE_HALO_BAND_PX):
    """边缘带掩码:alpha 边界向内外各 band_px 的环带(膨胀差集)。"""
    import cv2  # noqa: PLC0415 — vision.cv 懒加载组件(本机 READY;缺则降级见 gate)
    m = (_binmask(alpha) * 255).astype("uint8")
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                       (band_px * 2 + 1, band_px * 2 + 1))
    return ((cv2.dilate(m, kernel) > 0) & ~(cv2.erode(m, kernel) > 0))


def halo_ratio(frame, alpha, bg_bgr, band_px: int = MATTE_HALO_BAND_PX) -> float:
    """指标 3:边缘带内「残留幕色」像素占比。幕色判据:绿通道显著高于红蓝(绿幕)。"""
    band = _edge_band(alpha, band_px)
    n = int(band.sum())
    if n == 0:
        return 0.0
    b, g, r = frame[:, :, 0].astype(int), frame[:, :, 1].astype(int), frame[:, :, 2].astype(int)
    greenish = (g > b + 24) & (g > r + 24)          # 幕色渗出:绿占优
    return round(float((band & greenish).sum()) / float(n), 4)


def detail_retention(src_frame, comp_frame, alpha) -> float:
    """指标 4:边缘带高频能量比(合成帧/原帧,Laplacian 方差)。"""
    import cv2  # noqa: PLC0415
    band = _edge_band(alpha)
    if int(band.sum()) == 0:
        return 1.0
    g1 = cv2.cvtColor(src_frame, cv2.COLOR_BGR2GRAY).astype("uint8")
    g2 = cv2.cvtColor(comp_frame, cv2.COLOR_BGR2GRAY).astype("uint8")
    v1 = float(cv2.Laplacian(g1, cv2.CV_64F)[band].var())
    v2 = float(cv2.Laplacian(g2, cv2.CV_64F)[band].var())
    return round(v2 / v1, 4) if v1 > 1e-6 else 1.0


def transparency_spread(alpha) -> float:
    """指标 5:alpha 中间态(0.05,0.95)像素占比——硬二值化会退化为 0。"""
    mid = ((alpha > 0.05) & (alpha < 0.95)).sum()
    return round(float(mid) / float(alpha.size), 6)


def throughput_fps(n_frames: int, seconds: float) -> float:
    return round(n_frames / seconds, 2) if seconds > 0 else 0.0


# ---------------------------------------------------------------- 判定

def verdict_of(metrics: dict, fps: float, use_gpu: bool) -> tuple[str, list[str], list[str]]:
    """五项指标 → (verdict, warnings, suggestions)。PASS/WARN/FAIL 三态。"""
    warnings, suggestions = [], []
    fail = []
    if metrics["temporalIoU"] < MATTE_IOU_MIN:
        fail.append("temporalIoU")
        suggestions.append("边缘呼吸/闪烁:提高源分辨率或改用 RVM 时序引擎可改善")
    if metrics["jitterRate"] > MATTE_JITTER_MAX:
        (warnings if metrics["temporalIoU"] >= MATTE_IOU_MIN else fail).append("jitterRate")
    if metrics["haloRatio"] > MATTE_HALO_MAX:
        fail.append("haloRatio")
        suggestions.append("残留幕色:检查溢色/打光均匀度,或改走用户预处理(ADR-0031 默认路径)")
    if metrics["detailRetention"] < MATTE_DETAIL_MIN:
        fail.append("detailRetention")
        suggestions.append("发丝/细结构丢失:人物边缘高频细节过多时建议改走用户预处理")
    if metrics["transparencySpread"] < MATTE_TRANSPARENCY_MIN:
        fail.append("transparencySpread")
        suggestions.append("alpha 硬二值化(剪贴画感):换用带羽化的引擎档")
    fps_min = MATTE_FPS_GPU if use_gpu else MATTE_FPS_CPU
    if fps > 0 and fps < fps_min:
        fail.append("throughput")
        suggestions.append(f"吞吐 {fps}fps < {fps_min}fps:启用 GPU 或降分辨率到 720p 档")
    if not fail:
        if warnings:
            return "warn", warnings, suggestions
        return "pass", warnings, suggestions
    return "fail", warnings, suggestions


# ---------------------------------------------------------------- 引擎

def _engine_state(engine: str) -> dict:
    """引擎 → 懒加载三态(两引擎同挂 vision.matting 组件,ADR-0049 CAPABILITY_DEPS)。"""
    return rs_fetchable.state("vision.matting")


def extract_frames(source: Path, out_dir: Path, engine: str = "rvm",
                   max_frames: int = 48) -> dict:
    """抠出 alpha 帧序列(引擎就绪时);引擎缺失 → {"skipped": reason}。

    真实推理走 rs_fetchable 部署的组件(venv-torch 子进程);本函数返回 alpha PNG 落点。
    测试经 monkeypatch _engine_state / _run_engine 注入,不真下载。
    """
    st = _engine_state(engine)
    if st.get("state") != "READY":
        return {"skipped": f"{engine} 未部署({st.get('state')}):{st.get('message', '')}",
                "missingComponent": st.get("component", engine)}
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    n = _run_engine(source, out_dir, engine, max_frames)
    return {"frames": n, "fps": throughput_fps(n, time.time() - t0),
            "engine": engine, "dir": str(out_dir)}


def _run_engine(source: Path, out_dir: Path, engine: str, max_frames: int) -> int:
    """引擎调用点(独立函数便于 mock)。RVM: venv-torch 子进程逐段推理;rembg: 逐帧。"""
    py = rs_fetchable.venv_python("venv-torch" if engine == "rvm" else "venv-dsp")
    if not py or not Path(py).is_file():
        raise FileNotFoundError(f"引擎 venv 不存在:{py}(先 rs_fetchable install {engine})")
    cmd = [str(py), str(Path(__file__).resolve()), "--worker", engine,
           "--source", str(source), "--out", str(out_dir), "--max-frames", str(max_frames)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=3600)
    if p.returncode != 0:
        raise RuntimeError(f"抠像引擎失败:{p.stderr[-400:]}")
    return int(p.stdout.strip().splitlines()[-1])


def _imwrite_unicode(path: Path, img) -> None:
    """PNG 写盘(cv2.imwrite 对中文路径不可靠,Windows 实测静默失败——先编码后写字节)。"""
    import cv2  # noqa: PLC0415
    ok, buf = cv2.imencode(path.suffix, img)
    if not ok:
        raise RuntimeError(f"PNG 编码失败:{path}")
    path.write_bytes(buf.tobytes())


def _imread_unicode(path: Path):
    """PNG 读盘(与 _imwrite_unicode 同理,fromfile+imdecode 走 Unicode 安全路径)。"""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_UNCHANGED)


def _worker_main(engine: str, source: Path, out_dir: Path, max_frames: int) -> int:
    """venv 子进程 worker:真实模型推理(RVM 时序记忆 / rembg 逐帧)。"""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    cap = cv2.VideoCapture(str(source))
    n = 0
    if engine == "rvm":
        from inference_rvm import RVMInference  # noqa: PLC0415 — venv-torch 内部署件
        model = RVMInference()
        while n < max_frames:
            ok, frame = cap.read()
            if not ok:
                break
            alpha = model.infer(frame)             # (H,W) float 0-1,时序记忆由 RVMInference 维护
            _imwrite_unicode(out_dir / f"alpha_{n:05d}.png",
                             (np.clip(alpha, 0, 1) * 255).astype("uint8"))
            n += 1
    else:
        from rembg import remove  # noqa: PLC0415 — 逐帧备选(无时序,边缘抖动明显)
        from PIL import Image  # noqa: PLC0415
        while n < max_frames:
            ok, frame = cap.read()
            if not ok:
                break
            out = remove(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
            _imwrite_unicode(out_dir / f"alpha_{n:05d}.png", np.array(out.split()[-1]))
            n += 1
    cap.release()
    print(n)
    return n


# ---------------------------------------------------------------- 判定主流程

def gate(root: Path, source: Path, engine: str = "rvm", bg_src: str = "",
         use_gpu: bool = False, max_frames: int = 48) -> tuple[int, dict]:
    """extract + 五项判定,写 05_时间线工程/matte/quality.json。返回 (exit_code, doc)。"""
    matte_dir = rs_paths.of(root, "timeline") / "matte"
    st = _engine_state(engine)
    if st.get("state") != "READY":
        doc = {"verdict": "blocked", "engine": engine,
               "metrics": {}, "fps": 0.0,
               "warnings": [], "suggestions": [
                   f"组件未部署:rs_fetchable install {engine}(约 {st.get('size_mb', '?')}MB)"
                   "或改走用户预处理(ADR-0031 默认路径)"],
               "degraded": True, "degradeReason": "engine-missing",
               "missingComponent": st.get("component", engine)}
        _write_quality(matte_dir, doc)
        return 4, doc

    ex = extract_frames(source, matte_dir / "alpha", engine, max_frames)
    if ex.get("skipped"):
        doc = {"verdict": "blocked", "engine": engine, "metrics": {}, "fps": 0.0,
               "warnings": [], "suggestions": [ex["skipped"]],
               "degraded": True, "degradeReason": "engine-missing",
               "missingComponent": ex.get("missingComponent", engine)}
        _write_quality(matte_dir, doc)
        return 4, doc

    alphas, frames = _load_alpha_and_frames(matte_dir / "alpha", source)
    metrics = {"temporalIoU": 1.0, "jitterRate": 0.0, "haloRatio": 0.0,
               "detailRetention": 1.0, "transparencySpread": 0.0}
    if len(alphas) >= MATTE_MIN_FRAMES:
        metrics.update(temporal_metrics(alphas))
        bg_bgr = (26, 200, 60)                      # 绿幕标准色 BGR(60,200,26) 附近即判幕色
        metrics["haloRatio"] = max(halo_ratio(f, a, bg_bgr) for f, a in zip(frames, alphas))
        metrics["detailRetention"] = min(detail_retention(f, _composite(f, a), a)
                                         for f, a in zip(frames, alphas))
        metrics["transparencySpread"] = min(transparency_spread(a) for a in alphas)
    verdict, warnings, suggestions = verdict_of(metrics, ex.get("fps", 0.0), use_gpu)
    doc = {"verdict": verdict, "engine": engine, "metrics": metrics,
           "fps": ex.get("fps", 0.0), "frames": ex.get("frames", 0),
           "warnings": warnings, "suggestions": suggestions,
           "degraded": False,
           "bg": {"src": bg_src, "mode": "cover"} if bg_src else None,
           "cacheVer": _cache_ver()}
    if bg_src:
        doc["suggestions"] = suggestions
    _write_quality(matte_dir, doc)
    code = {"pass": 0, "warn": 0, "fail": 4, "blocked": 4}[verdict]
    return code, doc


def _cache_ver() -> str:
    from rs_render import CACHE_VER  # noqa: PLC0415 — 惰性导入防环
    return CACHE_VER


def _composite(frame, alpha):
    """合成帧 = 前景(按 alpha)铺在白底上(供 detailRetention 的高频对比)。"""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    a = alpha[:, :, None]
    return (frame.astype(float) * a + 255.0 * (1 - a)).astype("uint8")


def _load_alpha_and_frames(alpha_dir: Path, source: Path, limit: int = 16):
    """读 alpha PNG + 对应原帧(降采样对齐,判定只取前 limit 帧控耗时)。"""
    import cv2  # noqa: PLC0415
    alphas, frames = [], []
    cap = cv2.VideoCapture(str(source))
    paths = sorted(alpha_dir.glob("alpha_*.png"))[:limit]
    for i, p in enumerate(paths):
        a = _imread_unicode(p)
        if a is None:
            continue
        ok, frame = cap.read()
        if not ok:
            break
        if frame.shape[:2] != a.shape[:2]:
            a = cv2.resize(a, (frame.shape[1], frame.shape[0]))
        alphas.append(a.astype(float) / 255.0)
        frames.append(frame)
    cap.release()
    return alphas, frames


def _write_quality(matte_dir: Path, doc: dict) -> None:
    matte_dir.mkdir(parents=True, exist_ok=True)
    (matte_dir / "quality.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- CLI

def main() -> int:  # noqa: C901 — 子命令分派直写形态(与 rs_* 体例一致)
    argv = sys.argv[1:]
    if argv and argv[0] == "--worker":              # venv 子进程 worker 入口
        ap = argv.index("--worker")
        engine = argv[ap + 1]
        source = Path(argv[argv.index("--source") + 1])
        out = Path(argv[argv.index("--out") + 1])
        mf = int(argv[argv.index("--max-frames") + 1]) if "--max-frames" in argv else 48
        return _worker_main(engine, source, out, mf)

    import argparse
    ap = argparse.ArgumentParser(prog="rs_matting.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default=None,
                    choices=["gate", "extract", "check"])
    ap.add_argument("root", nargs="?", help="工程根(gate/check)")
    ap.add_argument("--source", help="素材视频路径(gate)")
    ap.add_argument("--engine", default="rvm", choices=["rvm", "rembg"])
    ap.add_argument("--bg", default="", help="背景图(合成用,写进 IR matte.bg)")
    ap.add_argument("--gpu", default="auto", choices=["auto", "on", "off"])
    ap.add_argument("--max-frames", type=int, default=48)
    a = ap.parse_args(argv)

    # 工程模式:无参 → 对 manifest 幕布素材逐条 gate
    if a.cmd is None:
        cwd = Path.cwd()
        return _project_mode(cwd, a.engine, a.max_frames)

    if a.cmd == "check":
        root = Path(a.root or Path.cwd())
        q = rs_paths.of(root, "timeline") / "matte" / "quality.json"
        if not q.is_file():
            return rs_common.emit(False, "MATTE_NO_REPORT", f"无判定报告:{q}", exit_code=2)
        doc = json.loads(q.read_text(encoding="utf-8"))
        return rs_common.emit(doc.get("verdict") in ("pass", "warn"),
                              f"MATTE_{doc.get('verdict', '?').upper()}",
                              f"抠像判定:{doc.get('verdict')}", doc,
                              exit_code=0 if doc.get("verdict") in ("pass", "warn") else 4)

    if not a.root or not a.source:
        return rs_common.emit(False, "BAD_INPUT", "gate 需要 <工程根> --source <视频>", exit_code=2)
    root, source = Path(a.root), Path(a.source)
    if not source.is_file():
        return rs_common.emit(False, "BAD_INPUT", f"素材不存在:{source}", exit_code=2)
    use_gpu = {"auto": _gpu_available(), "on": True, "off": False}[a.gpu]
    code, doc = gate(root, source, a.engine, a.bg, use_gpu, a.max_frames)
    ok = doc["verdict"] in ("pass", "warn")
    code_name = {"pass": QUALITY_PASS, "warn": QUALITY_WARN,
                 "fail": QUALITY_FAIL, "blocked": ENGINE_MISSING}[doc["verdict"]]
    hint = ("WARN:交付说明书必须标注「边缘质量一般」"
            if doc["verdict"] == "warn" else
            "FAIL:已阻断,勿启用自动抠像(建议见 suggestions);可改走用户预处理")
    return rs_common.emit(ok, code_name, f"抠像质量判定:{doc['verdict']};{hint}", doc,
                          exit_code=code)


def _gpu_available() -> bool:
    try:
        import torch  # noqa: PLC0415
        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001 — 无 torch 视作 CPU 档
        return False


def _project_mode(root: Path, engine: str, max_frames: int) -> int:
    """工程模式:manifest.greenScreen 素材逐条 gate,汇总 quality.json。"""
    manifest = rs_paths.manifest_json(root)
    flagged = []
    if manifest.is_file():
        for it in json.loads(manifest.read_text(encoding="utf-8")).get("items", []):
            if it.get("greenScreen") and not it.get("greenOk"):
                flagged.append(it.get("file", ""))
    if not flagged:
        return rs_common.emit(True, "MATTE_NOT_NEEDED", "无幕布素材,无需抠像门禁",
                              {"flagged": []})
    worst, docs = "pass", []
    for f in flagged:
        src = rs_paths.of(root, "materials") / f
        if not src.is_file():
            continue
        _code, doc = gate(root, src, engine, "", _gpu_available(), max_frames)
        docs.append({"file": f, **doc})
        if doc["verdict"] == "fail" or doc["verdict"] == "blocked":
            worst = "fail"
        elif doc["verdict"] == "warn" and worst != "fail":
            worst = "warn"
    code = 0 if worst in ("pass", "warn") else 4
    return rs_common.emit(code == 0, f"MATTE_{worst.upper()}",
                          f"抠像门禁汇总:{worst}({len(docs)} 条幕布素材)", {"items": docs},
                          exit_code=code)


if __name__ == "__main__":
    sys.exit(main())
