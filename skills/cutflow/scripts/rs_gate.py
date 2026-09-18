#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rs_gate.py — CutForge 统一门禁入口桥(计划书 5.5;等价 cutforge/tools/gates/gate.py)。

  python rs_gate.py M0 --json            # 同 cutforge 侧门禁,退出码透传
  python rs_gate.py M3 --check latency --json
  python rs_gate.py --probe

退出码即门禁语义:0 通过 / 2 门禁失败 / 3 环境缺失 / 4 内部错误。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit, ensure_utf8  # noqa: E402

DEFAULT_CUTFORGE = Path(r"E:\平日资料\GitHub\cutforge")


def forge_root() -> Path:
    env = os.environ.get("CUTFORGE_REPO")
    return Path(env) if env else DEFAULT_CUTFORGE


def main() -> int:
    if "--probe" in sys.argv:
        # M9 桥升版:probe 必须真检 gate.py 存在(此前无条件报 OK,doctor 体检假绿)
        gate = forge_root() / "tools" / "gates" / "gate.py"
        if not gate.is_file():
            return emit(False, "NO_ENV",
                        f"rs_gate 桥自检失败:CutForge 门禁不存在: {gate}(设 CUTFORGE_REPO)",
                        {"bridge": "gate", "forgeRoot": str(forge_root())}, exit_code=3)
        return emit(True, "OK", "rs_gate 桥自检通过(gate.py 在位:%s)" % gate,
                    {"bridge": "gate", "forgeRoot": str(gate)}, exit_code=0)
    ap = argparse.ArgumentParser(description="CutForge 门禁桥(退出码透传)")
    ap.add_argument("milestone", nargs="?", help="里程碑,如 M0")
    ap.add_argument("--check", help="只跑指定检查项")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not a.milestone:
        return emit(True, "OK", "rs_gate 桥自检通过(CUTFORGE_REPO=%s)" % forge_root(),
                    {"bridge": "gate", "forgeRoot": str(forge_root())}, exit_code=0)

    gate = forge_root() / "tools" / "gates" / "gate.py"
    if not gate.is_file():
        return emit(False, "NO_ENV", f"CutForge 门禁不存在: {gate}(设 CUTFORGE_REPO)",
                    {}, exit_code=3)

    cmd = [sys.executable, str(gate), a.milestone]
    if a.check:
        cmd += ["--check", a.check]
    if a.json:
        cmd += ["--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
    except subprocess.TimeoutExpired:
        return emit(False, "INTERNAL", "门禁超时(1800s)", {}, exit_code=4)
    out = (r.stdout or "") + (r.stderr or "")
    sys.stdout.write(out)
    return r.returncode if r.returncode is not None else 4


if __name__ == "__main__":
    ensure_utf8()
    sys.exit(main())
