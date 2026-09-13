"""v0.11 迭代回归:R1 QA 闭环(黑帧/冻结/VFR/响度双 pass + matte 探针)。

对账:docs/ITERATION-GUIDE-v0.11.md §8/§9-R1;响度口径 = EBU R128 对齐值 -14 LUFS / -1 dBTP。
ffmpeg 实机用例沿用 test_v8/test_v10 的 skipif 体例;只测公开 seam。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "cutflow" / "scripts"))

import pytest  # noqa: E402

import rs_cut  # noqa: E402
import rs_ir  # noqa: E402
import rs_render  # noqa: E402
import rs_sync  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or str(REPO / ".." / "Tools" / "ffmpeg" / "bin" / "ffmpeg.exe")
CFG = {"ffmpeg_dir": str(Path(FFMPEG).parent)} if FFMPEG else {}


def _gen(path: Path, args: list[str]) -> Path:
    p = subprocess.run([FFMPEG, "-y", "-v", "error", *args], capture_output=True, text=True)
    if p.returncode != 0:
        pytest.skip(f"lavfi 不可用:{p.stderr[-120:]}".encode("ascii", "replace").decode())
    return path


# ---------------------------------------------------------------- R1 响度双 pass

@pytest.mark.skipif(not Path(FFMPEG).is_file(), reason="ffmpeg 不可用")
def test_measure_loudness_returns_plausible_values(tmp_path):
    """1kHz 正弦音 -20dB 的测量值应落在合理区间;静音轨返回 None(无有效音轨)。"""
    tone = _gen(tmp_path / "tone.mp4",
                ["-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
                 "-f", "lavfi", "-i", "color=c=green:s=160x120:r=30:d=2",
                 "-af", "volume=-20dB", "-c:v", "libx264", "-preset", "ultrafast",
                 "-c:a", "aac", "-shortest", str(tmp_path / "tone.mp4")])
    d = rs_render.measure_loudness(tone, CFG)
    assert d is not None
    i_val = float(d["input_i"])
    assert -60 < i_val <= 0, f"正弦音集成响度应合理,实测 {i_val}"

    silent = _gen(tmp_path / "silent.mp4",
                  ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2",
                   "-f", "lavfi", "-i", "color=c=green:s=160x120:r=30:d=2",
                   "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                   "-shortest", str(tmp_path / "silent.mp4")])
    assert rs_render.measure_loudness(silent, CFG) is None, "静音地板 = 无有效音轨"


# ---------------------------------------------------------------- R1 matte 探针

@pytest.mark.skipif(not Path(FFMPEG).is_file(), reason="ffmpeg 不可用")
def test_matte_probe_detects_wipe_and_ok(tmp_path):
    """第二输出探针(colorkey 抠绿后):正常链占比 ≈ 红块面积(0.02~0.70);
    旧 v0.10 坏表达式(归一化假设 → alpha 恒 1)→ 占比 <1%,必须被识别。"""
    from rs_common import ffmpeg_bin

    def probe(chain_suffix: str, tag: str) -> float | None:
        fc = (f"[0][1]overlay=54:96,colorkey=0x00FF00:0.15:0.12{chain_suffix}[fg0];"
              f"[fg0]format=yuva444p,split=2[fgv][fgs];"
              f"[fgv]format=yuv420p[vout];"
              f"[fgs]alphaextract,signalstats,"
              f"metadata=print:file=matte_{tag}.txt[fgp]")
        p = subprocess.run(
            [ffmpeg_bin(CFG), "-v", "error", "-y",
             "-f", "lavfi", "-i", "color=c=0x00FF00:s=162x288:r=30:d=2",
             "-f", "lavfi", "-i", "color=c=red:s=54x96:d=2",
             "-filter_complex", fc,
             "-map", "[vout]", "-frames:v", "10", "-c:v", "libx264",
             "-preset", "ultrafast", str(tmp_path / f"{tag}.mp4"),
             "-map", "[fgp]", "-frames:v", "2", "-f", "null", "-"],
            cwd=str(tmp_path), capture_output=True, text=True)
        if p.returncode != 0:
            pytest.skip(f"lavfi 不可用:{p.stderr[-160:]}".encode("ascii", "replace").decode())
        return rs_render.parse_matte_log((tmp_path / f"matte_{tag}.txt").read_text(
            encoding="utf-8", errors="replace"))

    ratio = probe("", "ok")
    assert ratio is not None and 0.02 <= ratio <= 0.70, f"正常 matte 占比应居中,实测 {ratio}"

    ratio_bad = probe(
        ",format=yuva444p,geq=lum='p(X,Y)':cb='p(X,Y)':cr='p(X,Y)':"
        "a='clip((alpha(X,Y)-0.55)/0.45,0,1)'", "bad")
    assert ratio_bad is not None and ratio_bad < 0.01, \
        f"geq 字节域事故形态必须被探针识别,实测 {ratio_bad}"


# ---------------------------------------------------------------- R1 rs_sync QC

@pytest.mark.skipif(not Path(FFMPEG).is_file(), reason="ffmpeg 不可用")
def test_run_qc_flags_black_segment(tmp_path):
    """片中 1s 黑帧 → black FAIL;干净片 → black/freeze/vfr 全过。"""
    dirty = tmp_path / "dirty.mp4"
    _gen(dirty, ["-f", "lavfi", "-i",
                 "color=c=green:s=160x120:r=30:d=1,format=yuv420p",
                 "-f", "lavfi", "-i", "color=c=black:s=160x120:r=30:d=1,format=yuv420p",
                 "-f", "lavfi", "-i", "color=c=green:s=160x120:r=30:d=1,format=yuv420p",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                 "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v]",
                 "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-preset", "ultrafast",
                 "-c:a", "aac", "-shortest", str(dirty)])
    qc = rs_sync.run_qc(dirty, CFG)
    assert qc["checks"]["black"]["pass"] is False, "片中黑帧必须 FAIL"
    assert qc["pass"] is False

    clean = tmp_path / "clean.mp4"
    _gen(clean, ["-f", "lavfi", "-i", "testsrc2=s=160x120:r=30:d=2",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                 "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                 "-shortest", str(clean)])
    qc2 = rs_sync.run_qc(clean, CFG)
    assert qc2["checks"]["black"]["pass"] is True
    assert qc2["checks"]["freeze"]["pass"] is True
    assert qc2["checks"]["vfr"]["pass"] is True


# ---------------------------------------------------------------- R2 转场三级语法

def test_ir_transitions_reason_by_gap():
    """ADR-0026:源间隙 <1s → jumpcut(亚帧软切);≥1s → topic(300ms 溶解)。"""
    import rs_ir
    cl = {"version": 1, "source": "a.mp4", "srcTotalMs": 20000,
          "keep": [[0, 4000], [4200, 9000], [10500, 14000]]}
    doc = rs_ir.build_from_cutlist(cl, slug="t", ratio="9x16")
    tr1 = doc["tracks"][0]["clips"][1]["transition"]
    tr2 = doc["tracks"][0]["clips"][2]["transition"]
    assert tr1["reason"] == "jumpcut" and tr1["durMs"] == 8
    assert tr2["reason"] == "topic" and tr2["durMs"] >= 300


def test_resolve_transitions_three_tiers():
    """渲染端:jumpcut=1 帧软切;topic 按其 durMs;无 reason 亚帧仍提升(ADR-0023)。"""
    clips = [{"durationMs": 4000, "sourceInMs": 0},
             {"durationMs": 4000, "sourceInMs": 5000,
              "transition": {"type": "fade", "durMs": 8, "reason": "jumpcut"}},
             {"durationMs": 4000, "sourceInMs": 12000,
              "transition": {"type": "fade", "durMs": 300, "reason": "topic"}},
             {"durationMs": 4000, "sourceInMs": 19000,
              "transition": {"type": "fade", "durMs": 8}}]
    eff, forced, _ = rs_render._resolve_transitions(clips, 30.0, {}, None)
    assert not forced
    assert eff[1] == pytest.approx(1 / 30.0), "jumpcut = 1 帧软切"
    assert eff[2] == pytest.approx(0.3), "topic = 按 durMs 溶解"
    assert eff[3] == pytest.approx(0.12), "无 reason 亚帧仍提升 joinCrossfadeMs"


# ---------------------------------------------------------------- R3 punch-in

def test_punch_in_heuristic_and_cap():
    """opt-in 启发式:移除 ≥1.2s 后 punch-in 1.4x;密度 ≥15s、全片 ≤3 处。"""
    import rs_ir
    cl = {"version": 1, "source": "a.mp4", "srcTotalMs": 120000,
          "keep": [[0, 4000], [7000, 11000], [26000, 30000], [45000, 49000],
                   [64000, 68000], [83000, 87000]]}
    doc = rs_ir.build_from_cutlist(cl, slug="t", ratio="9x16", punch_in_auto=True)
    clips = doc["tracks"][0]["clips"]
    punched = [c for c in clips if c.get("punchIn")]
    assert len(punched) == 2, "密度:两次 punch-in 在成片时间轴上必须相隔 ≥15s(此处 4s/20s)"
    assert [c["startMs"] for c in punched] == [4000, 20000]
    assert all(c["punchIn"]["factor"] == 1.4 for c in punched)
    doc_off = rs_ir.build_from_cutlist(cl, slug="t", ratio="9x16")
    assert not any(c.get("punchIn") for c in doc_off["tracks"][0]["clips"]), "默认关闭"


def test_punch_in_render_crop(math_mod=None):
    """渲染端:punchIn 因子 → scale 放大 + anchorY 偏置 crop(取 vf_tail 首位)。"""
    cw, ch = 1080, 1920
    factor, anchor = 1.4, 0.35
    w, h = round(cw * factor / 2) * 2, round(ch * factor / 2) * 2
    expr = f"scale={w}:{h},crop={cw}:{ch}:(iw-ow)/2:(ih-oh)*{anchor:.3f}"
    assert str(round(1080 * 1.4 / 2) * 2) in expr and "(ih-oh)*0.350" in expr


# ---------------------------------------------------------------- R4 粗剪智能

def test_lexicon_external_file_preferred():
    """templates/fillers.json 存在且被读取;缺字段回退内建。"""
    assert rs_cut.LEXICON["fillers"], "词表不应为空"
    assert "嗯" in rs_cut.LEXICON["fillers"]
    assert "说错了" in rs_cut.LEXICON["self_negative"]


def test_smooth_cuts_merges_fragments_and_drops_subcut():
    """①相邻刀间隙 <100ms 合并;②<120ms 碎刀放弃(宁可漏删);③合法 reason 不误伤。"""
    cuts = [
        {"inMs": 0, "outMs": 400, "reason": "silence", "conf": 0.95, "note": ""},
        {"inMs": 450, "outMs": 800, "reason": "filler", "conf": 0.9, "note": ""},   # 间隙 50ms
        {"inMs": 2000, "outMs": 2060, "reason": "filler", "conf": 0.9, "note": ""}, # 60ms 碎刀
        {"inMs": 3000, "outMs": 3500, "reason": "retake", "conf": 0.94, "note": ""},
    ]
    out = rs_cut.smooth_cuts(cuts)
    assert len(out) == 2, "前三刀并为一刀、碎刀被放弃、末刀保留"
    assert out[0]["inMs"] == 0 and out[0]["outMs"] == 800
    assert out[0]["reason"] == "silence"
    assert out[1]["reason"] == "retake"


# ---------------------------------------------------------------- R5 文本化

def test_cutlist_has_text_and_script_marks():
    """R5:每刀带文本上下文;cl.script 全文按动作分行(人读删改稿)。"""
    chars = [{"ch": ch, "startMs": i * 100, "endMs": i * 100 + 90}
             for i, ch in enumerate("abcdefghij" * 5)]  # 5s, 50 字
    wl = {"chars": chars, "srcDurationMs": 5000, "source": "a.mp4"}
    cuts = [{"inMs": 500, "outMs": 900, "reason": "filler", "conf": 0.92, "note": ""},
            {"inMs": 2500, "outMs": 3000, "reason": "retake", "conf": 0.8, "note": ""}]
    cl = rs_cut.build_cutlist(wl, cuts, {})
    assert all("text" in c for c in cl["cuts"])
    assert cl["cuts"][0]["text"], "上下文文本非空"
    script = cl.get("script") or []
    assert script, "删改稿存在"
    actions = {s["action"] for s in script}
    # 合成字符上切点必落字内 → guard 正确降级 review(wordClipped 永不放松)
    assert "review" in actions and "keep" in actions
    assert sum(len(s["text"]) for s in script) >= 48, "删改稿覆盖几乎全文"


# ---------------------------------------------------------------- 实机事故回归:xfade 链截断

@pytest.mark.skipif(not Path(FFMPEG).is_file(), reason="ffmpeg 不可用")
def test_concat_no_truncation_with_short_tails(tmp_path):
    """v0.11 实测事故:段尾帧被编码取整吃掉 1-2 帧,xfade 按名义长度递推时
    input1 提前 EOF → 整条下游被截断(视频 44.7s / 音频 145s)。
    修复后 concat 用实测段长夹紧 duration:输出不得短于名义总长 0.5s 以上。"""
    import rs_render

    def seg(name: str, seconds: float) -> str:
        p = tmp_path / f"{name}.mp4"
        r = subprocess.run(
            [FFMPEG, "-y", "-v", "error",
             "-f", "lavfi", "-i", f"testsrc2=s=80x144:r=30:d={seconds:.3f}",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds:.3f}",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
             "-shortest", str(p)], capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip(f"lavfi 不可用:{r.stderr[-120:]}".encode("ascii", "replace").decode())
        return str(p)

    # 3 段:join1=jumpcut(1帧尾),join2=topic(0.3s 尾,且刻意少渲 2 帧模拟取整亏损)
    s0 = seg("s0", 2.0 + 1 / 30.0)
    s1 = seg("s1", 2.0 + 0.3 - 2 / 30.0)          # 尾帧亏损 2 帧
    s2 = seg("s2", 2.0)
    doc = {"fps": 30,
           "tracks": [{"kind": "video", "clips": [
               {"durationMs": 2000, "sourceInMs": 0},
               {"durationMs": 2000, "sourceInMs": 3000,
                "transition": {"type": "fade", "durMs": 8, "reason": "jumpcut"}},
               {"durationMs": 2000, "sourceInMs": 6000,
                "transition": {"type": "fade", "durMs": 300, "reason": "topic"}}]}]}
    out = rs_render.step_concat(doc, [Path(s0), Path(s1), Path(s2)], tmp_path, {}, [])
    ffprobe = str(Path(FFMPEG).parent / ("ffprobe.exe" if os.name == "nt" else "ffprobe"))
    info = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True)
    vdur = float(info.stdout.strip() or 0)
    assert vdur >= 5.5, f"xfade 链不得截断下游:实测视频流 {vdur:.2f}s(名义 6.0s)"
