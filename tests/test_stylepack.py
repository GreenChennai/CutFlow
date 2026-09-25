"""M6 · 风格包统一命名空间(ADR-0051)+ 内置提示词模板库(§5.11)门禁。

覆盖面(方案 §7.1 门禁 #13 / §7.3 M6 验收线):
· 每个包四件套(params/cards/README/frames.css)存在且字段齐(对照 _template 体例);
· registry 双向对拍:entries[].pack 指向的包真实存在;每个包都被 entries[].pack 引用;
· slug 命名法(<领域>-<调性> 小写连字符)与 params/cards slug 同名;
· artboard.styles/cases 引用真实存在(rs_artboard 的 artboard 目录定位机制;缺席 SKIP);
· rs_intent 在 pack 全备 / 缺失两种输入下产出可复现 resolved(字节级);缺失回退 + WARN;
· template match 对方案 §5.11 的两个 example_prompts 命中 mixcut-beat 且可复现;
· rs_artboard gen-frames --pack mixcut-douyin 集成冒烟:pack frames.css 被产物消费
  (M8 探测位已修正为 skills/cutflow/templates/styles/packs/,与真身同源,
  M7 的镜像桥已简化为直通;断言免 ffmpeg,导出桩必败即可)。
另:rs_stylepack check CLI 全绿 / new 脚手架 / _mini_yaml 与 PyYAML 等价。

运行:pytest tests/test_stylepack.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
STYLES = REPO / "skills" / "cutflow" / "templates" / "styles"
PACKS = STYLES / "packs"
PROMPTS = REPO / "skills" / "cutflow" / "templates" / "prompts"
sys.path.insert(0, str(SCRIPTS))

import rs_stylepack as sp  # noqa: E402
import rs_intent  # noqa: E402


def _run(script: str, *args: str, cwd: Path | None = None,
         env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=cwd or REPO, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=600)


def _last_json(p: subprocess.CompletedProcess) -> dict:
    lines = [ln for ln in (p.stdout or "").splitlines() if ln.strip().startswith("{")]
    assert lines, f"无 JSON 输出:\n{p.stdout}\n{p.stderr}"
    return json.loads(lines[-1])


def _pack_dirs() -> list[str]:
    return sorted(d.name for d in PACKS.iterdir()
                  if d.is_dir() and not d.name.startswith("_"))


# ================================================================ ① 四件套与字段齐备

def test_packs_complete_and_fields_typed():
    """每个包:params/cards/README/frames.css 存在;params 必填字段齐且类型合规。"""
    assert sp.REQUIRED_PARAM_FIELDS, "体例常量不得为空"
    for slug in _pack_dirs():
        pack = sp.load_pack(slug)
        assert pack is not None, slug
        params = pack["params"]
        for field in sp.REQUIRED_PARAM_FIELDS:
            assert field in params, f"{slug}: params 缺 {field}"
        assert params["slug"] == slug, f"{slug}: slug 与目录不同名"
        assert params["videoType"] and params["platform"] and params["ratio"]
        assert len(params["cardMs"]) == 2 and params["cardMs"][0] <= params["cardMs"][1]
        assert len(params["visualBeatSec"]) == 2
        assert set(params["bgm"]) >= {"enabled", "gainDb", "ducking"}
        assert set(params["transition"]) >= {"default", "allow", "snapToBeat"}
        assert set(params["subtitle"]) >= {"style", "maxChars", "cpsMax", "lyricOnly"}
        assert params["cards"]["density"] in ("无", "少", "多")
        assert isinstance(params["capabilities"], list), "capabilities 可为空数组但必须是数组"
        assert 1 <= len(params["artboard"]["styles"]) <= 2, "一次借 1–2 个 artboard 原子"
        cards = pack["cards"]
        assert cards.get("slug") == slug and cards.get("templates"), f"{slug}: cards.yaml 缺映射"
        assert set(cards["templates"]) <= set(sp.CARD_KINDS), f"{slug}: cards 卡型越界"
        assert cards["artboard"]["styles"] == params["artboard"]["styles"], \
            f"{slug}: params 与 cards 的 artboard.styles 不一致"
        bans = sp._ban_lines(pack["readme"])
        assert len(bans) >= 5, f"{slug}: README 禁则仅 {len(bans)} 条(<5)"


def test_packs_are_param_source_of_registry_values():
    """参数真身纪律:pack 的 pacing/subtitle/cards 与 registry 同名条目一致(不许两处各说各话)。"""
    registry = sp.load_registry()
    by_id = {e["id"]: e for e in registry["entries"]}
    for slug in _pack_dirs():
        pack = sp.load_pack(slug)
        entry = by_id.get(slug)
        if entry is None:
            continue
        p = pack["params"]
        assert p["pacing"] == entry["pacing"], slug
        assert p["subtitle"]["style"] == entry["style"], slug
        assert p["cards"]["density"] == entry["cards"]["density"], slug
        if entry.get("bgmLibrary"):
            assert p["bgm"]["enabled"] == entry["bgmLibrary"]["enabled"], slug
            assert p["bgm"]["gainDb"] == entry["bgmLibrary"]["gainDb"], slug


# ================================================================ ② registry 双向对拍

def test_registry_pack_bidirectional():
    """entries[].pack → 包存在;包 → 有 entries[].pack 引用(双向都红才算对拍)。"""
    registry = sp.load_registry()
    entries = registry["entries"]
    on_disk = set(_pack_dirs())
    declared = set()
    for e in entries:
        pack = e.get("pack")
        if not pack:
            continue
        declared.add(pack)
        assert (PACKS / pack / "params.yaml").is_file(), \
            f"{e['id']}: registry 声明 pack={pack} 但包不存在(文档有参数无)"
    orphans = on_disk - declared
    assert not orphans, f"参数有、registry 无:未被 entries[].pack 引用的包 {sorted(orphans)}"
    # videoTypes 引用方向:每个包的 videoType 必须已登记(缺省条目由 v21 门禁兜底)
    for slug in _pack_dirs():
        vt = sp.load_pack(slug)["params"]["videoType"]
        assert vt in registry["videoTypes"], f"{slug}: videoType {vt} 未登记"


def test_stylepack_check_cli_green():
    """rs_stylepack.py check 全量对拍(门禁 #13 的 CLI 形态)必须全绿。"""
    r = _run("rs_stylepack.py", "check")
    out = _last_json(r)
    assert r.returncode == 0 and out["ok"], out.get("errors")
    assert len(out["data"]["packs"]) == 6, "方案 §5.8 规定六个风格包"
    assert out["data"]["nTemplates"] >= 8, "7 场景模板 + _custom 定制骨架"


# ================================================================ ③④ slug / artboard 引用

def test_slug_naming_and_no_template_dir_in_packs():
    """slug 命名法:<领域>-<调性> 小写连字符 ≥2 段;下划线目录(_ 开头)不算包。"""
    assert sp.SLUG_RE.fullmatch("mixcut-douyin")
    assert sp.SLUG_RE.fullmatch("knowledge-talkshow-douyin")
    assert not sp.SLUG_RE.fullmatch("Mixcut-Douyin")
    assert not sp.SLUG_RE.fullmatch("mixcut")            # 单段不合 <领域>-<调性>
    assert not sp.SLUG_RE.fullmatch("mixcut_douyin")
    assert not sp.SLUG_RE.fullmatch("1x-")               # 空段
    for slug in _pack_dirs():
        assert sp.SLUG_RE.fullmatch(slug), slug
    assert "_template" not in _pack_dirs(), "脚手架目录不得被当作包对拍"


def test_artboard_refs_real_via_artboard_locator():
    """artboard.styles/cases 引用真实存在(用 rs_artboard 的目录定位机制;缺席 SKIP)。"""
    import rs_artboard as ab  # noqa: PLC0415 — 只读其定位机制
    cfg = ab._load_artboard_config()
    ab_dir = cfg.get("artboard_dir", "")
    if not ab_dir or not Path(ab_dir).is_dir():
        pytest.skip(f"artboard 技能目录未配置或不存在:{ab_dir!r}")
    ab_dir = Path(ab_dir)
    for slug in _pack_dirs():
        pack = sp.load_pack(slug)
        c_ab = (pack["cards"].get("artboard") or {})
        for s in c_ab.get("styles") or []:
            assert (ab_dir / "references" / "styles" / f"{s}.md").is_file(), \
                f"{slug}: artboard 风格不存在:{s}"
        for c in c_ab.get("cases") or []:
            name = str(c).replace("\\", "/").split("/")[-1]
            assert (ab_dir / "assets" / "cases" / name).is_file(), \
                f"{slug}: artboard 案例不存在:{name}"
    # 两套定位机制(镜像实现 vs rs_artboard 本尊)必须同源
    mine = sp.artboard_dir()
    assert mine is not None and mine.resolve() == Path(ab_dir).resolve()


# ================================================================ ⑤ rs_intent 可复现(字节级)

_BRIEF = {"videoType": "混剪", "styleRef": "混剪 卡点 高燃", "title": "t",
          "terms": [], "subtitle": {"on": True}}
_PLAN = {"cut": {"enabled": False}, "subtitle": {"style": "", "maxChars": 0, "cpsMax": 0},
         "cards": "none", "sfx": "minimal", "pacing": ""}


def _compile_dry(brief: dict, plan: dict, tmp: Path, tag: str) -> dict:
    d = tmp / tag
    d.mkdir(parents=True, exist_ok=True)
    (d / "brief.json").write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
    (d / "plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    r = _run("rs_intent.py", "compile", "--brief", str(d / "brief.json"),
             "--plan", str(d / "plan.json"), "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    return _last_json(r)


def test_intent_reproducible_with_pack_present(tmp_path):
    """pack 全备:同输入两次 → stdout JSON 字节级一致;pack 字段进 resolved 且留痕。"""
    out1 = _compile_dry(_BRIEF, _PLAN, tmp_path, "r1")
    out2 = _compile_dry(_BRIEF, _PLAN, tmp_path, "r2")
    a = json.dumps(out1, ensure_ascii=False, sort_keys=True).encode("utf-8")
    b = json.dumps(out2, ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert a == b, "pack 全备时 compile 输出必须字节级可复现"
    resolved = out1["data"]["resolved"]
    assert resolved["stylePack"] == "mixcut-douyin"
    assert resolved["cardMs"] == [800, 2400] and resolved["transition"]["snapToBeat"] is True
    assert resolved["artboard"]["styles"] == ["ecommerce-promo"]
    why = {d["field"]: d["why"] for d in out1["data"]["decisions"]}
    assert "params.yaml" in why.get("transition", ""), "pack 参数必须逐字段留痕出处"
    assert out1["data"]["warnings"] == []


def test_intent_fallback_reproducible_without_pack(tmp_path):
    """pack 缺失(目录临时移走)→ WARN stylePackMissing + 按 registry 原路径回退,
    两次运行字节级一致;恢复后 resolved 重新带 pack。"""
    pack_dir = PACKS / "mixcut-douyin"
    away = tmp_path / "_pack_away"
    shutil.move(str(pack_dir), str(away))
    try:
        out1 = _compile_dry(_BRIEF, _PLAN, tmp_path, "f1")
        out2 = _compile_dry(_BRIEF, _PLAN, tmp_path, "f2")
        a = json.dumps(out1, ensure_ascii=False, sort_keys=True).encode("utf-8")
        b = json.dumps(out2, ensure_ascii=False, sort_keys=True).encode("utf-8")
        assert a == b, "pack 缺失时 compile 输出同样必须字节级可复现"
        resolved = out1["data"]["resolved"]
        assert "stylePack" not in resolved, "回退路径不得残留 pack 键"
        assert resolved["cardMs"] == [800, 2400], "回退到 registry.pacingTiers.music"
        assert any("stylePackMissing" in w for w in out1["data"]["warnings"])
        # 决策来源口径:回退路径的 source 仍落在 user/registry/default 三种内(v21 同规)
        assert all(d["source"] in ("user", "registry", "default")
                   for d in out1["data"]["decisions"])
    finally:
        shutil.move(str(away), str(pack_dir))
    out3 = _compile_dry(_BRIEF, _PLAN, tmp_path, "f3")
    assert out3["data"]["resolved"]["stylePack"] == "mixcut-douyin"


def test_intent_no_pack_entry_stays_legacy_shape(tmp_path):
    """无 pack 的条目(knowledge-cards-douyin):resolved 不新增 pack 键、无告警 ——
    与 ADR-0051 落地前的行为字节兼容(回退纪律)。"""
    brief = dict(_BRIEF, videoType="talking-head+animation", styleRef="信息卡")
    out = _compile_dry(brief, _PLAN, tmp_path, "np")
    assert "stylePack" not in out["data"]["resolved"]
    assert out["data"]["warnings"] == []


# ================================================================ ⑥ template match

def test_template_match_plan_examples_and_reproducible():
    """方案 §5.11 的两条 mixcut example_prompts 必须命中 mixcut-beat;同输入同答。"""
    examples = ("把这几段游戏录屏做成一条高燃卡点", "音乐驱动的混剪,节奏要快")
    for text in examples:
        tpl1, hits1, why1 = sp.match_template(text)
        tpl2, hits2, why2 = sp.match_template(text)
        assert tpl1 is not None and tpl1["id"] == "mixcut-beat", (text, why1)
        assert (tpl1["id"], hits1, why1) == (tpl2["id"], hits2, why2), "match 必须可复现"
        assert tpl1["pack"] == "mixcut-douyin"
    # CLI 形态同一结论(rs_intent template match)
    r = _run("rs_intent.py", "template", "match", examples[0])
    out = _last_json(r)
    assert r.returncode == 0 and out["data"]["matched"] == "mixcut-beat"


def test_template_library_structure():
    """7 个场景模板 + _custom;必填键齐(对照 _schema.json);pack 引用真实;trigger 可命中。"""
    schema = json.loads((PROMPTS / "_schema.json").read_text(encoding="utf-8"))
    assert schema["version"] == 1
    tpls = sp.list_templates()
    assert len(tpls) == 7, f"方案 §5.11 规定 7 个场景模板,得 {len(tpls)}"
    assert sp.load_template("custom") is not None, "_custom.md 的 front matter id=custom"
    for t in tpls:
        for field in schema["required"]:
            assert field in t, f"{t['id']}: 缺 {field}"
        assert t["id"] == t["_file"], f"{t['id']}: id 与文件名不一致"
        assert sp.SLUG_RE.fullmatch(t["pack"] or "a-b") or not t["pack"], t["id"]
        if t["pack"]:
            assert (PACKS / t["pack"] / "params.yaml").is_file(), f"{t['id']}: pack 不存在"
        assert t["videoType"] in sp.load_registry()["videoTypes"], t["id"]
        assert t["example_prompts"], f"{t['id']}: 至少 1 条 example_prompts"
    packs_of = {t["pack"] for t in tpls if t["pack"]}
    assert {"knowledge-talkshow-douyin", "tutorial-bilibili", "mixcut-douyin",
            "vlog-douyin", "drama-vertical", "screen-tutorial"} >= packs_of


def test_template_match_full_coverage_and_deterministic():
    """每个场景模板的首条 example_prompts 自锚(命中自己);全部命中在两次运行间一致。"""
    tpls = sp.list_templates()
    results = []
    for t in tpls:
        hit, hits, why = sp.match_template(t["example_prompts"][0])
        assert hit is not None and hit["id"] == t["id"], (t["id"], why)
        results.append((t["id"], hit["id"], tuple(hits), why))
    again = []
    for t in tpls:
        hit, hits, why = sp.match_template(t["example_prompts"][0])
        again.append((t["id"], hit["id"], tuple(hits), why))
    assert results == again


def test_compile_template_prefills_and_template_source(tmp_path):
    """compile --template:骨架预填 + source=template 的 templateQuote;auto-fill 补 title。"""
    out_dir = tmp_path / "proj"
    r = _run("rs_intent.py", "compile", "--template", "drama-vertical",
             "--out", str(out_dir), "--auto-fill")
    assert r.returncode == 0, r.stdout + r.stderr
    doc = json.loads((out_dir / "00_制作简报" / "intent_decisions.json").read_text(encoding="utf-8"))
    assert doc["template"] == {"id": "drama-vertical", "autoFill": True}
    resolved = doc["resolved"]
    assert resolved["videoType"] == "drama" and resolved["stylePack"] == "drama-vertical"
    assert resolved["maxChars"] == 14, "pack 参数真身(14 字台词)应胜出"
    tsrc = [d for d in doc["decisions"] if d["source"] == "template"]
    assert tsrc and all("prompts/drama-vertical.md#" in d["promptQuote"] for d in tsrc)
    assert any(d["field"] == "videoType" for d in tsrc)
    # 用户文件覆盖骨架后,被覆盖字段回到 user 来源(模板只在「未被覆盖」时背锅)
    user_brief = {"title": "我的短剧"}
    (tmp_path / "b.json").write_text(json.dumps(user_brief, ensure_ascii=False), encoding="utf-8")
    r2 = _run("rs_intent.py", "compile", "--template", "drama-vertical",
              "--brief", str(tmp_path / "b.json"), "--out", str(tmp_path / "p2"))
    assert r2.returncode == 0, r2.stdout + r2.stderr
    doc2 = json.loads((tmp_path / "p2" / "00_制作简报" / "intent_decisions.json").read_text(encoding="utf-8"))
    assert doc2["resolved"]["title"] == "我的短剧"
    by_field = {d["field"]: d for d in doc2["decisions"]}
    assert by_field["videoType"]["source"] == "template", "未被覆盖的骨架字段仍归 template"
    assert by_field["styleEntry"]["source"] == "registry"


# ================================================================ ⑦ gen-frames --pack 集成冒烟

@pytest.fixture
def pack_probe_bridge():
    """M8 简化:rs_artboard 探测位已修正为 skills/cutflow/templates/styles/packs/
    (与真身同目录,M7 的 repo 根镜像桥不再需要),本夹具退化为直通。"""
    yield PACKS / "mixcut-douyin"


def _stub_artboard(tmp_path: Path, export_ok: bool) -> Path:
    """假 artboard 技能(同 test_artboard_frames 手法):机检恒过,导出可置败(免 ffmpeg)。"""
    art = tmp_path / "artboard-skill"
    (art / "scripts").mkdir(parents=True)
    (art / "scripts" / "check_overflow.py").write_text(
        "import json\nprint(json.dumps({'ok': True}))\n", encoding="utf-8")
    (art / "scripts" / "export.py").write_text(
        "import argparse, json, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--source', required=True)\n"
        "p.add_argument('--output', required=True)\n"
        "p.add_argument('--max-wait', type=float, default=15.0)\n"
        "a = p.parse_args()\n"
        "if not " + str(export_ok) + ":\n"
        "    print(json.dumps({'ok': False, 'error': 'STUB_FAIL'})); sys.exit(1)\n"
        "print(json.dumps({'ok': True, 'engine': 'stub'}))\n", encoding="utf-8")
    (art / "scripts" / "scaffold.py").write_text(
        "import argparse, json, os, pathlib\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('slug'); p.add_argument('--size', default='vertical')\n"
        "p.add_argument('--fonts', default=''); p.add_argument('--force', action='store_true')\n"
        "a = p.parse_args()\n"
        "studio = pathlib.Path(os.environ['ARTBOARD_STUDIO'])\n"
        "proj = studio / a.slug\n"
        "(proj / 'src').mkdir(parents=True, exist_ok=True)\n"
        "(proj / 'export').mkdir(parents=True, exist_ok=True)\n"
        "(proj / 'src' / 'index.html').write_text('<!-- scaffold 模板 -->', encoding='utf-8')\n"
        "(proj / 'project.json').write_text(json.dumps({'slug': a.slug, 'width': 1080,\n"
        "    'height': 1920}), encoding='utf-8')\n"
        "print(json.dumps({'ok': True, 'project': str(proj)}))\n", encoding="utf-8")
    return art


def test_gen_frames_consumes_real_pack(tmp_path, pack_probe_bridge):
    """gen-frames --pack mixcut-douyin:真包 frames.css 被注入产物(无 stylePackMissing)。

    导出桩置败(退出码 4 EXPORT_FAILED)以免依赖 ffmpeg —— pack 消费发生在
    HTML 生成步,断言产物 HTML 含真包标记即完成「pack 参数被消费」。
    """
    art = _stub_artboard(tmp_path, export_ok=False)
    env_extra = {"CUTFLOW_ARTBOARD_DIR": str(art), "ARTBOARD_STUDIO": str(tmp_path / "studio")}
    root = tmp_path / "proj"
    (root / "00_制作简报").mkdir(parents=True)
    (root / "00_制作简报" / "cards.json").write_text(json.dumps([
        {"id": "op01", "kind": "opener", "title": "混剪钩子", "kicker": "M6",
         "accent": "#FF6B4A"}], ensure_ascii=False), encoding="utf-8")
    r = _run("rs_artboard.py", "gen-frames", str(root), "--kind", "opener",
             "--pack", "mixcut-douyin", cwd=root, env_extra=env_extra)
    out = _last_json(r)
    assert not any("stylePackMissing" in w for w in out["data"]["warnings"]), \
        f"真包在场却报缺失:{out['data']['warnings']}"
    html = (root / "03_创作素材" / "artboard" / "op01" / "src" / "index.html").read_text(encoding="utf-8")
    assert "/* pack:mixcut-douyin */" in html, "真包 frames.css 未被注入产物"
    # 对照组:全新工程 + 不存在的包 → WARN 且无注入(回退内置规格,行为可复现)
    root2 = tmp_path / "proj-nofallback"
    (root2 / "00_制作简报").mkdir(parents=True)
    (root2 / "00_制作简报" / "cards.json").write_text(json.dumps([
        {"id": "op02", "kind": "opener", "title": "对照卡", "kicker": "M6",
         "accent": "#4F8CFF"}], ensure_ascii=False), encoding="utf-8")
    r2 = _run("rs_artboard.py", "gen-frames", str(root2), "--kind", "opener",
              "--pack", "no-such-pack", cwd=root2, env_extra=env_extra)
    out2 = _last_json(r2)
    assert any("stylePackMissing" in w for w in out2["data"]["warnings"])
    html2 = (root2 / "03_创作素材" / "artboard" / "op02" / "src" / "index.html").read_text(encoding="utf-8")
    assert "pack:mixcut-douyin" not in html2


# ================================================================ 加载 API 与解析器

def test_load_pack_returns_none_silently():
    """缺包返回 None 不抛(消费者按 registry 原路径回退)。"""
    assert sp.load_pack("no-such-pack") is None
    assert sp.load_pack("") is None


def test_mini_yaml_parity_with_pyyaml():
    """子集解析器兜底必须与 PyYAML 在全部包/模板文件上等价(ADR-0049 懒加载纪律)。"""
    try:
        import yaml
    except ImportError:  # noqa: BLE001 — 无 PyYAML 的环境无从对拍
        pytest.skip("无 pyyaml")
    files = sorted(PACKS.rglob("*.yaml")) + sorted((STYLES / "_template").rglob("*.yaml"))
    assert len(files) >= 13, f"包文件数量异常:{len(files)}"
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert sp._mini_yaml(text) == yaml.safe_load(text), f.name
    for f in sorted(PROMPTS.glob("*.md")):
        fm = f.read_text(encoding="utf-8").split("---", 2)[1]
        assert sp._mini_yaml(fm) == yaml.safe_load(fm), f.name


def test_param_sources_extracts_provenance():
    """行内 [内部]/[经验] 出处注释可被提取(供 rs_intent decision_log 留痕)。"""
    text = (PACKS / "mixcut-douyin" / "params.yaml").read_text(encoding="utf-8")
    src = sp.param_sources(text)
    assert "[内部]" in src.get("cardMs", "")
    assert "[内部]" in src.get("bgm", "") or "[经验]" in src.get("bgm", "")
    assert "[经验]" in src.get("transition", "")


def test_cmd_new_scaffolds_and_replaces_placeholders(tmp_path, monkeypatch):
    """new <slug>:从 _template 复制、占位符替换;非法 slug 拒绝;已存在拒绝。"""
    monkeypatch.setattr(sp, "PACKS_DIR", tmp_path / "packs")
    (tmp_path / "packs").mkdir()
    r = _run("rs_stylepack.py", "new", "Bad_Slug")
    assert r.returncode == 2 and "BAD_SLUG" in r.stdout
    # 进程内调用(monkeypatch 只对本进程生效,子进程跑不了,故直接调函数)
    rc = sp.cmd_new("demo-style", label="演示风格", video_type="vlog", artboard_slug="tech-kv")
    assert rc == 0
    text = (tmp_path / "packs" / "demo-style" / "params.yaml").read_text(encoding="utf-8")
    assert "slug: demo-style" in text and "label: 演示风格" in text
    assert "videoType: vlog" in text and "tech-kv" in text
    cards = (tmp_path / "packs" / "demo-style" / "cards.yaml").read_text(encoding="utf-8")
    assert "slug: demo-style" in cards and "TEMPLATE" not in cards
    css = (tmp_path / "packs" / "demo-style" / "frames.css").read_text(encoding="utf-8")
    assert "demo-style" in css and "TEMPLATE" not in css
    # 已存在拒绝
    rc2 = sp.cmd_new("demo-style")
    assert rc2 == 2
