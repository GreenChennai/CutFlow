# -*- coding: utf-8 -*-
"""拿 ASR 字级时间戳看每句实际起点。"""
import json
import subprocess
from pathlib import Path

REPO = Path(r"E:\平日资料\GitHub\CutFlow")
PY = str(REPO / "tools" / ".venv-asr" / "Scripts" / "python.exe")
import os
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
p = subprocess.run([PY, str(REPO / "tools" / "fun_asr.py"),
                    str(REPO / "tests" / "fixtures" / "diagnosis" / "baseline.mp4"), "--json"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
doc = json.loads(lines[-1])
segs = (doc.get("data") or {}).get("segments") or []
out = []
for s in segs[:4]:
    ts = s.get("timestamp") or []
    text = s.get("text", "")
    out.append(f"[{s.get('start')}-{s.get('end')}] {text!r}")
    for ch, (a, b) in list(zip(text, ts))[:6]:
        out.append(f"   {ch!r} {a}-{b}")
# 关键锚字:每句首字的时间戳
anchors = {}
full = "".join(s.get("text", "") for s in segs)
for s in segs:
    ts = s.get("timestamp") or []
    for ch, (a, b) in zip(s.get("text", ""), ts):
        if ch in "今遇第然":
            anchors.setdefault(ch, []).append((a, b))
out.append(f"anchors: {anchors}")
(REPO / ".cluster" / "baseline_ts.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
