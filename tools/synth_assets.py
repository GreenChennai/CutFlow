# -*- coding: utf-8 -*-
"""M12 素材库生成器:自产合成 sfx / bgm / 元素贴图 / 花字模板,并构建统一索引。

职责(分册01 §2/§4,ADR-0053):
  · 自产合成音效(ffmpeg + numpy)、BGM 曲库、透明元素贴图(Pillow)、
    花字 ASS 基础档模板与 artboard 模式 S 源卡;
  · 对全部素材 ffprobe 实测时长 + 计 sha256,写 `skills/cutflow/assets/manifest.json`
    (四许可字段 source/license/commercial/attribution + aiGenerated,R45);
  · 由统一 manifest 再生 `assets/bgm/manifest.json` 兼容件(rs_intent 旧读口);
  · 由 artboard `fonts/README.md` 生成 `templates/fonts.json` 字体索引(禁手改,§5)。

纪律:
  · 确定性:同代码同输入必得同素材(随机源一律固定种子);
  · 许可诚实:自产素材 source=自产、aiGenerated=true,绝不虚报外部来源;
  · 体积预算(R38):音效 ≤200KB / BGM ≤3MB / 元素单图 ≤300KB,生成端即守;
  · 幂等:重复运行产出一致(manifest generatedAt 固定为构建日,不随钟面漂移)。

用法:
  python tools/synth_assets.py --all          # 全量(素材 + manifest + 字体表)
  python tools/synth_assets.py --fonts-only   # 只再生成 templates/fonts.json
  python tools/synth_assets.py --manifest-only  # 只重算 manifest(不重合成)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
import subprocess
import sys
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "skills" / "cutflow" / "assets"
SFX_DIR = ASSETS / "sfx"
BGM_DIR = ASSETS / "bgm"
ELEM_DIR = ASSETS / "elements"
HUAZI_DIR = ASSETS / "huazi"
MANIFEST = ASSETS / "manifest.json"
FONTS_JSON = REPO / "skills" / "cutflow" / "templates" / "fonts.json"
# artboard 路径锁定(用户 2026-09-26 确认,分册01 §5):工作区级为唯一准绳
ARTBOARD_LOCKED = Path(r"E:\平日资料\GitHub\.agents\skills\artboard")
FONTS_README = ARTBOARD_LOCKED / "fonts" / "README.md"

GENERATED_AT = "2026-09-26T00:00:00+08:00"   # 固定构建日,保证幂等
SELF_SOURCE = "自产"
SELF_LICENSE = "自产合成(ffmpeg/numpy/Pillow,无版权约束)"
SELF_ATTRIBUTION = "CutFlow 自产合成,无需归因"
MIXKIT_SOURCE = "Mixkit"
MIXKIT_LICENSE = "Mixkit Sound Effects Free License"
MIXKIT_ATTRIBUTION = "Mixkit (mixkit.co/free-sound-effects/)"

FFMPEG = None


def ffmpeg_bin() -> str:
    global FFMPEG
    if FFMPEG:
        return FFMPEG
    cfg_p = REPO / "config.json"
    if cfg_p.is_file():
        try:
            d = json.loads(cfg_p.read_text(encoding="utf-8")).get("ffmpeg_dir", "")
            p = Path(d) / "ffmpeg.exe"
            if p.is_file():
                FFMPEG = str(p)
                return FFMPEG
        except (json.JSONDecodeError, OSError):
            pass
    FFMPEG = "ffmpeg"
    return FFMPEG


# ================================================================ 音频合成基元(numpy)

import numpy as np  # noqa: E402

SR = 44100
RNG = np.random.default_rng(20260926)   # 固定种子:确定性


def _t(n: int) -> np.ndarray:
    """n 个样本 → 时间轴(秒)。统一口径:配方内部一律传样本数。"""
    return np.arange(int(n)) / SR


def _env(n: int, a: float = 0.01, r: float = 0.3, shape: float = 2.0) -> np.ndarray:
    """attack-release 包络(秒);主体平顶。"""
    total = n / SR
    na, nr = max(1, int(a * SR)), max(1, int(r * SR))
    env = np.ones(n)
    na, nr = min(na, n), min(nr, n)
    env[:na] = np.linspace(0, 1, na)
    if nr <= n:
        env[n - nr:] = np.linspace(1, 0, nr) ** shape
    return env


def _decay(n: int, tau: float) -> np.ndarray:
    return np.exp(-np.arange(n) / (SR * tau))


def sine(f: float | np.ndarray, n: int) -> np.ndarray:
    t = _t(n)
    freq = np.full(n, f, dtype=float) if np.isscalar(f) else f[:n]
    phase = 2 * np.pi * np.cumsum(freq) / SR
    return np.sin(phase)


def sweep(f0: float, f1: float, n: int, shape: float = 1.0) -> np.ndarray:
    """频率从 f0 滑到 f1 的正弦(指数滑音更自然)。"""
    t = _t(n)
    k = (t / t[-1]) ** shape
    freq = f0 * (f1 / f0) ** k
    return np.sin(2 * np.pi * np.cumsum(freq) / SR)


def noise(n: int) -> np.ndarray:
    return RNG.standard_normal(n)


def lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """简单一阶低通(柔化噪声用)。"""
    a = 1.0 - math.exp(-2 * math.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i in range(len(x)):
        acc += a * (x[i] - acc)
        y[i] = acc
    return y


def bandpass_center(x: np.ndarray, f_lo: float, f_hi: float) -> np.ndarray:
    """带通(两次一阶:低通-高通),元素音色整形用。"""
    return lowpass(x, f_hi) - lowpass(lowpass(x, f_hi), f_lo)


def bell_tone(f: float, dur: float, partials=((1.0, 1.0), (2.4, 0.4), (3.9, 0.2)),
              tau: float = 0.25) -> np.ndarray:
    """钟/铃类:非谐分音 + 指数衰减。"""
    n = int(SR * dur)
    out = np.zeros(n)
    for mult, amp in partials:
        out += amp * np.sin(2 * np.pi * f * mult * _t(n)) * _decay(n, tau / mult ** 0.5)
    return out


def normalize(x: np.ndarray, peak: float = 0.89) -> np.ndarray:
    m = float(np.max(np.abs(x))) or 1.0
    return x / m * peak


def _wav_bytes(x: np.ndarray) -> bytes:
    """float mono → 16bit WAV bytes(喂 ffmpeg stdin,不留临时 wav)。"""
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return buf.getvalue()


def _wav_bytes_stereo(x: np.ndarray) -> bytes:
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    pcm = pcm.reshape(-1, 2) if pcm.ndim == 2 else np.stack([pcm, pcm], axis=1)
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def encode_mp3(x: np.ndarray, dst: Path, kbps: int = 96, stereo: bool = False) -> None:
    """numpy 波形 → mp3(ffmpeg stdin,体积预算由 kbps×时长保证)。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    payload = _wav_bytes_stereo(x) if stereo else _wav_bytes(x)
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "wav", "-i", "pipe:0",
           "-codec:a", "libmp3lame", "-b:a", f"{kbps}k", str(dst)]
    p = subprocess.run(cmd, input=payload, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg 编码失败 {dst.name}: {p.stderr.decode('utf-8', 'replace')[:200]}")


# ================================================================ 音效配方(33 自产)

def sfx_whoosh_up() -> np.ndarray:
    n = int(SR * 0.7)
    x = bandpass_center(noise(n), 300, 3000) * _env(n, 0.15, 0.35)
    return normalize(x + 0.3 * sweep(300, 1800, n) * _env(n, 0.2, 0.3))


def sfx_whoosh_down() -> np.ndarray:
    n = int(SR * 0.7)
    x = bandpass_center(noise(n), 300, 2500) * _env(n, 0.12, 0.4)
    return normalize(x + 0.3 * sweep(1600, 250, n) * _env(n, 0.15, 0.35))


def sfx_whoosh_side() -> np.ndarray:
    n = int(SR * 0.8)
    x = bandpass_center(noise(n), 400, 3200) * _env(n, 0.3, 0.3)
    pan = np.linspace(-0.8, 0.8, n)     # 左→右声像扫(入轨后由渲染单声道混)
    return normalize(x * (0.7 + 0.3 * np.abs(pan)))


def sfx_impact() -> np.ndarray:
    n = int(SR * 0.9)
    thump = sweep(160, 42, n) * _decay(n, 0.16)
    crash = lowpass(noise(n), 4000) * _decay(n, 0.08)
    return normalize(thump + 0.5 * crash)


def sfx_glitch() -> np.ndarray:
    n = int(SR * 0.45)
    out = np.zeros(n)
    pos = 0
    k = 0
    while pos < n:                       # 交替方波/噪声碎块(数字毛刺感)
        seg = min(int(SR * 0.03), n - pos)
        piece = (sine(900 + 500 * (k % 3), seg) * (1 if k % 2 else -0.6)
                 if k % 3 else noise(seg))
        out[pos:pos + seg] = piece * _env(seg, 0.002, 0.02)
        pos += seg
        k += 1
    return normalize(out)


def sfx_chime() -> np.ndarray:
    n = int(SR * 1.1)
    out = np.zeros(n)
    for i, f in enumerate((1046.5, 1318.5, 1568.0)):   # C6 E6 G6 上行琶音
        start = int(SR * 0.12 * i)
        tone = bell_tone(f, 0.8, tau=0.3)
        out[start:start + len(tone)] += tone * 0.8
    return normalize(out)


def sfx_tada() -> np.ndarray:
    n = int(SR * 1.3)
    out = np.zeros(n)
    for i, f in enumerate((523.25, 659.25, 783.99, 1046.5)):   # C 大调Wide strum
        start = int(SR * 0.04 * i)
        seg = int(SR * 1.0)
        tone = np.sin(2 * np.pi * f * _t(seg)) * _decay(seg, 0.45)
        out[start:start + seg] += tone * 0.7
    out += 0.25 * bandpass_center(noise(n), 4000, 12000) * _decay(n, 0.5)
    return normalize(out)


def sfx_sparkle() -> np.ndarray:
    n = int(SR * 0.9)
    out = np.zeros(n)
    for _ in range(14):                  # 高频随机星芒闪烁
        start = int(RNG.uniform(0, 0.5) * SR)
        f = float(RNG.uniform(2500, 6000))
        seg = min(int(SR * 0.12), n - start)
        if seg <= 0:
            continue
        out[start:start + seg] += np.sin(2 * np.pi * f * _t(seg)) * _decay(seg, 0.04)
    return normalize(out)


def sfx_duang() -> np.ndarray:
    n = int(SR * 1.0)
    t = _t(n)
    vib = 1 + 0.25 * np.sin(2 * np.pi * 9 * t) * np.exp(-t / 0.35)   # 弹簧颤音衰减
    freq = np.where(True, 170 * vib, 170)
    x = np.sin(2 * np.pi * np.cumsum(freq) / SR) * _decay(n, 0.4)
    return normalize(x)


def sfx_magic() -> np.ndarray:
    n = int(SR * 0.9)
    out = np.zeros(n)
    run = (523.25, 587.33, 659.25, 783.99, 880.0, 1046.5, 1318.5)   # 五声上行滑跑
    for i, f in enumerate(run):
        start = int(SR * 0.055 * i)
        seg = min(int(SR * 0.3), n - start)
        if seg <= 0:
            break
        out[start:start + seg] += np.sin(2 * np.pi * f * _t(seg)) * _decay(seg, 0.12)
    return normalize(out + 0.2 * bandpass_center(noise(n), 5000, 11000) * _env(n, 0.05, 0.5))


def sfx_tick() -> np.ndarray:
    n = int(SR * 0.08)
    x = np.sign(np.sin(2 * np.pi * 3000 * _t(n))) * _env(n, 0.001, 0.05, 3)
    return normalize(lowpass(x, 8000) * 0.9)


def sfx_tap() -> np.ndarray:
    n = int(SR * 0.12)
    x = lowpass(noise(n), 1600) * _env(n, 0.001, 0.08, 3)
    return normalize(x + 0.4 * sweep(500, 220, n) * _decay(n, 0.03))


def sfx_toggle() -> np.ndarray:
    n = int(SR * 0.22)
    out = np.zeros(n)
    for start, f in ((0, 2400), (int(SR * 0.1), 1600)):   # 开/关两击
        seg = int(SR * 0.07)
        out[start:start + seg] += np.sign(np.sin(2 * np.pi * f * _t(seg))) * \
            _env(seg, 0.001, 0.05, 3) * 0.9
    return normalize(lowpass(out, 8000))


def sfx_typewriter() -> np.ndarray:
    n = int(SR * 0.14)
    thock = lowpass(noise(n), 2500) * _env(n, 0.001, 0.09, 4)
    clickh = bandpass_center(noise(n), 3000, 8000)[:n] * _decay(n, 0.01)
    return normalize(thock + 0.5 * clickh)


def sfx_heartbeat() -> np.ndarray:
    n = int(SR * 1.6)
    out = np.zeros(n)
    for base in (0.0, 0.28, 0.8, 1.08):    # lub-dub ×2
        seg = int(SR * 0.22)
        start = int(SR * base)
        out[start:start + seg] += sweep(95, 45, seg) * _decay(seg, 0.09) * (0.9 if base in (0.0, 0.8) else 0.6)
    return normalize(out)


def sfx_tension() -> np.ndarray:
    n = int(SR * 2.2)
    t = _t(n)
    tremolo = 0.6 + 0.4 * np.sin(2 * np.pi * 7 * t)    # 颤音渐强
    cluster = (np.sin(2 * np.pi * 220 * t) + np.sin(2 * np.pi * 311 * t)   # 三全音簇
               + 0.5 * np.sin(2 * np.pi * 466 * t))
    growl = 0.4 * lowpass(noise(n), 500) * (t / t[-1])
    return normalize(cluster * tremolo * (0.4 + 0.6 * t / t[-1]) / 2.5 + growl * 0.4)


def sfx_sad_hit() -> np.ndarray:
    n = int(SR * 1.4)
    out = np.zeros(n)
    for i, (f, dt) in enumerate(((440.0, 0.0), (349.23, 0.18), (261.63, 0.36))):  # A4-F4-C4 下行
        start = int(SR * dt)
        seg = int(SR * 0.9)
        tone = (np.sin(2 * np.pi * f * _t(seg)) + 0.4 * np.sin(2 * np.pi * f * 2 * _t(seg))) \
            * _decay(seg, 0.5)
        out[start:start + seg] += tone * 0.8
    return normalize(out)


def sfx_warm_up() -> np.ndarray:
    n = int(SR * 1.6)
    t = _t(n)
    chord = (np.sin(2 * np.pi * 261.63 * t) + np.sin(2 * np.pi * 329.63 * t)
             + np.sin(2 * np.pi * 392.0 * t)) / 3
    swell = np.minimum(t / 0.9, 1.0) * _env(n, 0.9, 0.5)
    return normalize(chord * swell + 0.15 * lowpass(noise(n), 900) * swell)


def sfx_sigh() -> np.ndarray:
    n = int(SR * 1.3)
    breath = lowpass(noise(n), 1200) * _env(n, 0.25, 0.8, 1.5)
    fall = sweep(420, 180, n) * _env(n, 0.2, 0.8) * 0.5
    return normalize(breath * 0.8 + fall)


def sfx_drone() -> np.ndarray:
    n = int(SR * 2.5)
    t = _t(n)
    x = (np.sin(2 * np.pi * 55 * t) + np.sin(2 * np.pi * 110.5 * t)     # 微失谐拍频
         + 0.3 * np.sin(2 * np.pi * 165 * t)) / 2.3
    return normalize(x * _env(n, 0.4, 0.6))


def sfx_applause() -> np.ndarray:
    n = int(SR * 2.0)
    out = np.zeros(n)
    for _ in range(420):                 # 密集随机掌击(掌声的经典合成法)
        start = int(RNG.uniform(0, 1.7) * SR)
        seg = min(int(SR * 0.02), n - start)
        if seg <= 0:
            continue
        out[start:start + seg] += bandpass_center(noise(seg), 1200, 6000) * RNG.uniform(0.3, 1.0)
    grow = np.concatenate([np.linspace(0.5, 1.0, n // 2), np.linspace(1.0, 0.3, n - n // 2)])
    return normalize(out * grow)


def sfx_crowd_cheer() -> np.ndarray:
    n = int(SR * 2.2)
    t = _t(n)
    murmur = bandpass_center(noise(n), 500, 2500) * _env(n, 0.5, 0.9)
    whistles = np.zeros(n)
    for _ in range(5):                   # 零星口哨(欢呼感)
        start = int(RNG.uniform(0, 1.2) * SR)
        f = float(RNG.uniform(1800, 3200))
        seg = min(int(SR * 0.4), n - start)
        if seg <= 0:
            continue
        vib = f * (1 + 0.04 * np.sin(2 * np.pi * 6 * _t(seg)))
        whistles[start:start + seg] += np.sin(2 * np.pi * np.cumsum(vib) / SR) * _env(seg, 0.08, 0.2) * 0.25
    return normalize(murmur * 0.8 + whistles)


def sfx_boo() -> np.ndarray:
    n = int(SR * 1.5)
    rough = np.sin(2 * np.pi * 130 * _t(n)) * (1 + 0.5 * np.sign(np.sin(2 * np.pi * 28 * _t(n))))
    fall = sweep(200, 95, n)
    x = (rough * 0.6 + fall * 0.6) * _env(n, 0.3, 0.7)
    return normalize(lowpass(x, 900))


def sfx_surprise() -> np.ndarray:
    n = int(SR * 0.8)
    up = sweep(300, 850, int(SR * 0.35))
    down = sweep(850, 520, n - len(up))
    return normalize(np.concatenate([up, down]) * _env(n, 0.02, 0.3))


def sfx_drumroll() -> np.ndarray:
    n = int(SR * 1.8)
    out = np.zeros(n)
    period = int(SR / 22)                # 22Hz 双手滚奏
    k = 0
    while k * period // 2 < n:
        start = (k * period) // 2
        seg = min(int(SR * 0.02), n - start)
        if seg <= 0:
            break
        amp = 0.35 + 0.65 * (start / n)  # 渐强
        out[start:start + seg] += lowpass(noise(seg), 3000) * amp
        k += 1
    return normalize(out)


def sfx_gavel() -> np.ndarray:
    n = int(SR * 0.9)
    out = np.zeros(n)
    for start in (0, int(SR * 0.32)):    # 木槌两击
        seg = int(SR * 0.25)
        knock = (sweep(240, 120, seg) * _decay(seg, 0.06)
                 + 0.5 * bandpass_center(noise(seg), 2000, 7000) * _decay(seg, 0.015))
        out[start:start + seg] += knock
    return normalize(out)


def sfx_wind() -> np.ndarray:
    n = int(SR * 3.0)
    t = _t(n)
    gust = lowpass(noise(n), 600) * (0.55 + 0.45 * np.sin(2 * np.pi * 0.4 * t + 1.2))
    hiss = lowpass(noise(n), 3000) * 0.3 * (0.5 + 0.5 * np.sin(2 * np.pi * 0.23 * t))
    return normalize(gust + hiss)


def sfx_rain() -> np.ndarray:
    n = int(SR * 3.0)
    bed = lowpass(noise(n), 5000) * 0.4
    out = bed.copy()
    for _ in range(260):                 # 稀疏雨滴
        start = int(RNG.uniform(0, 2.8) * SR)
        seg = min(int(SR * 0.012), n - start)
        if seg <= 0:
            continue
        out[start:start + seg] += np.sin(2 * np.pi * RNG.uniform(1500, 5000) * _t(seg)) \
            * _decay(seg, 0.004) * RNG.uniform(0.2, 0.7)
    return normalize(out)


def sfx_city() -> np.ndarray:
    n = int(SR * 3.0)
    t = _t(n)
    rumble = lowpass(noise(n), 220) * 0.9
    murmur = bandpass_center(noise(n), 300, 1400) * (0.4 + 0.2 * np.sin(2 * np.pi * 0.3 * t))
    return normalize(rumble + murmur * 0.6)


def sfx_room_tone() -> np.ndarray:
    n = int(SR * 3.0)
    return normalize(lowpass(noise(n), 400) * 0.5, peak=0.35)


def sfx_outro_bell() -> np.ndarray:
    x = bell_tone(392.0, 2.8, partials=((1.0, 1.0), (2.4, 0.5), (3.9, 0.25), (5.4, 0.1)), tau=0.9)
    shimmer = 0.15 * bandpass_center(noise(len(x)), 6000, 12000) * _decay(len(x), 0.8)
    return normalize(x + shimmer)


def sfx_fade_riser() -> np.ndarray:
    n = int(SR * 2.0)
    x = sweep(180, 1400, n) * 0.5 + bandpass_center(noise(n), 400, 2600) * 0.6
    env = np.linspace(0.2, 1.0, int(n * 0.7)).tolist() + np.linspace(1.0, 0.0, n - int(n * 0.7)).tolist()
    return normalize(x * np.array(env))


def sfx_end_sting() -> np.ndarray:
    n = int(SR * 2.2)
    t = _t(n)
    out = sweep(120, 45, n) * _decay(n, 0.25)
    for f in (261.63, 329.63, 392.0, 523.25):   # 终结 C 大三和弦(宽位)
        out += (np.sin(2 * np.pi * f * t) + 0.3 * np.sin(2 * np.pi * f * 2 * t)) * _decay(n, 0.9) * 0.5
    return normalize(out / 2.2)


# 音效登记表:file → (id, label, tags, usage, gainHintDb, source 档)
SFX_SPECS: list[dict] = [
    # ---- 既有 7 个(Mixkit;id/组名即旧伪协议名,行为不变)
    {"file": "whoosh_01.mp3", "id": "sfx.whoosh.01", "label": "转场呼啸", "gen": None,
     "tags": ["转场", "冲击", "卡片"], "usage": ["transition"], "gain": -12,
     "source": MIXKIT_SOURCE, "license": MIXKIT_LICENSE, "attr": MIXKIT_ATTRIBUTION},
    {"file": "swipe_01.mp3", "id": "sfx.swipe.01", "label": "快速滑切", "gen": None,
     "tags": ["转场", "快速", "滑动"], "usage": ["transition"], "gain": -12,
     "source": MIXKIT_SOURCE, "license": MIXKIT_LICENSE, "attr": MIXKIT_ATTRIBUTION},
    {"file": "pop_01.mp3", "id": "sfx.pop.01", "label": "弹出强调", "gen": None,
     "tags": ["强调", "弹出", "列举"], "usage": ["enumeration", "punchline"], "gain": -12,
     "source": MIXKIT_SOURCE, "license": MIXKIT_LICENSE, "attr": MIXKIT_ATTRIBUTION},
    {"file": "ding_01.mp3", "id": "sfx.ding.01", "label": "提示叮", "gen": None,
     "tags": ["强调", "关键词", "正确"], "usage": ["punchline"], "gain": -12,
     "source": MIXKIT_SOURCE, "license": MIXKIT_LICENSE, "attr": MIXKIT_ATTRIBUTION},
    {"file": "bell_01.mp3", "id": "sfx.bell.01", "label": "章节铃", "gen": None,
     "tags": ["章节", "提醒", "收尾"], "usage": ["chapter", "ending"], "gain": -14,
     "source": MIXKIT_SOURCE, "license": MIXKIT_LICENSE, "attr": MIXKIT_ATTRIBUTION},
    {"file": "click_01.mp3", "id": "sfx.click.01", "label": "点击", "gen": None,
     "tags": ["列举", "点击", "步骤"], "usage": ["enumeration"], "gain": -14,
     "source": MIXKIT_SOURCE, "license": MIXKIT_LICENSE, "attr": MIXKIT_ATTRIBUTION},
    {"file": "riser_01.mp3", "id": "sfx.riser.01", "label": "悬念上扫", "gen": None,
     "tags": ["章节", "悬念", "铺垫"], "usage": ["chapter", "transition"], "gain": -14,
     "source": MIXKIT_SOURCE, "license": MIXKIT_LICENSE, "attr": MIXKIT_ATTRIBUTION},
    # ---- 自产 33 个
    {"file": "whoosh_02.mp3", "id": "sfx.whoosh.02", "label": "呼啸上行", "gen": sfx_whoosh_up,
     "tags": ["转场", "上行", "快节奏"], "usage": ["transition"], "gain": -12},
    {"file": "whoosh_03.mp3", "id": "sfx.whoosh.03", "label": "呼啸下行", "gen": sfx_whoosh_down,
     "tags": ["转场", "下行", "落版"], "usage": ["transition"], "gain": -12},
    {"file": "whoosh_04.mp3", "id": "sfx.whoosh.04", "label": "呼啸横扫", "gen": sfx_whoosh_side,
     "tags": ["转场", "横移", "滑动"], "usage": ["transition"], "gain": -12},
    {"file": "impact_01.mp3", "id": "sfx.impact.01", "label": "数据冲击", "gen": sfx_impact,
     "tags": ["强调", "数据", "重击"], "usage": ["punchline", "chapter"], "gain": -12},
    {"file": "glitch_01.mp3", "id": "sfx.glitch.01", "label": "数字毛刺", "gen": sfx_glitch,
     "tags": ["转场", "科技", "故障"], "usage": ["transition"], "gain": -14},
    {"file": "chime_01.mp3", "id": "sfx.chime.01", "label": "上行风铃", "gen": sfx_chime,
     "tags": ["章节", "轻快", "提示"], "usage": ["chapter", "punchline"], "gain": -14},
    {"file": "tada_01.mp3", "id": "sfx.tada.01", "label": "亮相和弦", "gen": sfx_tada,
     "tags": ["强调", "亮相", "喜庆"], "usage": ["punchline", "chapter"], "gain": -12},
    {"file": "sparkle_01.mp3", "id": "sfx.sparkle.01", "label": "星芒闪烁", "gen": sfx_sparkle,
     "tags": ["强调", "闪亮", "可爱"], "usage": ["punchline", "decor"], "gain": -14},
    {"file": "duang_01.mp3", "id": "sfx.duang.01", "label": "弹簧咚", "gen": sfx_duang,
     "tags": ["强调", "数据", "搞笑"], "usage": ["punchline"], "gain": -12},
    {"file": "magic_01.mp3", "id": "sfx.magic.01", "label": "魔法上行", "gen": sfx_magic,
     "tags": ["强调", "魔法", "上行"], "usage": ["punchline", "chapter"], "gain": -14},
    {"file": "tick_01.mp3", "id": "sfx.tick.01", "label": "秒针嗒", "gen": sfx_tick,
     "tags": ["列举", "计时", "步骤"], "usage": ["enumeration"], "gain": -16},
    {"file": "tap_01.mp3", "id": "sfx.tap.01", "label": "软敲", "gen": sfx_tap,
     "tags": ["列举", "轻点", "UI"], "usage": ["enumeration", "ui"], "gain": -16},
    {"file": "toggle_01.mp3", "id": "sfx.toggle.01", "label": "开关切换", "gen": sfx_toggle,
     "tags": ["列举", "切换", "对比"], "usage": ["enumeration"], "gain": -16},
    {"file": "typewriter_01.mp3", "id": "sfx.typewriter.01", "label": "打字机", "gen": sfx_typewriter,
     "tags": ["列举", "打字", "字幕"], "usage": ["enumeration"], "gain": -16},
    {"file": "heartbeat_01.mp3", "id": "sfx.heartbeat.01", "label": "心跳", "gen": sfx_heartbeat,
     "tags": ["情绪", "紧张", "低谷"], "usage": ["emotion"], "gain": -12},
    {"file": "tension_01.mp3", "id": "sfx.tension.01", "label": "紧张音簇", "gen": sfx_tension,
     "tags": ["情绪", "紧张", "悬念"], "usage": ["emotion"], "gain": -14},
    {"file": "sad_hit_01.mp3", "id": "sfx.sad_hit.01", "label": "伤感下行", "gen": sfx_sad_hit,
     "tags": ["情绪", "悲伤", "落泪点"], "usage": ["emotion"], "gain": -14},
    {"file": "warm_up_01.mp3", "id": "sfx.warm_up.01", "label": "暖意升起", "gen": sfx_warm_up,
     "tags": ["情绪", "温暖", "正向"], "usage": ["emotion"], "gain": -16},
    {"file": "sigh_01.mp3", "id": "sfx.sigh.01", "label": "叹气", "gen": sfx_sigh,
     "tags": ["情绪", "失落", "转折"], "usage": ["emotion"], "gain": -16},
    {"file": "drone_01.mp3", "id": "sfx.drone.01", "label": "低吟铺底", "gen": sfx_drone,
     "tags": ["情绪", "氛围", "深度"], "usage": ["emotion", "chapter"], "gain": -18},
    {"file": "applause_01.mp3", "id": "sfx.applause.01", "label": "掌声", "gen": sfx_applause,
     "tags": ["综艺", "掌声", "喝彩"], "usage": ["punchline", "ending"], "gain": -12},
    {"file": "crowd_cheer_01.mp3", "id": "sfx.crowd_cheer.01", "label": "人群欢呼(合成模拟)",
     "gen": sfx_crowd_cheer, "tags": ["综艺", "欢呼", "人群"], "usage": ["punchline", "ending"], "gain": -12},
    {"file": "boo_01.mp3", "id": "sfx.boo.01", "label": "嘘声(合成模拟)", "gen": sfx_boo,
     "tags": ["综艺", "嘘声", "反差"], "usage": ["punchline"], "gain": -14},
    {"file": "surprise_01.mp3", "id": "sfx.surprise.01", "label": "惊讶哇哦", "gen": sfx_surprise,
     "tags": ["综艺", "惊讶", "转折"], "usage": ["punchline"], "gain": -12},
    {"file": "drumroll_01.mp3", "id": "sfx.drumroll.01", "label": "滚奏鼓点", "gen": sfx_drumroll,
     "tags": ["综艺", "悬念", "揭晓"], "usage": ["chapter", "punchline"], "gain": -12},
    {"file": "gavel_01.mp3", "id": "sfx.gavel.01", "label": "法槌定音", "gen": sfx_gavel,
     "tags": ["综艺", "定论", "敲定"], "usage": ["punchline", "ending"], "gain": -12},
    {"file": "wind_01.mp3", "id": "sfx.wind.01", "label": "风声", "gen": sfx_wind,
     "tags": ["环境", "风", "户外"], "usage": ["decor", "emotion"], "gain": -18},
    {"file": "rain_01.mp3", "id": "sfx.rain.01", "label": "雨声", "gen": sfx_rain,
     "tags": ["环境", "雨", "安静"], "usage": ["decor", "emotion"], "gain": -18},
    {"file": "city_01.mp3", "id": "sfx.city.01", "label": "城市底噪", "gen": sfx_city,
     "tags": ["环境", "城市", "vlog"], "usage": ["decor"], "gain": -20},
    {"file": "room_tone_01.mp3", "id": "sfx.room_tone.01", "label": "室内底噪", "gen": sfx_room_tone,
     "tags": ["环境", "室内", "铺底"], "usage": ["decor"], "gain": -24},
    {"file": "outro_bell_01.mp3", "id": "sfx.outro_bell.01", "label": "片尾钟", "gen": sfx_outro_bell,
     "tags": ["收尾", "片尾", "定格"], "usage": ["ending"], "gain": -12},
    {"file": "fade_riser_01.mp3", "id": "sfx.fade_riser.01", "label": "渐隐上扫", "gen": sfx_fade_riser,
     "tags": ["收尾", "预告", "悬念"], "usage": ["ending", "chapter"], "gain": -14},
    {"file": "end_sting_01.mp3", "id": "sfx.end_sting.01", "label": "终结和弦", "gen": sfx_end_sting,
     "tags": ["收尾", "定格", "落版"], "usage": ["ending"], "gain": -12},
]


# ================================================================ BGM 配方(12 自产)

def _bgm_pad(freqs: list[float], n: int, detune: float = 0.996) -> np.ndarray:
    """和声垫:多音 + 微失谐三角波叠(柔和不刺耳)。"""
    t = _t(n)
    out = np.zeros(n)
    for f in freqs:
        for k, dt in ((1.0, 1.0), (1.0, detune)):
            out += np.sin(2 * np.pi * f * k * dt * t) / (len(freqs) * 2)
        out += 0.3 * np.sin(2 * np.pi * f * 2 * t) / (len(freqs) * 4)
    return out


def _bgm_bass(freq: float, n: int) -> np.ndarray:
    return np.sin(2 * np.pi * freq / 2 * _t(n)) * 0.5


def _bgm_kick(n_beats: int, beat_n: int) -> np.ndarray:
    out = np.zeros(n_beats * beat_n)
    for b in range(n_beats):
        seg = int(SR * 0.12)
        if b % 1 == 0 and b * beat_n + seg < len(out):
            out[b * beat_n:b * beat_n + seg] += sweep(130, 45, seg) * _decay(seg, 0.05) * 0.9
    return out


def _bgm_hats(n_beats: int, beat_n: int, density: int = 2) -> np.ndarray:
    out = np.zeros(n_beats * beat_n)
    step = beat_n // max(1, density)
    for i in range(0, n_beats * density):
        start = i * step
        seg = min(int(SR * 0.03), len(out) - start)
        if seg <= 0:
            break
        out[start:start + seg] += bandpass_center(noise(seg), 6000, 12000) * _decay(seg, 0.008) * 0.25
    return out


def _bgm_arpeggio(freqs: list[float], n: int, step_s: float) -> np.ndarray:
    out = np.zeros(n)
    step_n = int(SR * step_s)
    i = 0
    while i * step_n < n:
        f = freqs[i % len(freqs)]
        seg = min(step_n, n - i * step_n)
        out[i * step_n:i * step_n + seg] += np.sin(2 * np.pi * f * _t(seg)) * _env(seg, 0.005, 0.06, 2) * 0.35
        i += 1
    return out


def _make_bgm(bars: int, bpm: float, chord_roots: list[tuple[float, str]],
              drums: str = "soft", arp: bool = False, vinyl: bool = False) -> np.ndarray:
    """小曲库合成器:和弦垫 + 贝斯 + 鼓组(+ 琶音/噪底)。确定性。"""
    beat = 60.0 / bpm
    beat_n = int(SR * beat)
    bar_n = 4 * beat_n          # 时长以整数样本拍为基,保证与鼓组轨长度严格一致
    n = bars * bar_n
    out = np.zeros(n)
    for bar in range(bars):
        root, quality = chord_roots[bar % len(chord_roots)]
        third = root * (5 ** (4 / 12)) if quality == "maj" else root * (5 ** (3 / 12))
        fifth = root * (7 ** (7 / 12))
        seg = bar_n
        base = bar * bar_n
        pad = _bgm_pad([root, third, fifth], seg,
                       detune=0.997 if quality == "maj" else 0.9965)
        pad *= _env(seg, 0.3, 0.5)
        out[base:base + seg] += pad * 0.8
        out[base:base + seg] += _bgm_bass(root, seg) * _env(seg, 0.01, 0.2)
        if arp:
            out[base:base + seg] += _bgm_arpeggio(
                [root, fifth, third * 2, fifth * 2], seg, beat / 4)
    if drums == "soft":
        out += _bgm_hats(bars * 4, beat_n, density=2) * 0.8
        out += _bgm_kick(bars * 4, beat_n) * 0.4
    elif drums == "drive":
        out += _bgm_kick(bars * 4, beat_n) * 1.1
        out += _bgm_hats(bars * 4, beat_n, density=4)
    elif drums == "half":
        out += _bgm_kick(max(1, bars * 2), beat_n * 2) * 0.5
    if vinyl:
        out += lowpass(noise(n), 6000) * 0.02
    return normalize(out, peak=0.8)


def bgm_bright_fast() -> np.ndarray:
    return _make_bgm(10, 128, [(261.63, "maj"), (392.0, "maj"), (440.0, "min"), (349.23, "maj")],
                     drums="drive", arp=True)


def bgm_tech_fast() -> np.ndarray:
    return _make_bgm(10, 132, [(220.0, "min"), (293.66, "min"), (220.0, "min"), (329.63, "maj")],
                     drums="drive", arp=True)


def bgm_drive_fast() -> np.ndarray:
    return _make_bgm(10, 140, [(196.0, "min"), (246.94, "min"), (293.66, "min"), (220.0, "min")],
                     drums="drive")


def bgm_tension_normal() -> np.ndarray:
    return _make_bgm(8, 110, [(220.0, "min"), (233.08, "min"), (220.0, "min"), (311.13, "min")],
                     drums="soft")


def bgm_suspense_normal() -> np.ndarray:
    return _make_bgm(8, 95, [(207.65, "min"), (220.0, "min"), (185.0, "min"), (207.65, "min")],
                     drums="half")


def bgm_sad_normal() -> np.ndarray:
    return _make_bgm(8, 80, [(220.0, "min"), (174.61, "maj"), (130.81, "maj"), (196.0, "maj")],
                     drums="half")


def bgm_bright_normal() -> np.ndarray:
    return _make_bgm(9, 105, [(261.63, "maj"), (349.23, "maj"), (440.0, "min"), (293.66, "maj")],
                     drums="soft", arp=True)


def bgm_tech_normal() -> np.ndarray:
    return _make_bgm(9, 112, [(233.08, "min"), (311.13, "min"), (261.63, "min"), (349.23, "maj")],
                     drums="soft", arp=True)


def bgm_warm_slow() -> np.ndarray:
    return _make_bgm(8, 75, [(261.63, "maj"), (349.23, "maj"), (220.0, "min"), (293.66, "maj")],
                     drums="half", vinyl=True)


def bgm_sad_slow() -> np.ndarray:
    return _make_bgm(8, 70, [(220.0, "min"), (164.81, "maj"), (146.83, "min"), (174.61, "maj")],
                     drums="half")


def bgm_chill_slow() -> np.ndarray:
    return _make_bgm(8, 85, [(261.63, "maj"), (311.13, "min"), (293.66, "min"), (349.23, "maj")],
                     drums="half", vinyl=True)


def bgm_bright_music() -> np.ndarray:
    return _make_bgm(10, 128, [(261.63, "maj"), (329.63, "min"), (392.0, "maj"), (440.0, "maj")],
                     drums="drive", arp=True)


BGM_SPECS: list[dict] = [
    # ---- 既有 4 条(v1 自产,pacing×mood 并入统一矩阵)
    {"file": "pulse_rhythm_140bpm.mp3", "id": "bgm.pulse.01", "label": "脉冲节奏 140", "gen": None,
     "bpm": 140, "mood": "节奏驱动/高燃", "pacingFit": ["music", "fast"], "gain": -12,
     "tags": ["混剪", "卡点", "高燃"], "durationSec": 28.8},
    {"file": "warm_pad_120bpm.mp3", "id": "bgm.warm_pad.01", "label": "暖垫 120", "gen": None,
     "bpm": 120, "mood": "温暖铺底/正向", "pacingFit": ["fast", "normal"], "gain": -18,
     "tags": ["知识口播", "温暖", "正向"], "durationSec": 32.0},
    {"file": "lofi_groove_100bpm.mp3", "id": "bgm.lofi.01", "label": "LoFi 慢摇 100", "gen": None,
     "bpm": 100, "mood": "松弛/生活感", "pacingFit": ["normal"], "gain": -18,
     "tags": ["vlog", "松弛", "生活感"], "durationSec": 36.0},
    {"file": "ambient_calm_90bpm.mp3", "id": "bgm.ambient.01", "label": "氛围低吟 90", "gen": None,
     "bpm": 90, "mood": "安静/深度", "pacingFit": ["slow"], "gain": -20,
     "tags": ["教程", "深度", "安静"], "durationSec": 40.0},
    # ---- 自产 12 条(铺满 pacing×mood 矩阵,分册01 §4.4)
    {"file": "bright_fast_128bpm.mp3", "id": "bgm.bright_fast.01", "label": "明亮快线 128", "gen": bgm_bright_fast,
     "bpm": 128, "mood": "明亮/轻快", "pacingFit": ["fast", "music"], "gain": -12,
     "tags": ["快节奏", "明亮", "种草"]},
    {"file": "tech_fast_132bpm.mp3", "id": "bgm.tech_fast.01", "label": "科技脉冲 132", "gen": bgm_tech_fast,
     "bpm": 132, "mood": "科技/紧凑", "pacingFit": ["fast", "music"], "gain": -12,
     "tags": ["科技", "数码", "评测"]},
    {"file": "drive_fast_140bpm.mp3", "id": "bgm.drive_fast.01", "label": "鼓动驱动 140", "gen": bgm_drive_fast,
     "bpm": 140, "mood": "鼓动/热血", "pacingFit": ["fast", "music"], "gain": -12,
     "tags": ["热血", "运动", "燃"]},
    {"file": "tension_normal_110bpm.mp3", "id": "bgm.tension.01", "label": "紧张脉冲 110", "gen": bgm_tension_normal,
     "bpm": 110, "mood": "紧张/压迫", "pacingFit": ["normal", "fast", "music"], "gain": -18,
     "tags": ["紧张", "悬疑", "冲突"]},
    {"file": "suspense_normal_95bpm.mp3", "id": "bgm.suspense.01", "label": "悬疑铺陈 95", "gen": bgm_suspense_normal,
     "bpm": 95, "mood": "悬疑/探究", "pacingFit": ["normal"], "gain": -18,
     "tags": ["悬疑", "解密", "叙事"]},
    {"file": "sad_normal_80bpm.mp3", "id": "bgm.sad.01", "label": "叙事伤感 80", "gen": bgm_sad_normal,
     "bpm": 80, "mood": "悲伤/叙事", "pacingFit": ["normal", "slow"], "gain": -18,
     "tags": ["伤感", "故事", "情感号"]},
    {"file": "bright_normal_105bpm.mp3", "id": "bgm.bright.01", "label": "明亮轻快 105", "gen": bgm_bright_normal,
     "bpm": 105, "mood": "明亮/日常", "pacingFit": ["normal"], "gain": -18,
     "tags": ["日常", "轻快", "知识口播"]},
    {"file": "tech_normal_112bpm.mp3", "id": "bgm.tech.01", "label": "科技中速 112", "gen": bgm_tech_normal,
     "bpm": 112, "mood": "科技/理性", "pacingFit": ["normal", "music"], "gain": -18,
     "tags": ["科技", "教程", "演示"]},
    {"file": "warm_slow_75bpm.mp3", "id": "bgm.warm_slow.01", "label": "温暖慢板 75", "gen": bgm_warm_slow,
     "bpm": 75, "mood": "温暖/治愈", "pacingFit": ["slow"], "gain": -20,
     "tags": ["治愈", "温暖", "慢节奏"]},
    {"file": "sad_slow_70bpm.mp3", "id": "bgm.sad_slow.01", "label": "伤感慢板 70", "gen": bgm_sad_slow,
     "bpm": 70, "mood": "悲伤/深度", "pacingFit": ["slow"], "gain": -20,
     "tags": ["伤感", "深度", "人物"]},
    {"file": "chill_slow_85bpm.mp3", "id": "bgm.chill_slow.01", "label": "闲适慢摇 85", "gen": bgm_chill_slow,
     "bpm": 85, "mood": "闲适/生活", "pacingFit": ["slow", "normal"], "gain": -20,
     "tags": ["闲适", "生活", "vlog"]},
    {"file": "bright_music_128bpm.mp3", "id": "bgm.bright_music.01", "label": "明亮卡点 128", "gen": bgm_bright_music,
     "bpm": 128, "mood": "明亮/卡点", "pacingFit": ["music", "fast"], "gain": -12,
     "tags": ["混剪", "卡点", "明亮"]},
]


# ================================================================ 元素贴图(Pillow 合成)

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

# 尺寸三档(分册01 §4.2:9:16 基准宽 ≤400px 以 @3x 计)
S1X, S2X, S3X = 120, 240, 360

PALETTE = {
    "red": (255, 68, 90, 255), "gold": (255, 196, 40, 255), "blue": (60, 150, 255, 255),
    "green": (60, 200, 120, 255), "white": (255, 255, 255, 255),
    "ink": (24, 26, 32, 255), "pink": (255, 110, 160, 255), "purple": (150, 100, 255, 255),
    "cyan": (80, 220, 230, 255), "orange": (255, 140, 50, 255),
}


def _img(w: int, h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    return im, ImageDraw.Draw(im)


def _star_poly(cx: float, cy: float, r_out: float, r_in: float, points: int, rot: float = -90.0) -> list:
    pts = []
    for i in range(points * 2):
        r = r_out if i % 2 == 0 else r_in
        a = math.radians(rot + i * 180.0 / points)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _arrow_poly(direction: str, s: int) -> list:
    """方向箭头多边形(设计坐标 0..s)。"""
    c, w, hl, hw = s / 2, s * 0.16, s * 0.42, s * 0.24
    base = [(-w, -c + s * 0.08), (w, -c + s * 0.08), (w, c - hl), (hw, c - hl),
            (0, c), (-hw, c - hl), (-w, c - hl)]      # 朝下基准
    rot = {"down": 0, "up": 180, "left": 90, "right": -90,
           "downright": -45, "downleft": 45, "upright": -135, "upleft": 135}[direction]
    a = math.radians(rot)
    ca, sa = math.cos(a), math.sin(a)
    return [((x * ca - y * sa) + c, (x * sa + y * ca) + c) for x, y in base]


def _grad_block(w: int, h: int, c1: tuple, c2: tuple, horizontal: bool = False) -> Image.Image:
    """双色渐变(线性),透明度均匀。"""
    import numpy as _np
    t = _np.linspace(0, 1, w if horizontal else h).reshape(-1, 1)
    a1, a2 = _np.array(c1, float), _np.array(c2, float)
    rows = (1 - t) * a1 + t * a2                    # (N,4)
    if horizontal:
        arr = _np.tile(rows[np.newaxis, :, :], (h, 1, 1))
    else:
        arr = _np.tile(rows[:, np.newaxis, :], (1, w, 1))
    return Image.fromarray(arr.astype("uint8"), "RGBA")


def _radial_glow(s: int, color: tuple, power: float = 2.2) -> Image.Image:
    """径向光斑(高斯式衰减)。"""
    import numpy as _np
    yy, xx = _np.mgrid[0:s, 0:s]
    c = (s - 1) / 2
    r = _np.sqrt((xx - c) ** 2 + (yy - c) ** 2) / c
    alpha = _np.clip(1 - r, 0, 1) ** power
    arr = _np.zeros((s, s, 4), "uint8")
    arr[..., 0], arr[..., 1], arr[..., 2] = color[0], color[1], color[2]
    arr[..., 3] = (alpha * 255).astype("uint8")
    return Image.fromarray(arr, "RGBA")


def _save_within_budget(path: Path, im: Image.Image, budget: int = 300 * 1024) -> None:
    """落盘单张 PNG;超预算(R38:单图 ≤300KB)时降位深重存(量化灰阶/透明级)。"""
    im.save(path)
    if path.stat().st_size <= budget:
        return
    import numpy as _np
    arr = _np.array(im)
    arr[..., :3] = (arr[..., :3] >> 6) << 6          # RGB 降到 4 级
    arr[..., 3] = ((_np.asarray(arr[..., 3], int) * 3) // 255) * 85  # alpha 降到 4 级
    Image.fromarray(arr, "RGBA").save(path, optimize=True)
    if path.stat().st_size > budget:
        raise AssertionError(f"元素超体积预算:{path.name} {path.stat().st_size}B > 300KB")


def _save_variants(im: Image.Image, name: str) -> dict:
    """@1x/@2x/@3x 三档落盘(主文件 = @2x);返回 sizeHint。"""
    base = ELEM_DIR / "png"
    base.mkdir(parents=True, exist_ok=True)
    w2, h2 = im.size
    for scale, target_w in (("1x", S1X), ("2x", S2X), ("3x", S3X)):
        target = im.resize((target_w, max(1, round(h2 * target_w / w2))), Image.LANCZOS)
        suffix = "" if scale == "2x" else f"@{scale}"
        _save_within_budget(base / f"{name}{suffix}.png", target)
    return {"base": "2x", "w2x": w2, "h2x": h2,
            "w1x": S1X, "h1x": max(1, round(h2 * S1X / w2)),
            "w3x": S3X, "h3x": max(1, round(h2 * S3X / w2))}


def draw_badge(kind: str) -> Image.Image:
    s = 300
    im, d = _img(s, s)
    if kind == "new":                       # 星爆角标
        d.polygon(_star_poly(s / 2, s / 2, 145, 96, 12), fill=PALETTE["red"])
        d.polygon(_star_poly(s / 2, s / 2, 96, 60, 12, rot=-78), fill=(255, 255, 255, 60))
    elif kind == "hot":                     # 火苗
        d.pieslice([s * 0.18, s * 0.3, s * 0.82, s * 0.94], 180, 360, fill=PALETTE["orange"])
        d.polygon([(s * 0.5, s * 0.04), (s * 0.82, s * 0.55), (s * 0.18, s * 0.55)], fill=PALETTE["orange"])
        d.pieslice([s * 0.32, s * 0.48, s * 0.68, s * 0.9], 180, 360, fill=PALETTE["gold"])
        d.polygon([(s * 0.5, s * 0.3), (s * 0.68, s * 0.62), (s * 0.32, s * 0.62)], fill=PALETTE["gold"])
    elif kind == "top1":                    # 皇冠
        d.polygon([(s * 0.12, s * 0.68), (s * 0.12, s * 0.34), (s * 0.3, s * 0.5),
                   (s * 0.5, s * 0.2), (s * 0.7, s * 0.5), (s * 0.88, s * 0.34),
                   (s * 0.88, s * 0.68)], fill=PALETTE["gold"])
        d.rectangle([s * 0.12, s * 0.68, s * 0.88, s * 0.8], fill=PALETTE["gold"])
        for px in (0.24, 0.5, 0.76):
            d.ellipse([s * px - 10, s * 0.71 - 10, s * px + 10, s * 0.71 + 10], fill=PALETTE["red"])
    elif kind == "limited":                 # 时钟环(限时)
        d.arc([s * 0.12] * 2 + [s * 0.88] * 2, -90, 250, fill=PALETTE["blue"], width=26)
        d.line([(s * 0.5, s * 0.3), (s * 0.5, s * 0.52), (s * 0.68, s * 0.6)],
               fill=PALETTE["ink"], width=18, joint="curve")
    elif kind == "recommend":               # 竖拇指(推荐)
        d.rounded_rectangle([s * 0.16, s * 0.5, s * 0.4, s * 0.86], 18, fill=PALETTE["blue"])
        d.polygon([(s * 0.42, s * 0.52), (s * 0.58, s * 0.18), (s * 0.7, s * 0.26),
                   (s * 0.62, s * 0.5), (s * 0.78, s * 0.52), (s * 0.78, s * 0.84),
                   (s * 0.42, s * 0.84)], fill=PALETTE["blue"])
    elif kind == "mustsee":                 # 闪电(必看)
        d.polygon([(s * 0.56, s * 0.06), (s * 0.24, s * 0.56), (s * 0.46, s * 0.56),
                   (s * 0.4, s * 0.94), (s * 0.76, s * 0.4), (s * 0.52, s * 0.4)],
                  fill=PALETTE["gold"])
    elif kind == "drygoods":                # 书本(干货)
        d.rounded_rectangle([s * 0.18, s * 0.2, s * 0.82, s * 0.84], 14, fill=PALETTE["green"])
        d.rectangle([s * 0.5, s * 0.2, s * 0.54, s * 0.84], fill=(255, 255, 255, 120))
        d.rectangle([s * 0.26, s * 0.34, s * 0.46, s * 0.4], fill=(255, 255, 255, 160))
    elif kind == "favorite":                # 书签丝带(收藏)
        d.polygon([(s * 0.28, s * 0.12), (s * 0.72, s * 0.12), (s * 0.72, s * 0.88),
                   (s * 0.5, s * 0.68), (s * 0.28, s * 0.88)], fill=PALETTE["pink"])
    return im


def draw_arrow(direction: str) -> Image.Image:
    s = 300
    im, d = _img(s, s)
    d.polygon(_arrow_poly(direction, s), fill=PALETTE["red"])
    return im


def draw_highlight_circle() -> Image.Image:
    s = 300
    im, d = _img(s, s)
    d.ellipse([14, 14, s - 14, s - 14], outline=PALETTE["red"], width=22)
    return im


def draw_highlight_box() -> Image.Image:
    w, h = 400, 260
    im, d = _img(w, h)
    d.rounded_rectangle([12, 12, w - 12, h - 12], 26, outline=PALETTE["gold"], width=20)
    return im


def draw_underline_brush() -> Image.Image:
    w, h = 400, 90
    im, d = _img(w, h)
    # 笔刷质感:多条抖动横线叠加
    import numpy as _np
    rng = _np.random.default_rng(7)
    for i in range(9):
        y = 30 + i * 4 + float(rng.uniform(-2, 2))
        x0 = float(rng.uniform(0, 26))
        x1 = w - float(rng.uniform(0, 26))
        d.line([(x0, y), (x1, y)], fill=(255, 68, 90, 190 - i * 14), width=7)
    return im.filter(ImageFilter.GaussianBlur(0.6))


def draw_progress(color: str, frac: float) -> Image.Image:
    w, h = 400, 56
    im, d = _img(w, h)
    d.rounded_rectangle([8, 8, w - 8, h - 8], h // 2 - 4, fill=(255, 255, 255, 70))
    fill_w = int((w - 16) * frac)
    if fill_w > h - 16:
        d.rounded_rectangle([8, 8, 8 + fill_w, h - 8], h // 2 - 4, fill=PALETTE[color])
    return im


def draw_progress_ring() -> Image.Image:
    s = 300
    im, d = _img(s, s)
    d.arc([20, 20, s - 20, s - 20], 0, 360, fill=(255, 255, 255, 70), width=34)
    d.arc([20, 20, s - 20, s - 20], -90, 155, fill=PALETTE["green"], width=34)
    return im


def draw_countdown_arc() -> Image.Image:
    s = 300
    im, d = _img(s, s)
    d.arc([20, 20, s - 20, s - 20], 0, 360, fill=(255, 255, 255, 70), width=30)
    d.arc([20, 20, s - 20, s - 20], -90, 40, fill=PALETTE["red"], width=30)
    d.ellipse([s * 0.44, s * 0.44, s * 0.56, s * 0.56], fill=PALETTE["red"])
    return im


def draw_counter_frame() -> Image.Image:
    w, h = 300, 180
    im, d = _img(w, h)
    d.rounded_rectangle([10, 10, w - 10, h - 10], 24, fill=(24, 26, 32, 210))
    d.rounded_rectangle([10, 10, w - 10, h - 10], 24, outline=PALETTE["gold"], width=8)
    d.rounded_rectangle([w * 0.14, h * 0.4, w * 0.86, h * 0.62], 10, fill=(255, 255, 255, 60))
    return im


def draw_divider_wave() -> Image.Image:
    w, h = 400, 60
    im, d = _img(w, h)
    pts = []
    for x in range(0, w + 1, 8):
        pts.append((x, h / 2 + 10 * math.sin(x / 26.0)))
    d.line(pts, fill=PALETTE["purple"], width=10, joint="curve")
    d.ellipse([w / 2 - 12, h / 2 - 22, w / 2 + 12, h / 2 + 2], fill=PALETTE["gold"])
    return im


def draw_light_spot() -> Image.Image:
    return _radial_glow(300, (255, 240, 190), power=1.8)


def draw_star_burst() -> Image.Image:
    s = 300
    im, d = _img(s, s)
    d.polygon(_star_poly(s / 2, s / 2, 145, 34, 4), fill=PALETTE["gold"])
    d.polygon(_star_poly(s / 2, s / 2, 90, 22, 4, rot=45), fill=(255, 255, 255, 170))
    return im


def draw_particles() -> Image.Image:
    s = 300
    im, d = _img(s, s)
    import numpy as _np
    rng = _np.random.default_rng(11)
    for _ in range(26):
        x, y = float(rng.uniform(20, s - 20)), float(rng.uniform(20, s - 20))
        r = float(rng.uniform(3, 12))
        col = PALETTE[["cyan", "gold", "white", "pink"][int(rng.integers(0, 4))]]
        d.ellipse([x - r, y - r, x + r, y + r], fill=col)
    return im.filter(ImageFilter.GaussianBlur(0.8))


def draw_grid_base() -> Image.Image:
    s = 400
    im, d = _img(s, s)
    step = s // 8
    for i in range(0, s + 1, step):
        d.line([(i, 0), (i, s)], fill=(255, 255, 255, 46), width=3)
        d.line([(0, i), (s, i)], fill=(255, 255, 255, 46), width=3)
    return im


def draw_noise_overlay() -> Image.Image:
    """噪点覆层:量化到 3 级灰 + 2 级透明(PNG 压得住体积预算,R38)。"""
    import numpy as _np
    rng = _np.random.default_rng(13)
    gray = _np.array([0, 128, 255], dtype=_np.uint8)
    alpha = _np.array([18, 40], dtype=_np.uint8)
    arr = _np.zeros((300, 300, 4), dtype=_np.uint8)
    arr[..., 0] = gray[rng.integers(0, 3, (300, 300))]
    arr[..., 1] = arr[..., 0]
    arr[..., 2] = arr[..., 0]
    arr[..., 3] = alpha[rng.integers(0, 2, (300, 300))]
    return Image.fromarray(arr, "RGBA")


def draw_gradient_block() -> Image.Image:
    im = _grad_block(400, 220, (255, 68, 90, 230), (150, 100, 255, 230))
    return im


def draw_dot_matrix() -> Image.Image:
    s = 300
    im, d = _img(s, s)
    for r in range(5):
        for c in range(5):
            x, y = 36 + c * 57, 36 + r * 57
            d.ellipse([x - 12, y - 12, x + 12, y + 12],
                      fill=(255, 255, 255, 150 - r * 22))
    return im


def draw_ui_like() -> Image.Image:
    s = 300
    im, d = _img(s, s)
    d.ellipse([16, 16, s - 16, s - 16], fill=(255, 68, 90, 235))
    # 心形(两圆+三角)
    d.ellipse([s * 0.22, s * 0.3, s * 0.5, s * 0.55], fill=PALETTE["white"])
    d.ellipse([s * 0.5, s * 0.3, s * 0.78, s * 0.55], fill=PALETTE["white"])
    d.polygon([(s * 0.26, s * 0.47), (s * 0.74, s * 0.47), (s * 0.5, s * 0.76)], fill=PALETTE["white"])
    return im


def draw_ui_follow() -> Image.Image:
    s = 300
    im, d = _img(s, s)
    d.rounded_rectangle([14, 14, s - 14, s - 14], 60, fill=(24, 26, 32, 220))
    # 铃铛
    d.pieslice([s * 0.28, s * 0.24, s * 0.72, s * 0.72], 180, 360, fill=PALETTE["gold"])
    d.rectangle([s * 0.28, s * 0.48, s * 0.72, s * 0.62], fill=PALETTE["gold"])
    d.rounded_rectangle([s * 0.44, s * 0.66, s * 0.56, s * 0.74], 8, fill=PALETTE["gold"])
    # 加号(右上)
    d.rounded_rectangle([s * 0.66, s * 0.12, s * 0.9, s * 0.2], 8, fill=PALETTE["red"])
    d.rounded_rectangle([s * 0.74, s * 0.04, s * 0.82, s * 0.28], 8, fill=PALETTE["red"])
    return im


def draw_ui_comment() -> Image.Image:
    s = 300
    im, d = _img(s, s)
    d.rounded_rectangle([20, 40, s - 20, s * 0.68], 46, fill=PALETTE["blue"])
    d.polygon([(s * 0.3, s * 0.64), (s * 0.46, s * 0.64), (s * 0.28, s * 0.86)], fill=PALETTE["blue"])
    for i in range(3):
        x = s * (0.3 + 0.2 * i)
        d.ellipse([x - 14, s * 0.4 - 14, x + 14, s * 0.4 + 14], fill=PALETTE["white"])
    return im


def draw_ui_share() -> Image.Image:
    s = 300
    im, d = _img(s, s)
    d.polygon(_arrow_poly("right", s), fill=PALETTE["green"])
    d.arc([s * 0.1, s * 0.3, s * 0.9, s * 1.05], 180, 300, fill=PALETTE["green"], width=22)
    return im


def draw_ui_fav() -> Image.Image:
    s = 300
    im, d = _img(s, s)
    d.polygon(_star_poly(s / 2, s / 2 + 8, 132, 56, 5), fill=PALETTE["gold"])
    return im


def draw_ui_next() -> Image.Image:
    w, h = 400, 110
    im2, d2 = _img(w, h)
    d2.rounded_rectangle([8, 8, w - 8, h - 8], 40, fill=(24, 26, 32, 215))
    d2.rounded_rectangle([8, 8, w - 8, h - 8], 40, outline=PALETTE["gold"], width=6)
    arrow = Image.new("RGBA", (70, 70), (0, 0, 0, 0))
    ImageDraw.Draw(arrow).polygon(_arrow_poly("right", 70), fill=PALETTE["gold"])
    im2.alpha_composite(arrow, (w - 96, (h - 70) // 2))
    for i in range(3):
        d2.rounded_rectangle([36, 30 + i * 20, 210 - i * 40, 42 + i * 20], 6,
                             fill=(255, 255, 255, 120 - i * 30))
    return im2


def draw_frame_polaroid() -> Image.Image:
    w, h = 400, 400
    im, d = _img(w, h)
    d.rounded_rectangle([10, 10, w - 10, h - 46], 12, fill=(255, 255, 255, 235))
    d.rectangle([30, 30, w - 30, h - 96], fill=(24, 26, 32, 60))
    d.rectangle([10, h - 46, w - 10, h - 10], fill=(255, 255, 255, 235))
    return im


def draw_frame_film() -> Image.Image:
    w, h = 400, 300
    im, d = _img(w, h)
    d.rounded_rectangle([8, 8, w - 8, h - 8], 10, fill=(24, 26, 32, 230))
    d.rectangle([36, 22, w - 36, h - 22], outline=(255, 255, 255, 200), width=4)
    for i in range(8):
        x = 14 + i * 47
        d.rounded_rectangle([x, 14, x + 30, 32], 4, fill=(255, 255, 255, 190))
        d.rounded_rectangle([x, h - 32, x + 30, h - 14], 4, fill=(255, 255, 255, 190))
    return im


def draw_frame_round() -> Image.Image:
    w, h = 400, 400
    im, d = _img(w, h)
    d.rounded_rectangle([10, 10, w - 10, h - 10], 60, outline=PALETTE["white"], width=16)
    return im


def draw_frame_stroke() -> Image.Image:
    w, h = 400, 400
    im, d = _img(w, h)
    d.rectangle([12, 12, w - 12, h - 12], outline=PALETTE["gold"], width=12)
    d.rectangle([30, 30, w - 30, h - 30], outline=(255, 255, 255, 120), width=4)
    return im


# 序列帧组(有限帧,禁无限循环;每组 12 帧,30fps = 0.4s)
SEQ_FRAMES = 12


def seq_bar_rise(frame_dir: Path) -> None:
    """柱状图升起:4 柱依 ease-out 逐根到位(纯图形,无文字)。"""
    frame_dir.mkdir(parents=True, exist_ok=True)
    w, h = 400, 300
    heights = [0.62, 0.86, 0.44, 0.7]
    colors = ["red", "gold", "blue", "green"]
    for f in range(1, SEQ_FRAMES + 1):
        t = 1 - (1 - f / SEQ_FRAMES) ** 3        # ease-out cubic
        im, d = _img(w, h)
        for i, (hf, col) in enumerate(zip(heights, colors)):
            bh = int((h - 40) * hf * min(1.0, max(0.0, t * 1.4 - i * 0.12)))
            x0 = 36 + i * 92
            if bh > 6:
                d.rounded_rectangle([x0, h - 30 - bh, x0 + 68, h - 30], 12, fill=PALETTE[col])
        d.line([(24, h - 30), (w - 24, h - 30)], fill=(255, 255, 255, 130), width=5)
        im.save(frame_dir / f"{f:04d}.png")


def seq_line_draw(frame_dir: Path) -> None:
    frame_dir.mkdir(parents=True, exist_ok=True)
    w, h = 400, 300
    pts = [(30, 240), (110, 170), (190, 205), (270, 110), (350, 60)]
    for f in range(1, SEQ_FRAMES + 1):
        t = f / SEQ_FRAMES
        im, d = _img(w, h)
        segs = max(1, int(round(t * (len(pts) - 1))))
        path = pts[:segs + 1]
        if t < 1.0 and segs < len(pts) - 1:
            k = t * (len(pts) - 1) - segs
            a, b = pts[segs], pts[segs + 1]
            path = path[:-1] + [(a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)]
        d.line(path, fill=PALETTE["cyan"], width=12, joint="curve")
        if path:
            x, y = path[-1]
            d.ellipse([x - 14, y - 14, x + 14, y + 14], fill=PALETTE["white"])
        im.save(frame_dir / f"{f:04d}.png")


def seq_pie_sweep(frame_dir: Path) -> None:
    frame_dir.mkdir(parents=True, exist_ok=True)
    s = 300
    slices = ((-90, 120, "red"), (30, 100, "gold"), (130, 70, "blue"), (200, 60, "green"))
    for f in range(1, SEQ_FRAMES + 1):
        t = f / SEQ_FRAMES
        im, d = _img(s, s)
        for a0, span, col in slices:
            d.pieslice([24, 24, s - 24, s - 24], a0, a0 + span * t, fill=PALETTE[col])
        d.ellipse([s * 0.36, s * 0.36, s * 0.64, s * 0.64], fill=(0, 0, 0, 0))
        im.save(frame_dir / f"{f:04d}.png")


def seq_gauge_fill(frame_dir: Path) -> None:
    frame_dir.mkdir(parents=True, exist_ok=True)
    s = 300
    for f in range(1, SEQ_FRAMES + 1):
        t = f / SEQ_FRAMES
        im, d = _img(s, s)
        d.arc([28, 48, s - 28, s + 60], 180, 360, fill=(255, 255, 255, 70), width=36)
        d.arc([28, 48, s - 28, s + 60], 180, 180 + int(180 * t), fill=PALETTE["orange"], width=36)
        d.ellipse([s / 2 - 12, s * 0.52 - 12, s / 2 + 12, s * 0.52 + 12], fill=PALETTE["white"])
        im.save(frame_dir / f"{f:04d}.png")


def seq_count_up(frame_dir: Path) -> None:
    """数字滚动底衬:递进色块(文字由花字层负责,元素不内嵌文字)。"""
    frame_dir.mkdir(parents=True, exist_ok=True)
    w, h = 400, 160
    for f in range(1, SEQ_FRAMES + 1):
        t = f / SEQ_FRAMES
        im, d = _img(w, h)
        d.rounded_rectangle([10, 10, w - 10, h - 10], 26, fill=(24, 26, 32, 200))
        d.rounded_rectangle([10, 10, 10 + int((w - 20) * t), h - 10], 26, fill=(255, 196, 40, 120))
        for i in range(3):
            alpha = 160 if (f % SEQ_FRAMES) > i * 3 else 70
            d.rounded_rectangle([40 + i * 110, 52, 130 + i * 110, 108], 12,
                                fill=(255, 255, 255, alpha))
        im.save(frame_dir / f"{f:04d}.png")


def seq_progress_fill(frame_dir: Path) -> None:
    frame_dir.mkdir(parents=True, exist_ok=True)
    w, h = 400, 80
    for f in range(1, SEQ_FRAMES + 1):
        t = f / SEQ_FRAMES
        im, d = _img(w, h)
        d.rounded_rectangle([10, 10, w - 10, h - 10], 30, fill=(255, 255, 255, 70))
        fw = int((w - 20) * t)
        if fw > 40:
            d.rounded_rectangle([10, 10, 10 + fw, h - 10], 30, fill=PALETTE["red"])
        im.save(frame_dir / f"{f:04d}.png")


ELEMENT_SPECS: list[dict] = [
    # 角标/标签 8(纯图形,不内嵌文字——文字走花字/字幕)
    {"file": "png/badge_new.png", "id": "element.badge.new", "label": "NEW 星爆角标", "draw": lambda: draw_badge("new"),
     "tags": ["角标", "上新", "促销"], "usage": ["decor"]},
    {"file": "png/badge_hot.png", "id": "element.badge.hot", "label": "HOT 火焰角标", "draw": lambda: draw_badge("hot"),
     "tags": ["角标", "热门", "爆款"], "usage": ["decor"]},
    {"file": "png/badge_top1.png", "id": "element.badge.top1", "label": "TOP1 皇冠角标", "draw": lambda: draw_badge("top1"),
     "tags": ["角标", "排名", "第一"], "usage": ["decor"]},
    {"file": "png/badge_limited.png", "id": "element.badge.limited", "label": "限时钟环角标", "draw": lambda: draw_badge("limited"),
     "tags": ["角标", "限时", "紧迫"], "usage": ["decor"]},
    {"file": "png/badge_recommend.png", "id": "element.badge.recommend", "label": "推荐拇指角标", "draw": lambda: draw_badge("recommend"),
     "tags": ["角标", "推荐", "好评"], "usage": ["decor"]},
    {"file": "png/badge_mustsee.png", "id": "element.badge.mustsee", "label": "必看闪电角标", "draw": lambda: draw_badge("mustsee"),
     "tags": ["角标", "必看", "重点"], "usage": ["decor"]},
    {"file": "png/badge_drygoods.png", "id": "element.badge.drygoods", "label": "干货书本角标", "draw": lambda: draw_badge("drygoods"),
     "tags": ["角标", "干货", "知识"], "usage": ["decor"]},
    {"file": "png/badge_favorite.png", "id": "element.badge.favorite", "label": "收藏书签角标", "draw": lambda: draw_badge("favorite"),
     "tags": ["角标", "收藏", "引导"], "usage": ["decor"]},
    # 指示/强调 11(8 方向箭头 + 高亮圈/框 + 笔刷)
    *[{"file": f"png/arrow_{d}.png", "id": f"element.arrow.{d.replace('down', 'dn').replace('up', 'up')}",
       "label": f"箭头·{lbl}", "draw": (lambda dd=d: draw_arrow(dd)),
       "tags": ["箭头", "指示", "强调"], "usage": ["decor"]}
      for d, lbl in (("up", "上"), ("down", "下"), ("left", "左"), ("right", "右"),
                     ("upleft", "左上"), ("upright", "右上"), ("downleft", "左下"), ("downright", "右下"))],
    {"file": "png/highlight_circle.png", "id": "element.highlight.circle", "label": "高亮圈", "draw": draw_highlight_circle,
     "tags": ["强调", "圈注", "重点"], "usage": ["decor"]},
    {"file": "png/highlight_box.png", "id": "element.highlight.box", "label": "高亮框", "draw": draw_highlight_box,
     "tags": ["强调", "框选", "重点"], "usage": ["decor"]},
    {"file": "png/underline_brush.png", "id": "element.highlight.brush", "label": "笔刷下划线", "draw": draw_underline_brush,
     "tags": ["强调", "笔刷", "下划线"], "usage": ["decor"]},
    # 进度/计数 6
    {"file": "png/progress_red.png", "id": "element.progress.red", "label": "进度条·红 72%", "draw": lambda: draw_progress("red", 0.72),
     "tags": ["进度", "对比", "红"], "usage": ["data"]},
    {"file": "png/progress_gold.png", "id": "element.progress.gold", "label": "进度条·金 45%", "draw": lambda: draw_progress("gold", 0.45),
     "tags": ["进度", "对比", "金"], "usage": ["data"]},
    {"file": "png/progress_green.png", "id": "element.progress.green", "label": "进度条·绿 88%", "draw": lambda: draw_progress("green", 0.88),
     "tags": ["进度", "对比", "绿"], "usage": ["data"]},
    {"file": "png/progress_ring.png", "id": "element.progress.ring", "label": "环形进度 68%", "draw": draw_progress_ring,
     "tags": ["进度", "环形", "占比"], "usage": ["data"]},
    {"file": "png/countdown_arc.png", "id": "element.progress.countdown", "label": "倒计时弧", "draw": draw_countdown_arc,
     "tags": ["倒计时", "紧迫", "环形"], "usage": ["data"]},
    {"file": "png/counter_frame.png", "id": "element.progress.counter", "label": "计数框底衬", "draw": draw_counter_frame,
     "tags": ["计数", "数据", "底衬"], "usage": ["data"]},
    # 装饰 8
    {"file": "png/divider_wave.png", "id": "element.decor.divider", "label": "波浪分隔线", "draw": draw_divider_wave,
     "tags": ["装饰", "分隔", "过渡"], "usage": ["decor"]},
    {"file": "png/light_spot.png", "id": "element.decor.light_spot", "label": "光斑", "draw": draw_light_spot,
     "tags": ["装饰", "光效", "氛围"], "usage": ["decor"]},
    {"file": "png/star_burst.png", "id": "element.decor.star_burst", "label": "四芒星闪", "draw": draw_star_burst,
     "tags": ["装饰", "星芒", "闪亮"], "usage": ["decor"]},
    {"file": "png/particles.png", "id": "element.decor.particles", "label": "彩色粒子", "draw": draw_particles,
     "tags": ["装饰", "粒子", "活泼"], "usage": ["decor"]},
    {"file": "png/grid_base.png", "id": "element.decor.grid", "label": "网格底", "draw": draw_grid_base,
     "tags": ["装饰", "网格", "科技"], "usage": ["decor"]},
    {"file": "png/noise_overlay.png", "id": "element.decor.noise", "label": "噪点覆层", "draw": draw_noise_overlay,
     "tags": ["装饰", "噪点", "质感"], "usage": ["decor"]},
    {"file": "png/gradient_block.png", "id": "element.decor.gradient", "label": "渐变色块", "draw": draw_gradient_block,
     "tags": ["装饰", "渐变", "色块"], "usage": ["decor"]},
    {"file": "png/dot_matrix.png", "id": "element.decor.dots", "label": "圆点阵", "draw": draw_dot_matrix,
     "tags": ["装饰", "圆点", "阵列"], "usage": ["decor"]},
    # 平台 UI 6
    {"file": "png/ui_like.png", "id": "element.ui.like", "label": "点赞红心", "draw": draw_ui_like,
     "tags": ["平台", "点赞", "引导"], "usage": ["ui"]},
    {"file": "png/ui_follow.png", "id": "element.ui.follow", "label": "关注提醒铃", "draw": draw_ui_follow,
     "tags": ["平台", "关注", "引导"], "usage": ["ui"]},
    {"file": "png/ui_comment.png", "id": "element.ui.comment", "label": "评论气泡", "draw": draw_ui_comment,
     "tags": ["平台", "评论", "互动"], "usage": ["ui"]},
    {"file": "png/ui_share.png", "id": "element.ui.share", "label": "分享箭头", "draw": draw_ui_share,
     "tags": ["平台", "分享", "引导"], "usage": ["ui"]},
    {"file": "png/ui_fav.png", "id": "element.ui.favorite", "label": "收藏星", "draw": draw_ui_fav,
     "tags": ["平台", "收藏", "引导"], "usage": ["ui"]},
    {"file": "png/ui_next.png", "id": "element.ui.next", "label": "下期预告条", "draw": draw_ui_next,
     "tags": ["平台", "预告", "下期"], "usage": ["ui"]},
    # 相框/边框 4
    {"file": "png/frame_polaroid.png", "id": "element.frame.polaroid", "label": "拍立得相框", "draw": draw_frame_polaroid,
     "tags": ["相框", "拍立得", "照片"], "usage": ["decor"]},
    {"file": "png/frame_film.png", "id": "element.frame.film", "label": "胶片框", "draw": draw_frame_film,
     "tags": ["相框", "胶片", "复古"], "usage": ["decor"]},
    {"file": "png/frame_round.png", "id": "element.frame.round", "label": "圆角相框", "draw": draw_frame_round,
     "tags": ["相框", "圆角", "简约"], "usage": ["decor"]},
    {"file": "png/frame_stroke.png", "id": "element.frame.stroke", "label": "描边相框", "draw": draw_frame_stroke,
     "tags": ["相框", "描边", "金框"], "usage": ["decor"]},
]

SEQ_SPECS: list[dict] = [
    {"dir": "seq/bar_rise", "id": "element.seq.bar_rise", "label": "柱状图升起", "gen": seq_bar_rise,
     "tags": ["数据", "柱状图", "入场"], "usage": ["data"]},
    {"dir": "seq/line_draw", "id": "element.seq.line_draw", "label": "折线绘制", "gen": seq_line_draw,
     "tags": ["数据", "折线", "入场"], "usage": ["data"]},
    {"dir": "seq/pie_sweep", "id": "element.seq.pie_sweep", "label": "饼图扫开", "gen": seq_pie_sweep,
     "tags": ["数据", "饼图", "占比"], "usage": ["data"]},
    {"dir": "seq/gauge_fill", "id": "element.seq.gauge_fill", "label": "仪表盘填充", "gen": seq_gauge_fill,
     "tags": ["数据", "仪表盘", "指标"], "usage": ["data"]},
    {"dir": "seq/count_up", "id": "element.seq.count_up", "label": "数字滚动底衬", "gen": seq_count_up,
     "tags": ["数据", "计数", "递进"], "usage": ["data"]},
    {"dir": "seq/progress_fill", "id": "element.seq.progress_fill", "label": "进度条充满", "gen": seq_progress_fill,
     "tags": ["数据", "进度", "入场"], "usage": ["data"]},
]


# ================================================================ 花字模板(基础档 ASS 8 套 + 进阶卡 6 套)

# 元数据放 [Script Info] 的 `; hz-键: 值` 注释行(机器可读,ASS 语法合法)
HUAZI_ASS: list[dict] = [
    {"id": "huazi.keyword.box", "file": "keyword_box.ass", "label": "关键词·方框底衬",
     "usage": ["punchline"], "effect": "box",
     "tags": ["关键词", "底衬", "强调"], "params": {"pad": 10, "color": "&H00E5FF00"}},
    {"id": "huazi.keyword.brush", "file": "keyword_brush.ass", "label": "关键词·笔刷底衬",
     "usage": ["punchline"], "effect": "brush",
     "tags": ["关键词", "笔刷", "手写感"], "params": {"element": "element.highlight.brush"}},
    {"id": "huazi.title.pop", "file": "title_pop.ass", "label": "标题·逐字弹入",
     "usage": ["chapter"], "effect": "pop",
     "tags": ["标题", "弹入", "逐字"], "params": {"overshoot": 1.1, "stepMs": 60}},
    {"id": "huazi.title.slide", "file": "title_slide.ass", "label": "标题·逐行滑入",
     "usage": ["chapter"], "effect": "slide",
     "tags": ["标题", "滑入", "整行"], "params": {"distPx": 80, "durMs": 260}},
    {"id": "huazi.quote.typewriter", "file": "quote_typewriter.ass", "label": "金句·打字机",
     "usage": ["punchline"], "effect": "typewriter",
     "tags": ["金句", "打字机", "逐字"], "params": {"stepMs": 70}},
    {"id": "huazi.data.count", "file": "data_count.ass", "label": "数据·数字滚动",
     "usage": ["punchline"], "effect": "count",
     "tags": ["数据", "滚动", "递进"], "params": {"steps": 5, "stepMs": 90}},
    {"id": "huazi.section.tab", "file": "section_tab.ass", "label": "章节·斜切色块",
     "usage": ["chapter"], "effect": "tab",
     "tags": ["章节", "斜切", "色块"], "params": {"skew": 12, "color": "&H00E5FF00"}},
    {"id": "huazi.ending.subscribe", "file": "ending_subscribe.ass", "label": "片尾·关注引导",
     "usage": ["ending"], "effect": "subscribe",
     "tags": ["片尾", "引导", "关注"], "params": {"fadMs": [200, 400]}},
]


def _huazi_ass_text(spec: dict) -> str:
    """生成一套基础档花字 ASS 模板(元数据注释 + 双 Style + 示例 Dialogue)。

    示例 Dialogue 展示该模板的三类关键标签(底衬 \\bord/\\p1、逐字动画 \\t、淡入 \\fad),
    供 rs_subtitle --huazi 解析 params 后按真实事件重排;样本文本固定,保证确定性。
    """
    p = spec.get("params", {})
    meta = [f"; hz-id: {spec['id']}", f"; hz-label: {spec['label']}",
            f"; hz-usage: {' '.join(spec['usage'])}", f"; hz-effect: {spec['effect']}",
            "; hz-params: " + json.dumps(p, ensure_ascii=False, sort_keys=True),
            "; 说明:基础档花字模板(ADR-0057)。rs_subtitle --huazi 读取本文件元数据,",
            "; 按事件重排 Dialogue;文本匹配/计数前必须剥 {...} override(硬规则 18)。"]
    sample_body = {
        "box": r"{\bord6\bordcolor&H00E5FF00&}关键词",
        "brush": r"{\bord0}关键词",
        "pop": r"{\fscx20\fscy20\t(0,120,\fscx110\fscy110)\t(120,200,\fscx100\fscy100)}关{\t(,60,\fscx110\fscy110)}键词",
        "slide": r"{\move(620,1500,540,1500,0,260)\fad(200,0)}标题行",
        "typewriter": r"{\alpha&HFF&}关{\alpha&H00&}键{\alpha&H00&}词",
        "count": r"{\t(0,90,\fscx104\fscy104)\t(90,180,\fscx100\fscy100)}10086",
        "tab": r"{\bord8\bordcolor&H00E5FF00&\frz-4}章节",
        "subscribe": r"{\fad(200,400)}关注不迷路",
    }[spec["effect"]]
    lines = [
        "[Script Info]",
        *meta,
        "Title: CutFlow huazi template",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: HuaziMain,Source Han Sans SC,92,&H00FFFFFF,&H000000FF,&H00101010,&H80000000,"
        "1,0,0,0,100,100,0,0,1,6,1,2,60,60,500,1",
        "Style: HuaziBack,Source Han Sans SC,92,&H00000000,&H000000FF,&H00000000,&H00000000,"
        "1,0,0,0,100,100,0,0,3,10,0,2,60,60,500,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        f"Dialogue: 1,0:00:00.00,0:00:02.00,HuaziMain,,0,0,0,,{sample_body}",
    ]
    return "\n".join(lines) + "\n"


HUAZI_CARDS: list[dict] = [
    {"id": "huazi.card.title.burst", "label": "进阶卡·标题爆闪", "ratio": "9x16",
     "effect": "burst", "usage": ["chapter"], "tags": ["标题", "爆闪", "片头"]},
    {"id": "huazi.card.keyword.gold", "label": "进阶卡·金字强调", "ratio": "9x16",
     "effect": "gold", "usage": ["punchline"], "tags": ["关键词", "金色", "强调"]},
    {"id": "huazi.card.quote.paper", "label": "进阶卡·金句纸卡", "ratio": "9x16",
     "effect": "paper", "usage": ["punchline"], "tags": ["金句", "纸卡", "文艺"]},
    {"id": "huazi.card.section.tab", "label": "进阶卡·章节斜切", "ratio": "9x16",
     "effect": "tab", "usage": ["chapter"], "tags": ["章节", "斜切", "转场"]},
    {"id": "huazi.card.stat.rise", "label": "进阶卡·数据跃升", "ratio": "9x16",
     "effect": "stat", "usage": ["punchline"], "tags": ["数据", "跃升", "大字"]},
    {"id": "huazi.card.ending.follow", "label": "进阶卡·片尾关注", "ratio": "9x16",
     "effect": "follow", "usage": ["ending"], "tags": ["片尾", "关注", "引导"]},
]


def _huazi_card_html(spec: dict) -> str:
    """artboard 模式 S 源卡(自包含 HTML/CSS,禁 JS 死循环动画;finite)。

    CutFlow 侧只存「可复用源」,渲染走 rs_artboard(分册01 §2 分工:设计生成归
    artboard,素材库只管可复用资产);卡片画布与画幅由 meta 对齐 platforms.json。
    """
    w, h = {"9x16": (1080, 1920)}[spec["ratio"]]
    scenes = {
        "burst": ("<div class='burst'>标题炸场</div>",
                  ".burst{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;"
                  "font-size:150px;font-weight:900;color:#fff;text-shadow:0 0 40px #ff445a,0 6px 0 #b91c3a;"
                  "animation:pop .45s cubic-bezier(.2,1.6,.4,1) both;}"
                  "@keyframes pop{0%{transform:scale(.2);opacity:0}100%{transform:scale(1);opacity:1}}"),
        "gold": ("<div class='gold'><span>关键词</span></div>",
                 ".gold{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;}"
                 ".gold span{font-size:130px;font-weight:900;color:#ffc428;background:#1a1a20;"
                 "padding:.1em .4em;border-radius:24px;transform:rotate(-3deg);"
                 "animation:slidein .4s ease-out both;}"
                 "@keyframes slidein{0%{transform:translateX(80px) rotate(-3deg);opacity:0}"
                 "100%{transform:translateX(0) rotate(-3deg);opacity:1}}"),
        "paper": ("<div class='paper'>把每一天,当作作品来打磨</div>",
                  ".paper{position:absolute;left:80px;right:80px;top:38%;background:#fffdf5;"
                  "border-radius:18px;padding:70px 60px;font-size:88px;line-height:1.5;"
                  "color:#3a3630;box-shadow:0 24px 60px rgba(0,0,0,.35);"
                  "animation:rise .5s ease-out both;}"
                  "@keyframes rise{0%{transform:translateY(60px);opacity:0}100%{transform:none;opacity:1}}"),
        "tab": ("<div class='tab'>第二章</div>",
                ".tab{position:absolute;left:0;top:32%;background:#ffc428;color:#1a1a20;"
                "font-size:110px;font-weight:900;padding:26px 90px 26px 70px;"
                "clip-path:polygon(0 0,100% 0,calc(100% - 48px) 100%,0 100%);"
                "animation:tabin .4s ease-out both;}"
                "@keyframes tabin{0%{transform:translateX(-120px);opacity:0}100%{transform:none;opacity:1}}"),
        "stat": ("<div class='stat'><em>98.6<i>%</i></em><small>好评率</small></div>",
                 ".stat{position:absolute;inset:0;display:flex;flex-direction:column;gap:30px;"
                 "align-items:center;justify-content:center;color:#fff;}"
                 ".stat em{font-style:normal;font-size:220px;font-weight:900;color:#ffc428;"
                 "text-shadow:0 10px 0 rgba(0,0,0,.4);animation:countup .6s ease-out both;}"
                 ".stat i{font-style:normal;font-size:110px}.stat small{font-size:64px;opacity:.85}"
                 "@keyframes countup{0%{transform:scale(.6);opacity:0}100%{transform:scale(1);opacity:1}}"),
        "follow": ("<div class='follow'>关注 · 下期更精彩</div>",
                   ".follow{position:absolute;left:90px;right:90px;bottom:22%;text-align:center;"
                   "font-size:96px;font-weight:900;color:#1a1a20;background:#fff;"
                   "border:8px solid #ffc428;border-radius:999px;padding:34px 0;"
                   "animation:bob 1.2s ease-in-out 2 both;}@keyframes bob{0%,100%{transform:none}"
                   "50%{transform:translateY(-24px)}}"),
    }
    body, css = scenes[spec["effect"]]
    return (f"<!DOCTYPE html>\n<!-- CutFlow 花字进阶卡(artboard 模式 S 源卡,ADR-0057) "
            f"id={spec['id']} 画布 {w}x{h};动画 finite,无 JS -->\n"
            f"<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">\n"
            f"<style>html,body{{margin:0;width:{w}px;height:{h}px;overflow:hidden;"
            f"background:linear-gradient(160deg,#14151c,#232637);"
            f"font-family:'Source Han Sans SC','Microsoft YaHei',sans-serif;position:relative}}{css}</style>\n"
            f"</head><body>{body}</body></html>\n")


# ================================================================ manifest 构建

def ffprobe_duration_ms(path: Path) -> int:
    p = subprocess.run([_ffprobe_bin(), "-v", "error",
                        "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe 失败 {path}: {p.stderr[:200]}")
    return int(round(float(json.loads(p.stdout)["format"]["duration"]) * 1000))


def _ffprobe_bin() -> str:
    cfg_p = REPO / "config.json"
    if cfg_p.is_file():
        try:
            d = json.loads(cfg_p.read_text(encoding="utf-8")).get("ffmpeg_dir", "")
            p = Path(d) / "ffprobe.exe"
            if p.is_file():
                return str(p)
        except (json.JSONDecodeError, OSError):
            pass
    return "ffprobe"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def build_asset_record(kind: str, spec: dict, *, duration_ms: int | None,
                       size_bytes: int | None = None, extra: dict | None = None) -> dict:
    """统一 manifest 条目(四许可字段 + aiGenerated 为硬字段,R45/ADR-0053)。"""
    rel = spec["file"]
    if kind == "sfx":
        src = spec.get("source") or SELF_SOURCE
        lic = spec.get("license") or SELF_LICENSE
        attr = spec.get("attr") or SELF_ATTRIBUTION
        ai = src == SELF_SOURCE
    else:
        src, lic, attr, ai = SELF_SOURCE, SELF_LICENSE, SELF_ATTRIBUTION, True
    path = ASSETS / rel
    rec = {
        "id": spec["id"], "kind": kind, "label": spec["label"], "file": rel,
        "tags": spec["tags"],
        # BGM 无显式 usage 时默认 emotion(情绪铺底;枚举见分册01 §3)
        "usage": spec.get("usage") or ["emotion"],
        "durationMs": duration_ms if duration_ms is not None else 0,
        "gainHintDb": spec.get("gain", 0),
        "sha256": sha256_of(path),
        "source": src, "license": lic, "commercial": True, "attribution": attr,
        "aiGenerated": ai,
    }
    if spec.get("mood"):
        rec["mood"] = spec["mood"]
    if kind == "bgm":
        rec["pacingFit"] = spec["pacingFit"]
        rec["bpm"] = spec["bpm"]
    if kind == "element":
        rec["sizeHint"] = spec.get("sizeHint")
        rec["anchorHint"] = "center"
        if spec.get("variant3x"):
            rec["variants"] = spec["variant3x"]
    if extra:
        rec.update(extra)
    return rec


def synth_all() -> None:
    """全量合成四类素材(幂等;既有 Mixkit 7 条不重合成只登记)。"""
    print("[1/5] sfx …")
    for spec in SFX_SPECS:
        dst = SFX_DIR / spec["file"]
        if spec["gen"] is not None:
            encode_mp3(normalize(spec["gen"]()), dst, kbps=96)
        size = dst.stat().st_size
        assert size <= 200 * 1024, f"音效超体积预算:{dst.name} {size}B > 200KB"
    print("[2/5] bgm …")
    for spec in BGM_SPECS:
        dst = BGM_DIR / spec["file"]
        if spec["gen"] is not None:
            x = spec["gen"]()
            encode_mp3(x, dst, kbps=128, stereo=True)
        size = dst.stat().st_size
        assert size <= 3 * 1024 * 1024, f"BGM 超体积预算:{dst.name} {size}B > 3MB"
    print("[3/5] elements …")
    for spec in ELEMENT_SPECS:
        im = spec["draw"]()
        hint = _save_variants(im, Path(spec["file"]).stem)
        spec["sizeHint"] = hint
        spec["variant3x"] = [f"png/{Path(spec['file']).stem}@1x.png",
                             f"png/{Path(spec['file']).stem}@3x.png"]
        p = ELEM_DIR / spec["file"]
        size = max(p.stat().st_size,
                   *[(ELEM_DIR / v).stat().st_size for v in spec["variant3x"]])
        assert size <= 300 * 1024, f"元素超体积预算:{spec['file']} {size}B > 300KB"
    for spec in SEQ_SPECS:
        fdir = ELEM_DIR / spec["dir"]
        spec["gen"](fdir)
        size = max(q.stat().st_size for q in fdir.glob("*.png"))
        assert size <= 300 * 1024, f"序列帧超体积预算:{spec['dir']} {size}B > 300KB"
    print("[4/5] huazi …")
    (HUAZI_DIR / "ass").mkdir(parents=True, exist_ok=True)
    for spec in HUAZI_ASS:
        (HUAZI_DIR / "ass" / spec["file"]).write_text(_huazi_ass_text(spec), encoding="utf-8")
    (HUAZI_DIR / "cards").mkdir(parents=True, exist_ok=True)
    for spec in HUAZI_CARDS:
        cdir = HUAZI_DIR / "cards" / "_".join(spec["id"].split(".")[2:])
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "card.html").write_text(_huazi_card_html(spec), encoding="utf-8")
        (cdir / "card.meta.json").write_text(json.dumps(
            {"id": spec["id"], "label": spec["label"], "ratio": spec["ratio"],
             "effect": spec["effect"], "usage": spec["usage"],
             "medium": "artboard-card", "renderVia": "rs_artboard"},
            ensure_ascii=False, indent=1), encoding="utf-8")
    print("[5/5] manifest …")
    build_manifest()


def _element_size_hint(stem: str) -> dict:
    """从盘面三档 PNG 实测尺寸导出 sizeHint(幂等:--manifest-only 不依赖合成态)。"""
    base = ELEM_DIR / "png"
    w1, h1 = Image.open(base / f"{stem}@1x.png").size
    w2, h2 = Image.open(base / f"{stem}.png").size
    w3, h3 = Image.open(base / f"{stem}@3x.png").size
    return {"base": "2x", "w1x": w1, "h1x": h1, "w2x": w2, "h2x": h2,
            "w3x": w3, "h3x": h3}


def build_manifest() -> None:
    """重算统一 manifest(ffprobe 实测 + sha256)+ bgm 兼容件。"""
    assets: list[dict] = []
    for spec in SFX_SPECS:
        p = SFX_DIR / spec["file"]
        # 盘面用组内文件名,manifest 的 file 字段是相对 assets/ 的路径(带 sfx/ 前缀)
        assets.append(build_asset_record(
            "sfx", {**spec, "file": f"sfx/{spec['file']}"},
            duration_ms=ffprobe_duration_ms(p)))
    for spec in BGM_SPECS:
        p = BGM_DIR / spec["file"]
        dur = ffprobe_duration_ms(p)
        assets.append(build_asset_record(
            "bgm", {**spec, "file": f"bgm/{spec['file']}"}, duration_ms=dur,
            extra={"durationSec": round(dur / 1000, 1)}))
    for spec in ELEMENT_SPECS:
        stem = Path(spec["file"]).stem
        hint = _element_size_hint(stem)
        assets.append(build_asset_record(
            "element", {**spec, "file": f"elements/{spec['file']}"}, duration_ms=0,
            extra={"sizeHint": hint, "anchorHint": "center",
                   "variants": [f"elements/png/{stem}@1x.png",
                                f"elements/png/{stem}@3x.png"]}))
    for spec in SEQ_SPECS:
        extra = {"seqDir": f"elements/{spec['dir']}", "frameCount": SEQ_FRAMES,
                 "fps": 30, "anchorHint": "center"}
        assets.append(build_asset_record(
            "element", {**spec, "file": f"elements/{spec['dir']}/0001.png"},
            duration_ms=SEQ_FRAMES * 1000 // 30, extra=extra))
    for spec in HUAZI_ASS:
        assets.append(build_asset_record(
            "huazi", {**spec, "file": f"huazi/ass/{spec['file']}"},
            duration_ms=0, extra={"medium": "ass", "effect": spec["effect"],
                                  "params": spec["params"]}))
    for spec in HUAZI_CARDS:
        rel = "huazi/cards/" + "_".join(spec["id"].split(".")[2:]) + "/card.html"
        assets.append(build_asset_record(
            "huazi", {**spec, "file": rel}, duration_ms=0,
            extra={"medium": "artboard-card", "effect": spec["effect"],
                   "ratio": spec["ratio"], "renderVia": "rs_artboard"}))
    doc = {"version": 1, "generatedAt": GENERATED_AT, "assets": assets}
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_name(MANIFEST.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(MANIFEST)
    # bgm 兼容件(旧读口 rs_intent.bgm_library_pick 仍可用)
    tracks = []
    for a in assets:
        if a["kind"] != "bgm":
            continue
        tracks.append({"name": a["label"], "id": a["id"], "file": Path(a["file"]).name,
                       "durationSec": a.get("durationSec"), "bpm": a.get("bpm"),
                       "mood": a.get("mood", ""), "pacingFit": a["pacingFit"],
                       "gainHintDb": a["gainHintDb"], "commercial": a["commercial"]})
    bgm_compat = {
        "version": 2,
        "license": SELF_LICENSE,
        "engine": "ffmpeg-synth",
        "note": ("M12 兼容件:由统一索引 assets/manifest.json 派生(kind=bgm),"
                 "真相源在统一索引;rs_intent 旧读口继续可用。禁手改。"),
        "tracks": tracks,
    }
    compat = BGM_DIR / "manifest.json"
    tmp2 = compat.with_name(compat.name + ".tmp")
    tmp2.write_text(json.dumps(bgm_compat, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp2.replace(compat)
    kinds = {}
    for a in assets:
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    print(f"manifest 完成:{kinds} 共 {len(assets)} 条")


# ================================================================ 字体表(链接 artboard,不自建)

def _font_family(dir_path: Path, fallback: str) -> tuple[str, str]:
    """读目录内代表款字体文件的家族名(供 ASS Style 用)。

    Pillow FreeType 直读字体内部名,零猜测;读不出时回退「文件名清洗」并标注。
    返回 (family, file);目录无字体文件时返回 ("", "")——由调用方决定降级。
    """
    files = sorted([*dir_path.glob("*.ttf"), *dir_path.glob("*.otf"),
                    *dir_path.glob("*.ttc")])
    if not files:
        return "", ""
    f = files[0]
    try:
        from PIL import ImageFont
        fam = ImageFont.truetype(str(f), 12).getname()[0]
    except Exception:  # noqa: BLE001 — Pillow/FreeType 失败 → 文件名清洗兜底
        stem = f.stem
        stem = re.sub(r"-(VF|Regular|Bold|Light|Medium)$", "", stem)
        fam = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem)
    return fam, f.name


def build_fonts_json() -> None:
    """由 artboard fonts/README.md 生成 templates/fonts.json(逐键一致,禁手改)。"""
    if not FONTS_README.is_file():
        raise SystemExit(f"找不到 artboard 字体总目录:{FONTS_README}(检查工作区级 artboard 技能)")
    text = FONTS_README.read_text(encoding="utf-8")
    fonts = []
    for m in re.finditer(r"^\| \[[\w-]+\]([\w-]+)/INTRO\.md \| (.+?) \| (.+?) \| (.+?) \| (.+?) \|",
                         text, re.M):
        keywords = [k.strip() for k in m.group(4).split(",") if k.strip()]
        weights = [w.strip() for w in m.group(5).split("/") if w.strip()]
        fam, fam_file = _font_family(ARTBOARD_LOCKED / "fonts" / m.group(1), m.group(2))
        entry = {"dir": m.group(1), "name": m.group(2).strip(),
                 "category": m.group(3).strip(),
                 "keywords": keywords, "weights": weights}
        if fam:
            entry["family"] = fam       # 字体内部家族名(ASS Style 的 Fontname 查这里)
            entry["file"] = fam_file    # artboard 代表款文件(缺席=未下载,按需 fetch_font)
        fonts.append(entry)
    if len(fonts) < 20:
        raise SystemExit(f"artboard README 解析异常:仅 {len(fonts)} 款,预期 ≥20(表格格式变了?)")
    doc = {
        "version": 1,
        "_doc": ("字体索引与对拍表(分册01 §5/ADR-0053):由 artboard fonts/README.md 生成,"
                 "唯一真相源在 artboard;**禁手改**——重跑 tools/synth_assets.py --fonts-only。"),
        "artboardLockedPath": str(ARTBOARD_LOCKED),
        "generatedFrom": "artboard/fonts/README.md",
        "default": {
            "subtitle": "source-han-sans",
            "subtitleFallback": "Noto Sans SC",
            "note": ("rs_subtitle 查 default.subtitle 对应字体的家族名;该字体在"
                     "artboard fonts/ 代表款缺失时回退 subtitleFallback,均缺则"
                     "降级内置兜底并留痕 fontDegraded。"),
        },
        "fonts": fonts,
    }
    FONTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = FONTS_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(FONTS_JSON)
    print(f"fonts.json 完成:{len(fonts)} 款(源:{FONTS_README})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--fonts-only", action="store_true")
    ap.add_argument("--manifest-only", action="store_true")
    a = ap.parse_args()
    if a.fonts_only:
        build_fonts_json()
        return 0
    if a.manifest_only:
        build_manifest()
        return 0
    if a.all or not (a.fonts_only or a.manifest_only):
        synth_all()
        build_fonts_json()
    return 0


if __name__ == "__main__":
    sys.exit(main())
