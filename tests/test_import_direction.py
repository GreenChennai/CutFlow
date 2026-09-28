"""T2.14 门禁:rs_* 脚本分层(引擎/机械臂/桥/工具)的 import 方向。

唯一真相源 = CONTEXT.md「rs_* 脚本分层表」(机器可读):本测试**解析该表**并
按它断言全仓 import 方向 —— 改分层先改表,测试自动跟随;表未收录的脚本即红。

方向规则( rank: 工具 0 < 机械臂 1 < 引擎 2 < 桥 3 ):
  · 只许 import rank ≤ 自身 层的模块;
  · 唯一例外:引擎 → 机械臂 的「数据表/判据复用」白名单(表内代码块逐条带理由);
  · 桥是端点:任何非桥脚本 import 桥即红;
  · 覆盖 scripts/ 全部 .py(含 rs_fx/ 子包与懒加载 import,AST 全量走查)。

运行:pytest tests/test_import_direction.py -q
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
CONTEXT_MD = REPO / "CONTEXT.md"

LAYERS = ("工具层", "机械臂层", "引擎层", "桥层")
RANK = {name: i for i, name in enumerate(LAYERS)}

# scripts/ 顶层模块名 → 表内登记名的别名(子包归并为一个登记条目)
ALIAS = {"rs_fx": "rs_fx"}

_INTRA = re.compile(r"^(rs_[a-z_]+|segmentation|textopt|rs_fx)$")


def _parse_context_layers() -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """解析 CONTEXT.md 分层表 → (脚本 → 层, {(引擎, 机械臂): 理由} 白名单)。"""
    text = CONTEXT_MD.read_text(encoding="utf-8")
    where: dict[str, str] = {}
    block = text.split("## rs_* 脚本分层表", 1)
    assert len(block) == 2, "CONTEXT.md 缺「rs_* 脚本分层表」段(T2.14 表被误删?)"
    body = block[1].split("## ", 1)[0]
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < 3 or cells[1] not in LAYERS:
            continue
        layer = cells[1]
        for name in cells[2].split(","):
            name = name.strip()
            if name and not name.startswith("**"):
                where[name] = layer
    # 白名单代码块:`rs_a → rs_b   # 理由`
    allow: dict[tuple[str, str], str] = {}
    for m in re.finditer(r"^\s*(rs_\w+)\s*→\s*(rs_\w+)\s*#\s*(.+)$", body, re.M):
        allow[(m.group(1), m.group(2))] = m.group(3).strip()
    return where, allow


def _module_key(mod: str) -> str | None:
    """import 的模块名 → 表内登记名;不在脚本体系内(标准库/第三方)→ None。"""
    base = mod.split(".")[0]
    base = ALIAS.get(base, base)
    return base if _INTRA.fullmatch(base) else None


def _iter_scripts() -> list[Path]:
    # 只扫脚本层本体;vendor/(pyJianYingDraft 等第三方)不在分层体系内
    return [p for p in sorted(SCRIPTS.rglob("*.py"))
            if "__pycache__" not in p.parts and "vendor" not in p.parts]


def _module_of(path: Path) -> str:
    rel = path.relative_to(SCRIPTS)
    if path.stem == "__init__":
        return path.parent.name            # rs_fx/__init__.py → rs_fx(子包归并)
    mod = str(rel.with_suffix("")).replace("\\", "/").replace("/", ".")
    if mod.startswith("rs_fx."):           # rs_fx.registry / rs_fx.t2_glsl → rs_fx
        return "rs_fx"                     # (子包整体按一个登记条目分层)
    return ALIAS.get(mod, mod)


def _intra_imports(path: Path) -> list[str]:
    """本文件 import 的脚本体系模块名(AST 全量,含函数内懒加载)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.append(node.module)
    return [m for m in (_module_key(x) for x in mods) if m]


# ---------------------------------------------------------------- 断言

def test_context_table_parses_and_covers_all_scripts():
    """分层表必须收录 scripts/ 下每一个 .py(零静默:新脚本不登记即红)。"""
    where, _ = _parse_context_layers()
    problems = []
    for path in _iter_scripts():
        mod = _module_of(path)
        key = ALIAS.get(mod, mod)
        if key not in where:
            problems.append(f"{mod} 未收录进 CONTEXT.md 分层表")
    assert not problems, "\n".join(problems)
    assert set(where) >= {"rs_run", "rs_common", "rs_paths", "rs_codes",
                          "segmentation", "textopt", "rs_fx", "rs_editor", "rs_edit"}


def test_table_has_all_four_layers():
    where, allow = _parse_context_layers()
    for layer in LAYERS:
        assert layer in where.values(), f"分层表缺 {layer}"
    assert len(allow) >= 6, "引擎→机械臂白名单条目异常减少(每条须带理由)"


def test_import_direction_follows_layers():
    """核心门禁:只许 import rank ≤ 自身的层;越级仅限 CONTEXT.md 白名单
    (引擎→机械臂 与 机械臂→引擎 两个白名族群,每条带理由)。"""
    where, allow = _parse_context_layers()
    problems: list[str] = []
    n_edges = 0
    for path in _iter_scripts():
        src_mod = ALIAS.get(_module_of(path), _module_of(path))
        src_rank = RANK[where[src_mod]]
        for mod in _intra_imports(path):
            n_edges += 1
            dst_rank = RANK[where[mod]]
            if dst_rank <= src_rank:
                continue
            if (src_mod, mod) in allow:
                continue
            problems.append(
                f"{src_mod}({where[src_mod]}) → {mod}({where[mod]}) 违反分层方向"
                "(越级复用须在 CONTEXT.md 白名单登记理由)")
    assert n_edges >= 60, f"import 边扫描量异常(仅 {n_edges}),扫描器失灵?"
    assert not problems, "分层方向违规:\n" + "\n".join(problems)


def test_no_one_imports_bridge_except_bridges():
    """桥是端点:rs_editor/rs_edit/rs_notes/rs_oplog/rs_gate/rs_jy_draft 只能被桥 import。"""
    where, _ = _parse_context_layers()
    bridges = {m for m, l in where.items() if l == "桥层"}
    offenders: list[str] = []
    for path in _iter_scripts():
        src_mod = ALIAS.get(_module_of(path), _module_of(path))
        if src_mod in bridges:
            continue
        for mod in _intra_imports(path):
            if mod in bridges:
                offenders.append(f"{src_mod} → {mod}(桥层不得被外部 import)")
    assert not offenders, "\n".join(offenders)


def test_engine_arm_whitelist_matches_reality():
    """白名单不许虚挂也不许漏挂:登记的越级边(双向)必须真实存在;真实存在的
    越级边(引擎→机械臂 / 机械臂→引擎)必须全部登记。"""
    where, allow = _parse_context_layers()
    real: set[tuple[str, str]] = set()
    for path in _iter_scripts():
        src_mod = ALIAS.get(_module_of(path), _module_of(path))
        for mod in _intra_imports(path):
            pair = (where[src_mod], where[mod])
            if pair == ("引擎层", "机械臂层") or pair == ("机械臂层", "引擎层"):
                real.add((src_mod, mod))
    assert real == set(allow), (
        f"白名单与实际不符;多登记:{set(allow) - real},漏登记:{real - set(allow)}")
