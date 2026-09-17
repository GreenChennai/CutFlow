#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rs_oplog.py — CutForge OpLog 读取/审计桥(计划书 5.5)。

append-only,不修改既有 Op;回答"AI 改了什么":

  python rs_oplog.py tail <工程> [--rev N] [--actor agent] [--limit 20]
  python rs_oplog.py report <工程>            # 按 actor 汇总改动
  python rs_oplog.py --probe

退出码:0 通过 / 2 输入错 / 3 前置缺失 / 4 内部错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit, ensure_utf8  # noqa: E402


def load_ops(root: Path) -> list[dict]:
    d = root / ".cutforge" / "oplog"
    if not d.is_dir():
        return []
    ops: list[dict] = []
    for f in sorted(d.glob("*.jsonl")):  # 文件名升序 = 日期升序
        for ln in f.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                try:
                    ops.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue  # 半行(上次写入中断)跳过,rev 以文件为准
    ops.sort(key=lambda o: o.get("rev") or 0)
    return ops


def main() -> int:
    if "--probe" in sys.argv:
        return emit(True, "OK", "rs_oplog 桥自检通过", {"bridge": "oplog"}, exit_code=0)
    ap = argparse.ArgumentParser(description="CutForge OpLog 审计桥(只读)")
    ap.add_argument("cmd", nargs="?", default="--probe", choices=["tail", "report", "--probe"])
    ap.add_argument("root", nargs="?", help="工程目录")
    ap.add_argument("--rev", type=int, default=None, help="只看 rev > N")
    ap.add_argument("--actor", choices=["user", "agent", "script"], help="按 actor.kind 过滤")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.cmd == "--probe":
        return emit(True, "OK", "rs_oplog 桥自检通过", {"bridge": "oplog"}, exit_code=0)

    if not a.root:
        return emit(False, "USAGE", "需要工程目录参数", {}, exit_code=2)
    root = Path(a.root)
    if not root.is_dir():
        return emit(False, "NO_ENV", f"工程目录不存在: {root}", {}, exit_code=3)

    ops = load_ops(root)
    if a.cmd == "tail":
        picked = [o for o in ops
                  if (a.rev is None or (o.get("rev") or 0) > a.rev)
                  and (not a.actor or (o.get("actor") or {}).get("kind") == a.actor)]
        return emit(True, "OK", f"{len(picked[-a.limit:])} 条(共 {len(ops)})",
                    {"ops": picked[-a.limit:], "total": len(ops),
                     "lastRev": ops[-1].get("rev") if ops else 0},
                    exit_code=0)

    by_actor = Counter((o.get("actor") or {}).get("kind") or "?" for o in ops)
    agents = [f"rev{o.get('rev')}: {o.get('summary')}" for o in ops
              if (o.get("actor") or {}).get("kind") == "agent"]
    return emit(True, "OK", "OpLog 审计",
                {"total": len(ops), "byActor": dict(by_actor),
                 "agentChanges": agents[-50:],
                 "hint": "批量撤销 AI 改动走 cutforge-cli undo"},
                exit_code=0)


if __name__ == "__main__":
    ensure_utf8()
    sys.exit(main())
