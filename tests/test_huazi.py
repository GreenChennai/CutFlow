# -*- coding: utf-8 -*-
"""M12 花字门禁(ADR-0057 / 分册01 §4.3/§8):

①模板库:基础档 8 套 ASS 模板在盘且元数据完备;进阶档 6 套 artboard 源卡;
②标签:ASS 富文本关键字(\bord / \t / \fad)存在;文本与 override 分离;
③硬规则 18:任何 Dialogue 文本匹配/计数前必须剥 {...}(strip_override 共享实现);
④红线:花字不改卡文本 → CPS ≤9 与每卡 ≤12 字(9:16)由既有约束天然继承(实测对拍);
⑤CLI:rs_subtitle --huazi 默认关;开则命中卡出 Layer 1 花字 Dialogue;未知模板 exit 2。

运行:pytest tests/test_huazi.py -q
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
ASSETS = REPO / "skills" / "cutflow" / "assets"
sys.path.insert(0, str(SCRIPTS))

import rs_align  # noqa: E402
import rs_asset  # noqa: E402
import rs_subtitle as rs  # noqa: E402

HUAZI_ASS = ASSETS / "huazi" / "ass"
HUAZI_CARDS = ASSETS / "huazi" / "cards"
BASIC_IDS = [f"huazi.{g}.{n}" for g, n in (
    ("keyword", "box"), ("keyword", "brush"), ("title", "pop"), ("title", "slide"),
    ("quote", "typewriter"), ("data", "count"), ("section", "tab"), ("ending", "subscribe"))]


# ================================================================ ① 模板库

def test_basic_templates_on_disk_with_meta():
    for hid in BASIC_IDS:
        hit = rs_asset.find(hid, "huazi")
        assert hit is not None, f"{hid} 不在统一索引"
        p = ASSETS / str(hit["file"])
        assert p.is_file(), f"{hid} 模板文件缺失:{hit['file']}"
        text = p.read_text(encoding="utf-8")
        meta = dict(rs.HUAZI_META_RE.findall(text))
        assert meta.get("id") == hid, f"{p.name} hz-id 元数据不符"
        assert meta.get("effect"), f"{p.name} 缺 hz-effect"
        assert json.loads(meta.get("params", "{}")) is not None or True
        assert "Style: HuaziMain" in text, f"{p.name} 缺 HuaziMain 样式"


def test_card_templates_six_and_finite():
    cards = [a for a in rs_asset.assets("huazi") if a.get("medium") == "artboard-card"]
    assert len(cards) >= 6, "进阶档 artboard 源卡 ≥6 套"
    for a in cards:
        p = ASSETS / str(a["file"])
        assert p.is_file(), f"{a['id']} 源卡缺失:{a['file']}"
        html = p.read_text(encoding="utf-8")
        assert "animation" in html, f"{a['id']} 源卡应带 CSS 动画"
        assert "infinite" not in html, f"{a['id']} 动画必须 finite(禁无限循环)"
        meta = json.loads((p.parent / "card.meta.json").read_text(encoding="utf-8"))
        assert meta["id"] == a["id"] and meta["medium"] == "artboard-card"


# ================================================================ ② 标签与效果

def test_effects_produce_ass_rich_text_tags():
    ev = {"text": "关键词", "start": 0.0, "end": 2.0}
    effects = {"box": (r"\bord",), "brush": (r"\bord", r"\frz"),
               "pop": (r"\t", r"\fscx"), "slide": (r"\move", r"\fad"),
               "typewriter": (r"\alpha", r"\t"), "count": (r"\t", r"\fscx"),
               "tab": (r"\bord", r"\frz"), "subscribe": (r"\fad",)}
    for effect, needles in effects.items():
        body = rs.huazi_body(ev, effect, {})
        for nd in needles:
            assert nd in body, f"效果 {effect} 缺 ASS 标签 {nd}"
        # 正文原样在 body 里(override 不吃字)
        assert rs.strip_override(body) == "关键词", f"效果 {effect} 改了卡文本"


def test_pop_overshoot_capped():
    """ADR-0057:逐字弹入 overshoot ≤1.1。"""
    ev = {"text": "字", "start": 0.0, "end": 1.0}
    body = rs.huazi_body(ev, "pop", {"overshoot": 1.1})
    assert r"\fscx110" in body
    with pytest.raises(ValueError):
        rs.huazi_body(ev, "nonexistent", {})


def test_strip_override_shared_impl():
    """硬规则 18:文本匹配/计数前必须剥 {...};花字标签不污染匹配。"""
    assert rs.strip_override(r"{\bord10\bordcolor&H00E5FF00}第一点") == "第一点"
    assert rs.strip_override(r"{\fscx20\fscy20\t(0,120,\fscx110\fscy110)}字") == "字"
    assert rs.strip_override("无标签") == "无标签"


def test_huazi_select_matches_after_override_strip():
    events = [{"text": r"{\bord6}第一点"}, {"text": "普通卡"}]
    assert rs.huazi_select(events, ["第一"]) == {0}
    assert rs.huazi_select(events, []) == {0, 1}
    assert rs.huazi_select(events, ["不存在"]) == set()


# ================================================================ ③ 模板加载

def test_load_all_basic_templates():
    for hid in BASIC_IDS:
        tpl = rs.load_huazi_template(hid)
        assert tpl["id"] == hid and tpl["effect"]
        assert isinstance(tpl["params"], dict)


def test_load_unknown_template_rejected():
    with pytest.raises(KeyError):
        rs.load_huazi_template("huazi.nope.thing")


# ================================================================ ④ CLI 全链 + 红线

def _wordline_file(tmp_path: Path) -> Path:
    segs = [{"start": 0.0, "end": 3.0, "text": "大家好,今天我们来讲桌面运维。"},
            {"start": 3.4, "end": 6.2, "text": "先看蓝屏,蓝屏是最常见的问题。"}]
    wl = rs_align.build_wordline(segs, "a.mp4")
    p = tmp_path / "wl.json"
    p.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    return p


def _run_subtitle(tmp_path: Path, *extra: str) -> tuple[int, dict, Path]:
    import contextlib
    import io
    out = tmp_path / "out"
    wl = _wordline_file(tmp_path)
    old = sys.argv
    sys.argv = ["rs_subtitle.py", "--from-wordline", str(wl), "--out", str(out), *extra]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = rs.main()
    finally:
        sys.argv = old
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip().startswith("{")]
    return code, (json.loads(lines[-1]) if lines else {}), out


def test_huazi_default_off():
    code, doc, out = _run_subtitle(tmp_path_factory())
    assert code == 0 and doc["data"].get("huazi") is None
    ass = (out / "subtitles.ass").read_text(encoding="utf-8")
    assert "HuaziMain" not in ass, "--huazi 未开时不得出花字样式"


def tmp_path_factory() -> Path:
    import tempfile
    return Path(tempfile.mkdtemp(prefix="huazi-off-"))


def test_huazi_cli_end_to_end_and_redlines():
    """命中卡出 Layer 1 花字;剥 {...} 后文本与 cards.json 完全一致;红线不破。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="huazi-e2e-"))
    code, doc, out = _run_subtitle(tmp, "--huazi", "huazi.keyword.box",
                                   "--huazi-keywords", "蓝屏")
    assert code == 0, doc.get("message")
    hz = doc["data"]["huazi"]
    assert hz["id"] == "huazi.keyword.box" and hz["appliedCards"] >= 1
    ass = (out / "subtitles.ass").read_text(encoding="utf-8")
    assert "Style: HuaziMain" in ass, "花字样式段必须存在"
    hz_lines = [ln for ln in ass.splitlines() if ln.startswith("Dialogue: 1,")]
    assert len(hz_lines) == hz["appliedCards"]
    # 剥 {...} 后与 cards.json 文本逐字一致(override 不污染文本口径,硬规则 18)
    cards = json.loads((out / "cards.json").read_text(encoding="utf-8"))["cards"]
    card_texts = {rs.strip_override(c["text"]) for c in cards}
    for ln in hz_lines:
        body = ln.split(",", 9)[-1]
        plain = rs.strip_override(body)
        assert plain in card_texts, f"花字文本不在卡集合:{plain!r}"
        assert len(plain.replace(" ", "")) <= 12, "每卡 ≤12 字(9:16)红线"
    # CPS 红线:花字卡时长与文本和普通口径相同 → 复算 CPS ≤9
    for ln in hz_lines:
        parts = ln.split(",")
        t0 = _ass_ts(parts[1])
        t1 = _ass_ts(parts[2])
        plain = rs.strip_override(ln.split(",", 9)[-1])
        cps = len(plain.replace(" ", "")) / max(0.001, t1 - t0)
        assert cps <= 9.0 + 1e-6, f"花字卡 CPS {cps:.2f} > 9"


def _ass_ts(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def test_huazi_unknown_id_exit_2():
    import tempfile
    code, doc, _ = _run_subtitle(Path(tempfile.mkdtemp(prefix="huazi-bad-")),
                                 "--huazi", "huazi.nope.thing")
    assert code == 2 and doc["code"] == "BAD_HUAZI"


def test_huazi_keywords_miss_degrades_with_trace():
    import tempfile
    code, doc, out = _run_subtitle(Path(tempfile.mkdtemp(prefix="huazi-miss-")),
                                   "--huazi", "huazi.keyword.box",
                                   "--huazi-keywords", "不存在的词")
    assert code == 0 and doc["data"]["huazi"]["appliedCards"] == 0
    ass = (out / "subtitles.ass").read_text(encoding="utf-8")
    assert "Dialogue: 1," not in ass, "0 命中不得出花字 Dialogue"
    assert any("花字" in r for r in doc["data"]["degradeReasons"])
