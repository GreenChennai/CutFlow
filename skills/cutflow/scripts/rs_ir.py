"""IR 校验与生成。
用法:python rs_ir.py validate <project.json>
      python rs_ir.py build --from-cutlist 04_粗剪决策/cutlist.applied.json --slug X --out 05_时间线工程/project.json
      python rs_ir.py build --from-cards 03_创作素材/artboard/manifest.json \\
             --anchors 00_制作简报/cards.json --wordline 05_时间线工程/wordline.json \\
             --voice 03_创作素材/vo/voice.wav --slug X --ratio 16x9 --out 05_时间线工程/project.json
      python rs_ir.py add-overlay 05_时间线工程/project.json --manifest 03_创作素材/artboard/manifest.json \\
             --plan 00_制作简报/cards.json [--track-name overlay] [--replace]

build 把 CutList 的 keep 区间转成 IR 主轨——**消灭「Agent 手写毫秒」这一整类误差**(rules/compose.md)。
build --from-cards(ADR-0027,I7):纯动画工程一条命令组装 IR——卡片↔旁白字符级锚点
分组、停顿中点切卡、冻结帧补长(freezeMs),此前每个工程要重写一遍脚本(安信德 GEO)。
add-overlay(T1-1 升格,原 _apply_overlay.py / 上一版 O2):口播+动画工程的 S4 官方装配入口——
按卡片时间窗表挂 overlay 轨 + 回写 manifest usedIn + 标 _meta.manualEdit,不再手写轨 JSON。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit, write_text_atomic  # noqa: E402
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量
from rs_align import keep_to_segments, map_src_to_final  # noqa: E402
from rs_common import RATIOS, content_text  # noqa: E402
import segmentation  # noqa: E402  — 标点口径与断句/字幕全链一致

CANVAS = {k: {"width": w, "height": h} for k, (w, h) in RATIOS.items()}
_CANVAS_PAIRS = tuple(RATIOS.values())

MOTION_IN = {"none", "fadeIn", "slideInLeft", "slideInRight", "scaleIn", "zoomIn"}
MOTION_OUT = {"none", "fadeOut", "slideOutLeft", "slideOutRight"}
# cut/none = 显式硬切(ADR-0026;R02/v2 M11:schema enum 合法值,渲染端 rs_render 有消费分支)
TRANSITIONS = {"fade", "wipeleft", "wipeup", "slideleft", "circleopen", "cut", "none"}
KINDS = {"video", "audio", "text"}
PUNCH_MIN_GAP_MS = 15000               # R3 punch-in 最小间隔(经验值,ITERATION-GUIDE §5.3)
REMOVED_CLIP_FIELDS = ("chroma", "background")   # v0.14(ADR-0031):抠像/背景合成已移除


def _validate_removed_fields(where: str, clip: dict, errs: list[str]) -> None:
    """v0.14(ADR-0031):IR 不再支持 chroma/background —— 抠像与背景合成由用户在交付前完成。

    旧工程 IR 若仍带这两个字段,明确报错并给出迁移指引,而不是静默忽略(否则用户会
    以为抠像仍在生效)。"""
    for f in REMOVED_CLIP_FIELDS:
        if f in clip:
            errs.append(f"{where}.{f} 已移除(ADR-0031):CutFlow 不再抠像 —— "
                        "请先在外部完成抠像+背景合成,再把成片素材交付剪辑")


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
                    import rs_asset   # M12:素材索引统一解析口(id/组名/旧名三级)
                    exists = rs_asset.resolve_sfx_ref(
                        clip["src"].split(":", 1)[1]) is not None
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
                _validate_removed_fields(where, clip, errs)
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


def _subtitle_refs(project_root: Path | None) -> dict:
    """IR 的 subtitle 相对引用:按工程实际目录名解析(旧结构工程出旧名,内引用不悬空)。"""
    if project_root is None:
        return {"ass": rs_paths.p("output") + "/subtitles.ass",
                "source": rs_paths.p("timeline") + "/wordline.json"}
    return {"ass": rs_paths.rel(project_root, "output", "subtitles.ass"),
            "source": rs_paths.rel(project_root, "timeline", "wordline.json")}


def _intent_bgm(project_root: Path) -> dict | None:
    """读意图编译产物里 bgm=auto 的曲库选曲(M8 接线口)。

    返回 {absPath, gainDb};未选曲/文件不存在 → None(不臆测,渲染端不加 BGM)。
    """
    p = project_root / rs_paths.p("brief") / "intent_decisions.json"
    if not p.is_file():
        return None
    try:
        bgm = (json.loads(p.read_text(encoding="utf-8")).get("resolved") or {}).get("bgm") or {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    rel = str(bgm.get("src") or "")
    if not rel or not bgm.get("enabled"):
        return None
    from rs_common import REPO_ROOT  # noqa: PLC0415
    abs_path = Path(rel)
    if not abs_path.is_absolute():
        abs_path = REPO_ROOT / rel
    if not abs_path.is_file():
        return None
    return {"absPath": abs_path, "gainDb": float(bgm.get("gainDb", -18))}


def build_from_cutlist(cutlist: dict, *, slug: str, ratio: str = "9x16",
                       xfade_ms: int = 8, with_audio: bool = True,
                       punch_in_auto: bool = False,
                       project_root: Path | None = None) -> dict:
    """CutList(keep 区间)→ IR 主视频/音频轨(时间一律经 map_src_to_final 换算)。"""
    keep = [[int(a), int(b)] for a, b in (cutlist.get("keep") or [])]
    if not keep:
        raise ValueError("cutlist 缺少 keep 区间(先跑 rs_cut.py --apply)")
    src = cutlist.get("source") or rs_paths.p("materials") + "/"
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
        # 原生 clipId(M4 寻址设计;实剪②发现:无 id 时 rs_edit 回退内容寻址,
        # 对重复 keep 片段 cf- 哈希碰撞 → BAD_ADDRESS。id 唯一且稳定=段序)
        # clipId 契约形态(V1-001;cutforge pattern ^[VAT][0-9]+-[0-9]{3}$,
        # 实剪②反馈:c001 形态被 cutforge 拒收 INTERNAL)
        clip["id"] = f"V1-{i + 1:03d}"
        video.append(clip)
        if with_audio:
            audio.append({"src": src, "startMs": cursor, "durationMs": dur,
                          "sourceInMs": a, "role": "voice"})
        cursor += dur

    return {
        "version": 1, "slug": slug, "fps": 30, "canvas": dict(CANVAS[ratio]),
        "tracks": [{"kind": "video", "clips": video},
                   {"kind": "audio", "clips": audio}],
        "subtitle": _subtitle_refs(project_root),
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
        "subtitle": _subtitle_refs(base_dir),
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
    # M9 桥升版:CutForge 编辑痕迹(schemaVersion 由 cutforge open/persist 写入,
    # fresh build 永不产生)——编辑器的改动同样是手改,S3 重建覆盖前必须显式 --force
    if old.get("schemaVersion"):
        hits.append(f"CutForge 编辑痕迹(schemaVersion={old['schemaVersion']};"
                    "编辑器改动须走 cutforge,重建请 --force)")
    for ti, t in enumerate(old.get("tracks", [])):
        for ci, c in enumerate(t.get("clips", [])):
            if c.get("chroma"):
                hits.append(f"tracks[{ti}].clips[{ci}].chroma")
            if c.get("background"):
                hits.append(f"tracks[{ti}].clips[{ci}].background")
    return hits


# ---------------------------------------------------------------- T1-1:add-overlay(原 _apply_overlay.py)

_OVERLAY_MOTION_KEYS = {"in", "inMs", "out", "outMs"}   # P8 教训:只写 schema 允许的字段


def parse_overlay_plan(plan: object) -> list[dict]:
    """卡片时间窗表 → 规范化条目 [{card, startMs, durationMs, motion, freezeMs?}]。

    兼容两种键名:O2 原始口径的 `card` 与 gen-cards 计划的 `id`;与 rs_artboard
    gen-cards **共用同一份 cards.json** —— 内容字段(id/title/lines/…)在此放行不消费
    (归 gen-cards),白名单外字段仍硬报错(P8:静默吞字段 = 幽灵键一路漏到 schema 才炸);
    按 startMs 升序稳定排序。
    """
    from rs_artboard import PLAN_CONTENT_KEYS  # noqa: PLC0415 — 白名单单一来源
    known = PLAN_CONTENT_KEYS | {"card", "id", "startMs", "durationMs", "motion", "freezeMs"}
    if not isinstance(plan, list) or not plan:
        raise ValueError("计划必须是数组且非空:[{card|id, startMs, durationMs, motion?}]")
    out: list[dict] = []
    for i, raw in enumerate(plan):
        where = f"plan[{i}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where} 不是对象")
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"{where} 有白名单外字段 {unknown}"
                             "(P8:overlay 轨只写 schema 允许的字段)")
        card = str(raw.get("card") or raw.get("id") or "").strip()
        if not card:
            raise ValueError(f"{where} 缺 card/id")
        for k in ("startMs", "durationMs"):
            v = raw.get(k)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                raise ValueError(f"{where}.{k} 非法:{v!r}(须为非负毫秒数)")
        if raw["durationMs"] <= 0:
            raise ValueError(f"{where}.durationMs 必须为正:{raw['durationMs']}")
        motion = raw.get("motion") or {}
        if not isinstance(motion, dict):
            raise ValueError(f"{where}.motion 必须是对象")
        bad = sorted(set(motion) - _OVERLAY_MOTION_KEYS)
        if bad:
            raise ValueError(f"{where}.motion 有白名单外字段 {bad}(合法:{sorted(_OVERLAY_MOTION_KEYS)})")
        entry = {"card": card, "startMs": int(raw["startMs"]),
                 "durationMs": int(raw["durationMs"]), "motion": dict(motion)}
        if raw.get("freezeMs") is not None:
            fz = raw["freezeMs"]
            if not isinstance(fz, (int, float)) or isinstance(fz, bool) or fz <= 0:
                raise ValueError(f"{where}.freezeMs 非法:{fz!r}(须为正毫秒数)")
            entry["freezeMs"] = int(fz)
        out.append(entry)
    out.sort(key=lambda c: c["startMs"])
    return out


def resolve_card_srcs(plan: list[dict], manifest: dict, root: Path,
                      manifest_dir: Path) -> tuple[dict[str, str], list[str]]:
    """卡片 id → IR 产物路径(工程根相对)。两种清单口径都归一:
    工程根相对(test_v5/实工程手登记)直接用;artboard 目录相对(scan 原生)补前缀。
    判定唯一依据 = 该路径从工程根出发真实存在;两处都不存在 = 硬问题。
    """
    items = {str(it.get("id")): it for it in (manifest.get("items") or [])}
    rel_prefix = manifest_dir.relative_to(root).as_posix() if manifest_dir.is_relative_to(root) else ""
    srcs: dict[str, str] = {}
    issues: list[str] = []
    for p in plan:
        cid = p["card"]
        item = items.get(cid)
        if item is None:
            issues.append(f"{cid}:不在 manifest(先 rs_artboard --scan 或 gen-cards)")
            continue
        out = str(item.get("output", ""))
        cands = [out, f"{rel_prefix}/{out}"] if rel_prefix and not out.startswith(f"{rel_prefix}/") else [out]
        for c in cands:
            if (root / c).is_file():
                srcs[cid] = c
                break
        else:
            issues.append(f"{cid}:产物不存在,先跑 rs_artboard --export / export-fallback(试过 {cands})")
    return srcs, issues


def build_overlay_track(plan: list[dict], srcs: dict[str, str]) -> tuple[list[tuple[str, dict]], list[str]]:
    """时间窗表 → [(卡片id, clip)](按 startMs 升序;返回 (挂轨对, issues))。"""
    pairs: list[tuple[str, dict]] = []
    issues: list[str] = []
    for p in plan:
        if p["card"] not in srcs:
            continue
        clip: dict = {"src": srcs[p["card"]], "startMs": p["startMs"],
                      "durationMs": p["durationMs"]}
        if p["motion"]:
            clip["motion"] = p["motion"]
        if p.get("freezeMs"):
            clip["freezeMs"] = p["freezeMs"]
        pairs.append((p["card"], clip))
    spans = sorted((c["startMs"], c["startMs"] + c["durationMs"]) for _, c in pairs)
    for a, b in zip(spans, spans[1:]):
        if b[0] < a[1]:
            issues.append(f"卡片时间窗重叠:{a} 与 {b}(同一时间只够铺一层)")
    return pairs, issues


def add_overlay(doc: dict, manifest: dict, plan: list[dict], *, root: Path,
                manifest_dir: Path, track_name: str = "overlay",
                replace: bool = False) -> tuple[dict, dict, list[str]]:
    """把卡片时间窗表挂成 IR overlay 轨(返回 (新 IR, 摘要, issues))。

    · 挂轨:同名 video 轨已存在时,未 --replace 即报错(防重复挂轨);
      --replace 先清掉旧轨 clips 在各 manifest 卡片上的 usedIn 痕迹再换;
    · 写 usedIn:track/clipIndex/startMs/durationMs,按 (track, clipIndex) 去重;
    · 标 manualEdit:这是官方手改入口,重建护栏(_manual_edits)必须认得它。
    """
    srcs, issues = resolve_card_srcs(plan, manifest, root, manifest_dir)
    pairs, ov_issues = build_overlay_track(plan, srcs)
    issues.extend(ov_issues)
    if issues:
        return doc, {}, issues
    clips = [c for _, c in pairs]

    tracks = doc.setdefault("tracks", [])
    existing = [i for i, t in enumerate(tracks)
                if t.get("kind") == "video" and t.get("name") == track_name]
    by_id = {str(it.get("id")): it for it in (manifest.get("items") or [])}

    if existing:
        if not replace:
            return doc, {}, [f"轨「{track_name}」已存在(确认替换请加 --replace,"
                             "或换 --track-name 另挂一层)"]
        ti = existing[0]
        # 换轨先清旧挂点:旧轨占用的 track 下标在所有卡片上的 usedIn 一并撤下,防陈旧残留
        for it in manifest.get("items", []):
            it["usedIn"] = [u for u in it.get("usedIn", []) if u.get("track") != ti]
        tracks[ti]["clips"] = clips
    else:
        tracks.append({"kind": "video", "name": track_name, "clips": clips})
        ti = len(tracks) - 1

    used_cards: list[str] = []
    for ci, (cid, c) in enumerate(pairs):
        item = by_id.get(cid)
        if item is None:
            continue
        item.setdefault("usedIn", [])
        if not any(u.get("track") == ti and u.get("clipIndex") == ci for u in item["usedIn"]):
            item["usedIn"].append({"track": ti, "clipIndex": ci,
                                   "startMs": c["startMs"], "durationMs": c["durationMs"]})
        used_cards.append(cid)
    meta = doc.setdefault("_meta", {})
    meta["manualEdit"] = True
    meta["manualEditNote"] = f"add-overlay:{len(clips)} 卡挂轨「{track_name}」"
    summary = {"track": ti, "trackName": track_name, "clipCount": len(clips),
               "cards": used_cards, "replaced": bool(existing)}
    return doc, summary, issues


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["validate", "build", "add-overlay", "add-matte"])
    ap.add_argument("project", nargs="?")
    ap.add_argument("--clip", help="add-matte:目标 clipId(主轨)")
    ap.add_argument("--quality", help="add-matte:rs_matting gate 产出的 matte/quality.json")
    ap.add_argument("--alpha-dir", dest="alpha_dir", default="",
                    help="add-matte:alpha PNG 序列目录(默认 05_时间线工程/matte/alpha)")
    ap.add_argument("--matte-bg", dest="matte_bg", default="",
                    help="add-matte:背景图路径(写进 matte.bg.src;cover 模式)")
    ap.add_argument("--from-cutlist")
    ap.add_argument("--from-cards", dest="from_cards",
                    help="I7 纯动画组装:artboard manifest.json(卡清单);"
                         "配合 --anchors/--wordline/--voice")
    ap.add_argument("--anchors", help="卡片↔旁白分组表 JSON:[{\"card\":…,\"match\":\"词A|词B\"}]"
                                      "(顺序=卡片顺序;match 归一化后必须与旁白逐字一致)")
    ap.add_argument("--wordline", help="from-cards:05_时间线工程/wordline.json(时间唯一来源)")
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
    ap.add_argument("--manifest", help="add-overlay:artboard manifest.json(卡片 id → 产物路径)")
    ap.add_argument("--plan", help="add-overlay:卡片时间窗表 JSON:[{card|id, startMs, durationMs, motion}]"
                                  "(与 rs_artboard gen-cards 共用一份 cards.json)")
    ap.add_argument("--track-name", dest="track_name", default="overlay",
                    help="add-overlay:overlay 轨名(默认 overlay;同名轨已存在须 --replace)")
    ap.add_argument("--replace", action="store_true",
                    help="add-overlay:同名轨已存在时替换其 clips(默认报错,防重复挂轨)")
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
            out = Path(a.out or rs_paths.p("timeline") + "/project.json")
            out.parent.mkdir(parents=True, exist_ok=True)
            manual = _manual_edits(out)
            if manual and not a.force:
                return emit(False, "IR_MANUAL_EDITS",
                            f"现存 IR 含手注痕迹,重建会冲掉:{';'.join(manual[:4])};"
                            f"确认放弃手注请加 --force(I7 生成式产物同样不得手改)", exit_code=2)
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
            cl_doc = json.loads(cl_path.read_text(encoding="utf-8"))
            doc = build_from_cutlist(cl_doc,
                                     slug=a.slug, ratio=a.ratio, xfade_ms=a.xfade,
                                     with_audio=not a.no_audio,
                                     punch_in_auto=a.punch_in_auto,
                                     project_root=(cl_path.parent.parent
                                                   if rs_paths.is_stage_dirname(cl_path.parent.name)
                                                   else None))
        except (ValueError, KeyError) as exc:
            return emit(False, "BUILD_FAIL", f"生成失败:{exc}", exit_code=2)
        # M8(清欠账 #13):bgm=auto 的曲库选曲从意图编译产物接线进 IR。
        # 最小接线不重构:resolved.bgm 有 src(库选)且 IR 尚无 bgm 才落;增益用
        # resolved.bgm.gainDb(节奏档/风格包是增益真相源,曲库 pick 只提供文件)。
        # src 落【绝对路径】—— rs_render step_mix 以工程根解析相对路径,而曲库在
        # 仓库不在工程;rs_edit bgm.set 的存在性检查对绝对路径同样成立。
        root_guess = (cl_path.parent.parent
                      if rs_paths.is_stage_dirname(cl_path.parent.name) else Path.cwd())
        bgm_lib = _intent_bgm(root_guess)
        if bgm_lib and not doc.get("bgm"):
            doc["bgm"] = {"src": str(bgm_lib["absPath"]),
                          "gainDb": float(bgm_lib["gainDb"]), "ducking": True}
            doc.setdefault("_meta", {})["bgmFrom"] = "intent-library"
        out = Path(a.out or rs_paths.p("timeline") + "/project.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        # P27-3(副文档 07):「改 keep 但 wordline 时长字段未同步」在 S3 入口就报错,
        # 不等 rs_sync 事后挂红叉。给出修复命令而非静默继续。
        wl_path = out.parent / "wordline.json"
        if wl_path.is_file() and (cl_doc.get("srcTotalMs") or 0) > 0:
            try:
                wl_doc = json.loads(wl_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                wl_doc = None
            if isinstance(wl_doc, dict) and wl_doc.get("srcDurationMs") is not None:
                from rs_common import duration_ledger_error
                if abs(int(wl_doc["srcDurationMs"]) - int(cl_doc["srcTotalMs"])) > 1:
                    ledger = (f"wordline.srcDurationMs({wl_doc['srcDurationMs']}) 与 "
                              f"cutlist.srcTotalMs({cl_doc['srcTotalMs']}) 不一致")
                else:
                    ledger = duration_ledger_error({
                        "srcDurationMs": cl_doc["srcTotalMs"],
                        "removedMs": cl_doc.get("removedMs") or 0,
                        "finalDurationMs": wl_doc.get("finalDurationMs"),
                    })
                if ledger:
                    return emit(False, "DURATION_LEDGER",
                                f"{ledger}。先平账再建 IR:python <scripts>/rs_cut.py --apply "
                                f"{cl_path}(自动同步 wordline 三个时长字段),"
                                "或 rs_align.py refresh-durations <wordline> --media <素材>",
                                exit_code=2)
        manual = _manual_edits(out)
        if manual and not a.force:
            return emit(False, "IR_MANUAL_EDITS",
                        f"现存 IR 含手注痕迹,重建会冲掉:{';'.join(manual[:4])};"
                        f"确认放弃手注请加 --force;要保留手注只重烧录用 "
                        f"python {rs_paths.p('output')}/rebuild.py"
                        "(S8,只用现有 ass,不碰 IR;BUGREPORT B8)", exit_code=2)
        errs = validate(doc, out.parent.parent if rs_paths.is_stage_dirname(out.parent.name)
                        else out.parent)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        msg = (f"IR 已生成:{doc['_meta']['keepSegments']} 段 / "
               f"{doc['_meta']['finalDurationMs'] / 1000:.1f}s")
        if errs:
            msg += f"(校验 {len(errs)} 个提示:src 文件可能尚未就位)"
        return emit(True, "IR_BUILT", msg, {"path": str(out), **doc["_meta"], "validateErrors": errs})

    if a.command == "add-matte":
        # M9(ADR-0050):gate 判定写进 clip.matte;verdict ∈ pass|warn 才放行,
        # fail/blocked 拒绝写入——「不达标不启用」是本 ADR 的核心纪律。
        if not a.project or not a.clip or not a.quality:
            return emit(False, "NO_INPUT",
                        "add-matte 需要:<project.json> --clip <clipId> --quality <quality.json>",
                        exit_code=2)
        ir_path = Path(a.project)
        if not ir_path.is_file():
            return emit(False, "NO_PROJECT", f"IR 不存在:{ir_path}", exit_code=2)
        doc = json.loads(ir_path.read_text(encoding="utf-8"))
        q = json.loads(Path(a.quality).read_text(encoding="utf-8"))
        if q.get("verdict") not in ("pass", "warn"):
            return emit(False, "MATTE_QUALITY_FAIL",
                        f"抠像判定 {q.get('verdict')} 不达启用档(仅 pass/warn 可入 IR);"
                        "建议改走用户预处理(ADR-0031 默认路径)", {"quality": q}, exit_code=4)
        clip_hit = next((c for t in doc.get("tracks", []) if t.get("kind") == "video"
                         for c in t.get("clips", []) if c.get("id") == a.clip), None)
        if clip_hit is None:
            return emit(False, "NO_CLIP", f"主轨找不到 clipId {a.clip}", exit_code=2)
        matte = {"engine": q.get("engine", "rvm"),
                 "quality": {"verdict": q["verdict"], "metrics": q.get("metrics", {})},
                 "alphaDir": a.alpha_dir or f"{rs_paths.p('timeline')}/matte/alpha",
                 "cacheVer": q.get("cacheVer", "")}
        if a.matte_bg:
            matte["bg"] = {"src": a.matte_bg, "mode": "cover"}
        clip_hit["matte"] = matte
        errs = validate(doc, ir_path.parent.parent
                        if rs_paths.is_stage_dirname(ir_path.parent.name) else ir_path.parent)
        ir_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return emit(True, "MATTE_APPLIED",
                    f"matte 已写入 {a.clip}(verdict={q['verdict']},engine={q.get('engine')})",
                    {"clipId": a.clip, "verdict": q["verdict"], "validateErrors": errs})

    if a.command == "add-overlay":
        if not a.project or not a.manifest or not a.plan:
            return emit(False, "NO_INPUT",
                        "add-overlay 需要:<project.json> --manifest <artboard manifest> "
                        "--plan <cards.json>(O2 升格:挂轨+usedIn+manualEdit 一条命令)", exit_code=2)
        root = Path(a.root).resolve()
        ir_path = Path(a.project)
        if not ir_path.is_file():
            return emit(False, "NO_PROJECT", f"IR 不存在:{ir_path}", exit_code=2)
        mpath = Path(a.manifest)
        if not mpath.is_file():
            return emit(False, "NO_MANIFEST",
                        f"artboard 清单不存在:{mpath}(先 rs_artboard --scan / gen-cards)", exit_code=2)
        try:
            doc = json.loads(ir_path.read_text(encoding="utf-8"))
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
            plan = parse_overlay_plan(json.loads(Path(a.plan).read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValueError) as exc:
            return emit(False, "BAD_PLAN", f"计划或文件解析失败:{exc}", exit_code=2)
        if doc.get("schemaVersion") and not a.force:
            return emit(False, "IR_MANUAL_EDITS",
                        f"IR 带 CutForge 编辑痕迹(schemaVersion={doc['schemaVersion']});"
                        "编辑器工程请走 cutforge 挂轨;确认绕过请 --force", exit_code=2)
        doc, summary, issues = add_overlay(doc, manifest, plan, root=root,
                                           manifest_dir=mpath.resolve().parent,
                                           track_name=a.track_name, replace=a.replace)
        if issues:
            return emit(False, "OVERLAY_ISSUES",
                        f"{len(issues)} 个问题,已停止(不带着坏输入往下跑)",
                        {"issues": issues}, exit_code=2)
        errs = validate(doc, root)
        if errs:
            return emit(False, "IR_INVALID", f"挂轨后 IR 校验 {len(errs)} 个问题",
                        {"errors": errs}, exit_code=2)
        write_text_atomic(ir_path, json.dumps(doc, ensure_ascii=False, indent=1))
        write_text_atomic(mpath, json.dumps(manifest, ensure_ascii=False, indent=1))
        return emit(True, "OVERLAY_OK",
                    f"{summary['clipCount']} 卡挂入轨「{summary['trackName']}」(track {summary['track']});"
                    "usedIn 已回写 manifest;_meta.manualEdit 已标(重建护栏认得这次手改)",
                    {**summary, "staleStages": ["S4", "S5", "S8"], "ir": str(ir_path)})

    p = Path(a.project or "")
    if not p.is_file():
        return emit(False, "NO_PROJECT", f"IR 文件不存在:{p}", exit_code=2)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return emit(False, "BAD_JSON", f"JSON 解析失败:{exc}", exit_code=2)
    errs = validate(doc, p.parent.parent)  # IR 在 05_时间线工程/ 下,相对路径以工程根为基准
    if errs:
        return emit(False, "IR_INVALID", f"{len(errs)} 个问题(hint:逐条修复后重跑)", {"errors": errs}, exit_code=2)
    return emit(True, "IR_VALID", "IR 校验通过",
                {"slug": doc.get("slug"), "canvas": doc.get("canvas"),
                 "tracks": len(doc.get("tracks", []))})


if __name__ == "__main__":
    sys.exit(main())
