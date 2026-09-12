"""IR → 剪映 5.9 明文草稿(vendored pyJianYingDraft,MIT)。

用法:python rs_jy_draft.py <project.json> [--name 草稿名] [--open]
产出:<draft_root>/<name>/draft_content.json + draft_meta_info.json,并注册进 root_meta_info.json。
安全:写前检测剪映进程;模板已脱敏(device_id/mac 置空)。
限制:chroma 绿幕叠加无 5.9 对应(跳过并警告);视觉淡入淡出 v1 不写关键帧(音频淡入淡出写入)。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "vendor"))

from rs_common import die, emit, ffprobe_json, load_config  # noqa: E402

# ---- pymediainfo shim:用 ffprobe 顶替,保持零第三方依赖 ----
import rs_common  # noqa: E402


class _FakeTrack:
    def __init__(self, dur_ms, w, h):
        self.duration, self.width, self.height = dur_ms, w, h


class _FakeInfo:
    def __init__(self):
        self.video_tracks: list = []
        self.image_tracks: list = []
        self.audio_tracks: list = []


class _FakeMediaInfo:
    @staticmethod
    def can_parse() -> bool:
        return True

    @staticmethod
    def parse(path, **kw):
        cfg = load_config()
        info = _FakeInfo()
        if str(path).lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            pr = ffprobe_json(path, cfg)
            v = next((s for s in pr["streams"] if s["codec_type"] == "video"), {})
            info.image_tracks.append(_FakeTrack(None, v.get("width", 1080), v.get("height", 1920)))
            return info
        pr = ffprobe_json(path, cfg)
        v = next((s for s in pr["streams"] if s["codec_type"] == "video"), None)
        a = next((s for s in pr["streams"] if s["codec_type"] == "audio"), None)
        dur_ms = float(pr.get("format", {}).get("duration") or 0) * 1000
        if v is not None:
            info.video_tracks.append(_FakeTrack(dur_ms, v.get("width", 1080), v.get("height", 1920)))
        if a is not None:
            info.audio_tracks.append(_FakeTrack(dur_ms, None, None))
        if v is None and a is not None:
            # 纯音频:pyJianYingDraft 的 AudioMaterial 走 audio_tracks
            pass
        return info


import types as _types
_fake_mod = _types.ModuleType("pymediainfo")
_fake_mod.MediaInfo = _FakeMediaInfo
sys.modules["pymediainfo"] = _fake_mod
import pymediainfo  # noqa: E402,F401  (shim 生效)

# ---- Python 3.14 兼容补丁:PEP 649 懒注解使 cls.__dict__['__annotations__'] 为空 ----
import typing as _typing  # noqa: E402
from pyJianYingDraft import util as _jy_util  # noqa: E402


def _assign_attr_with_json_py314(obj: object, attrs, json_data: dict):
    type_hints = _typing.get_type_hints(type(obj))
    for attr in attrs:
        t = type_hints[attr]
        if hasattr(t, "import_json"):
            obj.__setattr__(attr, t.import_json(json_data[attr]))
        else:
            obj.__setattr__(attr, t(json_data[attr]))


_jy_util.assign_attr_with_json = _assign_attr_with_json_py314

from pyJianYingDraft import (  # noqa: E402
    AudioMaterial, AudioSegment, ClipSettings, ScriptFile, TextBorder, TextSegment,
    TextStyle, Timerange, TrackType, VideoMaterial, VideoSegment)
from pyJianYingDraft.metadata import TransitionType  # noqa: E402

# IR transition.type → 剪映转场枚举名(未映射的回退叠化并警告)
JY_TRANSITION = {"fade": "叠化", "wipeleft": "向左擦除", "wipeup": "向上擦除",
                 "slideleft": "左移", "circleopen": "叠化"}

SUB_SIZE = {"9x16": 9.0, "3x4": 8.5, "16x9": 7.5}


def assert_jianying_closed() -> None:
    p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe"],
                       capture_output=True)
    out = (p.stdout or b"").decode("utf-8", errors="ignore")
    if "JianyingPro.exe" in out:
        die(4, "JY_RUNNING", "剪映正在运行,禁止写草稿(先关闭再重试)")


def register_in_root_meta(cfg: dict, name: str, draft_id: str, fold: Path) -> None:
    root_file = Path(cfg["jianying59"].get("root_meta") or
                     (Path(cfg["jianying59"]["draft_root"]) / "root_meta_info.json"))
    draft_root = root_file.parent
    now_us = time.time() * 1e6
    entry = {
        "draft_cloud_last_action_download": False, "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "", "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "", "draft_cover": str(fold / "draft_cover.jpg"),
        "draft_fold_path": str(fold), "draft_id": draft_id,
        "draft_is_ai_shorts": False, "draft_is_invisible": False,
        "draft_json_file": str(fold / "draft_content.json"),
        "draft_name": name, "draft_new_version": "",
        "draft_root_path": str(Path(cfg["jianying59"]["draft_root"])),
        "draft_timeline_materials_size": 0, "draft_type": "",
        "tm_draft_cloud_completed": "", "tm_draft_cloud_modified": 0,
        "tm_draft_create": int(now_us), "tm_draft_modified": int(now_us),
        "tm_draft_removed": 0, "tm_duration": 0,
    }
    doc = {"all_draft_store": [], "draft_ids": 0, "root_path": str(draft_root)}
    if root_file.is_file():
        doc = json.loads(root_file.read_text(encoding="utf-8"))
    doc.setdefault("all_draft_store", [])
    doc["all_draft_store"] = [e for e in doc["all_draft_store"] if e.get("draft_fold_path") != str(fold)]
    doc["all_draft_store"].append(entry)
    doc["draft_ids"] = len(doc["all_draft_store"])
    root_file.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def build_draft(doc: dict, project_path: Path, name: str, cfg: dict, warnings: list[str]) -> dict:
    base_dir = project_path.parent.parent
    template = Path(__file__).parents[1] / "templates" / "jy59_empty_draft.json"
    script = ScriptFile.load_template(str(template))

    script.width = doc["canvas"]["width"]
    script.height = doc["canvas"]["height"]
    script.fps = float(doc["fps"])

    def cp(clip):
        p = Path(clip["src"])
        return p if p.is_absolute() else base_dir / p

    # 主视频轨
    script.add_track(TrackType.video, "V1")
    video_tracks = [t for t in doc["tracks"] if t["kind"] == "video"]
    prev_seg = None
    for i, clip in enumerate(video_tracks[0]["clips"]):
        mat = VideoMaterial(str(cp(clip)))
        src_in = clip.get("sourceInMs", 0)
        take = int(clip["durationMs"] / clip.get("speed", 1.0) * 1000)
        seg = VideoSegment(mat, Timerange(int(clip["startMs"]) * 1000, int(clip["durationMs"]) * 1000),
                           source_timerange=Timerange(int(src_in) * 1000, int(take)),
                           speed=clip.get("speed", 1.0), volume=clip.get("volume", 1.0))
        script.add_segment(seg, "V1")
        # 转场挂在前一片段;音频淡入淡出按 IR clip.fade 写入
        if prev_seg is not None and clip.get("transition"):
            tr = clip["transition"]
            enum_name = JY_TRANSITION.get(tr.get("type", "fade"), "叠化")
            if tr.get("type") not in JY_TRANSITION:
                warnings.append(f"V1:转场 {tr.get('type')} 无映射,回退叠化")
            prev_seg.add_transition(getattr(TransitionType, enum_name),
                                    duration=int(tr.get("durMs", 500)) * 1000)
        if clip.get("fade"):
            seg.add_fade(int(clip["fade"].get("inMs", 0)) * 1000,
                         int(clip["fade"].get("outMs", 0)) * 1000)
        prev_seg = seg

    # 画中画/信息卡轨(V2+,render_index 更高)
    for ti, track in enumerate(video_tracks[1:]):
        tname = f"V{ti + 2}"
        script.add_track(TrackType.video, tname)
        for clip in track["clips"]:
            if clip.get("chroma"):
                warnings.append(f"{tname}:chroma 绿幕无 5.9 草稿对应,该叠加请走 FFmpeg 直出版本")
            mat = VideoMaterial(str(cp(clip)))
            src_in = clip.get("sourceInMs", 0)
            take = int(clip["durationMs"] / clip.get("speed", 1.0) * 1000)
            pos = clip.get("position", {"x": 0.5, "y": 0.5})
            sc = clip.get("scale", 1.0)
            cw, chh = doc["canvas"]["width"], doc["canvas"]["height"]
            mat_w = (mat.width or cw) * sc
            clipset = ClipSettings(
                scale_x=sc, scale_y=sc,
                transform_x=(pos["x"] - 0.5) * cw / (cw / 2),
                transform_y=(pos["y"] - 0.5) * chh / (chh / 2))
            seg = VideoSegment(mat, Timerange(int(clip["startMs"]) * 1000, int(clip["durationMs"]) * 1000),
                               source_timerange=Timerange(int(src_in) * 1000, int(take)),
                               speed=clip.get("speed", 1.0), volume=clip.get("volume", 1.0),
                               clip_settings=clipset)
            script.add_segment(seg, tname)

    # 音频轨
    audio_tracks = [t for t in doc["tracks"] if t["kind"] == "audio"]
    if audio_tracks:
        script.add_track(TrackType.audio, "A1")
        for track in audio_tracks:
            for clip in track["clips"]:
                mat = AudioMaterial(str(cp(clip)))
                dur_ms = clip.get("durationMs") or mat.duration // 1000
                seg = AudioSegment(mat, Timerange(int(clip["startMs"]) * 1000, int(dur_ms) * 1000),
                                   volume=clip.get("volume", 1.0))
                script.add_segment(seg, "A1")

    # 字幕轨(text 事件来自 tts manifest 或 transcript)
    sub_src = doc.get("subtitle", {}).get("source")
    if sub_src:
        sp = Path(sub_src)
        if not sp.is_absolute():
            sp = base_dir / sp
        events = []
        docj = json.loads(sp.read_text(encoding="utf-8"))
        if "sentences" in docj:  # tts manifest
            events = [{"start": s["start_s"], "end": s["end_s"], "text": s["text"]} for s in docj["sentences"]]
        else:  # transcript
            events = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in docj["segments"]]
        try:                                   # 画幅查表(1080 不再等价于 9x16,有 3x4)
            ratio = rs_common.ratio_for_canvas(doc["canvas"]["width"], doc["canvas"]["height"])
        except ValueError:
            ratio = "9x16"
        style = TextStyle(size=SUB_SIZE[ratio], bold=True, color=(1.0, 1.0, 1.0), align=1)
        border = TextBorder(color=(0.0, 0.0, 0.0), width=25.0)
        clipset = ClipSettings(transform_y=-0.8)
        for tname in ("T1",):
            script.add_track(TrackType.text, tname)
        for e in events[:120]:
            seg = TextSegment(e["text"], Timerange(int(e["start"] * 1e6), int((e["end"] - e["start"]) * 1e6)),
                              style=style, border=border, clip_settings=clipset)
            script.add_segment(seg, "T1")

    script.duration = max(
        [c["startMs"] + c.get("durationMs", 0) for t in doc["tracks"] if t["kind"] in ("video", "audio")
         for c in t["clips"]] + [0]) * 1000  # ms → μs

    draft_root = Path(cfg["jianying59"]["draft_root"])
    draft_root.mkdir(parents=True, exist_ok=True)
    fold = draft_root / name
    fold.mkdir(parents=True, exist_ok=True)
    draft_id = str(uuid.uuid4()).upper()

    content_path = fold / "draft_content.json"
    script.dump(str(content_path))
    # draft_content.json 顶层补身份字段(load_template 保留了模板的空 id)
    cj = json.loads(content_path.read_text(encoding="utf-8"))
    cj["id"] = draft_id
    cj["name"] = name
    cj["create_time"] = int(time.time() * 1e6)
    cj["tm_draft_create"] = int(time.time() * 1e6)
    cj["duration"] = script.duration
    content_path.write_text(json.dumps(cj, ensure_ascii=False), encoding="utf-8")

    meta = {
        "draft_fold_path": str(fold).replace("\\", "/"), "draft_id": draft_id,
        "draft_name": name, "draft_root_path": str(draft_root).replace("\\", "/"),
        "draft_type": "", "tm_draft_create": int(time.time() * 1e6),
        "tm_draft_modified": int(time.time() * 1e6), "tm_duration": script.duration // 1000,
        "draft_materials": [], "draft_cover": "draft_cover.jpg",
    }
    (fold / "draft_meta_info.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    register_in_root_meta(cfg, name, draft_id, fold)
    return {"draft_dir": str(fold), "draft_id": draft_id,
            "segments": sum(len(t["clips"]) for t in doc["tracks"])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--name", default=None)
    ap.add_argument("--subtitles", default=None, help="字幕源(tts manifest/transcript json),覆盖 IR")
    ap.add_argument("--open", action="store_true", help="生成后启动剪映 5.9")
    a = ap.parse_args()
    p = Path(a.project)
    if not p.is_file():
        return emit(False, "NO_PROJECT", f"IR 不存在:{p}", exit_code=2)
    doc = json.loads(p.read_text(encoding="utf-8"))
    if a.subtitles:
        doc.setdefault("subtitle", {})["source"] = a.subtitles
    cfg = load_config()
    assert_jianying_closed()
    warnings: list[str] = []
    name = a.name or f"cutflow_{time.strftime('%m%d_%H%M')}"
    data = build_draft(doc, p, name, cfg, warnings)
    if a.open:
        subprocess.Popen(["cmd", "/c", "start", "", cfg["jianying59"]["exe"]],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    data["warnings"] = warnings
    return emit(True, "DRAFT_OK", f"草稿已生成:{data['draft_dir']}", data)


if __name__ == "__main__":
    sys.exit(main())
