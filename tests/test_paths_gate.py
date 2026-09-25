# -*- coding: utf-8 -*-
"""路径契约门禁(ADR-0045 / ADR-0046):

①字面量扫描:skills/cutflow/scripts/ 下 .py 不得出现 9 个旧目录名字面量
  (白名单仅 rs_paths.py —— 唯一真相源本身);用 pathlib 遍历,不用 rg/grep
  (rg 在中文路径下有历史问题,HANDOFF-v0.10 记录在案)。
②STAGE_DIRS ↔ 真实磁盘一致性:临时夹具工程上 rs_paths.ensure 铺出的结构与
  常量表逐目录对拍(废弃目录绝不创建)。
③resolve() 兜底语义:旧结构工程 → 返回旧名且 WARN(每 工程×键 只告一次);
  新结构工程 → 新名、零告警。

运行:pytest tests/test_paths_gate.py -q
"""
from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_paths  # noqa: E402

OLD_NAMES = ["00_brief", "01_materials", "02_sensed", "03_assets", "04_cut",
             "04_ai_prompts", "05_ir", "06_output", "_state"]
# 词边界:前后不得是字母/数字/下划线(read_state 之类的标识符不算字面量命中)
LITERAL_RE = re.compile("(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, OLD_NAMES)) + ")"
                        "(?![A-Za-z0-9_])")


# ================================================================ ① 字面量扫描

def test_gate_no_stage_dir_literals_in_scripts():
    """scripts/ 全部 .py(含 vendor)零旧目录字面量;白名单仅 rs_paths.py。"""
    offenders: list[str] = []
    for f in sorted(SCRIPTS.rglob("*.py")):
        if "__pycache__" in f.parts or f.name == "rs_paths.py":
            continue
        hits = sorted({m.group(1) for m in LITERAL_RE.finditer(f.read_text(encoding="utf-8"))})
        if hits:
            offenders.append(f"{f.relative_to(REPO)}: {hits}")
    assert not offenders, "阶段目录字面量必须经 rs_paths 取用(ADR-0046):" + ";".join(offenders)


def test_gate_rs_paths_is_the_single_source():
    """唯一真相源自洽:别名表键值一一对应;9 个旧名全覆盖;新名必须真的改了名。"""
    assert set(rs_paths.LEGACY_ALIASES.values()) <= set(rs_paths.STAGE_DIRS)
    for old, key in rs_paths.LEGACY_ALIASES.items():
        assert rs_paths.p(key) != old, f"{old} 的中文名未变"
    assert set(OLD_NAMES) - {"04_ai_prompts", "02_sensed/frames"} == set(rs_paths.LEGACY_ALIASES)
    assert rs_paths.JIANYING_SUB == ("导出", "剪映59")


# ================================================================ ② 常量 ↔ 磁盘一致

def test_gate_stage_dirs_match_disk_structure(tmp_path):
    """ensure 铺出的磁盘结构 = STAGE_DIRS(成品/ 交付容器按需创建,ensure 不代建;
    RETIRED 绝不创建;旧名绝不出现)。"""
    root = tmp_path / "proj"
    rs_paths.ensure(root)
    created = {d.name for d in root.iterdir() if d.is_dir()}
    expected = set(rs_paths.STAGE_DIRS.values()) - {rs_paths.p("deliver")}
    assert created == expected, f"磁盘结构与契约不符:{created} != {expected}"
    for old in rs_paths.LEGACY_ALIASES:
        assert not (root / old).exists(), f"ensure 不得创建旧目录 {old}"
    for r in rs_paths.RETIRED:
        assert not (root / r).exists(), f"ensure 不得创建废弃目录 {r}"
    assert not (root / rs_paths.p("deliver")).exists(), "成品/ 是交付容器,按需创建"
    # 常用落点都在新结构下
    assert rs_paths.pipeline_json(root) == root / "05_时间线工程" / "pipeline.json"
    assert rs_paths.project_json(root) == root / "05_时间线工程" / "project.json"
    assert rs_paths.jianying_draft(root) == root / "05_时间线工程" / "导出" / "剪映59"
    assert rs_paths.verify_json(root) == root / "_内部状态" / "verify.json"


def test_gate_check_reports_mixed_and_retired(tmp_path):
    """rs_paths.check:新结构 ok;纯旧结构 ok(legacy 如实列出);新旧并存 → ok=False。"""
    root = tmp_path / "a"
    rs_paths.ensure(root)
    rep = rs_paths.check(root)
    assert rep["ok"] and not rep["legacy"] and not rep["mixed"]

    root2 = tmp_path / "b"
    for old in ("00_brief", "05_ir", "_state"):
        (root2 / old).mkdir(parents=True)
    rep2 = rs_paths.check(root2)
    assert rep2["ok"], "纯旧结构是合法过渡态"
    assert set(rep2["legacy"]) == {"00_brief", "05_ir", "_state"}

    root3 = tmp_path / "c"
    (root3 / "00_制作简报").mkdir(parents=True)
    (root3 / "00_brief").mkdir()          # 新旧并存 = 迁移事故
    rep3 = rs_paths.check(root3)
    assert not rep3["ok"] and rep3["mixed"] == ["00_brief"]


# ================================================================ ③ resolve 兜底语义

def test_gate_resolve_legacy_fallback_warns_once(tmp_path, recwarn):
    """旧结构工程:resolve 返回旧名 + LegacyPathWarning;同 (工程, 键) 只告一次。"""
    root = tmp_path / "legacy"
    (root / "05_ir").mkdir(parents=True)
    (root / "_state").mkdir()
    assert rs_paths.resolve(root, "timeline") == root / "05_ir"
    assert rs_paths.resolve_name(root, "state") == "_state"
    warned = [w for w in recwarn.list if issubclass(w.category, rs_paths.LegacyPathWarning)]
    assert len(warned) == 2, "两个键各告一次(recwarn 全程记录):" + repr([str(w.message) for w in warned])
    rs_paths.resolve(root, "timeline")
    rs_paths.resolve_name(root, "state")
    warned = [w for w in recwarn.list if issubclass(w.category, rs_paths.LegacyPathWarning)]
    assert len(warned) == 2, "告警必须去重(同 工程×键 只告一次)"
    # 旧名不存在的新键照常出新名(不告警)
    n_before = len([w for w in recwarn.list
                    if issubclass(w.category, rs_paths.LegacyPathWarning)])
    assert rs_paths.resolve(root, "output") == root / "06_成片输出"
    n_after = len([w for w in recwarn.list
                   if issubclass(w.category, rs_paths.LegacyPathWarning)])
    assert n_after == n_before


def test_gate_resolve_new_structure_no_warning(tmp_path, recwarn):
    """新结构工程:resolve 全出新名,零告警。"""
    root = tmp_path / "fresh"
    rs_paths.ensure(root)
    with warnings.catch_warnings():
        warnings.simplefilter("error", rs_paths.LegacyPathWarning)
        assert rs_paths.resolve(root, "timeline") == root / "05_时间线工程"
        assert rs_paths.resolve(root, "output") == root / "06_成片输出"
        assert rs_paths.resolve(root, "state") == root / "_内部状态"
        assert rs_paths.rel(root, "timeline", "wordline.json") == "05_时间线工程/wordline.json"
    assert not [w for w in recwarn.list if issubclass(w.category, rs_paths.LegacyPathWarning)]


def test_gate_legacy_project_end_to_end_read_path(tmp_path, recwarn):
    """向后兼容(ADR-0045 四问「旧工程怎么办」):旧结构夹具工程上,
    注册表/状态/pipeline 落点全部 resolve 到旧名 —— 不迁移也能继续跑。"""
    import rs_run
    root = tmp_path / "legacy"
    for old in ("00_brief", "01_materials", "04_cut", "05_ir", "06_output", "_state"):
        (root / old).mkdir(parents=True)
    (root / "01_materials" / "a.mp4").write_bytes(b"fake")
    (root / "05_ir" / "wordline.json").write_text("{}", encoding="utf-8")
    spec = rs_run.spec(root)
    s1 = next(s for s in spec if s["id"] == "S1")
    assert "05_ir/wordline.json" in s1["outputs"], "旧结构工程注册表必须兜底到旧名"
    s7 = next(s for s in spec if s["id"] == "S7")
    assert any(a == "06_output" for a in s7["cmd"]), "S7 --out 在旧结构工程上必须指向 06_output"
    rs_run.write_state(root, "S6", {"status": "done"})
    assert (root / "_state" / "S6.json").is_file(), "状态必须写进旧 _state(兜底)"
    assert (root / "05_ir" / "pipeline.json").is_file()
