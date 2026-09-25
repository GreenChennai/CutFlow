"""效果目录 CLI(M13/ADR-0054,分册02 §7):效果目录的查询/校验/规范化/覆盖率。

用法:
  python rs_effects.py list [--category in,out,combo,transition] [--subclass X]
         [--tier T1] [--status 可执行] [--commonness 高] [--source 用户清单,补充清单]
         [--json]
  python rs_effects.py show <id>
  python rs_effects.py search <query> [--limit 5]
  python rs_effects.py normalize [--source sources/剪映效果名-原始清单.md] [--report]
  python rs_effects.py check
  python rs_effects.py coverage [--json]

normalize 纪律(分册02 §2.4):**不做人工转录** —— 原始清单(sources/)逐字保留,
本命令做机器规范化:去重 / [fx:] 组归并(别名进 aliases)/ 模式打级 / 删除名单
(EFFECT_REMOVED)→ 幂等输出 catalog.json(同输入字节级同输出)。check 是门禁入口
(分级↔状态↔fxId 三向自洽;注册表无孤儿;删除项 why 含「用户确认删除」)。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit  # noqa: E402
import rs_fx  # noqa: E402  — fxId 注册表(M13 单一真相源)

EFFECTS_DIR = Path(__file__).resolve().parents[1] / "templates" / "effects"
CATALOG_PATH = EFFECTS_DIR / "catalog.json"
DEFAULT_SOURCE = EFFECTS_DIR / "sources" / "剪映效果名-原始清单.md"

REMOVED_WHY = "语义不明(原文含 OCR 噪声),用户确认删除"
T2_PENDING_WHY = "T2 候选(需着色器/素材/字幕侧逐元素表达);本轮未落地,登记待实现"
TRANSITION_T2_PENDING_WHY = "T2 候选转场(GLSL 遮罩/3D/粒子);gl-transitions 未收录或逐条验证未过"

# §6 标准 NLE 通用效果词典(62 条)的规范 id → 本仓落地 id 族。
# 直登条目(id 即 fxId)不列;参数化家族列出全部落地档。
NLE62_FAMILY: dict[str, list[str]] = {
    "tr.wipe.linear": ["tr.wipe.left", "tr.wipe.right", "tr.wipe.up", "tr.wipe.down",
                       "tr.wipe.tl", "tr.wipe.tr", "tr.wipe.bl", "tr.wipe.br",
                       "tr.wipe.diag.tl", "tr.wipe.diag.tr", "tr.wipe.diag.bl",
                       "tr.wipe.diag.br"],
    "tr.push.dir": ["tr.push.left", "tr.push.right", "tr.push.up", "tr.push.down"],
    "tr.roll.dir": ["tr.roll.left", "tr.roll.right", "tr.roll.up", "tr.roll.down"],
    "in.slide.dir": ["in.slide.left", "in.slide.right", "in.slide.up", "in.slide.down"],
    "out.slide.dir": ["out.slide.left", "out.slide.right", "out.slide.up"],
    "in.wipe.dir4": ["in.wipe.dir"],
    "out.wipe.dir4": ["out.wipe.dir"],
}
NLE62_IDS: frozenset[str] = frozenset({
    # 转场 24(§6.1)
    "tr.fade.black", "tr.fade.white", "tr.dissolve.cross", "tr.blur.dissolve",
    "tr.iris.circle", "tr.whip.pan", "tr.invisible.cut", "tr.match.cut",
    "tr.lcut", "tr.jcut", "tr.audio.crossfade", "tr.punch.zoom", "tr.flash.zoom",
    "tr.hold.frame", "tr.light.leak", "tr.film.burn", "tr.glitch.slice",
    "tr.gradient.wipe", "tr.shape.wipe", "tr.morph.cut",
    "tr.speed.ramp",   # = effect.speed.ramp 的转场语义(同一落点登记)
    "tr.wipe.linear", "tr.push.dir", "tr.roll.dir",
} | set(NLE62_FAMILY) - {"in.slide.dir", "out.slide.dir", "in.wipe.dir4", "out.wipe.dir4"}
| {
    # 入场 26(§6.2)
    "in.fade.up", "in.fade.down", "in.fade.left", "in.fade.right",
    "in.anchor.scale", "in.scale.pop", "in.blur.in", "in.clip.reveal",
    "in.stagger", "in.text.char", "in.text.word", "in.text.line", "in.typewriter",
    "in.count.up", "in.progress.bar", "in.ring.progress", "in.glow",
    "in.shadow.drop", "in.skew", "in.breathe", "in.bounce.pop", "in.lower.third",
    "in.mask.reveal", "in.stroke.draw", "in.cursor.click", "in.keystroke",
    # 出场 10(§6.3)
    "out.fade.up", "out.fade.down", "out.fade.left", "out.fade.right",
    "out.scale.shrink", "out.blur.out", "out.wipe.dir", "out.stagger",
    "out.spin.fade", "out.mask.close", "out.collapse", "out.letterbox", "out.slide.off",
    # 组合 2(§6.4)
    "fx.compare.slider", "fx.spotlight",
})

# 分册02 §4.2 不实现模式清单(命中即不实现;逃生口 = artboard 场景卡,why 里注明)
_NOT_IMPLEMENTED_PATTERNS: list[tuple[str, str]] = [
    (r"玫瑰|枫叶|樱花|花瓣|心形|爱心|粽叶|落叶|茉莉|桂香|云朵|星星",
     "图形遮罩类且与内容强绑定(造型固定只适用特定题材);artboard 一次性定制更划算"),
    (r"iPhone|NEWS|手机|状态栏|报纸|相册|轻触|订阅|开机|倒计时|Dally|Daily",
     "品牌/界面模板类,有商标与形象风险且画幅/语言强绑定;平台 UI 走 elements/ 通用元素"),
    (r"冰块|冰棱|玻璃|钻石|水晶|金属|箔带|绒布",
     "拟物类(GLSL 成本极高、性价比低);T1 近似档另登记(如 effect.ice.frost)"),
    (r"中秋|月圆|春节|圣诞|樱花季",
     "节庆类具象造型,与内容强绑定;artboard 场景卡一次性定制更划算"),
    (r"足球|篮球|赛车",
     "人物/体育具象类,与内容强绑定;artboard 一次性定制更划算"),
]

_COMMONNESS_T2_DEFAULT = "中"


# ---------------------------------------------------------------- 目录读写

def load_catalog() -> dict | None:
    if not CATALOG_PATH.is_file():
        return None
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def entries_of(catalog: dict | None) -> list[dict]:
    if not catalog:
        return []
    es = catalog.get("effects")
    return [e for e in es if isinstance(e, dict)] if isinstance(es, list) else []


def is_executable(fx_id: str) -> bool:
    """fxId 在目录中是否可执行(status=可执行 且 fxId 已注册;intent/verify 共用)。"""
    e = next((x for x in entries_of(load_catalog()) if x.get("id") == fx_id), None)
    return bool(e and e.get("status") == "可执行" and e.get("fxId") in rs_fx.registered_ids())


def _entry_id_for_name(name: str, category: str) -> str:
    """非可执行条目的确定性 id:`fx.<category>.<原名>`(中文 id 合法且稳定)。"""
    return f"fx.{category}.{name}"


# ---------------------------------------------------------------- normalize

def _parse_source(path: Path) -> dict:
    """sources 清单 → {groups: {fxId: [名…]}, pending: [(名, section)], explicit: […],
    deleted: [名], nonEffects: [名], sectionOf: {名: section}}。"""
    groups: dict[str, list[str]] = {}
    pending: list[tuple[str, str]] = []
    explicit: list[tuple[str, str]] = []          # (名, why) —— [不实现: …]
    deleted: list[str] = []
    non_effects: list[str] = []
    section = ""
    current_fx: str | None = None
    section_kind = "normal"
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") and not line.startswith("#"):
            pass
        if line.startswith("## "):
            section = line[3:].strip()
            current_fx = None
            section_kind = ("deleted" if "已确认删除" in section
                            else "non-effect" if "非效果" in section
                            else "t2" if "T2" in section
                            else "normal")
            continue
        if line.startswith("[fx:") and line.endswith("]"):
            current_fx = line[4:-1].strip()
            continue
        if not line.startswith("- "):
            continue
        name = line[2:].strip()
        m_no = re.search(r"\[不实现[:：]\s*(.+?)\]\s*$", name)
        if m_no:
            explicit.append((name[:m_no.start()].strip(), m_no.group(1).strip()))
            continue
        if section_kind == "deleted" or re.search(r"\[删除\]\s*$", name):
            deleted.append(re.sub(r"\[删除\]\s*$", "", name).strip())
            continue
        if section_kind == "non-effect":
            non_effects.append(name)
            continue
        if current_fx:
            groups.setdefault(current_fx, []).append(name)
        else:
            pending.append((name, section))
    return {"groups": groups, "pending": pending, "explicit": explicit,
            "deleted": deleted, "nonEffects": non_effects}


def _pattern_not_implemented(name: str) -> str | None:
    for pat, why in _NOT_IMPLEMENTED_PATTERNS:
        if re.search(pat, name):
            return why
    return None


def normalize(source_path: Path = DEFAULT_SOURCE) -> tuple[dict, dict]:
    """sources + 注册表 → (catalog, report)。纯函数,幂等(同输入同字节输出)。"""
    fx = rs_fx
    src = _parse_source(source_path)
    used_names: set[str] = set()
    effects: list[dict] = []
    alias_owner: dict[str, str] = {}

    def _claim_alias(alias: str, owner: str) -> bool:
        if not alias or alias in alias_owner:
            return False
        alias_owner[alias] = owner
        used_names.add(alias)
        return True

    # ---- ① 注册表全量 → 可执行 / 登记待实现(T2 占位)条目(id == fxId) ----
    for fxid, e in fx.TRANSITIONS.items():
        tier = e.get("tier", "T1")
        entry = {
            "id": fxid, "label": e.get("label", fxid), "category": "transition",
            "subclass": "基础" if tier == "T1" and not e.get("flashy") else "基础",
            "aliases": [], "tier": tier, "status": "可执行", "fxId": fxid,
            "params": {"durMs": e.get("durMs")}, "usage": [], "commonness": "高"
            if fxid in ("tr.fade.black", "tr.dissolve.cross", "tr.cut") else "中",
            "why": None, "source": "补充清单" if fxid in NLE62_IDS else "用户清单",
            "license": e.get("license") if tier == "T2" else None,
            "domain": e.get("domain") or "render",
            "note": e.get("note"),
            "originName": None,
        }
        if e.get("flashy"):
            entry["note"] = ((entry["note"] + ";") if entry["note"] else "") + \
                "花哨类(处方 flashy_max 管辖,分册06 §5.1)"
        effects.append(entry)
    for fxid, e in fx.CLIP_FX.items():
        tier = e.get("tier", "T1")
        slot = e.get("slot", "any")
        category = ("transition" if fxid.startswith("tr.")
                    else "combo" if slot == "combo" or fxid.startswith("fx.")
                    else slot if slot in ("in", "out") else "in")
        entry = {
            "id": fxid, "label": e.get("label", fxid), "category": category,
            "subclass": "标准NLE" if fxid in NLE62_IDS else _subclass_of(fxid),
            "aliases": [], "tier": tier,
            "status": "登记待实现" if tier == "T2" else "可执行",
            "fxId": fxid, "params": dict(e.get("params") or {}),
            "usage": list(e.get("usage") or []),
            "commonness": _COMMONNESS_T2_DEFAULT,
            "why": T2_PENDING_WHY if tier == "T2" else None,
            "source": "补充清单" if fxid in NLE62_IDS else "用户清单",
            "license": None, "domain": e.get("domain") or "render",
            "note": e.get("note"), "originName": None,
        }
        effects.append(entry)

    # ---- ② sources [fx:] 组 → 别名归并(合并同类项的证据) ----
    orphans: list[str] = []
    by_id = {e["id"]: e for e in effects}
    for fxid, names in src["groups"].items():
        e = by_id.get(fxid)
        if e is None:
            orphans.append(fxid)
            continue
        for n in names:
            if _claim_alias(n, fxid) and n not in (e["aliases"] or []):
                e["aliases"].append(n)

    # ---- ③ 显式不实现 / 删除名单 ----
    for name, why in src["explicit"]:
        if name in used_names:
            continue
        effects.append({
            "id": _entry_id_for_name(name, "unknown"), "label": name,
            "category": "unknown", "subclass": "特效", "aliases": [name],
            "tier": "T3", "status": "不实现", "fxId": None, "params": None,
            "usage": [], "commonness": "低", "why": why, "source": "用户清单",
            "license": None, "domain": "artboard", "note": "逃生口:artboard 场景卡",
            "originName": name})
        _claim_alias(name, _entry_id_for_name(name, "unknown"))
    for name in src["deleted"]:
        eid = _entry_id_for_name(name, "unknown")
        effects.append({
            "id": eid, "label": name, "category": "unknown", "subclass": "已确认删除",
            "aliases": [name], "tier": "T3", "status": "不实现", "fxId": None,
            "params": None, "usage": [], "commonness": "低", "why": REMOVED_WHY,
            "source": "用户清单", "license": None, "domain": "render",
            "note": None, "originName": name})
        _claim_alias(name, eid)

    # ---- ④ 无组头散名:T2 候选 / 模式判据 / 兜底登记待实现 ----
    for name, section in src["pending"]:
        if name in used_names:
            continue
        eid = _entry_id_for_name(name, "transition" if "转场" in section else "in")
        if section and "T2" in section:
            why = TRANSITION_T2_PENDING_WHY
            tier, domain = "T2", "render"
        else:
            why = _pattern_not_implemented(name)
            tier, domain = ("T3", "artboard") if why else ("T2", "render")
            if not why:
                why = T2_PENDING_WHY
        effects.append({
            "id": eid, "label": name,
            "category": "transition" if "转场" in section else "unknown",
            "subclass": "T2候选" if tier == "T2" else "特效",
            "aliases": [name], "tier": tier,
            "status": "登记待实现" if tier == "T2" else "不实现",
            "fxId": None, "params": None, "usage": [],
            "commonness": "低", "why": why,
            "source": "用户清单", "license": None, "domain": domain,
            "note": None, "originName": name})
        _claim_alias(name, eid)

    effects.sort(key=lambda e: e["id"])
    report = {
        "source": str(source_path),
        "registryTransitions": len(fx.TRANSITIONS),
        "registryClipFx": len(fx.CLIP_FX),
        "aliasGroups": sum(len(v) for v in src["groups"].values()),
        "explicitNotImplemented": len(src["explicit"]),
        "deleted": len(src["deleted"]),
        "pendingT2": sum(1 for e in effects if e["status"] == "登记待实现"),
        "nonEffectsExcluded": len(src["nonEffects"]),
        "orphanGroupIds": sorted(orphans),
        "total": len(effects),
    }
    stats = _stats(effects)
    catalog = {"version": 1, "kind": "cutflow-effect-catalog",
               "sourceDoc": f"sources/{source_path.name}",
               "stats": stats, "effects": effects}
    report["stats"] = stats
    return catalog, report


def _subclass_of(fxid: str) -> str:
    if fxid.startswith("tr."):
        return "基础"
    if fxid.startswith(("in.text.", "in.typewriter", "in.count.", "in.lower.")):
        return "标准NLE" if fxid in NLE62_IDS else "MG"
    for key, sub in (("effect.", "特效"), ("zoompan.", "运镜"), ("push.", "运镜"),
                     ("pull.", "运镜"), ("slide.", "运镜"), ("in.slide.", "运镜"),
                     ("out.slide.", "运镜"), ("rotate.", "运镜"), ("jitter.", "运镜"),
                     ("swing.", "运镜"), ("stretch.", "运镜"), ("blur.", "运镜"),
                     ("fade.", "基础"), ("flash.", "基础"), ("dissolve.", "基础"),
                     ("dither.", "基础"), ("in.wipe.", "遮罩"), ("out.wipe.", "遮罩"),
                     ("in.clip.", "遮罩")):
        if fxid.startswith(key):
            return sub
    return "组合"


def _stats(effects: list[dict]) -> dict:
    out: dict = {"total": len(effects)}
    for key in ("status", "tier", "source", "subclass", "domain", "category"):
        bucket: dict[str, int] = {}
        for e in effects:
            k = str(e.get(key))
            bucket[k] = bucket.get(k, 0) + 1
        out[key] = dict(sorted(bucket.items()))
    out["executable"] = sum(1 for e in effects if e.get("status") == "可执行")
    return out


# ---------------------------------------------------------------- check(门禁)

def check(catalog: dict | None = None) -> tuple[bool, list[str]]:
    """目录契约自洽门禁(分册02 §8.1 门禁 1/2/4/5/9;三向 tier↔status↔fxId)。"""
    catalog = catalog if catalog is not None else load_catalog()
    if catalog is None:
        return False, ["catalog.json 不存在或不可读(先跑 rs_effects.py normalize)"]
    fx = rs_fx
    errs: list[str] = []
    es = entries_of(catalog)
    ids = [e.get("id") for e in es]
    if len(ids) != len(set(ids)):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        errs.append(f"id 重复:{dup[:5]}")
    alias_owner: dict[str, str] = {}
    for e in es:
        eid, status, tier = e.get("id"), e.get("status"), e.get("tier")
        where = f"[{eid}]"
        if status == "可执行":
            if not e.get("fxId"):
                errs.append(f"{where} status=可执行 但 fxId 为空(门禁红,分册02 §2.2)")
            elif e.get("fxId") not in fx.registered_ids():
                errs.append(f"{where} fxId {e['fxId']} 不在渲染端注册表(虚报能力)")
            if e.get("fxId") != eid:
                errs.append(f"{where} 可执行条目 id 必须等于 fxId")
        if status == "不实现" and not str(e.get("why") or "").strip():
            errs.append(f"{where} status=不实现 但 why 为空")
        if tier == "T2" and status == "可执行" and not str(e.get("license") or "").strip():
            errs.append(f"{where} tier=T2 可执行 但 license 为空(许可合规)")
        for a in e.get("aliases") or []:
            if a in alias_owner and alias_owner[a] != eid:
                errs.append(f"别名 {a!r} 同时挂在 {alias_owner[a]} 与 {eid}(search 无法唯一)")
            alias_owner[a] = eid
        if status == "不实现" and "用户确认删除" in str(e.get("why") or "") \
                and not e.get("originName"):
            errs.append(f"{where} 删除项缺 originName(sources 可查性)")
    # 注册表无孤儿:注册表每个键都要在目录里有一条同 id 条目(可执行或待实现)
    catalog_ids = set(ids)
    for fxid in sorted(fx.registered_ids() - catalog_ids):
        errs.append(f"注册表 fxId {fxid} 在目录无条目(孤儿;normalize 重建)")
    return not errs, errs


# ---------------------------------------------------------------- 查询命令

def cmd_list(a) -> int:
    es = entries_of(load_catalog())
    if a.category:
        want = {c.strip() for c in a.category.split(",") if c.strip()}
        es = [e for e in es if e.get("category") in want]
    for attr in ("subclass", "tier", "status", "commonness"):
        v = getattr(a, attr, None)
        if v:
            es = [e for e in es if e.get(attr) == v]
    if a.source:
        want = {s.strip() for s in a.source.split(",") if s.strip()}
        es = [e for e in es if e.get("source") in want]
    if a.json:
        return emit(True, "EFFECTS_LIST", f"共 {len(es)} 条", {"effects": es})
    print(f"{'id':<28}{'类别':<6}{'级':<5}{'状态':<8}{'常用':<5}label")
    for e in es:
        print(f"{e['id']:<28}{e.get('category', ''):<6}{e.get('tier', ''):<5}"
              f"{e.get('status', ''):<8}{e.get('commonness', ''):<5}{e.get('label', '')}")
    return emit(True, "EFFECTS_LIST", f"共 {len(es)} 条", {"count": len(es)})


def cmd_show(a) -> int:
    if not str(getattr(a, "id", "")).strip():
        return emit(False, "BAD_ARGS", "用法:rs_effects.py show <id>", exit_code=2)
    es = entries_of(load_catalog())
    e = next((x for x in es if x.get("id") == a.id), None)
    if e is None:
        return emit(False, "EFFECT_NOT_FOUND", f"目录无此 id:{a.id}", exit_code=2)
    print(json.dumps(e, ensure_ascii=False, indent=1))
    return emit(True, "EFFECT_SHOWN", f"{e['id']} — {e.get('label', '')}", {"effect": e})


def cmd_search(a) -> int:
    q = str(getattr(a, "query", "")).strip()
    if not q:
        return emit(False, "BAD_ARGS", "用法:rs_effects.py search <query>", exit_code=2)
    es = entries_of(load_catalog())
    hits = [e for e in es
            if q == e.get("id") or q == e.get("label")
            or q in (e.get("aliases") or [])
            or (len(q) >= 2 and (q in str(e.get("label") or "")
                                 or any(q in al for al in (e.get("aliases") or []))))]
    hits = hits[:max(int(a.limit), 1)]
    if not hits:
        return emit(False, "EFFECT_NOT_FOUND",
                    f"未找到:{q}(剪映名/中文名/fxId 均无命中)", exit_code=2)
    removed = [e for e in hits if e.get("status") == "不实现"
               and "用户确认删除" in str(e.get("why") or "")]
    if removed and all(e in removed for e in hits):
        return emit(False, "EFFECT_REMOVED",
                    f"「{q}」已确认删除(语义不明,用户 2026-09-26 决策):"
                    + ";".join(str(e.get("why")) for e in removed),
                    {"query": q, "verdict": "EFFECT_REMOVED",
                     "effects": removed}, exit_code=2)
    code = "EFFECT_FOUND" if any(e.get("status") == "可执行" for e in hits) \
        else "EFFECT_NOT_AVAILABLE"
    return emit(code == "EFFECT_FOUND", code,
                f"「{q}」命中 {len(hits)} 条(可执行 "
                f"{sum(1 for e in hits if e.get('status') == '可执行')})",
                {"query": q, "verdict": code, "effects": hits},
                exit_code=0 if code == "EFFECT_FOUND" else 2)


def cmd_normalize(a) -> int:
    src = Path(a.source) if a.source else DEFAULT_SOURCE
    if not src.is_file():
        return emit(False, "SOURCE_MISSING", f"原始清单不存在:{src}", exit_code=2)
    catalog, report = normalize(src)
    if CATALOG_PATH.is_file():
        try:
            if CATALOG_PATH.read_text(encoding="utf-8") == _dump(catalog):
                return emit(True, "EFFECTS_NO_CHANGE",
                            "normalize 幂等:catalog.json 无变更",
                            {"report": report})
        except OSError:
            pass
    CATALOG_PATH.write_text(_dump(catalog), encoding="utf-8")
    if a.report:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    ok, errs = check(catalog)
    return emit(ok, "EFFECTS_NORMALIZED",
                f"catalog.json 重生成:{report['total']} 条"
                + ("" if ok else f";check 未过:{errs[:3]}"),
                {"report": report, "errors": errs}, exit_code=0 if ok else 2)


def _dump(catalog: dict) -> str:
    return json.dumps(catalog, ensure_ascii=False, indent=1) + "\n"


def cmd_check(_a) -> int:
    ok, errs = check()
    return emit(ok, "EFFECTS_CHECK_OK" if ok else "EFFECTS_CHECK_FAIL",
                ("目录契约自洽" if ok else "目录契约违规:" + ";".join(errs[:5])),
                {"errors": errs}, exit_code=0 if ok else 2)


def cmd_coverage(a) -> int:
    es = entries_of(load_catalog())
    stats = _stats(es)
    nle_ok, nle_missing = _nle_coverage(es)
    stats["nle62"] = {"covered": nle_ok, "missing": nle_missing,
                      "note": "tr.* 的 §6 参数化家族按落地档计数(NLE62_FAMILY)"}
    if a.json:
        return emit(True, "EFFECTS_COVERAGE", "覆盖率报告", {"coverage": stats})
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return emit(True, "EFFECTS_COVERAGE", "覆盖率报告", {"coverage": stats})


def _nle_coverage(es: list[dict]) -> tuple[list[str], list[str]]:
    """§6 的 62 条覆盖对拍:直登 id 状态=可执行,或家族映射内全部落地档可执行。"""
    exec_ids = {e["id"] for e in es if e.get("status") == "可执行"}
    covered, missing = [], []
    for cid in sorted(NLE62_IDS):
        realized = NLE62_FAMILY.get(cid, [cid])
        # 同义落点:tr.speed.ramp → effect.speed.ramp(转场语义同登记)
        pool = set(realized)
        if cid == "tr.speed.ramp":
            pool.add("effect.speed.ramp")
        if cid in exec_ids or pool & exec_ids:
            covered.append(cid)
        else:
            missing.append(cid)
    return covered, missing


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(prog="rs_effects.py",
                                 description="效果目录 CLI(M13/ADR-0054):查询/校验/规范化/覆盖率")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="列效果(可按类别/子类/级/状态/常用度/来源过滤)")
    p_list.add_argument("--category", default="", help="in,out,combo,transition 逗号分隔")
    p_list.add_argument("--subclass", default="", help="子类精确匹配")
    p_list.add_argument("--tier", default="", help="T1/T2/T3")
    p_list.add_argument("--status", default="", help="可执行/登记待实现/不实现")
    p_list.add_argument("--commonness", default="", help="高/中/低")
    p_list.add_argument("--source", default="", help="用户清单/补充清单/开源收录 逗号分隔")
    p_list.add_argument("--json", action="store_true")
    p_show = sub.add_parser("show", help="看单条详情")
    p_show.add_argument("id", nargs="?", default="",
                        help="效果 id(缺省打印用法提示)")
    p_search = sub.add_parser("search", help="按剪映名或中文名反查(支持别名;删除项返回 EFFECT_REMOVED)")
    p_search.add_argument("query", nargs="?", default="",
                          help="剪映名/中文名/fxId(缺省打印用法提示)")
    p_search.add_argument("--limit", type=int, default=5)
    p_norm = sub.add_parser("normalize", help="规范化器:原始清单 → catalog.json(幂等)")
    p_norm.add_argument("--source", default="", help="原始清单路径(缺省 sources/ 内置)")
    p_norm.add_argument("--report", action="store_true", help="打印规范化报告")
    p_check = sub.add_parser("check", help="契约校验(门禁入口)")
    p_cov = sub.add_parser("coverage", help="覆盖率报告(tier/status/subclass/source)")
    p_cov.add_argument("--json", action="store_true")
    a = ap.parse_args()
    return {"list": cmd_list, "show": cmd_show, "search": cmd_search,
            "normalize": cmd_normalize, "check": cmd_check,
            "coverage": cmd_coverage}[a.cmd](a)


if __name__ == "__main__":
    from rs_common import ensure_utf8
    ensure_utf8()
    sys.exit(main())
