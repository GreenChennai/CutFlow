"""素材库统一入口(M12,ADR-0053 / 分册01 §6):索引 · 检索 · 许可门禁 · 归因。

用法:
  rs_asset.py list [--kind sfx,element,huazi,bgm] [--tag X] [--usage X] [--commercial-only] [--json]
  rs_asset.py search <关键词> [--kind …] [--limit 5] [--json]
  rs_asset.py get <id>                          # 落盘引用返回(absPath / repoRelPath)
  rs_asset.py check [--strict]                  # ★ 许可与完整性门禁入口
  rs_asset.py attribution --root <工程根> [--out 成品/说明书/素材归因.md]
  rs_asset.py add --kind sfx --file <路径> --source … --license … [--commercial true] --attribution …
  rs_asset.py scan <目录> --kind sfx --source … --license …   # 批量入库(新增/变更/移除三态)

设计纪律:
  · 输出统一 {"ok","code","message","data"}(rs_common.emit);
  · add / scan 写 manifest.json 一律 write_text_atomic(原子写);
  · list / search / check / attribution 纯函数式:同输入必得同输出
    (manifest 的 generatedAt 不进任何比较口径);
  · check 退出码:0 通过 / 2 输入错 / 4 校验失败(沿用 rs_common 约定);
  · 旧工程兜底:manifest 缺失时 sfx 走硬编码 7 名字表 + WARN(ADR-0053 四问)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import (EXIT_EXEC, EXIT_INPUT, EXIT_OK, REPO_ROOT, emit,  # noqa: E402
                       write_text_atomic)
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(工程侧落点禁止目录字面量)

# ---------------------------------------------------------------- 素材库定位

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
MANIFEST_PATH = ASSETS_DIR / "manifest.json"
FONTS_JSON = Path(__file__).resolve().parents[1] / "templates" / "fonts.json"

KINDS = ("sfx", "element", "huazi", "bgm")
USAGES = ("transition", "punchline", "enumeration", "ending", "chapter",
          "emotion", "ui", "decor", "data")
LICENSE_FIELDS = ("source", "license", "commercial", "attribution")
# 体积预算(R38):音效单条 ≤200KB / BGM 单条 ≤3MB / 元素单图 ≤300KB(花字模板是文本不设限)
SIZE_BUDGET = {"sfx": 200 * 1024, "bgm": 3 * 1024 * 1024,
               "element": 300 * 1024, "huazi": 1024 * 1024}
SUPPORTED_EXT = {".mp3", ".wav", ".flac", ".png", ".jpg", ".jpeg", ".webp", ".ass", ".html"}

# 旧硬编码兜底表(ADR-0053 回滚承诺:manifest 缺失时 7 名字仍可用,**不删**)
LEGACY_SFX = ("whoosh", "swipe", "pop", "click", "ding", "riser", "bell")
# 旧伪协议根(仓库 assets/sfx,历史工程直解析处;兜底第二跳)
LEGACY_SFX_DIR = REPO_ROOT / "assets" / "sfx"


def _warn(msg: str) -> None:
    print(f"[rs_asset] WARN {msg}", file=sys.stderr)


def load_manifest() -> dict | None:
    """读统一索引;缺失/损坏 → None(调用方兜底 + WARN,不臆测)。"""
    if not MANIFEST_PATH.is_file():
        return None
    try:
        doc = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("assets"), list) else None


def assets(kind: str | None = None) -> list[dict]:
    """索引条目(kind 过滤;manifest 缺失 → 空表,由调用方决定兜底)。"""
    doc = load_manifest()
    if doc is None:
        return []
    out = [a for a in doc["assets"] if not kind or a.get("kind") == kind]
    return sorted(out, key=lambda a: str(a.get("id", "")))


def file_of(asset: dict) -> Path:
    """条目 file 字段(相对 assets/)→ 绝对路径。"""
    return ASSETS_DIR / str(asset.get("file", ""))


def find(ref: str, kind: str | None = None) -> dict | None:
    """引用解析:精确 id → kind 内「组名」匹配(旧伪协议 assets_sfx:<组> 兼容)。

    组名匹配取排序最小的序号段(确定性);找不到返回 None。
    """
    pool = assets(kind)
    for a in pool:
        if a.get("id") == ref:
            return a
    # 组名匹配(旧伪协议 assets_sfx:<组> 与裸组名 get/search 同一口):取排序最小的
    # 序号段(确定性)。ref 形态:全 id / <kind>.<组> / 裸 <组>。
    parts = ref.split(".")
    group = parts[1] if len(parts) >= 2 and parts[0] in KINDS else         (parts[0] if len(parts) == 1 else None)
    if group:
        hits = [a for a in pool
                if len(str(a.get("id", "")).split(".")) > 1
                and str(a.get("id", "")).split(".")[1] == group]
        if hits:
            return sorted(hits, key=lambda a: str(a.get("id", "")))[0]
    return None


def resolve_sfx_ref(ref: str) -> Path | None:
    """`assets_sfx:<id|旧名>` → 盘面音效路径(渲染/剪映/校验共用单一解析口)。

    解析顺序:manifest(id/组名)→ 契约目录旧名(<名>_01.mp3)→ 旧仓库根
    assets/sfx/<名>.mp3(更老检出的工程兼容)。全部落空返回 None(调用方如实
    上报缺失,绝不臆造路径)。
    """
    hit = find(ref, "sfx")
    if hit is not None:
        p = file_of(hit)
        if p.is_file():
            return p
    if re.fullmatch(r"[a-z_]+", ref):
        moved = ASSETS_DIR / "sfx" / f"{ref}_01.mp3"      # M12 迁移后的契约目录
        if moved.is_file():
            return moved
        if (LEGACY_SFX_DIR / f"{ref}.mp3").is_file():    # 迁移前的旧位置兜底
            return LEGACY_SFX_DIR / f"{ref}.mp3"
    return None


def pick_sfx(usage: str, *, avoid: set[str] | None = None,
             prefer: str | None = None) -> dict | None:
    """按语义用途选音效(分册01 §4.1:不再硬编码 7 名字,不再奇偶交替)。

    确定性:候选按 id 排序,优先 prefer 组,其次未进 avoid 表的首条;全 avoid → None。
    """
    pool = [a for a in assets("sfx")
            if usage in (a.get("usage") or []) and a.get("commercial", True)]
    if not pool:
        return None
    pool.sort(key=lambda a: str(a["id"]))
    if prefer:
        hit = next((a for a in pool if str(a["id"]).split(".")[1] == prefer
                    and str(a["id"]) not in (avoid or set())), None)
        if hit:
            return hit
    if avoid:
        pool = [a for a in pool if str(a["id"]) not in avoid] or pool
    return pool[0]


# ---------------------------------------------------------------- check(门禁)

def _scan_stylepack_references() -> list[tuple[str, str]]:
    """风格包/提示词模板对素材 id 的全部字面引用(R42:引用必须存在且可商用)。"""
    refs: list[tuple[str, str]] = []
    base = Path(__file__).resolve().parents[1] / "templates"
    id_re = re.compile(r"\b(?:sfx|element|huazi|bgm)\.[a-z0-9_]+(?:\.[a-z0-9_]+)+")
    for p in sorted(base.rglob("*")):
        if p.suffix not in (".yaml", ".yml", ".json", ".md") or not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in id_re.finditer(text):
            refs.append((str(p.relative_to(base.parent)), m.group(0)))
    return refs


def check_manifest(doc: dict | None = None, *, strict: bool = False) -> dict:
    """manifest ↔ 盘面对拍 + 四许可字段 + 体积预算 + commercial 引用校验。

    返回 {"ok", "errors", "warnings", "counts"};errors 恒为失败;warnings 仅
    --strict 时升级为失败(退出码由 CLI 层决定)。
    """
    doc = doc if doc is not None else load_manifest()
    errors: list[str] = []
    warnings: list[str] = []
    if doc is None:
        return {"ok": False, "errors": [f"缺少统一素材索引:{MANIFEST_PATH}"],
                "warnings": [], "counts": {}}
    seen: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for a in doc.get("assets", []):
        aid = str(a.get("id", ""))
        where = f"[{aid}]"
        # ---- id:唯一 + 命名法(分册01 §3;末段允许语义名,如 element.arrow.right)
        if not aid:
            errors.append("[<无 id>] 条目缺 id")
            continue
        if aid in seen:
            errors.append(f"{where} id 重复(与 {seen[aid].get('file')})")
        seen[aid] = a
        segs = aid.split(".")
        if (len(segs) < 3 or segs[0] not in KINDS
                or not re.fullmatch(r"[a-z0-9_.]+", aid)):
            errors.append(f"{where} id 命名法非法(应 <kind>.<组>.<序号|语义名>)")
        kind = str(a.get("kind", ""))
        if kind not in KINDS:
            errors.append(f"{where} kind 非法:{kind!r}")
            continue
        counts[kind] = counts.get(kind, 0) + 1
        # ---- 四许可字段(ADR-0053 硬约束:缺一即门禁失败)
        for f in LICENSE_FIELDS:
            v = a.get(f)
            if v is None or (isinstance(v, str) and not v.strip()):
                errors.append(f"{where} 许可四字段缺 {f}")
        if not isinstance(a.get("commercial"), bool):
            errors.append(f"{where} commercial 必须是布尔")
        if "aiGenerated" not in a:
            warnings.append(f"{where} 缺 aiGenerated(R45)")
        # ---- 盘面对拍
        rel = str(a.get("file", ""))
        if not rel:
            errors.append(f"{where} 缺 file")
            continue
        p = ASSETS_DIR / rel
        if not p.is_file():
            errors.append(f"{where} file 不存在:{rel}")
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if str(a.get("sha256", "")) != digest:
            errors.append(f"{where} sha256 与盘面不符(先重跑 tools/synth_assets.py --manifest-only)")
        budget = SIZE_BUDGET.get(kind)
        size = p.stat().st_size
        if budget and size > budget:
            errors.append(f"{where} 超体积预算:{size}B > {budget}B")
        # ---- 时长(sfx/bgm 必须实测;元素/花字模板静态无时长)
        if kind in ("sfx", "bgm") and int(a.get("durationMs") or 0) <= 0:
            errors.append(f"{where} durationMs 缺失或非正(sfx/bgm 必须 ffprobe 实测)")
        if kind == "bgm" and not (a.get("pacingFit") or []):
            warnings.append(f"{where} bgm 缺 pacingFit(pacing×mood 矩阵选不到它)")
        if not (a.get("usage") or []):
            warnings.append(f"{where} 缺 usage(语义检索检索不到)")
        elif any(u not in USAGES for u in a.get("usage") or []):
            errors.append(f"{where} usage 出枚举:{a.get('usage')}")
        if kind == "element" and rel.endswith(".png"):
            variants = a.get("variants") or []
            for v in variants:
                if not (ASSETS_DIR / str(v)).is_file():
                    warnings.append(f"{where} 变体缺盘面:{v}")
    # ---- 数量判据(分册01 §8.2:sfx ≥40 / element ≥48 / huazi ≥14 / bgm ≥16)
    for need, k in ((40, "sfx"), (48, "element"), (14, "huazi"), (16, "bgm")):
        if counts.get(k, 0) < need:
            errors.append(f"kind={k} 仅 {counts.get(k, 0)} 条,低于 M12 验收线 {need}")
    # ---- commercial:false 不得被风格包/提示词模板引用(R42/门禁 2)
    noncommercial = {a["id"] for a in doc.get("assets", [])
                     if a.get("commercial") is False}
    pack_refs = _scan_stylepack_references()
    for src, ref in pack_refs:
        if ref in noncommercial:
            errors.append(f"风格包/模板 {src} 引用了不可商用素材 {ref}(R42 门禁)")
        elif find(ref) is None and ref.split(".")[0] in KINDS:
            warnings.append(f"风格包/模板 {src} 引用的素材 id 不在索引:{ref}")
    # ---- 商用红线总闸:任何 commercial:false 条目存在即提示(允许登记,但不可入交付)
    if noncommercial:
        warnings.append(f"{len(noncommercial)} 条 commercial:false 素材(不得进入交付:"
                        f"{','.join(sorted(noncommercial)[:5])})")
    ok = not errors and (not strict or not warnings)
    return {"ok": ok, "errors": errors, "warnings": warnings, "counts": counts}


# ---------------------------------------------------------------- 归因(交付面)

def collect_project_refs(root: Path) -> list[dict]:
    """工程实际引用的素材(单一口径,归因与 verify 共用)。

    扫描两处:
      ① 05_时间线工程/project.json(IR)audio clips 的 `assets_sfx:<ref>` 伪协议;
      ② 00_制作简报/intent_decisions.json resolved.bgm.src(仓库相对路径在曲库内)。
    命中带 {id, via};重复引用去重保序。
    """
    root = rs_paths.root(root)
    hits: list[dict] = []
    seen: set[str] = set()

    def _add(ref: str, via: str, kind: str) -> None:
        a = find(ref, kind)
        if a is not None and a["id"] not in seen:
            seen.add(a["id"])
            hits.append({"id": a["id"], "kind": a["kind"], "label": a.get("label", ""),
                         "source": a.get("source", ""), "license": a.get("license", ""),
                         "commercial": a.get("commercial", True),
                         "attribution": a.get("attribution", ""),
                         "aiGenerated": a.get("aiGenerated", False),
                         "file": a.get("file", ""), "via": via})

    pj = rs_paths.project_json(root)
    if pj.is_file():
        try:
            ir = json.loads(pj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            ir = {}
        for t in ir.get("tracks") or []:
            for c in t.get("clips") or []:
                src = str(c.get("src") or "")
                if src.startswith("assets_sfx:"):
                    _add(src.split(":", 1)[1], "IR audio clip", "sfx")
    intent = root / rs_paths.p("brief") / "intent_decisions.json"
    if intent.is_file():
        try:
            bgm_src = str(((json.loads(intent.read_text(encoding="utf-8"))
                            .get("resolved") or {}).get("bgm") or {}).get("src") or "")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            bgm_src = ""
        if bgm_src:
            for a in assets("bgm"):
                if str(a.get("file", "")).endswith(Path(bgm_src).name):
                    _add(a["id"], "intent bgm.src", "bgm")
                    break
    return hits


# 归因清单/交付面里素材 id 的提取口径(表格写法:反引号包裹)
ASSET_ID_RE = re.compile(r"`((?:sfx|element|huazi|bgm)\.[\w.]+)`")


def asset_ids_in(text: str) -> set[str]:
    """从文本提取素材 id 集(归因双向对拍共用;rs_verify/rs_ingest 同口径)。"""
    return set(ASSET_ID_RE.findall(text or ""))


def build_attribution_md(root: Path, refs: list[dict] | None = None) -> str:
    """素材归因清单(成品/说明书/素材归因.md):只列本工程实际引用的素材。"""
    refs = collect_project_refs(root) if refs is None else refs
    root = rs_paths.root(root)
    lines = [f"# 素材归因 — {root.name}", "",
             "> 由 `rs_asset.py attribution` 生成(经 rs_ingest deliverables --publish"
             " 汇入交付);条目 = 本工程实际引用的素材,与统一索引逐条对拍。", ""]
    if not refs:
        lines += ["本工程未引用内置素材库条目(无外部素材归因义务)。", ""]
        return "\n".join(lines)
    lines += ["| 素材 id | 类别 | 名称 | 来源 | 许可 | 可商用 | AI 生成 | 归因 | 引用位置 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in refs:
        lines.append(
            f"| `{r['id']}` | {r['kind']} | {r['label']} | {r['source']} | {r['license']} "
            f"| {'是' if r['commercial'] else '**否**'} | {'是' if r['aiGenerated'] else '否'} "
            f"| {r['attribution']} | {r['via']} |")
    ai = [r for r in refs if r["aiGenerated"]]
    if ai:
        lines += ["", f"AI 生成标识(R45):以上 {len(ai)} 条为自产合成素材"
                  f"(aiGenerated=true)。"]
    nc = [r for r in refs if not r["commercial"]]
    if nc:
        lines += ["", f"⚠ 不可商用素材引用({len(nc)} 条):"
                  f"{','.join(r['id'] for r in nc)} —— 不得进入交付。"]
    return "\n".join(lines) + "\n"


def write_attribution(root: Path, refs: list[dict] | None = None) -> Path:
    """归因清单落盘(工程区 06_成片输出 与 发布流程均经此写,publish 拷入成品/)。"""
    refs = collect_project_refs(root) if refs is None else refs
    dst = rs_paths.resolve(root, "output") / "素材归因.md"
    write_text_atomic(dst, build_attribution_md(rs_paths.root(root), refs))
    return dst


# ---------------------------------------------------------------- add / scan

def _probe_duration_ms(path: Path) -> int:
    from rs_common import ffprobe_json
    try:
        info = ffprobe_json(path)
    except SystemExit:
        return 0
    return int(round(float(info.get("format", {}).get("duration") or 0) * 1000))


def _next_seq(pool: list[dict], kind: str, group: str) -> int:
    nums = []
    for a in pool:
        segs = str(a.get("id", "")).split(".")
        if len(segs) == 3 and segs[0] == kind and segs[1] == group and segs[2].isdigit():
            nums.append(int(segs[2]))
    return (max(nums) + 1) if nums else 1


def _register(kind: str, src: Path, *, group: str, label: str, tags: list[str],
              usage: list[str], source: str, license_name: str, commercial: bool,
              attribution: str, ai_generated: bool, gain_hint_db: int,
              extra: dict | None = None) -> tuple[dict, str]:
    """登记/更新一条素材(拷贝进素材库 + 写索引);返回 (条目, 动作)。"""
    if kind not in KINDS:
        raise ValueError(f"未知 kind {kind!r}(可选 {'/'.join(KINDS)})")
    pool = assets(kind)
    dest_dir = ASSETS_DIR / {"sfx": "sfx", "bgm": "bgm", "element": "elements/png",
                             "huazi": "huazi/ass"}[kind]
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = next((a for a in pool
                     if Path(a.get("file", "")).name == src.name), None)
    if existing:
        aid, action = existing["id"], "changed" if existing.get("sha256") != \
            hashlib.sha256(src.read_bytes()).hexdigest() else "unchanged"
    else:
        seq = _next_seq(pool, kind, group)
        aid = f"{kind}.{group}.{seq:02d}"
        action = "added"
    dest = dest_dir / src.name
    if src.resolve() != dest.resolve():
        dest.write_bytes(src.read_bytes())
    duration_ms = _probe_duration_ms(dest) if kind in ("sfx", "bgm") else 0
    entry = {"id": aid, "kind": kind, "label": label or src.stem, "file":
             str(dest.relative_to(ASSETS_DIR)).replace("\\", "/"),
             "tags": tags, "usage": usage or ["decor" if kind in ("element", "huazi") else "emotion"],
             "durationMs": duration_ms, "gainHintDb": gain_hint_db,
             "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
             "source": source, "license": license_name, "commercial": commercial,
             "attribution": attribution, "aiGenerated": ai_generated}
    if extra:
        entry.update(extra)
    return entry, action


def _write_manifest_atomic(assets_list: list[dict]) -> None:
    import time as _time
    doc = {"version": 1, "generatedAt": _time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
           "assets": assets_list}
    write_text_atomic(MANIFEST_PATH,
                      json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


# ---------------------------------------------------------------- CLI

def _fmt_list(items: list[dict]) -> str:
    kinds: dict[str, int] = {}
    for a in items:
        kinds[a.get("kind", "?")] = kinds.get(a.get("kind", "?"), 0) + 1
    head = " ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
    return f"{len(items)} 条({head})"


def cmd_list(a: argparse.Namespace) -> int:
    items = assets(a.kind)
    if a.tag:
        items = [x for x in items if a.tag in (x.get("tags") or [])]
    if a.usage:
        items = [x for x in items if a.usage in (x.get("usage") or [])]
    if a.commercial_only:
        items = [x for x in items if x.get("commercial") is True]
    if a.json:
        return emit(True, "ASSET_LIST_OK", _fmt_list(items), {"assets": items})
    for x in items:
        lic = "" if x.get("commercial", True) else "  [不可商用]"
        print(f"{x['id']:<28} {x.get('label', '')}  {','.join(x.get('tags') or [])}{lic}")
    return emit(True, "ASSET_LIST_OK", _fmt_list(items), {"count": len(items)})


def cmd_search(a: argparse.Namespace) -> int:
    """标签精确 → 标签子串 → label 子串,三级计分(纯本地,零模型,确定性)。"""
    q = a.query.strip()
    scored: list[tuple[int, dict]] = []
    for x in assets(a.kind):
        tags = x.get("tags") or []
        s = 0
        if q in tags:
            s = 100
        elif any(q in t for t in tags):
            s = 60
        elif q in str(x.get("label", "")):
            s = 30
        else:
            hay = " ".join([*tags, str(x.get("label", "")), str(x.get("mood", "")),
                            " ".join(x.get("usage") or [])])
            if q in hay:
                s = 10
        if s:
            scored.append((-s, x))
    scored.sort(key=lambda t: (t[0], str(t[1]["id"])))
    items = [x for _, x in scored[:a.limit]]
    if a.json:
        return emit(True, "ASSET_SEARCH_OK", f"命中 {len(items)} 条", {"assets": items})
    for x in items:
        print(f"{x['id']:<28} {x.get('label', '')}  {','.join(x.get('tags') or [])}")
    return emit(True, "ASSET_SEARCH_OK", f"命中 {len(items)} 条", {"count": len(items)})


def cmd_get(a: argparse.Namespace) -> int:
    hit = find(a.id)
    if hit is None:
        return emit(False, "ASSET_NOT_FOUND", f"索引中没有 {a.id}", exit_code=EXIT_INPUT)
    data = dict(hit)
    data["absPath"] = str(file_of(hit))
    data["repoRelPath"] = file_of(hit).relative_to(REPO_ROOT).as_posix()
    data["sfxRef"] = f"assets_sfx:{hit['id']}" if hit["kind"] == "sfx" else None
    msg = f"{hit['id']} → {data['repoRelPath']}"
    if not hit.get("commercial", True):
        msg += "(⚠ 不可商用:不得进入交付)"
    return emit(True, "ASSET_GET_OK", msg, data)


def cmd_check(a: argparse.Namespace) -> int:
    res = check_manifest(strict=a.strict)
    code = "ASSET_CHECK_OK" if res["ok"] else "ASSET_CHECK_FAIL"
    msg = (f"check {'通过' if res['ok'] else '失败'}:{res['counts']};"
           f"errors={len(res['errors'])} warnings={len(res['warnings'])}")
    data = {"strict": a.strict, **res}
    return emit(res["ok"], code, msg, data, exit_code=EXIT_OK if res["ok"] else EXIT_EXEC)


def _resolve_root(a: argparse.Namespace) -> Path:
    """工程模式:--root 缺省取 cwd(cwd 即工程根的约定场景)。"""
    r = Path(a.root) if getattr(a, "root", None) else Path.cwd()
    return rs_paths.root(r)


def cmd_attribution(a: argparse.Namespace) -> int:
    root = _resolve_root(a)
    refs = collect_project_refs(root)
    refs = sorted(refs, key=lambda r: (r["kind"], r["id"]))
    dst = Path(a.out) if a.out else rs_paths.resolve(root, "output") / "素材归因.md"
    if dst.is_absolute():
        write_text_atomic(dst, build_attribution_md(root, refs))
    else:
        write_text_atomic(root / dst, build_attribution_md(root, refs))
    nc = [r["id"] for r in refs if not r["commercial"]]
    msg = f"归因清单 {len(refs)} 条 → {dst}"
    if nc:
        msg += f";⚠ 不可商用 {len(nc)} 条:{','.join(nc)}"
    return emit(not nc, "ASSET_ATTRIBUTION_OK" if not nc else "ASSET_ATTRIBUTION_NONCOMMERCIAL",
                msg, {"path": str(dst), "count": len(refs), "noncommercial": nc,
                      "ids": [r["id"] for r in refs]},
                exit_code=EXIT_OK if not nc else EXIT_EXEC)


def cmd_add(a: argparse.Namespace) -> int:
    src = Path(a.file)
    if not src.is_file():
        return emit(False, "ASSET_ADD_NO_FILE", f"素材文件不存在:{src}", exit_code=EXIT_INPUT)
    doc = load_manifest() or {"version": 1, "assets": []}
    kind = a.kind
    entry, action = _register(
        kind, src, group=a.group or (src.stem.split("_")[0].lower()),
        label=a.label or "", tags=[t for t in (a.tags or "").split(",") if t.strip()],
        usage=[u for u in (a.usage or "").split(",") if u.strip()],
        source=a.source, license_name=a.license,
        commercial=str(a.commercial).lower() in ("1", "true", "yes"),
        attribution=a.attribution,
        ai_generated=str(a.ai_generated).lower() in ("1", "true", "yes"),
        gain_hint_db=a.gain_db)
    pool = [x for x in doc.get("assets", []) if x.get("id") != entry["id"]]
    pool.append(entry)
    doc["assets"] = sorted(pool, key=lambda x: str(x.get("id", "")))
    _write_manifest_atomic(doc["assets"])
    return emit(True, "ASSET_ADD_OK", f"{action}: {entry['id']} → {entry['file']}",
                {"asset": entry, "action": action})


def cmd_scan(a: argparse.Namespace) -> int:
    d = Path(a.dir)
    if not d.is_dir():
        return emit(False, "ASSET_SCAN_NO_DIR", f"目录不存在:{d}", exit_code=EXIT_INPUT)
    doc = load_manifest() or {"version": 1, "assets": []}
    pool = doc.get("assets", [])
    changed: list[dict] = []
    stats = {"added": 0, "changed": 0, "unchanged": 0, "removed": 0}
    for f in sorted(d.rglob("*")):
        if f.suffix.lower() not in SUPPORTED_EXT or not f.is_file():
            continue
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        existing = next((x for x in pool if x.get("kind") == a.kind
                         and Path(x.get("file", "")).name == f.name), None)
        if existing and existing.get("sha256") == digest:
            stats["unchanged"] += 1
            continue
        entry, action = _register(
            a.kind, f, group=(a.group or f.stem.split("_")[0].lower()),
            label=f.stem, tags=[t for t in (a.tags or "").split(",") if t.strip()],
            usage=[u for u in (a.usage or "").split(",") if u.strip()],
            source=a.source, license_name=a.license,
            commercial=str(a.commercial).lower() in ("1", "true", "yes"),
            attribution=a.attribution,
            ai_generated=str(a.ai_generated).lower() in ("1", "true", "yes"),
            gain_hint_db=a.gain_db)
        pool = [x for x in pool if x.get("id") != entry["id"]]
        pool.append(entry)
        changed.append({"id": entry["id"], "file": entry["file"], "action": action})
        stats[action] += 1
    # 移除对账:该 kind 下盘面已消失的条目(按 kind 目录前缀圈定,不误伤他类)
    kind_prefix = {"sfx": "sfx/", "bgm": "bgm/", "element": "elements/", "huazi": "huazi/"}[a.kind]
    kept: list[dict] = []
    for x in pool:
        if x.get("kind") == a.kind and str(x.get("file", "")).startswith(kind_prefix):
            p = ASSETS_DIR / str(x.get("file", ""))
            if not p.is_file():
                stats["removed"] += 1
                changed.append({"id": x.get("id"), "file": x.get("file"), "action": "removed"})
                continue
        kept.append(x)
    doc["assets"] = sorted(kept, key=lambda x: str(x.get("id", "")))
    _write_manifest_atomic(doc["assets"])
    return emit(True, "ASSET_SCAN_OK",
                f"scan {a.kind}:新增 {stats['added']} / 变更 {stats['changed']} / "
                f"移除 {stats['removed']} / 未变 {stats['unchanged']}",
                {"stats": stats, "changed": changed})


def main() -> int:
    ap = argparse.ArgumentParser(description="CutFlow 素材库(ADR-0053)")
    sub = ap.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("list", help="列出素材")
    sp.add_argument("--kind", default=None, help="逗号分隔:" + "/".join(KINDS))
    sp.add_argument("--tag", default=None)
    sp.add_argument("--usage", default=None, help="语义用途:" + "/".join(USAGES))
    sp.add_argument("--commercial-only", dest="commercial_only", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("search", help="标签/名称检索(纯本地)")
    sp.add_argument("query")
    sp.add_argument("--kind", default=None)
    sp.add_argument("--limit", type=int, default=5)
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("get", help="素材详情 + 落盘引用")
    sp.add_argument("id")

    sp = sub.add_parser("check", help="许可与完整性门禁")
    sp.add_argument("--strict", action="store_true", help="warnings 也视为失败")

    sp = sub.add_parser("attribution", help="生成本工程素材归因清单")
    sp.add_argument("--root", default=None, help="工程根(缺省取 cwd)")
    sp.add_argument("--out", default=None, help="落盘路径(缺省 06_成片输出/素材归因.md)")

    sp = sub.add_parser("add", help="登记一条新素材")
    sp.add_argument("--kind", required=True, choices=KINDS)
    sp.add_argument("--file", required=True)
    sp.add_argument("--group", default=None, help="id 组名(缺省取文件名首段)")
    sp.add_argument("--label", default="")
    sp.add_argument("--tags", default="")
    sp.add_argument("--usage", default="")
    sp.add_argument("--source", required=True)
    sp.add_argument("--license", required=True)
    sp.add_argument("--commercial", default="true")
    sp.add_argument("--attribution", required=True)
    sp.add_argument("--ai-generated", dest="ai_generated", default="true")
    sp.add_argument("--gain-db", dest="gain_db", type=int, default=-14)

    sp = sub.add_parser("scan", help="批量入库(新增/变更/移除三态对账)")
    sp.add_argument("dir")
    sp.add_argument("--kind", required=True, choices=KINDS)
    sp.add_argument("--group", default=None)
    sp.add_argument("--tags", default="")
    sp.add_argument("--usage", default="")
    sp.add_argument("--source", required=True)
    sp.add_argument("--license", required=True)
    sp.add_argument("--commercial", default="true")
    sp.add_argument("--attribution", required=True)
    sp.add_argument("--ai-generated", dest="ai_generated", default="true")
    sp.add_argument("--gain-db", dest="gain_db", type=int, default=-14)

    a = ap.parse_args()
    if a.command == "list":
        if a.kind:
            bad = [k for k in a.kind.split(",") if k not in KINDS]
            if bad:
                return emit(False, "BAD_KIND", f"未知 kind:{bad}(可选 {'/'.join(KINDS)})",
                            exit_code=EXIT_INPUT)
        return cmd_list(a)
    if a.command == "search":
        return cmd_search(a)
    if a.command == "get":
        return cmd_get(a)
    if a.command == "check":
        return cmd_check(a)
    if a.command == "attribution":
        return cmd_attribution(a)
    if a.command == "add":
        return cmd_add(a)
    if a.command == "scan":
        return cmd_scan(a)
    return emit(False, "BAD_COMMAND", f"未知子命令:{a.command}", exit_code=EXIT_INPUT)


if __name__ == "__main__":
    sys.exit(main())
