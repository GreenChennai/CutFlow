# -*- coding: utf-8 -*-
"""M12 字体单源门禁(分册01 §5 / ADR-0053 决策 4):

①templates/fonts.json 与 artboard fonts/README.md 逐键一致(目录集合双向对拍);
②字体家族名来自 artboard 代表款字体内部名(FreeType 实读),不靠猜;
③rs_subtitle 查表兜底:resolve_font 走 fonts.json;表缺失降级内置兜底 + WARN 留痕;
④硬编码字体名残留扫描:scripts/ 全部 .py 禁现 Microsoft YaHei / 微软雅黑(缺陷 D 家族);
⑤rs_doctor:config.artboard_dir 与工作区级锁定路径不一致必须报错(锁定路径缺席机器跳过)。

运行:pytest tests/test_fonts.py -q
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

import os as _os
_ARTBOARD = _os.environ.get('CUTFLOW_ARTBOARD_DIR',
                              json.load(open(Path(__file__).resolve().parents[1] / 'config.json', encoding='utf-8')).get('artboard_dir', '') if Path(__file__).resolve().parents[1].joinpath('config.json').is_file() else '')
_HAS_ARTBOARD = bool(_ARTBOARD) and Path(_ARTBOARD).is_dir()

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
FONTS_JSON = REPO / "skills" / "cutflow" / "templates" / "fonts.json"
sys.path.insert(0, str(SCRIPTS))

import rs_common  # noqa: E402
import rs_doctor  # noqa: E402
import rs_subtitle as rs  # noqa: E402

ARTBOARD_FONTS = rs_common.ARTBOARD_LOCKED_DIR / "fonts"

# 硬编码字体名黑名单(写入 ASS/CSS 的字体必须走查表;系统私有字体名禁现)
FONT_DENYLIST = ("Microsoft YaHei", "微软雅黑")


# ================================================================ ① 表 ↔ artboard 对拍

@pytest.mark.skipif(not _HAS_ARTBOARD, reason='artboard 技能目录缺席(CI),字体对表由本地/装 artboard 环境把守')
def test_fonts_json_matches_artboard_readme():
    assert FONTS_JSON.is_file(), "templates/fonts.json 缺失(--fonts-only 可再生成)"
    doc = json.loads(FONTS_JSON.read_text(encoding="utf-8"))
    readme = (ARTBOARD_FONTS / "README.md").read_text(encoding="utf-8")
    art_dirs = set(re.findall(r"\[([\w-]+)\][\w-]+/INTRO\.md", readme))
    table_dirs = {f["dir"] for f in doc["fonts"]}
    assert table_dirs == art_dirs, \
        f"fonts.json 与 artboard README 不一致:多 {table_dirs - art_dirs} 缺 {art_dirs - table_dirs}"
    assert doc.get("artboardLockedPath") == str(rs_common.ARTBOARD_LOCKED_DIR)
    assert doc.get("default", {}).get("subtitle") in table_dirs


def test_font_families_from_real_font_files():
    """family 字段来自代表款字体内部名;写了 file 就必须能对上盘面(或留待下载)。"""
    doc = json.loads(FONTS_JSON.read_text(encoding="utf-8"))
    with_family = [f for f in doc["fonts"] if f.get("family")]
    assert len(with_family) >= 20, "至少 20 款有真实家族名(artboard 代表款)"
    for f in with_family:
        assert f.get("file"), f"{f['dir']} 有 family 无 file"
        p = ARTBOARD_FONTS / f["dir"] / f["file"]
        if p.is_file():
            from PIL import ImageFont
            fam = ImageFont.truetype(str(p), 12).getname()[0]
            assert fam == f["family"], f"{f['dir']} 家族名与字体内部名不符:{fam} ≠ {f['family']}"


# ================================================================ ② 查表兜底

def test_resolve_font_uses_table_and_caches():
    family = rs.resolve_font()
    assert family and "/" not in family and "\\" not in family
    doc = json.loads(FONTS_JSON.read_text(encoding="utf-8"))
    want = next(f["family"] for f in doc["fonts"]
                if f["dir"] == doc["default"]["subtitle"])
    assert family == want, "resolve_font 必须查 fonts.json default.subtitle 的 family"


def test_resolve_font_degrades_without_table(monkeypatch):
    """回滚档(分册01 §9):fonts.json 删除 → 内置兜底 + WARN + 留痕。"""
    monkeypatch.setattr(rs, "FONTS_JSON", Path("Z:/nonexistent/fonts.json"))
    rs._font_state.update({"family": None, "degraded": False, "reason": ""})
    family = rs.resolve_font()
    assert family == rs.FONT_FALLBACK
    st = rs.font_state()
    assert st["degraded"] is True and "fontDegraded" in st["reason"] or "降级" in st["reason"]
    # 恢复缓存,避免污染其他用例
    rs._font_state.update({"family": None, "degraded": False, "reason": ""})
    rs.resolve_font()


def test_styles_use_font_sentinel_not_hardcode():
    for name, st in rs.STYLES.items():
        assert st["font"] is None, f"{name} 样式仍是硬编码字体(st.font 必须为 None 哨兵)"


# ================================================================ ③ 硬编码残留扫描

def test_no_hardcoded_font_names_in_scripts():
    offenders: list[str] = []
    for f in sorted(SCRIPTS.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        hits = sorted({w for w in FONT_DENYLIST if w in text})
        if hits:
            offenders.append(f"{f.relative_to(REPO)}: {hits}")
    assert not offenders, "scripts/ 不得硬编码系统字体名(走 templates/fonts.json 查表):" \
                          + ";".join(offenders)


def test_generated_huazi_templates_use_table_font():
    """花字模板 Style 里的字体名只作占位,写盘时被 resolve_font() 覆写(单源)。"""
    ass = (SCRIPTS.parent / "assets" / "huazi" / "ass")
    for f in sorted(ass.glob("*.ass")):
        text = f.read_text(encoding="utf-8")
        for w in FONT_DENYLIST:
            assert w not in text, f"{f.name} 出现硬编码字体名 {w}"


# ================================================================ ④ rs_doctor 锁定路径

@pytest.mark.skipif(not _HAS_ARTBOARD, reason='artboard 技能目录缺席(CI),字体对表由本地/装 artboard 环境把守')
def test_doctor_artboard_locked_path_check():
    from pathlib import Path as _P
    locked = str(rs_common.ARTBOARD_LOCKED_DIR)
    ok = rs_doctor._artboard_locked_check(locked)
    assert ok["ok"] is True and ok["fatal"] is False
    bad = rs_doctor._artboard_locked_check("C:/definitely/not/artboard")
    assert bad["ok"] is False and bad["fatal"] is True, \
        "config.artboard_dir 指向非锁定路径必须报错"
    skip = rs_doctor._artboard_locked_check(None)
    assert skip["ok"] is True and skip["fatal"] is False, "未配置走既有「已配置」判据,不误报"


def test_doctor_source_registers_locked_check():
    src = (SCRIPTS / "rs_doctor.py").read_text(encoding="utf-8")
    assert "_artboard_locked_check" in src, "doctor 未登记 artboard 锁定路径判据"
    assert str(rs_common.ARTBOARD_LOCKED_DIR) not in src or \
        "ARTBOARD_LOCKED_DIR" in src, "锁定路径应取 rs_common 常量,不在 doctor 里写字面量"
