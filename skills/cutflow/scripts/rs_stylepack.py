"""风格包加载/校验/脚手架(ADR-0051:CutFlow ↔ artboard 共用 slug 命名空间)。

用法:
  rs_stylepack.py check                  # 全量校验:三处同名对拍 + 引用真实性 + 提示词模板结构
  rs_stylepack.py show <slug>            # 打印风格包摘要(参数/卡片映射/出处标注)
  rs_stylepack.py new <slug> [--label 中文名] [--video-type KEY] [--artboard-slug SLUG]
                                         # 从 _template 脚手架新包(六步之第 1 步)

风格包 = templates/styles/packs/<slug>/{params.yaml, cards.yaml, README.md, frames.css}。
三处同名对拍(门禁 #13,params/cards/registry/artboard 四向):
  · packs/<slug>/params.yaml 存在且 slug 与目录同名;
  · registry.entries[] 有 pack==slug(双向:文档有参数无 / 参数有文档无都红);
  · pack.videoType 已在 registry.videoTypes 登记;
  · cards.yaml 的 artboard.styles/cases 指向的 artboard 技能文件真实存在(artboard 缺席 SKIP)。

加载 API(供 rs_intent 等消费者使用,缺包返回 None 不抛):
  load_pack(slug) -> dict | None
  load_template(tid) -> dict | None       # templates/prompts/<tid>.md 的 front matter
  match_template(text) -> (tpl | None, hits, why)   # triggers 确定性计分

本文件不做任何工程阶段路径操作(阶段目录一律走 rs_paths,与本文件无关)。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import RATIOS, emit  # noqa: E402
from rs_subtitle import STYLES, load_platforms  # noqa: E402

STYLES_DIR = Path(__file__).resolve().parents[1] / "templates" / "styles"
PACKS_DIR = STYLES_DIR / "packs"
TEMPLATE_DIR = STYLES_DIR / "_template"
REGISTRY_PATH = STYLES_DIR / "registry.json"
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "templates" / "prompts"
SCHEMA_PATH = PROMPTS_DIR / "_schema.json"

# 风格包 slug 命名法:<领域>-<调性>,小写连字符,≥2 段(与 artboard 风格 slug 同构)。
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")
# 提示词模板 id:小写连字符(允许单词段,如 custom)。
TEMPLATE_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
REQUIRED_PARAM_FIELDS = ("slug", "label", "videoType", "platform", "ratio", "pacing",
                         "cardMs", "visualBeatSec", "bgm", "transition", "subtitle",
                         "cards", "capabilities", "artboard")
CARD_KINDS = ("opener", "title", "section", "stat", "compare", "endcard")
DUCKING_MODES = ("none", "on_voice_only")
REQUIRED_TEMPLATE_FIELDS = ("id", "label", "triggers", "pack", "videoType", "brief_skeleton",
                            "plan_skeleton", "assets_required", "assets_forbidden",
                            "example_prompts", "acceptance")


# ---------------------------------------------------------------- YAML(双通道:PyYAML 优先,子集解析器兜底)

def _split_flow_items(s: str) -> list[str]:
    """行内 [..] / {..} 内容按顶层逗号切分(引号内逗号不切)。"""
    items, buf, quote, depth = [], "", "", 0
    for ch in s:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf += ch
            continue
        if ch in "[{":
            depth += 1
        if ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(buf)
            buf = ""
            continue
        buf += ch
    if buf.strip():
        items.append(buf)
    return items


def _flow_scalar(tok: str):
    """行内标量:剥引号 + YAML 风格的布尔/数字归一(其余保持字符串)。"""
    tok = tok.strip()
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    low = tok.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(tok)
    except ValueError:
        pass
    try:
        return float(tok)
    except ValueError:
        pass
    return tok


def _strip_comment(line: str) -> str:
    """去行尾注释(# 不在引号内才算注释;缩进注释行整行变空)。"""
    out, quote = "", ""
    for ch in line:
        if quote:
            out += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            out += ch
            continue
        if ch == "#":
            break
        out += ch
    return out.rstrip()


def _flow_value(tok: str):
    """行内值:列表 / 字典 / 标量。"""
    tok = tok.strip()
    if tok.startswith("[") and tok.endswith("]"):
        return [_flow_value(x) for x in _split_flow_items(tok[1:-1]) if x.strip()]
    if tok.startswith("{") and tok.endswith("}"):
        out: dict = {}
        for item in _split_flow_items(tok[1:-1]):
            if not item.strip():
                continue
            k, _, v = item.partition(":")
            out[str(_flow_scalar(k))] = _flow_value(v)
        return out
    return _flow_scalar(tok)


def _mini_yaml(text: str):
    """YAML 子集解析器(无 PyYAML 环境的兜底,ADR-0049 懒加载纪律):
    块映射(任意层)+ 块标量列表(`- item`)+ 行内列表/字典 + 引号标量 + 注释;
    不支持嵌套列表项/多行标量/锚点等完整语法 —— 风格包与模板文件体例受
    _template/_schema 约束,够用即诚实。"""
    root: dict = {}
    stack = [(-1, root)]
    pending: tuple[int, dict, str] | None = None   # (宿主键缩进, 宿主容器, 键)——块列表挂点
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        if body.startswith("- "):
            if pending is None:
                raise ValueError(f"块列表项缺少宿主键:{body!r}")
            host_indent, host_parent, host_key = pending
            if indent <= host_indent:
                raise ValueError(f"块列表项缩进不合体例:{body!r}")
            cur = host_parent[host_key]
            if not isinstance(cur, list):
                host_parent[host_key] = cur = []
            cur.append(_flow_value(body[2:].strip()))
            continue
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        container = stack[-1][1]
        key, _, val = body.partition(":")
        key = _flow_scalar(key)
        val = val.strip()
        pending = None
        if val == "":
            child: dict = {}
            container[key] = child
            stack.append((indent, child))
            pending = (indent, container, key)     # 后续 `- item` 行挂到本键
        else:
            container[key] = _flow_value(val)
    return root


def _load_yaml(text: str):
    """PyYAML 优先(与 test_v21 同口径);ImportError 时退回子集解析器。"""
    try:
        import yaml  # noqa: PLC0415 — 懒加载,缺库走兜底
    except ImportError:
        return _mini_yaml(text)
    return yaml.safe_load(text)


def param_sources(text: str) -> dict[str, str]:
    """params.yaml 顶层字段的行内出处注释(`key: value # [内部]/[经验] …`)。

    供 decision_log 留痕:注释即「为什么是这个值」的人读出处,逐字段进 why。
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw or raw[0] in " \t#":
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):.*?\s#\s*(.+)$", raw.rstrip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


# ---------------------------------------------------------------- 定位(与 rs_artboard 同机制的只读镜像)

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def artboard_dir() -> Path | None:
    """artboard 技能目录定位(镜像 rs_artboard._load_artboard_config 的查找序,
    只读不改):CUTFLOW_ARTBOARD_DIR 环境变量 > 仓库根 config.json > skills/config.json。"""
    env = __import__("os").environ.get("CUTFLOW_ARTBOARD_DIR", "").strip()
    if env:
        return Path(env)
    repo_cfg = _read_json(Path(__file__).resolve().parents[3] / "config.json")
    local_cfg = _read_json(Path(__file__).resolve().parents[2] / "config.json")
    merged = dict(local_cfg)
    merged.update({k: v for k, v in repo_cfg.items() if v})
    d = str(merged.get("artboard_dir", "")).strip()
    return Path(d) if d else None


def load_registry() -> dict:
    return _read_json(REGISTRY_PATH)


# ---------------------------------------------------------------- 加载 API

def load_pack(slug: str) -> dict | None:
    """风格包 → dict;包目录或 params.yaml 缺失返回 None(不抛,调用方回退)。

    返回 {slug, dir, params, cards, sources, readme, frames_css}。
    """
    if not slug:
        return None
    pack_dir = PACKS_DIR / slug
    params_path = pack_dir / "params.yaml"
    if not params_path.is_file():
        return None
    cards_path = pack_dir / "cards.yaml"
    readme_path = pack_dir / "README.md"
    css_path = pack_dir / "frames.css"
    params_text = params_path.read_text(encoding="utf-8")
    params = _load_yaml(params_text)
    if not isinstance(params, dict):
        return None
    cards = _load_yaml(cards_path.read_text(encoding="utf-8")) if cards_path.is_file() else {}
    readme = readme_path.read_text(encoding="utf-8") if readme_path.is_file() else ""
    css = css_path.read_text(encoding="utf-8") if css_path.is_file() else ""
    return {"slug": slug, "dir": pack_dir, "params": params, "cards": cards or {},
            "sources": param_sources(params_text), "readme": readme, "frames_css": css}


def load_template(tid: str) -> dict | None:
    """提示词模板 → front matter dict;文件缺失/无 front matter 返回 None。

    文件名解析:id 与文件名一一对应;唯一例外 id=custom ↔ _custom.md(定制骨架)。
    """
    if not tid or not TEMPLATE_ID_RE.fullmatch(tid):
        return None
    candidates = [PROMPTS_DIR / f"{tid}.md"]
    if tid == "custom":
        candidates.append(PROMPTS_DIR / "_custom.md")
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return None
    return parse_template_text(path.read_text(encoding="utf-8"))


def parse_template_text(text: str) -> dict | None:
    """模板文本 → front matter dict(_custom.md 的 id=custom 与文件名解耦,id 一致性由 check 对拍)。"""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    fm = _load_yaml(parts[1])
    if not isinstance(fm, dict):
        return None
    fm["_body"] = parts[2].lstrip("\n")
    return fm


def list_templates(include_custom: bool = False) -> list[dict]:
    """全部场景模板(文件名序;默认不含 _ 定制骨架)。附带 _file 键供 check 对拍 id↔文件名。"""
    out = []
    for p in sorted(PROMPTS_DIR.glob("*.md")):
        if p.name.startswith("_") and not include_custom:
            continue
        try:
            tpl = parse_template_text(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tpl = None
        if tpl:
            tpl["_file"] = p.stem
            out.append(tpl)
    return out


def match_template(text: str) -> tuple[dict | None, list[str], str]:
    """模糊描述 → (命中模板, 命中词, 解释)。确定性计分:
    命中个数 → 命中词总字长 → 文件名序(同分取在前;与 rs_intent.match_entry 同构)。"""
    best, best_hits, best_key = None, [], (0, 0)
    for tpl in list_templates():
        hits = [t for t in (tpl.get("triggers") or [])
                if isinstance(t, str) and t and t in text]
        key = (len(hits), sum(len(h) for h in hits))
        if hits and key > best_key:      # 全同分不替换 → 文件名序在前者胜(确定性)
            best, best_hits, best_key = tpl, hits, key
    if best is None:
        return None, [], "无 triggers 命中(可用 template list 查看全部场景)"
    other = [f"{t['id']}({len([x for x in t.get('triggers') or [] if x in text])})"
             for t in list_templates() if t.get("id") != best["id"]
             and any(x in text for x in (t.get("triggers") or []))]
    why = (f"命中模板 {best['id']} 的 triggers:{'、'.join(best_hits)}"
           + (f";其余候选 {','.join(other)} 命中更少" if other else ""))
    return best, best_hits, why


# ---------------------------------------------------------------- check(门禁 #13)

def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _check_pack(slug: str, registry: dict, platforms: dict, ab_dir: Path | None,
                descriptors: dict | None) -> tuple[list[str], list[str], list[str]]:
    """单包校验 → (errors, warnings, skips)。"""
    errs: list[str] = []
    warns: list[str] = []
    skips: list[str] = []
    pack_dir = PACKS_DIR / slug
    if not SLUG_RE.fullmatch(slug):
        errs.append(f"{slug}: slug 不合命名法 <领域>-<调性>(小写连字符,≥2 段)")
    for fname in ("params.yaml", "cards.yaml", "README.md", "frames.css"):
        if not (pack_dir / fname).is_file():
            errs.append(f"{slug}: 缺 {fname}(风格包四件套,见 _template/)")
    loaded = load_pack(slug)
    if loaded is None:
        errs.append(f"{slug}: params.yaml 缺失或不可解析")
        return errs, warns, skips
    params, cards = loaded["params"], loaded["cards"]
    for field in REQUIRED_PARAM_FIELDS:
        if field not in params:
            errs.append(f"{slug}: params.yaml 缺必填字段 {field}")
    if params.get("slug") != slug:
        errs.append(f"{slug}: params.slug={params.get('slug')!r} 与目录名不一致")
    if params.get("videoType") not in registry.get("videoTypes", {}):
        errs.append(f"{slug}: videoType {params.get('videoType')!r} 未在 registry.videoTypes 登记")
    if params.get("pacing") not in registry.get("pacingTiers", {}):
        errs.append(f"{slug}: pacing {params.get('pacing')!r} 不在 registry.pacingTiers")
    if params.get("platform") not in platforms:
        errs.append(f"{slug}: platform {params.get('platform')!r} 不在 platforms.json")
    if params.get("ratio") not in RATIOS:
        errs.append(f"{slug}: ratio {params.get('ratio')!r} 不在 rs_common.RATIOS")
    for key in ("cardMs", "visualBeatSec"):
        v = params.get(key)
        if not (isinstance(v, list) and len(v) == 2 and all(_is_num(x) for x in v)
                and v[0] <= v[1]):
            errs.append(f"{slug}: {key} 须为 [下限, 上限] 数字对,得 {v!r}")
    bgm = params.get("bgm")
    if not (isinstance(bgm, dict) and isinstance(bgm.get("enabled"), bool)
            and _is_num(bgm.get("gainDb")) and bgm.get("ducking") in DUCKING_MODES):
        errs.append(f"{slug}: bgm 须为 {{enabled, gainDb, ducking}}(ducking ∈ {'/'.join(DUCKING_MODES)}),得 {bgm!r}")
    trans = params.get("transition")
    if not (isinstance(trans, dict) and isinstance(trans.get("default"), str)
            and isinstance(trans.get("allow"), list) and trans["allow"]
            and isinstance(trans.get("snapToBeat"), bool)):
        errs.append(f"{slug}: transition 须为 {{default, allow[], snapToBeat}},得 {trans!r}")
    sub = params.get("subtitle")
    if not isinstance(sub, dict):
        errs.append(f"{slug}: subtitle 缺失")
    else:
        if sub.get("style") not in STYLES:
            errs.append(f"{slug}: subtitle.style {sub.get('style')!r} 不在 rs_subtitle.STYLES")
        mc, cps = sub.get("maxChars"), sub.get("cpsMax")
        if not (_is_num(mc) and 4 <= int(mc) <= 40):
            errs.append(f"{slug}: subtitle.maxChars 越界:{mc}(允许 4–40)")
        if not (_is_num(cps) and 1.0 <= float(cps) <= 20.0):
            errs.append(f"{slug}: subtitle.cpsMax 越界:{cps}(允许 1–20)")
        if not isinstance(sub.get("lyricOnly"), bool):
            errs.append(f"{slug}: subtitle.lyricOnly 须为布尔")
    cd = params.get("cards")
    if not (isinstance(cd, dict) and cd.get("density") in ("无", "少", "多")
            and isinstance(cd.get("template"), str) and cd["template"]):
        errs.append(f"{slug}: cards 须为 {{density ∈ 无/少/多, template}},得 {cd!r}")
    caps = params.get("capabilities")
    if not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
        errs.append(f"{slug}: capabilities 须为字符串数组(可为空,M8 补)")
    elif descriptors is not None:
        unknown = [c for c in caps if c not in descriptors]
        if unknown:
            errs.append(f"{slug}: capabilities 声明了未登记描述符 {unknown}(诚实纪律:未落地不声明)")
    ab = params.get("artboard")
    ab_styles: list = []
    if not (isinstance(ab, dict) and isinstance(ab.get("styles"), list)
            and 1 <= len(ab["styles"]) <= 2 and isinstance(ab.get("templates"), list)):
        errs.append(f"{slug}: artboard 须为 {{styles[1..2], templates[]}},得 {ab!r}")
    else:
        ab_styles = ab["styles"]
        for t in ab["templates"]:
            if not (isinstance(t, str) and re.fullmatch(
                    r"(" + "|".join(CARD_KINDS) + r"):\d+", t)):
                errs.append(f"{slug}: artboard.templates 条目 {t!r} 不合「kind:数量」体例")
    # README:禁则 ≥5 条且每条带理由
    readme = loaded["readme"]
    ban_lines = _ban_lines(readme)
    if len(ban_lines) < 5:
        errs.append(f"{slug}: README 禁则仅 {len(ban_lines)} 条(<5),且每条须带理由")
    else:
        weak = [b for b in ban_lines if len(b) < 10]
        if weak:
            warns.append(f"{slug}: {len(weak)} 条禁则疑似缺理由(过短):{weak[:2]}")
    # cards.yaml 对拍
    if not isinstance(cards, dict) or not cards:
        errs.append(f"{slug}: cards.yaml 缺失或不可解析")
    else:
        if cards.get("slug") != slug:
            errs.append(f"{slug}: cards.yaml slug={cards.get('slug')!r} 与目录名不一致")
        c_ab = cards.get("artboard") or {}
        c_styles = c_ab.get("styles") or []
        if c_styles != ab_styles:
            errs.append(f"{slug}: cards.yaml artboard.styles={c_styles!r} 与 params.yaml {ab_styles!r} 不一致")
        tpl_map = cards.get("templates") or {}
        if not isinstance(tpl_map, dict) or not tpl_map:
            errs.append(f"{slug}: cards.yaml 缺 templates 映射(六类卡数量)")
        else:
            bad = [k for k in tpl_map if k not in CARD_KINDS]
            if bad:
                errs.append(f"{slug}: cards.yaml templates 含未知卡型 {bad}(可选 {'/'.join(CARD_KINDS)})")
        ab_cases = c_ab.get("cases") or []
        if ab_dir is None:
            skips.append(f"{slug}: artboard 技能目录未配置,styles/cases 存在性 SKIP")
        else:
            for s in ab_styles:
                if not (ab_dir / "references" / "styles" / f"{s}.md").is_file():
                    errs.append(f"{slug}: artboard 风格不存在:{ab_dir}/references/styles/{s}.md")
            for c in ab_cases:
                rel = str(c)
                rel = rel.replace("\\", "/").removeprefix("assets/cases/").removeprefix("assets/cases\\")
                if not (ab_dir / "assets" / "cases" / rel).is_file():
                    errs.append(f"{slug}: artboard 案例不存在:{ab_dir}/assets/cases/{rel}")
    return errs, warns, skips


def _ban_lines(readme: str) -> list[str]:
    """README 禁则区(## 禁则 起始的节)的条目行。"""
    lines = readme.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip().startswith("## 禁则")), None)
    if start is None:
        return []
    out = []
    for ln in lines[start + 1:]:
        if ln.strip().startswith("## "):
            break
        if ln.strip().startswith("- "):
            out.append(ln.strip()[2:])
    return out


def cmd_check() -> int:
    """全量校验:包四件套 + registry 双向对拍 + artboard 引用 + 提示词模板结构。"""
    registry = load_registry()
    platforms = load_platforms()
    ab_dir = artboard_dir()
    descriptors = None
    try:
        from rs_run import load_capability_descriptors  # noqa: PLC0415 — 懒加载
        descriptors = load_capability_descriptors()
    except Exception:  # noqa: BLE001 — 描述符加载不可用时降级为不校验(check 不因环境红)
        descriptors = None

    errors: list[str] = []
    warns: list[str] = []
    skips: list[str] = []
    pack_dirs = sorted(d.name for d in PACKS_DIR.iterdir()
                       if d.is_dir() and not d.name.startswith("_")) if PACKS_DIR.is_dir() else []
    if not pack_dirs:
        errors.append(f"packs/ 下没有任何风格包(先 rs_stylepack.py new <slug>)")

    entries = registry.get("entries", [])
    reg_packs = {e["pack"] for e in entries if e.get("pack")}
    for slug in pack_dirs:
        e, w, s = _check_pack(slug, registry, platforms, ab_dir, descriptors)
        errors.extend(e)
        warns.extend(w)
        skips.extend(s)
        if slug not in reg_packs:
            errors.append(f"{slug}: 参数有、registry 无 —— entries[] 没有任何条目的 pack 指向本包(双向对拍红)")
    for e in entries:
        p = e.get("pack")
        if not p:
            continue
        if p not in pack_dirs:
            errors.append(f"{e['id']}: registry 声明 pack={p!r} 但 packs/{p}/params.yaml 不存在(文档有参数无,双向对拍红)")

    # 提示词模板:_schema.json 结构对拍 + pack 引用 + 自锚
    schema = _read_json(SCHEMA_PATH)
    if not schema:
        errors.append("templates/prompts/_schema.json 缺失或不可解析")
    tpls = list_templates(include_custom=True)
    if len(tpls) < 7:
        errors.append(f"场景模板仅 {len(tpls)} 个(方案 §5.11 要求 7 个 + _custom)")
    for tpl in tpls:
        tid = tpl.get("id", "?")
        fname = str(tpl.get("_file") or "")
        # 唯一合法例外:_custom.md 的 id 是 custom(它是供复制的骨架,不是场景模板)。
        expect_id = "custom" if fname == "_custom" else fname
        if tid != expect_id:
            errors.append(f"template {tid}: id={tid!r} 与文件名 {fname!r} 不一致(id 必须等于文件名去 .md)")
        for field in REQUIRED_TEMPLATE_FIELDS:
            if field not in tpl:
                errors.append(f"template {tid}: 缺必填字段 {field}(对照 _schema.json)")
        if not TEMPLATE_ID_RE.fullmatch(str(tpl.get("id", ""))):
            errors.append(f"template {tid}: id 不合小写连字符体例")
        if not isinstance(tpl.get("triggers"), list) or len(tpl["triggers"]) < 2:
            errors.append(f"template {tid}: triggers 须 ≥2 个")
        if not isinstance(tpl.get("brief_skeleton"), dict) or "videoType" not in (tpl.get("brief_skeleton") or {}):
            errors.append(f"template {tid}: brief_skeleton 须为对象且必填 videoType")
        if not isinstance(tpl.get("plan_skeleton"), dict):
            errors.append(f"template {tid}: plan_skeleton 须为对象")
        pk = str(tpl.get("pack") or "")
        if pk and pk not in pack_dirs:
            errors.append(f"template {tid}: pack {pk!r} 指向不存在的风格包")
        if tpl.get("videoType") not in registry.get("videoTypes", {}):
            errors.append(f"template {tid}: videoType {tpl.get('videoType')!r} 未登记")
        exs = tpl.get("example_prompts") or []
        if not exs:
            errors.append(f"template {tid}: example_prompts 至少 1 条(回归锚)")
        elif fname != "_custom":     # _custom 是待复制骨架,example 是占位符,不做自锚
            hit, _, _why = match_template(str(exs[0]))
            if hit is None or hit.get("id") != tid:
                errors.append(f"template {tid}: example_prompts[0] 未自锚(命中 {hit.get('id') if hit else '无'})")

    ok = not errors
    return emit(ok, "STYLEPACK_CHECK" if ok else "STYLEPACK_CHECK_FAILED",
                (f"风格包对拍通过:{len(pack_dirs)} 包 / {len(tpls)} 模板"
                 + (f";SKIP {len(skips)} 项(artboard 缺席)" if skips else ""))
                if ok else f"风格包对拍失败:{len(errors)} 处红",
                {"packs": pack_dirs, "errors": errors, "warnings": warns,
                 "skipped": skips, "artboardDir": str(ab_dir) if ab_dir else "",
                 "nTemplates": len(tpls)}, exit_code=0 if ok else 2)


# ---------------------------------------------------------------- show / new

def cmd_show(slug: str) -> int:
    pack = load_pack(slug)
    if pack is None:
        return emit(False, "NO_PACK", f"风格包不存在:{PACKS_DIR / slug}(load_pack 返回 None,消费者按 registry 原路径回退)",
                    exit_code=2)
    registry = load_registry()
    entry = next((e for e in registry.get("entries", []) if e.get("pack") == slug), None)
    data = {"slug": slug, "dir": str(pack["dir"]),
            "params": pack["params"], "sources": pack["sources"],
            "cards": pack["cards"], "framesCssBytes": len(pack["frames_css"].encode("utf-8")),
            "registryEntry": (entry or {}).get("id", ""),
            "readmeHead": "\n".join(pack["readme"].splitlines()[:12])}
    p = pack["params"]
    print(f"{p.get('label', slug)}({slug})\n"
          f"  videoType={p.get('videoType')}  platform={p.get('platform')}/{p.get('ratio')}"
          f"  pacing={p.get('pacing')}  cardMs={p.get('cardMs')}\n"
          f"  bgm={p.get('bgm')}\n  subtitle={p.get('subtitle')}\n"
          f"  cards={p.get('cards')}  artboard={p.get('artboard')}")
    return emit(True, "PACK_SHOWN", f"风格包 {slug} 摘要(出处标注见 data.sources)", data)


def cmd_new(slug: str, label: str = "", video_type: str = "", artboard_slug: str = "") -> int:
    if not SLUG_RE.fullmatch(slug):
        return emit(False, "BAD_SLUG", f"slug 不合命名法 <领域>-<调性>(小写连字符,≥2 段):{slug!r}",
                    exit_code=2)
    target = PACKS_DIR / slug
    if target.exists():
        return emit(False, "PACK_EXISTS", f"风格包已存在:{target}", exit_code=2)
    if not TEMPLATE_DIR.is_dir():
        return emit(False, "NO_TEMPLATE", f"脚手架缺失:{TEMPLATE_DIR}", exit_code=2)
    shutil.copytree(TEMPLATE_DIR, target)
    label = label or slug
    for fname, pairs in (
        ("params.yaml", {"slug: TEMPLATE": f"slug: {slug}",
                         "label: 模板·未命名风格": f"label: {label}",
                         "videoType: TEMPLATE": f"videoType: {video_type or 'TEMPLATE'}",
                         "ARTBOARD-SLUG": artboard_slug or "ARTBOARD-SLUG"}),
        ("cards.yaml", {"slug: TEMPLATE": f"slug: {slug}",
                        "ARTBOARD-SLUG": artboard_slug or "ARTBOARD-SLUG"}),
        ("frames.css", {"TEMPLATE": slug}),
        ("README.md", {"<slug>": slug, "<label>": label}),
    ):
        fp = target / fname
        text = fp.read_text(encoding="utf-8")
        for old, new in pairs.items():
            text = text.replace(old, new)
        fp.write_text(text, encoding="utf-8")
    return emit(True, "PACK_SCAFFOLDED",
                f"新风格包 {slug} 已从 _template 脚手架生成 → {target}",
                {"dir": str(target),
                 "next": ["填 params.yaml(每个值标 [内部] 出处或 [经验])",
                          "填 cards.yaml(借 1–2 个 artboard 风格 slug)",
                          "写 README.md 禁则 ≥5 条带理由",
                          "registry.json 加一条 entries(含 match.keywords 与 pack)",
                          "rs_stylepack.py check + rs_intent compile --dry-run 冒烟"]})


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="风格包加载/校验/脚手架(ADR-0051)")
    ap.add_argument("command", choices=["check", "show", "new"], help="check=全量对拍;show=打印包摘要;new=脚手架新包")
    ap.add_argument("slug", nargs="?", default="", help="show/new 的风格包 slug")
    ap.add_argument("--label", default="", help="new:中文名(缺省用 slug)")
    ap.add_argument("--video-type", dest="video_type", default="", help="new:registry.videoTypes 键")
    ap.add_argument("--artboard-slug", dest="artboard_slug", default="", help="new:借用的 artboard 风格 slug")
    a = ap.parse_args()
    if a.command == "check":
        return cmd_check()
    if not a.slug:
        return emit(False, "BAD_ARGS", f"{a.command} 需要 <slug> 参数", exit_code=2)
    if a.command == "show":
        return cmd_show(a.slug)
    return cmd_new(a.slug, label=a.label, video_type=a.video_type, artboard_slug=a.artboard_slug)


if __name__ == "__main__":
    from rs_common import ensure_utf8
    ensure_utf8()
    sys.exit(main())
