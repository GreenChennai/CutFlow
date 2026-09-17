# -*- coding: utf-8 -*-
"""排查基线视频 D1/D2 误报:ASR 实测 vs 字幕时间轴的逐卡偏移明细。"""
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
for s in segs[:10]:
    out.append(f"[{s.get('start')}-{s.get('end')}] {s.get('text', '')!r}")
# ASS 各卡起点
ass = (REPO / "tests" / "fixtures" / "diagnosis" / "baseline.ass").read_text(encoding="utf-8")
cards = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
out.append("--- ASS cards ---")
for c in cards:
    parts = c.split(",")
    out.append(f"[{parts[1]}-{parts[2]}] {parts[-1]!r}")
(REPO / ".cluster" / "baseline_align.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
