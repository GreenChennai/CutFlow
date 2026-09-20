#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rs_notes.py — notes.json(时间轴标注)读写/孤儿统计桥(计划书 5.5)。

与 CutForge `schemas/notes.schema.json` 语义一致;只读统计+结构校验,
创建/结案等写操作走 CutForge 命令通道(notes_add/notes_resolve 工具或 CLI):

  python rs_notes.py list <工程> [--state open] [--author user]
  python rs_notes.py stats <工程>              # 各状态计数 + 孤儿清单
  python rs_notes.py --probe

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

VALID_STATES = {"open", "resolved", "rejected", "orphan"}


def load_notes(root: Path) -> list[dict]:
    p = root / "notes.json"
    if not p.is_file():
        return []
    doc = json.loads(p.read_text(encoding="utf-8"))
    items = doc.get("items", [])
    # 结构自检(id/anchor/state 三要素;不完整即视为工程内容缺陷)
    for n in items:
        if not all(k in n for k in ("id", "anchor", "state")) or n["state"] not in VALID_STATES:
            raise ValueError(f"标注结构不合法: {json.dumps(n, ensure_ascii=False)[:120]}")
    return items


def main() -> int:
    if "--probe" in sys.argv:
        return emit(True, "OK", "rs_notes 桥自检通过", {"bridge": "notes"}, exit_code=0)
    ap = argparse.ArgumentParser(description="CutForge 标注桥(读/统计)")
    ap.add_argument("cmd", nargs="?", default="--probe", choices=["list", "stats", "--probe"])
    ap.add_argument("root", nargs="?", help="工程目录")
    ap.add_argument("--state", choices=sorted(VALID_STATES), help="按状态过滤")
    ap.add_argument("--author", choices=["user", "agent"], help="按作者过滤")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.cmd == "--probe":
        return emit(True, "OK", "rs_notes 桥自检通过", {"bridge": "notes"}, exit_code=0)

    if not a.root:
        # P23-1:错误码与 CutForge 5.4 码表对齐 —— 输入错=PRECONDITION_FAILED,
        # 不再用表外码 USAGE/NO_ENV
        return emit(False, "PRECONDITION_FAILED", "需要工程目录参数", {}, exit_code=2)
    root = Path(a.root)
    if not root.is_dir():
        return emit(False, "PRECONDITION_FAILED", f"工程目录不存在: {root}", {}, exit_code=2)

    try:
        items = load_notes(root)
    except Exception as exc:  # noqa: BLE001
        return emit(False, "INTERNAL", f"notes.json 解析失败: {exc}", {}, exit_code=4)

    if a.cmd == "list":
        picked = [n for n in items
                  if (not a.state or n["state"] == a.state)
                  and (not a.author or n.get("author") == a.author)]
        return emit(True, "OK", f"{len(picked)}/{len(items)} 条标注",
                    {"notes": picked, "orphans": sum(1 for n in items if n["state"] == "orphan")},
                    exit_code=0)

    counts = Counter(n["state"] for n in items)
    orphans = [n for n in items if n["state"] == "orphan"]
    return emit(True, "OK", "标注统计",
                {"total": len(items),
                 "byState": dict(counts),
                 "orphanIds": [n["id"] for n in orphans],
                 "hint": "孤儿标注必须显式处置(重挂/否决),禁止静默丢弃" if orphans else None},
                exit_code=0)


if __name__ == "__main__":
    ensure_utf8()
    sys.exit(main())
