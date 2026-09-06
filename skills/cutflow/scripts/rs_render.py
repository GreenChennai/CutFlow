"""FFmpeg 渲染器:IR → 七步管线 → 成片。

用法:python rs_render.py <project.json> [--ratio 9x16|16x9] [--profile final|preview|draft]
七步:probe → segment → concat → compose → mix → subtitle → encode。
铁律(见 SKILL.md):统一帧率、逐段提取、字幕最后叠、总线 loudnorm -14 LUFS、段间 8ms afade。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import die, emit, ffmpeg_bin, ffprobe_json, load_config, media_duration_s, run  # noqa: E402

RATIO = {"9x16": (1080, 1920), "16x9": (1920, 1080)}
LOUDNORM_BUS = "loudnorm=I=-14:TP=-1.0:LRA=11"
LOUDNORM_VOICE = "loudnorm=I=-16:TP=-1.5:LRA=11"


def esc_sub(path: Path) -> str:
    """ASS 路径转 ffmpeg filter 转义(Windows 盘符冒号)。"""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def cover_crop(iw: int, ih: int, cw: int, ch: int, anchor_y: float = 0.5) -> str:
    """放大到覆盖画布,再按 anchorY 纵向裁切(重构图)。"""
    return (f"scale={cw}:{ch}:force_original_aspect_ratio=increase,"
            f"crop={cw}:{ch}:(iw-{cw})/2:(ih-{ch})*{anchor_y}")


def clip_path(clip: dict, base_dir: Path) -> Path:
    p = Path(clip["src"])
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


# ---------------- 步骤 2:逐段提取 ----------------

def step_segment(doc: dict, ratio: str, build: Path, base_dir: Path, cfg: dict,
                 warnings: list[str]) -> list[Path]:
    cw, ch = RATIO[ratio]
    fps = doc["fps"]
    base_clips = [t for t in doc["tracks"] if t["kind"] == "video"][0]["clips"]
    seg_files: list[Path] = []

    for i, clip in enumerate(base_clips):
        pr = probe_clip(clip, base_dir, cfg)
        speed = clip.get("speed", 1.0)
        out_ms = clip["durationMs"]
        anchor = clip.get("reframe", {}).get("anchorY", 0.5)
        take_s = out_ms / 1000.0 / speed

        cmd = [ffmpeg_bin(cfg), "-v", "error", "-y"]
        if pr["type"] == "image":
            cmd += ["-loop", "1", "-t", f"{out_ms/1000:.3f}", "-i", pr["path"]]
            vf = [f"scale={cw}:{ch}:force_original_aspect_ratio=decrease",
                  f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:color=black"]
        else:
            cmd += ["-ss", f"{clip.get('sourceInMs', 0)/1000:.3f}", "-t", f"{take_s:.3f}",
                    "-i", pr["path"]]
            vf = [cover_crop(pr.get("width") or cw, pr.get("height") or ch, cw, ch, anchor)]
        vf += [f"fps={fps}", "setsar=1"]

        motion = clip.get("motion", {})
        if motion.get("in", "none") != "none":
            if motion["in"] in ("scaleIn", "zoomIn"):
                warnings.append(f"seg[{i}]:motion.{motion['in']} v1 退化为 fadeIn")
            vf.append(f"fade=t=in:st=0:d={motion.get('inMs', 400)/1000:.3f}")
        if motion.get("in") in ("slideInLeft", "slideInRight"):
            warnings.append(f"seg[{i}]:slideIn 属 overlay 动效,基轨段退化为 fadeIn")
            vf.append(f"fade=t=in:st=0:d={motion.get('inMs', 400)/1000:.3f}")
        if motion.get("out", "none") != "none":
            d = motion.get("outMs", 400) / 1000
            vf.append(f"fade=t=out:st={max(0.0, out_ms/1000 - d):.3f}:d={d:.3f}")
        vf.append("format=yuv420p")

        has_audio = pr.get("has_audio", False)
        seg = build / f"seg_{i:04d}.mp4"
        cmd += ["-vf", ",".join(vf)]
        if has_audio:
            af = (f"atrim=0:{out_ms/1000:.3f},asetpts=PTS-STARTPTS,"
                  f"volume={clip.get('volume', 1.0)},"
                  f"afade=t=in:st=0:d=0.008,"
                  f"afade=t=out:st={max(0.0, out_ms/1000 - 0.008):.3f}:d=0.008,"
                  f"aresample=48000,aformat=channel_layouts=stereo")
            cmd += ["-af", af, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
        else:
            cmd += ["-an"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                "-r", str(fps), "-video_track_timescale", "15360", str(seg)]
        p = run(cmd, timeout=1800)
        if p.returncode != 0:
            die(4, "SEGMENT_FAIL", f"段 {i} 提取失败:{(p.stderr or '')[-400:]}")
        seg_files.append(seg)
        print(f"  seg[{i+1}/{len(base_clips)}] {out_ms/1000:.2f}s", file=sys.stderr)
    return seg_files


# ---------------- 步骤 3:无损拼接 ----------------

def step_concat(seg_files: list[Path], build: Path, cfg: dict) -> Path:
    lst = build / "concat.txt"
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in seg_files), encoding="utf-8")
    base = build / "base.mp4"
    p = run([ffmpeg_bin(cfg), "-v", "error", "-y", "-f", "concat", "-safe", "0",
             "-i", str(lst), "-c", "copy", str(base)])
    if p.returncode != 0:
        die(4, "CONCAT_FAIL", f"拼接失败:{(p.stderr or '')[-300:]}")
    return base


# ---------------- 步骤 4:overlay 合成 ----------------

def _overlay_exprs(clip: dict, cw: int, ch: int, start_s: float) -> tuple[str, str, str]:
    """返回 (target_w, x_expr, y_expr)。position/scale 归一化;slide 线性滑入。"""
    scale = clip.get("scale", 1.0)
    px = clip.get("position", {}).get("x", 0.5)
    py = clip.get("position", {}).get("y", 0.5)
    w = int(cw * scale)
    x_f = px * cw - w / 2
    y_f = py * ch
    motion = clip.get("motion", {})
    in_d = motion.get("inMs", 400) / 1000
    x, y = f"{x_f:.1f}", f"{y_f:.1f}"
    if motion.get("in") == "slideInLeft":
        x = (f"if(lt(t,{start_s + in_d:.3f}),"
             f"-{w}+({w}+{x_f:.1f})*(t-{start_s:.3f})/{in_d:.3f},{x_f:.1f})")
    elif motion.get("in") == "slideInRight":
        x = (f"if(lt(t,{start_s + in_d:.3f}),"
             f"{cw}+({x_f:.1f}-{cw})*(t-{start_s:.3f})/{in_d:.3f},{x_f:.1f})")
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
                hexc = "0x00FF00" if c.get("color", "green") == "green" else "0x0000FF"
                chain.append(f"chromakey={hexc}:{c.get('similarity', 0.12)}:{c.get('blend', 0.08)}")
                if c.get("despill", True):
                    chain.append("despill=type=green")
                warnings.append(f"compose[{idx}]:chroma 已应用(相似度 {c.get('similarity', 0.12)},效果需自评确认)")
            w, x, y = _overlay_exprs(clip, cw, ch, start_s)
            motion = clip.get("motion", {})
            if motion.get("in") == "fadeIn":
                chain.append(f"fade=t=in:st={start_s:.3f}:d={motion.get('inMs', 400)/1000:.3f}:alpha=1")
            if motion.get("out") == "fadeOut":
                d = motion.get("outMs", 400) / 1000
                chain.append(f"fade=t=out:st={start_s + dur_s - d:.3f}:d={d:.3f}:alpha=1")
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

def render(doc: dict, project_path: Path, ratio: str, profile: str) -> dict:
    cfg = load_config()
    base_dir = project_path.parent.parent  # 05_ir/ → 工程根
    doc["_base_dir"] = str(base_dir)
    build = base_dir / "06_output" / "_build" / ratio
    build.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    warnings: list[str] = []

    segs = step_segment(doc, ratio, build, base_dir, cfg, warnings)
    base = step_concat(segs, build, cfg)
    composed = step_compose(doc, ratio, base, build, base_dir, cfg, warnings)
    mixed = step_mix(doc, composed, build, base_dir, cfg)
    subtitled = step_subtitle(doc, mixed, build, cfg)
    name = f"final_{doc.get('slug', 'out')}_{ratio.replace('x', '')}.mp4" if profile == "final" \
        else f"{profile}_{doc.get('slug', 'out')}_{ratio.replace('x', '')}.mp4"
    out = base_dir / "06_output" / name
    step_encode(subtitled, out, profile, cfg, doc["fps"])
    return {"output": str(out), "duration_s": round(media_duration_s(out, cfg), 2),
            "elapsed_s": round(time.time() - t0, 1), "profile": profile, "warnings": warnings}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--ratio", default=None, choices=["9x16", "16x9"])
    ap.add_argument("--profile", default="final", choices=["final", "preview", "draft"])
    ap.add_argument("--ass", default=None, help="覆盖 IR 的字幕 ass 路径(每比例各一个 ass)")
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
    ratio = a.ratio or ("9x16" if canvas == "1080x1920" else "16x9")
    if a.ass:
        doc.setdefault("subtitle", {})["ass"] = a.ass
    data = render(doc, p, ratio, a.profile)
    return emit(True, "RENDER_OK", f"渲染完成:{data['output']}({data['duration_s']}s)", data)


if __name__ == "__main__":
    sys.exit(main())
