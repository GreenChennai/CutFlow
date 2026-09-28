# -*- coding: utf-8 -*-
"""T2.8 / M1–M14 回归:中危缺陷逐条断言(修复 + 防复发)。

每条一个用例;M4(死函数)与 M11(framebuffer/子进程回收)是行为级,分别在
tests/test_pixabay_selector.py 与 tests/test_t2_uv_sampling.py 覆盖,本文件做
源码级防复发。全离线,产物写 tempfile。

运行:pytest tests/test_med_fixes.py -q
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_asset  # noqa: E402
import rs_common  # noqa: E402
import rs_edit  # noqa: E402
import rs_intent  # noqa: E402
import rs_ir  # noqa: E402
import rs_render  # noqa: E402
import rs_subtitle  # noqa: E402
import rs_verify  # noqa: E402
from rs_fx import registry as fxreg  # noqa: E402


def _src(module) -> str:
    return inspect.getsource(module)


# ---------------------------------------------------------------- M1 rs_subtitle 重复加载

def test_m1_huazi_template_loaded_once():
    """M1:main 里 load_huazi_template(a.huazi) 只出现一次(其余复用 huazi_tpl)。"""
    src = _src(rs_subtitle)
    assert src.count("load_huazi_template(a.huazi)") == 1, \
        "main 仍二次调用 load_huazi_template(M1 复发)"
    assert "huazi_tpl=(huazi_tpl if a.huazi" in src, "write_ass 未复用已加载的 huazi_tpl"


# ---------------------------------------------------------------- M2 花字描边两套口径

def test_m2_huazi_body_outline_from_template():
    """M2:box/brush 内联描边取模板 outline_w;缺省才用历史默认(10/16)。"""
    ev = {"text": "重点"}
    assert r"\bord10" in rs_subtitle.huazi_body(ev, "box", {})          # 无模板 → 默认
    assert r"\bord16" in rs_subtitle.huazi_body(ev, "brush", {})
    assert r"\bord7" in rs_subtitle.huazi_body(ev, "box", {}, outline_w=7)
    assert r"\bord7" in rs_subtitle.huazi_body(ev, "brush", {}, outline_w=7)
    # apply_huazi 必须把模板 style.outline_w 传进去(真实模板解析链)
    tpl = {"effect": "box", "params": {}, "style": {"outline_w": 9}}
    events = [{"text": "关键词命中"}]
    rs_subtitle.apply_huazi(events, tpl, ["关键词"])
    assert r"\bord9" in events[0]["_huazi"], events[0]["_huazi"]


def test_m2_parse_real_template_keeps_outline():
    """真实模板解析出的 style.outline_w 进 huazi_body(HuaziBack 口径单源)。"""
    ids = [a["id"] for a in rs_asset.assets("huazi")
           if str(a.get("file", "")).endswith(".ass")]
    assert ids, "花字模板索引缺失"
    for hid in ids[:3]:
        tpl = rs_subtitle.load_huazi_template(hid)
        assert isinstance(tpl.get("style", {}).get("outline_w", 0), int), hid


# ---------------------------------------------------------------- M3 registry if False 死分支

def test_m3_no_dead_branch_in_registry():
    """M3:_g_stretch 的调试残留死分支已删(剥离注释后源码无该形态)。"""
    import re
    src = re.sub(r"#.*", "", _src(fxreg))          # 只看代码,不看注释
    assert "if False" not in src, "registry 仍残留调试死分支(M3 复发)"


# ---------------------------------------------------------------- M4 pixabay 死函数(行为级另测)

def test_m4_dead_music_functions_gone():
    """M4:_music_search_urls / _music_meta 已删除(离线样本行为见 test_pixabay_selector)。"""
    import rs_pixabay
    assert not hasattr(rs_pixabay, "_music_search_urls")
    assert not hasattr(rs_pixabay, "_music_meta")


# ---------------------------------------------------------------- M5 rs_sfx from_ending legacy

def test_m5_from_ending_uses_legacy_fallback(monkeypatch):
    """M5:legacy 回落名真实投入使用(素材缺失 → 旧名 bell;有真实组 → 组名)。"""
    ir = {"tracks": [{"kind": "video", "clips": [{"startMs": 0, "durationMs": 4000}]}]}
    monkeypatch.setattr(rs_sfx_mod(), "_pick", lambda *a, **k: None)
    out = rs_sfx_mod().from_ending(ir, None)
    assert out and out[0]["src"] == "assets_sfx:bell", out
    monkeypatch.setattr(rs_sfx_mod(), "_pick", lambda *a, **k: {"id": "sfx.outro_bell.01"})
    out2 = rs_sfx_mod().from_ending(ir, None)
    assert out2[0]["src"] == "assets_sfx:outro_bell", out2


def rs_sfx_mod():
    import rs_sfx
    return rs_sfx


# ---------------------------------------------------------------- M6 镜像音轨死参

def test_m6_mirror_audio_twin_signature():
    """M6:changes 形参已删,调用点不再有字典推导冗余包装。"""
    sig = inspect.signature(rs_edit._mirror_audio_twin)
    assert "changes" not in sig.parameters, "changes 死参仍在(M6 复发)"
    src = _src(rs_edit)
    assert '{k: v for k, v in boxes["clip"]}' not in src, "调用点冗余包装仍在"
    assert "_mirror_audio_twin(ctx, clip, pre)" in src


# ---------------------------------------------------------------- M7 元素删除只标 S4

def _doc_with_element() -> dict:
    return {"tracks": [
        {"id": "V1", "kind": "video", "clips": [
            {"id": "c1", "src": "a.mp4", "startMs": 0, "durationMs": 2000}]},
        {"id": "V2", "kind": "video", "name": "overlay", "clips": [
            {"id": "e1", "src": "elements/arrow.png", "assetId": "element.arrow.right.01",
             "startMs": 0, "durationMs": 500}]},
    ]}


def test_m7_element_remove_marks_only_s4(tmp_path, monkeypatch):
    """M7:删元素只标 S4(不标 S3、不重复登记),元素真的被删。"""
    doc = _doc_with_element()
    ctx = rs_edit.Ctx(tmp_path, doc, 30, "test", 1, None)
    monkeypatch.setattr(rs_edit, "validate_after", lambda op, idx: {})
    rs_edit.op_element_remove(ctx, {"op": "element.remove", "target": "e1"}, 0)
    assert ctx.dirty == {"S4"}, f"脏标记应仅 S4,得到 {ctx.dirty}"
    assert doc["tracks"][1]["clips"] == [], "元素未被删除"
    assert len(ctx.pending_entries) == 1, "oplog 待写条目应恰一条"


# ---------------------------------------------------------------- M8 元素守卫补覆盖轨校验

def test_m8_guard_rejects_non_overlay_element(tmp_path, monkeypatch):
    """M8:assetId 段不在覆盖轨 → BAD_ADDRESS(docstring 与实现对齐)。"""
    doc = {"tracks": [{"id": "V1", "kind": "video", "clips": [
        {"id": "c2", "src": "b.png", "assetId": "element.arrow.right.01",
         "startMs": 0, "durationMs": 500}]}]}
    ctx = rs_edit.Ctx(tmp_path, doc, 30, "test", 1, None)
    monkeypatch.setattr(rs_edit, "validate_after", lambda op, idx: {"durationMs": 800})
    with pytest.raises(rs_edit.EditError) as ei:
        rs_edit.op_element_retime(ctx, {"op": "element.retime", "target": "c2",
                                        "after": {"durationMs": 800}}, 0)
    assert "覆盖轨" in str(ei.value)
    # 覆盖轨上的元素段照常通过(与 M7 同构的 doc)
    doc2 = _doc_with_element()
    ctx2 = rs_edit.Ctx(tmp_path, doc2, 30, "test", 1, None)
    rs_edit._element_clip_guard(ctx2, {"op": "element.retime", "target": "e1"})


# ---------------------------------------------------------------- M9 _boundary_plans 重复赋值

def test_m9_no_duplicate_boundary_assignment():
    """M9:res_out["boundary"] 的重复赋值已删(源码恰一次)。"""
    src = inspect.getsource(rs_render._boundary_plans)
    assert src.count('b = res_out["boundary"]') == 1, "M9 复发:重复赋值仍在"


# ---------------------------------------------------------------- M10 rs_verify 变量遮蔽

def test_m10_usage_events_no_shadowing():
    """M10:flash 检查循环改 fi/ft,不再遮蔽 effects 检查的 i/t。"""
    src = _src(rs_verify)
    assert "for fi, ft in enumerate(fl)" in src
    # fl 窗口扫描必须用新名(遮蔽旧写法不会再回来)
    assert "for i, t in enumerate(fl)" not in src


# ---------------------------------------------------------------- M11 t2_glsl 资源回收(行为级另测)

def test_m11_render_overlap_cleanup_guards():
    """M11:framebuffer 循环外复用 + finally 统一回收子进程(源码级防复发;
    行为级真渲见 test_t2_uv_sampling)。"""
    import rs_fx.t2_glsl as t2
    src = _src(t2)
    assert "dec_a = dec_b = enc = None" in src, "子进程未初始化为 None(finally 不可达)"
    assert src.count("simple_framebuffer((w, h))") == 1, "framebuffer 未复用(M11 复发)"
    assert "fbo.release()" in src and "p.terminate()" in src and "p.kill()" in src


# ---------------------------------------------------------------- M12 BGM file 字段语义统一

def test_m12_bgm_file_semantics_unified(monkeypatch):
    """M12:file = manifest 原值(相对 assets/);repoRelPath 由它拼出,不再取 basename。"""
    fake = [{"id": "bgm.x.01", "label": "X", "file": "bgm/x.mp3", "commercial": True,
             "pacingFit": ["fast"]}]
    monkeypatch.setattr(rs_asset, "assets", lambda kind=None: fake if kind == "bgm" else [])
    tracks = rs_intent._bgm_tracks()
    assert tracks[0]["file"] == "bgm/x.mp3", "file 应为 manifest 原值(M12)"
    pick = rs_intent.bgm_library_pick("fast")
    assert pick["repoRelPath"] == "skills/cutflow/assets/bgm/x.mp3", pick
    assert pick["file"] == "bgm/x.mp3"


def test_m12_fallback_manifest_file_prefixed(monkeypatch, tmp_path):
    """兼容件裸文件名 → 显式补 bgm/ 前缀(两条来源同一语义)。"""
    man = tmp_path / "manifest.json"
    man.write_text(json.dumps({"tracks": [{"id": "b1", "name": "B1",
                                           "file": "bare.mp3", "pacingFit": ["fast"]}]}),
                   encoding="utf-8")
    monkeypatch.setattr(rs_asset, "assets", lambda kind=None: [])
    monkeypatch.setattr(rs_intent, "BGM_LIBRARY_DIR", tmp_path)
    tracks = rs_intent._bgm_tracks()
    assert tracks[0]["file"] == "bgm/bare.mp3"
    pick = rs_intent.bgm_library_pick("fast")
    assert pick["repoRelPath"] == "skills/cutflow/assets/bgm/bare.mp3"


# ---------------------------------------------------------------- M13 build_from_cards 补 A1

def test_m13_cards_ir_has_audio_track_id():
    """M13:build_from_cards 的音轨带 id=A1(与 build_from_cutlist 契约对称)。"""
    wl = {"chars": [{"ch": c, "startMs": i * 200, "endMs": i * 200 + 180}
                    for i, c in enumerate("大家好今天讲桌面运维")],
          "srcDurationMs": 3000, "finalDurationMs": 3000}
    manifest = {"items": [{"id": "c1", "output": "cards/c1/index.html"}]}
    doc = rs_ir.build_from_cards(manifest, wl, [{"card": "c1", "match": "今天"}],
                                 slug="t", ratio="9x16")
    ids = [t.get("id") for t in doc["tracks"]]
    assert ids == ["V1", "A1"], ids


# ---------------------------------------------------------------- M14 字体查表三处合一

def test_m14_font_lookup_shared(tmp_path, monkeypatch):
    """M14:三处委托 rs_common 同一查表;表缺失各自按口径兜底。"""
    fj = tmp_path / "fonts.json"
    fj.write_text(json.dumps({"default": {"subtitle": "fira"},
                              "fonts": [{"dir": "fira", "family": "Fira Code"}]}),
                  encoding="utf-8")
    monkeypatch.setattr(rs_common, "FONTS_JSON", fj)
    monkeypatch.setattr(rs_subtitle, "_font_state", {"family": None, "degraded": False,
                                                     "reason": ""})
    assert rs_subtitle.resolve_font() == "Fira Code"
    import rs_artboard
    assert rs_artboard._css_font() == "Fira Code"
    assert rs_artboard.default_fonts() == "fira"
    # 查表落空:subtitle 降级内置兜底,artboard 各自回退
    fj2 = tmp_path / "fonts2.json"
    fj2.write_text(json.dumps({"default": {"subtitle": "nope"}, "fonts": []}),
                   encoding="utf-8")
    monkeypatch.setattr(rs_common, "FONTS_JSON", fj2)
    monkeypatch.setattr(rs_subtitle, "_font_state", {"family": None, "degraded": False,
                                                     "reason": ""})
    assert rs_subtitle.resolve_font() == rs_subtitle.FONT_FALLBACK
    assert rs_subtitle.font_state()["degraded"] is True
    assert rs_artboard._css_font() == "Source Han Sans SC"
    # default_fonts 语义 = default.subtitle 目录名原样返回(表在即直通,与旧实现一致)
    assert rs_artboard.default_fonts() == "nope"


def test_m14_delegation_wiring():
    """源码级:三处确实经 rs_common(实现不再各写一套取 default → 查 family)。"""
    import rs_artboard
    assert "rs_common.resolve_font_family()" in _src(rs_subtitle)
    assert "rs_common.resolve_font_family()" in _src(rs_artboard)
    assert "rs_common.default_font_dir()" in _src(rs_artboard)


# ================================================================ T2.9 登记待实现三要素

def _load_catalog() -> dict:
    import rs_effects
    doc = rs_effects.load_catalog()
    assert doc is not None, "catalog.json 不存在(先 normalize)"
    return doc


def test_t29_pending_entries_have_three_elements():
    """T2.9:53 条登记待实现 100% 带归属册号(owner)/触发条件(trigger)/
    降级行为与留痕字段(degrade)。"""
    import collections
    pending = [e for e in _load_catalog()["effects"]
               if e.get("status") == "登记待实现"]
    assert len(pending) == 53, f"登记待实现条目数漂移:{len(pending)}"
    missing = [e["id"] for e in pending
               if not all(str(e.get(k) or "").strip()
                          for k in ("owner", "trigger", "degrade"))]
    assert not missing, f"三要素缺失:{missing[:5]}"
    owners = collections.Counter(e["owner"] for e in pending)
    assert set(owners) <= {"第二册", "第三册", "第四册"}, owners
    # 降级行为按实际代码分两态:已注册 T2 占位(fxDegraded)/未注册(FX_UNREGISTERED)
    flavors = {"T2 占位" in e["degrade"] for e in pending}
    assert flavors == {True, False}, "降级行为必须同时覆盖两种真实代码路径"


def test_t29_owner_mapping_matches_domain():
    """owner 按消费域机械映射(render→第二册;element/artboard→第三册;subtitle→第四册)。"""
    import rs_effects
    for e in _load_catalog()["effects"]:
        if e.get("status") != "登记待实现":
            continue
        want = rs_effects.PENDING_OWNER_BY_DOMAIN.get(str(e.get("domain")))
        assert e["owner"] == want, f"{e['id']}: owner {e['owner']} ≠ 映射 {want}"


def test_t29_check_gate_enforces_three_elements():
    """check 门禁:抽掉任一要素即红(防「补齐后被静默拿掉」)。"""
    import copy
    import rs_effects
    doc = copy.deepcopy(_load_catalog())
    target = next(e for e in doc["effects"] if e.get("status") == "登记待实现")
    target.pop("trigger", None)
    ok, errs = rs_effects.check(doc)
    assert not ok and any("trigger" in x for x in errs), errs[:5]
    # 完整目录必须全绿
    ok2, errs2 = rs_effects.check(_load_catalog())
    assert ok2, errs2[:5]


def test_t29_normalize_regeneration_is_enriched():
    """normalize 的产出(非手工补丁)自带三要素:对 normalize 重新生成的目录断言。"""
    import rs_effects
    catalog, _report = rs_effects.normalize()
    pending = [e for e in catalog["effects"] if e.get("status") == "登记待实现"]
    assert pending and all(all(str(e.get(k) or "").strip()
                               for k in ("owner", "trigger", "degrade"))
                           for e in pending)
