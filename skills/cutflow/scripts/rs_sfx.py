"""S6 音效自动落点:把「手动挂音效」变成「Agent 出草案 + 人审」(rules/sfx.md)。

用法:
  rs_sfx.py 05_时间线工程/project.json --auto --out 05_时间线工程/sfx_draft.json
  rs_sfx.py 05_时间线工程/project.json --apply 05_时间线工程/sfx_draft.json [--write 05_时间线工程/project.sfx.json]
  rs_sfx.py … --auto --keywords 蓝屏,免费     # 关键词上屏 → ding(强调类,缺陷 C 修复)

落点规则(rules/sfx.md 落点语义表):转场=按转场类型/方向选(缺陷 B 修复,奇偶交替
废止);强调=关键词上屏 ding;列举=第N项 pop / 连接词 click;章节=markers riser;
结尾=outro_bell。密度硬约束:**每 15s ≤2 个** + **同一音效 10s 内不重复**;
混剪(videoType=mixcut)转场音效默认让位 BGM 卡点。

素材真相源(M12/ADR-0053):skills/cutflow/assets/manifest.json 按 usage 检索,
不再硬编码 7 个名字;索引缺失时旧 7 名字兜底表继续可用(WARN ASSET_MANIFEST_MISSING)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit, normalize_markers  # noqa: E402
import rs_asset  # noqa: E402  — 素材库统一索引(分册01 §6)
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量

DENSITY_WINDOW_MS = 15000
DENSITY_MAX = 2
REPEAT_WINDOW_MS = 10000        # M12 新增:同一音效 10s 内不得重复(防"哒哒哒")
DEFAULT_GAIN_DB = -14
ENUM_ORDINALS = ("第一", "第二", "第三", "第四", "第五", "第六")
ENUM_CONNECTIVES = ("首先", "其次", "最后", "再来", "另外")
ENDS = "。！？…"
# 旧 7 名字兜底表(ADR-0053 回滚承诺;manifest 缺失时按此解析,**不删**)
LEGACY_SFX = ("whoosh", "swipe", "pop", "click", "ding", "riser", "bell")
# 转场类型 → (偏好精确 id, 偏好组名):按转场方向选对应呼啸变体(缺陷 B 修复;
# 奇偶交替废止)。精确 id 缺席(旧索引)时回落组名。
TRANSITION_PREFER = {
    "fade": ("sfx.whoosh.01", "whoosh"),      # 溶解 → 呼啸
    "wipeleft": ("sfx.whoosh.04", "whoosh"),  # 左擦 → 横扫
    "slideleft": ("sfx.whoosh.04", "whoosh"), # 左滑 → 横扫
    "wipeup": ("sfx.whoosh.02", "whoosh"),    # 上擦 → 上行
    "circleopen": ("sfx.whoosh.03", "whoosh"),# 圆展开 → 下行落定
}


def _manifest_missing_note() -> None:
    if rs_asset.load_manifest() is None:
        print("[rs_sfx] WARN ASSET_MANIFEST_MISSING 素材索引缺失,按旧 7 名字兜底",
              file=sys.stderr)


def _src_of(asset: dict | None, legacy: str) -> str:
    """选中素材 → 伪协议引用。组首条(.01)用组名(旧工程/草案可读性),变体用全 id;
    兜底占位(.legacy)与素材缺失时回落旧名,老解析路径不变。"""
    if asset is None:
        return f"assets_sfx:{legacy}"
    aid = str(asset["id"])
    if aid.endswith(".legacy"):
        return f"assets_sfx:{legacy}"
    group = aid.split(".")[1] if len(aid.split(".")) > 2 else aid
    return f"assets_sfx:{group}" if aid.endswith(".01") else f"assets_sfx:{aid}"


def _pick(usage: str, prefer: str, avoid: set[str] | None = None,
          exact: str | None = None) -> dict | None:
    """按语义用途从 manifest 选音效(先精确 id,再组名,再该用途首条);
    索引缺席 → 旧名兜底(转 dict 形态,仅作 src 生成占位)。"""
    hit = rs_asset.find(exact, "sfx") if exact else None
    if hit is None:
        hit = rs_asset.pick_sfx(usage, prefer=prefer, avoid=avoid)
    if hit is not None:
        return hit
    if prefer in LEGACY_SFX:
        return {"id": f"sfx.{prefer}.legacy", "kind": "sfx"}    # 仅作 src 生成占位
    return None


def from_transitions(ir: dict) -> list[dict]:
    """转场落点(缺陷 B 修复):按 clip.transition 的类型/方向选音色,奇偶交替废止。

    · 显式硬切(cut/none 且无 topic 理由)→ 不加音效(把节奏让给画面);
    · jumpcut(同段跳切)→ 轻 swipe(弱标记);
    · topic/溶解/滑动/擦除 → 按 TRANSITION_PREFER 选组,swipe 之外的组都算转场系;
    · 旧 IR 无 transition 字段 → 按轻转场(swipe)处理,行为与 v1 相容但不奇偶交替。
    """
    out = []
    for t in ir.get("tracks", []):
        if t.get("kind") != "video":
            continue
        for i, c in enumerate(t.get("clips", [])):
            if i == 0:
                continue
            at = int(c.get("startMs", 0))
            tr = c.get("transition") or {}
            ttype = str(tr.get("type") or "")
            reason = str(tr.get("reason") or "")
            if ttype in ("cut", "none"):
                # 显式硬切:仅 topic 理由(真话题切换)保留轻标记,纯硬切不撒声
                if reason != "topic":
                    continue
                asset = _pick("transition", "swipe")
                out.append({"atMs": at, "src": _src_of(asset, "swipe"), "trigger": "cut",
                            "conf": 0.7, "gainDb": DEFAULT_GAIN_DB,
                            "note": f"话题硬切 @{t.get('name') or '主轨'}#{i}"})
                continue
            prefer_pair = TRANSITION_PREFER.get(ttype) or ("sfx.swipe.01", "swipe")
            prefer_exact, prefer_group = prefer_pair
            asset = _pick("transition", prefer_group, exact=prefer_exact)
            out.append({"atMs": at, "src": _src_of(asset, prefer_group), "trigger": "cut",
                        "conf": 0.9, "gainDb": DEFAULT_GAIN_DB,
                        "note": f"转场 {ttype or '软切'} @{t.get('name') or '主轨'}#{i}"})
    return out


def from_markers(ir: dict) -> list[dict]:
    # R01(v2 M11):schema 契约是 {ms,label};经 rs_common.normalize_markers 统一口,
    # 旧 atMs 兼容归一 —— 禁止直读字段(schema 口径 markers 曾被静默取 0)。
    out = []
    for m in normalize_markers(ir.get("markers")):
        asset = _pick("chapter", "riser")
        out.append({"atMs": m["ms"], "src": _src_of(asset, "riser"),
                    "trigger": "chapter", "conf": 0.85, "gainDb": DEFAULT_GAIN_DB,
                    "note": f"章节:{m['label']}"})
    return out


def from_enumeration(wordline: dict | None) -> list[dict]:
    """列举落点(缺陷 C 修复):第N项 → pop;首先/其次类连接词 → click。

    此前只会产 click,rules/sfx.md 承诺的「列举 → pop」从未兑现。
    """
    if not wordline:
        return []
    chars = wordline.get("chars", [])
    text = "".join(c["ch"] for c in chars)
    out = []
    for w in ENUM_ORDINALS:
        start = 0
        while True:
            k = text.find(w, start)
            if k < 0:
                break
            asset = _pick("enumeration", "pop")
            out.append({"atMs": int(chars[k]["startMs"]), "src": _src_of(asset, "pop"),
                        "trigger": "enumerate", "conf": 0.8, "gainDb": DEFAULT_GAIN_DB,
                        "anchorChar": k, "note": f"序号「{w}」"})
            start = k + 1
    for w in ENUM_CONNECTIVES:
        start = 0
        while True:
            k = text.find(w, start)
            if k < 0:
                break
            asset = _pick("enumeration", "click")
            out.append({"atMs": int(chars[k]["startMs"]), "src": _src_of(asset, "click"),
                        "trigger": "enumerate", "conf": 0.75, "gainDb": DEFAULT_GAIN_DB,
                        "anchorChar": k, "note": f"连接词「{w}」"})
            start = k + 1
    return out


def from_emphasis(wordline: dict | None, keywords: list[str]) -> list[dict]:
    """强调落点(缺陷 C 修复):关键词/金句上屏 → ding(rules/sfx.md §2 既有承诺)。

    关键词来源:CLI --keywords 或 IR `sfx.keywords`(工程级配置);都不给 → 不产。
    """
    if not wordline or not keywords:
        return []
    chars = wordline.get("chars", [])
    text = "".join(c["ch"] for c in chars)
    out = []
    for kw in keywords:
        if not kw:
            continue
        start = 0
        while True:
            k = text.find(kw, start)
            if k < 0:
                break
            asset = _pick("punchline", "ding")
            out.append({"atMs": int(chars[k]["startMs"]), "src": _src_of(asset, "ding"),
                        "trigger": "emphasize", "conf": 0.8, "gainDb": DEFAULT_GAIN_DB,
                        "anchorChar": k, "note": f"关键词「{kw}」上屏"})
            start = k + len(kw)
    return out


def from_ending(ir: dict, wordline: dict | None) -> list[dict]:
    total = 0
    for t in ir.get("tracks", []):
        if t.get("kind") == "video":
            for c in t.get("clips", []):
                total = max(total, int(c.get("startMs", 0)) + int(c.get("durationMs", 0)))
    if total:
        asset = _pick("ending", "outro_bell")
        legacy = "bell" if asset is None or str(asset.get("id", "")).endswith(".legacy") \
            else "outro_bell"
        return [{"atMs": max(0, total - 800), "src": _src_of(asset, "bell"),
                 "trigger": "ending", "conf": 0.8, "gainDb": DEFAULT_GAIN_DB,
                 "note": "片尾定格"}]
    return []


def anchor(wordline: dict | None, placement: dict) -> dict:
    """把落点吸附到最近的锚点字时间(rules/sfx.md §4:偏差 ≤1 帧)。"""
    if not wordline:
        return placement
    chars = wordline.get("chars", [])
    if not chars:
        return placement
    target = int(placement["atMs"])
    best, best_d = None, None
    for c in chars:
        d = abs(int(c["startMs"]) - target)
        if best_d is None or d < best_d:
            best, best_d = c, d
    if best is not None and best_d is not None and best_d <= 500:
        placement = dict(placement, atMs=int(best["startMs"]), anchorChar=best["i"])
    return placement


def enforce_density(placements: list[dict]) -> tuple[list[dict], list[dict]]:
    """每 15s 窗口内 ≤2 个;超出按 章节/转场 > 强调 > 列举 > 氛围 优先级保留。"""
    priority = {"chapter": 0, "cut": 1, "emphasize": 2, "enumerate": 3, "ending": 0,
                "ambient": 4}
    ordered = sorted(placements, key=lambda p: (p["atMs"], priority.get(p["trigger"], 3)))
    kept: list[dict] = []
    dropped: list[dict] = []
    for p in ordered:
        window = [k for k in kept if p["atMs"] - DENSITY_WINDOW_MS < k["atMs"] <= p["atMs"]]
        if len(window) >= DENSITY_MAX:
            dropped.append({**p, "reason": f"{DENSITY_WINDOW_MS // 1000}s 窗口内已 {DENSITY_MAX} 个"})
        else:
            kept.append(p)
    return kept, dropped


def enforce_repeat(placements: list[dict]) -> tuple[list[dict], list[dict]]:
    """M12 新增:同一音色 10s 内不重复(防"哒哒哒");后到者进 dropped。"""
    ordered = sorted(placements, key=lambda p: p["atMs"])
    kept: list[dict] = []
    dropped: list[dict] = []
    last_at: dict[str, int] = {}
    for p in ordered:
        prev = last_at.get(p["src"])
        if prev is not None and p["atMs"] - prev < REPEAT_WINDOW_MS:
            dropped.append({**p, "reason": f"同一音效 {REPEAT_WINDOW_MS // 1000}s 内已用过"})
            continue
        last_at[p["src"]] = p["atMs"]
        kept.append(p)
    return kept, dropped


def _yield_to_bgm(ir: dict, placements: list[dict]) -> tuple[list[dict], list[dict]]:
    """混剪类(videoType=mixcut)BGM 即时间轴骨架:转场音效默认让位(R42/分册01 §4.1)。"""
    vtype = str((ir.get("_meta") or {}).get("videoType")
                or ir.get("videoType") or "")
    if vtype != "mixcut":
        return placements, []
    kept = [p for p in placements if p.get("trigger") != "cut"]
    dropped = [{**p, "reason": "混剪类 BGM 卡点优先,转场音效让位(可人工捞回)"}
               for p in placements if p.get("trigger") == "cut"]
    return kept, dropped


def build_draft(ir: dict, wordline: dict | None, keywords: list[str] | None = None) -> dict:
    _manifest_missing_note()
    kws = list(keywords or []) + list((ir.get("sfx") or {}).get("keywords") or [])
    raw: list[dict] = []
    raw += from_markers(ir)
    raw += from_transitions(ir)
    raw += from_enumeration(wordline)
    raw += from_emphasis(wordline, kws)
    raw += from_ending(ir, wordline)
    raw = [anchor(wordline, p) for p in raw]
    kept, dropped = enforce_density(raw)
    kept, rep_dropped = enforce_repeat(kept)
    dropped = rep_dropped + dropped
    kept, bgm_dropped = _yield_to_bgm(ir, kept)
    dropped = bgm_dropped + dropped
    missing = sorted({p["src"].split(":", 1)[1] for p in kept
                      if rs_asset.resolve_sfx_ref(p["src"].split(":", 1)[1]) is None})
    return {"version": 2, "placements": kept, "dropped": dropped, "missingAssets": missing}


def apply_draft(ir: dict, draft: dict) -> dict:
    doc = json.loads(json.dumps(ir))
    audio = next((t for t in doc.get("tracks", []) if t.get("kind") == "audio"), None)
    if audio is None:
        audio = {"kind": "audio", "clips": []}
        doc.setdefault("tracks", []).append(audio)
    audio["clips"] = [c for c in audio.get("clips", []) if c.get("role") != "sfx"]
    for p in draft.get("placements", []):
        # IR 落盘字段白名单(schema 契约;实剪②反馈:note/gainDb/trigger/atMs 是
        # 内部 placement 结构,泄漏进 IR 被 cutforge v2 校验拒绝)。gainDb 换算
        # 为 schema 的 volume(线性比例)。
        gain = float(p.get("gainDb", DEFAULT_GAIN_DB))
        audio["clips"].append({"src": p["src"], "startMs": int(p["atMs"]),
                               "durationMs": int(p.get("durationMs", 1200)),
                               "role": "sfx",
                               "volume": round(10 ** (gain / 20.0), 3)})
    audio["clips"].sort(key=lambda c: int(c.get("startMs", 0)))
    return doc


def write_notes(draft: dict, outdir: Path) -> None:
    if not draft.get("dropped"):
        return
    lines = ["# 被丢弃的音效候选(密度/重复/BGM 让位约束)", "",
             f"规则:每 {DENSITY_WINDOW_MS // 1000}s ≤ {DENSITY_MAX} 个;"
             f"同一音效 {REPEAT_WINDOW_MS // 1000}s 内不重复;混剪类转场音效让位 BGM。", "",
             "| atMs | src | trigger | 原因 |", "|---|---|---|---|"]
    for d in draft["dropped"]:
        lines.append(f"| {d['atMs']} | {d['src']} | {d.get('trigger', '')} | {d['reason']} |")
    (outdir / "sfx_dropped.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ir")
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--apply")
    ap.add_argument("--write")
    ap.add_argument("--wordline")
    ap.add_argument("--keywords", default="",
                    help="强调关键词(逗号分隔):命中词上屏 → ding(缺陷 C 接线)")
    ap.add_argument("--out", default=rs_paths.p("timeline") + "/sfx_draft.json")
    a = ap.parse_args()
    ir = json.loads(Path(a.ir).read_text(encoding="utf-8"))

    if a.apply:
        draft = json.loads(Path(a.apply).read_text(encoding="utf-8"))
        doc = apply_draft(ir, draft)
        dst = Path(a.write or a.ir)
        dst.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        # R29(v2 M12):统计必须数「本次写入的 sfx 条数」,不再数 doc["tracks"][-1]
        # (apply_draft 复用既有 audio 轨时它未必是最后一轨,旧口径会虚报/报 0)。
        audio2 = next((t for t in doc.get("tracks", []) if t.get("kind") == "audio"),
                      {"clips": []})
        n = len([c for c in audio2.get("clips", []) if c.get("role") == "sfx"])
        return emit(True, "SFX_APPLIED", f"已写入 {n} 条音效 → {dst}",
                    {"path": str(dst), "count": n})

    wl = json.loads(Path(a.wordline).read_text(encoding="utf-8")) if a.wordline else None
    draft = build_draft(ir, wl, keywords=[k for k in a.keywords.split(",") if k.strip()])
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(draft, ensure_ascii=False, indent=1), encoding="utf-8")
    write_notes(draft, out.parent)
    msg = f"{len(draft['placements'])} 条音效落点(丢弃 {len(draft['dropped'])} 条)"
    if draft["missingAssets"]:
        msg += f";⚠ 缺失音色:{','.join(draft['missingAssets'])}"
    return emit(True, "SFX_DRAFT_OK", msg, {"path": str(out), **draft})


if __name__ == "__main__":
    sys.exit(main())
