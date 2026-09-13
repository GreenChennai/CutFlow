"""S5 品牌层:Logo 变体矩阵(rules/branding.md,ADR-0014/ADR-0025)。

用法:
  rs_brand.py --analyze 03_assets/branding/logos/a.png     # 真实宽高/alpha/内容包围盒
  rs_brand.py --expand --logos brandA,brandB --ratios 9x16,16x9 --out 05_ir/variants.json
  rs_brand.py 05_ir/project.json --variants 05_ir/variants.json --out 06_output [--plan]

策略:共享中间件,只分叉最后一步——base/composed/mixed/subtitled 各算一次,
每个变体只做 logo overlay + encode(2 Logo × 2 比例 ≈ 1.3 倍单变体成本)。

v0.10(ADR-0025)修复与增强:
  · probe_logo 接线(旧版定义了却没人调用)+ alpha 内容包围盒:PNG 画板的透明
    padding 不再污染宽高比,Logo 按真实内容形状缩放;
  · logo_rect 按真实宽高比求高(旧版假定方形占位),高度上限 8% 画高;
  · anchor 扩 6 位:topLeft/topRight/bottomLeft/bottomRight/topCenter/bottomCenter
    (+watermark 兼容保留);bottom 系自动抬到字幕带上方并留痕 lifted;
  · 变体轨产 clip.overlay={x,y,w,h,opacity}(rs_render step_compose 已消费;
    v0.9 该字段无人认领,Logo 会贴满画布 —— 潜伏致命 bug);
  · check_safe_area 补 x 方向与四边硬越界检查。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import canvas_for, emit, ffmpeg_bin, ffprobe_json, load_config  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent

ANCHORS = ("topLeft", "topRight", "bottomLeft", "bottomRight",
           "topCenter", "bottomCenter", "watermark")


def _alpha_bbox(src: Path, w: int, h: int) -> list[int] | None:
    """RGBA 内容包围盒 [x, y, w, h](alpha>8 视为内容),纯 ffmpeg 管道零三方依赖。"""
    try:
        p = subprocess.run([ffmpeg_bin(load_config()), "-v", "error", "-i", str(src),
                            "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                           capture_output=True, timeout=30)
        raw = p.stdout
        if p.returncode != 0 or len(raw) != w * h * 4:
            return None
        minx, miny, maxx, maxy = w, h, -1, -1
        for yy in range(h):
            base = yy * w * 4
            xs = [i for i in range(w) if raw[base + i * 4 + 3] > 8]
            if xs:
                miny = min(miny, yy)
                maxy = max(maxy, yy)
                minx = min(minx, xs[0])
                maxx = max(maxx, xs[-1])
        if maxx >= minx and maxy >= miny:
            return [minx, miny, maxx - minx + 1, maxy - miny + 1]
    except Exception:  # noqa: BLE001 — 包围盒失败退整图,不阻塞
        pass
    return None


def probe_logo(src: str | Path) -> dict:
    """识别 Logo 真实属性(T4):像素宽高、alpha、宽高比、内容包围盒。"""
    p = Path(src)
    if not p.is_file():
        raise FileNotFoundError(f"Logo 文件不存在:{p}")
    info = ffprobe_json(p, None)
    vs = next((s for s in info.get("streams", []) if s["codec_type"] == "video"), None)
    if vs is None:
        raise ValueError(f"不是图片/无法解析:{p}")
    w, h = int(vs.get("width", 0)), int(vs.get("height", 0))
    fmt = str(vs.get("pix_fmt", ""))
    # v0.10 修:旧实现 `"alpha" in fmt` 对 rgba/bgra 永远 False(rgba 不含 "alpha" 子串)
    has_alpha = (fmt.startswith(("rgba", "bgra", "argb", "abgr"))
                 or "yuva" in fmt or "alpha" in fmt or fmt == "pal8")
    bbox = _alpha_bbox(p, w, h) if (w and h and has_alpha) else None
    cw, ch = (bbox[2], bbox[3]) if bbox else (w, h)
    return {"path": str(p), "width": w, "height": h,
            "hasAlpha": has_alpha, "aspect": (w / h) if h else 1.0,
            "content": {"bbox": bbox, "width": cw, "height": ch,
                        "aspect": (cw / ch) if ch else 1.0}}


def crop_to_content(src: str | Path, bbox: list[int]) -> Path:
    """把 Logo 裁到内容包围盒,派生文件按 bbox 缓存(v0.10 P1 实测修复)。

    整图带透明 padding 时,overlay 会把**整张图**缩到 rect.w —— 可见内容宽度
    远小于 scale 承诺(e2e 实测 12% → 3.7%)。裁到 bbox 后 rect.w 就是视觉宽度。
    裁剪失败退整图,不阻塞(与 _alpha_bbox 同一容错口径)。
    """
    p = Path(src)
    x, y, w, h = (int(v) for v in bbox)
    out = p.with_name(f"{p.stem}.crop{w}x{h}+{x}+{y}.png")
    if out.is_file():
        return out
    r = subprocess.run([ffmpeg_bin(load_config()), "-v", "error", "-y", "-i", str(p),
                        "-vf", f"crop={w}:{h}:{x}:{y}", str(out)], capture_output=True)
    return out if (r.returncode == 0 and out.is_file()) else p


def expand_matrix(logos: list[str], ratios: list[str], durations: list[str] | None = None) -> list[dict]:
    """变体矩阵笛卡尔积。"""
    durations = durations or ["full"]
    out = []
    for lg in logos:
        for rt in ratios:
            for du in durations:
                vid = f"{lg}_{rt}" + ("" if du == "full" else f"_{du}")
                out.append({"id": vid, "logo": lg, "ratio": rt, "duration": du,
                            "backends": ["ffmpeg"]})
    return out


def logo_rect(logo: dict, canvas: dict, ratio: str, margin_pct: float = 0.04) -> dict:
    """Logo 落点矩形(rules/branding.md §3,v0.10 真实尺寸版)。

    · `scale` 占**画宽**比例 → 目标宽;高按**真实内容宽高比**求出(不再假定方形);
    · 高度上限 8% 画高(超限则按高等比缩宽)——过大的角标会压人物/标题;
    · margin = 4% 画宽;
    · bottom 系锚点自动抬到字幕带(9:16 底部 25%,ADR-0009)上缘再留 margin,
      并在返回值 `lifted=true` 留痕(交付说明需标注「Logo 已避开字幕带」)。
    """
    probe = logo.get("_probe") or {}
    aspect = float((probe.get("content") or {}).get("aspect")
                   or probe.get("aspect") or 1.0)
    scale = float(logo.get("scale", 0.12))
    m = int(canvas["width"] * margin_pct)
    w = int(canvas["width"] * scale)
    h = int(round(w / aspect)) if aspect else w
    max_h = int(canvas["height"] * 0.08)
    if h > max_h:
        h = max_h
        w = int(round(h * aspect))
    anchor = logo.get("anchor", "topRight")
    top_safe = int(canvas["height"] * 0.12)           # 顶部 12% 安全区
    band_top = int(canvas["height"] * 0.75)           # 底部 25% 为字幕带
    xs = {"topLeft": m, "bottomLeft": m,
          "topRight": canvas["width"] - w - m, "bottomRight": canvas["width"] - w - m,
          "topCenter": (canvas["width"] - w) // 2,
          "bottomCenter": (canvas["width"] - w) // 2,
          "watermark": (canvas["width"] - w) // 2}
    ys = {"topLeft": top_safe + m, "topRight": top_safe + m, "topCenter": top_safe + m,
          "bottomLeft": band_top - h - m, "bottomRight": band_top - h - m,
          "bottomCenter": band_top - h - m,
          "watermark": (canvas["height"] - h) // 2}
    if anchor not in xs:
        raise ValueError(f"未知 anchor:{anchor}(可选 {'/'.join(ANCHORS)})")
    return {"x": int(xs[anchor]), "y": int(ys[anchor]), "w": int(w), "h": int(h),
            "anchor": anchor, "ratio": ratio, "lifted": anchor.startswith("bottom"),
            "aspect": round(aspect, 4)}


def check_safe_area(rect: dict, canvas: dict) -> list[str]:
    """Logo 不得进入字幕带 / 不得越界(v0.10 补 x 方向与四边检查)。"""
    errs = []
    if rect["y"] + rect["h"] > int(canvas["height"] * 0.75):
        errs.append(f"Logo 进入字幕带(y+h={rect['y'] + rect['h']} > {int(canvas['height'] * 0.75)})")
    if rect["x"] < 0 or rect["y"] < 0 or \
       rect["x"] + rect["w"] > canvas["width"] or rect["y"] + rect["h"] > canvas["height"]:
        errs.append("Logo 越界")
    return errs


def variant_ir(ir: dict, variant: dict, logo: dict, ratio: str) -> dict:
    """在 IR 末尾追加一条 logo overlay 轨(纯声明,不改动其它轨道)。

    v0.10:clip 产 `overlay={x,y,w,h,opacity}` 绝对像素落点(真实尺寸、真实宽高比),
    由 rs_render step_compose 消费 —— v0.9 该字段无人认领,Logo 会贴满画布。
    """
    doc = json.loads(json.dumps(ir))
    w, h = canvas_for(ratio)                       # 画幅查表(rs_common 唯一真相源)
    doc["canvas"] = {"width": w, "height": h}
    lg = dict(logo)
    if "_probe" not in lg:                             # 已带探测结果(批量/测试)则不重复读盘
        lg["_probe"] = probe_logo(logo["src"])
    rect = logo_rect(lg, doc["canvas"], ratio)
    # v0.10 P1(ADR-0025 补):整图含透明 padding 时把素材裁到内容包围盒再上轨 ——
    # 否则 overlay 缩的是整张图,可见 Logo 远小于 scale 承诺(e2e 实测 12%→3.7%)。
    logo_src = logo["src"]
    bbox = (lg["_probe"].get("content") or {}).get("bbox")
    if bbox and (bbox[2] < lg["_probe"]["width"] or bbox[3] < lg["_probe"]["height"]):
        logo_src = str(crop_to_content(logo["src"], bbox))
    doc["outputs"] = [ratio]
    doc["variantId"] = variant["id"]
    total = 0
    for c in doc.get("tracks", []):
        if c.get("kind") == "video" and c.get("clips"):
            total = max(int(cl.get("startMs", 0)) + int(cl.get("durationMs", 0)) for cl in c["clips"])
    doc.setdefault("tracks", []).append({"kind": "video", "name": f"brand_{variant['id']}", "clips": [
        {"src": logo_src, "startMs": int(logo.get("inMs", 0)),
         "durationMs": int(logo["outMs"] - logo["inMs"]) if logo.get("outMs") else max(total, 1000),
         "overlay": {"x": rect["x"], "y": rect["y"], "w": rect["w"], "h": rect["h"],
                     "opacity": float(logo.get("opacity", 0.9))}}]})
    doc["_variant"] = {"id": variant["id"], "ratio": ratio, "logoRect": rect,
                       "logoProbe": {k: lg["_probe"][k] for k in ("width", "height", "hasAlpha")},
                       "backends": variant.get("backends", ["ffmpeg"])}
    return doc


def load_variants(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("matrix"):
        return doc
    doc["matrix"] = expand_matrix([l["id"] for l in doc.get("logos", [])],
                                  doc.get("ratios", ["9x16"]), doc.get("durations"))
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ir", nargs="?")
    ap.add_argument("--expand", action="store_true")
    ap.add_argument("--analyze", help="只分析一个 Logo 文件:真实宽高/alpha/内容包围盒")
    ap.add_argument("--logos", default="")
    ap.add_argument("--ratios", default="9x16")
    ap.add_argument("--durations", default="full")
    ap.add_argument("--variants")
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile", default="final")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--logo-src", default="03_assets/branding/logos/{id}.png")
    a = ap.parse_args()

    if a.analyze:
        try:
            info = probe_logo(a.analyze)
        except (FileNotFoundError, ValueError) as exc:
            return emit(False, "LOGO_INVALID", str(exc), exit_code=2)
        return emit(True, "LOGO_ANALYZED",
                    f"{info['width']}x{info['height']}"
                    f"(内容 {info['content']['width']}x{info['content']['height']},"
                    f"比例 {info['content']['aspect']:.3f},alpha={info['hasAlpha']})", info)

    if a.expand:
        logos = [x.strip() for x in a.logos.split(",") if x.strip()]
        if not logos:
            return emit(False, "NO_LOGO", "--expand 需要 --logos a,b", exit_code=2)
        doc = {"version": 1,
               "logos": [{"id": lg, "src": a.logo_src.format(id=lg), "anchor": "topRight",
                          "scale": 0.12, "opacity": 0.9} for lg in logos],
               "ratios": [x.strip() for x in a.ratios.split(",") if x.strip()],
               "durations": [x.strip() for x in a.durations.split(",") if x.strip()],
               "matrix": expand_matrix(logos, [x.strip() for x in a.ratios.split(",") if x.strip()],
                                       [x.strip() for x in a.durations.split(",") if x.strip()])}
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return emit(True, "VARIANTS_OK", f"变体矩阵 {len(doc['matrix'])} 条 → {out}",
                    {"path": str(out), "count": len(doc["matrix"]), "matrix": doc["matrix"]})

    if not a.ir or not a.variants:
        return emit(False, "NO_INPUT", "需要 <ir> --variants <variants.json> --out <dir>", exit_code=2)
    ir = json.loads(Path(a.ir).read_text(encoding="utf-8"))
    vdoc = load_variants(Path(a.variants))
    logos = {l["id"]: l for l in vdoc.get("logos", [])}
    outdir = Path(a.out)
    vdir = outdir / "_variants"
    vdir.mkdir(parents=True, exist_ok=True)

    results, plans, all_errs = [], [], []
    for v in vdoc["matrix"]:
        lg = logos.get(v["logo"])
        if not lg:
            all_errs.append(f"变体 {v['id']} 引用了不存在的 logo:{v['logo']}")
            continue
        try:
            doc = variant_ir(ir, v, lg, v["ratio"])
        except (FileNotFoundError, ValueError) as exc:
            all_errs.append(f"{v['id']}: {exc}")
            continue
        errs = check_safe_area(doc["_variant"]["logoRect"], doc["canvas"])
        if errs:
            all_errs.extend(f"{v['id']}: {e}" for e in errs)
        if doc["_variant"]["logoRect"].get("lifted"):
            all_errs.append(f"{v['id']}: Logo 自动抬升避开字幕带(交付说明需标注)")
        vp = vdir / f"{v['id']}.json"
        vp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        final = outdir / f"成片_{v['ratio']}_{v['logo']}_{a.profile}.mp4"
        cmd = [sys.executable, str(SCRIPTS_DIR / "rs_render.py"), str(vp),
               "--ratio", v["ratio"], "--profile", a.profile]
        plans.append({"variant": v["id"], "ir": str(vp), "output": str(final),
                      "cmd": " ".join(cmd)})
        if not a.plan:
            p = subprocess.run(cmd, capture_output=True, text=True)
            results.append({"variant": v["id"], "ok": p.returncode == 0,
                            "output": str(final) if p.returncode == 0 else None,
                            "err": None if p.returncode == 0 else (p.stderr or p.stdout)[-200:]})

    if a.plan:
        for it in plans:
            print(f"{it['variant']}: {it['cmd']}")
        return emit(True, "BRAND_PLAN", f"{len(plans)} 个变体待渲染", {"plan": plans, "errors": all_errs})

    ok = sum(1 for r in results if r["ok"])
    msg = f"{ok}/{len(results)} 个变体渲染完成(共享中间件)"
    if all_errs:
        msg += f";{len(all_errs)} 个安全区/标注问题"
    return emit(ok == len(results) and not all_errs, "BRAND_OK" if ok else "BRAND_PARTIAL", msg,
                {"results": results, "errors": all_errs, "plan": plans})


if __name__ == "__main__":
    sys.exit(main())
