# -*- coding: utf-8 -*-
"""C 组(T2.16c)回归:artboard 清单相对路径的解析基准(独立素材仓模式)。

缺陷(assetC 16x9 真实工程回填实测):manifest 的 project/output 在「artboard
目录在工程根下」时写工程根相对、独立素材仓时写目录相对(_item_prefix 的两态
契约),但 --export / export-fallback / --apply 的读出口一律按 `--root`(cwd)
解析 → 独立仓 + cwd≠工程根 时:主引擎 EXPORT_OK 假成功(产物落进 cwd、
sourceHash 回写丢失)、export-fallback 拿 cwd 下的野产物假 EXPORT_SKIP、
--apply 产物存在性检查全数误判。

修复:_item_base(root, artboard_root) 与 _item_prefix 互逆(在工程根下 → 工程根;
独立仓 → 清单目录),main 的 export/export-fallback/apply 全部改走该基准。

运行:pytest tests/test_artboard_item_base.py -q(纯逻辑,离线)
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_artboard  # noqa: E402


def test_item_base_inverse_of_item_prefix():
    """读基准与写基准互逆:工程根下 → root;独立仓 → 清单目录。"""
    root = Path(r"C:\tmp\proj")
    under = root / "03_创作素材" / "artboard"
    standalone = Path(r"C:\tmp\other\artboard")
    assert rs_artboard._item_prefix(under, root) == "03_创作素材/artboard"
    assert rs_artboard._item_base(root, under) == root
    assert rs_artboard._item_prefix(standalone, root) == ""
    assert rs_artboard._item_base(root, standalone) == standalone


def test_item_base_does_not_absorb_unrelated_dirs(tmp_path):
    """清单目录不在工程根下时,解析不得落回 cwd/root(回归野产物判「在盘」)。"""
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "studio" / "artboard"
    outside.mkdir(parents=True)
    base = rs_artboard._item_base(root, outside)
    assert base == outside
    assert not (base / "c01" / "export" / "c01.png").exists()
    # 野产物落在 root 下时,不得被当成清单产物(root 不是解析基准)
    stray = root / "c01" / "export"
    stray.mkdir(parents=True)
    (stray / "c01.png").write_bytes(b"x")
    assert not (base / "c01" / "export" / "c01.png").exists()
