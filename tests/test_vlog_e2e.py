# -*- coding: utf-8 -*-
"""M8 第二波 vlog 类型端到端门禁(横转竖自动重构 + 方案 §5.5.2 验收清单机械化)。

覆盖面:
· rs_render 消费 reframe_plan 的渲染端单测(夹具工程在仓外 tmp_path):
    - 横屏源 → 9:16 输出:输出尺寸正确、crop 窗比=9:16、锚点偏移生效、方块不变形;
    - mode=track 多关键帧 → 渲染侧按时间线性插值平移;
    - violations 非空(REFRAME_CLIP_SUBJECT)→ 回退 static-center 并留痕;
    - 裁切窗比例错配 → REFRAME_RATIO 报错不拉伸(硬约束);
    - IR clip.reframe 手动锚点优先于 plan(人工改锚走 rs_edit);
· vlog 夹具工程端到端:8 条碎片(4s,横屏 16:9 / 竖屏 9:16 混杂,碎片内部 2s 处一张
  已知硬切)→ rs_shot detect(逐碎片与切点真值机械对拍)→ rs_reframe plan --ratio 9x16
  → rs_render → 成片逐条断言;
· §5.5.2 验收 8 条逐条机械断言;「镜头边界人工抽查 ≥90%」用 fixture 切点真值做机械等价
  (8/8 = 100%)。无人声 vlog 是常态:ASR/口播链路按既有 e2e 口径不进本夹具,
  字幕断言按「无人声 → 无字幕轨」验证。

几何真值:碎片 = 去饱和(hue=s=0)testsrc2 背景 + 60x60 纯红方块「主体」
(横屏横移 / 竖屏纵移,正弦往复,解析已知)。背景去饱和保证像素级红块检测
不受 testsrc2 彩色干扰;主体位置用 subject_rect() 解析可算。

运行:pytest tests/test_vlog_e2e.py -q(ffmpeg 不可用时整模块 SKIP)
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
BGM_DIR = REPO / "skills" / "cutflow" / "assets" / "bgm"
sys.path.insert(0, str(SCRIPTS))

import rs_paths  # noqa: E402
import rs_common  # noqa: E402
import rs_edit  # noqa: E402
import rs_render  # noqa: E402
import rs_shot  # noqa: E402
import rs_reframe  # noqa: E402


def _ffmpeg_bin() -> str:
    try:
        cfg = rs_common.load_config()
        p = rs_common.ffmpeg_bin(cfg)
        if Path(p).is_file():
            return p
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


FF = _ffmpeg_bin()
pytestmark = pytest.mark.skipif(not FF, reason="ffmpeg 不可用(合成素材必需)")

try:
    import numpy as np
    from PIL import Image
    HAS_IMGLIB = True
except ImportError:                                    # 像素抽样断言的依赖
    HAS_IMGLIB = False

pixel_only = pytest.mark.skipif(not HAS_IMGLIB, reason="像素抽样断言需要 numpy+PIL")

# ---------------------------------------------------------------- 夹具几何真值
FPS = 30
FRAG_S, FRAG_MS = 4.0, 4000        # 每条碎片 4s(方案 §5.5.2:碎片 4–8s)
CUT_S, CUT_MS = 2.0, 2000          # 碎片内部已知硬切点(第二段全画面反相)
N_FRAGS = 8                        # 8 条碎片(8–12 条下限,控测试时长)
LAND, PORT = (640, 360), (360, 640)  # 横屏 16:9 / 竖屏 9:16 混杂
SUBJ, AMP = 60, 40                 # 主体方块边长 / 正弦往复幅度(源像素)
CW, CH = 1080, 1920                # 9:16 目标画幅
MINI_MS = 2000                     # 渲染端单测的迷你源时长
CARD_W, CARD_H = 600, 600          # 结尾引导卡(overlay)
CTA_X, CTA_Y, CTA_MS = 240, 660, 3000
CARD_RGB = (20, 112, 240)          # 0x1470F0
BGM = BGM_DIR / "ambient_calm_90bpm.mp3"   # 曲库授权源(自产无版权)


def subject_rect(size: tuple[int, int], t_s: float) -> tuple[float, float, float, float]:
    """碎片内 t 时刻主体方块源像素 bbox(x0,y0,x1,y1):横屏横移 / 竖屏纵移,解析可算。"""
    w, h = size
    off = AMP * math.sin(2 * math.pi * t_s / FRAG_S)
    if w >= h:
        cx, cy = w / 2 + off, h / 2
    else:
        cx, cy = w / 2, h / 2 + off
    return (cx - SUBJ / 2, cy - SUBJ / 2, cx + SUBJ / 2, cy + SUBJ / 2)


def subject_span(size: tuple[int, int]) -> tuple[float, float, float, float]:
    """整条碎片内主体的极值包络(x0,x1,y0,y1)—— reframe 锚点不得切掉的硬边界。"""
    w, h = size
    if w >= h:
        return (w / 2 - AMP - SUBJ / 2, w / 2 + AMP + SUBJ / 2,
                h / 2 - SUBJ / 2, h / 2 + SUBJ / 2)
    return (w / 2 - SUBJ / 2, w / 2 + SUBJ / 2,
            h / 2 - AMP - SUBJ / 2, h / 2 + AMP + SUBJ / 2)


# ---------------------------------------------------------------- 合成与运行工具

def _run_ff(*args: str, timeout: int = 300) -> None:
    p = subprocess.run([FF, "-v", "error", "-y", *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    assert p.returncode == 0, f"ffmpeg 合成失败:{p.stderr[-400:]}"


def _run(script: str, *args: str, cwd: Path) -> dict:
    """跑 rs_*.py,断言 ok,返回最后一行 JSON(v7 e2e 同款)。"""
    p = subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=str(cwd),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=1800)
    out = (p.stdout or "").strip().splitlines()
    doc = json.loads(out[-1]) if out else {"ok": False, "code": "NO_OUTPUT",
                                           "message": p.stderr[-300:]}
    assert doc.get("ok"), f"{script} 失败:{doc.get('code')} {doc.get('message')}\n{p.stderr[-400:]}"
    return doc


def _synth_fragment(path: Path, size: tuple[int, int]) -> None:
    """vlog 碎片:去饱和 testsrc2 + 2s 处全画面反相硬切 + 正弦往复红色方块主体。"""
    w, h = size
    if w >= h:
        x_expr, y_expr = f"(W-w)/2+{AMP:g}*sin(2*PI*t/{FRAG_S:g})", "(H-h)/2"
    else:
        x_expr, y_expr = "(W-w)/2", f"(H-h)/2+{AMP:g}*sin(2*PI*t/{FRAG_S:g})"
    _run_ff(
        "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={FPS}:duration={CUT_S:g},hue=s=0",
        "-f", "lavfi", "-i",
        f"testsrc2=size={w}x{h}:rate={FPS}:duration={CUT_S:g},hue=s=0,negate",
        "-f", "lavfi", "-i",
        f"color=c=red:size={SUBJ}x{SUBJ}:rate={FPS}:duration={FRAG_S:g}",
        "-filter_complex",
        f"[0:v][1:v]concat=n=2:v=1[bg];[bg][2:v]overlay=x='{x_expr}':y='{y_expr}',"
        "format=yuv420p[out]",
        "-map", "[out]", "-t", f"{FRAG_S:g}", "-r", str(FPS),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", str(path))


def _synth_mini_source(path: Path, centered: bool = False) -> None:
    """渲染端单测迷你源:640x360(横屏)2s,去饱和背景 + 静止红色方块。"""
    x_expr = "(W-w)/2" if centered else "150"
    _run_ff(
        "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={FPS},hue=s=0",
        "-f", "lavfi", "-i", f"color=c=red:size={SUBJ}x{SUBJ}:rate={FPS}:duration=2",
        "-filter_complex",
        f"[0:v][1:v]overlay=x='{x_expr}':y='(H-h)/2',format=yuv420p[out]",
        "-map", "[out]", "-t", "2", "-r", str(FPS),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", str(path))


def _frame(video: Path, t: float, workdir: Path, tag: str):
    """抽帧 → RGB ndarray(像素抽样断言的输入)。"""
    png = workdir / f"frm_{tag}.png"
    p = subprocess.run([FF, "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
                        "-frames:v", "1", str(png)], capture_output=True, text=True)
    assert p.returncode == 0, f"抽帧失败 t={t}:{p.stderr[-200:]}"
    return np.asarray(Image.open(png).convert("RGB"), dtype=np.int16)


def _red_bbox(arr):
    """纯红主体掩码的 bbox(背景已去饱和,纯红掩码无歧义)。"""
    mask = (arr[..., 0] > 150) & (arr[..., 1] < 110) & (arr[..., 2] < 110)
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _is_card_blue(px) -> bool:
    r, g, b = int(px[0]), int(px[1]), int(px[2])
    return abs(r - CARD_RGB[0]) <= 25 and abs(g - CARD_RGB[1]) <= 25 \
        and abs(b - CARD_RGB[2]) <= 25


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ================================================================ §1 渲染端 reframe 消费(单测)

def _mini_project(tmp_path: Path, src: Path, *, centered_subject: bool = False,
                  clip_extra: dict | None = None, plan_entry: dict | None = None,
                  slug: str = "mini"):
    """最小工程:05_时间线工程/project.json 单 clip IR(+可选手写 reframe_plan)。"""
    root = tmp_path / slug
    (root / rs_paths.p("timeline")).mkdir(parents=True)
    (root / rs_paths.p("output")).mkdir(parents=True, exist_ok=True)
    clip = {"id": "c1", "src": str(src), "startMs": 0, "durationMs": MINI_MS,
            "sourceInMs": 0}
    if clip_extra:
        clip.update(clip_extra)
    doc = {"version": 1, "slug": slug, "fps": FPS,
           "canvas": {"width": CW, "height": CH},
           "tracks": [{"id": "V1", "kind": "video", "name": "main", "clips": [clip]}],
           "outputs": ["9x16"]}
    ir_path = root / rs_paths.p("timeline") / "project.json"
    ir_path.write_text(rs_edit.dump_json(doc), encoding="utf-8")
    if plan_entry is not None:
        plan = {"version": 1, "ratio": "9x16", "canvas": [CW, CH],
                "engine": plan_entry.get("mode", "static-center"), "clips": [plan_entry]}
        rs_reframe.reframe_path(root).write_text(
            json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc, ir_path, root


def _static_entry(src: Path, x0: float = 100.0, **over) -> dict:
    """static-center 条目(x0 为欲验证的横向窗位;窗 202.5x360 = 9:16)。"""
    entry = {"clipId": "c1", "src": str(src), "srcWidth": 640, "srcHeight": 360,
             "startMs": 0, "durationMs": MINI_MS, "mode": "static-center",
             "anchorX": x0 + 101.25, "anchorY": 180.0, "scale": 1.0,
             "cropWindow": {"x0": x0, "y0": 0.0, "w": 202.5, "h": 360.0},
             "trajectory": [{"tMs": 0, "anchorX": x0 + 101.25, "anchorY": 180.0,
                             "scale": 1.0}],
             "violations": []}
    entry.update(over)
    return entry


@pixel_only
def test_render_reframe_static_anchor_offset(tmp_path):
    """横屏源 → 9:16:输出尺寸正确、crop 窗比=9:16、锚点偏移生效、方块不变形。"""
    src = tmp_path / "land.mp4"
    _synth_mini_source(src)                       # 主体在源 x∈[150,210](偏左)
    doc, ir_path, root = _mini_project(tmp_path, src,
                                       plan_entry=_static_entry(src, x0=100.0))
    res = rs_render.render(doc, ir_path, "9x16", "draft", use_cache=False)
    out = Path(res["output"])
    assert not any("reframe" in w for w in res["warnings"]), res["warnings"]
    # 输出尺寸 = 9:16 画幅
    info = rs_common.ffprobe_json(out, rs_common.load_config())
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (vs["width"], vs["height"]) == (CW, CH)
    # 像素抽样:窗 x0=100(非居中 218.75)→ 方块左缘应落在 ~267 而非居中位置
    arr = _frame(out, 1.0, tmp_path, "static")
    bb = _red_bbox(arr)
    assert bb, "红块必须可见(锚点偏移后主体仍在窗内)"
    x0, y0, x1, y1 = bb
    w_px, h_px = x1 - x0 + 1, y1 - y0 + 1
    assert abs(w_px - h_px) <= 5, f"方块不得变形(宽 {w_px} vs 高 {h_px})"
    assert 305 <= w_px <= 340, f"方块宽应 ≈60*1080/202≈320,实测 {w_px}(不拉伸)"
    exp_left = (150 - 100) * CW / 202
    assert abs(x0 - exp_left) <= 12, f"锚点偏移生效:左缘 {x0} vs 期望 {exp_left:.0f}"


@pixel_only
def test_render_reframe_track_interpolation(tmp_path):
    """mode=track 多关键帧 → 渲染侧按时间线性插值平移(中间时刻落在两端点之间)。"""
    src = tmp_path / "land_track.mp4"
    _synth_mini_source(src, centered=True)        # 主体居中:x∈[290,350]
    # 轨迹:锚点 381.25 → 281.25(窗 x0:280 → 180,主体 290..350 全程在窗内)
    entry = _static_entry(src, x0=280.0, mode="track",
                          trajectory=[{"tMs": 0, "anchorX": 381.25, "anchorY": 180.0,
                                       "scale": 1.0},
                                      {"tMs": MINI_MS, "anchorX": 281.25, "anchorY": 180.0,
                                       "scale": 1.0}])
    doc, ir_path, _ = _mini_project(tmp_path, src, plan_entry=entry, slug="track")
    res = rs_render.render(doc, ir_path, "9x16", "draft", use_cache=False)
    out = Path(res["output"])
    assert not any("reframe" in w or "过期" in w for w in res["warnings"]), res["warnings"]

    def left_at(t: float, tag: str) -> float:
        bb = _red_bbox(_frame(out, t, tmp_path, tag))
        assert bb, f"t={t} 红块必须可见(轨迹窗不得切掉主体)"
        return float(bb[0])

    # 窗 x0(t) = 280 - 100*t/2;方块源 x=290 → 输出左缘 = (290-x0)*1080/202
    k = CW / 202
    e_025, e_100, e_175 = ((290 - 267.5) * k, (290 - 230.0) * k, (290 - 192.5) * k)
    a_025, a_100, a_175 = (left_at(0.25, "t025"), left_at(1.0, "t100"),
                           left_at(1.75, "t175"))
    assert abs(a_025 - e_025) <= 16, f"t=0.25 左缘 {a_025} vs 线性插值期望 {e_025:.0f}"
    assert abs(a_100 - e_100) <= 16, f"t=1.00 左缘 {a_100} vs 线性插值期望 {e_100:.0f}"
    assert abs(a_175 - e_175) <= 16, f"t=1.75 左缘 {a_175} vs 线性插值期望 {e_175:.0f}"
    assert a_025 < a_100 < a_175, "轨迹必须单调平移(插值而非阶跃)"


@pixel_only
def test_render_reframe_violations_fallback_static(tmp_path):
    """violations 非空(REFRAME_CLIP_SUBJECT)→ 回退 static-center 渲染 + 警告留痕。"""
    src = tmp_path / "land_v.mp4"
    _synth_mini_source(src, centered=True)        # 居中主体:static-center 窗内可见
    entry = _static_entry(src, x0=218.75, mode="track",
                          trajectory=[{"tMs": 0, "anchorX": 50.0, "anchorY": 180.0,
                                       "scale": 1.0},
                                      {"tMs": MINI_MS, "anchorX": 40.0, "anchorY": 180.0,
                                       "scale": 1.0}],
                          violations=[{"code": "REFRAME_CLIP_SUBJECT", "atMs": 0,
                                       "detail": "主体包围盒超出裁切窗"}])
    doc, ir_path, _ = _mini_project(tmp_path, src, plan_entry=entry, slug="viol")
    res = rs_render.render(doc, ir_path, "9x16", "draft", use_cache=False)
    assert any("violations 非空" in w and "static-center" in w for w in res["warnings"]), \
        res["warnings"]
    arr = _frame(Path(res["output"]), 1.0, tmp_path, "viol")
    bb = _red_bbox(arr)
    assert bb, "回退居中后主体必须完整可见"
    x0, y0, x1, y1 = bb
    center = (x0 + x1) / 2
    assert abs(center - CW / 2) <= 20, f"居中窗:主体应落在画面中线,实测 {center:.0f}"
    assert (x1 - x0 + 1) >= 300, "轨迹若被错误消费主体会被切出窗(左缘应远离边界)"


def test_render_reframe_ratio_mismatch_errors(tmp_path, capsys):
    """裁切窗比例 ≠ 目标画幅比 → REFRAME_RATIO 报错不拉伸(硬约束)。"""
    src = tmp_path / "land_r.mp4"
    _synth_mini_source(src)
    entry = _static_entry(src, cropWindow={"x0": 0.0, "y0": 0.0, "w": 360.0, "h": 360.0},
                          anchorX=180.0)
    doc, ir_path, _ = _mini_project(tmp_path, src, plan_entry=entry, slug="ratio")
    with pytest.raises(SystemExit) as ei:
        rs_render.render(doc, ir_path, "9x16", "draft", use_cache=False)
    assert ei.value.code == 4
    assert "REFRAME_RATIO" in capsys.readouterr().out


@pixel_only
def test_render_manual_reframe_overrides_plan(tmp_path):
    """IR clip.reframe 手动锚点优先:跳过 plan 走旧 cover_crop 档,渲染留痕。"""
    src = tmp_path / "land_m.mp4"
    _synth_mini_source(src, centered=True)
    doc, ir_path, _ = _mini_project(tmp_path, src,
                                    clip_extra={"reframe": {"anchorY": 0.1}},
                                    plan_entry=_static_entry(src, x0=100.0),
                                    slug="manual")
    res = rs_render.render(doc, ir_path, "9x16", "draft", use_cache=False)
    assert any("手动锚点生效" in w and "reframe_plan" in w for w in res["warnings"]), \
        res["warnings"]
    arr = _frame(Path(res["output"]), 1.0, tmp_path, "manual")
    bb = _red_bbox(arr)
    assert bb, "旧档 cover_crop(横向恒居中)主体必须可见"
    # 旧档窗源 x∈[218.75,421.25] → 主体左缘 ≈ (290-218.75)*1920/360 ≈ 380;
    # 若 plan 被错误消费(x0=100)左缘会落在 ≈1014。
    left = float(bb[0])
    assert abs(left - (290 - 218.75) * CH / 360) <= 16, \
        f"手动档应走 cover_crop(左缘≈380),实测 {left:.0f}(疑似 plan 未被跳过)"


# ================================================================ §2 vlog 夹具工程端到端

@pytest.fixture(scope="module")
def vlog_proj(tmp_path_factory):
    """vlog 夹具工程:8 碎片(横/竖混杂)→ detect → plan → 渲染,产物一次产出多处断言。"""
    root = tmp_path_factory.mktemp("vlog_e2e")
    rs_paths.ensure(root)
    frames_dir = root / rs_paths.p("state") / "probe_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    mat = root / rs_paths.p("materials")
    frags = []
    for i in range(N_FRAGS):
        size = LAND if i % 2 == 0 else PORT
        p = mat / f"a{i:02d}_{'land' if i % 2 == 0 else 'port'}.mp4"
        _synth_fragment(p, size)
        frags.append((p, size))
    (mat / "manifest.json").write_text(json.dumps({"version": 1, "items": [
        {"file": p.name, "probe": "ok", "width": s[0], "height": s[1],
         "durationMs": FRAG_MS, "hasAudio": False} for p, s in frags]},
        ensure_ascii=False), encoding="utf-8")

    total_ms = N_FRAGS * FRAG_MS
    clips = [{"id": f"V1-{i + 1:03d}", "src": str(p), "startMs": i * FRAG_MS,
              "durationMs": FRAG_MS, "sourceInMs": 0}
             for i, (p, _s) in enumerate(frags)]
    # 结尾引导卡(overlay 轨,纯色 0x1470F0,收尾 3s)
    card = root / rs_paths.p("assets") / "cta_card.png"
    _run_ff("-f", "lavfi", "-i", f"color=c=0x{(CARD_RGB[0] << 16) | (CARD_RGB[1] << 8) | CARD_RGB[2]:06x}"
            f":size={CARD_W}x{CARD_H}", "-frames:v", "1", str(card))
    cta = {"id": "V2-001", "src": str(card), "startMs": total_ms - CTA_MS,
           "durationMs": CTA_MS,
           "overlay": {"x": CTA_X, "y": CTA_Y, "w": CARD_W, "h": CARD_H}}
    ir = {"version": 1, "slug": "vlog-e2e", "fps": FPS,
          "canvas": {"width": CW, "height": CH},
          "tracks": [{"id": "V1", "kind": "video", "name": "main", "clips": clips},
                     {"id": "V2", "kind": "video", "name": "overlay", "clips": [cta]}],
          "outputs": ["9x16"],
          # BGM:vlog 档增益 -18dB + ducking(人声优先);无人声工程 ducking 仅配置留档
          "bgm": {"src": str(BGM), "gainDb": -18, "ducking": True}}
    ir_path = rs_paths.project_json(root)
    ir_path.write_text(rs_edit.dump_json(ir), encoding="utf-8")

    # S2 前置:rs_shot detect 逐碎片(shots.json 为单素材产物,逐次覆盖,真值=2000ms)
    detects = []
    for p, _s in frags:
        doc = _run("rs_shot.py", "detect", str(p), "--out", str(root), cwd=root)
        detects.append({"file": p.name, "data": doc["data"],
                        "onDisk": _read(rs_shot.shots_path(root))})
    # S3 IR 定稿后:rs_reframe plan
    _run("rs_reframe.py", "plan", str(root), "--ratio", "9x16", cwd=root)
    plan = _read(rs_reframe.reframe_path(root))
    # S8 渲染(draft 档真出片)
    render_doc = _run("rs_render.py", rs_paths.rel(root, "timeline", "project.json"),
                      "--ratio", "9x16", "--profile", "draft", cwd=root)
    return {"root": root, "ir": ir, "ir_path": ir_path, "plan": plan,
            "detects": detects, "render": render_doc["data"],
            "out": Path(render_doc["data"]["output"]), "card": card,
            "frames": frames_dir, "total_ms": total_ms}


def test_a_shots_json_and_boundary_agreement(vlog_proj):
    """验收① shots.json 存在;镜头边界一致率 ≥90% —— 用 fixture 切点真值机械等价(8/8)。"""
    root = vlog_proj["root"]
    sp = rs_shot.shots_path(root)
    assert sp.is_file(), "04_粗剪决策/shots.json 必须存在"
    last = _read(sp)
    assert {"shots", "transitions", "engine", "degraded"} <= set(last), last.keys()
    matched = 0
    for d in vlog_proj["detects"]:
        shots = d["onDisk"]["shots"]
        assert shots, f"{d['file']} 必须切出镜头"
        cuts = [s["startMs"] for s in shots[1:]]
        hit = [c for c in cuts if abs(c - CUT_MS) <= 300]   # 10fps 降采样粒度容差
        assert len(cuts) == 1 and hit, f"{d['file']} 真值切点 {CUT_MS}ms 未命中:{cuts}"
        assert shots[0]["startMs"] == 0 and shots[-1]["endMs"] >= FRAG_MS - 200
        assert len(shots) == 2 and all(s["durMs"] >= rs_shot.MIN_SHOT_MS for s in shots)
        assert [t["kind"] for t in d["onDisk"]["transitions"]] == ["cut"]
        matched += 1
    agreement = matched / len(vlog_proj["detects"])
    assert agreement >= 0.9, f"边界一致率 {agreement:.0%} < 90%"
    assert agreement == 1.0, "fixture 真值已知的场景切换点应 8/8 全命中"


def test_j_reframe_plan_schema(vlog_proj):
    """reframe_plan:9x16、主轨 8 clip 逐条在册、裁切窗比=9:16、窗在源内(渲染同口径);
    渲染端逐段报告确认 plan 被真实消费(8/8 段 static-center 档,而非静默回退旧档)。"""
    plan = vlog_proj["plan"]
    assert plan["ratio"] == "9x16" and plan["canvas"] == [CW, CH]
    assert plan["engine"] == "static-center" and plan["degraded"] is True, \
        "本环境 bytetrack 未部署,须为 static-center 降级档(如实留痕)"
    entries = {e["clipId"]: e for e in plan["clips"]}
    seg_tags = [r.get("reframe", "") for r in vlog_proj["render"]["segs"]]
    assert seg_tags == ["static-center"] * N_FRAGS, \
        f"主轨 8 段必须逐段消费 reframe_plan:{seg_tags}"
    for i in range(N_FRAGS):
        cid = f"V1-{i + 1:03d}"
        assert cid in entries, f"主轨 {cid} 缺 reframe 条目"
        e = entries[cid]
        win = e["cropWindow"]
        assert abs(win["w"] / win["h"] - 9 / 16) * max(win["w"], win["h"]) \
            <= rs_render.REFRAME_RATIO_EPS, f"{cid} 窗比必须=9:16(裁切不拉伸)"
        assert win["x0"] >= 0 and win["y0"] >= 0
        assert win["x0"] + win["w"] <= e["srcWidth"] + 1e-6
        assert win["y0"] + win["h"] <= e["srcHeight"] + 1e-6
        assert e["durationMs"] == FRAG_MS and e["violations"] == []


@pixel_only
def test_b_no_stretch_landscape_to_portrait(vlog_proj):
    """验收②(前半)横屏素材进竖屏工程无拉伸:输出 1080x1920,已知几何方块不变形。"""
    out, frames = vlog_proj["out"], vlog_proj["frames"]
    info = rs_common.ffprobe_json(out, rs_common.load_config())
    vs = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (vs["width"], vs["height"]) == (CW, CH), "成片必须是 9:16 1080x1920"
    # 横屏碎片(clip0,全局 0–4s):源 640x360 → 居中窗 202x360 → 方块 60px ≈ 320px 方形
    bb = _red_bbox(_frame(out, 1.0, frames, "e2e_land"))
    assert bb, "横屏碎片主体必须可见"
    w_px, h_px = bb[2] - bb[0] + 1, bb[3] - bb[1] + 1
    assert abs(w_px - h_px) <= 5, f"方块不得变形(宽 {w_px} vs 高 {h_px})"
    assert 305 <= w_px <= 340, f"横屏方块宽应≈320(=60*1080/202),实测 {w_px}"
    # 竖屏碎片(clip1,全局 4–8s):源即 9:16,窗=全幅,缩放 3x → 方块 ≈180px 方形
    bb2 = _red_bbox(_frame(out, FRAG_S + 1.0, frames, "e2e_port"))
    assert bb2, "竖屏碎片主体必须可见"
    w2, h2 = bb2[2] - bb2[0] + 1, bb2[3] - bb2[1] + 1
    assert abs(w2 - h2) <= 5, f"竖屏方块不得变形(宽 {w2} vs 高 {h2})"
    assert 170 <= w2 <= 192, f"竖屏方块宽应≈180(=60*1080/360),实测 {w2}"


@pixel_only
def test_c_anchor_keeps_subject(vlog_proj):
    """验收②(后半)reframe 锚点未切掉主体:plan 窗包含主体包络 + 成片方块完整在框内。"""
    plan, frames, out = vlog_proj["plan"], vlog_proj["frames"], vlog_proj["out"]
    entries = {e["clipId"]: e for e in plan["clips"]}
    for i in range(N_FRAGS):
        size = LAND if i % 2 == 0 else PORT
        e = entries[f"V1-{i + 1:03d}"]
        win = e["cropWindow"]
        sx0, sx1, sy0, sy1 = subject_span(size)
        assert win["x0"] <= sx0 - 5 and sx1 + 5 <= win["x0"] + win["w"], \
            f"V1-{i + 1:03d} 横向切主体:窗[{win['x0']},{win['x0'] + win['w']}] 主体[{sx0},{sx1}]"
        assert win["y0"] <= sy0 - 5 and sy1 + 5 <= win["y0"] + win["h"], \
            f"V1-{i + 1:03d} 纵向切主体"
    # 成片像素级:横/竖两帧主体完整在框内(不贴边 = 未被窗边切掉)
    for t, tag in ((1.0, "c_land"), (FRAG_S + 1.0, "c_port")):
        bb = _red_bbox(_frame(out, t, frames, tag))
        assert bb, f"t={t} 主体必须可见"
        x0, y0, x1, y1 = bb
        assert x0 >= 10 and y0 >= 10 and x1 <= CW - 11 and y1 <= CH - 11, \
            f"t={t} 主体贴边/出框,锚点切掉了主体:{bb}"


@pixel_only
def test_d_first_three_seconds_hook(vlog_proj):
    """验收③ 前 3 秒有画面钩子:首 3s 抽 4 帧,非黑场(均值)且非静态 logo(方差)。"""
    out, frames = vlog_proj["out"], vlog_proj["frames"]
    for t in (0.2, 1.0, 2.0, 2.9):
        arr = _frame(out, t, frames, f"hook_{t}")
        gray = arr.mean(axis=2)
        assert 20 < gray.mean() < 235, f"t={t} 亮度均值 {gray.mean():.1f} 疑黑场/白场"
        assert gray.std() > 25, f"t={t} 亮度方差 {gray.std():.1f} 过低,疑黑场/logo 定版"


def test_e_bgm_ducking_config_and_audio(vlog_proj):
    """验收④ BGM 与人声不打架(ducking):配置在 IR(-18dB + ducking),成片有音轨。

    本夹具是无连续口播的 vlog(常态),无人声 → 渲染端 ducking 自动不生效、
    BGM 按增益直混;配置留档即为机械口径(有人声工程由 rs_render sidechaincompress 消费)。
    """
    ir, out = vlog_proj["ir"], vlog_proj["out"]
    bgm = ir["bgm"]
    assert bgm["gainDb"] == -18 and bgm["ducking"] is True
    assert Path(bgm["src"]).is_file(), "IR.bgm 必须是存在的绝对路径"
    info = rs_common.ffprobe_json(out, rs_common.load_config())
    assert any(s["codec_type"] == "audio" for s in info["streams"]), "成片必须有 BGM 音轨"
    assert not any("MIX_FAIL" in w or "ENCODE_FAIL" in w for w in vlog_proj["render"]["warnings"])
    assert abs(vlog_proj["render"]["duration_s"] - vlog_proj["total_ms"] / 1000) <= 0.15, \
        vlog_proj["render"]["duration_s"]


def test_f_no_voice_no_subtitle_track(vlog_proj):
    """验收⑤ 字幕只跟人声:无人声工程不出字幕轨(IR 无 subtitle.ass,输出无 ass 产物)。"""
    ir = vlog_proj["ir"]
    assert not ir.get("subtitle"), "无人声 vlog 不得有字幕配置(空转字幕红线)"
    ass_files = list((vlog_proj["root"] / rs_paths.p("output")).rglob("*.ass"))
    assert ass_files == [], f"无人声工程不得产出字幕文件:{ass_files}"


@pixel_only
def test_g_ending_hook_card(vlog_proj):
    """验收⑦ 结尾有下期钩子/订阅引导:overlay 卡在 IR 收尾 + 成片像素可见、不压字幕带。"""
    ir, out, frames = vlog_proj["ir"], vlog_proj["out"], vlog_proj["frames"]
    total_ms = vlog_proj["total_ms"]
    ov_tracks = [t for t in ir["tracks"] if t["kind"] == "video"][1:]
    ov_clips = [c for t in ov_tracks for c in t["clips"]]
    tail = [c for c in ov_clips
            if c["startMs"] >= total_ms - 5000 and c["startMs"] + c["durationMs"] >= total_ms]
    assert tail, "结尾 5s 内必须有贯穿到片尾的引导卡"
    assert vlog_proj["card"].is_file()
    # 成片像素:结尾 1s 卡面纯色可见(三点抽样);引导前同点位不是卡色
    end_t = total_ms / 1000 - 1.0
    arr_end = _frame(out, end_t, frames, "cta_end")
    pts = [(300, 1200), (760, 1180), (540, 1230)]     # 卡内且避开主体包络 ≥40px
    assert all(_is_card_blue(arr_end[y, x]) for x, y in pts), \
        f"结尾引导卡未渲染到位:{[tuple(arr_end[y, x]) for x, y in pts]}"
    arr_pre = _frame(out, end_t - 5.5, frames, "cta_pre")
    assert not any(_is_card_blue(arr_pre[y, x]) for x, y in pts), \
        "引导前同点位不应出现卡色(引导只在收尾段)"


def test_h_bgm_licensed_source(vlog_proj):
    """验收⑧ BGM 用授权源:曲库 manifest 自产无版权声明,选中曲在册。"""
    src = Path(vlog_proj["ir"]["bgm"]["src"])
    assert BGM_DIR in src.parents, f"BGM 必须来自内置曲库:{src}"
    man = _read(BGM_DIR / "manifest.json")
    assert "无版权" in man["license"]
    assert any((BGM_DIR / t["file"]) == src for t in man["tracks"]), \
        f"选中曲必须在曲库 manifest 在册:{src.name}"


def test_i_no_half_frame_flicker(vlog_proj):
    """验收⑥ 碎片拼接无半帧闪烁(机械等价):无损 concat 段长恒整数帧 + B2 对齐断言零漂移。

    「无重复动作残留」属粗剪 review 保守保留刀的人工确认项(L1),fixture 无废镜头不适用。
    """
    warnings = vlog_proj["render"]["warnings"]
    assert not any("音画对齐断言" in w for w in warnings), warnings
    dur = rs_common.media_duration_s(vlog_proj["out"], rs_common.load_config())
    nominal = vlog_proj["total_ms"] / 1000
    assert abs(dur - nominal) <= 1.5 / FPS + 0.02, \
        f"成片 {dur:.3f}s vs 名义 {nominal:.3f}s 超 1.5 帧容差(拼接漂移)"
