"""artboard 闭环:改完图 → 一键导出 → 回填 IR → 出片(rules/artboard.md)。

用法:
  # ⓪ 从卡片计划 JSON 批量生成卡片工程并登记 manifest(T1-1 升格,原 _gen_cards.py)
  rs_artboard.py gen-cards --from 00_brief/cards.json [--out 03_assets/artboard/manifest.json] [--ratio 9x16]

  # ① 扫描 artboard 工程生成/更新清单
  rs_artboard.py --scan 03_assets/artboard --out 03_assets/artboard/manifest.json

  # ② 只重导出源码变了的卡片(内容寻址,秒级);P6-2:产物不在盘也重导(存在才 skip)
  rs_artboard.py 03_assets/artboard/manifest.json --export

  # ②' 主引擎不可用时的浏览器截图兜底导出(升格自 export_fallback.py)
  rs_artboard.py export-fallback 03_assets/artboard/manifest.json [--only id1,id2] [--scale 1] [--transparent]

  # ③ 把新产物回填 IR(尺寸/时长校验),并报告受影响的下游阶段
  rs_artboard.py 03_assets/artboard/manifest.json --apply 05_ir/project.json

  # ④ 一条龙:②+③+从 S4 级联重跑 —— 由 03_assets/artboard/rebuild.py 调用

清单是唯一映射表:artboard 工程 ↔ 导出产物 ↔ IR 挂点(manifest.json)。
卡片计划 JSON 与 rs_ir.py add-overlay 共用(内容字段归 gen-cards,时间窗字段归 add-overlay)。
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import RATIOS, REPO_ROOT, emit, write_text_atomic  # noqa: E402

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
    # 原子写(P15-1 同纪律):临时文件 + os.replace,中断不留半个清单
    write_text_atomic(path, json.dumps(doc, ensure_ascii=False, indent=1))


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
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"导出超时(>{timeout}s;P24-1:子进程必须带超时)"
    if p.returncode != 0 or not out.is_file():
        return False, (p.stderr or p.stdout or "导出失败")[-300:]
    return True, str(out)


def fallback_python(artboard_dir: Path) -> Path | None:
    """兜底导出脚本(Playwright 截图,升格自 artboard 技能的 scripts/export_fallback.py)。"""
    for rel in ("scripts/export_fallback.py", "export_fallback.py"):
        p = artboard_dir / rel
        if p.is_file():
            return p
    return None


def export_item_fallback(item: dict, root: Path, artboard_dir: Path, *, scale: int = 1,
                         transparent: bool = False, timeout: int = 900) -> tuple[bool, str]:
    """浏览器截图兜底导出:主引擎(Kiln)不可用/导不出时的独立通道(PNG)。

    与主路径同一张清单、同一套产物落点(<id>/export/<id>.png),导完同样回写
    sourceHash —— 下游 --apply / rebuild 不区分产物出自哪个引擎。
    """
    script = fallback_python(artboard_dir)
    if script is None:
        return False, f"找不到兜底导出脚本({artboard_dir}/scripts/export_fallback.py)"
    w, h = item.get("size") or SIZE_BY_RATIO["9x16"]
    out = root / item["output"]
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(script), "--source", str(_src_dir(root, item)),
           "--output", str(out), "--width", str(w), "--height", str(h)]
    if scale != 1:
        cmd += ["--scale", str(scale)]
    if transparent:
        cmd += ["--transparent"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"兜底导出超时(>{timeout}s;P24-1:子进程必须带超时)"
    if p.returncode != 0 or not out.is_file():
        return False, (p.stderr or p.stdout or "兜底导出失败")[-300:]
    return True, str(out)


# ---------------------------------------------------------------- gen-cards(T1-1 升格,原 _gen_cards.py)

# 计划字段白名单:内容字段归 gen-cards,时间窗字段归 rs_ir.py add-overlay(共用一份 cards.json)。
# P8 教训(手挂轨只写 schema 允许的字段)同源:白名单外字段直接报错,不静默吞。
PLAN_CONTENT_KEYS = {"id", "template", "title", "lines", "accent", "kicker"}
PLAN_TIME_KEYS = {"card", "startMs", "durationMs", "motion", "freezeMs"}
PLAN_KNOWN_KEYS = PLAN_CONTENT_KEYS | PLAN_TIME_KEYS
PLAN_TEMPLATES = ("info", "stat", "section")
ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# 确定性卡片模板:同一份计划必得同一字节(无时间戳/无随机);字号与安全区按
# rules/artboard.md(ADR-0009:顶部 12%、底部 30% 字幕带、左右 8% 不放内容)。
_CARD_CSS = """
  * { margin:0; padding:0; box-sizing:border-box; }
  body { width:%(w)dpx; height:%(h)dpx; overflow:hidden; background:#0E1116; color:#F5F7FA;
         font-family:"MiSans","Source Han Sans SC","Noto Sans SC","Microsoft YaHei",sans-serif; }
  .card { width:100%%; height:100%%; display:flex; flex-direction:column; justify-content:center;
          padding:%(pad_t)dpx %(pad_x)dpx %(pad_b)dpx; }
  .kicker { font-size:34px; font-weight:500; letter-spacing:6px; color:%(accent)s; margin-bottom:36px; }
  .title { font-weight:700; line-height:1.25; }
  .lines { margin-top:48px; display:flex; flex-direction:column; gap:28px; }
  .lines div { font-size:46px; font-weight:400; color:#C9D4E3; padding-left:26px;
               border-left:8px solid %(accent)s; line-height:1.4; }
  .stat .title { font-size:230px; color:%(accent)s; letter-spacing:4px; }
  .stat .lines div { font-size:56px; color:#F5F7FA; border-left:none; padding-left:0; }
  .info .title { font-size:96px; }
  .section { align-items:center; text-align:center; }
  .section .title { font-size:120px; }
"""


def render_card_html(card: dict, size: tuple[int, int]) -> str:
    """计划条目 → 卡片 HTML(纯函数:同样的输入必得同样的字节)。"""
    w, h = size
    tpl = card.get("template") or "info"
    accent = card["accent"]
    title = html.escape(str(card.get("title", "")))
    kicker = html.escape(str(card.get("kicker", "")))
    lines = "".join(f"<div>{html.escape(str(x))}</div>" for x in (card.get("lines") or []))
    css = _CARD_CSS % {"w": w, "h": h, "accent": accent,
                       "pad_t": int(h * 0.13), "pad_b": int(h * 0.31), "pad_x": int(w * 0.09)}
    kick = f'<div class="kicker">{kicker}</div>' if kicker else ""
    line_html = f'<div class="lines">{lines}</div>' if lines else ""
    return ('<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">\n'
            f"<title>{title}</title><style>{css}</style></head>\n"
            f'<body><div class="card {html.escape(tpl)}">{kick}'
            f'<div class="title">{title}</div>{line_html}</div></body></html>\n')


def parse_card_plan(plan: object) -> tuple[list[dict], list[str]]:
    """卡片计划 JSON → (规范化条目, 警告)。白名单外字段硬报错(P8 教训:静默吞字段
    曾让幽灵键一路漏到 schema 校验才炸);排版纪律(行数/行长)只警告。"""
    if not isinstance(plan, list) or not plan:
        raise ValueError("卡片计划必须是数组且非空:[{id, title, …}]")
    out: list[dict] = []
    warns: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(plan):
        where = f"plan[{i}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where} 不是对象")
        unknown = sorted(set(raw) - PLAN_KNOWN_KEYS)
        if unknown:
            raise ValueError(f"{where} 有白名单外字段 {unknown}"
                             f"(合法:{sorted(PLAN_KNOWN_KEYS)};P8:不静默吞字段)")
        cid = str(raw.get("id", "")).strip()
        if not cid:
            raise ValueError(f"{where} 缺 id")
        if not ID_RE.match(cid):
            raise ValueError(f"{where}.id 非法(须 [A-Za-z0-9_-] 且不以 -_ 开头):{cid}")
        if cid in seen:
            raise ValueError(f"id 重复:{cid}")
        seen.add(cid)
        title = str(raw.get("title", "")).strip()
        if not title:
            raise ValueError(f"{where}({cid}) 缺 title")
        tpl = str(raw.get("template") or "info")
        if tpl not in PLAN_TEMPLATES:
            raise ValueError(f"{where}.template 非法 {tpl}(可选 {'/'.join(PLAN_TEMPLATES)})")
        accent = str(raw.get("accent") or "#4F8CFF")
        if not ACCENT_RE.match(accent):
            raise ValueError(f"{where}.accent 非法 {accent}(须 #RRGGBB)")
        lines = [str(x) for x in (raw.get("lines") or [])]
        if len(lines) > 3:
            warns.append(f"{cid}:lines {len(lines)} 组超过纪律上限 3 组(rules/artboard.md)")
        if any(len(x) > 20 for x in lines):
            warns.append(f"{cid}:存在超长行(单行 ≤14 字纪律,导出后请目测)")
        out.append({"id": cid, "template": tpl, "title": title,
                    "lines": lines, "accent": accent,
                    "kicker": str(raw.get("kicker") or "")})
    return out, warns


def gen_cards(entries: list[dict], artboard_root: Path, *, ratio: str = "9x16",
              force: bool = False) -> tuple[list[dict], list[dict]]:
    """批量生成卡片工程(src/index.html),返回 (新建, 原样保留)。

    幂等:同一份计划重复跑,已存在且内容一致的卡不重写;内容不一致视为
    「手改过的卡片」拒绝覆盖(显式 --force 才放行)——绝不静默冲掉手改。
    """
    size = SIZE_BY_RATIO[ratio]
    written, kept = [], []
    for card in entries:
        src_dir = artboard_root / card["id"] / "src"
        target = src_dir / "index.html"
        content = render_card_html(card, size)
        if target.is_file():
            if target.read_text(encoding="utf-8") == content:
                kept.append(card)
                continue
            if not force:
                raise FileExistsError(
                    f"{card['id']}:卡片已存在且与计划不一致(疑似手改);"
                    "确认以计划覆盖请加 --force")
        write_text_atomic(target, content)
        written.append(card)
    return written, kept


def _item_prefix(artboard_root: Path, root: Path) -> str:
    """清单字段(project/output)的路径基准:P6 教训 —— artboard 目录在工程根下时写
    **工程根相对**,`--export/--apply` 用默认 `--root`(cwd=工程根)即可命中,
    不再踩「静默 0 匹配」;artboard 目录不在工程根下(独立素材仓)才写目录相对。"""
    try:
        return artboard_root.relative_to(root).as_posix()
    except ValueError:
        return ""


def upsert_manifest(doc: dict, entries: list[dict], *, ratio: str = "9x16",
                    root: Path, artboard_root: Path) -> list[str]:
    """把生成的卡片登记进 manifest(保留既有 usedIn/durationMs;按 id 幂等合并)。"""
    size = list(SIZE_BY_RATIO[ratio])
    prefix = _item_prefix(artboard_root, root)
    by_id = {it.get("id"): it for it in doc.get("items", [])}
    for card in entries:
        cid = card["id"]
        old = by_id.get(cid, {})
        pre = f"{prefix}/" if prefix else ""
        item = {"id": cid, "project": f"{pre}{cid}/src",
                "sourceHash": hash_source(artboard_root / cid / "src"),
                "output": f"{pre}{cid}/export/{cid}.png", "kind": "png", "size": size,
                "durationMs": old.get("durationMs"),
                "usedIn": old.get("usedIn", [])}
        by_id[cid] = item
    doc["version"] = 1
    doc.setdefault("root", ".")
    doc["items"] = [by_id[k] for k in sorted(by_id)]
    return [it["id"] for it in doc["items"]]


def _cmd_gen_cards(a: argparse.Namespace, root: Path, artboard_dir: Path) -> int:
    """gen-cards 子命令:卡片计划 → 卡片工程 + manifest 登记(原子写,失败非零)。"""
    if not a.from_plan:
        return emit(False, "NO_PLAN", "gen-cards 需要 --from <cards.json>(卡片计划数组)", exit_code=2)
    plan_path = Path(a.from_plan)
    if not plan_path.is_file():
        return emit(False, "NO_PLAN", f"卡片计划不存在:{plan_path}", exit_code=2)
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        entries, warns = parse_card_plan(plan)
    except (json.JSONDecodeError, ValueError) as exc:
        return emit(False, "BAD_PLAN", f"卡片计划不合法:{exc}", exit_code=2)
    out = Path(a.out) if a.out else root / "03_assets" / "artboard" / REGISTRY
    artboard_root = out.parent
    try:
        written, kept = gen_cards(entries, artboard_root, ratio=a.ratio, force=a.force)
    except FileExistsError as exc:
        return emit(False, "CARD_EXISTS", str(exc), {"hint": "加 --force 以计划覆盖手改"}, exit_code=2)
    doc: dict = {}
    if out.is_file():
        try:
            doc = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            doc = {}
    ids = upsert_manifest(doc, entries, ratio=a.ratio, root=root, artboard_root=artboard_root)
    save_manifest(doc, out)
    msg = (f"生成 {len(written)} 张卡片(复用 {len(kept)} 张)→ {out};"
           f"清单共 {len(ids)} 卡;导出请跑 --export / export-fallback")
    return emit(True, "CARDS_GENERATED", msg,
                {"written": [c["id"] for c in written], "kept": [c["id"] for c in kept],
                 "manifest": str(out), "warnings": warns})


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
    ap.add_argument("head", nargs="?",
                    help="子命令:gen-cards(从卡片计划批量生成卡片)/ "
                         "export-fallback(浏览器截图兜底导出);其余情况此位置是 manifest.json 路径")
    ap.add_argument("manifest", nargs="?", help="artboard 清单 manifest.json"
                    "(export-fallback 时跟在子命令后面)")
    ap.add_argument("--from", dest="from_plan", default="",
                    help="gen-cards:卡片计划 JSON([{id,title,lines,…}];时间窗字段供 rs_ir add-overlay 共用)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--scan")
    ap.add_argument("--out", default="")
    ap.add_argument("--ratio", default="9x16", choices=list(SIZE_BY_RATIO),
                    help="gen-cards:卡片画幅(默认 9x16)")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--apply", dest="apply_ir", default="")
    ap.add_argument("--only", default="", help="只处理指定卡片 id(逗号分隔)")
    ap.add_argument("--strict", action="store_true",
                    help="apply 时未被 IR 引用的卡片按硬失败处理(默认跳过+告警)")
    ap.add_argument("--force", action="store_true",
                    help="忽略 source hash 全部重导 / gen-cards 以计划覆盖手改卡片")
    ap.add_argument("--scale", type=int, default=1, choices=[1, 2, 4],
                    help="export-fallback:截图倍率(默认 1)")
    ap.add_argument("--transparent", action="store_true",
                    help="export-fallback:透明底")
    ap.add_argument("--probe", action="store_true",
                    help="自检:artboard 桥配置/导出脚本在位(rs_doctor 体检用,P21-1)")
    a = ap.parse_args()

    if a.probe:
        # P21-1:doctor 的「artboard 桥」检查不再只看目录存在 —— 真探:
        # config.artboard_dir 配置了?目录在?导出脚本找得到?三关全过才算通。
        raw = str(_load_artboard_config().get("artboard_dir") or "").strip()
        if not raw:
            return emit(False, "DEP_MISSING",
                        "artboard 桥自检失败:config.artboard_dir 未配置",
                        {"bridge": "artboard", "artboardDir": None}, exit_code=3)
        artboard_dir = Path(raw)
        detail = {"bridge": "artboard", "artboardDir": str(artboard_dir)}
        if not artboard_dir.is_dir():
            return emit(False, "DEP_MISSING",
                        f"artboard 桥自检失败:artboard_dir 不存在:{artboard_dir}", detail,
                        exit_code=3)
        if export_python(artboard_dir) is None:
            return emit(False, "DEP_MISSING",
                        f"artboard 桥自检失败:找不到导出脚本({artboard_dir}/scripts/export.py)",
                        detail, exit_code=3)
        return emit(True, "OK", f"artboard 桥自检通过({artboard_dir})", detail, exit_code=0)

    root = Path(a.root).resolve()
    cfg = _load_artboard_config()
    artboard_dir = Path(cfg.get("artboard_dir") or "")

    if a.head == "gen-cards":
        return _cmd_gen_cards(a, root, artboard_dir)

    if a.scan:
        out = Path(a.out or (Path(a.scan) / REGISTRY))
        doc = scan(Path(a.scan).resolve(), str(artboard_dir))
        out.parent.mkdir(parents=True, exist_ok=True)
        save_manifest(doc, out)
        return emit(True, "SCAN_OK", f"扫描到 {len(doc['items'])} 个 artboard 卡片 → {out}",
                    {"path": str(out), "count": len(doc["items"])})

    # 位置参数归一:head 是子命令时 manifest 落第二位;否则 head 就是 manifest。
    sub = a.head if a.head in ("export-fallback",) else ""
    if sub:
        mpath_arg = a.manifest
    else:
        if a.head is not None and a.manifest is not None:
            return emit(False, "NO_MANIFEST",
                        f"一次只接受一个 manifest 位置参数(收到 {a.head} 与 {a.manifest})", exit_code=2)
        mpath_arg = a.manifest or a.head

    if not mpath_arg:
        return emit(False, "NO_MANIFEST", "需要 <manifest.json>,或用 --scan <artboard 目录> / "
                                         "gen-cards --from <cards.json> 生成", exit_code=2)
    mpath = Path(mpath_arg)
    if not mpath.is_file():
        return emit(False, "NO_MANIFEST", f"清单不存在:{mpath}(先跑 --scan 或 gen-cards)", exit_code=2)
    doc = load_manifest(mpath)
    only = {x.strip() for x in a.only.split(",") if x.strip()}

    if sub == "export-fallback":
        if not artboard_dir.is_dir():
            return emit(False, "NO_ARTBOARD",
                        f"artboard 技能目录不存在:{artboard_dir};请在 config.artboard_dir 配置", exit_code=3)
        # P6-2 同一口径:源码变了 **或产物不在盘** 都要导,存在且未变才 skip。
        todo = [it for it in doc["items"] if (not only or it["id"] in only)
                and (a.force or hash_source(_src_dir(root, it)) != it.get("sourceHash")
                     or not (root / it["output"]).is_file())]
        if not todo:
            return emit(True, "EXPORT_SKIP", "所有卡片源码未变且产物在盘,无需兜底导出",
                        {"exported": 0})
        okd, failed = [], []
        for it in todo:
            ok, info = export_item_fallback(it, root, artboard_dir,
                                            scale=a.scale, transparent=a.transparent)
            if ok:
                it["sourceHash"] = hash_source(_src_dir(root, it))
                okd.append(it["id"])
            else:
                failed.append({"id": it["id"], "error": info})
        save_manifest(doc, mpath)
        msg = f"兜底导出 {len(okd)}/{len(todo)} 个卡片(Playwright 截图)"
        if failed:
            msg += f";失败 {len(failed)}"
        return emit(not failed, "FALLBACK_OK" if not failed else "FALLBACK_PARTIAL", msg,
                    {"exported": okd, "failed": failed}, exit_code=0 if not failed else 4)

    if a.export:
        if not artboard_dir.is_dir():
            return emit(False, "NO_ARTBOARD",
                        f"artboard 技能目录不存在:{artboard_dir};请在 config.artboard_dir 配置", exit_code=3)
        # P6-2(上一版 P6 的真缺口):skip 判定必须是「源码未变 **且产物在盘**」——
        # 旧版只比 source hash,首次 scan 后(产物还没导过)会假 EXPORT_SKIP 漏导。
        todo = [it for it in doc["items"] if (not only or it["id"] in only)
                and (a.force or hash_source(_src_dir(root, it)) != it.get("sourceHash")
                     or not (root / it["output"]).is_file())]
        if not todo:
            return emit(True, "EXPORT_SKIP", "所有卡片源码未变且产物在盘,无需重导",
                        {"exported": 0})
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
        # P11-1:appliedAt 是 S4 的「真实产物标记」—— rs_run 据此判 S4 是否真的做过
        #(此前 apply 成功不留痕,S4 被迫借 S3 的产物自动判 done)。apply 成功即盖戳,
        # 哪怕 0 个卡片变更(回了填 IR、确认过尺寸)。
        doc["appliedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
        save_manifest(doc, mpath)
        st = stale_stages(changes)
        msg = f"回填 {len(changes)} 个卡片;下游需重跑:{','.join(st)}"
        if skipped:
            msg += f";跳过 {len(skipped)} 个未引用卡片:{','.join(s['id'] for s in skipped[:5])}"
        return emit(True, "APPLY_OK", msg,
                    {"changes": changes, "skipped": skipped, "staleStages": st,
                     "appliedAt": doc["appliedAt"], "ir": str(ir_path)})

    return emit(False, "NO_ACTION", "需要 --export 或 --apply <ir>", exit_code=2)


if __name__ == "__main__":
    sys.exit(main())
