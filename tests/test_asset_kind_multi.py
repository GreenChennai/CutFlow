# -*- coding: utf-8 -*-
"""T2.1 / H1 回归:`rs_asset --kind sfx,bgm` 必须等于两类之和(不再静默 0 条)。

缺陷:help 明示 `--kind` 支持逗号分隔,但 main() 只校验未拆分,`assets()` 按
`a.get("kind") == "sfx,bgm"` 精确比较 → 静默返回 0 条。
修复:main() 拆逗号成列表(非法 kind 报 BAD_KIND);assets() 签名放宽
`str | Iterable[str]`。

运行:pytest tests/test_asset_kind_multi.py -q
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_asset  # noqa: E402


def _manifest_counts() -> dict[str, int]:
    doc = rs_asset.load_manifest()
    assert doc is not None, "统一素材索引必须存在(仓库自带 manifest.json)"
    counts: dict[str, int] = {}
    for a in doc["assets"]:
        counts[a.get("kind")] = counts.get(a.get("kind"), 0) + 1
    return counts


# ---------------------------------------------------------------- 库函数层

def test_assets_accepts_iterable_kind():
    """assets(["sfx","bgm"]) 条数 == 两类各自条数之和(核心判据)。"""
    counts = _manifest_counts()
    both = rs_asset.assets(["sfx", "bgm"])
    assert len(both) == counts["sfx"] + counts["bgm"]
    assert {a["kind"] for a in both} == {"sfx", "bgm"}


def test_assets_single_and_multi_consistent():
    """多 kind 结果 == 分别单 kind 查询的并(id 不重不漏)。"""
    single = {k: {a["id"] for a in rs_asset.assets(k)} for k in ("sfx", "bgm")}
    multi = {a["id"] for a in rs_asset.assets(["sfx", "bgm"])}
    assert multi == single["sfx"] | single["bgm"]


def test_assets_empty_iterable_means_no_filter():
    """空列表语义 = 不过滤(与 None 一致),避免「传空列表静默 0 条」复发。"""
    assert len(rs_asset.assets([])) == len(rs_asset.assets(None)) == \
        len(rs_asset.load_manifest()["assets"])


# ---------------------------------------------------------------- CLI 层

def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "rs_asset.py"), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO), timeout=120)


def test_cli_list_multi_kind_counts_sum():
    """CLI `list --kind sfx,bgm --json` 条数 == 两类之和(缺陷复现场景转正)。"""
    counts = _manifest_counts()
    r = _cli("list", "--kind", "sfx,bgm", "--json")
    assert r.returncode == 0, r.stderr
    data = rs_common_json(r.stdout)
    items = data["data"]["assets"]
    assert len(items) == counts["sfx"] + counts["bgm"]
    assert {a["kind"] for a in items} == {"sfx", "bgm"}


def test_cli_list_bad_kind_structured_error():
    """非法 kind(含混在逗号串里)→ BAD_KIND 结构化错误,退出非零。"""
    for kind in ("nope", "sfx,nope"):
        r = _cli("list", "--kind", kind, "--json")
        assert r.returncode != 0, f"kind={kind} 应失败"
        data = rs_common_json(r.stdout)
        assert data["code"] == "BAD_KIND", data
        assert data["ok"] is False


def test_cli_search_multi_kind():
    """search 同样受益:--kind sfx,bgm 命中池 = 两类并集(search 走同一拆分口)。"""
    r = _cli("search", "提示音", "--kind", "sfx,bgm", "--json")
    assert r.returncode == 0, r.stderr
    data = rs_common_json(r.stdout)
    for a in data["data"]["assets"]:
        assert a["kind"] in ("sfx", "bgm")


def rs_common_json(stdout: str) -> dict:
    """CLI stdout 首行 JSON(emit 协议)。"""
    import json
    line = next(ln for ln in stdout.splitlines() if ln.strip().startswith("{"))
    return json.loads(line)


@pytest.mark.parametrize("raw,want", [
    (None, None), ("", None), ("sfx", ["sfx"]),
    (" sfx , bgm ", ["sfx", "bgm"]),
])
def test_split_kinds_parsing(raw, want):
    """_split_kinds 解析口径:空白容忍、空串 → None。"""
    assert rs_asset._split_kinds(raw) == want
