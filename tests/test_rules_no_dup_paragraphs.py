# -*- coding: utf-8 -*-
"""第一册 T1.4 门禁:rules 分册间重复段落检测。

同一口径在多个分册各写一段(安全区写三遍、响度写四遍)是 Token 消耗的结构性来源
(docs/BASELINE-v0.20.md §3.2)。本门禁对 rules/**/*.md(含 video-types/)做段落级
相似度对比:difflib.SequenceMatcher 归一化相似度 >0.8 且段落 >3 行的**跨文件**组合即红。

豁免:
  * 同文件内部的重复不归本门禁管(同一册内前后呼应是合法写法);
  * ≤3 行的短段(表头、单行铁律引用)不比——大量合法的短引用语会造成噪声;
  * 代码围栏块不比(样例命令天然雷同);
  * WHITELIST:逐对豁免,须写理由(如 video-types 分册按模板继承的固定栏目)。

运行:pytest tests/test_rules_no_dup_paragraphs.py -q
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "skills" / "cutflow" / "rules"

SIM_THRESHOLD = 0.8
MIN_LINES = 4  # 段落 >3 行才参与对比(与判据「>3 行」一致)

# 跨文件重复白名单:文件名对(无序)→ 理由。只对"段落对"维度豁免由段内容自动判定,
# 文件对级白名单用于整册结构性同构(模板继承),逐条写明理由。
PAIR_WHITELIST: dict[frozenset, str] = {}


@dataclass
class Para:
    file: str
    start: int
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def norm(self) -> str:
        return re.sub(r"\s+", "", self.text)


def _paragraphs(path: Path) -> list[Para]:
    """段落 = 空行分隔的连续非空行;代码围栏整体跳过。"""
    out: list[Para] = []
    cur: list[str] = []
    start = 1
    in_fence = False
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            if cur:
                out.append(Para(path.name, start, cur))
                cur = []
            continue
        if in_fence:
            continue
        if not raw.strip():
            if cur:
                out.append(Para(path.name, start, cur))
                cur = []
            continue
        if not cur:
            start = i
        cur.append(raw)
    if cur:
        out.append(Para(path.name, start, cur))
    return [p for p in out if len(p.lines) >= MIN_LINES]


def find_dup_pairs(threshold: float = SIM_THRESHOLD) -> list[str]:
    """返回「文件A:startL-startR ↔ 文件B:startL-startR (相似度)」问题清单。"""
    files = sorted(RULES.glob("*.md")) + sorted((RULES / "video-types").glob("*.md"))
    paras: dict[str, list[Para]] = {f.name: _paragraphs(f) for f in files}
    problems: list[str] = []
    names = sorted(paras)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if PAIR_WHITELIST.get(frozenset((a, b))):
                continue
            for pa in paras[a]:
                for pb in paras[b]:
                    # 快速预筛:长度差过大不可能过阈
                    if not (0.7 < len(pa.norm) / max(1, len(pb.norm)) < 1.43):
                        continue
                    ratio = difflib.SequenceMatcher(None, pa.norm, pb.norm).ratio()
                    if ratio > threshold:
                        problems.append(
                            f"{a}:{pa.start} ↔ {b}:{pb.start} 相似度 {ratio:.2f}\n"
                            f"  A> {pa.lines[0][:80]}\n  B> {pb.lines[0][:80]}")
    return problems


def test_no_duplicate_paragraphs_across_rules():
    problems = find_dup_pairs()
    assert not problems, (
        "分册间重复段落(同一口径只许一处文档出处;让位给 anchor 分册后写「见 X」):\n"
        + "\n".join(problems))
