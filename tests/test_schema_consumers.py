# -*- coding: utf-8 -*-
"""v2 M11 · ADR-0055 零静默吞能力门禁 + R02/R04/R10-R14 回归。

①schema 枚举审计(动态遍历 project.schema.json 的全部 enum):
  每个枚举路径必须登记消费者文件,且每个枚举值都能在该文件源码中找到
  (新增枚举而未登记 → 本门禁红,逼着「要么实现、要么删声明」落到桌面);
  UNIMPLEMENTED 白名单 ≤3 条,每条带 reason + milestone(白名单也是债)。
②行为回归:cut/none 过 rs_ir.validate(R02);bgm.loop=false 生效(R04);
  motion 五个空壳的真实现产段(R10-R13);matte.bg.mode contain(R055)。
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

import rs_ir  # noqa: E402
import rs_paths  # noqa: E402
import rs_render  # noqa: E402

SCHEMA = REPO / "skills" / "cutflow" / "templates" / "project.schema.json"


def _ffmpeg_bin() -> str:
    try:
        cfg = rs_common.load_config()
        p = rs_common.ffmpeg_bin(cfg)
        if Path(p).is_file():
            return p
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


import rs_common  # noqa: E402

FF = _ffmpeg_bin()

# ---------------------------------------------------------------- ① 枚举审计

# schema enum 路径 → (消费者文件, 判定模式)
#   mode="value":每个枚举值的字符串必须在消费者源码出现
#   mode="key":数值型枚举,只要求字段名被消费(值本身是通用数值)
FILE_MAP = {
    "/properties/fps": ("rs_render.py", "key"),
    "/properties/canvas/properties/width": ("rs_render.py", "key"),
    "/properties/canvas/properties/height": ("rs_render.py", "key"),
    "/properties/tracks/items/properties/kind": ("rs_render.py", "value"),
    "/properties/outputs/items": ("rs_common.py", "value"),
    "/$defs/clip/properties/role": ("rs_render.py", "value"),
    "/$defs/clip/properties/motion/properties/in": ("rs_render.py", "value"),
    "/$defs/clip/properties/motion/properties/out": ("rs_render.py", "value"),
    "/$defs/clip/properties/transition/properties/type": ("rs_ir.py", "value"),
    "/$defs/clip/properties/transition/properties/reason": ("rs_render.py", "value"),
    "/$defs/clip/properties/punchIn/properties/source": ("rs_ir.py", "value"),
    "/$defs/clip/properties/matte/properties/engine": ("rs_matting.py", "value"),
    "/$defs/clip/properties/matte/properties/quality/properties/verdict": ("rs_ir.py", "value"),
    "/$defs/clip/properties/matte/properties/bg/properties/mode": ("rs_render.py", "value"),
}

# ADR-0055:声明支持但渲染端确实未实现的值。**白名单也是债**:≤3 条,
# 每条必须 reason + milestone;超限或清账期到 → 红。
UNIMPLEMENTED_WHITELIST = {
    "/$defs/clip/properties/role": {
        "values": ["music", "ambient"],
        "reason": "三轨优先级(解说>原声>BGM 之外的语义分轨)在 M14 日志/混音批次消费;"
                  "voice/sfx 已消费",
        "milestone": "M14",
    },
    "/$defs/clip/properties/punchIn/properties/source": {
        "values": ["manual"],
        "reason": "手动 punch-in 由 clip.reframe 手动锚点表达(render 走人工优先);"
                  "source 值随 M14 编辑层扩展接 rs_edit",
        "milestone": "M14",
    },
}


def test_every_schema_enum_has_registered_consumer():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    found: dict = {}

    def walk(d, path=""):
        if isinstance(d, dict):
            if "enum" in d:
                found[path] = list(d["enum"])
            for k, v in d.items():
                walk(v, f"{path}/{k}")
        elif isinstance(d, list):
            for i, v in enumerate(d):
                walk(v, f"{path}[{i}]")

    walk(schema)
    assert found, "schema 里一个 enum 都没有?审计表失效"
    missing = [p for p in found if p not in FILE_MAP]
    assert not missing, f"新枚举未登记消费者(ADR-0055):{missing}"
    stale = [p for p in FILE_MAP if p not in found]
    assert not stale, f"审计表指向已不存在的枚举路径(清表):{stale}"

    for path, values in found.items():
        fname, mode = FILE_MAP[path]
        src = (SCRIPTS / fname).read_text(encoding="utf-8")
        whitelisted = set(UNIMPLEMENTED_WHITELIST.get(path, {}).get("values", []))
        if mode == "key":
            key = path.rsplit("/", 1)[-1]
            assert key in src, f"{path}: 字段 {key} 无消费者证据({fname})"
            continue
        unchecked = [v for v in values if str(v) not in src and str(v) not in whitelisted]
        assert not unchecked, f"{path}: 枚举值 {unchecked} 在 {fname} 无实现证据(ADR-0055)"


def test_whitelist_is_bounded_and_documented():
    assert len(UNIMPLEMENTED_WHITELIST) <= 3, "ADR-0055:白名单 >3 必须回到「实现或删声明」"
    for path, entry in UNIMPLEMENTED_WHITELIST.items():
        assert entry.get("reason") and entry.get("milestone"), f"{path}: 白名单条目缺 reason/milestone"


# ---------------------------------------------------------------- ② 行为回归

def test_transition_cut_none_passes_validate(tmp_path):
    """R02:schema 合法的 cut/none 必须过 rs_ir.validate(此前契约自相矛盾)。"""
    root = tmp_path / "proj"
    (root / rs_paths.p("materials")).mkdir(parents=True)
    (root / rs_paths.p("materials") / "a.mp4").write_bytes(b"")
    for tr_t in ("cut", "none"):
        doc = {"version": 1, "slug": "p", "fps": 30,
               "canvas": {"width": 1080, "height": 1920},
               "tracks": [{"kind": "video", "name": "main", "clips": [
                   {"id": "c1", "src": "01_原始素材/a.mp4", "startMs": 0,
                    "durationMs": 1000, "sourceInMs": 0,
                    "transition": {"type": tr_t}}]}],
               "outputs": ["9x16"]}
        errs = rs_ir.validate(doc, root)
        assert not errs, f"transition.type={tr_t} 应合法:{errs}"


def _mk_ir(tmp_path: Path, motion: dict, with_audio: bool = False) -> tuple[Path, Path]:
    root = tmp_path / f"proj{abs(hash(json.dumps(motion, sort_keys=True))) % 99999}"
    mat = root / rs_paths.p("materials")
    mat.mkdir(parents=True, exist_ok=True)
    src = mat / "a.mp4"
    if FF and not src.exists():
        subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=0x4080C0:size=320x240:rate=10:duration=1.2",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
                        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", "-shortest", str(src)], check=False)
    ir = {"version": 1, "slug": "p", "fps": 10,
          "canvas": {"width": 320, "height": 240},
          "tracks": [{"kind": "video", "name": "main", "clips": [
              {"id": "c1", "src": str(src), "startMs": 0,
               "durationMs": 1200, "sourceInMs": 0, "motion": motion}]}],
          "outputs": ["9x16"]}
    ir_path = root / rs_paths.p("timeline") / "project.json"
    ir_path.parent.mkdir(parents=True, exist_ok=True)
    ir_path.write_text(json.dumps(ir, ensure_ascii=False), encoding="utf-8")
    return root, ir_path


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_motion_shells_render_real_effects(tmp_path):
    """R10-R13:schema 全部 motion 枚举值逐个出段成功(此前 5 个空壳退化/静默)。"""
    cases = [
        {"in": "fadeIn"}, {"in": "scaleIn"}, {"in": "zoomIn"},
        {"in": "slideInLeft"}, {"in": "slideInRight"},
        {"out": "fadeOut"}, {"out": "slideOutLeft"}, {"out": "slideOutRight"},
        {"in": "slideInLeft", "out": "slideOutRight"},
    ]
    for motion in cases:
        root, ir_path = _mk_ir(tmp_path, motion)
        doc = rs_render.render(json.loads(ir_path.read_text(encoding="utf-8")),
                               ir_path, "9x16", "draft")
        out_mp4 = Path(doc["output"])
        assert out_mp4.is_file() and out_mp4.stat().st_size > 0, \
            f"motion={motion} 未产成片:{doc}"


def test_no_video_track_structured_error():
    """R28:无 video 轨 → 结构化 NO_VIDEO_TRACK(SystemExit 2),不再 IndexError 裸栈。"""
    doc = {"version": 1, "slug": "p", "fps": 30,
           "canvas": {"width": 1080, "height": 1920},
           "tracks": [{"kind": "audio", "name": "a", "clips": []}],
           "outputs": ["9x16"]}
    with pytest.raises(SystemExit) as ei:
        rs_render._main_video_clips(doc)
    assert ei.value.code == 2


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用")
def test_bgm_loop_false_no_stream_loop(tmp_path, monkeypatch):
    """R04/R13:bgm.loop=false 时不加 -stream_loop(行为变更,CHANGELOG 已喊)。"""
    captured = {}

    class FakeP:
        returncode = 0
        stderr = ""

        def replace(self, *a):
            pass

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return FakeP()

    monkeypatch.setattr(rs_render, "run", fake_run)
    root = tmp_path / "proj"
    mat = root / rs_paths.p("materials")
    mat.mkdir(parents=True)
    bgm = mat / "bgm.mp3"
    shutil.copy(_any_asset_audio(), bgm) if _any_asset_audio() else bgm.write_bytes(b"x")
    src = mat / "a.mp4"
    subprocess.run([FF, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:size=160x120:rate=10:duration=0.5",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
                    str(src)], check=False)
    doc = {"version": 1, "slug": "p", "fps": 10,
           "canvas": {"width": 160, "height": 120},
           "tracks": [{"kind": "video", "name": "main", "clips": [
               {"id": "c1", "src": str(src), "startMs": 0, "durationMs": 500}]},
               {"kind": "audio", "name": "a", "clips": []}],
           "bgm": {"src": str(bgm), "gainDb": -18, "ducking": False, "loop": False},
           "outputs": ["9x16"]}
    build = root / rs_paths.p("output") / "_build"
    build.mkdir(parents=True)
    try:
        rs_render.step_mix(doc, src, build, root, {})
    except SystemExit:
        pass                      # 假 run 只记录命令,后续步骤失败无关紧要
    cmd = captured.get("cmd") or []
    assert "-stream_loop" not in cmd, f"loop:false 不得循环:{cmd}"


def _any_asset_audio() -> Path:
    for d in (REPO / "skills" / "cutflow" / "assets" / "bgm",):
        if d.is_dir():
            m = sorted(d.glob("*.mp3"))
            if m:
                return m[0]
    return Path("")
