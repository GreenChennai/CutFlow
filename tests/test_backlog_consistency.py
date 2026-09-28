# -*- coding: utf-8 -*-
"""第一册 T1.7 门禁:BACKLOG.md 同 ID 结论冲突检测。

背景(docs/BASELINE-v0.20.md §6.4-3):同一欠账 ID 在不同轮次的清账区里出现
「已清」与「仍成立」互斥的结论(实测:W6、尾部黑场 I3),台账自相矛盾。
本门禁解析 BACKLOG.md 里每个 ID 的多条状态声明,冲突即红。

判定口径(与台账头部约定一致):
  · 逐行分类:`[ ]` → open;`[x]` → resolved(行内含「部分」→ open,部分落地余账仍开);
    清账表处置格 `**清**`/`**关门**`/`**关闭**`/`**修(…**` → resolved;
    `**部分清**`/`**顺延**`(明确不做但记账)→ 不参与冲突判定;
  · ID 归属:checkbox 行只认**行首主 ID**(`- [x] **W6 …`),行内括号里的引用不算
    (如「B5 …(B3 已完成…)」不得把 B3 判成 open);清账表行整行归 ID;
  · 作用域:清账表行 = 全局结论(权威,清账就是对全部同名 ID 收口);
    checkbox 行 = 所属小节作用域 —— 不同轮次复用同字母 ID 但语义不同
    (如 v5 批次 A 的 A3 ≠ v4 批次 A 的 A3)不算冲突;
  · 冲突 = 同 ID「全局已清」vs「任一小节仍开」或「同小节内一清一开」。

运行:pytest tests/test_backlog_consistency.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKLOG = REPO / "docs" / "BACKLOG.md"

SUBJECT_RX = re.compile(r"^\s*-\s*\[[ xX]\]\s*(?:\*\*)?#?([WIABCRE])(\d{1,3})(?![\w-])")
ID_RX = re.compile(r"(?<![\w#])(#?[WIABCRE])(\d{1,3})(?![\w-])")
RESOLVED_RX = re.compile(r"\[[xX]\]|\*\*(?:清|关门|关闭)\*\*|\*\*修(?:\(|\*\*)")
OPEN_RX = re.compile(r"\[[ \]]\]|仍成立")
PARTIAL = "部分"
GLOBAL = "__清账表__"


def _norm(m: re.Match) -> str:
    return f"{m.group(1).lstrip('#')}{int(m.group(2))}"


def classify_line(line: str) -> set[str]:
    """一行 → {resolved / open} 状态集合(空 = 中性叙述,忽略)。"""
    st: set[str] = set()
    if RESOLVED_RX.search(line):
        st.add("resolved")
    if "[ ]" in line or (re.search(r"\[[xX]\]", line) and PARTIAL in line):
        st.add("open")
    if not st and OPEN_RX.search(line):
        st.add("open")
    # 「[x]」+「部分」:resolved 不成立,余账仍开
    if PARTIAL in line and "resolved" in st and re.search(r"\[[xX]\]", line):
        st.discard("resolved")
    return st


def detect_conflicts(text: str) -> list[str]:
    """返回冲突描述列表(空 = 无冲突)。"""
    claims: dict[tuple[str, str], set[str]] = {}
    section = GLOBAL
    for i, line in enumerate(text.splitlines(), 1):
        if line.startswith("#") and not line.startswith("#?"):
            section = line.strip("# ").strip() or GLOBAL
        st = classify_line(line)
        if not st:
            continue
        m = SUBJECT_RX.match(line)
        ids = [_norm(m)] if m else [_norm(x) for x in ID_RX.finditer(line)]
        # checkbox 主 ID 归小节;checkbox 行内被引用的 ID 同样归小节(旁引不是全局结论);
        # 只有清账表等非 checkbox 行才给全局结论
        scope = section if line.lstrip().startswith("- [") else GLOBAL
        for key in ids:
            claims.setdefault((scope, key), set()).update(st)
    problems: list[str] = []
    scopes: dict[str, dict[str, set[str]]] = {}
    for (scope, key), st in claims.items():
        scopes.setdefault(key, {})[scope] = st
    for key, by_scope in sorted(scopes.items()):
        g = by_scope.get(GLOBAL, set())
        # 全局已清 vs 任一小节仍开(经典 W6/I3 形态)
        if "resolved" in g and any("open" in v for k, v in by_scope.items() if k != GLOBAL):
            opens = [k for k, v in by_scope.items() if k != GLOBAL and "open" in v]
            problems.append(f"ID {key}:清账表判「已清/已修」,小节 {opens} 仍挂开账")
        # 同小节内一清一开
        for scope, st in by_scope.items():
            if scope != GLOBAL and {"resolved", "open"} <= st:
                problems.append(f"ID {key}:小节「{scope}」内已清与开账互斥")
    return problems


def test_backlog_has_no_conclusion_conflicts():
    text = BACKLOG.read_text(encoding="utf-8")
    conflicts = detect_conflicts(text)
    assert not conflicts, "BACKLOG.md 同 ID 结论冲突(修正到单一结论,历史行标注「过时,已收口」):\n" + "\n".join(conflicts)


def test_detector_catches_synthetic_contradiction():
    """检测器自证:W6 形态的互斥(清账表「清」vs 小节「仍成立」)必须红,收口后必须绿。"""
    contradiction = (
        "| 3 | `rs_sync` 容差未按 fps 自适应(W6) | **清** | tests/test_v12.py |\n"
        "### v0.8.1(2026-09-13)\n"
        "- [ ] **W6 rs_sync 容差按 fps 自适应**(仍成立:OVERLAP_TOL_MS 仍为常量默认)\n")
    assert detect_conflicts(contradiction), "检测器漏报:互斥结论未被发现"
    fixed = (
        "| 3 | `rs_sync` 容差未按 fps 自适应(W6) | **清** | tests/test_v12.py |\n"
        "### v0.8.1(2026-09-13)\n"
        "- [x] **W6 rs_sync 容差按 fps 自适应**:已于 v0.19 落地(原「仍成立」过时,已收口)\n")
    assert not detect_conflicts(fixed), "检测器误报:已收口的台账被判冲突"


def test_detector_scopes_reused_ids_by_section():
    """跨轮复用同 ID 但语义不同(如 v5 批次 A 的 A4 ≠ v4 批次 A 的 A4)不误报。"""
    text = (
        "### v5 落地验收清单\n"
        "- [x] A4 `rs_align` 改调自带运行器\n"
        "### v4 落地验收清单\n"
        "- [ ] A4 粗剪保守性:retake 检出 ≥90%(需真实素材)\n")
    assert not detect_conflicts(text), "检测器误报:不同小节复用同 ID 被判冲突"


def test_detector_ignores_prose_mentions_and_milestones():
    """括号里的旁引((B3 已完成…))与 P0/M11/S7 这类记号不得当欠账 ID。"""
    line = "- [ ] B5 无改动重跑 `--from S3` 全程命中 ≤2s(B3 已完成,实测计时待真实素材)"
    line2 = "- [x] **批修**:跑 `M11` 与 `P0` 相关项,产物落 `S7`"
    assert detect_conflicts(line + "\n" + line2) == []
