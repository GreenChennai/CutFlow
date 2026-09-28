# -*- coding: utf-8 -*-
"""第一册 T1.3 门禁:数值单一化 + 入口命令 ↔ 能力目录对拍。

T1.3  五类口径(-14 LUFS / 0.83s / maxChars / 40ms / 粗剪 margin 150·300ms)在文档里
      只允许「一处文档出处(anchor)+ 白名单证据件」;白名单外出现即红。
      白名单 = 本文件 WHITELIST 常量(集中管理、逐条带理由);另一逃生口是
      文件头标注 `<!-- single-source-ignore -->`(整文件豁免,须在 PR 说明理由)。
      机器真相源不入扫:templates/platforms.json、segmentation.MAX_CHARS、
      rs_render.LOUDNESS_TARGET、rs_sync.MEDIAN_MAX、rs_cut.MARGIN_IN/OUT_MS。

T1.1  SKILL.md 出现的每条 rs_* 命令,其脚本必须已在 capabilities.json 能力目录登记
      (入口不得引用未编目的能力)。

扫描范围:skills/cutflow/SKILL.md、README.md、skills/cutflow/rules/**/*.md、docs/ 顶层 *.md。
历史证据件(BASELINE/CHANGELOG/ITERATION-GUIDE/capability-matrix/BACKLOG)不改写历史,
故按文件级白名单放行——理由见 WHITELIST 注释。

运行:pytest tests/test_doc_single_source.py -q
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "cutflow" / "SKILL.md"
README = REPO / "README.md"
RULES = REPO / "skills" / "cutflow" / "rules"
DOCS = REPO / "docs"
CAPABILITIES = REPO / "skills" / "cutflow" / "capabilities.json"

IGNORE_MARKER = "<!-- single-source-ignore -->"


def _scan_files() -> list[Path]:
    files = [SKILL, README, *sorted(DOCS.glob("*.md"))]
    files += sorted(RULES.glob("*.md")) + sorted((RULES / "video-types").glob("*.md"))
    return [f for f in files if f.is_file()]


# ---------------------------------------------------------------- 五类口径:模式 / 唯一文档出处 / 白名单

# 每类口径:正则 + anchor(唯一文档出处,必须命中)+ 白名单{相对路径: 理由}
PATTERNS: dict[str, dict] = {
    # 总线响度目标:文档出处 rules/compose.md;机器 rs_render.LOUDNESS_TARGET
    "loudness -14": {
        "regex": r"(?<![\d.])-14(?![\d])",
        "anchor": "skills/cutflow/rules/compose.md",
        "whitelist": {
            "docs/BASELINE-v0.20.md": "基线证据表:逐文件列旧位置(§3.2 重复口径表),不改写历史结论",
            "docs/CHANGELOG.md": "版本台账:历史记录不改写",
            "docs/ITERATION-GUIDE-v0.11.md": "数值出处索引:行业出处登记处(§8.2/§10),它就是 provenance 本体",
            "docs/capability-matrix.md": "双后端对拍实测数值(-14.02 vs -14.00 LUFS),证据性引用",
        },
    },
    # 单卡最短时长/必并线:文档出处 rules/subtitles.md;机器 rs_sync.DUR_MIN(=MIN_DUR_S)
    "min-dur 0.83": {
        "regex": r"(?<![\d.])0\.83(?![\d])",
        "anchor": "skills/cutflow/rules/subtitles.md",
        "whitelist": {
            "docs/BASELINE-v0.20.md": "基线证据表(§5.1 断句机制)",
            "docs/CHANGELOG.md": "版本台账:历史记录不改写",
            "skills/cutflow/rules/compose.md": "实测战报表(v0.6.0 JJAV2815):历史实测记录,只读不改写",
        },
    },
    # 每卡字数上限字段:文档出处 rules/platforms.md;机器 segmentation.MAX_CHARS + platforms.json
    "maxChars": {
        "regex": r"maxChars",
        "anchor": "skills/cutflow/rules/platforms.md",
        "whitelist": {
            "docs/BASELINE-v0.20.md": "基线证据表(§3.2 重复口径表与复现命令)",
            "docs/CHANGELOG.md": "版本台账:历史记录不改写",
            "skills/cutflow/rules/incremental.md": "rs_run paramKeys/pipeline.json 字段名的机械引用(缓存键语义),非数值复述",
        },
    },
    # 对齐偏移门禁:文档出处 rules/verify.md §2;机器 rs_sync.MEDIAN_MAX/P95_MAX
    "align 40ms": {
        "regex": r"(?<!\d)40\s*ms",
        "anchor": "skills/cutflow/rules/verify.md",
        "whitelist": {
            "docs/BASELINE-v0.20.md": "基线证据表(§3.2 重复口径表)",
            "docs/BACKLOG.md": "验收清单历史原文(v4 批次 A2 验收线),台账不改写",
            "docs/CHANGELOG.md": "版本台账:历史记录不改写",
            "docs/ITERATION-GUIDE-v0.11.md": "数值出处索引:EBU R37 告警阈的出处登记(§10),provenance 本体",
            "docs/capability-matrix.md": "M6 字幕对拍实测(偏移中位 0.0ms ≤40ms),证据性引用",
        },
    },
    # 粗剪 margin 前后留白:文档出处 rules/roughcut.md §8.5;机器 rs_cut.MARGIN_IN_MS/MARGIN_OUT_MS
    "margin 150/300": {
        # 150 与 300 相邻出现(≤12 字符)才视为同一口径;单独的 150ms/300ms(转场溶解等)不算
        "regex": r"(?<!\d)150(?!\d)[^\n]{0,12}(?<!\d)300(?!\d)",
        "anchor": "skills/cutflow/rules/roughcut.md",
        "whitelist": {
            "docs/BASELINE-v0.20.md": "基线证据表(§3.2 重复口径表)",
            "docs/CHANGELOG.md": "版本台账:历史记录不改写",
        },
    },
}


def test_values_have_single_doc_source():
    """白名单外出现任一口径数值即红;anchor 丢失该数值也红(防止唯一出处被删空)。

    围栏代码块(JSON/命令样例,如 `gainDb: -14` 是音量增益不是响度口径)不入扫——
    文档口径声明指散文;样例以机器文件为真相源。
    """
    problems: list[str] = []
    for name, spec in PATTERNS.items():
        rx = re.compile(spec["regex"])
        anchor_hit = False
        for f in _scan_files():
            rel = f.relative_to(REPO).as_posix()
            text = f.read_text(encoding="utf-8")
            if IGNORE_MARKER in text[:400]:
                continue  # 整文件豁免(须带理由的 PR)
            in_fence = False
            hits: list[int] = []
            for i, ln in enumerate(text.splitlines()):
                if ln.strip().startswith("```"):
                    in_fence = not in_fence
                    continue
                if in_fence or rx.search(ln):
                    if not in_fence and rx.search(ln):
                        hits.append(i + 1)
            if not hits:
                continue
            if rel == spec["anchor"]:
                anchor_hit = True
                continue
            if rel in spec["whitelist"]:
                continue
            problems.append(f"[{name}] {rel}:{hits[:5]} 白名单外出现(出处应为 {spec['anchor']})")
        if not anchor_hit:
            problems.append(f"[{name}] 唯一文档出处 {spec['anchor']} 未命中该口径(出处被删?)")
    assert not problems, "数值单一化违规:\n" + "\n".join(problems)


def test_whitelist_entries_exist():
    """白名单里的文件必须真实存在(防改名后白名单变死条目)。"""
    known = {f.relative_to(REPO).as_posix() for f in _scan_files()}
    for spec in PATTERNS.values():
        for rel in spec["whitelist"]:
            assert rel in known, f"白名单条目不存在:{rel}"


# ---------------------------------------------------------------- T1.1:SKILL 命令 ↔ 能力目录

def test_skill_commands_are_in_capabilities():
    """SKILL.md 出现的每条 rs_* 命令,脚本必须已在 capabilities.json 登记。"""
    caps = json.loads(CAPABILITIES.read_text(encoding="utf-8"))
    known = {t["script"] for t in caps["tools"]}
    text = SKILL.read_text(encoding="utf-8")
    # 行内反引号 span 与围栏块里的 rs_*.py 都算;「提及脚本名」同样要求已编目
    used = set(re.findall(r"rs_[a-z_]+\.py", text))
    missing = sorted(used - known)
    assert not missing, f"SKILL.md 引用了能力目录外的脚本:{missing}(先升格并 rs_caps.py generate)"


def test_skill_no_loose_command_tables():
    """T1.2:SKILL/README 不得再养手写命令速查表(单一真相源 = capabilities.json)。"""
    for f in (SKILL, README):
        text = f.read_text(encoding="utf-8")
        assert "命令速查" not in text, f"{f.name} 仍保留手写命令速查表(改查 capabilities.json / rs_caps.py search)"
        assert "capabilities.json" in text, f"{f.name} 未指向能力目录"
