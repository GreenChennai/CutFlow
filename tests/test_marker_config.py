"""T5.3c 门禁:pytest marker 注册纪律(补第零册欠账的防回归锁)。

历史教训:tests/ 长期没有 pytest 配置,`pytest.mark.perf` 一直带
PytestUnknownMarkWarning 跑;e2e 层落地时若 marker 没注册,`pytest -m e2e`
会静默匹配不到任何用例(default 干跑)。本文件把两件事钉死:
  1. perf / e2e 必须在 pytest.ini 的 markers 里注册(消 UnknownMarkWarning);
  2. e2e 必须默认排除(addopts `-m "not e2e"`,CI 只跑单元 + 工程层);
  3. tests/e2e/ 下每个测试文件必须显式携带 e2e 标记 —— 新增 e2e 文件忘了
     打标记,就会混进默认全量,拖垮 CI。

运行:pytest tests/test_marker_config.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INI = REPO / "pytest.ini"
E2E_DIR = Path(__file__).resolve().parent / "e2e"


def test_perf_and_e2e_markers_registered():
    """perf / e2e 必须已注册(否则 pytest 报 UnknownMarkWarning = 配置欠账回潮)。"""
    text = INI.read_text(encoding="utf-8")
    for name in ("perf", "e2e"):
        assert re.search(rf"(?m)^\s*{name}\s*:", text), f"marker {name} 未注册"


def test_e2e_excluded_by_default():
    """addopts 必须默认排除 e2e(CI 只跑单元 + 工程层;`pytest -m e2e` 显式覆盖)。"""
    text = INI.read_text(encoding="utf-8")
    assert re.search(r'(?m)^\s*addopts\s*=.*-m\s+"not e2e"', text), \
        "pytest.ini addopts 缺少 -m \"not e2e\",e2e 会混进默认全量"


def test_every_e2e_file_carries_marker():
    """tests/e2e/ 下每个 test_*.py 都必须显式携带 pytest.mark.e2e。"""
    files = sorted(E2E_DIR.glob("test_*.py"))
    assert files, "e2e 目录为空(至少应有冒烟用例)"
    for f in files:
        assert "pytest.mark.e2e" in f.read_text(encoding="utf-8"), \
            f"{f.name} 缺 e2e 标记 —— 会混进默认全量套件"
