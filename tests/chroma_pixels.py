r"""Chroma bench 像素工具:PPM/PGM 读写(无 PIL 依赖) + 指标计算 + 场景合成。

供 v0.13 绿幕算法基准测试与 rs_chroma_bench.py 复用。
PPM(P6)/PGM(P5) 是 ffmpeg 原生支持的裸格式,写帧/读帧零依赖。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


# ---------------------------------------------------------------- PPM/PGM IO

def write_ppm(path: Path, arr: np.ndarray) -> None:
    """arr: (h, w, 3) uint8 → PPM P6。"""
    h, w, _ = arr.shape
    with open(path, "wb") as f:
        f.write(f"P6\n{w} {h}\n255\n".encode("ascii"))
        f.write(arr.astype(np.uint8).tobytes())


def write_pgm(path: Path, arr: np.ndarray) -> None:
    h, w = arr.shape
    with open(path, "wb") as f:
        f.write(f"P5\n{w} {h}\n255\n".encode("ascii"))
        f.write(arr.astype(np.uint8).tobytes())


def read_pnm(path: Path) -> np.ndarray:
    data = Path(path).read_bytes()
    magic = data[:2]
    # 头部:magic + 单空白 + 两个 ASCII 尺寸 token + maxval + 单空白
    parts = data.split(b"\n", 3)
    magic, dims, maxval = parts[0], parts[1], parts[2]
    assert magic in (b"P5", b"P6"), f"非 PNM P5/P6:{magic}"
    w, h = (int(x) for x in dims.split())
    hdr_end = data.index(maxval) + len(maxval)
    # maxval 行后可能紧跟 \n 或 \r\n
    while data[hdr_end:hdr_end + 1] in (b"\n", b"\r"):
        hdr_end += 1
    ch = 3 if magic == b"P6" else 1
    px = np.frombuffer(data[hdr_end:hdr_end + w * h * ch], dtype=np.uint8)
    return px.reshape(h, w, ch) if ch == 3 else px.reshape(h, w)


# ---------------------------------------------------------------- 场景合成

def make_screen_field(w: int, h: int, base=(42, 168, 30), vignette=0.55,
                      hot_spot=(0.32, 0.30), seed: int = 7) -> np.ndarray:
    """光照不均绿幕底:vignette(暗角) + 顶部热斑,模拟实拍灯位问题。

    base: 屏幕基准色;vignette: 角落相对亮度下限(0.55 = 角落只亮 55%)。
    """
    rng = np.random.RandomState(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w / 2.0, h / 2.0
    r = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2) / np.sqrt(2)
    gain = 1.0 - vignette * (r ** 1.6)
    hx, hy, hw = hot_spot[0] * w, hot_spot[1] * h, 0.16 * w
    gain += 0.35 * np.exp(-(((xx - hx) ** 2 + (yy - hy) ** 2) / (2 * hw ** 2)))
    noise = rng.normal(0, 1.6, size=(h, w, 1)).astype(np.float32)
    field = np.zeros((h, w, 3), np.float32)
    for i, c in enumerate(base):
        field[..., i] = c * gain + noise[..., 0]
    return np.clip(field, 0, 255)


def make_person(w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    """合成"人物"前景:躯干椭圆 + 头 + 细发丝 + 半透明纱巾。

    返回 (rgb, alpha):rgb 是纯前景色(不含背景),alpha 是 0-1 连续真值。
    """
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    rgb = np.zeros((h, w, 3), np.float32)
    alpha = np.zeros((h, w), np.float32)

    def ellipse(cx, cy, rx, ry, val):
        return np.clip(1.0 - ((xx - cx) / rx) ** 2 - ((yy - cy) / ry) ** 2, 0, 1) * val

    body = ellipse(w * 0.5, h * 0.78, w * 0.24, h * 0.34, 1.0)
    head = ellipse(w * 0.5, h * 0.40, w * 0.085, h * 0.115, 1.0)
    rgb[body > 0] = np.array([38, 52, 120], np.float32)
    rgb[head > 0] = np.array([196, 152, 128], np.float32)
    alpha = np.maximum(body, head)
    hair = ellipse(w * 0.5, h * 0.30, w * 0.095, h * 0.075, 1.0) * (yy < h * 0.315)
    rgb[hair > 0] = np.array([28, 22, 20], np.float32)
    alpha = np.maximum(alpha, hair)
    rng = np.random.RandomState(11)
    for k in range(12):
        ang = rng.uniform(-1.2, 1.2)
        x0, y0 = w * 0.5 + np.sin(ang) * w * 0.08, h * 0.30
        length = rng.uniform(h * 0.06, h * 0.16)
        for t in np.linspace(0, 1, 220):
            px = x0 + np.sin(ang + t * 0.8) * t * length * 0.9
            py = y0 + t * length
            xi, yi = int(px), int(py + t * 1.5)
            if 0 <= yi < h and 0 <= xi < w:
                a = (1.0 - t) * 0.9
                alpha[yi, xi] = max(alpha[yi, xi], a)
                rgb[yi, xi] = np.array([30, 24, 22], np.float32)
                if xi + 1 < w:
                    alpha[yi, xi + 1] = max(alpha[yi, xi + 1], a * 0.7)
                    rgb[yi, xi + 1] = np.array([36, 30, 28], np.float32)
    veil = ellipse(w * 0.36, h * 0.62, w * 0.10, h * 0.13, 0.35)
    veil_a = veil > 0
    rgb[veil_a] = np.clip(rgb[veil_a] * 0.5 + np.array([220, 210, 200], np.float32) * 0.5, 0, 255)
    alpha = np.maximum(alpha, veil)
    return rgb, alpha


def compose_person_on_screen(w: int, h: int, screen: np.ndarray,
                             return_person: bool = False):
    """人物(含边缘抗锯齿)按真值 alpha 合到绿幕上,附投影与绿色溢光。

    返回 (合成帧 uint8 RGB, alpha 真值 0-1) 或加 (纯前景 RGB 真值)。
    """
    rgb_f, alpha_f = make_person(w, h)
    out = screen.copy()
    a3 = alpha_f[..., None]
    # 溢光:边缘半透明像素受屏幕绿污染(G 通道抬升)—— 模拟镜头 spill
    spill_zone = (alpha_f > 0) & (alpha_f < 0.9)
    spill = np.zeros((h, w), np.float32)
    spill[spill_zone] = 26.0
    person = rgb_f + np.stack([np.zeros_like(spill), spill, np.zeros_like(spill)], axis=-1)
    out = out * (1 - a3) + person * a3
    # 投影:人物右下淡阴影(压暗屏幕 30%)—— 暗场难例
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    sh = np.clip(1.0 - ((xx - w * 0.60) / (w * 0.20)) ** 2 - ((yy - h * 0.92) / (h * 0.05)) ** 2, 0, 1) * 0.30
    for i in range(3):
        out[..., i] *= (1 - sh)
    comp = np.clip(out, 0, 255).astype(np.uint8)
    if return_person:
        return comp, alpha_f, np.clip(rgb_f, 0, 255).astype(np.uint8)
    return comp, alpha_f


# ---------------------------------------------------------------- 指标

def metrics(keyed_rgba: np.ndarray, alpha_true: np.ndarray, screen: np.ndarray) -> dict:
    """对 keying 结果计算残绿/暗场/黑边指标。

    keyed_rgba: (h,w,4) uint8(抠像后含 alpha 的前景帧);
    alpha_true: 0-1 真值;screen: 屏幕底(用于亮度参照)。

    指标口径:
      bg_visible_pct  背景区(真alpha<0.05)中"未被抠掉"(out alpha>0.10)的占比 —— 总残留
      residue_pct     背景区残留中偏绿的(残绿)
      darkhole_pct    背景区残留中亮度 < 屏幕均值 0.5 的(暗场/黑块)
      edge_dark_bias  边缘带(0.05<真alpha<0.97, out alpha>0.3)中合成色最高通道
                      与实体区同指标的中位差 —— 黑边(负值大 = 边缘被压暗)
      alpha_mae       全图 alpha 与真值 MAE(0-255 尺度)
      fg_leak_pct     主体内部(真alpha>0.95)被抠透(out alpha<0.5)占比
    """
    a_out = keyed_rgba[..., 3].astype(np.float32) / 255.0
    rgb_out = keyed_rgba[..., :3].astype(np.float32)
    a_true = alpha_true
    bg = a_true < 0.05
    visible = a_out > 0.10
    g, r, b = rgb_out[..., 1], rgb_out[..., 0], rgb_out[..., 2]
    greenish = g > np.maximum(r, b) + 12
    screen_luma = float(screen.mean())
    dark = rgb_out.max(axis=-1) < screen_luma * 0.5
    residue = bg & visible & greenish
    darkhole = bg & visible & dark
    band = (a_true > 0.05) & (a_true < 0.97)
    solid_mask = a_true > 0.95
    solid = rgb_out[solid_mask].max(axis=-1) if solid_mask.any() else np.array([128.0])
    band_sel = band & (a_out > 0.3)
    edge_bias = (float(np.median(rgb_out[band_sel].max(axis=-1))) - float(np.median(solid))
                 if band_sel.any() else 0.0)
    fg_leak = solid_mask & (a_out < 0.5)
    return {
        "bg_visible_pct": round(float((bg & visible).sum()) / max(1, int(bg.sum())) * 100, 4),
        "residue_pct": round(float(residue.sum()) / max(1, int(bg.sum())) * 100, 4),
        "darkhole_pct": round(float(darkhole.sum()) / max(1, int(bg.sum())) * 100, 4),
        "edge_dark_bias": round(edge_bias, 1),
        "alpha_mae": round(float(np.abs(a_out - a_true).mean()) * 255, 2),
        "fg_leak_pct": round(float(fg_leak.sum()) / max(1, int(solid_mask.sum())) * 100, 4),
    }
