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
  · 基轨绿幕:clip.chroma + clip.background(色块/图片/视频/lavfi 动态渐变),
    抠像在 segment 步完成(逐段合成背景,天然支持缓存)。
  · --explain 逐段/逐步报告命中情况(不执行);--clear-cache 清 seg 缓存。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import (RATIOS, die, emit, ffmpeg_bin, ffprobe_json, load_config,  # noqa: E402
                       media_duration_s, ratio_for_canvas, run)

RATIO = dict(RATIOS)             # 画幅唯一真相源在 rs_common(新增画幅只改那里)
LOUDNORM_BUS = "loudnorm=I=-14:TP=-1.0:LRA=11"
LOUDNORM_VOICE = "loudnorm=I=-16:TP=-1.5:LRA=11"
CACHE_VER = "v2"                 # 渲染语义变更时 +1,防旧缓存幽灵命中
SEG_CACHE_KEEP = 400             # segcache 最大保留文件数(超出按 mtime 淘汰)
BG_TYPES = {"color", "image", "video", "gradient"}


def esc_sub(path: Path) -> str:
    """ASS 路径转 ffmpeg filter 转义(Windows 盘符冒号)。"""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def cover_crop(iw: int, ih: int, cw: int, ch: int, anchor_y: float = 0.5) -> str:
    """放大到覆盖画布,再按 anchorY 纵向裁切(重构图)。"""
    return (f"scale={cw}:{ch}:force_original_aspect_ratio=increase,"
            f"crop={cw}:{ch}:(iw-{cw})/2:(ih-{ch})*{anchor_y}")


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
                  "reframe", "motion", "chroma", "background")},
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


# ---------------------------------------------------------------- 绿幕

def sample_chroma(src: Path, cfg: dict) -> str:
    """从素材边缘 4 点自动采绿幕色(取绿色主导点的均值)。

    采样点避开常见天花板/地板:左右两侧 12% 与 45% 高度。失败则要求显式指定 color。
    """
    pr = ffprobe_json(src, cfg)
    vs = next((s for s in pr["streams"] if s["codec_type"] == "video"), None)
    if not vs:
        die(4, "CHROMA_NO_VIDEO", f"无法采样色度(无视频流):{src}")
    w, h = int(vs["width"]), int(vs["height"])
    pts = [(0.03, 0.12), (0.97, 0.12), (0.03, 0.45), (0.97, 0.45)]
    samples: list[tuple[int, int, int]] = []
    for fx, fy in pts:
        x, y = max(0, int(w * fx) - 8), max(0, int(h * fy) - 8)
        tmp = src.with_suffix(f".chroma_{fx}_{fy}.pam")
        p = run([ffmpeg_bin(cfg), "-v", "error", "-y", "-ss", "1", "-i", str(src),
                 "-vf", f"crop=16:16:{x}:{y},format=rgb24", "-frames:v", "1", str(tmp)])
        if p.returncode != 0 or not tmp.is_file():
            continue
        raw = tmp.read_bytes()
        tmp.unlink(missing_ok=True)
        end = raw.find(b"ENDHDR\n")
        if end < 0:
            continue
        px = raw[end + 7:]
        if len(px) < 48:
            continue
        mid = px[24:27]  # 中心像素
        samples.append((mid[0], mid[1], mid[2]))
    greens = [s for s in samples if s[1] > 100 and s[1] > s[0] * 1.30 and s[1] > s[2] * 1.30]
    if not greens:
        die(4, "CHROMA_SAMPLE_FAIL",
            f"边缘采样未找到绿幕(样本:{samples});请在 clip.chroma.color 显式指定,如 0x2AA81E")
    r = sum(s[0] for s in greens) // len(greens)
    g = sum(s[1] for s in greens) // len(greens)
    b = sum(s[2] for s in greens) // len(greens)
    return f"0x{r:02X}{g:02X}{b:02X}"


def chroma_hex(c: dict, src: Path, cfg: dict) -> str:
    col = c.get("color", "auto")
    if isinstance(col, str) and col.startswith("0x"):
        return col
    if col == "blue":
        return "0x0000FF"
    if col == "green":
        return "0x00FF00"
    return sample_chroma(src, cfg)  # auto / 未知值 → 自动采样


def _crop_pct_chain(c: dict) -> list[str]:
    chain = []
    if c.get("cropTopPct"):
        chain.append(f"crop=iw:ih*{1 - float(c['cropTopPct']):.4f}:0:ih*{float(c['cropTopPct']):.4f}")
    if c.get("cropBottomPct"):
        pct = float(c["cropBottomPct"])
        chain.append(f"crop=iw:ih*{1 - pct:.4f}:0:0" if not c.get("cropTopPct")
                     else f"crop=iw:ih*{1 - pct:.4f}:0:0")
    return chain


# ---------------- 步骤 2:逐段提取(带 seg 缓存 + 基轨绿幕) ----------------

def step_segment(doc: dict, ratio: str, build: Path, base_dir: Path, cfg: dict,
                 warnings: list[str], use_cache: bool = True, dry_run: bool = False
                 ) -> tuple[list[Path], list[dict], list[str]]:
    cw, ch = RATIO[ratio]
    fps = doc["fps"]
    base_clips = [t for t in doc["tracks"] if t["kind"] == "video"][0]["clips"]
    cache_dir = build / "segcache"
    seg_files: list[Path] = []
    seg_keys: list[str] = []
    reports: list[dict] = []

    for i, clip in enumerate(base_clips):
        pr = probe_clip(clip, base_dir, cfg)
        src = Path(pr["path"])
        fp = media_fingerprint(src)
        key = seg_key(clip, doc, cw, ch, fp)
        seg_keys.append(key)
        cached = cache_dir / f"{key}.mp4"
        hit = use_cache and cached.is_file()
        reports.append({"i": i, "key": key[:10], "hit": hit, "path": str(cached)})
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
        take_s = out_ms / 1000.0 / speed

        chroma = clip.get("chroma")
        bg = clip.get("background") if chroma else None
        if bg and bg.get("type") not in BG_TYPES:
            die(2, "BG_TYPE_INVALID", f"seg[{i}]:background.type 非法:{bg.get('type')}(可选 {sorted(BG_TYPES)})")

        tmp = cache_dir / f".tmp_{key}.mp4"
        cmd = [ffmpeg_bin(cfg), "-v", "error", "-y"]
        if pr["type"] == "image":
            cmd += ["-loop", "1", "-t", f"{out_ms/1000:.3f}", "-i", pr["path"]]
            vf = [f"scale={cw}:{ch}:force_original_aspect_ratio=decrease",
                  f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black"]
        else:
            loop = ["-stream_loop", "-1"] if clip.get("loop") else []
            cmd += loop + ["-ss", f"{clip.get('sourceInMs', 0)/1000:.3f}", "-t", f"{take_s:.3f}",
                           "-i", pr["path"]]
            vf = [cover_crop(pr.get("width") or cw, pr.get("height") or ch, cw, ch, anchor)]

        has_audio = pr.get("has_audio", False)
        fparts: list[str] = []
        if bg:
            # 前景:裁边 → 抠像 → 去绿边 → 覆盖画布
            fg = [x for x in _crop_pct_chain(chroma) if x]
            # dev-jj2815 实测(ffmpeg 2026-07-30 git master 回归):chromakey 输出的
            # alpha 全坏(人物区域≈0,YAVG 2.07/255;alphaextract 实测),叠任何背景
            # 都是"幽灵人物";-vf 单输入 + JPG 因丢弃 alpha 而看不出来。
            # colorkey(RGB 距离键控)alpha 正常(人物 255/背景 0),改用之。
            fg.append(f"colorkey={chroma_hex(chroma, src, cfg)}:"
                      f"{chroma.get('similarity', 0.24)}:{chroma.get('blend', 0.12)}")
            if chroma.get("despill", True):
                fg.append("despill=type=green")
            fg.append(cover_crop(pr.get("width") or cw, pr.get("height") or ch, cw, ch, anchor))
            fparts.append(f"[0:v]{','.join(fg)}[fg]")
            # 背景:四种来源统一覆盖画布
            btype = bg.get("type")
            if btype == "color":
                cmd += ["-f", "lavfi", "-t", f"{take_s:.3f}", "-i",
                        f"color=c={bg.get('value', '0x101820')}:s={cw}x{ch}:r={fps}"]
                bchain = "null"
            elif btype == "gradient":
                cmd += ["-f", "lavfi", "-t", f"{take_s:.3f}", "-i",
                        f"gradients=s={cw}x{ch}:c0={bg.get('from', '0x0F2027')}"
                        f":c1={bg.get('to', '0x2C5364')}:speed={bg.get('speed', 0.015)}"]
                bchain = "null"
            else:
                bp = Path(bg.get("src", ""))
                bp = bp if bp.is_absolute() else base_dir / bp
                if not bp.is_file():
                    die(2, "BG_SRC_MISSING", f"seg[{i}]:background.src 不存在:{bp}")
                if btype == "image":
                    cmd += ["-loop", "1", "-t", f"{take_s:.3f}", "-i", str(bp)]
                else:
                    cmd += ["-stream_loop", "-1", "-t", f"{take_s:.3f}", "-i", str(bp)]
                bchain = cover_crop(cw, ch, cw, ch, 0.5)
            if bg.get("dim"):
                bchain += f",eq=brightness=-{float(bg['dim']):.3f}"
            fparts.append(f"[1:v]{bchain},fps={fps},setsar=1[bg]")
            fparts.append("[bg][fg]overlay=0:0:shortest=1[m]")
            warnings.append(f"seg[{i}]:基轨绿幕已应用(color={chroma.get('color', 'auto')},"
                            f"相似度 {chroma.get('similarity', 0.24)};效果需 L1 目测确认)")
            vf_tail = [f"fps={fps}", "setsar=1"]
        else:
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
        vf_tail.append("format=yuv420p")

        if bg:
            fparts[-1] = fparts[-1].replace("[m]", "[m0]")
            fparts.append(f"[m0]{','.join(vf_tail)}[vout]")
            cmd += ["-filter_complex", ";".join(fparts), "-map", "[vout]", "-map", "0:a?"]
        else:
            cmd += ["-vf", ",".join(vf + vf_tail)]

        if has_audio:
            af = (f"atrim=0:{out_ms/1000:.3f},asetpts=PTS-STARTPTS,"
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
        p = run(cmd, timeout=1800)
        if p.returncode != 0:
            die(4, "SEGMENT_FAIL", f"段 {i} 提取失败:{(p.stderr or '')[-400:]}")
        tmp.replace(cached)
        prune_seg_cache(cache_dir)
        print(f"  seg[{i+1}/{len(base_clips)}] {out_ms/1000:.2f}s"
              f"{' 缓存命中' if hit else ''}", file=sys.stderr)
    return seg_files, reports, seg_keys


# ---------------- 步骤 3:拼接(无转场=无损 concat;有转场=xfade 链) ----------------

def step_concat(doc: dict, seg_files: list[Path], build: Path, cfg: dict,
                warnings: list[str]) -> Path:
    base_clips = [t for t in doc["tracks"] if t["kind"] == "video"][0]["clips"]
    has_transition = any(c.get("transition") for c in base_clips[1:])
    base = build / "base.mp4"

    if not has_transition:
        lst = build / "concat.txt"
        lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in seg_files), encoding="utf-8")
        p = run([ffmpeg_bin(cfg), "-v", "error", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(lst), "-c", "copy", str(base)])
        if p.returncode != 0:
            die(4, "CONCAT_FAIL", f"拼接失败:{(p.stderr or '')[-300:]}")
        return base

    # xfade 链:相邻段重叠 transition 时长;段分辨率/帧率已由 segment 步统一
    n = len(seg_files)
    durs = [c["durationMs"] / 1000.0 for c in base_clips[:n]]
    cmd = [ffmpeg_bin(cfg), "-v", "error", "-y"]
    for f in seg_files:
        cmd += ["-i", str(f)]
    parts, last, lasta = [], "0:v", "0:a"
    cum = durs[0]
    all_have_audio = all(_seg_has_audio(f, cfg) for f in seg_files)
    if not all_have_audio:
        warnings.append("concat:部分段无音轨,转场仅作用于画面,输出将无音频")
    for i in range(1, n):
        tr = base_clips[i].get("transition") or {"type": "fade", "durMs": 500}
        tdur = min(tr.get("durMs", 500) / 1000.0, durs[i - 1] / 2, durs[i] / 2)
        offset = cum - tdur
        parts.append(f"[{last}][{i}:v]xfade=transition={tr['type']}:"
                     f"duration={tdur:.3f}:offset={offset:.3f}[vx{i}]")
        if all_have_audio:
            parts.append(f"[{lasta}][{i}:a]acrossfade=d={tdur:.3f}[ax{i}]")
            lasta = f"ax{i}"
        last = f"vx{i}"
        cum = offset + durs[i]
    cmd += ["-filter_complex", ";".join(parts), "-map", f"[{last}]"]
    cmd += ["-map", f"[{lasta}]", "-c:a", "aac", "-b:a", "192k"] if all_have_audio else ["-an"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-r", str(doc["fps"]), str(base)]
    p = run(cmd, timeout=3600)
    if p.returncode != 0:
        die(4, "CONCAT_XFADE_FAIL", f"转场拼接失败:{(p.stderr or '')[-500:]}")
    return base


def _seg_has_audio(seg: Path, cfg: dict) -> bool:
    info = ffprobe_json(seg, cfg)
    return any(s["codec_type"] == "audio" for s in info.get("streams", []))


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
            if pr["type"] != "image" and clip.get("chroma"):
                c = clip["chroma"]
                if c.get("cropTopPct"):
                    pct = float(c["cropTopPct"])
                    chain.append(f"crop=iw:ih*{1-pct:.4f}:0:ih*{pct:.4f}")
                hexc = chroma_hex(c, Path(pr["path"]), cfg)
                # 同上:chromakey alpha 坏 → colorkey(见 step_segment 内注释)
                chain.append(f"colorkey={hexc}:{c.get('similarity', 0.12)}:{c.get('blend', 0.08)}")
                if c.get("despill", True):
                    chain.append("despill=type=green")
                warnings.append(f"compose[{idx}]:chroma 已应用({hexc},效果需自评确认)")
            w, x, y = _overlay_exprs(clip, cw, ch, start_s)
            motion = clip.get("motion", {})
            if motion.get("in") == "fadeIn":
                chain.append(f"fade=t=in:st=0:d={motion.get('inMs', 400)/1000:.3f}:alpha=1")
            if motion.get("out") == "fadeOut":
                d = motion.get("outMs", 400) / 1000
                chain.append(f"fade=t=out:st={max(0.0, dur_s - d):.3f}:d={d:.3f}:alpha=1")
            chain += [f"scale={w}:-2", "format=yuva420p",
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
            cmd += ["-i", str(sp)]
            chain = ["aresample=48000", "aformat=channel_layouts=stereo"]
            if clip.get("role") == "voice":
                chain.append(LOUDNORM_VOICE)
            chain.append(f"volume={clip.get('volume', 1.0)}")
            chain.append(f"adelay={int(clip['startMs'])}|{int(clip['startMs'])}")
            if clip.get("durationMs"):
                chain.append(f"atrim=0:{clip['durationMs']/1000:.3f}")
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
            parts.append(f"[{idx}:a]{bgm_chain}[bgraw]")
            parts.append(f"[bgraw][{mixes[0][1:-1]}]"
                         f"sidechaincompress=threshold=0.02:ratio=6:attack=60:release=500[bgm]")
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

def step_subtitle(doc: dict, src: Path, build: Path, cfg: dict) -> Path:
    ass = doc.get("subtitle", {}).get("ass")
    if not ass:
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

def step_encode(src: Path, out: Path, profile: str, cfg: dict, fps: int) -> None:
    crf = {"final": "19", "preview": "26", "draft": "28"}[profile]
    preset = {"final": "medium", "preview": "veryfast", "draft": "ultrafast"}[profile]
    cmd = [ffmpeg_bin(cfg), "-v", "error", "-y", "-i", str(src),
           "-af", LOUDNORM_BUS, "-c:v", "libx264", "-preset", preset, "-crf", crf,
           "-pix_fmt", "yuv420p", "-r", str(fps), "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", str(out)]
    p = run(cmd, timeout=7200)
    if p.returncode != 0:
        die(4, "ENCODE_FAIL", f"编码失败:{(p.stderr or '')[-400:]}")


# ---------------- 主流程 ----------------

def render(doc: dict, project_path: Path, ratio: str, profile: str, *,
           use_cache: bool = True, dry_run: bool = False, clear_cache: bool = False) -> dict:
    cfg = load_config()
    # dev-jj2815 实测:IR 用相对路径传入时,seg/输出路径全为相对,concat demuxer
    # 以 concat.txt 所在目录为基准再拼一次 → 路径双重拼接打不开。入口即绝对化。
    base_dir = project_path.parent.parent.resolve()  # 05_ir/ → 工程根
    doc["_base_dir"] = str(base_dir)
    build = base_dir / "06_output" / "_build" / ratio
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

    # 转场会吞时长 → 音频/字幕若存在将整体漂移;提前警告(语义见 schema transition 描述)
    base_clips = [t for t in doc["tracks"] if t["kind"] == "video"][0]["clips"]
    has_tr = any(c.get("transition") for c in base_clips[1:])
    if has_tr and (any(t.get("clips") for t in doc["tracks"] if t["kind"] == "audio") or doc.get("subtitle", {}).get("ass")):
        warnings.append("时间轴提示:转场将吞掉重叠时长(每处 -durMs),音频/字幕的 startMs 若按转场前时间轴排布会漂移;"
                        "建议 Agent 在 IR 中预扣转场消耗(see schema)")

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
                                               "position", "motion", "chroma")}])
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
                              {k: c.get(k) for k in ("startMs", "durationMs", "volume", "role")}])
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

    name = f"final_{doc.get('slug', 'out')}_{ratio.replace('x', '')}.mp4" if profile == "final" \
        else f"{profile}_{doc.get('slug', 'out')}_{ratio.replace('x', '')}.mp4"
    out = base_dir / "06_output" / name
    k_enc = step_key("encode", k_sub, {"profile": profile, "fps": doc["fps"]})
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
        step_encode(subtitled, out, profile, cfg, doc["fps"])

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
                  use_cache=not a.no_cache, dry_run=a.explain, clear_cache=a.clear_cache)
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
