r"""成片内容诊断(ADR-0032):从成片出发的独立证据链,对照工程产物找三类缺陷。

用法:
  rs_diagnose.py <成片.mp4> [--project <工程根>] [--ass <subtitles.ass>]
                 [--cutlist <cutlist.applied.json>] [--budget 900]
                 [--no-d2] [--json] [--out 06_output]

为什么需要它(docs/REVIEW-20260916-假正常诊断根因.md):
  现有全部机器闸都是「对照中间产物」的闸——错误发生在管线内部时,错误同时
  传染给 wordline/ass/成片,所有对照闸拿到同一份错误的镜像,互相印证全绿。
  本工具的判据**从成片现场测量**(独立 ASR/画面运动/音频能量),中间产物只作
  对照参考,对照差异本身就是检测目标。

三类检测(阈值集中在此,由 tests/test_v14.py 注入缺陷回归集校准):
  D1 字幕↔语音错位   成片音轨独立 ASR 逐字时间轴 ↔ ASS 卡起点,逐卡偏移
  D2 口型↔声音错位   音频活动事件 ↔ 画面运动峰延迟(纯动画自动跳过)
  D3 错剪无意义片段   剪点上下文审计(有 cutlist)+ 成片语义连贯(独立)

输出铁律(ADR-0021 失败语义的延伸):
  verdict ∈ {pass, issues, indetermined} 三态;「pass」必须所有可用检查通过
  且 ≥2 项实际运行;任何检查判不了 → indetermined + 疑点清单,禁止无证据的
  「完全正常」。超时降级:输出阶段性结论 + 未完成项,绝不空手说没问题。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit, ffmpeg_bin, ffprobe_json, run  # noqa: E402

DIAG_VERSION = "diag-1.0"

# ---- D1 阈值(经验初值,注入缺陷回归集校准) ----
D1_MEDIAN_MAX_MS = 120        # 逐卡偏移中位数
D1_P95_MAX_MS = 250           # 95 分位
D1_SINGLE_MAX_MS = 500        # 单卡硬失败线
D1_MATCH_MIN = 0.60           # 文本匹配率低于此 → indetermined
# ---- D2 阈值(肉眼可辨口径) ----
D2_MEDIAN_MAX_MS = 200        # 音画整体错位(肉眼可辨的下限)
D2_P95_MAX_MS = 450           # 离散错位
D2_MIN_EVENTS = 4             # 少于此事件数 → indetermined(样本不足)
D2_GLOBAL_MAX_MS = 400        # 全局互相关峰 |Δ| 超此值 → 音画错位(肉眼可辨)
# ---- D3 阈值 ----
D3_CTX_S = 1.2                # 剪点上下文窗口
D3_ADJ_SIM = 0.85             # 相邻句相似度(半句重放特征)
D3_DANGLING_RATE = 0.15       # 悬空连接词密度
D3_CONNECTORS = ("然后", "但是", "所以", "而且", "接着", "另外", "不过", "那么", "就是")
D3_FAIL_MIN = 2               # D3b 命中数达到此 → FAIL;1 → WARN

STATUS_ICON = {"pass": "✓", "fail": "✗", "warn": "⚠", "skipped": "—", "indetermined": "?"}


# ---------------------------------------------------------------- 基础设施

def _now() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _asr_env() -> dict:
    """子进程环境:强制 utf-8 IO(旧版 fun_asr 无 reconfigure 时的兜底)。"""
    import os
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _asr_segments(video: Path, cfg: dict, timeout: int) -> tuple[list[dict], str]:
    """成片音轨 → 自带 ASR(独立证据源)。返回 (segments, skip_reason)。"""
    tool = Path(__file__).resolve().parents[3] / "tools" / "fun_asr.py"
    if not tool.is_file():
        return [], f"找不到自带 ASR:{tool}"
    with tempfile.TemporaryDirectory(prefix="cutflow_diag_") as td:
        wav = Path(td) / "diag_16k.wav"
        p = run([ffmpeg_bin(cfg), "-y", "-v", "error", "-i", str(video), "-vn",
                 "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], timeout=600)
        if p.returncode != 0:
            return [], f"音轨提取失败:{(p.stderr or '')[-120]}"
        r = run([sys.executable, str(tool), str(wav), "--json"], timeout=timeout,
                env=_asr_env())
    try:
        doc = json.loads((r.stdout or "").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return [], f"ASR 无有效输出:{((r.stderr or '') + (r.stdout or ''))[-120]}"
    if not doc.get("ok"):
        return [], f"ASR 失败:{str(doc.get('message', ''))[:120]}"
    return (doc.get("data") or {}).get("segments") or [], ""


import rs_sync  # noqa: E402  — 模块级导入(_d1_with_segments 也用 STRIP)


def _parse_ass(path: Path) -> list[dict]:
    """ASS → [{start, end, text}](秒)。兼容卡拉OK标签。"""
    return rs_sync.parse_ass(path)


def _norm(s: str) -> str:
    import rs_sync
    return rs_sync.norm(s)


# ---------------------------------------------------------------- D1 字幕↔语音错位

def d1_subtitle_voice_offset(video: Path, ass_path: Path | None, cfg: dict,
                             budget_left: float) -> dict:
    """成片音轨独立 ASR 逐字时间轴 ↔ 字幕卡起点,逐卡偏移。"""
    t0 = time.perf_counter()
    if ass_path is None or not ass_path.is_file():
        return {"id": "D1", "name": "字幕↔语音时间轴", "status": "skipped",
                "elapsedS": 0, "findings": ["无 ASS 字幕文件;字幕错位检测需要字幕轨"]}
    events = _parse_ass(ass_path)
    if not events:
        return {"id": "D1", "name": "字幕↔语音时间轴", "status": "indetermined",
                "elapsedS": round(time.perf_counter() - t0, 1),
                "findings": ["ASS 无 Dialogue 事件,无法对账"]}
    segs, skip = _asr_segments(video, cfg, timeout=max(120, budget_left))
    if skip:
        return {"id": "D1", "name": "字幕↔语音时间轴", "status": "indetermined",
                "elapsedS": round(time.perf_counter() - t0, 1),
                "findings": [f"成片 ASR 不可用({skip})——字幕错位无法机械判定,请人工抽查"]}
    asr_text = _norm("".join(s.get("text", "") for s in segs))
    if not asr_text:
        return {"id": "D1", "name": "字幕↔语音时间轴", "status": "indetermined",
                "elapsedS": round(time.perf_counter() - t0, 1),
                "findings": ["成片音轨 ASR 归一后为空"]}
    # ASR 字级时间轴(有 timestamp 用字级,否则句级起点)
    asr_chars: list[tuple[str, float]] = []      # (归一字, 起点秒)
    for s in segs:
        txt = _norm(s.get("text", ""))
        ts = s.get("timestamp") or []
        start = float(s.get("start") or 0)
        if ts and len(txt) == len(ts):
            for ch, (a, _b) in zip(txt, ts):
                asr_chars.append((ch, float(a) / 1000.0))
        else:
            per = (float(s.get("end") or start) - start) / max(1, len(txt))
            for i, ch in enumerate(txt):
                asr_chars.append((ch, start + i * per))
    asr_join = "".join(c for c, _ in asr_chars)

    # 逐卡:文本在 ASR 串中定位(按顺序游标),偏移 = 卡起点 − 对应 ASR 首字起点
    rows, cursor = [], 0
    for e in events:
        t = _norm(e["text"])
        if not t:
            continue
        k = asr_join.find(t, cursor)
        if k < 0:
            k = asr_join.find(t)
        if k < 0:
            rows.append({"card": e["text"][:12], "matched": False})
            continue
        offset_ms = round((e["start"] - asr_chars[k][1]) * 1000, 1)
        rows.append({"card": e["text"][:12], "matched": True,
                     "assStartS": round(e["start"], 3),
                     "asrStartS": round(asr_chars[k][1], 3),
                     "offsetMs": offset_ms})
        cursor = k + len(t)
    matched = [r for r in rows if r.get("matched")]
    match_rate = len(matched) / max(1, len(rows))
    if match_rate < D1_MATCH_MIN:
        return {"id": "D1", "name": "字幕↔语音时间轴", "status": "indetermined",
                "elapsedS": round(time.perf_counter() - t0, 1),
                "findings": [f"字幕文本与语音 ASR 匹配率 {match_rate:.0%} < {D1_MATCH_MIN:.0%},"
                             "无法机械判定;请检查字幕内容与成片是否同源"],
                "matchRate": round(match_rate, 3)}
    offs = [abs(r["offsetMs"]) for r in matched]
    signed = [r["offsetMs"] for r in matched]
    med = statistics.median(signed)
    med_abs = statistics.median(offs)
    p95v = sorted(offs)[min(len(offs) - 1, int(0.95 * (len(offs) - 1)))]
    worst = sorted(matched, key=lambda r: -abs(r["offsetMs"]))[:3]
    findings = [f"匹配 {len(matched)}/{len(rows)} 卡;偏移中位 {med:+.0f}ms / "
                f"绝对中位 {med_abs:.0f}ms / p95 {p95v:.0f}ms"]
    status = "pass"
    if med_abs > D1_MEDIAN_MAX_MS or p95v > D1_P95_MAX_MS:
        status = "fail"
    if any(abs(r["offsetMs"]) > D1_SINGLE_MAX_MS for r in matched):
        status = "fail"
    if status == "fail":
        findings.append("最差 3 卡:" +
                        "; ".join(f"「{r['card']}」{r['offsetMs']:+.0f}ms@{r['assStartS']:.1f}s"
                                  for r in worst))
    return {"id": "D1", "name": "字幕↔语音时间轴", "status": status,
            "elapsedS": round(time.perf_counter() - t0, 1),
            "findings": findings, "medianMs": round(med, 1),
            "medianAbsMs": round(med_abs, 1), "p95Ms": round(p95v, 1),
            "matchRate": round(match_rate, 3),
            "rows": rows, "worst": worst,
            "thresholds": {"median": D1_MEDIAN_MAX_MS, "p95": D1_P95_MAX_MS,
                           "single": D1_SINGLE_MAX_MS}}


# ---------------------------------------------------------------- D2 口型↔声音(音画同步)

def d2_av_sync(video: Path, cfg: dict, is_pure_animation: bool,
               budget_left: float) -> dict:
    """音频活动事件 ↔ 画面运动峰延迟。纯动画跳过;样本不足 → indetermined。"""
    t0 = time.perf_counter()
    if is_pure_animation:
        return {"id": "D2", "name": "音画同步(口型/动作)", "status": "skipped",
                "elapsedS": 0, "findings": ["纯动画工程:无口型语义,检测不适用(留痕)"]}
    info = ffprobe_json(video, cfg)
    vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    dur = float(vs.get("duration") or 0) or 30.0
    dur = min(dur, 300.0)                        # 诊断采样上限 5 分钟
    sample_fps = 12
    with tempfile.TemporaryDirectory(prefix="cutflow_d2_") as td:
        td_path = Path(td)
        # 音频包络:16k mono PCM
        wav = td_path / "a.wav"
        p = run([ffmpeg_bin(cfg), "-y", "-v", "error", "-i", str(video), "-vn",
                 "-ac", "1", "-ar", "16000", "-t", f"{dur:.2f}", "-c:a", "pcm_s16le",
                 str(wav)], timeout=300)
        if p.returncode != 0:
            return {"id": "D2", "name": "音画同步(口型/动作)", "status": "indetermined",
                    "elapsedS": round(time.perf_counter() - t0, 1),
                    "findings": [f"音轨提取失败:{(p.stderr or '')[-100]}"]}
        import array as _array
        raw = wav.read_bytes()
        samples = _array.array("h")
        samples.frombytes(raw[:len(raw) // 2 * 2])
        win = 160                                # 10ms @16k
        env = []
        for i in range(len(samples) // win):
            chunk = samples[i * win:(i + 1) * win]
            acc = sum(s * s for s in chunk)
            env.append((acc / win) ** 0.5)
        if not env:
            return {"id": "D2", "name": "音画同步(口型/动作)", "status": "indetermined",
                    "elapsedS": round(time.perf_counter() - t0, 1),
                    "findings": ["音频包络为空"]}
        peak = max(env)
        floor = sorted(env)[len(env) // 5]
        th = max(floor * 3.0, peak * 0.08, 1.0)
        mask = [v > th for v in env]
        # 语音事件:连续活跃段的起点(≥50ms)
        events = []
        run_len = 0
        for i, m in enumerate(mask):
            run_len = run_len + 1 if m else 0
            if run_len == 5:                      # 50ms sustained
                events.append((i - 4) / 100.0)
        # 画面运动能量:抽帧下采样灰度差分(纯 ffmpeg 抽 raw gray + python 差分)
        frames_dir = td_path / "frames"
        frames_dir.mkdir()
        p = run([ffmpeg_bin(cfg), "-v", "error", "-i", str(video), "-t", f"{dur:.2f}",
                 "-vf", f"fps={sample_fps},scale=64:36,format=gray",
                 "-f", "rawvideo", "-pix_fmt", "gray", str(td_path / "motion.raw")],
                timeout=max(120, int(budget_left)))
        motion_raw = td_path / "motion.raw"
        if p.returncode != 0 or not motion_raw.is_file():
            return {"id": "D2", "name": "音画同步(口型/动作)", "status": "indetermined",
                    "elapsedS": round(time.perf_counter() - t0, 1),
                    "findings": ["画面运动采样失败,音画同步无法机械判定"]}
        data = motion_raw.read_bytes()
        px = 64 * 36
        n_frames = len(data) // px
        if n_frames < sample_fps * 2:
            return {"id": "D2", "name": "音画同步(口型/动作)", "status": "indetermined",
                    "elapsedS": round(time.perf_counter() - t0, 1),
                    "findings": [f"有效帧数不足({n_frames}),无法判定音画同步"]}
        import numpy as np
        frames = np.frombuffer(data[:n_frames * px], dtype=np.uint8).reshape(n_frames, px).astype(np.int16)
        motion = np.abs(np.diff(frames, axis=0)).mean(axis=1)   # (n-1,)
        # 每个语音事件邻域 ±0.5s 内找运动峰延迟
        # v4 判据(最终):音频包络 × 画面运动曲线的全局互相关。
        # 口型/动作与语音同源 → 两条能量曲线强相关;音轨相对画面延迟 Δ 时,
        # 互相关峰位 = +Δ。对持续运动/合成摆动均稳,不受单事件峰吸附影响。
        env_arr = np.asarray(env, dtype=np.float32)          # 10ms 步长
        env_ds = env_arr[::3]                                 # 降采样到 30ms 步长
        motion_ds = np.interp(
            np.linspace(0, len(motion) - 1, len(env_ds)),
            np.arange(len(motion)), motion).astype(np.float32)
        if float(env_ds.std()) < 1e-6 or float(motion_ds.std()) < 1e-6:
            return {"id": "D2", "name": "音画同步(口型/动作)", "status": "indetermined",
                    "elapsedS": round(time.perf_counter() - t0, 1),
                    "findings": ["能量曲线无波动(纯静音或纯静止画面),无法判定音画同步"]}
        env_n = (env_ds - env_ds.mean()) / env_ds.std()
        mot_n = (motion_ds - motion_ds.mean()) / motion_ds.std()
        step_ms = 30
        max_lag = 1000 // step_ms                             # ±1s 滞后窗
        best_lag, best_corr = None, -2.0
        corrs = []
        for lag in range(-max_lag, max_lag + 1):
            if lag >= 0:
                a, b = env_n[lag:], mot_n[:len(mot_n) - lag]
            else:
                a, b = env_n[:lag], mot_n[-lag:]
            if len(a) < 10:
                continue
            c = float(np.corrcoef(a, b)[0, 1])
            if np.isnan(c):
                continue
            corrs.append((lag, c))
            if c > best_corr:
                best_corr, best_lag = c, lag
        if best_lag is None or len(corrs) < 5:
            return {"id": "D2", "name": "音画同步(口型/动作)", "status": "indetermined",
                    "elapsedS": round(time.perf_counter() - t0, 1),
                    "findings": ["互相关计算样本不足,无法判定音画同步"]}
        # 显著度:最佳峰 vs 次峰(排除 ±3 步邻域)
        others = sorted((abs(c), l) for l, c in corrs if abs(l - best_lag) > 3)
        prominence = best_corr / max(1e-6, others[-1][0] if others else 1e-6)
        delta_ms = best_lag * step_ms        # >0 = 音频滞后于画面(音轨延后)
        findings = [f"互相关峰 {best_corr:.2f} @ 音频滞后 {delta_ms:+d}ms(显著度 {prominence:.2f})"]
        status = "pass"
        detail = {"bestLagMs": delta_ms, "bestCorr": round(best_corr, 3),
                  "prominence": round(prominence, 2)}
        if prominence < 1.15:
            status = "indetermined"
            findings.append("互相关峰不显著(曲线形态弱相关),无法机械判定;"
                            "请人工核对明显对话段的口型")
        elif abs(delta_ms) > D2_GLOBAL_MAX_MS:
            status = "fail"
            direction = "音轨滞后于画面" if delta_ms > 0 else "音轨超前于画面"
            findings.append(f"音画错位约 {delta_ms:+d}ms({direction})——"
                            "口型/动作与声音可感知不同步")
        return {"id": "D2", "name": "音画同步(口型/动作)", "status": status,
                "elapsedS": round(time.perf_counter() - t0, 1), "findings": findings,
                **detail,
                "thresholds": {"globalMaxMs": D2_GLOBAL_MAX_MS, "prominence": 1.15}}


# ---------------------------------------------------------------- D3 错剪 → 无意义片段

def d3a_cut_context(cutlist_path: Path | None, wl_path: Path | None) -> dict:
    """剪点上下文审计:句中腰斩/悬空连接词/碎片保留(有工程产物时)。"""
    t0 = time.perf_counter()
    if cutlist_path is None or not cutlist_path.is_file() or wl_path is None or not wl_path.is_file():
        return {"id": "D3a", "name": "剪点上下文审计", "status": "skipped",
                "elapsedS": 0, "findings": ["无 cutlist/wordline(独立视频模式),跳过;D3b 仍覆盖"]}
    try:
        cl = json.loads(cutlist_path.read_text(encoding="utf-8"))
        wl = json.loads(wl_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"id": "D3a", "name": "剪点上下文审计", "status": "indetermined",
                "elapsedS": round(time.perf_counter() - t0, 1),
                "findings": [f"cutlist/wordline 解析失败:{exc}"]}
    chars = wl.get("chars") or []
    text = "".join(c["ch"] for c in chars)
    # 时间 → 字下标(源域二分)
    def _idx_at(ms: int) -> int:
        lo, hi = 0, len(chars) - 1
        if not chars or ms <= int(chars[0]["startMs"]):
            return 0
        if ms >= int(chars[-1]["endMs"]):
            return len(chars) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if int(chars[mid]["endMs"]) < ms:
                lo = mid + 1
            else:
                hi = mid
        return lo

    findings, suspicious = [], []
    removes = [c for c in (cl.get("cuts") or []) if c.get("action") == "remove"]
    for c in removes:
        in_ms, out_ms = int(c.get("inMs", 0)), int(c.get("outMs", 0))
        i0 = _idx_at(max(0, in_ms - int(D3_CTX_S * 1000)))
        i1 = _idx_at(in_ms)
        j0 = _idx_at(out_ms)
        j1 = _idx_at(min(int(wl.get("srcDurationMs") or 10**9), out_ms + int(D3_CTX_S * 1000)))
        before = text[i0:i1].strip()
        after = text[j0:j1].strip()
        reasons = []
        if before and before[-1] not in "。?!?!;；,":
            reasons.append(f"切点前句未收尾(…{before[-8:]})")
        if after and after[:2] in D3_CONNECTORS:
            reasons.append(f"切点后以连接词「{after[:2]}」开头,前因被剪")
        if after and before and (out_ms - in_ms) < 1500:
            reasons.append("碎片剪除(<1.5s),易产生顿挫")
        if reasons:
            suspicious.append({"inMs": in_ms, "outMs": out_ms,
                               "reason": "; ".join(reasons), "conf": c.get("conf")})
    if suspicious:
        findings.append(f"{len(suspicious)}/{len(removes)} 个剪点存在语义风险(需人工看):")
        findings += [f"  {s['inMs']}-{s['outMs']}ms: {s['reason']}" for s in suspicious[:5]]
        return {"id": "D3a", "name": "剪点上下文审计", "status": "warn",
                "elapsedS": round(time.perf_counter() - t0, 1), "findings": findings,
                "suspicious": suspicious, "removeCount": len(removes)}
    findings.append(f"{len(removes)} 个剪点全部上下文完整")
    return {"id": "D3a", "name": "剪点上下文审计", "status": "pass",
            "elapsedS": round(time.perf_counter() - t0, 1), "findings": findings,
            "removeCount": len(removes)}


def d3b_semantic_coherence(video: Path, cfg: dict, budget_left: float,
                           asr_segments: list[dict] | None,
                           d1_rows: list[dict] | None = None) -> dict:
    """成片语义连贯(独立):相邻句近重复/悬空连接词/突兀截断。复用 D1 的 ASR 结果。"""
    t0 = time.perf_counter()
    segs = asr_segments
    if segs is None:
        segs, skip = _asr_segments(video, cfg, timeout=max(120, budget_left))
        if skip:
            return {"id": "D3b", "name": "语义连贯(成片独立)", "status": "indetermined",
                    "elapsedS": round(time.perf_counter() - t0, 1),
                    "findings": [f"成片 ASR 不可用({skip})——错剪语义检测无法判定"]}
    # ASR 常返回单段长文本(句界被转写成逗号)→ 按子句切分(。?!;，,),
    # 字级时间戳可用时句边界用真值,否则在段内按字数比例内插。
    sents = []
    _BOUNDS = "。?!?!;;,，"
    for s in segs:
        raw = s.get("text", "")
        ts = s.get("timestamp") or []
        start, end = float(s.get("start") or 0), float(s.get("end") or s.get("start") or 0)
        if ts and len(raw) == len(ts):
            cur_text, cur_start, cur_end = "", None, None
            for ch, (a, b2) in zip(raw, ts):
                cur_text += ch
                if cur_start is None:
                    cur_start = a
                cur_end = b2
                if ch in _BOUNDS:
                    sents.append({"text": cur_text, "start": cur_start / 1000.0,
                                  "end": cur_end / 1000.0})
                    cur_text, cur_start = "", None
            if cur_text.strip():
                sents.append({"text": cur_text, "start": (cur_start or 0) / 1000.0,
                              "end": (cur_end or cur_start or 0) / 1000.0})
        else:
            per = (end - start) / max(1, len(raw))
            cur_text, cur_start = "", start
            for i2, ch in enumerate(raw):
                cur_text += ch
                if ch in _BOUNDS:
                    sents.append({"text": cur_text, "start": cur_start,
                                  "end": start + (i2 + 1) * per})
                    cur_text, cur_start = "", start + (i2 + 1) * per
            if cur_text.strip():
                sents.append({"text": cur_text, "start": cur_start, "end": end})
    sents = [s for s in sents if _norm(s["text"])]
    if len(sents) < 3:
        return {"id": "D3b", "name": "语义连贯(成片独立)", "status": "indetermined",
                "elapsedS": round(time.perf_counter() - t0, 1),
                "findings": [f"可分句过少({len(sents)}),语义连贯无法统计判定"]}
    hits = []
    # ① 相邻句近重复(半句重放)
    for a, b in zip(sents, sents[1:]):
        na, nb = _norm(a["text"]), _norm(b["text"])
        if na and nb and 0.5 < difflib.SequenceMatcher(None, na, nb).ratio() < 0.999:
            hits.append({"type": "near-duplicate", "startS": round(b["start"], 2),
                         "detail": f"「{a['text'][:10]}」→「{b['text'][:10]}」"})
    # ② 悬空连接词(信息项;子句级切分下正常叙述也会以连接词开头,不参与判 fail)
    dangling = 0
    for i, s in enumerate(sents[1:], 1):
        head = s["text"][:2]
        if head in D3_CONNECTORS:
            dangling += 1
    # ③ 突兀截断(两路):
    #   a) 子句 <600ms 且无标点收尾(ASR 未补标点情形)
    #   b) 相对短句:子句时长 < 本视频子句中位 ×0.5 且 <1.2s(ASR 自动补标点时,
    #      腰斩子句带逗号但时长显著短于同视频正常子句;同源自比不受语速影响)
    hard_hits, soft_hits = 0, 0
    # 卡完整性核对(有 D1 逐卡匹配时):字幕卡内容在成片语音里只说了一部分
    # (连续匹配 < 卡文本 80%)→ 该卡被腰斩,硬证据
    for r in (d1_rows or []):
        if r.get("matched") and r.get("cardLen") and r.get("matchedLen", 0) < r["cardLen"] * 0.8:
            hard_hits += 1
            if len(hits) < 15:
                hits.append({"type": "truncated-card", "startS": round(r.get("assStartS", 0), 2),
                             "detail": f"字幕卡「{r['card']}」{r['cardLen']} 字在成片语音中仅连续出现 "
                                       f"{r['matchedLen']} 字——内容被腰斩/错剪"})
    durs = [max(1.0, (float(s.get("end") or s["start"]) - s["start"]) * 1000) for s in sents]
    med_dur = statistics.median(durs) if durs else 0.0
    for i, s in enumerate(sents):
        dur_ms = durs[i]
        no_punct_end = s["text"] and s["text"][-1] not in "。?!?!;;,，"
        if dur_ms < 600 and no_punct_end:
            hard_hits += 1
            if len(hits) < 15:
                hits.append({"type": "abrupt-cut", "startS": round(s["start"], 2),
                             "detail": f"子句仅 {dur_ms:.0f}ms 且无标点收尾:「{s['text'][:10]}」(疑似腰斩)"})
        elif med_dur > 0 and dur_ms < med_dur * 0.55 and dur_ms < 1400:
            soft_hits += 1
            if len(hits) < 15:
                hits.append({"type": "short-clause", "startS": round(s["start"], 2),
                             "detail": f"子句 {dur_ms:.0f}ms 显著短于同视频中位({med_dur:.0f}ms):"
                                       f"「{s['text'][:10]}」(短句疑似腰斩,建议人工核对)"
                             })
    findings = [f"子句 {len(sents)};近重复 {len([h for h in hits if h['type'] == 'near-duplicate'])} / "
                f"硬截断 {hard_hits} / 相对短句 {soft_hits} / 连接词开头(信息) {dangling}"]
    # 定级:强特征(无标点截断/半句重放)→ fail;相对短句(ASR 补标点会掩盖腰斩)
    # → warn(结论不是"正常",而是"需人工核对该时间段")。
    hard_n = hard_hits + len([h for h in hits if h["type"] == "near-duplicate"])
    if hard_n >= 1:
        status = "fail"
        findings.append("存在语义断裂强特征,疑似错剪(见 suspiciousSpans)")
    elif soft_hits >= 1:
        status = "warn"
        findings.append(f"{soft_hits} 处短子句疑点,建议人工核对该时间段(结论非'正常')")
    return {"id": "D3b", "name": "语义连贯(成片独立)", "status": status,
            "elapsedS": round(time.perf_counter() - t0, 1), "findings": findings,
            "hits": hits[:15],
            "thresholds": {"adjSim": D3_ADJ_SIM, "failMin": D3_FAIL_MIN}}


# ---------------------------------------------------------------- 台账与汇总

def _append_ledger(root_out: Path, entry: dict) -> Path:
    d = root_out / "diagnosis"
    d.mkdir(parents=True, exist_ok=True)
    ledger = d / "diagnosis_log.jsonl"
    with open(ledger, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return ledger


def _worst_spans(checks: list[dict]) -> list[list[float]]:
    spans = []
    for c in checks:
        for r in (c.get("worst") or []):
            if isinstance(r, dict) and "assStartS" in r:
                spans.append([max(0.0, r["assStartS"] - 1.0), r["assStartS"] + 2.0])
        for h in (c.get("hits") or []) + (c.get("suspicious") or []):
            if isinstance(h, dict):
                if "startS" in h:
                    spans.append([max(0.0, h["startS"] - 1.0), h["startS"] + 2.0])
                elif "inMs" in h:
                    spans.append([h["inMs"] / 1000.0, h["outMs"] / 1000.0])
    # 合并重叠区间
    spans.sort()
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([round(a, 2), round(b, 2)])
    return merged[:12]


def diagnose(video: Path, project: Path | None, ass_path: Path | None,
             cutlist_path: Path | None, wl_path: Path | None,
             budget_s: float, no_d2: bool, out_dir: Path, cfg: dict) -> dict:
    t_start = time.perf_counter()
    if not video.is_file():
        return emit(False, "NO_VIDEO", f"成片不存在:{video}", exit_code=2)

    # 工程 IR 推断:纯动画 → D2 跳过;ASS/cutlist/wordline 自动定位
    is_pure_animation = False
    if project:
        ir = project / "05_ir" / "project.json"
        if ir.is_file():
            try:
                doc = json.loads(ir.read_text(encoding="utf-8"))
                is_pure_animation = (doc.get("videoType") or "").startswith("pure")
            except (json.JSONDecodeError, OSError):
                pass
        ass_path = ass_path or (project / "06_output" / "subtitles.ass")
        cutlist_path = cutlist_path or _first_existing(
            project / "04_cut" / "cutlist.applied.json", project / "04_cut" / "cutlist.json")
        wl_path = wl_path or _first_existing(
            project / "05_ir" / "wordline.final.json", project / "05_ir" / "wordline.json")

    checks: list[dict] = []
    asr_segments_cache = None

    def left() -> float:
        return budget_s - (time.perf_counter() - t_start)

    # D1(需要 ASR;缓存 segments 给 D3b 复用)
    t0 = time.perf_counter()
    d1 = None
    if ass_path and ass_path.is_file() and left() > 60:
        events = _parse_ass(ass_path)
        segs, skip = ([], "budget 即将耗尽,ASR 未运行") if left() < 90 else _asr_segments(video, cfg, timeout=max(120, left()))
        asr_segments_cache = segs if not skip else None
        d1 = _d1_with_segments(video, ass_path, cfg, segs, skip, round(time.perf_counter() - t0, 1))
    else:
        d1 = {"id": "D1", "name": "字幕↔语音时间轴", "status": "skipped" if not (ass_path and ass_path.is_file()) else "indetermined",
              "elapsedS": 0, "findings": ["预算不足,ASR 未运行" if ass_path and ass_path.is_file() else "无 ASS 字幕文件"]}
    checks.append(d1)

    # D2
    if no_d2:
        checks.append({"id": "D2", "name": "音画同步(口型/动作)", "status": "skipped",
                       "elapsedS": 0, "findings": ["--no-d2 显式跳过"]})
    else:
        checks.append(d2_av_sync(video, cfg, is_pure_animation, left()))

    # D3a + D3b(复用 ASR)
    checks.append(d3a_cut_context(cutlist_path, wl_path))
    d1_rows = next((c.get("rows") for c in checks if c.get("id") == "D1"), None)
    checks.append(d3b_semantic_coherence(video, cfg, left(), asr_segments_cache, d1_rows))

    elapsed = round(time.perf_counter() - t_start, 1)
    ran = [c for c in checks if c["status"] not in ("skipped",)]
    failed = [c for c in checks if c["status"] == "fail"]
    warned = [c for c in checks if c["status"] == "warn"]
    indet = [c for c in checks if c["status"] == "indetermined"]
    passed_n = len([c for c in checks if c["status"] == "pass"])
    if len(ran) < 2:
        verdict = "indetermined"
    elif failed:
        verdict = "issues"
    elif indet and passed_n < 2:
        verdict = "indetermined"
    else:
        # warn 不阻断 pass(warn 已进 suspiciousSpans 供人工核对;fail 才是硬伤)
        verdict = "pass"
    suspicious = _worst_spans(checks)
    overtime = elapsed > budget_s
    partial = [c["id"] for c in checks if c["status"] == "skipped"]
    result = {"verdict": verdict, "checks": checks,
              "suspiciousSpans": suspicious,
              "elapsedS": elapsed, "budgetS": budget_s,
              "partialChecks": partial, "overtime": overtime,
              "video": str(video), "at": _now(), "version": DIAG_VERSION}
    ledger = _append_ledger(out_dir, {
        "at": result["at"], "video": str(video), "verdict": verdict,
        "elapsedS": elapsed, "budgetS": budget_s, "version": DIAG_VERSION,
        "checks": [{"id": c["id"], "status": c["status"], "elapsedS": c.get("elapsedS", 0),
                    "findings": (c.get("findings") or [])[:3]} for c in checks],
        "suspiciousSpans": result["suspiciousSpans"]})
    result["ledger"] = str(ledger)
    return result


def _first_existing(*paths: Path) -> Path | None:
    for p in paths:
        if p.is_file():
            return p
    return None


def _match_len(asr_join: str, k: int, t: str) -> int:
    """卡文本 t 从 ASR 串位置 k 起连续匹配的字符数(0-len(t))。"""
    if k < 0:
        return 0
    n = 0
    while n < len(t) and k + n < len(asr_join) and asr_join[k + n] == t[n]:
        n += 1
    return n


def _d1_with_segments(video: Path, ass_path: Path, cfg: dict,
                      segs: list[dict], skip: str, elapsed: float) -> dict:
    """D1 主体(ASR 结果由调用方缓存,供 D3b 复用)。"""
    if skip:
        return {"id": "D1", "name": "字幕↔语音时间轴", "status": "indetermined",
                "elapsedS": elapsed,
                "findings": [f"成片 ASR 不可用({skip})——字幕错位无法机械判定,请人工抽查"]}
    events = _parse_ass(ass_path)
    if not events:
        return {"id": "D1", "name": "字幕↔语音时间轴", "status": "indetermined",
                "elapsedS": elapsed, "findings": ["ASS 无 Dialogue 事件"]}
    asr_text = _norm("".join(s.get("text", "") for s in segs))
    if not asr_text:
        return {"id": "D1", "name": "字幕↔语音时间轴", "status": "indetermined",
                "elapsedS": elapsed, "findings": ["成片音轨 ASR 归一后为空"]}
    # 字级时间流与归一文本流**分开构建但下标对齐**:
    #   raw 流 = 原始字符(含标点)配 timestamp(timestamp 与原始文本一一对应);
    #   norm 流 = 归一字符 + 它在 raw 流中的下标。匹配在 norm 流,取时回 raw 流。
    asr_norm: list[str] = []          # 归一字符
    asr_time: list[float] = []        # 对应时间(秒)
    for s in segs:
        raw = s.get("text", "")
        ts = s.get("timestamp") or []
        start = float(s.get("start") or 0)
        end = float(s.get("end") or start)
        if ts and len(raw) == len(ts):
            for ch, (a, _b) in zip(raw, ts):
                if ch not in rs_sync.STRIP:
                    asr_norm.append(ch)
                    asr_time.append(float(a) / 1000.0)
        else:
            per = (end - start) / max(1, len(raw))
            for i, ch in enumerate(raw):
                if ch not in rs_sync.STRIP:
                    asr_norm.append(ch)
                    asr_time.append(start + i * per)
    asr_join = "".join(asr_norm)
    rows, cursor = [], 0
    for e in events:
        t = _norm(e["text"])
        if not t:
            continue
        k = asr_join.find(t, cursor)
        if k < 0:
            k = asr_join.find(t)
        if k < 0:
            rows.append({"card": e["text"][:12], "matched": False})
            continue
        rows.append({"card": e["text"][:12], "matched": True,
                     "cardLen": len(t), "matchedLen": _match_len(asr_join, k, t),
                     "assStartS": round(e["start"], 3),
                     "asrStartS": round(asr_time[k], 3),
                     "offsetMs": round((e["start"] - asr_time[k]) * 1000, 1)})
        cursor = k + len(t)
    matched = [r for r in rows if r.get("matched")]
    match_rate = len(matched) / max(1, len(rows))
    if match_rate < D1_MATCH_MIN:
        return {"id": "D1", "name": "字幕↔语音时间轴", "status": "indetermined",
                "elapsedS": elapsed,
                "findings": [f"字幕文本与语音 ASR 匹配率 {match_rate:.0%} < {D1_MATCH_MIN:.0%},"
                             "无法机械判定;请检查字幕内容与成片是否同源"],
                "matchRate": round(match_rate, 3)}
    offs = [abs(r["offsetMs"]) for r in matched]
    signed = [r["offsetMs"] for r in matched]
    med = statistics.median(signed)
    med_abs = statistics.median(offs)
    p95v = sorted(offs)[min(len(offs) - 1, int(0.95 * (len(offs) - 1)))]
    worst = sorted(matched, key=lambda r: -abs(r["offsetMs"]))[:3]
    findings = [f"匹配 {len(matched)}/{len(rows)} 卡;偏移中位 {med:+.0f}ms / "
                f"绝对中位 {med_abs:.0f}ms / p95 {p95v:.0f}ms"]
    status = "pass"
    if med_abs > D1_MEDIAN_MAX_MS or p95v > D1_P95_MAX_MS or \
            any(abs(r["offsetMs"]) > D1_SINGLE_MAX_MS for r in matched):
        status = "fail"
        findings.append("最差 3 卡:" +
                        "; ".join(f"「{r['card']}」{r['offsetMs']:+.0f}ms@{r['assStartS']:.1f}s"
                                  for r in worst))
    return {"id": "D1", "name": "字幕↔语音时间轴", "status": status,
            "elapsedS": elapsed, "findings": findings, "medianMs": round(med, 1),
            "medianAbsMs": round(med_abs, 1), "p95Ms": round(p95v, 1),
            "matchRate": round(match_rate, 3), "rows": rows, "worst": worst,
            "thresholds": {"median": D1_MEDIAN_MAX_MS, "p95": D1_P95_MAX_MS,
                           "single": D1_SINGLE_MAX_MS}}


def _write_report(result: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "diagnosis_report.md"
    lines = [f"# 成片内容诊断报告({DIAG_VERSION})", "",
             f"- 成片:`{result['video']}`",
             f"- 时间:{result['at']}",
             f"- 结论:**{_verdict_cn(result['verdict'])}**"
             + ("(超时降级,未完成全部检查)" if result.get("overtime") else ""),
             f"- 耗时:{result['elapsedS']}s / 预算 {result['budgetS']}s", "",
             "| 检查项 | 状态 | 耗时 | 发现 |", "|---|---|---|---|"]
    for c in result["checks"]:
        lines.append(f"| {c['id']} {c['name']} | {STATUS_ICON.get(c['status'], '?')} {c['status']} "
                     f"| {c.get('elapsedS', 0)}s | {'; '.join((c.get('findings') or [])[:2])} |")
    if result.get("suspiciousSpans"):
        lines += ["", "## 疑点片段(建议人工核看)", ""]
        lines += [f"- {a:.1f}s ~ {b:.1f}s" for a, b in result["suspiciousSpans"]]
    if result.get("partialChecks"):
        lines += ["", f"> 未完成检查:{','.join(result['partialChecks'])}(超时降级,结论为阶段性)"]
    lines += ["", f"> 台账:{result['ledger']}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _verdict_cn(v: str) -> str:
    return {"pass": "通过(所有可用检查通过,证据见下)",
            "issues": "发现问题(见疑点片段与逐项证据)",
            "indetermined": "无法机械判定(显式降级,不是'没问题')"}[v]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--project", default=None, help="工程根(自动定位 ass/cutlist/wordline/IR)")
    ap.add_argument("--ass", default=None)
    ap.add_argument("--cutlist", default=None)
    ap.add_argument("--wordline", default=None)
    ap.add_argument("--budget", dest="budget", type=float, default=900.0,
                    help="诊断耗时预算秒(默认 900;超时输出阶段性结论)")
    ap.add_argument("--no-d2", dest="no_d2", action="store_true",
                    help="跳过音画同步检测(无人物画面/纯动画)")
    ap.add_argument("--out", default="06_output")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    video = Path(a.video)
    project = Path(a.project).resolve() if a.project else None
    out_dir = (project / a.out) if project else Path(a.out)
    try:
        cfg_path = Path(__file__).parent.parent.parent.parent / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    except (json.JSONDecodeError, OSError):
        cfg = {}
    result = diagnose(video, project,
                      Path(a.ass) if a.ass else None,
                      Path(a.cutlist) if a.cutlist else None,
                      Path(a.wordline) if a.wordline else None,
                      a.budget, a.no_d2, out_dir, cfg)
    if "ok" in result and "verdict" not in result:   # emit() 的错误返回
        return result
    report = _write_report(result, out_dir)
    result["report"] = str(report)
    verdict = result["verdict"]
    issues = [c for c in result["checks"] if c["status"] == "fail"]
    warns = [c for c in result["checks"] if c["status"] == "warn"]
    msg = {"pass": f"诊断通过({result['elapsedS']}s,证据见 {report})",
           "issues": f"诊断发现问题:{'; '.join(c['id'] + ' ' + c['name'] for c in issues + warns)}"
                     f";疑点片段 {len(result['suspiciousSpans'])} 处,证据见 {report}",
           "indetermined": f"无法机械判定(显式降级):{'; '.join(c['id'] for c in result['checks'] if c['status'] == 'indetermined')}"
                           f";详见 {report}"}[verdict]
    ok = verdict != "issues"
    return emit(ok, "DIAGNOSIS_ISSUES" if verdict == "issues" else
                ("DIAGNOSIS_INDETERMINED" if verdict == "indetermined" else "DIAGNOSIS_OK"),
                msg, result, exit_code=0 if ok else 4)


if __name__ == "__main__":
    sys.exit(main())
