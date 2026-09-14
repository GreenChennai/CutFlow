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
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import (RATIOS, die, emit, ffmpeg_bin, ffprobe_json, load_config,  # noqa: E402
                       media_duration_s, ratio_for_canvas, run)

RATIO = dict(RATIOS)             # 画幅唯一真相源在 rs_common(新增画幅只改那里)
LOUDNORM_BUS = "loudnorm=I=-14:TP=-1.0:LRA=11"
LOUDNORM_VOICE = "loudnorm=I=-16:TP=-1.5:LRA=11"
LOUDNESS_TARGET = {"I": -14.0, "TP": -1.0, "LRA": 11.0}   # 硬规则 11;出处 ITERATION-GUIDE §8.2
CACHE_VER = "v5"                 # 渲染语义变更时 +1,防旧缓存幽灵命中(v0.12:seg 支持 clip.freezeMs 冻结帧补长)
CHROMA_DEFAULTS = {"similarity": 0.15, "blend": 0.12}   # 基轨/overlay/schema 三处统一(唯一真相源)
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
                  "reframe", "motion", "chroma", "background", "tailMs", "punchIn",
                  "freezeMs")},
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


def parse_matte_log(text: str) -> float | None:
    """从 metadata=print 输出解析 alpha YAVG → 前景占比(≈ 人物面积比,二值 matte)。
    多帧取均值;无数据返回 None。"""
    vals = [float(v) / 255.0 for v in re.findall(r"lavfi\.signalstats\.YAVG=([\d.]+)", text or "")]
    return round(sum(vals) / len(vals), 4) if vals else None


def matte_fg_ratio(seg: Path, dur_s: float, cfg: dict) -> float | None:
    """独立探针:对已合成段抽帧测 alpha 前景占比(仅用于测试/诊断;编码后的 mp4
    已无 alpha 平面,管线内探针走 step_segment 的第二输出,见 matte_probe_args)。"""
    p = run([ffmpeg_bin(cfg), "-v", "info", "-ss", f"{max(0.1, dur_s * 0.5):.2f}",
             "-i", str(seg), "-frames:v", "1",
             "-vf", "format=yuva444p,alphaextract,signalstats,metadata=print",
             "-f", "null", "-"], timeout=120)
    return parse_matte_log(p.stderr) if p.returncode == 0 else None


def matte_probe_args(key: str, cache_dir: Path) -> tuple[str, list[str]]:
    """段命令的第二输出:抽 [fg](alpha 消费点之前)2 帧统计 alpha,写 cache_dir 下文件。

    返回 (文件名, 追加到命令尾部的参数)。文件用**相对名**,配合 run(cwd=cache_dir)
    —— metadata=print:file= 的路径冒号在 filtergraph 里转义不可靠(单/双转义均
    解析失败,v0.11 实测),相对名是唯一稳解;继续走 run() 保持可 monkeypatch。
    """
    name = f"matte_{key[:16]}.txt"
    return name, ["-map", "[fgprobe]", "-frames:v", "2", "-f", "null", "-"]


def _crop_pct_chain(c: dict) -> list[str]:
    chain = []
    if c.get("cropTopPct"):
        chain.append(f"crop=iw:ih*{1 - float(c['cropTopPct']):.4f}:0:ih*{float(c['cropTopPct']):.4f}")
    if c.get("cropBottomPct"):
        pct = float(c["cropBottomPct"])
        chain.append(f"crop=iw:ih*{1 - pct:.4f}:0:0" if not c.get("cropTopPct")
                     else f"crop=iw:ih*{1 - pct:.4f}:0:0")
    return chain


def _chroma_fg_chain(c: dict) -> list[str]:
    """抠像后处理(v0.10,ADR-0022):alpha 腐蚀收缩 → 细羽化 → killRect(green-only)。

    v0.9 只有 edgeBlur 羽化:colorkey 的 alpha 近二值,边缘锯齿与暗色残边(黑边)
    原样保留 —— 用户实测发丝锯齿+黑边。腐蚀 = alpha 大 sigma 模糊后用偏高频阈值
    重新硬化(收缩量 ≈ sigma×系数),再小 sigma 羽化找回平滑过渡。
    killRect 默认 `killRectMode=green`:框内**只清绿色主导像素**(despill 后仍
    cb/cr 双低),入区人体(中性色)不受影响 —— 硬矩形连人一起抹、矩形边界随
    人物动作进出穿帮,是店群工程"左下角闪烁黑影"的根因。`all` 保留 v0.9 硬清。

    ⚠ geq 域约定(v0.10.1 实测修复):geq 的 alpha/cb/cr(X,Y) 返回**原始 0-255
    字节值**,表达式结果也按字节写入 —— 不是归一化 [0,1]。旧式
    `clip((alpha-t)/(1-t),0,1)` 把 255 当 1.0 算 → 恒输出 1 → 人物整帧透明
    (店群工程实测全片无人物)。阈值 t∈[0,1] 是参数域,必须换算进字节域再比较。
    """
    chain = []
    shrink = float(c.get("edgeShrink", 1.2) or 0)
    t = min(max(float(c.get("edgeShrinkT", 0.55)), 0.0), 1.0)
    feather = float(c.get("edgeFeather", c.get("edgeBlur", 0.6)) or 0)
    core = "alpha(X,Y)"
    if shrink > 0:
        # 字节域腐蚀:t*255 以下归 0,以上线性爬升回 255(半透明过渡带宽度 = (1-t)*255)
        tn, rn = t * 255.0, (1.0 - t) * 255.0
        core = f"clip((alpha(X,Y)-{tn:.1f})/{rn:.1f}*255,0,255)"
    conds = []
    mode = c.get("killRectMode", "green")
    for r in c.get("killRects") or []:
        x0, y0, x1, y1 = (float(v) for v in r)
        box = (f"(between(X,W*{x0:.4f},W*{x1:.4f})"
               f"*between(Y,H*{y0:.4f},H*{y1:.4f}))")
        if mode == "green":
            # despill 后残留绿仍呈 cb/cr 双低(pure green cb≈44/cr≈21,半中和 ≈86/75);
            # 中性灰/人体 cb≈cr≈128,不会被误清。116 取两者分界。
            conds.append(f"{box}*lt(cb(X,Y),116)*lt(cr(X,Y),116)")
        else:
            conds.append(box)
    if conds:
        core = "if(" + "+".join(conds) + f",0,{core})"
    if shrink > 0 or conds:
        pre = "format=yuva444p" + (f",gblur=sigma={shrink:.2f}:planes=8" if shrink > 0 else "")
        chain.append(f"{pre},geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':a='{core}'")
    if feather > 0:
        chain.append(f"gblur=sigma={feather:.2f}:planes=8")
    return chain


# ---------------- 步骤 2:逐段提取(带 seg 缓存 + 基轨绿幕) ----------------

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
    seg_files: list[Path] = []
    seg_keys: list[str] = []
    reports: list[dict] = []

    for i, clip in enumerate(base_clips):
        clip = {**clip, "tailMs": round(tails[i] * 1000)}   # 入键:尾帧变化必须换缓存键
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
        # v0.10 帧量化:段边界吸附帧网格。±半帧的取整偏差原本散落在拼接边界上,
        # 是"每段首尾差一帧"类闪烁的隐形来源;量化后段长恒为整数帧。
        q_in_ms = round(clip.get("sourceInMs", 0) * fps / 1000.0) / fps * 1000.0
        q_out_ms = max(1, round(out_ms / 1000.0 * fps)) / fps * 1000.0
        take_s = (q_out_ms + tails[i] * 1000.0) / 1000.0 / speed

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
            # I7/v0.12 freezeMs:冻结帧补长(纯动画卡片比旁白短时)。**-t 只读到冻结
            # 起点(输入侧)**,之后 tpad 克隆尾帧补足——B8 教训:-t 放输出侧会把
            # tpad 补的帧整段截掉(安信德工程丢 20.4s 的事故形态)。
            freeze_ms = float(clip.get("freezeMs") or 0)
            read_s = min(take_s, freeze_ms / 1000.0) if freeze_ms > 0 else take_s
            cmd += loop + ["-ss", f"{q_in_ms / 1000:.3f}", "-t", f"{read_s:.3f}",
                           "-i", pr["path"]]
            vf = [cover_crop(pr.get("width") or cw, pr.get("height") or ch, cw, ch, anchor)]
            if freeze_ms > 0:
                stop_s = max(0.0, take_s - read_s) + 0.5     # 补足请求时长 + 帧取整余量
                vf.append(f"tpad=stop_mode=clone:stop_duration={stop_s:.3f}")

        has_audio = pr.get("has_audio", False)
        fparts: list[str] = []
        if bg:
            # 前景:裁边 → 抠像 → 去绿边 → 覆盖画布
            fg = ["setpts=PTS-STARTPTS"] + [x for x in _crop_pct_chain(chroma) if x]
            # dev-jj2815 实测(ffmpeg 2026-07-30 git master 回归):chromakey 输出的
            # alpha 全坏(人物区域≈0,YAVG 2.07/255;alphaextract 实测),叠任何背景
            # 都是"幽灵人物";-vf 单输入 + JPG 因丢弃 alpha 而看不出来。
            # colorkey(RGB 距离键控)alpha 正常(人物 255/背景 0),改用之。
            fg.append(f"colorkey={chroma_hex(chroma, src, cfg)}:"
                      f"{chroma.get('similarity', CHROMA_DEFAULTS['similarity'])}:"
                      f"{chroma.get('blend', CHROMA_DEFAULTS['blend'])}")
            if chroma.get("despill", True):
                fg.append("despill=type=green")
            fg += _chroma_fg_chain(chroma)
            fg.append(cover_crop(pr.get("width") or cw, pr.get("height") or ch, cw, ch, anchor))
            # v0.11 R1:显式 yuva444p + split —— alpha 平面从这里分给 overlay(合成)
            # 与 matte 探针;否则格式协商会被下游 yuv420p 分支拉成无 alpha,探针恒 255。
            fparts.append(f"[0:v]{','.join(fg)}[fg0]")
            fparts.append("[fg0]format=yuva444p,split=2[fg][fgs]")
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
                            f"相似度 {chroma.get('similarity', CHROMA_DEFAULTS['similarity'])};"
                            "edgeShrink 腐蚀+羽化+green-only killRect;效果需 L1 目测确认)")
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
        # v0.11 R3 punch-in(ITERATION-GUIDE §5.3):切点处 1.25~1.5x 变焦交替构图,
        # 是 jump cut 的通行掩饰(Hitchcock 规则:更紧构图只给重点)。anchorY 决定
        # 纵向裁切偏置(0=贴顶,保护人物头部)。插在最前,fade 作用在变焦后的画面上。
        punch_f = float((clip.get("punchIn") or {}).get("factor", 0) or 0)
        if 1.01 < punch_f:
            punch_f = min(punch_f, 2.0)
            vf_tail.insert(0, f"scale={round(cw * punch_f / 2) * 2}:{round(ch * punch_f / 2) * 2},"
                              f"crop={cw}:{ch}:(iw-ow)/2:(ih-oh)*{anchor:.3f}")
        vf_tail.append("format=yuv420p")

        probe_name, probe_args = "", []
        if bg:
            fparts[-1] = fparts[-1].replace("[m]", "[m0]")
            fparts.append(f"[m0]{','.join(vf_tail)}[vout]")
            # v0.11 R1 matte 探针:第二输出抽 [fgs](split 自 alpha 平面,overlay 消费
            # 之前)2 帧,统计写 cache_dir/matte_<key>.txt —— 编码前的真 alpha。
            probe_name, probe_args = matte_probe_args(key, cache_dir)
            fparts.append(f"[fgs]alphaextract,signalstats,"
                          f"metadata=print:file={probe_name}[fgprobe]")
            cmd += ["-filter_complex", ";".join(fparts), "-map", "[vout]", "-map", "0:a?"]
        else:
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
        if probe_args:
            cmd += probe_args          # 第二输出(必须跟在主输出之后;选项按输出生效)
        p = run(cmd, timeout=1800, cwd=str(cache_dir))
        if p.returncode != 0:
            die(4, "SEGMENT_FAIL", f"段 {i} 提取失败:{(p.stderr or '')[-400:]}")
        tmp.replace(cached)
        prune_seg_cache(cache_dir)
        # v0.11 R1 matte 探针(ADR-0022 修订的护栏):色度链改动的语义错误(如 geq
        # 字节域事故=全片人物透明)字符串测试测不出来;第二输出在 alpha 消费点前
        # 采样,占比异常直接进 warnings —— 事故从"人看成片"提前到"渲染期"。
        if bg:
            pf = cache_dir / probe_name
            ratio = parse_matte_log(pf.read_text(encoding="utf-8", errors="replace")
                                    if pf.is_file() else "")
            reports[-1]["matteFgRatio"] = ratio
            pf.unlink(missing_ok=True)
            if ratio is not None and (ratio < 0.01 or ratio > 0.70):
                warnings.append(
                    f"seg[{i}]:matte_suspect 前景占比 {ratio:.1%}(正常 10%~60%)——"
                    "疑似抠像失效/键带过宽,先查 chroma 参数与 L1 目测,不要直接交付")
        print(f"  seg[{i+1}/{len(base_clips)}] {out_ms/1000:.2f}s"
              f"{' 缓存命中' if hit else ''}", file=sys.stderr)
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
            if pr["type"] != "image" and clip.get("chroma"):
                c = clip["chroma"]
                if c.get("cropTopPct"):
                    pct = float(c["cropTopPct"])
                    chain.append(f"crop=iw:ih*{1-pct:.4f}:0:ih*{pct:.4f}")
                hexc = chroma_hex(c, Path(pr["path"]), cfg)
                # 同上:chromakey alpha 坏 → colorkey(见 step_segment 内注释)
                chain.append(f"colorkey={hexc}:{c.get('similarity', CHROMA_DEFAULTS['similarity'])}:"
                             f"{c.get('blend', CHROMA_DEFAULTS['blend'])}")
                if c.get("despill", True):
                    chain.append("despill=type=green")
                chain += _chroma_fg_chain(c)      # v0.10:overlay 层同样吃边缘精修
                warnings.append(f"compose[{idx}]:chroma 已应用({hexc},效果需自评确认)")
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
                                               "position", "motion", "chroma",
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
    out = base_dir / "06_output" / name
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
