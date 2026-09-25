"""v0.10 迭代回归:边缘精修 / 交叉溶解 / ASR 自举 / Logo 真实尺寸。

对账:ADR-0022(colorkey 边缘腐蚀+green-only killRect)、ADR-0023(sub-1帧转场提升为
交叉溶解 + 尾帧扩展零漂移)、ADR-0024(ASR 自举 NameError 修复)、ADR-0025(Logo
真实尺寸与 6 锚点排版)。只测公开 seam;ffmpeg 实机用例沿用 test_v8 skipif 体例。
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "cutflow" / "scripts"))

import pytest  # noqa: E402

import rs_brand  # noqa: E402
import rs_render  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or str(REPO / ".." / "Tools" / "ffmpeg" / "bin" / "ffmpeg.exe")

CLIPS3 = [
    {"src": "a.mp4", "startMs": 0, "durationMs": 4000, "sourceInMs": 0},
    {"src": "a.mp4", "startMs": 4000, "durationMs": 4000, "sourceInMs": 8000,
     "transition": {"type": "fade", "durMs": 8}},
    {"src": "a.mp4", "startMs": 8000, "durationMs": 4000, "sourceInMs": 16000,
     "transition": {"type": "fade", "durMs": 8}},
]


# ---------------------------------------------------------------- ADR-0023 交叉溶解

def test_subframe_transition_promoted_to_crossfade():
    """0<tdur<1帧 的转场不再整链弃用,提升为 joinCrossfadeMs(用户反馈#2)。"""
    eff, forced_off, reasons = rs_render._resolve_transitions(CLIPS3, 30, {}, None)
    assert not forced_off and not reasons
    assert eff[1] == pytest.approx(0.12)
    assert eff[2] == pytest.approx(0.12)


def test_promote_ms_overridable_per_doc():
    eff, _, _ = rs_render._resolve_transitions(CLIPS3, 30, {"joinCrossfadeMs": 200}, None)
    assert eff[1] == pytest.approx(0.2)


def test_cut_type_means_hard_cut():
    clips = [dict(CLIPS3[0]), {**CLIPS3[1], "transition": {"type": "cut"}}]
    eff, forced_off, _ = rs_render._resolve_transitions(clips, 30, {}, None)
    assert eff[1] == 0 and not forced_off


def test_explicit_transition_above_frame_respected():
    clips = [dict(CLIPS3[0]),
             {**CLIPS3[1], "transition": {"type": "fade", "durMs": 500}}]
    eff, _, _ = rs_render._resolve_transitions(clips, 30, {}, None)
    assert eff[1] == pytest.approx(0.5)


def test_tight_gap_still_forces_concat():
    """源间隙放不下尾帧扩展 → 整链弃用(宁缺勿错,保守回退不变)。"""
    clips = [dict(CLIPS3[0]),
             {**CLIPS3[1], "sourceInMs": 4050}]          # gap=50ms < 120ms
    eff, forced_off, reasons = rs_render._resolve_transitions(clips, 30, {}, None)
    assert forced_off and all(v == 0 for v in eff) and reasons


def test_tail_equals_next_join_transition():
    """尾帧扩展不变量:段 i 的尾帧 = 它与下一段的转场时长(xfade offset=后段名义起点)。"""
    eff, _, _ = rs_render._resolve_transitions(CLIPS3, 30, {}, None)
    tails = [eff[i + 1] if i + 1 < len(CLIPS3) else 0.0 for i in range(len(CLIPS3))]
    fps = 30
    qdurs = [max(1, round(c["durationMs"] / 1000 * fps)) / fps for c in CLIPS3]
    cum = qdurs[0] + tails[0]
    for i in range(1, len(CLIPS3)):
        offset = cum - eff[i]
        assert offset == pytest.approx(sum(qdurs[:i]))   # 时间零漂移
        cum = offset + qdurs[i] + tails[i]
    assert cum == pytest.approx(sum(qdurs) + tails[-1])


def test_step_segment_uses_tails_and_quantization():
    src = inspect.getsource(rs_render.step_segment)
    # M13:转场解析改走注册表版 resolve_joins(旧 _resolve_transitions 为兼容薄壳)
    assert ("resolve_joins" in src or "_resolve_transitions" in src) and "tailMs" in src
    assert "q_in_ms" in src and "q_out_ms" in src


def test_concat_trims_to_nominal_total():
    """v0.11:offset 保持名义起点(零漂移锚),但时长按实测段长夹紧并留安全边际
    (xfade 贴边会整段坍缩,店群/dev 工程实测),末尾仍按名义总长 -t 裁齐。"""
    src = inspect.getsource(rs_render.step_concat)
    assert "total_s" in src and "nominal_cum" in src
    assert "offset = nominal_cum" in src and "_video_stream_len" in src
    assert "cum - offset - frame" in src          # 安全边际


# ---------------------------------------------------------------- ADR-0024 ASR 自举

def _load_fun_asr():
    spec = importlib.util.spec_from_file_location("fun_asr", REPO / "tools" / "fun_asr.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fun_asr_any_backend_ready_defined():
    """v0.10:ensure_backend 曾引用未定义的 any_backend_ready → 每次转写必崩(反馈#3 真凶)。"""
    mod = _load_fun_asr()
    assert callable(mod.any_backend_ready)
    assert isinstance(mod.any_backend_ready(), bool)


def test_doctor_uses_local_probe():
    src = inspect.getsource(import_rs_doctor())
    assert "probe_asr_local" in src
    assert "--ensure" in src               # 检查项 hint 指向自动部署,而非手装
    assert "fun_asr.py" in src


def import_rs_doctor():
    spec = importlib.util.spec_from_file_location(
        "rs_doctor", REPO / "skills" / "cutflow" / "scripts" / "rs_doctor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- ADR-0025 Logo 真实尺寸

CANVAS = {"width": 1080, "height": 1920}


def test_logo_rect_real_aspect_not_square():
    logo = {"scale": 0.12, "anchor": "topRight", "_probe": {"content": {"aspect": 2.0}}}
    r = rs_brand.logo_rect(logo, CANVAS, "9x16")
    assert r["w"] == 129                                        # int(1080*0.12)
    assert r["h"] == int(round(129 / 2.0))                      # 真实宽高比,非方形
    assert r["x"] == CANVAS["width"] - r["w"] - 43 and not r["lifted"]


def test_logo_rect_height_cap():
    logo = {"scale": 0.5, "anchor": "topLeft", "_probe": {"content": {"aspect": 0.5}}}
    r = rs_brand.logo_rect(logo, CANVAS, "9x16")
    max_h = int(CANVAS["height"] * 0.08)
    assert r["h"] == max_h                                      # 8% 画高上限
    assert r["w"] == int(round(max_h * 0.5))


def test_logo_bottom_anchors_lift_above_band():
    for anchor in ("bottomLeft", "bottomRight", "bottomCenter"):
        logo = {"scale": 0.12, "anchor": anchor, "_probe": {"content": {"aspect": 1.0}}}
        r = rs_brand.logo_rect(logo, CANVAS, "9x16")
        assert r["lifted"]
        assert r["y"] + r["h"] <= int(CANVAS["height"] * 0.75)   # 字幕带上缘


def test_logo_center_anchors():
    logo = {"scale": 0.12, "anchor": "topCenter", "_probe": {"content": {"aspect": 1.0}}}
    r = rs_brand.logo_rect(logo, CANVAS, "9x16")
    assert abs(r["x"] - (CANVAS["width"] - r["w"]) / 2) <= 1


def test_logo_unknown_anchor_rejected():
    with pytest.raises(ValueError):
        rs_brand.logo_rect({"anchor": "middle"}, CANVAS, "9x16")


def test_variant_ir_produces_overlay_dict():
    ir = {"version": 1, "slug": "t", "fps": 30,
          "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"kind": "video", "clips": [
              {"src": "a.mp4", "startMs": 0, "durationMs": 5000}]}]}
    logo = {"id": "b", "src": "x.png", "anchor": "topRight", "scale": 0.12, "opacity": 0.9,
            "_probe": {"width": 100, "height": 50, "hasAlpha": True, "aspect": 2.0,
                       "content": {"bbox": [0, 0, 100, 50], "width": 100, "height": 50,
                                   "aspect": 2.0}}}
    doc = rs_brand.variant_ir(ir, {"id": "b_9x16", "backends": ["ffmpeg"]}, logo, "9x16")
    clip = doc["tracks"][-1]["clips"][0]
    assert clip["overlay"]["w"] == 129
    assert clip["overlay"]["h"] == int(round(129 / 2.0))
    assert clip["overlay"]["opacity"] == pytest.approx(0.9)
    assert doc["_variant"]["logoProbe"]["width"] == 100


@pytest.mark.skipif(not Path(FFMPEG).is_file(), reason="ffmpeg 不可用")
def test_probe_logo_alpha_bbox(tmp_path):
    """透明 padding 不算尺寸:200x100 画板,内容为 (120,40) 起 60x40 红块 → 比例 1.5。"""
    png = tmp_path / "logo.png"
    p = subprocess.run([FFMPEG, "-y", "-v", "error",
                        "-f", "lavfi", "-i", "color=c=black@0.0:s=200x100:d=1,format=rgba",
                        "-f", "lavfi", "-i", "color=c=red:s=60x40:d=1,format=rgba",
                        "-filter_complex", "[0][1]overlay=120:40",
                        "-frames:v", "1", str(png)], capture_output=True)
    if p.returncode != 0:
        pytest.skip(f"lavfi 不可用:{p.stderr[-120:]}"
                    .encode("ascii", "replace").decode())
    info = rs_brand.probe_logo(png)
    assert info["hasAlpha"]
    assert info["content"]["bbox"] == [120, 40, 60, 40]
    assert info["content"]["aspect"] == pytest.approx(1.5)


def test_schema_json_valid_and_new_fields():
    schema = json.loads((REPO / "skills" / "cutflow" / "templates" /
                         "project.schema.json").read_text(encoding="utf-8"))
    props = schema["$defs"]["clip"]["properties"]
    assert "chroma" not in props and "background" not in props, \
        "v0.14(ADR-0031):抠像/背景合成已移除,schema 不得再留字段"
    assert "overlay" in props
    assert "joinCrossfadeMs" in schema["properties"]
    assert "cut" in schema["$defs"]["clip"]["properties"]["transition"]["properties"]["type"]["enum"]


# ---------------------------------------------------------------- P0 附带修复:S1 first_material

def test_first_material_skips_manifest_and_images(tmp_path):
    """01_原始素材 里的 S0 产物(manifest.json/MANIFEST.md)与图片不得当 ASR 输入;
    Windows 大小写不敏感排序会把 manifest.json 排到最前(店群工程 S1 必崩根因)。"""
    import rs_run
    mat = tmp_path / "01_原始素材"
    mat.mkdir()
    for name in ("manifest.json", "MANIFEST.md", "背景.jpg", "视频.MP4"):
        (mat / name).write_bytes(b"x")
    mats = rs_run.expand(tmp_path, ["01_原始素材/*"])
    picked = rs_run.pick_asr_media(mats)
    assert picked is not None and picked.name == "视频.MP4"
    st = next(s for s in rs_run.spec() if s["id"] == "S1")
    cmd = rs_run.build_cmd(tmp_path, st)
    assert cmd[-3] == "01_原始素材\视频.MP4" or cmd[-3].endswith("视频.MP4")


@pytest.mark.skipif(not Path(FFMPEG).is_file(), reason="ffmpeg 不可用")
def test_removed_chroma_helpers_are_gone():
    """v0.14(ADR-0031):抠像链函数整体移除 —— 不得残留任何键控实现。"""
    for name in ("sample_chroma", "chroma_hex", "_chroma_key_v2_chain", "_chroma_fg_chain",
                 "_crop_pct_chain", "parse_matte_log", "matte_fg_ratio", "matte_probe_args",
                 "CHROMA_DEFAULTS", "BG_TYPES"):
        assert not hasattr(rs_render, name), f"rs_render.{name} 应随抠像功能一并删除"


def _pil():
    try:
        from PIL import Image  # noqa: PLC0415
        return Image
    except ImportError:
        return None


def test_s9_uses_final_space_wordline(tmp_path):
    """S9 对账必须用成片空间的 wordline.final.json(remap 产物,rs_verify 同一约定);
    wordline.json 是源空间,拿它对账时长必差一个粗剪裁剪量(店群工程 S9 假失败实测)。"""
    import rs_run
    ir = tmp_path / "05_时间线工程"
    ir.mkdir()
    (ir / "wordline.final.json").write_text("{}", encoding="utf-8")
    st = next(s for s in rs_run.spec() if s["id"] == "S9")
    cmd = rs_run.build_cmd(tmp_path, st)
    assert any(x.endswith("wordline.final.json") for x in cmd)
    (ir / "wordline.final.json").unlink()          # 无 remap 产物时回退,不空转
    cmd2 = rs_run.build_cmd(tmp_path, st)
    assert any(x.endswith("wordline.json") for x in cmd2)
