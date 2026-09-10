"""S9 文案生成:标题 / 简介 / Tag(rules/meta.md)。

用法:
  rs_meta.py --wordline 05_ir/wordline.json --brief 00_brief/brief.md `
      --platform douyin,bili --out 06_output [--title "..." --desc "..." --tags a,b,c]

分工:**标题/简介由 Agent 撰写**(本项目不接外部 LLM API);本脚本负责
① 校验与截断 ② 从 markers/句子生成 B站章节时间戳 ③ 落盘 metadata.json + metadata.md。
未提供 --title 时输出占位标题并标 `needsAgent: true`(不猜内容)。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit  # noqa: E402

PLATFORM_SPECS = {
    "douyin": {"label": "抖音", "titleMax": 30, "descMax": 55, "tags": (5, 5), "chapters": False,
               "hookChars": 8},
    "bili": {"label": "B站", "titleMax": 80, "descMax": 2000, "tags": (10, 10), "chapters": True},
    "shipin": {"label": "视频号", "titleMax": 22, "descMax": 120, "tags": (3, 5), "chapters": False},
}
CHAPTER_MIN_S = 30
PUNCT = "。，、；：,;:…!?！？ \u3000"


def load_brief_text(path: Path | None) -> str:
    return path.read_text(encoding="utf-8") if path and path.is_file() else ""


def brief_field(text: str, keys: list[str]) -> str:
    for key in keys:
        m = re.search(rf"^\s*[-*]?\s*{key}\s*[:：]\s*(.+)$", text, re.M)
        if m:
            return m.group(1).strip()
    return ""


def auto_chapters(wl: dict, min_s: float = CHAPTER_MIN_S) -> list[dict]:
    """无 markers 时按句群自动分章(每章 ≥ min_s),并提示需人工核。"""
    chars = wl.get("chars", [])
    sents = wl.get("sentences") or []
    if not chars:
        return []
    chapters, acc_start, acc_text = [], 0, []
    for si, s in enumerate(sents):
        a, b = s.get("span", [0, 0])
        if b <= a:
            continue
        t0 = int(chars[min(a, len(chars) - 1)]["startMs"])
        if not acc_text:
            acc_start = t0
        acc_text.append(s.get("text", ""))
        is_last = si == len(sents) - 1
        t1 = int(chars[min(b, len(chars)) - 1]["endMs"])
        if is_last or (t1 - acc_start) >= min_s * 1000:
            label = "".join(acc_text)[:18] or f"第 {len(chapters) + 1} 节"
            chapters.append({"atMs": acc_start, "title": label})
            acc_text = []
    return chapters


def fmt_ts(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    return f"{total // 60:02d}:{total % 60:02d}"


def build_platform(key: str, spec: dict, wl: dict, *, title: str, desc: str,
                   tags: list[str], ir_markers: list[dict] | None = None) -> dict:
    warns: list[str] = []
    if len(title) > spec["titleMax"]:
        warns.append(f"{spec['label']}标题超限 {len(title)}>{spec['titleMax']},已截断")
        title = title[:spec["titleMax"]]
    if len(desc) > spec["descMax"]:
        warns.append(f"{spec['label']}简介超限 {len(desc)}>{spec['descMax']},已截断")
        desc = desc[:spec["descMax"]]
    lo, hi = spec["tags"]
    if len(tags) > hi:
        warns.append(f"{spec['label']}Tag 超限 {len(tags)}>{hi},已截断")
        tags = tags[:hi]
    if len(tags) < lo:
        warns.append(f"{spec['label']}Tag 不足 {len(tags)}<{lo}")
    out = {"title": title, "desc": desc,
           "tags": [f"#{t}" for t in tags] if key == "douyin" else tags}
    if spec["chapters"]:
        chs = ir_markers if ir_markers else auto_chapters(wl)
        out["chapters"] = [{"atMs": int(c["atMs"]), "title": c.get("title") or c.get("label", ""),
                            "ts": fmt_ts(int(c["atMs"]))} for c in chs]
        if not ir_markers:
            warns.append("章节为自动切分(无 markers),建议人工核")
    if warns:
        out["warnings"] = warns
    return out


def render_md(meta: dict, brief: dict) -> str:
    lines = ["# 平台文案", ""]
    if brief.get("needsAgent"):
        lines += ["> ⚠ 标题/简介为占位内容,需 Agent 撰写后重跑本脚本。", ""]
    for key, p in meta["platforms"].items():
        lines += [f"## {PLATFORM_SPECS[key]['label']}", "",
                  f"**标题**({len(p['title'])} 字)", "", p["title"], "",
                  f"**简介**({len(p['desc'])} 字)", "", p["desc"], "",
                  "**Tag**:" + " ".join(p["tags"]), ""]
        if p.get("chapters"):
            lines += ["**章节**", "", "```"]
            lines += [f"{c['ts']} {c['title']}" for c in p["chapters"]]
            lines += ["```", ""]
        for w in p.get("warnings", []):
            lines.append(f"> ⚠ {w}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wordline", required=True)
    ap.add_argument("--brief")
    ap.add_argument("--platform", default="douyin,bili")
    ap.add_argument("--title", default="")
    ap.add_argument("--desc", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--markers")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    wl = json.loads(Path(a.wordline).read_text(encoding="utf-8"))
    brief_text = load_brief_text(Path(a.brief) if a.brief else None)
    title = a.title.strip()
    needs_agent = False
    if not title:
        topic = brief_field(brief_text, ["标题", "主题", "项目", "类型"]) or Path(a.wordline).parent.parent.name
        title = f"{topic}(待补标题)"
        needs_agent = True
    desc = a.desc.strip() or "(待补简介)"
    if not a.desc.strip():
        needs_agent = True
    tags = [t.strip().lstrip("#") for t in a.tags.split(",") if t.strip()]
    markers = None
    if a.markers:
        m = json.loads(Path(a.markers).read_text(encoding="utf-8"))
        markers = m.get("markers") if isinstance(m, dict) else m

    keys = [k.strip() for k in a.platform.split(",") if k.strip()]
    unknown = [k for k in keys if k not in PLATFORM_SPECS]
    if unknown:
        return emit(False, "BAD_PLATFORM", f"未知平台 {unknown}(可选 {list(PLATFORM_SPECS)})", exit_code=2)

    meta = {"version": 1, "needsAgent": needs_agent, "platforms": {}}
    for k in keys:
        meta["platforms"][k] = build_platform(k, PLATFORM_SPECS[k], wl, title=title, desc=desc,
                                              tags=tags, ir_markers=markers)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "metadata.md").write_text(render_md(meta, meta), encoding="utf-8")

    warns = sum(len(p.get("warnings", [])) for p in meta["platforms"].values())
    msg = f"{len(keys)} 个平台文案已生成" + (f";{warns} 条警告" if warns else "")
    if needs_agent:
        msg += ";⚠ 标题/简介为占位,请 Agent 补写后重跑"
    return emit(True, "META_OK", msg, {"json": str(out / "metadata.json"),
                                       "md": str(out / "metadata.md"),
                                       "needsAgent": needs_agent, "platforms": list(keys)})


if __name__ == "__main__":
    sys.exit(main())
