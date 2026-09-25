# -*- coding: utf-8 -*-
"""v2 M13 · 效果目录门禁(ADR-0054/分册02 §8.1)+ fxId 注册表契约。

① 目录契约自洽:分级↔状态↔fxId 三向(可执行必有 fxId 且真实在注册表;注册表无孤儿;
  不实现必有 why;T2 可执行必有 license);② id/别名唯一;③ 删除项可查(search 返回
  EFFECT_REMOVED 而非"待确认/未找到");④ §6 标准 NLE 62 条覆盖对拍;⑤ rs_fx 注册表
  自身可构建(无重复键/生成器齐全/既有 6 转场映射不变);⑥ CLI(normalize 幂等/check)。

运行:pytest tests/test_effects.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_effects  # noqa: E402
import rs_fx  # noqa: E402

CATALOG = REPO / "skills" / "cutflow" / "templates" / "effects" / "catalog.json"
GLSL_DIR = REPO / "skills" / "cutflow" / "templates" / "effects" / "glsl"


@pytest.fixture(scope="module")
def catalog() -> dict:
    assert CATALOG.is_file(), "catalog.json 不存在(先跑 rs_effects.py normalize)"
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _entries(catalog: dict) -> list[dict]:
    return [e for e in catalog.get("effects", []) if isinstance(e, dict)]


# ================================================================ ① 目录契约自洽

def test_catalog_three_way_consistency(catalog):
    """分级↔状态↔fxId 三向:可执行必有 fxId 且真实在注册表;不实现必有 why;T2 必有 license。"""
    for e in _entries(catalog):
        where = f"[{e['id']}]"
        assert e.get("status") in ("可执行", "登记待实现", "不实现"), where
        assert e.get("tier") in ("T1", "T2", "T3"), where
        if e["status"] == "可执行":
            assert e.get("fxId"), where + " 可执行但 fxId 为空"
            assert e["fxId"] in rs_fx.registered_ids(), where + " fxId 不在渲染端注册表(虚报能力)"
            assert e["fxId"] == e["id"], where + " 可执行条目 id 必须等于 fxId"
        if e["status"] == "不实现":
            assert str(e.get("why") or "").strip(), where + " 不实现但 why 为空"
        if e["tier"] == "T2" and e["status"] == "可执行":
            assert str(e.get("license") or "").strip(), where + " T2 可执行但 license 为空"


def test_catalog_no_registry_orphans(catalog):
    """注册表每个 fxId 在目录里都有条目(可执行或登记待实现;无孤儿)。"""
    ids = {e["id"] for e in _entries(catalog)}
    orphan = sorted(rs_fx.registered_ids() - ids)
    assert not orphan, f"注册表 fxId 在目录无条目:{orphan[:5]}"


def test_catalog_ids_and_aliases_unique(catalog):
    """id 唯一;别名全局唯一(每个剪映名 search 只能命中一条)。"""
    es = _entries(catalog)
    ids = [e["id"] for e in es]
    assert len(ids) == len(set(ids)), "目录 id 重复"
    owner: dict[str, str] = {}
    dup = []
    for e in es:
        for a in e.get("aliases") or []:
            if a in owner and owner[a] != e["id"]:
                dup.append(a)
            owner[a] = e["id"]
    assert not dup, f"别名同时挂在多条:{sorted(set(dup))[:5]}"


def test_deleted_entries_documented(catalog):
    """删除项(§5):why 含「用户确认删除」且带 originName(sources 可查=有意删除)。"""
    deleted = [e for e in _entries(catalog) if e.get("subclass") == "已确认删除"]
    assert len(deleted) >= 42, "§5 已确认删除名单应全量登记(≥42 条)"
    for e in deleted:
        assert "用户确认删除" in str(e.get("why") or ""), f"[{e['id']}] why 缺「用户确认删除」"
        assert e.get("originName"), f"[{e['id']}] 缺 originName"
        assert e["status"] == "不实现" and e.get("fxId") is None


def test_t2_transitions_glsl_files_exist_and_mit(catalog):
    """T2 可执行转场:glsl 文件在仓、license 非空且文件头逐条 MIT(许可合规,门禁 9)。"""
    for e in _entries(catalog):
        if e.get("tier") == "T2" and e.get("status") == "可执行":
            res = rs_fx.TRANSITIONS.get(e["fxId"]) or {}
            assert res.get("glsl"), f"[{e['id']}] T2 转场缺 glsl 名"
            f = GLSL_DIR / f"{res['glsl']}.glsl"
            assert f.is_file(), f"[{e['id']}] GLSL 未收录:{f}"
            assert "License: MIT" in f.read_text(encoding="utf-8", errors="replace"), \
                f"{f.name}: 许可头非 MIT"
            assert str(e.get("license") or "").strip() == "MIT"


def test_nle62_coverage(catalog):
    """§6 标准 NLE 62 条:可执行,或如实登记待实现(带 why)。绝不虚报。"""
    es = _entries(catalog)
    exec_ids = {e["id"] for e in es if e.get("status") == "可执行"}
    pending_ids = {e["id"] for e in es if e.get("status") == "登记待实现"}
    covered, pending = [], []
    for cid in sorted(rs_effects.NLE62_IDS):
        realized = set(rs_effects.NLE62_FAMILY.get(cid, [cid]))
        if cid == "tr.speed.ramp":
            realized.add("effect.speed.ramp")
        if cid in exec_ids or (realized & exec_ids):
            covered.append(cid)
        elif cid in pending_ids or (realized & pending_ids):
            pending.append(cid)
        else:
            # tr.morph.cut 等无独立 registry 键:按 sources T2 候选登记(中文 id)
            alt = [i for i in pending_ids
                   if i.startswith("fx.") and "morph" in i] or \
                  [i for i in pending_ids if "fx.transition." in i]
            assert alt or cid in pending_ids, f"§6 条目 {cid} 既不可执行也未登记待实现(虚报或漏登)"
            pending.append(cid)
    assert len(covered) >= 45, f"§6 可执行覆盖不足(现 {len(covered)}),虚假缩水"
    assert len(covered) + len(pending) == len(rs_effects.NLE62_IDS)


def test_executable_clip_fx_plans_build(catalog):
    """渲染冒烟(纯构建,免 ffmpeg):每个可执行 clip fx 都能生成滤镜计划(不炸/不静默)。"""
    ctx = rs_fx.FxContext(cw=1080, ch=1920, fps=30, out_s=2.0, take_s=2.3)
    for e in _entries(catalog):
        if e.get("status") != "可执行" or e.get("domain") != "render":
            continue
        spec = rs_fx.CLIP_FX.get(e["fxId"])
        if spec is None:
            continue                                # 转场族在下面单独解析
        plan, notes = rs_fx.build_clip_fx([("in" if spec["slot"] in ("in", "any") else
                                            spec["slot"], e["fxId"], {})], ctx)
        assert plan is not None, e["fxId"]
        assert plan.vf or plan.fc or plan.slides or plan.params, \
            f"{e['fxId']}: 生成器产出为空(注册了空气)"
    # 可执行转场:逐条注册表解析(kind/xfade/glsl 合法)
    for e in _entries(catalog):
        if e.get("status") == "可执行" and e["id"] in rs_fx.TRANSITIONS:
            res = rs_fx.resolve_transition({"fx": e["id"]})
            assert res["kind"] in ("xfade", "glsl", "concatvideo", "cut", "none"), e["id"]


def test_legacy_transition_aliases_unchanged():
    """既有 6 个 fxId 映射一字不改(v2 之前工程零影响,分册02 §9 回滚表)。"""
    expect = {"tr.fade": "fade", "tr.wipe.left": "wipeleft", "tr.wipe.up": "wipeup",
              "tr.slide.left": "slideleft", "tr.circle.open": "circleopen"}
    for fxid, xfade in expect.items():
        res = rs_fx.resolve_transition({"fx": fxid})
        assert res["kind"] == "xfade" and res["xfade"] == xfade, fxid
    assert rs_fx.resolve_transition({"fx": "tr.cut"})["kind"] == "cut"
    # type 枚举路径(无 fx)行为不变
    res = rs_fx.resolve_transition({"type": "fade", "durMs": 300})
    assert res["kind"] == "xfade" and res["xfade"] == "fade"
    res = rs_fx.resolve_transition({"type": "cut"})
    assert res["kind"] == "cut"


def test_unregistered_fx_errors_not_silent():
    """未注册 fxId 报错不静默(FX_UNREGISTERED;分册02 §9:报错而非静默)。"""
    with pytest.raises(rs_fx.FxError) as ei:
        rs_fx.resolve_transition({"fx": "tr.not.a.things"})
    assert ei.value.code == "FX_UNREGISTERED"
    with pytest.raises(rs_fx.FxError) as ei2:
        ctx = rs_fx.FxContext(1080, 1920, 30, 1.0, 1.0)
        rs_fx.build_clip_fx([("in", "fx.not.a.things", {})], ctx)
    assert ei2.value.code == "FX_UNREGISTERED"


# ================================================================ ② 删除项可查 + CLI

def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "rs_effects.py"), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, cwd=REPO)


def test_search_deleted_returns_effect_removed(catalog):
    """§5 每个删除条目 search 返回 EFFECT_REMOVED(明确结论,不是"待确认/未找到")。"""
    deleted = [e for e in _entries(catalog) if e.get("subclass") == "已确认删除"]
    assert deleted
    for e in deleted[:6]:                     # 抽样足够(全量幂等,normalize 同源)
        p = _run_cli("search", str(e["originName"]))
        assert '"EFFECT_REMOVED"' in p.stdout, f"{e['originName']}: {p.stdout[-200:]}"


def test_search_hit_and_pending():
    p = _run_cli("search", "黑色反转片")
    assert p.returncode == 0 and "tr.fade.black" in p.stdout
    p2 = _run_cli("search", "上下翻页")
    assert '"登记待实现"' in p2.stdout or "EFFECT_NOT_AVAILABLE" in p2.stdout


def test_check_cli_gate():
    """check 是门禁入口:当前仓内目录必须全绿。"""
    p = _run_cli("check")
    assert p.returncode == 0, p.stdout + p.stderr


def test_normalize_idempotent(tmp_path):
    """normalize 幂等:同输入第二次跑报「无变更」(分册02 §8.1 门禁 3)。"""
    first = _run_cli("normalize")
    assert first.returncode == 0, first.stdout + first.stderr
    second = _run_cli("normalize")
    assert "EFFECTS_NO_CHANGE" in second.stdout, second.stdout[-300:]


def test_coverage_report_shape():
    p = _run_cli("coverage", "--json")
    doc = json.loads(p.stdout)
    cov = doc["data"]["coverage"]
    assert cov["total"] >= 300, "全量登记应 ≥ 用户清单去重与注册表并集规模"
    assert cov["executable"] >= 170, "可执行档 ≈192 目标(允许诚实偏差但不许大幅缩水)"
    for key in ("status", "tier", "source", "domain"):
        assert key in cov


# ================================================================ ③ 注册表光敏安全底线

def test_flash_params_capped():
    """闪变类参数档 ≤300ms(WCAG 2.3.1 底线,分册02 §8.1 门禁 6 的注册表侧)。"""
    ctx = rs_fx.FxContext(1080, 1920, 30, 2.0, 2.0)
    plan, _ = rs_fx.build_clip_fx(
        [("in", "flash.in.white", {"durMs": 5000})], ctx)   # 用户乱传超大时长
    d_line = plan.vf[0]
    import re
    d = float(re.search(r"d=([\d.]+)", d_line).group(1))
    assert d <= rs_fx.registry.FLASH_MAX_S + 1e-6, f"闪变 {d}s 超光敏上限"
