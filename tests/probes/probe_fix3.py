# -*- coding: utf-8 -*-
"""排查 FIX3:D3b 应抓突兀截断却 pass。看 D3b 收到的子句与间隙。"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(r"E:\平日资料\GitHub\CutFlow")
PY = str(REPO / "tools" / ".venv-asr" / "Scripts" / "python.exe")
import os
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
sys.path.insert(0, str(REPO / "skills" / "cutflow" / "scripts"))
p = subprocess.run([PY, str(REPO / "tools" / "fun_asr.py"),
                    str(REPO / "tests" / "fixtures" / "diagnosis" / "fix3_bad_cut.mp4"), "--json"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
doc = json.loads(lines[-1])
segs = (doc.get("data") or {}).get("segments") or []
out = []
for s in segs:
    out.append(f"[{s.get('start')}-{s.get('end')}] {s.get('text', '')!r}")
# 手动重放 D3b 分句
import rs_diagnose as rd  # noqa
sents = []
_BOUNDS = "。?!?!;;,，"
for s in segs:
    raw = s.get("text", "")
    ts = s.get("timestamp") or []
    start, end = float(s.get("start") or 0), float(s.get("end") or s.get("start") or 0)
    if ts and len(raw) == len(ts):
        cur_text, cur_start, cur_end = "", None, None
        for ch, (a, b2) in zip(raw, ts):
            cur_text += ch
            if cur_start is None:
                cur_start = a
            cur_end = b2
            if ch in _BOUNDS:
                sents.append({"text": cur_text, "start": cur_start / 1000.0, "end": cur_end / 1000.0})
                cur_text, cur_start = "", None
        if cur_text.strip():
            sents.append({"text": cur_text, "start": (cur_start or 0) / 1000.0,
                          "end": (cur_end or cur_start or 0) / 1000.0})
    else:
        per = (end - start) / max(1, len(raw))
        cur_text, cur_start = "", start
        for i2, ch in enumerate(raw):
            cur_text += ch
            if ch in _BOUNDS:
                sents.append({"text": cur_text, "start": cur_start, "end": start + (i2 + 1) * per})
                cur_text, cur_start = "", start + (i2 + 1) * per
        if cur_text.strip():
            sents.append({"text": cur_text, "start": cur_start, "end": end})
sents = [s for s in sents if rd._norm(s["text"])]
out.append(f"--- sents({len(sents)}) ---")
for s in sents:
    out.append(f"[{s['start']:.2f}-{s['end']:.2f}] {s['text']!r}")
    if s is not sents[-1]:
        nxt = sents[sents.index(s) + 1] if sents.index(s) + 1 < len(sents) else None
for a, b in zip(sents, sents[1:]):
    gap = (b["start"] - float(a.get("end") or a["start"])) * 1000
    out.append(f"gap {a['text'][-6:]!r}->{b['text'][:6]!r}: {gap:.0f}ms")
(REPO / ".cluster" / "fix3_probe.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
