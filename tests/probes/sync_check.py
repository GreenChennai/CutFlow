# -*- coding: utf-8 -*-
"""v0.14 同步策略确认:检查远端 main 的 rs_verify 是否已有 --content(用户可能自己加了)。"""
import subprocess
from pathlib import Path

REPO = Path(r"E:\平日资料\GitHub\CutFlow")
r = subprocess.run(["git", "show", "origin/main:skills/cutflow/scripts/rs_verify.py"],
                   capture_output=True)
src = r.stdout.decode("utf-8", errors="replace")
out = [
    f"origin/main rs_verify len={len(src)}",
    f"has --content: {'--content' in src}",
    f"has rs_diagnose: {'rs_diagnose' in src}",
    f"has scope mechanical: {'mechanical' in src}",
]
r2 = subprocess.run(["git", "ls-tree", "-r", "origin/main", "--name-only"],
                    capture_output=True)
tree = r2.stdout.decode("utf-8", errors="replace")
out.append(f"has rs_diagnose.py on origin/main: {'rs_diagnose.py' in tree}")
out.append(f"has REVIEW doc: {'REVIEW-20260916' in tree}")
out.append(f"has fixtures: {'fixtures/diagnosis' in tree}")
(REPO / ".cluster" / "sync_check.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
