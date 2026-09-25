"""artboard 闭环:改完图 → 一键导出 → 回填 IR → 出片(rules/artboard.md)。

用法:
  # ⓪ 从卡片计划 JSON 批量生成卡片工程并登记 manifest(T1-1 升格,原 _gen_cards.py)
  rs_artboard.py gen-cards --from 00_制作简报/cards.json [--out 03_创作素材/artboard/manifest.json] [--ratio 9x16]

  # ⓪' 六类场景卡(片头尾/标题卡,M7):五段式动画 + MP4 导出 + 安全区机检联动
  #    kind ∈ opener|outro(别名 endcard)|title|section|stat|compare;卡片条目带 kind 字段
  rs_artboard.py gen-frames <工程根> --kind opener,title,endcard [--pack <slug>] [--ratio 9x16]

  # ① 扫描 artboard 工程生成/更新清单
  rs_artboard.py --scan 03_创作素材/artboard --out 03_创作素材/artboard/manifest.json

  # ② 只重导出源码变了的卡片(内容寻址,秒级);P6-2:产物不在盘也重导(存在才 skip)
  rs_artboard.py 03_创作素材/artboard/manifest.json --export

  # ②' 主引擎不可用时的浏览器截图兜底导出(升格自 export_fallback.py)
  rs_artboard.py export-fallback 03_创作素材/artboard/manifest.json [--only id1,id2] [--scale 1] [--transparent]

  # ③ 把新产物回填 IR(尺寸/时长校验),并报告受影响的下游阶段
  rs_artboard.py 03_创作素材/artboard/manifest.json --apply 05_时间线工程/project.json

  # ④ 一条龙:②+③+从 S4 级联重跑 —— 由 03_创作素材/artboard/rebuild.py 调用

清单是唯一映射表:artboard 工程 ↔ 导出产物 ↔ IR 挂点(manifest.json)。
卡片计划 JSON 与 rs_ir.py add-overlay 共用(内容字段归 gen-cards,时间窗字段归 add-overlay)。
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import RATIOS, REPO_ROOT, emit, write_text_atomic  # noqa: E402
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量

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
    M7:环境变量 CUTFLOW_ARTBOARD_DIR 最高优先——子进程测试与临时目录换技能
    目录的唯一缝(不污染真实 config.json)。
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
    env_dir = os.environ.get("CUTFLOW_ARTBOARD_DIR", "").strip()
    if env_dir:
        merged["artboard_dir"] = env_dir
    return merged


# ---------------------------------------------------------------- 清单

def hash_source(project: Path) -> str:
    """artboard 工程源码 hash(排除 export/ 与 __pycache__)。

    M7:gen-frames 经 scaffold.py 建工程,src/fonts、src/vendor 是指向技能资产库的
    **目录联接**(约 500MB 字体)——联接内容不是本卡源码,必须排除,否则每次
    hash 都要把整个字体库读一遍(导出/回填每张卡都会调)。只排**读取**,枚举
    照走(联接目录条目少,枚举开销可忽略)。
    """
    h = hashlib.sha1()
    if not project.is_dir():
        return ""
    for f in sorted(project.rglob("*")):
        if not f.is_file():
            continue
        if "export" in f.parts or "__pycache__" in f.parts:
            continue
        if "fonts" in f.parts or "vendor" in f.parts:
            continue
        h.update(str(f.relative_to(project)).encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()


def scan(root: Path, artboard_dir: str = "") -> dict:
    """扫描 03_创作素材/artboard 下的卡片工程(scaffold 产出 src/ 的目录)。

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


def export_item(item: dict, root: Path, artboard_dir: Path, timeout: int = 900,
                max_wait: float | None = None) -> tuple[bool, str]:
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
        # M7 gen-frames:--max-wait = 五段总和 + 1.5s(artboard 语义里即动画总时长);
        # 不传时维持旧行为(export.py 自身缺省 15s),既有 PNG 卡不受影响。
        if max_wait:
            cmd += ["--max-wait", str(max_wait)]
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

# 计划字段白名单:内容字段归 gen-cards/gen-frames,时间窗字段归 rs_ir.py add-overlay
# (共用一份 cards.json)。P8 教训(手挂轨只写 schema 允许的字段)同源:白名单外字段
# 直接报错,不静默吞。M7:`kind`(六类场景卡)入内容白名单——gen-cards 忽略它,
# gen-frames 按 `--kind` 筛选;rs_ir 白名单从本文件导入,自动同步放行。
PLAN_CONTENT_KEYS = {"id", "template", "title", "lines", "accent", "kicker", "kind"}
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
         font-family:"MiSans","Source Han Sans SC","%(font)s",sans-serif; }
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
    css = _CARD_CSS % {"w": w, "h": h, "accent": accent, "font": _css_font(),
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
        # M7:kind = 六类场景卡(可选字段;缺省 None = 普通 gen-cards 卡)。
        kind_raw = str(raw.get("kind") or "").strip() or None
        kind = KIND_ALIASES.get(kind_raw, kind_raw) if kind_raw else None
        if kind is not None and kind not in FRAME_KINDS:
            raise ValueError(f"{where}.kind 非法 {kind_raw}"
                             f"(可选 {'/'.join(FRAME_KINDS)};endcard 是 outro 的别名)")
        # durationMs 原样透传(挂轨时间窗;gen-frames 按导出片长语义反推持住)
        dur_raw = raw.get("durationMs")
        try:
            duration_ms = int(dur_raw) if dur_raw not in (None, "") else None
        except (TypeError, ValueError):
            raise ValueError(f"{where}.durationMs 不是整数毫秒:{dur_raw!r}") from None
        out.append({"id": cid, "template": tpl, "title": title,
                    "lines": lines, "accent": accent,
                    "kicker": str(raw.get("kicker") or ""), "kind": kind,
                    "durationMs": duration_ms})
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
    out = Path(a.out) if a.out else rs_paths.resolve(root, "assets") / "artboard" / REGISTRY
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


def _cmd_gen_frames(a: argparse.Namespace, root: Path, artboard_dir: Path) -> int:
    """gen-frames:六类场景卡一条龙(scaffold → 五段式 HTML → 安全区机检 →
    manifest → MP4 导出 → 按探测时长回填 IR)。

    硬语义(继承既有闭环):机检 ok:false 整条命令失败;尺寸不符报错不拉伸;
    时长变化会传播(apply_to_ir 平移后续 clip 并给出需重跑的下游阶段)。
    """
    if not artboard_dir.is_dir():
        return emit(False, "NO_ARTBOARD",
                    f"artboard 技能目录不存在:{artboard_dir};请在 config.artboard_dir 配置",
                    exit_code=3)

    # ---- 1) 卡片计划(与 gen-cards 同一份 cards.json)----
    plan_path = Path(a.from_plan) if a.from_plan else rs_paths.resolve(root, "brief") / "cards.json"
    if not plan_path.is_file():
        return emit(False, "NO_PLAN",
                    f"卡片计划不存在:{plan_path}(gen-frames 读 00_制作简报/cards.json,"
                    "或用 --from 指定)", exit_code=2)
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        entries, plan_warns = parse_card_plan(plan)
    except (json.JSONDecodeError, ValueError) as exc:
        return emit(False, "BAD_PLAN", f"卡片计划不合法:{exc}", exit_code=2)

    # ---- 2) --kind 归一与筛选(条目必须带 kind;普通 gen-cards 卡跳过并告警)----
    want: list[str] = []
    for raw in (a.kind or "").split(","):
        k = raw.strip()
        if not k:
            continue
        k = KIND_ALIASES.get(k, k)
        if k not in FRAME_KINDS:
            return emit(False, "BAD_KIND", f"--kind 非法 {raw.strip()}"
                        f"(可选 {'/'.join(FRAME_KINDS)};endcard 是 outro 的别名)", exit_code=2)
        if k not in want:
            want.append(k)
    want = want or list(FRAME_KINDS)
    selected = [e for e in entries if e.get("kind") in want]
    skipped = [{"id": e["id"], "kind": e.get("kind") or None,
                "reason": "kind 不在 --kind 筛选" if e.get("kind") else
                          "条目未写 kind(六类场景卡必须在计划里标 kind)"}
               for e in entries if e.get("kind") not in want]
    if not selected:
        return emit(False, "NO_FRAMES",
                    f"计划里没有带 kind ∈ {{{','.join(want)}}} 的卡片"
                    f"(共 {len(entries)} 条);六类场景卡须在 cards.json 标 kind 字段",
                    {"skipped": skipped}, exit_code=2)
    for e in selected:
        if e["kind"] == "compare" and len(e.get("lines") or []) < 2:
            return emit(False, "BAD_PLAN",
                        f"{e['id']}:compare 对比卡需要 ≥2 条 lines(态A/态B 文本)", exit_code=2)

    # ---- 3) 风格包(M6 前缺省回退 + WARN)----
    pack_css, pack_warns = load_style_pack(getattr(a, "pack", "") or "")
    ratio = a.ratio
    size = SIZE_BY_RATIO[ratio]
    artboard_root = rs_paths.resolve(root, "assets") / "artboard"

    # ---- 4) 逐卡:五段式 HTML(幂等,手改保护同 gen-cards;内容变才 scaffold+重写)----
    # 顺序要点:先比对再 scaffold —— scaffold --force 会落模板 index.html,若先跑
    # scaffold 再比对,自己的模板就会顶掉上一轮产物、误触「手改保护」。
    warns = list(plan_warns) + list(pack_warns)
    frames: list[dict] = []          # (card, seg, src_dir, mode)
    written, kept = [], []
    try:
        for card in selected:
            seg, seg_warns = five_segment(card["kind"], card.get("durationMs"))
            warns.extend(seg_warns)
            warns.extend(dwell_warnings(card, seg))
            html_text = render_frame_html(card, size, seg, ratio, pack_css)
            target = artboard_root / card["id"] / "src" / "index.html"
            existing = target.read_text(encoding="utf-8") if target.is_file() else None
            if existing is not None and existing != html_text and not a.force:
                raise FileExistsError(f"{card['id']}:index.html 已存在且与计划不一致(疑似手改);"
                                      "确认以计划覆盖请加 --force")
            if existing == html_text:
                kept.append(card["id"])
                mode = "kept"
            else:
                mode, why = scaffold_project(card["id"], artboard_root, ratio, artboard_dir)
                write_card_html(card["id"], html_text, artboard_root, force=True)
                written.append(card["id"])
                if mode == "direct":
                    warns.append(f"{card['id']}:scaffold 不可用,已直写工程({why})")
            frames.append({"card": card, "seg": seg, "mode": mode,
                           "src": artboard_root / card["id"] / "src"})
    except FileExistsError as exc:
        return emit(False, "CARD_EXISTS", str(exc),
                    {"hint": "加 --force 以计划覆盖手改"}, exit_code=2)

    # ---- 5) 安全区机检(硬门禁:ok:false 即整条命令失败)----
    checks, check_fails = [], []
    for fr in frames:
        ok, summary, report = run_safe_check(fr["src"], ratio, artboard_dir)
        checks.append({"id": fr["card"]["id"], "ok": ok, "summary": summary})
        if not ok:
            check_fails.append({"id": fr["card"]["id"], "summary": summary,
                                "issues": report.get("issues") or []})
    if check_fails:
        return emit(False, "SAFE_CHECK_FAILED",
                    f"安全区机检未过 {len(check_fails)}/{len(frames)}"
                    "(ok:false 即失败;修内容或降档后重跑)", {"failed": check_fails}, exit_code=4)

    # ---- 6) manifest 登记(kind=mp4;保留既有 usedIn/durationMs)----
    out = artboard_root / REGISTRY
    doc: dict = {}
    if out.is_file():
        try:
            doc = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            doc = {}
    by_id = {it.get("id"): it for it in doc.get("items", [])}
    try:
        prefix = artboard_root.relative_to(root).as_posix()
    except ValueError:
        prefix = ""
    pre = f"{prefix}/" if prefix else ""
    items = []
    for fr in frames:
        card, seg = fr["card"], fr["seg"]
        cid = card["id"]
        old = by_id.get(cid, {})
        item = {"id": cid, "project": f"{pre}{cid}/src",
                "sourceHash": old.get("sourceHash", ""),
                "output": f"{pre}{cid}/export/{cid}.mp4", "kind": "mp4", "fps": 25,
                "maxWait": seg["maxWait"], "frameKind": card["kind"],
                "fiveSeg": {k: seg[k] for k in ("t0", "in", "hold", "out", "p1", "total")},
                "size": list(size), "durationMs": old.get("durationMs"),
                "usedIn": old.get("usedIn", [])}
        by_id[cid] = item
        items.append(item)
    doc["version"] = 1
    doc.setdefault("root", ".")
    doc["items"] = [by_id[k] for k in sorted(by_id)]
    save_manifest(doc, out)

    # ---- 7) 导出 MP4(--format MP4 --fps 25 --max-wait 五段总和+1.5s)----
    # P6-2 同口径:源码未变 **且** 产物在盘才跳过;否则(重)导,成功后回写 sourceHash。
    okd, uptodate, failed = [], [], []
    for it in items:
        cur_hash = hash_source(root / it["project"])
        if it["sourceHash"] == cur_hash and (root / it["output"]).is_file():
            uptodate.append(it["id"])
            continue
        ok, info = export_item(it, root, artboard_dir, max_wait=it["maxWait"])
        if ok:
            it["sourceHash"] = cur_hash
            okd.append(it["id"])
        else:
            failed.append({"id": it["id"], "error": info})
    save_manifest(doc, out)
    if failed:
        hint = ffmpeg_hint()
        if hint:
            warns.append(hint)
        return emit(False, "EXPORT_FAILED",
                    f"导出 {len(okd)}/{len(items)};失败 {len(failed)}(HTML 与机检结果已留盘,"
                    "修引擎后重跑或用 --export 补导)",
                    {"exported": okd, "upToDate": uptodate, "failed": failed,
                     "warnings": warns}, exit_code=4)

    # ---- 8) 按探测时长回填 IR(复用 --apply 入轨逻辑;IR 存在才做)----
    ir_path = rs_paths.project_json(root)
    attach = {"done": False}
    if ir_path.is_file():
        ir = json.loads(ir_path.read_text(encoding="utf-8"))
        ir, issues, changes, ir_skipped = apply_to_ir(doc, ir, root, only={i["id"] for i in items})
        if issues:
            return emit(False, "APPLY_ISSUES",
                        f"{len(issues)} 个问题(尺寸不符报错不拉伸;不带着坏输入往下跑)",
                        {"issues": issues}, exit_code=4)
        ir_path.write_text(json.dumps(ir, ensure_ascii=False, indent=1), encoding="utf-8")
        doc["appliedAt"] = datetime.now().astimezone().isoformat(timespec="seconds")
        save_manifest(doc, out)
        attach = {"done": True, "changes": changes, "skipped": ir_skipped,
                  "staleStages": stale_stages(changes), "ir": str(ir_path)}
    else:
        attach = {"done": False, "note":
                  f"IR 不存在({ir_path}),未挂轨——gen-frames 不发明时间窗,"
                  "挂点用 rs_ir.py add-overlay / build --from-cards 排定后再跑本命令回填"}

    engine_kiln, engine_tried = find_kiln_exe()
    return emit(True, "FRAMES_OK",
                f"生成 {len(written)} 张场景卡(复用 {len(kept)})→ 机检全过 → "
                f"导出 {len(okd) + len(uptodate)}/{len(items)} MP4"
                f"(新导 {len(okd)},在盘未变 {len(uptodate)})"
                f" → {ir_path.name if attach['done'] else '未挂轨(IR 缺)'}",
                {"written": written, "kept": kept, "skipped": skipped,
                 "manifest": str(out), "checks": checks, "exported": okd,
                 "upToDate": uptodate,
                 "samples": [str(root / it["output"]) for it in items],
                 "attach": attach, "warnings": warns,
                 "engine": {"kiln": engine_kiln or None, "kilnTried": engine_tried,
                            "fallback": "playwright+edge/chrome(export-fallback,仅 PNG)"},
                 "pack": a.pack or None})


# 六类模板(方案 §5.10):opener 片头 / outro 片尾 / title 标题卡 / section 章节卡 /
# stat 数据卡 / compare 对比卡。endcard 是方案示例里对片尾的别名写法。
FRAME_KINDS = ("opener", "outro", "title", "section", "stat", "compare")
KIND_ALIASES = {"endcard": "outro"}

# 五段式硬规范(artboard animation.md §八/§十二;ADR-0012):
#   前置静置 ≥2.0s(吸收录制起点偏移 1.7–1.9s)→ 入场 0.5–0.8s decelerate →
#   持住(与解说句对齐)→ 出场 0.4–0.6s accelerate → 收尾静置 ≥0.3s;全 finite 禁 infinite。
T0_S, IN_S, OUT_S, P1_S = 2.0, 0.6, 0.5, 0.3
MAX_WAIT_MARGIN_S = 1.5     # --max-wait = 五段总和 + 1.5s(§八 默认)
EXPORT_EATEN_S = 1.8        # 导出片长 ≈ 五段总和 − 1.8s(前置静置被录制起点吃掉)

# 各类持住档(秒):(缺省持住, 下限, 上限)。缺省值让「导出片长 ≈ 持住 + 1.6s」
# 落进方案 §5.10 各类规格窗;durationMs 给出时按导出片长语义反推持住并夹限。
# stat/compare 上限 6.4 = 导出 ≤8s(6.4+1.6);compare 下限 1.6 = A/B 态切换要留两拍。
FRAME_SPECS = {
    "opener":  (0.8, 0.2, 1.4),   # 导出 1.5–3.0s
    "outro":   (1.4, 0.4, 2.4),   # 导出 2.0–4.0s
    "title":   (2.6, 1.0, 8.0),   # 与解说句对齐,不定窗
    "section": (0.6, 0.2, 0.9),   # 导出 1.5–2.5s
    "stat":    (3.4, 0.6, 6.4),   # 导出 ≤8s;dwell ≥1.5s/13 字符
    "compare": (4.4, 1.6, 6.4),   # 导出 ≤8s
}

# 安全区标准档 px(video-safe-area.md §2.1):9:16 左右 184 / 顶 230 / 底 576;
# 16:9 与 3:4 用各画幅数值。gen-frames 写进 .safe 层,机检(--safe-area)同口径。
SAFE_PX = {
    "9x16": (230, 576, 184),   # (top, bottom, side) @1080×1920
    "16x9": (108, 173, 154),   # @1920×1080
    "3x4":  (144, 259, 76),    # @1080×1440
}
# scaffold.py 的 --size 预设 ↔ CutFlow 画幅
SCAFFOLD_SIZE = {"9x16": "vertical", "3x4": "xhs", "16x9": "kv"}


def five_segment(kind: str, duration_ms: int | None = None) -> tuple[dict, list[str]]:
    """kind + 可选 durationMs → (五段时间轴, 警告)。纯函数:同输入必得同输出。

    durationMs 语义 = 挂轨时间窗 = 导出片长 ≈ 持住 + (五段固定和 − 1.8s) = 持住 + 1.6s,
    故反推 持住 = durationMs/1000 − 1.6,并按各类上下限夹限(夹限必 WARN)。
    """
    d_hold, h_min, h_max = FRAME_SPECS[kind]
    hold, warns = float(d_hold), []
    if duration_ms:
        hold = duration_ms / 1000.0 - (T0_S + IN_S + OUT_S + P1_S - EXPORT_EATEN_S)
    clamped = min(max(hold, h_min), h_max)
    if abs(clamped - hold) > 1e-9:
        warns.append(f"{kind}:持住 {hold:.2f}s 超出档位 [{h_min}, {h_max}]s,"
                     f"已夹限为 {clamped:.2f}s(导出片长随实测为准)")
        hold = clamped
    total = T0_S + IN_S + hold + OUT_S + P1_S
    seg = {"t0": T0_S, "in": IN_S, "hold": round(hold, 3), "out": OUT_S, "p1": P1_S,
           "total": round(total, 3), "maxWait": round(total + MAX_WAIT_MARGIN_S, 3)}
    return seg, warns


_FRAME_COLORS = {"bg": "#0E1116", "ink": "#F5F7FA", "muted": "#C9D4E3"}

_FRAME_BASE_CSS = """
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body { margin:0; background:%(bg)s; }
  body { width:%(w)dpx; height:%(h)dpx; overflow:hidden;
         font-family:"MiSans","Source Han Sans SC","%(font)s",sans-serif; }
  :root {
    /* 五段式(animation.md §八):前置静置→入场→持住→出场→收尾静置;全部有限时长,无无限循环 */
    --t0: %(t0)gs; --in: %(in)gs; --hold: %(hold)gs; --out: %(out)gs; --p1: %(p1)gs;
    --ease-in:  cubic-bezier(0, 0, 0, 1);      /* M3 decelerate:元素入画 */
    --ease-out: cubic-bezier(.3, 0, .8, .15);  /* M3 emphasized-accelerate:元素离画 */
    --c-bg: %(bg)s; --c-ink: %(ink)s; --c-muted: %(muted)s; --c-accent: %(accent)s;
  }
  .poster { position:relative; width:%(w)dpx; height:%(h)dpx; overflow:hidden;
            background:var(--c-bg); }
  .bg { position:absolute; right:-12%%; top:-8%%; width:70%%; height:36%%; border-radius:50%%;
        background:radial-gradient(closest-side, %(accent)s44, transparent 70%%); opacity:.35; }
  /* 【硬】安全区包裹层(video-safe-area.md §2.1 标准档):一切内容都在这层里 */
  .safe { position:absolute; left:%(side)dpx; right:%(side)dpx;
          top:%(top)dpx; bottom:%(bottom)dpx;
          display:flex; flex-direction:column; justify-content:center; }
  /* 【硬】入场与出场分属两层嵌套元素(§十二):外层 .si 只挂入场(--i 排观看看顺序),
     内层 .so 只挂出场(--j 排反序,后进先出)。同一元素挂两条动画会让出场的
     backwards fill 压住入场 from → 元素从 0s 起常驻 = 没有入场。 */
  .si { display:block; animation:rise-in  var(--in)  var(--ease-in)
        calc(var(--t0) + var(--i, 0) * 120ms) both; }
  .so { display:block; animation:rise-out var(--out) var(--ease-out)
        calc(var(--t0) + var(--in) + var(--hold) + var(--j, 0) * 80ms) both; }
  @keyframes rise-in  { from { transform:translateY(28px); opacity:0; }
                        to   { transform:none;             opacity:1; } }
  @keyframes rise-out { from { transform:none;                  opacity:1; }
                        to   { transform:translateY(-20px) scale(.97); opacity:0; } }
"""


def _esc(t: object) -> str:
    return html.escape(str(t))


def _io(i: int, j: int, inner: str, cls: str = "") -> str:
    """两层嵌套元素:外层 .si 入(--i 顺序)、内层 .so 出(--j 反序)。"""
    c = f" {cls}" if cls else ""
    return (f'<div class="si{c}" style="--i:{i}">'
            f'<div class="so" style="--j:{j}">{inner}</div></div>')


def _frame_content_opener(card: dict) -> tuple[str, str]:
    """片头:骨架(色条)先行 → kicker → 主标(§九 骨架先行)。"""
    css = (".kicker { font-size:34px; font-weight:500; letter-spacing:10px;"
           " color:var(--c-accent); margin-bottom:40px; }\n"
           ".title { font-size:108px; font-weight:700; line-height:1.22; color:var(--c-ink); }\n"
           ".rule { width:96px; height:10px; background:var(--c-accent);"
           " border-radius:5px; margin-bottom:44px; }\n")
    parts = [_io(0, 2, '<div class="rule"></div>')]           # 骨架 0ms 先落
    n = 1
    if card.get("kicker"):
        parts.append(_io(n, 1, f'<div class="kicker">{_esc(card["kicker"])}</div>'))
        n += 1
    parts.append(_io(n, 0, f'<h1 class="title">{_esc(card.get("title", ""))}</h1>'))
    return css, "".join(parts)


def _frame_content_outro(card: dict) -> tuple[str, str]:
    """片尾:CTA 行先入先退,主标(感谢/预告)压轴退场(§九 stagger 反序)。"""
    css = (".title { font-size:96px; font-weight:700; line-height:1.25; color:var(--c-ink); }\n"
           ".ctas { margin-top:52px; display:flex; flex-direction:column; gap:26px; }\n"
           ".ctas div { font-size:44px; color:var(--c-muted); line-height:1.4; }\n")
    rows = [str(x) for x in (card.get("lines") or [])][:3]
    title = _io(1, 0, f'<h1 class="title">{_esc(card.get("title", ""))}</h1>')
    if not rows:
        return css, title
    cta = "".join(f"<div>{_esc(x)}</div>" for x in rows)
    return css, _io(0, 1, f'<div class="ctas">{cta}</div>') + title


def _frame_content_title(card: dict) -> tuple[str, str]:
    """标题卡:标题逐行(§九 断行=换色=stagger 三合一,+80ms/行;奇数行强调色)。"""
    css = ("h1 { margin:0; }\n"
           ".tl { display:block; font-size:88px; font-weight:700; line-height:1.3; }\n")
    rows = [str(x) for x in (card.get("lines") or [])] or [str(card.get("title", ""))]
    rows = rows[:3]
    n = len(rows)
    parts = []
    for k, row in enumerate(rows):
        color = "var(--c-accent)" if k % 2 == 1 else "var(--c-ink)"
        parts.append(f'<span class="tl si" style="--i:{k}">'
                     f'<span class="tl so" style="--j:{n - 1 - k}; color:{color}">'
                     f'{_esc(row)}</span></span>')
    return css, "".join(parts)


def _frame_content_section(card: dict) -> tuple[str, str]:
    """章节卡:居中;色条骨架先行,kicker(章节号)+ 大标。"""
    css = (".section { align-items:center; text-align:center; }\n"
           ".rule { width:88px; height:10px; background:var(--c-accent);"
           " border-radius:5px; margin:0 auto 40px; }\n"
           ".kicker { font-size:32px; font-weight:500; letter-spacing:12px;"
           " color:var(--c-accent); margin-bottom:32px; }\n"
           ".title { font-size:104px; font-weight:700; line-height:1.24; color:var(--c-ink); }\n")
    parts = [_io(0, 2, '<div class="rule"></div>')]
    n = 1
    if card.get("kicker"):
        parts.append(_io(n, 1, f'<div class="kicker">{_esc(card["kicker"])}</div>'))
        n += 1
    parts.append(_io(n, 0, f'<h1 class="title">{_esc(card.get("title", ""))}</h1>'))
    return css, f'<div class="section">{"".join(parts)}</div>'


def _frame_content_stat(card: dict) -> tuple[str, str]:
    """数据卡:大数字(强调色/tabular-nums 防抖)+ 要点行;数字压轴退场。"""
    css = (".stat-num { font-size:230px; font-weight:700; color:var(--c-accent);"
           " letter-spacing:4px; font-variant-numeric:tabular-nums; line-height:1.1; }\n"
           ".stat-caps { margin-top:44px; display:flex; flex-direction:column; gap:26px; }\n"
           ".stat-caps div { font-size:52px; color:var(--c-ink); line-height:1.4; }\n")
    rows = [str(x) for x in (card.get("lines") or [])][:3]
    caps = "".join(f"<div>{_esc(x)}</div>" for x in rows)
    parts = [_io(0, 1, f'<div class="stat-num">{_esc(card.get("title", ""))}</div>')]
    if caps:
        parts.append(_io(1, 0, f'<div class="stat-caps">{caps}</div>'))
    return css, "".join(parts)


def _frame_content_compare(card: dict) -> tuple[str, str]:
    """对比卡(§十 前后对比切换):态 A 随卡片入场 → 持住中点 0.3s 快速退场 →
    态 B 入场 → 随卡片统一出场。全部有限次数,时刻由五段变量算出。
    标签取 kicker 的「A/B」写法(如 错/对、旧/新),缺省 前/后。"""
    css = (".cmp { display:grid; }\n"
           ".cmp-cell { grid-area:1/1; display:flex; flex-direction:column;"
           " align-items:center; gap:34px; }\n"
           "/* 态 A:持住 45% 处 0.3s accelerate 快速退场 */\n"
           ".so-a { animation:rise-out .3s var(--ease-out)"
           " calc(var(--t0) + var(--in) + var(--hold) * 0.45) both; }\n"
           "/* 态 B:态 A 退场完成后入场,随卡片统一出场(--j 缺省 0) */\n"
           ".si-b { animation:rise-in .3s var(--ease-in)"
           " calc(var(--t0) + var(--in) + var(--hold) * 0.45 + .3s) both; }\n"
           ".badge { font-size:30px; font-weight:600; letter-spacing:8px; padding:10px 36px;"
           " border-radius:999px; border:3px solid var(--c-muted); color:var(--c-muted); }\n"
           ".badge-b { border-color:var(--c-accent); color:var(--c-accent); }\n"
           ".cmp-text { font-size:60px; font-weight:700; line-height:1.35;"
           " color:var(--c-ink); text-align:center; }\n")
    labels = str(card.get("kicker") or "前/后").split("/")
    la = labels[0].strip() or "前"
    lb = labels[1].strip() if len(labels) > 1 and labels[1].strip() else "后"
    rows = [str(x) for x in (card.get("lines") or [])]
    a = (f'<div class="cmp-cell si" style="--i:0"><div class="cmp-cell so-a">'
         f'<div class="badge">{_esc(la)}</div>'
         f'<div class="cmp-text">{_esc(rows[0])}</div></div></div>')
    b = (f'<div class="cmp-cell si-b"><div class="cmp-cell so">'
         f'<div class="badge badge-b">{_esc(lb)}</div>'
         f'<div class="cmp-text">{_esc(rows[1])}</div></div></div>')
    return css, f'<div class="cmp">{a}{b}</div>'


_FRAME_BUILDERS = {
    "opener": _frame_content_opener, "outro": _frame_content_outro,
    "title": _frame_content_title, "section": _frame_content_section,
    "stat": _frame_content_stat, "compare": _frame_content_compare,
}


def render_frame_html(card: dict, size: tuple[int, int], seg: dict,
                      ratio: str = "9x16", pack_css: str = "") -> str:
    """场景卡条目 → 五段式 HTML(纯函数:同一份输入必得同一字节;无时间戳无随机)。

    硬约束落进产物本身:五段变量、全 finite(模板里没有任何 infinite)、
    入场/出场两层嵌套(.si 外/.so 内)、内容全在 .safe 层、data-* 供机检与测试断言。
    """
    w, h = size
    top, bottom, side = SAFE_PX[ratio]
    kind = card.get("kind") or "title"
    kind_css, content = _FRAME_BUILDERS[kind](card)
    colors = dict(_FRAME_COLORS, accent=card["accent"], w=w, h=h, font=_css_font(),
                  top=top, bottom=bottom, side=side, **{k: seg[k] for k in ("t0", "in", "hold", "out", "p1")})
    css = _FRAME_BASE_CSS % colors + "\n" + kind_css + (pack_css or "")
    five = "/".join(str(seg[k]) for k in ("t0", "in", "hold", "out", "p1"))
    return ('<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">\n'
            f"<title>{_esc(card.get('id', ''))}</title><style>{css}</style></head>\n"
            f'<body><div class="poster" data-kind="{_esc(kind)}" data-five-seg="{five}">'
            f'<div class="bg" data-allow-overflow></div>'
            f'<div class="safe">{content}</div></div></body></html>\n')


def load_style_pack(slug: str) -> tuple[str, list[str]]:
    """风格包 → (附加 CSS, 警告)。包真身在 skills/cutflow/templates/styles/packs/<slug>/
    (ADR-0051/方案 §5.8,与 rs_stylepack/rs_intent 同一真相源;M8 修正:M7 曾探测
    <repo>/templates/styles/packs —— 仓库根并无此目录,真包永远 stylePackMissing)。
    包不存在时缺省回退:内置默认卡片规格 + WARN stylePackMissing,行为可复现。
    包命中且含 frames.css 时注入为附加样式。"""
    if not slug:
        return "", ["stylePackMissing:未指定 --pack,使用内置默认卡片规格"
                    "(M6 风格包落地后片头尾与正片视觉才会对齐)"]
    pack_dir = (REPO_ROOT / "skills" / "cutflow" / "templates" / "styles"
                / "packs" / slug)
    if not pack_dir.is_dir():
        return "", [f"stylePackMissing:风格包 {slug} 不存在({pack_dir});"
                    "使用内置默认卡片规格,行为可复现"]
    css = pack_dir / "frames.css"
    if css.is_file():
        return css.read_text(encoding="utf-8"), []
    return "", []


def find_kiln_exe(repo_cfg: dict | None = None) -> tuple[str, list[str]]:
    """Kiln 引擎探测(与 artboard export.py find_kiln 同序,本侧只做存在性探测):
    config kiln_cli_exe → VellumBench/target/release/kiln-cli.exe → dist/Kiln-noGUI-CLI.exe。
    返回 (命中路径(空=不可用), 探测轨迹)。"""
    cfg = repo_cfg if repo_cfg is not None else _load_artboard_config()
    tried: list[str] = []
    cand_cfg = str(cfg.get("kiln_cli_exe") or "").strip()
    if cand_cfg:
        tried.append(f"config:{cand_cfg}")
        if Path(cand_cfg).is_file():
            return cand_cfg, tried
    vb = REPO_ROOT.parent / "VellumBench"
    for rel in ("target/release/kiln-cli.exe", "dist/Kiln-noGUI-CLI.exe"):
        p = vb / rel
        tried.append(str(p))
        if p.is_file():
            return str(p), tried
    return "", tried


def ffmpeg_hint() -> str:
    """MP4 导出的 ffmpeg 可用性提示(环境变量坑,安信德 #13):
    Kiln 车道要 ffmpeg 在 PATH;WPI/浏览器兜底车道认 WPI_FFMPEG;
    ARTBOARD_FFMPEG 对 MP4 导出无效。可用时返回空串。"""
    if shutil.which("ffmpeg") or os.environ.get("WPI_FFMPEG"):
        return ""
    return ("MP4 导出需要 ffmpeg:Kiln 车道要求 ffmpeg 在 PATH;"
            "WPI/浏览器兜底车道认 WPI_FFMPEG(ARTBOARD_FFMPEG 对 MP4 导出无效)")


def check_overflow_python(artboard_dir: Path) -> Path | None:
    for rel in ("scripts/check_overflow.py", "check_overflow.py"):
        p = artboard_dir / rel
        if p.is_file():
            return p
    return None


def run_safe_check(src_dir: Path, ratio: str, artboard_dir: Path,
                   timeout: int = 300) -> tuple[bool, str, dict]:
    """安全区机检:check_overflow.py <src> --safe-area <ratio>。
    返回 (通过, 摘要, 原始报告)。ok:false / 脚本缺失 / 输出不可解析都算不通过。"""
    script = check_overflow_python(artboard_dir)
    if script is None:
        return False, f"找不到机检脚本({artboard_dir}/scripts/check_overflow.py)", {}
    cmd = [sys.executable, str(script), str(src_dir), "--safe-area", ratio]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"安全区机检超时(>{timeout}s)", {}
    report: dict = {}
    for line in reversed((p.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    report = obj
                    break
            except ValueError:
                continue
    if not report:
        return False, (p.stderr or p.stdout or "机检无 JSON 输出")[-300:], {}
    if report.get("ok"):
        return True, f"安全区 {ratio} 机检通过", report
    issues = report.get("issues") or []
    brief = ";".join(f"[{i.get('type')}]{i.get('selector')}:{i.get('hint', '')[:60]}"
                     for i in issues[:5])
    return False, brief or "机检 ok:false", report


def _css_font() -> str:
    """卡片 CSS 字体栈第三顺位(分册01 §5):fonts.json 查表家族名,缺失回退
    思源黑体系;不再写死任何系统私有字体。"""
    fj = Path(__file__).resolve().parents[1] / "templates" / "fonts.json"
    try:
        doc = json.loads(fj.read_text(encoding="utf-8"))
        dkey = (doc.get("default") or {}).get("subtitle")
        for f in doc.get("fonts") or []:
            if f.get("dir") == dkey and f.get("family"):
                return str(f["family"])
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return "Source Han Sans SC"


def default_fonts() -> str:
    """默认字体目录名(分册01 §5):templates/fonts.json 的 default.subtitle。

    fonts.json 缺失 → artboard scaffold 自身默认(source-han-sans)兜底,不传空值
    ——空 --fonts 意味着「用系统字体」,跨机器必然不一致(缺陷 D 的根因)。
    """
    fj = Path(__file__).resolve().parents[1] / "templates" / "fonts.json"
    try:
        d = json.loads(fj.read_text(encoding="utf-8"))
        return str((d.get("default") or {}).get("subtitle") or "source-han-sans")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return "source-han-sans"


def scaffold_project(card_id: str, artboard_root: Path, ratio: str,
                     artboard_dir: Path, timeout: int = 120,
                     fonts: str | None = None) -> tuple[str, str]:
    """调 artboard scaffold.py 建单卡工程(ARTBOARD_STUDIO 指向工程内 artboard 目录)。
    返回 (模式, 说明):mode = scaffold | direct。scaffold 不可用/失败时回退
    gen-cards 同款直写(补一份最小 project.json 供机检读画布),行为仍可复现。"""
    script = artboard_dir / "scripts" / "scaffold.py"
    src_dir = artboard_root / card_id / "src"
    if script.is_file():
        env = os.environ.copy()
        env["ARTBOARD_STUDIO"] = str(artboard_root)
        cmd = [sys.executable, str(script), card_id, "--size", SCAFFOLD_SIZE[ratio],
               "--fonts", fonts if fonts else default_fonts(), "--force"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout, env=env)
        except (subprocess.TimeoutExpired, OSError) as exc:
            p = None
            why = str(exc)[:120]
        else:
            why = ((p.stderr or p.stdout or "").strip())[-160:]
        if p is not None and p.returncode == 0 and src_dir.is_dir():
            return "scaffold", str(script)
    # 回退:直写(gen-cards 同款组织),补最小 project.json(机检按它读画布)
    src_dir.mkdir(parents=True, exist_ok=True)
    (artboard_root / card_id / "project.json").parent.mkdir(parents=True, exist_ok=True)
    w, h = SIZE_BY_RATIO[ratio]
    write_text_atomic(artboard_root / card_id / "project.json",
                      json.dumps({"slug": card_id, "width": w, "height": h,
                                  "created_by": "cutflow.gen-frames"},
                                 ensure_ascii=False, indent=2))
    return "direct", why if script.is_file() else f"找不到 {script}"


def write_card_html(card_id: str, content: str, artboard_root: Path,
                    *, force: bool) -> tuple[str, str]:
    """幂等写单卡 index.html:内容一致不重写(kept);不一致视为手改,无 --force 拒绝
    (gen-cards 同纪律:绝不静默冲掉手改)。返回 (written|kept, 路径)。"""
    target = artboard_root / card_id / "src" / "index.html"
    if target.is_file() and target.read_text(encoding="utf-8") == content:
        return "kept", str(target)
    if target.is_file() and not force:
        raise FileExistsError(f"{card_id}:index.html 已存在且与计划不一致(疑似手改);"
                              "确认以计划覆盖请加 --force")
    write_text_atomic(target, content)
    return "written", str(target)


def dwell_warnings(card: dict, seg: dict) -> list[str]:
    """字数纪律(软告警):停留 ≥1.5s/13 字符(rules/artboard.md;MD3 legibility)。
    可见时长按 入场+持住 估;超纪律不拦导出,提醒拆卡或拉长持住。"""
    texts = [str(card.get("title", ""))] + [str(x) for x in (card.get("lines") or [])]
    longest = max((len(t) for t in texts if t), default=0)
    if not longest:
        return []
    need = 1.5 * math.ceil(longest / 13)
    visible = seg["in"] + seg["hold"]
    if visible < need:
        return [f"{card['id']}:最长文本 {longest} 字,按 1.5s/13 字符需停留 ≥{need:.1f}s,"
                f"当前入场+持住 {visible:.1f}s——请拆卡/精简文案或给该卡指定 durationMs"]
    return []




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
                    help="子命令:gen-cards(从卡片计划批量生成卡片)/ gen-frames(六类场景卡)/"
                         "export-fallback(浏览器截图兜底导出);其余情况此位置是 manifest.json 路径")
    ap.add_argument("manifest", nargs="?", help="artboard 清单 manifest.json"
                    "(export-fallback/gen-frames 时跟在子命令后面;"
                    "gen-frames 时此位置可写工程根,缺省用 --root)")
    ap.add_argument("--from", dest="from_plan", default="",
                    help="gen-cards/gen-frames:卡片计划 JSON([{id,title,lines,…}];"
                         "时间窗字段供 rs_ir add-overlay 共用;gen-frames 缺省读 00_制作简报/cards.json)")
    ap.add_argument("--kind", default="",
                    help="gen-frames:逗号分隔的场景卡类型筛选"
                         "(opener/outro(别名 endcard)/title/section/stat/compare;缺省全部)")
    ap.add_argument("--pack", default="",
                    help="gen-frames:风格包 slug(templates/styles/packs/<slug>/;"
                         "M6 落地前缺失即回退内置默认规格 + WARN)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--scan")
    ap.add_argument("--out", default="")
    ap.add_argument("--ratio", default="9x16", choices=list(SIZE_BY_RATIO),
                    help="gen-cards/gen-frames:卡片画幅(默认 9x16)")
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
        # M7:附带 gen-frames 引擎探测(Kiln / ffmpeg / 机检脚本),如实报告不判死。
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
        kiln, tried = find_kiln_exe()
        hint = ffmpeg_hint()
        detail["frames"] = {"kiln": kiln or None, "kilnTried": tried,
                            "ffmpegOnPath": bool(shutil.which("ffmpeg")),
                            "wpiFfmpeg": bool(os.environ.get("WPI_FFMPEG")),
                            "checkOverflow": bool(check_overflow_python(artboard_dir)),
                            "scaffold": bool((artboard_dir / "scripts" / "scaffold.py").is_file())}
        if hint:
            detail["frames"]["mp4Hint"] = hint
        return emit(True, "OK", f"artboard 桥自检通过({artboard_dir})", detail, exit_code=0)

    root = Path(a.root).resolve()
    cfg = _load_artboard_config()
    artboard_dir = Path(cfg.get("artboard_dir") or "")

    if a.head == "gen-cards":
        return _cmd_gen_cards(a, root, artboard_dir)

    if a.head == "gen-frames":
        # 方案 §5.10 用法 `gen-frames <工程>`:位置参数承接工程根(缺省 --root)
        if a.manifest:
            root = Path(a.manifest).resolve()
        return _cmd_gen_frames(a, root, artboard_dir)

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
