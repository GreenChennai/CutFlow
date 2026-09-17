# -*- coding: utf-8 -*-
"""ASR 对 SAPI wav 的识别效果探测。"""
import json
import subprocess
from pathlib import Path

REPO = Path(r"E:\平日资料\GitHub\CutFlow")
PY = str(REPO / "tools" / ".venv-asr" / "Scripts" / "python.exe")
video = str(REPO / "tests" / "fixtures" / "diagnosis" / "sapi_voice.wav")
p = subprocess.run([PY, str(REPO / "tools" / "fun_asr.py"), video, "--json"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
try:
    doc = json.loads(lines[-1])
    segs = (doc.get("data") or {}).get("segments") or []
    out = [f"backend={(doc.get('data') or {}).get('backend')}"]
    for s in segs[:8]:
        out.append(f"[{s.get('start')}-{s.get('end')}] {s.get('text', '')!r}")
except Exception as exc:
    out = [f"parse fail: {exc}", (lines[-1] if lines else "no output")[:300]]
(REPO / ".cluster" / "asr_sapi.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
