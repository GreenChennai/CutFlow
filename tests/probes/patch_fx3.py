# -*- coding: utf-8 -*-
"""fixture v3:头部运动幅度直接跟随语音包络(强口型耦合,模拟真实说话)。"""
from pathlib import Path

REPO = Path(r"E:\平日资料\GitHub\CutFlow")
p = REPO / "tests" / "make_fixtures.py"
src = p.read_text(encoding="utf-8")

old = '''def build_base(tmp: Path):
    wavs, timeline, total = sapi_tts(tmp)'''
new = '''def _voice_envelope(tmp: Path, voice_wav: Path, total: float) -> np.ndarray:
    """语音 RMS 包络(30ms 步),供画面运动耦合。"""
    import array as _array
    raw = voice_wav.read_bytes()
    samples = _array.array("h")
    samples.frombytes(raw[:len(raw) // 2 * 2])
    win = SR * 30 // 1000
    env = []
    for i in range(len(samples) // win):
        chunk = samples[i * win:(i + 1) * win]
        acc = sum(s * s for s in chunk)
        env.append((acc / win) ** 0.5)
    peak = max(env) or 1.0
    return np.asarray([v / peak for v in env], dtype=np.float32)


def build_base(tmp: Path):
    wavs, timeline, total = sapi_tts(tmp)'''
assert old in src, "build_base sig not found"
src = src.replace(old, new, 1)

# base_wav 生成后算包络,画面运动改用包络驱动
old2 = '''    base_wav = tmp / "voice.wav"
    subprocess.run([FF, "-v", "error", "-y", *inputs, "-filter_complex", ";".join(fc),
                    "-map", "[aout]", "-ar", str(SR), str(base_wav)], check=True)
    # 画面:说话时头部快速移动
    w, h, fps = 320, 240, 24
    n_frames = int(total * fps)
    frames_dir = tmp / "frames"
    frames_dir.mkdir(exist_ok=True)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    for i in range(n_frames):
        t = i / fps
        speaking = any(tl["start"] - 0.2 <= t <= tl["start"] + tl["dur"] + 0.2 for tl in timeline)
        speed = 3.2 if speaking else 0.5
        cx = w * 0.5 + np.sin(t * speed) * w * 0.07
        cy = h * 0.4 + np.cos(t * speed * 0.8) * h * 0.05'''
new2 = '''    base_wav = tmp / "voice.wav"
    subprocess.run([FF, "-v", "error", "-y", *inputs, "-filter_complex", ";".join(fc),
                    "-map", "[aout]", "-ar", str(SR), str(base_wav)], check=True)
    voice_env = _voice_envelope(tmp, base_wav, total)
    # 画面:头部运动幅度 = 语音包络(强口型耦合,模拟真实说话的唇动/头部动作)
    w, h, fps = 320, 240, 24
    n_frames = int(total * fps)
    frames_dir = tmp / "frames"
    frames_dir.mkdir(exist_ok=True)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    env_at = np.interp(np.arange(n_frames) / fps * 1000.0 / 30.0,
                       np.arange(len(voice_env)), voice_env)
    for i in range(n_frames):
        t = i / fps
        amp = 0.15 + 0.85 * float(env_at[i])      # 静默时 15% 微动,说话时满幅
        cx = w * 0.5 + np.sin(t * 6.0) * w * 0.07 * amp
        cy = h * 0.4 + np.cos(t * 4.8) * h * 0.05 * amp'''
assert old2 in src, "motion block not found"
src = src.replace(old2, new2, 1)
p.write_text(src, encoding="utf-8")
print("fixture v3 strong coupling ok")
