# -*- coding: utf-8 -*-
"""v2 M13 · 剪辑语法用法层(ADR-0059/分册06,门禁表 #4/#5)。

①决策树存在(rules/editing-grammar.md 用法层 B 节,四问按序);
②13 类无技巧转场映射表完整,每条手法都有可执行 fxId 或明确「无需特效」;
③转场决策表的每个推荐 fxId 都真实可执行(catalog 可查,不虚报);
④风格包 effects_prescription 字段完整(分册06 §10.1 门禁 2 扩展)。

运行:pytest tests/test_editing_grammar.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_effects  # noqa: E402
import rs_fx  # noqa: E402
import rs_stylepack  # noqa: E402

GRAMMAR = REPO / "skills" / "cutflow" / "rules" / "editing-grammar.md"

THIRTEEN = ["相同主体", "相似体", "承接因素", "两级镜头", "特写转场", "挡黑镜头",
            "声音转场", "空镜头", "主观镜头", "运动镜头", "逻辑转场", "隐喻式转场",
            "反差因素"]


@pytest.fixture(scope="module")
def grammar_text() -> str:
    assert GRAMMAR.is_file()
    return GRAMMAR.read_text(encoding="utf-8")


def test_decision_tree_exists(grammar_text):
    """转场决策树(四问按序)写进文档:能接上吗→时空/段落→运动节奏→能一句话解释吗。"""
    assert "转场决策树" in grammar_text
    for q in ("接上", "时空跳跃", "段落切换", "运动/节奏", "一句话解释"):
        assert q in grammar_text, f"决策树缺关键问句:{q}"
    assert grammar_text.index("转场决策树") < grammar_text.index("无技巧转场 13 类"), \
        "决策树应在 13 类映射表之前(先问要不要,再问怎么接)"


def test_thirteen_no_trick_transitions_mapped(grammar_text):
    """13 类无技巧转场逐类有落地:fxId(可执行)或明确「无需特效」;第一反应纪律在册。"""
    m = re.search(r"### 27\. 无技巧转场 13 类.*?(?=\n### 28\.)", grammar_text, re.S)
    assert m, "13 类映射表(B-27 节)缺失"
    table = m.group(0)
    for name in THIRTEEN:
        assert name in table, f"13 类缺:{name}"
    n_nofx = table.count("无需特效")
    assert n_nofx >= 8, f"13 类里至少 9 类不需要特效(现 {n_nofx} 条「无需特效」)"
    # 表内出现的 fxId 必须真实可执行(不虚报)
    for fxid in set(re.findall(r"fxId[:：]\s*([A-Za-z][\w.]*)", table)):
        assert rs_effects.is_executable(fxid), f"13 类映射表推荐了不可执行 fxId:{fxid}"
    assert "第一反应" in grammar_text, "「第一反应=能不能无技巧」纪律必须在册"


def test_transition_decision_table_fxids_executable(grammar_text):
    """转场决策表(B-28)每个推荐 fxId 在目录可执行;禁忌列在册。"""
    m = re.search(r"### 28\. 转场选择决策表.*?(?=\n### 29\.)", grammar_text, re.S)
    assert m, "转场决策表(B-28)缺失"
    fxids = set(re.findall(r"\b(tr\.[\w.]+|fx\.[\w.]+)\b", m.group(0)))
    assert fxids, "决策表没有任何 fxId"
    for fxid in fxids:
        # tr.wipe.linear 等参数化家族 id(NLE62)按「家族内全部落地档可执行」判定
        fam = rs_effects.NLE62_FAMILY.get(fxid)
        if fam:
            assert all(rs_effects.is_executable(x) for x in fam), \
                f"决策表家族 {fxid} 有未落地档"
            continue
        assert fxid in rs_fx.TRANSITIONS, f"决策表引用未注册转场:{fxid}"
        assert rs_effects.is_executable(fxid), f"决策表引用不可执行转场:{fxid}"
    assert "禁忌" in m.group(0)


def test_density_discipline_hard_numbers(grammar_text):
    """密度纪律硬数字(B-30):1 处/8s、片长÷10、花哨 ≤3、10s 不重复、闪变 ≤3 次/秒。"""
    m = re.search(r"## 30\. 密度与节制纪律.*?(?=\nEOF|\Z)", grammar_text, re.S)
    table = grammar_text[grammar_text.index("### 30."):]
    for token in ("8s", "÷10", "≤3", "10s", "FLASH_UNSAFE", "EFFECTS_REPEATED",
                  "EFFECTS_UNMOTIVATED"):
        assert token in table, f"密度纪律缺 {token}"


def test_stylepacks_prescription_complete():
    """每个风格包必须声明 effects_prescription(分册06 §10.1 门禁 2):
    字段齐、min≤max、prefer∩forbid=∅、prefer/forbid 的 fxId 在目录登记。"""
    required = {"transition", "in", "out", "sfx", "flashy_max", "require_reason"}
    catalog_ids = {e["id"] for e in
                   rs_effects.entries_of(rs_effects.load_catalog())}
    for slug in sorted(d.name for d in rs_stylepack.PACKS_DIR.iterdir()
                       if d.is_dir() and not d.name.startswith("_")):
        pack = rs_stylepack.load_pack(slug)
        assert pack, slug
        pres = (pack["params"] or {}).get("effects_prescription")
        assert isinstance(pres, dict) and pres, f"{slug}: 缺 effects_prescription"
        assert required <= set(pres), f"{slug}: 处方缺字段 {sorted(required - set(pres))}"
        for key in ("transition", "in", "out", "sfx"):
            seg = pres.get(key) or {}
            mn, mx = seg.get("min", 0), seg.get("max")
            if mx is not None:
                assert mn <= mx, f"{slug}.{key}: min({mn}) > max({mx})"
            prefer = [str(x) for x in (seg.get("prefer") or [])]
            forbid = [str(x) for x in (seg.get("forbid") or [])]
            assert not (set(prefer) & set(forbid)), f"{slug}.{key}: prefer∩forbid ≠ ∅"
            def _ok(f: str) -> bool:
                if f in catalog_ids or f == "beat.snap":
                    return True
                fam = rs_effects.NLE62_FAMILY.get(f)
                return bool(fam) and all(x in catalog_ids for x in fam)
            unknown = [f for f in prefer + forbid if not _ok(f)]
            assert not unknown, f"{slug}.{key}: 处方引用目录外 fxId {unknown}"
        assert isinstance(pres["flashy_max"], int) and pres["flashy_max"] >= 0
        assert isinstance(pres["require_reason"], bool)
    # 各型纪律:纯口播/录屏 flashy_max=0(§7.1/§7.7),混剪允许 ≤3(§7.5)
    talk = rs_stylepack.load_pack("knowledge-talkshow-douyin")["params"]["effects_prescription"]
    mix = rs_stylepack.load_pack("mixcut-douyin")["params"]["effects_prescription"]
    assert talk["flashy_max"] == 0 and mix["flashy_max"] == 3
