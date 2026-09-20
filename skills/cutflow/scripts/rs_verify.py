"""分级自检(ADR-0016 / rules/verify.md)。

用法:
  rs_verify.py <工程根>                 # L0 机械自检(秒级,每次产出后都该跑)
  rs_verify.py <工程根> --level L1      # 追加"待目测清单"(不自动判定,交给 Agent/用户)
  rs_verify.py <工程根> --mark-first --result pass   # 记录首次检查已通过
  rs_verify.py <工程根> --status        # 只看验证状态

三级验证:
  L0 机械自检  纯脚本,秒级,**每一次**跑           — 能写成"给定输入必得同一输出"的判据
  L1 语义自检  需看图,首次 + 构图变更时           — 脚本只产出抽帧清单,判定权在 Agent
  L2 人工验收  用户显式要求                       — 所有权在用户

铁律:**输出必须携带 verifyLevel 与 firstCheckDone**;缺失即视为未验证。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rs_common  # noqa: E402
from rs_common import emit  # noqa: E402
import rs_ir  # noqa: E402
import rs_sync  # noqa: E402
import segmentation  # noqa: E402

CST = timezone(timedelta(hours=8))
PICTURE_STAGES = {"S3", "S4", "S5"}          # 任一被重跑 → 画面变了 → 提示 L1
DEFAULT_MAX_CHARS = segmentation.MAX_CHARS


def now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 状态

def verify_path(root: Path) -> Path:
    return root / "_state" / "verify.json"


def load_verify(root: Path) -> dict:
    p = verify_path(root)
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"version": 1, "firstCheck": {"done": False}, "lastL0": None, "userReview": None}


def save_verify(root: Path, doc: dict) -> None:
    p = verify_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")


def first_check_done(root: Path) -> bool:
    return bool((load_verify(root).get("firstCheck") or {}).get("done"))


def picture_changed(root: Path) -> list[str]:
    """哪些画面阶段**曾经做过、现在失效了**。

    "从未记录状态"不算变更 —— 那只是阶段没跑过。**只有 stale/failed 才算"改过画面"的证据。**
    单一实现放 rs_run(它才有阶段求值能力);这里委托过去,避免两套真相。
    """
    try:
        import rs_run
        changed = []
        for sid in ("S3", "S4", "S5"):
            if rs_run.read_state(root, sid) is None:
                continue
            st = next(s for s in rs_run.spec() if s["id"] == sid)
            if rs_run.evaluate(root, st)["status"] != "done":
                changed.append(sid)
        return sorted(changed)
    except Exception:  # noqa: BLE001 — 兜底:读 pipeline.json 快照
        p = root / "05_ir" / "pipeline.json"
        if not p.is_file():
            return []
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        return sorted(sid for sid, st in (doc.get("stages") or {}).items()
                      if sid in PICTURE_STAGES and (st or {}).get("status") in ("stale", "failed"))


# ---------------------------------------------------------------- L0 判据

def _ratio_of(root: Path) -> str:
    """从 IR 画布反查比例。新画幅(3x4)必须走查表,不能拿字符串比较 1080x1920。"""
    p = root / "05_ir" / "project.json"
    if p.is_file():
        try:
            c = (json.loads(p.read_text(encoding="utf-8")) or {}).get("canvas") or {}
            from rs_common import ratio_for_canvas
            return ratio_for_canvas(c.get("width", 1080), c.get("height", 1920))
        except Exception:  # noqa: BLE001 — 画布异常 → 退回默认比例,不阻塞自检
            pass
    return "9x16"


def _max_chars(root: Path) -> int:
    ratio = _ratio_of(root)
    if ratio not in DEFAULT_MAX_CHARS:
        ratio = "9x16"
    p = root / "05_ir" / "pipeline.json"
    if p.is_file():
        try:
            params = json.loads(p.read_text(encoding="utf-8")).get("params") or {}
            # P12-1:params.maxChars 支持两种形态 —— 按画幅字典(默认快照)或
            # brief 显式声明的全局整数(每卡字数:N);两种都消费,读不到回退默认。
            mc = params.get("maxChars")
            v = mc.get(ratio) if isinstance(mc, dict) else mc
            if isinstance(v, int) and 4 <= v <= 40:
                return v
        except json.JSONDecodeError:
            pass
    return DEFAULT_MAX_CHARS[ratio]


def check_ir(root: Path) -> dict:
    p = root / "05_ir" / "project.json"
    if not p.is_file():
        return {"name": "IR 可解析且校验通过", "ok": True, "skipped": "尚未生成 IR"}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"name": "IR 可解析且校验通过", "ok": False, "detail": f"JSON 解析失败:{exc}"}
    errs = rs_ir.validate(doc, root)
    return {"name": "IR 可解析且校验通过", "ok": not errs,
            "detail": "" if not errs else "; ".join(errs[:5]), "errors": errs}


def check_wordline(root: Path) -> dict:
    p = root / "05_ir" / "wordline.json"
    if not p.is_file():
        return {"name": "Wordline 存在且单调", "ok": False, "detail": "缺 05_ir/wordline.json"}
    try:
        wl = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"name": "Wordline 存在且单调", "ok": False, "detail": f"JSON 解析失败:{exc}"}
    chars = wl.get("chars") or []
    if not chars:
        return {"name": "Wordline 存在且单调", "ok": False, "detail": "chars 为空"}
    starts = [int(c["startMs"]) for c in chars]
    mono = starts == sorted(starts)
    bad_len = [c["i"] for c in chars if int(c["endMs"]) <= int(c["startMs"])]
    st = wl.get("stats") or {}
    ok = mono and not bad_len
    detail = []
    if not mono:
        detail.append("时间戳非单调")
    if bad_len:
        detail.append(f"{len(bad_len)} 个字的 endMs<=startMs")
    if wl.get("degraded"):
        detail.append("⚠ 降级模式:" + ";".join(wl.get("degradeReasons") or [])[:80])
    return {"name": "Wordline 存在且单调", "ok": ok, "detail": " / ".join(detail),
            "stats": st, "degraded": bool(wl.get("degraded"))}


def check_cutlist(root: Path) -> dict:
    p = root / "04_cut" / "cutlist.applied.json"
    if not p.is_file():
        p = root / "04_cut" / "cutlist.json"
    if not p.is_file():
        return {"name": "粗剪 guard 全过", "ok": True, "skipped": "尚未做粗剪"}
    try:
        cl = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"name": "粗剪 guard 全过", "ok": False, "detail": f"JSON 解析失败:{exc}"}
    removes = [c for c in cl.get("cuts", []) if c.get("action") == "remove"]

    bad = [c["id"] for c in removes if not rs_common.guard_passed(c.get("guard"))]
    total = int(cl.get("srcTotalMs") or 0)
    keep = cl.get("keep") or []
    cover_ok = bool(keep) and keep[0][0] == 0 and keep[-1][1] == total
    ok = not bad and cover_ok
    detail = []
    if bad:
        detail.append(f"guard 未过却标 remove:{','.join(bad)}")
    if not cover_ok:
        detail.append("keep 区间未完整覆盖源时长")
    return {"name": "粗剪 guard 全过", "ok": ok, "detail": "; ".join(detail),
            "removeCount": len(removes), "removedMs": cl.get("removedMs")}


def check_subtitles(root: Path) -> dict:
    ass = root / "06_output" / "subtitles.ass"
    if not ass.is_file():
        # B9(BUGREPORT-20260913):阶段式运行(--only S2 等)时字幕尚未生成是
        # **正常中间态**,标"未涉及"而不是 ✗ —— 全量 L0 的误报会淹没真故障。
        return {"name": "字幕合规(字数/CPS/时长/不重叠)", "ok": True,
                "skipped": "尚未生成字幕(S7 未跑,中间态不算失败)"}
    events = rs_sync.parse_ass(ass)
    if not events:
        return {"name": "字幕合规(字数/CPS/时长/不重叠)", "ok": False, "detail": "ASS 无 Dialogue 事件"}
    max_chars = _max_chars(root)
    cards = []
    for i, e in enumerate(events):
        txt = e["text"].replace(" ", "")
        dur_ms = int(round((e["end"] - e["start"]) * 1000))
        cards.append({"i": i, "text": e["text"], "chars": len(txt),
                      "startMs": int(e["start"] * 1000), "endMs": int(e["end"] * 1000),
                      "durMs": dur_ms,
                      "cps": round(len(txt) / (dur_ms / 1000.0), 2) if dur_ms else 0.0})
    viol = segmentation.check_constraints(cards, max_chars, segmentation.cps_max_for(max_chars))
    over = [v for v in viol if "时长" in v and ">" in v]
    # v0.11 实测修正:rules/subtitles.md §8 —— >7s 是硬失败,<0.83s 是**软告警**
    # ("残余项可由 sync_report.md 解释":必并余量耗尽时保留字级精确时间属预期)。
    # 旧实现把短卡也当 L0 硬失败,与规范自相矛盾。
    soft = [v for v in viol if "时长" in v and "<" in v]
    hard = [v for v in viol if v not in soft]
    return {"name": "字幕合规(字数/CPS/时长/不重叠)", "ok": not hard,
            "detail": "; ".join((hard or soft)[:5]), "eventCount": len(events),
            "ratio": _ratio_of(root), "maxChars": max_chars,
            "violations": viol, "hardDuration": over, "softWarnings": soft}


def check_alignment(root: Path) -> dict:
    ass = root / "06_output" / "subtitles.ass"
    wl_path = root / "05_ir" / "wordline.final.json"
    if not wl_path.is_file():
        wl_path = root / "05_ir" / "wordline.json"
    if not ass.is_file() or not wl_path.is_file():
        return {"name": "字幕↔Wordline 对齐", "ok": True, "skipped": "缺字幕或 Wordline"}
    wl = json.loads(wl_path.read_text(encoding="utf-8"))
    events = rs_sync.parse_ass(ass)
    rows = rs_sync.check_offsets(events, wl)
    res = rs_sync.summarize(rows, events)
    return {"name": "字幕↔Wordline 对齐", "ok": bool(res["pass"]),
            "detail": f"中位数 {res['medianMs']}ms / 95 分位 {res['p95Ms']}ms"
                      + (f";未匹配 {len(res['unmatched'])}" if res["unmatched"] else ""),
            "summary": res}


def list_videos(root: Path) -> list[Path]:
    """全部成片路径(P10b-1):06_output 顶层 + final/ + branded/ 独占子目录。

    S8/S5 的交付成片已各落独占子目录;这里合并列出(按 mtime),
    让 L0 产物检查 / 体检 / L1 抽帧清单在新旧落点上都找得到成片。
    """
    out = root / "06_output"
    if not out.is_dir():
        return []
    vids: list[Path] = []
    for pat in ("*.mp4", "final/*.mp4", "branded/*.mp4"):
        vids.extend(out.glob(pat))
    return sorted(vids, key=lambda p: p.stat().st_mtime)


def check_artifacts(root: Path) -> dict:
    out = root / "06_output"
    vids = list_videos(root)
    names = sorted(p.relative_to(out).as_posix() for p in vids)
    return {"name": "产物存在", "ok": bool(vids), "skipped": None if vids else "尚无成片",
            "videos": names}


def check_qc(root: Path) -> dict:
    """B5(v0.12):成片体检(黑帧/冻结/VFR/响度)纳入 L0。

    rules/verify.md 早已把它列为 L0 判据,但 rs_verify 从不跑——只有显式
    `rs_sync --qc` 的路径才体检,spec 与实现脱节。这里补齐:对**最新**成片跑
    rs_sync.run_qc。ffmpeg/ffprobe 异常 → skipped 留痕(闸的缺席必须显式,
    ADR-0021 失败语义),不硬失败;体检判 FAIL 才 ok=False。
    """
    name = "成片体检(黑帧/冻结/VFR/响度)"
    videos = list_videos(root)
    if not videos:
        return {"name": name, "ok": True, "skipped": "尚无成片"}
    try:
        qc = rs_sync.run_qc(videos[-1])
    except SystemExit as exc:
        return {"name": name, "ok": True, "skipped": f"体检工具不可用:{exc}", "video": videos[-1].name}
    except Exception as exc:  # noqa: BLE001 — 闸缺席留痕,不阻塞其余 L0
        return {"name": name, "ok": True, "skipped": f"体检执行失败:{exc}", "video": videos[-1].name}
    if qc.get("skipped"):
        return {"name": name, "ok": True, "skipped": qc["skipped"], "video": videos[-1].name}
    failed = sorted(k for k, v in (qc.get("checks") or {}).items()
                    if isinstance(v, dict) and v.get("pass") is False)
    return {"name": name, "ok": bool(qc.get("pass")),
            "detail": "" if qc.get("pass") else f"{videos[-1].name}:FAIL {','.join(failed)}",
            "video": videos[-1].name, "checks": qc.get("checks")}


def check_greenscreen(root: Path) -> dict:
    """ADR-0031:v0.14 起 CutFlow 不做抠像,素材不得仍含未处理的绿幕/蓝幕。

    读 S0 的 `01_materials/manifest.json`(rs_ingest 已逐条检测),命中且无用户放行说明
    → 硬失败 —— 交付前的最后一处闸,防止"用户忘了预处理"的绿幕素材被剪进成片。
    旧工程 manifest 无 `greenScreen` 字段 → skipped(不误伤历史工程)。
    """
    name = "素材无未处理的绿幕/蓝幕(ADR-0031)"
    man = root / "01_materials" / "manifest.json"
    if not man.is_file():
        return {"name": name, "ok": True, "skipped": "尚无 01_materials/manifest.json(S0 未跑)"}
    try:
        doc = json.loads(man.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"name": name, "ok": False, "detail": f"manifest 解析失败:{exc}"}
    items = doc.get("items") or []
    if not any("greenScreen" in i for i in items):
        return {"name": name, "ok": True, "skipped": "素材未做幕布检测(旧工程,跳过)"}
    flagged = [i for i in items if (i.get("greenScreen") or {}).get("detected")]
    if not flagged:
        return {"name": name, "ok": True, "detail": "未检出幕布素材"}
    override = doc.get("greenOverride")
    if not override:
        try:
            import rs_greenscreen
            override = rs_greenscreen.read_override(root)
        except Exception:  # noqa: BLE001 — 放行文件读失败按未放行处理(宁可拦)
            override = None
    ok = bool(override)
    return {"name": name, "ok": ok,
            "detail": (f"已按用户说明放行:{str(override)[:60]}" if ok
                       else f"{len(flagged)} 条素材仍含幕布:"
                            f"{','.join(i['file'] for i in flagged[:3])}"
                            "(请先自行抠像+合成背景,或 `rs_ingest.py green-ok` 放行)"),
            "flagged": [i["file"] for i in flagged]}


L0_CHECKS = (check_ir, check_wordline, check_cutlist, check_greenscreen, check_subtitles,
             check_alignment, check_artifacts, check_qc)


def collect_l0(root: Path) -> dict:
    checks = [fn(root) for fn in L0_CHECKS]
    hard = [c for c in checks if not c.get("skipped")]
    failed = [c for c in hard if not c["ok"]]
    # REVIEW-20260916 根因 5:L0 是机械自检,只保证产物自洽,不保证内容正确。
    # scope 显式写出,防"通过=没问题"的表述漂移。
    return {"level": "L0", "scope": "mechanical",
            "scopeNote": "L0=机械自检(产物自洽);内容正确性需 rs_diagnose(contentVerdict)",
            "checks": checks,
            "pass": not failed,
            "failed": [c["name"] for c in failed],
            "skipped": [c["name"] for c in checks if c.get("skipped")],
            "at": now()}


# ---------------------------------------------------------------- L1 清单

L1_CHECKLIST = [
    "画面无黑帧 / 花屏 / 绿幕残留",
    "字幕未压脸、未出安全区(9:16 底 25%/顶 12%;3:4 底 18%/顶 10%;16:9 底 16%/顶 8%)",
    "信息卡/动画卡内容在安全带内(顶部 12% / 底部 30% / 左右 8%)",
    "Logo 未进入字幕带、未遮挡关键信息",
    "转场无跳变、无音画错位可感知",
    "封面过安全区、文字可读",
]


def l1_payload(root: Path) -> dict:
    out = root / "06_output"
    vids = list_videos(root)
    names = sorted(p.relative_to(out).as_posix() for p in vids)
    cmds = [f"python skills/cutflow/scripts/rs_bench.py 06_output/{v} "
            f"--ir 05_ir/project.json --out 06_output/bench_{Path(v).stem}.png" for v in names]
    return {"level": "L1", "needsAgentReview": True,
            "checklist": L1_CHECKLIST, "benchCommands": cmds,
            "note": "L1 判定权在 Agent/用户;脚本只产出清单与抽帧命令,不自动判定"}


def write_report(res: dict, path: Path, l1: dict | None = None) -> None:
    scope_note = ("L0 通过 = 机械自检通过(产物自洽),**不等于内容正确**;"
                  "内容正确性请跑 rs_diagnose 或 rs_verify --content"
                  if res.get("scope") == "mechanical" and res["pass"] else "")
    lines = [f"# 自检报告({res['level']})", "",
             f"- 时间:{res['at']}", f"- 判定:**{'通过' if res['pass'] else '未通过'}**"]
    if scope_note:
        lines.append(f"- 边界:{scope_note}")
    lines.append("")
    lines += ["", "| 检查项 | 结果 | 说明 |", "|---|---|---|"]
    for c in res["checks"]:
        mark = "— 未涉及" if c.get("skipped") else ("✓" if c["ok"] else "✗")
        lines.append(f"| {c['name']} | {mark} | {c.get('detail') or c.get('skipped') or ''} |")
    if res.get("failed"):
        lines += ["", "## 未通过项", ""] + [f"- {x}" for x in res["failed"]]
    if l1:
        lines += ["", "## L1 待目测清单(判定权在 Agent/用户)", ""]
        lines += [f"- [ ] {x}" for x in l1["checklist"]]
        if l1["benchCommands"]:
            lines += ["", "```powershell"] + l1["benchCommands"] + ["```"]
        lines += ["", f"> {l1['note']}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".")
    ap.add_argument("--level", default="L0", choices=["L0", "L1"])
    ap.add_argument("--content", dest="content", action="store_true",
                    help="追加成片内容诊断(ADR-0032 rs_diagnose):字幕↔语音时间轴/"
                         "音画同步/错剪语义;需要成片;结果记入 contentVerdict 与诊断台账")
    ap.add_argument("--out", default="06_output")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--mark-first", dest="mark_first", action="store_true")
    ap.add_argument("--result", default="pass", choices=["pass", "fail"])
    ap.add_argument("--content-budget", dest="content_budget", type=float, default=900.0,
                    help="--content 诊断预算秒(默认 900;超时输出阶段性结论)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    if not root.is_dir():
        return emit(False, "NO_PROJECT", f"工程目录不存在:{root}", exit_code=2)

    if a.status:
        st = load_verify(root)
        first = bool((st.get("firstCheck") or {}).get("done"))
        return emit(True, "VERIFY_STATUS",
                    f"首次检查:{'已完成' if first else '未完成'}"
                    + (f";画面变更待复核:{','.join(picture_changed(root))}" if picture_changed(root) else ""),
                    {"firstCheckDone": first, "state": st, "pictureChanged": picture_changed(root),
                     "verifyLevel": "L0"})

    if a.mark_first:
        st = load_verify(root)
        st["firstCheck"] = {"done": True, "at": now(), "level": "L1", "result": a.result}
        st["lastL0"] = {"at": now(), "level": "L0", "result": a.result}
        save_verify(root, st)
        return emit(True, "FIRST_CHECK_MARKED",
                    f"首次检查已记录({a.result})", {"firstCheckDone": True})

    res = collect_l0(root)
    content_verdict = None
    if a.content:
        videos = list_videos(root)
        if not videos:
            content_verdict = {"verdict": "indetermined", "note": "无成片,内容诊断未运行"}
        else:
            import rs_diagnose
            cfg_path = Path(__file__).resolve().parents[3] / "config.json"
            cfg = {}
            if cfg_path.is_file():
                try:
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    cfg = {}
            budget = float(a.content_budget) if a.content_budget else 900.0
            diag = rs_diagnose.diagnose(videos[-1], root, None, None, None, budget,
                                        False, root / "06_output", cfg)
            if "verdict" not in diag:
                content_verdict = {"verdict": "indetermined",
                                   "note": str(diag.get("message", ""))[:200]}
            else:
                content_verdict = {"verdict": diag["verdict"],
                                   "suspiciousSpans": diag.get("suspiciousSpans") or [],
                                   "report": diag.get("report"),
                                   "ledger": diag.get("ledger"),
                                   "elapsedS": diag.get("elapsedS")}
                res["contentVerdict"] = content_verdict
                if diag["verdict"] == "issues":
                    res["pass"] = False
                    res["failed"].append("内容诊断(rs_diagnose)")
    l1 = l1_payload(root) if a.level == "L1" else None
    out = root / a.out
    out.mkdir(parents=True, exist_ok=True)
    write_report(res, out / "verify_report.md", l1)

    st = load_verify(root)
    st["lastL0"] = {"at": res["at"], "level": "L0", "result": "pass" if res["pass"] else "fail"}
    if l1 and res["pass"]:
        st["firstCheck"] = {"done": True, "at": res["at"], "level": "L1", "result": "pending-review"}
    save_verify(root, st)

    first_done = bool((st.get("firstCheck") or {}).get("done"))
    msg = f"L0 {'通过' if res['pass'] else '未通过'}({len(res['checks']) - len(res['skipped'])} 项)"
    if res["failed"]:
        msg += f";未过:{','.join(res['failed'][:3])}"
    if content_verdict is not None:
        msg += f";内容诊断 {content_verdict['verdict']}" + (
            f"({len(content_verdict.get('suspiciousSpans') or [])} 处疑点)"
            if content_verdict.get("suspiciousSpans") else "")
    if l1:
        msg += ";已生成 L1 待目测清单(判定权在 Agent/用户)"
    data = {"verifyLevel": "L1" if l1 else "L0", "firstCheckDone": first_done,
            **res, "report": str(out / "verify_report.md"),
            "pictureChanged": picture_changed(root)}
    if l1:
        data["l1"] = l1
    return emit(res["pass"], "VERIFY_OK" if res["pass"] else "VERIFY_FAIL", msg, data,
                exit_code=0 if res["pass"] else 4)


if __name__ == "__main__":
    sys.exit(main())
