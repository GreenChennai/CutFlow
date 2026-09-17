# -*- coding: utf-8 -*-
"""排查卡完整性核对为何没生效:看 D1 rows 的 cardLen/matchedLen 实际值。"""
import json
import subprocess
import os
from pathlib import Path

REPO = Path(r"E:\平日资料\GitHub\CutFlow")
PY = str(REPO / "tools" / ".venv-asr" / "Scripts" / "python.exe")
env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
p = subprocess.run([PY, str(REPO / "skills/cutflow/scripts/rs_diagnose.py"),
                    str(REPO / "tests/fixtures/diagnosis/fix3_bad_cut.mp4"),
                    "--ass", str(REPO / "tests/fixtures/diagnosis/fix3_bad_cut.ass"),
                    "--budget", "600", "--json"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
doc = json.loads(lines[-1])
d1 = [c for c in (doc.get("data") or doc).get("checks", []) if c["id"] == "D1"][0]
rows = d1.get("rows") or []
out = [f"{r.get('card')!r} len={r.get('cardLen')} matched={r.get('matchedLen')}" for r in rows]
(REPO / ".cluster" / "rows_check.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
