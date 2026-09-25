# -*- coding: utf-8 -*-
"""迁移工具回归(tools/migrate_paths.py,ADR-0045 §4.6):

旧结构夹具工程 → migrate → 新结构 + 内引用已改写 + 报告存在
→ 幂等重跑退出 0 → --rollback 完整复原(目录名与内引用都还原)。

运行:pytest tests/test_migrate.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATE = REPO / "tools" / "migrate_paths.py"

NEW = {"brief": "00_制作简报", "materials": "01_原始素材", "sensed": "02_转写与校对",
       "assets": "03_创作素材", "cut": "04_粗剪决策", "timeline": "05_时间线工程",
       "output": "06_成片输出", "state": "_内部状态"}
OLD = {"brief": "00_brief", "materials": "01_materials", "sensed": "02_sensed",
       "assets": "03_assets", "cut": "04_cut", "timeline": "05_ir",
       "output": "06_output", "state": "_state"}


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(MIGRATE), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


def _mk_legacy_project(root: Path) -> None:
    """旧结构夹具工程:全 8 个旧目录 + 废弃目录 + 内引用(JSON/ASS)。"""
    for d in OLD.values():
        (root / d).mkdir(parents=True)
    (root / "02_sensed" / "frames").mkdir()          # RETIRED:迁移即移入备份
    (root / "04_ai_prompts").mkdir()                 # RETIRED:迁移即移入备份
    (root / OLD["brief"] / "brief.md").write_text("# 旧结构工程\n", encoding="utf-8")
    (root / OLD["materials"] / "a.mp4").write_bytes(b"fake")
    (root / OLD["timeline"] / "project.json").write_text(json.dumps({
        "subtitle": {"ass": "06_output/subtitles.ass",
                     "source": "05_ir/wordline.json"},
        "tracks": [{"kind": "video", "clips": [{"src": "01_materials/a.mp4",
                                                "startMs": 0, "durationMs": 1000}]}],
    }, ensure_ascii=False), encoding="utf-8")
    (root / OLD["output"] / "subtitles.ass").write_text(
        "; source: 05_ir/wordline.json\nDialogue: 0\n", encoding="utf-8")
    (root / OLD["state"] / "S1.json").write_text("{}", encoding="utf-8")


def test_migrate_full_cycle(tmp_path):
    """迁移 → 新结构 + 内引用改写 + 报告/清单存在;幂等重跑退出 0;
    回滚 → 目录名与内引用完整还原。"""
    root = tmp_path / "proj"
    _mk_legacy_project(root)

    # ---- dry-run:只打印,不动盘
    r = _run(str(root), "--dry-run")
    assert r.returncode == 0 and "05_ir/" in r.stdout, r.stdout + r.stderr
    assert (root / OLD["timeline"]).is_dir() and not (root / NEW["timeline"]).is_dir()

    # ---- 迁移
    r = _run(str(root))
    assert r.returncode == 0, r.stdout + r.stderr
    # 新结构齐(含剪映落点子目录)
    for name in NEW.values():
        assert (root / name).is_dir(), f"缺新目录 {name}"
    assert (root / NEW["timeline"] / "导出" / "剪映59").is_dir()
    # 废弃目录不迁入新结构
    assert not (root / "04_ai_prompts").exists()
    assert not (root / "02_sensed" / "frames").exists()
    # 内引用已改写(JSON 与 ASS)
    pj = json.loads((root / NEW["timeline"] / "project.json").read_text(encoding="utf-8"))
    assert pj["subtitle"]["ass"] == f"{NEW['output']}/subtitles.ass"
    assert pj["subtitle"]["source"] == f"{NEW['timeline']}/wordline.json"
    assert pj["tracks"][0]["clips"][0]["src"] == f"{NEW['materials']}/a.mp4"
    ass = (root / NEW["output"] / "subtitles.ass").read_text(encoding="utf-8")
    assert f"{NEW['timeline']}/wordline.json" in ass and "05_ir" not in ass
    # 报告与机器清单存在;备份存在
    reports = sorted((root / NEW["state"]).glob("migrate-*.md"))
    manifests = sorted((root / NEW["state"]).glob("migrate-*.json"))
    assert reports and manifests
    man = json.loads(manifests[-1].read_text(encoding="utf-8"))
    assert man["kind"] == "cutflow-migrate" and len(man["renames"]) == 8
    assert (Path(man["backup"]) / "_manifest.json").is_file()
    # 备份在契约位置(_内部状态/backup/migrate-<ts>/;清单记的是迁移时旧名路径)
    physical = (root / NEW["state"]) / "backup" / Path(man["backup"]).name
    assert physical.is_dir() and (physical / "_manifest.json").is_file()
    # 语义字段不受内引用改写牵连(brief.md 无路径引用,原样保留)
    assert (root / NEW["brief"] / "brief.md").read_text(encoding="utf-8") == "# 旧结构工程\n"

    # ---- 幂等:重跑报「无需迁移」退出 0
    r2 = _run(str(root))
    assert r2.returncode == 0 and "无需迁移" in r2.stdout, r2.stdout + r2.stderr

    # ---- 回滚:目录名与内引用都还原
    r3 = _run(str(root), "--rollback")
    assert r3.returncode == 0, r3.stdout + r3.stderr
    for name in OLD.values():
        assert (root / name).is_dir(), f"回滚后缺旧目录 {name}"
    for name in NEW.values():
        assert not (root / name).exists(), f"回滚后新目录 {name} 应已还原为旧名"
    assert (root / "04_ai_prompts").is_dir(), "废弃目录应从备份还原"
    assert (root / "02_sensed" / "frames").is_dir()
    pj_old = json.loads((root / OLD["timeline"] / "project.json").read_text(encoding="utf-8"))
    assert pj_old["subtitle"]["ass"] == "06_output/subtitles.ass"
    assert pj_old["tracks"][0]["clips"][0]["src"] == "01_materials/a.mp4"
    ass_old = (root / OLD["output"] / "subtitles.ass").read_text(encoding="utf-8")
    assert "05_ir/wordline.json" in ass_old
    # 备份保留(回滚证据链)
    assert Path(man["backup"]).is_dir()


def test_migrate_idempotent_on_fresh_project(tmp_path):
    """全新工程(空目录/新结构)→ 无需迁移,退出 0,不动盘。"""
    root = tmp_path / "empty"
    root.mkdir()
    r = _run(str(root))
    assert r.returncode == 0 and "无需迁移" in r.stdout, r.stdout + r.stderr


def test_migrate_aborts_on_mixed_structure(tmp_path):
    """新旧同名目录并存 → 报错停,绝不合并。"""
    root = tmp_path / "mixed"
    (root / OLD["brief"]).mkdir(parents=True)
    (root / NEW["brief"]).mkdir()
    r = _run(str(root))
    assert r.returncode != 0 and "并存" in (r.stdout + r.stderr), r.stdout + r.stderr
    assert (root / OLD["brief"]).is_dir() and (root / NEW["brief"]).is_dir(), "并存时不得动盘"
