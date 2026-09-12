"""阶段缓存与增量编排(ADR-0013 / rules/incremental.md)。

用法(在工程根目录执行):
  rs_run.py --status                 阶段状态灯 ✓ / ⚠ stale / ✗ missing
  rs_run.py --explain S3             为什么 stale(逐个输入/参数/脚本 hash 对比)
  rs_run.py --only S7                只跑 S7
  rs_run.py --from S3                从 S3 起重跑
  rs_run.py --dirty                  只跑 stale 的阶段
  rs_run.py --mark S4                人工介入后标记为 done
  rs_run.py --plan --from S3         只打印将要执行的命令,不执行

缓存键 = sha1(上游产物内容 hash + 参数快照 + **本阶段脚本文件 hash** + 外部服务版本)。
粒度到 segment(见 rules/incremental.md §2)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit  # noqa: E402
import segmentation  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))
SKIP_DIRS = {"_state", ".git", "__pycache__"}

ONLY = "only"
FROM = "from"
S13 = "all"


def spec() -> list[dict]:
    """S0–S10 阶段注册表(rules/incremental.md §2 / SKILL.md §1)。"""
    return [
        {"id": "S0", "name": "基础素材", "manual": True,
         "inputs": ["00_brief/brief.md", "01_materials/*"],
         "outputs": ["01_materials/manifest.json"], "scripts": ["rs_ingest.py"],
         "cmd": ["rs_ingest.py", "scan", ".", "--slug", "{slug}"]},
        {"id": "S1", "name": "转写与字级对齐",
         "inputs": ["01_materials/*"], "outputs": ["05_ir/wordline.json"],
         "scripts": ["rs_align.py"],
         "cmd": ["rs_align.py", "build", "--media", "{first_material}",
                 "--out", "05_ir/wordline.json"]},
        {"id": "S2", "name": "粗剪处理",
         "inputs": ["05_ir/wordline.json"], "outputs": ["04_cut/cutlist.json"],
         "scripts": ["rs_cut.py"],
         "cmd": ["rs_cut.py", "05_ir/wordline.json", "--detect", "all", "--out", "04_cut"]},
        {"id": "S3", "name": "基础合成",
         "inputs": ["04_cut/cutlist.applied.json", "05_ir/wordline.json"],
         "outputs": ["05_ir/project.json"], "scripts": ["rs_ir.py", "rs_render.py"],
         "cmd": ["rs_ir.py", "build", "--from-cutlist", "04_cut/cutlist.applied.json",
                 "--slug", "{slug}", "--out", "05_ir/project.json"]},
        {"id": "S4", "name": "动画/信息卡", "manual": True,
         "inputs": ["05_ir/project.json"], "outputs": ["05_ir/project.json"],
         "scripts": []},
        {"id": "S5", "name": "品牌(Logo 变体)",
         "inputs": ["05_ir/project.json", "05_ir/variants.json"],
         "outputs": ["06_output/final_*.mp4"], "scripts": ["rs_brand.py"],
         "cmd": ["rs_brand.py", "05_ir/project.json", "--variants", "05_ir/variants.json",
                 "--out", "06_output"]},
        {"id": "S6", "name": "音效",
         "inputs": ["05_ir/project.json"], "outputs": ["_state/sfx.applied.json"],
         "scripts": ["rs_sfx.py"],
         "cmd": ["rs_sfx.py", "05_ir/project.json", "--auto", "--out", "05_ir/sfx_draft.json"]},
        {"id": "S7", "name": "字幕",
         "inputs": ["05_ir/wordline.json"], "outputs": ["06_output/subtitles.ass"],
         "scripts": ["rs_subtitle.py", "textopt.py", "segmentation.py"],
         "cmd": ["rs_subtitle.py", "--from-wordline", "05_ir/wordline.json",
                 "--style", "talkshow-bold", "--ratio", "9x16", "--out", "06_output"]},
        # S8 是"手改字幕"的落点:它**只用现有 ass 重新烧录导出**,不重新生成字幕。
        # 没有这一段,改完字幕的一键重建会把用户的修改冲掉(见 OPTIMIZATION-v5 §4.2)。
        {"id": "S8", "name": "烧录导出",
         "inputs": ["06_output/subtitles.ass", "05_ir/project.json"],
         "outputs": ["06_output/final_*.mp4"],
         "scripts": ["rs_render.py"],
         "cmd": ["rs_render.py", "05_ir/project.json", "--ratio", "9x16",
                 "--profile", "final"]},
        {"id": "S9", "name": "自评与对齐断言",
         "inputs": ["06_output/subtitles.ass"], "outputs": ["06_output/sync_report.md"],
         "scripts": ["rs_sync.py"],
         "cmd": ["rs_sync.py", "--wordline", "05_ir/wordline.json",
                 "--ass", "06_output/subtitles.ass", "--out", "06_output"]},
        {"id": "S10", "name": "封面与文案",
         "inputs": ["05_ir/wordline.json", "00_brief/brief.md"],
         "outputs": ["06_output/metadata.json"], "scripts": ["rs_meta.py"],
         "cmd": ["rs_meta.py", "--wordline", "05_ir/wordline.json",
                 "--brief", "00_brief/brief.md", "--platform", "douyin,bili",
                 "--out", "06_output"]},
        {"id": "S11", "name": "交付", "manual": True,
         "inputs": ["06_output/metadata.json"], "outputs": ["06_output/deliverables.md"],
         "scripts": []},
    ]


# ---------------------------------------------------------------- hash

def sha1_file(p: Path) -> str:
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def expand(root: Path, patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        for p in sorted(root.glob(pat)):
            if p.is_file() and not any(part in SKIP_DIRS for part in p.parts):
                out.append(p)
    return out


def stage_parts(root: Path, st: dict, params: dict, external: dict) -> dict:
    inputs, tool = {}, {}
    for p in expand(root, st["inputs"]):
        inputs[str(p.relative_to(root))] = sha1_file(p)
    for name in st["scripts"]:
        sp = SCRIPTS_DIR / name
        if sp.is_file():
            tool[name] = sha1_file(sp)
    return {"inputs": inputs, "params": params, "tool": tool, "external": external}


def key_of(parts: dict) -> str:
    return sha1_text(json.dumps(parts, sort_keys=True, ensure_ascii=False))


# ---------------------------------------------------------------- 状态

def state_path(root: Path, sid: str) -> Path:
    return root / "_state" / f"{sid}.json"


def read_state(root: Path, sid: str) -> dict | None:
    p = state_path(root, sid)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_state(root: Path, sid: str, doc: dict) -> None:
    d = root / "_state"
    d.mkdir(parents=True, exist_ok=True)
    state_path(root, sid).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    agg = {"version": 1, "slug": root.name,
           "updatedAt": datetime.now(CST).isoformat(timespec="seconds"),
           "stages": {s["id"]: (read_state(root, s["id"]) or {"status": "missing"})
                      for s in spec()}}
    (root / "05_ir").mkdir(parents=True, exist_ok=True)
    (root / "05_ir" / "pipeline.json").write_text(
        json.dumps(agg, ensure_ascii=False, indent=1), encoding="utf-8")


def params_of(root: Path) -> dict:
    """阶段参数快照:从 pipeline.json 读,缺省用默认值(保证旧工程可复现)。"""
    p = root / "05_ir" / "pipeline.json"
    if p.is_file():
        try:
            return (json.loads(p.read_text(encoding="utf-8")).get("params") or {})
        except json.JSONDecodeError:
            pass
    return {"maxChars": dict(segmentation.MAX_CHARS), "cpsMax": 9}


def evaluate(root: Path, st: dict) -> dict:
    """返回 {"status": done|stale|missing|manual, "staleReason": [...]}"""
    rec = read_state(root, st["id"])
    outs = expand(root, st["outputs"])
    if st.get("manual") and not rec:
        return {"status": "done" if outs else "missing", "staleReason": [], "manual": True}
    if not rec:
        return {"status": "missing", "staleReason": ["从未记录状态"], "outs": len(outs)}
    cur = stage_parts(root, st, rec.get("parts", {}).get("params", {}),
                      rec.get("parts", {}).get("external", {}))
    diff = diff_parts(rec.get("parts") or {}, cur)
    if diff:
        return {"status": "stale", "staleReason": diff, "outs": len(outs)}
    if not outs and st["outputs"]:
        return {"status": "missing", "staleReason": ["产物缺失"], "outs": 0}
    return {"status": "done", "staleReason": [], "outs": len(outs)}


def diff_parts(old: dict, new: dict) -> list[str]:
    why: list[str] = []
    for group in ("inputs", "tool", "external", "params"):
        o, n = old.get(group) or {}, new.get(group) or {}
        for k in sorted(set(o) | set(n)):
            if o.get(k) != n.get(k):
                if k not in o:
                    why.append(f"{group} 新增 {k}")
                elif k not in n:
                    why.append(f"{group} 移除 {k}")
                else:
                    why.append(f"{group} 变化 {k}({str(o[k])[:10]} → {str(n[k])[:10]})")
    return why


STATUS_ICON = {"done": "✓", "stale": "⚠", "missing": "✗"}


def cmd_status(root: Path) -> int:
    lines, data = [], []
    for st in spec():
        r = evaluate(root, st)
        icon = STATUS_ICON.get(r["status"], "?")
        extra = f"  ({r['staleReason'][0][:60]})" if r.get("staleReason") else ""
        extra += "  [人工阶段]" if st.get("manual") else ""
        lines.append(f"{st['id']} {icon} {r['status']:<8} {st['name']}{extra}")
        data.append({"id": st["id"], "name": st["name"], **r})
    print("\n".join(lines))
    return emit(True, "STATUS_OK", f"{sum(1 for d in data if d['status'] == 'done')}/{len(data)} 阶段已完成",
                {"stages": data})


def cmd_explain(root: Path, sid: str) -> int:
    st = next((s for s in spec() if s["id"] == sid), None)
    if not st:
        return emit(False, "BAD_STAGE", f"未知阶段:{sid}", exit_code=2)
    r = evaluate(root, st)
    return emit(True, "EXPLAIN_OK",
                f"{sid} = {r['status']}" + ("" if not r["staleReason"] else ": " + "; ".join(r["staleReason"])),
                {"stage": sid, **r})


# ---------------------------------------------------------------- 备份 / 一键重建

BACKUP_KEEP = 5


def backup_paths(root: Path, st: dict) -> Path | None:
    """把该阶段将覆盖的产物备份到 _state/backup/<时间戳>/(手改成果的唯一保险)。"""
    files = expand(root, st["outputs"])
    if not files:
        return None
    ts = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    dest = root / "_state" / "backup" / ts
    for f in files:
        rel = f.relative_to(root)
        tgt = dest / rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, tgt)
    (dest / "_manifest.json").write_text(json.dumps(
        {"stage": st["id"], "at": ts, "files": [str(f.relative_to(root)) for f in files]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    _prune_backups(root)
    return dest


def _prune_backups(root: Path) -> None:
    bdir = root / "_state" / "backup"
    if not bdir.is_dir():
        return
    items = sorted([d for d in bdir.iterdir() if d.is_dir()], key=lambda d: d.name)
    for old in items[:-BACKUP_KEEP]:
        shutil.rmtree(old, ignore_errors=True)


def rollback(root: Path, at: str = "") -> tuple[bool, str]:
    bdir = root / "_state" / "backup"
    if not bdir.is_dir():
        return False, "没有可用的备份"
    items = sorted([d for d in bdir.iterdir() if d.is_dir()], key=lambda d: d.name)
    if not items:
        return False, "没有可用的备份"
    target = next((d for d in reversed(items) if d.name == at), None) if at else items[-1]
    if target is None:
        return False, f"找不到备份 {at}(可用:{', '.join(d.name for d in items)})"
    n = 0
    for f in target.rglob("*"):
        if f.is_file() and f.name != "_manifest.json":
            rel = f.relative_to(target)
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            n += 1
    return True, f"已从备份 {target.name} 还原 {n} 个文件"


REBUILD_TMPL = '''"""一键重建 —— {label}

改完 `{folder}/` 里的东西后运行本脚本。它会:
  1) 备份将被覆盖的产物到 _state/backup/
  2) 从 {sid} 级联重跑(上游命中缓存,所以很快)
  3) 跑完输出成片 + 自检报告

只改这一个文件夹 → 只点这一个脚本。不要手动去调 rs_render。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[{depth}]
RUNNER = r"{runner}"
sys.exit(subprocess.run(
    [sys.executable, RUNNER, "--root", str(ROOT), "--from", "{sid}", "--force"],
    cwd=str(ROOT)).returncode)
'''

INIT_MAP = [("04_cut", "S2", "粗剪决策(CutList)"), ("05_ir", "S3", "IR / Wordline"),
            ("03_assets/artboard", "S4", "artboard 卡片"),
            ("06_output", "S8", "字幕(改完只重烧录导出,不重新生成字幕)")]


ARTBOARD_REBUILD_TMPL = '''"""一键重建 —— artboard 卡片

改完卡片源码后运行本脚本。它会:
  1) 只重导出**源码变了**的卡片(内容寻址)
  2) 把新产物回填 IR(尺寸/时长校验;尺寸不符会停住而不是拉伸)
  3) 从 S4 级联重跑(上游走缓存)→ 出片 + 自检
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(r"{scripts}")
MANIFEST = ROOT / "03_assets" / "artboard" / "manifest.json"


def run(*args):
    p = subprocess.run([sys.executable, *[str(x) for x in args]], cwd=str(ROOT))
    if p.returncode != 0:
        sys.exit(p.returncode)


if not MANIFEST.is_file():
    run(SCRIPTS / "rs_artboard.py", "--root", ROOT, "--scan",
        ROOT / "03_assets" / "artboard", "--out", MANIFEST)
run(SCRIPTS / "rs_artboard.py", "--root", ROOT, MANIFEST, "--export")
run(SCRIPTS / "rs_artboard.py", "--root", ROOT, MANIFEST, "--apply",
    ROOT / "05_ir" / "project.json")
run(SCRIPTS / "rs_run.py", "--root", ROOT, "--from", "S4", "--force")
'''


def init_rebuild(root: Path) -> list[str]:
    """在每个阶段文件夹里放一个 rebuild.py(薄壳),用户只需知道"改哪点哪"。"""
    runner = Path(__file__).resolve()
    made = []
    for folder, sid, label in INIT_MAP:
        d = root / folder
        if not d.is_dir() and folder != "06_output":
            continue
        d.mkdir(parents=True, exist_ok=True)
        if folder == "03_assets/artboard":
            body = ARTBOARD_REBUILD_TMPL.format(scripts=SCRIPTS_DIR)
        else:
            body = REBUILD_TMPL.format(label=label, folder=folder, sid=sid, runner=str(runner),
                                       depth=1)
        (d / "rebuild.py").write_text(body, encoding="utf-8")
        made.append(f"{folder}/rebuild.py")
    body = REBUILD_TMPL.format(label="全量重建", folder="工程根", sid="S0", runner=str(runner),
                               depth=0)
    (root / "rebuild.py").write_text(body, encoding="utf-8")
    made.append("rebuild.py")
    readme = ["# 改了东西怎么办?", "",
              "| 你改了什么 | 运行哪个脚本 |", "|---|---|",
              "| 字幕(06_output/subtitles.ass) | `python 06_output/rebuild.py` |",
              "| IR 或 wordline(05_ir/) | `python 05_ir/rebuild.py` |",
              "| 粗剪决策(04_cut/cutlist*.json) | `python 04_cut/rebuild.py` |",
              "| artboard 卡片(03_assets/artboard/) | `python 03_assets/artboard/rebuild.py` |",
              "| 拿不准 | `python rebuild.py`(全量) |", "",
              "每个脚本都会**先备份**再重跑,跑砸了可以 `--rollback` 还原。"]
    (root / "REBUILD.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
    made.append("REBUILD.md")
    return made


# ---------------------------------------------------------------- 验证

def run_verify(root: Path, level: str) -> tuple[bool, str, dict]:
    """按状态决定跑 L0 还是 L0+L1。**输出必须带 verifyLevel / firstCheckDone。**"""
    cmd = [sys.executable, str(SCRIPTS_DIR / "rs_verify.py"), str(root)]
    if level == "L1":
        cmd += ["--level", "L1"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        doc = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return False, f"自检脚本无有效输出:{(p.stderr or p.stdout or '')[-200:]}", {}
    data = doc.get("data") or {}
    return bool(doc.get("ok")), doc.get("message", ""), data


def verify_policy(root: Path) -> tuple[str, str]:
    """返回 (该跑的级别, 原因)。"""
    vp = root / "_state" / "verify.json"
    first_done = False
    if vp.is_file():
        try:
            first_done = bool((json.loads(vp.read_text(encoding="utf-8"))
                               .get("firstCheck") or {}).get("done"))
        except json.JSONDecodeError:
            pass
    if not first_done:
        return "L1", "首次检查(未做过全量验证)"
    # 只有"曾经做过、现在失效"才算画面变更;从未记录状态(没跑过)不算
    changed = []
    for sid in ("S3", "S4", "S5"):
        if read_state(root, sid) is None:
            continue
        st = next(s for s in spec() if s["id"] == sid)
        if evaluate(root, st)["status"] != "done":
            changed.append(sid)
    if changed:
        return "L1", f"画面相关阶段失效:{','.join(changed)}"
    return "L0", "非首次且画面未变(改字幕/文案只跑机械自检)"


# ---------------------------------------------------------------- 执行

def build_cmd(root: Path, st: dict) -> list[str] | None:
    if not st.get("cmd"):
        return None
    mats = expand(root, ["01_materials/*"])
    mapping = {"{first_material}": str(mats[0].relative_to(root)) if mats else "01_materials/",
               "{slug}": root.name}
    out = [sys.executable, str(SCRIPTS_DIR / st["cmd"][0])]
    for tok in st["cmd"][1:]:
        out.append(mapping.get(tok, tok))
    return out


def run_stage(root: Path, st: dict) -> tuple[bool, str]:
    cmd = build_cmd(root, st)
    if cmd is None:
        return False, f"{st['id']} 是人工阶段(Agent 介入),完成后用 --mark {st['id']}"
    p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    if p.returncode != 0:
        return False, f"{st['id']} 失败(exit {p.returncode}):{(p.stderr or p.stdout or '')[-300:]}"
    parts = stage_parts(root, st, params_of(root), external_versions())
    write_state(root, st["id"], {"status": "done", "key": key_of(parts), "parts": parts,
                                 "ts": datetime.now(CST).isoformat(timespec="seconds")})
    return True, f"{st['id']} ✓ {st['name']}"


def external_versions() -> dict:
    """外部服务版本(缓存键的一部分,防模型/服务升级后误命中缓存)。"""
    ext = {}
    try:
        cfg = json.loads((SCRIPTS_DIR.parents[2] / "config.json").read_text(encoding="utf-8"))
        ext["asr_model"] = cfg.get("asr", {}).get("model", "")
        ext["asr_url"] = cfg.get("asr", {}).get("url", "")
    except Exception:  # noqa: BLE001
        pass
    ext["detector"] = "cutflow-1.0"
    return ext


def select(root: Path, mode: str, target: str | None) -> list[dict]:
    stages = spec()
    ids = [s["id"] for s in stages]
    if mode == ONLY:
        return [s for s in stages if s["id"] == target]
    if mode == FROM:
        if target not in ids:
            return []
        return stages[ids.index(target):]
    # dirty
    out = []
    for st in stages:
        r = evaluate(root, st)
        if r["status"] != "done":
            out.append(st)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--from", dest="from_stage")
    ap.add_argument("--only", dest="only_stage")
    ap.add_argument("--dirty", action="store_true")
    ap.add_argument("--explain")
    ap.add_argument("--mark")
    ap.add_argument("--plan", action="store_true", help="只打印将执行的命令")
    ap.add_argument("--force", action="store_true",
                    help="强制重跑起点阶段(上游仍走缓存);跑前自动备份其产物")
    ap.add_argument("--init", action="store_true", help="在阶段文件夹生成 rebuild.py")
    ap.add_argument("--rollback", action="store_true", help="还原最近一次备份")
    ap.add_argument("--at", default="", help="--rollback 指定备份时间戳")
    ap.add_argument("--verify", action="store_true", help="按策略跑 L0 / L1 自检")
    ap.add_argument("--verify-full", dest="verify_full", action="store_true",
                    help="强制跑 L1(含待目测清单)")
    a = ap.parse_args()

    root = Path(a.root).resolve()

    if a.init:
        if not root.is_dir():
            return emit(False, "NO_PROJECT", f"工程目录不存在:{root}", exit_code=2)
        made = init_rebuild(root)
        return emit(True, "INIT_OK", f"已生成 {len(made)} 个一键重建脚本", {"files": made})

    if a.rollback:
        ok, msg = rollback(root, a.at)
        return emit(ok, "ROLLBACK_OK" if ok else "ROLLBACK_FAIL", msg,
                    exit_code=0 if ok else 2)

    if a.verify or a.verify_full:
        level, why = verify_policy(root)
        if a.verify_full:
            level, why = "L1", "显式要求全量(L1)"
        ok, msg, data = run_verify(root, level)
        payload = {"verifyLevel": level, "firstCheckDone": data.get("firstCheckDone"),
                   "reason": why, "l0Pass": data.get("pass"),
                   "failed": data.get("failed") or [], "report": data.get("report")}
        if level == "L1":
            payload["l1"] = data.get("l1")
            payload["needsAgentReview"] = True
        return emit(ok, "VERIFY_OK" if ok else "VERIFY_FAIL",
                    f"[{level}] {msg} —— 原因:{why}", payload,
                    exit_code=0 if ok else 4)

    if a.explain:
        return cmd_explain(root, a.explain)
    if a.mark:
        st = next((s for s in spec() if s["id"] == a.mark), None)
        if not st:
            return emit(False, "BAD_STAGE", f"未知阶段:{a.mark}", exit_code=2)
        parts = stage_parts(root, st, params_of(root), external_versions())
        write_state(root, st["id"], {"status": "done", "key": key_of(parts), "parts": parts,
                                     "manual": True,
                                     "ts": datetime.now(CST).isoformat(timespec="seconds")})
        return emit(True, "MARK_OK", f"{a.mark} 已标记完成", {"stage": a.mark})
    if a.status or not (a.from_stage or a.only_stage or a.dirty):
        return cmd_status(root)

    mode, target = (FROM, a.from_stage) if a.from_stage else \
                   ((ONLY, a.only_stage) if a.only_stage else (S13, None))
    todo = select(root, mode, target)
    if not todo:
        return emit(False, "BAD_STAGE", f"未知阶段:{target}", exit_code=2)

    if a.plan:
        plan = [{"stage": s["id"], "name": s["name"],
                 "cmd": " ".join(build_cmd(root, s) or ["<人工阶段>"])} for s in todo]
        for item in plan:
            print(f"{item['stage']} {item['name']}: {item['cmd']}")
        return emit(True, "PLAN_OK", f"{len(plan)} 个阶段待执行", {"plan": plan})

    forced_id = target if a.force else None
    t0 = time.time()
    results = []
    for st in todo:
        if st.get("manual"):
            results.append({"stage": st["id"], "ok": True, "skipped": "人工阶段"})
            continue
        r = evaluate(root, st)
        forced = (forced_id is not None and st["id"] == forced_id)
        if r["status"] == "done" and not forced:
            results.append({"stage": st["id"], "ok": True, "cached": True})
            continue
        backed = None
        if forced:
            bp = backup_paths(root, st)
            backed = str(bp) if bp else None
        ok, msg = run_stage(root, st)
        results.append({"stage": st["id"], "ok": ok, "message": msg, "forced": forced,
                        "backup": backed})
        if not ok:
            return emit(False, "STAGE_FAILED", msg, {"results": results,
                                                     "elapsedSec": round(time.time() - t0, 2)},
                        exit_code=4)

    elapsed = round(time.time() - t0, 2)
    hit = sum(1 for r in results if r.get("cached"))
    level, why = verify_policy(root)
    okv, vmsg, vdata = run_verify(root, level)
    first_done = bool(vdata.get("firstCheckDone"))
    msg = f"{len(results)} 阶段({hit} 命中缓存),耗时 {elapsed}s;[{level}] {vmsg}"
    if level == "L1":
        msg += "(需 Agent 目测;详见 06_output/verify_report.md)"
    return emit(okv, "RUN_OK" if okv else "RUN_VERIFY_FAIL", msg,
                {"results": results, "cached": hit, "elapsedSec": elapsed,
                 "verifyLevel": level, "firstCheckDone": first_done,
                 "verifyReason": why,
                 "report": vdata.get("report"), "failed": vdata.get("failed") or []},
                exit_code=0 if okv else 4)


if __name__ == "__main__":
    sys.exit(main())
