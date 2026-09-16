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


def _ffmpeg() -> str | None:
    try:
        import rs_common
        cand = rs_common.ffmpeg_bin()
    except SystemExit:
        cand = "ffmpeg"
    return shutil.which(cand) or (cand if Path(cand).is_file() else None)


import shutil  # noqa: E402

FFMPEG = _ffmpeg()
CFG = {"ffmpeg_dir": str(Path(FFMPEG).parent)} if FFMPEG else {}
needs_ff = pytest.mark.skipif(not FFMPEG, reason="本机没有 ffmpeg,跳过")


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


@needs_ff
def test_calibrate_fixes_systematic_offset(tmp_path):
    """验收:对齐精度——+150ms 系统性偏移校准后中位偏差 ≤40ms(基线 140ms,改善 ≥70%)。"""
    import rs_align
    wl, wav = _make_offset_wl(tmp_path, bias_ms=150)
    env, win_ms = rs_align.energy_envelope(wav, CFG)
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
    doc, rep = rs_align.calibrate_wordline(json.loads(json.dumps(wl)), wav, CFG)
    after = med_off(doc)
    assert before >= 100, f"偏移素材基线应 ≥100ms:{before}"
    assert after <= 40, f"校准后中位偏差应 ≤40ms:{after}(before={before})"
    assert rep["snapped"] >= 3
    assert doc.get("calibrated") is True
    # 单调性守护
    chars = doc["chars"]
    assert all(chars[i]["endMs"] <= chars[i + 1]["startMs"] + 1 for i in range(len(chars) - 1))


@needs_ff
def test_calibrate_noop_on_aligned(tmp_path):
    """无偏移素材:校准不制造偏差(min_shift 抖动保护)。"""
    import rs_align
    wl, wav = _make_offset_wl(tmp_path, bias_ms=5)
    doc, rep = rs_align.calibrate_wordline(json.loads(json.dumps(wl)), wav, CFG)
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
                                           CFG)
    assert "note" in rep or rep.get("snapped", 0) == 0


@needs_ff
def test_corrupt_media_fails_loudly(tmp_path):
    """异常:损坏输入文件 → 明确报错,不崩溃、不出损坏产物。"""
    import rs_align
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"\x00" * 512)
    with pytest.raises(SystemExit):
        rs_align.energy_envelope(bad, CFG)
