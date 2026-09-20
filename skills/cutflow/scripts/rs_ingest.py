"""S0 素材摄取 + 交付清单(OPTIMIZATION-v7 #9)+ 绿幕门禁(ADR-0031)+ 决策说明书(N4)。

用法:
  rs_ingest.py scan <工程根> [--slug X] [--ratio 9x16]   # 01_materials → manifest + IR 骨架
  rs_ingest.py deliverables <工程根>                      # 汇总 → 06_output/deliverables.md
  rs_ingest.py green-ok <工程根> --reason "误判说明"       # 绿幕误判放行(写 override 留痕)
  rs_ingest.py decisions <工程根>                          # 生成 06_output/决策说明书.md(N4)

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
    """把"跑了什么、过了没、产物在哪"汇总成一页交付清单,**并做真对账**(P18-1)。

    对账项(缺任一即 `DELIVERABLES_INCOMPLETE`,退出码非零——S11「清单齐全」门禁从此真能拦人):
      · 成片 ≥1(06_output 顶层 / final/ / branded/);
      · `subtitles.ass` + `master.srt`(S7 产物);
      · `metadata.json` 且 `platforms` 条目非空(S10 产物);
      · `封面.png`(rs_common.COVER_PNG,P17-1 同一常量);
      · `sync_report.md`(S9 产物);
      · `variants.json` 存在时:矩阵每条 (ratio, logo) 都要有对应 `成片_<ratio>_<logo>_*.mp4`。
    清单文件 deliverables.md 照常生成(缺失项逐条点名),ok/exit 码如实反映对账结果。
    """
    from rs_common import COVER_PNG, ratio_for_canvas
    out = root / "06_output"
    out.mkdir(parents=True, exist_ok=True)
    # P10b-1:成片清单含独占子目录 final/、branded/(相对路径列出,交付位置可追)
    videos = sorted({p.relative_to(out).as_posix()
                     for pat in ("*.mp4", "final/*.mp4", "branded/*.mp4")
                     for p in out.glob(pat)})
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

    # ---- 真对账(P18-1):逐项核验,缺一项记一项
    missing: list[str] = []
    if not videos:
        missing.append("成片(06_output 顶层 / final/ / branded/ 至少 1 个 .mp4)")
    for name in ("subtitles.ass", "master.srt", "sync_report.md", COVER_PNG):
        if not (out / name).is_file():
            missing.append(f"06_output/{name}")
    meta: dict = {}
    meta_path = out / "metadata.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            missing.append("06_output/metadata.json(存在但无法解析)")
    if not (isinstance(meta.get("platforms"), dict) and meta.get("platforms")):
        missing.append("06_output/metadata.json 平台条目(platforms 缺失或为空;由 rs_meta 生成)")
    # 变体对账:variants.json 声明的矩阵 ↔ 盘面成片(账实相符)
    variant_notes: list[str] = []
    vj = root / "05_ir" / "variants.json"
    if vj.is_file():
        matrix: list = []
        try:
            doc = json.loads(vj.read_text(encoding="utf-8"))
            matrix = (doc or {}).get("matrix") or []
        except json.JSONDecodeError:
            variant_notes.append("05_ir/variants.json 无法解析,按 0 条变体对账")
        final_names = [Path(v).name for v in videos]
        for v in matrix:
            want = f"成片_{v.get('ratio')}_{v.get('logo')}_"
            if not any(n.startswith(want) and n.endswith(".mp4") for n in final_names):
                missing.append(f"变体 {v.get('id')} 缺成片({want}<profile>.mp4)")
        if not matrix and not variant_notes:
            variant_notes.append("05_ir/variants.json 无 matrix 条目(未启用品牌变体)")

    lines = [
        f"# 交付清单 — {brief_title.lstrip('# ').strip() or root.name}", "",
        f"- 画幅:`{ratio or '未定'}`", f"- 成片:{len(videos)} 个",
        f"- 验证等级:`{verify.get('lastL0', {}).get('level', '—') if isinstance(verify.get('lastL0'), dict) else '—'}`"
        f" ｜ 首次全检:`{'已完成' if (verify.get('firstCheck') or {}).get('done') else '未完成'}`",
        f"- 内容指纹:`{(verify.get('lastL0') or {}).get('at', '—') if isinstance(verify.get('lastL0'), dict) else '—'}`",
        "", "## 成片", ""]
    lines += ([f"- `06_output/{v}`" for v in videos] or ["- (尚无成片)"])
    lines += ["", "## 对账(P18-1:缺一项即 DELIVERABLES_INCOMPLETE)", ""]
    for name in ("subtitles.ass", "master.srt", COVER_PNG, "metadata.json",
                 "sync_report.md"):
        mark = "✓" if not any(name in m for m in missing) else "**缺失**"
        lines.append(f"- {mark} `06_output/{name}`")
    if not videos:
        lines.append("- **缺失** 成片(至少 1 个)")
    lines.append(f"- {'✓' if not any('变体' in m for m in missing) else '**账实不符**'} "
                 f"变体成片 ↔ `05_ir/variants.json` 矩阵")
    for note in variant_notes:
        lines.append(f"  - ℹ {note}")
    lines += ["", "## 校验摘要", ""]
    lines.append(f"- 粗剪:{cut_line or '未做'}")
    lines.append(f"- 对齐:{sync_line or '未做 rs_sync'}")
    lines.append(f"- 字幕:`06_output/subtitles.ass` "
                 f"{'存在' if (out / 'subtitles.ass').is_file() else '**缺失**'}")
    if missing:
        lines += ["", "## 缺失项(交付前必须补齐)", ""]
        lines += [f"- ⚠ {m}" for m in missing]
    lines += ["", "> 本文件由 `rs_ingest.py deliverables` 生成;下游交付/复核以此为准。", ""]
    dst = out / "deliverables.md"
    dst.write_text("\n".join(lines), encoding="utf-8")
    # N4:S11 交付自动附带决策说明书(无决策信息的旧工程不强造)
    notes_path = None
    try:
        notes_path = write_decision_notes(root)
    except OSError:
        notes_path = None
    ok = not missing
    return {"ok": ok, "code": "DELIVERABLES_OK" if ok else "DELIVERABLES_INCOMPLETE",
            "path": str(dst), "videos": videos, "missing": missing,
            "decisionNotes": str(notes_path) if notes_path else None,
            "ratio": ratio, "firstCheckDone": bool((verify.get("firstCheck") or {}).get("done"))}


# ---------------------------------------------------------------- 决策说明书(N4)

# 「想改什么 → 改哪里 → 重跑哪段」对照(改一条、重跑一段,不整片重来)
RERUN_HINTS: list[tuple[tuple[str, ...], str, str]] = [
    (("maxChars", "cpsMax", "subStyle"), "00_brief/brief.md「管线参数」段",
     "python <scripts>/rs_run.py --root <工程> --only S7 --force"),
    (("ratio", "platform"), "brief.json → rs_intent.py compile 重编译 00_brief/",
     "python <scripts>/rs_run.py --root <工程> --from S3 --force"),
    (("durationTarget",), "brief.json(durationTarget)→ 重编译", "文案/粗剪口径随 brief 重跑 --from S2 --force"),
    (("bgm", "pacing"), "00_brief/brief.md 风格段(节奏档/BGM)→ 混音段", "python <scripts>/rs_run.py --root <工程> --from S6 --force"),
    (("cardsPlan", "density"), "00_brief/cards.json / 03_assets/artboard/", "python 03_assets/artboard/rebuild.py"),
    (("styleEntry", "prompt"), "00_brief/intent_decisions.json(整体重编译)", "rs_intent.py compile → rs_run.py --root <工程> --from S3 --force --auto"),
]
_SRC_LEGEND = ("来源图例:`user`=用户/提示词明说;`registry`=风格注册表或平台预设缺省;"
               "`default`=全局缺省;`auto`=--auto 无人值守自动裁决。"
               "`inferred=true` 的条目都不是用户亲口说的,可逐条推翻重跑。")


def _notes_rows(log: list[dict], source_prefix: str) -> list[str]:
    rows = []
    for i, d in enumerate(log, 1):
        if not isinstance(d, dict):
            continue
        field = str(d.get("field") or d.get("id") or "?")
        what = str(d.get("what") or d.get("value") or "")
        why = str(d.get("why") or "")
        quote = str(d.get("promptQuote") or "")
        rows.append(
            f"| {source_prefix}{i} | {field} | {what[:48]} | {d.get('source', '?')} "
            f"| {'是' if d.get('inferred') else '否'} | {why[:60]} | {quote[:40]} |")
    return rows


def build_decision_notes(root: Path) -> str | None:
    """人类可读中文「决策说明书」(副文档 04 N4):每个关键参数的来历与改法。

    数据源:05_ir/pipeline.json 的 params + decision_log(意图编译 + --auto 运行时)
    与 00_brief/intent_decisions.json(未被 --auto 跑过的工程也能看懂编译结果)。
    没有任何决策信息时返回 None(非意图流程的旧工程不强造说明书)。
    """
    import rs_run  # noqa: PLC0415 — 延迟导入,避免 rs_ingest 常规路径变重
    pipeline: dict = {}
    pj = root / "05_ir" / "pipeline.json"
    if pj.is_file():
        try:
            pipeline = json.loads(pj.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pipeline = {}
    log = [d for d in (pipeline.get("decision_log") or []) if isinstance(d, dict)]
    intent = [d for d in rs_run.intent_decisions_of(root) if isinstance(d, dict)]
    if not log and not intent:
        return None
    by_src: dict[str, list[dict]] = {}
    for d in log:
        by_src.setdefault(str(d.get("source") or "?"), []).append(d)
    intent_ids = {d.get("id") for d in intent}
    runtime = [d for d in log if d.get("id") not in intent_ids]

    style = {}
    ip = root / "00_brief" / "intent_decisions.json"
    if ip.is_file():
        try:
            style = (json.loads(ip.read_text(encoding="utf-8")) or {}).get("style") or {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            style = {}
    verify: dict = {}
    vp = root / "_state" / "verify.json"
    if vp.is_file():
        try:
            verify = json.loads(vp.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            verify = {}
    params = pipeline.get("params") or {}

    lines = [f"# 决策说明书 — {root.name}", "",
             "> 这份片子每个关键参数**从哪来、为什么、想改的话改哪里重跑哪段**。",
             "> 生成:`rs_ingest.py decisions <工程>`(S11 交付自动附带,可随时重跑再生成)。",
             "> **L2 最终验收始终归用户**——本说明书不代替验收,只让验收有的放矢。", "",
             "## 1. 一句话", ""]
    if style:
        lines.append(f"- 风格条目:`{style.get('id')}`({style.get('label')})→ "
                     f"{style.get('platform')} / {style.get('ratio')} / 字幕 {style.get('subStyle')}"
                     f" / 节奏 {style.get('pacing')}(templates/styles/registry.json)")
    else:
        lines.append("- 本工程未经意图编译(非 --from-prompt 流程);以下为 --auto 运行时决策。")
    lines += ["", "## 2. 参数快照(复现快照,05_ir/pipeline.json)", "",
              "| 参数 | 值 |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(params.items())] or ["| (无) | |"]
    lines += ["", "## 3. 决策逐条", "", _SRC_LEGEND, "",
              "| # | 字段/决策 | 值/内容 | 来源 | 推断 | 为什么 | 提示词依据 |",
              "|---|---|---|---|---|---|---|"]
    lines += _notes_rows([d for d in log if d.get("id") in intent_ids], "意图")
    lines += _notes_rows(runtime, "运行")
    if not (log or intent):
        lines.append("| — | (无决策记录) | | | | | |")
    lines += ["", "## 4. 改一条、重跑一段", "",
              "| 想改什么 | 改哪里 | 重跑哪段 |", "|---|---|---|"]
    for keys, where, rerun in RERUN_HINTS:
        lines.append(f"| {'/'.join(keys)} | {where} | `{rerun}` |")
    l0 = (verify.get("lastL0") or {}) if isinstance(verify.get("lastL0"), dict) else {}
    lines += ["", "## 5. 验收状态", "",
              f"- L0 机械自检:{l0.get('result') or '未记录'}(唯一自动闸,**不放松**)",
              "- L1 目测:--auto 降级为抽帧留证(`06_output/L1未人工确认_抽帧留证.png`),未人工确认",
              "- **L2 最终验收:归用户**。对照本说明书逐条看,不认可的改一条、重跑一段(§4)。", "",
              "> 本文件由脚本生成,内容全部来自 pipeline.json 的 decision_log 与 intent_decisions.json;",
              "> 决策条目带 id,重跑同 id 覆盖不堆积。", ""]
    return "\n".join(lines)


def write_decision_notes(root: Path) -> Path | None:
    """生成 06_output/决策说明书.md(原子写);无决策信息时返回 None。"""
    md = build_decision_notes(root)
    if md is None:
        return None
    from rs_common import write_text_atomic  # noqa: PLC0415
    dst = root / "06_output" / "决策说明书.md"
    write_text_atomic(dst, md)
    return dst


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="scan",
                    choices=["scan", "deliverables", "green-ok", "decisions"])
    ap.add_argument("root", help="工程根目录")
    ap.add_argument("--slug", default=None)
    ap.add_argument("--ratio", default="9x16", choices=list(RATIOS))
    ap.add_argument("--reason", default="", help="green-ok:用户对误判的说明(必填)")
    a = ap.parse_args()
    root = Path(a.root)
    if not root.is_dir():
        return emit(False, "NO_PROJECT", f"工程根不存在:{root}", exit_code=2)
    if a.command == "deliverables":
        # P18-1:对账不齐 = 交付门禁不过,必须非零退出(不再永远 ok=true 假绿)
        res = build_deliverables(root)
        ok = bool(res.get("ok"))
        miss = res.get("missing") or []
        msg = f"交付清单 → {res['path']}"
        if not ok:
            msg = (f"交付对账不齐({len(miss)} 项):{'、'.join(miss[:3])}"
                   + ("…" if len(miss) > 3 else "") + f";清单已生成 → {res['path']}")
        return emit(ok, res["code"], msg,
                    {k: v for k, v in res.items() if k not in ("code", "ok")},
                    exit_code=0 if ok else 4)
    if a.command == "decisions":
        # N4:人类可读「决策说明书」—— 每个关键参数的来历与「改一条、重跑一段」
        dst = write_decision_notes(root)
        if dst is None:
            return emit(False, "NO_DECISIONS",
                        "没有可用的决策记录(先 rs_intent.py compile 或 rs_run.py --auto)",
                        exit_code=2)
        return emit(True, "DECISIONS_OK", f"决策说明书已生成 → {dst}", {"path": str(dst)})
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
