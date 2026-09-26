"""S0 素材摄取 + 交付清单(OPTIMIZATION-v7 #9)+ 绿幕门禁(ADR-0031)+ 决策说明书(N4)。

用法:
  rs_ingest.py scan <工程根> [--slug X] [--ratio 9x16]   # 01_原始素材 → manifest + IR 骨架
  rs_ingest.py deliverables <工程根>                      # 汇总 → 06_成片输出/deliverables.md
  rs_ingest.py deliverables <工程根> --publish            # 发布 → 成品/(拷贝非移动,ADR-0052)
  rs_ingest.py green-ok <工程根> --reason "误判说明"       # 绿幕误判放行(写 override 留痕)
  rs_ingest.py decisions <工程根>                          # 生成 06_成片输出/决策说明书.md(N4)

设计:机械动作全脚本化,Agent 只补"内容摘要"。
  · probe 失败**不静默**:写进 manifest 的 `probe: failed/unavailable` 并在报告里点名;
  · v0.14 起 CutFlow 不做抠像:scan 会对每条视频跑幕布检测(rs_greenscreen),命中且未放行
    时**阻断** S0(GREEN_SCREEN_INPUT),提示用户先自行抠像+合成背景再重跑;
  · 不覆盖已有 `05_时间线工程/project.json`(只生成 `project.skeleton.json`);
  · 输出统一 `{ok, code, message, data}`,退出码 0/2/3/4。

--publish(ADR-0052 半成品/成品分离):
  · 从工程区**拷贝/衍生**最终产物到 `成品/`(成片/字幕/封面/文案/说明书/对账.md/
    半成品入口.md)—— 拷贝不是移动,工程区母版原样保留;
  · 覆写前把旧 `成品/` 整区备份到 `_内部状态/backup/publish-<ts>/`;
  · 幂等:重复执行文件集不变;发布流水(`_内部状态/publish.json`)记 hash 留痕;
  · 前置+后置只读区自检:工程文件/剪映草稿/重建脚本混进 `成品/` → READONLY_ZONE_VIOLATED;
  · 对账按方案 §4.3(缺项 → `DELIVERABLES_INCOMPLETE` 非零退出,`成品/对账.md` 红标点名);
  · 旧工程交付物散在 06_成片输出 顶层的照常归集(只读,不改旧结构)。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rs_common  # noqa: E402
import rs_greenscreen  # noqa: E402
import rs_matting  # noqa: E402  — M9(ADR-0050):--allow-auto-matting 质量门禁分流
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量
from rs_common import RATIOS, canvas_for, emit  # noqa: E402

MANIFEST_JSON = "manifest.json"
MANIFEST_MD = "MANIFEST.md"
SKELETON = "project.skeleton.json"
COPYRIGHT_JSON = "copyright.json"    # 引用素材登记表(01_原始素材/copyright.json,方案 §5.5.3)
SKIP_NAMES = {MANIFEST_JSON, MANIFEST_MD, "GREENSCREEN.md", COPYRIGHT_JSON}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv",
              ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".mxf"}

# 引用素材登记 usage 枚举(方案 §5.5.3:引用素材必须登记 来源 + 时长 + 单部引用占比;
# 权威占比门禁在 rs_verify L0,对成片时长结算 —— 这里按素材侧做预检与留痕)
USAGE_ORIGINAL = "original"          # 原创/自产/AI 生成
USAGE_QUOTE = "quote"                # 影视片段引用(受版权门禁约束)
USAGE_LICENSED = "licensed"          # 已获授权
USAGE_KINDS = (USAGE_ORIGINAL, USAGE_QUOTE, USAGE_LICENSED)
# 预检阈值(与 rs_verify L0 判据同初值;此处只 WARN,硬闸在 rs_verify)
COPYRIGHT_MAX_SINGLE_SOURCE = 0.30
COPYRIGHT_MAX_QUOTE_TOTAL = 0.70


def copyright_json(root: Path) -> Path:
    """01_原始素材/copyright.json(引用登记表路径)。"""
    return rs_paths.resolve(root, "materials") / COPYRIGHT_JSON


def dur_map(items: list[dict]) -> dict[str, int]:
    """素材文件 → probe 时长 ms(登记最简形态下占比计算的回退依据)。"""
    return {str(i.get("file")): int(i.get("durationMs") or 0)
            for i in items if i.get("file")}


def load_copyright(root: Path) -> dict:
    """读登记表;缺文件/坏 JSON → 空表(调用方按「缺登记」处置)。"""
    p = copyright_json(root)
    if not p.is_file():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def copyright_stats(items: list[dict], registered: list[dict]) -> dict:
    """登记表 × 素材时长 → 占比统计(素材侧预检口径,单部/总量/逐作品明细)。

    items = manifest.items(probe 时长);registered = 登记条目。
    分母 = 全部视频素材时长之和(素材侧口径;成片口径在 rs_verify L0 按 IR/成片结算)。
    """
    dur_by_file = {i.get("file"): int(i.get("durationMs") or 0) for i in items}
    total_ms = sum(d for d in dur_by_file.values() if d > 0)
    per_source: dict[str, int] = {}
    quote_total = 0
    for it in registered:
        if not isinstance(it, dict) or str(it.get("usage") or "") != USAGE_QUOTE:
            continue
        # 条目 quotedMs 缺省时回退素材时长(登记的最简形态:file+source 即可算)
        q = it.get("quotedMs")
        q = int(q) if isinstance(q, (int, float)) else dur_by_file.get(str(it.get("file")), 0)
        src = str(it.get("source") or "(未注明来源)")
        per_source[src] = per_source.get(src, 0) + max(0, q)
        quote_total += max(0, q)
    worst = max(per_source.items(), key=lambda kv: kv[1]) if per_source else ("", 0)
    return {
        "totalMaterialMs": total_ms,
        "quoteTotalMs": quote_total,
        "quoteRatio": round(quote_total / total_ms, 4) if total_ms else None,
        "perSource": {k: {"quotedMs": v,
                          "ratio": round(v / total_ms, 4) if total_ms else None}
                      for k, v in sorted(per_source.items())},
        "worstSource": worst[0],
        "worstRatio": round(worst[1] / total_ms, 4) if total_ms and worst[1] else None,
    }

# ---------------------------------------------------------------- 发布(ADR-0052)

# 成品/ 只读区断言(ADR-0052 §4.3「不放」清单):工程文件/剪映草稿/重建脚本
# 混进交付区即违规(publish 前置+后置各跑一次;发现即拒发,绝不代删用户文件)。
READONLY_FORBIDDEN = {"project.json", "wordline.json", "wordline.final.json",
                      "cutlist.json", "notes.json", "rebuild.py"}
READONLY_FORBIDDEN_GLOBS = ("*.draft_content.json",)


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


def scan(root: Path, slug: str = "project", ratio: str = "9x16",
         allow_auto_matting: bool = False) -> dict:
    mat = rs_paths.resolve(root, "materials")
    if not mat.is_dir():
        return {"ok": False, "code": "NO_MATERIALS", "message": f"缺素材目录 {mat.name}:{mat}"}
    files = sorted(p for p in mat.iterdir() if p.is_file() and p.name not in SKIP_NAMES)
    items = []
    for p in files:
        item = {"file": p.name, "sizeBytes": p.stat().st_size}
        item.update(probe_media(p))
        if item.get("probe") == "ok" and p.suffix.lower() in VIDEO_EXTS:
            item["greenScreen"] = rs_greenscreen.detect_media(p)
        items.append(item)

    # v0.14(ADR-0031):绿幕门禁 —— 命中且未放行则阻断。放行说明写在
    # 00_制作简报/greenscreen-override.txt(或 brief.md 的「绿幕检测:误判…」行)。
    override = rs_greenscreen.read_override(root)
    flagged = [i for i in items if (i.get("greenScreen") or {}).get("detected")]
    if override:
        for i in flagged:
            i["greenScreen"]["overridden"] = True

    doc = {"version": 1, "materials": str(mat.name), "items": items,
           "greenOverride": override}
    mat.joinpath(MANIFEST_JSON).write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    mat.joinpath(MANIFEST_MD).write_text(
        _manifest_md(items, override,
                     copyright_registered=bool(load_copyright(root).get("items"))),
        encoding="utf-8")

    ir_dir = rs_paths.resolve(root, "timeline")
    ir_dir.mkdir(parents=True, exist_ok=True)
    skeleton = _skeleton(items, slug, ratio, materials_dir=mat.name)
    wrote_skeleton = False
    if not (ir_dir / "project.json").is_file():
        ir_dir.joinpath(SKELETON).write_text(json.dumps(skeleton, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
        wrote_skeleton = True
    failed = [i["file"] for i in items if i.get("probe") != "ok"]
    if flagged and not override and allow_auto_matting:
        # (b) 分流(ADR-0050):显式允许自动抠像 → 先跑五项质量门禁,达标才放行。
        # 引擎未部署/不达标 → 阻断(留安装/预处理建议);默认行为 (a) 不变。
        for i in flagged:
            src = mat / i["file"]
            code_m, gdoc = rs_matting.gate(root, src, max_frames=48)
            i["greenScreen"]["autoMatting"] = {"verdict": gdoc.get("verdict"),
                                               "engine": gdoc.get("engine"),
                                               "metrics": gdoc.get("metrics", {}),
                                               "suggestions": gdoc.get("suggestions", [])}
            if gdoc.get("verdict") in ("pass", "warn"):
                i["greenScreen"]["overridden"] = True
                i["greenScreen"]["autoMattingApplied"] = True
        # 放行结果回写 manifest(留痕:autoMatting 判定与放行标记必须落盘)
        mat.joinpath(MANIFEST_JSON).write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        blocked = [i for i in flagged if not i["greenScreen"].get("overridden")]
        if blocked:
            names = ",".join(i["file"] for i in blocked[:3])
            verdicts = {i["file"]: i["greenScreen"]["autoMatting"]["verdict"] for i in blocked}
            return {"ok": False, "code": "MATTE_QUALITY_FAIL",
                    "message": (f"自动抠像质量门禁未过({names}):{verdicts}。"
                                "引擎未部署时先 rs_fetchable install rvm;"
                                "质量不达标请改走用户预处理(ADR-0031 默认路径)"),
                    "items": items, "flagged": [i["file"] for i in flagged],
                    "failed": failed, "manifest": str(mat / MANIFEST_JSON)}
    # (a)/(c) 分流:全局 override 已在上方标 overridden;(b) 放行的条目同样带
    # overridden —— 只拦「仍未放行」的素材,不误伤已过质量门禁的。
    unresolved = [i for i in flagged if not i["greenScreen"].get("overridden")]
    if unresolved:
        gpath = rs_greenscreen.guidance_md(root, unresolved)
        names = ",".join(i["file"] for i in unresolved[:3])
        return {"ok": False, "code": "GREEN_SCREEN_INPUT",
                "message": f"检测到 {len(unresolved)} 条素材仍含绿幕/蓝幕({names});"
                           "CutFlow v0.14 起不再抠像 —— 请先自行抠像并合成背景,替换原素材后重跑 "
                           "scan;若为误判,运行 `rs_ingest.py green-ok <工程根> --reason \"…\"`;"
                           "或显式加 --allow-auto-matting 走自动抠像质量门禁(ADR-0050)。"
                           f"处理指引见 {gpath.relative_to(root).as_posix()}",
                "items": items, "flagged": [i["file"] for i in unresolved],
                "guidance": str(gpath), "failed": failed,
                "manifest": str(mat / MANIFEST_JSON), "report": str(mat / MANIFEST_MD)}
    return {"ok": True, "code": "INGEST_OK", "items": items, "failed": failed,
            "flagged": [i["file"] for i in flagged],
            "skeletonWritten": wrote_skeleton,
            "manifest": str(mat / MANIFEST_JSON), "report": str(mat / MANIFEST_MD)}


def _manifest_md(items: list[dict], green_override: str | None = None,
                 copyright_registered: bool | None = None) -> str:
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
                      f"(见 `{rs_paths.p('materials')}/GREENSCREEN.md`)或运行 `rs_ingest.py green-ok`。"]
    lines += ["", "## 版权登记(方案 §5.5.3)", "",
              f"- 引用素材登记表:`{rs_paths.p('materials')}/{COPYRIGHT_JSON}` → "
              + ("已登记(校验:`rs_ingest.py copyright <工程根>`)"
                 if copyright_registered else
                 "**缺失** —— 引用型工程(短剧/影视解说)必须登记 来源/时长/单部引用占比;"
                 "纯原创工程登记 usage=original 条目即可"),
              ""]
    lines += ["", "> 类型/videoType、比例、风格、声音方案请在 "
              f"`{rs_paths.p('brief')}/brief.md` 声明。", ""]
    return "\n".join(lines)


def _skeleton(items: list[dict], slug: str, ratio: str, materials_dir: str = "") -> dict:
    """IR 骨架。src 用**实际素材目录名**(新结构直用;旧结构兜底同名,内引用不悬空)。"""
    w, h = canvas_for(ratio)
    mdir = materials_dir or rs_paths.p("materials")
    clips, cursor = [], 0
    for i in items:
        dur = int(i.get("durationMs") or 0)
        if dur <= 0:
            continue
        clips.append({"src": f"{mdir}/{i['file']}", "startMs": cursor, "durationMs": dur,
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
      · 成片 ≥1(06_成片输出 顶层 / final/ / branded/);
      · `subtitles.ass` + `master.srt`(S7 产物);
      · `metadata.json` 且 `platforms` 条目非空(S10 产物);
      · `封面.png`(rs_common.COVER_PNG,P17-1 同一常量);
      · `sync_report.md`(S9 产物);
      · `variants.json` 存在时:矩阵每条 (ratio, logo) 都要有对应 `成片_<ratio>_<logo>_*.mp4`。
    清单文件 deliverables.md 照常生成(缺失项逐条点名),ok/exit 码如实反映对账结果。
    """
    from rs_common import COVER_PNG, ratio_for_canvas
    out = rs_paths.resolve(root, "output")
    out.mkdir(parents=True, exist_ok=True)
    # P10b-1:成片清单含独占子目录 final/、branded/(相对路径列出,交付位置可追)
    videos = sorted({p.relative_to(out).as_posix()
                     for pat in ("*.mp4", "final/*.mp4", "branded/*.mp4")
                     for p in out.glob(pat)})
    canvas = {}
    pj = rs_paths.project_json(root)
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
    vp = rs_paths.verify_json(root)
    if vp.is_file():
        try:
            verify = json.loads(vp.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            verify = {}
    brief_title = _first_line(rs_paths.brief_md(root), "#")
    sync_line = _first_line(out / "sync_report.md", "**总判定**")
    cut_line = _first_line(rs_paths.resolve(root, "cut") / "cut_report.md", "- 源时长")

    # ---- 真对账(P18-1):逐项核验,缺一项记一项
    missing: list[str] = []
    if not videos:
        missing.append(f"成片({out.name} 顶层 / final/ / branded/ 至少 1 个 .mp4)")
    for name in ("subtitles.ass", "master.srt", "sync_report.md", COVER_PNG):
        if not (out / name).is_file():
            missing.append(f"{out.name}/{name}")
    meta: dict = {}
    meta_path = out / "metadata.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            missing.append(f"{out.name}/metadata.json(存在但无法解析)")
    if not (isinstance(meta.get("platforms"), dict) and meta.get("platforms")):
        missing.append(f"{out.name}/metadata.json 平台条目(platforms 缺失或为空;由 rs_meta 生成)")
    # 变体对账:variants.json 声明的矩阵 ↔ 盘面成片(账实相符)
    variant_notes: list[str] = []
    vj = rs_paths.resolve(root, "timeline") / "variants.json"
    if vj.is_file():
        matrix: list = []
        try:
            doc = json.loads(vj.read_text(encoding="utf-8"))
            matrix = (doc or {}).get("matrix") or []
        except json.JSONDecodeError:
            variant_notes.append(f"{rs_paths.p('timeline')}/variants.json 无法解析,按 0 条变体对账")
        final_names = [Path(v).name for v in videos]
        for v in matrix:
            want = f"成片_{v.get('ratio')}_{v.get('logo')}_"
            if not any(n.startswith(want) and n.endswith(".mp4") for n in final_names):
                missing.append(f"变体 {v.get('id')} 缺成片({want}<profile>.mp4)")
        if not matrix and not variant_notes:
            variant_notes.append(f"{rs_paths.p('timeline')}/variants.json 无 matrix 条目(未启用品牌变体)")

    lines = [
        f"# 交付清单 — {brief_title.lstrip('# ').strip() or root.name}", "",
        f"- 画幅:`{ratio or '未定'}`", f"- 成片:{len(videos)} 个",
        f"- 验证等级:`{verify.get('lastL0', {}).get('level', '—') if isinstance(verify.get('lastL0'), dict) else '—'}`"
        f" ｜ 首次全检:`{'已完成' if (verify.get('firstCheck') or {}).get('done') else '未完成'}`",
        f"- 内容指纹:`{(verify.get('lastL0') or {}).get('at', '—') if isinstance(verify.get('lastL0'), dict) else '—'}`",
        "", "## 成片", ""]
    lines += ([f"- `{out.name}/{v}`" for v in videos] or ["- (尚无成片)"])
    lines += ["", "## 对账(P18-1:缺一项即 DELIVERABLES_INCOMPLETE)", ""]
    for name in ("subtitles.ass", "master.srt", COVER_PNG, "metadata.json",
                 "sync_report.md"):
        mark = "✓" if not any(name in m for m in missing) else "**缺失**"
        lines.append(f"- {mark} `{out.name}/{name}`")
    if not videos:
        lines.append("- **缺失** 成片(至少 1 个)")
    lines.append(f"- {'✓' if not any('变体' in m for m in missing) else '**账实不符**'} "
                 f"变体成片 ↔ `{rs_paths.p('timeline')}/variants.json` 矩阵")
    for note in variant_notes:
        lines.append(f"  - ℹ {note}")
    lines += ["", "## 校验摘要", ""]
    lines.append(f"- 粗剪:{cut_line or '未做'}")
    lines.append(f"- 对齐:{sync_line or '未做 rs_sync'}")
    lines.append(f"- 字幕:`{out.name}/subtitles.ass` "
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
    (("maxChars", "cpsMax", "subStyle"), f"{rs_paths.p('brief')}/brief.md「管线参数」段",
     "python <scripts>/rs_run.py --root <工程> --only S7 --force"),
    (("ratio", "platform"), f"brief.json → rs_intent.py compile 重编译 {rs_paths.p('brief')}/",
     "python <scripts>/rs_run.py --root <工程> --from S3 --force"),
    (("durationTarget",), "brief.json(durationTarget)→ 重编译", "文案/粗剪口径随 brief 重跑 --from S2 --force"),
    (("bgm", "pacing"), f"{rs_paths.p('brief')}/brief.md 风格段(节奏档/BGM)→ 混音段", "python <scripts>/rs_run.py --root <工程> --from S6 --force"),
    (("cardsPlan", "density"), f"{rs_paths.p('brief')}/cards.json / {rs_paths.p('assets')}/artboard/",
     f"python {rs_paths.p('assets')}/artboard/rebuild.py"),
    (("styleEntry", "prompt"), f"{rs_paths.p('brief')}/intent_decisions.json(整体重编译)",
     "rs_intent.py compile → rs_run.py --root <工程> --from S3 --force --auto"),
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

    数据源:05_时间线工程/pipeline.json 的 params + decision_log(意图编译 + --auto 运行时)
    与 00_制作简报/intent_decisions.json(未被 --auto 跑过的工程也能看懂编译结果)。
    没有任何决策信息时返回 None(非意图流程的旧工程不强造说明书)。
    """
    import rs_run  # noqa: PLC0415 — 延迟导入,避免 rs_ingest 常规路径变重
    pipeline: dict = {}
    pj = rs_paths.pipeline_json(root)
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
    ip = rs_paths.resolve(root, "brief") / "intent_decisions.json"
    if ip.is_file():
        try:
            style = (json.loads(ip.read_text(encoding="utf-8")) or {}).get("style") or {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            style = {}
    verify: dict = {}
    vp = rs_paths.verify_json(root)
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
    lines += ["", f"## 2. 参数快照(复现快照,{rs_paths.p('timeline')}/pipeline.json)", "",
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
              f"- L1 目测:--auto 降级为抽帧留证(`{rs_paths.p('output')}/L1未人工确认_抽帧留证.png`),未人工确认",
              "- **L2 最终验收:归用户**。对照本说明书逐条看,不认可的改一条、重跑一段(§4)。", "",
              "> 本文件由脚本生成,内容全部来自 pipeline.json 的 decision_log 与 intent_decisions.json;",
              "> 决策条目带 id,重跑同 id 覆盖不堆积。", ""]
    return "\n".join(lines)


def write_decision_notes(root: Path) -> Path | None:
    """生成 06_成片输出/决策说明书.md(原子写);无决策信息时返回 None。"""
    md = build_decision_notes(root)
    if md is None:
        return None
    from rs_common import write_text_atomic  # noqa: PLC0415
    dst = rs_paths.resolve(root, "output") / "决策说明书.md"
    write_text_atomic(dst, md)
    return dst


# ---------------------------------------------------------------- 发布(ADR-0052)

def readonly_zone_violations(deliver: Path) -> list[str]:
    """成品/ 只读区断言(ADR-0052「不放」清单):返回违规相对路径(空 = 干净)。

    命中 = 工程文件(project/wordline/cutlist/notes)、重建脚本 rebuild.py、
    剪映草稿 *.draft_content.json 混进了交付区 —— 半成品只能待在工程区。
    """
    if not deliver.is_dir():
        return []
    bad = []
    for f in deliver.rglob("*"):
        if f.name in READONLY_FORBIDDEN or any(f.match(g) for g in READONLY_FORBIDDEN_GLOBS):
            bad.append(f.relative_to(deliver).as_posix())
    return sorted(bad)


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _png_size(path: Path) -> tuple[int, int] | None:
    """PNG 头部 IHDR → (宽, 高);非 PNG / 读不得 → None(零依赖,不给探针添配置)。"""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def _canvas_wh(root: Path) -> tuple[int, int] | None:
    c = _load_json(rs_paths.project_json(root)).get("canvas") or {}
    try:
        return int(c["width"]), int(c["height"])
    except (KeyError, TypeError, ValueError):
        return None


def _platform_presets(root: Path) -> list[dict]:
    """工程适用平台预设(封面 coverSize/说明书平台适配共用):声明平台优先,
    未声明 → 与画布同尺寸的全部平台(最严口径,与 rs_verify._platform_safe_areas 同构)。"""
    try:
        from rs_subtitle import load_platforms
        table = load_platforms() or {}
    except Exception:  # noqa: BLE001 — 预设读不得 → 视为无平台可对账
        return []
    plat = str(_load_json(rs_paths.pipeline_json(root)).get("params", {}).get("platform") or "")
    if plat in table:
        return [dict(table[plat], _key=plat)]
    canvas = _canvas_wh(root)
    if not canvas:
        return []
    return [dict(v, _key=k) for k, v in table.items()
            if isinstance(v.get("canvas"), list)
            and (int(v["canvas"][0]), int(v["canvas"][1])) == canvas]


def _declared_outputs(root: Path) -> list[str]:
    """brief/IR 声明的画幅集合(project.json.outputs;未声明 → [](判据缺席不硬造))。"""
    outs = _load_json(rs_paths.project_json(root)).get("outputs") or []
    return [str(o) for o in outs if isinstance(o, str)]


def _video_ratios(videos: list[str]) -> set[str]:
    """成片文件名里的画幅(成片_<ratio>_<logo>_*.mp4 命名约定)。"""
    rs = set()
    for v in videos:
        m = re.match(r"成片_(\d+x\d+)_", Path(v).name)
        if m:
            rs.add(m.group(1))
    return rs


def _has_decision_log(root: Path) -> bool:
    """工程是否留过决策(--auto / 意图编译):pipeline.json.decision_log 非空。"""
    return bool(_load_json(rs_paths.pipeline_json(root)).get("decision_log"))


def build_delivery_notes(root: Path, base: dict | None = None) -> str:
    """交付说明书(成品/说明书/交付说明书.md,§4.3)。

    内容全部机械取自盘上台账:成片构成 / 质量与降级留痕 / 素材幕布放行 /
    文案状态与平台适配 / 版权与 AI 标识待确认区。**有 degraded 的产物必须在此
    列明原因与影响**(对账项「降级诚实」机械核对本文件是否含降级原因)。
    全程无时间戳,保证 --publish 幂等。base 可传入 publish 已算好的对账基线,
    避免同一轮发布里 build_deliverables 跑两遍。
    """
    from rs_common import COVER_PNG
    out = rs_paths.resolve(root, "output")
    if base is None:
        base = build_deliverables(root)
    videos = base.get("videos") or []
    wl = _load_json(rs_paths.wordline_json(root))
    verify = _load_json(rs_paths.verify_json(root))
    l0 = verify.get("lastL0") if isinstance(verify.get("lastL0"), dict) else {}
    brief_title = _first_line(rs_paths.brief_md(root), "#").lstrip("# ").strip() or root.name
    sync_line = _first_line(out / "sync_report.md", "**总判定**")
    platforms = _platform_presets(root)
    meta = _load_json(out / "metadata.json")
    plats = meta.get("platforms") if isinstance(meta.get("platforms"), dict) else {}

    lines = [f"# 交付说明书 — {brief_title}", "",
             "> 由 `rs_ingest.py deliverables --publish` 生成(成品/ 只读区的说明页)。",
             "> 可编辑的工程在「半成品入口.md」指向的位置,本区文件请勿手工改。", "",
             "## 1. 交付构成", "",
             f"- 成片:{len(videos)} 个" + (f",画幅 `{base.get('ratio') or '未定'}`" if videos else "(**缺失**)")]
    lines += [f"  - `{v}`" for v in videos] or ["  - (尚无成片)"]
    lines += [f"- 字幕:`subtitles.ass` + `master.srt`"
              f"{'(成对)' if (out / 'subtitles.ass').is_file() and (out / 'master.srt').is_file() else '(**不成对**)'}",
              f"- 封面:`{COVER_PNG}`" + ("" if (out / COVER_PNG).is_file() else "(**缺失**)"),
              f"- 文案:metadata.json({len(plats)} 平台"
              + (",⚠ 含占位标题/简介,发布前需 Agent 补写" if meta.get("needsAgent") else "") + ")",
              ""]
    lines += ["## 2. 质量与降级留痕", "",
              f"- L0 机械自检:{l0.get('result') or '未记录'}(唯一自动闸,不放松)",
              f"- 对齐:{sync_line or '未做 rs_sync'}",
              f"- 首次人工检查:"
              f"{'已完成' if (verify.get('firstCheck') or {}).get('done') else '**未完成**——L2 最终验收归用户'}"]
    if wl.get("degraded"):
        reasons = [str(r) for r in (wl.get("degradeReasons") or [])] or ["原因未记录"]
        lines += [f"- ⚠ **降级交付**:字幕时间轴处于降级模式,影响 = 对齐精度受限;",
                  f"  - 原因:{';'.join(reasons)}",
                  "  - 处置:可修复上游(转写/对齐)后重跑 S2–S7,或按现状人工验收"]
    else:
        lines += ["- 降级:无(字幕时间轴全程字级精确)"]
    lines += ["", "## 3. 素材与抠像(ADR-0031)", ""]
    manifest = _load_json(rs_paths.manifest_json(root))
    if manifest.get("greenOverride"):
        lines += [f"- 幕布素材已按用户说明**放行**:{manifest['greenOverride']}"]
    elif any((i.get("greenScreen") or {}).get("detected") for i in manifest.get("items") or []):
        lines += ["- ⚠ 素材含幕布且未见放行说明(见 01_原始素材/GREENSCREEN.md)"]
    else:
        lines += ["- 素材未检出幕布(或未做检测;v0.14 起 CutFlow 不做抠像)"]
    lines += ["", "## 4. 平台适配", ""]
    if platforms:
        lines += ["| 平台 | 画幅 | 封面 | 时长参考 | 说明 |", "|---|---|---|---|---|"]
        for v in platforms:
            lines.append(f"| {v.get('label') or v.get('_key', '?')} | {v.get('ratio', '—')} "
                         f"| {v.get('coverSize', '—')} | {v.get('durationHint', '—')} "
                         f"| {str(v.get('note', ''))[:40]} |")
    else:
        lines += ["- 工程未声明平台(先 rs_intent compile),按画幅缺省交付。"]
    lines += ["", "## 5. 版权与 AI 标识(交付前人工确认,脚本不判定)", ""]
    # 版权节(方案 §5.5.3):必含 引用清单 + 转化性说明 + AI 标识。
    cp_doc = load_copyright(root)
    cp_items = cp_doc.get("items") if isinstance(cp_doc.get("items"), list) else []
    if cp_items:
        lines += ["### 5.1 引用清单(登记表 机械誊录)", "",
                  "| 素材 | 来源(单部作品) | usage | 引用时长 |", "|---|---|---|---|"]
        for it in cp_items:
            if not isinstance(it, dict):
                continue
            q = it.get("quotedMs")
            lines.append(f"| {it.get('file', '—')} | {it.get('source', '—')} "
                         f"| {it.get('usage', '—')} | "
                         f"{f'{int(q) / 1000:.1f}s' if isinstance(q, (int, float)) else '—'} |")
        lines.append("")
        miss_note = ("**缺失 —— L1/用户项:无法机械判定,交付前必须人工补写"
                     "(WARN 非 PASS,方案 R7)**")
        lines.append(f"- 转化性说明:{cp_doc.get('transformNote') or miss_note}")
        ai_note = ("已声明" if cp_doc.get("aiDisclosure")
                   else "**未声明 —— 涉 AI 内容须按规定标识,交付前人工确认**")
        lines.append(f"- AI 标识:{ai_note}")
        lines.append("- 占比门禁(单部 ≤30% / 总引用 ≤70% / 解说轨 ≥25%):以 "
                     "`_内部状态/verify.json` L0 版权判据结果为准。")
    else:
        lines.append("- 引用登记:工程无引用登记表(非引用型工程不适用;引用型工程必须先登记)。")
    lines += ["- [ ] 音乐/字体/素材素材版权已确认可商用",
              "- [ ] 平台 AI 生成内容标识已按要求开启",
              "- [ ] 文案标题/简介为 Agent 撰写并经人工确认" +
              ("(当前 metadata.json 含占位,**必须补写**)" if meta.get("needsAgent") else ""),
              "", "> 缺项以 `成品/对账.md` 红标为准;本说明书只解释「交付了什么、有什么已知限制」。", ""]
    return "\n".join(lines)


def build_pointer_md(root: Path) -> str:
    """半成品入口.md(§4.3 ★指针文件):写明可编辑工程在哪、改完怎么让 Agent 接住。

    只写路径与两步说明,不复制工程本身(ADR-0052 被否决项:复制 = 双份真相源)。
    """
    project_rel = rs_paths.rel(root, "timeline", "project.json")
    jy_rel = rs_paths.jianying_draft(root).relative_to(root).as_posix()
    return f"""# 半成品入口(可继续编辑的工程在哪)

`成品/` 里全是**不可编辑**的最终产物。想改片子,请改工程区的半成品,改完重新发布。

## 可编辑工程(半成品落点)

- CutForge 同源工程(唯一真相源,不复制副本):`{project_rel}`
- 剪映 5.9 草稿(**单向出口**,只进不回):`{jy_rel}/<草稿名>/`
  - 为什么单向:5.9 明文草稿会被剪映规范化重排、元素 id 不稳定,无法建可信内容寻址
    锚点;6.0+ 加密永不读写。完整说明见 `skills/cutflow/rules/editing-roundtrip.md`。

## 改完怎么让 Agent 接住(两步)

1. **标脏**:在 CutForge 盘面上改完后跑
   `python skills/cutflow/scripts/rs_editor.py diff <工程根>` 看人话差异,
   阶段账会识别输入变化(或 `python skills/cutflow/scripts/rs_run.py --status --root <工程根>`);
2. **重出片**:`python skills/cutflow/scripts/rs_run.py --root <工程根> --from S3 --force`,
   完成后重跑 `python skills/cutflow/scripts/rs_ingest.py deliverables <工程根> --publish`
   刷新本 `成品/`。

> 剪映侧精修的成果请在剪映里直接导出,不要期待改动回流工程区(单向出口)。
"""


def build_reconcile_md(root: Path, base: dict, items: list[dict], files: list[str]) -> str:
    """成品/对账.md(§4.3):对账表 + 已发布清单 + 缺失项红标。无时间戳(幂等)。"""
    brief_title = _first_line(rs_paths.brief_md(root), "#").lstrip("# ").strip() or root.name
    lines = [f"# 成品对账 — {brief_title}", "",
             "> `rs_ingest.py deliverables --publish` 生成;对账项 = 方案 §4.3。",
             "> 缺项 = 此处红标 + 退出码 DELIVERABLES_INCOMPLETE(非零)。", "",
             "| 对账项 | 判定 | 说明 |", "|---|---|---|"]
    for it in items:
        lines.append(f"| {it['item']} | {'✓' if it['ok'] else '**✗ 缺**'} | {it['detail']} |")
    lines += ["", "## 已发布文件", ""]
    lines += [f"- `{f}`" for f in files] or ["- (无)"]
    all_missing = list(base.get("missing") or [])
    all_missing += [m for it in items for m in it.get("misses", [])]
    if all_missing:
        lines += ["", "## 缺失项(交付前必须补齐)", ""]
        lines += [f"- ⚠ {m}" for m in all_missing]
    lines += ["", "> 发布是**拷贝不是移动**:工程区母版原样保留;旧 成品/ 在"
              "`_内部状态/backup/publish-<时间戳>/`。", ""]
    return "\n".join(lines)


def _copy_deliverables(root: Path, out: Path, videos: list[str],
                       deliver: Path) -> list[str]:
    """工程区 → 成品/ 的拷贝清单(拷贝非移动;返回成品内相对路径,稳定排序)。

    旧工程交付物散在 06_成片输出 顶层的照常归集(只读它们,不改旧结构);
    重名冲突(顶层与 final/ 同名)时以子目录前缀消解,绝不静默覆盖。
    """
    from rs_common import COVER_PNG
    pairs: list[tuple[Path, Path]] = []
    seen: set[str] = set()
    for rel in videos:
        name = Path(rel).name
        if name in seen:
            name = rel.replace("/", "_")
        seen.add(name)
        pairs.append((out / rel, deliver / "成片" / name))
    for sub, names in (("字幕", ("subtitles.ass", "master.srt")),
                       ("封面", (COVER_PNG,)),
                       ("文案", ("metadata.json",))):
        for n in names:
            if (out / n).is_file():
                pairs.append((out / n, deliver / sub / n))
    copied: list[str] = []
    for src, dst in pairs:
        if not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(dst.relative_to(deliver).as_posix())
    return sorted(copied)


def _reconcile_publish(root: Path, base: dict, deliver: Path,
                       has_decision: bool) -> tuple[list[dict], list[str]]:
    """§4.3 对账表(在 build_deliverables 基线之上补 publish 专属判据)。

    返回 (对账行, 扩展缺项文案);行含 misses 键供 对账.md 汇总缺失项。
    """
    items: list[dict] = []
    all_missing: list[str] = []

    def add(item: str, ok: bool, detail: str, misses: list[str]) -> None:
        items.append({"item": item, "ok": bool(ok), "detail": detail, "misses": misses})
        all_missing.extend(misses)

    videos = base.get("videos") or []
    main_ratio = base.get("ratio") or ""

    # ① 成片 ≥1 且画幅集合与 brief.outputs(project.json.outputs)一致
    declared = _declared_outputs(root)
    have = _video_ratios(videos) or ({main_ratio} if videos and main_ratio else set())
    m1 = []
    if not videos:
        m1.append("成片:至少 1 个(06_成片输出 顶层 / final/ / branded/)")
    if declared:
        lack = sorted(r for r in declared if r not in have)
        if lack:
            m1.append(f"成片画幅 {lack} 无对应成片(声明 outputs={sorted(declared)})")
    add("成片 ≥1 且画幅集合与 brief.outputs 一致", not m1,
        f"{len(videos)} 个;实际画幅 {sorted(have) or '—'}"
        + (f";声明 {sorted(declared)}" if declared else ";工程未声明 outputs"), m1)

    # ② 字幕成对(实剪①补口径:无口播工程合法地无字幕——无 ass 且无 srt 且
    # 无 wordline → skipped 留痕,不按缺项红标)
    out = rs_paths.resolve(root, "output")
    m2 = [f"字幕:{n} 缺失" for n in ("subtitles.ass", "master.srt")
          if not (out / n).is_file()]
    if m2 and not (rs_paths.wordline_json(root).is_file()
                   or (out / "subtitles.ass").is_file()):
        add("字幕 subtitles.ass + master.srt 成对", True,
            "skipped:无字幕工程(无 wordline 无字幕产物)", [])
    else:
        add("字幕 subtitles.ass + master.srt 成对", not m2,
            "成对存在" if not m2 else "、".join(m2), m2)

    # ③ 封面尺寸 = 画幅 且过平台 coverSize(PNG IHDR 机械读尺寸)
    from rs_common import COVER_PNG
    cover = out / COVER_PNG
    size = _png_size(cover) if cover.is_file() else None
    m3, detail3 = [], "缺失"
    if size is not None:
        canvas = _canvas_wh(root)
        bad = []
        if canvas and tuple(size) != tuple(canvas):
            bad.append(f"尺寸 {size[0]}x{size[1]} ≠ 画幅 {canvas[0]}x{canvas[1]}")
        for v in _platform_presets(root):
            cs = v.get("coverSize")
            if isinstance(cs, list) and (int(cs[0]), int(cs[1])) != tuple(size):
                bad.append(f"不过平台 {v.get('label') or v.get('_key', '?')} "
                           f"coverSize {cs[0]}x{cs[1]}")
        detail3 = f"{size[0]}x{size[1]}" + ("" if not bad else "(" + ";".join(bad) + ")")
        m3 = [f"封面:{b}" for b in bad]
    add("封面 尺寸=画幅 且过平台 coverSize", not m3, detail3, m3)

    # ④ 文案 metadata.json 平台字数(rs_meta 截断口径复检)
    meta = _load_json(out / "metadata.json")
    plats = meta.get("platforms") if isinstance(meta.get("platforms"), dict) else {}
    m4 = []
    if not plats:
        m4.append("文案:metadata.json 平台条目缺失或为空")
    else:
        from rs_meta import PLATFORM_SPECS
        for k, p in sorted(plats.items()):
            spec = PLATFORM_SPECS.get(k)
            if not spec:
                continue
            t, d = str(p.get("title") or ""), str(p.get("desc") or "")
            if len(t) > spec["titleMax"]:
                m4.append(f"文案:{k} 标题 {len(t)} > {spec['titleMax']} 字")
            if len(d) > spec["descMax"]:
                m4.append(f"文案:{k} 简介 {len(d)} > {spec['descMax']} 字")
    add("文案 metadata.json 平台字数合规", not m4, f"{len(plats)} 平台", m4)

    # ⑤ 说明书:交付说明书必有(publish 生成);留过决策的工程必须有决策说明书
    notes = deliver / "说明书"
    m5 = []
    if not (notes / "交付说明书.md").is_file():
        m5.append("说明书:交付说明书.md 未生成")
    if has_decision and not (notes / "决策说明书.md").is_file():
        m5.append("说明书:工程留有决策记录(--auto/意图编译),缺 决策说明书.md")
    add("说明书存在(--auto 时含决策说明书)", not m5,
        "交付说明书" + (" + 决策说明书" if (notes / "决策说明书.md").is_file() else ""), m5)

    # ⑤b 素材归因:素材归因.md 必在(publish 生成)且与实际引用双向一致(M12/R42)
    import rs_asset
    refs = rs_asset.collect_project_refs(root)
    attr = notes / "素材归因.md"
    mb = []
    if not attr.is_file():
        mb.append("说明书:素材归因.md 未生成")
    else:
        listed = rs_asset.asset_ids_in(attr.read_text(encoding="utf-8", errors="replace"))
        wanted = {r["id"] for r in refs}
        if listed != wanted:
            mb.append(f"说明书:素材归因与引用不一致(文件多出 {sorted(listed - wanted)};"
                      f"引用缺登 {sorted(wanted - listed)})")
        nc = [r["id"] for r in refs if not r.get("commercial", True)]
        if nc:
            mb.append(f"商用:归因清单含不可商用素材 {nc}(不得进入交付)")
    add("素材归因.md 与实际引用双向一致", not mb,
        f"{len(refs)} 条引用" if attr.is_file() else "缺失", mb)

    # ⑥ 降级诚实:有 degraded 的产物必须在交付说明书列明原因与影响
    wl = _load_json(rs_paths.wordline_json(root))
    m6 = []
    if wl.get("degraded"):
        note_text = ""
        p = notes / "交付说明书.md"
        if p.is_file():
            note_text = p.read_text(encoding="utf-8", errors="replace")
        reasons = [str(r) for r in (wl.get("degradeReasons") or [])]
        hit = bool(reasons) and any(r.strip()[:12] and r.strip()[:12] in note_text
                                    for r in reasons)
        if "降级" not in note_text or not hit:
            m6.append("降级诚实:工程有降级标记,交付说明书未列明原因与影响")
    add("降级诚实(有 degraded 必须在交付说明书列明)", not m6,
        "无降级" if not wl.get("degraded") else ("已列明" if not m6 else "未列明"), m6)

    # ⑦ 变体矩阵(基线对账已核,这里只取结果展示)
    variant_miss = [m for m in (base.get("missing") or []) if "变体" in m]
    add("变体矩阵齐全(Logo × 画幅)", not variant_miss,
        "与 variants.json 逐条相符" if not variant_miss else "、".join(variant_miss[:2]),
        variant_miss)

    # ⑧ 安全区硬校验(清欠 #A6,判据实现在 rs_verify L0)
    import rs_verify  # noqa: PLC0415 — 延迟导入,保持 rs_ingest 轻量
    sa = rs_verify.check_safe_area(root)
    m8 = ([] if sa.get("ok") else [f"安全区:{d}" for d in sa.get("violations") or [sa.get("detail", "未过")]])
    add("安全区硬校验(卡片与字幕过平台 safeArea)", sa.get("ok", False),
        sa.get("skipped") or ("、".join(sa.get("platforms") or []) + ":过" if not m8
                              else sa.get("detail", "")), m8)
    return items, all_missing


def _write_publish_ledger(root: Path, deliver: Path, missing: list[str]) -> Path:
    """发布流水留痕(_内部状态/publish.json):文件清单 + sha256 + 缺项(ADR-0052 取舍项)。"""
    import hashlib
    from rs_common import write_text_atomic
    files: dict[str, str] = {}
    for f in sorted(deliver.rglob("*")):
        if f.is_file():
            files[f.relative_to(deliver).as_posix()] = hashlib.sha256(
                f.read_bytes()).hexdigest()
    doc = {"version": 1, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "deliver": rs_paths.p("deliver"), "files": files, "missing": missing}
    dst = rs_paths.resolve(root, "state") / "publish.json"
    write_text_atomic(dst, json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True))
    return dst


def publish(root: Path) -> dict:
    """`deliverables --publish`:成品/ 交付区发布(ADR-0052)。

    流程:前置只读区自检 → 工程区基线真对账 → 旧 成品/ 整区备份 → 重建成品/
    (拷贝非移动)→ §4.3 扩展对账 → 写 对账.md → 后置只读区自检 → 流水留痕。
    缺项不阻断发布(先把能交付的摆进去),但退出码非零(DELIVERABLES_INCOMPLETE)。
    """
    deliver = rs_paths.resolve(root, "deliver")
    # 1) 前置自检:工程文件混进交付区 → 拒发(不代删,先让用户/Agent 清理)
    pre = readonly_zone_violations(deliver)
    if pre:
        return {"ok": False, "code": "READONLY_ZONE_VIOLATED",
                "message": "", "path": str(deliver), "violations": pre,
                "missing": [], "files": [], "backup": None}
    # 2) 工程区基线对账(真对账;缺项照常点名,不阻断拷贝)
    base = build_deliverables(root)
    out = rs_paths.resolve(root, "output")
    # 3) 备份旧 成品/(整区 copytree;回滚点)
    backup: Path | None = None
    if deliver.is_dir() and any(deliver.iterdir()):
        backup = rs_paths.backup_dir(root, f"publish-{time.strftime('%Y%m%d-%H%M%S')}")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(deliver, backup)
    # 4) 重建 成品/
    if deliver.exists():
        shutil.rmtree(deliver)
    deliver.mkdir(parents=True, exist_ok=True)
    copied = _copy_deliverables(root, out, base.get("videos") or [], deliver)
    notes = deliver / "说明书"
    notes.mkdir(parents=True, exist_ok=True)
    from rs_common import write_text_atomic
    write_text_atomic(notes / "交付说明书.md", build_delivery_notes(root, base))
    has_decision = (out / "决策说明书.md").is_file()
    if has_decision:
        shutil.copy2(out / "决策说明书.md", notes / "决策说明书.md")
    # M12(ADR-0053/R45):素材归因清单——由统一索引汇总本工程**实际引用**的素材,
    # 含四许可字段与 aiGenerated 标识;rs_verify L0 对交付再跑一次双向对拍。
    import rs_asset
    asset_refs = rs_asset.collect_project_refs(root)
    write_text_atomic(notes / "素材归因.md", rs_asset.build_attribution_md(root, asset_refs))
    write_text_atomic(deliver / "半成品入口.md", build_pointer_md(root))
    # 5) §4.3 扩展对账 + 对账.md
    items, extended = _reconcile_publish(root, base, deliver, has_decision)
    write_text_atomic(deliver / "对账.md",
                      build_reconcile_md(root, base, items, copied))
    # 6) 后置自检:发布流程自身绝不能把工程文件带进交付区
    post = readonly_zone_violations(deliver)
    # 7) 流水留痕
    missing = list(base.get("missing") or []) + extended
    ledger = _write_publish_ledger(root, deliver, missing)
    ok = not missing and not post
    if post:
        code = "READONLY_ZONE_VIOLATED"
    elif ok:
        code = "PUBLISH_OK"
    else:
        code = "DELIVERABLES_INCOMPLETE"
    return {"ok": ok, "code": code, "path": str(deliver), "missing": missing,
            "items": items, "files": copied,
            "backup": str(backup) if backup else None, "ledger": str(ledger),
            "violations": post,
            "decisionNotes": str(out / "决策说明书.md") if has_decision else None}


# ---------------------------------------------------------------- 版权登记(方案 §5.5.3)

def check_copyright_registration(root: Path, *, max_single: float = COPYRIGHT_MAX_SINGLE_SOURCE,
                                 max_total: float = COPYRIGHT_MAX_QUOTE_TOTAL) -> dict:
    """引用素材登记检查(方案 §5.5.3:引用素材**必须登记**)。

    · 缺登记表 / 登记表无条目 → 硬失败(必须登记,不 WARN 放行);
    · 条目校验:file 在素材清单内、usage ∈ 枚举、quote 条目必须有 source 与时长;
    · 占比统计(素材侧预检):单部引用占比 > 30% 或总引用占比 > 70% → WARN
      (权威门禁在 rs_verify L0,对成片时长结算;此处只预警+留痕,不假绿也不越权)。
    """
    man = rs_paths.manifest_json(root)
    items: list[dict] = []
    if man.is_file():
        try:
            items = json.loads(man.read_text(encoding="utf-8")).get("items") or []
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            items = []
    doc = load_copyright(root)
    registered = doc.get("items") if isinstance(doc.get("items"), list) else []
    if not doc or not registered:
        return {"ok": False, "code": "COPYRIGHT_REGISTRATION_MISSING",
                "message": (f"缺引用素材登记表:{rs_paths.p('materials')}/{COPYRIGHT_JSON}"
                            "(方案 §5.5.3:引用素材必须登记 来源/时长/单部引用占比;"
                            "纯原创无引用工程登记 original 条目或 usage=original 即可)"),
                "expect": str(copyright_json(root))}

    known = {str(i.get("file")) for i in items}
    errors: list[str] = []
    warns: list[str] = []
    seen_files: set[str] = set()
    for k, it in enumerate(registered):
        if not isinstance(it, dict):
            errors.append(f"条目#{k} 不是对象")
            continue
        f = str(it.get("file") or "")
        usage = str(it.get("usage") or "")
        if not f:
            errors.append(f"条目#{k} 缺 file")
        elif known and f not in known:
            errors.append(f"条目#{k}:file {f} 不在素材清单(probe 未见)")
        if f in seen_files:
            errors.append(f"条目#{k}:file {f} 重复登记")
        seen_files.add(f)
        if usage not in USAGE_KINDS:
            errors.append(f"条目#{k}:usage {usage!r} 不在 {'/'.join(USAGE_KINDS)}")
        if usage == USAGE_QUOTE:
            if not str(it.get("source") or "").strip():
                errors.append(f"条目#{k}:quote 条目必须注明 source(单部作品标识)")
            q = it.get("quotedMs")
            if q is not None and (not isinstance(q, (int, float)) or q < 0):
                errors.append(f"条目#{k}:quotedMs 非法({q!r})")
            elif q is None and f not in dur_map(items):
                errors.append(f"条目#{k}:quote 条目缺 quotedMs 且素材无 probe 时长,无法计占比")

    stats = copyright_stats(items, registered)
    if stats.get("worstRatio") is not None and stats["worstRatio"] > max_single:
        warns.append(f"单部《{stats['worstSource']}》引用占比 {stats['worstRatio']:.0%} "
                     f"> {max_single:.0%}(rs_verify L0 将按成片时长硬判)")
    if stats.get("quoteRatio") is not None and stats["quoteRatio"] > max_total:
        warns.append(f"总引用占比 {stats['quoteRatio']:.0%} > {max_total:.0%}(同上)")
    if errors:
        return {"ok": False, "code": "COPYRIGHT_REGISTRATION_INVALID",
                "message": f"登记表校验失败 {len(errors)} 项:{';'.join(errors[:4])}",
                "errors": errors, "stats": stats}
    return {"ok": True, "code": "COPYRIGHT_OK", "errors": [], "warns": warns,
            "stats": stats, "registered": len(registered),
            "transformNote": str(doc.get("transformNote") or ""),
            "aiDisclosure": bool(doc.get("aiDisclosure"))}


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="scan",
                    choices=["scan", "deliverables", "green-ok", "decisions", "copyright"])
    ap.add_argument("root", help="工程根目录")
    ap.add_argument("--slug", default=None)
    ap.add_argument("--ratio", default="9x16", choices=list(RATIOS))
    ap.add_argument("--reason", default="", help="green-ok:用户对误判的说明(必填)")
    ap.add_argument("--publish", action="store_true",
                    help="deliverables:发布最终产物到 成品/(拷贝非移动;先备份旧 成品/,"
                         "ADR-0052 半成品/成品分离)")
    ap.add_argument("--allow-auto-matting", dest="allow_auto_matting", action="store_true",
                    help="scan:检测到幕布时先跑 rs_matting 质量门禁(ADR-0050),"
                         "PASS/WARN 才放行自动抠像;FAIL 阻断 MATTE_QUALITY_FAIL。"
                         "默认不启用——保持 ADR-0031「用户预处理」默认路径")
    a = ap.parse_args()
    root = Path(a.root)
    if not root.is_dir():
        return emit(False, "NO_PROJECT", f"工程根不存在:{root}", exit_code=2)
    if a.command == "deliverables":
        if a.publish:
            # ADR-0052:发布 = 拷贝/衍生到 成品/ + §4.3 真对账 + 只读区前后自检
            res = publish(root)
            ok = bool(res.get("ok"))
            viol = res.get("violations") or []
            miss = res.get("missing") or []
            if viol:
                msg = (f"成品/ 只读区被违反({len(viol)} 项):"
                       f"{';'.join(viol[:3])}" + ("…" if len(viol) > 3 else "")
                       + ";发布已拒绝(先清理工程文件,半成品只能待在工程区)")
            elif ok:
                msg = (f"已发布 → {res['path']}(对账全绿,{len(res.get('files') or [])} 个文件;"
                       f"流水 → {res.get('ledger')})")
            else:
                msg = (f"发布完成但对账不齐({len(miss)} 项):{'、'.join(miss[:3])}"
                       + ("…" if len(miss) > 3 else "")
                       + f";红标见 {rs_paths.p('deliver')}/对账.md")
            return emit(ok, res["code"], msg,
                        {k: v for k, v in res.items() if k not in ("ok", "code")},
                        exit_code=0 if ok else 4)
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
    if a.command == "copyright":
        # 版权门禁 S0 侧(方案 §5.5.3):引用素材必须登记;缺登记 → 非零退出
        res = check_copyright_registration(root)
        ok = bool(res.get("ok"))
        stats = res.get("stats") or {}
        if ok:
            msg = (f"版权登记 {res.get('registered', 0)} 条;"
                   f"总引用占比 {stats.get('quoteRatio') if stats.get('quoteRatio') is not None else '—'}")
            for w in res.get("warns") or []:
                msg += f";⚠ {w}"
            if not (res.get("transformNote") or "").strip():
                msg += ";⚠ 转化性说明(transformNote)缺失:L1/用户项,无法机械判定,交付前须人工补写(WARN 非 PASS,方案 R7)"
        else:
            msg = res.get("message", "")
        return emit(ok, res["code"], msg,
                    {k: v for k, v in res.items() if k not in ("ok", "code", "message")},
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
    res = scan(root, a.slug or root.name, a.ratio,
               allow_auto_matting=a.allow_auto_matting)
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
