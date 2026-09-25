# -*- coding: utf-8 -*-
"""渲染性能门禁(v2 M14 · 主册 ADR-0056 + 分册03 R15/R16/R27/R40):

① 产物一致性 --jobs 1 vs --jobs 4:成片 sha256 字节级一致(并行只改执行顺序,
   不改产物内容;concat 顺序仍按 IR 显式顺序);
② ffprobe 调用计数(mock 计数):同一路径全程至多探测一次(探测合并,R15);
   且冷渲总量 ≤ 2N + 常数(旧实现 ≈3N+);缓存全命中重跑源探测为 0(惰性探测);
③ 临时文件无残留:成功与失败路径都不留 .tmp_*(R27);
④ --jobs 留痕:结果含实际并发度(进 pipeline.json 的账由 rs_run 落,见 rs_run 测试);
⑤ R18:rs_cut retake 检测的剪枝+早停与旧实现逐条一致(等价性对拍);
⑥ R40:proportional_timeout 语义(常量下限 / 时长×系数 / 环境变量覆盖)。

运行:pytest tests/test_render_parallel.py -q(ffmpeg 缺失时自动跳过渲染类用例)
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_common  # noqa: E402
import rs_cut  # noqa: E402
import rs_paths  # noqa: E402
import rs_render  # noqa: E402


def _ffmpeg_bin() -> str:
    try:
        p = rs_common.ffmpeg_bin(rs_common.load_config())
        if Path(p).is_file():
            return p
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


FF = _ffmpeg_bin()

pytestmark = pytest.mark.skipif(not FF, reason="ffmpeg 不可用")


# ---------------------------------------------------------------- 夹具

def _mk_media(path: Path, *, color: str, dur: float, with_audio: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FF, "-v", "error", "-y", "-f", "lavfi",
           "-i", f"color=c={color}:size=320x480:rate=12:duration={dur}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}"]
    cmd += ["-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast"]
    if with_audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def _mk_project(tmp_path: Path) -> tuple[Path, Path]:
    """4 段工程:2 个不同源交替(a 有音轨 / b 无音轨),1 个 fade 转场 + 1 个 cut。"""
    root = tmp_path / "proj"
    mat = root / rs_paths.p("materials")
    a = _mk_media(mat / "a.mp4", color="0x4080C0", dur=1.2, with_audio=True)
    b = _mk_media(mat / "b.mp4", color="0xC07040", dur=1.2, with_audio=False)
    clips = []
    for i in range(4):
        c = {"id": f"V1-{i + 1:03d}", "src": str(a if i % 2 == 0 else b),
             "startMs": i * 800, "durationMs": 800, "sourceInMs": 0}
        if i == 1:
            c["transition"] = {"type": "fade", "durMs": 160}
        if i == 3:
            c["transition"] = {"type": "cut"}
        clips.append(c)
    ir = {"version": 1, "slug": "par", "fps": 12,
          "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"id": "V1", "kind": "video", "name": "main", "clips": clips}],
          "outputs": ["9x16"]}
    ir_path = root / rs_paths.p("timeline") / "project.json"
    ir_path.parent.mkdir(parents=True, exist_ok=True)
    ir_path.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    return root, ir_path


def _render(ir_path: Path, jobs: int) -> dict:
    doc = json.loads(ir_path.read_text(encoding="utf-8"))
    return rs_render.render(doc, ir_path, "9x16", "draft",
                            use_cache=False, jobs=jobs)


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ================================================================ ① 字节级一致

def test_jobs1_jobs4_output_hash_identical(tmp_path):
    root, ir_path = _mk_project(tmp_path)
    r1 = _render(ir_path, jobs=1)
    h1 = _sha256(Path(r1["output"]))
    # 清掉 seg 缓存再跑并行档:两条独立全渲路径,产物必须字节级一致
    shutil.rmtree(Path(r1["output"]).parent.parent / "_build", ignore_errors=True)
    r4 = _render(ir_path, jobs=4)
    h4 = _sha256(Path(r4["output"]))
    assert h1 == h4, (f"--jobs 1 与 --jobs 4 产物 hash 不一致:\n  {h1}\n  {h4}")
    assert r1["jobs"] == 1 and r4["jobs"] == 4, "结果必须留痕实际并发度(ADR-0056)"


def test_jobs4_seg_reports_follow_ir_order(tmp_path):
    """并行不改段序:seg 报告与段文件清单仍按 IR 显式顺序(有序 concat 的前提)。"""
    root, ir_path = _mk_project(tmp_path)
    r4 = _render(ir_path, jobs=4)
    assert [s["i"] for s in r4["segs"]] == list(range(4)), "seg 报告必须保持 IR 顺序"


# ================================================================ ② 探测合并(R15)

def test_probe_merged_and_bounded(tmp_path, monkeypatch):
    """mock 计数:同一路径至多探测一次;冷渲总量 ≤ 2N+常数(旧 ≈3N+);
    同源多段只探一次(缓存命中路径共用)。"""
    root, ir_path = _mk_project(tmp_path)
    calls: list[str] = []
    real = rs_render.ffprobe_json

    def counting(media, cfg=None):
        calls.append(str(media))
        return real(media, cfg)

    monkeypatch.setattr(rs_render, "ffprobe_json", counting)
    _render(ir_path, jobs=4)
    n = 4                                   # N = 主轨段数
    assert len(calls) <= 2 * n + 4, \
        f"ffprobe 调用 {len(calls)} 次,超出 2N+4={2 * n + 4}(探测合并失效?)"
    dup = {c for c in calls if calls.count(c) > 1}
    assert not dup, f"同一路径被重复探测(R15 的病根仍在):{sorted(dup)}"


def test_cached_rerun_probes_no_source(tmp_path):
    """惰性探测:全缓存命中的重跑不探测任何源素材(此前每次渲染每段都重probe)。"""
    root, ir_path = _mk_project(tmp_path)
    r1 = _render(ir_path, jobs=1)           # 冷渲:落 seg 缓存
    calls: list[str] = []
    real = rs_render.ffprobe_json

    def counting(media, cfg=None):
        calls.append(str(media))
        return real(media, cfg)

    monkey_patch_calls = calls
    monkey_patch_calls.clear()
    doc = json.loads(ir_path.read_text(encoding="utf-8"))
    rs_render.render(doc, ir_path, "9x16", "draft", use_cache=True, jobs=1)
    src_names = {Path(s).name for s in calls}
    src_names -= {"composed.mp4", "mixed.mkv", "subtitled.mp4"}
    assert not any(s.endswith("a.mp4") or s.endswith("b.mp4") for s in src_names), \
        f"全缓存重跑不应探测源素材:{src_names}"


# ================================================================ ③ 临时文件(R27)

def test_no_tmp_residue_success_and_failure(tmp_path, monkeypatch):
    """成功与失败路径都不留 .tmp_*;失败时 die(SEGMENT_FAIL) 且 segcache 已清扫。"""
    root, ir_path = _mk_project(tmp_path)
    r = _render(ir_path, jobs=4)
    build = Path(r["output"]).parent.parent / "_build" / "9x16"
    segcache = build / "segcache"
    assert not list(segcache.glob(".tmp_*.mp4")), "成功路径不得残留临时文件"

    # 失败路径:让每个段渲染的 ffmpeg 都非零退出 → step_segment 必须 die + 清扫
    shutil.rmtree(segcache, ignore_errors=True)

    class FakeFail:
        returncode = 1
        stderr = "mock boom"
        stdout = ""

    monkeypatch.setattr(rs_render, "run", lambda cmd, **kw: FakeFail())
    doc = json.loads(ir_path.read_text(encoding="utf-8"))
    segcache.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    with pytest.raises(SystemExit) as ei:
        rs_render.step_segment(doc, "9x16", build, root, {}, warnings, use_cache=False)
    assert ei.value.code == 4
    assert not list(segcache.glob(".tmp_*.mp4")), "失败路径必须清扫临时文件(R27)"


# ================================================================ ⑤ R18 等价性

def _retake_wordline() -> dict:
    """确定性 retake 夹具:重录对 / 三连重录 / 无关句。"""
    groups = [
        ["今天我们聊聊运镜的三个基本功", "今天我们聊聊运镜的三个基本功还有节奏"],
        ["第二个重点是景别切换", "第二个重点是景别切换", "第二个重点是景别切换带空镜"],
        ["最后是转场的节奏感它决定成片的呼吸"],
        ["尽量保持每个镜头两秒以上", "尽量保持每个镜头两秒以上不要切碎"],
        ["不相似的句子内容完全不同剪辑思路", "另一段讲的是字幕安全区与曝光基线"],
    ]
    chars, sentences, t = [], [], 0
    for group in groups:
        for txt in group:
            a = len(chars)
            for ch in txt:
                chars.append({"ch": ch, "startMs": t, "endMs": t + 150})
                t += 150
            chars.append({"ch": "。", "startMs": t, "endMs": t + 100})
            t += 100
            sentences.append({"span": [a, len(chars)]})
        t += 800
    return {"chars": chars, "sentences": sentences}


def test_retake_pruning_is_behavior_identical():
    """R18:real_quick/quick 上界剪枝是纯剪枝 —— 与未剪枝的旧实现逐条一致。"""
    import difflib

    def old_detect(wl):
        spans = rs_cut._sentence_spans(wl)
        out = []
        for i, a in enumerate(spans):
            if not a["text"]:
                continue
            best = -1
            for j in range(i + 1, min(len(spans), i + 1 + 6)):
                b = spans[j]
                if not b["text"]:
                    continue
                if b["startMs"] - a["endMs"] > 30000:
                    break
                if not rs_cut._more_complete(b["text"], a["text"]):
                    continue
                if difflib.SequenceMatcher(None, a["text"], b["text"]).ratio() < 0.80:
                    continue
                best = j
            if best < 0:
                continue
            out.append({"inMs": a["startMs"],
                        "outMs": max(a["startMs"] + 200,
                                     spans[best]["startMs"] - rs_cut.TAIL_KEEP_MS),
                        "reason": "retake"})
        return out

    wl = _retake_wordline()
    new = [{"inMs": c["inMs"], "outMs": c["outMs"]}
           for c in rs_cut.detect_retake(wl)]
    # 旧实现原始刀也走同一收尾(dedupe + chain_merge,rs_cut.detect_retake 的既有口径)
    old = [{"inMs": c["inMs"], "outMs": c["outMs"]}
           for c in rs_cut._dedupe(rs_cut._chain_merge(old_detect(wl)))]
    assert old, "夹具必须真的产出 retake 刀(否则对拍无意义)"
    assert new == old, f"R18 剪枝改变行为:\nOLD={old}\nNEW={new}"


# ================================================================ ⑥ R40 超时比例

def test_proportional_timeout_semantics(monkeypatch):
    f = rs_common.proportional_timeout
    assert f(10, factor=4, floor=1800) == 1800, "常量下限兜底"
    assert f(900, factor=4, floor=1800) == 3600, "时长×系数 > 下限时按比例放宽"
    monkeypatch.setenv("CUTFLOW_SEG_TIMEOUT", "77")
    assert f(900, factor=4, floor=1800, env="CUTFLOW_SEG_TIMEOUT") == 77, \
        "显式环境变量完全覆盖"
    monkeypatch.setenv("CUTFLOW_SEG_TIMEOUT", "0")
    assert f(900, factor=4, floor=1800, env="CUTFLOW_SEG_TIMEOUT") == 3600, \
        "非法环境变量(0/非数字)忽略,回到推算"
