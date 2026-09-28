# -*- coding: utf-8 -*-
"""第一册 T1.8/T1.9/T1.10 门禁:上下文预算 + AGENTS/CLAUDE 合一 + references 对拍。

T1.8   上下文预算门禁化:行数阈值从口号变成可断言——
         · SKILL.md ≤ 200 行(入口体量锁,原 270);
         · 任一 rules/**/*.md ≤ 150 行(册头 6 行内标注「超限理由」则放行);
         · SKILL.md + README.md 合计 ≤ 470 行;
       测量口径与「最小必读行数」记录在 docs/context-budget.md(本测试与文档同源更新)。
T1.9   AGENTS.md 是唯一真相源;CLAUDE.md 只许是一行引用——两文件内容 hash 相同即红。
T1.10  references/project-layout.md 的目录表 ↔ rs_paths.STAGE_DIRS 逐条对拍;
       references/ir-sample.json 必须能被 rs_ir.validate 接受(补齐占位素材后)。

运行:pytest tests/test_context_budget.py -q
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
SKILL = REPO / "skills" / "cutflow" / "SKILL.md"
README = REPO / "README.md"
RULES = REPO / "skills" / "cutflow" / "rules"
sys.path.insert(0, str(SCRIPTS))

import rs_paths  # noqa: E402

SKILL_MAX = 200          # 入口体量锁(第一册 T1.1;原锁 ≤270 见 test_v5)
BOOK_MAX = 150           # 分册体量锁;册头「超限理由」标注可超
SKILL_README_MAX = 470   # 入口 + README 合计(旧值 ≈566)
OVERFLOW_MARK = "超限理由"  # 册头标注,声明超限原因后放行


# ---------------------------------------------------------------- T1.8 行数预算

def _overflow_justified(text: str) -> bool:
    """册头(前 6 行)声明「超限理由」→ 放行。"""
    return any(OVERFLOW_MARK in ln for ln in text.splitlines()[:6])


def test_skill_md_within_budget():
    n = len(SKILL.read_text(encoding="utf-8").splitlines())
    assert n <= SKILL_MAX, f"SKILL.md {n} 行 > {SKILL_MAX}(入口只留路由与铁律,细节下沉分册)"


def test_rules_books_within_budget():
    books = sorted(RULES.glob("*.md")) + sorted((RULES / "video-types").glob("*.md"))
    problems = []
    for f in books:
        text = f.read_text(encoding="utf-8")
        n = len(text.splitlines())
        if n > BOOK_MAX and not _overflow_justified(text):
            problems.append(f"{f.relative_to(REPO).as_posix()} {n} 行 > {BOOK_MAX} 且册头无「{OVERFLOW_MARK}」标注")
    assert not problems, "分册超限未声明理由:\n" + "\n".join(problems)


def test_skill_plus_readme_within_budget():
    total = (len(SKILL.read_text(encoding="utf-8").splitlines())
             + len(README.read_text(encoding="utf-8").splitlines()))
    assert total <= SKILL_README_MAX, (
        f"SKILL.md+README.md = {total} 行 > {SKILL_README_MAX}(命令表/数值口径已改查单一真相源,勿再回填)")


def test_context_budget_doc_records_measurement():
    """测量口径必须真实写进 docs/context-budget.md(文档与门禁同源)。"""
    doc = (REPO / "docs" / "context-budget.md").read_text(encoding="utf-8")
    for needle in ("SKILL.md", "最小必读", "test_context_budget.py"):
        assert needle in doc, f"context-budget.md 缺测量记录:{needle}"


# ---------------------------------------------------------------- T1.9 AGENTS/CLAUDE 合一

def test_agents_claude_not_duplicated():
    agents = REPO / "AGENTS.md"
    claude = REPO / "CLAUDE.md"
    if not agents.is_file() or not claude.is_file():
        return  # 两文件都不在(如上游剥离)则无重复可言
    ha = hashlib.sha256(agents.read_bytes()).hexdigest()
    hc = hashlib.sha256(claude.read_bytes()).hexdigest()
    assert ha != hc, "AGENTS.md 与 CLAUDE.md 内容相同(第一册 T1.9:保留 AGENTS.md,另一份改一行引用)"
    assert "AGENTS.md" in claude.read_text(encoding="utf-8"), "CLAUDE.md 应是一行「见 AGENTS.md」引用"


# ---------------------------------------------------------------- T1.10 references 对拍

def test_project_layout_matches_rs_paths():
    """project-layout.md 的「逻辑键 | 目录」表必须与 rs_paths.STAGE_DIRS 逐条一致。"""
    doc = (REPO / "skills" / "cutflow" / "references" / "project-layout.md").read_text(encoding="utf-8")
    listed: dict[str, str] = {}
    for m in re_rows(doc):
        listed[m[0]] = m[1]
    expect = dict(rs_paths.STAGE_DIRS)
    assert listed == expect, (
        f"目录契约漂移:文档 {listed} vs rs_paths.STAGE_DIRS {expect}(改 rs_paths 后同步本表,反之亦然)")


def re_rows(doc: str) -> list[tuple[str, str]]:
    """解析 | 逻辑键 | `目录` | 用途 | 表格行。"""
    import re
    rows = []
    for ln in doc.splitlines():
        m = re.match(r"^\|\s*(\w+)\s*\|\s*`([^`]+)`\s*\|", ln)
        if m:
            rows.append((m.group(1), m.group(2)))
    return rows


def test_ir_sample_passes_rs_ir_validate(tmp_path):
    """ir-sample.json 与 rs_ir 实际校验器一致:补齐占位素材后 validate 必须通过。"""
    sample = REPO / "skills" / "cutflow" / "references" / "ir-sample.json"
    ir = json.loads(sample.read_text(encoding="utf-8"))
    # 收集样例引用的全部 src/ass/bgm 路径,在临时工程里补齐占位文件
    refs: list[str] = []
    for t in ir.get("tracks", []):
        refs += [c["src"] for c in t.get("clips", []) if c.get("src")]
    if ir.get("bgm", {}).get("src"):
        refs.append(ir["bgm"]["src"])
    if ir.get("subtitle", {}).get("ass"):
        refs.append(ir["subtitle"]["ass"])
    for rel in refs:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"placeholder")
    dst = tmp_path / ir.get("subtitle", {}).get("source", "05_时间线工程/wordline.json")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("{}", encoding="utf-8")
    proj = tmp_path / "project.json"
    shutil.copy(sample, proj)
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "rs_ir.py"), "validate", "project.json"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, f"references/ir-sample.json 未通过 rs_ir validate:\n{r.stdout}\n{r.stderr}"
