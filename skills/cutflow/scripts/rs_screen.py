"""录屏分析(M8 能力 screen.cursor / screen.zoom / screen.keys / screen.redact 的 detector,
兼 screen.compress 的 waiting 检测辅助,ADR-0047)。

用法:
  rs_screen.py analyze [视频] [--cursor] [--zoom] [--keys] [--redact] [--waiting] --out <工程根>
  rs_screen.py                 # 无参 = 工程模式(挂载器契约:cwd=工程根,自动选视频素材,
                               #   waiting 全量分析 + 各节降级留痕 → screen.json)

双档三态(懒加载体系 rs_fetchable,ADR-0049;本脚本只探测、绝不下载):
  READY    vision.cv(opencv)probe 通过 → 光标轨迹/点击点/键位卡/敏感区检测
  MISSING  降级(方案 §5.5.4):不追光标 / static-zoom 固定区域缩放 / 无键位卡 /
           不打码 + L1 提醒;waiting 检测只用 ffmpeg+标准库,两档同引擎(恒可用)

waiting 检测(方案 §5.5.4 screen.compress 的辅助输出,§5.9 手法 25):
  ≥2.0s 无视觉变化 且 无语音 → 供粗剪加速(2–8×)或删除。视觉变化 = 降采样灰度帧
  平均绝对差;无语音 = ffmpeg silencedetect(复用 rs_cut 既有解析口径)。

产物 04_粗剪决策/screen.json:
  {"cursorTrace": [{tMs, xPct, yPct}], "clickPoints", "keyHints", "redactBoxes",
   "waiting": [{startMs, endMs, ms}], "waitingTotalMs", "zoomPlan", "tiers", "degraded"...}
  光标坐标用源画面百分比(xPct/yPct,0–100),分辨率无关。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import (EXIT_INPUT, EXIT_OK, emit, ffmpeg_bin, load_config,  # noqa: E402
                       media_duration_s, run, write_text_atomic)
import rs_common  # noqa: E402  — ffprobe_json(音轨探测)
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量
import rs_cut  # noqa: E402  — 复用既有 silencedetect 解析口径(方案 §5.5.4 明示复用)
import rs_fetchable  # noqa: E402  — 三态探测(只 state(),绝不触发下载)

# ---------------------------------------------------------------- 参数常量(描述符 params 同源,改一处必改两处)

WAITING_MIN_MS = 2000       # waiting 下限(方案 §5.5.4:≥2.0s 无视觉变化且无语音)
VISUAL_SAMPLE_FPS = 2       # 视觉变化降采样帧率(屏幕内容变化以秒计,2fps 足够)
VISUAL_DOWNSCALE_W = 96     # 视觉变化降采样宽度(平均绝对差在小图上同样有效)
VISUAL_DOWNSCALE_H = 54     # 视觉变化降采样高度(与宽成对定尺寸,见 visual_static_spans 警注)
VISUAL_CHANGE_EPS = 2.0     # 平均绝对差 ≤ 此值 = 无视觉变化(0–255 域,经验值)
SILENCE_DB = -35.0          # 语音静音阈值(与 rs_cut detect_dead_air 同口径)
SILENCE_MIN_MS = 500        # silencedetect 最短静音段(细粒度交并给 waiting)
ZOOM_FACTOR = 1.4           # punch-in 缩放倍率(与 rs_ir punchIn 同值,方案 §5.9 手法 18)
CURSOR_DWELL_MS = 400       # READY 档点击判据:光标滞留 ≥400ms 后快速移动 = 点击(经验值)
CURSOR_TRACE_W = 480        # 光标轨迹分析宽度(精度够光标高亮,解码省时)
CURSOR_STRIDE = 3           # 抽帧步长(~10fps 轨迹,光标高亮够用)
CURSOR_JUMP_MAX_PCT = 40    # 相邻轨迹点跳变 >40% 画面宽 = 不可信,丢弃(场景切换误跟)
KEY_CARD_MIN_MS = 400       # 键位卡最短停留(标定口径:浮层卡滞留 ≥400ms 才算提示卡)
REDACT_MAX_BOXES = 12       # 单帧敏感候选区上限(过检为主,防刷屏)
REDACT_CAND_CONF = 0.3      # 候选区置信度固定低值(机检不判定内容,L1 复核为准)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts", ".flv"}


def screen_path(project: Path) -> Path:
    """04_粗剪决策/screen.json(单一落点)。"""
    return rs_paths.resolve(project, "cut") / "screen.json"


# ---------------------------------------------------------------- waiting 检测(降级友好,ffmpeg+标准库)

def visual_static_spans(media: Path, cfg: dict) -> tuple[list[tuple[int, int]], float]:
    """降采样灰度帧平均绝对差 → 「无视觉变化」区间 ms 列表。返回 (spans, 时长秒)。

    帧尺寸强制 scale={VISUAL_DOWNSCALE_W}:{VISUAL_DOWNSCALE_H},不用 -2 自适应 ——
    按总字节数反推帧高会命中最小偶数高度(96x2 条带),差分恒近零,静区误判全片
    (实测踩坑);定尺寸后 frame_len 恒定,残尾字节直接截断。
    """
    total_s = 0.0
    try:
        total_s = media_duration_s(media, cfg)
    except SystemExit:
        total_s = 0.0
    frame_len = VISUAL_DOWNSCALE_W * VISUAL_DOWNSCALE_H
    try:
        p = subprocess.run(
            [ffmpeg_bin(cfg), "-v", "error", "-i", str(media), "-vf",
             f"fps={VISUAL_SAMPLE_FPS},scale={VISUAL_DOWNSCALE_W}:{VISUAL_DOWNSCALE_H},format=gray",
             "-f", "rawvideo", "-"],
            capture_output=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired):
        return [], total_s
    raw = p.stdout
    if p.returncode != 0 or len(raw) < frame_len * 2:
        return [], total_s
    n = len(raw) // frame_len
    if n < 2:
        return [], total_s
    frame_ms = 1000.0 / VISUAL_SAMPLE_FPS
    prev = raw[:frame_len]
    diffs = [255.0]                             # 第 0 帧视作「有变化」(起点非静态)
    for i in range(1, n):
        cur = raw[i * frame_len:(i + 1) * frame_len]
        acc = 0
        for a, b in zip(prev, cur):
            acc += a - b if a > b else b - a
        diffs.append(acc / frame_len)
        prev = cur
    spans: list[tuple[int, int]] = []
    start = None
    for i, d in enumerate(diffs):
        if d <= VISUAL_CHANGE_EPS:
            if start is None:
                start = i
        else:
            if start is not None:
                _close_span(spans, start, i, frame_ms)
                start = None
    if start is not None:
        _close_span(spans, start, len(diffs), frame_ms)
    return spans, total_s


def _close_span(spans: list[tuple[int, int]], start_f: int, end_f: int, frame_ms: float) -> None:
    s, e = int(start_f * frame_ms), int(end_f * frame_ms)
    if e > s:
        spans.append((s, e))


def waiting_spans(media: Path, cfg: dict) -> tuple[list[dict], float]:
    """「≥WAITING_MIN_MS 无视觉变化 且 无语音」区间(方案 §5.5.4 waiting 判据)。"""
    static, total_s = visual_static_spans(media, cfg)
    total_ms = int(total_s * 1000)
    if _has_audio(media, cfg):
        sil: list[tuple[int, int]] = []
        try:
            p = run([ffmpeg_bin(cfg), "-hide_banner", "-nostats", "-i", str(media),
                     "-af", f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN_MS / 1000.0:.3f}",
                     "-f", "null", "-"], timeout=1800)
            if p.returncode == 0:
                sil = [(int(s["startMs"]), int(s["endMs"]))
                       for s in rs_cut.parse_silencedetect(p.stderr or "")]
        except (OSError, subprocess.TimeoutExpired):
            sil = []
    else:
        # 无音轨 = 全程无语音(录屏不收麦克风的常态),waiting 判据只看视觉变化
        sil = [(0, max(total_ms, 1))]
    out: list[dict] = []
    for ss, se in static:
        for qs, qe in sil:
            lo, hi = max(ss, qs), min(se, qe)
            if hi - lo >= WAITING_MIN_MS:
                out.append({"startMs": lo, "endMs": hi, "ms": hi - lo})
    out.sort(key=lambda w: (w["startMs"], w["endMs"]))
    # 重叠合并(静音段交并可能产生包含关系)
    merged: list[dict] = []
    for w in out:
        if merged and w["startMs"] <= merged[-1]["endMs"]:
            last = merged[-1]
            last["endMs"] = max(last["endMs"], w["endMs"])
            last["ms"] = last["endMs"] - last["startMs"]
            continue
        merged.append(w)
    return merged, total_s


# ---------------------------------------------------------------- READY 档(opencv;主解释器装了 opencv 即真跑)

def _has_audio(media: Path, cfg: dict) -> bool:
    """ffprobe 探测是否有音轨(无音轨 = 全程无语音,waiting 只看视觉变化)。"""
    try:
        info = rs_common.ffprobe_json(media, cfg)
        return any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    except (SystemExit, OSError, json.JSONDecodeError, KeyError):
        return False


def ready_cursor_trace(media: Path, cfg: dict) -> list[dict] | None:
    """READY 档光标轨迹:帧差小连通域质心 → 百分比坐标序列(None = 组件不可用)。

    判据(经典屏幕光标检测,经验实现):相邻帧 absdiff → 阈值二值 → 小面积连通域
    (光标箭头在小图上只占几十~几百像素)→ 与上一位置最近的质心为当前光标。
    """
    try:
        import cv2  # noqa: PLC0415 — probe READY 才走到;组件缺失由调用方降级
    except ImportError:
        return None
    cap = cv2.VideoCapture(str(media))
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    src_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1
    src_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1
    step_ms = 1000.0 / max(fps, 1e-6)
    scale = CURSOR_TRACE_W / max(float(src_w), 1.0)
    proc_h = max(2, int(float(src_h) * scale) // 2 * 2)
    prev, trace, idx = None, [], 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % CURSOR_STRIDE:
            idx += 1
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break
        small = cv2.resize(frame, (CURSOR_TRACE_W, proc_h))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if prev is not None:
            diff = cv2.absdiff(gray, prev)
            _, bw = cv2.threshold(diff, 24, 255, cv2.THRESH_BINARY)
            n, labels, stats, _ = cv2.connectedComponentsWithStats(bw)
            best, best_d = None, 1e18
            last = trace[-1] if trace else None
            for i in range(1, n):
                area = int(stats[i, cv2.CC_STAT_AREA])
                if not (8 <= area <= 2000):      # 光标大小域;过大 = 场景切换,过小 = 噪点
                    continue
                cx = (stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH] / 2) / CURSOR_TRACE_W * 100
                cy = (stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT] / 2) / proc_h * 100
                d = 0.0 if last is None else (cx - last["xPct"]) ** 2 + (cy - last["yPct"]) ** 2
                if d < best_d:
                    best, best_d = (cx, cy), d
            if best is not None and (last is None or best_d <= CURSOR_JUMP_MAX_PCT ** 2):   # 跳变超限视作不可信
                trace.append({"tMs": int(idx * step_ms),
                              "xPct": round(best[0], 2), "yPct": round(best[1], 2)})
        prev = gray
        idx += 1
    cap.release()
    return trace


def ready_click_points(trace: list[dict]) -> list[dict]:
    """READY 档点击点:光标滞留 ≥CURSOR_DWELL_MS 后快速移动的位置(经验判据)。

    判据拆两步:位移 >1%(画面宽)记一次「移动」;两次移动之间隔 ≥CURSOR_DWELL_MS
    即「滞留后移动」= 疑似点击,点击点取移动起点(移动发生前的光标位置)。
    """
    clicks: list[dict] = []
    last_move_t: int = trace[0]["tMs"] if trace else 0    # 录制起点视作上次移动时刻
    for i in range(1, len(trace)):
        dx = trace[i]["xPct"] - trace[i - 1]["xPct"]
        dy = trace[i]["yPct"] - trace[i - 1]["yPct"]
        if (dx * dx + dy * dy) ** 0.5 <= 1.0:
            continue                             # 微动不算移动(抖动/亚像素)
        if trace[i - 1]["tMs"] - last_move_t >= CURSOR_DWELL_MS:
            clicks.append({"tMs": trace[i - 1]["tMs"],
                           "xPct": trace[i - 1]["xPct"], "yPct": trace[i - 1]["yPct"]})
        last_move_t = trace[i - 1]["tMs"]
    return clicks


def ready_key_hints(media: Path, cfg: dict) -> list[dict]:
    """READY 档键位/快捷键提示卡检测。

    M8 第一波口径:算法依赖录屏样本标定(浮层卡的视觉特征因录制工具而异),
    本期如实返回空表 + tiers 留痕,不伪造检测结论;标定后在此补真实现。
    """
    return []


def ready_redact_boxes(media: Path, cfg: dict) -> list[dict]:
    """READY 档敏感信息候选区:梯度形态学的类文本域(过检为主,全部标「须人工确认」)。

    机检只圈候选、不判定内容 —— 密钥/隐私的最终判定是 L1 目测(方案 §5.5.4)。
    """
    try:
        import cv2  # noqa: PLC0415
    except ImportError:
        return []
    cap = cv2.VideoCapture(str(media))
    if not cap.isOpened():
        return []
    total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(total * 0.5)))   # 取中段一帧做代表
    ok, frame = cap.read()
    cap.release()
    if not ok:
        return []
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    closed = cv2.morphologyEx(bw, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (9, 3)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed)
    out: list[dict] = []
    for i in range(1, n):
        x, y, cw, ch, area = (int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP]),
                              int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT]),
                              int(stats[i, cv2.CC_STAT_AREA]))
        if ch < 8 or cw < 24 or not (1.5 <= cw / ch <= 40):   # 类文本行形状域
            continue
        out.append({"xPct": round(x / w * 100, 2), "yPct": round(y / h * 100, 2),
                    "wPct": round(cw / w * 100, 2), "hPct": round(ch / h * 100, 2),
                    "conf": REDACT_CAND_CONF,
                    "note": "类文本候选区,须人工确认是否敏感信息(L1)"})
        if len(out) >= REDACT_MAX_BOXES:
            break
    return out


def _cv_ready() -> bool:
    return rs_fetchable.state("vision.cv")["state"] == "READY"


# ---------------------------------------------------------------- 组装与产物

def analyze(media: Path, out_root: Path, *, cursor: bool = False, zoom: bool = False,
            keys: bool = False, redact: bool = False, waiting: bool = False,
            force: bool = False) -> tuple[dict, int]:
    """单素材全流程 → screen.json。只分析显式请求的节;无参工程模式全量(全是轻活)。"""
    out_path = screen_path(out_root)
    cfg = load_config()
    want_all = not (cursor or zoom or keys or redact or waiting)   # 挂载器无参全量
    doc: dict = {"source": media.name}
    tiers: dict = {}
    degraded_reasons: list[str] = []

    if waiting or want_all:
        spans, total_s = waiting_spans(media, cfg)
        doc["waiting"] = spans
        doc["waitingTotalMs"] = sum(w["ms"] for w in spans)
        doc["durationSec"] = round(total_s, 3)
        tiers["waiting"] = {"engine": "frame-diff+silencedetect", "degraded": False,
                            "minMs": WAITING_MIN_MS}

    cv_ready = _cv_ready()
    if cv_ready:
        trace = ready_cursor_trace(media, cfg)
        trace = trace if trace is not None else []
        tiers["cursor"] = {"engine": "opencv", "degraded": False}
    else:
        trace = []
        tiers["cursor"] = {"engine": "none", "degraded": True,
                           "degradeReason": "none", "missingComponent": "opencv",
                           "note": "不追光标(组件未部署;如需光标高亮先 rs_fetchable install opencv)"}
        degraded_reasons.append("cursor")
    if cursor or want_all:
        doc["cursorTrace"] = trace if (cv_ready and (cursor or want_all)) else []

    if zoom or want_all:
        if cv_ready:
            clicks = ready_click_points(trace)
            doc["clickPoints"] = clicks
            doc["zoomPlan"] = [{"tMs": c["tMs"], "scale": ZOOM_FACTOR,
                                "xPct": c["xPct"], "yPct": c["yPct"]} for c in clicks]
            tiers["zoom"] = {"engine": "opencv", "degraded": False}
        else:
            # 降级档 static-zoom:固定居中 punch-in(方案 §5.5.4)
            doc["clickPoints"] = []
            doc["zoomPlan"] = [{"tMs": 0, "scale": ZOOM_FACTOR, "xPct": 50.0, "yPct": 50.0,
                                "mode": "static-zoom"}]
            tiers["zoom"] = {"engine": "static-zoom", "degraded": True,
                             "degradeReason": "static-zoom", "missingComponent": "opencv",
                             "note": "固定区域缩放(无点击检测,点击落拍需人工/READY 档)"}
            degraded_reasons.append("zoom")

    if keys or want_all:
        doc["keyHints"] = ready_key_hints(media, cfg) if cv_ready else []
        if not cv_ready:
            tiers["keys"] = {"engine": "none", "degraded": True,
                             "degradeReason": "none", "missingComponent": "opencv",
                             "note": "无键位提示卡检测;快捷键讲解请人工补 artboard 卡"
                                     f"(标定口径:浮层卡滞留 ≥{KEY_CARD_MIN_MS}ms 才算提示卡)"}
            degraded_reasons.append("keys")
        else:
            tiers["keys"] = {"engine": "opencv", "degraded": False}

    if redact or want_all:
        doc["redactBoxes"] = ready_redact_boxes(media, cfg) if cv_ready else []
        if not cv_ready:
            tiers["redact"] = {"engine": "none", "degraded": True,
                               "degradeReason": "none", "missingComponent": "opencv",
                               "note": "L1 提醒:交付前必须人工目测密钥/隐私信息并决定是否补打码"}
            degraded_reasons.append("redact")
        else:
            tiers["redact"] = {"engine": "opencv", "degraded": False,
                               "note": "机检是辅助,L1 目测复核仍是交付前置"}

    doc["tiers"] = tiers
    if degraded_reasons:
        rec = rs_fetchable.degrade_record("vision.cv")
        doc.update(rec)
        doc["degradedSections"] = degraded_reasons
    else:
        doc["degraded"] = False
    _write(out_path, doc)
    return doc, EXIT_OK


def _write(out_path: Path, doc: dict) -> None:
    write_text_atomic(out_path, json.dumps(doc, ensure_ascii=False, indent=1))


def _pick_project_video(root: Path) -> Path | None:
    """工程模式选素材:manifest probe 通过的视频优先,扩展名兜底(与 rs_shot 同口径)。"""
    man = rs_paths.manifest_json(root)
    items: list[dict] = []
    if man.is_file():
        try:
            items = json.loads(man.read_text(encoding="utf-8")).get("items") or []
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            items = []
    mat_dir = rs_paths.resolve(root, "materials")
    for it in items:
        if it.get("probe") == "ok" and Path(str(it.get("file", ""))).suffix.lower() in VIDEO_EXTS:
            q = mat_dir / str(it.get("file", ""))
            if q.is_file():
                return q
    if mat_dir.is_dir():
        for q in sorted(mat_dir.iterdir()):
            if q.is_file() and q.suffix.lower() in VIDEO_EXTS:
                return q
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rs_screen.py",
        description="录屏分析(M8):光标/点击/键位/打码 + waiting 检测;READY=opencv,降级必留痕")
    ap.add_argument("command", nargs="?", choices=["analyze"], default=None,
                    help="analyze=显式指定素材;缺省 = 工程模式(自动选素材,挂载器契约)")
    ap.add_argument("media", nargs="?", default=None, help="录屏视频路径")
    ap.add_argument("--out", default=None, help="工程根(产物 04_粗剪决策/screen.json)")
    ap.add_argument("--cursor", action="store_true", help="光标轨迹追踪(缺组件降级:不追光标)")
    ap.add_argument("--zoom", action="store_true", help="点击处 punch-in 缩放(缺组件降级:static-zoom)")
    ap.add_argument("--keys", action="store_true", help="按键/快捷键提示卡检测(缺组件降级:无)")
    ap.add_argument("--redact", action="store_true", help="敏感信息区检测(缺组件降级:none + L1 提醒)")
    ap.add_argument("--waiting", action="store_true",
                    help="waiting 检测:≥2.0s 无视觉变化且无语音区间(供加速/删除,screen.compress 用)")
    ap.add_argument("--force", action="store_true", help="工程模式下忽略产物新鲜度强制重算")
    a = ap.parse_args(argv)

    if a.command == "analyze":
        if not a.media or not a.out:
            return emit(False, "BAD_INPUT", "analyze 需要 <视频> 与 --out <工程根>", exit_code=EXIT_INPUT)
        media = Path(a.media)
        if not media.is_file():
            return emit(False, "NO_MEDIA", f"素材不存在:{media}", exit_code=EXIT_INPUT)
        doc, rc = analyze(media, Path(a.out), cursor=a.cursor, zoom=a.zoom, keys=a.keys,
                          redact=a.redact, waiting=a.waiting, force=a.force)
        return emit(rc == EXIT_OK, "SCREEN_OK",
                    f"waiting={len(doc.get('waiting') or [])}段 cursor={len(doc.get('cursorTrace') or [])}点 "
                    f"degraded={doc.get('degraded', False)}", doc, exit_code=rc)
    # 工程模式(无参,挂载器/第二波 Agent 的默认入口)
    root = Path.cwd()
    out_path = screen_path(root)
    media = _pick_project_video(root)
    if media is None:
        return emit(False, "NO_MEDIA",
                    f"工程无视频素材({rs_paths.p('materials')}/ 为空或 probe 全败);未写产物",
                    exit_code=EXIT_INPUT)
    if not a.force and out_path.is_file() and out_path.stat().st_mtime >= media.stat().st_mtime:
        try:
            doc = json.loads(out_path.read_text(encoding="utf-8"))
            return emit(True, "SCREEN_CACHED",
                        f"screen.json 新于素材,跳过重算(--force 强制):waiting={len(doc.get('waiting') or [])}段",
                        doc)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass                                # 坏产物 → 落到重算
    doc, rc = analyze(media, root, force=a.force)
    return emit(rc == EXIT_OK, "SCREEN_OK",
                f"素材={media.name} waiting={len(doc.get('waiting') or [])}段 "
                f"degraded={doc.get('degraded', False)}", doc, exit_code=rc)


if __name__ == "__main__":
    sys.exit(main())
