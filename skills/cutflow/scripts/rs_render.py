"""FFmpeg 渲染器:IR → 七步管线 → 成片。

用法:python rs_render.py <project.json> [--ratio 9x16|16x9] [--profile final|preview|draft]
       [--explain] [--clear-cache] [--no-cache]
七步:probe → segment → concat → compose → mix → subtitle → encode。
铁律(见 SKILL.md):统一帧率、逐段提取、字幕最后叠、总线 loudnorm -14 LUFS、段间 8ms afade。

v0.6.0 新增(rules/incremental.md §3):
  · seg 级内容寻址缓存:缓存键 = 媒体指纹 + clip 参数 + 画布/帧率 + **脚本文件 hash**;
    只改一张卡 → 仅该卡重渲,其余全部命中。
  · 步骤级缓存链:concat/compose/mix/subtitle/encode 各自的键 = 上游键 + 本步参数;
    零改动重跑整条渲染链全部命中,秒级返回。
  · v0.14(ADR-0031):抠像/背景合成已移除 —— 用户须先自行抠像+合成背景再交付;
    本渲染器只做剪辑(转场/punch-in/字幕/音效/品牌)。
  · --explain 逐段/逐步报告命中情况(不执行);--clear-cache 清 seg 缓存。

M8 vlog 新增:消费 05_时间线工程/reframe_plan.json(rs_reframe plan 产出)——
命中 plan 的主轨 clip 走「crop 裁切窗 → scale 目标画幅」的横转竖自动重构:
裁切窗比例恒等于目标画幅比(不等即报错,绝不拉伸);轨迹多关键帧按时间线性插值;
violations 非空回退 static-center 并留痕;IR clip.reframe 手动锚点优先于 plan。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import (RATIOS, die, emit, ffmpeg_bin, ffprobe_json, load_config,  # noqa: E402
                       media_duration_s, ratio_for_canvas, run)
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量

RATIO = dict(RATIOS)             # 画幅唯一真相源在 rs_common(新增画幅只改那里)
LOUDNORM_BUS = "loudnorm=I=-14:TP=-1.0:LRA=11"
LOUDNORM_VOICE = "loudnorm=I=-16:TP=-1.5:LRA=11"
LOUDNESS_TARGET = {"I": -14.0, "TP": -1.0, "LRA": 11.0}   # 硬规则 11;出处 ITERATION-GUIDE §8.2
CACHE_VER = "v9"                 # 渲染语义变更时 +1,防旧缓存幽灵命中(v8:M8 消费 reframe_plan;v9:M9 matte 预合成)
SEG_CACHE_KEEP = 400             # segcache 最大保留文件数(超出按 mtime 淘汰)


def esc_sub(path: Path) -> str:
    """ASS 路径转 ffmpeg filter 转义(Windows 盘符冒号)。"""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def cover_crop(iw: int, ih: int, cw: int, ch: int, anchor_y: float = 0.5) -> str:
    """放大到覆盖画布,再按 anchorY 纵向裁切(重构图)。"""
    return (f"scale={cw}:{ch}:force_original_aspect_ratio=increase,"
            f"crop={cw}:{ch}:(iw-{cw})/2:(ih-{ch})*{anchor_y}")


# ---------------- matte 预合成(M9,ADR-0050:带 matte 的 clip 先前景/背景合成再进段渲染) ----------------

def matte_precompose(clip: dict, src: Path, pr: dict, build: Path, base_dir: Path,
                     fps: float, cfg: dict, warnings: list[str]) -> Path | None:
    """clip.matte → 前景(alphamerge alpha 序列)铺到背景(cover)上的中间 mp4。

    返回中间产物路径(替换段渲染的 src,分辨率与源一致,下游 crop/转场/混音零改动);
    alpha 序列缺失或帧数不足以覆盖源 → WARN 留痕并返回 None(按未抠像渲染,不臆测)。
    """
    m = clip.get("matte") or {}
    adir = Path(m.get("alphaDir") or "")
    if adir and not adir.is_absolute():
        adir = base_dir / adir
    if not m.get("alphaDir") or not adir.is_dir():
        warnings.append(f"segment:matte.alphaDir 缺失({adir})→ 该段按未抠像渲染")
        return None
    bg = m.get("bg") or {}
    bg_src = Path(bg.get("src") or "")
    if bg_src and not bg_src.is_absolute():
        bg_src = base_dir / bg_src
    if not bg_src or not bg_src.is_file():
        warnings.append("segment:matte.bg 缺失或不存在 → 该段按未抠像渲染")
        return None
    alphas = sorted(adir.glob("alpha_*.png"))
    try:
        dur_s = float(media_duration_s(src, cfg))
    except Exception:  # noqa: BLE001 — 探测失败按 2 帧下限兜底
        dur_s = 0.0
    need = max(int(dur_s * fps) + 1, 2)
    if len(alphas) < need:
        warnings.append(f"segment:matte alpha 帧数不足({len(alphas)}<{need})→ 该段按未抠像渲染")
        return None
    out_dir = build / "matte"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}_matted.mp4"
    out_tmp = out.with_suffix(".part.mp4")
    src_w, src_h = pr.get("width"), pr.get("height")
    if not src_w or not src_h:
        warnings.append("segment:matte 源尺寸探测失败 → 该段按未抠像渲染")
        return None
    vf = (f"[1:v]scale={src_w}:{src_h}:force_original_aspect_ratio=increase,"
          f"crop={src_w}:{src_h}[bg];"
          f"[0:v][2:v]alphamerge[fg];[bg][fg]overlay=0:0:shortest=1")
    cmd = [ffmpeg_bin(cfg), "-y", "-v", "error",
           "-i", str(src),
           "-i", str(bg_src),
           "-framerate", f"{fps:g}", "-start_number", "0", "-i", str(adir / "alpha_%05d.png"),
           "-filter_complex", vf,
           "-pix_fmt", "yuv420p", str(out_tmp)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=3600)
    if p.returncode != 0:
        warnings.append(f"segment:matte 预合成失败:{p.stderr[-200:]} → 按未抠像渲染")
        return None
    out_tmp.replace(out)
    return out


# ---------------- reframe_plan 消费(M8 vlog:vision.track / vision.reframe 渲染端) ----------------
#
# rs_reframe plan 产出 05_时间线工程/reframe_plan.json(坐标一律源像素,窗比恒等于目标比)。
# 渲染端职责(方案 §5.5.2 横转竖硬约束):
#   · 命中 plan 的 clip 走「crop 裁切窗 → scale 目标画幅」——窗比 ≠ 目标比直接报错,绝不拉伸;
#   · mode=track 且轨迹 ≥2 关键帧 → 按时间线性插值平移(滑窗中位数平滑/限速已在 plan 侧做完);
#   · violations 非空(REFRAME_CLIP_SUBJECT 主体被切破)→ 回退 static-center 并警告留痕;
#   · IR clip.reframe 手动锚点优先于 plan(人工改锚走 rs_edit clip.reframe);
#   · plan 缺失/坏档/过期 → 旧 cover_crop 档,M8 之前的行为完全不变。

REFRAME_RATIO_EPS = 2.0     # 窗比容差:按窗长边计的像素级取整容差(真正的比例错配远超此值)


def load_reframe_plan(base_dir: Path) -> dict:
    """读工程根下 05_时间线工程/reframe_plan.json;缺失/坏档 → {}(全走旧档,不臆测)。"""
    if not base_dir:
        return {}
    p = rs_paths.resolve(base_dir, "timeline") / "reframe_plan.json"
    if not p.is_file():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return doc if isinstance(doc, dict) and doc.get("clips") else {}


def plan_entry_for(plan: dict, clip: dict) -> dict | None:
    """IR clip → plan 条目:clipId 精确匹配优先(同素材多 clip 轨迹各异),src 兜底。"""
    cid, src = str(clip.get("id") or ""), str(clip.get("src") or "")
    for e in plan.get("clips") or []:
        if cid and e.get("clipId") == cid:
            return e
    for e in plan.get("clips") or []:
        if src and e.get("src") == src:
            return e
    return None


def _plan_digest(entry: dict) -> str:
    """plan 条目内容摘要 —— 进 seg 缓存键:plan 一改,该 clip 必重渲。"""
    return hashlib.sha1(json.dumps(entry, sort_keys=True, ensure_ascii=False)
                        .encode("utf-8")).hexdigest()[:16]


def _snap_crop_window(win: dict, src_w: int, src_h: int, cw: int, ch: int
                      ) -> tuple[int, int, int, int]:
    """浮点裁切窗 → 偶数整数窗 (w,h,x0,y0),并强制「窗比=目标比」(裁切不拉伸):

    · 先校验 plan 窗比:与目标比的偏差折算超过半像素级容差 → REFRAME_RATIO 报错不拉伸
      (继承 artboard 桥「尺寸不符报错不拉伸」精神,方案 §5.5.2 硬约束);
    · 再取偶对齐(宽按目标比从高重推,与浮点窗差 ≤1 源像素,缩放后为亚像素级偏差);
    · 窗越出源边界 → REFRAME_PLAN_INVALID 报错(渲染绝不静默平移/收缩裁切窗)。
    """
    w0 = max(2, int(round(float(win.get("w", 0)))))
    h0 = max(2, int(round(float(win.get("h", 0)))))
    if abs(w0 / h0 - cw / ch) * max(w0, h0) > REFRAME_RATIO_EPS:
        die(4, "REFRAME_RATIO",
            f"reframe 裁切窗 {w0}x{h0} 比例 ≠ 目标画幅 {cw}x{ch} —— 裁切不拉伸是硬约束,"
            "请重跑 rs_reframe plan(裁切窗比例必须恒等于目标画幅比)")
    if (float(win.get("x0", 0)) < -REFRAME_RATIO_EPS
            or float(win.get("y0", 0)) < -REFRAME_RATIO_EPS
            or float(win.get("x0", 0)) + w0 > src_w + REFRAME_RATIO_EPS
            or float(win.get("y0", 0)) + h0 > src_h + REFRAME_RATIO_EPS):
        die(4, "REFRAME_PLAN_INVALID",
            f"reframe 裁切窗({win.get('x0')},{win.get('y0')},{w0}x{h0})越出源边界 "
            f"{src_w}x{src_h};请重跑 rs_reframe plan(渲染不静默平移裁切窗)")
    w, h = w0, h0
    w = max(2, 2 * int(round(h * cw / ch / 2.0)))        # 宽随高按目标比取偶
    if w > src_w:                                         # 源比目标比更窄:以源宽为限,高随之重推
        w = max(2, src_w - src_w % 2)
        h = max(2, 2 * int(round(w * ch / cw / 2.0)))
    if h > src_h:
        h = max(2, src_h - src_h % 2)
        w = max(2, 2 * int(round(h * cw / ch / 2.0)))
    if abs(w / h - cw / ch) * max(w, h) > REFRAME_RATIO_EPS:
        die(4, "REFRAME_RATIO",
            f"reframe 裁切窗 {w}x{h} 取偶后比例仍 ≠ 目标画幅 {cw}x{ch}(源 {src_w}x{src_h}"
            "放不下目标比)—— 裁切不拉伸是硬约束,请改用与目标画幅同比的素材")
    x0 = int(round(min(max(float(win.get("x0", 0)) + (float(win.get("w", w)) - w) / 2.0, 0.0),
                        max(src_w - w, 0.0))))
    y0 = int(round(min(max(float(win.get("y0", 0)) + (float(win.get("h", h)) - h) / 2.0, 0.0),
                        max(src_h - h, 0.0))))
    if x0 + w > src_w or y0 + h > src_h:
        die(4, "REFRAME_PLAN_INVALID",
            f"reframe 裁切窗越界:窗({x0},{y0},{w}x{h}) 源({src_w}x{src_h});请重跑 rs_reframe plan")
    return w, h, x0, y0


def _piecewise_expr(knots: list[tuple[float, float]]) -> str:
    """[(t_s, v)] 升序 → ffmpeg 时变表达式:分段线性(渲染侧线性插值),t 越界夹紧到首/末值。

    逗号统一转义,可直接作为 crop 的 x/y 位置表达式嵌入 -vf(filtergraph 里 t=帧时间秒)。
    """
    knots = sorted((float(t), float(v)) for t, v in knots)
    dedup: list[tuple[float, float]] = []
    for t, v in knots:                                   # 同刻度重复关键帧保留后者
        if dedup and t <= dedup[-1][0]:
            dedup[-1] = (t, v)
        else:
            dedup.append((t, v))
    if not dedup:
        return "0"
    if len(dedup) == 1:
        return f"{dedup[0][1]:.2f}"
    expr = f"{dedup[0][1]:.2f}"                          # t 早于首个关键帧 → 夹紧首值
    for (t0, v0), (t1, v1) in zip(dedup, dedup[1:]):     # 首段在最内层、末段最外层
        dt = t1 - t0
        if dt <= 0:
            continue
        seg = (f"{v0:.2f}+({v1:.2f}-{v0:.2f})"
               f"*min(max((t-{t0:.3f})/{dt:.6f},0),1)")
        expr = f"if(gte(t,{t0:.3f}),{seg},{expr})"
    return expr.replace(",", "\\,")


def _resolve_reframe(clip: dict, i: int, pr: dict, cw: int, ch: int, fps: float,
                     plan: dict, warnings: list[str]
                     ) -> tuple[list[str] | None, str, str]:
    """IR clip × reframe_plan → (段级 crop/scale 滤镜链 | None, 缓存键摘要, 应用档位)。

    返回 (None, "", "") = 不消费 plan(无条目/手动锚点/方案过期)→ 旧 cover_crop 档。
    应用档位:track(轨迹插值)/ static-center(plan 静态窗或回退)。
    """
    entry = plan_entry_for(plan, clip)
    if not entry or not isinstance(entry.get("cropWindow"), dict):
        return None, "", ""
    if clip.get("reframe"):
        warnings.append(f"seg[{i}]:clip.reframe 手动锚点生效,跳过 reframe_plan(人工优先于自动)")
        return None, "", ""
    src_w = int(pr.get("width") or entry.get("srcWidth") or 0)
    src_h = int(pr.get("height") or entry.get("srcHeight") or 0)
    if src_w <= 0 or src_h <= 0:
        return None, "", ""
    if (entry.get("srcWidth") and int(entry["srcWidth"]) != src_w) or \
       (entry.get("srcHeight") and int(entry["srcHeight"]) != src_h):
        warnings.append(f"seg[{i}]:reframe_plan 源画幅与实际不符"
                        f"(plan {entry.get('srcWidth')}x{entry.get('srcHeight')} vs "
                        f"{src_w}x{src_h}),疑过期 → 旧档 cover_crop;建议重跑 rs_reframe plan")
        return None, "", ""
    w, h, x0, y0 = _snap_crop_window(entry["cropWindow"], src_w, src_h, cw, ch)
    digest = _plan_digest(entry)
    static_vf = [f"crop={w}:{h}:{x0}:{y0}", f"scale={cw}:{ch}"]
    if entry.get("violations"):
        # REFRAME_CLIP_SUBJECT 留痕:violations 非空 → 不消费轨迹,回退 static-center
        # (rs_reframe 侧降级时 cropWindow 已改写为居中窗;外来 track 方案同样按此窗渲染)。
        v0 = entry["violations"][0]
        code = v0.get("code", "REFRAME_CLIP_SUBJECT") if isinstance(v0, dict) \
            else "REFRAME_CLIP_SUBJECT"
        warnings.append(f"seg[{i}]:reframe violations 非空({code})"
                        "→ 回退 static-center 渲染(留痕)")
        return static_vf, digest, "static-center"
    traj = [k for k in (entry.get("trajectory") or [])
            if isinstance(k, dict) and {"tMs", "anchorX", "anchorY"} <= set(k)]
    if str(entry.get("mode")) == "track" and len(traj) >= 2:
        plan_ms = float(entry.get("durationMs") or 0)
        clip_ms = float(clip.get("durationMs") or 0)
        if plan_ms <= 0 or abs(plan_ms - clip_ms) > 1500.0 / max(fps, 1.0):
            warnings.append(f"seg[{i}]:reframe_plan 轨迹时长({plan_ms:.0f}ms)与 IR clip"
                            f"({clip_ms:.0f}ms)不符,疑过期 → 按裁切窗 static 渲染;"
                            "建议重跑 rs_reframe plan")
            return static_vf, digest, "static-center"
        scales = {round(float(k.get("scale", 1.0)), 4) for k in traj}
        if len(scales) > 1:
            # 渲染侧轨迹只做平移插值(裁切窗恒定);变焦轨迹不属于 plan 现有产出,如实降级留痕。
            warnings.append(f"seg[{i}]:reframe 轨迹含变焦(渲染端按恒定裁切窗实现)"
                            "→ 取首关键帧 static 渲染")
            return static_vf, digest, "static-center"
        # 关键帧锚点 → 裁切窗左上角(贴边夹紧,与 plan 侧 crop_window 同口径),按时间线性插值
        kx = [(max(float(k["tMs"]), 0.0) / 1000.0,
               min(max(float(k["anchorX"]) - w / 2.0, 0.0), max(src_w - w, 0.0)))
              for k in traj]
        ky = [(max(float(k["tMs"]), 0.0) / 1000.0,
               min(max(float(k["anchorY"]) - h / 2.0, 0.0), max(src_h - h, 0.0)))
              for k in traj]
        return [f"crop={w}:{h}:x={_piecewise_expr(kx)}:y={_piecewise_expr(ky)}",
                f"scale={cw}:{ch}"], digest, "track"
    return static_vf, digest, "static-center"


def clip_path(clip: dict, base_dir: Path):
    """解析 clip.src:相对工程根;`assets_sfx:<名>` 指向仓库内置音效库。"""
    src = clip["src"]
    if src.startswith("assets_sfx:"):
        from rs_common import REPO_ROOT
        return REPO_ROOT / "assets" / "sfx" / (src.split(":", 1)[1] + ".mp3")
    p = Path(src)
    return p if p.is_absolute() else base_dir / p


def probe_clip(clip: dict, base_dir: Path, cfg: dict) -> dict:
    src = clip_path(clip, base_dir)
    info = {"path": str(src), "type": "image" if src.suffix.lower() in
            (".png", ".jpg", ".jpeg", ".webp") else "video"}
    if info["type"] == "video":
        pr = ffprobe_json(src, cfg)
        as_ = next((s for s in pr["streams"] if s["codec_type"] == "audio"), None)
        vs = next((s for s in pr["streams"] if s["codec_type"] == "video"), None)
        info["has_audio"] = as_ is not None
        info["width"], info["height"] = (vs or {}).get("width"), (vs or {}).get("height")
    return info


# ---------------------------------------------------------------- 缓存键

_SCRIPT_HASH: str | None = None


def script_hash() -> str:
    """rs_render.py + rs_common.py 内容 hash —— 代码一改,全键失效(SKILL 硬规则 4)。"""
    global _SCRIPT_HASH
    if _SCRIPT_HASH is None:
        h = hashlib.sha1()
        for name in ("rs_render.py", "rs_common.py"):
            h.update((Path(__file__).parent / name).read_bytes())
        _SCRIPT_HASH = h.hexdigest()[:16]
    return _SCRIPT_HASH


def media_fingerprint(path: Path) -> str:
    """媒体内容指纹:size + mtime + 首尾各 1MB blake2b(286MB 素材 <10ms)。"""
    st = path.stat()
    h = hashlib.blake2b(digest_size=16)
    h.update(f"{path.name}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8", "replace"))
    with path.open("rb") as f:
        h.update(f.read(1 << 20))
        tail = min(1 << 20, st.st_size)
        f.seek(-tail, 2)
        h.update(f.read(tail))
    return h.hexdigest()


def seg_key(clip: dict, doc: dict, cw: int, ch: int, fp: str) -> str:
    """单段缓存键:**不含段序号**——两个内容相同的段共享缓存(重排序也命中)。"""
    payload = {
        "cacheVer": CACHE_VER, "scripts": script_hash(),
        "fps": doc["fps"], "canvas": [cw, ch], "media": fp,
        "clip": {k: clip.get(k) for k in
                 ("src", "durationMs", "sourceInMs", "speed", "loop", "volume",
                  "reframe", "motion", "tailMs", "punchIn", "freezeMs", "_planKey")},
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                        .encode("utf-8")).hexdigest()


def step_key(kind: str, upstream: str, extra: dict) -> str:
    payload = {"cacheVer": CACHE_VER, "scripts": script_hash(), "step": kind,
               "upstream": upstream, "extra": extra}
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False)
                        .encode("utf-8")).hexdigest()


def _link(src: Path, dst: Path) -> None:
    """硬链接优先(同盘零拷贝),失败退复制。"""
    dst.unlink(missing_ok=True)
    try:
        os_link = getattr(__import__("os"), "link")
        os_link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def prune_seg_cache(cache_dir: Path) -> None:
    files = sorted(cache_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    for old in files[:-SEG_CACHE_KEEP] if len(files) > SEG_CACHE_KEEP else []:
        old.unlink(missing_ok=True)


# v0.14(ADR-0031):抠像与背景合成整体移除 —— 用户须在交付前自行抠好并合成背景。
# 本脚本不再有任何色度键控/背景替换/alpha 探针代码;素材是否仍为幕布由 S0
# rs_greenscreen 检测并在 rs_ingest / rs_verify 门禁。历史键控实现见 git 历史
# (v0.13 及以前)与 docs/adr/0029-绿幕键控v2.md(已作废)。


# ---------------- 步骤 2:逐段提取(带 seg 缓存) ----------------

def step_segment(doc: dict, ratio: str, build: Path, base_dir: Path, cfg: dict,
                 warnings: list[str], use_cache: bool = True, dry_run: bool = False
                 ) -> tuple[list[Path], list[dict], list[str]]:
    cw, ch = RATIO[ratio]
    fps = doc["fps"]
    base_clips = [t for t in doc["tracks"] if t["kind"] == "video"][0]["clips"]
    # v0.10(ADR-0023):转场计划在 segment 步就要用 —— 段 i 渲染时多取 tails[i] 尾帧,
    # 供 step_concat 的 xfade/acrossfade 重叠消费,时间模型才零漂移。
    eff_tr, forced_off, tr_reasons = _resolve_transitions(base_clips, fps, doc, cfg)
    tails = [eff_tr[i + 1] if i + 1 < len(base_clips) else 0.0
             for i in range(len(base_clips))]
    if forced_off:
        warnings.append("segment:部分衔接点源间隙放不下转场尾帧,已整体弃用转场走无损 concat")
    for r in tr_reasons:
        warnings.append(f"segment:{r}")
    cache_dir = build / "segcache"
    plan_doc = load_reframe_plan(base_dir)   # M8 vlog:reframe_plan 缺失/坏档 = 全走旧 cover_crop 档
    seg_files: list[Path] = []
    seg_keys: list[str] = []
    reports: list[dict] = []

    for i, clip in enumerate(base_clips):
        clip = {**clip, "tailMs": round(tails[i] * 1000)}   # 入键:尾帧变化必须换缓存键
        pr = probe_clip(clip, base_dir, cfg)
        src = Path(pr["path"])
        # M9(ADR-0050):带 matte 的 clip 先前景/背景预合成;中间产物替换 src,
        # 指纹随之变化 → 段缓存键自动换挡(旧缓存不幽灵命中)。
        if clip.get("matte") and pr["type"] == "video":
            matted = matte_precompose(clip, src, pr, build, base_dir, fps, cfg, warnings)
            if matted is not None:
                pr = {**pr, "path": str(matted)}
                src = matted
        fp = media_fingerprint(src)
        # M8 vlog:reframe_plan 命中判定(纯解析,不碰 ffmpeg;ratio 错配在这里即报错)
        plan_vf, plan_key, plan_tag = ((None, "", "") if pr["type"] == "image"
                                       else _resolve_reframe(clip, i, pr, cw, ch, fps,
                                                             plan_doc, warnings))
        if plan_key:
            clip["_planKey"] = plan_key          # plan 内容进段缓存键:方案一改该段必重渲
        key = seg_key(clip, doc, cw, ch, fp)
        seg_keys.append(key)
        cached = cache_dir / f"{key}.mp4"
        hit = use_cache and cached.is_file()
        reports.append({"i": i, "key": key[:10], "hit": hit, "path": str(cached),
                        "reframe": plan_tag})   # M8:该段 reframe 应用档位(traceability)
        if hit:
            seg_files.append(cached)
            continue
        seg_files.append(cached)
        if dry_run:
            continue
        cache_dir.mkdir(parents=True, exist_ok=True)

        speed = clip.get("speed", 1.0)
        out_ms = clip["durationMs"]
        anchor = clip.get("reframe", {}).get("anchorY", 0.5)
        # v0.10 帧量化:段边界吸附帧网格。±半帧的取整偏差原本散落在拼接边界上,
        # 是"每段首尾差一帧"类闪烁的隐形来源;量化后段长恒为整数帧。
        q_in_ms = round(clip.get("sourceInMs", 0) * fps / 1000.0) / fps * 1000.0
        q_out_ms = max(1, round(out_ms / 1000.0 * fps)) / fps * 1000.0
        take_s = (q_out_ms + tails[i] * 1000.0) / 1000.0 / speed

        tmp = cache_dir / f".tmp_{key}.mp4"
        cmd = [ffmpeg_bin(cfg), "-v", "error", "-y"]
        if pr["type"] == "image":
            cmd += ["-loop", "1", "-t", f"{out_ms/1000:.3f}", "-i", pr["path"]]
            vf = [f"scale={cw}:{ch}:force_original_aspect_ratio=decrease",
                  f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black"]
        else:
            loop = ["-stream_loop", "-1"] if clip.get("loop") else []
            # I7/v0.12 freezeMs:冻结帧补长(纯动画卡片比旁白短时)。**-t 只读到冻结
            # 起点(输入侧)**,之后 tpad 克隆尾帧补足——B8 教训:-t 放输出侧会把
            # tpad 补的帧整段截掉(安信德工程丢 20.4s 的事故形态)。
            freeze_ms = float(clip.get("freezeMs") or 0)
            read_s = min(take_s, freeze_ms / 1000.0) if freeze_ms > 0 else take_s
            cmd += loop + ["-ss", f"{q_in_ms / 1000:.3f}", "-t", f"{read_s:.3f}",
                           "-i", pr["path"]]
            # M8 vlog:plan 命中 → crop 裁切窗(窗比=目标比,绝不拉伸)+ scale;
            # 未命中/手动锚点 → 旧 cover_crop 档(行为与 M8 之前一致)。
            vf = plan_vf or [cover_crop(pr.get("width") or cw, pr.get("height") or ch,
                                        cw, ch, anchor)]
            if freeze_ms > 0:
                stop_s = max(0.0, take_s - read_s) + 0.5     # 补足请求时长 + 帧取整余量
                vf.append(f"tpad=stop_mode=clone:stop_duration={stop_s:.3f}")

        has_audio = pr.get("has_audio", False)
        vf_tail = [f"fps={fps}", "setsar=1"]

        motion = clip.get("motion", {})
        if motion.get("in", "none") != "none":
            if motion["in"] in ("scaleIn", "zoomIn"):
                warnings.append(f"seg[{i}]:motion.{motion['in']} v1 退化为 fadeIn")
            vf_tail.append(f"fade=t=in:st=0:d={motion.get('inMs', 400)/1000:.3f}")
        if motion.get("in") in ("slideInLeft", "slideInRight"):
            warnings.append(f"seg[{i}]:slideIn 属 overlay 动效,基轨段退化为 fadeIn")
            vf_tail.append(f"fade=t=in:st=0:d={motion.get('inMs', 400)/1000:.3f}")
        if motion.get("out", "none") != "none":
            d = motion.get("outMs", 400) / 1000
            vf_tail.append(f"fade=t=out:st={max(0.0, out_ms/1000 - d):.3f}:d={d:.3f}")
        # v0.11 R3 punch-in(ITERATION-GUIDE §5.3):切点处 1.25~1.5x 变焦交替构图,
        # 是 jump cut 的通行掩饰(Hitchcock 规则:更紧构图只给重点)。anchorY 决定
        # 纵向裁切偏置(0=贴顶,保护人物头部)。插在最前,fade 作用在变焦后的画面上。
        punch_f = float((clip.get("punchIn") or {}).get("factor", 0) or 0)
        if 1.01 < punch_f:
            punch_f = min(punch_f, 2.0)
            vf_tail.insert(0, f"scale={round(cw * punch_f / 2) * 2}:{round(ch * punch_f / 2) * 2},"
                              f"crop={cw}:{ch}:(iw-ow)/2:(ih-oh)*{anchor:.3f}")
        vf_tail.append("format=yuv420p")

        cmd += ["-vf", ",".join(vf + vf_tail)]

        if has_audio:
            af = (f"atrim=0:{take_s:.3f},asetpts=PTS-STARTPTS,"
                  f"volume={clip.get('volume', 1.0)},"
                  f"afade=t=in:st=0:d=0.008,"
                  f"afade=t=out:st={max(0.0, out_ms/1000 - 0.008):.3f}:d=0.008,"
                  f"aresample=48000,aformat=channel_layouts=stereo")
            cmd += ["-af", af, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        else:
            cmd += ["-an"]
        # dev-jj2815 实测(ffmpeg 2026-07-30 git master 回归):filter_complex 含
        # overlay 且视频输入 -ss 为 0/缺省时,输入 -t 被按"帧数 = t × time_base_den"
        # 解释——tb=1/600 的手机 HEVC 膨胀 20×(4.26s→85.33s),合成源 tb=1/15360
        # 膨胀 512×;-ss>0 或单输入 -vf 不触发。输出侧 -t 在编码器层钳制时长,
        # 与该怪癖解耦(speed=1 时等于输出时长)。
        cmd += ["-t", f"{take_s:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-r", str(fps), "-video_track_timescale", "15360", str(tmp)]
        p = run(cmd, timeout=int(__import__('os').environ.get('CUTFLOW_SEG_TIMEOUT', '1800')), cwd=str(cache_dir))
        if p.returncode != 0:
            die(4, "SEGMENT_FAIL", f"段 {i} 提取失败:{(p.stderr or '')[-400:]}")
        tmp.replace(cached)
        prune_seg_cache(cache_dir)
        print(f"  seg[{i+1}/{len(base_clips)}] {out_ms/1000:.2f}s"
              f"{' 缓存命中' if hit else ''}"
              f"{f' [reframe:{plan_tag}]' if plan_tag else ''}", file=sys.stderr)
    return seg_files, reports, seg_keys


# ---------------- 步骤 3:拼接(无转场=无损 concat;有转场=xfade 链) ----------------

def _resolve_transitions(base_clips: list[dict], fps: float, doc: dict,
                         cfg: dict | None) -> tuple[list[float], bool, list[str]]:
    """各相邻段的有效转场时长(s)、是否整体弃用、弃用原因列表。

    v0.10(ADR-0023):B2 的"tdur<1帧 → 整链弃用"让跳切全部退回硬切,用户实测
    读感为"人物瞬间闪烁消失再显示"。现在 0<tdur<1帧 的转场**提升为交叉溶解**
    (joinCrossfadeMs,默认 120ms;doc 级 `joinCrossfadeMs` 或 config.render 可覆),
    配合 step_segment 的尾帧扩展,xfade offset 恒等于后段名义起点 → 时间零漂移。
    整链弃用只留给"源间隙放不下尾帧"的 join(无法重叠就硬切,宁缺勿错)。
    `transition.type` 为 cut/none 仍表示显式硬切。

    v0.11(ADR-0026)三级语法:transition.reason ==
    - `jumpcut`:同段内跳切 → 1 帧软切(视觉即硬切,仅吃掉姿态/alpha pop 与爆音);
    - `topic`:真话题/章节切换 → 按 durMs 溶解(默认 300ms);
    - 无 reason(既有 IR)→ 原行为(亚帧提升 joinCrossfadeMs)。
    """
    frame_s = 1.0 / fps if fps and fps > 0 else 1 / 30.0
    cfg_ms = ((cfg or {}).get("render") or {}).get("joinCrossfadeMs", 120)
    promote_s = float(doc.get("joinCrossfadeMs", cfg_ms)) / 1000.0
    eff = [0.0] * len(base_clips)
    reasons: list[str] = []
    forced_off = False
    for i in range(1, len(base_clips)):
        tr = base_clips[i].get("transition")
        if not tr:
            continue
        if str(tr.get("type", "fade")).lower() in ("cut", "none"):
            continue
        cap = min(base_clips[i - 1]["durationMs"] / 2000.0,
                  base_clips[i]["durationMs"] / 2000.0)
        reason = str(tr.get("reason", "")).lower()
        if reason == "jumpcut":
            tdur = min(frame_s, cap)             # 软切:至多 1 帧
        else:
            tdur = min(tr.get("durMs", 500) / 1000.0, cap)
            if tdur < frame_s:
                tdur = min(promote_s, cap)
        if tdur <= 0:
            continue
        # 尾帧扩展余量:本段源出点与下一段源入点之间的被剪间隙
        prev, nxt = base_clips[i - 1], base_clips[i]
        gap_ms = nxt.get("sourceInMs", 0) - (prev.get("sourceInMs", 0) + prev["durationMs"])
        if gap_ms < tdur * 1000.0:
            reasons.append(f"join{i}:源间隙 {gap_ms:.0f}ms 放不下转场 {tdur * 1000:.0f}ms")
            forced_off = True
            continue
        eff[i] = tdur
    if forced_off and any(eff):
        eff = [0.0] * len(base_clips)   # xfade 链必须整链一致:一处放不下 → 全部走 concat
    return eff, forced_off, reasons


def step_concat(doc: dict, seg_files: list[Path], build: Path, cfg: dict,
                warnings: list[str]) -> Path:
    base_clips = [t for t in doc["tracks"] if t["kind"] == "video"][0]["clips"]
    fps = doc.get("fps", 30)
    eff_tr, forced_off, tr_reasons = _resolve_transitions(base_clips, fps, doc, cfg)
    tails = [eff_tr[i + 1] if i + 1 < len(base_clips) else 0.0
             for i in range(len(base_clips))]
    has_transition = any(t > 0 for t in eff_tr)
    for r in tr_reasons:
        warnings.append(f"concat:{r}")
    base = build / "base.mp4"

    if not has_transition:
        lst = build / "concat.txt"
        lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in seg_files), encoding="utf-8")
        p = run([ffmpeg_bin(cfg), "-v", "error", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(lst), "-c", "copy", str(base)])
        if p.returncode != 0:
            die(4, "CONCAT_FAIL", f"拼接失败:{(p.stderr or '')[-300:]}")
        return base

    # xfade 链(v0.10,ADR-0023):每段已在 segment 步多渲染 tails[i] 尾帧。
    # offset 恒等于后段名义起点 sum(qdurs[:i]) → 成片时长与字幕时间零漂移;
    # 末尾按名义总长裁齐(尾帧只进重叠,不外溢)。
    #
    # v0.11 实测修复(dev 工程视频 44.7s/音频 145s 截断):段文件的尾帧常被编码
    # 取整吃掉 1-2 帧,按名义 q+tail 递推会在链上累积缺口——xfade 在 input1 提前
    # EOF 时把**整条下游截断**。现在每段用实测流长递推:offset 保持名义值(零漂移
    # 不变),重叠不足时把该 join 的 duration 夹短到实际可用量,截断不可能发生。
    n = len(seg_files)
    qdurs = [max(1, round(c["durationMs"] / 1000.0 * fps)) / fps for c in base_clips[:n]]
    seg_lens = [_video_stream_len(f, cfg) for f in seg_files]
    cmd = [ffmpeg_bin(cfg), "-v", "error", "-y"]
    for f in seg_files:
        cmd += ["-i", str(f)]
    parts, last, lasta = [], "0:v", "0:a"
    frame = 1.0 / fps
    cum = seg_lens[0]
    nominal_cum = 0.0                       # 后段名义起点(零漂移锚点)
    all_have_audio = all(_seg_has_audio(f, cfg) for f in seg_files)
    if not all_have_audio:
        warnings.append("concat:部分段无音轨,转场仅作用于画面,输出将无音频")
    for i in range(1, n):
        tr = base_clips[i].get("transition") or {"type": "fade"}
        tdur = eff_tr[i]
        nominal_cum += qdurs[i - 1]
        offset = nominal_cum
        # 重叠可用量 = 实测累计长 - 名义 offset - 1 帧安全边际:
        # ① 足够 → 名义路径(offset/tdur 都零漂移);
        # ② 差 1~2 帧(尾帧被编码取整吃掉)→ 缩短本次 dissolve,切点仍零漂移;
        # ③ 连安全余量都没有 → 贴着实测末尾做 1 帧软切,保链不断(绝不截断下游)。
        # ⚠ 实测:offset+dur 恰好贴齐/越过 input1 长度时,该 ffmpeg 构建的 xfade
        # 会整段坍缩(offset 语义失效,输出只剩 input2)——边际 1 帧不可省。
        avail = cum - offset - frame
        if avail >= tdur:
            pass
        elif avail >= frame:
            tdur = avail
        else:
            offset = max(cum - frame - frame, 0.0)
            tdur = frame
        parts.append(f"[{last}][{i}:v]xfade=transition={tr.get('type', 'fade')}:"
                     f"duration={tdur:.3f}:offset={offset:.3f}[vx{i}]")
        if all_have_audio:
            parts.append(f"[{lasta}][{i}:a]acrossfade=d={tdur:.3f}:curve1=tri:curve2=tri[ax{i}]")
            lasta = f"ax{i}"
        last = f"vx{i}"
        cum = offset + seg_lens[i]
    total_s = sum(qdurs)
    cmd += ["-filter_complex", ";".join(parts), "-map", f"[{last}]"]
    cmd += ["-map", f"[{lasta}]", "-c:a", "aac", "-b:a", "192k"] if all_have_audio else ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-r", str(fps), "-t", f"{total_s:.3f}", str(base)]
    p = run(cmd, timeout=3600)
    if p.returncode != 0:
        die(4, "CONCAT_XFADE_FAIL", f"转场拼接失败:{(p.stderr or '')[-500:]}")
    return base


def _seg_has_audio(seg: Path, cfg: dict) -> bool:
    info = ffprobe_json(seg, cfg)
    return any(s["codec_type"] == "audio" for s in info.get("streams", []))


def _video_stream_len(seg: Path, cfg: dict) -> float:
    """视频流实际时长(s)。容器时长含音频垫尾不可靠;流缺 duration 时退 nb_frames/fps。"""
    info = ffprobe_json(seg, cfg)
    vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    d = float(vs.get("duration") or 0.0)
    if d > 0:
        return d
    nf = int(vs.get("nb_frames") or 0)
    if nf > 0:
        num, _, den = (vs.get("r_frame_rate") or "30/1").partition("/")
        try:
            return nf * den / float(num or 30)
        except (ZeroDivisionError, ValueError):
            return nf / 30.0
    return float(info.get("format", {}).get("duration") or 0.0)


# ---------------- 步骤 4:overlay 合成 ----------------

def _overlay_exprs(clip: dict, cw: int, ch: int, start_s: float) -> tuple[str, str, str]:
    """返回 (target_w, x_expr, y_expr)。position=overlay 中心点(归一化),用 W/H/w/h
    表达式定位,任意 overlay 尺寸都正确居中;slide 为线性滑入。"""
    scale = clip.get("scale", 1.0)
    px = clip.get("position", {}).get("x", 0.5)
    py = clip.get("position", {}).get("y", 0.5)
    w = int(cw * scale)
    x_c = f"{px:.4f}*W-w/2"
    y_c = f"{py:.4f}*H-h/2"
    motion = clip.get("motion", {})
    in_d = motion.get("inMs", 400) / 1000
    x, y = x_c, y_c
    if motion.get("in") == "slideInLeft":
        x = (f"if(lt(t,{start_s + in_d:.3f}),"
             f"-w+({w}+{px:.4f}*W-w/2)*(t-{start_s:.3f})/{in_d:.3f},{x_c})")
    elif motion.get("in") == "slideInRight":
        x = (f"if(lt(t,{start_s + in_d:.3f}),"
             f"W+({x_c}-{cw})*(t-{start_s:.3f})/{in_d:.3f},{x_c})")
    return str(w), x, y


def step_compose(doc: dict, ratio: str, base: Path, build: Path, base_dir: Path,
                 cfg: dict, warnings: list[str]) -> Path:
    cw, ch = RATIO[ratio]
    overlay_tracks = [t for t in doc["tracks"] if t["kind"] == "video"][1:]
    if not overlay_tracks:
        return base

    cmd = [ffmpeg_bin(cfg), "-v", "error", "-y", "-i", str(base)]
    parts, last, idx = [], "0:v", 1
    for t in overlay_tracks:
        for clip in t["clips"]:
            pr = probe_clip(clip, base_dir, cfg)
            start_s = clip["startMs"] / 1000
            dur_s = clip["durationMs"] / 1000
            if pr["type"] == "image":
                cmd += ["-loop", "1", "-t", f"{dur_s:.3f}", "-i", pr["path"]]
            else:
                cmd += ["-ss", f"{clip.get('sourceInMs', 0)/1000:.3f}",
                        "-t", f"{dur_s:.3f}", "-i", pr["path"]]
            chain = []
            # v0.10(ADR-0025):支持 clip.overlay={x,y,w,h,opacity} 绝对像素定位
            # (rs_brand 变体轨的产出;v0.9 该字段无人消费,Logo 会以 scale 默认值贴满画布)。
            # scale/position(相对画幅)仍是通用路径,overlay 存在时优先。
            ov = clip.get("overlay") or {}
            if ov.get("w"):
                w = int(ov["w"])
                x, y = f"{int(ov.get('x', 0))}", f"{int(ov.get('y', 0))}"
                scale_expr = (f"scale={w}:{int(ov['h'])}" if ov.get("h")
                              else f"scale={w}:-2")
            else:
                w, x, y = _overlay_exprs(clip, cw, ch, start_s)
                scale_expr = f"scale={w}:-2"
            motion = clip.get("motion", {})
            if motion.get("in") == "fadeIn":
                chain.append(f"fade=t=in:st=0:d={motion.get('inMs', 400)/1000:.3f}:alpha=1")
            if motion.get("out") == "fadeOut":
                d = motion.get("outMs", 400) / 1000
                chain.append(f"fade=t=out:st={max(0.0, dur_s - d):.3f}:d={d:.3f}:alpha=1")
            chain.append(scale_expr)
            opacity = float(ov.get("opacity", clip.get("opacity", 1.0)) or 1.0)
            if opacity < 1.0:
                chain.append(f"format=rgba,colorchannelmixer=aa={opacity:.3f}")
            chain += ["format=yuva420p",
                      f"setpts=PTS-STARTPTS+{start_s:.3f}/TB"]
            parts.append(f"[{idx}:v]{','.join(chain)}[ov{idx}]")
            parts.append(f"[{last}][ov{idx}]overlay=x='{x}':y='{y}':"
                         f"enable='between(t,{start_s:.3f},{start_s + dur_s:.3f})'[cmp{idx}]")
            last = f"cmp{idx}"
            idx += 1

    out = build / "composed.mp4"
    cmd += ["-filter_complex", ";".join(parts), "-map", f"[{last}]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "copy",
            "-r", str(doc["fps"]), str(out)]
    p = run(cmd, timeout=3600)
    if p.returncode != 0:
        die(4, "COMPOSE_FAIL", f"合成失败:{(p.stderr or '')[-500:]}")
    return out


# ---------------- 步骤 5:混音 ----------------

def _voice_chain(clip: dict) -> list[str]:
    """单个人声 clip 的音频滤镜链(v0.8.1 修 Bug:先裁后延)。

    旧实现 `adelay` 在前、`atrim=0:dur` 在后:atrim 裁的是**延迟后**流的前 dur 毫秒
    —— startMs>0 的段会被裁掉 startMs 毫秒内容,甚至整段只剩静音(实测音轨缩到 13.9s)。
    正确顺序:**先 atrim 取本段时长,再 adelay 推到时间轴位置**。
    """
    chain = ["aresample=48000", "aformat=channel_layouts=stereo"]
    if clip.get("role") == "voice":
        chain.append(LOUDNORM_VOICE)
    chain.append(f"volume={clip.get('volume', 1.0)}")
    if clip.get("durationMs"):
        chain.append(f"atrim=0:{clip['durationMs']/1000:.3f}")
    chain.append(f"adelay={int(clip['startMs'])}|{int(clip['startMs'])}")
    return chain


def step_mix(doc: dict, src: Path, build: Path, base_dir: Path, cfg: dict) -> Path:
    audio_tracks = [t for t in doc["tracks"] if t["kind"] == "audio"]
    bgm = doc.get("bgm", {})
    if not audio_tracks and not bgm.get("src"):
        return src

    info = ffprobe_json(src, cfg)
    total_s = float(info["format"]["duration"])
    cmd = [ffmpeg_bin(cfg), "-v", "error", "-y", "-i", str(src)]
    parts, mixes, idx = [], [], 1
    for t in audio_tracks:
        for clip in t["clips"]:
            sp = clip_path(clip, base_dir)
            # B1(BUGREPORT-20260913):人声 clip 的 sourceInMs 必须在**输入侧寻址**,
            # 否则 _voice_chain 的 atrim=0:dur 永远从源文件 0s 取 —— 粗剪后的多段
            # 人声(每段 sourceInMs>0)每段都重播片头,amix 叠加后音画全错。
            # 旧绕过法(按 keep 预抽 voice_full.wav 单 clip)仍有效,但不再必需。
            if clip.get("sourceInMs"):
                cmd += ["-ss", f"{clip['sourceInMs'] / 1000:.3f}", "-i", str(sp)]
            else:
                cmd += ["-i", str(sp)]
            chain = _voice_chain(clip)
            parts.append(f"[{idx}:a]{','.join(chain)}[a{idx}]")
            mixes.append(f"[a{idx}]")
            idx += 1

    if bgm.get("src"):
        sp = Path(bgm["src"])
        sp = sp if sp.is_absolute() else base_dir / sp
        cmd += ["-stream_loop", "-1", "-i", str(sp)]
        bgm_chain = (f"atrim=0:{total_s:.3f},volume={bgm.get('gainDb', -18)}dB,"
                     f"aresample=48000,aformat=channel_layouts=stereo")
        ducking = bgm.get("ducking", True) and mixes
        if ducking:
            # B3(v0.12):filtergraph 标签**只能被消费一次**——旧写法把 [a1] 同时喂给
            # sidechaincompress 与 amix → `MIX_FAIL: Stream specifier 'a1' matches
            # no streams`(且只给 [a1] asplit 也只 duck 第一段人声)。正确做法:
            # 先把**全部人声**合成一条总线,再 asplit 出 duck 侧链与正式混音两路。
            parts.append(f"[{idx}:a]{bgm_chain}[bgraw]")
            parts.append("".join(mixes)
                         + f"amix=inputs={len(mixes)}:duration=longest:normalize=0[voice]")
            parts.append("[voice]asplit=2[voice_m][voice_d]")
            parts.append("[bgraw][voice_d]"
                         "sidechaincompress=threshold=0.02:ratio=6:attack=60:release=500[bgm]")
            graph = (";".join(parts)
                     + ";[voice_m][bgm]amix=inputs=2:duration=first:normalize=0[mix]")
        else:
            parts.append(f"[{idx}:a]{bgm_chain}[bgm]")
            n_in = len(mixes) + 1
            graph = ";".join(parts) + ";" + "".join(mixes) + "[bgm]" + \
                f"amix=inputs={n_in}:duration=first:normalize=0[mix]"
    else:
        graph = ";".join(parts) + ";" + "".join(mixes) + \
            f"amix=inputs={len(mixes)}:duration=longest:normalize=0[mix]"

    out = build / "mixed.mkv"
    cmd += ["-filter_complex", graph, "-map", "0:v", "-map", "[mix]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)]
    p = run(cmd, timeout=3600)
    if p.returncode != 0:
        die(4, "MIX_FAIL", f"混音失败:{(p.stderr or '')[-500:]}")
    return out


# ---------------- 步骤 6:字幕(最后叠) ----------------

def step_subtitle(doc: dict, src: Path, build: Path, cfg: dict,
                  warnings: list[str] | None = None) -> Path:
    sub = doc.get("subtitle", {})
    ass = sub.get("ass")
    if not ass:
        # B4(v0.12):ass 缺失 = 本次**不会烧录字幕**——第一版"成功渲染"的成片
        # 无字幕,靠 L1 抽帧才被发现。subtitle.ass 才是烧录字段(rules/compose.md),
        # source 仅作溯源;有 source 没 ass 必须显式 WARN,不许静默。
        if sub.get("source") and warnings is not None:
            warnings.append("IR 有 subtitle.source 但无 subtitle.ass → 本次不会烧录字幕"
                            "(subtitle.ass 才是烧录字段)")
        return src
    p = Path(ass)
    if not p.is_absolute():
        p = Path(doc.get("_base_dir", ".")) / p
    out = build / "subtitled.mp4"
    cmd = [ffmpeg_bin(cfg), "-v", "error", "-y", "-i", str(src),
           "-vf", f"ass='{esc_sub(p)}'", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "18", "-c:a", "copy", str(out)]
    r = run(cmd, timeout=3600)
    if r.returncode != 0:
        die(4, "SUBTITLE_FAIL", f"字幕烧录失败:{(r.stderr or '')[-400:]}")
    return out


# ---------------- 步骤 7:总线响度 + 编码 ----------------

def measure_loudness(src: Path, cfg: dict) -> dict | None:
    """loudnorm 测量 pass(音频-only 解码,秒级)。返回 measured 字典或 None(无音轨/测量失败)。

    v0.11 R1:单 pass loudnorm 动态受输入响度牵动(EBU R128 的 linear 双 pass 才是
    交付口径,ITERATION-GUIDE §8.2);final 档先测后编,preview/draft 维持单 pass。
    """
    p = subprocess.run(
        [ffmpeg_bin(cfg), "-v", "info", "-i", str(src),
         "-af", "loudnorm=I=-14:TP=-1.0:LRA=11:print_format=json",
         "-vn", "-f", "null", "-"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=3600)
    if p.returncode != 0:
        return None
    m = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', p.stderr or "")
    if not m:
        return None
    try:
        d = json.loads(m[-1])
        if float(d.get("input_i", -70)) <= -69.0:      # 静音地板 = 无有效音轨
            return None
        return d
    except (json.JSONDecodeError, ValueError):
        return None


def step_encode(src: Path, out: Path, profile: str, cfg: dict, fps: int,
                loudness: dict | None = None) -> None:
    crf = {"final": "19", "preview": "26", "draft": "28"}[profile]
    preset = {"final": "medium", "preview": "veryfast", "draft": "ultrafast"}[profile]
    if profile == "final" and loudness:
        # 双 pass(linear=true):用实测值回填,动态不被单 pass 的归一化曲线拉花。
        af = (f"loudnorm=I=-14:TP=-1.0:LRA=11:"
              f"measured_I={loudness['input_i']}:measured_TP={loudness['input_tp']}:"
              f"measured_LRA={loudness['input_lra']}:measured_thresh={loudness['input_thresh']}:"
              f"offset={loudness.get('target_offset', 0)}:linear=true")
    else:
        af = LOUDNORM_BUS
    cmd = [ffmpeg_bin(cfg), "-v", "error", "-y", "-i", str(src),
           "-af", af, "-c:v", "libx264", "-preset", preset, "-crf", crf,
           "-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", str(out)]
    p = run(cmd, timeout=7200)
    if p.returncode != 0:
        die(4, "ENCODE_FAIL", f"编码失败:{(p.stderr or '')[-400:]}")


# ---------------- 主流程 ----------------

def render(doc: dict, project_path: Path, ratio: str, profile: str, *,
           use_cache: bool = True, dry_run: bool = False, clear_cache: bool = False,
           out_override: str = "") -> dict:
    cfg = load_config()
    # dev-jj2815 实测:IR 用相对路径传入时,seg/输出路径全为相对,concat demuxer
    # 以 concat.txt 所在目录为基准再拼一次 → 路径双重拼接打不开。入口即绝对化。
    base_dir = project_path.parent.parent.resolve()  # 05_时间线工程/ → 工程根
    doc["_base_dir"] = str(base_dir)
    build = base_dir / rs_paths.resolve_name(base_dir, "output") / "_build" / ratio
    build.mkdir(parents=True, exist_ok=True)
    keys_path = build / "step_keys.json"
    prev: dict = {}
    if keys_path.is_file():
        try:
            prev = json.loads(keys_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}
    if clear_cache:
        cache_dir = build / "segcache"
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir, ignore_errors=True)
        prev = {}
    t0 = time.time()
    warnings: list[str] = []
    skipped: list[str] = []

    # v0.10(ADR-0023):尾帧扩展法下转场**不再吞时长**(offset=后段名义起点),
    # 音频/字幕时间轴零漂移,旧"预扣转场消耗"警告作废。仅当提升被禁用且存在
    # 亚帧转场时提示回退硬切。
    base_clips = [t for t in doc["tracks"] if t["kind"] == "video"][0]["clips"]
    eff_warn, off_warn, reasons_warn = _resolve_transitions(base_clips, doc.get("fps", 30), doc, cfg)
    if off_warn:
        warnings.append("时间轴提示:部分衔接点源间隙放不下转场尾帧,已回退无损 concat(硬切):"
                        + ";".join(reasons_warn))

    segs, seg_reports, seg_keys = step_segment(doc, ratio, build, base_dir, cfg, warnings,
                                               use_cache=use_cache, dry_run=dry_run)

    # -- 步骤键链:上游键 + 本步参数(v0.6.0 增量) --
    k_concat = step_key("concat", "|".join(seg_keys),
                        {"transitions": [c.get("transition") for c in base_clips]})
    base = build / "base.mp4"
    if not dry_run and use_cache and prev.get("concat") == k_concat and base.is_file():
        skipped.append("concat")
    elif not dry_run:
        base = step_concat(doc, segs, build, cfg, warnings)

    overlay_def = []
    for t in [x for x in doc["tracks"] if x["kind"] == "video"][1:]:
        for c in t["clips"]:
            cp = clip_path(c, base_dir)
            fp = media_fingerprint(cp) if cp.is_file() else "missing"
            overlay_def.append([str(cp), fp, {k: c.get(k) for k in
                                              ("startMs", "durationMs", "sourceInMs", "scale",
                                               "position", "motion",
                                               "overlay", "opacity")}])
    k_compose = step_key("compose", k_concat, {"overlays": overlay_def})
    composed = build / "composed.mp4"
    if [t for t in doc["tracks"] if t["kind"] == "video"][1:]:
        if not dry_run and use_cache and prev.get("compose") == k_compose and composed.is_file():
            skipped.append("compose")
        elif not dry_run:
            composed = step_compose(doc, ratio, base, build, base_dir, cfg, warnings)
    else:
        composed = base

    audio_def = []
    for t in [x for x in doc["tracks"] if x["kind"] == "audio"]:
        for c in t["clips"]:
            cp = clip_path(c, base_dir)
            audio_def.append([str(cp), media_fingerprint(cp) if cp.is_file() else "missing",
                              {k: c.get(k) for k in ("startMs", "durationMs", "sourceInMs",
                                                     "volume", "role")}])
    bgm_def = doc.get("bgm") or {}
    k_mix = step_key("mix", k_compose, {"audio": audio_def, "bgm": bgm_def})
    mixed = build / "mixed.mkv"
    if audio_def or bgm_def.get("src"):
        if not dry_run and use_cache and prev.get("mix") == k_mix and mixed.is_file():
            skipped.append("mix")
        elif not dry_run:
            mixed = step_mix(doc, composed, build, base_dir, cfg)
    else:
        mixed = composed

    ass_path = None
    if doc.get("subtitle", {}).get("ass"):
        ass_path = Path(doc["subtitle"]["ass"])
        if not ass_path.is_absolute():
            ass_path = base_dir / ass_path
    k_sub = step_key("subtitle", k_mix,
                     {"ass": hashlib.sha1(ass_path.read_bytes()).hexdigest()[:16]
                      if ass_path and ass_path.is_file() else None})
    subtitled = build / "subtitled.mp4"
    if ass_path:
        if not dry_run and use_cache and prev.get("subtitle") == k_sub and subtitled.is_file():
            skipped.append("subtitle")
        elif not dry_run:
            subtitled = step_subtitle(doc, mixed, build, cfg)
    else:
        subtitled = mixed
        # B4(v0.12):IR 只写 subtitle.source 没写 subtitle.ass 时,字幕静默不烧。
        # 这里是主流程的实际跳过点,必须 WARN 留痕(与 step_subtitle 内防御同文案)。
        if doc.get("subtitle", {}).get("source"):
            warnings.append("IR 有 subtitle.source 但无 subtitle.ass → 本次不会烧录字幕"
                            "(subtitle.ass 才是烧录字段)")

    name = f"final_{doc.get('slug', 'out')}_{ratio.replace('x', '')}.mp4" if profile == "final" \
        else f"{profile}_{doc.get('slug', 'out')}_{ratio.replace('x', '')}.mp4"
    # P10b-1:final 档(交付成片)默认落 06_成片输出/final/ 独占子目录,与品牌变体
    #(06_成片输出/branded/)彻底隔离,消除顶层 glob 交叠互相打脏的历史;
    # preview/draft 是探针/中间档,留在 06_成片输出 顶层(归 rs_cleanup 清理)。
    # rs_brand 变体渲染传 --out 显式指定落点(变体 IR 的 base_dir 是 branded/,不适用上规)。
    if out_override:
        ov = Path(out_override)
        out = ov if ov.is_absolute() else Path.cwd() / ov
    else:
        out_name = rs_paths.resolve_name(base_dir, "output")
        out_dir = (base_dir / out_name / "final") if profile == "final" \
            else (base_dir / out_name)
        out = out_dir / name
    # v0.11 R1:final 档双 pass loudnorm —— 先音频-only 测量(秒级),实测值回填编码。
    # 测量值是 subtitled 的纯函数(已在 k_sub 里),键只需记"是否双 pass"。
    loud2 = measure_loudness(subtitled, cfg) if profile == "final" else None
    if profile == "final" and loud2 is None:
        warnings.append("loudness: 未测得有效音轨,回退单 pass 总线响度")
    k_enc = step_key("encode", k_sub, {"profile": profile, "fps": doc["fps"],
                                       "loud2pass": bool(loud2)})
    if dry_run:
        hits = {"concat": prev.get("concat") == k_concat and base.is_file(),
                "compose": prev.get("compose") == k_compose and (composed.is_file() if composed != base else prev.get("concat") == k_concat and base.is_file()),
                "mix": prev.get("mix") == k_mix and (mixed.is_file() if mixed != composed else True),
                "subtitle": prev.get("subtitle") == k_sub and (subtitled.is_file() if subtitled != mixed else True),
                "encode": prev.get("encode") == k_enc and out.is_file()}
        return {"dryRun": True, "segs": seg_reports,
                "segHits": sum(1 for r in seg_reports if r["hit"]),
                "segTotal": len(seg_reports), "steps": hits,
                "output": str(out)}
    if use_cache and prev.get("encode") == k_enc and out.is_file():
        skipped.append("encode")
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        step_encode(subtitled, out, profile, cfg, doc["fps"], loudness=loud2)

    # B2 回归断言:成片视频流时长 vs IR 名义总长差 ≤1.5 帧。
    # v0.10 尾帧扩展法下转场不吞时长,名义总长即预期;字段名错/漂移仍会在此暴露。
    try:
        info_v = ffprobe_json(out, cfg)
        vs = next((s for s in info_v.get("streams", []) if s["codec_type"] == "video"), None)
        vdur = float((vs or {}).get("duration") or 0)
        if vdur > 0:
            expected_ms = sum(c["durationMs"] for c in base_clips)
            drift_ms = abs(vdur * 1000 - expected_ms)
            if drift_ms > 1500.0 / doc.get("fps", 30):     # 1.5 帧容差
                warnings.append(
                    f"音画对齐断言:成片视频流 {vdur:.2f}s vs IR 预期 {expected_ms/1000:.2f}s"
                    f"(差 {drift_ms:.0f}ms > 1.5 帧)——转场吞时或漂移,排查后再交付(BUGREPORT B2)")
    except Exception as exc:  # noqa: BLE001 — 断言失败不阻塞产出,但必须留痕
        warnings.append(f"音画对齐断言探测失败:{exc}")

    if use_cache:
        keys_path.write_text(json.dumps(
            {"concat": k_concat, "compose": k_compose, "mix": k_mix,
             "subtitle": k_sub, "encode": k_enc, "segs": seg_keys},
            ensure_ascii=False, indent=1), encoding="utf-8")
    return {"output": str(out), "duration_s": round(media_duration_s(out, cfg), 2),
            "elapsed_s": round(time.time() - t0, 1), "profile": profile, "warnings": warnings,
            "cache": {"segHits": sum(1 for r in seg_reports if r["hit"]),
                      "segTotal": len(seg_reports), "stepsSkipped": skipped},
            "segs": seg_reports}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--ratio", default=None, choices=list(RATIO))
    ap.add_argument("--profile", default="final", choices=["final", "preview", "draft"])
    ap.add_argument("--ass", default=None, help="覆盖 IR 的字幕 ass 路径(每比例各一个 ass)")
    ap.add_argument("--out", default="",
                    help="显式输出文件路径(P10b-1:rs_brand 变体渲染用;缺省按 profile "
                         f"落 {rs_paths.p('output')}[/final]/)")
    ap.add_argument("--explain", action="store_true",
                    help="只报告 seg/步骤缓存命中情况,不执行渲染")
    ap.add_argument("--clear-cache", action="store_true", help="清除该工程的 seg 缓存后渲染")
    ap.add_argument("--no-cache", action="store_true", help="忽略已有缓存,全部重渲")
    a = ap.parse_args()
    p = Path(a.project)
    if not p.is_file():
        return emit(False, "NO_PROJECT", f"IR 不存在:{p}", exit_code=2)
    doc = json.loads(p.read_text(encoding="utf-8"))
    import rs_ir
    errs = rs_ir.validate(doc, p.parent.parent)
    if errs:
        return emit(False, "IR_INVALID", f"渲染前校验失败 {len(errs)} 项", {"errors": errs}, exit_code=2)
    canvas = f"{doc['canvas']['width']}x{doc['canvas']['height']}"
    try:
        ratio = a.ratio or ratio_for_canvas(doc["canvas"]["width"], doc["canvas"]["height"])
    except ValueError as exc:
        return emit(False, "BAD_CANVAS", str(exc), exit_code=2)
    if a.ass:
        doc.setdefault("subtitle", {})["ass"] = a.ass
    data = render(doc, p, ratio, a.profile,
                  use_cache=not a.no_cache, dry_run=a.explain, clear_cache=a.clear_cache,
                  out_override=a.out)
    if a.explain:
        msg = (f"[explain] seg 命中 {data['segHits']}/{data['segTotal']};"
               f"步骤命中:" + ",".join(k for k, v in data["steps"].items() if v))
        return emit(True, "RENDER_EXPLAIN", msg, data)
    cache = data.pop("cache")
    msg = (f"渲染完成:{data['output']}({data['duration_s']}s, {data['elapsed_s']}s;"
           f"seg 命中 {cache['segHits']}/{cache['segTotal']}"
           + (f",步骤跳过:{','.join(cache['stepsSkipped'])}" if cache["stepsSkipped"] else "") + ")")
    return emit(True, "RENDER_OK", msg, data)


if __name__ == "__main__":
    sys.exit(main())
