# -*- coding: utf-8 -*-
"""v2 M11 · R30 字段级契约对拍(R01 的整类根因:机械对拍只覆盖命令,不覆盖字段)。

对五份 schema 的高风险字段逐条登记「生产者 → 消费者」链,断言:
①消费者源码里能找到该字段的读取证据;
②登记表覆盖 schema 的全部顶层 clip 字段(新增字段必须进表,防再漏)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

SCHEMA = REPO / "skills" / "cutflow" / "templates" / "project.schema.json"

# 字段(clip 内路径) → 消费者文件列表(读取证据 = 源码含字段名字符串)
# markers 由 rs_common.normalize_markers 统一消费(R01 教训:禁止各脚本直读)
FIELD_CONSUMERS = {
    "src": ["rs_render.py"],
    "startMs": ["rs_render.py"],
    "durationMs": ["rs_render.py"],
    "sourceInMs": ["rs_render.py"],
    "speed": ["rs_render.py"],
    "volume": ["rs_render.py"],
    "role": ["rs_render.py", "rs_sfx.py"],
    "text": ["rs_render.py"],
    "position": ["rs_render.py"],
    "scale": ["rs_render.py"],
    "reframe": ["rs_render.py"],
    "motion": ["rs_render.py"],
    "transition": ["rs_render.py"],
    "overlay": ["rs_render.py", "rs_verify.py"],
    "opacity": ["rs_render.py"],
    "fade": ["rs_render.py"],
    "loop": ["rs_render.py"],
    "punchIn": ["rs_render.py"],
    "freezeMs": ["rs_render.py"],
    "matte": ["rs_render.py"],
}
TOP_CONSUMERS = {
    "markers": ["rs_common.py", "rs_sfx.py", "rs_meta.py"],
    "bgm": ["rs_render.py", "rs_ir.py"],
    "outputs": ["rs_brand.py", "rs_ingest.py"],
    "canvas": ["rs_render.py", "rs_brand.py"],
    "fps": ["rs_render.py"],
    "tracks": ["rs_render.py"],
}


def test_clip_fields_have_consumers():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    clip_props = schema["$defs"]["clip"]["properties"]
    missing = [k for k in clip_props if k not in FIELD_CONSUMERS]
    assert not missing, f"schema clip 新增字段未登记消费者链(R30/R01 类根因):{missing}"
    stale = [k for k in FIELD_CONSUMERS if k not in clip_props]
    assert not stale, f"字段契约表指向已删除的 clip 字段(清表):{stale}"
    for field, consumers in FIELD_CONSUMERS.items():
        for f in consumers:
            src = (SCRIPTS / f).read_text(encoding="utf-8")
            assert field in src, f"clip.{field} 声明的消费者 {f} 源码中无读取证据"


def test_top_fields_have_consumers():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    top = set(schema.get("properties", {}).keys())
    missing = [k for k in TOP_CONSUMERS if k not in top]
    assert not missing, f"顶层字段契约表指向不存在的 schema 字段(清表):{missing}"
    for field, consumers in TOP_CONSUMERS.items():
        for f in consumers:
            src = (SCRIPTS / f).read_text(encoding="utf-8")
            assert field in src, f"顶层 {field} 声明的消费者 {f} 源码中无读取证据"


def test_markers_read_only_via_normalize():
    """R01 防复发:rs_sfx / rs_meta 禁止直读 markers 字段(必须走 normalize_markers)。"""
    for f in ("rs_sfx.py", "rs_meta.py"):
        src = (SCRIPTS / f).read_text(encoding="utf-8")
        assert 'ir.get("markers"' not in src or "normalize_markers" in src, \
            f"{f} 直读 markers 未走归一口(R01 类静默错位风险)"
    assert "def normalize_markers" in (SCRIPTS / "rs_common.py").read_text(encoding="utf-8")
