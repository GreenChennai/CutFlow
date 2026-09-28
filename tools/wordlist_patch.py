"""词表治理自动化(T4.11):切词事故文本 → COMMON_WORDS / PROTECTED_WORDS / REGRESSION 三处补丁草案。

用法:
  python tools/wordlist_patch.py "借款时发生纳税年度内周转|归还依据财税〔2003〕|158号文件的规定"
  python tools/wordlist_patch.py "…事故文本…" --max-chars 12 --json out.json

输入约定:事故文本用「|」标出**实际发生**的坏切点(卡与卡的边界,与原文等长,
只多出 | 号)。工具据此:
  1. 复现事故:检查每个坏切点是否落在 word_spans 词跨度内部(词内切)或
     切在 NEG/复合词语义单元上;
  2. 产出三处**可直接合入**的补丁草案(人审后合入,不自动写盘改源码):
     · COMMON_WORDS 兜底词表 —— 2–4 字高频词,兜底分词路径认词;
     · PROTECTED_WORDS —— 动宾搭配/专名,始终强制并入词跨度(与 terms 同强度);
     · REGRESSION —— 回归用例,固化本次事故文本,防止复现。
  3. 已在词表/已有回归集里的词自动跳过(不重复提案)。

零第三方依赖(标准库 + segmentation)。人审纪律见 rules/subtitles.md §4.2:
新切词案例 = 词表 + REGRESSION 双保险,替代人工散点补词。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "cutflow" / "scripts"))

import segmentation as sg  # noqa: E402


def parse_accident(raw: str) -> tuple[str, list[int]]:
    """「a|b|c」形态的事故文本 → (原文, 坏切点位置集合)。

    切点位置 = 在 text[pos-1] 与 text[pos] 之间切(与 forbidden_positions 同口径)。
    ASCII 词内的 | 原样保留语义:同样按位置计入(ASCII 词内切也是事故)。
    """
    text_chars: list[str] = []
    cuts: list[int] = []
    for ch in raw:
        if ch == "|":
            cuts.append(len(text_chars))
            continue
        text_chars.append(ch)
    return "".join(text_chars), cuts


def build_patches(accident: str, max_chars: int = 12) -> dict:
    """事故文本 → 三处补丁草案(+ 逐切点诊断)。纯函数,可直接单测。"""
    text, cuts = parse_accident(accident)
    spans = sg.word_spans(text)
    compounds = sg.SEMANTIC_COMPOUNDS

    common = []            # (word, reason)
    protected = []         # (word, reason)
    compounds_hit = []     # (word, reason) —— SEMANTIC_COMPOUNDS 跨卡(I1)
    regression_terms = []  # terms 建议词
    per_cut = []
    for p in sorted(set(cuts)):
        if p <= 0 or p >= len(text):
            per_cut.append({"pos": p, "verdict": "invalid", "reason": "切点越界"})
            continue
        around = text[max(0, p - 3):p] + "|" + text[p:p + 3]
        entry: dict = {"pos": p, "around": around}
        compound_hit = None
        for t in compounds:                      # 复合词跨卡(I1):运营|效率 店群|企业
            k = text.find(t)
            while k >= 0:
                if k < p < k + len(t):
                    compound_hit = t
                    break
                k = text.find(t, k + 1)
            if compound_hit:
                break
        crossing = next(((a, b) for a, b in spans if a < p < b), None)
        if crossing:
            a, b = crossing
            word = text[a:b]
            entry.update({"verdict": "in-word", "word": word,
                          "reason": f"词「{word}」被拦腰切断"})
            already_protected = word in sg.PROTECTED_WORDS or word in sg.DEFAULT_IDIOMS
            if 2 <= len(word) <= 4 and word not in sg.COMMON_WORDS and not already_protected:
                common.append((word, f"{entry['around']} 实测切词"))
            if not already_protected and 2 <= len(word) <= 6:
                protected.append((word, f"{entry['around']} 动宾/专名搭配,须整体并入词跨度"))
            if word not in regression_terms:
                regression_terms.append(word)
        elif compound_hit:
            entry.update({"verdict": "compound", "word": compound_hit,
                          "reason": f"复合词「{compound_hit}」被拆到两卡(I1 语义单元跨卡)"})
            if compound_hit not in compounds_hit:
                compounds_hit.append(
                    (compound_hit, f"{entry['around']} jieba/词表不认为是词,但语义上不可拆"))
            if compound_hit not in regression_terms:
                regression_terms.append(compound_hit)
        else:
            bigram = text[max(0, p - 1):p + 1]
            cjk = all("\u4e00" <= ch <= "\u9fff" for ch in bigram) if bigram else False
            # 功能词/虚词不得当候选搭配(「月的」「地看」这类提案是噪声)
            functional = any(ch in sg.FORBID_AFTER + sg.TAIL_FUNC + "了不没是在和与"
                             for ch in bigram)
            entry.update({"verdict": "boundary", "reason":
                          f"切点在词边界上(非词内切);若语义仍断裂,请人工判断是否属于"
                          f"复合词/动宾搭配(候选二元组「{bigram}」)"})
            if len(bigram) == 2 and cjk and not functional and bigram not in sg.COMMON_WORDS \
                    and not any(bigram in c for c in compounds) \
                    and bigram not in sg.PROTECTED_WORDS:
                protected.append((bigram, f"{entry['around']} 候选搭配(人工确认后合入)"))
        per_cut.append(entry)
    regression_case = None
    if text and (common or protected or regression_terms):
        regression_case = {"text": text, "terms": tuple(regression_terms),
                           "must_not_split": tuple(regression_terms)}
    dup = any(r.get("text") == text for r in sg.REGRESSION)
    return {"accident": accident, "text": text, "cuts": sorted(set(cuts)),
            "perCut": per_cut,
            "patches": {
                "COMMON_WORDS": sorted({w for w, _ in common}),
                "PROTECTED_WORDS": sorted({w for w, _ in protected}),
                "SEMANTIC_COMPOUNDS": sorted({w for w, _ in compounds_hit}),
                "REGRESSION": [] if (regression_case is None or dup) else [regression_case],
            },
            "alreadyCovered": dup}


def render_report(doc: dict) -> str:
    """可直接阅读/粘贴的报告:三处补丁草案以**源码片段**形态给出。"""
    text, patches = doc["text"], doc["patches"]
    lines = [f"# 切词事故补丁草案(源文本:{text})", ""]
    lines.append("## 逐切点诊断")
    for c in doc["perCut"]:
        lines.append(f"- pos {c['pos']} {c.get('around', '')}: {c['verdict']} —— {c['reason']}")
    lines += ["", "## 补丁 1:COMMON_WORDS(segmentation.py 兜底词表)"]
    if patches["COMMON_WORDS"]:
        lines.append("```python")
        lines.append("COMMON_WORDS 追加:")
        lines.append("    " + " ".join(patches["COMMON_WORDS"]))
        lines.append("```")
    else:
        lines.append("(无 —— 坏切点都已被现有词表覆盖,或不属于 2–4 字高频词)")
    lines += ["", "## 补丁 2:PROTECTED_WORDS(segmentation.py 强制并入词跨度)"]
    if patches["PROTECTED_WORDS"]:
        lines.append("```python")
        lines.append("PROTECTED_WORDS 追加:")
        lines.append("    " + ", ".join(f'"{w}"' for w in patches["PROTECTED_WORDS"]) + ",")
        lines.append("```")
    else:
        lines.append("(无)")
    lines += ["", "## 补丁 2b:SEMANTIC_COMPOUNDS(segmentation.py I1 复合词罚分表)"]
    if patches.get("SEMANTIC_COMPOUNDS"):
        lines.append("```python")
        lines.append("SEMANTIC_COMPOUNDS 追加:")
        lines.append("    " + ", ".join(f'"{w}"' for w in patches["SEMANTIC_COMPOUNDS"]) + ",")
        lines.append("```")
    else:
        lines.append("(无)")
    lines += ["", "## 补丁 3:REGRESSION(segmentation.py 回归集)"]
    for case in patches["REGRESSION"]:
        terms = ", ".join(f'"{t}"' for t in case["terms"])
        mns = ", ".join(f'"{t}"' for t in case["must_not_split"])
        lines.append("```python")
        lines.append('    {"text": "' + case["text"] + '", "terms": (' + terms + '),')
        lines.append('     "must_not_split": (' + mns + ')},')
        lines.append("```")
    if doc["alreadyCovered"]:
        lines.append("(该事故文本已在 REGRESSION 中,跳过)")
    if not patches["REGRESSION"] and not doc["alreadyCovered"]:
        lines.append("(无 —— 未产生新回归用例)")
    lines += ["", "> 人审后合入;合入前跑 tests/test_v8.py::test_regression_all_cases_words_intact。"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("accident", help="事故文本,用 | 标出实际坏切点")
    ap.add_argument("--max-chars", dest="max_chars", type=int, default=12)
    ap.add_argument("--json", dest="json_out", default=None,
                    help="补丁草案同时写 JSON 文件(供机器合入)")
    a = ap.parse_args()
    doc = build_patches(a.accident, a.max_chars)
    print(render_report(doc))
    if a.json_out:
        payload = {**doc,
                   "patches": {k: ([{**c, "terms": list(c["terms"]),
                                     "must_not_split": list(c["must_not_split"])}
                                    for c in v] if k == "REGRESSION" else v)
                               for k, v in doc["patches"].items()}}
        Path(a.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
        print(f"\nJSON 草案已写:{a.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
