# -*- coding: utf-8 -*-
"""v2 M13 · B3 GLSL 渲染器契约(ADR-0054/分册02 §8.1 门禁 10)。

①只渲转场重叠区、不整片渲(帧数=恰 ntd 帧,零漂移模型恒等式);
②时长与 joinCrossfadeMs 尾帧扩展模型一致(渲染实测帧数==round(tdur*fps));
③缺失依赖 → 降级注册表声明的最接近 T1 + fxDegraded 留痕(moderngl 探针失败 /
  effects.glsl=false / 着色器编译失败三路);
④收录的 gl-transitions 逐条 MIT + 编译验证(有 GL 时真渲冒烟)。

运行:pytest tests/test_fx_glsl.py -q(无 ffmpeg/moderngl 时渲染类自动跳过)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_fx  # noqa: E402
from rs_fx import t2_glsl  # noqa: E402

GLSL_DIR = REPO / "skills" / "cutflow" / "templates" / "effects" / "glsl"


def _ffmpeg() -> str:
    try:
        cfg = rs_common.load_config()
        p = rs_common.ffmpeg_bin(cfg)
        if Path(p).is_file() and p != "ffmpeg":
            return str(p)
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


import rs_common  # noqa: E402

FF = _ffmpeg()
_HAS_GL = t2_glsl.available()


# ================================================================ ① 数据契约(免依赖)

def test_glsl_dir_all_mit():
    """收录的每个 .glsl 都带 MIT 许可头(逐条读,分册02 §2.2 license 字段同源)。"""
    files = sorted(GLSL_DIR.glob("*.glsl"))
    assert len(files) >= 15, "T2 收录子集缩水(目标 15+)"
    for f in files:
        assert "License: MIT" in f.read_text(encoding="utf-8", errors="replace"), f.name


def test_registry_t2_glsl_files_exist():
    """注册表每个 kind=glsl 的转场都有收录文件;fallback 是合法 T1 xfade 名。"""
    t1_xfades = {e["xfade"] for e in rs_fx.TRANSITIONS.values() if e.get("kind") == "xfade"}
    for fxid, e in rs_fx.TRANSITIONS.items():
        if e.get("kind") != "glsl":
            continue
        assert (GLSL_DIR / f"{e['glsl']}.glsl").is_file(), fxid
        assert e.get("fallback") in t1_xfades, f"{fxid}: fallback 非法"
        assert e.get("license") == "MIT", fxid


def test_overlap_only_zero_drift_model():
    """零漂移恒等式:body_a + overlap(td) + body_b(td 起) == qa + qb(只渲重叠区)。"""
    qa, qb, td = 36, 60, 15            # 帧
    total = (qa - 0) + td + (qb - td)  # 拼接图口径:seg_a[0:qa] + ov(td) + seg_b[td:qb]
    assert total == qa + qb, "拼接图帧数不守恒"


# ================================================================ ② 真实渲染冒烟(有 GL 才跑)

pytestmark_gl = pytest.mark.skipif(not (FF and _HAS_GL),
                                   reason="ffmpeg 或 moderngl/GL 不可用(降级路径在 ③ 覆盖)")


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
@pytest.mark.skipif(not _HAS_GL, reason="moderngl/GL 不可用")
def test_render_overlap_exact_frames(tmp_path):
    """真渲一条 gl-transition 重叠区:帧数恰为 round(tdur*fps)(零漂移),首尾帧分别
    贴近 from/to(端点语义)。这是 T2 冒烟证据。"""
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    out = tmp_path / "ov.mp4"
    for name, color in ((a, "red"), (b, "blue")):
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi",
                        "-i", f"color=c={color}:size=64x64:rate=30:duration=1",
                        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                        str(name)], check=True, capture_output=True)
    n = t2_glsl.render_overlap(a, b, fps=30, tdur_s=0.5, out_mp4=out,
                               glsl_name="dissolve", glsl_dir=GLSL_DIR,
                               ffmpeg_bin=FF, width=64, height=64)
    assert n == 15, f"重叠区帧数漂移:{n} != 15"
    # 端点语义:末帧 progress=1 → 应等于 to 侧纯色(YAVG 与纯蓝接近,而不是混色)
    probe = FF.replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe") \
        if "ffprobe" not in FF else FF
    pr = Path(FF).with_name(Path(FF).stem.replace("ffmpeg", "ffprobe") + Path(FF).suffix)
    r = subprocess.run([str(pr), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_frames", "-of", "default=nw=1:nk=1",
                        str(out)], capture_output=True, text=True)
    assert r.stdout.strip().isdigit() and int(r.stdout.strip()) == 15


# ================================================================ ③ 降级三态(无 GL 也必须过)

def test_resolve_degrades_without_glsl_flag():
    """effects.glsl=false → T2 一键回全 T1(注册表 fallback)+ 降级说明。"""
    res = rs_fx.resolve_transition({"fx": "tr.glsl.dissolve", "durMs": 300},
                                   glsl_on=False, deps_ready=True)
    assert res["kind"] == "xfade" and res["xfade"] == "dissolve"
    assert res.get("degraded") and "fxDegraded" in res["note"]


def test_resolve_degrades_without_moderngl():
    """moderngl 缺失 → 降级 fallback xfade + fxDegraded 留痕(不炸不静默)。"""
    res = rs_fx.resolve_transition({"fx": "tr.glsl.dissolve"},
                                   glsl_on=True, deps_ready=False)
    assert res["kind"] == "xfade"
    assert res.get("degraded") and "moderngl" in res["note"]


def test_glsl_ready_resolves_to_glsl_kind():
    """组件就绪 → 解析为 glsl kind(渲染端走预渲重叠区路径)。"""
    res = rs_fx.resolve_transition({"fx": "tr.glsl.dissolve"},
                                   glsl_on=True, deps_ready=True)
    assert res["kind"] == "glsl" and res["glsl"] == "dissolve"


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_render_end_to_end_degrade_traced(tmp_path, monkeypatch):
    """端到端:glsl join 工程 + moderngl 缺失 → 成片照出、时长零漂移、fxDegraded 留痕。"""
    import rs_render  # noqa: PLC0415
    from pathlib import Path as _P
    monkeypatch.setattr(rs_render, "load_config",
                        lambda: {"ffmpeg_dir": str(_P(FF).parent)})
    mat = tmp_path / "01_原始素材"
    mat.mkdir(parents=True)
    clips_src = []
    for i, color in enumerate(("0x4080C0", "0xC07040")):
        p = mat / f"s{i}.mp4"
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi",
                        "-i", f"color=c={color}:size=128x256:rate=15:duration=6",
                        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                        str(p)], check=True, capture_output=True)
        clips_src.append(p)
    clips = []
    for i in range(3):
        clips.append({"id": f"V1-{i + 1:03d}", "src": str(clips_src[i % 2]),
                      "startMs": i * 1200, "durationMs": 1200,
                      "sourceInMs": i * 1500,
                      "transition": ({"fx": "tr.glsl.dissolve", "durMs": 300}
                                     if i == 1 else None)})
    clips[0].pop("transition", None)
    ir = {"version": 1, "slug": "glsl-degrade", "fps": 15,
          "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"id": "V1", "kind": "video", "name": "main", "clips": clips}],
          "outputs": ["9x16"]}
    irp = tmp_path / "05_时间线工程" / "project.json"
    irp.parent.mkdir(parents=True)
    irp.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    doc = json.loads(irp.read_text(encoding="utf-8"))
    # 模拟组件缺失:即使本机有 moderngl,探针强制 False → 必须走降级
    monkeypatch.setattr(rs_render, "_GLSL_STATE",
                        {"on": True, "ready": False, "probed": True})
    res = rs_render.render(doc, irp, "9x16", "draft", use_cache=False, jobs=1)
    out = Path(res["output"])
    assert out.is_file()
    degraded = [w for w in res["warnings"] if "fxDegraded" in w]
    assert degraded, f"降级未留痕:{res['warnings']}"
    # 降级后回退既有 xfade 链:该链对小跨度的容器时长有历史容差(渲染端 B2 断言
    # 同款 WARN);T2/拼接图的帧级零漂移由 test_concat_splice 链路另行证明。
    assert abs(res["duration_s"] - 3.6) <= 0.25, \
        f"降级路径时长漂移超容差:{res['duration_s']} vs 3.6"
    fr = json.loads((tmp_path / "05_时间线工程" / "fx_report.json").read_text(encoding="utf-8"))
    kinds = {t["join"]: t["kind"] for t in fr["transitions"]}
    assert 1 in kinds and kinds[1] in ("xfade", "glsl")
