"""S0 素材摄取 + 交付清单(OPTIMIZATION-v7 #9)+ 绿幕门禁(ADR-0031)。

用法:
  rs_ingest.py scan <工程根> [--slug X] [--ratio 9x16]   # 01_materials → manifest + IR 骨架
  rs_ingest.py deliverables <工程根>                      # 汇总 → 06_output/deliverables.md
  rs_ingest.py green-ok <工程根> --reason "误判说明"       # 绿幕误判放行(写 override 留痕)

设计:机械动作全脚本化,Agent 只补"内容摘要"。
  · probe 失败**不静默**:写进 manifest 的 `probe: failed/unavailable` 并在报告里点名;
  · v0.14 起 CutFlow 不做抠像:scan 会对每条视频跑幕布检测(rs_greenscreen),命中且未放行
    时**阻断** S0(GREEN_SCREEN_INPUT),提示用户先自行抠像+合成背景再重跑;
  · 不覆盖已有 `05_ir/project.json`(只生成 `project.skeleton.json`);
  · 输出统一 `{ok, code, message, data}`,退出码 0/2/3/4。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rs_common  # noqa: E402
import rs_greenscreen  # noqa: E402
from rs_common import RATIOS, canvas_for, emit  # noqa: E402

MANIFEST_JSON = "manifest.json"
MANIFEST_MD = "MANIFEST.md"
SKELETON = "project.skeleton.json"
SKIP_NAMES = {MANIFEST_JSON, MANIFEST_MD, "GREENSCREEN.md"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv",
              ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".mxf"}


# ---------------------------------------------------------------- scan

def probe_media(path: Path) -> dict:
    """ffprobe 一条素材。失败/不可用都返回结构化结果(留痕,不抛、不静默)。

    刻意**不用 `rs_common.ffprobe_json`** —— 它在失败时 `die()` 会 sys.exit 并把错误 JSON 打到
    stdout,既污染 `--json` 契约,又让调用方无法逐条容错。
    """
    if not rs_common.CONFIG_PATH.is_file():
        return {"probe": "unavailable",
                "error": "缺 config.json(复制 config.example.json 并填 ffmpeg_dir)"}
    try:
        p = rs_common.run([rs_common.ffprobe_bin(), "-v", "error", "-show_format",
                           "-show_streams", "-of", "json", str(path)])
    except Exception as exc:  # noqa: BLE001 — ffprobe 缺失 → 留痕
        return {"probe": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    if p.returncode != 0:
        return {"probe": "failed", "error": (p.stderr or "ffprobe 非零退出")[:200]}
    try:
        info = json.loads(p.stdout or "{}")
    except json.JSONDecodeError as exc:
        return {"probe": "failed", "error": f"ffprobe 输出无法解析:{exc}"}
    fmt, streams = info.get("format") or {}, info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    def _fps(raw: str | None) -> float | None:
        try:
            num, _, den = (raw or "").partition("/")
            return round(float(num) / float(den or 1), 3) if num else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    return {"probe": "ok",
            "durationMs": int(round(float(fmt.get("duration") or 0) * 1000)),
            "width": v.get("width"), "height": v.get("height"),
            "fps": _fps(v.get("avg_frame_rate") or v.get("r_frame_rate")),
            "hasAudio": a is not None, "codec": v.get("codec_name")}


def scan(root: Path, slug: str = "project", ratio: str = "9x16") -> dict:
    mat = root / "01_materials"
    if not mat.is_dir():
        return {"ok": False, "code": "NO_MATERIALS", "message": f"缺 01_materials:{mat}"}
    files = sorted(p for p in mat.iterdir() if p.is_file() and p.name not in SKIP_NAMES)
    items = []
    for p in files:
        item = {"file": p.name, "sizeBytes": p.stat().st_size}
        item.update(probe_media(p))
        if item.get("probe") == "ok" and p.suffix.lower() in VIDEO_EXTS:
            item["greenScreen"] = rs_greenscreen.detect_media(p)
        items.append(item)

    # v0.14(ADR-0031):绿幕门禁 —— 命中且未放行则阻断。放行说明写在
    # 00_brief/greenscreen-override.txt(或 brief.md 的「绿幕检测:误判…」行)。
    override = rs_greenscreen.read_override(root)
    flagged = [i for i in items if (i.get("greenScreen") or {}).get("detected")]
    if override:
        for i in flagged:
            i["greenScreen"]["overridden"] = True

    doc = {"version": 1, "materials": str(mat.name), "items": items,
           "greenOverride": override}
    mat.joinpath(MANIFEST_JSON).write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    mat.joinpath(MANIFEST_MD).write_text(_manifest_md(items, override), encoding="utf-8")

    ir_dir = root / "05_ir"
    ir_dir.mkdir(parents=True, exist_ok=True)
    skeleton = _skeleton(items, slug, ratio)
    wrote_skeleton = False
    if not (ir_dir / "project.json").is_file():
        ir_dir.joinpath(SKELETON).write_text(json.dumps(skeleton, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
        wrote_skeleton = True
    failed = [i["file"] for i in items if i.get("probe") != "ok"]
    if flagged and not override:
        gpath = rs_greenscreen.guidance_md(root, flagged)
        names = ",".join(i["file"] for i in flagged[:3])
        return {"ok": False, "code": "GREEN_SCREEN_INPUT",
                "message": f"检测到 {len(flagged)} 条素材仍含绿幕/蓝幕({names});"
                           "CutFlow v0.14 起不再抠像 —— 请先自行抠像并合成背景,替换原素材后重跑 "
                           "scan;若为误判,运行 `rs_ingest.py green-ok <工程根> --reason \"…\"`。"
                           f"处理指引见 {gpath.relative_to(root).as_posix()}",
                "items": items, "flagged": [i["file"] for i in flagged],
                "guidance": str(gpath), "failed": failed,
                "manifest": str(mat / MANIFEST_JSON), "report": str(mat / MANIFEST_MD)}
    return {"ok": True, "code": "INGEST_OK", "items": items, "failed": failed,
            "flagged": [i["file"] for i in flagged],
            "skeletonWritten": wrote_skeleton,
            "manifest": str(mat / MANIFEST_JSON), "report": str(mat / MANIFEST_MD)}


def _manifest_md(items: list[dict], green_override: str | None = None) -> str:
    lines = ["# 素材清单(rs_ingest 生成)", "",
             "| 文件 | 大小 | 时长 | 分辨率 | 帧率 | 音轨 | 幕布 | probe | 内容摘要(待 Agent 补) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for i in items:
        res = f"{i['width']}x{i['height']}" if i.get("width") else "—"
        dur = f"{i['durationMs'] / 1000:.1f}s" if i.get("durationMs") else "—"
        gs = i.get("greenScreen") or {}
        if gs.get("detected"):
            kind = {"green": "绿幕", "blue": "蓝幕"}.get(gs.get("kind"), "幕布")
            screen = f"⚠ {kind}({gs.get('confidence', 0):.0%})"
            if gs.get("overridden"):
                screen += " 已放行"
        else:
            screen = "—"
        lines.append(f"| {i['file']} | {i['sizeBytes'] / 1e6:.1f}MB | {dur} | {res} | "
                     f"{i.get('fps') or '—'} | {'有' if i.get('hasAudio') else '无'} | "
                     f"{screen} | {i.get('probe')} |  |")
    bad = [i for i in items if i.get("probe") != "ok"]
    if bad:
        lines += ["", "## ⚠ probe 未通过(不阻塞,但需人工确认)", ""]
        lines += [f"- {i['file']}: {i.get('error', i.get('probe'))}" for i in bad]
    flagged = [i for i in items if (i.get("greenScreen") or {}).get("detected")]
    if flagged:
        lines += ["", "## 幕布检测(ADR-0031)", ""]
        if green_override:
            lines += [f"- 检测到 {len(flagged)} 条幕布素材,已按用户说明**放行**:{green_override}"]
        else:
            lines += [f"- ⛔ 检测到 {len(flagged)} 条幕布素材,**未放行** —— 请先自行抠像+合成背景"
                      "(见 `01_materials/GREENSCREEN.md`)或运行 `rs_ingest.py green-ok`。"]
    lines += ["", "> 类型/videoType、比例、风格、声音方案请在 `00_brief/brief.md` 声明。", ""]
    return "\n".join(lines)


def _skeleton(items: list[dict], slug: str, ratio: str) -> dict:
    w, h = canvas_for(ratio)
    clips, cursor = [], 0
    for i in items:
        dur = int(i.get("durationMs") or 0)
        if dur <= 0:
            continue
        clips.append({"src": f"01_materials/{i['file']}", "startMs": cursor, "durationMs": dur,
                      "sourceInMs": 0})
        cursor += dur
    return {"version": 1, "slug": slug, "fps": 30, "canvas": {"width": w, "height": h},
            "tracks": [{"kind": "video", "name": "main", "clips": clips}],
            "outputs": [ratio], "_skeleton": True,
            "_note": "rs_ingest 生成的原样拼接骨架;请按 brief 用 rs_ir/artboard 补齐卡片/音效(素材背景由用户预处理)"}


# ---------------------------------------------------------------- deliverables

def _first_line(path: Path, prefix: str = "") -> str:
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s and s.startswith(prefix):
            return s
    return ""


def build_deliverables(root: Path) -> dict:
    """把"跑了什么、过了没、产物在哪"汇总成一页交付清单。"""
    from rs_common import ratio_for_canvas
    out = root / "06_output"
    out.mkdir(parents=True, exist_ok=True)
    videos = sorted(p.name for p in out.glob("*.mp4"))
    canvas = {}
    pj = root / "05_ir" / "project.json"
    if pj.is_file():
        try:
            canvas = (json.loads(pj.read_text(encoding="utf-8")) or {}).get("canvas") or {}
        except json.JSONDecodeError:
            canvas = {}
    ratio = ""
    if canvas:
        try:
            ratio = ratio_for_canvas(canvas.get("width", 0), canvas.get("height", 0))
        except ValueError:
            ratio = f"{canvas.get('width')}x{canvas.get('height')}"
    verify = {}
    vp = root / "_state" / "verify.json"
    if vp.is_file():
        try:
            verify = json.loads(vp.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            verify = {}
    brief_title = _first_line(root / "00_brief" / "brief.md", "#")
    sync_line = _first_line(out / "sync_report.md", "**总判定**")
    cut_line = _first_line(root / "04_cut" / "cut_report.md", "- 源时长")
    lines = [
        f"# 交付清单 — {brief_title.lstrip('# ').strip() or root.name}", "",
        f"- 画幅:`{ratio or '未定'}`", f"- 成片:{len(videos)} 个",
        f"- 验证等级:`{verify.get('lastL0', {}).get('level', '—') if isinstance(verify.get('lastL0'), dict) else '—'}`"
        f" ｜ 首次全检:`{'已完成' if (verify.get('firstCheck') or {}).get('done') else '未完成'}`",
        f"- 内容指纹:`{(verify.get('lastL0') or {}).get('at', '—') if isinstance(verify.get('lastL0'), dict) else '—'}`",
        "", "## 成片", ""]
    lines += ([f"- `06_output/{v}`" for v in videos] or ["- (尚无成片)"])
    lines += ["", "## 校验摘要", ""]
    lines.append(f"- 粗剪:{cut_line or '未做'}")
    lines.append(f"- 对齐:{sync_line or '未做 rs_sync'}")
    lines.append(f"- 字幕:`06_output/subtitles.ass` "
                 f"{'存在' if (out / 'subtitles.ass').is_file() else '**缺失**'}")
    for extra in ("metadata.json", "cover.png", "subtitles.ass", "sync_report.md"):
        if not (out / extra).is_file():
            lines.append(f"- ⚠ 缺 `06_output/{extra}`")
    lines += ["", "> 本文件由 `rs_ingest.py deliverables` 生成;下游交付/复核以此为准。", ""]
    dst = out / "deliverables.md"
    dst.write_text("\n".join(lines), encoding="utf-8")
    return {"ok": True, "code": "DELIVERABLES_OK", "path": str(dst), "videos": videos,
            "ratio": ratio, "firstCheckDone": bool((verify.get("firstCheck") or {}).get("done"))}


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="scan",
                    choices=["scan", "deliverables", "green-ok"])
    ap.add_argument("root", help="工程根目录")
    ap.add_argument("--slug", default=None)
    ap.add_argument("--ratio", default="9x16", choices=list(RATIOS))
    ap.add_argument("--reason", default="", help="green-ok:用户对误判的说明(必填)")
    a = ap.parse_args()
    root = Path(a.root)
    if not root.is_dir():
        return emit(False, "NO_PROJECT", f"工程根不存在:{root}", exit_code=2)
    if a.command == "deliverables":
        res = build_deliverables(root)
        return emit(True, res["code"], f"交付清单 → {res['path']}",
                    {k: v for k, v in res.items() if k != "code"})
    if a.command == "green-ok":
        if not a.reason.strip():
            return emit(False, "NEED_REASON", "green-ok 必须提供 --reason(用户对误判的说明)",
                        exit_code=2)
        p = rs_greenscreen.write_override(root, a.reason.strip())
        return emit(True, "GREEN_OVERRIDE", f"已记录绿幕误判放行:{p}", {"path": str(p)})
    res = scan(root, a.slug or root.name, a.ratio)
    if not res.get("ok"):
        return emit(False, res["code"], res["message"],
                    {k: v for k, v in res.items()
                     if k not in ("ok", "code", "message")}, exit_code=2)
    msg = f"摄取 {len(res['items'])} 条素材"
    if res["failed"]:
        msg += f";⚠ {len(res['failed'])} 条 probe 未通过({','.join(res['failed'][:3])})"
    if res.get("flagged"):
        msg += f";幕布素材已放行({','.join(res['flagged'][:3])})"
    if not res["skeletonWritten"]:
        msg += ";已有 project.json,未覆盖骨架"
    return emit(True, res["code"], msg,
                {"manifest": res["manifest"], "report": res["report"],
                 "count": len(res["items"]), "failed": res["failed"],
                 "flagged": res.get("flagged", []),
                 "skeletonWritten": res["skeletonWritten"], "items": res["items"]})


if __name__ == "__main__":
    rs_common.ensure_utf8()
    sys.exit(main())
