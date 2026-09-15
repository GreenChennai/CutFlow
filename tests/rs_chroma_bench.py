r"""v0.13 绿幕基准 v3:合成难例帧 → 旧/新抠像链 → 像素指标对比。

新链 v3 两处核心创新(相对 v0.10-0.12 旧链):
  1) alpha:RGB 比值键控 —— ratio = min(G/max(R,1), G/max(B,1)),
     亮度完全无关(暗角/热斑/投影下屏幕 ratio 恒 ≈4.0,人物恒 <1.4)
  2) despill:alpha 感知 un-premultiply —— fg=(pix-(1-a)*screen)/a,
     边缘像素恢复真实人物色(旧链 despill 把混合像素的 G 钳到 max(R,B) → 黑边根因)
场景:even / vignette(0.38) / shadow(0.45) × full/420(4:2:0)
用法: python rs_chroma_bench.py [--out .cluster/bench]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
import chroma_pixels as cp  # noqa: E402

FFMPEG = r"E:\Tools\ffmpeg\bin\ffmpeg.exe"
SCREEN_RGB = (42, 168, 30)          # 与 make_screen_field 的 base 一致


def run_ff(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([FFMPEG, *cmd], capture_output=True, text=True, encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- 旧链(v0.10-0.12)

def key_old_chain(similarity: float = 0.15, blend: float = 0.12, despill: bool = True,
                  shrink: float = 1.2, t: float = 0.55, feather: float = 0.6) -> str:
    core = f"clip((alpha(X,Y)-{t * 255:.1f})/{(1 - t) * 255:.1f}*255,0,255)"
    chain = [f"colorkey=0x2AA81E:{similarity}:{blend}"]
    if despill:
        chain.append("despill=type=green")
    chain.append(f"format=yuva444p,gblur=sigma={shrink:.2f}:planes=8,"
                 f"geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='{core}'")
    if feather > 0:
        chain.append(f"gblur=sigma={feather:.2f}:planes=8")
    chain.append("format=rgba")
    return ",".join(chain)


# ---------------------------------------------------------------- 新链(v3)

def key_new_chain(screen_rgb=SCREEN_RGB, t_cut: float = 2.2, feather: float = 1.2,
                  despill: float = 0.85, a_floor: float = 0.35,
                  dist_cut: float = 3.0) -> str:
    """v3.9 定稿:紧阈值比值 matte(亮度无关) → matte blur → YUV 域 un-premultiply
    + 分离门控溢色中和。

    geq#1(yuva444p): a = 255*clip(t_cut - min(G/max(R,1),G/max(B,1)),0,1)(二值 matte,
      亮度无关);RGB 直通。半透明由 matte blur 空间平均给出。
    geq#2(YUV 域,limited-range 对混色线性):
      ae = max(a/255, a_floor)
      Y'/Cb'/Cr' = ((平面值-中性)-(1-ae)*(屏幕值-中性))/ae + 中性   ← 精确扣除屏幕混色
      溢色中和(分方向门控):绿染 = Cb 偏低(Cb<118)→Cb 向 128 收;
      Cr 只在明显偏绿(Cr<112)时向 128 收(避免伤肤色/紫边)。
      R/B 通道(Y 域)永不触碰 → 无黑边源。
    """
    r, g, b = screen_rgb
    ys = 16 + 219 * (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
    rn, gn, bn = (x / 255.0 for x in screen_rgb)
    cbs = 128 + 224 * (-0.168736 * rn - 0.331264 * gn + 0.5 * bn)
    crs = 128 + 224 * (0.5 * rn - 0.418688 * gn - 0.081312 * bn)
    pre = ("st(0,1.16438*(lum(X,Y)-16));"
           "st(1,ld(0)+1.59603*(cr(X,Y)-128));"
           "st(2,ld(0)-0.39176*(cb(X,Y)-128)-0.81297*(cr(X,Y)-128));"
           "st(3,ld(0)+2.01723*(cb(X,Y)-128))")
    # 主导通道方向按键色自动选:绿幕看 G 比值,蓝幕看 B 比值
    if b >= g:
        ratio = "ld(3)/max(ld(1),1)"
    else:
        ratio = "ld(2)/max(ld(3),1)"
    # 第二判据:键色→中性灰混合线的垂直距离(dline)。背景任何混色(含 4:2:0
    # 漂移)都在连线上(dline≈0-2),绿色前景(绿衣≈11)明显偏离 → 干净分界。
    _rn, _gn, _bn = (x / 255.0 for x in screen_rgb)
    _kcb = 128 + 224 * (-0.168736 * _rn - 0.331264 * _gn + 0.5 * _bn)
    _kcr = 128 + 224 * (0.5 * _rn - 0.418688 * _gn - 0.081312 * _bn)
    _vx, _vy = 128 - _kcb, 128 - _kcr
    _vlen = (_vx * _vx + _vy * _vy) ** 0.5
    cdist = f"abs({_vx:.0f}*(cr(X,Y)-{_kcr:.0f})-{_vy:.0f}*(cb(X,Y)-{_kcb:.0f}))/{_vlen:.1f}"
    # a *= clip((distCut-距离)/distCut):键色附近=0 不影响;远离键色的绿色前景→乘 1 保留
    # 前景 = (ratio<tCut) 或 (ratio 歧义带内 且 色度远离键色,如绿衣);
    # dist 项用 ratio 门控,防止 4:2:0 色度漂移的背景像素被误救
    a1 = (f"{pre};max(255*clip({t_cut:.2f}-{ratio},0,1),"
          f"255*clip(({cdist}-{dist_cut:.1f})*0.5,0,1)*clip(({ratio}-{t_cut:.2f})*3,0,1))")
    pre2 = f"st(9,max(alpha(X,Y)/255,{a_floor:.2f}));"
    y2 = f"{pre2}clip((lum(X,Y)-16-(1-ld(9))*{ys - 16:.1f})/ld(9)+16,16,235)"
    cb2 = (f"{pre2}"
           f"st(8,clip((cb(X,Y)-128-(1-ld(9))*{cbs - 128:.1f})/ld(9)+128,16,240));"
           f"st(7,clip((cr(X,Y)-128-(1-ld(9))*{crs - 128:.1f})/ld(9)+128,16,240));"
           f"ld(8)+(128-ld(8))*{despill:.2f}*clip((122-ld(8))/25,0,1)")
    cr2 = (f"{pre2}"
           f"st(8,clip((cb(X,Y)-128-(1-ld(9))*{cbs - 128:.1f})/ld(9)+128,16,240));"
           f"st(7,clip((cr(X,Y)-128-(1-ld(9))*{crs - 128:.1f})/ld(9)+128,16,240));"
           f"ld(7)+(128-ld(7))*{despill:.2f}*clip((115-ld(7))/25,0,1)")
    chain = [
        "format=yuva444p",
        f"geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':a='{a1}'",
        f"gblur=sigma={feather:.2f}:planes=8",
        f"geq=lum='{y2}':cb='{cb2}':cr='{cr2}':a='alpha(X,Y)'",
        "format=rgba",
    ]
    return ",".join(chain)

def key_frame(frame_ppm: Path, vf_chain: str, out_pam: Path, threads: int = 8) -> float:
    cmd = ["-v", "error", "-filter_threads", str(threads), "-y", "-i", str(frame_ppm)]
    t0 = time.perf_counter()
    p = run_ff([*cmd, "-vf", vf_chain, "-frames:v", "1", str(out_pam)])
    dt = time.perf_counter() - t0
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg fail: {p.stderr[-900:]}")
    return dt


def yuv420_roundtrip(src_ppm: Path, dst_ppm: Path) -> None:
    p = run_ff(["-v", "error", "-y", "-i", str(src_ppm), "-vf", "format=yuv420p",
                "-frames:v", "1", str(dst_ppm)])
    if p.returncode != 0:
        raise RuntimeError(f"420 roundtrip fail: {p.stderr[-400:]}")


def read_pam_rgba(path: Path) -> np.ndarray:
    """读 PAM。TUPLTYPE=RGB_ALPHA( packed 交错 RGBA)直接读;
    YUV 系(兼容旧产物)按 limited-range 转 full RGB。"""
    data = Path(path).read_bytes()
    assert data.startswith(b"P7"), f"非 PAM:{data[:8]}"
    hdr, idx = {}, 2
    while True:
        nl = data.index(b"\n", idx)
        line = data[idx:nl].decode("ascii", "replace").strip()
        idx = nl + 1
        if line == "ENDHDR":
            break
        kk, _, v = line.partition(" ")
        hdr[kk] = v
    w, h, depth = int(hdr["WIDTH"]), int(hdr["HEIGHT"]), int(hdr["DEPTH"])
    px = np.frombuffer(data[idx:idx + w * h * depth], dtype=np.uint8).reshape(h, w, depth)
    tup = (hdr.get("TUPLTYPE") or "").upper()
    if "RGB_ALPHA" in tup or (depth == 4 and "YUV" not in tup):
        return px.copy()          # packed R,G,B,A
    y = px[..., 0].astype(np.float32)
    u = px[..., 1].astype(np.float32) - 128.0
    v = px[..., 2].astype(np.float32) - 128.0
    r = y + 1.402 * v
    g = y - 0.344136 * u - 0.714136 * v
    b = y + 1.772 * u
    rgba = np.stack([r, g, b, px[..., 3].astype(np.float32)], axis=-1)
    return np.clip(rgba, 0, 255).astype(np.uint8)


def composite_on(rgba: np.ndarray, bg=(120, 120, 120)) -> np.ndarray:
    a = rgba[..., 3:4].astype(np.float32) / 255.0
    out = np.zeros_like(rgba[..., :3].astype(np.float32))
    for i in range(3):
        out[..., i] = bg[i] * (1 - a[..., 0]) + rgba[..., :3][..., i] * a[..., 0]
    return np.clip(out, 0, 255).astype(np.uint8)


def metrics2(keyed_rgba: np.ndarray, alpha_true: np.ndarray, person_rgb: np.ndarray,
             screen: np.ndarray) -> dict:
    """公平口径:新旧输出都合成到中灰底(虚拟背景),与"完美抠像合成"逐像素比。

    edge_mean_err / edge_p95_err: 边缘带(0.05<真alpha<0.97)合成结果与真值合成的
      绝对差(0-255)均值与 p95 —— 用户可见的边缘瑕疵总量(黑边+绿边+亮边一并计入)。
    fringe_pct: 边缘带中比真值暗 25+ 的像素占比(黑边)。
    spill_pct: 边缘带中 G 比 max(R,B) 高 15+ 的像素占比(绿边/溢色)。
    """
    a_out = keyed_rgba[..., 3].astype(np.float32) / 255.0
    rgb_out = keyed_rgba[..., :3].astype(np.float32)
    a_t = alpha_true
    bg_gray = 127.0
    comp_out = rgb_out * a_out[..., None] + bg_gray * (1 - a_out[..., None])
    comp_true = person_rgb.astype(np.float32) * a_t[..., None] + bg_gray * (1 - a_t[..., None])
    luma = lambda im: 0.299 * im[..., 0] + 0.587 * im[..., 1] + 0.114 * im[..., 2]
    band = (a_t > 0.05) & (a_t < 0.97)
    vis_band = band & (a_out > 0.15)
    err = np.abs(comp_out - comp_true).max(axis=-1)
    edge_mean = float(err[vis_band].mean()) if vis_band.any() else 0.0
    edge_p95 = float(np.percentile(err[vis_band], 95)) if vis_band.any() else 0.0
    fringe = vis_band & (luma(comp_out) < luma(comp_true) - 25.0)
    spill = vis_band & (comp_out[..., 1] > comp_out[..., 0] + 15.0) & (comp_out[..., 1] > comp_out[..., 2] + 15.0)
    bg = a_t < 0.05
    visible = a_out > 0.10
    g, r, b = rgb_out[..., 1], rgb_out[..., 0], rgb_out[..., 2]
    greenish = g > np.maximum(r, b) + 12
    screen_luma = float(screen.mean())
    dark = rgb_out.max(axis=-1) < screen_luma * 0.5
    solid = a_t > 0.95
    semi = (a_t > 0.10) & (a_t < 0.70)
    eaten = semi & (a_out < 0.05)          # 边缘被吃掉(透明化,旧链 colorkey 球外直接丢)
    hard_rim = semi & (a_out > 0.95)       # 边缘被硬化(旧链把混合像素当实心 → 深色硬边)
    return {
        "eaten_pct": round(float(eaten.sum()) / max(1, int(semi.sum())) * 100, 2),
        "hard_rim_pct": round(float(hard_rim.sum()) / max(1, int(semi.sum())) * 100, 2),
        "bg_visible_pct": round(float((bg & visible).sum()) / max(1, int(bg.sum())) * 100, 4),
        "residue_pct": round(float((bg & visible & greenish).sum()) / max(1, int(bg.sum())) * 100, 4),
        "darkhole_pct": round(float((bg & visible & dark).sum()) / max(1, int(bg.sum())) * 100, 4),
        "edge_mean_err": round(edge_mean, 1),
        "edge_p95_err": round(edge_p95, 1),
        "fringe_pct": round(float(fringe.sum()) / max(1, int(vis_band.sum())) * 100, 3),
        "spill_pct": round(float(spill.sum()) / max(1, int(vis_band.sum())) * 100, 3),
        "alpha_mae": round(float(np.abs(a_out - a_t).mean()) * 255, 2),
        "fg_leak_pct": round(float((solid & (a_out < 0.5)).sum()) / max(1, int(solid.sum())) * 100, 4),
    }

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=720)
    ap.add_argument("--out", default=".cluster/bench")
    a = ap.parse_args()
    out_dir = REPO / a.out
    out_dir.mkdir(parents=True, exist_ok=True)
    W, H = a.w, a.h

    scenes = {
        "even": dict(vignette=0.0),
        "vignette": dict(vignette=0.45),
        "shadow": dict(vignette=0.20, shadow=0.60),
    }
    results: dict = {}
    timings = {}
    for name, kw in scenes.items():
        screen = cp.make_screen_field(W, H, vignette=kw.get("vignette", 0.0), seed=7)
        frame, alpha_true, person_rgb = cp.compose_person_on_screen(W, H, screen, return_person=True)
        shadow = kw.get("shadow", 0.0)
        if shadow > 0:
            yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
            sh = np.clip(1.0 - ((xx - W * 0.62) / (W * 0.22)) ** 2 - ((yy - H * 0.90) / (H * 0.06)) ** 2, 0, 1) * shadow
            frame = np.clip(frame.astype(np.float32) * (1 - sh)[..., None], 0, 255).astype(np.uint8)
        frame_ppm = out_dir / f"{name}_input.ppm"
        cp.write_ppm(frame_ppm, frame)
        if name == "even":
            cp.write_pgm(out_dir / "alpha_true.pgm", (alpha_true * 255).astype(np.uint8))
        p420 = out_dir / f"{name}_420.ppm"
        yuv420_roundtrip(frame_ppm, p420)

        for variant, src in (("full", frame_ppm), ("420", p420)):
            tag = name if variant == "full" else f"{name}+420"
            old_pam, new_pam = out_dir / f"{tag}_old.pam", out_dir / f"{tag}_new.pam"
            to = key_frame(src, key_old_chain(), old_pam)
            tn = key_frame(src, key_new_chain(), new_pam)
            timings[f"{tag}_old_s"] = round(to, 3)
            timings[f"{tag}_new_s"] = round(tn, 3)
            old_rgba, new_rgba = read_pam_rgba(old_pam), read_pam_rgba(new_pam)
            results.setdefault("old", {})[tag] = metrics2(old_rgba, alpha_true, person_rgb, screen)
            results.setdefault("new", {})[tag] = metrics2(new_rgba, alpha_true, person_rgb, screen)
            if name in ("even", "shadow"):
                cp.write_ppm(out_dir / f"{tag}_old_comp.ppm", composite_on(old_rgba))
                cp.write_ppm(out_dir / f"{tag}_new_comp.ppm", composite_on(new_rgba))
                cp.write_pgm(out_dir / f"{tag}_new_alpha.pgm", new_rgba[..., 3])
                cp.write_pgm(out_dir / f"{tag}_old_alpha.pgm", old_rgba[..., 3])

    print(json.dumps({"metrics": results, "timings_s": timings}, ensure_ascii=False, indent=1))
    (out_dir / "compare.json").write_text(
        json.dumps({"metrics": results, "timings": timings}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def grid_search() -> int:
    """参数网格:t_keep/t_cut/feather/despill 组合,3 场景 420 变体打分。"""
    out_dir = REPO / ".cluster" / "bench"
    out_dir.mkdir(parents=True, exist_ok=True)
    W, H = 640, 360
    scenes = {"even": dict(vignette=0.0, shadow=0.0),
              "vignette": dict(vignette=0.45, shadow=0.0),
              "shadow": dict(vignette=0.20, shadow=0.60)}
    frames = {}
    for name, kw in scenes.items():
        screen = cp.make_screen_field(W, H, vignette=kw["vignette"], seed=7)
        frame, alpha_true, person_rgb = cp.compose_person_on_screen(W, H, screen, return_person=True)
        if kw["shadow"] > 0:
            yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
            sh = np.clip(1.0 - ((xx - W * 0.62) / (W * 0.22)) ** 2 - ((yy - H * 0.90) / (H * 0.06)) ** 2, 0, 1) * kw["shadow"]
            frame = np.clip(frame.astype(np.float32) * (1 - sh)[..., None], 0, 255).astype(np.uint8)
        p420 = out_dir / f"g_{name}_420.ppm"
        cp.write_ppm(p420, frame)
        p420_in = out_dir / f"g_{name}_in.ppm"
        yuv420_roundtrip(p420, p420_in)
        frames[name] = (p420_in, alpha_true, person_rgb, screen)
    rows = []
    for t_keep in (1.40, 1.45, 1.55):
        for t_cut in (2.4, 2.8, 3.0, 3.2, 3.4):
            for feather in (1.2, 1.8):
                for despill in (0.85,):
                    score, detail = 0.0, {}
                    for name, (src, a_t, pr, scr) in frames.items():
                        pam = out_dir / "g_tmp.pam"
                        key_frame(src, key_new_chain(t_cut=t_cut,
                                                     despill=despill, feather=feather), pam)
                        m = metrics2(read_pam_rgba(pam), a_t, pr, scr)
                        s = (m["edge_mean_err"] + m["bg_visible_pct"] * 30.0 + m["alpha_mae"] * 2.0
                             + m["fringe_pct"] * 0.6 + m["spill_pct"] * 0.5 + m["fg_leak_pct"] * 8.0
                             + m["eaten_pct"] * 0.2 + m["hard_rim_pct"] * 0.2)
                        score += s
                        detail[name] = round(s, 1)
                    rows.append({"tc": t_cut, "fe": feather, "ds": despill,
                                 "score": round(score, 1), "detail": detail})
    rows.sort(key=lambda r: r["score"])
    (out_dir / "grid.json").write_text(json.dumps(rows[:15], ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rows[:8], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", action="store_true")
    a, _ = ap.parse_known_args()
    sys.exit(grid_search() if a.grid else main())
