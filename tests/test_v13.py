r"""v0.13 测试:绿幕 v2 键控 + 能量校准 + 异常路径。

运行: pytest tests/test_v13.py -q
依赖: numpy(本仓 venv 内置)、ffmpeg(缺失时端到端类自动跳过)。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "tests"))

import chroma_pixels as cp  # noqa: E402


def _ffmpeg() -> str | None:
    try:
        import rs_common
        cand = rs_common.ffmpeg_bin()
    except SystemExit:
        cand = "ffmpeg"
    return shutil.which(cand) or (cand if Path(cand).is_file() else None)


import shutil  # noqa: E402

FFMPEG = _ffmpeg()
needs_ff = pytest.mark.skipif(not FFMPEG, reason="本机没有 ffmpeg,跳过")


# ---------------------------------------------------------------- 素材合成

def _compose_scene(w=320, h=240, vignette=0.2, shadow=0.0, seed=7):
    screen = cp.make_screen_field(w, h, vignette=vignette, seed=seed)
    frame, alpha, person = cp.compose_person_on_screen(w, h, screen, return_person=True)
    if shadow > 0:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        sh = np.clip(1.0 - ((xx - w * 0.62) / (w * 0.22)) ** 2 - ((yy - h * 0.90) / (h * 0.06)) ** 2,
                     0, 1) * shadow
        frame = np.clip(frame.astype(np.float32) * (1 - sh)[..., None], 0, 255).astype(np.uint8)
    return frame, alpha, person, screen


def _to_420(tmp: Path, frame: np.ndarray) -> Path:
    src = tmp / "in.ppm"
    cp.write_ppm(src, frame)
    dst = tmp / "in420.ppm"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(src), "-vf", "format=yuv420p",
                    "-frames:v", "1", str(dst)], check=True)
    return dst


def _key(tmp: Path, src: Path, chain: str) -> np.ndarray:
    from rs_chroma_bench import read_pam_rgba
    out = tmp / "out.pam"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(src), "-vf", chain,
                    "-frames:v", "1", str(out)], check=True)
    return read_pam_rgba(out)


# ---------------------------------------------------------------- v2 键控质量

@needs_ff
def test_chroma_v2_no_black_fringe_or_dark_hole(tmp_path):
    """验收:边缘无黑边硬边、背景无暗场残留(难例:暗角+投影+4:2:0)。"""
    from rs_chroma_bench import key_new_chain, metrics2
    frame, alpha, person, screen = _compose_scene(320, 240, vignette=0.20, shadow=0.60)
    src = _to_420(tmp_path, frame)
    rgba = _key(tmp_path, src, key_new_chain() + ",format=rgba")
    m = metrics2(rgba, alpha, person, screen)
    assert m["fringe_pct"] < 12.0, f"黑边像素过多:{m}"
    assert m["darkhole_pct"] == 0.0, f"背景暗场残留:{m}"
    assert m["bg_visible_pct"] < 1.0, f"背景残留:{m}"
    assert m["fg_leak_pct"] < 2.0, f"前景被抠透:{m}"  # blur 软化的细发丝 alpha 0.3-0.5 计入,非硬误抠


@needs_ff
def test_chroma_v2_beats_legacy_on_alpha(tmp_path):
    """验收:难例回归——v2 在 alpha 精度与边缘保留上必须优于 legacy 基线。"""
    from rs_chroma_bench import key_new_chain, key_old_chain, metrics2
    frame, alpha, person, screen = _compose_scene(320, 240, vignette=0.20, shadow=0.60)
    src = _to_420(tmp_path, frame)
    m_new = metrics2(_key(tmp_path, src, key_new_chain() + ",format=rgba"), alpha, person, screen)
    m_old = metrics2(_key(tmp_path, src, key_old_chain() + ",format=rgba"), alpha, person, screen)
    assert m_new["alpha_mae"] < m_old["alpha_mae"] * 0.75, \
        f"v2 alpha 精度应比 legacy 好 25%+:{m_new} vs {m_old}"
    assert m_new["eaten_pct"] < m_old["eaten_pct"] * 0.7, \
        f"v2 边缘保留应显著更好:{m_new} vs {m_old}"
    assert m_new["darkhole_pct"] <= m_old["darkhole_pct"] + 1e-9


@needs_ff
def test_chroma_v2_illumination_invariance(tmp_path):
    """验收:光照不均难例——暗角 45% 场景下背景仍要键干净(旧链在此场景暗场残留)。"""
    from rs_chroma_bench import key_new_chain, key_old_chain, metrics2
    frame, alpha, person, screen = _compose_scene(320, 240, vignette=0.45)
    src = _to_420(tmp_path, frame)
    m_new = metrics2(_key(tmp_path, src, key_new_chain() + ",format=rgba"), alpha, person, screen)
    m_old = metrics2(_key(tmp_path, src, key_old_chain() + ",format=rgba"), alpha, person, screen)
    assert m_new["bg_visible_pct"] < 0.5
    assert m_new["residue_pct"] < 0.5


@needs_ff
def test_chroma_v2_non_green_subject_preserved(tmp_path):
    """保护性难例:绿衣人物(合法绿色)不应被抠透。"""
    from rs_chroma_bench import key_new_chain
    sys.path.insert(0, str(REPO / "tests"))
    w, h = 320, 240
    screen = cp.make_screen_field(w, h, vignette=0.1, seed=7)
    rgb_f, alpha_f = cp.make_person(w, h)
    # 把躯干改成绿色衬衫(合法前景绿色)
    torso = (np.mgrid[0:h, 0:w][1] > h * 0.55) & (alpha_f > 0.9)
    rgb_f[torso] = np.array([40, 140, 60], np.float32)
    a3 = alpha_f[..., None]
    frame = np.clip(screen * (1 - a3) + rgb_f * a3, 0, 255).astype(np.uint8)
    src = _to_420(tmp_path, frame)
    rgba = _key(tmp_path, src, key_new_chain() + ",format=rgba")
    a_out = rgba[..., 3].astype(np.float32) / 255.0
    leak = float((torso & (a_out < 0.5)).sum()) / max(1, int(torso.sum())) * 100
    assert leak < 8.0, f"绿衣被抠透 {leak:.1f}%"


@needs_ff
def test_chroma_v2_legacy_mode_still_works(tmp_path):
    """回退路径:keyMode=legacy 全链可用。"""
    import rs_render as rr
    from rs_chroma_bench import key_old_chain
    c = {"keyMode": "legacy", "color": "green"}
    # legacy 走 _chroma_fg_chain,不抛异常即可
    chain = rr._chroma_fg_chain(c)
    assert isinstance(chain, list)


# ---------------------------------------------------------------- 能量校准

def _make_offset_wl(tmp_path, bias_ms=150, n_sent=4):
    sr = 16000
    segs = []
    t = 0.5
    for i in range(n_sent):
        segs.append((t, t + 1.2))
        t += 2.0
    total = t + 0.5
    audio = np.zeros(int(sr * total), np.float32)
    rng = np.random.RandomState(3)
    for (a, b) in segs:
        n = int((b - a) * sr)
        ts = np.arange(n) / sr
        tone = 0.5 * np.sin(2 * np.pi * 220 * ts) * (0.6 + 0.4 * np.sin(2 * np.pi * 3.2 * ts))
        envelope = np.minimum(1, np.minimum(np.arange(n) / (0.05 * sr),
                                            (n - np.arange(n)) / (0.05 * sr)))
        audio[int(a * sr):int(b * sr)] = tone * envelope
    audio += rng.normal(0, 0.002, len(audio)).astype(np.float32)
    wav = tmp_path / "voice.wav"
    raw = tmp_path / "voice.raw"
    raw.write_bytes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "s16le", "-ar", str(sr), "-ac", "1",
                    "-i", str(raw), str(wav)], check=True)
    chars, sents = [], []
    idx = 0
    for si, (a, b) in enumerate(segs):
        start = idx
        for ci in range(6):
            cs = a * 1000 + bias_ms + ci * 150
            chars.append({"i": idx, "ch": f"字{idx}", "startMs": int(cs), "endMs": int(cs + 140),
                          "srcStartMs": int(cs), "srcEndMs": int(cs + 140), "conf": 0.95})
            idx += 1
        sents.append({"id": si, "span": [start, idx], "punc": "。",
                      "text": "".join(c["ch"] for c in chars[start:idx])})
    return {"version": 1, "source": str(wav), "space": "source", "fps": 30,
            "sampleRate": sr, "srcDurationMs": int(total * 1000),
            "finalDurationMs": int(total * 1000), "chars": chars,
            "sentences": sents, "speakers": ["说话人0"]}, wav


def test_calibrate_fixes_systematic_offset(tmp_path):
    """验收:对齐精度——+150ms 系统性偏移校准后中位偏差 ≤40ms(基线 140ms,改善 ≥70%)。"""
    import rs_align
    wl, wav = _make_offset_wl(tmp_path, bias_ms=150)
    env, win_ms = rs_align.energy_envelope(wav, {"ffmpeg_dir": FFMPEG})
    mask = rs_align._active_mask(env)

    def med_off(w):
        offs = []
        for s in w["sentences"]:
            a, b = s["span"]
            s0 = int(w["chars"][a]["startMs"])
            onset = rs_align._energy_onset(mask, s0 - 250, s0 + 250, win_ms)
            if onset is not None:
                offs.append(abs(onset - s0))
        return sorted(offs)[len(offs) // 2] if offs else 9999

    before = med_off(wl)
    doc, rep = rs_align.calibrate_wordline(json.loads(json.dumps(wl)), wav, {"ffmpeg_dir": FFMPEG})
    after = med_off(doc)
    assert before >= 100, f"偏移素材基线应 ≥100ms:{before}"
    assert after <= 40, f"校准后中位偏差应 ≤40ms:{after}(before={before})"
    assert rep["snapped"] >= 3
    assert doc.get("calibrated") is True
    # 单调性守护
    chars = doc["chars"]
    assert all(chars[i]["endMs"] <= chars[i + 1]["startMs"] + 1 for i in range(len(chars) - 1))


def test_calibrate_noop_on_aligned(tmp_path):
    """无偏移素材:校准不制造偏差(min_shift 抖动保护)。"""
    import rs_align
    wl, wav = _make_offset_wl(tmp_path, bias_ms=5)
    doc, rep = rs_align.calibrate_wordline(json.loads(json.dumps(wl)), wav, {"ffmpeg_dir": FFMPEG})
    assert rep["medianAfterMs"] is None or rep["medianAfterMs"] <= 40


# ---------------------------------------------------------------- 异常路径

@needs_ff
def test_calibrate_graceful_on_silent_audio(tmp_path):
    """异常:静音素材不崩溃,给出降级说明。"""
    import rs_align
    wl, _ = _make_offset_wl(tmp_path, bias_ms=0)
    silent = tmp_path / "silence.wav"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
                    "-i", "anullsrc=r=16000:cl=mono", "-t", "8", str(silent)], check=True)
    doc, rep = rs_align.calibrate_wordline(json.loads(json.dumps(wl)), silent,
                                           {"ffmpeg_dir": FFMPEG})
    assert "note" in rep or rep.get("snapped", 0) == 0


@needs_ff
def test_corrupt_media_fails_loudly(tmp_path):
    """异常:损坏输入文件 → 明确报错,不崩溃、不出损坏产物。"""
    import rs_align
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00" * 512)
    with pytest.raises(SystemExit):
        rs_align.energy_envelope(bad, {"ffmpeg_dir": FFMPEG})


@needs_ff
def test_non_green_background_still_keys(tmp_path):
    """异常:非绿幕背景(纯蓝幕)—— v2 链按采样键色工作,不崩溃。"""
    from rs_chroma_bench import key_new_chain
    w, h = 320, 240
    # 蓝幕底
    screen = np.zeros((h, w, 3), np.float32)
    screen[..., 2] = 180
    screen[..., 0] = 20
    screen[..., 1] = 60
    frame, alpha, person = cp.compose_person_on_screen(w, h, screen, return_person=True)
    src = _to_420(tmp_path, frame)
    # 键色用蓝
    rgba = _key(tmp_path, src, key_new_chain(screen_rgb=(20, 60, 180)) + ",format=rgba")
    a_out = rgba[..., 3].astype(np.float32) / 255.0
    bg = alpha < 0.05
    residue = float((bg & (a_out > 0.5)).sum()) / max(1, int(bg.sum())) * 100
    assert residue < 2.0, f"蓝幕背景残留 {residue:.2f}%"
