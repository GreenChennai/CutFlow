"""S1 字级对齐:产出 wordline.json(全片时间的唯一真相源,ADR-0011)。

用法:
  rs_align.py build --from-transcript 02_转写与校对/transcript_corrected.json --out 05_时间线工程/wordline.json
  rs_align.py build --from-tts 03_创作素材/tts/manifest.json --out 05_时间线工程/wordline.json
  rs_align.py build --media 01_原始素材/a.mp4 --out 05_时间线工程/wordline.json
  rs_align.py build --media a.mp4 --hotwords "安信德 GEO优化" --out 05_时间线工程/wordline.json
  rs_align.py smooth 05_时间线工程/wordline.json --out 05_时间线工程/wordline.final.json
  rs_align.py remap 05_时间线工程/wordline.json --cutlist 04_粗剪决策/cutlist.json --out 05_时间线工程/wordline.final.json
  rs_align.py remap … --force-remap            # P29-1:越过 final 域防护(慎用)
  rs_align.py prune-ghost 05_时间线工程/wordline.final.json --cutlist 04_粗剪决策/cutlist.applied.json
  rs_align.py refresh-durations 05_时间线工程/wordline.final.json --media 01_原始素材/a.mp4

三条入口统一落到同一数据结构;取不到字级时间戳时降级为「句级均分」并**显式标注 degraded**。
本模块同时导出 map_src_to_final():全部下游唯一允许的时间换算函数。
P26(副文档 07):build 的 srcDurationMs 以 **ffprobe 实测媒体**为准,并内置
「末字 endMs==记录总时长」的钳制检测(只标疑似不改数)。
P28:remap 本体丢弃 src 区间完全落在删除区间内的幽灵字符,重建 span、重排 chars[].i。
P29:对已重映射(space=final)的 wordline 拒绝二次 remap;只改时长走 refresh-durations。
"""
from __future__ import annotations

import argparse
import copy
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import die, emit, load_config, duration_ledger_error, sync_wordline_durations  # noqa: E402
import segmentation  # noqa: E402  — PUNCT_WS 与断句/字幕同一标点口径

VERSION = 1
GAP_MIN_MS = 150          # 相邻字间隔超过此值记为一个 gap(供断句用)
MIN_COVERAGE = 0.99       # 门禁:时间跨度覆盖率(见 time_coverage)


def probe_media_duration_ms(media, cfg: dict | None = None) -> int | None:
    """P26-1:ffprobe 实测媒体时长(ms)。

    凡是「总时长/末段边界」一律以实测为准,不信任上游记录值(20260920 NCLM1605
    教训:wordline.srcDurationMs 继承自 ASR 链路,比真实媒体短 789ms,粗剪把
    keep 终点钉在错误总时长上 → 片尾戛然而止)。工具不可用/解码失败返回 None,
    由调用方退回记录值并留痕(探测缺席必须显式,不静默)。
    """
    from rs_common import ffprobe_json
    try:
        info = ffprobe_json(media, cfg)
        dur = float(info.get("format", {}).get("duration") or 0)
        if dur <= 0:                       # format 缺 duration 时取流级最大值
            for s in info.get("streams") or []:
                try:
                    dur = max(dur, float(s.get("duration") or 0))
                except (TypeError, ValueError):
                    continue
        return int(round(dur * 1000)) if dur > 0 else None
    except SystemExit:
        return None
    except Exception:  # noqa: BLE001 — 无 ffprobe/坏文件:返回 None,调用方降级
        return None


def detect_end_clamp(chars: list[dict], recorded_dur_ms: int, fps: int = 30) -> dict | None:
    """P26-2 钳制检测:末字 endMs 恰与记录总时长重合(误差 <1 帧)→ 疑似被钳制。

    「字尾 endMs == 总时长」几乎必然意味着「音频被按错误长度喂给 ASR」或
    「时长字段被字尾钳制」—— 只标疑似并给复核提示(RMS 曲线验证结尾是
    说完收静还是说一半被切),**不改任何数**(经验贴:只标疑似不改数)。
    """
    hard = [c for c in chars if str(c.get("ch", "")).strip()]
    if not hard or recorded_dur_ms <= 0:
        return None
    last_end = max(int(c["srcEndMs"]) if "srcEndMs" in c else int(c["endMs"]) for c in hard)
    tol = int(round(1000.0 / max(1, fps)))
    if abs(last_end - recorded_dur_ms) < tol:
        return {"suspect": True, "lastCharEndMs": last_end,
                "recordedDurationMs": int(recorded_dur_ms), "toleranceMs": tol,
                "hint": "末字 endMs 与记录总时长重合(误差 <1 帧):疑似按错误长度喂给 ASR;"
                        "请用 ffprobe 实测媒体时长复核,并按 50ms 步长看 RMS 曲线确认"
                        "结尾是「说完收静」还是「说一半被切」"}
    return None


def time_coverage(chars: list[dict], src_dur: int) -> float:
    """时间跨度覆盖率 = (首字起点→末字终点) ÷ 转写声明区间。

    2026-09-11 dev-jj2815 实测修正:旧公式(字时长和 ÷ 末字时间)把词间停顿
    全算成"未覆盖"——真实字级时间戳必然 ~75%,降级均分反而 100%,语义颠倒。
    字间停顿是语音常态,不算未覆盖;该指标只应对 ASR 漏转写开头/结尾敏感。
    """
    hard = [c for c in chars if str(c.get("ch", "")).strip()]
    if not hard or src_dur <= 0:
        return 0.0
    span = max(c["srcEndMs"] for c in hard) - min(c["srcStartMs"] for c in hard)
    return round(max(0.0, min(1.0, span / src_dur)), 4)
MIN_CONF_MEDIAN = 0.8     # 门禁:conf 中位数


# ---------------------------------------------------------------- 时间换算

def keep_to_segments(keep: list[list[int]]) -> list[dict]:
    """keep 区间 → 分段线性映射段(供 map_src_to_final 使用)。"""
    segs, acc = [], 0
    for src_in, src_out in keep:
        segs.append({"srcIn": int(src_in), "srcOut": int(src_out), "finalIn": acc})
        acc += int(src_out) - int(src_in)
    return segs


def map_src_to_final(t_src_ms: float, segments: list[dict]) -> float:
    """源域时间 → 成片域时间。**全下游唯一允许的时间换算入口。**

    落在被删除区间的时刻 → 吸附到下一个 keep 区间起点(不插值穿透),保证单调不减。
    """
    if not segments:
        return float(t_src_ms)
    t = float(t_src_ms)
    if t <= segments[0]["srcIn"]:
        return float(segments[0]["finalIn"])
    for seg in segments:
        if seg["srcIn"] <= t < seg["srcOut"]:
            return float(seg["finalIn"] + (t - seg["srcIn"]))
    nxt = [s for s in segments if s["srcIn"] > t]
    if nxt:
        return float(nxt[0]["finalIn"])
    last = segments[-1]
    return float(last["finalIn"] + (last["srcOut"] - last["srcIn"]))


# ---------------------------------------------------------------- 构建

def _strip_speaker(text: str) -> str:
    import re
    return re.sub(r"^\s*说话人\d+\s*[:：]\s*", "", text or "").strip()


def build_wordline(segments: list[dict], source: str, *, fps: int = 30,
                   degraded: str | None = None, space: str = "source",
                   media_duration_ms: int | None = None) -> dict:
    """segments: [{start, end, text, timestamp?}] → wordline。

    有 char-level timestamp(每字 [start_ms, end_ms])时用真值;否则句内均分并标 degraded。
    P26-1:`media_duration_ms`(ffprobe 实测)给出时 `srcDurationMs` 直接取实测值,
    **不再**取 max(末字 endMs, 转写段终点)—— 那正是片尾被截断的根因。
    """
    chars: list[dict] = []
    sentences: list[dict] = []
    degrade_reasons = [degraded] if degraded else []
    char_timing_estimated = False     # 无字级时间戳 → 字时间是句内估算,不可当字级用

    for si, seg in enumerate(segments):
        raw_text = seg.get("text") or ""
        text = _strip_speaker(raw_text) if seg.get("strip_speaker", True) else raw_text
        try:
            st_ms = float(seg.get("start", seg.get("start_s", 0))) * 1000.0
            en_ms = float(seg.get("end", seg.get("end_s", 0)) or 0) * 1000.0
        except (TypeError, ValueError):
            st_ms, en_ms = 0.0, 0.0
        if en_ms <= st_ms:
            en_ms = st_ms + max(1000.0, 120 * len(text))

        ts = seg.get("timestamp") or seg.get("char_timestamps") or seg.get("chars")
        idxs: list[int] = []
        if isinstance(ts, list) and len(ts) == len(text) and text:
            for ch, pair in zip(text, ts):
                a, b = int(pair[0]), int(pair[1])
                chars.append({"i": len(chars), "ch": ch, "startMs": a, "endMs": max(b, a + 20),
                              "srcStartMs": a, "srcEndMs": max(b, a + 20),
                              "conf": round(float(seg.get("conf", 0.95)), 3)})
                idxs.append(len(chars) - 1)
        else:
            n = max(len(text), 1)
            span = en_ms - st_ms
            for k, ch in enumerate(text):
                a = int(round(st_ms + span * k / n))
                b = int(round(st_ms + span * (k + 1) / n))
                chars.append({"i": len(chars), "ch": ch, "startMs": a, "endMs": max(b, a + 20),
                              "srcStartMs": a, "srcEndMs": max(b, a + 20), "conf": 0.40,
                              "estimated": True})
                idxs.append(len(chars) - 1)
            if text:
                char_timing_estimated = True
                degrade_reasons.append("字级时间戳缺失:句内按均分估算(不可当字级用)")

        punc = ""
        for ch in reversed(text):
            if ch in "。！？；，、：!?;:,…":
                punc = ch
                break
        sentences.append({"id": si, "span": [idxs[0], idxs[-1] + 1] if idxs else [0, 0],
                          "punc": punc, "text": text})

    hard = [c for c in chars if c["ch"].strip()]
    confs = [c["conf"] for c in hard] or [0.0]
    seg_end = 0
    for s in segments:                        # 转写声明的时间轴终点(含结尾静音)
        try:
            seg_end = max(seg_end, int(round(float(s.get("end", s.get("end_s", 0)) or 0) * 1000)))
        except (TypeError, ValueError):
            pass
    recorded_dur = max(max((c["srcEndMs"] for c in chars), default=0), seg_end)
    # P26-1:总时长以 ffprobe 实测为唯一真相;测不到才退回 ASR 链路记录值(留痕)
    measured = int(media_duration_ms) if media_duration_ms and int(media_duration_ms) > 0 else 0
    src_dur = measured or recorded_dur
    coverage = time_coverage(hard, src_dur)
    clamp = detect_end_clamp(chars, recorded_dur, fps)

    return {
        "version": VERSION,
        "source": source,
        "space": space,
        "fps": fps,
        "chars": chars,
        "gaps": compute_gaps(chars),
        "sentences": sentences,
        "speakers": sorted({s.get("speaker") for s in segments if s.get("speaker")}),
        "srcDurationMs": src_dur,
        "finalDurationMs": src_dur,
        "removedMs": 0,
        "stats": {"charCount": len(hard), "coverage": round(coverage, 4),
                  "confMedian": round(statistics.median(confs), 3)},
        "degraded": bool(degrade_reasons),
        "degradeReasons": sorted(set(degrade_reasons)),
        # 字级时间是"句内均分估算"而非真实时间戳 → 下游只能按句级用,不得细分到字
        "charTimingEstimated": char_timing_estimated,
        # P26-1/2 时长来源留痕 + 钳制检测(只标疑似,不改数)
        "durationProvenance": "ffprobe" if measured else "asr-chain",
        "recordedDurationMs": recorded_dur,
        **({"endClampSuspect": clamp} if clamp else {}),
    }


def compute_gaps(chars: list[dict], min_ms: int = GAP_MIN_MS) -> list[dict]:
    """相邻字间隔 ≥ min_ms 记为一个 gap(断句的候选边界来源之一)。"""
    gaps = []
    for a, b in zip(chars, chars[1:]):
        d = b["startMs"] - a["endMs"]
        if d >= min_ms:
            kind = "silence" if d >= 600 else "pause"
            gaps.append({"after": a["i"], "ms": int(d), "kind": kind})
    return gaps




# ---------------------------------------------------------------- 能量校准(v0.13 FMSmartSnap)

ENV_SR = 16000                    # 包络采样率
ENV_WIN_MS = 10                   # RMS 窗长
ENV_SUSTAIN_MS = 50               # 起止判定需连续活跃的时长
DEFAULT_CALIB_WINDOW_MS = 250     # 搜索窗口
DEFAULT_CALIB_MIN_SHIFT_MS = 20   # 小于此偏移不动(噪声抖动)


def energy_envelope(media: Path, cfg: dict | None = None,
                    sr: int = ENV_SR, win_ms: int = ENV_WIN_MS) -> tuple[list[float], int]:
    """mono PCM → RMS 能量包络(每 win_ms 一个值)。纯 stdlib(array+math)。

    返回 (envelope, win_ms_actual) —— 第二个值是每窗毫秒宽(供 mask 下标换算:
    下标 i 覆盖 [i*win_ms_actual, (i+1)*win_ms_actual) );失败 die(PCM_EXTRACT_FAIL)。
    """
    import array as _array
    import subprocess as _sp
    from rs_common import ffmpeg_bin, die
    win = max(1, int(sr * win_ms / 1000))
    cmd = [ffmpeg_bin(cfg or {}), "-v", "error", "-i", str(media), "-vn",
           "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    proc = _sp.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        die(4, "PCM_EXTRACT_FAIL",
            f"音频包络提取失败:{(proc.stderr or b'')[-200:]}(文件损坏或无音轨)")
    samples = _array.array("h")
    samples.frombytes(proc.stdout[:len(proc.stdout) // 2 * 2])
    win_ms_actual = max(1, round(win * 1000 / sr))
    step = round(sr * win_ms_actual / 1000)
    n = len(samples) // step
    env: list[float] = []
    for i in range(n):
        chunk = samples[i * step:(i + 1) * step]
        acc = 0
        for s in chunk:
            acc += s * s
        env.append((acc / step) ** 0.5)
    return env, win_ms_actual


def _active_mask(env: list[float], peak_ratio: float = 0.08,
                 floor_mult: float = 3.0) -> list[bool]:
    """能量活跃掩码:env > max(噪声底*floor_mult, 峰值*peak_ratio)。"""
    if not env:
        return []
    peak = max(env)
    floor = sorted(env)[max(0, len(env) // 5)]        # 20 分位近似噪声底
    th = max(floor * floor_mult, peak * peak_ratio, 1.0)
    return [v > th for v in env]


def _energy_onset(mask: list[bool], t0_ms: int, t1_ms: int, win_ms: int = ENV_WIN_MS,
                  sustain_ms: int = ENV_SUSTAIN_MS, *, from_end: bool = False) -> int | None:
    """在 [t0,t1] 内找首个(或末个,from_end)连续活跃点,返回 ms;找不到 None。

    mask 下标 = 包络窗序号(每窗 ENV_WIN_MS);t0/t1 单位 ms。
    """
    sustain = max(1, sustain_ms // ENV_WIN_MS)
    i0, i1 = max(0, t0_ms // ENV_WIN_MS), min(len(mask), t1_ms // ENV_WIN_MS)
    if from_end:
        rng = range(i1 - 1, i0 - 1, -1)
    else:
        rng = range(i0, i1)
    run = 0
    for i in rng:
        run = run + 1 if mask[i] else 0
        if run >= sustain:
            return (i - sustain + 1) * win_ms if not from_end else (i + 1) * win_ms
    return None


def calibrate_wordline(wl: dict, media: Path, cfg: dict | None = None, *,
                       window_ms: int = DEFAULT_CALIB_WINDOW_MS,
                       min_shift_ms: int = DEFAULT_CALIB_MIN_SHIFT_MS) -> tuple[dict, dict]:
    """按音频能量包络校准 wordline 字/句时间。

    返回 (new_wl, report)。report 含句级前后偏差(median/p95)、修正数、跳过原因。
    """
    env, win_ms_used = energy_envelope(media, cfg)
    mask = _active_mask(env)
    chars = wl.get("chars") or []
    sents = wl.get("sentences") or []
    if not chars:
        die(2, "NO_CHARS", "wordline 无 chars,无法校准")
    if not mask:
        rep = {"sentences": len(sents), "snapped": 0, "skipped": len(sents),
               "medianBeforeMs": None, "medianAfterMs": None,
               "note": "音频无可检测的语音能量(静音/无声素材),未做修正"}
        out = json.loads(json.dumps(wl, ensure_ascii=False))
        out["calibration"] = rep
        return out, rep
    if not sents:                       # 无句结构:按 8 字滑窗粗分组兜底
        sents = [{"id": i, "span": [i, min(i + 8, len(chars))]}
                 for i in range(0, len(chars), 8)]
    space = wl.get("space", "source")
    frame_ms = 1000.0 / float(wl.get("fps") or 30)
    rows, moves = [], []
    for s in sents:
        a, b = s.get("span") or [0, 0]
        if b <= a:
            continue
        s0 = int(chars[a]["startMs"])
        s1 = int(chars[b - 1]["endMs"])
        onset = _energy_onset(mask, s0 - window_ms, s0 + window_ms, win_ms_used)
        offset = (onset - s0) if onset is not None else None
        end_at = _energy_onset(mask, s1 - window_ms, s1 + window_ms, win_ms_used, from_end=True)
        end_off = (end_at - s1) if end_at is not None else None
        rows.append({"id": s.get("id"), "startMs": s0, "onsetOffsetMs": offset,
                     "endOffsetMs": end_off})
        if offset is not None and abs(offset) >= min_shift_ms:
            moves.append((a, b, offset))
    def _med(vals: list[int]) -> float | None:
        vs = sorted(vals)
        return float(vs[len(vs) // 2]) if vs else None
    med_before = _med([abs(r["onsetOffsetMs"]) for r in rows if r["onsetOffsetMs"] is not None])
    # 逐句位移(含全局偏置一次性修正);守单调:句 i 位移后 start 不得早于句 i-1 位移后 end
    applied: list[dict] = []
    prev_end = -1
    for (a, b, off) in moves:
        s0 = int(chars[a]["startMs"]) + off
        s0 = max(s0, prev_end + 1)
        real = s0 - int(chars[a]["startMs"])
        if abs(real) < min_shift_ms:
            real = 0
        delta = {"a": a, "b": b, "shift": int(real)}
        applied.append(delta)
        prev_end = int(chars[b - 1]["endMs"]) + real
    for d in applied:
        for i in range(d["a"], d["b"]):
            c = chars[i]
            c["startMs"] = int(c["startMs"]) + d["shift"]
            c["endMs"] = int(c["endMs"]) + d["shift"]
            if space == "source" and "srcStartMs" in c:
                c["srcStartMs"] = int(c["srcStartMs"]) + d["shift"]
                c["srcEndMs"] = int(c["srcEndMs"]) + d["shift"]
        if applied:
            d["applied"] = True
    # 残差统计(校准后再测一次)
    after = []
    for r, d in zip(rows, [None] * len(rows)):
        pass
    # 重算残差:直接按位移后 chars 重新 onset
    resid = []
    for s, d in zip([s for s in sents if (s.get("span") or [0, 0])[1] > (s.get("span") or [0, 0])[0]],
                    [None] * len(sents)):
        pass
    # 简化:重扫一遍
    resid_rows = []
    for s in sents:
        a, b = s.get("span") or [0, 0]
        if b <= a:
            continue
        s0 = int(chars[a]["startMs"])
        onset = _energy_onset(mask, s0 - window_ms, s0 + window_ms, win_ms_used)
        if onset is not None:
            resid_rows.append(abs(onset - s0))
    med_after = _med(resid_rows)
    gaps = compute_gaps(chars)
    wl["gaps"] = gaps
    rep = {"sentences": len(sents), "snapped": len(applied),
           "skipped": len(sents) - len(applied),
           "medianBeforeMs": round(med_before, 1) if med_before is not None else None,
           "medianAfterMs": round(med_after, 1) if med_after is not None else None,
           "p95BeforeMs": round(sorted([abs(r["onsetOffsetMs"]) for r in rows if r["onsetOffsetMs"] is not None])[int(0.95 * max(0, len([r for r in rows if r["onsetOffsetMs"] is not None]) - 1))], 1) if any(r["onsetOffsetMs"] is not None for r in rows) else None,
           "windowMs": window_ms, "minShiftMs": min_shift_ms,
           "shifts": [{"charRange": [d["a"], d["b"]], "shiftMs": d["shift"]} for d in applied]}
    wl["calibration"] = rep
    wl["calibrated"] = True
    return wl, rep


# ---------------------------------------------------------------- 重映射

def _removed_spans(keep: list[list[int]], total: int) -> list[list[int]]:
    """keep 区间的补集 = 被删除区间(供幽灵字符判定)。"""
    spans: list[list[int]] = []
    cursor = 0
    for a, b in sorted((int(x), int(y)) for x, y in keep):
        if a > cursor:
            spans.append([cursor, a])
        cursor = max(cursor, b)
    if cursor < total:
        spans.append([cursor, total])
    return spans


def remap_wordline(wl: dict, keep: list[list[int]], cutlist: dict | None = None) -> dict:
    """按 CutList 的 keep 区间把 wordline 从 source 域重映射到 final 域。

    P28-1(副文档 07):src 区间**完全落在 remove 区间内**的字符(幽灵字符,
    整句复录/口误被删)在本体直接丢弃,并重建 `sentences.span`、重排 `chars[].i`
    —— 下游契约要求 i 连续、span 自洽;此前靠临时脚本 _drop_ghost_chars.py 补救,
    现升格为本体行为(整句被删的工程 remap 后 S9 直接全绿)。
    """
    segs = keep_to_segments(keep)
    out = json.loads(json.dumps(wl))
    chars = out.get("chars") or []
    total_src = int(out.get("srcDurationMs") or 0)
    if chars:
        total_src = max(total_src, max(int(c.get("srcEndMs", c["endMs"])) for c in chars))
    removed = _removed_spans(keep, total_src)

    def _is_ghost(c: dict) -> bool:
        s = int(c.get("srcStartMs", c["startMs"]))
        e = int(c.get("srcEndMs", c["endMs"]))
        return any(ra <= s and e <= rb for ra, rb in removed)

    new_chars: list[dict] = []
    old_to_new: dict[int, int] = {}
    dropped = 0
    for c in chars:
        if _is_ghost(c):
            dropped += 1
            continue                       # 幽灵字符:src 区间完全落在被删区间内
        old_to_new[int(c["i"])] = len(new_chars)
        c = dict(c)
        c["srcStartMs"] = c["startMs"]
        c["srcEndMs"] = c["endMs"]
        s = map_src_to_final(c["startMs"], segs)
        e = map_src_to_final(c["endMs"], segs)
        c["startMs"], c["endMs"] = int(round(s)), int(round(max(e, s + 20)))
        new_chars.append(c)
    # 重排 chars[].i(下游契约:i 必须连续)
    for k, c in enumerate(new_chars):
        c["i"] = k
    out["chars"] = new_chars

    # 重建 sentences.span(整句被删 → 句一并丢弃;句内部分被删 → span 收缩、文本重拼)
    new_sents: list[dict] = []
    dropped_sents = 0
    for s in out.get("sentences") or []:
        a, b = s.get("span") or [0, 0]
        kept_idx = [old_to_new[i] for i in range(int(a), int(b)) if i in old_to_new]
        if not kept_idx:
            dropped_sents += 1
            continue
        text = "".join(new_chars[k]["ch"] for k in kept_idx)
        punc = s.get("punc") or ""
        if punc and punc not in text:
            punc = ""                       # 原句尾标点已被删,不能继承
        new_sents.append({"id": len(new_sents), "span": [min(kept_idx), max(kept_idx) + 1],
                          "punc": punc, "text": text})
    out["sentences"] = new_sents

    # 保证单调
    prev = 0
    for c in new_chars:
        if c["startMs"] < prev:
            c["startMs"] = prev
        if c["endMs"] <= c["startMs"]:
            c["endMs"] = c["startMs"] + 20
        prev = c["startMs"]
    out["space"] = "final"
    out["finalDurationMs"] = sum(int(b) - int(a) for a, b in keep)
    out["removedMs"] = int((cutlist or {}).get("removedMs") or 0) or \
        sum(rb - ra for ra, rb in removed)
    out["gaps"] = compute_gaps(new_chars)
    if cutlist:
        out["cutlist"] = cutlist.get("source") or cutlist.get("version")
    hard_n = len([c for c in new_chars if str(c.get("ch", "")).strip()])
    out["stats"] = dict(out.get("stats") or {}, monotonic=True,
                        charCount=hard_n,
                        finalDurationMs=out["finalDurationMs"])
    # P28-1 留痕:丢弃规模可审计(temporary-script 时代的补救从此可见)
    out["pruned"] = {"ghostChars": dropped, "ghostSentences": dropped_sents,
                     "chars": f"{len(chars)}->{len(new_chars)}"}
    return out


# ---------------------------------------------------------------- 平滑(v0.12 B7)

SMOOTH_PUNCT = frozenset(segmentation.PUNCT_WS + "·—–-《》〈〉")


def smooth_wordline(wl: dict) -> tuple[dict, dict]:
    """wordline 平滑上游化(B7):此前纯动画/配音工程各自手写 _smooth_wordline.py,
    把标点设成零宽时还违反 rs_verify 的单调门禁(endMs<=startMs 硬失败)。统一规则:
      1. 标点/空白零宽:endMs = startMs + 1(+1ms 恰好满足严格单调,不占显示时长);
      2. startMs 单调不减(后字起点早于前字 → 钳到前字起点);
      3. 内容字重叠钳制:前字 endMs > 后字 startMs → 收到后字 startMs
        (不足 1ms 的间隙让位给对齐精度);
      4. 保底:任何 endMs<=startMs → startMs+1。
    不动 build 路径的 max(b, a + 20) 保底——那是单调门禁的第一道护栏,平滑只在本入口做。
    """
    doc = copy.deepcopy(wl)
    chars = doc.get("chars") or []
    stats = {"zeroWidthPunct": 0, "clampedOverlap": 0, "minWidthFixed": 0}
    for c in chars:
        ch = str(c.get("ch", ""))
        if (not ch.strip()) or ch in SMOOTH_PUNCT:
            c["endMs"] = int(c["startMs"]) + 1
            stats["zeroWidthPunct"] += 1
    prev = None
    for c in chars:
        if prev is not None and int(c["startMs"]) < prev:
            c["startMs"] = prev
            stats["clampedOverlap"] += 1
        prev = int(c["startMs"])
    for a, b in zip(chars, chars[1:]):
        if int(a["endMs"]) > int(b["startMs"]):
            a["endMs"] = int(b["startMs"])
            stats["clampedOverlap"] += 1
    for c in chars:
        if int(c["endMs"]) <= int(c["startMs"]):
            c["endMs"] = int(c["startMs"]) + 1
            stats["minWidthFixed"] += 1
    doc["gaps"] = compute_gaps(chars)
    doc["smooth"] = {"applied": True, **stats}
    return doc, stats


# ---------------------------------------------------------------- 校对回灌(v0.6.0)

SENT_END = "。！？；!?"
SENT_ALL = SENT_END + "，、：,:…"
GAP_SENT_MS = 600          # 静音 gap 达到此值也切句
INSERT_CONF = 0.85         # 插入/替换字的置信度(低于真实锚点,高于模糊线)


def _spread(text: str, a: int, b: int, conf: float) -> list[dict]:
    """把一段文本摊到 [a, b] 毫秒内(锚定真实区间内部的局部摊派,非整段比例缩放)。"""
    n = max(len(text), 1)
    span = max(b - a, n)
    out = []
    for k, ch in enumerate(text):
        s = a + span * k // n
        e = a + span * (k + 1) // n
        out.append({"i": 0, "ch": ch, "startMs": int(s), "endMs": int(max(e, s + 1)),
                    "srcStartMs": int(s), "srcEndMs": int(max(e, s + 1)), "conf": conf})
    return out


def _apply_opcodes(ref: list[dict], new_text: str, sm, conf: float) -> tuple[list[dict], dict]:
    """把 `new_text` 的时间锚到 `ref` 上(retext 回灌 / 配音强制对齐共用的核心)。

    - equal 区间:原锚点原样保留(**时间零漂移**)
    - replace:新字在旧区间 [首字start, 末字end] 内均分(有真实锚点兜底)
    - delete:锚点一并删除
    - insert:在相邻两字的 [prev.end, next.start] 区间内均分;区间过窄则压缩单字时长,
      **绝不平移既有锚点**(保住与音频的真实对应)
    """
    durs = sorted(max(c["endMs"] - c["startMs"], 1) for c in ref)
    med = durs[len(durs) // 2]
    out: list[dict] = []
    stats = {"replace": 0, "delete": 0, "insert": 0, "editChars": 0}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.extend(ref[i1:i2])
        elif tag == "delete":
            stats["delete"] += i2 - i1
            stats["editChars"] += i2 - i1
        elif tag == "replace":
            stats["replace"] += 1
            stats["editChars"] += (i2 - i1) + (j2 - j1)
            a = ref[i1]["startMs"]
            b = max(ref[i2 - 1]["endMs"], a + 40 * (j2 - j1))
            out.extend(_spread(new_text[j1:j2], a, b, conf))
        elif tag == "insert":
            stats["insert"] += 1
            stats["editChars"] += j2 - j1
            text = new_text[j1:j2]
            if out and i2 < len(ref):            # 中间插入:[prev.end, next.start]
                a = out[-1]["endMs"]
                b = ref[i2]["startMs"]
                if b < a:
                    b = a
                out.extend(_spread(text, a, b, conf))
            elif out:                            # 末尾插入
                a = out[-1]["endMs"]
                out.extend(_spread(text, a, a + med * len(text), conf))
            else:                                # 开头插入
                f = ref[i2] if i2 < len(ref) else ref[-1]
                a = max(0, f["startMs"] - med * len(text))
                out.extend(_spread(text, a, f["startMs"], conf))
    prev = 0
    for c in out:                                # 单调化(保险):起点不减,终点至少 +1ms
        if c["startMs"] < prev:
            c["startMs"] = prev
        if c["endMs"] <= c["startMs"]:
            c["endMs"] = c["startMs"] + 1
        prev = c["startMs"]
    for k, c in enumerate(out):
        c["i"] = k
    return out, stats


def retime_to_reference(ref_chars: list[dict], text: str, *, conf: float = INSERT_CONF,
                        min_ratio: float = 0.5) -> tuple[list[dict], dict]:
    """把 `text` 的每个字锚到 `ref_chars` 的**真实**字级时间戳上(TTS 配音强制对齐,#10)。

    与 `retext_wordline` 同一套 opcode 逻辑,区别只在方向:
    retext 是"拿校对稿去改已有锚点";本函数是"拿真实锚点去给目标文本定时间"。
    """
    from difflib import SequenceMatcher
    if not ref_chars:
        raise ValueError("参考时间轴没有 chars,无法对齐")
    orig = "".join(c["ch"] for c in ref_chars)
    sm = SequenceMatcher(None, orig, text, autojunk=False)
    if sm.ratio() < min_ratio:
        raise ValueError(f"配音转写与目标文本相似度过低({sm.ratio():.0%}),疑似拿错音频/文本")
    out, stats = _apply_opcodes(ref_chars, text, sm, conf)
    stats["similarity"] = round(sm.ratio(), 4)
    return out, stats


def retext_wordline(wl: dict, new_text: str) -> tuple[dict, dict]:
    """把校对后的文本回灌到 **source 域** wordline:改动锚定在字级时间戳上。

    逻辑见 `_apply_opcodes`;相似度 <0.5 直接拒绝(疑似拿错文件)。
    """
    from difflib import SequenceMatcher
    chars = wl.get("chars") or []
    if not chars:
        raise ValueError("wordline 没有 chars,无法回灌")
    orig = "".join(c["ch"] for c in chars)
    sm = SequenceMatcher(None, orig, new_text, autojunk=False)
    if sm.ratio() < 0.5:
        raise ValueError(f"校对稿与原稿相似度过低({sm.ratio():.0%}),疑似拿错文件")
    out, stats = _apply_opcodes(chars, new_text, sm, INSERT_CONF)

    doc = dict(wl)
    doc["chars"] = out
    doc["sentences"] = _resplit_sentences(out)
    doc["gaps"] = compute_gaps(out)
    hard = [c for c in out if c["ch"].strip()]
    src_dur = max(int(wl.get("srcDurationMs") or 0),
                  max((c["srcEndMs"] for c in out), default=0))
    import statistics
    doc["stats"] = {"charCount": len(hard),
                    "coverage": time_coverage(out, src_dur),
                    "confMedian": round(statistics.median([c["conf"] for c in hard]), 3)}
    doc["degraded"] = bool(wl.get("degraded"))
    doc["srcDurationMs"] = max(src_dur, out[-1]["endMs"] if out else 0)
    doc["retext"] = {"similarity": round(sm.ratio(), 4), **stats,
                     "chars": f"{len(orig)}->{len(new_text)}"}
    return doc, stats


def _resplit_sentences(chars: list[dict]) -> list[dict]:
    """校对后字数已变,旧 span 作废:按强标点/静音 gap 重切句(供 DP 断句用)。"""
    sents: list[dict] = []
    a = 0
    last_punc = ""
    for i, c in enumerate(chars):
        if c["ch"] in SENT_ALL:
            last_punc = c["ch"]
        nxt = chars[i + 1] if i + 1 < len(chars) else None
        gap = (nxt["startMs"] - c["endMs"]) if nxt else 0
        if nxt is None or c["ch"] in SENT_END or gap >= GAP_SENT_MS:
            text = "".join(x["ch"] for x in chars[a:i + 1])
            body = any(x["ch"] not in SENT_ALL for x in chars[a:i + 1])
            if body:
                sents.append({"id": len(sents), "span": [a, i + 1],
                              "punc": last_punc if last_punc in SENT_ALL else "", "text": text})
            elif sents:
                # dev-jj2815 实测:零宽继承时间的孤立标点(如"?")在 gap 切句后
                # 不得单独成句 —— 单标点句会让下游 DP 产卡缺 startMs,污染首卡
                sents[-1]["span"][1] = i + 1
                sents[-1]["text"] += text
                if last_punc in SENT_ALL:
                    sents[-1]["punc"] = last_punc
            a = i + 1
            last_punc = ""
    return sents



def _load_segments(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(doc, list):
        return doc
    return doc.get("segments") or doc.get("sentences") or []


def _from_media(media: Path, cfg: dict, backend: str = "auto",
                max_end_sil: int = 0, hotwords: str = "") -> tuple[list[dict], dict]:
    """调 **CutFlow 自带** ASR(tools/fun_asr.py)取转写 —— 不需要任何外部服务器。

    返回值: (segments, meta),meta 含 backend / capabilities / degraded / degradeReasons。
    热词经 --hotwords 透传(I1):默认模型 paraformer-zh **就是** SeACo-Paraformer
    (funasr name_maps_from_hub.py 的短名映射),热词是其模型级原生能力,直接生效。
    """
    import subprocess
    runner = Path(__file__).resolve().parents[3] / "tools" / "fun_asr.py"
    if not runner.is_file():
        die(3, "NO_ASR_RUNNER", f"找不到自带 ASR 运行器:{runner}")
    cmd = [sys.executable, str(runner), str(media), "--json", "--backend", backend]
    if max_end_sil:
        cmd += ["--max-end-sil", str(max_end_sil)]
    if hotwords:
        cmd += ["--hotwords", hotwords]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip()
    if not out:
        die(3, "ASR_NO_OUTPUT",
            f"自带 ASR 无输出(exit {p.returncode}):{(p.stderr or '')[-300:]}。"
            f"先跑 `python tools/fun_asr.py --probe` 看后端状态,"
            f"或用 `python tools/fetch_deps.py asr --onnx` 部署")
    try:
        doc = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        die(4, "ASR_BAD_JSON", f"自带 ASR 输出无法解析:{out[:200]}")
    if not doc.get("ok"):
        die(3, "ASR_FAILED", doc.get("message") or "自带 ASR 失败",
            {"code": doc.get("code"), "detail": doc.get("data")})
    data = doc.get("data") or {}
    meta = {"backend": data.get("backend"), "capabilities": data.get("capabilities") or {},
            "elapsedSec": data.get("elapsedSec"), "segmentCount": data.get("segmentCount"),
            "asrDegraded": bool(data.get("degraded")),
            "asrDegradeReasons": data.get("degradeReasons") or []}
    return data.get("segments") or [], meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="build",
                    choices=["build", "remap", "retext", "smooth", "calibrate",
                             "prune-ghost", "refresh-durations"])
    ap.add_argument("source", nargs="?", help="remap/retext/smooth/prune-ghost/refresh-durations 时为 wordline 路径")
    ap.add_argument("--from-transcript")
    ap.add_argument("--from-tts")
    ap.add_argument("--media")
    ap.add_argument("--backend", default="auto", choices=["auto", "pkg", "onnx", "server"],
                    help="自带 ASR 后端;auto=精度优先(pkg→onnx→server)")
    ap.add_argument("--hotwords", default="",
                    help="I1 热词(空格分隔):专名/生造词,如「安信德 GEO优化」。"
                         "透传自带 ASR(默认模型 paraformer-zh=SeACo,原生吃热词);"
                         "wordline.asr.hotwords 留痕")
    ap.add_argument("--terms-file", dest="terms_file", default=None,
                    help="热词文件:每行一个词(# 注释;约定放 00_制作简报/terms.txt,"
                         "来源 brief.md 术语表;与 --hotwords 合并)")
    ap.add_argument("--calib-window", dest="calib_window", type=int,
                    default=DEFAULT_CALIB_WINDOW_MS,
                    help="calibrate:能量起点搜索窗口 ms(默认 250)")
    ap.add_argument("--calib-min-shift", dest="calib_min_shift", type=int,
                    default=DEFAULT_CALIB_MIN_SHIFT_MS,
                    help="calibrate:小于该偏移不修正 ms(默认 20,抗噪声抖动)")
    ap.add_argument("--max-end-sil", dest="max_end_sil", type=int, default=0,
                    help="VAD 静音切分阈值 ms(0=用工具默认 400)")
    ap.add_argument("--cutlist")
    ap.add_argument("--force-remap", dest="force_remap", action="store_true",
                    help="P29-1:wordline 已在 final 域时仍强行二次重映射"
                         "(会把 current startMs 当源时间整体错位;确知自己在做什么才用)")
    ap.add_argument("--removed-ms", dest="removed_ms", type=int, default=None,
                    help="refresh-durations:粗剪裁掉量 ms(缺省取 wordline.removedMs,"
                         "再缺省取 --cutlist 的 removedMs)")
    ap.add_argument("--src", help="覆盖 wordline.source;--from-transcript 时用它指向真实素材路径"
                                  "(否则下游 IR 会把转写稿当成视频源)")
    ap.add_argument("--text", help="retext:校对后的纯文本文件(标点可有可无)")
    ap.add_argument("--out", required=False)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="retext:只报告编辑摘要,不写文件")
    a = ap.parse_args()
    out = Path(a.out) if a.out else None


    if a.command == "calibrate":
        if not a.source or not a.media:
            return emit(False, "NO_INPUT",
                        "calibrate 需要:<wordline.json> --media <音视频素材> [--out <path>] "
                        "(无 --out 时原地覆盖)", exit_code=2)
        media = Path(a.media)
        if not media.is_file():
            return emit(False, "NO_MEDIA", f"素材不存在:{media}", exit_code=2)
        wl = json.loads(Path(a.source).read_text(encoding="utf-8"))
        cfg = load_config()
        doc, rep = calibrate_wordline(wl, media, cfg,
                                      window_ms=a.calib_window,
                                      min_shift_ms=a.calib_min_shift)
        out = out or Path(a.source)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        before = rep.get("medianBeforeMs")
        after = rep.get("medianAfterMs")
        msg = (f"能量校准:{rep['snapped']}/{rep['sentences']} 句修正"
               f"(窗口 ±{a.calib_window}ms);起点中位偏差 "
               f"{before if before is not None else '-'}ms → {after if after is not None else '-'}ms → {out}")
        return emit(True, "CALIBRATE_OK", msg,
                    {"path": str(out), **rep})

    if a.command == "smooth":
        if not a.source:
            return emit(False, "NO_INPUT", "smooth 需要:<wordline.json> [--out <path>]"
                          "(无 --out 时原地覆盖)", exit_code=2)
        wl = json.loads(Path(a.source).read_text(encoding="utf-8"))
        doc, stats = smooth_wordline(wl)
        out = out or Path(a.source)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return emit(True, "SMOOTH_OK",
                    f"平滑完成:标点零宽 {stats['zeroWidthPunct']} / 重叠钳制 "
                    f"{stats['clampedOverlap']} / 最小宽度修复 {stats['minWidthFixed']} → {out}",
                    {"path": str(out), **stats})

    if a.command == "retext":
        if not a.source or not a.text:
            return emit(False, "NO_INPUT", "retext 需要:<wordline.json> --text <校对稿.txt>", exit_code=2)
        wl = json.loads(Path(a.source).read_text(encoding="utf-8"))
        if wl.get("space") == "final":
            return emit(False, "NOT_SOURCE_SPACE",
                        "该 wordline 已在 final 域(粗剪后);请回灌到 source 域 wordline 后再重跑 remap", exit_code=2)
        new_text = Path(a.text).read_text(encoding="utf-8")
        new_text = " ".join(new_text.split())          # 压缩换行/多余空白
        try:
            doc, stats = retext_wordline(wl, new_text)
        except ValueError as exc:
            return emit(False, "RETEXT_REJECT", str(exc), exit_code=2)
        msg = (f"校对回灌:{stats['replace']} 处替换 / {stats['delete']} 字删除 / "
               f"{stats['insert']} 处插入,字符 {doc['retext']['chars']},"
               f"相似度 {doc['retext']['similarity']:.0%}(equal 区间时间零漂移)")
        if a.dry_run:
            return emit(True, "RETEXT_DRYRUN", msg + "(dry-run,未写盘)",
                        {"stats": stats, "charCount": doc["stats"]["charCount"],
                         "sentences": len(doc["sentences"])})
        out = out or Path(a.source)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        return emit(True, "RETEXT_OK", f"{msg} → {out}",
                    {"path": str(out), "stats": stats, **doc["stats"],
                     "sentences": len(doc["sentences"]),
                     "hint": "下一步:rs_run --mark S1(记录新状态)→ --from S2 级联"})

    if a.command == "remap":
        if not a.source or not a.cutlist:
            return emit(False, "NO_INPUT", "remap 需要:<wordline.json> --cutlist <cutlist.json>", exit_code=2)
        if not a.out:
            return emit(False, "NO_INPUT", "remap 需要 --out", exit_code=2)
        wl = json.loads(Path(a.source).read_text(encoding="utf-8"))
        cut = json.loads(Path(a.cutlist).read_text(encoding="utf-8"))
        keep = cut.get("keep") or []
        if not keep:
            return emit(False, "NO_KEEP", "cutlist 缺少 keep 区间(先跑 rs_cut.py --apply)", exit_code=2)
        # P29-1 二次重映射防护:remap 会把当前 startMs 当作新的源时间,对已重映射的
        # wordline 再跑一次 = 整体错位(20260920 经验贴 ⚠ 教训)。只改时长请走
        # refresh-durations;确要强行重映射须显式 --force-remap。
        if wl.get("space") == "final" and not a.force_remap:
            return emit(False, "ALREADY_FINAL_SPACE",
                        "该 wordline 已重映射到 final 域(space=final);二次重映射会整体错位。"
                        "只改时长字段请用:rs_align.py refresh-durations <wordline> --media <素材>;"
                        "确要强行重映射请加 --force-remap", exit_code=2)
        ledger = duration_ledger_error({**wl, "srcDurationMs":
                                        cut.get("srcTotalMs") if cut.get("srcTotalMs") else wl.get("srcDurationMs"),
                                        "removedMs": cut.get("removedMs") if cut.get("removedMs") is not None else wl.get("removedMs"),
                                        "finalDurationMs": sum(int(b) - int(a) for a, b in keep)})
        doc = remap_wordline(wl, keep, cut)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        pruned = doc.get("pruned") or {}
        msg = f"重映射完成:{out}"
        if pruned.get("ghostChars"):
            msg += (f";丢弃幽灵字符 {pruned['ghostChars']}(整句 "
                    f"{pruned.get('ghostSentences', 0)} 句),chars {pruned.get('chars')}")
        if ledger:
            msg += f" ⚠ {ledger}"
        return emit(True, "REMAP_OK", msg, {"space": doc["space"],
                    "finalDurationMs": doc["finalDurationMs"], "chars": len(doc["chars"]),
                    "pruned": pruned, "ledgerWarning": ledger})

    if a.command == "prune-ghost":
        # P28-3:_drop_ghost_chars.py 临时脚本升格为官方入口。面向**已重映射**的工程:
        # 按 cutlist 的 keep 补集把 src 区间完全落在删除区间内的字符丢弃,
        # 重建 sentences.span 并重排 chars[].i——不做时间重映射(时间已在 final 域)。
        if not a.source or not a.cutlist:
            return emit(False, "NO_INPUT",
                        "prune-ghost 需要:<wordline.final.json> --cutlist <cutlist.applied.json>"
                        " [--out <path>](无 --out 时原地覆盖)", exit_code=2)
        wl = json.loads(Path(a.source).read_text(encoding="utf-8"))
        if wl.get("space") != "final":
            return emit(False, "NOT_FINAL_SPACE",
                        "prune-ghost 面向已重映射(space=final)的 wordline;source 域请直接"
                        " rs_align.py remap(本体已内置幽灵字符丢弃),无需本命令", exit_code=2)
        cut = json.loads(Path(a.cutlist).read_text(encoding="utf-8"))
        keep = cut.get("keep") or []
        if not keep:
            return emit(False, "NO_KEEP", "cutlist 缺少 keep 区间", exit_code=2)
        chars = wl.get("chars") or []
        total_src = int(wl.get("srcDurationMs") or 0)
        if chars:
            total_src = max(total_src, max(int(c.get("srcEndMs", c["endMs"])) for c in chars))
        removed = _removed_spans(keep, total_src)

        def _ghost(c: dict) -> bool:
            s = int(c.get("srcStartMs", c["startMs"]))
            e = int(c.get("srcEndMs", c["endMs"]))
            return any(ra <= s and e <= rb for ra, rb in removed)

        kept: list[dict] = []
        old_to_new: dict[int, int] = {}
        for c in chars:
            if _ghost(c):
                continue
            old_to_new[int(c["i"])] = len(kept)
            kept.append(dict(c))
        for k, c in enumerate(kept):
            c["i"] = k
        new_sents = []
        for s in wl.get("sentences") or []:
            lo, hi = s.get("span") or [0, 0]
            idxs = [old_to_new[i] for i in range(int(lo), int(hi)) if i in old_to_new]
            if not idxs:
                continue
            text = "".join(kept[k]["ch"] for k in idxs)
            punc = s.get("punc") or ""
            if punc and punc not in text:
                punc = ""
            new_sents.append({"id": len(new_sents), "span": [min(idxs), max(idxs) + 1],
                              "punc": punc, "text": text})
        doc = dict(wl)
        doc["chars"] = kept
        doc["sentences"] = new_sents
        doc["gaps"] = compute_gaps(kept)
        hard_n = len([c for c in kept if str(c.get("ch", "")).strip()])
        doc["stats"] = dict(doc.get("stats") or {}, charCount=hard_n)
        doc["pruned"] = {"ghostChars": len(chars) - len(kept),
                         "ghostSentences": len(wl.get("sentences") or []) - len(new_sents),
                         "chars": f"{len(chars)}->{len(kept)}"}
        ledger = duration_ledger_error(doc)
        dest = out or Path(a.source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        pr = doc["pruned"]
        msg = (f"幽灵字符清理:丢弃 {pr['ghostChars']} 字(整句 {pr['ghostSentences']} 句),"
               f"chars {pr['chars']},i 已重排 → {dest}")
        if ledger:
            msg += f" ⚠ {ledger}"
        return emit(True, "PRUNE_GHOST_OK", msg, {"path": str(dest), **pr, "ledgerWarning": ledger})

    if a.command == "refresh-durations":
        # P29-2:只改时长字段、绝不动字符时间——这是「已重映射工程修正总时长」的
        # 唯一合法入口(对比:remap 会把当前 startMs 当源时间,二次重映射整体错位)。
        if not a.source or not a.media:
            return emit(False, "NO_INPUT",
                        "refresh-durations 需要:<wordline.json> --media <音视频素材>"
                        " [--cutlist <cutlist>] [--out <path>]", exit_code=2)
        media = Path(a.media)
        if not media.is_file():
            return emit(False, "NO_MEDIA", f"素材不存在:{media}", exit_code=2)
        wl = json.loads(Path(a.source).read_text(encoding="utf-8"))
        cfg = load_config()
        measured = probe_media_duration_ms(media, cfg)
        if not measured:
            return emit(False, "PROBE_FAIL",
                        f"ffprobe 实测失败:{media}(工具不可用或文件损坏;时长账拒绝在无实测时改写)",
                        exit_code=4)
        removed = a.removed_ms
        if removed is None:
            removed = wl.get("removedMs")
        if removed is None and a.cutlist:
            removed = json.loads(Path(a.cutlist).read_text(encoding="utf-8")).get("removedMs")
        removed = int(removed or 0)
        old = {"srcDurationMs": wl.get("srcDurationMs"),
               "removedMs": wl.get("removedMs"),
               "finalDurationMs": wl.get("finalDurationMs")}
        doc = sync_wordline_durations(wl, measured, removed)
        clamp = detect_end_clamp(doc.get("chars") or [], int(old.get("srcDurationMs") or 0),
                                 int(doc.get("fps") or 30))
        if clamp:
            doc["endClampSuspect"] = clamp
        ledger = duration_ledger_error(doc)
        dest = out or Path(a.source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        msg = (f"时长已按 ffprobe 实测刷新(字符时间未动):srcDurationMs "
               f"{old['srcDurationMs']} → {doc['srcDurationMs']},removedMs {removed},"
               f"finalDurationMs {old['finalDurationMs']} → {doc['finalDurationMs']} → {dest}")
        if clamp:
            msg += f" ⚠ 疑似钳制:末字 endMs({clamp['lastCharEndMs']})与旧记录总时长重合"
        return emit(True, "REFRESH_DURATIONS_OK", msg,
                    {"path": str(dest), "before": old, "after": {
                        "srcDurationMs": doc["srcDurationMs"],
                        "removedMs": doc["removedMs"],
                        "finalDurationMs": doc["finalDurationMs"]},
                     "endClampSuspect": bool(clamp), "ledgerOk": ledger is None})

    source, segments, degraded = "", [], None
    asr_meta: dict = {}
    # I1 热词:--terms-file(每行一词,# 注释;json 数组亦可)+ --hotwords 合并去重
    hotwords = a.hotwords or ""
    if a.terms_file:
        tp = Path(a.terms_file)
        if not tp.is_file():
            return emit(False, "NO_TERMS_FILE", f"热词文件不存在:{tp}", exit_code=2)
        try:
            terms_doc = json.loads(tp.read_text(encoding="utf-8"))
            file_terms = [str(t) for t in terms_doc] if isinstance(terms_doc, list) else []
        except json.JSONDecodeError:
            file_terms = [ln.strip() for ln in tp.read_text(encoding="utf-8").splitlines()
                          if ln.strip() and not ln.strip().startswith("#")]
        hotwords = " ".join(dict.fromkeys(hotwords.split() + file_terms))
    if a.from_transcript:
        p = Path(a.from_transcript)
        segments, source = _load_segments(p), (a.src or str(p))
    elif a.from_tts:
        p = Path(a.from_tts)
        man = json.loads(p.read_text(encoding="utf-8"))
        segments = [{"start": s["start_s"], "end": s["end_s"], "text": s["text"],
                     "timestamp": s.get("timestamp") or s.get("chars")}
                    for s in man.get("sentences", [])]
        source = str(p)
        degraded = "TTS 路径:句级时间 + 句内均分"
    elif a.media:
        media = Path(a.media)
        if not media.is_file():
            return emit(False, "NO_MEDIA", f"素材不存在:{media}", exit_code=2)
        cfg = load_config()
        source = str(media)
        segments, asr_meta = _from_media(media, cfg, a.backend, a.max_end_sil, hotwords)
        if hotwords:
            asr_meta["hotwords"] = hotwords     # 留痕:复跑/排障要知道当时喂了什么
        if asr_meta.get("asrDegraded"):
            degraded = "；".join(asr_meta.get("asrDegradeReasons")
                                or ["自带 ASR 标注为降级"]) + \
                       f"(后端 {asr_meta.get('backend')})"
    else:
        return emit(False, "NO_INPUT", "需要 --from-transcript / --from-tts / --media 之一", exit_code=2)

    if not a.out:
        return emit(False, "NO_INPUT", "build 需要 --out", exit_code=2)

    # P26-1:ffprobe 实测媒体时长为 srcDurationMs 唯一真相(--media 直接给;
    # --from-transcript 时 --src 指向真实素材同样探测)。测不到退回记录值并留痕。
    probe_target = a.media or (a.src if (a.from_transcript and a.src) else None)
    measured_ms = None
    probe_note = ""
    if probe_target and Path(probe_target).is_file():
        measured_ms = probe_media_duration_ms(probe_target, load_config())
        if not measured_ms:
            probe_note = "⚠ ffprobe 实测失败,srcDurationMs 退回 ASR 链路记录值(请检查素材/工具)"
    doc = build_wordline(segments, source, fps=a.fps, degraded=degraded,
                         media_duration_ms=measured_ms)
    doc["asr"] = asr_meta
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    st = doc["stats"]
    msg = (f"Wordline 已生成:{st['charCount']} 字,时间跨度覆盖率 {st['coverage']:.1%},"
           f"conf 中位数 {st['confMedian']}"
           f";srcDurationMs={doc['srcDurationMs']}({doc['durationProvenance']})")
    if probe_note:
        msg += f" {probe_note}"
    clamp = doc.get("endClampSuspect")
    if clamp:
        msg += (f" ⚠ 疑似钳制:末字 endMs({clamp['lastCharEndMs']})与记录总时长重合"
                f"(误差 <{clamp['toleranceMs']} 帧);{clamp['hint']}")
    if st["coverage"] < MIN_COVERAGE:
        msg += (f" ⚠ 覆盖率 {st['coverage']:.1%} < {MIN_COVERAGE:.0%}:"
                "检查 ASR 是否漏转写开头/结尾(字间停顿不算未覆盖)")
    if asr_meta.get("backend"):
        msg = f"[ASR {asr_meta['backend']}] " + msg
    if doc["degraded"]:
        msg += f" ⚠ 降级模式:{';'.join(doc['degradeReasons'])}"
    return emit(True, "ALIGN_OK", msg, {"path": str(out), **st, "degraded": doc["degraded"],
                                        "degradeReasons": doc["degradeReasons"],
                                        "asr": asr_meta})


if __name__ == "__main__":
    sys.exit(main())
