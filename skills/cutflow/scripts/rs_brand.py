"""S5 品牌层:Logo 变体矩阵(rules/branding.md,ADR-0014/ADR-0025)。

用法:
  rs_brand.py --analyze 03_创作素材/branding/logos/a.png     # 真实宽高/alpha/内容包围盒
  rs_brand.py --expand --logos brandA,brandB --ratios 9x16,16x9 --out 05_时间线工程/variants.json
  rs_brand.py 05_时间线工程/project.json --variants 05_时间线工程/variants.json --out 06_成片输出 [--plan]

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
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量

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


def logo_rect(logo: dict, canvas: dict, ratio: str, margin_pct: float = 0.04,
              platform: str = "") -> dict:
    """Logo 落点矩形(rules/branding.md §3,v0.10 真实尺寸版;v2 M11 R03 安全区单一化)。

    · `scale` 占**画宽**比例 → 目标宽;高按**真实内容宽高比**求出(不再假定方形);
    · 高度上限 8% 画高(超限则按高等比缩宽)——过大的角标会压人物/标题;
    · margin = 4% 画宽;
    · 安全区查 `platforms.json` 的 safeArea(与 rs_verify 同源;`platform` 缺省或
      未登记 → 退回旧保守带 顶部12%/底部25% 并在 `safeAreaSource` 留痕);
    · bottom 系锚点自动抬到字幕带上缘再留 margin,
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
    # R03+R35(v2 M11,ADR-0058):安全区判据与 rs_verify 单一化 —— 改查
    # templates/platforms.json 的 safeArea(与 rs_verify._platform_safe_areas 同源),
    # 不再硬编码抖音口径(此前小红书/B站变体会越过自家禁区且自家校验器判不出)。
    sa = _platform_safe_area(platform)
    if sa:
        top_safe = int(canvas["height"] * float(sa.get("top", 0.12)))
        band_top = canvas["height"] - int(canvas["height"] * float(sa.get("bottom", 0.25)))
        side_l = int(canvas["width"] * float(sa.get("left", 0.0)))
        side_r = canvas["width"] - int(canvas["width"] * float(sa.get("right", 0.0)))
    else:
        # 平台未声明 → 保留旧保守带(顶部 12% / 底部 25%)并留痕,不猜平台
        top_safe = int(canvas["height"] * 0.12)
        band_top = int(canvas["height"] * 0.75)
        side_l, side_r = 0, canvas["width"]
    xs = {"topLeft": max(m, side_l + m), "bottomLeft": max(m, side_l + m),
          "topRight": min(canvas["width"] - w - m, side_r - w - m),
          "bottomRight": min(canvas["width"] - w - m, side_r - w - m),
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
            "aspect": round(aspect, 4),
            "safeAreaSource": (f"platforms.json:{platform}" if sa
                               else "legacy-bands(平台未声明)")}

def _platform_safe_area(platform: str) -> dict | None:
    """平台 safeArea(与 rs_verify 同源:templates/platforms.json 单一事实源)。

    平台未声明/未登记/读不得 → None(调用方退回旧保守带并留痕,不猜平台)。
    """
    if not platform:
        return None
    try:
        from rs_subtitle import load_platforms  # noqa: PLC0415 — 懒加载防环
        entry = (load_platforms() or {}).get(platform) or {}
    except Exception:  # noqa: BLE001 — 预设读不得 → 保守带(闸的缺席必须显式)
        return None
    sa = entry.get("safeArea")
    return dict(sa) if isinstance(sa, dict) else None


def check_safe_area(rect: dict, canvas: dict, platform: str = "") -> list[str]:
    """Logo 不得进入字幕带 / 不得越界(v0.10 补 x 方向与四边检查;v2 M11 R03 同源化)。

    字幕带 = 平台 safeArea.bottom(platforms.json,与 logo_rect/rs_verify 同源);
    platform 未声明 → 与 logo_rect 同款旧保守带(25%),两处口径恒一致。
    """
    errs = []
    sa = _platform_safe_area(platform)
    bottom_band = (canvas["height"] - int(canvas["height"] * float(sa.get("bottom", 0.25)))
                   if sa else int(canvas["height"] * 0.75))
    if rect["y"] + rect["h"] > bottom_band:
        errs.append(f"Logo 进入字幕带(y+h={rect['y'] + rect['h']} > {bottom_band})")
    if rect["x"] < 0 or rect["y"] < 0 or \
       rect["x"] + rect["w"] > canvas["width"] or rect["y"] + rect["h"] > canvas["height"]:
        errs.append("Logo 越界")
    return errs


def variant_ir(ir: dict, variant: dict, logo: dict, ratio: str,
               platform: str = "") -> dict:
    """在 IR 末尾追加一条 logo overlay 轨(纯声明,不改动其它轨道)。

    v0.10:clip 产 `overlay={x,y,w,h,opacity}` 绝对像素落点(真实尺寸、真实宽高比),
    由 rs_render step_compose 消费 —— v0.9 该字段无人认领,Logo 会贴满画布。
    v2 M11(R03):platform 透传给 logo_rect → 安全区查 platforms.json(单一事实源)。
    """
    doc = json.loads(json.dumps(ir))
    w, h = canvas_for(ratio)                       # 画幅查表(rs_common 唯一真相源)
    doc["canvas"] = {"width": w, "height": h}
    lg = dict(logo)
    if "_probe" not in lg:                             # 已带探测结果(批量/测试)则不重复读盘
        lg["_probe"] = probe_logo(logo["src"])
    rect = logo_rect(lg, doc["canvas"], ratio, platform=platform)
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


def _absolutize_assets(doc: dict, base: Path) -> None:
    """变体 IR 的素材路径就地改绝对(锚定工程根)。

    rs_render 以「IR 文件位置推导 base_dir」——变体 IR 在 06_成片输出/branded/_variants/
    下,工程根相对的 src 在那里必然解析错位,渲染/校验必挂;此前被 rs_brand 恒 0 的
    退出码掩盖(失败也记 S5 done,P10b-1 随独占子目录一并修正)。
    """
    def _abs(v: str) -> str:
        return v if not v or Path(v).is_absolute() else str((base / v).resolve())

    for tr in doc.get("tracks", []):
        for c in tr.get("clips", []):
            if c.get("src"):
                c["src"] = _abs(str(c["src"]))
    sub = doc.get("subtitle")
    if isinstance(sub, dict):
        for k in ("ass", "source"):
            if sub.get(k):
                sub[k] = _abs(str(sub[k]))
    bgm = doc.get("bgm")
    if isinstance(bgm, dict) and bgm.get("src"):
        bgm["src"] = _abs(str(bgm["src"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ir", nargs="?")
    ap.add_argument("--expand", action="store_true")
    ap.add_argument("--analyze", help="只分析一个 Logo 文件:真实宽高/alpha/内容包围盒")
    ap.add_argument("--logos", default="")
    ap.add_argument("--ratios", default="9x16")
    ap.add_argument("--durations", default="full")
    ap.add_argument("--variants")
    # P19-1 伴随修:--out 不再 argparse 级 required —— `--analyze` / `--expand` 模式
    # 用不到落点目录,全局 required 曾让手册里的裸 `--analyze` 示例永远解析不过;
    # 真正需要落点的变体渲染在下面用 NO_INPUT 显式把关(退出码语义不变)。
    ap.add_argument("--out", default="")
    ap.add_argument("--profile", default="final")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--logo-src", default=rs_paths.p("assets") + "/branding/logos/{id}.png")
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
        if not a.out:
            return emit(False, "NO_INPUT", "--expand 需要 --out <variants.json 落点>", exit_code=2)
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return emit(True, "VARIANTS_OK", f"变体矩阵 {len(doc['matrix'])} 条 → {out}",
                    {"path": str(out), "count": len(doc["matrix"]), "matrix": doc["matrix"]})

    if not a.ir or not a.variants or not a.out:
        return emit(False, "NO_INPUT", "需要 <ir> --variants <variants.json> --out <dir>", exit_code=2)
    ir = json.loads(Path(a.ir).read_text(encoding="utf-8"))
    # R03:平台声明取 pipeline.json params.platform(与 rs_verify._platform_safe_areas 同一口径
    # ——只认意图编译落账的声明,不猜画幅);IR 旁找不到 pipeline.json 就留空(保守带)。
    platform = ""
    try:
        _pp = rs_paths.pipeline_json(Path(a.ir).resolve().parent.parent)
        if _pp.is_file():
            platform = str((json.loads(_pp.read_text(encoding="utf-8"))
                            or {}).get("params", {}).get("platform") or "")
    except (OSError, json.JSONDecodeError):
        platform = ""
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
            doc = variant_ir(ir, v, lg, v["ratio"], platform=platform)
        except (FileNotFoundError, ValueError) as exc:
            all_errs.append(f"{v['id']}: {exc}")
            continue
        errs = check_safe_area(doc["_variant"]["logoRect"], doc["canvas"], platform=platform)
        if errs:
            all_errs.extend(f"{v['id']}: {e}" for e in errs)
        if doc["_variant"]["logoRect"].get("lifted"):
            all_errs.append(f"{v['id']}: Logo 自动抬升避开字幕带(交付说明需标注)")
        vp = vdir / f"{v['id']}.json"
        _absolutize_assets(doc, Path.cwd())
        vp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        # P10b-1:品牌变体成片全部落 --out 指定的独占子目录(06_成片输出/branded/),
        # 通过 --out 显式告知 rs_render —— 预测路径与实际写盘必须逐字一致。
        final = (outdir / f"成片_{v['ratio']}_{v['logo']}_{a.profile}.mp4").resolve()
        cmd = [sys.executable, str(SCRIPTS_DIR / "rs_render.py"), str(vp),
               "--ratio", v["ratio"], "--profile", a.profile, "--out", str(final)]
        plans.append({"variant": v["id"], "ir": str(vp), "output": str(final),
                      "cmd": " ".join(cmd)})
        if not a.plan:
            # P24-1:子进程显式 UTF-8 + 限时(变体渲染失败信息在 cp936 控制台不乱码)
            p = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=3600)
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
    # P10b-1 伴随修正:渲染有失败必须非零退出(此前恒 0,rs_run 会把失败的 S5 记成 done
    # —— 产物声明=实际写盘在缓存账上不成立)
    all_ok = ok == len(results) and not all_errs and bool(results)
    return emit(all_ok, "BRAND_OK" if all_ok else "BRAND_PARTIAL", msg,
                {"results": results, "errors": all_errs, "plan": plans},
                exit_code=0 if all_ok else 4)


if __name__ == "__main__":
    sys.exit(main())
