# -*- coding: utf-8 -*-
"""Windows SAPI TTS:用 -Command 单行 + Base64 避免 PS1 编码问题。"""
import base64
import subprocess
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="sapi_"))
out = tmp / "sapi.wav"
inner = (
    "Add-Type -AssemblyName System.Speech; "
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$voices = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' }; "
    "if ($voices) { $s.SelectVoice($voices[0].VoiceInfo.Name) }; "
    f"$s.SetOutputToWaveFile('{out}'); "
    "$s.Speak('今天我们讲桌面运维的第一课。遇到蓝屏先不要慌。第一步检查内存条。然后重新插拔再开机。'); "
    "$s.Dispose()"
)
cmd_b64 = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", cmd_b64],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
log = [f"rc={r.returncode}"]
if r.stderr:
    log.append("stderr: " + r.stderr[-200:].encode("ascii", "replace").decode())
log.append(f"wav exists: {out.is_file()}")
if out.is_file():
    log.append(f"size: {out.stat().st_size}")
    # 拷到仓库固定位置
    dest = Path(r"E:\平日资料\GitHub\CutFlow\tests\fixtures\diagnosis\sapi_voice.wav")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(out.read_bytes())
    log.append(f"copied to: {dest}")
log_path = Path(r"E:\平日资料\GitHub\CutFlow\.cluster\sapi_probe.txt")
log_path.write_text("\n".join(log), encoding="utf-8")
print("ok")
