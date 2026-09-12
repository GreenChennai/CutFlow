"""S5 品牌层:Logo 变体矩阵(rules/branding.md,ADR-0014)。

用法:
  rs_brand.py --expand --logos brandA,brandB --ratios 9x16,16x9 --out 05_ir/variants.json
  rs_brand.py 05_ir/project.json --variants 05_ir/variants.json --out 06_output [--plan]

策略:共享中间件,只分叉最后一步——base/composed/mixed/subtitled 各算一次,
每个变体只做 logo overlay + encode(2 Logo × 2 比例 ≈ 1.3 倍单变体成本)。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import canvas_for, emit  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent


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
    """Logo 落点矩形(相对画幅),避开安全区(rules/branding.md §3)。"""
    scale = float(logo.get("scale", 0.12))
    m = int(canvas["width"] * margin_pct)
    w = int(canvas["width"] * scale)
    h = w                                             # 方形占位,实际由 rs_render 按素材比缩放
    anchor = logo.get("anchor", "topRight")
    top_safe = int(canvas["height"] * 0.12)           # 顶部 12% 安全区
    bottom_safe = int(canvas["height"] * 0.75)        # 底部 25% 为字幕带
    x = {"topLeft": m, "bottomLeft": m,
         "topRight": canvas["width"] - w - m, "bottomRight": canvas["width"] - w - m,
         "watermark": (canvas["width"] - w) // 2}[anchor]
    y = {"topLeft": top_safe + m, "topRight": top_safe + m,
         "bottomLeft": bottom_safe - h - m, "bottomRight": bottom_safe - h - m,
         "watermark": (canvas["height"] - h) // 2}[anchor]
    return {"x": int(x), "y": int(y), "w": int(w), "h": int(h),
            "anchor": anchor, "ratio": ratio}


def check_safe_area(rect: dict, canvas: dict) -> list[str]:
    """Logo 不得进入字幕带 / 不得越界。"""
    errs = []
    if rect["y"] + rect["h"] > int(canvas["height"] * 0.75):
        errs.append(f"Logo 进入字幕带(y+h={rect['y'] + rect['h']} > {int(canvas['height'] * 0.75)})")
    if rect["x"] < 0 or rect["y"] < 0 or rect["x"] + rect["w"] > canvas["width"]:
        errs.append("Logo 越界")
    return errs


def variant_ir(ir: dict, variant: dict, logo: dict, ratio: str) -> dict:
    """在 IR 末尾追加一条 logo overlay 轨(纯声明,不改动其它轨道)。"""
    doc = json.loads(json.dumps(ir))
    w, h = canvas_for(ratio)                       # 画幅查表(rs_common 唯一真相源)
    doc["canvas"] = {"width": w, "height": h}
    rect = logo_rect(logo, doc["canvas"], ratio)
    doc["outputs"] = [ratio]
    doc["variantId"] = variant["id"]
    total = 0
    for c in doc.get("tracks", []):
        if c.get("kind") == "video" and c.get("clips"):
            total = max(int(cl.get("startMs", 0)) + int(cl.get("durationMs", 0)) for cl in c["clips"])
    doc.setdefault("tracks", []).append({"kind": "video", "name": f"brand_{variant['id']}", "clips": [
        {"src": logo["src"], "startMs": int(logo.get("inMs", 0)),
         "durationMs": int(logo["outMs"] - logo["inMs"]) if logo.get("outMs") else max(total, 1000),
         "overlay": {"x": rect["x"], "y": rect["y"], "w": rect["w"], "h": rect["h"],
                     "opacity": float(logo.get("opacity", 1.0))}}]})
    doc["_variant"] = {"id": variant["id"], "ratio": ratio, "logoRect": rect,
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
    ap.add_argument("--logos", default="")
    ap.add_argument("--ratios", default="9x16")
    ap.add_argument("--durations", default="full")
    ap.add_argument("--variants")
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile", default="final")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--logo-src", default="03_assets/branding/logos/{id}.png")
    a = ap.parse_args()

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
        doc = variant_ir(ir, v, lg, v["ratio"])
        errs = check_safe_area(doc["_variant"]["logoRect"], doc["canvas"])
        if errs:
            all_errs.extend(f"{v['id']}: {e}" for e in errs)
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
        msg += f";{len(all_errs)} 个安全区问题"
    return emit(ok == len(results) and not all_errs, "BRAND_OK" if ok else "BRAND_PARTIAL", msg,
                {"results": results, "errors": all_errs, "plan": plans})


if __name__ == "__main__":
    sys.exit(main())
