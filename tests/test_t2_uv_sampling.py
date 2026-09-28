# -*- coding: utf-8 -*-
"""T2.2 / H2 回归:t2_glsl 统一头必须采样入参 `uv`(不再恒采 v_uv)。

缺陷:`getFromColor(vec2 uv) { return texture(from, v_uv); }` 把所有依赖 UV
变换的 gl-transition(Mosaic 分块 / Swap 透视 / LinearBlur 偏移采样…)静默
抹平成"恒等采样",能编译能出片、完全无报错。
修复:头改为 `texture(from, uv)` / `texture(to, uv)`。

断言策略(ADR-0055:像素级、与"已知正确采样"有可判别关系):
  · swap / linear.blur:CPU(numpy)参考实现逐像素对照 —— 渲染结果贴近正确
    采样、明显偏离"恒等采样"预测;
  · mosaic:横幅红渐变输入下,from 分支的采样坐标必须不再是恒等映射
    (恒等采样 ⇒ out_r == p.x 逐像素成立;分块旋转后必然偏离)。
另:全部收录 GLSL 逐条编译验证(不过编译者按 ADR-0055 移回「登记待实现」,
本仓当前 22 条全过)。

运行:pytest tests/test_t2_uv_sampling.py -q(无 ffmpeg/moderngl 时跳过)
产物全部写 tempfile.gettempdir(),不落仓库。
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_common  # noqa: E402
from rs_fx import t2_glsl  # noqa: E402

GLSL_DIR = REPO / "skills" / "cutflow" / "templates" / "effects" / "glsl"
W = H = 64
FPS = 30


def _ffmpeg() -> str:
    try:
        p = rs_common.ffmpeg_bin(rs_common.load_config())
        if Path(p).is_file() and p != "ffmpeg":
            return str(p)
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


FF = _ffmpeg()
_HAS_GL = t2_glsl.available()
pytestmark = pytest.mark.skipif(not (FF and _HAS_GL),
                                reason="ffmpeg 或 moderngl/GL 不可用(编译门禁在无 GL 环境仍覆盖)")


# ---------------------------------------------------------------- 输入帧构造

def _gradient_frame() -> bytes:
    """from 侧:横向红渐变(r = uv.x);马赛克/swap 判定的公共输入。"""
    return bytes(bytearray(
        int(255 * (i + 0.5) / W) if ch == 0 else 0
        for j in range(H) for i in range(W) for ch in range(3)))


def _stripes_frame() -> bytes:
    """from 侧(线性模糊用):8 列红白竖条纹(非线性格案,模糊必抹平)。"""
    row = bytearray()
    for i in range(W):
        on = ((i * 8 // W) % 2 == 0)
        row += bytes((255, 0, 0) if on else (0, 0, 0))
    return bytes(row) * H


def _encode_input(frame: bytes, path: Path) -> Path:
    """单帧 bytes → 1s H.420 MP4(rawvideo 管道,不经 PNG/图像库)。"""
    cmd = [FF, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-frames:v", str(FPS), "-pix_fmt", "yuv420p",
           "-c:v", "libx264", "-preset", "ultrafast", str(path)]
    p = subprocess.run(cmd, input=frame * FPS, capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr[-300:]
    return path


def _render_overlap(tmp: Path, glsl_name: str, from_bytes: bytes,
                    params: dict | None = None) -> "list[bytes]":
    """渲一条 2 帧重叠区(progress=0.5、1.0),解码回 rgb 帧列表。"""
    a = _encode_input(from_bytes, tmp / "a.mp4")
    b = _encode_input(bytes([0, 255, 0]) * (W * H), tmp / "b.mp4")   # to = 纯绿
    out = tmp / "ov.mp4"
    n = t2_glsl.render_overlap(a, b, fps=FPS, tdur_s=2 / FPS, out_mp4=out,
                               glsl_name=glsl_name, glsl_dir=GLSL_DIR,
                               ffmpeg_bin=FF, width=W, height=H, params=params)
    assert n == 2, f"重叠区帧数漂移:{n}"
    p = subprocess.run([FF, "-v", "error", "-i", str(out), "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-"],
                       capture_output=True, timeout=60, check=True)
    raw = p.stdout
    assert len(raw) % (W * H * 3) == 0
    return [raw[k * W * H * 3:(k + 1) * W * H * 3] for k in range(len(raw) // (W * H * 3))]


def _frame_array(frame: bytes) -> "list[list[tuple[float, float, float]]]":
    """rgb 帧字节 → 0..1 归一化像素矩阵(与 CPU 参考同口径)。"""
    return [[tuple(c / 255.0 for c in frame[(j * W + i) * 3:(j * W + i) * 3 + 3])
             for i in range(W)] for j in range(H)]


def _median(vals):
    s = sorted(vals)
    return s[len(s) // 2]


# ---------------------------------------------------------------- CPU 参考采样

def _grid():
    return [[((i + 0.5) / W, (j + 0.5) / H) for i in range(W)] for j in range(H)]


def _pred_swap(pxs):
    """swap 的 CPU 参考实现(与 GLSL 同式;from=红渐变,to=纯绿,bg=黑)。"""
    progress, depth, persp_c = 0.5, 3.0, 0.2
    refl = 0.4
    out = []
    for j, row in enumerate(pxs):
        orow = []
        for i, _ in enumerate(row):
            px, py = (i + 0.5) / W, (j + 0.5) / H
            def project(p):
                return (p[0] * 1.0, p[1] * -1.2 + -0.02 + 0.0)

            def in_bounds(p):
                return 0.0 < p[0] < 1.0 and 0.0 < p[1] < 1.0
            size = 1.0 + (depth - 1.0) * progress
            persp = persp_c * progress
            pfr = ((px + 0.0) * (size / (1.0 - persp_c * progress)),
                   (py - 0.5) * (size / (1.0 - size * persp * px)) + 0.5)
            size2 = 1.0 + (depth - 1.0) * (1.0 - progress)
            persp2 = persp_c * (1.0 - progress)
            pto = ((px - 1.0) * (size2 / (1.0 - persp_c * (1.0 - progress))) + 1.0,
                   (py - 0.5) * (size2 / (1.0 - size2 * persp2 * (0.5 - px))) + 0.5)
            # progress < 0.5 为假;先 pto 后 pfr
            if in_bounds(pto):
                orow.append((0.0, 1.0, 0.0))
            elif in_bounds(pfr):
                orow.append((pfr[0], 0.0, 0.0))
            else:
                c = [0.0, 0.0, 0.0]
                q = project(pfr)
                if in_bounds(q):
                    w_ = refl * (1.0 - 0.0 * q[1])
                    c[0] += w_ * q[0]
                q2 = project(pto)
                if in_bounds(q2):
                    w2 = refl * (1.0 - 0.0 * q2[1])
                    c[1] += w2 * 1.0
                orow.append(tuple(c))
        out.append(orow)
    return out


def _pred_identity(_pxs):
    """「恒等采样」预测(H2 缺陷行为):分支坐标照算、采样点恒为 p 本身。

    from 分支(out_r>0 且为红)→ 采 p 本身 ⇒ out_r = p.x;to 分支(纯绿)与
    黑背景与正确实现一致(不依赖采样点)。"""
    ref = _pred_swap(_grid())
    out = []
    for j, row in enumerate(ref):
        orow = []
        for i, c in enumerate(row):
            if c[0] > c[1] and c[1] == 0.0:      # from 分支(红通道 = pfr.x)
                orow.append(((i + 0.5) / W, 0.0, 0.0))
            else:                                # to 分支 / 背景:采样点无关
                orow.append(c)
        out.append(orow)
    return out


def _med_abs(a, b) -> float:
    """两帧逐通道绝对差的中位数(0..1 口径)。"""
    diffs = [abs(pa - pb) for ra, rb in zip(a, b)
             for ca, cb in zip(ra, rb) for pa, pb in zip(ca, cb)]
    return _median(diffs)


# ---------------------------------------------------------------- 用例

def test_all_glsl_compile():
    """收录 GLSL 逐条真编译(T2.2 前置:编译不过者应移回「登记待实现」)。"""
    names = sorted(p.stem for p in GLSL_DIR.glob("*.glsl"))
    assert len(names) >= 20, "GLSL 收录集缩水"
    failed = {n: msg for n in names
              for ok, msg in [t2_glsl.validate_shader(n, GLSL_DIR)] if not ok}
    assert not failed, f"编译不过(应按 ADR-0055 降级登记):{failed}"


def test_swap_matches_reference_not_identity(tmp_path):
    """swap:渲出帧贴近 CPU 正确采样,明显偏离恒等采样(修复前恒等即渲出)。

    只在两种预测有分歧的像素(= from 分支采样区)上比较:绿/黑背景与采样点
    无关,计入会把中位数稀释成 0。"""
    frames = _render_overlap(tmp_path, "swap", _gradient_frame())
    got = _frame_array(frames[0])            # progress=0.5
    correct = _pred_swap(_grid())
    buggy = _pred_identity(_grid())
    disc = [(g, c, b) for gr, cr, br in zip(got, correct, buggy)
            for g, c, b in zip(gr, cr, br)
            if max(abs(x - y) for x, y in zip(c, b)) > 0.02]
    assert len(disc) >= 0.05 * W * H, f"分歧像素过少({len(disc)}),判据不可判"
    d_ok = _median([max(abs(x - y) for x, y in zip(g, c)) for g, c, _ in disc])
    d_bug = _median([max(abs(x - y) for x, y in zip(g, b)) for g, _, b in disc])
    assert d_ok < 0.12, f"swap 偏离正确采样:median={d_ok:.3f}"
    assert d_bug > 0.05, \
        f"swap 更贴近恒等采样(bug 行为):correct={d_ok:.3f} identity={d_bug:.3f}"


def test_mosaic_breaks_identity_sampling(tmp_path):
    """mosaic:from 分支的输出红通道不再等于 p.x(分块旋转采样生效)。"""
    frames = _render_overlap(tmp_path, "Mosaic", _gradient_frame())
    got = _frame_array(frames[0])
    # from 分支掩码:偏红(g 明显小于 r)且避开左缘(yuv 噪声区)
    mask = [(j, i) for j in range(H) for i in range(4, W)
            if got[j][i][0] > 0.25 and got[j][i][1] < got[j][i][0] * 0.5]
    assert len(mask) >= 0.05 * W * H, f"from 分支掩码过小({len(mask)}px),判据不可判"
    devs = [abs(got[j][i][0] / 255.0 - (i + 0.5) / W) for j, i in mask]
    med = _median(devs)
    assert med >= 0.03, \
        f"mosaic from 分支仍呈恒等采样(中位偏差 {med:.4f} ≈ 0,疑似忽略 uv)"


def test_linearblur_matches_reference_not_crossfade(tmp_path):
    """linear.blur(intensity=0.6):36 点偏移均值采样生效;恒等采样(=交叉淡化)
    会保留竖条纹高对比,正确采样把条纹几乎抹平。"""
    frames = _render_overlap(tmp_path, "LinearBlur", _stripes_frame(),
                             params={"intensity": 0.6})
    got = _frame_array(frames[0])
    # CPU 参考:36 点网格偏移 + REPEAT 环绕取均 → mix(from_mean, green, 0.5)
    disp = 0.6 * (0.5 - abs(0.5 - 0.5))
    src = [[(1.0, 0.0, 0.0) if ((i * 8 // W) % 2 == 0) else (0.0, 0.0, 0.0)
            for i in range(W)] for _ in range(H)]
    acc = [[0.0, 0.0] for _ in range(W * H)]
    for xi in range(6):
        for yi in range(6):
            dx = int(round(disp * (xi / 6 - 0.5) * W))
            dy = int(round(disp * (yi / 6 - 0.5) * H))
            for j in range(H):
                for i in range(W):
                    r, _, _ = src[(j + dy) % H][(i + dx) % W]
                    acc[j * W + i][0] += r / 36.0
                    acc[j * W + i][1] += 0.0
    ref = [[(acc[j * W + i][0] * 0.5, 0.5, 0.0) for i in range(W)] for j in range(H)]
    # 恒等采样预测:out_r = 0.5*stripe(p) ∈ {0,0.5}(条纹保留)
    stripe = [[(0.5 * src[j][i][0], 0.5, 0.0) for i in range(W)] for j in range(H)]
    got_flat = [px for row in got for px in row]
    ref_flat = [px for row in ref for px in row]
    st_flat = [px for row in stripe for px in row]
    d_ok = _median([abs(a[0] - b[0]) for a, b in zip(got_flat, ref_flat)])
    d_bug = _median([abs(a[0] - b[0]) for a, b in zip(got_flat, st_flat)])
    assert d_ok < 0.10, f"linear.blur 偏离偏移采样参考:median={d_ok:.3f}"
    assert d_bug > 0.12, \
        f"linear.blur 贴近恒等采样(bug 行为):correct={d_ok:.3f} identity={d_bug:.3f}"
