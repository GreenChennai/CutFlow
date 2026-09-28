"""第四册 T4.11 回归:词表治理自动化(tools/wordlist_patch.py)。

判据(计划文档 4.2 T4.11):输入一条「切词事故」文本,输出可直接合入的
COMMON_WORDS / PROTECTED_WORDS(+SEMANTIC_COMPOUNDS)/ REGRESSION 三处补丁草案。

运行:pytest tests/test_wordlist_patch.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "skills" / "cutflow" / "scripts"))

import wordlist_patch as wp  # noqa: E402
import segmentation as sg  # noqa: E402


# ---------------------------------------------------------------- 解析

def test_parse_accident_marks_to_cuts():
    text, cuts = wp.parse_accident("周转|归还资金")
    assert text == "周转归还资金"
    assert cuts == [2]


def test_parse_accident_multi_marks():
    text, cuts = wp.parse_accident("借款|时周转|归还依据")
    assert text == "借款时周转归还依据"
    assert cuts == [2, 5]


# ---------------------------------------------------------------- 补丁生成

def test_in_word_cut_produces_three_patches():
    """词内切事故 → 三处补丁草案齐备且可直接合入(词不在表内才提案)。"""
    # 「及时」不在 COMMON_WORDS/PROTECTED_WORDS,jieba 跨度(及时)被 | 拦腰切断
    doc = wp.build_patches("请大家及|时反馈问题")
    assert doc["perCut"][0]["verdict"] == "in-word"
    patches = doc["patches"]
    assert patches["COMMON_WORDS"], patches
    assert patches["PROTECTED_WORDS"], patches
    reg = patches["REGRESSION"]
    assert reg, patches
    case = reg[0]
    assert case["text"] == "请大家及时反馈问题"
    assert case["terms"] and case["must_not_split"]


def test_compound_cut_proposes_semantic_compound():
    """I1 复合词跨卡事故 → SEMANTIC_COMPOUNDS 补丁 + REGRESSION 用例。"""
    doc = wp.build_patches("这个运营|效率方案能提高收益")
    verdicts = [c["verdict"] for c in doc["perCut"]]
    assert "compound" in verdicts
    assert "运营效率" in doc["patches"]["SEMANTIC_COMPOUNDS"]
    assert doc["patches"]["REGRESSION"], doc["patches"]


def test_protected_words_not_reproposed():
    """已在 PROTECTED_WORDS 的词不重复提案(「周转归还」实测案例已收口)。"""
    doc = wp.build_patches("借款时发生纳税年度内周转|归还依据财税〔2003〕158号文件的规定")
    patches = doc["patches"]
    assert "周转归还" not in patches["COMMON_WORDS"]
    assert "周转归还" not in patches["PROTECTED_WORDS"]


def test_already_covered_regression_skipped():
    """事故文本已在 REGRESSION → 不再产出重复用例,标 alreadyCovered。"""
    doc = wp.build_patches("借款时发生纳税年度内周转|归还依据财税〔2003〕158号文件的规定")
    assert doc["alreadyCovered"] is True
    assert doc["patches"]["REGRESSION"] == []


def test_boundary_cut_flags_candidate_bigram():
    """切点在词边界上(非词内切)→ 给出候选二元组供人工判断,不臆测入表。"""
    doc = wp.build_patches("我们测了三个月|的数据发现转化率翻倍")
    entry = doc["perCut"][0]
    assert entry["verdict"] == "boundary"
    # 「月|的」的候选二元组含功能词「的」,不得直接提案为保护词
    assert "月的" not in doc["patches"]["PROTECTED_WORDS"]
    assert "的" not in "".join(doc["patches"]["PROTECTED_WORDS"])


# ---------------------------------------------------------------- 输出形态

def test_render_report_contains_all_three_patches():
    doc = wp.build_patches("请大家及|时反馈问题")
    report = wp.render_report(doc)
    assert "补丁 1:COMMON_WORDS" in report
    assert "补丁 2:PROTECTED_WORDS" in report
    assert "补丁 3:REGRESSION" in report
    assert "及时" in report


def test_json_output_roundtrip(tmp_path):
    """--json 产物可解析且含全部三处补丁(机器合入通道)。"""
    doc = wp.build_patches("这个运营|效率方案能提高收益")
    out = tmp_path / "patch.json"
    payload = {**doc,
               "patches": {k: ([{**c, "terms": list(c["terms"]),
                                 "must_not_split": list(c["must_not_split"])}
                                for c in v] if k == "REGRESSION" else v)
                           for k, v in doc["patches"].items()}}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    back = json.loads(out.read_text(encoding="utf-8"))
    assert back["patches"]["SEMANTIC_COMPOUNDS"] == doc["patches"]["SEMANTIC_COMPOUNDS"]


def test_patches_are_mergeable_against_current_tables():
    """产出的补丁与现行词表无冲突(合入前机械自检:全部不在现有表内)。"""
    doc = wp.build_patches("这个运营|效率方案能提高收益")
    p = doc["patches"]
    assert not (set(p["COMMON_WORDS"]) & set(sg.COMMON_WORDS))
    assert not (set(p["PROTECTED_WORDS"]) & set(sg.PROTECTED_WORDS))
    for case in p["REGRESSION"]:
        assert not any(r["text"] == case["text"] for r in sg.REGRESSION)
