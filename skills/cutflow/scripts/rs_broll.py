"""B-roll 素材语义检索(M8 能力 text.broll 的 detector,ADR-0047)。

用法:
  rs_broll.py match [工程根] [--query "<语义>"] [--top 8]
  rs_broll.py                  # 无参 = 工程模式(cwd=工程根;query 缺省取 brief 标题+术语)

双档三态(懒加载体系 rs_fetchable,ADR-0049;本脚本只探测、绝不下载):
  READY    clip(CLIP 类图文模型)probe 通过 → 子进程图文向量检索(实现调用,测试 mock)
  MISSING  降级档 keyword-match:对素材 manifest 的文件名/既有 OCR 文本做关键词命中
           (CJK 二字 token + 西文词;文件名命中加权;纯 Python 确定性计分)

产物 04_粗剪决策/broll_matches.json:
  {"query", "matches": [{file, score, matched, source}], "engine", "degraded",
   "degradeReason": "keyword-match", "missingComponent": "clip", "scanned"}
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import EXIT_OK, emit, write_text_atomic  # noqa: E402
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量
import rs_fetchable  # noqa: E402  — 三态探测(只 state(),绝不触发下载)

# ---------------------------------------------------------------- 参数常量(描述符 params 同源,改一处必改两处)

TOP_K = 8                   # 最多返回的匹配条数(第二波 Agent 的选卡池深度)
FILENAME_WEIGHT = 2         # 文件名命中权重(名字是作者自己的语义标注,强于 OCR 文本)
MIN_TOKEN_LEN = 2           # 单 token 最短长度(西文词;CJK 天然按二字切)
CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
LATIN_RE = re.compile(r"[A-Za-z0-9]+")
# manifest 条目里可作语义依据的文本字段(逐条尝试;缺字段即跳过,不臆测)
HAY_FIELDS = ("ocr", "content", "text", "note", "desc", "title", "keywords")


def broll_path(project: Path) -> Path:
    """04_粗剪决策/broll_matches.json(单一落点)。"""
    return rs_paths.resolve(project, "cut") / "broll_matches.json"


# ---------------------------------------------------------------- 降级档 keyword-match

def tokenize(query: str) -> list[str]:
    """query → token 列表:CJK 按二字滑窗 + 西文小写词(确定性,无停用词表依赖)。"""
    tokens: list[str] = []
    for cjk in CJK_RE.findall(query or ""):
        if len(cjk) == 1:
            tokens.append(cjk)
            continue
        tokens.extend(cjk[i:i + 2] for i in range(len(cjk) - 1))
    for w in LATIN_RE.findall(query or ""):
        w = w.lower()
        if len(w) >= MIN_TOKEN_LEN:
            tokens.append(w)
    seen: set[str] = set()
    return [t for t in tokens if not (t in seen or seen.add(t))]


def item_hay(item: dict) -> tuple[str, str]:
    """manifest 条目 → (文件名干, 文本字段拼接)。"""
    name = Path(str(item.get("file", ""))).stem
    texts: list[str] = []
    for f in HAY_FIELDS:
        v = item.get(f)
        if isinstance(v, str) and v.strip():
            texts.append(v)
        elif isinstance(v, list):
            texts.extend(str(x) for x in v if str(x).strip())
    return name, " ".join(texts)


def keyword_rank(query: str, items: list[dict]) -> list[dict]:
    """关键词命中计分:token 命中文本 +1、命中文件名 +FILENAME_WEIGHT。

    排序:score 降序 → file 升序(全序确定,字节级可复现)。零命中不进清单。
    """
    tokens = tokenize(query)
    if not tokens:
        return []
    out: list[dict] = []
    for it in items:
        name, hay = item_hay(it)
        name_l, hay_l = name.lower(), hay.lower()
        score, matched = 0, []
        for t in tokens:
            in_name = t in name_l
            in_hay = t in hay_l
            if in_name:
                score += FILENAME_WEIGHT
            if in_hay:
                score += 1
            if in_name or in_hay:
                matched.append(t)
        if score > 0:
            out.append({"file": str(it.get("file", "")), "score": score,
                        "matched": matched, "source": "keyword-match"})
    out.sort(key=lambda m: (-m["score"], m["file"]))
    return out[:TOP_K]


# ---------------------------------------------------------------- READY 档(CLIP 图文检索;测试 mock 本函数)

def _clip_rank(query: str, items: list[dict], out_json: Path) -> subprocess.CompletedProcess:
    """子进程调 clip 模型做图文向量检索,结果落 out_json(实现调用;测试 mock)。"""
    code = ("import json,sys;"
            "from cutflow_clip_driver import rank;"   # venv 内驱动约定(tools/deps 部署)
            f"rank({query!r},{json.dumps(items, ensure_ascii=False)},{str(out_json)!r})")
    import os  # noqa: PLC0415
    env = {**os.environ, "PYTHONPATH": str(rs_fetchable.tools_dir())}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900, env=env)


def ready_tier(query: str, items: list[dict], out_root: Path) -> tuple[list[dict] | None, dict]:
    """probe text.clip → READY 则子进程检索。返回 (matches 或 None, tiers 留痕)。"""
    st = rs_fetchable.state("text.clip")
    if st["state"] != "READY":
        return None, {"broll": {"engine": "keyword-match", "degraded": True,
                                **{k: v for k, v in rs_fetchable.degrade_record("text.clip").items()
                                   if k != "degraded"}}}
    tmp_json = broll_path(out_root).with_suffix(".infer.json")
    try:
        p = _clip_rank(query, items, tmp_json)
    except (OSError, subprocess.TimeoutExpired):
        return None, {"broll": {"engine": "clip", "degraded": True,
                                "note": "clip 子进程失败,退回 keyword-match"}}
    if p.returncode != 0 or not tmp_json.is_file():
        return None, {"broll": {"engine": "clip", "degraded": True,
                                "note": "clip 输出缺失,退回 keyword-match"}}
    try:
        matches = json.loads(tmp_json.read_text(encoding="utf-8"))
        assert isinstance(matches, list)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, AssertionError):
        return None, {"broll": {"engine": "clip", "degraded": True,
                                "note": "clip 输出解析失败,退回 keyword-match"}}
    return matches, {"broll": {"engine": "clip", "degraded": False}}


# ---------------------------------------------------------------- 组装与产物

def match_project(root: Path, query: str | None = None, top: int = TOP_K) -> tuple[dict, int]:
    """工程全流程 → broll_matches.json。query 缺省取 intent resolved 的标题+术语。"""
    out_path = broll_path(root)
    items: list[dict] = []
    man = rs_paths.manifest_json(root)
    if man.is_file():
        try:
            items = json.loads(man.read_text(encoding="utf-8")).get("items") or []
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            items = []
    eff_query = query
    if not eff_query:
        resolved: dict = {}
        p = rs_paths.resolve(root, "brief") / "intent_decisions.json"
        if p.is_file():
            try:
                resolved = json.loads(p.read_text(encoding="utf-8")).get("resolved") or {}
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                resolved = {}
        parts = [str(resolved.get("title") or "")] + [str(t) for t in resolved.get("terms") or []]
        eff_query = " ".join(x for x in parts if x.strip())
    matches, tiers = ready_tier(eff_query, items, root)
    engine = "clip"
    doc: dict
    if matches is not None:
        doc = {"query": eff_query, "matches": matches[:top], "engine": engine,
               "scanned": len(items), "tiers": tiers, "degraded": False}
    else:
        matches = keyword_rank(eff_query or "", items)[:top]
        doc = {"query": eff_query, "matches": matches, "engine": "keyword-match",
               "scanned": len(items), "tiers": tiers}
        doc.update(rs_fetchable.degrade_record("text.clip"))
    if not (eff_query or "").strip():
        doc["note"] = "无 query(manifest/brief 均未提供语义)→ 空匹配,不猜"
    _write(out_path, doc)
    return doc, EXIT_OK


def _write(out_path: Path, doc: dict) -> None:
    write_text_atomic(out_path, json.dumps(doc, ensure_ascii=False, indent=1))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rs_broll.py",
        description="B-roll 素材语义检索(M8):READY=clip 图文模型;降级=keyword-match 关键词命中,必留痕")
    ap.add_argument("command", nargs="?", choices=["match"], default=None,
                    help="match=按语义检索素材;缺省 = 工程模式(cwd=工程根)")
    ap.add_argument("project", nargs="?", default=None, help="工程根(缺省 cwd)")
    ap.add_argument("--query", default=None, help="语义查询(brief 的 OCR/文件名关键词命中;缺省取标题+术语)")
    ap.add_argument("--top", type=int, default=TOP_K, help=f"返回条数上限(缺省 {TOP_K})")
    a = ap.parse_args(argv)

    root = Path(a.project) if a.project else Path.cwd()
    doc, rc = match_project(root, query=a.query, top=max(1, a.top))
    return emit(rc == EXIT_OK, "BROLL_OK",
                f"matches={len(doc.get('matches') or [])}/{doc.get('scanned')} "
                f"engine={doc.get('engine')} degraded={doc.get('degraded', False)}",
                doc, exit_code=rc)


if __name__ == "__main__":
    sys.exit(main())
