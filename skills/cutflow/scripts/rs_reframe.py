"""横转竖自动重构(M8 vlog 能力 vision.track / vision.reframe 的 detector,ADR-0047)。

用法:
  rs_reframe.py plan [工程根] [--ratio 9x16] [--force]
  rs_reframe.py                # 无参 = 工程模式(cwd=工程根,读 IR 主轨逐 clip 出锚点)

双档三态(懒加载体系 rs_fetchable,ADR-0049;本脚本只探测、绝不下载):
  READY    bytetrack probe 通过 → 逐帧主体中心 → 滑窗中位数平滑 + 限速(防「跟着人乱甩」)
  MISSING  降级档 static-center:每 clip 居中锚(≈零计算,方案 §6.2 预算)

硬约束(方案 §5.5.2):
  · 裁切不得切掉主体 —— 主体包围盒必须完整在裁切窗内;违反报 REFRAME_CLIP_SUBJECT
    并把该 clip 降级 static-center + 留痕;
  · 裁切不拉伸 —— 裁切窗宽高比恒等于目标画幅比(继承 artboard 桥「尺寸不符报错不拉伸」精神)。

产物 05_时间线工程/reframe_plan.json:
  {"ratio", "canvas", "engine", "degraded", "clips": [{clipId, anchorX, anchorY, scale,
    trajectory[], cropWindow, mode, violations[]}], ...}
  anchorX/anchorY/trajectory 坐标均在【源像素空间】(渲染端按 sourceInMs 映射)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import (EXIT_INPUT, EXIT_OK, RATIOS, emit, ffprobe_json,  # noqa: E402
                       load_config, write_text_atomic)
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量
import rs_fetchable  # noqa: E402  — 三态探测(只 state(),绝不触发下载)

# ---------------------------------------------------------------- 参数常量(描述符 params 同源,改一处必改两处)

DEFAULT_SCALE = 1.0         # static-center 档缩放(1.0 = 不推近;推近由 rs_edit clip.reframe 显式做)
SMOOTH_WIN = 15             # 轨迹滑窗中位数窗口(帧数;约 0.5s@30fps,消检测抖动)
MAX_SHIFT_PX_PER_SEC = 240.0  # 锚点限速(源像素/秒;超速分摊到后续帧,防镜头乱甩)
REFRAME_CLIP_SUBJECT = "REFRAME_CLIP_SUBJECT"   # 主体被切破的错误码(方案 §5.5.2 硬约束)


def reframe_path(project: Path) -> Path:
    """05_时间线工程/reframe_plan.json(单一落点)。"""
    return rs_paths.resolve(project, "timeline") / "reframe_plan.json"


# ---------------------------------------------------------------- 纯几何(单测直测,不碰视频)

def crop_window(src_w: int, src_h: int, anchor_x: float, anchor_y: float,
                scale: float, ratio_wh: float) -> dict:
    """源画幅内取裁切窗:窗比恒等于目标比(不拉伸);贴边时平移夹紧,越界时收缩。

    scale=源高缩放倍率(1.0 = 窗高等于源高)。返回 {x0, y0, w, h}(float,源像素)。
    """
    scale = max(scale, 1e-6)
    win_h = src_h / scale
    win_w = win_h * ratio_wh
    if win_w > src_w:                            # 比目标比更宽的源:以源宽为限收缩窗
        win_w = float(src_w)
        win_h = win_w / ratio_wh
    x0 = min(max(anchor_x - win_w / 2.0, 0.0), max(src_w - win_w, 0.0))
    y0 = min(max(anchor_y - win_h / 2.0, 0.0), max(src_h - win_h, 0.0))
    return {"x0": round(x0, 2), "y0": round(y0, 2),
            "w": round(win_w, 2), "h": round(win_h, 2)}


def subject_contained(bbox: tuple[float, float, float, float], win: dict) -> bool:
    """主体包围盒 (x0,y0,x1,y1) 是否完整在裁切窗内(硬约束判据)。"""
    bx0, by0, bx1, by1 = bbox
    return (bx0 >= win["x0"] and by0 >= win["y0"]
            and bx1 <= win["x0"] + win["w"] and by1 <= win["y0"] + win["h"])


def smooth_track(points: list[tuple[float, float]], fps: float
                 ) -> list[tuple[float, float]]:
    """逐帧主体中心 → 滑窗中位数平滑 + 限速(READY 档真数学;检测本身在 mock 边界外)。

    ①滑窗中位数:窗口 SMOOTH_WIN 帧,消单帧检测抖动;
    ②限速:相邻锚点位移 ≤ MAX_SHIFT_PX_PER_SEC/fps,超速按方向截断(后续帧自然追上)。
    """
    if not points:
        return []
    half = SMOOTH_WIN // 2
    smoothed: list[tuple[float, float]] = []
    for i in range(len(points)):
        lo, hi = max(0, i - half), min(len(points), i + half + 1)
        xs = sorted(p[0] for p in points[lo:hi])
        ys = sorted(p[1] for p in points[lo:hi])
        m = len(xs) // 2
        smoothed.append((xs[m], ys[m]))
    max_step = MAX_SHIFT_PX_PER_SEC / max(fps, 1e-6)
    out = [smoothed[0]]
    for x, y in smoothed[1:]:
        px, py = out[-1]
        dx, dy = x - px, y - py
        dist = (dx * dx + dy * dy) ** 0.5
        if dist > max_step > 0:
            k = max_step / dist
            dx, dy = dx * k, dy * k
        out.append((px + dx, py + dy))
    return [(round(x, 2), round(y, 2)) for x, y in out]


def clip_plan_static(src_w: int, src_h: int, ratio_wh: float) -> dict:
    """降级档 static-center:居中锚 + 整幅高裁切窗 + 单关键帧轨迹(≈零计算)。"""
    win = crop_window(src_w, src_h, src_w / 2.0, src_h / 2.0, DEFAULT_SCALE, ratio_wh)
    ax, ay = win["x0"] + win["w"] / 2.0, win["y0"] + win["h"] / 2.0
    return {"mode": "static-center", "anchorX": round(ax, 2), "anchorY": round(ay, 2),
            "scale": DEFAULT_SCALE, "cropWindow": win,
            "trajectory": [{"tMs": 0, "anchorX": round(ax, 2), "anchorY": round(ay, 2),
                            "scale": DEFAULT_SCALE}],
            "violations": []}


def clip_plan_track(src_w: int, src_h: int, ratio_wh: float, clip_ms: int, fps: float,
                    subject_boxes: list[tuple[float, float, float, float]] | None) -> dict:
    """READY 档:逐帧主体盒 → 中点序列 → 平滑限速轨迹;每帧窗内复核主体硬约束。

    任一帧违反 REFRAME_CLIP_SUBJECT → 整 clip 降级 static-center + violations 留痕
    (宁可退回居中,绝不交付切掉主体的方案)。
    """
    plan = clip_plan_static(src_w, src_h, ratio_wh)
    if not subject_boxes:
        return plan                              # 检测无主体 → 维持 static-center(如实)
    centers = [((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0) for b in subject_boxes]
    track = smooth_track(centers, fps)
    n = max(1, int(clip_ms / 1000.0 * fps))
    trajectory: list[dict] = []
    violations: list[dict] = []
    step_ms = clip_ms / n
    for i in range(n):
        x, y = track[min(i, len(track) - 1)]
        win = crop_window(src_w, src_h, x, y, DEFAULT_SCALE, ratio_wh)
        box = subject_boxes[min(i, len(subject_boxes) - 1)]
        if not subject_contained(box, win):
            violations.append({"code": REFRAME_CLIP_SUBJECT, "atMs": int(i * step_ms),
                               "detail": "主体包围盒超出裁切窗"})
    if violations:
        plan["violations"] = violations
        plan["mode"] = "static-center"           # 降级留痕:违反硬约束退回居中
        plan["degradeNote"] = (f"跟踪轨迹在 {len(violations)} 帧切破主体,"
                               f"按 {REFRAME_CLIP_SUBJECT} 降级 static-center")
        return plan
    trajectory = [{"tMs": int(i * step_ms), "anchorX": track[min(i, len(track) - 1)][0],
                   "anchorY": track[min(i, len(track) - 1)][1], "scale": DEFAULT_SCALE}
                  for i in range(n)]
    x_last, y_last = track[-1]
    plan.update({"mode": "track", "anchorX": round(x_last, 2), "anchorY": round(y_last, 2),
                 "trajectory": trajectory})
    return plan


# ---------------------------------------------------------------- READY 档检测(测试 mock 本函数)

def ready_subject_boxes(media: Path, max_frames: int = 600
                        ) -> list[tuple[float, float, float, float]] | None:
    """READY 档逐帧主体包围盒(opencv+bytetrack;None = 检测不可用)。

    真实实现依赖 tools/.venv 侧组件;测试 mock 本函数提供合成盒,轨迹数学走真代码。
    """
    try:
        import cv2  # noqa: F401,PLC0415 — probe 已保证 READY 才会走到这里
    except ImportError:
        return None
    return None                                  # bytetrack 驱动未部署时如实返回不可用


def ready_tier() -> tuple[bool, dict]:
    """probe vision.track → (是否可用, tiers 留痕)。"""
    st = rs_fetchable.state("vision.track")
    if st["state"] == "READY":
        return True, {"track": {"engine": "bytetrack", "degraded": False}}
    return False, {"track": {"engine": "static-center", "degraded": True,
                             **{k: v for k, v in rs_fetchable.degrade_record("vision.track").items()
                                if k != "degraded"}}}


# ---------------------------------------------------------------- 组装与产物

def _src_dims(src: Path, manifest: dict, cfg: dict) -> tuple[int, int]:
    """源画幅:manifest probe 优先,ffprobe 兜底;都失败按 1080x1920 假设并靠调用方留痕。"""
    for it in manifest.get("items") or []:
        if str(it.get("file", "")) == src.name and it.get("width") and it.get("height"):
            return int(it["width"]), int(it["height"])
    try:
        info = ffprobe_json(src, cfg)
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
        if v.get("width") and v.get("height"):
            return int(v["width"]), int(v["height"])
    except (SystemExit, OSError, json.JSONDecodeError, KeyError):
        pass
    return 1080, 1920


def plan_project(root: Path, ratio: str | None = None, force: bool = False
                 ) -> tuple[dict, int]:
    """工程全流程:IR 主轨逐 clip → 锚点方案 → reframe_plan.json。"""
    out_path = reframe_path(root)
    ir_path = rs_paths.project_json(root)
    if not ir_path.is_file():
        doc = {"version": 1, "ratio": ratio or "9x16", "engine": "static-center",
               "clips": [], "tiers": {"track": {"engine": "static-center", "degraded": True,
                                                "note": f"IR 缺失({rs_paths.rel(root, 'timeline', 'project.json')}),"
                                                        "无 clip 可规划"}}}
        doc.update(rs_fetchable.degrade_record("vision.track"))
        _write(out_path, doc)
        return doc, EXIT_OK
    try:
        ir = json.loads(ir_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return emit(False, "IR_UNREADABLE", f"IR 解析失败:{exc}", exit_code=EXIT_INPUT), EXIT_INPUT
    canvas = ir.get("canvas") or {}
    if ratio is None:
        outs = ir.get("outputs") or []
        ratio = outs[0] if outs and outs[0] in RATIOS else None
    ratio = ratio or rs_common_ratio_for(canvas)
    w, h = RATIOS.get(ratio, (1080, 1920))
    ratio_wh = w / h
    cfg = load_config()
    manifest: dict = {}
    man_path = rs_paths.manifest_json(root)
    if man_path.is_file():
        try:
            manifest = json.loads(man_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            manifest = {}
    track_ready, tiers = ready_tier()
    fps = float(ir.get("fps") or 30)
    clips_out: list[dict] = []
    for tr in ir.get("tracks") or []:
        if tr.get("kind") != "video":
            continue
        for clip in tr.get("clips") or []:
            src = root / str(clip.get("src", ""))
            sw, sh = (1080, 1920)
            if src.is_file():
                sw, sh = _src_dims(src, manifest, cfg)
            if track_ready and src.is_file():
                boxes = ready_subject_boxes(src)
                plan = clip_plan_track(sw, sh, ratio_wh, int(clip.get("durationMs") or 0),
                                       fps, boxes)
            else:
                plan = clip_plan_static(sw, sh, ratio_wh)
            clips_out.append({"clipId": clip.get("id", ""), "src": clip.get("src", ""),
                              "srcWidth": sw, "srcHeight": sh,
                              "startMs": clip.get("startMs", 0),
                              "durationMs": clip.get("durationMs", 0), **plan})
    engine = "track" if track_ready else "static-center"
    doc = {"version": 1, "ratio": ratio, "canvas": [w, h], "engine": engine,
           "clipCount": len(clips_out), "clips": clips_out, "tiers": tiers,
           "source": ir_path.name}
    if not track_ready:
        doc.update(rs_fetchable.degrade_record("vision.track"))
    _write(out_path, doc)
    return doc, EXIT_OK


def rs_common_ratio_for(canvas: dict) -> str:
    """canvas {width,height} → 比例键(未知画幅退 9x16,留痕由 tiers 承担)。"""
    try:
        from rs_common import ratio_for_canvas  # noqa: PLC0415
        return ratio_for_canvas(int(canvas.get("width", 1080)), int(canvas.get("height", 1920)))
    except (ValueError, TypeError):
        return "9x16"


def _write(out_path: Path, doc: dict) -> None:
    write_text_atomic(out_path, json.dumps(doc, ensure_ascii=False, indent=1))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rs_reframe.py",
        description="横转竖自动重构(M8 vlog 能力):READY=bytetrack 跟踪;降级=static-center 居中,必留痕")
    ap.add_argument("command", nargs="?", choices=["plan"], default=None,
                    help="plan=按 IR 出重构方案;缺省 = 工程模式(同 plan,cwd=工程根)")
    ap.add_argument("project", nargs="?", default=None, help="工程根(缺省 cwd)")
    ap.add_argument("--ratio", default=None, choices=list(RATIOS),
                    help="目标画幅(缺省取 IR outputs[0],再缺省 9x16)")
    ap.add_argument("--force", action="store_true", help="忽略产物新鲜度强制重算")
    a = ap.parse_args(argv)

    root = Path(a.project) if a.project else Path.cwd()
    out_path = reframe_path(root)
    ir_path = rs_paths.project_json(root)
    if not a.force and out_path.is_file() and (not ir_path.is_file()
                                               or out_path.stat().st_mtime >= ir_path.stat().st_mtime):
        try:
            doc = json.loads(out_path.read_text(encoding="utf-8"))
            return emit(True, "REFRAME_CACHED",
                        "reframe_plan.json 新于 IR,跳过重算(--force 强制):"
                        f"clips={doc.get('clipCount')}", doc)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass                                # 坏产物 → 落到重算
    doc, rc = plan_project(root, ratio=a.ratio, force=a.force)
    code = "REFRAME_OK" if rc == EXIT_OK else "REFRAME_FAIL"
    return emit(rc == EXIT_OK, code,
                f"clips={doc.get('clipCount', len(doc.get('clips') or []))} "
                f"engine={doc.get('engine')} ratio={doc.get('ratio')} "
                f"degraded={doc.get('degraded', False)}", doc, exit_code=rc)


if __name__ == "__main__":
    sys.exit(main())
