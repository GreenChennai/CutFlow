"""CutFlow smoke 测试:pytest tests/ -q"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "cutflow" / "scripts"))

import rs_subtitle as rsub  # noqa: E402
import rs_ir  # noqa: E402


def test_breakline_short():
    assert rsub.break_line("你好世界", 16) == ["你好世界"]


def test_breakline_punct_priority():
    lines = rsub.break_line("今天天气很好,我们一起去公园散步然后吃午饭吧", 12)
    assert all(len(l) <= 12 for l in lines)
    assert any(l.endswith("很") or l.endswith("好,") or l.endswith(",") for l in lines)


def test_breakline_ascii_not_split():
    lines = rsub.break_line("这个版本是 v20260907 build123 号更新的内容", 10)
    assert any("v20260907" in l for l in lines), f"ASCII 词被切断:{lines}"
    assert any("build123" in l for l in lines), f"ASCII 词被切断:{lines}"


def test_events_and_srt_ass(tmp_path):
    events = rsub.build_events([{"start_s": 0.0, "end_s": 2.0, "text": "第一句台词"}], 16)
    rsub.write_srt(events, tmp_path / "a.srt")
    rsub.write_ass(events, tmp_path / "a.ass", "talkshow-bold", "9x16", "1080x1920")
    srt = (tmp_path / "a.srt").read_text(encoding="utf-8")
    assert "-->" in srt and "第一句台词" in srt
    ass = (tmp_path / "a.ass").read_text(encoding="utf-8")
    assert "PlayResX: 1080" in ass and "Dialogue: 0" in ass


def test_ir_validate_ok_and_bad(tmp_path):
    (tmp_path / "x.mp4").write_bytes(b"fake")
    ir = {"version": 1, "slug": "t", "fps": 30, "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"kind": "video", "clips": [{"src": "x.mp4", "startMs": 0, "durationMs": 1000}]}]}
    assert rs_ir.validate(ir, tmp_path) == []
    bad = dict(ir)
    bad["fps"] = 90
    errs = rs_ir.validate(bad, tmp_path)
    assert any("fps" in e for e in errs)


def test_project_schema_exists():
    schema = json.loads((REPO / "skills/cutflow/templates/project.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["version"]["const"] == 1
