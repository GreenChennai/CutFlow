# -*- coding: utf-8 -*-
"""v2 M11 · R01/R20 markers 全链路回归(R01 的根因正是本路径零测试)。

四方一致:schema `{ms,label}` ↔ rs_common.normalize_markers ↔ rs_sfx.from_markers
↔ rs_meta 章节。覆盖:
· schema 口径 markers → rs_sfx 章节音效落在 ms(此前被静默取 0 再吸附到首字);
· schema 口径 markers → rs_meta 不再 KeyError(此前写死 int(c["atMs"]));
· 旧 atMs 口径兼容归一;title 回退 label;
· auto_chapters(无 markers)路径不受影响。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_common  # noqa: E402
import rs_meta  # noqa: E402
import rs_paths  # noqa: E402
import rs_sfx  # noqa: E402


def _capture(fn, *args, **kw):
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip().startswith("{")]
    return code, (json.loads(lines[-1]) if lines else {})


def _capture_argv(argv):
    import contextlib, io, sys as _sys
    old = _sys.argv
    _sys.argv = argv
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = rs_meta.main()
    finally:
        _sys.argv = old
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip().startswith("{")]
    return code, (json.loads(lines[-1]) if lines else {})


def test_normalize_is_single_source():
    """归一口:ms 优先 / atMs 兜底 / title 回退 label / 非法 ms 记 0。"""
    out = rs_common.normalize_markers([
        {"ms": 1500, "label": "开场"},
        {"atMs": 99, "title": "旧口径"},
        {"label": "无时间"},
        {"ms": "bad", "label": "坏值"},
    ])
    assert out == [{"ms": 1500, "label": "开场"}, {"ms": 99, "label": "旧口径"},
                   {"ms": 0, "label": "无时间"}, {"ms": 0, "label": "坏值"}]
    assert rs_common.normalize_markers(None) == []


def test_sfx_schema_markers_land_on_ms():
    """schema 口径 {ms,label} → 章节音效落点 = ms(此前静默取 0)。"""
    ir = {"markers": [{"ms": 8000, "label": "第二章"}, {"ms": 16000, "label": "第三章"}]}
    placements = rs_sfx.from_markers(ir)
    assert [p["atMs"] for p in placements] == [8000, 16000]
    assert all(p["src"] == "assets_sfx:riser" and p["trigger"] == "chapter"
               for p in placements)
    assert placements[0]["note"] == "章节:第二章"


def test_sfx_legacy_atms_compat():
    """旧 atMs 口径仍可用(归一兼容,不破坏既有工程)。"""
    placements = rs_sfx.from_markers({"markers": [{"atMs": 4200, "label": "旧"}]})
    assert placements[0]["atMs"] == 4200


def test_meta_schema_markers_no_keyerror(tmp_path):
    """schema 口径 markers → rs_meta 正常产章节(此前 int(c["atMs"]) 直接崩)。"""
    wl = {"chars": [{"ch": c, "startMs": 200 * i, "endMs": 200 * i + 180, "i": i}
                    for i, c in enumerate("测试句子")] if False else []}
    # rs_meta 走 main 的 --markers JSON 文件入口,构造最小工程面
    root = tmp_path / "工程甲"
    (root / rs_paths.p("timeline")).mkdir(parents=True)
    chars = [{"ch": c, "startMs": 300 * i, "endMs": 300 * i + 280, "i": i}
             for i, c in enumerate("大家好今天讲三个要点")]
    wl = {"chars": chars, "sentences": [], "durationMs": len(chars) * 300}
    wpath = tmp_path / "wl.json"
    wpath.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    markers = {"markers": [{"ms": 1000, "label": "第一段"}, {"ms": 9000, "label": "第二段"}]}
    mpath = tmp_path / "markers.json"
    mpath.write_text(json.dumps(markers, ensure_ascii=False), encoding="utf-8")
    code, doc = _capture_argv([
        "rs_meta.py", "--wordline", str(wpath), "--markers", str(mpath),
        "--title", "测试主题", "--platform", "bili",
        "--out", str(root / rs_paths.p("output"))])
    assert code == 0, doc
    meta = json.loads((root / rs_paths.p("output") / "metadata.json")
                      .read_text(encoding="utf-8"))
    chapters = meta["platforms"]["bili"]["chapters"]
    assert [c["atMs"] for c in chapters] == [1000, 9000]
    assert chapters[0]["title"] == "第一段"


def test_meta_auto_chapters_unaffected(tmp_path):
    """无 markers → 自动切分路径照常(归一后单口径,不回归)。"""
    root = tmp_path / "工程乙"
    (root / rs_paths.p("output")).mkdir(parents=True)
    chars = [{"ch": c, "startMs": 300 * i, "endMs": 300 * i + 280, "i": i}
             for i, c in enumerate("大家好今天讲三个要点" * 3)]
    n = len(chars)
    wl = {"chars": chars, "durationMs": n * 300,
          "sentences": [{"text": "大家好", "span": [0, 3]},
                        {"text": "今天讲三个要点", "span": [3, n]}]}
    wpath = tmp_path / "wl.json"
    wpath.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    code, doc = _capture_argv([
        "rs_meta.py", "--wordline", str(wpath), "--title", "测试",
        "--platform", "bili", "--out", str(root / rs_paths.p("output"))])
    assert code == 0, doc
    meta = json.loads((root / rs_paths.p("output") / "metadata.json")
                      .read_text(encoding="utf-8"))
    ch = meta["platforms"]["bili"].get("chapters") or []
    assert ch and all("atMs" in c and "ts" in c for c in ch)
