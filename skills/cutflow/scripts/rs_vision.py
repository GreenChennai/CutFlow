"""画面感知层(第三册 T3.3/T3.4/T3.7/T3.8/T3.11):逐镜像素统计 + 聚合 + 骨架 + 降级报告。

用法:
  rs_vision.py vision   <工程根> [--force]   # shots.json(v2) → 05_时间线工程/vision.json
  rs_vision.py skeleton <工程根> [--force]   # vision/shots + IR → 05_时间线工程/skeleton.json
  rs_vision.py report   <工程根>             # 画面能力三态清点 → 05_时间线工程/sense_report.md

三级感知(T3.8,rules/sense.md 同口径):
  L0 结构  纯 ffmpeg 数值(帧差/直方图/PCM 均方根),零模型零网络,默认开;
  L1 标签  本地模型(cv2 Haar 人脸 + 帧差主体粗框),可选件,默认开、缺失显式降级;
  L2 描述  VLM 逐镜一句中文描述,默认关,预算显式开启(在 rs_sense.py,不在此)。

设计纪律:
  · 像素级数据一律留在本层算成结构化字段,不进 Agent context(分册三 §3.1 原则 1);
  · 解码走 ffmpeg 管道(rawvideo/s16le)——cv2.VideoCapture/imread 对中文路径静默失败,
    rs_matting/rs_screen 已有前科,本层从根上绕开:cv2 只吃内存里的帧字节;
  · 所有统计都是整数累加(字节值平方/差值),numpy 有无两条路径结果逐位一致,
    浮点只在最终除法与 round 出现 —— 两次运行逐字段确定(T3.1 判据)。
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import (EXIT_INPUT, EXIT_OK, REPO_ROOT, emit, ffmpeg_bin,  # noqa: E402
                       load_config, media_duration_s, p95, write_text_atomic)
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量
import rs_fetchable  # noqa: E402  — 三态探测(只 state(),绝不触发下载)

# ---------------------------------------------------------------- 参数常量(改一处必改消费方文档)

PROFILE_FPS = 4              # L0 逐镜统计降采样帧率(4fps 对运动档/亮度档足够)
PROFILE_W = 160              # L0 解码宽度(与 rs_shot.DOWNSCALE_W 同量级,解码省时)
AUDIO_RATE = 16000           # audioRms 采样率(mono s16le)
MOTION_STILL = 0.02          # 静帧判据:相邻帧平均亮度差 <2%(0–1 归一)
FACE_FPS = 3.0               # L1 人脸/主体采样帧率
FACE_W = 320                 # L1 解码宽度(Haar 最小窗 24px,160 宽不够)
FACE_MAX_FRAMES = 1200       # L1 单媒体采样帧硬上限(长素材防炸预算)
FACE_CONF_MIN_AREA = 0.004   # 人脸框最小面积占比(低于视为噪点)
DIFF_BLOB_MAX_AREA = 0.60    # 帧差变化占比超过此值 = 全画面变化(切镜/曝光),不算主体
SKIN_BAND_RATIO = 0.20       # T3.5:单帧带内肤色像素占比阈值(粗判压脸)
BAND_MOTION_RATIO = 0.15     # T3.5:单帧带内时序变化像素占比阈值(粗判压主体)
BAND_CONTENT_FRAMES = 0.30   # T3.5:采样帧中「带内有内容」帧占比阈值
BAND_SAMPLES = 8             # T3.5:带占用检测采样帧数(全片均匀,确定性)
BAND_BOTTOM_FRAC = 0.25      # 字幕带参考口径(9:16 底 25%,与 L1 清单同值)
BAND_TOP_FRAC = 0.12         # 顶部带参考口径(9:16 顶 12%)
SKEL_BUDGET_BYTES = 2048     # T3.3:skeleton.json 硬预算(3 分钟成片 ≤2KB)
VISION_BUDGET_BYTES = 65536  # T3.7:vision.json 硬预算(3 分钟成片 ≤64KB)
SPEECH_HEAD_CHARS = 12       # T3.3:台词摘要前 N 字
MOTION_BANDS = (0.02, 0.06, 0.15)      # 运动档阈值:0 静 / 1 缓 / 2 中 / 3 烈
BRIGHTNESS_BANDS = (0.28, 0.45, 0.65)  # 亮度档阈值:0 暗 / 1 偏暗 / 2 偏亮 / 3 亮
ROLE_OPEN, ROLE_BODY, ROLE_TRANS, ROLE_CLOSE = 0, 1, 2, 3


def vision_path(project: Path) -> Path:
    """05_时间线工程/vision.json(单一落点,T3.7)。"""
    return rs_paths.resolve(project, "timeline") / "vision.json"


def skeleton_path(project: Path) -> Path:
    """05_时间线工程/skeleton.json(单一落点,T3.3)。"""
    return rs_paths.resolve(project, "timeline") / "skeleton.json"


def sense_report_path(project: Path) -> Path:
    """05_时间线工程/sense_report.md(单一落点,T3.11)。"""
    return rs_paths.resolve(project, "timeline") / "sense_report.md"


def shots_doc(project: Path) -> dict | None:
    """读 04_粗剪决策/shots.json(缺失/坏产物 → None,调用方显式降级)。"""
    p = rs_paths.resolve(project, "cut") / "shots.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def wordline_chars(project: Path) -> list[dict]:
    """wordline 字级时间轴(缺失 → [],speechRate/台词摘要显式降级为 0/空)。"""
    p = rs_paths.wordline_json(project)
    if not p.is_file():
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    out = []
    for c in doc.get("chars") or []:
        try:
            out.append({"startMs": int(c["startMs"]), "endMs": int(c["endMs"]),
                        "ch": str(c.get("ch", ""))})
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- ffmpeg 管道解码(中文路径安全)

def _np():
    """numpy 懒加载(可用则提速;无则纯 Python 整数累加,结果逐位一致)。"""
    try:
        import numpy  # noqa: PLC0415
        return numpy
    except ImportError:
        return None


def _decode_pass(media: Path, cfg: dict, *, fps: float, width: int):
    """ffmpeg 解码管道 Popen(rawvideo bgr24)。

    中文路径纪律:cv2.VideoCapture/imread 在 Windows 中文路径静默失败(rs_matting
    实测),ffmpeg 走宽字符 API 没有问题(test_cn_paths 端到端保证)——解码只走 ffmpeg。
    """
    cmd = [ffmpeg_bin(cfg), "-v", "error", "-nostats", "-i", str(media),
           "-vf", f"fps={fps},scale={width}:-2",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def frames_size(media: Path, cfg: dict, width: int) -> tuple[int, int]:
    """解码后帧尺寸:ffprobe 源高按宽度等比换算(与 scale=-2 的确定性一致)。"""
    try:
        from rs_common import ffprobe_json  # noqa: PLC0415
        info = ffprobe_json(media, cfg)
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        sw, sh = int(v.get("width") or width), int(v.get("height") or 0)
    except (SystemExit, OSError, json.JSONDecodeError, StopIteration, TypeError, ValueError):
        sw, sh = width, int(width * 16 / 9)
    if sw <= 0:
        sw = width
    return width, max(2, int(round(sh * width / sw)) // 2 * 2)


def stream_frames(media: Path, cfg: dict, *, fps: float = PROFILE_FPS,
                  width: int = PROFILE_W, max_frames: int = 0):
    """逐帧产出 (t_ms, bgr: bytes, w, h);max_frames>0 时硬上限。

    t_ms = round(i * 1000 / fps) —— fps 滤镜输出网格从 0 均匀起,两次运行一致。
    """
    w, h = frames_size(media, cfg, width)
    proc = _decode_pass(media, cfg, fps=fps, width=width)
    assert proc.stdout is not None
    frame_bytes = w * h * 3
    step_ms = 1000.0 / fps
    idx = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if not buf or len(buf) < frame_bytes:
                break
            yield int(round(idx * step_ms)), buf, w, h
            idx += 1
            if max_frames and idx >= max_frames:
                break
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def stream_pcm(media: Path, cfg: dict, *, rate: int = AUDIO_RATE):
    """音频解码管道(s16le mono)逐 chunk 产出;无音轨时产出空。"""
    cmd = [ffmpeg_bin(cfg), "-v", "error", "-nostats", "-i", str(media),
           "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert proc.stdout is not None
    try:
        while True:
            chunk = proc.stdout.read(1 << 16)
            if not chunk:
                break
            yield chunk
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------- 帧统计(整数累加,双路径逐位一致)

def _gray_int(bgr: bytes, w: int, h: int, np):
    """bgr24 → 亮度整数矩阵(BT.601 整数式;numpy 与纯 Python 公式逐位一致)。"""
    if np is not None:
        arr = np.frombuffer(bgr, dtype=np.uint8).reshape(h, w, 3)
        return (299 * arr[:, :, 2].astype(np.int32) + 587 * arr[:, :, 1].astype(np.int32)
                + 114 * arr[:, :, 0].astype(np.int32)) // 1000
    out = [0] * (w * h)
    for i in range(w * h):
        j = i * 3
        out[i] = (299 * bgr[j + 2] + 587 * bgr[j + 1] + 114 * bgr[j]) // 1000
    return out


class ShotStat:
    """单镜累加器:只存整数和与帧差列表,内存 O(帧差数)。"""

    __slots__ = ("n_frames", "n_px", "sum_gray", "sum_gray_sq",
                 "sum_rg", "sum_rg_sq", "sum_yb", "sum_yb_sq", "pair_means", "peak")

    def __init__(self) -> None:
        self.n_frames = 0
        self.n_px = 0
        self.sum_gray = 0
        self.sum_gray_sq = 0
        self.sum_rg = 0
        self.sum_rg_sq = 0
        self.sum_yb = 0
        self.sum_yb_sq = 0
        self.pair_means: list[float] = []
        self.peak = (0.0, -1)                # (max pair mean, 该帧 t_ms)

    def add_gray(self, gray, w: int, h: int, np) -> None:
        """累加一帧的亮度/色度整数和(rg=R-G,yb=(R+G-2B)/2,Hasler 彩色度量)。"""
        if np is not None:
            self.sum_gray += int(gray.sum(dtype=np.int64))
            self.sum_gray_sq += int((gray * gray).sum(dtype=np.int64))
        else:
            for v in gray:
                self.sum_gray += int(v)
                self.sum_gray_sq += int(v) * int(v)
        self.n_frames += 1
        self.n_px += w * h

    def add_color(self, bgr: bytes, w: int, h: int, np) -> None:
        if np is not None:
            arr = np.frombuffer(bgr, dtype=np.uint8).reshape(h, w, 3)
            b = arr[:, :, 0].astype(np.int32)
            g = arr[:, :, 1].astype(np.int32)
            r = arr[:, :, 2].astype(np.int32)
            rg = r - g
            yb = (r + g - 2 * b) // 2
            self.sum_rg += int(rg.sum(dtype=np.int64))
            self.sum_rg_sq += int((rg * rg).sum(dtype=np.int64))
            self.sum_yb += int(yb.sum(dtype=np.int64))
            self.sum_yb_sq += int((yb * yb).sum(dtype=np.int64))
        else:
            for i in range(0, len(bgr), 3):
                bb, gg, rr = bgr[i], bgr[i + 1], bgr[i + 2]
                rg = rr - gg
                yb = (rr + gg - 2 * bb) // 2
                self.sum_rg += rg
                self.sum_rg_sq += rg * rg
                self.sum_yb += yb
                self.sum_yb_sq += yb * yb

    def add_pair(self, prev_gray, cur_gray, t_ms: int, w: int, h: int, np) -> None:
        """相邻帧平均亮度差(整数和 → 均值在 finalize 归一)。"""
        if np is not None:
            d = int(np.abs(prev_gray - cur_gray).sum(dtype=np.int64))
        else:
            d = sum(abs(int(a) - int(b)) for a, b in zip(prev_gray, cur_gray))
        m = d / (w * h)
        self.pair_means.append(m)
        if m > self.peak[0]:
            self.peak = (m, t_ms)

    def finalize(self) -> dict:
        """聚合为 v2 档案字段(0–1 归一 + round;两路径/两次运行一致)。"""
        n = max(self.n_px, 1)
        mean_gray = self.sum_gray / n
        var = max(self.sum_gray_sq / n - mean_gray * mean_gray, 0.0)
        mean_rg = self.sum_rg / n
        var_rg = max(self.sum_rg_sq / n - mean_rg * mean_rg, 0.0)
        mean_yb = self.sum_yb / n
        var_yb = max(self.sum_yb_sq / n - mean_yb * mean_yb, 0.0)
        # Hasler-Süsstrunk 彩色度量,除以 255 归一;高饱和合成画面可超 1 → 截到 1
        # (0–1 契约,schema 校验同口径)
        colorfulness = min(1.0, (math.sqrt(var_rg + var_yb)
                                 + 0.3 * math.sqrt(mean_rg ** 2 + mean_yb ** 2)) / 255.0)
        pairs = self.pair_means
        motion = sum(pairs) / len(pairs) / 255.0 if pairs else 0.0
        still = sum(1 for p in pairs if p < MOTION_STILL * 255) / len(pairs) if pairs else 1.0
        stability = 1.0 - min(1.0, p95(pairs) / 255.0) if pairs else 1.0
        return {
            "motionScore": round(motion, 4),
            "brightness": round(mean_gray / 255.0, 4),
            "contrast": round(math.sqrt(var) / 255.0, 4),
            "colorfulness": round(colorfulness, 4),
            "stability": round(stability, 4),
            "staticRatio": round(still, 4),
            "motionPeakMs": int(self.peak[1]),
        }


def _span_index(spans: list[tuple[int, int, int]], t_ms: int) -> int | None:
    """t_ms → 所属镜 index(半开区间 [start,end);gap/越界 → None)。"""
    for a, b, i in spans:
        if a <= t_ms < b:
            return i
    return None


def _speech_rate(shot: dict, chars: list[dict]) -> float:
    """镜内字/秒(T3.1):字级时间轴与镜头区间求交,字数 ÷ 镜时长秒。"""
    a, b = int(shot["startMs"]), int(shot["endMs"])
    dur_s = max((b - a) / 1000.0, 1e-6)
    n = sum(1 for c in chars if int(c["endMs"]) > a and int(c["startMs"]) < b
            and str(c.get("ch", "")).strip())
    return round(n / dur_s, 2)


def profile_shots(media: Path, shots: list[dict], cfg: dict, chars: list[dict]
                  ) -> tuple[dict[int, dict], dict]:
    """L0 逐镜像素统计(T3.1):一整遍 4fps 解码按镜头分桶 + 一整遍 PCM 音频。

    返回 ({镜 index → 档案字段}, profile 留痕块)。解码失败 → 字段置缺省并留痕
    (零静默:degraded=true 写明原因;结构层产物不因此失败)。
    """
    stats = {int(s["index"]): ShotStat() for s in shots}
    spans = [(int(s["startMs"]), int(s["endMs"]), int(s["index"])) for s in shots]
    note = ""
    prev = (None, -1)                        # (gray, shot idx)
    n_decoded = 0
    np = _np()
    try:
        for t_ms, bgr, w, h in stream_frames(media, cfg, fps=PROFILE_FPS, width=PROFILE_W):
            idx = _span_index(spans, t_ms)
            if idx is None:
                prev = (None, -1)
                continue
            gray = _gray_int(bgr, w, h, np)
            stats[idx].add_gray(gray, w, h, np)
            stats[idx].add_color(bgr, w, h, np)
            if prev[0] is not None and prev[1] == idx:
                stats[idx].add_pair(prev[0], gray, t_ms, w, h, np)
            prev = (gray, idx)
            n_decoded += 1
    except (OSError, ValueError) as exc:
        note = f"帧解码失败({exc}),像素字段置 0"
    audio, audio_tier = _audio_profile(media, cfg, spans)
    out: dict[int, dict] = {}
    for s in shots:
        i = int(s["index"])
        d = stats[i].finalize()
        d["audioRms"] = audio.get(i, 0.0)
        d["speechRate"] = _speech_rate(s, chars)
        out[i] = d
    prof = {"engine": "ffmpeg-framestats", "sampleFps": PROFILE_FPS, "width": PROFILE_W,
            "audio": audio_tier, "frames": n_decoded}
    if note:
        prof.update({"degraded": True, "note": note})
    return out, prof


def _audio_profile(media: Path, cfg: dict,
                   spans: list[tuple[int, int, int]]) -> tuple[dict[int, float], dict]:
    """全片 PCM(s16le mono)一遍,按镜头累加平方和 → audioRms(0–1 归一)。"""
    np = _np()
    sums: dict[int, int] = {}
    counts: dict[int, int] = {}
    n_total = 0
    saw_any = False
    try:
        for chunk in stream_pcm(media, cfg):
            saw_any = True
            if np is not None:
                x = np.frombuffer(chunk, dtype=np.int16).astype(np.int64)
                for j in range(0, len(x), 1600):     # 0.1s 粒度分桶,控内存
                    seg = x[j:j + 1600]
                    ms = (n_total + j) * 1000 // AUDIO_RATE
                    idx = _span_index(spans, ms)
                    if idx is not None:
                        sums[idx] = sums.get(idx, 0) + int((seg * seg).sum())
                        counts[idx] = counts.get(idx, 0) + len(seg)
                n_total += len(x)
            else:
                view = memoryview(chunk)
                for j in range(0, len(view) - 1, 2):
                    v = view[j] | (view[j + 1] << 8)
                    if v >= 32768:
                        v -= 65536
                    idx = _span_index(spans, n_total * 1000 // AUDIO_RATE)
                    if idx is not None:
                        sums[idx] = sums.get(idx, 0) + v * v
                        counts[idx] = counts.get(idx, 0) + 1
                    n_total += 1
    except OSError:
        pass
    tier = {"engine": "pcm-s16le", "rate": AUDIO_RATE} if saw_any else {
        "engine": "none", "degraded": True, "note": "无音轨/音频解码失败,audioRms 置 0"}
    out: dict[int, float] = {}
    for i, cnt in counts.items():
        rms = math.sqrt((sums.get(i, 0) / max(cnt, 1)) / (32768.0 ** 2))
        out[i] = round(min(rms, 1.0), 4)
    return out, tier


# ---------------------------------------------------------------- L1:人脸/主体(cv2,内存帧)

DETECT_HIT_MIN_RATIO = 0.25   # 有效检出帧占比下限(低于 = 置信度不足 → 显式降级,方案 §3.4)


def _face_detector():
    """人脸检测器探测链(按可用性;None = 本机无人脸模型,如实留痕)。

    ① Haar(opencv<5 自带 haarcascades 数据,零下载;opencv 5.0 起官方移除);
    ② YuNet(cv2.FaceDetectorYN,opencv≥5 现役;模型 onnx 需另置 models/ 或 deps 落点);
    都没有 → (None, 原因)——调用方仍可走「帧差+肤色」无模型粗框,显式降级不静默。
    """
    try:
        import cv2  # noqa: PLC0415 — vision.cv 懒加载组件
    except ImportError:
        return (None, None), "cv2 缺失(vision.cv MISSING)"
    if hasattr(cv2, "CascadeClassifier"):
        path = str(getattr(cv2.data, "haarcascades", "")) + "/haarcascade_frontalface_default.xml"
        cascade = cv2.CascadeClassifier(path)
        if not cascade.empty():
            return ("haar", cascade), ""
    if hasattr(cv2, "FaceDetectorYN"):
        bases = [REPO_ROOT / "models",
                 Path(__file__).resolve().parents[2] / "models",
                 rs_fetchable.deps_dir() / "yunet"]
        for base in bases:
            if not base.is_dir():
                continue
            for onnx in sorted(base.glob("*.onnx")):
                try:
                    det = cv2.FaceDetectorYN_create(str(onnx), "", (320, 320),
                                                    score_threshold=0.6)
                    return ("yunet", det), ""
                except cv2.error:            # noqa: PERF203 — 坏模型试下一个
                    continue
    why = "无人脸模型(opencv≥5 已移除 Haar;YuNet onnx 未置 models/)"
    return (None, None), why


def subject_boxes(media: Path, src_w: int, src_h: int, cfg: dict,
                  *, fps: float = FACE_FPS, width: int = FACE_W,
                  max_frames: int = FACE_MAX_FRAMES
                  ) -> tuple[list[tuple[float, float, float, float]] | None, dict]:
    """逐采样帧主体包围盒(T3.4 READY 档):人脸检测(Haar/YuNet)优先,无脸帧
    帧差/肤色粗框补;全程无模型机器走「帧差+肤色」零模型档。

    返回 (源像素空间盒列表〔(x0,y0,x1,y1),与帧一一对应〕 或 None, tiers 留痕)。
    None = 检测整体不可用/置信度不足(调用方显式降级居中锚,不再静默)。
    """
    np = _np()
    if np is None:
        return None, {"track": {"engine": "static-center", "degraded": True,
                                "note": "numpy 缺失,灰度帧不可造,主体跟踪不可用→居中锚"}}
    (kind, det), why = _face_detector()
    boxes: list[tuple | None] = []
    n_face = n_blob = n_skin = n_miss = 0
    prev_bgr: bytes | None = None
    try:
        for _t_ms, bgr, w, h in stream_frames(media, cfg, fps=fps, width=width,
                                              max_frames=max_frames):
            gray = _gray_int(bgr, w, h, np)
            g8 = _to_uint8(gray, np)
            box = None
            if det is not None:
                box = _face_box(kind, det, g8, bgr, w, h, np)
                if box is not None:
                    n_face += 1
            if box is None and prev_bgr is not None:
                box = _diff_blob_box(prev_bgr, bgr, w, h, np)
                if box is not None:
                    n_blob += 1
            if box is None:
                box = _skin_box(bgr, w, h, np)
                if box is not None:
                    n_skin += 1
            if box is None:
                n_miss += 1
            boxes.append(box)
            prev_bgr = bgr
    except OSError as exc:
        return None, {"track": {"engine": "static-center", "degraded": True,
                                "note": f"帧解码失败({exc}),主体跟踪不可用→居中锚"}}
    total = len(boxes)
    engine = ("haar+frame-diff" if kind == "haar"
              else "yunet+frame-diff" if kind == "yunet" else "frame-diff+skin")
    filled = _backfill(boxes)
    hits = total - n_miss
    if filled is None or total == 0 or hits / total < DETECT_HIT_MIN_RATIO:
        detail = (f"face={n_face} diff={n_blob} skin={n_skin} miss={n_miss}"
                  + (f";{why}" if det is None and why else ""))
        return None, {"track": {"engine": "static-center", "degraded": True,
                                "note": f"主体跟踪缺失/置信度不足({detail})→居中锚"}}
    tiers = {"track": {"engine": engine, "degraded": False, "frames": total,
                       "faces": n_face, "diff": n_blob, "skin": n_skin, "miss": n_miss}}
    return [(round(x0 * src_w, 2), round(y0 * src_h, 2),
             round(x1 * src_w, 2), round(y1 * src_h, 2)) for x0, y0, x1, y1 in filled], tiers


def _to_uint8(gray, np):
    if np is not None:
        return gray.astype(np.uint8)
    return bytes(min(255, v) for v in gray)


def _face_box(kind, det, g8, bgr: bytes, w: int, h: int, np) -> tuple | None:
    """人脸检测(Haar/YuNet)→ 归一化盒;置信度不足(面积占比)→ None。"""
    try:
        import cv2  # noqa: PLC0415
    except ImportError:
        return None
    if kind == "haar":
        faces = det.detectMultiScale(g8, scaleFactor=1.1, minNeighbors=4, minSize=(24, 24))
        if len(faces):
            x, y, fw, fh = max(faces, key=lambda f: int(f[2]) * int(f[3]))
            if fw * fh >= FACE_CONF_MIN_AREA * w * h:
                return (x / w, y / h, (x + fw) / w, (y + fh) / h)
        return None
    # yunet:输入尺寸须与创建时一致 → 缩放到 320 宽再映射回
    try:
        img = cv2.imdecode(np.frombuffer(bgr, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error:
        return None
    if img is None:
        return None
    det.setInputSize((img.shape[1], img.shape[0]))
    _, faces = det.detect(img)
    if faces is not None and len(faces):
        f = max(faces, key=lambda r: float(r[-1]))
        x, y, fw, fh = float(f[0]), float(f[1]), float(f[2]), float(f[3])
        if fw * fh >= FACE_CONF_MIN_AREA * w * h:
            return (x / w, y / h, min((x + fw) / w, 1.0), min((y + fh) / h, 1.0))
    return None


def _skin_box(bgr: bytes, w: int, h: int, np) -> tuple | None:
    """肤色主体粗框(零模型档,YCbCr 经典域;静止人像的主力信号)。"""
    if np is None:
        return None
    arr = np.frombuffer(bgr, dtype=np.uint8).reshape(h, w, 3).astype(np.int16)
    b, g, r = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    cb = 128 - (43 * r + 85 * g - 107 * b) // 256
    cr = 128 + (107 * r - 85 * g - 21 * b) // 256
    mask = (cb >= 77) & (cb <= 127) & (cr >= 133) & (cr <= 173)
    frac = float(mask.mean())
    if frac < 0.02 or frac > DIFF_BLOB_MAX_AREA:
        return None
    xs = [i for i, v in enumerate(mask.mean(axis=0)) if v > 0.08]
    ys = [i for i, v in enumerate(mask.mean(axis=1)) if v > 0.08]
    if not xs or not ys:
        return None
    return (xs[0] / w, ys[0] / h, (xs[-1] + 1) / w, (ys[-1] + 1) / h)


def _backfill(boxes: list) -> list | None:
    """None 帧用最近有效盒回填(轨迹连续性);全无有效帧 → None。"""
    valid = [i for i, b in enumerate(boxes) if b is not None]
    if not valid:
        return None
    out: list = []
    last = boxes[valid[0]]
    for b in boxes:
        if b is not None:
            last = b
        out.append(last)
    return out


def _diff_blob_box(prev_bgr: bytes, cur_bgr: bytes, w: int, h: int, np) -> tuple | None:
    """帧差主体粗框(numpy 网格投影:行列变化率 >10% 的条带)。

    逐通道 absdiff 取最大(纯亮度差对「等亮度异色」运动盲,红块在蓝灰底上实测漏检)。
    """
    if np is None:
        return None
    a = np.frombuffer(prev_bgr, dtype=np.uint8).reshape(h, w, 3).astype(np.int16)
    b = np.frombuffer(cur_bgr, dtype=np.uint8).reshape(h, w, 3).astype(np.int16)
    d = np.abs(a - b).max(axis=2) > 24
    frac = float(d.mean())
    if frac < 0.01 or frac > DIFF_BLOB_MAX_AREA:
        return None                          # 变化太小=噪点;太大=全画面变(切镜),不算主体
    xs = [i for i, v in enumerate(d.mean(axis=0)) if v > 0.10]
    ys = [i for i, v in enumerate(d.mean(axis=1)) if v > 0.10]
    if not xs or not ys:
        return None
    return (xs[0] / w, ys[0] / h, (xs[-1] + 1) / w, (ys[-1] + 1) / h)


# ---------------------------------------------------------------- 安全区内容占用(T3.5 字段侧)

def _skin_ratio(band: bytes, np) -> float:
    """肤色像素占比(经典 YCbCr 粗判:77≤Cb≤127 且 133≤Cr≤173,定点整数近似)。"""
    hits = 0
    n = 0
    if np is not None:
        arr = np.frombuffer(band, dtype=np.uint8).reshape(-1, 3).astype(np.int16)
        b, g, r = arr[:, 0], arr[:, 1], arr[:, 2]
        cb = 128 - (43 * r + 85 * g - 107 * b) // 256
        cr = 128 + (107 * r - 85 * g - 21 * b) // 256
        hits = int(((cb >= 77) & (cb <= 127) & (cr >= 133) & (cr <= 173)).sum())
        n = len(arr)
    else:
        for i in range(0, len(band), 3):
            bb, gg, rr = band[i], band[i + 1], band[i + 2]
            cb = 128 - (43 * rr + 85 * gg - 107 * bb) // 256
            cr = 128 + (107 * rr - 85 * gg - 21 * bb) // 256
            if 77 <= cb <= 127 and 133 <= cr <= 173:
                hits += 1
            n += 1
    return hits / max(n, 1)


def band_flags_for_frame(bgr: bytes, w: int, h: int, prev_bgr: bytes | None,
                         np, *, bottom_frac: float = BAND_BOTTOM_FRAC,
                         top_frac: float = BAND_TOP_FRAC) -> dict:
    """单帧「字幕带/顶部带有无画面内容」标记:肤色粗判 + 与前帧的时序差。

    字幕文本是「静止的高频小笔画」,被这两个信号天然排除 —— 时序差只响应运动
    内容,肤色只响应皮肤。返回 {"bottom": bool, "top": bool}。
    """
    out = {}
    for key, lo_frac in (("bottom", bottom_frac), ("top", top_frac)):
        y0 = int(h * (1.0 - lo_frac)) if key == "bottom" else 0
        y1 = h if key == "bottom" else int(h * lo_frac)
        if y1 - y0 < 2:
            out[key] = False
            continue
        band = bgr[y0 * w * 3: y1 * w * 3]
        flag = _skin_ratio(band, np) >= SKIN_BAND_RATIO
        if not flag and prev_bgr is not None and len(prev_bgr) == len(bgr):
            pband = prev_bgr[y0 * w * 3: y1 * w * 3]   # 前帧同带切片(prev 是整帧)
            if np is not None:
                a = np.frombuffer(pband, dtype=np.uint8).astype(np.int16)
                b = np.frombuffer(band, dtype=np.uint8).astype(np.int16)
                mratio = float((np.abs(a - b) > 24).mean())
            else:
                diff = sum(1 for i in range(len(band)) if abs(band[i] - prev_bgr[i]) > 24)
                mratio = diff / max(len(band), 1)
            flag = mratio >= BAND_MOTION_RATIO
        out[key] = flag
    return out


def band_occupancy(media: Path, cfg: dict, *, bottom_frac: float = BAND_BOTTOM_FRAC,
                   top_frac: float = BAND_TOP_FRAC, n_samples: int = BAND_SAMPLES
                   ) -> dict:
    """全片字幕带/顶部带内容占用(T3.5 判据数据):全片均匀采样 n 帧。

    返回 {"bottom": {skinRatio, motionRatio, contentRatio}, "top": {...}, "frames": n}。
    """
    np = _np()
    dur = max(_media_sec(media, cfg), 0.1)
    fps = max(n_samples, 1) / dur         # 全片均匀 n 帧(确定性)
    flags: list[dict] = []
    prev = None
    try:
        for _t, bgr, w, h in stream_frames(media, cfg, fps=fps, width=PROFILE_W):
            flags.append(band_flags_for_frame(bgr, w, h, prev, np,
                                              bottom_frac=bottom_frac, top_frac=top_frac))
            prev = bgr
    except OSError:
        pass
    total = max(len(flags), 1)
    res = {"frames": len(flags)}
    for key in ("bottom", "top"):
        res[key] = {
            "contentRatio": round(sum(1 for f in flags if f.get(key)) / total, 3)}
    return res


def _media_sec(media: Path, cfg: dict) -> float:
    try:
        return media_duration_s(media, cfg)
    except SystemExit:
        return 0.0


# ---------------------------------------------------------------- L1 层(逐镜 hasFace/safeArea)

def l1_shot_fields(media: Path, shots: list[dict], cfg: dict
                   ) -> tuple[dict[int, dict], dict]:
    """L1 标签层:逐镜 hasFace/faceFirstMs/safeAreaOccupancy(缺失 → 显式降级)。

    人脸检测走 _face_detector 探测链(Haar/YuNet);本机无人脸模型时 hasFace
    如实置 False 并留痕,安全带占用(肤色+时序差,零模型)照常产出。
    """
    if rs_fetchable.state("vision.cv").get("state") != "READY":
        return {}, {"L1": {"state": "MISSING", "degraded": True,
                           "note": "cv2 缺失(vision.cv MISSING),L1 标签未生成;"
                                   "影响:hasFace/主体锚/安全带占用字段缺席"}}
    (kind, det), face_why = _face_detector()
    np = _np()
    if np is None:
        return {}, {"L1": {"state": "MISSING", "degraded": True,
                           "note": "numpy 缺失,L1 标签未生成"}}
    spans = [(int(s["startMs"]), int(s["endMs"]), int(s["index"])) for s in shots]
    out: dict[int, dict] = {}
    note = ""
    n_frames = 0
    prev = None
    prev_idx = -1
    try:
        for t_ms, bgr, w, h in stream_frames(media, cfg, fps=FACE_FPS, width=FACE_W,
                                             max_frames=FACE_MAX_FRAMES):
            idx = _span_index(spans, t_ms)
            if idx is None:
                prev, prev_idx = None, -1
                continue
            d = out.setdefault(idx, {"hasFace": False, "faceCount": 0, "faceFirstMs": -1,
                                     "_band": []})
            if det is not None:
                gray = _gray_int(bgr, w, h, np)
                box = _face_box(kind, det, _to_uint8(gray, np), bgr, w, h, np)
                if box is not None:
                    d["hasFace"] = True
                    d["faceCount"] += 1
                    if d["faceFirstMs"] < 0:
                        d["faceFirstMs"] = t_ms
            flags = band_flags_for_frame(bgr, w, h, prev if prev_idx == idx else None, np)
            d["_band"].append(flags)
            n_frames += 1
            prev, prev_idx = bgr, idx
    except OSError as exc:
        note = f"L1 帧解码失败({exc})"
    for i, d in out.items():
        band = d.pop("_band") or [{"bottom": False, "top": False}]
        total = max(len(band), 1)
        d["safeAreaOccupancy"] = {
            "bottom": round(sum(1 for f in band if f["bottom"]) / total, 3),
            "top": round(sum(1 for f in band if f["top"]) / total, 3)}
    tiers = {"L1": {"state": "READY", "degraded": False, "frames": n_frames}}
    if det is None:
        tiers["L1"].update({"degraded": True, "faceDetector": "missing",
                            "note": f"{face_why};hasFace 置 False(留痕),"
                                    "安全带占用(肤色+时序差)照常产出"})
    if note:
        tiers["L1"].update({"degraded": True, "note": note})
    return out, tiers


# ---------------------------------------------------------------- vision.json(T3.7)

def validate_vision(doc: dict) -> list[str]:
    """vision.json 自写 schema 校验(零依赖;jsonschema 存在时调用方另行双重校验)。

    返回错误列表(空 = 合法)。逐镜必填数值字段 + 0–1 值域 + index 连续 + 预算。
    """
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["顶层必须是对象"]
    if doc.get("version") != 1:
        errs.append("version 必须 =1")
    shots = doc.get("shots")
    if not isinstance(shots, list) or not shots:
        errs.append("shots 必须是非空数组")
        return errs
    need = ("index", "startMs", "endMs", "durMs", "motionScore", "brightness", "contrast",
            "colorfulness", "stability", "staticRatio", "audioRms", "speechRate")
    unit = ("motionScore", "brightness", "contrast", "colorfulness", "stability",
            "staticRatio", "audioRms")
    prev_idx = -1
    for i, s in enumerate(shots):
        for k in need:
            if k not in s:
                errs.append(f"shots[{i}] 缺字段 {k}")
        for k in unit:
            v = s.get(k)
            if v is not None and not (isinstance(v, (int, float)) and 0.0 <= v <= 1.0):
                errs.append(f"shots[{i}].{k} 必须在 [0,1]({v!r})")
        idx = s.get("index")
        if not isinstance(idx, int) or idx != prev_idx + 1:
            errs.append(f"shots[{i}].index 必须连续({idx!r},前值 {prev_idx})")
        prev_idx = idx if isinstance(idx, int) else prev_idx
        if s.get("caption") is not None and len(str(s["caption"])) > 20:
            errs.append(f"shots[{i}].caption 超 20 字")
    size = len(json.dumps(doc, ensure_ascii=False).encode("utf-8"))
    if size > VISION_BUDGET_BYTES:
        errs.append(f"vision.json {size}B 超预算 {VISION_BUDGET_BYTES}B")
    return errs


def build_vision(project: Path) -> int:
    """shots.json(v2 档案) + L1 标签 → 05_时间线工程/vision.json(写盘前校验)。"""
    src = shots_doc(project)
    if not src or not src.get("shots"):
        return emit(False, "NO_SHOTS", "缺 04_粗剪决策/shots.json(先跑 rs_shot.py detect);未写产物",
                    exit_code=EXIT_INPUT)
    cfg = load_config()
    media = rs_paths.resolve(project, "materials") / str(src.get("source") or "")
    if not media.is_file():
        cand = _pick_project_video(project)
        if cand is None:
            return emit(False, "NO_MEDIA", "素材缺失,vision 无法对齐像素字段",
                        exit_code=EXIT_INPUT)
        media = cand
    shots = src["shots"]
    chars = wordline_chars(project)
    try:
        profiles, prof_tier = profile_shots(media, shots, cfg, chars)
    except (OSError, ValueError) as exc:
        profiles, prof_tier = _fallback_profiles(shots, f"L0 统计失败({exc}),像素字段置缺省")
    l1_fields, l1_tier = l1_shot_fields(media, shots, cfg)
    rows = []
    degrade: list[str] = []
    if prof_tier.get("degraded"):
        degrade.append(f"L0:{prof_tier.get('note')}")
    if prof_tier.get("audio", {}).get("degraded"):
        degrade.append(f"L0 音频:{prof_tier['audio'].get('note')}")
    if l1_tier.get("L1", {}).get("degraded"):
        degrade.append(f"L1:{l1_tier['L1'].get('note')}")
    if not chars:
        degrade.append("wordline 缺失:speechRate=0、骨架台词摘要为空(显式留痕,非静默)")
    for s in shots:
        i = int(s["index"])
        row = {"index": i, "startMs": int(s["startMs"]), "endMs": int(s["endMs"]),
               "durMs": int(s["durMs"]),
               **{k: profiles.get(i, {}).get(k, 0.0)
                  for k in ("motionScore", "brightness", "contrast", "colorfulness",
                            "stability", "staticRatio", "audioRms", "speechRate",
                            "motionPeakMs")}}
        if i in l1_fields:
            row.update(l1_fields[i])
        rows.append(row)
    l0_tier = {k: v for k, v in prof_tier.items() if k != "degraded"}
    if prof_tier.get("degraded"):
        l0_tier.update({"degraded": True, "note": prof_tier.get("note")})
    doc = {"version": 1, "source": media.name,
           "shotsSource": "04_粗剪决策/shots.json", "shotCount": len(rows),
           "shots": rows,
           "tiers": {"L0": l0_tier, "L1": l1_tier.get("L1", {}),
                     "L2": {"state": "OFF",
                            "note": "默认关;rs_sense.py shots --tier L2 --budget-sec N 显式开启"}},
           "degradeReasons": degrade}
    errs = validate_vision(doc)
    if errs:
        return emit(False, "VISION_SCHEMA", "vision.json 校验失败:" + ";".join(errs[:5]),
                    {"errors": errs}, exit_code=4)
    out = vision_path(project)
    write_text_atomic(out, json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    return emit(True, "VISION_OK",
                f"vision={len(rows)} 镜,size={out.stat().st_size}B,降级 {len(degrade)} 项",
                doc)


def _fallback_profiles(shots: list[dict], note: str) -> tuple[dict[int, dict], dict]:
    """L0 解码失败的结构层退路:字段齐备但置缺省,零静默留痕。"""
    profiles = {int(s["index"]): {"motionScore": 0.0, "brightness": 0.0, "contrast": 0.0,
                                  "colorfulness": 0.0, "stability": 1.0, "staticRatio": 1.0,
                                  "motionPeakMs": -1, "audioRms": 0.0, "speechRate": 0.0}
                for s in shots}
    return profiles, {"engine": "none", "degraded": True, "note": note}


def _pick_project_video(root: Path) -> Path | None:
    """素材兜底选择(manifest probe 优先,扩展名兜底;与 rs_shot 同口径)。"""
    man = rs_paths.manifest_json(root)
    items: list[dict] = []
    if man.is_file():
        try:
            items = json.loads(man.read_text(encoding="utf-8")).get("items") or []
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            items = []
    mat = rs_paths.resolve(root, "materials")
    video_ext = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts", ".flv"}
    for it in items:
        if it.get("probe") == "ok" and Path(str(it.get("file", ""))).suffix.lower() in video_ext:
            q = mat / str(it.get("file", ""))
            if q.is_file():
                return q
    if mat.is_dir():
        for q in sorted(mat.iterdir()):
            if q.is_file() and q.suffix.lower() in video_ext:
                return q
    return None


# ---------------------------------------------------------------- skeleton.json(T3.3)

def motion_band(v: float) -> int:
    """运动档 0 静 / 1 缓 / 2 中 / 3 烈(MOTION_BANDS 阈值)。"""
    b = 0
    for t in MOTION_BANDS:
        if v >= t:
            b += 1
    return b


def brightness_band(v: float) -> int:
    """亮度档 0 暗 / 1 偏暗 / 2 偏亮 / 3 亮(BRIGHTNESS_BANDS 阈值)。"""
    b = 0
    for t in BRIGHTNESS_BANDS:
        if v >= t:
            b += 1
    return b


def speech_head(chars: list[dict], a: int, b: int, limit: int = SPEECH_HEAD_CHARS) -> str:
    """台词摘要前 N 字(拼接区间内字符,去空白)。"""
    buf: list[str] = []
    for c in sorted(chars, key=lambda x: int(x["startMs"])):
        if int(c["endMs"]) > a and int(c["startMs"]) < b:
            for ch in str(c.get("ch", "")):
                if ch.strip():
                    buf.append(ch)
        if len(buf) >= limit:
            break
    return "".join(buf[:limit])


def _assign_roles(rows: list[dict]) -> None:
    """建议角色(确定性规则):首镜开场 / 末镜收尾 / 无台词且静止 → 过渡 / 其余主体。"""
    n = len(rows)
    for i, r in enumerate(rows):
        if n == 1 or i == 0:
            r["r"] = ROLE_OPEN
        elif i == n - 1:
            r["r"] = ROLE_CLOSE
        elif r["m"] == 0 and not r["t"]:
            r["r"] = ROLE_TRANS
        else:
            r["r"] = ROLE_BODY


def build_skeleton(project: Path) -> int:
    """vision/shots + IR 主轨 → 05_时间线工程/skeleton.json(≤SKEL_BUDGET_BYTES)。"""
    vis_p = vision_path(project)
    vis = None
    if vis_p.is_file():
        try:
            vis = json.loads(vis_p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            vis = None
    src = vis if vis and vis.get("shots") else shots_doc(project)
    if not src or not src.get("shots"):
        return emit(False, "NO_SHOTS", "缺 vision.json/shots.json(先 rs_shot.py detect)",
                    exit_code=EXIT_INPUT)
    shots = src["shots"]
    chars = wordline_chars(project)
    ir = _load_ir_main(project)
    rows: list[dict] = []
    source_kind = "shots"
    if ir:
        mapped = _rows_from_ir(ir, shots, chars)
        if mapped:
            rows, source_kind = mapped, "timeline"
    if not rows:
        for s in shots:
            a, b = int(s["startMs"]), int(s["endMs"])
            rows.append(_row(s.get("index", len(rows)), b - a, s, chars, a, b))
    _assign_roles(rows)
    doc = {"version": 1, "source": source_kind, "shotCount": len(rows),
           "legend": "i镜号 d时长ms m运动档0静-3烈 b亮度档0暗-3亮 t有无台词 "
                     "h台词摘要前12字 r建议角色0开场1主体2过渡3收尾",
           "budgetBytes": SKEL_BUDGET_BYTES, "items": rows}
    doc = _fit_budget(doc)
    size = len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if size > SKEL_BUDGET_BYTES:
        return emit(False, "SKELETON_BUDGET",
                    f"skeleton.json 压缩到底仍 {size}B > {SKEL_BUDGET_BYTES}B;停,不静默",
                    {"size": size}, exit_code=4)
    out = skeleton_path(project)
    write_text_atomic(out, json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    return emit(True, "SKELETON_OK", f"skeleton {len(doc['items'])} 行,{size}B "
                                     f"≤{SKEL_BUDGET_BYTES}B", doc)


def _row(idx, dur, shot, chars, a, b) -> dict:
    return {"i": int(idx), "d": int(dur),
            "m": motion_band(float(shot.get("motionScore", 0.0))),
            "b": brightness_band(float(shot.get("brightness", 0.0))),
            "t": 1 if any(int(c["endMs"]) > a and int(c["startMs"]) < b
                          and str(c.get("ch", "")).strip() for c in chars) else 0,
            "h": speech_head(chars, a, b)}


def _load_ir_main(project: Path) -> dict | None:
    p = rs_paths.project_json(project)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _rows_from_ir(ir: dict, shots: list[dict], chars: list[dict]) -> list[dict]:
    """成片主轨 clip → 骨架行(源区间映射到镜,取重叠最大的主导镜档案)。"""
    vtracks = [t for t in ir.get("tracks") or [] if t.get("kind") == "video"]
    main = next((t for t in vtracks if t.get("name") == "main"),
                vtracks[0] if vtracks else None)
    if not main:
        return []
    rows = []
    for c in main.get("clips") or []:
        sim = c.get("sourceInMs")
        dur = int(c.get("durationMs") or 0)
        if sim is None or dur <= 0:
            continue
        a, b = int(sim), int(sim) + dur
        best, best_ov = None, 0
        for s in shots:
            ov = min(b, int(s["endMs"])) - max(a, int(s["startMs"]))
            if ov > best_ov:
                best, best_ov = s, ov
        if best is None:
            continue
        rows.append(_row(best.get("index", len(rows)), dur, best, chars, a, b))
    return rows


def _fit_budget(doc: dict) -> dict:
    """超预算逐级压缩:台词摘要 12→8→删;再超 → 截断 items 并留痕(绝不静默)。"""
    def size(d: dict) -> int:
        return len(json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    if size(doc) <= SKEL_BUDGET_BYTES:
        return doc
    for head_len in (8, 0):
        trimmed = []
        for it in doc["items"]:
            it2 = dict(it)
            if head_len == 0:
                it2.pop("h", None)
            elif it2.get("h"):
                it2["h"] = it2["h"][:head_len]
            trimmed.append(it2)
        doc2 = {**doc, "items": trimmed}
        if size(doc2) <= SKEL_BUDGET_BYTES:
            doc2["compressed"] = f"台词摘要压到 {head_len} 字以守 2KB 预算"
            return doc2
    keep = 0                                # 仍然超 → 截断行数(极端镜头数),留痕
    # 度量必须带上 truncated/note 页脚本身,否则截完仍超预算(实测踩过)
    while keep < len(doc["items"]):
        cand = {**doc, "items": doc["items"][:keep + 1], "truncated": True,
                "note": f"镜头数超骨架预算,仅保留前 {keep + 1} 行(完整档案看 vision.json)"}
        if size(cand) > SKEL_BUDGET_BYTES:
            break
        keep += 1
    return {**doc, "items": doc["items"][:keep], "truncated": True,
            "note": f"镜头数超骨架预算,仅保留前 {keep} 行(完整档案看 vision.json)"}


# ---------------------------------------------------------------- 价值选帧(T3.9)

FRAME_CAP_MAX = 24           # 价值选帧硬上限常数:min(FRAME_CAP_MAX, 3 + 镜头数)


def value_frames(shots: list[dict], chars: list[dict], fps: float = 30.0,
                 face_first_ms: dict[int, int] | None = None, cap: int | None = None
                 ) -> list[float]:
    """价值选帧(T3.9,替换均匀抽帧):镜头中点 + 运动峰值 + 字幕起点前 2 帧 + 人脸出现帧。

    硬上限 cap = min(FRAME_CAP_MAX, 3 + 镜头数)(缺省)。选点按优先级轮次填充
    (中点保底每镜覆盖 → 峰值 → 字幕前 → 人脸),同一时间窗(0.25s)内只取一个,
    超上限即止 —— 同预算下优先解释「剪点/切镜/说话起点」,而不是均匀撒点。
    返回升序时间戳(秒,round 3,确定性)。
    """
    cap = cap if cap is not None else min(FRAME_CAP_MAX, 3 + max(len(shots), 1))
    rounds: list[list[float]] = [[], [], [], []]
    spans = [(int(s["startMs"]), int(s["endMs"])) for s in shots]
    for s in shots:
        rounds[0].append((int(s["startMs"]) + int(s["endMs"])) / 2000.0)      # 镜头中点
        pk = int(s.get("motionPeakMs", -1) or -1)
        if pk > 0:
            rounds[1].append(pk / 1000.0)                                     # 运动峰值
    seen: set[int] = set()
    for c in sorted(chars, key=lambda x: int(x["startMs"])):                  # 字幕起点前 2 帧
        a_ms = int(c["startMs"])
        for k, (a, b) in enumerate(spans):
            if k not in seen and a <= a_ms < b:
                seen.add(k)
                rounds[2].append(max((a_ms - 2000.0 / max(fps, 1e-6)) / 1000.0, 0.0))
                break
    for ms in sorted((face_first_ms or {}).values()):                         # 人脸出现帧
        if ms > 0:
            rounds[3].append(ms / 1000.0)
    picked: list[float] = []

    def _near(t: float) -> bool:
        return any(abs(t - q) < 0.25 for q in picked)

    for rnd in rounds:
        for t in sorted(rnd):
            if len(picked) >= cap:
                break
            if not _near(t):
                picked.append(t)
        if len(picked) >= cap:
            break
    return sorted(round(t, 3) for t in picked)


# ---------------------------------------------------------------- sense_report.md(T3.11)

def build_report(project: Path) -> int:
    """画面能力三态清点 → 05_时间线工程/sense_report.md(零画面能力机器照常产出)。"""
    st_cv = rs_fetchable.state("vision.cv")
    st_track = rs_fetchable.state("vision.track")
    st_shot = rs_fetchable.state("vision.shot")
    cfg = load_config()
    has_vlm = bool(cfg.get("vqa_exe") or (cfg.get("vqa_python") and cfg.get("vqa_cli")))
    l1_ok = st_cv["state"] == "READY"
    lines = ["# 感知能力报告(sense_report)", "",
             "- 生成:rs_vision.py report(三级感知,rules/sense.md 口径)",
             "- L0 结构层:**可用**(纯 ffmpeg 数值,零模型零网络,任何机器都在)",
             "", "| 能力级 | 状态 | 影响的决策 |", "|---|---|---|",
             f"| L0 结构(逐镜档案/骨架) | 可用 | 开场镜选择/转场位置/节奏判断 |",
             f"| L1 标签(人脸/主体/安全带) | {'可用' if l1_ok else '缺失'} "
             f"| 横转竖主体锚/字幕压脸检测 |",
             f"| L2 描述(VLM 逐镜) | {'默认关(组件已备)' if has_vlm else '默认关·组件缺失'} "
             f"| 镜内画面语义描述 |",
             "", "## 组件三态(懒加载 ADR-0049)", "",
             f"- vision.shot:{st_shot['state']}({st_shot['message']})",
             f"- vision.cv:{st_cv['state']}({st_cv['message']})",
             f"- vision.track:{st_track['state']}({st_track['message']})",
             f"- VLM(vqa):{'已配置' if has_vlm else '未配置(config 缺 vqa_exe/vqa_python+vqa_cli)'}",
             "", "## 缺失与建议", ""]
    if not l1_ok:
        lines += ["- L1 缺失:横转竖退化为居中锚(可能裁切主体),字幕压脸只剩几何校验;"
                  "补法:`pip install opencv-python`(约 60MB,vision.cv 能力)"]
    else:
        lines += ["- L1 可用:主体跟踪/安全带占用可产出"]
    if not has_vlm:
        lines += ["- L2 默认关是**预算纪律**不是缺失;要开启:`python tools/fetch_deps.py vqa`"
                  "(约 630MB),再用 `rs_sense.py shots --tier L2 --budget-sec 120` 显式开"]
    lines += ["- 降级不阻塞:L0 结构层(骨架/逐镜档案)零模型照常产出,剪辑决策不中断"]
    path = sense_report_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, "\n".join(lines) + "\n")
    return emit(True, "SENSE_REPORT_OK", f"感知能力报告已写 {path.name}",
                {"path": str(path), "l1": l1_ok, "vlm": has_vlm})


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rs_vision.py",
        description="画面感知层:vision.json 聚合 / skeleton.json 骨架 / 感知能力报告(三级感知)")
    ap.add_argument("command", choices=["vision", "skeleton", "report"],
                    help="vision=聚合逐镜画面档案;skeleton=主轨骨架摘要;report=能力三态报告")
    ap.add_argument("project", nargs="?", default=None, help="工程根(缺省 cwd)")
    ap.add_argument("--force", action="store_true", help="忽略产物新鲜度强制重算")
    a = ap.parse_args(argv)
    root = Path(a.project) if a.project else Path.cwd()
    if not root.is_dir():
        return emit(False, "NO_PROJECT", f"工程目录不存在:{root}", exit_code=EXIT_INPUT)
    out = vision_path(root) if a.command == "vision" else skeleton_path(root)
    src = rs_paths.resolve(root, "cut") / "shots.json"
    if a.command != "report" and not a.force and out.is_file() and src.is_file() \
            and out.stat().st_mtime >= src.stat().st_mtime:
        try:
            doc = json.loads(out.read_text(encoding="utf-8"))
            return emit(True, "CACHED", f"{out.name} 新于 shots.json,跳过重算(--force 强制)",
                        doc)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass                            # 坏产物 → 落到重算
    if a.command == "vision":
        return build_vision(root)
    if a.command == "skeleton":
        return build_skeleton(root)
    return build_report(root)


if __name__ == "__main__":
    sys.exit(main())
