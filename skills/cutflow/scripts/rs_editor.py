#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rs_editor.py — 工程 ↔ CutForge 编辑器桥(计划书 5.5)。

只做协议转换与文件读取,**不实现任何阶段逻辑**;写操作走 CutForge 的
命令通道(cutforge-cli / cutforge-mcp),本脚本提供只读视图、工程体检与变更识别:

  python rs_editor.py view <工程>            # IR 全量视图(envelope.data.project)
  python rs_editor.py timeline <工程>        # 片段时间线摘要
  python rs_editor.py check <工程>           # 工程结构体检(五真相源 + .cutforge)
  python rs_editor.py diff <工程>            # 变更识别(RT-3):编辑器这次改了什么(人话 + JSON)
  python rs_editor.py --probe                # 自检(doctor 用)

锚点契约(P22-1,CONTEXT.md「锚点」条):被引用元素必须有稳定 id,**不得依赖数组下标**。
原生 IR 无 clip id 时,timeline 不再输出 `V1#2` 这类下标派号当 id —— 那会在 S3 重建
后指到别的片段(比 orphan 更难发现)。回退 id 改为**内容寻址**
`cf-<sha1(src|sourceInMs|startMs|durationMs)[:12]>`:重排下标不变、指错不可能发生,
可作锚点(anchorable=true);原下标形态降级为 `displayId`(仅人读标签,禁止当锚点)。
线协议 `idSource` 对回退行沿用 M9 的 "fallback" 值(同伴仓冒烟钉住)。

diff(RT-3)数据源择优:
  1) `.cutforge/session-summary.json`(cutforge RT-1 落盘,actor=human 的 Op 清单)
     —— 有它就按 Op 逐条人话;
  2) 否则取 `.cutforge/bases/<rev>.json`(最大 rev 的基线快照)与当前盘面对比。
错误码与 CutForge 5.4 码表对齐(P23-1):参数缺/路径错 → PRECONDITION_FAILED(2),
缺 IR 等依赖 → DEP_MISSING(3),解析失败 → INTERNAL(4);不再用表外码。

退出码:0 通过 / 2 输入错 / 3 前置缺失 / 4 内部错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit, ensure_utf8  # noqa: E402
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量

def truth_sources(root: Path | None = None) -> list[str]:
    """工程真相文件(相对路径):目录名经 rs_paths 按工程解析(ADR-0046);root=None 用新名。"""
    if root is None:
        return [rs_paths.p("timeline") + "/project.json", rs_paths.p("timeline") + "/wordline.json",
                rs_paths.p("cut") + "/cutlist.json", "notes.json"]
    return [rs_paths.rel(root, "timeline", "project.json"),
            rs_paths.rel(root, "timeline", "wordline.json"),
            rs_paths.rel(root, "cut", "cutlist.json"), "notes.json"]


TRUTH_SOURCES = truth_sources()   # 兼容旧引用(静态新名口径)

SESSION_SUMMARY_REL = ".cutforge/session-summary.json"
BASES_REL = ".cutforge/bases"
_KIND_LETTER = {"video": "V", "audio": "A", "text": "T"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def content_id(clip: dict) -> str:
    """P22-1:无原生 id 时回退的内容寻址 id(重排稳定,可作锚点)。

    以片段内容为身份:src + sourceInMs + startMs + durationMs 任一变了就是新身份
    —— 这正是锚点该有的语义(片段被改,旧锚点按孤儿处理,而不是静默指错)。
    """
    raw = "|".join(str(clip.get(k, "")) for k in
                   ("src", "sourceInMs", "startMs", "durationMs"))
    return "cf-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _display_id(tid: str, ci: int) -> str:
    return f"{tid}#{ci + 1}"     # 仅人读标签;禁止当锚点(见模块 docstring)


def timeline(project: dict) -> list[dict]:
    """时间线行。id 两源:native(工程自带)| fallback(内容寻址回退,P22-1;
    线协议值沿用 M9 的 "fallback",派生方式见 content_id)。

    每行带 `idSource` 与 `anchorable`:消费者据此判断该 id 可否作锚点 ——
    `displayId`(`V1#2` 形态)只是展示标签,**任何消费者不得拿它当锚点**。
    """
    rows: list[dict] = []
    for ti, t in enumerate(project.get("tracks", [])):
        if t.get("id"):
            tid, t_src = str(t["id"]), "native"
        else:
            tid = f"{_KIND_LETTER.get(t.get('kind'), 'X')}{ti + 1}"
            t_src = "fallback"
        for ci, c in enumerate(t.get("clips", [])):
            did = _display_id(tid, ci)
            if c.get("id"):
                cid, c_src, anchorable = str(c["id"]), "native", True
            else:
                # M9 线协议值保持 idSource="fallback"(同伴仓冒烟钉住该值);
                # P22-1 起派生方式改为内容寻址(content_id),重排稳定、可作锚点。
                cid, c_src, anchorable = content_id(c), "fallback", True
            rows.append({
                "id": cid,
                "idSource": c_src,
                "anchorable": anchorable,
                "displayId": did,
                "track": tid,
                "trackIdSource": t_src,
                "startMs": c.get("startMs"),
                "endMs": (c.get("startMs") or 0) + (c.get("durationMs") or 0),
                **({"src": c.get("src")} if c.get("src") else {}),
            })
    return rows


# ---------------------------------------------------------------- diff(RT-3)

def _sec(ms) -> str:
    try:
        v = float(ms)
    except (TypeError, ValueError):
        return "?"
    return f"{v / 1000:.1f}s" if v else "0.0s"


def _span(clip: dict) -> str:
    s, d = clip.get("startMs"), clip.get("durationMs")
    if s is None:
        return "?"
    return f"{_sec(s)}–{_sec((s or 0) + (d or 0))}"


def load_session_summary(root: Path) -> dict | None:
    """读 cutforge RT-1 会话摘要;不存在/损坏/非摘要文件 → None(RT-2 同口径:静默跳过)。"""
    p = root / SESSION_SUMMARY_REL
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return doc if doc.get("kind") == "cutforge-session-summary" else None


def load_latest_base(root: Path) -> tuple[int, dict] | None:
    """取 .cutforge/bases/ 下最大 rev 的基线快照(cutforge-io M9-1 落盘)。"""
    d = root / BASES_REL
    if not d.is_dir():
        return None
    revs = []
    for f in d.glob("*.json"):
        try:
            revs.append((int(f.stem), f))
        except ValueError:
            continue
    if not revs:
        return None
    rev, path = max(revs, key=lambda x: x[0])
    try:
        return rev, _load(path)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _clip_noun(track: dict, ti: int) -> str:
    """人话片段称呼:主轨(首个 video 轨)叫「段」,其余 video 轨叫「卡」(场景/overlay)。"""
    if track.get("kind") == "audio":
        return "音频段"
    if track.get("kind") == "text":
        return "字幕段"
    return "段" if (ti == 0 or track.get("name") == "main") else "卡"


def _clip_label(track: dict, ti: int, ci: int) -> str:
    return f"第{ci + 1}{_clip_noun(track, ti)}"


def human_from_ops(project: dict, ops: list[dict]) -> list[str]:
    """session-summary 的 Op 清单 → 人话差异(逐条)。"""
    tracks = project.get("tracks", []) if isinstance(project, dict) else []
    lines: list[str] = []
    for op in ops:
        kind = op.get("opKind") or op.get("op_kind") or "?"
        path = (op.get("target") or {}).get("path", "")
        summary = str(op.get("summary") or "").strip()
        where = _locate(tracks, path)
        detail = ""
        if not where or kind not in ("set", "insert", "delete"):
            detail = f" {path}" if path else ""
        before, after = op.get("before"), op.get("after")
        chg = ""
        if kind == "set" and before is not None:
            chg = f": {_short(before)} → {_short(after)}"
        elif kind == "insert" and after is not None:
            chg = f"({_describe_clip(after)})"
        elif kind == "delete" and before is not None:
            chg = f"(原 {_describe_clip(before)})"
        line = f"[{kind}] {where or path or '工程'}{chg}{detail}"
        if summary and summary not in line:
            line += f"  — {summary}"
        lines.append(line)
    return lines


def _locate(tracks: list[dict], pointer: str) -> str:
    """JSON Pointer(/tracks/0/clips/2/durationMs)→ 「V1 第3段 durationMs」。"""
    parts = [p for p in pointer.split("/") if p != ""]
    if len(parts) >= 4 and parts[0] == "tracks" and parts[2] == "clips":
        try:
            ti, ci = int(parts[1]), int(parts[3])
        except ValueError:
            return ""
        if not (0 <= ti < len(tracks)):
            return f"tracks[{ti}]"
        tr = tracks[ti]
        tid = tr.get("id") or f"{_KIND_LETTER.get(tr.get('kind'), 'X')}{ti + 1}"
        head = f"{tid} {_clip_label(tr, ti, ci)}"
        return f"{head} {'/'.join(parts[4:])}".strip() if len(parts) > 4 else head
    return ""


def _short(v) -> str:
    if isinstance(v, dict):
        return _describe_clip(v) or "{…}"
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= 40 else s[:37] + "…"


def _describe_clip(clip: dict) -> str:
    if not isinstance(clip, dict):
        return _short(clip)
    src = Path(str(clip.get("src", ""))).name if clip.get("src") else ""
    span = _span(clip)
    return " ".join(x for x in (src, span) if x and x != "?")


def diff_tracks(old: dict, new: dict) -> tuple[list[str], dict]:
    """基线快照 vs 当前盘面 → (人话差异, 机器可读结构)。"""
    human: list[str] = []
    machine: dict = {"added": [], "removed": [], "changed": []}
    old_tracks = old.get("tracks", []) if isinstance(old, dict) else []
    new_tracks = new.get("tracks", []) if isinstance(new, dict) else []
    for ti, nt in enumerate(new_tracks):
        ot = _match_track(old_tracks, nt, ti)
        tlabel = nt.get("id") or f"{_KIND_LETTER.get(nt.get('kind'), 'X')}{ti + 1}"
        if ot is None:
            n = len(nt.get("clips", []))
            human.append(f"{tlabel} 轨为新增(含 {n} 个片段)")
            machine["added"].append({"track": tlabel, "clips": nt.get("clips", [])})
            continue
        added, removed, changed = _diff_clips(ot, nt, tlabel, ti)
        if added:
            human += added
            machine["added"].append({"track": tlabel, "items": added})
        if removed:
            human += removed
            machine["removed"].append({"track": tlabel, "items": removed})
        if changed:
            human += changed
            machine["changed"].append({"track": tlabel, "items": changed})
    for oi, ot in enumerate(old_tracks):
        if _match_track(new_tracks, ot, oi) is None:
            tlabel = ot.get("id") or f"{_KIND_LETTER.get(ot.get('kind'), 'X')}{oi + 1}"
            human.append(f"{tlabel} 轨被删(原 {len(ot.get('clips', []))} 个片段)")
            machine["removed"].append({"track": tlabel, "clips": ot.get("clips", [])})
    return human, machine


def _match_track(tracks: list[dict], want: dict, fallback_i: int) -> dict | None:
    for t in tracks:
        if want.get("id") and t.get("id") == want.get("id"):
            return t
        if want.get("name") and t.get("name") == want.get("name"):
            return t
    for i, t in enumerate(tracks):
        if t.get("kind") == want.get("kind") and i == fallback_i:
            return t
    return None


def _diff_clips(ot: dict, nt: dict, tlabel: str, ti: int) -> tuple[list[str], list[str], list[str]]:
    """同轨片段对比:按「内容身份」(src+sourceInMs)配对,剩余即新增/被删;
    配对上的片段报告 startMs/durationMs/sourceInMs 字段级变化。"""
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    oclips = ot.get("clips", [])
    nclips = nt.get("clips", [])
    is_overlay = nt.get("kind") == "video" and ti != 0 and nt.get("name") != "main"
    noun = ("overlay 卡" if is_overlay else _clip_noun(nt, ti))

    by_key: dict[tuple, list[int]] = {}
    for oi, c in enumerate(oclips):
        by_key.setdefault(_ckey(c), []).append(oi)
    used_old: set[int] = set()
    for ni, c in enumerate(nclips):
        bucket = by_key.get(_ckey(c)) or []
        oi = next((x for x in bucket if x not in used_old), None)
        if oi is None:
            src = Path(str(c.get("src", ""))).name if c.get("src") else ""
            where = f" {_span(c)}" if c.get("startMs") is not None else ""
            what = f"新增 1 个 {noun}" if is_overlay else f"新增 {noun}"
            added.append(f"{tlabel} 轨{what}{where}" + (f"({src})" if src else ""))
            continue
        used_old.add(oi)
        oc = oclips[oi]
        for f in ("startMs", "durationMs", "sourceInMs"):
            if oc.get(f) != c.get(f) and c.get(f) is not None:
                changed.append(f"{tlabel} 第{ni + 1}个片段 {f}: {oc.get(f)} → {c.get(f)}")
    for oi, c in enumerate(oclips):
        if oi not in used_old:
            removed.append(f"{tlabel} 轨第{oi + 1}个片段被删(原 {_span(c)})")
    return added, removed, changed


def _ckey(c: dict) -> tuple:
    """片段「内容身份」:src + sourceInMs(时间可被编辑,身份不变)。"""
    return (str(c.get("src", "")), str(c.get("sourceInMs", "")))


def cmd_diff(root: Path) -> int:
    pj = rs_paths.project_json(root)
    if not pj.is_file():
        return emit(False, "DEP_MISSING", f"缺 IR,无法对比:{pj}",
                    {"bridge": "editor"}, exit_code=3)
    try:
        project = _load(pj)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return emit(False, "INTERNAL", f"project.json 解析失败:{exc}",
                    {"bridge": "editor"}, exit_code=4)
    # 数据源 1:cutforge 会话摘要(RT-1;actor=human 的 Op 清单)
    summary = load_session_summary(root)
    if summary and summary.get("ops"):
        ops = summary["ops"]
        human = human_from_ops(project, ops)
        return emit(True, "DIFF_SESSION",
                    f"编辑器本次会话 {summary.get('revFrom')}→{summary.get('revTo')} rev,"
                    f"{len(human)} 条人工改动",
                    {"source": "session-summary", "revFrom": summary.get("revFrom"),
                     "revTo": summary.get("revTo"), "human": human, "ops": ops},
                    exit_code=0)
    # 数据源 2:基线快照 vs 当前盘面
    base = load_latest_base(root)
    if base is not None:
        rev, old = base
        human, machine = diff_tracks(old, project)
        if not human:
            human = ["盘面与基线一致(未发现片段级差异)"]
        return emit(True, "DIFF_BASE",
                    f"与基线快照 rev{rev} 对比:{len(human)} 条差异",
                    {"source": "bases", "baseRev": rev, "human": human,
                     "diff": machine},
                    exit_code=0)
    return emit(True, "NO_BASELINE",
                "无 session-summary 也无 bases/ 快照,无法识别编辑器改动"
                "(编辑器从未打开过本工程,或其版本未落会话摘要)",
                {"source": None, "hint": "打开 CutForge 编辑器改一次即会产生两种数据源"},
                exit_code=0)


def main() -> int:
    if "--probe" in sys.argv:
        return emit(True, "OK", "rs_editor 桥自检通过(协议/依赖可用)", {"bridge": "editor"}, exit_code=0)
    ap = argparse.ArgumentParser(description="CutFlow ↔ CutForge 编辑器桥(只读)")
    ap.add_argument("cmd", nargs="?", default="--probe",
                    choices=["view", "timeline", "check", "diff", "--probe"])
    ap.add_argument("root", nargs="?", help="工程目录")
    ap.add_argument("--json", action="store_true", help="JSON 输出(默认)")
    a = ap.parse_args()

    if a.cmd == "--probe":
        return emit(True, "OK", "rs_editor 桥自检通过(协议/依赖可用)", {"bridge": "editor"}, exit_code=0)

    if not a.root:
        # P23-1:输入错统一 PRECONDITION_FAILED(5.4 码表),不再用表外码 USAGE
        return emit(False, "PRECONDITION_FAILED", "需要工程目录参数", {}, exit_code=2)
    root = Path(a.root)
    if not root.is_dir():
        return emit(False, "PRECONDITION_FAILED", f"工程目录不存在: {root}", {}, exit_code=2)

    if a.cmd == "diff":
        return cmd_diff(root)

    if a.cmd == "view":
        p = rs_paths.project_json(root)
        if not p.is_file():
            return emit(False, "DEP_MISSING", f"缺 IR: {p}", {}, exit_code=3)
        return emit(True, "OK", "工程视图", {"project": _load(p), "rev_hint": "写操作请走 cutforge-cli/mcp"}, exit_code=0)

    if a.cmd == "timeline":
        p = rs_paths.project_json(root)
        if not p.is_file():
            return emit(False, "DEP_MISSING", f"缺 IR: {p}", {}, exit_code=3)
        return emit(True, "OK", "时间线", {"clips": timeline(_load(p))}, exit_code=0)

    # check
    found = {rel: (root / rel).is_file() for rel in truth_sources(root)}
    state_ok = (root / ".cutforge").is_dir()
    missing = [k for k, v in found.items() if not v]
    ok = not missing
    return emit(ok, "OK" if ok else "DEP_MISSING",
                "工程结构完整" if ok else f"缺文件: {missing}",
                {"truth_sources": found, "cutforge_state": state_ok,
                 "hint": None if ok else "工程未初始化或产物缺失;写操作走 cutforge-cli"},
                exit_code=0 if ok else 3)


if __name__ == "__main__":
    ensure_utf8()
    sys.exit(main())
