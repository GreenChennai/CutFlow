# -*- coding:utf-8 -*-
"""T2.3 / H3 回归:zoompan 出场分支必须真实发生(旧判据 lt(in,n_frames) 恒真)。

缺陷:`_g_zoompan` 出场分支 `if(lt(in,{n_frames}), z0, …)` —— `in` 取值恰为
0..n_frames-1,条件恒真 → 恒返 z0,pull.out / zoompan.out.shrink /
out.scale.shrink 等全部出场缩放都是静默空操作(与 ADR-0055 冲突)。
修复:判据改用剩余帧数口径 `if(lt(in, n_frames-n_anim), …)`。

断言两层(任务书允许"表达式单元断言 + 小渲染冒烟"):
  ① 单元:对注册表 pull.out 生成的 z 表达式逐帧求值,首帧 vs 末帧缩放差 ≥5%
    (修复前恒等于 z0,差为 0);
  ② 渲染冒烟:同一表达式渲 3s 96x96 横渐变小片段,抽首末帧用"中列红通道
    斜率 ∝ 1/zoom"代理缩放,断言差 ≥5%。产物写 tempfile.gettempdir()。

运行:pytest tests/test_fx_out_animation.py -q
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_common  # noqa: E402
from rs_fx import registry as fxreg  # noqa: E402

W = H = 96
FPS = 30
DUR_S = 3.0
N_FRAMES = int(DUR_S * FPS)


def _ffmpeg() -> str:
    try:
        p = rs_common.ffmpeg_bin(rs_common.load_config())
        if Path(p).is_file() and p != "ffmpeg":
            return str(p)
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


FF = _ffmpeg()


# ---------------------------------------------------------------- ① 表达式单元断言

def _z_expr(fxid: str, ctx: fxreg.FxContext) -> str:
    """fxId → 注册表计划 → zoompan 的 z 表达式(走公开 build_clip_fx 入口)。"""
    plan, _notes = fxreg.build_clip_fx([("out", fxid, {})], ctx)
    assert plan.vf, f"{fxid} 未产出 vf"
    zoom = next((v for v in plan.vf if v.startswith("zoompan=z='")), None)
    assert zoom is not None, f"{fxid} 计划缺 zoompan:{plan.vf}"
    m = re.search(r"zoompan=z='(.*?)':x=", zoom)
    assert m, f"无法从 vf 提取 z 表达式:{zoom}"
    return m.group(1).replace(r"\,", ",")      # _xf 的 filtergraph 逗号转义还原


def _split_top(s: str) -> list[str]:
    """按**顶层**逗号拆分(括号深度为 0 的逗号才是分隔符)。"""
    parts, buf, depth = [], "", 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    return parts


def _eval_z(expr: str, in_frame: int, n_frames: int) -> float:
    """z 表达式求值(只支持本生成器产出的 if(lt(..))/min(..) 线性 ramp 形态)。"""
    def inner(e: str) -> float:
        e = e.strip()
        m = re.fullmatch(r"(if|min)\((.*)\)", e, re.S)
        if m:
            args = _split_top(m.group(2))
            if m.group(1) == "min":
                return min(inner(a) for a in args)
            assert len(args) == 3, f"if(...) 需要 3 参:{e}"
            cm = re.fullmatch(r"(lt|gt|lte|gte)\((.*)\)", args[0].strip())
            assert cm, f"if 条件不支持:{args[0]}"
            vals = [inner(a) for a in _split_top(cm.group(2))]
            op, x, y = cm.group(1), vals[0], vals[1]
            rel = {"lt": x < y, "gt": x > y, "lte": x <= y, "gte": x >= y}[op]
            return inner(args[1]) if rel else inner(args[2])
        # 其余 = 算术四则(可内嵌 min/max);`in` 是关键字,先改名再 eval
        e = re.sub(r"\bin\b", "_in", e)
        return float(eval(e, {"__builtins__": {}},                # noqa: S307
                         {"_in": in_frame, "n": n_frames, "min": min, "max": max}))
    return inner(expr)


@pytest.mark.parametrize("fxid,z0,z1", [
    ("pull.out", 1.12, 1.0),
    ("zoompan.out.shrink", 1.0, 0.94),
    ("out.scale.shrink", 1.0, 0.92),
])
def test_out_z_expression_animates(fxid, z0, z1):
    """出场 z 表达式首末帧缩放差 ≥5%(|z0−z1|/max);修复前恒为 0。"""
    ctx = fxreg.FxContext(cw=W, ch=H, fps=FPS, out_s=DUR_S, take_s=DUR_S)
    expr = _z_expr(fxid, ctx)
    z_first = _eval_z(expr, 0, N_FRAMES)
    z_last = _eval_z(expr, N_FRAMES - 1, N_FRAMES)
    assert abs(z_first - z_last) / max(z_first, z_last) >= 0.05, \
        f"{fxid} 出场动画仍未发生:z(0)={z_first:.4f} z(end)={z_last:.4f}(expr={expr})"
    # 方向判据:首帧锚 z0、末帧落在 z1 附近(ramp 完成度 ≥90%)
    assert abs(z_first - z0) < 1e-3, f"{fxid} 首帧未锚 z0:{z_first}"
    assert abs(z_last - z1) <= abs(z1 - z0) * 0.1 + 1e-3, \
        f"{fxid} 末帧未达 z1 附近:{z_last}"


# ---------------------------------------------------------------- ② 渲染冒烟

def _gradient_clip(path: Path) -> Path:
    """横渐变红通道(r=x)小片段(rawvideo 管道,零图像库依赖)。"""
    frame = bytes(bytearray(int(255 * (i + 0.5) / W) for j in range(H)
                            for i in range(W) for _ in range(3)))
    p = subprocess.run([FF, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                        "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                        "-frames:v", str(N_FRAMES), "-pix_fmt", "yuv420p",
                        "-c:v", "libx264", "-preset", "ultrafast", str(path)],
                       input=frame * N_FRAMES, capture_output=True, timeout=120)
    assert p.returncode == 0, p.stderr[-300:]
    return path


def _apply_vf(src: Path, vf: str, dst: Path) -> Path:
    p = subprocess.run([FF, "-v", "error", "-y", "-i", str(src), "-vf", vf,
                        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset",
                        "ultrafast", str(dst)], capture_output=True, timeout=120)
    assert p.returncode == 0, p.stderr[-400:]
    return dst


def _frame_rgb(path: Path, select: str) -> bytes:
    p = subprocess.run([FF, "-v", "error", "-i", str(path), "-vf", select,
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, timeout=60, check=True)
    assert len(p.stdout) == W * H * 3, f"抽帧尺寸异常:{len(p.stdout)}"
    return p.stdout


def _center_row_slope(frame: bytes) -> float:
    """中列行红通道对 x 的最小二乘斜率(输出斜率 ≈ 255/zoom)。"""
    row_j = H // 2
    xs = list(range(W // 5, W - W // 5))
    ys = [frame[(row_j * W + i) * 3] for i in xs]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / sxx


def test_pull_out_render_first_last_zoom_diff(tmp_path):
    """渲出段首末帧代理缩放差 ≥5%(修复前输出逐帧相同,差恒 0)。"""
    if not FF:
        pytest.skip("ffmpeg 不可用")
    ctx = fxreg.FxContext(cw=W, ch=H, fps=FPS, out_s=DUR_S, take_s=DUR_S)
    plan, _ = fxreg.build_clip_fx([("out", "pull.out", {})], ctx)
    vf = ",".join(plan.vf)
    src = _gradient_clip(tmp_path / "src.mp4")
    out = _apply_vf(src, vf, tmp_path / "out.mp4")
    first = _frame_rgb(out, "select=eq(n\\,0)")
    last = _frame_rgb(out, "select=eq(n\\,%d)" % (N_FRAMES - 1))
    s_first, s_last = _center_row_slope(first), _center_row_slope(last)
    # zoom 1.12 → 1.0,斜率比 ≈ 1/1.12 → 差 ≈ 10.7%;门限 5% 留 yuv 噪声余量
    diff = abs(s_last - s_first) / abs(s_last)
    assert diff >= 0.05, \
        f"pull.out 首末帧缩放差不足:slope_first={s_first:.2f} slope_last={s_last:.2f}" \
        f" diff={diff:.3%}(vf={vf})"
