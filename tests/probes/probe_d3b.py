# -*- coding: utf-8 -*-
"""排查 D3b 仍报分句过少:d3b 收到的 segments 里 timestamp 是否存在?"""
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
for s in segs:
    raw = s.get("text", "")
    ts = s.get("timestamp") or []
    out.append(f"len(raw)={len(raw)} len(ts)={len(ts)} ts[:3]={ts[:3]}")
(REPO / ".cluster" / "d3b_probe.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
