"""N1 意图编译器(副文档 04 · 阶段四):提示词 → 结构化参数 → 现有 intake 产物。

用法:
  rs_intent.py compile --brief brief.json --plan plan.json --out <工程根> `
      [--prompt prompt.txt] [--dry-run]
  rs_intent.py compile --template mixcut-beat --out <工程根> [--auto-fill] `
      [--brief brief.json] [--plan plan.json]   # M6:模板骨架预填后走同一校验链(§5.11)
  rs_intent.py template list                    # M6:场景模板清单(含 triggers)
  rs_intent.py template show <id>               # 打印模板全文
  rs_intent.py template match "<模糊描述>"       # 模糊描述 → 命中模板 + 理由(确定性计分)

三段结构(参照 jianying-headless 的 compile 分层,只学方法):
  ① 提示词(自然语言)──Agent 语义解析──► ② brief.json + plan.json(结构化、可读、可改)
  ──本脚本确定性编译──► ③ 现有 S0–S11 机械臂(一行不改)。

分工铁律(SKILL.md §1.1 同口径):
  · Agent 只做「读懂提示词 → 写 brief.json + plan.json」与「写文案」两处语义工作;
  · 本脚本是确定性编译:校验必填与枚举 → 补默认 → 查风格注册表(registry.json)→
    落现有 intake 产物(00_制作简报/brief.md、terms.txt)→ 输出推断表;
  · 给定输入必得同一输出(纯函数式:产物与 intent_decisions.json 不含时间戳,
    同输入字节级一致,tests/test_v21_prompt_auto.py 断言)。

M6(ADR-0051 / 方案 §5.11)两处扩展,均不引入新编译路径:
  · 风格包:registry 条目的 pack 字段 → templates/styles/packs/<slug>/params.yaml
    是参数真身,compile 把它逐字段并入 resolved 并在 decision_log 留痕(why 携带
    params.yaml 的 [内部]/[经验] 出处注释);pack 缺失按 registry 原路径回退 +
    WARN stylePackMissing,行为与落地前一致;
  · 模板:--template 只把 brief/plan 骨架【预填】,之后仍走既有校验/补默认/查表/留痕;
    预填值在 decision_log 里 source=template(与 user/registry/default 并列第四种来源),
    并记录 templateQuote(命中的模板与键)。

一切「默认推断」显式标 inferred=true / source=user|registry|default|template,区分
「用户明说」;全量决策随 00_制作简报/intent_decisions.json 落盘,rs_run --auto 将其并入
05_时间线工程/pipeline.json 的 decision_log,N4 据此生成决策说明书。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rs_common  # noqa: E402
from rs_common import RATIOS, emit, write_text_atomic  # noqa: E402
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量
import rs_stylepack  # noqa: E402  — 风格包加载 API(ADR-0051;缺包返回 None,不抛)
from rs_subtitle import STYLES, load_platforms, resolve_platform  # noqa: E402
from rs_run import _PLATFORM_ALIASES, load_capability_descriptors  # noqa: E402
#  — 平台别名单一真相源 + 能力描述符注册表(ADR-0047,rs_run 与本文件共用一份加载器)

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "templates" / "styles" / "registry.json"
STYLES_DIR = Path(__file__).resolve().parents[1] / "templates" / "styles"
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "templates" / "prompts"

# brief/plan 显式给出的枚举口径(给出即校验,不给不臆测)
# M8:auto = 从内置 BGM 小曲库按节奏档选曲(曲库 skills/cutflow/assets/bgm/,自产无版权)
BGM_MODES = ("none", "provided", "cc0", "auto")
VOICE_SOURCES = ("original", "tts", "none")
DENSITIES = ("无", "少", "多")

# M8 内置 BGM 小曲库(清欠账 #13):manifest 真相源 + 确定性选曲
BGM_LIBRARY_DIR = Path(__file__).resolve().parents[1] / "assets" / "bgm"
BGM_LIBRARY_RELPREFIX = "skills/cutflow/assets/bgm"


def _bgm_tracks() -> list[dict]:
    """曲库全量曲目(M12 真相源 = 统一索引 skills/cutflow/assets/manifest.json 的
    kind=bgm;旧兼容件 bgm/manifest.json 兜底,R42 闭环的取数口)。"""
    import rs_asset
    tracks = [{"id": str(a.get("id")), "name": str(a.get("label", "")),
               "file": str(a.get("file", "")).rsplit("/", 1)[-1],
               "pacingFit": list(a.get("pacingFit") or []),
               "gainHintDb": a.get("gainHintDb", -18), "mood": str(a.get("mood", "")),
               "durationSec": a.get("durationSec"), "bpm": a.get("bpm"),
               "commercial": bool(a.get("commercial", True))}
              for a in rs_asset.assets("bgm")]
    if tracks:
        return tracks
    man = BGM_LIBRARY_DIR / "manifest.json"
    if not man.is_file():
        return []
    try:
        raw = json.loads(man.read_text(encoding="utf-8")).get("tracks") or []
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    out = []
    for t in raw:
        if isinstance(t, dict):
            out.append({**t, "id": str(t.get("id") or t.get("name") or ""),
                        "commercial": bool(t.get("commercial", True))})
    return out


def bgm_library_pick(pacing: str, mood: str | None = None) -> dict | None:
    """曲库按节奏档(可选 mood 语义)选曲,确定性:

    ① pacingFit 含当前档;② 有 mood 时优先 mood 命中(曲目 mood 串含该词)的首条;
    ③ 否则 pacing 命中首条;④ 都没有 → 首条(同输入字节级同选)。
    曲库缺失/坏 JSON/空表 → None(调用方留痕 bgm.src=null,渲染端不加 BGM,不臆测)。
    返回 {id, name, file, repoRelPath, bpm, durationSec, gainHintDb, mood, commercial}。
    """
    tracks = [t for t in _bgm_tracks() if t.get("commercial", True)]
    if not tracks:
        return None
    hits = [t for t in tracks if pacing in (t.get("pacingFit") or [])]
    pool = hits or tracks
    if mood:
        hit = next((t for t in pool if mood in str(t.get("mood", ""))), None)
        if hit is not None:
            pool = [hit]
    hit = pool[0]
    return {"id": hit.get("id", ""), "name": str(hit.get("name", "")),
            "file": str(hit.get("file", "")),
            "repoRelPath": f"{BGM_LIBRARY_RELPREFIX}/{hit.get('file', '')}",
            "bpm": hit.get("bpm"), "durationSec": hit.get("durationSec"),
            "gainHintDb": hit.get("gainHintDb", -18), "mood": str(hit.get("mood", "")),
            "commercial": hit.get("commercial", True)}


def _alias(name: str) -> str:
    """平台别名 → 规范键(与 rs_run 参数解析同一张表,单一真相源)。"""
    return _PLATFORM_ALIASES.get(str(name), _PLATFORM_ALIASES.get(str(name).lower(), name))


def _fx_executable(fx_id: str) -> bool:
    """fxId 在效果目录中是否可执行(防御性:目录缺失/坏档 → False,不臆测)。"""
    try:
        import rs_effects  # noqa: PLC0415 — 懒加载防重导入开销
    except Exception:  # noqa: BLE001
        return False
    return rs_effects.is_executable(fx_id)


def build_effects_plan(prescription: dict, brief: dict) -> dict:
    """效果计划草案(ADR-0059/分册06 §9.2):按处方 + 纸面结构在开工前规划"该在哪用什么"。

    确定性:structure 分段数、prefer 首个【可执行】fxId(catalog 查证,登记待实现
    的跳过)共同决定,同输入同输出。草案是推荐,不是强制 —— apply 侧仍走 EditOp。
    """
    structure = str(brief.get("structure") or "")
    segs = [s.strip() for s in re.split(r"→|->", structure) if s.strip()]
    n_bounds = max(len(segs) - 1, 0)
    tr = prescription.get("transition") or {}
    tr_pref = [f for f in (tr.get("prefer") or []) if _fx_executable(str(f))]
    trans: list[dict] = []
    if tr_pref and n_bounds > 0:
        # 章节切换点 = 结构分段边界(全部列出;min 是下限,边界是上限,不虚构)
        trans.append({"atChapters": list(range(1, n_bounds + 1)),
                      "fx": tr_pref[0],
                      "reason": "章节切换(结构分段边界;分册06 §3 时空跳跃/段落切换语义)"})
    ins = prescription.get("in") or {}
    outs = prescription.get("out") or {}
    in_pref = [f for f in (ins.get("prefer") or []) if _fx_executable(str(f))]
    out_pref = [f for f in (outs.get("prefer") or []) if _fx_executable(str(f))]
    sfx = prescription.get("sfx") or {}
    return {
        "transitions": trans,
        "in": ([{"scope": "subtitle_bar", "fx": in_pref[0]}] if in_pref else []),
        "out": ([{"scope": "subtitle_bar", "fx": out_pref[0]}] if out_pref else []),
        "sfx": ([{"usage": "transition",
                  "density": f"per15s<={sfx.get('per15s', 2)}"}] if sfx else []),
        "flashyMax": prescription.get("flashy_max"),
        "requireReason": bool(prescription.get("require_reason", True)),
    }


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
    # ADR-0047:videoTypes.<id>.capabilities 必须指向已登记的能力描述符(诚实纪律:
    # detector 未落地的能力不登记,声明了未登记能力 = 编译期即失败,不留到运行期)
    if vt and vt in registry["videoTypes"]:
        known = load_capability_descriptors()
        for cap in registry["videoTypes"][vt].get("capabilities") or []:
            if cap not in known:
                errs.append(f"videoTypes.{vt} 声明了未登记能力:{cap!r}(已登记:"
                            + ("/".join(sorted(known)) or "无") + ";先在 "
                            "templates/capabilities/ 落描述符)")
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
                   registry: dict, platforms: dict,
                   template_info: dict | None = None) -> dict:
    """brief+plan(+提示词原文)(+模板/风格包信息)→ resolved + decisions。

    不落盘、不含时间戳;同一份输入必得同一字节输出。template_info 结构:
      {"id", "label", "brief_keys"(骨架顶层键), "plan_keys", "user_brief", "user_plan",
       "auto_fill"} —— 仅 compile --template 时非 None。
    """
    decisions: list[dict] = []

    def dec(field: str, value, source: str, why: str, quote: str = "") -> None:
        decisions.append({"id": f"intent:{field}", "field": field, "value": value,
                          "source": source, "inferred": source != "user",
                          "why": why, "promptQuote": quote})

    # 模板预填来源判定:某字段由骨架提供且未被用户文件覆盖 → source=template + templateQuote。
    ukeys_b = set((template_info or {}).get("user_brief") or [])
    ukeys_p = set((template_info or {}).get("user_plan") or [])

    def tpl_src(kind: str, top: str, path: str, present: bool, value) -> tuple[str, str]:
        """(source, quote):present=该值确实来自 brief/plan 的这一字段。"""
        if not template_info or not present:
            return "", ""
        keys = set(template_info.get(f"{kind}_keys") or [])
        user_keys = ukeys_b if kind == "brief" else ukeys_p
        if top in keys and top not in user_keys:
            q = (f"prompts/{template_info['id']}.md#{kind}_skeleton.{path}="
                 + json.dumps(value, ensure_ascii=False, sort_keys=True))
            return "template", q
        return "", ""

    entry, basis, quote = match_entry(brief, registry)
    vt = brief["videoType"]
    tsrc, tquote = tpl_src("brief", "videoType", "videoType", "videoType" in brief, vt)
    if tsrc:
        dec("videoType", vt, "template",
            f"模板 {template_info['id']} 预填 brief_skeleton.videoType(决定分册与管线分支)", tquote)
    else:
        dec("videoType", vt, "user", "决定 rules/video-types 分册与管线分支(S0 问卷第 0 问)")
    dec("styleEntry", entry["id"], "registry", basis, quote)

    # ADR-0047:videoType 声明的 capabilities 是数据,引擎(挂载器)只认识
    # 「阶段」与「能力」;逐条进 decision_log(source=registry),同输入字节级可复现。
    caps = [str(c) for c in registry["videoTypes"][vt].get("capabilities") or []]
    for cap in caps:
        dec(f"capability.{cap}", cap, "registry",
            f"注册表 videoTypes.{vt} 声明的能力(挂载器按描述符 stage 挂到阶段)")

    # ADR-0051:定位风格包(registry 条目 pack 字段 → packs/<slug>/params.yaml 参数真身)。
    pack_slug = str(entry.get("pack") or "")
    pack = rs_stylepack.load_pack(pack_slug) if pack_slug else None
    pack_warns: list[str] = []
    if pack_slug and pack is None:
        pack_warns.append(
            f"stylePackMissing:风格包 {pack_slug} 不存在({rs_stylepack.PACKS_DIR / pack_slug});"
            f"按注册表条目 {entry['id']} 原路径回退,行为与 ADR-0051 落地前一致")
    pp: dict = (pack or {}).get("params") or {}
    psrc: dict = (pack or {}).get("sources") or {}
    pack_sub: dict = pp.get("subtitle") or {}
    pack_bgm: dict = pp.get("bgm") or {}
    pack_cards: dict = pp.get("cards") or {}

    # 平台:用户明说 > 注册表条目 > douyin(与 rs_run 参数缺省一致)
    plat_key = _alias(brief["platform"]) if brief.get("platform") else entry["platform"]
    preset = resolve_platform(plat_key)
    tsrc, tquote = tpl_src("brief", "platform", "platform", "platform" in brief, plat_key)
    if tsrc:
        dec("platform", plat_key, "template", f"模板 {template_info['id']} 预填 brief_skeleton.platform", tquote)
    else:
        plat_src = "user" if brief.get("platform") else "registry"
        dec("platform", plat_key, plat_src,
            "平台预设决定画幅/安全区/每卡字数(templates/platforms.json)"
            if plat_src == "user" else f"注册表条目 {entry['id']} 的平台缺省")

    # 画幅:用户明说 > 平台预设(画幅由平台供给,registry _doc 口径)> 注册表条目
    ratio = brief.get("ratio") or preset.get("ratio") or entry["ratio"] or "9x16"
    tsrc, tquote = tpl_src("brief", "ratio", "ratio", "ratio" in brief, ratio)
    if tsrc:
        dec("ratio", ratio, "template", f"模板 {template_info['id']} 预填 brief_skeleton.ratio", tquote)
    else:
        ratio_src = "user" if brief.get("ratio") else "registry"
        why = "用户显式画幅,优先于平台预设" if ratio_src == "user" else \
            (f"平台预设 {plat_key}.ratio(templates/platforms.json)" if ratio == preset.get("ratio")
             else f"注册表条目 {entry['id']} 的画幅缺省")
        dec("ratio", ratio, ratio_src, why)

    plan_sub = plan.get("subtitle") or {}
    sub_style = (plan_sub.get("style") or pack_sub.get("style")
                 or entry["style"] or preset.get("style") or "subtitle-white")
    tsrc, tquote = tpl_src("plan", "subtitle", "subtitle.style", bool(plan_sub.get("style")), sub_style)
    if tsrc:
        dec("subStyle", sub_style, "template", f"模板 {template_info['id']} 预填 plan_skeleton.subtitle.style", tquote)
    elif plan_sub.get("style"):
        dec("subStyle", sub_style, "user", "plan 显式字幕样式")
    else:
        from_pack = bool(pack_sub.get("style"))
        dec("subStyle", sub_style, "registry",
            (f"风格包 {pack_slug}/params.yaml subtitle.style({psrc.get('subtitle', '')})"
             if from_pack else
             f"注册表条目 {entry['id']} 的字幕样式(样式模板 {entry.get('styleTemplate')})"))

    # 每卡字数裁决序:plan/模板显式 > 包为「自身平台」调的值 > 平台预设(用户换平台
    # 时权威,包不越平台)> 包值兜底 > 12 —— pack 的 maxChars 是给它自家平台调的,
    # 用户显式换平台(v21 门禁用例:xiaohongshu 15 / bilibili 22)时必须让位平台预设。
    pack_tuned = pack is not None and plat_key == pp.get("platform")
    max_chars = (plan_sub.get("maxChars")
                 or (pack_sub.get("maxChars") if pack_tuned else None)
                 or preset.get("maxChars")
                 or pack_sub.get("maxChars") or 12)
    tsrc, tquote = tpl_src("plan", "subtitle", "subtitle.maxChars", bool(plan_sub.get("maxChars")), max_chars)
    if tsrc:
        dec("maxChars", int(max_chars), "template", f"模板 {template_info['id']} 预填 plan_skeleton.subtitle.maxChars", tquote)
    elif plan_sub.get("maxChars"):
        dec("maxChars", int(max_chars), "user", "plan 显式每卡字数")
    elif pack_tuned and pack_sub.get("maxChars"):
        dec("maxChars", int(max_chars), "registry",
            f"风格包 {pack_slug}/params.yaml subtitle.maxChars({psrc.get('subtitle', '')})")
    elif preset.get("maxChars"):
        dec("maxChars", int(max_chars), "registry",
            f"平台预设 {plat_key}.maxChars(templates/platforms.json)")
    elif pack_sub.get("maxChars"):
        dec("maxChars", int(max_chars), "registry",
            f"风格包 {pack_slug}/params.yaml subtitle.maxChars({psrc.get('subtitle', '')})")
    else:
        dec("maxChars", int(max_chars), "registry", "全库缺省每卡字数")
    cps = plan_sub.get("cpsMax") or pack_sub.get("cpsMax") or 9.0
    tsrc, tquote = tpl_src("plan", "subtitle", "subtitle.cpsMax", bool(plan_sub.get("cpsMax")), cps)
    if tsrc:
        dec("cpsMax", float(cps), "template", f"模板 {template_info['id']} 预填 plan_skeleton.subtitle.cpsMax", tquote)
    elif plan_sub.get("cpsMax"):
        dec("cpsMax", float(cps), "user", "plan 显式 CPS 上限")
    elif pack_sub.get("cpsMax"):
        dec("cpsMax", float(cps), "registry",
            f"风格包 {pack_slug}/params.yaml subtitle.cpsMax({psrc.get('subtitle', '')})")
    else:
        dec("cpsMax", float(cps), "default", "全库统一 CPS ≤9(字幕三定律)")

    # 节奏档:plan/brief 明说 > 风格包 > 注册表条目
    pace_key = plan.get("pacing") or brief.get("pacing") or pp.get("pacing") or entry["pacing"]
    tier = registry["pacingTiers"][pace_key]
    pace_user = bool(plan.get("pacing") or brief.get("pacing"))
    tsrc, tquote = tpl_src("plan", "pacing", "pacing", bool(plan.get("pacing")), pace_key)
    if not tsrc and not pace_user:
        tsrc, tquote = tpl_src("brief", "pacing", "pacing", bool(brief.get("pacing")), pace_key)
    if tsrc:
        dec("pacing", pace_key, "template", f"模板 {template_info['id']} 预填骨架 pacing", tquote)
    elif pace_user:
        dec("pacing", pace_key, "user", "plan/brief 显式节奏档")
    else:
        from_pack = bool(pp.get("pacing"))
        dec("pacing", pace_key, "registry",
            (f"风格包 {pack_slug}/params.yaml pacing({psrc.get('pacing', '')})"
             if from_pack else f"注册表条目 {entry['id']} 的节奏档"))

    bgm_default_on = (bool(pack_bgm.get("enabled"))
                      or bool((entry.get("bgmLibrary") or {}).get("enabled"))
                      or bool(tier.get("bgm")))
    bgm_mode = brief.get("bgm") or ("cc0" if bgm_default_on else "none")
    bgm_on = bgm_mode != "none"
    bgm_gain = pack_bgm.get("gainDb",
                            (entry.get("bgmLibrary") or {}).get("gainDb", tier.get("bgmGainDb", -18)))
    tsrc, tquote = tpl_src("brief", "bgm", "bgm", "bgm" in brief, bgm_mode)
    if tsrc:
        dec("bgm", bgm_mode, "template", f"模板 {template_info['id']} 预填 brief_skeleton.bgm", tquote)
    elif brief.get("bgm"):
        dec("bgm", bgm_mode, "user", "brief 显式 BGM 意图")
    else:
        from_pack = bool(pack_bgm.get("enabled")) or "gainDb" in pack_bgm
        dec("bgm", bgm_mode, "registry",
            (f"风格包 {pack_slug}/params.yaml bgm({psrc.get('bgm', '')})"
             if from_pack else
             f"条目/节奏档缺省({pace_key} 档 bgm={'开' if tier.get('bgm') else '关'})"))

    dur = brief.get("durationTarget") or preset.get("durationHint") or "30-60s"
    dec("durationTarget", dur, "user" if brief.get("durationTarget") else "registry",
        "brief 显式目标时长" if brief.get("durationTarget") else f"平台预设 {plat_key}.durationHint")

    pack_density = pack_cards.get("density")
    density = brief.get("density") or pack_density or entry.get("cards", {}).get("density") or "少"
    tsrc, tquote = tpl_src("brief", "density", "density", "density" in brief, density)
    if tsrc:
        dec("density", density, "template", f"模板 {template_info['id']} 预填 brief_skeleton.density", tquote)
    elif brief.get("density"):
        dec("density", density, "user", "brief 显式穿插动画密度")
    elif pack_density:
        dec("density", density, "registry",
            f"风格包 {pack_slug}/params.yaml cards.density({psrc.get('cards', '')})")
    else:
        dec("density", density, "registry", f"注册表条目 {entry['id']} 的卡片密度")

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

    # --auto-fill:补便利缺省(title ← 模板 label);预填值同样 source=template 留痕。
    if template_info and template_info.get("auto_fill"):
        if not brief.get("title"):
            auto_title = str(template_info.get("label") or template_info["id"])
            brief = {**brief, "title": auto_title}
            dec("title", auto_title, "template",
                f"模板 {template_info['id']} --auto-fill 预填 title(=模板 label)",
                f"prompts/{template_info['id']}.md#auto-fill.title")

    resolved = {
        "videoType": vt,
        "videoTypeLabel": registry["videoTypes"][vt]["label"],
        "doc": registry["videoTypes"][vt]["doc"],
        "capabilities": caps,
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
        "cardMs": list(pp.get("cardMs") or tier["cardMs"]),
        "visualBeatSec": list(pp.get("visualBeatSec") or tier["visualBeatSec"]),
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

    # M8 内置 BGM 小曲库(清欠账 #13):bgm=auto → 按节奏档确定性选曲,选中文件
    # (仓库相对路径)写进 resolved.bgm.src,decision_log source=library 留痕。
    # 增益仍以节奏档/风格包为准(pick.gainHintDb 只是曲库参考值,不覆盖)。
    if bgm_mode == "auto":
        # R42(M12):风格包 bgmLibrary 声明的 mood 语义参与选曲(仍是确定性首条),
        # 选中素材 id 写进 resolved.bgm.assetId → 交付归因/rs_verify 商用对拍同源。
        lib_rule = (entry.get("bgmLibrary") or {}) if entry else {}
        pick = bgm_library_pick(pace_key, mood=lib_rule.get("mood"))
        resolved["bgm"]["src"] = pick["repoRelPath"] if pick else None
        resolved["bgm"]["assetId"] = pick["id"] if pick else None
        resolved["bgm"]["pick"] = pick
        dec("bgm.pick", pick, "library",
            (f"曲库按节奏档 {pace_key}"
             + (f"+mood {lib_rule['mood']}" if lib_rule.get("mood") else "")
             + f" 选曲:{pick['name']}({pick['file']},"
             f"bpm {pick['bpm']});来源 {pick.get('commercial', True) and '可商用' or '不可商用'}")
            if pick else f"曲库缺失或为空({BGM_LIBRARY_DIR}),未选曲(bgm.src=null)")

    # 风格包留痕(ADR-0051):pack 命中时逐字段并入 resolved + decision_log;
    # pack 缺失/条目无 pack 时本块整体缺席,resolved 与落地前字节一致(回退可复现)。
    if pack is not None:
        resolved["stylePack"] = pack_slug
        resolved["lyricOnly"] = bool(pack_sub.get("lyricOnly"))
        resolved["transition"] = pp.get("transition")
        ab = pp.get("artboard") or {}
        pack_cards_yaml = (pack.get("cards") or {}).get("artboard") or {}
        resolved["artboard"] = {"styles": list(ab.get("styles") or []),
                                "templates": list(ab.get("templates") or []),
                                "cases": list(pack_cards_yaml.get("cases") or [])}
        dec("stylePack", pack_slug, "registry",
            f"注册表条目 {entry['id']} 的 pack 字段 → packs/{pack_slug}/params.yaml(参数真身)")
        if pp.get("cardMs") and pp["cardMs"] != list(tier["cardMs"]):
            dec("cardMs", list(pp["cardMs"]), "registry",
                f"风格包 {pack_slug}/params.yaml cardMs({psrc.get('cardMs', '')});覆盖节奏档 {pace_key} 的 {tier['cardMs']}")
        if pp.get("visualBeatSec") and pp["visualBeatSec"] != list(tier["visualBeatSec"]):
            dec("visualBeatSec", list(pp["visualBeatSec"]), "registry",
                f"风格包 {pack_slug}/params.yaml visualBeatSec({psrc.get('visualBeatSec', '')})")
        dec("transition", pp.get("transition"), "registry",
            f"风格包 {pack_slug}/params.yaml transition({psrc.get('transition', '')})")
        dec("artboard", resolved["artboard"], "registry",
            f"风格包 {pack_slug} 的 artboard 借格(卡片视觉来源;cases 与 packs/{pack_slug}/cards.yaml 同源)")

    # ADR-0059(分册06 §7/§9):效果处方进 resolved + 效果计划草案;
    # 处方是 rs_verify EFFECTS_* 门禁与 rs_edit context「该用的特效」段的依据;
    # 无处方 → 门禁跳过留痕 NO_PRESCRIPTION,旧工程行为不变(防御性缺省)。
    pres = pp.get("effects_prescription")
    if isinstance(pres, dict) and pres:
        resolved["effectsPrescription"] = pres
        eplan = build_effects_plan(pres, brief)
        resolved["effectsPlan"] = eplan
        dec("effectsPrescription", pres, "prescription",
            f"风格包 {pack_slug}/params.yaml effects_prescription"
            "(分册06 §7;处方既是推荐也是门禁依据)")
        dec("effectsPlan", eplan, "prescription",
            "效果计划草案(处方+纸面结构;效果开工前规划,非最后才想起)")

    return {"resolved": resolved, "decisions": decisions, "entry": entry,
            "warnings": pack_warns}


# ---------------------------------------------------------------- 产物渲染

def render_brief_md(r: dict) -> str:
    bgm = r["bgm"]
    pace_line = (f"- 节奏档:{r['pacing']}(卡 {r['cardMs'][0]}–{r['cardMs'][1]}ms;"
                 f"视觉节拍 {r['visualBeatSec'][0]}–{r['visualBeatSec'][1]}s;"
                 f"BGM {'开,增益 %ddB' % bgm['gainDb'] if bgm['enabled'] else '关'})")
    voice_txt = {"original": "原声", "tts": f"TTS 音色卡({r['voice']['ttsVoice'] or 'koubo-test'})",
                 "none": "无声+BGM"}[r["voice"]["source"]]
    pack_line = ""
    if r.get("stylePack"):
        pack_line = f"- 风格包:{r['stylePack']}(packs/{r['stylePack']}/params.yaml;出处见 intent_decisions.json)\n"
    bgm_pick_line = ""
    if bgm.get("src"):
        pick = bgm.get("pick") or {}
        bgm_pick_line = (f"- BGM 选曲:{pick.get('name', '')}({pick.get('file', '')};"
                         f"bpm {pick.get('bpm')};曲库自产无版权,增益按节奏档 {bgm['gainDb']}dB)\n")
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
        pack_line.rstrip("\n") if pack_line else
        f"- 风格条目:{r['styleEntry']}({r['styleLabel']};templates/styles/registry.json)",
        bgm_pick_line.rstrip("\n") if bgm_pick_line else "", "",
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
    src_txt = {"user": "用户明说", "registry": "注册表/预设缺省", "default": "全局缺省",
               "template": "模板预填", "library": "内置曲库(M8 bgm=auto)",
               "prescription": "效果处方(风格包,ADR-0059)"}
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


# ---------------------------------------------------------------- template 子命令(§5.11)

def cmd_template(args: list[str]) -> int:
    """rs_intent.py template list | show <id> | match "<模糊描述>"。"""
    if not args:
        return emit(False, "BAD_ARGS",
                    "用法:rs_intent.py template list | show <id> | match \"<模糊描述>\"", exit_code=2)
    sub = args[0]
    if sub == "list":
        tpls = rs_stylepack.list_templates()
        print(f"{'id':<26}{'label':<16}{'pack':<28}{'videoType':<18}triggers")
        for t in tpls:
            print(f"{t['id']:<26}{str(t.get('label', '')):<16}"
                  f"{(t.get('pack') or '-'):<28}{str(t.get('videoType', '')):<18}"
                  + "、".join(t.get("triggers") or []))
        return emit(True, "TEMPLATE_LIST",
                    f"共 {len(tpls)} 个场景模板(方案 §5.11;_custom.md 为定制骨架,复制即新增)",
                    {"templates": [{"id": t["id"], "label": t.get("label"), "pack": t.get("pack"),
                                    "videoType": t.get("videoType"), "triggers": t.get("triggers")}
                                   for t in tpls]})
    if sub == "show":
        if len(args) < 2:
            return emit(False, "BAD_ARGS", "用法:rs_intent.py template show <id>", exit_code=2)
        tpl = rs_stylepack.load_template(args[1])
        if tpl is None:
            return emit(False, "TEMPLATE_NOT_FOUND",
                        f"模板不存在:{args[1]}(rs_intent.py template list 查看全部)", exit_code=2)
        print(tpl.get("_body", ""))
        fm = {k: v for k, v in tpl.items() if k != "_body"}
        return emit(True, "TEMPLATE_SHOWN", f"模板 {args[1]} 全文(front matter 见 data,正文已打印)",
                    {"template": fm})
    if sub == "match":
        if len(args) < 2:
            return emit(False, "BAD_ARGS", "用法:rs_intent.py template match \"<模糊描述>\"", exit_code=2)
        text = " ".join(args[1:])
        tpl, hits, why = rs_stylepack.match_template(text)
        print(f"描述:{text}")
        print(f"命中:{tpl['id'] + ' — ' + str(tpl.get('label')) if tpl else '(无)'}")
        print(f"理由:{why}")
        if tpl is None:
            return emit(False, "TEMPLATE_NO_MATCH", f"没有 triggers 命中:{text!r}",
                        {"matched": None, "why": why}, exit_code=2)
        return emit(True, "TEMPLATE_MATCH", f"命中模板 {tpl['id']}",
                    {"matched": tpl["id"], "hits": hits, "why": why,
                     "pack": tpl.get("pack") or "", "videoType": tpl.get("videoType")})
    return emit(False, "BAD_ARGS",
                f"未知 template 子命令:{sub}(可选 list/show/match)", exit_code=2)


# ---------------------------------------------------------------- CLI

def _read_json_file(label: str, path: str) -> tuple[dict | None, int | None]:
    """读 brief/plan JSON;失败时返回 (None, 退出码)。"""
    if not Path(path).is_file():
        return None, emit(False, "BAD_BRIEF", f"{label} 文件不存在:{path}", exit_code=2)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return None, _bad([f"JSON 解析失败:{exc}"])
    if not isinstance(data, dict):
        return None, _bad([f"{label} 必须是 JSON 对象"])
    return data, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", nargs="?", default="compile", choices=["compile", "template"],
                    help="compile=校验+补默认+查注册表+落 00_制作简报 产物(缺省即 compile);"
                         "template=场景提示词模板库(M6,§5.11)")
    ap.add_argument("subargs", nargs="*", help="template 子参数:list | show <id> | match <模糊描述>")
    ap.add_argument("--brief", default="", help="brief.json(Agent 语义解析产物;--template 预填时可省)")
    ap.add_argument("--plan", default="", help="plan.json(keeps/字幕/卡片/音效意图;--template 预填时可省)")
    ap.add_argument("--prompt", default="", help="原始提示词文件(仅作 decision_log 原文锚定)")
    ap.add_argument("--out", default=".", help="工程根目录(产物落 <out>/00_制作简报/)")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只打印推断表,不写任何文件")
    ap.add_argument("--template", dest="template_id", default="",
                    help="compile:先以 templates/prompts/<id>.md 骨架预填 brief/plan,再走既有校验链")
    ap.add_argument("--auto-fill", dest="auto_fill", action="store_true",
                    help="compile --template:补便利缺省(title←模板 label);预填值 source=template 留痕")
    a = ap.parse_args()

    if a.command == "template":
        return cmd_template(a.subargs)

    # ---- 组装 brief/plan:--template 骨架预填(用户文件可再覆盖)> 用户文件 ----
    template_info: dict | None = None
    if a.template_id:
        tpl = rs_stylepack.load_template(a.template_id)
        if tpl is None:
            return emit(False, "TEMPLATE_NOT_FOUND",
                        f"模板不存在:{a.template_id}(rs_intent.py template list 查看全部)", exit_code=2)
        sk_brief = dict(tpl.get("brief_skeleton") or {})
        sk_plan = dict(tpl.get("plan_skeleton") or {})
        user_brief_keys: set = set()
        user_plan_keys: set = set()
        if a.brief:
            user_brief, rc = _read_json_file("brief", a.brief)
            if rc is not None:
                return rc
            user_brief_keys = set(user_brief)
            sk_brief.update(user_brief)
        if a.plan:
            user_plan, rc = _read_json_file("plan", a.plan)
            if rc is not None:
                return rc
            user_plan_keys = set(user_plan)
            sk_plan.update(user_plan)
        brief, plan = sk_brief, sk_plan
        template_info = {"id": tpl["id"], "label": str(tpl.get("label") or ""),
                         "brief_keys": set(tpl.get("brief_skeleton") or {}),
                         "plan_keys": set(tpl.get("plan_skeleton") or {}),
                         "user_brief": user_brief_keys, "user_plan": user_plan_keys,
                         "auto_fill": bool(a.auto_fill)}
    else:
        if not a.brief or not a.plan:
            return emit(False, "BAD_BRIEF",
                        "compile 需要 --brief/--plan(或用 --template <id> 从场景模板骨架预填)",
                        {"hint": "rs_intent.py template list 查看全部模板"}, exit_code=2)
        brief, rc = _read_json_file("brief", a.brief)
        if rc is not None:
            return rc
        plan, rc = _read_json_file("plan", a.plan)
        if rc is not None:
            return rc
    prompt_text = ""
    if a.prompt:
        if not Path(a.prompt).is_file():
            return emit(False, "BAD_BRIEF", f"提示词文件不存在:{a.prompt}", exit_code=2)
        prompt_text = Path(a.prompt).read_text(encoding="utf-8")

    registry, platforms = load_registry(), load_platforms()
    if errs := validate(brief, plan, registry, platforms):
        return _bad(errs)
    try:
        out = compile_intent(brief, plan, prompt_text, registry, platforms, template_info)
    except ValueError as exc:
        return _bad([str(exc)])
    r, decisions = out["resolved"], out["decisions"]
    pack_warns = out.get("warnings") or []
    table = render_table(r, decisions)
    print(table)
    for w in pack_warns:
        print(f"WARN {w}")

    payload = {"version": 1, "kind": "cutflow-intent-decisions",
               "prompt": r["prompt"], "style": {
                   "id": r["styleEntry"], "label": r["styleLabel"], "platform": r["platform"],
                   "ratio": r["ratio"], "subStyle": r["subStyle"], "pacing": r["pacing"]},
               "resolved": r, "decisions": decisions}
    if template_info is not None:
        payload["template"] = {"id": template_info["id"], "autoFill": bool(template_info.get("auto_fill"))}
    if a.dry_run:
        return emit(True, "INTENT_DRY_RUN",
                    f"[dry-run] 推断 {len(decisions)} 条(条目 {r['styleEntry']}),未写盘",
                    {"resolved": r, "decisions": decisions, "nDecisions": len(decisions),
                     "warnings": pack_warns})

    brief_dir = Path(a.out) / rs_paths.p("brief")
    brief_md, terms_txt = render_brief_md(r), "\n".join(r["terms"]) + ("\n" if r["terms"] else "")
    write_text_atomic(brief_dir / "brief.md", brief_md)
    write_text_atomic(brief_dir / "terms.txt", terms_txt)
    write_text_atomic(brief_dir / "intent_decisions.json", intent_doc(payload))
    return emit(True, "INTENT_COMPILED",
                f"意图编译完成:条目 {r['styleEntry']}({r['platformLabel']}/{r['ratio']}/"
                f"{r['subStyle']}/{r['pacing']}),决策 {len(decisions)} 条 → {brief_dir}",
                {"brief": str(brief_dir / "brief.md"), "terms": str(brief_dir / "terms.txt"),
                 "decisions": str(brief_dir / "intent_decisions.json"),
                 "nDecisions": len(decisions), "resolved": r, "warnings": pack_warns})


if __name__ == "__main__":
    rs_common.ensure_utf8()
    sys.exit(main())
