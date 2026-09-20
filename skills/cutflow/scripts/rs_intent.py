"""N1 意图编译器(副文档 04 · 阶段四):提示词 → 结构化参数 → 现有 intake 产物。

用法:
  rs_intent.py compile --brief brief.json --plan plan.json --out <工程根> `
      [--prompt prompt.txt] [--dry-run]

三段结构(参照 jianying-headless 的 compile 分层,只学方法):
  ① 提示词(自然语言)──Agent 语义解析──► ② brief.json + plan.json(结构化、可读、可改)
  ──本脚本确定性编译──► ③ 现有 S0–S11 机械臂(一行不改)。

分工铁律(SKILL.md §1.1 同口径):
  · Agent 只做「读懂提示词 → 写 brief.json + plan.json」与「写文案」两处语义工作;
  · 本脚本是确定性编译:校验必填与枚举 → 补默认 → 查风格注册表(registry.json)→
    落现有 intake 产物(00_brief/brief.md、terms.txt)→ 输出推断表;
  · 给定输入必得同一输出(纯函数式:产物与 intent_decisions.json 不含时间戳,
    同输入字节级一致,tests/test_v21_prompt_auto.py 断言)。

一切「默认推断」显式标 inferred=true / source=registry|default,与「用户明说」
(source=user, inferred=false)区分;全量决策随 00_brief/intent_decisions.json 落盘,
rs_run --auto 将其并入 05_ir/pipeline.json 的 decision_log,N4 据此生成决策说明书。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rs_common  # noqa: E402
from rs_common import RATIOS, emit, write_text_atomic  # noqa: E402
from rs_subtitle import STYLES, load_platforms, resolve_platform  # noqa: E402
from rs_run import _PLATFORM_ALIASES  # noqa: E402  — 平台别名单一真相源

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "templates" / "styles" / "registry.json"
STYLES_DIR = Path(__file__).resolve().parents[1] / "templates" / "styles"

# brief/plan 显式给出的枚举口径(给出即校验,不给不臆测)
BGM_MODES = ("none", "provided", "cc0")
VOICE_SOURCES = ("original", "tts", "none")
DENSITIES = ("无", "少", "多")


def _alias(name: str) -> str:
    """平台别名 → 规范键(与 rs_run 参数解析同一张表,单一真相源)。"""
    return _PLATFORM_ALIASES.get(str(name), _PLATFORM_ALIASES.get(str(name).lower(), name))


def load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 校验

def _bad(msgs: list[str], code: str = "BAD_BRIEF") -> int:
    return emit(False, code, "意图编译失败:" + "；".join(msgs), {"errors": msgs}, exit_code=2)


def validate(brief: dict, plan: dict, registry: dict, platforms: dict) -> list[str]:
    errs: list[str] = []
    vt = brief.get("videoType")
    if not vt:
        errs.append("brief.videoType 必填(可选:" + "/".join(registry["videoTypes"]) + ")")
    elif vt not in registry["videoTypes"]:
        errs.append(f"brief.videoType 未知:{vt!r}(可选:" + "/".join(registry["videoTypes"]) + ")")
    plat = brief.get("platform")
    if plat and _alias(plat) not in platforms:
        errs.append(f"brief.platform 未知:{plat!r}(可选:" + "/".join(sorted(platforms)) + ")")
    ratio = brief.get("ratio")
    if ratio and ratio not in RATIOS:
        errs.append(f"brief.ratio 未知:{ratio!r}(可选:" + "/".join(RATIOS) + ")")
    pace = brief.get("pacing") or plan.get("pacing")
    if pace and pace not in registry["pacingTiers"]:
        errs.append(f"节奏档未知:{pace!r}(可选:" + "/".join(registry["pacingTiers"]) + ")")
    bgm = brief.get("bgm")
    if bgm and bgm not in BGM_MODES:
        errs.append(f"brief.bgm 未知:{bgm!r}(可选:" + "/".join(BGM_MODES) + ")")
    src = (brief.get("voice") or {}).get("source") if isinstance(brief.get("voice"), dict) else None
    if src and src not in VOICE_SOURCES:
        errs.append(f"brief.voice.source 未知:{src!r}(可选:" + "/".join(VOICE_SOURCES) + ")")
    density = brief.get("density")
    if density and density not in DENSITIES:
        errs.append(f"brief.density 未知:{density!r}(可选:" + "/".join(DENSITIES) + ")")
    name = brief.get("styleName")
    if name and name not in {e["id"] for e in registry["entries"]}:
        errs.append(f"brief.styleName 不在注册表:{name!r}(可选:"
                    + "/".join(e["id"] for e in registry["entries"]) + ")")
    sub_style = (plan.get("subtitle") or {}).get("style")
    if sub_style and sub_style not in STYLES:
        errs.append(f"plan.subtitle.style 未知:{sub_style!r}(可选:" + "/".join(sorted(STYLES)) + ")")
    mc = (plan.get("subtitle") or {}).get("maxChars")
    if mc and not (4 <= int(mc) <= 40):
        errs.append(f"plan.subtitle.maxChars 越界:{mc}(允许 4–40)")
    cps = (plan.get("subtitle") or {}).get("cpsMax")
    if cps and not (1.0 <= float(cps) <= 20.0):
        errs.append(f"plan.subtitle.cpsMax 越界:{cps}(允许 1–20)")
    return errs


# ---------------------------------------------------------------- 查注册表

def match_entry(brief: dict, registry: dict) -> tuple[dict, str, str]:
    """确定性选条目:styleName 精确 > styleRef 关键词计分 > videoType 缺省。

    返回 (条目, 依据说明, 命中提示词片段)。同分取注册表在前者(条目顺序即优先级)。
    """
    entries = registry["entries"]
    vt = brief.get("videoType")
    name = brief.get("styleName")
    if name:
        e = next(e for e in entries if e["id"] == name)
        return e, f"用户指定风格名 {name}", ""
    ref = str(brief.get("styleRef") or "")
    pool = [e for e in entries if vt in e["videoTypes"]] if vt else list(entries)
    best, best_hits, best_key = None, [], (0, 0)      # (命中个数, 命中字符长);同分取注册表在前者
    for e in pool:
        kws = [k for k in e["match"]["keywords"] if k and k in ref]
        key = (len(kws), sum(len(k) for k in kws))
        if kws and key > best_key:
            best, best_hits, best_key = e, kws, key
    if best is not None:
        return best, f"参考描述命中关键词 {'、'.join(best_hits)}", ref
    e = next((e for e in entries if vt in e.get("defaultFor", [])), None)
    if e is None:
        raise ValueError(f"注册表没有 videoType={vt!r} 的缺省条目(先在 registry.json 登记)")
    return e, f"videoType={vt} 的注册表缺省条目", ""


# ---------------------------------------------------------------- 编译(纯函数)

def compile_intent(brief: dict, plan: dict, prompt_text: str,
                   registry: dict, platforms: dict) -> dict:
    """brief+plan(+提示词原文)→ resolved + decisions。不落盘、不含时间戳。"""
    decisions: list[dict] = []

    def dec(field: str, value, source: str, why: str, quote: str = "") -> None:
        decisions.append({"id": f"intent:{field}", "field": field, "value": value,
                          "source": source, "inferred": source != "user",
                          "why": why, "promptQuote": quote})

    entry, basis, quote = match_entry(brief, registry)
    vt = brief["videoType"]
    dec("videoType", vt, "user", "决定 rules/video-types 分册与管线分支(S0 问卷第 0 问)")
    dec("styleEntry", entry["id"], "registry", basis, quote)

    # 平台:用户明说 > 注册表条目 > douyin(与 rs_run 参数缺省一致)
    plat_key = _alias(brief["platform"]) if brief.get("platform") else entry["platform"]
    preset = resolve_platform(plat_key)
    plat_src = "user" if brief.get("platform") else "registry"
    dec("platform", plat_key, plat_src,
        "平台预设决定画幅/安全区/每卡字数(templates/platforms.json)"
        if plat_src == "user" else f"注册表条目 {entry['id']} 的平台缺省")

    # 画幅:用户明说 > 平台预设(画幅由平台供给,registry _doc 口径)> 注册表条目
    ratio = brief.get("ratio") or preset.get("ratio") or entry["ratio"] or "9x16"
    ratio_src = "user" if brief.get("ratio") else "registry"
    why = "用户显式画幅,优先于平台预设" if ratio_src == "user" else \
        (f"平台预设 {plat_key}.ratio(templates/platforms.json)" if ratio == preset.get("ratio")
         else f"注册表条目 {entry['id']} 的画幅缺省")
    dec("ratio", ratio, ratio_src, why)

    plan_sub = plan.get("subtitle") or {}
    sub_style = plan_sub.get("style") or entry["style"] or preset.get("style") or "subtitle-white"
    dec("subStyle", sub_style, "user" if plan_sub.get("style") else "registry",
        "plan 显式字幕样式" if plan_sub.get("style")
        else f"注册表条目 {entry['id']} 的字幕样式(样式模板 {entry.get('styleTemplate')})")

    max_chars = plan_sub.get("maxChars") or preset.get("maxChars") or 12
    dec("maxChars", int(max_chars), "user" if plan_sub.get("maxChars") else "registry",
        "plan 显式每卡字数" if plan_sub.get("maxChars")
        else f"平台预设 {plat_key}.maxChars(templates/platforms.json)")
    cps = plan_sub.get("cpsMax") or 9.0
    dec("cpsMax", float(cps), "user" if plan_sub.get("cpsMax") else "default",
        "plan 显式 CPS 上限" if plan_sub.get("cpsMax") else "全库统一 CPS ≤9(字幕三定律)")

    pace_key = plan.get("pacing") or brief.get("pacing") or entry["pacing"]
    pace_src = "user" if (plan.get("pacing") or brief.get("pacing")) else "registry"
    tier = registry["pacingTiers"][pace_key]
    dec("pacing", pace_key, pace_src,
        "plan/brief 显式节奏档" if pace_src == "user" else f"注册表条目 {entry['id']} 的节奏档")

    bgm_default_on = bool((entry.get("bgmLibrary") or {}).get("enabled")) or bool(tier.get("bgm"))
    bgm_mode = brief.get("bgm") or ("cc0" if bgm_default_on else "none")
    bgm_on = bgm_mode != "none"
    bgm_gain = (entry.get("bgmLibrary") or {}).get("gainDb", tier.get("bgmGainDb", -18))
    dec("bgm", bgm_mode, "user" if brief.get("bgm") else "registry",
        "brief 显式 BGM 意图" if brief.get("bgm")
        else f"条目/节奏档缺省({pace_key} 档 bgm={'开' if tier.get('bgm') else '关'})")

    dur = brief.get("durationTarget") or preset.get("durationHint") or "30-60s"
    dec("durationTarget", dur, "user" if brief.get("durationTarget") else "registry",
        "brief 显式目标时长" if brief.get("durationTarget") else f"平台预设 {plat_key}.durationHint")

    density = brief.get("density") or entry.get("cards", {}).get("density") or "少"
    dec("density", density, "user" if brief.get("density") else "registry",
        "brief 显式穿插动画密度" if brief.get("density") else f"注册表条目 {entry['id']} 的卡片密度")

    voice = brief.get("voice") or {}
    voice_default = "tts" if vt == "pure-animation" else "original"
    dec("voiceSource", voice.get("source", voice_default),
        "user" if voice.get("source") else "default",
        "brief 显式声音来源" if voice.get("source") else
        (f"{'纯动画无真人画面,默认 TTS 音色卡' if vt == 'pure-animation' else '口播类默认原声优先'}(纯动画必填)"))
    sub_on = (brief.get("subtitle") or {}).get("on", True)
    dec("subtitleOn", bool(sub_on), "user" if isinstance((brief.get("subtitle") or {}).get("on"), bool)
        else "default", "brief 显式字幕开关" if isinstance((brief.get("subtitle") or {}).get("on"), bool)
        else "平台短视频默认开字幕")

    resolved = {
        "videoType": vt,
        "videoTypeLabel": registry["videoTypes"][vt]["label"],
        "doc": registry["videoTypes"][vt]["doc"],
        "styleEntry": entry["id"],
        "styleLabel": entry["label"],
        "platform": plat_key,
        "platformLabel": preset.get("label", plat_key),
        "ratio": ratio,
        "canvas": list(RATIOS[ratio]),
        "subStyle": sub_style,
        "styleTemplate": entry.get("styleTemplate", sub_style),
        "maxChars": int(max_chars),
        "cpsMax": float(cps),
        "pacing": pace_key,
        "cardMs": list(tier["cardMs"]),
        "visualBeatSec": list(tier["visualBeatSec"]),
        "bgm": {"mode": bgm_mode, "enabled": bgm_on, "gainDb": bgm_gain},
        "durationTarget": dur,
        "density": density,
        "voice": {"source": voice.get("source", voice_default),
                  "ttsVoice": voice.get("ttsVoice", "")},
        "subtitleOn": bool(sub_on),
        "highlight": list((brief.get("subtitle") or {}).get("highlight") or []),
        "title": str(brief.get("title") or ""),
        "terms": [str(t) for t in (brief.get("terms") or [])],
        "taboo": [str(t) for t in (brief.get("taboo") or [])],
        "structure": str(brief.get("structure") or "HOOK → 正文(要点…)→ CTA"),
        "introOutro": str(brief.get("introOutro") or "无"),
        "logo": bool(brief.get("logo")),
        "cardsPlan": str(plan.get("cards") or "none"),
        "cutEnabled": bool((plan.get("cut") or {}).get("enabled", True)),
        "sfx": str(plan.get("sfx") or "auto"),
        "prompt": prompt_text.strip(),
    }
    return {"resolved": resolved, "decisions": decisions, "entry": entry}


# ---------------------------------------------------------------- 产物渲染

def render_brief_md(r: dict) -> str:
    bgm = r["bgm"]
    pace_line = (f"- 节奏档:{r['pacing']}(卡 {r['cardMs'][0]}–{r['cardMs'][1]}ms;"
                 f"视觉节拍 {r['visualBeatSec'][0]}–{r['visualBeatSec'][1]}s;"
                 f"BGM {'开,增益 %ddB' % bgm['gainDb'] if bgm['enabled'] else '关'})")
    voice_txt = {"original": "原声", "tts": f"TTS 音色卡({r['voice']['ttsVoice'] or 'koubo-test'})",
                 "none": "无声+BGM"}[r["voice"]["source"]]
    lines = [
        f"# Brief — {r['title'] or '提示词工程'}", "",
        "> 本文件是唯一决策契约:此后一切制作决策只查此文件,不再询问。",
        "> 来源:提示词意图编译(`rs_intent.py compile`);每条参数的来历见 intent_decisions.json。", "",
        "## 目标",
        f"- videoType:`{r['videoType']}`({r['videoTypeLabel']};分册 {r['doc']})",
        f"- 绿幕预处理:{'不适用(非 talking-head 系)' if r['videoType'] == 'pure-animation' else '已由用户自行抠像并合成背景'}",
        f"- 穿插动画密度:{r['density']}",
        f"- 声音来源:{voice_txt}",
        f"- 用途/平台:{r['platformLabel']}({r['platform']})",
        f"- 比例/平台预设:{r['ratio']}(画布 {r['canvas'][0]}x{r['canvas'][1]})",
        f"- 目标时长:{r['durationTarget']}", "",
        "## 风格",
        f"- 字幕样式:{r['subStyle']}(模板 {r['styleTemplate']};字幕{'开' if r['subtitleOn'] else '关'}",
        f"- 高亮词表:{('、'.join(r['highlight'])) if r['highlight'] else '(无)'}",
        pace_line,
        f"- 风格条目:{r['styleEntry']}({r['styleLabel']};templates/styles/registry.json)", "",
        "## 管线参数(机器可读,rs_run 消费;改这里即改参数源)",
        f"- 平台:{r['platformLabel']}",
        f"- 画幅:{r['ratio']}",
        f"- 每卡字数:{r['maxChars']}",
        f"- CPS:{r['cpsMax']:g}",
        f"- 风格 token:{r['subStyle']}", "",
        "## 声音",
        f"- 方案:{voice_txt}",
        "- 语速:1.0", "",
        "## 结构",
        f"- 结构原型:{r['structure']}",
        f"- 片头片尾:{r['introOutro']}",
        f"- 粗剪:{'开(宁可漏删不可错删)' if r['cutEnabled'] else '关(显式关闭,留痕)'}",
        f"- 音效:{r['sfx']}",
        f"- 卡片计划:{r['cardsPlan']}", "",
        "## 素材清单",
        "| 文件 | 类型 | 内容 | 用途 |",
        "|---|---|---|---|", "",
        "## 术语表(ASR 校对用)",
        *([f"- {t}" for t in r["terms"]] or ["-"]), "",
        "## 禁忌",
        *([f"- {t}" for t in r["taboo"]] or ["-"]),
        f"- Logo:{'需要' if r['logo'] else '无'}", "",
    ]
    return "\n".join(lines)


def render_table(r: dict, decisions: list[dict]) -> str:
    """推断表:字段 → 值 → 来源 → 是否推断 → 依据(--dry-run 打印,不落盘)。"""
    src_txt = {"user": "用户明说", "registry": "注册表/预设缺省", "default": "全局缺省"}
    head = f"{'字段':<14}{'值':<28}{'来源':<12}{'推断':<6}依据"
    rows = [head, "-" * 96]
    for d in decisions:
        val = str(d["value"])
        rows.append(f"{d['field']:<14}{val:<28}{src_txt[d['source']]:<12}"
                    f"{'是' if d['inferred'] else '否':<6}{d['why'][:52]}")
    rows.append("-" * 96)
    rows.append(f"风格条目:{r['styleEntry']}({r['styleLabel']}) → "
                f"{r['platform']}/{r['ratio']}/{r['subStyle']}/{r['pacing']}")
    return "\n".join(rows)


def intent_doc(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="compile", choices=["compile"],
                    help="compile=校验+补默认+查注册表+落 00_brief 产物(缺省即 compile)")
    ap.add_argument("--brief", required=True, help="brief.json(Agent 语义解析产物)")
    ap.add_argument("--plan", required=True, help="plan.json(keeps/字幕/卡片/音效意图)")
    ap.add_argument("--prompt", default="", help="原始提示词文件(仅作 decision_log 原文锚定)")
    ap.add_argument("--out", default=".", help="工程根目录(产物落 <out>/00_brief/)")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只打印推断表,不写任何文件")
    a = ap.parse_args()

    for label, p in (("brief", a.brief), ("plan", a.plan)):
        if not Path(p).is_file():
            return emit(False, "BAD_BRIEF", f"{label} 文件不存在:{p}", exit_code=2)
    try:
        brief = json.loads(Path(a.brief).read_text(encoding="utf-8"))
        plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return _bad([f"JSON 解析失败:{exc}"])
    if not isinstance(brief, dict) or not isinstance(plan, dict):
        return _bad(["brief.json / plan.json 必须是 JSON 对象"])
    prompt_text = ""
    if a.prompt:
        if not Path(a.prompt).is_file():
            return emit(False, "BAD_BRIEF", f"提示词文件不存在:{a.prompt}", exit_code=2)
        prompt_text = Path(a.prompt).read_text(encoding="utf-8")

    registry, platforms = load_registry(), load_platforms()
    if errs := validate(brief, plan, registry, platforms):
        return _bad(errs)
    try:
        out = compile_intent(brief, plan, prompt_text, registry, platforms)
    except ValueError as exc:
        return _bad([str(exc)])
    r, decisions = out["resolved"], out["decisions"]
    table = render_table(r, decisions)
    print(table)

    payload = {"version": 1, "kind": "cutflow-intent-decisions",
               "prompt": r["prompt"], "style": {
                   "id": r["styleEntry"], "label": r["styleLabel"], "platform": r["platform"],
                   "ratio": r["ratio"], "subStyle": r["subStyle"], "pacing": r["pacing"]},
               "resolved": r, "decisions": decisions}
    if a.dry_run:
        return emit(True, "INTENT_DRY_RUN",
                    f"[dry-run] 推断 {len(decisions)} 条(条目 {r['styleEntry']}),未写盘",
                    {"resolved": r, "decisions": decisions, "nDecisions": len(decisions)})

    brief_dir = Path(a.out) / "00_brief"
    brief_md, terms_txt = render_brief_md(r), "\n".join(r["terms"]) + ("\n" if r["terms"] else "")
    write_text_atomic(brief_dir / "brief.md", brief_md)
    write_text_atomic(brief_dir / "terms.txt", terms_txt)
    write_text_atomic(brief_dir / "intent_decisions.json", intent_doc(payload))
    return emit(True, "INTENT_COMPILED",
                f"意图编译完成:条目 {r['styleEntry']}({r['platformLabel']}/{r['ratio']}/"
                f"{r['subStyle']}/{r['pacing']}),决策 {len(decisions)} 条 → {brief_dir}",
                {"brief": str(brief_dir / "brief.md"), "terms": str(brief_dir / "terms.txt"),
                 "decisions": str(brief_dir / "intent_decisions.json"),
                 "nDecisions": len(decisions), "resolved": r})


if __name__ == "__main__":
    rs_common.ensure_utf8()
    sys.exit(main())
