"""节拍检测(M8 混剪能力 audio.beat / audio.downbeat / audio.stem 的 detector,ADR-0047)。

用法:
  rs_beat.py detect <音频|视频> --out <工程根> [--downbeat] [--stems] [--force]
  rs_beat.py                    # 无参 = 工程模式(ADR-0047 挂载器契约:cwd=工程根,
                                #   自动选 01_原始素材里第一条带音轨的素材 → 写 beats.json)

双档三态(懒加载体系 rs_fetchable,ADR-0049;本脚本只探测、绝不下载):
  READY    beatnet/madmom/demucs probe 通过 → 子进程调 venv-dsp 推理(engine=beatnet 等)
  MISSING  降级档 onset-energy:ffmpeg 解低采样率 PCM → RMS 能量包络 → 半波整流差分
           取起音候选 → 自相关估 BPM → 相位搜索铺拍点网格(纯 Python,有 numpy 用 numpy)
  FAILED   同 MISSING,降级留痕多带 fetchLog

产物 04_粗剪决策/beats.json(消费者 rs_edit beat.snap):
  {"bpm", "meter", "beats", "downbeats", "confidence", "engine", "degraded", ...,
   "tiers": {"beats"/"downbeat"/"stems": 各档三态留痕}}
  ★ beats/downbeats 单位【毫秒】—— rs_edit beat.snap 用它与 clip.startMs 直接比较
    (方案 §5.5.1 示例是秒;本实现统一 ms,防两套单位混写;unit 字段显式声明)。

性能预算(方案 §6.2):降级档 3 分钟音频 ≤0.02× 实时(≈3.6s)。
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import sys
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import (EXIT_INPUT, EXIT_OK, emit, ffmpeg_bin, load_config,  # noqa: E402
                       media_duration_s, write_text_atomic)
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量
import rs_fetchable  # noqa: E402  — 三态探测(只 state(),绝不触发下载)

# ---------------------------------------------------------------- 参数常量(描述符 params 同源,改一处必改两处)

ONSET_WINDOW_MS = 20        # RMS 能量窗长(50Hz 包络率,起音检测常规粒度)
ONSET_MIN_GAP_MS = 200      # 相邻起音最小间隔(防同一次敲击抖动出双候选)
ONSET_THRESHOLD_K = 1.4     # 起音自适应阈值 = 起音强度中位数 × K(经验值)
PCM_RATE = 8000             # 降级档解码采样率(能量包络足够,无需全带宽)
DEFAULT_METER = 4           # 降级档小节拍数假设(4/4 最常见;仅 READY 档实测)
BPM_MIN, BPM_MAX = 60, 180  # BPM 自相关搜索范围(流行/电子素材常规域)
PHASE_TOL = 0.15            # 相位搜索容差(拍点周期的 ±15%,经验值)
BEATS_UNIT = "ms"           # beats/downbeats 时间单位(见模块 docstring 单位纪律)

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts", ".flv"}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS


def beats_path(project: Path) -> Path:
    """04_粗剪决策/beats.json(rs_edit beat.snap 的读取口,单一落点)。"""
    return rs_paths.resolve(project, "cut") / "beats.json"


# ---------------------------------------------------------------- 降级档 onset-energy

def decode_pcm(media: Path, cfg: dict) -> array | None:
    """ffmpeg 解单声道 s16le PCM 到内存(PIPE;失败返回 None 由调用方留痕)。

    刻意不走 rs_common.run —— 它强制 text=True,二进制 PCM 会被按 UTF-8 解码损坏;
    这里必须拿原始 bytes。
    """
    cmd = [ffmpeg_bin(cfg), "-v", "error", "-i", str(media), "-vn",
           "-ac", "1", "-ar", str(PCM_RATE), "-f", "s16le", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0 or not p.stdout:
        return None
    buf = array("h")
    buf.frombytes(p.stdout[:len(p.stdout) // 2 * 2])   # 凑齐到 2 字节对齐
    return buf


def rms_envelope(samples: array) -> list[float]:
    """按 ONSET_WINDOW_MS 窗长算 RMS 能量包络(有 numpy 用 numpy,无则纯 array)。"""
    win = max(1, PCM_RATE * ONSET_WINDOW_MS // 1000)
    n = len(samples) // win
    if n == 0:
        return []
    try:
        import numpy as np  # noqa: PLC0415 — 可用则提速,不可用走纯 Python 回退
        arr = np.frombuffer(samples, dtype=np.int16, count=n * win).astype(np.float32)
        arr = arr.reshape(n, win)
        env = np.sqrt((arr ** 2).mean(axis=1))
        return env.tolist()
    except ImportError:
        out = []
        for i in range(n):
            seg = samples[i * win:(i + 1) * win]
            acc = 0
            for v in seg:
                acc += v * v
            out.append(math.sqrt(acc / win))
        return out


def onset_strength(env: list[float]) -> list[float]:
    """半波整流差分:能量上涨量才是起音(经典 onset strength,经验实现)。"""
    return [0.0] + [max(0.0, env[i] - env[i - 1]) for i in range(1, len(env))]


def pick_onsets(strength: list[float]) -> list[int]:
    """起音候选:超过自适应阈值的局部极大值(单位 = 包络帧,×ONSET_WINDOW_MS 得 ms)。

    阈值 = min(中位数×K, 峰值×0.5):纯周期点击轨的起音强度几乎全等,中位数≈峰值,
    只用中位数×K 会把阈值抬到峰值之上而颗粒无收(实测 clicks.wav 踩过),故加峰值上限。
    """
    pos = [s for s in strength if s > 0]
    if not pos:
        return []
    thr = min(statistics.median(pos) * ONSET_THRESHOLD_K, max(pos) * 0.5)
    gap_frames = max(1, ONSET_MIN_GAP_MS // ONSET_WINDOW_MS)
    picked: list[int] = []
    for i, s in enumerate(strength):
        if s < thr:
            continue
        prev_s = strength[i - 1] if i else 0.0
        next_s = strength[i + 1] if i + 1 < len(strength) else 0.0
        if s < prev_s or s < next_s:            # 非局部极大 → 跳过(等式并肩取先者)
            continue
        if picked and i - picked[-1] < gap_frames:
            if s > strength[picked[-1]]:        # 间隔内更强的候选替换旧者
                picked[-1] = i
            continue
        picked.append(i)
    return picked


def estimate_bpm(strength: list[float]) -> float:
    """起音强度自相关估 BPM:对 60–180 BPM 对应的滞后逐个打分,取峰值(取 0.1 精度)。"""
    env_rate = 1000.0 / ONSET_WINDOW_MS
    lag_lo = max(2, int(env_rate * 60.0 / BPM_MAX))
    lag_hi = min(len(strength) - 1, int(env_rate * 60.0 / BPM_MIN))
    if lag_hi < lag_lo or len(strength) < lag_hi * 2:
        return 0.0
    mean = sum(strength) / len(strength)
    dev = [s - mean for s in strength]
    best_lag, best_score = 0, -1.0
    for lag in range(lag_lo, lag_hi + 1):
        score = sum(dev[i] * dev[i + lag] for i in range(len(dev) - lag))
        if score > best_score:
            best_lag, best_score = lag, score
    if best_lag == 0 or best_score <= 0:
        return 0.0
    bpm = 60.0 * env_rate / best_lag
    while bpm < BPM_MIN:                        # 倍频矫正:自相关对整倍周期不敏感
        bpm *= 2
    while bpm > BPM_MAX:
        bpm /= 2
    return round(bpm, 1)


def grid_from_onsets(onsets_ms: list[int], bpm: float, total_ms: float
                     ) -> tuple[list[int], float]:
    """相位搜索:以若干起音为候选相位铺等间隔网格,取「命中起音最多」的相位。

    返回 (拍点 ms 序列, confidence)。confidence = 被网格覆盖的起音占比
    (能量法的粗信度:0=完全没对上,1=全部起音都落在拍点上)。
    """
    if bpm <= 0 or not onsets_ms:
        return [], 0.0
    period = 60000.0 / bpm
    tol = period * PHASE_TOL
    candidates = onsets_ms[:16]
    best_beats, best_hits = [], -1
    for phase in candidates:
        grid, t = [], phase % period
        while t <= total_ms:
            grid.append(int(t))
            t += period
        if not grid:
            continue
        hits = 0
        j = 0
        for o in onsets_ms:
            while j < len(grid) and grid[j] < o - tol:
                j += 1
            if j < len(grid) and abs(grid[j] - o) <= tol:
                hits += 1
        if hits > best_hits:
            best_beats, best_hits = grid, hits
    conf = round(best_hits / len(onsets_ms), 2) if onsets_ms else 0.0
    return best_beats, max(conf, 0.0)


def detect_onset_energy(media: Path, cfg: dict) -> tuple[dict, float]:
    """降级档主路径:PCM → 包络 → 起音 → BPM → 网格。返回 (拍点结果, 时长秒)。"""
    total_s = 0.0
    try:
        total_s = media_duration_s(media, cfg)
    except SystemExit:                          # ffprobe die() → 按无时长继续
        total_s = 0.0
    samples = decode_pcm(media, cfg)
    if not samples:
        return {"bpm": 0.0, "beats": [], "confidence": 0.0}, total_s
    strength = onset_strength(rms_envelope(samples))
    onsets = [i * ONSET_WINDOW_MS for i in pick_onsets(strength)]
    bpm = estimate_bpm(strength)
    total_ms = max(total_s * 1000.0, onsets[-1] + 1 if onsets else 0)
    beats, conf = grid_from_onsets(onsets, bpm, total_ms)
    return {"bpm": bpm, "beats": beats, "confidence": conf}, total_s


# ---------------------------------------------------------------- READY 档(venv-dsp 推理;测试 mock 本函数)

def _venv_infer(py: Path, media: Path, out_json: Path, mode: str,
                timeout: int = 900) -> subprocess.CompletedProcess:
    """子进程调 venv-dsp 跑 BeatNet/madmom/Demucs 推理,结果落 out_json。

    真实推理脚本随 venv 部署时由 tools/deps/beatnet/ 提供;此处只负责「探测通过后
    按约定调用」。测试 mock 本函数(绝不真实下载模型)。
    """
    code = (
        "import json,sys;"
        f"from cutflow_beatnet_driver import infer;"   # venv 内驱动约定(tools/deps 部署)
        f"infer({str(media)!r},{str(out_json)!r},{mode!r})"
    )
    env = {**__import__("os").environ, "PYTHONPATH": str(rs_fetchable.tools_dir())}
    return subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def ready_tier(media: Path, out_root: Path, want_downbeat: bool, want_stems: bool
               ) -> tuple[dict | None, dict]:
    """READY 档:venv-dsp 推理 beat(+按需 downbeat/stems)。

    返回 (推理结果 dict 或 None, tiers 留痕块)。probe 不过 → (None, 降级 tiers)。
    """
    tiers: dict = {}
    st_beat = rs_fetchable.state("audio.beat")
    tiers["beats"] = ({"engine": "beatnet", "degraded": False}
                      if st_beat["state"] == "READY" else
                      {"engine": "onset-energy", "degraded": True,
                       **{k: v for k, v in rs_fetchable.degrade_record("audio.beat").items()
                          if k != "degraded"}})
    if st_beat["state"] != "READY":
        return None, tiers
    st_down = rs_fetchable.state("audio.downbeat")
    tiers["downbeat"] = ({"engine": "madmom", "degraded": False}
                         if st_down["state"] == "READY" else
                         {"engine": "beat-only", "degraded": True,
                          **{k: v for k, v in rs_fetchable.degrade_record("audio.downbeat").items()
                             if k != "degraded"}})
    if want_stems:
        st_stem = rs_fetchable.state("audio.stem")
        tiers["stems"] = ({"available": True, "engine": "demucs", "degraded": False}
                          if st_stem["state"] == "READY" else
                          {"available": False, "engine": "mixed-audio", "degraded": True,
                           "note": "直接对混音检测(缺人声/伴奏分离组件)",
                           **{k: v for k, v in rs_fetchable.degrade_record("audio.stem").items()
                              if k != "degraded"}})
    py = rs_fetchable.venv_python("venv-dsp")
    if py is None:
        # probe 说 READY 但 venv 缺失(状态漂移)→ 如实按降级处置,tiers 不许残留 READY 谎报
        return None, {"beats": {"engine": "onset-energy", "degraded": True,
                                **{k: v for k, v in rs_fetchable.degrade_record("audio.beat").items()
                                   if k != "degraded"},
                                "note": "venv-dsp 缺失,状态漂移;已按降级处置"}}
    tmp_json = beats_path(out_root).with_suffix(".infer.json")
    mode = "+".join(x for x, w in (("downbeat", want_downbeat), ("stems", want_stems)) if w)
    try:
        p = _venv_infer(py, media, tmp_json, mode or "beat")
    except (OSError, subprocess.TimeoutExpired):
        return None, tiers
    if p.returncode != 0 or not tmp_json.is_file():
        return None, tiers
    try:
        doc = json.loads(tmp_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None, tiers
    return doc, tiers


# ---------------------------------------------------------------- 组装与产物

def analyze(media: Path, out_root: Path, *, want_downbeat: bool = False,
            want_stems: bool = False, force: bool = False) -> tuple[dict, int]:
    """单素材全流程 → beats.json。返回 (产物 dict, 退出码)。"""
    out_path = beats_path(out_root)
    cfg = load_config()
    doc, tiers = ready_tier(media, out_root, want_downbeat, want_stems)
    if doc is not None:                          # READY 档
        doc.setdefault("meter", DEFAULT_METER)
        doc.setdefault("downbeats", [])
        doc.setdefault("confidence", 0.0)
        doc["engine"] = "beatnet"
        doc["unit"] = BEATS_UNIT
        doc["degraded"] = False
        doc["source"] = media.name
        doc["tiers"] = tiers
        # 驱动约定(tools/deps/beatnet):推理结果与产物同为毫秒整数,此处只归一排序
        doc["beats"] = sorted(int(round(float(b))) for b in doc.get("beats", []))
        doc["downbeats"] = sorted(int(round(float(b))) for b in doc.get("downbeats", []))
        _write(out_path, doc)
        return doc, EXIT_OK
    # 降级档 onset-energy
    res, total_s = detect_onset_energy(media, cfg)
    rec = rs_fetchable.degrade_record("audio.beat")
    doc = {"bpm": res["bpm"], "meter": DEFAULT_METER,
           "beats": res["beats"], "downbeats": [],
           "confidence": res["confidence"], "engine": "onset-energy",
           "unit": BEATS_UNIT, "source": media.name,
           "durationSec": round(total_s, 3), "tiers": tiers}
    doc.update(rec)
    if want_downbeat:
        doc["tiers"]["downbeat"] = {"engine": "beat-only", "degraded": True,
                                    "degradeReason": "beat-only",
                                    "missingComponent": "madmom",
                                    "note": "能量法无小节相位,只给拍点网格"}
    if want_stems:
        doc["tiers"]["stems"] = {"available": False, "engine": "mixed-audio",
                                 "degraded": True, "degradeReason": "none",
                                 "missingComponent": "demucs",
                                 "note": "直接对混音检测(缺人声/伴奏分离组件)"}
    _write(out_path, doc)
    return doc, EXIT_OK


def _write(out_path: Path, doc: dict) -> None:
    write_text_atomic(out_path, json.dumps(doc, ensure_ascii=False, indent=1))


def _pick_project_media(root: Path) -> Path | None:
    """工程模式选素材:manifest probe 通过且带音轨者优先,否则按扩展名兜底。"""
    man = rs_paths.manifest_json(root)
    items: list[dict] = []
    if man.is_file():
        try:
            items = json.loads(man.read_text(encoding="utf-8")).get("items") or []
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            items = []
    mat_dir = rs_paths.resolve(root, "materials")
    audioish = [it for it in items
                if it.get("probe") == "ok" and (it.get("hasAudio") or
                                                Path(it.get("file", "")).suffix.lower() in AUDIO_EXTS)]
    for it in audioish:
        q = mat_dir / str(it.get("file", ""))
        if q.is_file():
            return q
    if mat_dir.is_dir():
        for q in sorted(mat_dir.iterdir()):
            if q.is_file() and q.suffix.lower() in MEDIA_EXTS:
                return q
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rs_beat.py",
        description="节拍检测(M8 混剪能力):READY=beatnet 推理;降级=onset-energy 能量起音,必留痕")
    ap.add_argument("command", nargs="?", choices=["detect"], default=None,
                    help="detect=显式指定素材;缺省 = 工程模式(自动选素材,挂载器契约)")
    ap.add_argument("media", nargs="?", default=None, help="音频或视频素材路径")
    ap.add_argument("--out", default=None, help="工程根(产物 04_粗剪决策/beats.json)")
    ap.add_argument("--downbeat", action="store_true", help="同时产出下拍网格(madmom;缺组件降级 beat-only)")
    ap.add_argument("--stems", action="store_true", help="人声/伴奏分离后检测(demucs;缺组件对混音检测)")
    ap.add_argument("--force", action="store_true", help="工程模式下忽略产物新鲜度强制重算")
    a = ap.parse_args(argv)

    if a.command == "detect":
        if not a.media or not a.out:
            return emit(False, "BAD_INPUT", "detect 需要 <素材> 与 --out <工程根>", exit_code=EXIT_INPUT)
        media = Path(a.media)
        if not media.is_file():
            return emit(False, "NO_MEDIA", f"素材不存在:{media}", exit_code=EXIT_INPUT)
        doc, rc = analyze(media, Path(a.out), want_downbeat=a.downbeat,
                          want_stems=a.stems, force=a.force)
        return emit(rc == EXIT_OK, "BEATS_OK" if rc == EXIT_OK else "BEATS_FAIL",
                    f"bpm={doc.get('bpm')} beats={len(doc.get('beats') or [])} "
                    f"engine={doc.get('engine')} degraded={doc.get('degraded')}",
                    doc, exit_code=rc)
    # 工程模式(无参,挂载器/第二波 Agent 的默认入口)
    root = Path.cwd()
    out_path = beats_path(root)
    media = _pick_project_media(root)
    if media is None:
        return emit(False, "NO_MEDIA",
                    f"工程无带音轨素材({rs_paths.p('materials')}/ 为空或 probe 全败);"
                    "未写产物(beat.snap 将按 BEATS_MISSING 拒绝伪造)", exit_code=EXIT_INPUT)
    if not a.force and out_path.is_file() and out_path.stat().st_mtime >= media.stat().st_mtime:
        try:
            doc = json.loads(out_path.read_text(encoding="utf-8"))
            return emit(True, "BEATS_CACHED",
                        f"beats.json 新于素材,跳过重算(--force 强制):bpm={doc.get('bpm')}",
                        doc)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass                                # 坏产物 → 落到重算
    doc, rc = analyze(media, root, want_downbeat=a.downbeat, want_stems=a.stems,
                      force=a.force)
    return emit(rc == EXIT_OK, "BEATS_OK" if rc == EXIT_OK else "BEATS_FAIL",
                f"素材={media.name} bpm={doc.get('bpm')} beats={len(doc.get('beats') or [])} "
                f"engine={doc.get('engine')} degraded={doc.get('degraded')}",
                doc, exit_code=rc)


if __name__ == "__main__":
    sys.exit(main())
