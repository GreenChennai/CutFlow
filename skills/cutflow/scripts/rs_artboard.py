"""artboard 闭环:改完图 → 一键导出 → 回填 IR → 出片(rules/artboard.md)。

用法:
  # ① 扫描 artboard 工程生成/更新清单
  rs_artboard.py --scan 03_assets/artboard --out 03_assets/artboard/manifest.json

  # ② 只重导出源码变了的卡片(内容寻址,秒级)
  rs_artboard.py 03_assets/artboard/manifest.json --export

  # ③ 把新产物回填 IR(尺寸/时长校验),并报告受影响的下游阶段
  rs_artboard.py 03_assets/artboard/manifest.json --apply 05_ir/project.json

  # ④ 一条龙:②+③+从 S4 级联重跑 —— 由 03_assets/artboard/rebuild.py 调用

清单是唯一映射表:artboard 工程 ↔ 导出产物 ↔ IR 挂点(manifest.json)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import RATIOS, REPO_ROOT, emit  # noqa: E402

REGISTRY = "manifest.json"
SIZE_BY_RATIO = dict(RATIOS)


# ---------------------------------------------------------------- 归一助手(v0.8.1)

def _src_dir(root: Path, item: dict) -> Path:
    """卡片源码目录:manifest 的 project 带 /src 后缀(v0.8.1 起的 scan 口径)或
    不带(旧清单)都能归一到 **src 目录**——artboard 导出脚本的 --source 吃的是它,
    hash 也必须按它算(旧版 scan 按 src、changed/export 按卡片目录,口径不一致)。"""
    p = root / item["project"]
    if p.name != "src" and (p / "src").is_dir():
        return p / "src"
    return p


def _norm_path(p: str | Path, root: Path) -> str:
    """路径归一:相对/绝对/正反斜杠统一成「root 下解析后的 posix 小写」。

    IR 里的 clip src 可以写工程根相对路径,也可以写绝对路径——旧版字符串全等
    匹配在多变体/手工改路径的场景下匹配不上。
    """
    q = Path(str(p))
    if not q.is_absolute():
        q = root / q
    return os.path.normpath(str(q)).replace("\\", "/").lower()


def _load_artboard_config(repo_cfg: Path | None = None,
                          local_cfg: Path | None = None) -> dict:
    """config 查找(v0.8.1 修 junction 错位):仓库根优先,skills/config.json 兜底。

    junction 安装下 parents[2] 指向 <repo>/skills——旧实现只查那里,仓库根的
    config.json(含 artboard_dir)反而读不到。两处都读:仓库根键优先(单一事实源),
    兜底文件只补缺(通常只含 artboard_dir)。
    """
    def _read(p: Path | None) -> dict:
        if p is None or not p.is_file():
            return {}
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    merged = dict(_read(repo_cfg if repo_cfg is not None else REPO_ROOT / "config.json"))
    for k, v in _read(local_cfg if local_cfg is not None
                      else Path(__file__).resolve().parents[2] / "config.json").items():
        merged.setdefault(k, v)
    return merged


# ---------------------------------------------------------------- 清单

def hash_source(project: Path) -> str:
    """artboard 工程源码 hash(排除 export/ 与 __pycache__)。"""
    h = hashlib.sha1()
    if not project.is_dir():
        return ""
    for f in sorted(project.rglob("*")):
        if not f.is_file():
            continue
        if "export" in f.parts or "__pycache__" in f.parts:
            continue
        h.update(str(f.relative_to(project)).encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()


def scan(root: Path, artboard_dir: str = "") -> dict:
    """扫描 03_assets/artboard 下的卡片工程(scaffold 产出 src/ 的目录)。

    v0.8.1:`project` 统一写 `<卡片>/src`(导出 --source 与 hash 的真实口径)。
    """
    items = []
    for proj in sorted(p for p in root.rglob("src") if p.is_dir()):
        card = proj.parent
        rel = card.relative_to(root).as_posix()
        out_rel = f"{rel}/export/{card.name}.png"
        items.append({"id": card.name, "project": f"{rel}/src", "sourceHash": hash_source(proj),
                      "output": out_rel, "kind": "png", "size": [1080, 1920],
                      "durationMs": None, "usedIn": []})
    return {"version": 1, "artboardDir": artboard_dir, "root": ".", "items": items}


def load_manifest(path: Path) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc.setdefault("items", [])
    return doc


def save_manifest(doc: dict, path: Path) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def changed_items(doc: dict, root: Path) -> list[dict]:
    out = []
    for it in doc["items"]:
        cur = hash_source(_src_dir(root, it))
        if cur != it.get("sourceHash"):
            out.append(it)
    return out


# ---------------------------------------------------------------- 导出

def export_python(artboard_dir: Path) -> Path | None:
    for rel in ("scripts/export.py", "export.py"):
        p = artboard_dir / rel
        if p.is_file():
            return p
    return None


def export_item(item: dict, root: Path, artboard_dir: Path, timeout: int = 900) -> tuple[bool, str]:
    script = export_python(artboard_dir)
    if script is None:
        return False, f"找不到 artboard 导出脚本({artboard_dir}/scripts/export.py)"
    w, h = item.get("size") or SIZE_BY_RATIO["9x16"]
    out = root / item["output"]
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(script), "--source", str(_src_dir(root, item)),
           "--output", str(out), "--width", str(w), "--height", str(h)]
    if item.get("kind") == "mp4":
        cmd += ["--format", "MP4", "--fps", str(item.get("fps") or 25)]
    elif item.get("kind") == "gif":
        cmd += ["--format", "GIF"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 or not out.is_file():
        return False, (p.stderr or p.stdout or "导出失败")[-300:]
    return True, str(out)


# ---------------------------------------------------------------- 回填 IR

def probe_duration_ms(path: Path) -> int | None:
    try:
        from rs_common import media_duration_s
        return int(media_duration_s(path) * 1000)
    except Exception:  # noqa: BLE001
        return None


def shift_track(clips: list[dict], index: int, delta_ms: int) -> int:
    """第 index 个 clip 之后的所有 clip 平移 delta_ms(卡片时长变化的下游传播)。"""
    n = 0
    for c in clips[index + 1:]:
        c["startMs"] = int(c.get("startMs", 0)) + delta_ms
        n += 1
    return n


def apply_to_ir(doc: dict, ir: dict, root: Path, *, strict: bool = False,
                only: set[str] | None = None) -> tuple[dict, list[str], list[dict], list[dict]]:
    """把清单里的产物路径与尺寸回填 IR。返回 (新 IR, 硬问题, 变更, 跳过)。

    v0.8.1:
    · 匹配 = 归一化绝对路径(相对/绝对/反斜杠一视同仁),不再是字符串全等;
    · 未被 IR 引用的卡片**默认跳过+告警**(skipped),`--strict` 才恢复硬失败——
      多变体/多场景不再要求"清单卡片全部被单个 IR 引用";
    · `only` 过滤后未选中的卡片不参与校验(配 CLI 的 --only id1,id2)。
    """
    issues: list[str] = []
    changes: list[dict] = []
    skipped: list[dict] = []
    items = [it for it in doc["items"] if not only or it["id"] in only]
    canvas = ir.get("canvas") or {}
    cw, ch = canvas.get("width"), canvas.get("height")

    by_out = {_norm_path(it["output"], root): it for it in items}
    referenced: set[str] = set()
    for ti, track in enumerate(ir.get("tracks", [])):
        if track.get("kind") != "video":
            continue
        clips = track.get("clips", [])
        for ci, clip in enumerate(clips):
            raw_src = str(clip.get("src", "")).strip()
            src_norm = _norm_path(raw_src, root) if raw_src else ""
            referenced.add(src_norm)
            item = by_out.get(src_norm)
            if item is None:
                continue
            full = root / item["output"]
            if not full.is_file():
                issues.append(f"{item['id']}:产物不存在,先跑 --export({full})")
                continue
            w, h = item.get("size") or (cw, ch)
            if (w, h) != (cw, ch):
                issues.append(f"{item['id']}:导出尺寸 {w}x{h} 与画幅 {cw}x{ch} 不符"
                              f"(会拉伸;请按画幅重导)")
                continue
            old_dur = int(clip.get("durationMs", 0))
            new_dur = old_dur
            if item.get("kind") == "mp4":
                probed = probe_duration_ms(full)
                if probed:
                    new_dur = probed
            if new_dur != old_dur and old_dur > 0:
                delta = new_dur - old_dur
                shift_track(clips, ci, delta)
                clip["durationMs"] = new_dur
                changes.append({"id": item["id"], "track": ti, "clipIndex": ci,
                                "oldDurationMs": old_dur, "newDurationMs": new_dur,
                                "shiftedClips": sum(1 for _ in clips[ci + 1:])})
            else:
                clip["durationMs"] = new_dur or old_dur
            changes.append({"id": item["id"], "track": ti, "clipIndex": ci, "path": item["output"],
                            "size": [w, h], "srcHash": item.get("sourceHash", "")[:10]})
            item.setdefault("usedIn", [])
            if not any(u.get("track") == ti and u.get("clipIndex") == ci for u in item["usedIn"]):
                item["usedIn"].append({"track": ti, "clipIndex": ci,
                                       "startMs": clip.get("startMs"), "durationMs": clip.get("durationMs")})
    missing = [it for it in items if _norm_path(it["output"], root) not in referenced]
    for it in missing:
        if strict:
            issues.append(f"{it['id']}:清单里有卡片没挂进 IR({it['output']})——"
                          f"请先在 IR overlay 轨引用其产物路径")
        else:
            skipped.append({"id": it["id"], "output": it["output"],
                            "note": "未被当前 IR 引用,已跳过(--strict 可改为硬失败)"})
    return ir, issues, changes, skipped


def stale_stages(changes: list[dict]) -> list[str]:
    """时长变化 → 下游要重跑;仅路径变化 → S4 起。"""
    dur_changed = any(c.get("oldDurationMs") for c in changes)
    return ["S4", "S5", "S6", "S7", "S8", "S9"] if dur_changed else ["S4", "S5", "S8"]


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", nargs="?")
    ap.add_argument("--root", default=".")
    ap.add_argument("--scan")
    ap.add_argument("--out", default="")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--apply", dest="apply_ir", default="")
    ap.add_argument("--only", default="", help="只处理指定卡片 id(逗号分隔)")
    ap.add_argument("--strict", action="store_true",
                    help="apply 时未被 IR 引用的卡片按硬失败处理(默认跳过+告警)")
    ap.add_argument("--force", action="store_true", help="忽略 source hash,全部重导")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    cfg = _load_artboard_config()
    artboard_dir = Path(cfg.get("artboard_dir") or "")

    if a.scan:
        out = Path(a.out or (Path(a.scan) / REGISTRY))
        doc = scan(Path(a.scan).resolve(), str(artboard_dir))
        out.parent.mkdir(parents=True, exist_ok=True)
        save_manifest(doc, out)
        return emit(True, "SCAN_OK", f"扫描到 {len(doc['items'])} 个 artboard 卡片 → {out}",
                    {"path": str(out), "count": len(doc["items"])})

    if not a.manifest:
        return emit(False, "NO_MANIFEST", "需要 <manifest.json>,或用 --scan <artboard 目录> 生成", exit_code=2)
    mpath = Path(a.manifest)
    if not mpath.is_file():
        return emit(False, "NO_MANIFEST", f"清单不存在:{mpath}(先跑 --scan)", exit_code=2)
    doc = load_manifest(mpath)
    only = {x.strip() for x in a.only.split(",") if x.strip()}

    if a.export:
        if not artboard_dir.is_dir():
            return emit(False, "NO_ARTBOARD",
                        f"artboard 技能目录不存在:{artboard_dir};请在 config.artboard_dir 配置", exit_code=3)
        todo = [it for it in doc["items"] if (not only or it["id"] in only)
                and (a.force or hash_source(_src_dir(root, it)) != it.get("sourceHash"))]
        if not todo:
            return emit(True, "EXPORT_SKIP", "所有卡片源码未变,无需重导", {"exported": 0})
        okd, failed = [], []
        for it in todo:
            ok, info = export_item(it, root, artboard_dir)
            if ok:
                it["sourceHash"] = hash_source(_src_dir(root, it))
                okd.append(it["id"])
            else:
                failed.append({"id": it["id"], "error": info})
        save_manifest(doc, mpath)
        msg = f"重导 {len(okd)}/{len(todo)} 个卡片"
        if failed:
            msg += f";失败 {len(failed)}"
        return emit(not failed, "EXPORT_OK" if not failed else "EXPORT_PARTIAL", msg,
                    {"exported": okd, "failed": failed}, exit_code=0 if not failed else 4)

    if a.apply_ir:
        ir_path = Path(a.apply_ir)
        if not ir_path.is_file():
            return emit(False, "NO_IR", f"IR 不存在:{ir_path}", exit_code=2)
        ir = json.loads(ir_path.read_text(encoding="utf-8"))
        ir, issues, changes, skipped = apply_to_ir(doc, ir, root, strict=a.strict, only=only)
        if issues:
            return emit(False, "APPLY_ISSUES",
                        f"{len(issues)} 个问题,已停止(不带着坏输入往下跑)",
                        {"issues": issues}, exit_code=4)
        ir_path.write_text(json.dumps(ir, ensure_ascii=False, indent=1), encoding="utf-8")
        save_manifest(doc, mpath)
        st = stale_stages(changes)
        msg = f"回填 {len(changes)} 个卡片;下游需重跑:{','.join(st)}"
        if skipped:
            msg += f";跳过 {len(skipped)} 个未引用卡片:{','.join(s['id'] for s in skipped[:5])}"
        return emit(True, "APPLY_OK", msg,
                    {"changes": changes, "skipped": skipped, "staleStages": st,
                     "ir": str(ir_path)})

    return emit(False, "NO_ACTION", "需要 --export 或 --apply <ir>", exit_code=2)


if __name__ == "__main__":
    sys.exit(main())
