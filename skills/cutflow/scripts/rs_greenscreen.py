"""绿幕/蓝幕检测(ADR-0031)。

v0.14 起 CutFlow **不再承担抠像与背景合成** —— 用户须先自行抠像并合成好背景,
再把处理完的素材交付给管线剪辑。本模块在 S0 摄取时抽帧检测素材是否仍是绿幕/蓝幕:
命中即阻断流水线并提示用户自行处理;确属误判时,用户说明后可用
`rs_ingest.py green-ok` 记录放行(留痕可审计)。

判据(对每帧缩放为 64px 宽的 RGB 原始像素做纯 Python 统计):
  · 边框环(外 20%)的幕色像素占比 ≥ 0.45 —— 幕布通常铺满背景;
  · 全帧幕色像素占比 ≥ 0.18;
  · 幕色像素亮度离散度(std/mean)≤ 0.40 —— 幕布均匀,区别于草地/树叶等自然绿。
三个条件同时满足才判"绿幕/蓝幕",降低误报(误报可被用户放行,漏报才是事故)。

纯标准库实现:只调 ffmpeg 抽帧,不引入 numpy。
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rs_common  # noqa: E402
from rs_common import emit  # noqa: E402

OVERRIDE_REL = ("00_brief", "greenscreen-override.txt")
SAMPLE_RATIOS = (0.08, 0.24, 0.40, 0.56, 0.72, 0.88)
FRAME_W = 64
DOMINANCE = 1.20          # 幕色通道需比其余两个通道高 20%
MIN_CHANNEL = 60          # 幕色通道至少 60/255(排除暗场)
BORDER_FRAC = 0.45        # 边框环幕色占比阈值
OVERALL_FRAC = 0.18       # 全帧幕色占比阈值
MAX_CV = 0.40             # 幕色通道离散度上限(std/mean)


# ---------------------------------------------------------------- 抽帧/统计

def _raw_frame(src: Path, at_s: float, cfg: dict) -> tuple[bytes, int, int] | None:
    """抽一帧缩放到 FRAME_W 宽,返回 (rgb24 bytes, w, h)。失败返回 None(不抛)。"""
    cmd = [rs_common.ffmpeg_bin(cfg), "-v", "error", "-ss", f"{max(0.0, at_s):.3f}",
           "-i", str(src), "-frames:v", "1",
           "-vf", f"scale={FRAME_W}:-2,format=rgb24", "-f", "rawvideo", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True,
                           timeout=int(__import__("os").environ.get("CUTFLOW_GREEN_TIMEOUT", "60")))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0 or not p.stdout:
        return None
    w = FRAME_W
    h = len(p.stdout) // (w * 3)
    if h < 8:
        return None
    return p.stdout[:w * h * 3], w, h


def analyze_frame(raw: bytes, w: int, h: int) -> dict:
    """单帧幕色统计。返回 {greenRatio, blueRatio, borderGreen, borderBlue, cv}。"""
    n = w * h
    bx0, bx1 = int(w * 0.20), int(w * 0.80)
    by0, by1 = int(h * 0.20), int(h * 0.80)
    green = blue = border_green = border_blue = 0
    g_vals: list[int] = []
    b_vals: list[int] = []
    for i in range(n):
        r, g, b = raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2]
        is_g = g >= MIN_CHANNEL and g >= r * DOMINANCE and g >= b * DOMINANCE
        is_b = b >= MIN_CHANNEL and b >= r * DOMINANCE and b >= g * DOMINANCE
        if is_g:
            green += 1
            g_vals.append(g)
        elif is_b:
            blue += 1
            b_vals.append(b)
        x, y = i % w, i // w
        if x < bx0 or x >= bx1 or y < by0 or y >= by1:
            if is_g:
                border_green += 1
            elif is_b:
                border_blue += 1
    border_n = max(1, n - (bx1 - bx0) * (by1 - by0))

    def _cv(vals: list[int]) -> float:
        if len(vals) < 30:
            return 9.9
        mean = sum(vals) / len(vals)
        return (statistics.pstdev(vals) / mean) if mean else 9.9

    return {"greenRatio": round(green / n, 4), "blueRatio": round(blue / n, 4),
            "borderGreen": round(border_green / border_n, 4),
            "borderBlue": round(border_blue / border_n, 4),
            "cvGreen": round(_cv(g_vals), 3), "cvBlue": round(_cv(b_vals), 3)}


def _decide(frames: list[dict]) -> dict:
    """多帧平均 → 判定。返回 {detected, kind, confidence, ...} 或未检出时 detected=False。"""
    if not frames:
        return {"detected": False, "kind": None, "confidence": 0.0, "frames": 0}
    avg = {k: sum(f[k] for f in frames) / len(frames) for k in frames[0]}

    def _hit(prefix: str) -> bool:
        return (avg[f"border{prefix}"] >= BORDER_FRAC
                and avg[f"{prefix.lower()}Ratio"] >= OVERALL_FRAC
                and avg[f"cv{prefix}"] <= MAX_CV)

    g_hit, b_hit = _hit("Green"), _hit("Blue")
    kind = None
    if g_hit and (not b_hit or avg["borderGreen"] >= avg["borderBlue"]):
        kind = "green"
    elif b_hit:
        kind = "blue"
    res = {"detected": kind is not None, "kind": kind, "frames": len(frames),
           "greenRatio": round(avg["greenRatio"], 4), "blueRatio": round(avg["blueRatio"], 4),
           "borderGreen": round(avg["borderGreen"], 4), "borderBlue": round(avg["borderBlue"], 4),
           "cvGreen": round(avg["cvGreen"], 3), "cvBlue": round(avg["cvBlue"], 3),
           "sampleCount": len(frames)}
    res["confidence"] = round(max(avg["borderGreen"], avg["borderBlue"]), 4) if kind else 0.0
    return res


def detect_media(media: str | Path, cfg: dict | None = None, samples: int = 6) -> dict:
    """检测单个媒体文件;ffmpeg/ffprobe 不可用时返回 probe 状态而不是抛异常。"""
    src = Path(media)
    if not src.is_file():
        return {"probe": "missing", "detected": False, "error": f"文件不存在:{src}"}
    try:
        cfg = cfg or rs_common.load_config()
    except SystemExit as exc:
        return {"probe": "unavailable", "detected": False, "error": f"缺 config.json:{exc}"}
    try:
        info = rs_common.ffprobe_json(src, cfg)
        dur = float(info.get("format", {}).get("duration") or 0)
        has_video = any(s.get("codec_type") == "video" for s in info.get("streams", []))
    except SystemExit as exc:
        return {"probe": "unavailable", "detected": False, "error": f"ffprobe 不可用:{exc}"}
    if not has_video:
        return {"probe": "ok", "detected": False, "kind": None, "frames": 0,
                "error": "无视频流,跳过"}
    pts = [r * dur for r in SAMPLE_RATIOS[:max(1, samples)]] if dur > 0 else [0.0]
    frames: list[dict] = []
    for t in pts:
        got = _raw_frame(src, t, cfg)
        if got:
            frames.append(analyze_frame(*got))
    if not frames:
        return {"probe": "failed", "detected": False, "error": "抽帧失败(ffmpeg 不可用或素材损坏)"}
    res = _decide(frames)
    res["probe"] = "ok"
    res["durationMs"] = int(round(dur * 1000))
    return res


# ---------------------------------------------------------------- 放行留痕

def override_path(root: str | Path) -> Path:
    p = Path(root)
    for part in OVERRIDE_REL:
        p = p / part
    return p


def read_override(root: str | Path) -> str | None:
    """读放行说明;无文件/空文件 → None。也接受 brief.md 里的显式误判声明。"""
    p = override_path(root)
    if p.is_file():
        txt = p.read_text(encoding="utf-8", errors="replace").strip()
        if txt:
            return txt
    brief = Path(root) / "00_brief" / "brief.md"
    if brief.is_file():
        for line in brief.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip().lstrip("-* ").strip()
            if s.startswith("绿幕检测") and any(k in s for k in ("误判", "放行", "人工确认", "非绿幕")):
                return s
    return None


def write_override(root: str | Path, reason: str) -> Path:
    p = override_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"绿幕检测误判放行 — {time.strftime('%Y-%m-%d %H:%M:%S')}\n原因:{reason}\n",
                 encoding="utf-8")
    return p


def guidance_md(root: str | Path, flagged: list[dict]) -> Path:
    """写 01_materials/GREENSCREEN.md:给用户的处理指引(阻断时必产)。"""
    p = Path(root) / "01_materials" / "GREENSCREEN.md"
    lines = ["# ⛔ 检测到绿幕/蓝幕素材 —— 请先自行抠像并合成背景", "",
             "v0.14 起 CutFlow 不再做抠像与背景合成。以下素材仍含绿幕/蓝幕背景,"
             "必须由你**自行抠像并合成好背景**后,替换 `01_materials/` 里的原文件,再重新摄取:", "",
             "| 文件 | 类型 | 边框幕色占比 | 全帧幕色占比 |", "|---|---|---|---|"]
    for it in flagged:
        g = it.get("greenScreen") or {}
        kind = {"green": "绿幕", "blue": "蓝幕"}.get(g.get("kind"), "幕布")
        lines.append(f"| {it['file']} | {kind} | {g.get('borderGreen') or g.get('borderBlue')} | "
                     f"{g.get('greenRatio') or g.get('blueRatio')} |")
    lines += ["", "处理步骤:", "",
              "1. 用剪映/Pr/AE 等完成抠像,并合成好最终背景(导出为常规 mp4);",
              "2. 用处理后的文件替换 `01_materials/` 中的原素材(文件名保持一致或同步更新 brief);",
              "3. 重新运行 `python skills/cutflow/scripts/rs_ingest.py scan <工程根>`。", "",
              "如果这**不是**绿幕素材(误判),向 Agent 说明后执行:", "",
              "```powershell",
              "python skills/cutflow/scripts/rs_ingest.py green-ok <工程根> --reason \"误判原因\"",
              "```", "",
              "> 放行会写入 `00_brief/greenscreen-override.txt` 留痕;L0 自检据此不再阻断。", ""]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="绿幕/蓝幕检测(ADR-0031)")
    ap.add_argument("command", choices=["check", "allow"])
    ap.add_argument("target", help="check:<媒体文件>;allow:<工程根>")
    ap.add_argument("--reason", default="")
    ap.add_argument("--samples", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.command == "check":
        res = detect_media(a.target, samples=a.samples)
        msg = (f"{a.target}:未检出幕布" if not res.get("detected")
               else f"{a.target}:检出{res.get('kind')}幕(置信度 {res.get('confidence')})")
        return emit(True, "GREEN_CHECK", msg, res)
    if not a.reason.strip():
        return emit(False, "NEED_REASON", "green-ok 必须提供 --reason(用户对误判的说明)",
                    exit_code=2)
    p = write_override(a.target, a.reason.strip())
    return emit(True, "GREEN_OVERRIDE", f"已记录误判放行:{p}", {"path": str(p)})


if __name__ == "__main__":
    rs_common.ensure_utf8()
    sys.exit(main())
