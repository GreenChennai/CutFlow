"""IR 校验与生成。
用法:python rs_ir.py validate <project.json>
      python rs_ir.py build --from-cutlist 04_cut/cutlist.applied.json --slug X --out 05_ir/project.json
      python rs_ir.py build --from-cards 03_assets/artboard/manifest.json \\
             --anchors 00_brief/cards.json --wordline 05_ir/wordline.json \\
             --voice 03_assets/vo/voice.wav --slug X --ratio 16x9 --out 05_ir/project.json

build 把 CutList 的 keep 区间转成 IR 主轨——**消灭「Agent 手写毫秒」这一整类误差**(rules/compose.md)。
build --from-cards(ADR-0027,I7):纯动画工程一条命令组装 IR——卡片↔旁白字符级锚点
分组、停顿中点切卡、冻结帧补长(freezeMs),此前每个工程要重写一遍脚本(安信德 GEO)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit  # noqa: E402
from rs_align import keep_to_segments, map_src_to_final  # noqa: E402
from rs_common import RATIOS, content_text  # noqa: E402
import segmentation  # noqa: E402  — 标点口径与断句/字幕全链一致

CANVAS = {k: {"width": w, "height": h} for k, (w, h) in RATIOS.items()}
_CANVAS_PAIRS = tuple(RATIOS.values())

MOTION_IN = {"none", "fadeIn", "slideInLeft", "slideInRight", "scaleIn", "zoomIn"}
MOTION_OUT = {"none", "fadeOut", "slideOutLeft", "slideOutRight"}
TRANSITIONS = {"fade", "wipeleft", "wipeup", "slideleft", "circleopen"}
KINDS = {"video", "audio", "text"}
BG_TYPES = {"color", "image", "video", "gradient"}
PUNCH_MIN_GAP_MS = 15000               # R3 punch-in 最小间隔(经验值,ITERATION-GUIDE §5.3)
CHROMA_PRESET = {"green", "blue", "auto"}
HEX_PREFIX = "0x"


def _validate_chroma_bg(where: str, clip: dict, base_dir: Path, errs: list[str]) -> None:
    """v0.6.0:clip.chroma(抠像)+ clip.background(背景替换)校验。"""
    chroma = clip.get("chroma")
    bg = clip.get("background")
    if chroma:
        col = chroma.get("color", "auto")
        if col not in CHROMA_PRESET and not (isinstance(col, str) and col.startswith(HEX_PREFIX)):
            errs.append(f"{where}.chroma.color 非法:{col}(green/blue/auto/0xRRGGBB)")
        for k in ("similarity", "blend"):
            v = chroma.get(k)
            if v is not None and not (isinstance(v, (int, float)) and 0 <= v <= 1):
                errs.append(f"{where}.chroma.{k} 应在 0..1:{v}")
        for k in ("cropTopPct", "cropBottomPct"):
            v = chroma.get(k)
            if v is not None and not (isinstance(v, (int, float)) and 0 <= v <= 0.9):
                errs.append(f"{where}.chroma.{k} 应在 0..0.9:{v}")
        eb = chroma.get("edgeBlur")
        if eb is not None and not (isinstance(eb, (int, float)) and 0 <= eb <= 5):
            errs.append(f"{where}.chroma.edgeBlur 应在 0..5:{eb}")
        for i, r in enumerate(chroma.get("killRects") or []):
            try:
                x0, y0, x1, y1 = (float(v) for v in r)
            except (TypeError, ValueError):
                errs.append(f"{where}.chroma.killRects[{i}] 应为 4 个数字:{r}")
                continue
            if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
                errs.append(f"{where}.chroma.killRects[{i}] 应满足 0≤x0<x1≤1 / 0≤y0<y1≤1:{r}")
    if bg:
        t = bg.get("type")
        if t not in BG_TYPES:
            errs.append(f"{where}.background.type 非法:{t}(可选 {sorted(BG_TYPES)})")
        if t in ("image", "video"):
            src = bg.get("src", "")
            pp = Path(src)
            exists = pp.is_absolute() and pp.is_file() or (base_dir / src).is_file() if src else False
            if not exists:
                errs.append(f"{where}.background.src 不存在:{src}")
        if not chroma:
            errs.append(f"{where}:background 必须与 chroma 同用(没有抠像就没有换背景)")


def validate(doc: dict, base_dir: Path) -> list[str]:
    errs: list[str] = []
    if doc.get("version") != 1:
        errs.append("version 必须为 1")
    canvas = doc.get("canvas", {})
    w, h = canvas.get("width"), canvas.get("height")
    if (w, h) not in _CANVAS_PAIRS:
        allowed = " / ".join(f"{a}x{b}" for a, b in _CANVAS_PAIRS)
        errs.append(f"canvas 非法:{w}x{h}(可选 {allowed})")
    if doc.get("fps") not in (24, 25, 30, 50, 60):
        errs.append(f"fps 非法:{doc.get('fps')}")

    video_tracks = [t for t in doc.get("tracks", []) if t.get("kind") == "video"]
    if not video_tracks:
        errs.append("至少需要一个 video 轨")

    for ti, track in enumerate(doc.get("tracks", [])):
        kind = track.get("kind")
        if kind not in KINDS:
            errs.append(f"tracks[{ti}].kind 非法:{kind}")
        spans = []
        for ci, clip in enumerate(track.get("clips", [])):
            where = f"tracks[{ti}].clips[{ci}]"
            start, dur = clip.get("startMs"), clip.get("durationMs")
            if not isinstance(start, (int, float)) or start < 0:
                errs.append(f"{where}.startMs 非法:{start}")
                continue
            if not isinstance(dur, (int, float)) or dur <= 0:
                if kind == "video":
                    errs.append(f"{where}.durationMs 非法:{dur}(video 轨必填)")
                    continue
                dur = None  # audio/text 缺省 = 素材全长
            if dur is not None:
                spans.append((start, start + dur))
            if clip.get("src") and kind != "text":
                if clip["src"].startswith("assets_sfx:"):  # 内置音效库伪协议
                    from rs_common import REPO_ROOT
                    exists = (REPO_ROOT / "assets" / "sfx" / (clip["src"].split(":", 1)[1] + ".mp3")).is_file()
                else:
                    pp = Path(clip["src"])
                    exists = (pp if pp.is_absolute() else base_dir / pp).is_file()
                if not exists:
                    errs.append(f"{where}.src 不存在:{clip['src']}")
            motion = clip.get("motion", {})
            if motion.get("in", "none") not in MOTION_IN:
                errs.append(f"{where}.motion.in 非法:{motion.get('in')}")
            if motion.get("out", "none") not in MOTION_OUT:
                errs.append(f"{where}.motion.out 非法:{motion.get('out')}")
            tr = clip.get("transition")
            if tr:
                if tr.get("type") not in TRANSITIONS:
                    errs.append(f"{where}.transition.type 非法:{tr.get('type')}")
                # B2(BUGREPORT-20260913):唯一合法字段是 durMs。旧版 rs_ir 写过
                # "ms" —— schema/rs_render 都不认,静默落回默认 500ms 吞时长。
                if "ms" in tr and "durMs" not in tr:
                    errs.append(f"{where}.transition 用了旧字段 ms(唯一合法字段是 durMs,"
                                "旧版会静默按 500ms 吞时长)")
                if "durMs" in tr and not isinstance(tr["durMs"], (int, float)):
                    errs.append(f"{where}.transition.durMs 应为数字:{tr.get('durMs')}")
            if kind == "video":
                _validate_chroma_bg(where, clip, base_dir, errs)
        spans.sort()
        for a, b in zip(spans, spans[1:]):
            if b[0] < a[1] - 1:
                errs.append(f"tracks[{ti}] 时间重叠:{a} 与 {b}(转场重叠应 ≤1ms 容差)")

    for ti, track in enumerate(doc.get("tracks", [])):
        for ci, clip in enumerate(track.get("clips", [])):
            if track.get("kind") == "text" and not clip.get("text"):
                errs.append(f"tracks[{ti}].clips[{ci}] text 轨缺 text 字段")

    sub = doc.get("subtitle", {})
    if sub.get("ass") and not Path(sub["ass"]).is_absolute() and not (base_dir / sub["ass"]).is_file():
        errs.append(f"subtitle.ass 不存在:{sub['ass']}")
    elif sub.get("ass") and Path(sub["ass"]).is_absolute() and not Path(sub["ass"]).is_file():
        errs.append(f"subtitle.ass 不存在:{sub['ass']}")
    return errs


def build_from_cutlist(cutlist: dict, *, slug: str, ratio: str = "9x16",
                       xfade_ms: int = 8, with_audio: bool = True,
                       punch_in_auto: bool = False) -> dict:
    """CutList(keep 区间)→ IR 主视频/音频轨(时间一律经 map_src_to_final 换算)。"""
    keep = [[int(a), int(b)] for a, b in (cutlist.get("keep") or [])]
    if not keep:
        raise ValueError("cutlist 缺少 keep 区间(先跑 rs_cut.py --apply)")
    src = cutlist.get("source") or "01_materials/"
    segs = keep_to_segments(keep)

    video, audio = [], []
    cursor = 0
    last_punch_ms = -PUNCH_MIN_GAP_MS      # v0.11 R3 punch-in 密度控制(§5.3)
    punch_count = 0
    for i, (a, b) in enumerate(keep):
        dur = b - a
        clip = {"src": src, "startMs": cursor, "durationMs": dur, "sourceInMs": a}
        if i > 0 and xfade_ms > 0:
            # B2:唯一合法字段是 durMs(schema/rs_render 同口径);旧字段 "ms" 会被
            # 静默落回默认 500ms,7 处转场吞掉 3.5s 造成音画错位。
            # v0.11 R2(ADR-0026)转场三级语法:源间隙 <1s = 同段内跳切 → 亚帧软切
            # (渲染端 1 帧 xfade:视觉即硬切,仅吃掉姿态/alpha 单帧 pop 与音频爆音);
            # ≥1s = 真(话题/章节)切换 → 300ms 交叉溶解(Reisz 语法:dissolve 表达
            # "时间过去了",同段内不用)。渲染端 cap 与整链回退不变(ADR-0023)。
            gap = a - keep[i - 1][1]
            if gap < 1000:
                clip["transition"] = {"type": "fade", "durMs": int(xfade_ms),
                                      "reason": "jumpcut"}
            else:
                clip["transition"] = {"type": "fade", "durMs": max(int(xfade_ms), 300),
                                      "reason": "topic"}
            # R3 punch-in 启发式(opt-in):真剪辑点(移除 ≥1.2s)后切更紧构图,
            # 密度 ≥15s/次、全片 ≤3 处(多则失去强调意义)。
            if punch_in_auto and gap >= 1200 and punch_count < 3 \
                    and cursor - last_punch_ms >= PUNCH_MIN_GAP_MS:
                clip["punchIn"] = {"factor": 1.4, "source": "auto"}
                last_punch_ms = cursor
                punch_count += 1
        video.append(clip)
        if with_audio:
            audio.append({"src": src, "startMs": cursor, "durationMs": dur,
                          "sourceInMs": a, "role": "voice"})
        cursor += dur

    return {
        "version": 1, "slug": slug, "fps": 30, "canvas": dict(CANVAS[ratio]),
        "tracks": [{"kind": "video", "clips": video},
                   {"kind": "audio", "clips": audio}],
        "subtitle": {"ass": "06_output/subtitles.ass", "source": "05_ir/wordline.json"},
        "outputs": [ratio],
        "_meta": {"generatedFrom": "cutlist", "keepSegments": len(keep),
                  "finalDurationMs": cursor,
                  "mappedVia": "rs_align.map_src_to_final",
                  "srcTotalMs": cutlist.get("srcTotalMs"),
                  "removedMs": cutlist.get("removedMs")},
    }


# ---------------------------------------------------------------- I7:纯动画 IR 组装(ADR-0027)

CARD_FREEZE_RESERVE_MS = 950   # 卡片出场动画预留(出场 0.5s + 收尾 0.4s + 余量):冻结必须发生在出场开始前


def _resolve(base_dir: Path | None, p: str) -> Path:
    q = Path(p)
    if q.is_absolute() or base_dir is None:
        return q
    return base_dir / q


def assign_card_groups(chars: list[dict], anchors: list[dict]) -> list[dict]:
    """卡片↔旁白分组 = **字符级锚点扫描**(I7,安信德工程自写脚本上游化)。

    规则(全部来自实测,别再让下一个工程重推一遍):
    · 锚点 match 用 `|` 分隔多候选(任一命中即切组),归一化 = 去标点/空白;
    · **命中后消费 len(k) 个内容字**——防「交给安信德GEO优化系统」把
      「安信德GEO优化系统」的后缀锚点在原处重复触发;
    · 组号只进不退(range(gi, N) 向前扫);**绝不可写 `if gi == g: break`**——
      `g` 从 `gi` 起步时第一轮恒真,锚点永不前进(安信德 bug #9 的真凶),必须用 hit 标志;
    · 标点继承当前组(标点没有锚,属于正在讲的卡);
    · 同卡相邻组合并(如 c16 的两个锚点「八成客户」「先布局」)。
    锚词**优先取句首词**:句中锚会让上一张卡拖尾、下一张卡过短(rules/video-types/纯动画.md)。
    返回 [{card, startMs, endMs}](按锚点顺序,时间 = 组内首末字的字级时间)。
    """
    if not anchors:
        raise ValueError("anchors 为空:至少要有一张卡的锚点")
    text = "".join(str(c.get("ch", "")) for c in chars)
    keys: list[list[str]] = []
    cards: list[str] = []
    for a in anchors:
        alts = [content_text(k) for k in str(a.get("match", "")).split("|")]
        alts = [k for k in alts if k]
        if not alts:
            raise ValueError(f"锚点 match 归一化后为空:{a!r}")
        cards.append(str(a.get("card", "")))
        if not cards[-1]:
            raise ValueError(f"锚点缺 card 字段:{a!r}")
        keys.append(alts)
    # 同卡多条锚点合法(如「八成客户」「先布局」都指 c16):相邻自动合并,
    # 不相邻由组聚合后的同名组检查兜底报错。

    norm_pos = [i for i, ch in enumerate(text) if ch.strip() and ch not in segmentation.PUNCT_WS]
    norm = "".join(text[i] for i in norm_pos)
    assign = [0] * len(text)
    gi, p = 0, 0
    hit_count = [0] * len(keys)          # 每条锚点的命中次数(同卡可写多条锚点,如 c16)
    for i in range(len(text)):
        if p < len(norm_pos) and norm_pos[p] == i:
            hit = False
            for g in range(gi, len(keys)):
                for k in keys[g]:
                    if norm[p:p + len(k)] == k:
                        gi = g
                        p += len(k)            # 消费命中串(防后缀重复触发)
                        hit_count[g] += 1
                        hit = True
                        break
                if hit:
                    break
            if not hit:
                p += 1
        assign[i] = gi                          # 标点继承当前组

    gmap: dict[int, list[int]] = {}
    for i, g in enumerate(assign):
        gmap.setdefault(g, []).append(i)
    raw = [{"card": cards[g], "startMs": int(chars[idxs[0]]["startMs"]),
            "endMs": int(chars[idxs[-1]]["endMs"])}
           for g, idxs in sorted(gmap.items())]
    groups: list[dict] = []
    for r in raw:
        if groups and groups[-1]["card"] == r["card"]:
            groups[-1]["endMs"] = r["endMs"]    # 同卡相邻组合并
        else:
            groups.append(dict(r))
    missing = [cards[g] for g in range(len(cards)) if hit_count[g] == 0]
    if missing:
        raise ValueError(f"锚点未命中(空组,检查词是否与旁白逐字一致):{missing}")
    if len({g["card"] for g in groups}) != len(groups):
        raise ValueError("同名卡的组不相邻——锚点顺序必须与旁白出现顺序一致"
                         "(锚词优先取句首词,句中锚会让上一张卡拖尾)")
    return groups


def build_from_cards(manifest: dict, wordline: dict, anchors: list[dict], *, slug: str,
                     ratio: str = "16x9", voice: str | None = None,
                     voice_ms: int | None = None,
                     bgm: dict | None = None,
                     fps: int = 30, base_dir: Path | None = None) -> dict:
    """纯动画 IR 组装器(I7,ADR-0027):场景卡串联主轨 + 旁白音频轨 + BGM。

    时间唯一来源 = wordline 字级锚;组间边界 = 语音停顿中点;卡比旁白短 →
    clip.freezeMs(渲染端 tpad 冻结帧补长,`-t` 输入侧)。subtitle.ass **必须写全**
    (B4:缺 ass 字段 = 静默不烧字幕的事故根源)。BGM 不预裁:step_mix 的
    atrim + stream_loop 自动裁齐循环。卡片路径口径与 rs_artboard --apply 一致:
    manifest.output 写工程根相对路径(挂点匹配按归一化绝对路径,rules/artboard.md)。
    """
    from rs_common import media_duration_s  # noqa: PLC0415 — 延迟导入,纯逻辑单测不必装 ffmpeg
    root_items = {it.get("id"): it for it in (manifest.get("items") or [])}
    chars = wordline.get("chars") or []
    if not chars:
        raise ValueError("wordline 没有 chars(先跑 rs_align)")
    groups = assign_card_groups(chars, anchors)

    total_ms = int(voice_ms) if voice_ms else int(
        wordline.get("finalDurationMs") or wordline.get("srcDurationMs") or 0)
    if voice and not voice_ms:
        try:
            total_ms = int(media_duration_s(_resolve(base_dir, voice)) * 1000)
        except (Exception, SystemExit):  # noqa: BLE001 — 探测失败留 0,由下方总时长校验报
            total_ms = 0
    if total_ms <= 0:
        raise ValueError("无法确定旁白总时长(给 --voice 或保证 wordline 有 srcDurationMs)")

    bounds = [0]
    for a, b in zip(groups, groups[1:]):
        bounds.append((a["endMs"] + b["startMs"]) // 2)   # 组边界 = 语音停顿中点
    bounds.append(total_ms)

    clips: list[dict] = []
    freeze_count = 0
    for i, g in enumerate(groups):
        item = root_items.get(g["card"])
        if item is None:
            raise ValueError(f"卡「{g['card']}」不在 manifest(先 rs_artboard --scan)")
        src = str(item.get("output", ""))
        dur = bounds[i + 1] - bounds[i]
        clip = {"src": src, "startMs": bounds[i], "durationMs": dur, "sourceInMs": 0}
        if item.get("kind") == "mp4":
            try:
                card_ms = int(media_duration_s(_resolve(base_dir, src)) * 1000)
            except (Exception, SystemExit):  # noqa: BLE001 — ffprobe die()/缺失:validate 会兜底报
                card_ms = 0
            hold = card_ms - CARD_FREEZE_RESERVE_MS
            if card_ms > 0 and dur > hold:
                clip["freezeMs"] = max(1, hold)   # 出场动画开始前定格(时长语义见 rs_render.step_segment)
                freeze_count += 1
        clips.append(clip)

    audio = []
    if voice:
        audio.append({"src": voice, "startMs": 0, "durationMs": total_ms, "role": "voice"})
    doc = {
        "version": 1, "slug": slug, "fps": fps, "canvas": dict(CANVAS[ratio]),
        "tracks": [{"kind": "video", "clips": clips},
                   {"kind": "audio", "clips": audio}],
        "subtitle": {"ass": "06_output/subtitles.ass", "source": "05_ir/wordline.json"},
        "outputs": [ratio],
        "_meta": {"generatedFrom": "cards", "cardCount": len(clips),
                  "freezeClips": freeze_count, "finalDurationMs": bounds[-1],
                  "anchorCount": len(anchors)},
    }
    if bgm and bgm.get("src"):
        doc["bgm"] = {"src": bgm["src"],
                      "gainDb": float(bgm.get("gainDb", -20)),
                      "ducking": bool(bgm.get("ducking", True))}
    return doc


def _manual_edits(out: Path) -> list[str]:
    """检测现存 project.json 的手注痕迹(B8):这些字段 fresh build 永不产生,
    出现即说明 Agent 手改过 IR —— 重建覆盖前必须显式确认,防止静默冲掉。"""
    if not out.is_file():
        return []
    try:
        old = json.loads(out.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    hits: list[str] = []
    if (old.get("_meta") or {}).get("manualEdit"):
        hits.append("_meta.manualEdit")
    for ti, t in enumerate(old.get("tracks", [])):
        for ci, c in enumerate(t.get("clips", [])):
            if c.get("chroma"):
                hits.append(f"tracks[{ti}].clips[{ci}].chroma")
            if c.get("background"):
                hits.append(f"tracks[{ti}].clips[{ci}].background")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["validate", "build"])
    ap.add_argument("project", nargs="?")
    ap.add_argument("--from-cutlist")
    ap.add_argument("--from-cards", dest="from_cards",
                    help="I7 纯动画组装:artboard manifest.json(卡清单);"
                         "配合 --anchors/--wordline/--voice")
    ap.add_argument("--anchors", help="卡片↔旁白分组表 JSON:[{\"card\":…,\"match\":\"词A|词B\"}]"
                                      "(顺序=卡片顺序;match 归一化后必须与旁白逐字一致)")
    ap.add_argument("--wordline", help="from-cards:05_ir/wordline.json(时间唯一来源)")
    ap.add_argument("--voice", help="from-cards:旁白音频(总时长来源)")
    ap.add_argument("--bgm", help="from-cards:BGM 音频(渲染端自动裁齐循环)")
    ap.add_argument("--gain-db", dest="gain_db", type=float, default=-20)
    ap.add_argument("--no-ducking", dest="no_ducking", action="store_true",
                    help="BGM 关闭闪避(默认开;B3 修复后 ducking 可正常使用)")
    ap.add_argument("--root", default=".", help="工程根(解析卡片相对路径;默认 cwd)")
    ap.add_argument("--slug", default="project")
    ap.add_argument("--ratio", default="9x16", choices=list(RATIOS))
    ap.add_argument("--xfade", type=int, default=8)
    ap.add_argument("--punch-in-auto", dest="punch_in_auto", action="store_true",
                    help="R3:真剪辑点(移除 ≥1.2s)后自动 punch-in 1.4x(密度 15s/≤3 处;opt-in)")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="检测到手注痕迹时仍覆盖(放弃手注;BUGREPORT B8)")
    ap.add_argument("--out")
    a = ap.parse_args()

    if a.command == "build":
        if a.from_cards:
            for need, flag in ((a.anchors, "--anchors"), (a.wordline, "--wordline")):
                if not need:
                    return emit(False, "NO_INPUT", f"--from-cards 需要 {flag}", exit_code=2)
            base_dir = Path(a.root).resolve()
            try:
                manifest = json.loads(Path(a.from_cards).read_text(encoding="utf-8"))
                wl = json.loads(Path(a.wordline).read_text(encoding="utf-8"))
                anchors = json.loads(Path(a.anchors).read_text(encoding="utf-8"))
                if not isinstance(anchors, list):
                    return emit(False, "BAD_ANCHORS", "--anchors 必须是 [{card,match}] 数组", exit_code=2)
                bgm_cfg = {"src": a.bgm, "gainDb": a.gain_db,
                           "ducking": not a.no_ducking} if a.bgm else None
                doc = build_from_cards(manifest, wl, anchors, slug=a.slug, ratio=a.ratio,
                                       voice=a.voice, bgm=bgm_cfg, base_dir=base_dir)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                return emit(False, "BUILD_FAIL", f"生成失败:{exc}", exit_code=2)
            out = Path(a.out or "05_ir/project.json")
            out.parent.mkdir(parents=True, exist_ok=True)
            manual = _manual_edits(out)
            if manual and not a.force:
                return emit(False, "IR_MANUAL_EDITS",
                            f"现存 IR 含手注痕迹,重建会冲掉:{';'.join(manual[:4])};"
                            "确认放弃手注请加 --force(I7 生成式产物同样不得手改)", exit_code=2)
            errs = validate(doc, base_dir)
            out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            msg = (f"IR 已生成:{doc['_meta']['cardCount']} 卡 / "
                   f"{doc['_meta']['finalDurationMs'] / 1000:.1f}s / "
                   f"冻结补长 {doc['_meta']['freezeClips']} 卡")
            if errs:
                msg += f"(校验 {len(errs)} 个提示:src 文件可能尚未就位)"
            return emit(True, "IR_BUILT", msg,
                        {"path": str(out), **doc["_meta"], "validateErrors": errs})

        cl_path = Path(a.from_cutlist or "")
        if not cl_path.is_file():
            return emit(False, "NO_CUTLIST", f"cutlist 不存在:{cl_path}", exit_code=2)
        try:
            doc = build_from_cutlist(json.loads(cl_path.read_text(encoding="utf-8")),
                                     slug=a.slug, ratio=a.ratio, xfade_ms=a.xfade,
                                     with_audio=not a.no_audio,
                                     punch_in_auto=a.punch_in_auto)
        except (ValueError, KeyError) as exc:
            return emit(False, "BUILD_FAIL", f"生成失败:{exc}", exit_code=2)
        out = Path(a.out or "05_ir/project.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        manual = _manual_edits(out)
        if manual and not a.force:
            return emit(False, "IR_MANUAL_EDITS",
                        f"现存 IR 含手注痕迹,重建会冲掉:{';'.join(manual[:4])};"
                        "确认放弃手注请加 --force;要保留手注只重烧录用 python 06_output/rebuild.py"
                        "(S8,只用现有 ass,不碰 IR;BUGREPORT B8)", exit_code=2)
        errs = validate(doc, out.parent.parent if out.parent.name == "05_ir" else out.parent)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        msg = (f"IR 已生成:{doc['_meta']['keepSegments']} 段 / "
               f"{doc['_meta']['finalDurationMs'] / 1000:.1f}s")
        if errs:
            msg += f"(校验 {len(errs)} 个提示:src 文件可能尚未就位)"
        return emit(True, "IR_BUILT", msg, {"path": str(out), **doc["_meta"], "validateErrors": errs})

    p = Path(a.project or "")
    if not p.is_file():
        return emit(False, "NO_PROJECT", f"IR 文件不存在:{p}", exit_code=2)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return emit(False, "BAD_JSON", f"JSON 解析失败:{exc}", exit_code=2)
    errs = validate(doc, p.parent.parent)  # IR 在 05_ir/ 下,相对路径以工程根为基准
    if errs:
        return emit(False, "IR_INVALID", f"{len(errs)} 个问题(hint:逐条修复后重跑)", {"errors": errs}, exit_code=2)
    return emit(True, "IR_VALID", "IR 校验通过",
                {"slug": doc.get("slug"), "canvas": doc.get("canvas"),
                 "tracks": len(doc.get("tracks", []))})


if __name__ == "__main__":
    sys.exit(main())
