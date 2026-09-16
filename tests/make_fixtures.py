# -*- coding: utf-8 -*-
"""注入缺陷回归用例集 v2:SAPI TTS 真实语音 + 画面运动视频。

基线:4 句真实中文语音(Windows SAPI)+ 头部运动画面 + 正确 ASS。
缺陷:
  FIX1 错位字幕:ASS +0.5s
  FIX2 错位音轨:音轨/字幕同延 0.45s(画面不动 → D2 抓)
  FIX3 错剪腰斩:第 2 句句中剪 0.8s + 0.1s 硬接(→ D3b 抓)
"""
import base64
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(r"E:\平日资料\GitHub\CutFlow")
FF = r"E:\Tools\ffmpeg\bin\ffmpeg.exe"
OUT = REPO / "tests" / "fixtures" / "diagnosis"
OUT.mkdir(parents=True, exist_ok=True)
SR = 24000
SENTS = ["今天我们讲桌面运维的第一课。", "遇到蓝屏先不要慌。", "第一步检查内存条。",
         "然后重新插拔再开机。"]


def sapi_tts(tmp: Path) -> tuple[Path, list[dict]]:
    """逐句 TTS,返回 (拼接 wav, 每句时间)。"""
    wavs = []
    timeline = []
    t = 0.5
    for i, text in enumerate(SENTS):
        w = tmp / f"s{i}.wav"
        inner = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$voices = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' }; "
            "if ($voices) { $s.SelectVoice($voices[0].VoiceInfo.Name) }; "
            f"$s.SetOutputToWaveFile('{w}'); "
            f"$s.Speak('{text}'); "
            "$s.Dispose()"
        )
        r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand",
                            base64.b64encode(inner.encode("utf-16-le")).decode("ascii")],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert w.is_file(), f"TTS 失败:{r.stderr[-200:]}"
        # 时长
        p = subprocess.run([FF, "-v", "error", "-i", str(w), "-f", "null", "-"],
                           capture_output=True, text=True)
        import re
        m = re.search(r"time=(\d+):(\d+):([\d.]+)", p.stderr)
        dur = float(m.group(1)) * 3600 + float(m.group(2)) * 60 + float(m.group(3)) if m else 2.0
        wavs.append(w)
        timeline.append({"text": text, "start": round(t, 3), "dur": round(dur, 3)})
        t += dur + 0.6
    total = t + 0.5
    return wavs, timeline, total


def _voice_envelope(tmp: Path, voice_wav: Path, total: float) -> np.ndarray:
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
    wavs, timeline, total = sapi_tts(tmp)
    # 拼接音频(带 0.6s 间隔)
    inputs = []
    for w in wavs:
        inputs += ["-i", str(w)]
    n = len(wavs)
    parts, last = [], "0"
    cursor = timeline[0]["start"]
    fc = []
    # 用 adelay 把每句推到正确时间点,再 amix
    for i, tl in enumerate(timeline):
        delay_ms = int(tl["start"] * 1000)
        fc.append(f"[{i}:a]adelay=delays={delay_ms}:all=1[a{i}]")
    fc.append("".join(f"[a{i}]" for i in range(n)) + f"amix=inputs={n}:duration=longest:normalize=0[aout]")
    base_wav = tmp / "voice.wav"
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
        cy = h * 0.4 + np.cos(t * 4.8) * h * 0.05 * amp
        img = np.zeros((h, w, 3), np.uint8)
        img[..., 0] = 40
        img[..., 1] = 44
        img[..., 2] = 48
        head = (((xx - cx) / 26) ** 2 + ((yy - cy) / 30) ** 2) <= 1
        img[head] = (200, 160, 140)
        with open(frames_dir / f"f{i:04d}.ppm", "wb") as f:
            f.write(f"P6\n{w} {h}\n255\n".encode())
            f.write(img.tobytes())
    silent = tmp / "v.mp4"
    subprocess.run([FF, "-v", "error", "-y", "-framerate", str(fps),
                    "-i", str(frames_dir / "f%04d.ppm"), "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(silent)], check=True)
    return silent, base_wav, timeline, total


def _ts(sec: float) -> str:
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def write_ass(path: Path, timeline: list[dict], shift_s: float = 0.0):
    lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "",
             "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
             "Style: Main,Microsoft YaHei,54,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,60,60,60,1", "",
             "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for tl in timeline:
        a = tl["start"] + shift_s
        b = tl["start"] + tl["dur"] + 0.3 + shift_s
        lines.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Main,,0,0,0,,{tl['text'].rstrip('。')}")
    path.write_text("\n".join(lines), encoding="utf-8")


def mux(tmp: Path, silent: Path, wav: Path, out: Path, audio_delay_s: float = 0.0):
    delayed = tmp / "delayed.wav"
    subprocess.run([FF, "-v", "error", "-y", "-i", str(wav),
                    "-af", f"adelay=delays={int(audio_delay_s * 1000)}:all=1",
                    str(delayed)], check=True)
    subprocess.run([FF, "-v", "error", "-y", "-i", str(silent), "-i", str(delayed),
                    "-c:v", "copy", "-c:a", "aac", "-shortest", str(out)], check=True)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="fx2_"))
    silent, base_wav, timeline, total = build_base(tmp)
    manifest = []

    # 基线
    base = OUT / "baseline.mp4"
    mux(tmp, silent, base_wav, base, 0.0)
    ass = OUT / "baseline.ass"
    write_ass(ass, timeline, 0.0)
    manifest.append({"name": "baseline", "video": str(base), "ass": str(ass),
                     "expect": {"verdict": "pass", "D1": "pass", "D2": "pass"}})

    # FIX1 字幕 +0.5s
    v1 = OUT / "fix1_subtitle_shift.mp4"
    mux(tmp, silent, base_wav, v1, 0.0)
    a1 = OUT / "fix1_subtitle_shift.ass"
    write_ass(a1, timeline, 0.5)
    manifest.append({"name": "fix1-subtitle-shift", "video": str(v1), "ass": str(a1),
                     "expect": {"verdict": "issues", "D1": "fail"},
                     "note": "字幕整体 +0.5s(单卡偏移 500ms 触 D1 硬线)"})

    # FIX2 音轨延后 0.45s,字幕跟音轨走 → D1 pass,D2 应抓画面运动相对语音偏移
    v2 = OUT / "fix2_audio_shift.mp4"
    mux(tmp, silent, base_wav, v2, 0.45)
    a2 = OUT / "fix2_audio_shift.ass"
    write_ass(a2, timeline, 0.45)
    manifest.append({"name": "fix2-audio-shift", "video": str(v2), "ass": str(a2),
                     "expect": {"verdict": "issues", "D2": "fail", "D1": "pass"},
                     "note": "音轨+字幕同延 0.45s,画面不动 → 口型错位;D2 应检出整体偏移"})

    # FIX3 错剪:第 2 句句中(4.6s 处)剪 0.8s,拼接;字幕跟着重排(与音轨一致 → D1 pass)
    # 音频:第 2 句被腰斩:保留其前 40%,跳过后 60% + 0.5s,接第 3 句
    t2 = timeline[1]
    cut_point = t2["start"] + t2["dur"] * 0.4          # 句中腰斩点
    resume = t2["start"] + t2["dur"] + 0.6             # 跳到第 3 句前
    # 用 atrim 重组: [0, cut_point] + [resume, total]
    wav3 = tmp / "voice3.wav"
    subprocess.run([FF, "-v", "error", "-y", "-i", str(base_wav), "-filter_complex",
                    f"[0:a]atrim=0:{cut_point:.3f},asetpts=PTS-STARTPTS[a];"
                    f"[0:a]atrim={resume:.3f}:{total:.3f},asetpts=PTS-STARTPTS[b];"
                    f"[a][b]concat=n=2:v=0:a=1[aout]",
                    "-map", "[aout]", str(wav3)], check=True)
    # 视频同步剪:同区间
    v3 = OUT / "fix3_bad_cut.mp4"
    silent3 = tmp / "v3.mp4"
    subprocess.run([FF, "-v", "error", "-y", "-i", str(silent), "-filter_complex",
                    f"[0:v]trim=0:{cut_point:.3f},setpts=PTS-STARTPTS[f];"
                    f"[0:v]trim={resume:.3f}:{total:.3f},setpts=PTS-STARTPTS[g];"
                    f"[f][g]concat=n=2:v=1:a=0[out]",
                    "-map", "[out]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(silent3)], check=True)
    subprocess.run([FF, "-v", "error", "-y", "-i", str(silent3), "-i", str(wav3),
                    "-c:v", "copy", "-c:a", "aac", "-shortest", str(v3)], check=True)
    a3 = OUT / "fix3_bad_cut.ass"
    # 字幕:第 2 句被腰斩成「遇到蓝屏」+ 硬接「第一步检查内存条」;时间压缩
    cut_dur = cut_point - 0.5                          # 第 1 句时长不变
    tl3 = [timeline[0],
           {"text": "遇到蓝屏", "start": t2["start"], "dur": (cut_point - t2["start"]) * 0.9},
           {"text": "第一步检查内存条。", "start": cut_point + 0.1, "dur": timeline[2]["dur"]},
           {"text": "然后重新插拔再开机。", "start": cut_point + 0.1 + timeline[2]["dur"] + 0.6,
            "dur": timeline[3]["dur"]}]
    write_ass(a3, tl3, 0.0)
    manifest.append({"name": "fix3-bad-cut", "video": str(v3), "ass": str(a3),
                     "expect": {"verdict": "issues", "D3b": "fail", "D1": "pass"},
                     "note": "第 2 句句中腰斩硬接 → 语义断裂;D3b 应抓突兀截断/悬空,D1 pass(字幕跟音轨一致)"})

    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print("fixtures rebuilt at", OUT)
    for m in manifest:
        print(" ", m["name"], "->", m["expect"]["verdict"])


if __name__ == "__main__":
    main()
