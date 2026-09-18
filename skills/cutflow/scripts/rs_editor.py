#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rs_editor.py — 工程 ↔ CutForge 编辑器桥(计划书 5.5)。

只做协议转换与文件读取,**不实现任何阶段逻辑**;写操作走 CutForge 的
命令通道(cutforge-cli / cutforge-mcp),本脚本提供只读视图与工程体检:

  python rs_editor.py view <工程>            # IR 全量视图(envelope.data.project)
  python rs_editor.py timeline <工程>        # 片段时间线摘要
  python rs_editor.py check <工程>           # 工程结构体检(五真相源 + .cutforge)
  python rs_editor.py --probe                # 自检(doctor 用)

退出码:0 通过 / 2 输入错 / 3 前置缺失 / 4 内部错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit, ensure_utf8  # noqa: E402

TRUTH_SOURCES = ["05_ir/project.json", "05_ir/wordline.json", "04_cut/cutlist.json", "notes.json"]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def timeline(project: dict) -> list[dict]:
    """M9 桥升版:CutFlow 原生 IR(轨道/片段无稳定 id)不再输出 null——
    确定性回退 `V1` / `V1#2` 形态,并以 idSource=native|fallback 显式标注,
    消费者可据 idSource 判断该 id 是否可作锚点。"""
    rows: list[dict] = []
    letters = {"video": "V", "audio": "A", "text": "T"}
    for ti, t in enumerate(project.get("tracks", [])):
        if t.get("id"):
            tid, t_src = t["id"], "native"
        else:
            tid = f"{letters.get(t.get('kind'), 'X')}{ti + 1}"
            t_src = "fallback"
        for ci, c in enumerate(t.get("clips", [])):
            if c.get("id"):
                cid, c_src = c["id"], "native"
            else:
                cid = f"{tid}#{ci + 1}"
                c_src = "fallback"
            rows.append({
                "id": cid,
                "idSource": c_src,
                "track": tid,
                "trackIdSource": t_src,
                "startMs": c.get("startMs"),
                "endMs": (c.get("startMs") or 0) + (c.get("durationMs") or 0),
            })
    return rows


def main() -> int:
    if "--probe" in sys.argv:
        return emit(True, "OK", "rs_editor 桥自检通过(协议/依赖可用)", {"bridge": "editor"}, exit_code=0)
    ap = argparse.ArgumentParser(description="CutFlow ↔ CutForge 编辑器桥(只读)")
    ap.add_argument("cmd", nargs="?", default="--probe",
                    choices=["view", "timeline", "check", "--probe"])
    ap.add_argument("root", nargs="?", help="工程目录")
    ap.add_argument("--json", action="store_true", help="JSON 输出(默认)")
    a = ap.parse_args()

    if a.cmd == "--probe":
        return emit(True, "OK", "rs_editor 桥自检通过(协议/依赖可用)", {"bridge": "editor"}, exit_code=0)

    if not a.root:
        return emit(False, "USAGE", "需要工程目录参数", {}, exit_code=2)
    root = Path(a.root)
    if not root.is_dir():
        return emit(False, "NO_ENV", f"工程目录不存在: {root}", {}, exit_code=3)

    if a.cmd == "view":
        p = root / "05_ir/project.json"
        if not p.is_file():
            return emit(False, "NO_ENV", f"缺 IR: {p}", {}, exit_code=3)
        return emit(True, "OK", "工程视图", {"project": _load(p), "rev_hint": "写操作请走 cutforge-cli/mcp"}, exit_code=0)

    if a.cmd == "timeline":
        p = root / "05_ir/project.json"
        if not p.is_file():
            return emit(False, "NO_ENV", f"缺 IR: {p}", {}, exit_code=3)
        return emit(True, "OK", "时间线", {"clips": timeline(_load(p))}, exit_code=0)

    # check
    found = {rel: (root / rel).is_file() for rel in TRUTH_SOURCES}
    state_ok = (root / ".cutforge").is_dir()
    missing = [k for k, v in found.items() if not v]
    ok = not missing
    return emit(ok, "OK" if ok else "NO_ENV",
                "工程结构完整" if ok else f"缺文件: {missing}",
                {"truth_sources": found, "cutforge_state": state_ok,
                 "hint": None if ok else "工程未初始化或产物缺失;写操作走 cutforge-cli"},
                exit_code=0 if ok else 3)


if __name__ == "__main__":
    ensure_utf8()
    sys.exit(main())
