# -*- coding: utf-8 -*-
"""rs_common.run 支持 env 透传。"""
from pathlib import Path

REPO = Path(r"E:\平日资料\GitHub\CutFlow")
p = REPO / "skills" / "cutflow" / "scripts" / "rs_common.py"
src = p.read_text(encoding="utf-8")

i = src.index("def run(")
j = src.index("\ndef ", i + 10)
block = src[i:j]
print("BEFORE:", block[:300])

if "env" not in block:
    # 查看签名与 subprocess 调用
    import re
    m = re.search(r"def run\(([^)]*)\)", block)
    print("SIG:", m.group(1))
    m2 = re.search(r"subprocess\.run\(([^)]*)\)", block, re.S)
    print("CALL:", m2.group(1)[:200] if m2 else "not found")
