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
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rs_common  # noqa: E402
from rs_common import emit  # noqa: E402
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量
import rs_ir  # noqa: E402
import rs_sync  # noqa: E402
import segmentation  # noqa: E402

CST = timezone(timedelta(hours=8))
PICTURE_STAGES = {"S3", "S4", "S5"}          # 任一被重跑 → 画面变了 → 提示 L1
DEFAULT_MAX_CHARS = segmentation.MAX_CHARS

# ---------------------------------------------------------------- 版权门禁(方案 §5.5.3,初值可配)
# 初值 = 方案建议档;覆盖优先级:CLI 旗标 > pipeline.json params > 这里。
# 判据全部机械可算(时长占比);「转化性/纯剧透替代/AI 标识」机械不可判,
# 一律标 L1/用户项(l1Pending),WARN 非 PASS,绝不假绿(方案 R7)。
COPYRIGHT_MAX_SINGLE_SOURCE = 0.30   # 单部引用占比 ≤30%(初值)
COPYRIGHT_MAX_QUOTE_TOTAL = 0.70     # 总引用时长占比 ≤70%
COPYRIGHT_MIN_COMMENTARY = 0.25      # 原创解说轨时长 ≥25%(解说型)
COPYRIGHT_VIDEO_TYPES = ("drama",)   # 声明这些 videoType 的工程必须过版权门禁
                                     # (film-commentary 暂无独立注册键,随 drama 登记,见 registry._doc)
COPYRIGHT_JSON = "copyright.json"    # 登记表(01_原始素材/copyright.json,rs_ingest 同名常量)


def now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 状态

def verify_path(root: Path) -> Path:
    return rs_paths.verify_json(root)


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
            st = next(s for s in rs_run.spec(root) if s["id"] == sid)
            if rs_run.evaluate(root, st)["status"] != "done":
                changed.append(sid)
        return sorted(changed)
    except Exception:  # noqa: BLE001 — 兜底:读 pipeline.json 快照
        p = rs_paths.pipeline_json(root)
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
    p = rs_paths.project_json(root)
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
    p = rs_paths.pipeline_json(root)
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
    p = rs_paths.project_json(root)
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
    p = rs_paths.wordline_json(root)
    if not p.is_file():
        return {"name": "Wordline 存在且单调", "ok": False,
                "detail": f"缺 {rs_paths.p('timeline')}/wordline.json"}
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
    p = rs_paths.resolve(root, "cut") / "cutlist.applied.json"
    if not p.is_file():
        p = rs_paths.resolve(root, "cut") / "cutlist.json"
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
    ass = rs_paths.resolve(root, "output") / "subtitles.ass"
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
    ass = rs_paths.resolve(root, "output") / "subtitles.ass"
    wl_path = rs_paths.wordline_json(root, final=True)
    if not wl_path.is_file():
        wl_path = rs_paths.wordline_json(root)
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
    """全部成片路径(P10b-1):06_成片输出 顶层 + final/ + branded/ 独占子目录。

    S8/S5 的交付成片已各落独占子目录;这里合并列出(按 mtime),
    让 L0 产物检查 / 体检 / L1 抽帧清单在新旧落点上都找得到成片。
    """
    out = rs_paths.resolve(root, "output")
    if not out.is_dir():
        return []
    vids: list[Path] = []
    for pat in ("*.mp4", "final/*.mp4", "branded/*.mp4"):
        vids.extend(out.glob(pat))
    return sorted(vids, key=lambda p: p.stat().st_mtime)


def check_artifacts(root: Path) -> dict:
    out = rs_paths.resolve(root, "output")
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

    读 S0 的 `01_原始素材/manifest.json`(rs_ingest 已逐条检测),命中且无用户放行说明
    → 硬失败 —— 交付前的最后一处闸,防止"用户忘了预处理"的绿幕素材被剪进成片。
    旧工程 manifest 无 `greenScreen` 字段 → skipped(不误伤历史工程)。
    """
    name = "素材无未处理的绿幕/蓝幕(ADR-0031)"
    man = rs_paths.manifest_json(root)
    if not man.is_file():
        return {"name": name, "ok": True,
                "skipped": f"尚无 {rs_paths.p('materials')}/manifest.json(S0 未跑)"}
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


def _intent_video_type(root: Path) -> str:
    """intent_decisions.json 的 resolved.videoType(缺/坏 → 空串;不引 rs_run,保持轻量)。"""
    p = rs_paths.resolve(root, "brief") / "intent_decisions.json"
    if not p.is_file():
        return ""
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return ""
    resolved = doc.get("resolved") if isinstance(doc.get("resolved"), dict) else {}
    return str(resolved.get("videoType") or "")


def _copyright_thresholds(root: Path, max_single: float | None, max_total: float | None,
                          min_commentary: float | None) -> tuple[float, float, float]:
    """判据初值解析:入参 > pipeline.json params > 模块常量(CLI/配置双通道可覆盖)。"""
    params: dict = {}
    p = rs_paths.pipeline_json(root)
    if p.is_file():
        try:
            params = (json.loads(p.read_text(encoding="utf-8")) or {}).get("params") or {}
        except json.JSONDecodeError:
            params = {}

    def _pick(v_cli, key: str, default: float) -> float:
        v = v_cli if isinstance(v_cli, (int, float)) else params.get(key)
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = default
        return v if 0.0 < v <= 1.0 else default

    return (_pick(max_single, "copyrightMaxSingleSource", COPYRIGHT_MAX_SINGLE_SOURCE),
            _pick(max_total, "copyrightMaxQuoteTotal", COPYRIGHT_MAX_QUOTE_TOTAL),
            _pick(min_commentary, "copyrightMinCommentary", COPYRIGHT_MIN_COMMENTARY))


def _ir_timeline_ms(root: Path) -> tuple[list[tuple[str, int]], int]:
    """IR 主画面轨道 → [(素材文件名, 时长ms)] 与总时长(占比分母第一优先)。

    只统计 src 在素材目录下的视频轨 clip(卡片/贴片在 03_创作素材,不计引用占比);
    IR 缺席 → ([], 0),调用方退回成片实测/wordline 口径。
    """
    pj = rs_paths.project_json(root)
    if not pj.is_file():
        return [], 0
    try:
        doc = json.loads(pj.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [], 0
    # 新名 + 旧名(过渡期工程;旧英文名经 LEGACY_ALIASES 反查取用,不写裸字面量——ADR-0046)
    _legacy_mat = next((old for old, key in rs_paths.LEGACY_ALIASES.items() if key == "materials"), "")
    mat_names = {rs_paths.p("materials"), _legacy_mat}
    clips: list[tuple[str, int]] = []
    for tr in doc.get("tracks") or []:
        if tr.get("kind") != "video":
            continue
        for c in tr.get("clips") or []:
            first = str(c.get("src") or "").replace("\\", "/").partition("/")[0]
            if first in mat_names:
                clips.append((Path(str(c.get("src"))).name, int(c.get("durationMs") or 0)))
    return clips, sum(ms for _, ms in clips)


def _wordline_commentary_ms(root: Path) -> int:
    """原创解说轨时长(ms)= wordline 逐句(末字 endMs − 首字 startMs)之和。

    句级带 voice 标记时,**原声对白句不计入**(那是原片声音,不是原创解说);
    无标记退回全量(纯解说工程)。wordline 缺席 → 0(判据缺席,调用方留痕)。
    """
    p = rs_paths.wordline_json(root)
    if not p.is_file():
        return 0
    try:
        wl = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    chars = wl.get("chars") or []
    if not chars:
        return 0
    sents = wl.get("sentences") or []
    if sents:
        has_voice = any(str(s.get("voice") or "") == "dialogue" for s in sents)
        total = 0
        for s in sents:
            if has_voice and str(s.get("voice") or "") == "dialogue":
                continue
            a, b = (s.get("span") or [0, 0])[:2]
            a, b = max(0, int(a)), min(int(b), len(chars))
            if b > a:
                total += max(0, int(chars[b - 1]["endMs"]) - int(chars[a]["startMs"]))
        if total > 0:
            return total
    return max(0, int(chars[-1]["endMs"]) - int(chars[0]["startMs"]))


def check_copyright(root: Path, max_single: float | None = None,
                    max_total: float | None = None,
                    min_commentary: float | None = None) -> dict:
    """版权门禁 L0(方案 §5.5.3):引用登记完整 + 三条时长占比判据 + L1 显式标注。

    启用条件(数据驱动,非类型分支):工程声明 drama/影视解说(intent videoType),
    或存在登记表 copyright.json。其余工程 skipped(判据未声明,不误伤全库)。

    机械判据(任一不达标 → ok=False,L0 非零退出):
      · IR 在用的素材文件必须逐条登记(方案:引用素材必须登记);
      · 单部引用占比 ≤ max_single(初值 30%);总引用时长占比 ≤ max_total(初值 70%);
      · 解说型(kind=commentary,缺省):原创解说轨时长 ≥ min_commentary(初值 25%)。

    L1/用户项(机械不可判 → l1Pending 列表,WARN 非 PASS,绝不假绿,方案 R7):
      转化性说明缺失/需人工确认、「N 分钟看完」式纯剧透替代、AI 标识。
    """
    name = (f"版权门禁(单部引用≤{max_single if isinstance(max_single, float) else COPYRIGHT_MAX_SINGLE_SOURCE:.0%}/"
            f"总引用≤{max_total if isinstance(max_total, float) else COPYRIGHT_MAX_QUOTE_TOTAL:.0%}/"
            f"解说轨≥{min_commentary if isinstance(min_commentary, float) else COPYRIGHT_MIN_COMMENTARY:.0%})")
    mx_s, mx_t, mn_c = _copyright_thresholds(root, max_single, max_total, min_commentary)
    name = (f"版权门禁(单部引用≤{mx_s:.0%}/总引用≤{mx_t:.0%}/解说轨≥{mn_c:.0%})")

    cp_path = rs_paths.resolve(root, "materials") / COPYRIGHT_JSON
    vt = _intent_video_type(root)
    doc: dict = {}
    if cp_path.is_file():
        try:
            doc = json.loads(cp_path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict):
                doc = {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {"name": name, "ok": False, "detail": f"登记表解析失败:{cp_path}"}
    registered = doc.get("items") if isinstance(doc.get("items"), list) else []
    if vt not in COPYRIGHT_VIDEO_TYPES and not registered:
        return {"name": name, "ok": True,
                "skipped": "非引用型工程(未声明 drama/影视解说,无登记表),判据未启用"}

    # ---- 分母:IR 主画面轨道总时长 → 成片实测 → wordline 跨度(逐级退回,来源留痕)
    clips, total_ms = _ir_timeline_ms(root)
    basis = "ir"
    if total_ms <= 0:
        try:
            vids = list_videos(root)
            if vids:
                total_ms = int(round(rs_sync.media_duration_s(vids[-1]) * 1000))
                basis = "final-video"
        except Exception:  # noqa: BLE001 — ffprobe 缺失按缺席处理
            total_ms = 0
    if total_ms <= 0:
        wl_ms = _wordline_commentary_ms(root)
        if wl_ms > 0:
            total_ms, basis = wl_ms, "wordline"
    if total_ms <= 0:
        return {"name": name, "ok": True,
                "skipped": "无 IR/成片/wordline 可作占比分母(中间态不算失败)"}

    viols: list[str] = []
    l1_pending: list[str] = []
    # ---- ① 登记完整性:IR 在用的素材必须逐条登记(方案:引用素材必须登记)
    by_file = {str(it.get("file") or ""): it for it in registered if isinstance(it, dict)}
    unregistered = sorted({f for f, _ in clips if f and f not in by_file})
    if unregistered:
        viols.append(f"{len(unregistered)} 个在用素材未登记(必须登记):"
                     f"{','.join(unregistered[:4])}")
    # ---- ② 单部/总引用占比(按 IR clip 时长结算;未登记素材按引用计,宁可拦错不放过)
    per_source: dict[str, int] = {}
    quote_total = 0
    for f, ms in clips:
        it = by_file.get(f) or {}
        if ms <= 0:
            continue
        usage = str(it.get("usage") or "")
        if not usage or usage == "quote":     # 未登记/引用 → 计入引用时长
            src = str(it.get("source") or f"{f}(未注明来源)")
            per_source[src] = per_source.get(src, 0) + ms
            quote_total += ms
    single_ratio = (max(per_source.values()) / total_ms) if per_source else 0.0
    total_ratio = quote_total / total_ms
    if single_ratio > mx_s + 1e-9:
        worst = max(per_source, key=per_source.get)
        viols.append(f"单部《{worst}》引用占比 {single_ratio:.1%} > {mx_s:.0%}"
                     f"({per_source[worst]}ms/{total_ms}ms,分母 {basis})")
    if total_ratio > mx_t + 1e-9:
        viols.append(f"总引用时长占比 {total_ratio:.1%} > {mx_t:.0%}"
                     f"({quote_total}ms/{total_ms}ms,分母 {basis})")
    # ---- ③ 原创解说轨(kind=commentary 缺省;纯短剧剧情号显式声明 kind=drama 豁免)
    kind = str(doc.get("kind") or "commentary")
    commentary_ratio = None
    if kind != "drama":
        c_ms = _wordline_commentary_ms(root)
        if c_ms > 0:
            commentary_ratio = c_ms / total_ms
            if commentary_ratio < mn_c - 1e-9:
                viols.append(f"原创解说轨时长占比 {commentary_ratio:.1%} < {mn_c:.0%}"
                             f"({c_ms}ms/{total_ms}ms,分母 {basis})")
        else:
            l1_pending.append("解说轨时长无法核算(wordline 缺席)—— L1 目测确认解说轨存在且达标")
    # ---- ④ L1/用户项(机械不可判 → 显式 WARN 标注,不假绿,方案 R7)
    if not str(doc.get("transformNote") or "").strip():
        l1_pending.append("转化性说明缺失(登记表 transformNote)—— L1/用户项:原创观点/批评"
                          "是否构成转化性使用,机械不可判,须人工确认")
    else:
        l1_pending.append("转化性判定属 L1:登记表已含说明,仍须人工确认(机械不可判)")
    l1_pending.append("「N 分钟看完」式纯剧透替代机械不可完全判定 —— L1 目测:解说须含原创观点,"
                      "不得替代原片叙事")
    if not doc.get("aiDisclosure"):
        l1_pending.append("AI 参与标识未声明(登记表 aiDisclosure)—— 用户项:平台要求须标识")

    detail = "; ".join(viols[:4])
    if not viols:
        detail = (f"单部 {single_ratio:.1%} / 总引用 {total_ratio:.1%}"
                  + (f" / 解说轨 {commentary_ratio:.1%}" if commentary_ratio is not None else "")
                  + f"(分母 {basis} {total_ms}ms)")
        if l1_pending:
            detail += f";⚠ {len(l1_pending)} 项 L1/用户项待人工判定(WARN 非 PASS)"
    return {"name": name, "ok": not viols, "detail": detail,
            "violations": viols, "l1Pending": l1_pending,
            "ratios": {"singleSource": round(single_ratio, 4),
                       "quoteTotal": round(total_ratio, 4),
                       "commentary": None if commentary_ratio is None else round(commentary_ratio, 4)},
            "thresholds": {"maxSingleSource": mx_s, "maxQuoteTotal": mx_t,
                           "minCommentary": mn_c},
            "basis": basis, "denominatorMs": total_ms, "kind": kind,
            "registered": len(registered)}


def _platform_safe_areas(root: Path) -> list[dict]:
    """工程适用平台的 safeArea 数值(§4.3 清欠 #A6;templates/platforms.json 单一事实源)。

    口径:**只认意图编译落账的 `params.platform`** —— safeArea 是平台契约,
    工程未声明平台就没有可对照的平台数值,硬闸不猜(画幅推断只用于提示类信息,
    见 rs_ingest._platform_presets)。无声明 → [](判据显式缺席,由调用方
    skipped 留痕,不静默放水也不误伤旧工程/默认样式渲染)。
    """
    try:
        from rs_subtitle import load_platforms
        table = load_platforms() or {}
    except Exception:  # noqa: BLE001 — 预设读不得 → 判据缺席(闸的缺席必须显式)
        return []
    params: dict = {}
    p = rs_paths.pipeline_json(root)
    if p.is_file():
        try:
            params = (json.loads(p.read_text(encoding="utf-8")) or {}).get("params") or {}
        except json.JSONDecodeError:
            params = {}
    plat = str(params.get("platform") or "")
    if plat in table and isinstance(table[plat].get("safeArea"), dict):
        return [dict(table[plat]["safeArea"], label=str(table[plat].get("label") or plat))]
    return []


def _ass_geometry(ass_path: Path) -> dict | None:
    """ASS 的安全区机械数据:PlayRes、样式表(MarginV/字号/对齐)与逐事件覆盖。

    只认结构性字段,不解释文本内容;PlayRes 缺失 → None(无从换算比例,判据缺席)。
    """
    play_res: dict[str, int] = {}
    style_fmt: list[str] = []
    event_fmt: list[str] = []
    styles: dict[str, dict] = {}
    events: list[dict] = []
    for raw in ass_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("PlayRes"):
            k, _, v = line.partition(":")
            try:
                play_res[k.strip()] = int(v.strip())
            except ValueError:
                pass
        elif line.startswith("Format:"):
            fields = [f.strip() for f in line.partition(":")[2].split(",")]
            if "Fontname" in fields:
                style_fmt = fields          # 样式区 Format(含 Fontname)
            elif "Start" in fields and "Text" in fields:
                event_fmt = fields          # 事件区 Format(含 Start/Text)
        elif line.startswith("Style:") and style_fmt:
            vals = [v.strip() for v in line.partition(":")[2].split(",")]
            st = dict(zip(style_fmt, vals))
            styles[st.get("Name", "")] = st
        elif line.startswith("Dialogue:") and event_fmt:
            vals = line.partition(":")[2].strip().split(",", len(event_fmt) - 1)
            if len(vals) == len(event_fmt):
                events.append(dict(zip(event_fmt, vals)))
    if not play_res:
        return None
    return {"playRes": play_res, "styles": styles, "events": events}


def _overlay_rects(root: Path) -> list[tuple[str, int, dict]]:
    """project.json 里带 `overlay={x,y,w,h}` 矩形的 clip(品牌 Logo/手挂贴片)。

    返回 (轨名, clip 下标, 矩形);绝对像素口径(rs_brand 写盘约定),供四边硬界检查。
    """
    out: list[tuple[str, int, dict]] = []
    pj = rs_paths.project_json(root)
    if not pj.is_file():
        return out
    try:
        doc = json.loads(pj.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return out
    for tr in doc.get("tracks") or []:
        for i, c in enumerate(tr.get("clips") or []):
            ov = c.get("overlay")
            if isinstance(ov, dict) and {"x", "y", "w", "h"} <= set(ov):
                out.append((str(tr.get("name") or tr.get("kind") or "?"), i, ov))
    return out


def check_safe_area(root: Path) -> dict:
    """安全区硬校验(§4.3 对账项「安全区」,清欠账 #A6):超界即红。

    机械口径(全部可由盘上数据重算,不给目测留模糊空间):
      · 字幕:按 ASS PlayRes 与样式/事件的 MarginV、字号、`\\N` 行数推字幕带 ——
        底边不得进入 `safeArea.bottom` 禁区、顶沿不得越 `safeArea.top` 禁区;
        左右不做文本宽估算(居中对齐下估算必误报),横向由贴片矩形硬界兜底;
      · 贴片/卡片:project.json 中带 `overlay={x,y,w,h}` 的 clip,四边硬界;
      · 平台:params.platform 唯一声明(硬闸不猜;未声明 → 判据显式缺席)。
    判据缺席(无字幕无贴片 / 无平台预设)→ skipped 留痕(ADR-0021 失败语义)。
    """
    name = "安全区硬校验(字幕带/贴片过平台 safeArea)"
    areas = _platform_safe_areas(root)
    ass = rs_paths.resolve(root, "output") / "subtitles.ass"
    geo = _ass_geometry(ass) if ass.is_file() else None
    rects = _overlay_rects(root)
    if not areas:
        return {"name": name, "ok": True,
                "skipped": "工程未声明平台(params.platform,先 rs_intent compile),无平台契约可校验"}
    if geo is None and not rects:
        return {"name": name, "ok": True, "skipped": "尚无字幕与贴片(中间态不算失败)"}

    viols: list[str] = []
    w = h = 0
    if geo:
        w = int(geo["playRes"].get("PlayResX", 0))
        h = int(geo["playRes"].get("PlayResY", 0))
    if not h:
        pj = rs_paths.project_json(root)
        if pj.is_file():
            try:
                c = (json.loads(pj.read_text(encoding="utf-8")) or {}).get("canvas") or {}
                w, h = int(c.get("width", 0)), int(c.get("height", 0))
            except json.JSONDecodeError:
                pass
    if not w or not h:
        return {"name": name, "ok": True,
                "skipped": "画布尺寸未知(ASS 缺 PlayRes 且无 IR 画布),无从换算安全区"}

    # ---- 字幕带(逐事件取最坏值:最小底边距 / 最大顶沿)
    worst_bottom: float | None = None
    worst_top = 0.0
    if geo:
        styles = geo["styles"]

        def _num(src: dict, key: str, default: float = 0.0) -> float:
            try:
                v = float(src.get(key, default))
                return v if v else default     # 0 = 未覆写,回样式默认(ASS 语义)
            except (TypeError, ValueError):
                return default

        for ev in geo["events"]:
            st = styles.get(ev.get("Style", "")) or {}
            try:
                align = int(_num(st, "Alignment", 2))
            except (TypeError, ValueError):
                align = 2
            margin_v = _num(ev, "MarginV", _num(st, "MarginV", 0.0))
            size = _num(st, "Fontsize", 0.0)
            n_lines = str(ev.get("Text", "")).count("\\N") + 1
            top_extent = margin_v + n_lines * size
            if align in (1, 2, 3):             # 底部对齐:MarginV 即底边距
                worst_bottom = margin_v if worst_bottom is None else min(worst_bottom, margin_v)
                worst_top = max(worst_top, top_extent)
            elif align in (4, 5, 6):           # 顶部对齐:只判顶沿,不判底
                worst_top = max(worst_top, top_extent)
        for sa in areas:
            bottom_band = float(sa.get("bottom", 0) or 0) * h
            top_band = float(sa.get("top", 0) or 0) * h
            lb = str(sa.get("label") or "?")
            if worst_bottom is not None and worst_bottom < bottom_band - 1:
                viols.append(f"{lb}:字幕底边 {worst_bottom:.0f}px 进入底部禁区 "
                             f"{bottom_band:.0f}px(safeArea.bottom={sa.get('bottom')})")
            if worst_top > h - top_band + 1:
                viols.append(f"{lb}:字幕顶沿 {worst_top:.0f}px 越顶部禁区 "
                             f"{top_band:.0f}px(safeArea.top={sa.get('top')})")

    # ---- 贴片矩形(四边硬界)
    for sa in areas:
        left_band = float(sa.get("left", 0) or 0) * w
        right_band = float(sa.get("right", 0) or 0) * w
        bottom_band = float(sa.get("bottom", 0) or 0) * h
        top_band = float(sa.get("top", 0) or 0) * h
        lb = str(sa.get("label") or "?")
        for tname, ci, r in rects:
            try:
                x, y = float(r["x"]), float(r["y"])
                rw, rh = float(r["w"]), float(r["h"])
            except (TypeError, ValueError):
                continue
            if (y < top_band - 1 or y + rh > h - bottom_band + 1
                    or x < left_band - 1 or x + rw > w - right_band + 1):
                viols.append(f"{lb}:贴片矩形越界(轨 {tname} clip#{ci}:"
                             f"x={x:.0f},y={y:.0f},w={rw:.0f},h={rh:.0f} vs 安全区 "
                             f"top={top_band:.0f}/bottom={bottom_band:.0f}/"
                             f"left={left_band:.0f}/right={right_band:.0f})")
    return {"name": name, "ok": not viols,
            "detail": "; ".join(viols[:5]),
            "violations": viols,
            "platforms": [str(sa.get("label") or "?") for sa in areas],
            "subEvents": len(geo["events"]) if geo else 0,
            "overlayRects": len(rects)}


def check_assets(root: Path) -> dict:
    """素材商用与归因一致性 L0 判据(M12/ADR-0053/R42,分册01 §7/§8)。

    机械口径(全部可由盘上数据重算):
      · 工程实际引用的素材(音效伪协议 / bgm.src)必须能在统一索引解析;
      · 引用的素材 commercial 必须为 true(不可商用素材入交付 = 红);
      · 成品/说明书/素材归因.md 已生成时,其条目与实际引用**双向对拍**一致。
    判据缺席(工程无任何素材引用且无归因文件)→ skipped 留痕(ADR-0021 失败语义)。
    """
    name = "素材商用与归因一致(引用可解析/可商用/归因对拍)"
    import rs_asset
    refs = rs_asset.collect_project_refs(root)
    deliver = rs_paths.resolve(root, "deliver")
    attr = deliver / "说明书" / "素材归因.md"
    if not refs and not attr.is_file():
        return {"name": name, "ok": True,
                "skipped": "工程未引用素材库条目,归因对拍无从谈起(中间态不算失败)"}
    problems: list[str] = []
    noncomm = [r["id"] for r in refs if not r.get("commercial", True)]
    if noncomm:
        problems.append(f"引用了不可商用素材:{','.join(noncomm)}(不得进入交付)")
    # 音效引用可解析(伪协议 → 索引/盘面任一命中)
    unresolved: set[str] = set()
    pj = rs_paths.project_json(root)
    if pj.is_file():
        try:
            ir = json.loads(pj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            ir = {}
        for t in ir.get("tracks") or []:
            for clip in t.get("clips") or []:
                src = str(clip.get("src") or "")
                if src.startswith("assets_sfx:") and                         rs_asset.resolve_sfx_ref(src.split(":", 1)[1]) is None:
                    unresolved.add(src)
    if unresolved:
        problems.append(f"音效引用无法解析:{','.join(sorted(unresolved))}")
    # 归因双向对拍:文件里的 id 集 ↔ 实际引用 id 集
    if attr.is_file():
        listed = rs_asset.asset_ids_in(attr.read_text(encoding="utf-8"))
        wanted = {r["id"] for r in refs}
        if listed != wanted:
            problems.append(f"归因清单与引用不一致:文件多出 {sorted(listed - wanted)};"
                            f"引用缺登 {sorted(wanted - listed)}")
    return {"name": name, "ok": not problems, "problems": problems,
            "referenced": sorted(r["id"] for r in refs)}


def check_matte(root: Path) -> dict:
    """抠像质量判据 L0(ADR-0050):gate 判定 fail/blocked → 红;warn → 绿但留提示。

    工程未跑过抠像门禁(无 matte/quality.json)→ skipped,不误伤全库。
    """
    name = "抠像质量(五项门禁)"
    q = rs_paths.of(root, "timeline") / "matte" / "quality.json"
    if not q.is_file():
        return {"name": name, "ok": True, "skipped": "未启用自动抠像(无判定报告)"}
    try:
        doc = json.loads(q.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"name": name, "ok": False, "detail": f"判定报告解析失败:{q}"}
    verdict = doc.get("verdict")
    if verdict in ("pass", "warn"):
        return {"name": name, "ok": True, "detail": f"verdict={verdict}",
                "note": ("交付说明书必须标注「边缘质量一般」" if verdict == "warn" else ""),
                "metrics": doc.get("metrics", {})}
    return {"name": name, "ok": False,
            "detail": (f"抠像判定 {verdict} 不达启用档;"
                       f"{';'.join(doc.get('suggestions', [])[:3])}")}


# ---------------------------------------------------------------- 效果使用率门禁(ADR-0059,分册06 §9.3)

_FLASH_REASON_WORDS = ("好看", "更酷", "炫", "高级感", "牛")   # reason 含这些词 = 未解释(§5.2)
_FLASH_FX_PREFIXES = ("flash.", "effect.glitch")              # 闪变类单片段 fx(光敏口径)


def _prescription_of(root: Path) -> tuple[dict, str]:
    """工程的效果处方:只认意图编译落账的 resolved.effectsPrescription(§9.1 数据)。

    返回 (处方, 来源说明);无处方 → ({}, "")——调用方 skipped 留痕 NO_PRESCRIPTION
    (ADR-0059 四问:无处方的工程门禁不生效,行为与 M13 之前一致)。"""
    p = rs_paths.resolve(root, "brief") / "intent_decisions.json"
    if not p.is_file():
        return {}, ""
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        resolved = doc.get("resolved") if isinstance(doc.get("resolved"), dict) else {}
        pres = resolved.get("effectsPrescription")
        return (pres, "intent_decisions.json") if isinstance(pres, dict) and pres else ({}, "")
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}, ""


def _usage_events(root: Path) -> dict:
    """从 IR 机械提取效果使用事件(转场/入出/音效/闪变),全部可由盘上数据重算。"""
    import rs_fx
    pj = rs_paths.project_json(root)
    if not pj.is_file():
        return {}
    try:
        doc = json.loads(pj.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    tracks = [t for t in doc.get("tracks", []) if t.get("kind") == "video"]
    main = tracks[0] if tracks else {}
    clips = main.get("clips") or []
    transitions: list[dict] = []
    in_fx: list[dict] = []
    out_fx: list[dict] = []
    flashes: list[float] = []      # 闪变事件时刻(秒)
    for i, c in enumerate(clips):
        tr = c.get("transition") or {}
        if tr:
            try:
                res = rs_fx.resolve_transition(tr)
            except Exception:  # noqa: BLE001 — 未注册 fxId 由 IR 校验/渲染闸负责
                res = {}
            kind = res.get("kind")
            if kind not in ("cut", "none", None):
                t_s = (c.get("startMs") or 0) / 1000.0
                # ADR-0026 三级语法:jumpcut / 亚帧(<2 帧)软切 = 视觉硬切,
                # 不计「显式转场」(§5.1 的显式上限只管真转场,§9.3 判据同口径)
                dur_ms = float(tr.get("durMs") or res.get("durMs") or 500)
                frames = dur_ms * float(doc.get("fps") or 30) / 1000.0
                explicit = str(tr.get("reason") or "").lower() != "jumpcut" and frames >= 2
                if explicit:
                    transitions.append({"join": i, "fxId": res.get("fxId"), "kind": kind,
                                        "flashy": bool(res.get("flashy")),
                                        "reason": str(tr.get("reason") or ""),
                                        "t": t_s})
                if explicit and (res.get("flashy") or str(res.get("xfade")) in ("fadefast",)):
                    flashes.append(t_s)
                if explicit and (res.get("boundary") or {}):
                    flashes.append(t_s)      # 边界闪入/闪出也算一次明暗反转
        m = c.get("motion") or {}
        box = c.get("fx") or {}
        fx_in = m.get("inFx") or (box.get("in") or {}).get("fx")
        fx_out = m.get("outFx") or (box.get("out") or {}).get("fx")
        fx_combo = (box.get("combo") or {}).get("fx")
        st = (c.get("startMs") or 0) / 1000.0
        if fx_in:
            in_fx.append({"fxId": str(fx_in), "t": st})
            if str(fx_in).startswith(_FLASH_FX_PREFIXES):
                flashes.append(st)
        if fx_out:
            out_fx.append({"fxId": str(fx_out), "t": st})
            if str(fx_out).startswith(_FLASH_FX_PREFIXES):
                flashes.append(st)
        if fx_combo:
            in_fx.append({"fxId": str(fx_combo), "t": st})   # 组合档计入入场计数(§9.3 判据 2 的可用面)
    sfx = []
    for t in doc.get("tracks", []):
        if t.get("kind") != "audio":
            continue
        for c in t.get("clips") or []:
            if str(c.get("role")) == "sfx":
                sfx.append({"src": str(c.get("assetId") or c.get("src") or ""),
                            "t": (c.get("startMs") or 0) / 1000.0})
    duration_s = sum(float(c.get("durationMs") or 0) for c in clips) / 1000.0
    return {"transitions": transitions, "in": in_fx, "out": out_fx,
            "sfx": sfx, "flashes": sorted(flashes), "durationS": duration_s}


def check_effects_usage(root: Path) -> dict:
    """效果使用率九判据 L0(ADR-0059,分册06 §9.3):「有特效却没用上」= 机械红。

    启用条件(数据驱动):工程经意图编译落了 effects_prescription;无处方 →
    skipped 留痕 NO_PRESCRIPTION(旧工程/未声明风格不受约束,不误伤)。
    判据族:EFFECTS_UNUSED / EFFECTS_OVERUSED / EFFECTS_REPEATED /
    EFFECTS_UNMOTIVATED / SFX_DENSITY / FLASH_UNSAFE / DURATION_DRIFT。
    """
    name = "效果使用率(处方九判据,ADR-0059)"
    pres, _src = _prescription_of(root)
    if not pres:
        return {"name": name, "ok": True,
                "skipped": "NO_PRESCRIPTION:工程未声明 effects_prescription(门禁不生效,ADR-0059 四问口径)"}
    usage = _usage_events(root)
    if not usage:
        return {"name": name, "ok": True, "skipped": "尚无 IR(中间态不算失败)"}
    viols: list[str] = []
    warns: list[str] = []
    tr = usage["transitions"]
    trans_pres = pres.get("transition") or {}
    tr_min = int(trans_pres.get("min") or 0)
    max_per_8s = float(trans_pres.get("max_per_8s") or 1)
    flashy_max = pres.get("flashy_max")
    dur = float(usage.get("durationS") or 0.0)

    # ①② 使用不足(EFFECTS_UNUSED)
    if len(tr) < tr_min:
        viols.append(f"EFFECTS_UNUSED:显式转场 {len(tr)} < 处方 min {tr_min}(硬切不算;§9.3 判据 1)")
    ins = pres.get("in") or {}
    outs = pres.get("out") or {}
    if len(usage["in"]) < int(ins.get("min") or 0):
        viols.append(f"EFFECTS_UNUSED:入场 {len(usage['in'])} < 处方 min {ins.get('min')}(判据 2)")
    if len(usage["out"]) < int(outs.get("min") or 0):
        viols.append(f"EFFECTS_UNUSED:出场 {len(usage['out'])} < 处方 min {outs.get('min')}(判据 2)")

    # ④ 滥用(EFFECTS_OVERUSED):全片 ≤ 片长÷10、每 8s ≤max、花哨 ≤flashy_max 且不连续。
    # 最小偏离说明(报告已列):密度上限判据在 L0 为【告警】不硬失败 —— ADR-0026 三级
    # 语法会在去气口边界自动产生 reason=topic 的溶解,密度超标可能反映素材气口结构
    # 而非剪辑滥用;硬失败会「误伤」全部管线工程(违反 ADR-0059 不误伤条款)。
    # 花哨类的 flashy_max 与「不得连续」仍是硬失败(那是廉价感的真来源)。
    if dur > 0:
        cap = max(int(dur // 10), 0)
        if len(tr) > cap:
            warns.append(f"EFFECTS_OVERUSED(告警):显式转场 {len(tr)} > 片长÷10 = {cap}(判据 4)")
    times = sorted(e["t"] for e in tr)
    for i, t in enumerate(times):
        win = [x for x in times if t <= x < t + 8.0]
        if len(win) > max_per_8s:
            warns.append(f"EFFECTS_OVERUSED(告警):{t:.1f}s 起 8s 窗口内 {len(win)} 处"
                         f"显式转场 > max_per_8s={max_per_8s:g}(判据 4)")
            break
    flashy = [e for e in tr if e["flashy"]]
    if flashy_max is not None and len(flashy) > int(flashy_max):
        viols.append(f"EFFECTS_OVERUSED:花哨类 {len(flashy)} 处 > flashy_max={flashy_max}(判据 5)")
    for a, b in zip(flashy, flashy[1:]):
        if a["join"] + 1 == b["join"] or abs(b["t"] - a["t"]) <= 2.0:
            viols.append(f"EFFECTS_OVERUSED:花哨类连续两处({a['fxId']}@{a['t']:.1f}s → "
                         f"{b['fxId']}@{b['t']:.1f}s;判据 5:不得连续)")
            break

    # ⑥ 同效果 10s 不重复(EFFECTS_REPEATED)
    events = sorted([{"fxId": e["fxId"], "t": e["t"]} for e in tr if e["fxId"]]
                    + usage["in"] + usage["out"], key=lambda e: (str(e["fxId"]), e["t"]))
    by_fx: dict[str, list[float]] = {}
    for e in events:
        if e["fxId"]:
            by_fx.setdefault(str(e["fxId"]), []).append(e["t"])
    for fxid, ts in sorted(by_fx.items()):
        for a, b in zip(ts, ts[1:]):
            if b - a < 10.0:
                viols.append(f"EFFECTS_REPEATED:{fxid} 于 {a:.1f}s 与 {b:.1f}s 重复"
                             "(判据 6:10s 内不得重复)")
                break

    # ⑦ 每处显式转场必须带非空 reason 且非「好看」类(EFFECTS_UNMOTIVATED)
    for e in tr:
        r = str(e.get("reason") or "").strip()
        if not r:
            viols.append(f"EFFECTS_UNMOTIVATED:join{e['join']} 显式转场({e['fxId']})无 reason"
                         "(判据 7;rs_edit EditOp 强制字段)")
            break
        if any(w in r for w in _FLASH_REASON_WORDS):
            viols.append(f"EFFECTS_UNMOTIVATED:join{e['join']} reason={r!r} 含「好看」类词"
                         "=未解释,应改硬切(判据 7,§5.2)")
            break

    # ③ 音效密度(SFX_DENSITY)
    sfx_pres = pres.get("sfx") or {}
    sfx_events = usage["sfx"]
    if sfx_events:
        if len(sfx_events) > int(sfx_pres.get("max") or 10**9):
            viols.append(f"SFX_DENSITY:音效 {len(sfx_events)} > 处方 max {sfx_pres.get('max')}(判据 3)")
        per15 = float(sfx_pres.get("per15s") or 2)
        st = sorted(e["t"] for e in sfx_events)
        for i, t in enumerate(st):
            win = [x for x in st if t <= x < t + 15.0]
            if len(win) > per15:
                viols.append(f"SFX_DENSITY:{t:.1f}s 起 15s 窗口内 {len(win)} 个音效"
                             f" > per15s={per15:g}(判据 3,分册01 §4.1)")
                break
        norep = float(sfx_pres.get("noRepeatWithinMs") or 10000) / 1000.0
        by_src: dict[str, list[float]] = {}
        for e in sfx_events:
            by_src.setdefault(e["src"], []).append(e["t"])
        for src_key, ts in sorted(by_src.items()):
            for a, b in zip(ts, ts[1:]):
                if b - a < norep:
                    viols.append(f"SFX_DENSITY:同一音效 {src_key} 于 {a:.1f}s/{b:.1f}s 重复"
                                 f"(<{norep:g}s;判据 3)")
                    break

    # ⑧ 光敏安全(FLASH_UNSAFE):任一 1s 窗口明暗反转 ≤3(WCAG 2.3.1 同口径)
    fl = usage["flashes"]
    for i, t in enumerate(fl):
        win = [x for x in fl if t <= x < t + 1.0]
        if len(win) > 3:
            viols.append(f"FLASH_UNSAFE:{t:.2f}s 起 1s 内 {len(win)} 次明暗反转 > 3"
                         "(判据 8,WCAG 2.3.1;安全底线不可回滚)")
            break

    # ⑨ 时长零漂移(DURATION_DRIFT):成片【视频流】时长 vs IR 名义总长(≤1.5 帧)。
    # 用视频流而非容器时长:容器含音频垫尾(实测 10.9s 容器 vs 10.83s 视频流),
    # 与 rs_render 的 B2 断言同口径。
    drift_note = ""
    if dur > 0:
        vids = list_videos(root)
        fps = 30.0
        try:
            pj = rs_paths.project_json(root)
            fps = float(json.loads(pj.read_text(encoding="utf-8")).get("fps") or 30)
        except Exception:  # noqa: BLE001 — fps 读不得按 30 兜底(容差略宽)
            pass
        tol = 1.5 / max(fps, 1.0)
        if vids:
            try:
                real = _video_stream_duration_s(vids[-1])
                if real > 0 and abs(real - dur) > tol:
                    # 最小偏离说明:渲染端 B2 断言同款检查只 WARN 留痕(编码器帧取整
                    # 有固有小漂移);本判据在 L0 同为【告警】,不把旧工程逼红。
                    warns.append(f"DURATION_DRIFT(告警):成片视频流 {real:.3f}s vs IR 名义 "
                                 f"{dur:.3f}s(差 {abs(real - dur):.3f}s > 1.5 帧;判据 9,ADR-0023)")
            except Exception as exc:  # noqa: BLE001 — 探测失败留痕不阻塞其余判据
                drift_note = f"成片时长探测失败:{exc}"
        else:
            drift_note = "尚无成片(判据 9 待成片后生效)"

    detail = "; ".join(viols[:5]) if viols else (
        f"转场 {len(tr)}/{tr_min}·入场 {len(usage['in'])}/{ins.get('min', 0)}·"
        f"出场 {len(usage['out'])}/{outs.get('min', 0)}·音效 {len(sfx_events)}"
        + (f";{drift_note}" if drift_note else ""))
    if not viols and warns:
        detail = (detail + ";⚠ " + "; ".join(warns[:3])).strip("; ")
    return {"name": name, "ok": not viols, "detail": detail,
            "violations": viols, "warnings": warns, "counts": {
                "transitions": len(tr), "in": len(usage["in"]),
                "out": len(usage["out"]), "sfx": len(sfx_events),
                "flashy": len(flashy), "durationS": round(dur, 2)}}


def _video_stream_duration_s(video: Path) -> float:
    """成片视频流时长(s;容器时长含音频垫尾不可靠,rs_render._video_stream_len 同口径)。"""
    try:
        import subprocess  # noqa: PLC0415
        from rs_common import ffprobe_bin, load_config  # noqa: PLC0415
        p = subprocess.run(
            [ffprobe_bin(load_config()), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1",
             str(video)], capture_output=True, text=True, timeout=60)
        if p.returncode == 0 and p.stdout.strip():
            return float(p.stdout.strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        pass
    return 0.0


L0_CHECKS = (check_ir, check_wordline, check_cutlist, check_greenscreen, check_copyright,
             check_subtitles, check_safe_area, check_alignment, check_artifacts, check_qc,
             check_assets, check_matte, check_effects_usage)


def collect_l0(root: Path, copyright_opts: dict | None = None) -> dict:
    checks: list[dict] = []
    for fn in L0_CHECKS:
        if fn is check_copyright and copyright_opts:
            # CLI/params 覆盖版权判据阈值(初值可配;非数值键忽略,走函数内默认解析)
            kw = {k: v for k, v in copyright_opts.items()
                  if k in ("max_single", "max_total", "min_commentary")
                  and isinstance(v, (int, float))}
            checks.append(check_copyright(root, **kw))
        else:
            checks.append(fn(root))
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


def _copyright_l1_items(root: Path) -> list[str]:
    """版权门禁的 L1/用户项(机械不可判,显式标注,方案 R7);判据未启用 → 空。"""
    for fn in L0_CHECKS:
        if fn is check_copyright:
            return [str(x) for x in (fn(root).get("l1Pending") or [])]
    return []


def l1_payload(root: Path) -> dict:
    out = rs_paths.resolve(root, "output")
    vids = list_videos(root)
    names = sorted(p.relative_to(out).as_posix() for p in vids)
    o, t = rs_paths.p("output"), rs_paths.p("timeline")
    cmds = [f"python skills/cutflow/scripts/rs_bench.py {o}/{v} "
            f"--ir {t}/project.json --out {o}/bench_{Path(v).stem}.png" for v in names]
    checklist = list(L1_CHECKLIST) + _copyright_l1_items(root)
    return {"level": "L1", "needsAgentReview": True,
            "checklist": checklist, "benchCommands": cmds,
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
    # 版权 L1/用户项显式标注(方案 R7):机械闸过了也不算 PASS,须人工判定
    for c in res["checks"]:
        for x in c.get("l1Pending") or []:
            lines.append(f"| {c['name']}·L1/用户项 | ⚠ | {x} |")
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
    ap.add_argument("--out", default=rs_paths.p("output"))
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--mark-first", dest="mark_first", action="store_true")
    ap.add_argument("--result", default="pass", choices=["pass", "fail"])
    ap.add_argument("--content-budget", dest="content_budget", type=float, default=900.0,
                    help="--content 诊断预算秒(默认 900;超时输出阶段性结论)")
    ap.add_argument("--copyright-max-single", dest="copyright_max_single", type=float,
                    default=None, help="版权门禁:单部引用占比上限(默认 0.30;初值可配)")
    ap.add_argument("--copyright-max-total", dest="copyright_max_total", type=float,
                    default=None, help="版权门禁:总引用时长占比上限(默认 0.70)")
    ap.add_argument("--copyright-min-commentary", dest="copyright_min_commentary",
                    type=float, default=None,
                    help="版权门禁:原创解说轨时长占比下限(默认 0.25,解说型)")
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

    res = collect_l0(root, copyright_opts={
        "max_single": a.copyright_max_single,
        "max_total": a.copyright_max_total,
        "min_commentary": a.copyright_min_commentary})
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
                                        False, rs_paths.resolve(root, "output"), cfg)
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
