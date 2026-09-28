r"""T2.4 / H4 回归:个人机器绝对路径(盘符字面量)不得进入共享代码。

缺陷:`rs_common.ARTBOARD_LOCKED_DIR` 与 `rs_pixabay.pixabay_key()` 第三探测
路径硬编码 `E:\平日资料\...` —— 他人机器/CI 上误报 fatal 或 key 静默取不到。
修复:统一改为 `config.json artboard_dir → 环境变量 CUTFLOW_ARTBOARD_DIR →
None(不 fatal)`;rs_gate 的 cutforge 缺省改并置兄弟目录。

门禁:全仓 `*.py` 扫描 `X:\` / `X:/` 盘符字面量(白名单:第三方 vendor、
tests/probes 一次性探针、本测试自身),白名单外命中即红。

运行:pytest tests/test_no_absolute_machine_path.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_common  # noqa: E402
import rs_pixabay  # noqa: E402

# 盘符字面量:E:\ / E:/ 与 C:\Users 两种形态(盘符大小写不敏感)。
# 负向环视排除「前一个字符是字母/数字」的形态(如正则里的 style:\s 是合法内容,
# 不是盘符)。
_DRIVE = re.compile(r"[Ee]:[\\/]")
_CUSERS = re.compile(r"[Cc]:[\\/]users", re.IGNORECASE)
_NOT_IDENT = re.compile(r"[A-Za-z0-9_]")

# 白名单(相对仓库根的目录前缀):第三方上游代码 / 本机一次性探针(不入门禁)
WHITELIST_DIRS = ("vendor", "tests/probes")
SELF = Path(__file__).resolve()
# 虚拟环境/第三方包目录(按部件匹配)
EXCLUDE_PARTS = {"__pycache__", ".git", "site-packages", "node_modules"}


def _is_whitelisted(p: Path) -> bool:
    rel = p.relative_to(REPO).as_posix()
    if any(rel == w or rel.startswith(w + "/") for w in WHITELIST_DIRS):
        return True
    parts = set(p.parts)
    if parts & EXCLUDE_PARTS or any(part.startswith(".venv") for part in p.parts):
        return True
    return p.resolve() == SELF


def _iter_py():
    for p in sorted(REPO.rglob("*.py")):
        if not _is_whitelisted(p):
            yield p


def test_no_drive_letter_literals_in_py():
    """全仓 *.py 无盘符字面量(白名单外)。"""
    hits: list[str] = []
    for p in _iter_py():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, ln in enumerate(text.splitlines(), 1):
            m = _DRIVE.search(ln) or _CUSERS.search(ln)
            if not m:
                continue
            prev = ln[m.start() - 1] if m.start() > 0 else ""
            if _NOT_IDENT.match(prev):        # 前面紧贴字母/数字 → 是标识符片段不是盘符
                continue
            hits.append(f"{p.relative_to(REPO)}:{lineno}: {ln.strip()[:100]}")
    assert not hits, "共享代码出现个人盘符字面量(T2.4/H4 门禁):\n" + "\n".join(hits[:20])


def test_artboard_dir_resolves_env_then_none(monkeypatch, tmp_path):
    """解析链:config 缺失时 env 生效;都缺席 → None(不 fatal)。"""
    monkeypatch.setenv("CUTFLOW_ARTBOARD_DIR", str(tmp_path / "artboard"))
    monkeypatch.setattr(rs_common, "CONFIG_PATH", tmp_path / "no-config.json")
    assert rs_common.artboard_dir() == tmp_path / "artboard"
    monkeypatch.delenv("CUTFLOW_ARTBOARD_DIR", raising=False)
    assert rs_common.artboard_dir() is None


def test_artboard_dir_prefers_config(monkeypatch, tmp_path):
    """config.json 有 artboard_dir 时优先于 env(用户锁定口径不变)。"""
    import json
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"artboard_dir": str(tmp_path / "from_cfg")}),
                   encoding="utf-8")
    monkeypatch.setenv("CUTFLOW_ARTBOARD_DIR", str(tmp_path / "from_env"))
    monkeypatch.setattr(rs_common, "CONFIG_PATH", cfg)
    assert rs_common.artboard_dir() == tmp_path / "from_cfg"


def test_pixabay_key_chain_survives_missing_artboard(monkeypatch):
    """key 探测链:artboard 目录缺席(解析 None)不再引用盘符字面量,静默跳过即可。"""
    monkeypatch.delenv("ARTBOARD_PIXABAY_KEY", raising=False)
    monkeypatch.setattr(rs_common, "artboard_dir", lambda: None)
    # 本仓 config.json 无 pixabay_key → 返回空串(不抛异常即通过)
    assert rs_pixabay.pixabay_key() == ""
