# -*- coding: utf-8 -*-
"""run_diag v2:错误时打印 stderr 尾部到 .cluster/diag_err.txt。"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(r"E:\平日资料\GitHub\CutFlow")
PY = REPO / "tools" / ".venv-asr" / "Scripts" / "python.exe"
DIAG = REPO / "skills" / "cutflow" / "scripts" / "rs_diagnose.py"

video, ass = sys.argv[1], sys.argv[2]
budget = sys.argv[3] if len(sys.argv) > 3 else "600"
p = subprocess.run([str(PY), str(DIAG), video, "--ass", ass, "--budget", budget, "--json"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
if not lines:
    err_path = REPO / ".cluster" / "diag_err.txt"
    err_path.parent.mkdir(exist_ok=True)
    err_path.write_text((p.stderr or "")[-1500:], encoding="utf-8")
    print(f"NO STDOUT rc={p.returncode}, stderr -> {err_path}")
    raise SystemExit(1)
doc = json.loads(lines[-1])
data = doc.get("data") or doc
print("code:", doc.get("code"), "| verdict:", data.get("verdict"),
      "| elapsed:", data.get("elapsedS"), "s")
for c in data.get("checks", []):
    f0 = (c.get("findings") or [""])[0]
    print(f"  {c['id']} {c['status']:13s} {c.get('elapsedS', 0)}s :: {f0[:90]}")
print("spans:", data.get("suspiciousSpans"))
