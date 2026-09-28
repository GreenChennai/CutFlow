# -*- coding: utf-8 -*-
"""T2.6 / H6 回归:帧网格对齐前移 S7 生成期,盘面 subtitles.ass = 烧录 ASS(单一真相源)。

缺陷:rs_render.step_subtitle 烧录前把 ASS 按段平移另写 `_build/subtitled_aligned.ass`,
盘上 `06_成片输出/subtitles.ass` 保持名义时间 → S9 对账/交付 ASS 与画面两套时间基准
(长片 23 段累积 386ms)。
修复:rs_subtitle 生成期即按 IR 主轨各段「帧取整起点差」平移事件(rs_subtitle.
align_events_to_frame_grid);step_subtitle 直接烧盘面文件,`_shift_ass_for_frame_grid`
与 `subtitled_aligned.ass` 废止;S8 缓存键 k_sub = 盘面 ASS 内容 hash(烧什么就哈希什么)。

运行:pytest tests/test_ass_grid_single_source.py -q(全程离线,产物写 tempfile)
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_align  # noqa: E402
import rs_render  # noqa: E402
import rs_subtitle  # noqa: E402

FPS = 30
# 三段都非帧整数倍(30fps 帧长 33.33ms):1.237s / 0.989s / 1.503s
CLIPS = [{"src": "a.mp4", "durationMs": 1237},
         {"src": "b.mp4", "durationMs": 989},
         {"src": "c.mp4", "durationMs": 1503}]

ENTRIES = [
    {"start": 0.0, "end": 1.2, "text": "大家好,今天讲桌面运维第一课。"},
    {"start": 1.3, "end": 2.4, "text": "遇到蓝屏先不要慌张。"},
    {"start": 2.5, "end": 3.6, "text": "第一步检查内存条是否插牢。"},
]


def _mk_project(root: Path) -> Path:
    """构造段时长非帧整数倍的工程(IR 主视频轨 + wordline)。"""
    (root / "05_时间线工程").mkdir(parents=True, exist_ok=True)
    ir = {"version": 1, "slug": "t26", "fps": FPS, "ratio": "9x16",
          "tracks": [{"kind": "video", "clips": CLIPS},
                     {"kind": "audio", "clips": []}]}
    (root / "05_时间线工程" / "project.json").write_text(
        json.dumps(ir, ensure_ascii=False, indent=1), encoding="utf-8")
    wl = rs_align.build_wordline(ENTRIES, "a.mp4")
    (root / "05_时间线工程" / "wordline.json").write_text(
        json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    return root


def _run_subtitle(out: Path, wordline: Path) -> None:
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "rs_subtitle.py"),
         "--from-wordline", str(wordline), "--ratio", "9x16",
         "--out", str(out), "--fps", str(FPS)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO), timeout=120)
    assert r.returncode == 0, (r.stdout + r.stderr)[-600:]


_DIALOGUE = re.compile(r"^Dialogue:\s*[^,]*,([\d:.]+),([\d:.]+)", re.M)


def _ass2ms(ts: str) -> int:
    h, m, s = ts.split(":")
    return int(round((int(h) * 3600 + int(m) * 60 + float(s)) * 1000))


def _dialogue_ms(ass: Path) -> list[tuple[int, int]]:
    pairs = [(_ass2ms(a), _ass2ms(b))
             for a, b in _DIALOGUE.findall(ass.read_text(encoding="utf-8"))]
    assert pairs, f"ASS 无 Dialogue:{ass}"
    return pairs


def test_ass_on_disk_is_frame_grid_aligned(tmp_path):
    """盘面 subtitles.ass 的 Dialogue 时间 = 名义时间 + 所在段 delta(帧网格)。

    基准取**工程外同名生成**(无 IR → 名义时间,即旧盘面行为),两者只差
    delta(厘秒精度容差 ≤10ms);对齐前后必须有可见差异(证明 delta 非零)。"""
    root = _mk_project(tmp_path / "proj")
    bare = tmp_path / "bare"
    bare.mkdir()
    _run_subtitle(root / "06_成片输出", root / "05_时间线工程" / "wordline.json")
    _run_subtitle(bare, root / "05_时间线工程" / "wordline.json")
    aligned = _dialogue_ms(root / "06_成片输出" / "subtitles.ass")
    nominal = _dialogue_ms(bare / "subtitles.ass")
    assert len(aligned) == len(nominal) >= 2
    assert aligned != nominal, "工程内外生成结果相同,帧网格对齐未生效(delta=0?)"
    segs = rs_subtitle.frame_grid_deltas(CLIPS, FPS)
    assert any(d != 0 for _, _, d in segs), "测试段长恰为帧整数倍,判据失效"
    for (sa, ea), (sb, eb) in zip(aligned, nominal):
        # 每个时间戳按**各自所在段**的 delta 平移(与旧烧录期映射同式)
        ds = next((dd for a, b, dd in segs if a <= sb < b), segs[-1][2])
        de = next((dd for a, b, dd in segs if a <= eb < b), segs[-1][2])
        # ASS 厘秒精度 + 帧 snap:两条路径各做一次 centisecond 舍入,容差 10ms
        assert abs(sa - (sb + ds)) <= 10 and abs(ea - (eb + de)) <= 10, \
            f"卡时间 ≠ 名义+delta:aligned={sa},{ea} nominal={sb},{eb} ds={ds} de={de}"


def test_cards_json_same_source(tmp_path):
    """cards.json 的 finalTimes 与 ASS 同源(同一份 events,含帧网格对齐)。"""
    root = _mk_project(tmp_path / "proj")
    out = root / "06_成片输出"
    _run_subtitle(out, root / "05_时间线工程" / "wordline.json")
    cards = json.loads((out / "cards.json").read_text(encoding="utf-8"))["cards"]
    starts = {s for s, _ in _dialogue_ms(out / "subtitles.ass")}
    for c in cards:
        assert any(abs(s - c["startMs"]) <= 10 for s in starts), \
            f"cards.json 与 ASS 时间不同源:{c['startMs']} vs {sorted(starts)}"


# ---------------------------------------------------------------- 烧录侧(已废分叉)

def test_render_burns_on_disk_ass_directly(tmp_path, monkeypatch):
    """step_subtitle 直接烧盘面 ass:不再生成 subtitled_aligned.ass 第二份文件。"""
    root = _mk_project(tmp_path / "proj")
    out = root / "06_成片输出"
    _run_subtitle(out, root / "05_时间线工程" / "wordline.json")
    doc = {"fps": FPS, "_base_dir": str(root),
           "subtitle": {"ass": str(out / "subtitles.ass")}}
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(rs_render, "run", fake_run)
    monkeypatch.setattr(rs_render, "probe_duration_s", lambda *a, **k: 3.0)
    res = rs_render.step_subtitle(doc, tmp_path / "in.mp4", tmp_path, {})
    assert "subtitled_aligned" not in str(res), "烧录仍走 aligned 分叉"
    assert "subtitled_aligned" not in json.dumps(captured), "烧录命令引用 aligned 文件"
    vf = next((c for c in captured["cmd"] if c.startswith("ass=")), "")
    assert str(out / "subtitles.ass").replace("\\", "/").replace(":", "\\:") in vf, \
        f"烧录的 ass 不是盘面文件:{vf}"


def test_shift_function_removed():
    """旧烧录期平移函数已删(源头消灭分叉,不许残留第二套实现)。"""
    assert not hasattr(rs_render, "_shift_ass_for_frame_grid")
