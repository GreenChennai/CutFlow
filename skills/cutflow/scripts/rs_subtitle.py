"""字幕:时间轴来源(Wordline / TTS manifest / transcript)→ SRT + ASS(DP 卡切分 + 风格模板)。

用法:
  rs_subtitle.py --from-wordline 05_时间线工程/wordline.json --style talkshow-bold --ratio 9x16 --out 06_成片输出
  rs_subtitle.py --from-tts 03_创作素材/tts/manifest.json --style tutorial-clean --ratio 9x16 --out 06_成片输出
  rs_subtitle.py --from-transcript 02_转写与校对/transcript_corrected.json --ratio 16x9 --out 06_成片输出

核心变化(ADR-0001 修订 / ADR-0011):
  · 卡切分走约束最优 DP(segmentation.py),不再是长度驱动
  · **卡的时间从 Wordline 字级时间戳聚合**(首字 startMs-20ms ~ 末字 endMs+20ms),
    彻底废除「按字符数比例插值」
  · 无字级时间时,统一经 rs_align.build_wordline 生成(并在报告里标注 degraded),不另立第二套时间
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rs_common  # noqa: E402
from rs_common import RATIOS, canvas_for, emit  # noqa: E402
import segmentation  # noqa: E402
import textopt  # noqa: E402

PLATFORMS_PATH = Path(__file__).resolve().parents[1] / "templates" / "platforms.json"


def load_platforms() -> dict:
    """读平台预设档案(templates/platforms.json)。缺文件返回空表,不阻塞普通出片。"""
    if not PLATFORMS_PATH.is_file():
        return {}
    try:
        return json.loads(PLATFORMS_PATH.read_text(encoding="utf-8")).get("platforms") or {}
    except (json.JSONDecodeError, OSError):
        return {}


def resolve_max_chars(explicit: int | None, preset: dict, ratio: str) -> int:
    """生效的每卡字数:显式参数 > 平台预设 > 策略表(T4.4,segmentation.max_chars_for 共读)。"""
    return explicit or preset.get("maxChars") or segmentation.max_chars_for(ratio)


def resolve_platform(name: str | None) -> dict:
    """平台名 → 预设;未知名直接报错(不静默退回默认,否则会出一版错规格的片子)。"""
    if not name:
        return {}
    table = load_platforms()
    if name not in table:
        raise KeyError(f"未知平台 {name!r}(可选 {'/'.join(table) or '无'})")
    return dict(table[name])

MAX_CHARS = dict(segmentation.MAX_CHARS)          # 9x16=12 / 16x9=22(竖屏已从 16 下调)
TAIL_STOP = "。!?…;:!?"
MID_STOP = ",,、;:? "
TAIL_FUNC = "的了着地吧呢啊吗嘛"
BAD_END = "我你他她它们这那就都也很不有在和与跟对往朝从被把将"

FRAME_MS = 1000 / 30.0                            # 卡间距 ≥2 帧
MIN_GAP_FRAMES = 2

RELEASE_MS = segmentation.RELEASE_MS             # 卡相对首/末字的时间释放余量
EXTEND_MAX_S = 0.30                               # 为凑最短时长可延长的后沿上限(秒)

_PUNCT_ONLY = set("。,、;::!?!?…,. ;:~·—–-()()《》「」『』【】[]“”‘’\"' ")

# ---------------------------------------------------------------- 双 Style(M8 短剧/影视解说,方案 §5.5.3)
# 解说/原声对白双 Style:解说体 Main(常规样式)+ 原声对白体 Quote(暖黄斜体,
# 视觉可区分);对白文本自动包中文引号。voice 标记来自校对后的 wordline 句级
# 字段(sentences[].voice = "dialogue");无标记 → 全部按解说体(降级留痕)。
QUOTE_OPEN, QUOTE_CLOSE = "\u201c", "\u201d"   # “ ”
QUOTE_RESERVE_CHARS = 2                        # 对白卡为引号预留的字数预算(sub.pair 描述符同源)
VOICE_COMMENTARY, VOICE_DIALOGUE = "commentary", "dialogue"
QUOTE_STYLE_NAME = "Quote"                     # ASS 第二 Style 段名
QUOTE_COLOR = "&H0000E5FF"                     # 暖黄(BGR);与白字解说体一眼可分

# ---------------------------------------------------------------- 字体(链接 artboard,不自建;分册01 §5 / ADR-0053)
# 唯一真相源 = artboard 的 fonts/;本仓只存索引与对拍表 templates/fonts.json
# (由 tools/synth_assets.py --fonts-only 生成,**禁手改**)。STYLES 的 font 字段
# 恒为 None 哨兵,写入 ASS 时经 resolve_font() 查表——不再硬编码任何系统字体名。
# M14:FONTS_JSON 读口统一 rs_common.FONTS_JSON(三处合一),本模块不再自持路径。
FONT_FALLBACK = "Noto Sans SC"    # 内置兜底(开源可嵌;fonts.json 缺失/坏表时 + WARN)
_font_state: dict = {"family": None, "degraded": False, "reason": ""}


def load_fonts() -> dict | None:
    """读字体索引表;缺失/损坏 → None(调用方降级 + 留痕,不臆测)。

    M14:读表委托 rs_common.load_fonts_doc()(三处合一的共享读口)。"""
    return rs_common.load_fonts_doc()


def resolve_font() -> str:
    """ASS Style 的 Fontname(查表兜底,全链唯一解析口)。

    链条:fonts.json 的 default.subtitle(artboard 字体目录名)→ 该目录代表款
    的字体内部家族名(family 字段,生成时由 FreeType 实读)。查表任何一步落空
    → FONT_FALLBACK + WARN(fontDegraded 留痕;回滚档:删 fonts.json 即回到此)。
    M14:查表逻辑委托 rs_common.resolve_font_family(),本侧只保留缓存/WARN/
    代表款在盘校验等字幕链自己的口径。
    """
    if _font_state["family"] is not None:
        return _font_state["family"]
    fonts = load_fonts()
    family = None
    if fonts:
        family = rs_common.resolve_font_family()
        if family:
            dkey = (fonts.get("default") or {}).get("subtitle")
            hit = next((f for f in fonts["fonts"] if f.get("dir") == dkey), None) if dkey else None
            base = rs_common.artboard_dir() or Path(str(fonts.get("artboardLockedPath", "")))
            if not hit.get("file") or not (base / "fonts" / dkey / str(hit["file"])).is_file():
                # 代表款本地缺席(artboard 按需下载机制):仍写家族名(装上即生效),但留痕
                _font_state["reason"] = f"artboard 代表款未下载:{dkey}/{hit.get('file')}(fetch_font 可补)"
                print(f"[rs_subtitle] WARN fontDegraded-pending {_font_state['reason']}",
                      file=sys.stderr)
    if family is None:
        family = FONT_FALLBACK
        _font_state["degraded"] = True
        _font_state["reason"] = (f"templates/fonts.json 缺失或 default.subtitle 无 family,"
                                 f"降级内置兜底 {FONT_FALLBACK}")
        print(f"[rs_subtitle] WARN fontDegraded {_font_state['reason']}", file=sys.stderr)
    _font_state["family"] = family
    return family


def font_state() -> dict:
    """字体解析留痕(CLI data / 测试断言用);resolve_font 后取值。"""
    return dict(_font_state)


STYLES = {
    "talkshow-bold": {
        "font": None, "size": {"9x16": 78, "3x4": 72, "16x9": 64},
        "primary": "&H00FFFFFF", "outline": "&H00101010", "back": "&H80000000",
        "outline_w": 3, "shadow": 1, "margin_v": {"9x16": 500, "3x4": 400, "16x9": 180},
        "align": 2, "bold": 1,
    },
    "tutorial-clean": {
        "font": None, "size": {"9x16": 62, "3x4": 58, "16x9": 56},
        "primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H60000000",
        "outline_w": 2, "shadow": 0, "margin_v": {"9x16": 520, "3x4": 430, "16x9": 180},
        "align": 2, "bold": 0, "border_style": 3,
    },
    "subtitle-white": {
        "font": None, "size": {"9x16": 68, "3x4": 64, "16x9": 58},
        "primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H00000000",
        "outline_w": 2, "shadow": 1, "margin_v": {"9x16": 320, "3x4": 280, "16x9": 180},
        "align": 2, "bold": 1,
    },
}


# ---------------------------------------------------------------- 行断开(保留评分算法)

def score_break(line: str, pos: int) -> int:
    left, right = line[:pos], line[pos:]
    s = 0
    if left and left[-1] in MID_STOP + TAIL_STOP:
        s += 100
    if right and right[0] in MID_STOP:
        s += 90
    if right and right[0] == " ":
        s += 90
    if left and left[-1] in TAIL_FUNC:
        s += 30
    if left and left[-1] in BAD_END:
        s -= 150
    s -= 4 * pos
    return s


def pyramid_fix(lines: list[str]) -> list[str]:
    """金字塔形 + 禁顶行 1–2 字(rules/subtitles.md §2)。仅在安全时搬字。"""
    if len(lines) < 2:
        return lines
    last, prev = lines[-1], lines[-2]
    if len(last) > 2:
        return lines
    move = min(3 - len(last), len(prev) - 3)
    if move <= 0:
        return lines
    seg = prev[-move:]
    if any(c.isascii() and c.isalnum() for c in seg):
        return lines
    if last and last[0].isascii() and last[0].isalnum():
        return lines
    lines = list(lines)
    lines[-2] = prev[:-move].rstrip()
    lines[-1] = (prev[-move:] + last).strip()
    return lines


def _break_rec(line: str, max_chars: int) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    best, best_s = None, None
    for p in range(3, max_chars - 1):
        if len(line) > p + 1 and line[p - 1].isascii() and line[p - 1].isalnum() \
                and line[p].isascii() and line[p].isalnum():
            continue
        s = score_break(line, p)
        if best_s is None or s > best_s:
            best, best_s = p, s
    if best is None:
        best = max_chars
    return [line[:best].rstrip()] + _break_rec(line[best:].lstrip(), max_chars)


def break_line(line: str, max_chars: int, pyramid: bool = True) -> list[str]:
    lines = _break_rec(line, max_chars)
    return pyramid_fix(lines) if pyramid else lines


# ---------------------------------------------------------------- 时间格式

def _ts_srt(sec: float) -> str:
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    s, ms = int(sec % 60), int((sec % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ts_ass(sec: float) -> str:
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


# ---------------------------------------------------------------- Wordline 路径(唯一时间来源)

def _sentence_slices(wl: dict) -> list[tuple[str, list[int], dict, str]]:
    """把 Wordline 切成 (原始文本, 位置→chars下标 映射, 停顿表, 声轨标记)。

    声轨标记 = 句级 voice 字段("dialogue"=原声对白,其余/缺省=解说);
    双 Style(--dual-style)据此分派 ASS Style 与引号。
    """
    chars = wl.get("chars", [])
    sents = wl.get("sentences") or []
    out = []
    if not sents:
        sents = [{"id": 0, "span": [0, len(chars)]}]
    for s in sents:
        a, b = s.get("span", [0, 0])
        a, b = max(0, int(a)), min(int(b), len(chars))
        if b <= a:
            continue
        text = "".join(c["ch"] for c in chars[a:b])
        idxmap = list(range(a, b))
        gaps = {}
        for k in range(1, b - a):
            d = int(chars[a + k]["startMs"]) - int(chars[a + k - 1]["endMs"])
            if d > 0:
                gaps[k] = float(d)
        voice = str(s.get("voice") or VOICE_COMMENTARY)
        out.append((text, idxmap, gaps, voice))
    return out


def _to_cards(events: list[dict]) -> list[dict]:
    """事件 → 约束校验用的卡结构(与 events_from_wordline 尾部同口径)。"""
    out = []
    for i, e in enumerate(events):
        n_chars = len(e["text"].replace(" ", ""))
        dur_ms = int(round((e["end"] - e["start"]) * 1000))
        out.append({"i": i, "text": e["text"], "chars": n_chars,
                    "startMs": int(e["start"] * 1000), "endMs": int(e["end"] * 1000),
                    "durMs": dur_ms,
                    "cps": round(n_chars / (dur_ms / 1000.0), 2) if dur_ms else 0.0})
    return out


def _dp_events(wl: dict, max_chars: int, *, terms=(), top: int = 3,
               mode: str = "dp", dual_style: bool = False,
               engine: str | None = None,
               degrade_notes: list | None = None) -> tuple[list[dict], list[dict], list[str], int, list[dict]]:
    """逐句 DP 切分 → 原始事件(未必并/未延长/未间距),附候选与降级留痕。

    events_from_wordline 与 override 的 partial 模式共用(B7:部分替换需要
    DP 分组做基底)。
    dual_style=True 时:对白句的切分预算按 max_chars−QUOTE_RESERVE_CHARS 收紧
    (引号在出卡时补上,显示字形不超每卡上限);事件携带 voice 标记。
    第四册 T4.6:候选里输出 termsHit(术语命中)与 cuts(每句选中切点,
    segscore / review queue 的输入)。
    """
    events: list[dict] = []
    candidates: list[dict] = []
    seg_degrade: list[str] = []      # 单句 DP 失败 → 退回长度算法,但必须留痕
    word_fb_count = 0                # 词内全禁无可行解、走了词内强惩罚的句数(留痕)
    seg_infos: list[dict] = []       # T4.7 segscore 输入:每句 {text, cuts, gaps}
    for text, idxmap, gaps, voice in _sentence_slices(wl):
        if all(ch in _PUNCT_ONLY or not ch.strip() for ch in text):
            continue          # 纯标点句跳过:DP 对它产卡缺 startMs(会以 0.0s 污染排序)
        eff_max = max_chars
        if dual_style and voice == VOICE_DIALOGUE:
            # 引号预算:对白卡出卡时自动包“ ”,切分阶段先扣掉,显示不超上限
            eff_max = max(4, max_chars - QUOTE_RESERVE_CHARS)
        if mode == "length":
            cards = [{"i": i, "text": c, "start": None, "end": None}
                     for i, c in enumerate(textopt.card_split(text, eff_max, mode="length"))]
            plan = {"cards": cards, "violations": [], "ambiguous": False}
            cuts: list[int] = []
        else:
            try:
                plan = segmentation.segment(text, eff_max, gaps=gaps, index_map=idxmap,
                                            char_times=wl.get("chars"), terms=terms, top=top,
                                            engine=engine, degrade=degrade_notes)
            except Exception as exc:  # noqa: BLE001 — 单句分段失败不该炸掉整条字幕
                seg_degrade.append(f"句「{text[:12]}」DP 分段失败,退回长度算法"
                                   f"({type(exc).__name__}: {exc})")
                plan = {"cards": [{"i": i, "text": c, "startMs": None, "endMs": None}
                                  for i, c in enumerate(textopt.card_split_length(text, eff_max))],
                        "violations": [], "ambiguous": False, "plans": []}
            cuts = list(plan.get("cuts") or [])
        word_fb_count += 1 if plan.get("wordFallback") else 0
        seg_infos.append({"text": text, "cuts": cuts, "gaps": dict(gaps),
                          "semanticHits": plan.get("semanticHits") or []})
        candidates.append({"sentence": text, "ambiguous": plan.get("ambiguous", False),
                           "voice": voice,
                           "termsHit": [t for t in terms if t and t in text],
                           "cuts": cuts,
                           "plans": [{"score": p["score"], "cards": [c["text"] for c in p["cards"]]}
                                     for p in plan.get("plans", [])]})
        for c in plan["cards"]:
            cleaned = textopt._clean_card(c["text"])
            if not cleaned:
                continue
            if c.get("startMs") is None:            # length 模式:退化为句内均分(仅复现旧工程)
                ev = {"start": 0.0, "end": 0.0, "text": cleaned, "degraded": True}
            else:
                ev = {"start": c["startMs"] / 1000.0, "end": c["endMs"] / 1000.0, "text": cleaned}
                # 字级锚点:后沿/起点调整只能在释放余量内做(见 _enforce_gaps / _extend_short)
                ev["anchorStart"] = c.get("anchorStartMs", c["startMs"] + RELEASE_MS) / 1000.0
                ev["anchorEnd"] = c.get("anchorEndMs", c["endMs"] - RELEASE_MS) / 1000.0
            if c.get("charSpan"):
                # Agent 复核定位用:卡 ↔ wordline 内容字全局索引(ADR-0020)
                ev["charSpan"] = list(c["charSpan"])
            if dual_style:
                ev["voice"] = voice
            events.append(ev)
    return events, candidates, seg_degrade, word_fb_count, seg_infos


def events_from_wordline(wl: dict, max_chars: int, *, terms=(), top: int = 3,
                         mode: str = "dp", karaoke: bool = False,
                         cps_max: float | None = None,
                         dual_style: bool = False,
                         engine: str | None = None) -> tuple[list[dict], dict]:
    """Wordline → 字幕事件。卡时间 = 首字/末字时间戳聚合(align.md §4)。

    `wl.charTimingEstimated`(无字级时间戳)时,卡内位置是**估算**的:仍按 max_chars
    出卡以保证可读性,但在 `degradeReasons` 里显式标注"卡内位置为估算",并由
    `meta["charTimingEstimated"]` 告知上游 —— 真正的字级时间由 `rs_dub align`(#10)补齐。

    第四册 T4.1/T4.2/T4.3:有真实字级时间时,后处理**只调时间不碰文本**(time-only);
    字级信息缺失/降级才保留旧可读性后处理(legacy-readability),且必须留痕。
    meta 增:postProcess / unsatisfied / segscore(T4.7)/ reviewQueue(T4.12)。

    dual_style=True(方案 §5.5.3):对白句(voice=dialogue)切分预算预留引号位,
    事件携带 voice;并卡/吞卡不跨声轨(解说卡与对白卡不合并)。
    """
    degrade_notes: list[str] = []
    events, candidates, seg_degrade, word_fb_count, seg_infos = _dp_events(
        wl, max_chars, terms=terms, top=top, mode=mode, dual_style=dual_style,
        engine=engine, degrade_notes=degrade_notes)

    events.sort(key=lambda e: e["start"])
    kar_attached = 0
    if karaoke:
        # dev-jj2815 实测:必须**先挂字再排卡**。_clean_card 剥掉的标点会在卡拉OK
        # 显示层经 chars 原样带回(「能动性,」显示 5 字形),必并/合规校验若只数
        # 清洗文本会漏掉这笔预算 → ASS 里冒出 13-14 字卡。挂字后把文本刷新为
        # chars 拼接,合并预算与 rs_verify 从此同口径(显示字形,含标点)。
        kar_attached = _apply_karaoke_chars(events, wl)
    # 字级时间是否可信(决定后处理档位):估算/降级 → 旧可读性后处理 + 留痕
    char_known = not bool(wl.get("charTimingEstimated")) and not bool(wl.get("degraded"))
    post = _postprocess_events(events, max_chars, known_time=char_known)
    # 约束校验必须在**可读性调整之后**做,否则报的是已经不存在的问题
    final_cards = _to_cards(events)
    violations = segmentation.check_constraints(
        final_cards, max_chars, cps_max or segmentation.cps_max_for(max_chars))
    violations += _unsatisfied_to_violations(post["unsatisfied"])
    reasons = list(wl.get("degradeReasons", [])) + seg_degrade + list(degrade_notes)
    if post["ghostDropped"]:
        reasons.append(f"P28-2 幽灵卡保险:丢弃 {len(post['ghostDropped'])} 张 "
                       f"<{GHOST_MIN_MS}ms 卡(并卡 {post['ghostMerged']} 张):"
                       f"{'、'.join(post['ghostDropped'][:3])}")
    if not char_known:
        # T4.1c 留痕:字级信息缺失/降级,DP 约束缺席,旧后处理在场
        reasons.append("字级信息缺失/降级:DP 时长/幽灵卡约束缺席,保留旧可读性后处理"
                       "(必并/幽灵卡保险);补齐字级时间后自动切换 time-only")
    estimated = bool(wl.get("charTimingEstimated"))
    if estimated and not any("估算" in r for r in reasons):
        reasons.append("卡内位置为估算(无字级时间戳),建议 rs_dub align 补字级")
    if word_fb_count:
        reasons.append(f"{word_fb_count} 句词内全禁无可行解,按词内强惩罚切分(ADR-0020 留痕)")
    dlg = sum(1 for e in events if e.get("voice") == VOICE_DIALOGUE)
    dual_meta = None
    if dual_style:
        dual_meta = {"requested": True, "applied": dlg > 0, "dialogueCards": dlg,
                     "reason": "" if dlg else
                     "wordline 无 voice=dialogue 句(降级:单 Style + 引号标注,无对白可标)"}
        if dlg:
            reasons.append(f"双 Style:解说 {len(events) - dlg} 卡 / 原声对白 {dlg} 卡(引号区分)")
        else:
            reasons.append("双 Style 降级:wordline 无原声对白标记,按单 Style 出卡"
                           "(sub.pair 降级档:单 Style + 引号标注)")
    # T4.7 断句质量分(机械可算、同输入同分)
    score = segmentation.segscore(
        seg_infos,
        [max(0.0, e["end"] - e["start"]) for e in events],
        len(violations))
    meta = {"degraded": bool(wl.get("degraded")) or any(e.get("degraded") for e in events),
            "degradeReasons": reasons,
            "charTimingEstimated": estimated,
            "violations": violations, "candidates": candidates,
            "mergedShort": post["mergedShort"], "extendedShort": post["extendedShort"],
            "ghostCards": {"merged": post["ghostMerged"], "dropped": post["ghostDropped"]},
            "unsatisfied": post["unsatisfied"], "postProcess": post["mode"],
            "segscore": score,
            "karaokeAttached": kar_attached,
            "wordFallbackSentences": word_fb_count,
            "ambiguous": sum(1 for c in candidates if c["ambiguous"]),
            "reviewQueue": _build_review_queue(candidates=candidates,
                                               unsatisfied=post["unsatisfied"],
                                               violations=violations,
                                               seg_infos=seg_infos)}
    if dual_meta:
        meta["dualStyle"] = dual_meta
    return events, meta


def _content_index(chars: list[dict]) -> tuple[str, list[int]]:
    """内容字串 S + 每个内容字位置 → raw chars 下标(B7 文本锚定用)。

    v0.12 起委托 rs_common.content_index(与 rs_cut --from-text / rs_ir --from-cards
    同一实现,消三处重复);口径仍是标点(PUNCT_WS)+ 空白剔除。
    """
    return rs_common.content_index(chars)


def _normalize_card_text(t: str) -> str:
    """卡文本去标点/空白 → 内容字(B7:按内容定位,绝不做算术偏移)。"""
    return rs_common.content_text(t)


def _resolve_override_requests(ov: dict, chars: list[dict], s: str, idx: list[int]
                               ) -> list[dict]:
    """override 卡 → 内容字区间请求(递增、不重叠)。

    三种定位(B7,BUGREPORT-20260913):
      {"span": [a, b]}                旧契约:raw chars 内容字全局索引;
      {"text": "……"}                  新:去标点后在 S 上**顺序锚定**;
      {"textPrefix": "…", "textSuffix": "…"}  新:前缀定位起点、后缀定位终点(中间吞并)。
    按"内容字数"做算术偏移必然切错位(raw 索引含标点/空格条目),一律走锚定。
    """
    import bisect
    requests: list[dict] = []
    cursor = 0
    for it in (ov.get("cards") or []):
        note = it.get("note") if isinstance(it, dict) else None
        if not isinstance(it, dict):
            raise ValueError(f"override cards 元素应为对象:{it!r}")
        if "span" in it:
            try:
                a, b = int(it["span"][0]), int(it["span"][1])
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ValueError(f"override span 非法:{it!r}") from exc
            if not (0 <= a < b <= len(chars)):
                raise ValueError(f"override span 越界:[{a},{b}),chars 总数 {len(chars)}")
            ca, cb = bisect.bisect_left(idx, a), bisect.bisect_left(idx, b)
        elif "text" in it:
            t = _normalize_card_text(str(it["text"]))
            if not t:
                raise ValueError(f"override text 归一化后为空:{it!r}")
            pos = s.find(t, cursor)
            if pos < 0:
                raise ValueError(f"override text 无法在 wordline 内容串中锚定(必须按原文顺序):"
                                 f"「{t[:14]}…」(游标 {cursor}/{len(s)})")
            ca, cb = pos, pos + len(t)
            cursor = cb
        elif "textPrefix" in it:
            pre = _normalize_card_text(str(it.get("textPrefix") or ""))
            suf = _normalize_card_text(str(it.get("textSuffix") or ""))
            if not pre or not suf:
                raise ValueError(f"override textPrefix/textSuffix 均不能为空:{it!r}")
            pos = s.find(pre, cursor)
            if pos < 0:
                raise ValueError(f"override textPrefix 无法锚定:「{pre[:14]}…」(游标 {cursor})")
            end = s.find(suf, pos + len(pre))
            if end < 0:
                raise ValueError(f"override textSuffix 无法在前缀之后锚定:「{suf[:14]}…」")
            ca, cb = pos, end + len(suf)
            cursor = cb
        else:
            raise ValueError(f"override 卡缺定位字段(需要 span / text / textPrefix+textSuffix):{it!r}")
        requests.append({"content": [ca, cb], "note": note})
    for r1, r2 in zip(requests, requests[1:]):
        if r2["content"][0] < r1["content"][1]:
            raise ValueError(f"override 区间重叠/乱序:{r1['content']} 与 {r2['content']}"
                             f"(契约见 rules/subtitles.md §10.3,不做静默重排)")
    return requests


# 自然停顿字符(P30-3 拆分建议优先级:空格/顿号/逗号/分号 = 用户文案本就有的停顿)
_PRECHECK_PAUSE = " \u3000,，、;；:："
PRECHECK_GAP_MS = 200        # T4.13:字级停顿(gap ≥200ms)是比标点更可靠的切分点


def _precheck_split_requests(requests: list[dict], chars: list[dict], idx: list[int],
                             max_chars: int, *, allow_gap: bool = True,
                             review: list[dict] | None = None
                             ) -> tuple[list[dict], list[dict]]:
    """P30-3/T4.13 用户断句方案预检:超长 request 按「字级停顿 → 自然停顿」拆分并留痕。

    拆分点优先级(T4.13):① **字级停顿**(相邻字 gap ≥200ms——字级时间总是存在,
    比标点更可靠);② 文案本就有的停顿符(空格/顿号/逗号等,旧 P30-3 行为);
    ③ 拆不动 → **不再等 rs_verify 才红**:就地记 review 队列(带最长可容字卡数)
    并把违规写进预检 notes,由调用方进 violations / review_queue。
    返回 (新 requests, notes)。
    """
    notes: list[dict] = []
    out: list[dict] = []
    for r in requests:
        ca, cb = r["content"]
        n = cb - ca
        if n <= max_chars:
            out.append(r)
            continue
        raw_first, raw_last = idx[ca], idx[cb - 1]
        # 拆分点集合:① 字级停顿 gap ≥200ms;② 停顿符(内容字本身/相邻 raw 间隙里)
        gaps: set[int] = set()
        pauses: set[int] = set()
        for p in range(ca + 1, cb):
            prev_ch = str(chars[idx[p - 1]]["ch"])
            next_ch = str(chars[idx[p]]["ch"])
            gap_pause = any(str(chars[k]["ch"]) in _PRECHECK_PAUSE
                            for k in range(idx[p - 1] + 1, idx[p]))
            if prev_ch in _PRECHECK_PAUSE or next_ch in _PRECHECK_PAUSE or gap_pause:
                pauses.add(p)
            if allow_gap:
                gap_ms = int(chars[idx[p]]["startMs"]) - int(chars[idx[p - 1]]["endMs"])
                if gap_ms >= PRECHECK_GAP_MS:
                    gaps.add(p)
        text = "".join(chars[idx[p]]["ch"] for p in range(ca, cb))
        pieces: list[tuple[int, int]] = []
        start, strategy = ca, "gap"
        while cb - start > max_chars:
            limit = start + max_chars
            cut = max((p for p in gaps if start + segmentation.MIN_CHARS <= p <= limit),
                      default=None)
            if cut is None:
                strategy = "pause"
                cut = max((p for p in pauses if start + segmentation.MIN_CHARS <= p <= limit),
                          default=None)
            if cut is None:
                strategy = "review"
                break
            pieces.append((start, cut))
            start = cut
        if strategy == "review":
            # T4.13:拆不动 → 进复核队列 + 给出最长可容字卡数,编译期就红,
            # 不再「留痕后等 rs_verify 硬失败」(R3 的打回式收尾)。
            out.append(r)
            note = {"text": text, "chars": n, "maxChars": max_chars,
                    "splitInto": [], "strategy": "review",
                    "maxFeasibleChars": max_chars,
                    "note": "T4.13 预检:超长卡在字级停顿(≥200ms)与自然停顿符处均无刀点,"
                            "不强行拆分(强拆破坏词边界);已进复核队列——请人工在语义边界"
                            f"手动拆分或放宽平台预设(最长可容 {max_chars} 字/卡)"}
            notes.append(note)
            if review is not None:
                review.append({"type": "overrideUnsplittable", "text": text,
                               "chars": n, "maxChars": max_chars,
                               "maxFeasibleChars": max_chars,
                               "action": note["note"]})
            continue
        pieces.append((start, cb))
        for k, (a, b) in enumerate(pieces):
            piece = {"content": [a, b]}
            if k == 0 and r.get("note"):
                piece["note"] = r["note"]
            out.append(piece)
        notes.append({"text": text, "chars": n, "maxChars": max_chars,
                      "splitInto": [b - a for a, b in pieces],
                      "strategy": strategy,
                      "note": ("P30-3/T4.13 编译期预检:超长卡已在字级停顿处拆分(gap ≥200ms,"
                               "比标点更可靠)" if strategy == "gap" else
                               "P30-3 编译期预检:超长卡已在自然停顿处拆分"
                               "(与用户原案不完全一致,留痕待复核)")})
    return out, notes


def _event_from_content_range(ca: int, cb: int, chars: list[dict], idx: list[int]) -> dict | None:
    """内容字区间 [ca, cb) → 字幕事件(时间唯一真相源 = wordline 字级锚)。

    卡尾标点至多带一个(防「。，」连挂);纯标点/空 span 返回 None。
    """
    if cb <= ca:
        return None
    first = idx[ca]
    last = idx[cb - 1]
    content_last = last   # 终点锚 = 最后一个内容字(卡尾可带标点显示,但标点在重映射后
    if last + 1 < len(chars):   # 可能吸收停顿宽度,不能当终点锚;否则 rs_sync 的
        nxt = chars[last + 1]["ch"]   # 「滞留过久」闸必炸 — 2026-09-15 NCLM1605 实测)
        if not nxt.strip() or nxt in segmentation.PUNCT_WS:
            last += 1          # 卡尾标点至多带一个
    cleaned = textopt._clean_card("".join(c["ch"] for c in chars[first:last + 1]))
    if not cleaned:
        return None
    return {"start": max(0.0, (chars[first]["startMs"] - RELEASE_MS) / 1000.0),
            "end": (chars[content_last]["endMs"] + RELEASE_MS) / 1000.0,
            "text": cleaned,
            "anchorStart": chars[first]["startMs"] / 1000.0,
            "anchorEnd": chars[content_last]["endMs"] / 1000.0,
            "charSpan": [first, last + 1]}


def events_from_override(wl: dict, override: dict, max_chars: int, *,
                         karaoke: bool = False,
                         cps_max: float | None = None) -> tuple[list[dict], dict]:
    """Agent 复核修正(ADR-0020 / B7 扩展):按 override 从 wordline 重建卡片。

    时间唯一真相源仍是 wordline:起点 = 首字 startMs − 20ms、终点 = 末字 endMs + 20ms,
    重建后照常走必并/延长/间距/帧对齐与硬约束校验。

    覆盖模式:
      **full** —— override 区间覆盖全部内容字(旧行为):整表重建,不用 DP;
      **partial** —— 只覆盖一部分:以 DP 分组为基底,被 override 区间压住的 DP 卡
      被替换,其余沿用 DP 结果。微调一张卡不再需要重给全部 span(B7)。

    副文档 07 三道配套 + 第四册收口:
      P30-3 预检 —— 超长 request 在字级停顿/自然停顿处拆分并留痕(T4.13:gap 优先,
              拆不动进复核队列 + 最长可容字卡数,不再等 rs_verify 才红);
      P30-2 余字 —— 被压住的 DP 卡中未被覆盖的余字**自动生成重组 request**,
              不再要求 Agent 手动补(治「挪走科目、剩下按净额填列只有直接消失」式丢字);
      P28-2/T4.2 —— 已知字级时间时后处理只调时间;幽灵卡不再并/丢,改报 unsatisfied。
    """
    import bisect
    chars = wl.get("chars") or []
    if not chars:
        raise ValueError("wordline 没有 chars,无法按 override 重建")
    s, idx = _content_index(chars)
    requests = _resolve_override_requests(override, chars, s, idx)
    if not requests:
        raise ValueError("override 没有有效卡片")
    review_extra: list[dict] = []
    allow_gap = not bool(wl.get("charTimingEstimated"))
    requests, precheck_notes = _precheck_split_requests(
        requests, chars, idx, max_chars, allow_gap=allow_gap, review=review_extra)

    covered = 0
    for ca, cb in (r["content"] for r in requests):
        covered += cb - ca
    full_mode = covered >= len(s)

    residual_events: list[tuple[int, dict]] = []
    if not full_mode:
        base, _cand, _deg, _wfb, _si = _dp_events(wl, max_chars)
        if any("charSpan" not in e for e in base):
            raise ValueError("partial override 需要 DP 事件携带 charSpan(降级 wordline 不支持,"
                             "请改用全量 span 覆盖)")
        kept: list[tuple[int, dict]] = []      # (content 起点, 事件)
        for e in base:
            ra, rb = e["charSpan"]
            j0, j1 = bisect.bisect_left(idx, ra), bisect.bisect_left(idx, rb)
            if any(not (j1 <= ca or cb <= j0) for ca, cb in
                   (r["content"] for r in requests)):
                # P30-2 余字重组:被压住的 DP 卡里,未被任何 request 覆盖的内容字
                # 区间(= 卡区间减去与之相交的 request 区间的差集)自动成卡
                # (替代 Agent 手动补 request;不补即丢字)
                cur = j0
                segs: list[tuple[int, int]] = []
                for ca, cb in sorted(r["content"] for r in requests):
                    a, b = max(j0, ca), min(j1, cb)
                    if b <= a:
                        continue
                    if a > cur:
                        segs.append((cur, a))
                    cur = max(cur, b)
                if cur < j1:
                    segs.append((cur, j1))
                for a, b in segs:
                    ev = _event_from_content_range(a, b, chars, idx)
                    if ev:
                        residual_events.append((a, ev))
                continue                        # 被 override 压住的 DP 卡 → 替换
            kept.append((j0, e))
        replaced: list[tuple[int, dict]] = []
        for r in requests:
            ev = _event_from_content_range(r["content"][0], r["content"][1], chars, idx)
            if ev:
                replaced.append((r["content"][0], ev))
        events = [e for _, e in sorted(kept + replaced + residual_events,
                                       key=lambda t: t[0])]
        override_mode = "partial"
    else:
        events = []
        for r in requests:
            ev = _event_from_content_range(r["content"][0], r["content"][1], chars, idx)
            if ev:
                events.append(ev)
        override_mode = "full"

    events.sort(key=lambda e: e["start"])
    kar_attached = _apply_karaoke_chars(events, wl) if karaoke else 0
    char_known = not bool(wl.get("charTimingEstimated")) and not bool(wl.get("degraded"))
    post = _postprocess_events(events, max_chars, known_time=char_known)
    final_cards = _to_cards(events)
    violations = segmentation.check_constraints(
        final_cards, max_chars, cps_max or segmentation.cps_max_for(max_chars))
    violations += _unsatisfied_to_violations(post["unsatisfied"])
    audit = [{"span": list(e["charSpan"]), "text": e["text"],
              "startMs": int(round(e["start"] * 1000)), "endMs": int(round(e["end"] * 1000)),
              "chars": len(e["text"].replace(" ", ""))}
             for e in events if "charSpan" in e]
    reasons = list(wl.get("degradeReasons") or [])
    if residual_events:
        reasons.append(f"P30-2 余字重组:{len(residual_events)} 段被压住 DP 卡的余字已自动成卡")
    if post["ghostDropped"]:
        reasons.append(f"P28-2 幽灵卡保险:丢弃 {len(post['ghostDropped'])} 张 "
                       f"<{GHOST_MIN_MS}ms 卡(并卡 {post['ghostMerged']} 张):"
                       f"{'、'.join(post['ghostDropped'][:3])}")
    if not char_known:
        reasons.append("字级信息缺失/降级:DP 时长/幽灵卡约束缺席,保留旧可读性后处理"
                       "(必并/幽灵卡保险);补齐字级时间后自动切换 time-only")
    meta = {"degraded": bool(wl.get("degraded")),
            "degradeReasons": reasons,
            "charTimingEstimated": bool(wl.get("charTimingEstimated")),
            "violations": violations, "candidates": [],
            "mergedShort": post["mergedShort"], "extendedShort": post["extendedShort"],
            "unsatisfied": post["unsatisfied"], "postProcess": post["mode"],
            "karaokeAttached": kar_attached, "wordFallbackSentences": 0,
            "ambiguous": 0,
            "overrideApplied": True, "overrideCards": len(requests),
            "overrideMode": override_mode, "audit": audit,
            "overridePrecheck": precheck_notes,
            "residualRegrouped": len(residual_events),
            "ghostCards": {"merged": post["ghostMerged"], "dropped": post["ghostDropped"]},
            "reviewQueue": _build_review_queue(candidates=[], unsatisfied=post["unsatisfied"],
                                               violations=violations, seg_infos=[],
                                               precheck_notes=precheck_notes + review_extra)}
    return events, meta


MIN_DUR_S = 0.83
GHOST_MIN_MS = segmentation.GHOST_MIN_MS   # P28-2 幽灵卡线(T4.4 策略表共读)


def _ghost_span_ms(e: dict) -> float:
    """卡内**内容字**的有效时长 ms(锚点口径;无锚点退回卡时长)。"""
    if "anchorStart" in e and "anchorEnd" in e:
        return max(0.0, (e["anchorEnd"] - e["anchorStart"]) * 1000.0)
    return max(0.0, (e["end"] - e["start"]) * 1000.0)


def _postprocess_events(events: list[dict], max_chars: int, *, known_time: bool,
                        ) -> dict:
    """T4.2/T4.3 事件层后处理收口——「DP 是唯一的断句决策者」在代码里的落点。

    known_time=True(有真实字级时间):只许在**释放余量内调时间**
    (_extend_short 延后沿 / _enforce_gaps 收间距),严禁合并、吞并、丢弃文本;
    短卡/幽灵卡 → 标记 unsatisfied + 可执行替代方案,全部进 violations。
    known_time=False(字级信息缺失/降级):保留旧可读性后处理(必并/幽灵卡保险),
    调用方**必须留痕**(T4.1c)。
    """
    out: dict = {"mode": "time-only" if known_time else "legacy-readability",
                 "mergedShort": 0, "extendedShort": 0,
                 "ghostMerged": 0, "ghostDropped": [], "unsatisfied": []}
    if known_time:
        out["extendedShort"] = _extend_short(events)
        _enforce_gaps(events)
        gmin = segmentation.ghost_min_ms()
        for i, e in enumerate(events):
            span_ms = _ghost_span_ms(e)
            dur_ms = (e["end"] - e["start"]) * 1000.0
            if span_ms < gmin:
                out["unsatisfied"].append({
                    "card": i, "text": e.get("text") or "", "issue": "ghostCard",
                    "spanMs": int(span_ms), "ghostMinMs": gmin,
                    "suggestion": "字级时间已坍缩(<100ms):先修 wordline"
                                  "(rs_align remap/prune-ghost),或 rs_subtitle --override "
                                  "把该卡与相邻卡合并"})
            elif dur_ms < MIN_DUR_S * 1000 - 1e-6:
                out["unsatisfied"].append({
                    "card": i, "text": e.get("text") or "", "issue": "shortCard",
                    "durMs": int(dur_ms),
                    "suggestion": "过短卡(延长余量耗尽):调整平台预设(放宽最短时长),"
                                  "或 rs_subtitle --override 重新分组(回灌即重跑 DP 预检)"})
        return out
    # 降级路径:旧可读性后处理(必并 → 延长 → 间距 → 幽灵卡保险),留痕由调用方写
    events, merged = _merge_short(events, max_chars)
    out["mergedShort"] = merged
    out["extendedShort"] = _extend_short(events)
    _enforce_gaps(events)
    events, gm, gd = _drop_ghost_cards(events, max_chars)
    out["ghostMerged"], out["ghostDropped"] = gm, gd
    if gm:
        _enforce_gaps(events)
    return out


def _unsatisfied_to_violations(unsatisfied: list[dict]) -> list[str]:
    """unsatisfied → violations 文本(T4.3:不合规 100% 进 violations 且带替代方案)。"""
    out = []
    for u in unsatisfied:
        if u["issue"] == "ghostCard":
            out.append(f"卡{u['card']} 幽灵卡(内容字有效时长 {u['spanMs']}ms < "
                       f"{u['ghostMinMs']}ms,未自动并/丢):{u['suggestion']}")
        else:
            out.append(f"卡{u['card']} 时长 {u['durMs'] / 1000.0:.2f}s < {MIN_DUR_S}s"
                       f"(未自动合并):{u['suggestion']}")
    return out


def _build_review_queue(*, candidates: list[dict], unsatisfied: list[dict],
                        violations: list[str], seg_infos: list[dict],
                        precheck_notes: list[dict] | None = None) -> list[dict]:
    """T4.12:Agent 复核队列——DP ambiguous / violations 非空 / I1 语义命中 /
    override 预检未拆动,任何一项命中都进 review_queue.json,走 override 回灌通道
    而不是让后处理硬扛。每条带 type / 建议动作。"""
    queue: list[dict] = []
    for c in candidates or []:
        if c.get("ambiguous"):
            queue.append({"type": "ambiguous", "sentence": c.get("sentence"),
                          "plans": c.get("plans"),
                          "action": "从候选中选定,或写 subtitles_override.json 复核"})
    for si in seg_infos or []:
        hits = si.get("semanticHits") or []
        if hits:
            queue.append({"type": "semantic(I1)", "sentence": si.get("text"),
                          "hits": hits,
                          "action": "人工确认语义单元是否被拆;必要时 override 重新分组"})
    for u in unsatisfied or []:
        queue.append({"type": u["issue"], "card": u.get("card"), "text": u.get("text"),
                      "action": u.get("suggestion")})
    for v in violations or []:
        if v.startswith("卡") and "未自动" in v:
            continue           # unsatisfied 已入队,不重复
        queue.append({"type": "violation", "message": v,
                      "action": "按违规类型处理(字数/重叠=改 override 分组;"
                                "CPS/时长=平台预设放宽或 override)"})
    for n in precheck_notes or []:
        if n.get("strategy") == "review":
            queue.append({"type": "overrideUnsplittable", "text": n.get("text"),
                          "chars": n.get("chars"), "maxChars": n.get("maxChars"),
                          "action": n.get("note")})
    return queue


def _drop_ghost_cards(events: list[dict], max_chars: int,
                      min_ms: float = GHOST_MIN_MS) -> tuple[list[dict], int, list[str]]:
    """P28-2 幽灵卡保险(副文档 07):内容字有效时长 <100ms 的卡必须并卡或丢弃。

    remap 后个别字符坍缩到删除边界上会生成 0.06s 级碎卡——绝不静默出卡:
    ① 先试**并入上一卡**(不超字数);② 放不下则**并入下一卡**(幽灵文本作前缀);
    ③ 仍放不下 → 丢弃并留痕。根治靠 P28-1 的 remap 本体丢弃 + prune-ghost,
    这里兜旧工程/漏网路径。返回 (事件表, 并卡数, 丢弃文本列表)。
    """
    out: list[dict] = []
    merged, dropped = 0, []
    pending: dict | None = None              # 待并入下一卡的幽灵卡(向后吞)
    # 护栏:若**全部**卡都是幽灵时长(降级/合成时间轴的 pathological 形态),
    # 一张不丢——保险针对的是"正常卡旁边的零星碎卡",不是清空整句内容
    if events and all(_ghost_span_ms(e) < min_ms for e in events):
        return events, 0, []
    for e in events:
        if _ghost_span_ms(e) >= min_ms:
            if pending is not None and _same_voice(pending, e):
                # 幽灵卡作前缀并入下一张正常卡(跨声轨不并:对白/解说分界保持)
                joined = _join(pending.get("text", ""), e["text"])
                if len(joined.replace(" ", "")) <= max_chars:
                    e = dict(e)
                    e["start"] = min(e["start"], pending["start"])
                    e["text"] = joined
                    if "anchorStart" in pending:
                        e["anchorStart"] = min(e.get("anchorStart", 10 ** 9),
                                               pending["anchorStart"])
                    if pending.get("chars"):
                        e["chars"] = pending["chars"] + (e.get("chars") or [])
                    if "charSpan" in pending:
                        e["charSpan"] = [min(pending["charSpan"][0], e["charSpan"][0]),
                                         max(pending["charSpan"][1], e["charSpan"][1])] \
                            if "charSpan" in e else list(pending["charSpan"])
                    merged += 1
                    pending = None
                else:
                    dropped.append(pending.get("text") or "(空)")
                    pending = None
            out.append(e)
            continue
        # e 是幽灵卡:先试向前吞(并入上一张已落卡;跨声轨不并)
        text = e.get("text") or ""
        if out and _same_voice(out[-1], e):
            prev = out[-1]
            joined = _join(prev.get("text", ""), text)
            if len(joined.replace(" ", "")) <= max_chars:
                prev["end"] = max(prev["end"], e["end"])
                prev["text"] = joined
                if "anchorEnd" in e:
                    prev["anchorEnd"] = max(prev.get("anchorEnd", 0.0), e["anchorEnd"])
                if e.get("chars"):
                    prev["chars"] = (prev.get("chars") or []) + e["chars"]
                if "charSpan" in e:
                    prev["charSpan"] = [min(prev["charSpan"][0], e["charSpan"][0]),
                                        max(prev["charSpan"][1], e["charSpan"][1])] \
                        if "charSpan" in prev else list(e["charSpan"])
                merged += 1
                continue
        if not text:                             # 无文本的退化事件直接丢
            dropped.append("(空)")
            continue
        pending = e                              # 向后吞:等下一张正常卡来了作前缀并入
    if pending is not None:
        dropped.append(pending.get("text") or "(空)")
    return out, merged, dropped


def _join(a: str, b: str) -> str:
    sep = " " if (a and b and a[-1].isascii() and a[-1].isalnum()
                  and b[0].isascii() and b[0].isalnum()) else ""
    return a.rstrip() + sep + b.lstrip()


def _same_voice(a: dict, b: dict) -> bool:
    """两事件是否同声轨(双 Style 并卡护栏:解说卡与原声对白卡不合并)。

    无 voice 标记的事件(override 路径/旧工程)视为同轨,行为与 historic 一致。
    """
    return a.get("voice", "") == b.get("voice", "")


def _merge_short(events: list[dict], max_chars: int,
                 min_dur: float = MIN_DUR_S) -> tuple[list[dict], int]:
    """<0.83s 必并(rules/subtitles.md §4.4)——**仅降级路径**(T4.1c)。

    第四册 T4.2/T4.3 契约:有字级时间时 DP 以带权惩罚直接避免短卡,事件层**不再合并**
    (任何文本合并都是反写断句);只有字级信息缺失/降级的 wordline 才保留本后处理,
    且必须留痕(调用方负责写 degradeReasons)。
    本函数只保留「向上一卡并」的第一遍;**第二遍「向下一卡吞并」已删除(T4.3)**——
    吞并会在未被重新求解的情况下改写 DP/用户的文本分组,是「门禁强制断句」的根因 R1。
    预算放不下的过短卡 → 调用方标记 unsatisfied + 给替代方案(见 _unsatisfied_entries)。
    """
    out: list[dict] = []
    merged = 0
    for e in events:
        if out:
            prev = out[-1]
            text = _join(prev["text"], e["text"])
            short = (prev["end"] - prev["start"] < min_dur) or (e["end"] - e["start"] < min_dur)
            if short and _same_voice(prev, e) and len(text.replace(" ", "")) <= max_chars:
                prev["end"] = e["end"]
                prev["text"] = text
                if "anchorEnd" in e:
                    prev["anchorEnd"] = e["anchorEnd"]      # 合并后占的是后一卡的时间
                if "charSpan" in e:
                    prev["charSpan"] = [min(prev["charSpan"][0], e["charSpan"][0]),
                                        max(prev["charSpan"][1], e["charSpan"][1])] \
                        if "charSpan" in prev else list(e["charSpan"])
                if e.get("chars"):
                    # 卡拉OK:合并文本必须同步合并逐字时间,否则 _kar_text 丢字
                    prev["chars"] = (prev.get("chars") or []) + e["chars"]
                merged += 1
                continue
        out.append(dict(e))
    return out, merged


def _extend_short(events: list[dict], min_dur: float = MIN_DUR_S) -> int:
    """仍有余量时,把过短卡的后沿延到最短时长——**绝不超过锚点上限**。

    上限 = min(末字 endMs + EXTEND_MAX_S, 下一卡起点 − 2 帧)。延长只是让字多在
    屏上停一会(可读性),不能一路延到下一卡导致字幕滞留到停顿里。
    """
    min_gap = MIN_GAP_FRAMES * FRAME_MS / 1000.0
    n = 0
    for i, e in enumerate(events):
        if e["end"] - e["start"] >= min_dur:
            continue
        want = e["start"] + min_dur
        if "anchorEnd" in e:
            want = min(want, e["anchorEnd"] + EXTEND_MAX_S)
        if i + 1 < len(events):
            want = min(want, events[i + 1]["start"] - min_gap)
        if want > e["end"]:
            e["end"] = want
            n += 1
    return n


def _enforce_gaps(events: list[dict], fps: float = 30.0,
                  min_gap_frames: int = MIN_GAP_FRAMES) -> int:
    """卡间距 ≥2 帧;**但对齐精度优先**——只在释放余量内调整,绝不动字级锚点。

    释放余量 = 卡起点与其首字 startMs 之差、卡终点与其末字 endMs 之差(各 ≤RELEASE_MS)。
    调整顺序:① 推迟后卡起点(最多到其首字锚点);② 收早前卡终点(最多到其末字锚点)。
    余量耗尽仍不足 2 帧 → 保持字级精确时间(宁可间距紧,不可音画错位)。返回仍不足项数。
    """
    min_gap = min_gap_frames * (1000.0 / fps) / 1000.0
    tight = 0
    for a, b in zip(events, events[1:]):
        need = min_gap - (b["start"] - a["end"])
        if need <= 1e-9:
            continue
        if "anchorStart" in b:
            b["start"] += min(need, max(0.0, b["anchorStart"] - b["start"]))
            need = min_gap - (b["start"] - a["end"])
        if need > 1e-9 and "anchorEnd" in a:
            a["end"] -= min(need, max(0.0, a["end"] - a["anchorEnd"]))
            need = min_gap - (b["start"] - a["end"])
        if need > 1e-9:
            tight += 1
    # 防御:任何情况下不得重叠(锚点保证 ≥0;无锚点事件走这里兜底)
    for a, b in zip(events, events[1:]):
        if b["start"] < a["end"]:
            b["start"] = a["end"]
    return tight


def _finalize_events(events: list[dict], fps: float = 30.0) -> None:
    """B5 落盘兜底:正时长、单调、无重叠(就在释放余量/防御语义内,绝不动锚点)。"""
    events.sort(key=lambda e: e["start"])
    frame = 1.0 / fps if fps and fps > 0 else 1 / 30.0
    for e in events:
        if e["end"] <= e["start"]:
            e["end"] = e["start"] + frame
    _enforce_gaps(events, fps)


# ---------------------------------------------------------------- 帧网格对齐(T2.6/H6)

def frame_grid_deltas(clips: list[dict], fps: float) -> list[tuple[int, int, int]]:
    """「名义段起点 → 帧取整段起点」分段 delta 表(H6 单一真相源口径)。

    视频 = Σ 帧取整段长;名义 = Σ durationMs;每段 delta_k = 实际起点 − 名义起点。
    返回 [(nom_start_ms, nom_end_ms, delta_ms)];与旧 rs_render 烧录期平移同一
    映射,只是前移到了 S7 生成期(生成即对齐,盘面 ASS 不再有第二套时间)。
    """
    segs: list[tuple[int, int, int]] = []
    acc_nom, acc_act = 0.0, 0.0
    for c in clips:
        dur = int(c.get("durationMs") or 0)
        if dur <= 0:
            continue
        qf = max(1, round(dur / 1000.0 * fps))
        nom_start = int(acc_nom)
        segs.append((nom_start, nom_start + dur, int(round(acc_act * 1000)) - nom_start))
        acc_nom += dur
        acc_act += qf / fps
    return segs


def _delta_at(segs: list[tuple[int, int, int]], ms: int) -> int:
    for a, b, d in segs:
        if a <= ms < b:
            return d
    return segs[-1][2] if segs else 0


def align_events_to_frame_grid(events: list[dict], clips: list[dict],
                               fps: float) -> int:
    """S7 生成期把事件时间对齐到**拼接帧网格**(T2.6/H6:单一真相源)。

    每事件按其起点/终点所在段的 delta 平移;返回被平移的事件数(留痕)。
    段长恰为帧整数倍(delta 全 0)时是恒等操作。旧口径(烧录期平移
    `_build/subtitled_aligned.ass`,盘面 ASS 不动)已废。
    """
    if not clips or not fps or fps <= 0:
        return 0
    segs = frame_grid_deltas(clips, fps)
    if not segs or all(d == 0 for _, _, d in segs):
        return 0
    frame = 1.0 / fps
    moved = 0
    for e in events:
        s_ms = int(round(e["start"] * 1000))
        e_ms = int(round(e["end"] * 1000))
        d_s, d_e = _delta_at(segs, s_ms), _delta_at(segs, e_ms)
        if d_s == 0 and d_e == 0:
            continue
        e["start"] = max(0.0, (s_ms + d_s) / 1000.0)
        e["end"] = max(e["start"] + frame, (e_ms + d_e) / 1000.0)
        moved += 1
    events.sort(key=lambda x: x["start"])
    return moved


def _find_project_ir(out: Path) -> dict | None:
    """从输出落点向上找工程根的 project.json(H6:S7 对齐需要主轨段长)。

    找不到 → None(无工程上下文,名义时间即真相);存在但不合法 → WARN + None
    (不阻塞字幕生成,如实留痕)。目录定位一律走 rs_paths(禁字面量)。"""
    import rs_paths  # noqa: PLC0415 — 阶段路径唯一真相源
    for base in (out, *out.parents):
        pj = rs_paths.project_json(base)
        if pj.is_file():
            try:
                return rs_common.load_ir_path(pj)
            except rs_common.IrError as exc:
                print(f"[rs_subtitle] WARN frameGridAlign-skip project.json 不合法:"
                      f"{exc.message}", file=sys.stderr)
                return None
    return None


def _project_video_clips(ir: dict) -> list[dict]:
    """IR 主视频轨 clips(与 rs_render._main_video_clips 同口径;不跨层 import)。"""
    tracks = [t for t in ir.get("tracks", []) if t.get("kind") == "video"]
    if not tracks or not (tracks[0].get("clips") or []):
        return []
    return tracks[0]["clips"]


def snap_events_to_frames(events: list[dict], fps: float = 30.0) -> int:
    """把字幕时间量化到帧:起点向下取整、终点向上取整(渲染只认帧)。

    v0.8.1:snap 之后追加**碰撞消解**——起点 floor / 终点 ceil 会让相邻卡产生
    ≤1 帧的伪重叠(用户实测 3ms 级)。锚点有余量时消解之;余量耗尽则保留伪重叠
    (切割点吸附伪影,不可见),由 rs_sync 的 1 帧容差放行。

    返回被调整的事件数。fps ≤ 0 时不处理。
    """
    if not fps or fps <= 0:
        return 0
    frame = 1.0 / fps
    n = 0
    for e in events:
        s = math.floor(round(e["start"] / frame, 6)) * frame
        t = math.ceil(round(e["end"] / frame, 6)) * frame
        if s < 0:
            s = 0.0
        if t <= s:
            t = s + frame
        if abs(s - e["start"]) > 1e-9 or abs(t - e["end"]) > 1e-9:
            e["start"], e["end"] = s, t
            n += 1
    # 碰撞消解:① 后卡起点推迟(最多到其 anchorStart);② 前卡终点收早(最多到其 anchorEnd);
    # 两者都会破坏对齐精度时,保持伪重叠——不可见,rs_sync 以 1 帧容差放行。
    for a, b in zip(events, events[1:]):
        if b["start"] >= a["end"] - 1e-9:
            continue
        if "anchorStart" in b and b["anchorStart"] >= a["end"] - 1e-9:
            b["start"] = a["end"]
        elif "anchorEnd" in a and a["anchorEnd"] <= b["start"] + 1e-9:
            a["end"] = b["start"]
    return n


# ---------------------------------------------------------------- 兼容入口

def build_events(entries: list[dict], max_chars: int, per_span_s: float = 4.0,
                 optimize: bool = True, terms=(), top: int = 3) -> list[dict]:
    """entries: [{start_s, end_s, text}] → 事件列表。

    统一经 rs_align.build_wordline 建时间轴再聚合(不另立第二套时间);
    optimize=False 时退回逐句断行 + 句内均分(仅复现旧工程)。
    """
    if not optimize:
        out = []
        for e in entries:
            dur = e["end_s"] - e["start_s"]
            lines = break_line(e["text"].replace("\n", " ").strip(), max_chars)
            per = dur / max(1, len(lines))
            for i, ln in enumerate(lines):
                out.append({"start": e["start_s"] + i * per,
                            "end": e["start_s"] + (i + 1) * per, "text": ln})
        return out
    import rs_align
    wl = rs_align.build_wordline(entries, "inline", degraded="内存构建(无字级时间戳)")
    events, _ = events_from_wordline(wl, max_chars, terms=terms, top=top)
    return events


# ---------------------------------------------------------------- 输出

def write_srt(events: list[dict], path: Path) -> None:
    body = "".join(f"{i}\n{_ts_srt(e['start'])} --> {_ts_srt(e['end'])}\n{e['text']}\n\n"
                   for i, e in enumerate(events, 1))
    path.write_text(body, encoding="utf-8")


def _style_line(name: str, st: dict, ratio: str, secondary: str, italic: int = 0,
                font: str | None = None, size: int | None = None) -> str:
    """一条 ASS Style 行(双 Style 的 Main/Quote 共用同一拼装,保证字段同构)。

    font 缺省走 resolve_font() 查表(分册01 §5);花字模板 Style 可显式传
    font/size 覆写(仍以查表值为字体来源,模板只给字号/粗细等视觉参数)。
    """
    margin_r, margin_l = 60, 60
    fam = font or resolve_font()
    fsize = size if size is not None else st["size"][ratio]
    return (f"Style: {name},{fam},{fsize},{st['primary']},{secondary},"
            f"{st['outline']},{st['back']},{st['bold']},{italic},0,0,100,100,0,0,"
            f"{st.get('border_style', 1)},{st['outline_w']},{st['shadow']},{st['align']},"
            f"{margin_l},{margin_r},{st['margin_v'][ratio]},1")


def quote_text(e: dict) -> str:
    """原声对白卡文本:自动包中文引号(已带前引号不重复包;sub.pair 降级档同款规则)。"""
    t = e.get("text") or ""
    if not t or t.lstrip().startswith(QUOTE_OPEN):
        return t
    return f"{QUOTE_OPEN}{t}{QUOTE_CLOSE}"


def write_ass(events: list[dict], path: Path, style_name: str, ratio: str, canvas: str,
              karaoke: bool = False, dual_style: bool = False,
              huazi_tpl: dict | None = None) -> bool:
    """karaoke=True 时生成逐字卡拉OK:Primary=已唱色(黄),Secondary=未唱色(白),
    每字一个 \\kf 标签(时长=厘秒,取自字级时间戳;字间停顿计入前字)。

    dual_style=True(M8 短剧/影视解说):生成两个 Style 段 —— 解说体 Main(常规样式)
    + 原声对白体 Quote(暖黄斜体,视觉可区分);voice=dialogue 的事件走 Quote 并自动
    包中文引号。卡拉OK 路径只换 Style 不加引号字形(逐字时间不含引号,包引号会
    破坏「显示字形=预算」口径)。返回是否真的出了双 Style(供 CLI 留痕)。
    """
    st = STYLES.get(style_name) or STYLES["subtitle-white"]
    w, h = canvas.split("x")
    play_res = f"PlayResX: {w}\nPlayResY: {h}"
    if karaoke:
        st = dict(st)
        st["primary"] = "&H0000E5FF"          # 已唱:暖黄(BGR)
        secondary = "&H00FFFFFF"              # 未唱:白
    else:
        secondary = "&H000000FF"

    def _is_dlg(e: dict) -> bool:
        return dual_style and e.get("voice") == VOICE_DIALOGUE

    has_dlg = dual_style and any(_is_dlg(e) for e in events)
    header = f"""[Script Info]
Title: CutFlow subtitles
ScriptType: v4.00+
WrapStyle: 0
{play_res}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
"""
    header += _style_line("Main", st, ratio, secondary) + "\n"
    if has_dlg:
        # 原声对白体:同字号/同底部安全区(MarginV 不动),暖黄 + 斜体区分
        st_q = dict(st)
        st_q["primary"] = QUOTE_COLOR
        header += _style_line(QUOTE_STYLE_NAME, st_q, ratio, secondary, italic=1) + "\n"
    has_hz = any(e.get("_huazi") for e in events)
    if has_hz:
        # 花字主样式(ADR-0057 基础档):字号取模板 HuaziMain(缺省 = 基础样式 +14),
        # 字体仍走 resolve_font() 查表;标签动画在 Dialogue body,不改卡文本
        hz_st = dict(st, bold=1, outline_w=int((huazi_tpl or {}).get("style", {}).get("outline_w") or 10))
        hz_size = (huazi_tpl or {}).get("style", {}).get("size") or st["size"][ratio] + 14
        header += _style_line("HuaziMain", hz_st, ratio, secondary,
                              font=resolve_font(), size=hz_size) + "\n"
    header += f"""
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for e in events:
        if e.get("_huazi"):
            # 花字卡(ADR-0057):body 由模板效果生成(标签只进 override,正文原样),
            # 卡文本不变 → 必并/合规校验与剥 {...} 后的文本匹配口径都不受影响
            lines.append(f"Dialogue: 1,{_ts_ass(e['start'])},{_ts_ass(e['end'])},HuaziMain,"
                         f",0,0,0,,{e['_huazi']}\n")
            continue
        style = QUOTE_STYLE_NAME if _is_dlg(e) else "Main"
        if karaoke:
            body = _kar_text(e)
        elif _is_dlg(e):
            body = quote_text(e)              # 对白自动包引号(已是引号开头不重复包)
        else:
            body = e["text"]
        lines.append(f"Dialogue: 0,{_ts_ass(e['start'])},{_ts_ass(e['end'])},{style},"
                     f",0,0,0,,{body}\n")
    path.write_text(header + "".join(lines), encoding="utf-8")
    return has_dlg


def _kar_text(e: dict) -> str:
    """事件 → \\kf 逐字文本。首字从卡头起唱,末字吃到卡尾;字间停顿计入前字时长。"""
    chs = e.get("chars") or []
    if not chs:
        return e["text"]
    ev_start = int(round(e["start"] * 1000))
    ev_end = int(round(e["end"] * 1000))
    parts = []
    for k, c in enumerate(chs):
        cur = c["startMs"] if k > 0 else min(c["startMs"], ev_start)
        if k + 1 < len(chs):
            nxt = chs[k + 1]["startMs"]
        else:
            # 末字吃到末字真实结束时刻(而非卡尾)——卡尾可能因可读性被延长
            nxt = min(ev_end, int(chs[-1].get("endMs", ev_end)))
        cs = max(1, int(round((nxt - cur) / 10.0)))
        parts.append(f"{{\\kf{cs}}}{c['ch']}")
    return "".join(parts)


def attach_karaoke_chars(events: list[dict], wl: dict) -> int:
    """把 wordline 字级时间按「卡起点分界」分配到事件(卡拉OK 用)。

    用起点分界而不是 [start,end] 窗口:卡间距压缩后尾部字符不丢。
    """
    import bisect
    chars = wl.get("chars") or []
    for e in events:
        e["chars"] = []
    if not chars or not events:
        return 0
    starts = [e["start"] for e in events]
    n = 0
    prev_k: int | None = None
    for c in chars:
        is_punct = all(ch in _PUNCT_ONLY for ch in c["ch"] if ch.strip())
        ctr = (c["startMs"] + c["endMs"]) / 2000.0
        if is_punct and prev_k is not None:
            # dev-jj2815 实测:retext 给插入标点分了 gap 中段时间(如 ?[4.41,4.59]),
            # 按时间中心 bisect 会把尾标点划给下一卡 → 出现「?关于…」式领头标点。
            # 标点是上一字的余音,一律跟随前字所在卡。
            k, ok = prev_k, True
        else:
            k = bisect.bisect_right(starts, ctr) - 1
            ok = 0 <= k < len(events) and ctr <= events[k]["end"] + 0.5
        if ok:
            events[k]["chars"].append(c)
            prev_k = k
            n += 1
        else:
            prev_k = None
    return n


def _apply_karaoke_chars(events: list[dict], wl: dict) -> int:
    """挂字 + 用 chars 拼接刷新显示文本(卡拉OK 的预算/校验基准 = 显示字形)。

    dev-jj2815 实测:_clean_card 剥掉的标点会在 \\kf 显示层原样重现,文本若
    停留在清洗态,必并预算与 rs_verify 计数都会比实际显示少 1-2 字形。
    """
    n = attach_karaoke_chars(events, wl)
    if n:
        for e in events:
            if e.get("chars"):
                e["text"] = "".join(c["ch"] for c in e["chars"])
    return n


def _karaoke_ready(wl: dict) -> tuple[bool, str]:
    """卡拉OK 只认真实字级时间戳;降级 wordline 一律拒绝(防静默插值回禁区)。"""
    if wl.get("degraded"):
        return False, "wordline 是降级时间(无字级时间戳)"
    if not (wl.get("chars") or []):
        return False, "wordline 没有 chars"
    return True, ""


# ---------------------------------------------------------------- 花字基础档(ADR-0057,分册01 §4.3)
# ASS 富文本:底衬(\bord + \p1 绘制)+ 逐字/逐词动画(\t)+ 淡入淡出(\fad)。
# 模板存 assets/huazi/ass/(hz-* 元数据注释 + 示例 Dialogue);本侧只做解析与重排。
# 硬规则 18 不变:任何 ASS Dialogue 文本匹配/计数前必须剥 {...} override;
# 花字不改卡文本,因此 CPS ≤9 与每卡 ≤12 字(9:16)红线由既有约束天然继承。
HUAZI_META_RE = re.compile(r"^; hz-([\w-]+): (.*)$", re.M)
OVERRIDE_RE = re.compile(r"\{[^}]*\}")


def strip_override(text: str) -> str:
    """剥 {...} ASS override 标签(硬规则 18 的共享实现)。"""
    return OVERRIDE_RE.sub("", str(text))


def load_huazi_template(huazi_id: str) -> dict:
    r"""花字 id → 模板数据(manifest 检索 → hz-* 元数据 + 示例 Style 参数)。

    返回 {id, label, effect, params, file, usage, style};找不到/坏模板抛
    KeyError/ValueError(CLI 层转 BAD_HUAZI,不静默退回普通字幕)。
    """
    import rs_asset
    hit = rs_asset.find(huazi_id, "huazi")
    src = rs_asset.file_of(hit) if hit is not None else None
    if src is None:
        # 索引缺席时的兜底:按 id 末两段直猜 huazi/ass/<名>.ass(旧工程容错)
        guess = "_".join(huazi_id.split(".")[2:])
        cand = Path(__file__).resolve().parents[1] / "assets" / "huazi" / "ass" / f"{guess}.ass"
        src = cand if cand.is_file() else None
    if src is None or not Path(src).is_file():
        raise KeyError(f"花字模板不存在:{huazi_id}(rs_asset list --kind huazi 可查)")
    text = Path(src).read_text(encoding="utf-8")
    meta = dict(HUAZI_META_RE.findall(text))   # 键 = hz- 前缀后的短名(id/label/…)
    if not meta.get("id"):
        raise ValueError(f"花字模板缺 hz-id 元数据:{src}")
    try:
        params = json.loads(meta.get("params", "{}"))
    except json.JSONDecodeError:
        params = {}
    style: dict = {}
    m = re.search(r"^Style: HuaziMain,[^,]*,([^,]*),", text, re.M)
    if m:
        style["size"] = int(m.group(1))
    m2 = re.search(r"^Style: HuaziBack,(?:[^,]*,){15}(\d+),", text, re.M)
    if m2:
        style["outline_w"] = int(m2.group(1))
    return {"id": meta["id"], "label": meta.get("label", ""),
            "effect": meta.get("effect", ""), "params": params,
            "file": str(src), "usage": (meta.get("usage") or "").split(),
            "style": style}


def huazi_select(events: list[dict], keywords: list[str]) -> set[int]:
    """选卡:keywords 命中(剥 override 后子串)的卡;空表 → 全选。

    文本匹配一律走 strip_override(硬规则 18),杜绝 override 污染匹配。
    """
    if not keywords:
        return set(range(len(events)))
    out = set()
    for i, e in enumerate(events):
        plain = strip_override(e.get("text", ""))
        if any(k and k in plain for k in keywords):
            out.add(i)
    return out


def huazi_body(event: dict, effect: str, params: dict, canvas_w: int = 1080,
               outline_w: int | None = None) -> str:
    r"""单卡文本 → 花字 ASS body(标签只进 override,正文逐字原样)。

    效果与 assets/huazi/ass/ 八套模板一一对应;动画幅度取模板 params
    (overshoot ≤1.1,ADR-0057 进阶档纪律)。M2:box/brush 的内联描边一律取
    模板解析出的 style.outline_w(HuaziBack),模板未给时才用各效果的历史默认。
    """
    text = event.get("text", "")
    step = int(params.get("stepMs", 70))
    bord = int(outline_w or 0)
    if effect == "box":
        color = str(params.get("color", "&H00E5FF00"))
        return f"{{\\bord{bord or 10}\\bordcolor{color}}}{text}"
    if effect == "brush":
        # ASS 侧底衬(厚描边 + 微倾手写感);params.element 由 overlay 路径消费
        color = str(params.get("color", "&H6E44FF"))
        return f"{{\\bord{bord or 16}\\bordcolor{color}\\frz-2}}{text}"
    if effect == "pop":
        over = float(params.get("overshoot", 1.1))
        o100 = int(over * 100)
        parts = []
        for i, ch in enumerate(text):
            t0 = i * step
            t1 = t0 + 120
            t2 = t1 + 80
            parts.append(f"{{\\fscx20\\fscy20\\t({t0},{t1},\\fscx{o100}\\fscy{o100})"
                         f"\\t({t1},{t2},\\fscx100\\fscy100)}}{ch}")
        return "".join(parts)
    if effect == "slide":
        dist = int(params.get("distPx", 80))
        dur = int(params.get("durMs", 260))
        x0 = max(0, canvas_w // 2 - dist // 2)
        x1 = canvas_w // 2
        y = 1500
        return f"{{\\move({x0},{y},{x1},{y},0,{dur})\\fad(180,0)}}{text}"
    if effect == "typewriter":
        parts = []
        for i, ch in enumerate(text):
            t = i * step
            parts.append(f"{{\\alpha&HFF&\\t({t},{t},\\alpha&H00&)}}{ch}")
        return "".join(parts)
    if effect == "count":
        return f"{{\\t(0,90,\\fscx106\\fscy106)\\t(90,180,\\fscx100\\fscy100)}}{text}"
    if effect == "tab":
        skew = int(params.get("skew", 12))
        color = str(params.get("color", "&H00E5FF00"))
        return f"{{\\bord8\\bordcolor{color}\\frz-{skew // 3}}}{text}"
    if effect == "subscribe":
        fad = params.get("fadMs") or [200, 400]
        return f"{{\\fad({int(fad[0])},{int(fad[1])})}}{text}"
    raise ValueError(f"未知花字效果:{effect!r}")


def apply_huazi(events: list[dict], tpl: dict, keywords: list[str]) -> set[int]:
    """就地标记命中卡(事件挂 _huazi body);返回命中下标集(CLI 留痕/测试用)。"""
    idx = huazi_select(events, keywords)
    for i in idx:
        events[i]["_huazi"] = huazi_body(events[i], tpl["effect"], tpl["params"],
                                         outline_w=(tpl.get("style") or {}).get("outline_w"))
    return idx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-wordline")
    ap.add_argument("--from-tts")
    ap.add_argument("--from-transcript")
    ap.add_argument("--style", default=None, help="字幕样式(默认按平台预设,再退回 subtitle-white)")
    ap.add_argument("--ratio", default=None, choices=list(RATIOS), help="画幅比例(默认按平台预设)")
    ap.add_argument("--canvas", default=None, help="如 1080x1920,默认按比例/平台预设推断")
    ap.add_argument("--platform", default=None,
                    help="平台预设:douyin / shipinhao / xiaohongshu / bilibili(见 templates/platforms.json)")
    ap.add_argument("--max-chars", dest="max_chars", type=int, default=None, help="覆盖每卡字数上限")
    ap.add_argument("--out", required=True)
    ap.add_argument("--terms", default="", help="专有名词表(逗号分隔),断句禁切用")
    ap.add_argument("--top", type=int, default=3, help="输出前 N 个切分候选")
    ap.add_argument("--segment", default="dp", choices=["dp", "length"])
    ap.add_argument("--no-optimize", action="store_true", help="关闭轻改写(默认开启)")
    ap.add_argument("--karaoke", action="store_true",
                    help="逐字卡拉OK字幕(\\kf 染色;需 pkg 后端字级时间戳的 wordline)")
    ap.add_argument("--dual-style", dest="dual_style", action="store_true",
                    help="解说/原声对白双 Style(方案 §5.5.3):解说体 Main + 原声对白体 Quote,"
                         "对白自动包中文引号;需 --from-wordline 且句级 voice=dialogue 标记,"
                         "无标记时降级为单 Style(留痕)")
    ap.add_argument("--allow-degraded", dest="allow_degraded", action="store_true",
                    help="卡拉OK 但 wordline 降级时,降级为普通字幕而不是报错")
    ap.add_argument("--huazi", default=None,
                    help="花字基础档(ADR-0057):模板 id(如 huazi.keyword.box,默认关);"
                         "从 assets/huazi/ass/ 读模板,按效果给命中卡加 ASS 富文本动画")
    ap.add_argument("--huazi-keywords", dest="huazi_keywords", default="",
                    help="花字选卡关键词(逗号分隔;缺省=全部卡上花字)")
    ap.add_argument("--override", default=None,
                    help="Agent 复核修正文件(subtitles_override.json):按 span / text / "
                         "textPrefix+textSuffix 从 wordline 重建卡片;支持部分替换(未提及卡沿用 DP),"
                         "仅支持 --from-wordline")
    ap.add_argument("--auto", dest="auto", action="store_true",
                    help="T4.12 自动模式:必定落盘 review_queue.json(有待复核项时非空),"
                         "把 ambiguous/违规/语义命中/预检未拆动交给 Agent 复核→override 通道")
    ap.add_argument("--tokenizer", dest="tokenizer", default=None,
                    help="分词引擎(T4.8):auto/jieba/pkuseg/lac/hanlp/lexicon;"
                         "缺省读 config.json 的 subtitleTokenizer,再缺省 auto;"
                         "引擎缺失自动降级并留痕")
    ap.add_argument("--fps", type=float, default=30.0, help="帧率(字幕时间量化到帧)")
    ap.add_argument("--no-snap", dest="no_snap", action="store_true",
                    help="不做帧对齐,保留亚帧精度")
    a = ap.parse_args()

    # 优先级:显式参数 > 平台预设 > 内置默认(见 templates/platforms.json)
    try:
        preset = resolve_platform(a.platform)
    except KeyError as exc:
        return emit(False, "BAD_PLATFORM", str(exc), exit_code=2)
    ratio = a.ratio or preset.get("ratio") or "9x16"
    if ratio not in RATIOS:
        return emit(False, "BAD_RATIO", f"未知比例 {ratio}(可选 {'/'.join(RATIOS)})", exit_code=2)
    style = a.style or preset.get("style") or "subtitle-white"
    if a.canvas:
        canvas = a.canvas
    elif preset.get("canvas"):
        canvas = f"{preset['canvas'][0]}x{preset['canvas'][1]}"
    else:
        w, h = canvas_for(ratio)
        canvas = f"{w}x{h}"
    terms = tuple(t.strip() for t in a.terms.split(",") if t.strip())
    max_chars = resolve_max_chars(a.max_chars, preset, ratio)
    # T4.8 分词引擎:CLI > config.json subtitleTokenizer > auto(jieba→lexicon)
    engine = a.tokenizer
    if not engine:
        try:
            cfg = rs_common.load_config()
            engine = str((cfg or {}).get("subtitleTokenizer") or "auto")
        except Exception:  # noqa: BLE001 — config 不可读不阻塞出片
            engine = "auto"

    # 双 Style 判定先于读源:voice 标记只来自校对后的 wordline,其他源直接拒绝
    dual_style = bool(a.dual_style)
    if dual_style and not a.from_wordline:
        return emit(False, "DUAL_NEEDS_WORDLINE",
                    "--dual-style 仅支持 --from-wordline(voice 标记来自校对后的句级字段)",
                    exit_code=2)

    if a.from_wordline:
        wl = json.loads(Path(a.from_wordline).read_text(encoding="utf-8"))
    elif a.from_tts:
        man = json.loads(Path(a.from_tts).read_text(encoding="utf-8"))
        entries = [{"start_s": s["start_s"], "end_s": s["end_s"], "text": s["text"],
                    "timestamp": s.get("timestamp") or s.get("chars")}
                   for s in man.get("sentences", [])]
        wl = _wordline(entries, a.from_tts, "TTS 路径:句级时间(+ 字级若可用)")
    elif a.from_transcript:
        tr = json.loads(Path(a.from_transcript).read_text(encoding="utf-8"))
        entries = [{"start": float(s["start"]),
                    "end": float(s.get("end") or (float(s["start"]) + 3.0)),
                    "text": s["text"], "timestamp": s.get("timestamp") or s.get("chars")}
                   for s in tr.get("segments", [])]
        wl = _wordline(entries, a.from_transcript, "transcript 句级时间(未字级对齐)")
    else:
        return emit(False, "NO_SOURCE", "需要 --from-wordline / --from-tts / --from-transcript", exit_code=2)

    # 卡拉OK判定必须先于建卡:挂字要在必并/合规校验之前做(预算口径=显示字形)
    karaoke = bool(a.karaoke)
    kar_note = ""
    if karaoke and not a.from_wordline:
        return emit(False, "KARAOKE_NEEDS_WORDLINE", "--karaoke 仅支持 --from-wordline", exit_code=2)
    if karaoke:
        ok, why = _karaoke_ready(wl)
        if not ok:
            if a.allow_degraded:
                karaoke = False
                kar_note = f"卡拉OK 已降级为普通字幕({why})"
            else:
                return emit(False, "KARAOKE_NEEDS_WORD_TS",
                            why + ";用 pkg 后端重建 wordline,或加 --allow-degraded 降级为普通字幕",
                            exit_code=2)

    if a.override:
        # Agent 复核修正(ADR-0020):时间真相源仍是 wordline,只动文本分组
        if not a.from_wordline:
            return emit(False, "OVERRIDE_NEEDS_WORDLINE",
                        "--override 仅支持 --from-wordline(需要字级时间真相源)", exit_code=2)
        try:
            ov = json.loads(Path(a.override).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return emit(False, "BAD_OVERRIDE_FILE", f"读取 override 失败:{exc}", exit_code=2)
        if not isinstance(ov, dict) or not isinstance(ov.get("cards"), list):
            return emit(False, "BAD_OVERRIDE_FILE",
                        'override 结构应为 {"cards":[{"span":[a,b] | "text":"…" | '
                        '"textPrefix":"…","textSuffix":"…","note":"..."}]}', exit_code=2)
        try:
            events, meta = events_from_override(wl, ov, max_chars, karaoke=karaoke,
                                                cps_max=preset.get("cpsMax"))
        except ValueError as exc:
            return emit(False, "BAD_OVERRIDE", str(exc), exit_code=2)
        if karaoke and not meta.get("karaokeAttached"):
            karaoke = False
            kar_note = "卡拉OK 降级:字级时间未覆盖任何字幕卡"
    elif a.no_optimize:
        events = build_events([{"start_s": c["startMs"] / 1000, "end_s": c["endMs"] / 1000,
                                "text": c["ch"]} for c in wl.get("chars", [])],
                              max_chars, optimize=False)
        meta = {"degraded": True, "degradeReasons": ["--no-optimize 关闭轻改写"], "violations": [],
                "candidates": [], "ambiguous": 0}
        if karaoke and not _apply_karaoke_chars(events, wl):
            karaoke = False
            kar_note = "卡拉OK 降级:字级时间未覆盖任何字幕卡"
    else:
        events, meta = events_from_wordline(wl, max_chars, terms=terms, top=a.top,
                                            mode=a.segment, karaoke=karaoke,
                                            cps_max=preset.get("cpsMax"),
                                            dual_style=dual_style, engine=engine)
        if karaoke and not meta.get("karaokeAttached"):
            karaoke = False
            kar_note = "卡拉OK 降级:字级时间未覆盖任何字幕卡"
    # 花字(ADR-0057):事件定形后套模板;不改卡文本,红线(必并/CPS/字数)天然继承
    huazi_idx: set = set()
    if a.huazi:
        try:
            huazi_tpl = load_huazi_template(a.huazi)
        except (KeyError, ValueError) as exc:
            return emit(False, "BAD_HUAZI", str(exc), exit_code=2)
        keywords = [k.strip() for k in a.huazi_keywords.split(",") if k.strip()]
        huazi_idx = apply_huazi(events, huazi_tpl, keywords)
        meta["huazi"] = {"id": huazi_tpl["id"], "effect": huazi_tpl["effect"],
                         "appliedCards": len(huazi_idx),
                         "keywords": keywords}
        if not huazi_idx:
            meta["degradeReasons"] = list(meta.get("degradeReasons") or []) + [
                f"花字选卡 0 张(关键词 {keywords} 未命中任何卡)"]
    if kar_note:
        meta["degradeReasons"] = list(meta.get("degradeReasons") or []) + [kar_note]
    if not a.no_snap:
        meta["snapped"] = snap_events_to_frames(events, a.fps)
        # B3(BUGREPORT-20260913):snap 的 start floor / end ceil 会把相邻卡推回
        # 30–40ms 重叠(rs_sync 容差 1 帧 → 判 FAIL)。锚点释放余量内再收一次间距;
        # 余量耗尽仍重叠的留给 rs_sync 帧容差(对齐精度优先,Hard Rule 20)。
        meta["postSnapGaps"] = _enforce_gaps(events, a.fps)
    out = Path(a.out)
    # T2.6/H6:S7 生成期帧网格对齐(单一真相源)—— 输出落点在工程内时,按 IR 主轨
    # 各段「帧取整起点 − 名义起点」平移事件;盘面 subtitles.ass 即烧录时间基准,
    # 不再有 _build/subtitled_aligned.ass 第二套时间(S9 对账/交付/缓存同源)。
    ir_doc = _find_project_ir(out)
    if ir_doc is not None:
        meta["frameGridAligned"] = align_events_to_frame_grid(
            events, _project_video_clips(ir_doc), a.fps or 30.0)
    # B5(BUGREPORT-20260913)落盘防御:cards.json 与 ass 由**同一份 events** 写出,
    # 时间必须单调、正时长、无重叠;任何上游调整(必并/延长/间距/帧对齐)后在此兜底,
    # 不得把倒挂/漂移的时间写进审计件。
    _finalize_events(events, a.fps or 30.0)

    out.mkdir(parents=True, exist_ok=True)
    write_srt(events, out / "master.srt")
    dual_applied = write_ass(events, out / "subtitles.ass", style, ratio, canvas,
                             karaoke=karaoke, dual_style=dual_style,
                             huazi_tpl=(huazi_tpl if a.huazi and huazi_idx
                                        else None))   # M1:复用外层已加载的模板,不再二次读盘解析

    if meta["candidates"]:
        (out / "segments_candidates.json").write_text(
            json.dumps(meta["candidates"], ensure_ascii=False, indent=1), encoding="utf-8")

    # T4.12:Agent 复核队列——ambiguous / 违规 / I1 语义命中 / 预检未拆动 → review_queue.json;
    # --auto 下必定落盘(空队列也是显式留痕),默认只在非空时写。
    review_queue = meta.get("reviewQueue") or []
    if review_queue or a.auto:
        (out / "review_queue.json").write_text(
            json.dumps({"auto": bool(a.auto),
                        "postProcess": meta.get("postProcess"),
                        "items": review_queue}, ensure_ascii=False, indent=1),
            encoding="utf-8")

    # Agent 复核输入:卡 ↔ charSpan(wordline 内容字全局索引);改 span 后用 --override 回灌
    cards_json = [{"i": i, "text": e["text"], "voice": e.get("voice") or "commentary",
                   "charSpan": e.get("charSpan"),
                   "startMs": int(round(e["start"] * 1000)), "endMs": int(round(e["end"] * 1000))}
                  for i, e in enumerate(events)]
    (out / "cards.json").write_text(
        json.dumps({"maxChars": max_chars, "ratio": ratio,
                    "finalTimes": True,   # B5:时间 = 最终 ass 同源快照(必并/延长/间距/帧对齐之后)
                    "cards": cards_json},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    msg = f"{len(events)} 条字幕事件(卡切分 {a.segment},每卡 ≤{max_chars} 字)"
    if dual_style:
        dm = meta.get("dualStyle") or {}
        if dual_applied:
            msg += (f";双 Style 生效(解说 {len(events) - (dm.get('dialogueCards') or 0)}"
                    f" / 对白 {dm.get('dialogueCards')} 卡,引号区分)")
        else:
            msg += ";双 Style 降级:单 Style 出卡(wordline 无对白标记)"
    if meta.get("overrideApplied"):
        msg += f";override 重建 {meta['overrideCards']} 卡"
        if meta.get("residualRegrouped"):
            msg += f";P30-2 余字自动重组 {meta['residualRegrouped']} 段"
        if meta.get("overridePrecheck"):
            msg += (f";P30-3 预检:{len(meta['overridePrecheck'])} 张超长卡已在自然停顿处拆分"
                    f"({';'.join(str(n['splitInto']) for n in meta['overridePrecheck'])})")
    gc = meta.get("ghostCards") or {}
    if gc.get("merged") or gc.get("dropped"):
        msg += f";P28-2 幽灵卡:并卡 {gc.get('merged', 0)} / 丢弃 {len(gc.get('dropped', []))}"
    if meta.get("huazi"):
        msg += (f";花字 {meta['huazi']['id']} 命中 {meta['huazi']['appliedCards']} 卡"
                f"({meta['huazi']['effect']})")
    if meta["ambiguous"]:
        msg += f";{meta['ambiguous']} 句切分歧义(见 segments_candidates.json)"
    sc = meta.get("segscore") or {}
    if sc.get("score") is not None:
        msg += f";断句质量分 {sc['score']:.3f}"
    if review_queue:
        msg += f";⚠ {len(review_queue)} 项待 Agent 复核(review_queue.json)"
    if meta["violations"]:
        msg += f";⚠ {len(meta['violations'])} 项约束违规"
    if meta["degraded"]:
        msg += ";⚠ 降级模式:" + ";".join(meta["degradeReasons"][:2])

    return emit(True, "SUBTITLE_OK", msg,
                {"srt": str(out / "master.srt"), "ass": str(out / "subtitles.ass"),
                 "cards": str(out / "cards.json"),
                 "reviewQueue": str(out / "review_queue.json") if review_queue else None,
                 "style": style, "ratio": ratio, "platform": a.platform,
                 "canvas": canvas, "count": len(events),
                 "dualStyle": dual_style,
                 "dualStyleApplied": bool(dual_applied),
                 "huazi": meta.get("huazi"),
                 "font": resolve_font(),
                 "fontDegraded": font_state()["degraded"],
                 "overrideApplied": bool(meta.get("overrideApplied")),
                 "maxChars": max_chars, "ambiguous": meta["ambiguous"],
                 "segscore": meta.get("segscore"),
                 "unsatisfied": meta.get("unsatisfied"),
                 "postProcess": meta.get("postProcess"),
                 "reviewQueueCount": len(review_queue),
                 "tokenizer": engine,
                 "violations": meta["violations"][:20], "degraded": meta["degraded"],
                 "charTimingEstimated": bool(meta.get("charTimingEstimated")),
                 "degradeReasons": meta["degradeReasons"]})


def _wordline(entries: list[dict], source: str, reason: str) -> dict:
    import rs_align
    return rs_align.build_wordline(entries, source, degraded=reason)


if __name__ == "__main__":
    sys.exit(main())
