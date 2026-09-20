"""IR → 剪映 5.9 明文草稿(vendored pyJianYingDraft,MIT)。

用法:python rs_jy_draft.py <project.json> [--name 草稿名] [--dry-run] [--open]
产出:<draft_root>/<name>/draft_content.json + draft_meta_info.json,并注册进 root_meta_info.json。

编译层(阶段六 J1/J3,副文档 06):
  IR ──compile──► 草稿计划(draft plan,帧对齐中间表示)──门禁──► 写草稿
  · 计划是可校验的中间表示:每段带 帧号/微秒 双记法,先编译、先校验,后写盘;
  · 门禁:轨道数符合计划 / 主轨时长和 == 预期 / 首段从 0 且不重叠 / 无黑场间隙 /
    帧对齐断言(所有边界落在工程 fps 的帧网格上);任一失败 → PLAN_GATE_FAIL 拒写;
  · --dry-run:打印「IR 片段 → 草稿片段」映射表(人读),不写任何文件、不查剪映进程。
安全:写前检测剪映进程(运行中即 JY_RUNNING 拒绝);只写 config.jianying59 指向的
  5.9 明文草稿根,11.3+ 加密草稿永不读写。
限制:视觉动效关键帧(motion/reframe/punchIn)v1 不写(计划留 warning,见
  rules/jianying-verification.md「未支持」区);音频淡入淡出经 fade 字段写入。
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

from rs_common import die, emit, load_config  # noqa: E402

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
            pr = rs_common.ffprobe_json(path, cfg)
            v = next((s for s in pr["streams"] if s["codec_type"] == "video"), {})
            info.image_tracks.append(_FakeTrack(None, v.get("width", 1080), v.get("height", 1920)))
            return info
        pr = rs_common.ffprobe_json(path, cfg)
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

# ---- uiautomation 惰性桩:vendor 包的 jianying_controller 顶层 import 它,且在
# Python ≤3.13 下会急切求值类型注解(uia.WindowControl 等)——桩必须宽容(任何属性/
# 调用都返回自身),否则注解求值直接炸掉 import。本脚本的剪映进程检测走 tasklist,
# 从不驱动 GUI;真有人调到 GUI 自动化时自然会得到不可用的桩对象。
try:
    import uiautomation  # noqa: F401
except ImportError:
    _uia_stub = _types.ModuleType("uiautomation")

    class _UiaAbsent:
        def __getattr__(self, name: str):
            return _uia_absent

        def __call__(self, *args, **kwargs):
            return _uia_absent

        def __bool__(self) -> bool:
            return False

        def __repr__(self) -> str:
            return "<uiautomation 未安装(桩)>"

    _uia_absent = _UiaAbsent()
    _uia_stub.__getattr__ = lambda name: _uia_absent  # type: ignore[method-assign]
    sys.modules["uiautomation"] = _uia_stub

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

# ---- 草稿计划(编译层中间表示,阶段六 J1)----
PLAN_KIND = "cutflow-jy-draft-plan"
PLAN_VERSION = 1
MAX_TEXT_SEGMENTS = 120        # T1 字幕条数上限(既有口径,行为不变)
FRAME_EPS_US = 1               # 帧网格点的 μs 表示精度容差(网格点四舍五入到整微秒)
KIND_TO_TRACK = {"video": TrackType.video, "audio": TrackType.audio, "text": TrackType.text}


# ---------------------------------------------------------------- 安全闸

def _jianying_running(tasklist_output: str) -> bool:
    """tasklist 输出 → 是否检测到剪映进程(独立成函数供单测,不真开进程)。"""
    return "JianyingPro.exe" in (tasklist_output or "")


def assert_jianying_closed() -> None:
    p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe"],
                       capture_output=True)
    out = (p.stdout or b"").decode("utf-8", errors="ignore")
    if _jianying_running(out):
        die(4, "JY_RUNNING", "剪映正在运行,禁止写草稿(先关闭再重试)")


# ---------------------------------------------------------------- 帧网格

def frames_of(ms: float, fps: float) -> int:
    """毫秒 → 最近帧号(编译层的量化口径:四舍五入到工程 fps 的帧网格)。"""
    return int(round(float(ms) * float(fps) / 1000.0))


def frame_to_us(k: int, fps: float) -> int:
    """帧号 → 微秒(剪映内部时间的最小单位;帧网格点取整微秒表示)。"""
    return int(round(k * 1_000_000.0 / float(fps)))


def _q(ms: float, fps: float) -> tuple[int, int]:
    """毫秒 → (帧号, 微秒)。段边界一律过这里,保证同值毫秒必得同网格点。"""
    k = frames_of(ms, fps)
    return k, frame_to_us(k, fps)


def resolve_src(src: str, base_dir: Path) -> Path:
    """IR src → 绝对路径;assets_sfx: 伪协议解析到内置音效库(J4)。"""
    s = str(src or "")
    if s.startswith("assets_sfx:"):
        return rs_common.REPO_ROOT / "assets" / "sfx" / (s.split(":", 1)[1] + ".mp3")
    p = Path(s)
    return p if p.is_absolute() else base_dir / p


def _probe_duration_ms(path: Path, cfg: dict | None, errors: list[str], where: str) -> int:
    """ffprobe 实测媒体时长 ms(不 die:失败返回 0,由调用方决定降级还是拒绝)。"""
    try:
        from rs_common import ffprobe_bin
        p = rs_common.run([ffprobe_bin(cfg), "-v", "error", "-show_entries",
                           "format=duration", "-of", "csv=p=0", str(path)])
        if p.returncode == 0:
            rows = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
            if rows and float(rows[-1]) > 0:
                return int(round(float(rows[-1]) * 1000))
    except Exception:  # noqa: BLE001 — 无 ffprobe/坏文件:返回 0,调用方显式处理
        pass
    errors.append(f"{where}:媒体时长实测失败(ffprobe 不可用或素材不可解码):{path}")
    return 0


# ---------------------------------------------------------------- 编译(J1)

def _seg_common(seg_id: str, src: Path, start_ms: float, dur_ms: float, fps: float,
                ir_ref: dict) -> dict:
    """段的时间四件套:帧号 + 微秒双记法(时长 = 边界差,保证相邻段精确铺贴)。"""
    k0, u0 = _q(start_ms, fps)
    k1, u1 = _q(start_ms + dur_ms, fps)
    return {"planSegId": seg_id, "src": str(src),
            "startUs": u0, "endUs": u1, "durationUs": u1 - u0,
            "startFrame": k0, "endFrame": k1, "durationFrames": k1 - k0,
            "irRef": ir_ref}


def compile_draft_plan(doc: dict, project_path: Path, cfg: dict | None = None,
                       warnings: list[str] | None = None) -> dict:
    """IR → 草稿计划(帧对齐、可校验)。纯映射:不写盘、不开进程、不建素材。"""
    warnings = warnings if warnings is not None else []
    errors: list[str] = []
    base_dir = project_path.parent.parent
    fps = float(doc.get("fps") or 30)
    canvas = {"width": int(doc["canvas"]["width"]), "height": int(doc["canvas"]["height"])}
    tracks: list[dict] = []

    def new_track(tid: str, kind: str, role: str) -> dict:
        t = {"trackId": tid, "kind": kind, "role": role, "segments": []}
        tracks.append(t)
        return t

    def push(t: dict, seg: dict) -> None:
        seg["planSegId"] = f"{t['trackId']}-{len(t['segments']) + 1:03d}"
        t["segments"].append(seg)

    # ---- 视频轨:首条 video = 主轨 V1,其余 = 画中画 V2+(位置/缩放)----
    first_video_ti = next((i for i, t in enumerate(doc.get("tracks", []))
                           if t.get("kind") == "video"), None)
    overlay_no = 0
    for ti, tr in enumerate(doc.get("tracks", [])):
        if tr.get("kind") != "video":
            continue
        is_main = ti == first_video_ti
        if is_main:
            vt = new_track("V1", "video", "main")
        else:
            overlay_no += 1
            vt = new_track(f"V{overlay_no + 1}", "video", "overlay")
        for ci, clip in enumerate(tr.get("clips", [])):
            dur_ms = float(clip.get("durationMs") or 0)
            seg = _seg_common("tmp", resolve_src(clip.get("src"), base_dir),
                              float(clip.get("startMs") or 0), dur_ms, fps,
                              {"track": ti, "clip": ci,
                               "startMs": int(clip.get("startMs") or 0),
                               "durationMs": int(dur_ms)})
            speed = float(clip.get("speed") or 1.0)
            seg["speed"] = speed
            seg["volume"] = float(clip.get("volume", 1.0))
            _, take_us = _q(dur_ms / speed, fps)
            seg["sourceInUs"] = _q(float(clip.get("sourceInMs") or 0), fps)[1]
            seg["sourceDurationUs"] = take_us
            tr_d = clip.get("transition")
            if tr_d:
                enum_name = JY_TRANSITION.get(tr_d.get("type", "fade"), "叠化")
                if tr_d.get("type") not in JY_TRANSITION:
                    warnings.append(f"{vt['trackId']}:转场 {tr_d.get('type')} 无映射,回退叠化")
                _, tdu = _q(float(tr_d.get("durMs", 500)), fps)
                seg["transitionIn"] = {"irType": tr_d.get("type", "fade"),
                                       "map": enum_name, "durationUs": tdu}
            if clip.get("fade"):
                fin = _q(float(clip["fade"].get("inMs", 0)), fps)[1]
                fout = _q(float(clip["fade"].get("outMs", 0)), fps)[1]
                if fin or fout:
                    seg["fadeUs"] = {"in": fin, "out": fout}
            motion = clip.get("motion") or {}
            for side in ("in", "out"):
                if motion.get(side, "none") != "none":
                    warnings.append(f"{vt['trackId']}:motion.{side}={motion.get(side)} "
                                    "无草稿映射(视觉动效 v1 不写关键帧,仅 rs_render 支持)")
            if ci == 0 and seg.get("transitionIn"):
                warnings.append(f"{vt['trackId']}:首段携带转场,无处挂靠(转场挂前段),已忽略")
            if not is_main:
                pos = clip.get("position") or {"x": 0.5, "y": 0.5}
                seg["transform"] = {"x": float(pos.get("x", 0.5)), "y": float(pos.get("y", 0.5)),
                                    "scale": float(clip.get("scale", 1.0))}
            push(vt, seg)

    # ---- 音频轨:全部 audio 轨并入 A1(既有口径);音效 gainDb → 线性音量(J4)----
    a1: dict | None = None
    for ti, tr in enumerate(doc.get("tracks", [])):
        if tr.get("kind") != "audio":
            continue
        if a1 is None:
            a1 = new_track("A1", "audio", "sound")
        for ci, clip in enumerate(tr.get("clips", [])):
            src = resolve_src(clip.get("src"), base_dir)
            dur_ms = clip.get("durationMs")
            if not dur_ms:
                dur_ms = _probe_duration_ms(src, cfg, errors, f"A1:tracks[{ti}].clips[{ci}]")
            start_ms = float(clip.get("startMs") or 0)
            seg = _seg_common("tmp", src, start_ms, float(dur_ms), fps,
                              {"track": ti, "clip": ci, "startMs": int(start_ms),
                               "durationMs": int(dur_ms)})
            seg["sourceInUs"] = 0
            seg["sourceDurationUs"] = seg["durationUs"]
            seg["speed"] = 1.0
            volume = clip.get("volume")
            if volume is None and clip.get("gainDb") is not None:
                volume = 10 ** (float(clip["gainDb"]) / 20.0)   # J4:音效 gainDb → 线性音量
            seg["volume"] = float(volume if volume is not None else 1.0)
            seg["role"] = str(clip.get("role") or "sound")
            push(a1, seg)

    # ---- 时间线时长:视频/音频段的最后一帧(与旧口径一致,不含文本)----
    va_tracks = [t for t in tracks if t["kind"] in ("video", "audio")]
    max_frame = max((s["endFrame"] for t in va_tracks for s in t["segments"]), default=0)
    timeline_us = frame_to_us(max_frame, fps)

    # ---- BGM(IR 已表达、草稿可承载 → 编译层映射,J4;素材不可用时**可降级**跳过,
    #      但必须留 warning —— BGM 缺席不毁草稿,静默缺席才毁信任)----
    bgm = doc.get("bgm") or {}
    if bgm.get("ducking"):
        warnings.append("bgm.ducking 无草稿映射(v1;剪映侧请手动开人声闪避)")
    if bgm.get("src") and max_frame > 0:
        src = resolve_src(bgm["src"], base_dir)
        b_errors: list[str] = []
        b_ms = (_probe_duration_ms(src, cfg, b_errors, "bgm")
                if src.is_file() else 0)
        if b_ms <= 0:
            warnings.append(f"bgm 不可用({b_errors[0] if b_errors else '素材不存在'}),"
                            "已跳过(BGM 为可降级项,不毁草稿)")
        else:
            take_frames = min(frames_of(b_ms, fps), max_frame)
            if take_frames < max_frame:
                warnings.append(f"bgm 比时间线短 {max_frame - take_frames} 帧,只铺前段")
            a2 = new_track("A2", "audio", "bgm")
            seg = _seg_common("tmp", src, 0, frame_to_us(take_frames, fps) / 1000.0, fps,
                              {"ir": "bgm", "startMs": 0, "durationMs": int(b_ms)})
            seg["sourceInUs"] = 0
            seg["sourceDurationUs"] = seg["durationUs"]
            seg["speed"] = 1.0
            seg["volume"] = 10 ** (float(bgm.get("gainDb", 0)) / 20.0)
            seg["role"] = "bgm"
            push(a2, seg)

    # ---- 字幕轨(T1,≤120 条,行为不变)----
    sub_src = (doc.get("subtitle") or {}).get("source")
    if sub_src:
        sp = Path(sub_src)
        if not sp.is_absolute():
            sp = base_dir / sp
        if not sp.is_file():
            errors.append(f"T1:字幕源不存在:{sp}")
        else:
            try:
                docj = json.loads(sp.read_text(encoding="utf-8"))
                if "sentences" in docj:      # tts manifest
                    events = [{"start": s["start_s"], "end": s["end_s"], "text": s["text"]}
                              for s in docj["sentences"]]
                else:                        # transcript
                    events = [{"start": s["start"], "end": s["end"], "text": s["text"]}
                              for s in docj["segments"]]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                events = []
                errors.append(f"T1:字幕源不可解析({type(exc).__name__}):{sp}")
            if len(events) > MAX_TEXT_SEGMENTS:
                warnings.append(f"字幕 {len(events)} 条超上限,截前 {MAX_TEXT_SEGMENTS} 条")
            if events:
                t1 = new_track("T1", "text", "subtitle")
                for i, e in enumerate(events[:MAX_TEXT_SEGMENTS]):
                    k0, u0 = _q(round(float(e["start"]) * 1000), fps)
                    k1, u1 = _q(round(float(e["end"]) * 1000), fps)
                    if k1 <= k0:
                        warnings.append(f"T1:第 {i + 1} 条字幕不足 1 帧,已丢弃:"
                                        f"「{str(e['text'])[:12]}…」")
                        continue
                    push(t1, {"src": "", "text": str(e["text"]),
                              "startUs": u0, "endUs": u1, "durationUs": u1 - u0,
                              "startFrame": k0, "endFrame": k1, "durationFrames": k1 - k0,
                              "sourceInUs": 0, "sourceDurationUs": u1 - u0, "speed": 1.0,
                              "irRef": {"source": sub_src, "index": i}})

    main_track = next((t for t in tracks if t["role"] == "main"), None)
    return {
        "planVersion": PLAN_VERSION, "kind": PLAN_KIND,
        "slug": str(doc.get("slug") or ""),
        "fps": fps, "canvas": canvas,
        "target": {"backend": "jianying", "version": "5.9", "draftFormat": "plain"},
        "durationUs": timeline_us, "durationFrames": max_frame,
        "expects": {
            "trackCount": len(tracks),
            "segmentCount": sum(len(t["segments"]) for t in tracks),
            "mainDurationFrames": sum(s["durationFrames"]
                                      for s in (main_track["segments"] if main_track else [])),
        },
        "tracks": tracks,
        "compileErrors": errors,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 门禁(J3)

def plan_gates(plan: dict) -> list[str]:
    """草稿计划门禁:任一失败 → 拒绝写草稿(PLAN_GATE_FAIL,退出码非零)。

    断言面:轨道数符合计划 / 片段时长和 == 预期 / 主轨首段从 0 且不重叠 /
    无黑场间隙(相邻段无缝;转场不产生间隙)/ 帧对齐(所有边界落在帧网格上)。
    """
    errs: list[str] = []
    errs.extend(plan.get("compileErrors") or [])
    if plan.get("kind") != PLAN_KIND:
        return [f"plan.kind 非法:{plan.get('kind')!r}(应为 {PLAN_KIND})"]
    if plan.get("planVersion") != PLAN_VERSION:
        errs.append(f"plan.planVersion 非法:{plan.get('planVersion')}")
    fps = float(plan.get("fps") or 0)
    if fps <= 0:
        errs.append(f"plan.fps 非法:{plan.get('fps')}")
        return errs
    if not plan.get("canvas", {}).get("width") or not plan["canvas"].get("height"):
        errs.append(f"plan.canvas 非法:{plan.get('canvas')}")
    tracks: list[dict] = plan.get("tracks") or []
    ids = [t.get("trackId") for t in tracks]
    if len(set(ids)) != len(ids):
        errs.append(f"轨道 id 重复:{ids}")
    mains = [t for t in tracks if t.get("role") == "main"]
    if len(mains) != 1:
        errs.append(f"主轨必须恰一条,实有 {len(mains)}")
    elif not mains[0]["segments"]:
        errs.append("主轨 V1 没有任何片段")

    total_segs = 0
    for t in tracks:
        tid = t.get("trackId", "?")
        segs = sorted(t.get("segments") or [], key=lambda s: s["startUs"])
        total_segs += len(segs)
        if t.get("role") == "subtitle" and len(segs) > MAX_TEXT_SEGMENTS:
            errs.append(f"{tid}:字幕 {len(segs)} 条超上限 {MAX_TEXT_SEGMENTS}")
        prev_end: int | None = None
        for s in segs:
            sid = s.get("planSegId", "?")
            if s.get("durationFrames", 0) < 1:
                errs.append(f"{sid}:时长 {s.get('durationFrames')} 帧 <1(不足一帧的段不可上轨)")
            if s.get("durationUs") != s.get("endUs", 0) - s.get("startUs", 0):
                errs.append(f"{sid}:durationUs ≠ endUs − startUs(时长账不平)")
            if s.get("durationFrames") != s.get("endFrame", 0) - s.get("startFrame", 0):
                errs.append(f"{sid}:durationFrames ≠ endFrame − startFrame(帧账不平)")
            for b in ("start", "end"):
                k, u = s.get(f"{b}Frame"), s.get(f"{b}Us")
                if k is None or u is None:
                    errs.append(f"{sid}:缺 {b}Frame/{b}Us(计划必须帧/微秒双记法)")
                elif abs(u - frame_to_us(int(k), fps)) > FRAME_EPS_US:
                    errs.append(f"{sid}:{b} 不在帧网格上:{u}μs vs 帧 {k} "
                                f"(fps={fps:g},偏差 {abs(u - frame_to_us(int(k), fps))}μs)")
            if s.get("sourceDurationUs", 0) < 0:
                errs.append(f"{sid}:sourceDurationUs 为负")
            if s.get("src") and not Path(s["src"]).is_file():
                errs.append(f"{sid}:素材不存在:{s['src']}")
            if prev_end is not None and s.get("startUs", 0) < prev_end:
                errs.append(f"{tid}:{sid} 与前段重叠(起点 {s.get('startUs')} < 前段终点 {prev_end})")
            prev_end = s.get("endUs")
        if t.get("role") == "main":
            ordered = segs
            if ordered and ordered[0]["startFrame"] != 0:
                errs.append(f"主轨首段不从 0 开始:startFrame={ordered[0]['startFrame']}")
            for a, b in zip(ordered, ordered[1:]):
                if b["startUs"] > a["endUs"]:
                    errs.append(f"主轨黑场间隙:{a['planSegId']}→{b['planSegId']} 之间空 "
                                f"{b['startUs'] - a['endUs']}μs(帧 {b['startFrame'] - a['endFrame']});"
                                "转场不产生间隙,主轨必须无缝铺满")
            if ordered:
                last = ordered[-1]
                if last["endFrame"] != plan.get("durationFrames"):
                    errs.append(f"主轨未铺到时间线末帧:末端帧 {last['endFrame']} vs "
                                f"计划时长 {plan.get('durationFrames')} 帧")
                main_sum = sum(s["durationFrames"] for s in ordered)
                expect = (plan.get("expects") or {}).get("mainDurationFrames")
                if expect is not None and main_sum != int(expect):
                    errs.append(f"主轨片段时长和 {main_sum} 帧 ≠ 预期 {expect} 帧")
    max_frame = max((s["endFrame"] for t in tracks
                     if t["kind"] in ("video", "audio") for s in t.get("segments") or []),
                    default=0)
    if plan.get("durationFrames") != max_frame:
        errs.append(f"计划时长 {plan.get('durationFrames')} 帧 ≠ 视频/音频最末帧 {max_frame}")
    if plan.get("durationUs") != frame_to_us(max_frame, fps):
        errs.append(f"计划时长 μs 与帧数不一致:{plan.get('durationUs')} vs "
                    f"帧 {max_frame} @fps={fps:g}")
    expects = plan.get("expects") or {}
    if "trackCount" in expects and expects["trackCount"] != len(tracks):
        errs.append(f"轨道数不符计划:实有 {len(tracks)},计划声明 {expects['trackCount']}")
    if "segmentCount" in expects and expects["segmentCount"] != total_segs:
        errs.append(f"片段总数不符计划:实有 {total_segs},计划声明 {expects['segmentCount']}")
    return errs


# ---------------------------------------------------------------- 映射表(--dry-run)

def format_mapping_table(doc: dict, plan: dict) -> str:
    """「IR 片段 → 草稿片段」人读映射表;--dry-run 只打印不落盘。"""
    fps = float(plan["fps"])
    lines = [
        f"草稿计划映射(IR → 剪映 {plan['target']['version']} 草稿)· "
        f"fps={fps:g} · 画布 {plan['canvas']['width']}x{plan['canvas']['height']} · "
        f"时长 {plan['durationUs'] / 1e6:.3f}s({plan['durationFrames']} 帧)",
        "-" * 100,
        f"{'IR 片段':<34}→ {'轨·段':<12}{'时间线(帧 / 秒)':<26}素材",
    ]
    text_rows: list[str] = []
    for t in plan["tracks"]:
        for s in t["segments"]:
            ref = s.get("irRef") or {}
            if t["role"] == "subtitle":
                text_rows.append(f"{'subtitle[' + str(ref.get('index', '?')) + ']':<34}→ "
                                 f"{s['planSegId']:<12}"
                                 f"帧[{s['startFrame']}–{s['endFrame']}) "
                                 f"{s['startUs'] / 1e6:.3f}–{s['endUs'] / 1e6:.3f}s  "
                                 f"「{s.get('text', '')[:16]}」")
                continue
            ir_where = (f"tracks[{ref.get('track')}].clips[{ref.get('clip')}]"
                        if "track" in ref else str(ref.get("ir", "bgm")))
            row = (f"{ir_where} {ref.get('startMs', '?')}–"
                   f"{ref.get('startMs', 0) + ref.get('durationMs', 0)}ms".ljust(34)
                   + f"→ {s['planSegId']:<12}"
                   f"帧[{s['startFrame']}–{s['endFrame']}) "
                   f"{s['startUs'] / 1e6:.3f}–{s['endUs'] / 1e6:.3f}s  "
                   f"{Path(s['src']).name if s['src'] else '(空)'}")
            extra = []
            if s.get("speed", 1.0) != 1.0:
                extra.append(f"speed={s['speed']:g}")
            if s.get("volume", 1.0) != 1.0:
                extra.append(f"vol={s['volume']:.3f}")
            if s.get("transitionIn"):
                extra.append(f"转场←{s['transitionIn']['irType']}({s['transitionIn']['map']} "
                             f"{s['transitionIn']['durationUs']}μs)")
            if s.get("fadeUs"):
                extra.append(f"淡入淡出 in={s['fadeUs']['in']}us out={s['fadeUs']['out']}us")
            if s.get("transform"):
                tv = s["transform"]
                extra.append(f"pos=({tv['x']:g},{tv['y']:g}) scale={tv['scale']:g}")
            if extra:
                row += "  [" + ";".join(extra) + "]"
            lines.append(row)
    if text_rows:
        lines.append(f"  (字幕 T1 共 {len(text_rows)} 条)")
        lines.extend(text_rows[:5])
        if len(text_rows) > 5:
            lines.append(f"  …另有 {len(text_rows) - 5} 条字幕映射略(全部写入草稿)")
    if plan.get("compileErrors"):
        lines.append("门禁错误:")
        lines.extend(f"  ✗ {e}" for e in plan["compileErrors"])
    if plan.get("warnings"):
        lines.append("编译警告(不影响门禁):")
        lines.extend(f"  ! {w}" for w in plan["warnings"])
    return "\n".join(lines)


# ---------------------------------------------------------------- 写草稿

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


def build_draft_from_plan(plan: dict, name: str, cfg: dict, warnings: list[str]) -> dict:
    """草稿计划 → 剪映 5.9 草稿文件。只做「计划 → 库调用」的机械翻译,不再含映射决策。"""
    template = Path(__file__).parents[1] / "templates" / "jy59_empty_draft.json"
    script = ScriptFile.load_template(str(template))
    script.width = plan["canvas"]["width"]
    script.height = plan["canvas"]["height"]
    script.fps = float(plan["fps"])
    cw, chh = plan["canvas"]["width"], plan["canvas"]["height"]

    text_style: dict | None = None
    for t in plan["tracks"]:
        if t["kind"] == "text" and t["segments"]:
            try:
                ratio = rs_common.ratio_for_canvas(cw, chh)
            except ValueError:
                ratio = "9x16"
            text_style = {
                "style": TextStyle(size=SUB_SIZE[ratio], bold=True, color=(1.0, 1.0, 1.0), align=1),
                "border": TextBorder(color=(0.0, 0.0, 0.0), width=25.0),
                "clip": ClipSettings(transform_y=-0.8),
            }

    for t in plan["tracks"]:
        tid, kind = t["trackId"], t["kind"]
        script.add_track(KIND_TO_TRACK[kind], tid)
        prev_seg = None
        for s in t["segments"]:
            target = Timerange(int(s["startUs"]), int(s["durationUs"]))
            if kind == "video":
                mat = VideoMaterial(s["src"])
                kw = dict(source_timerange=Timerange(int(s["sourceInUs"]), int(s["sourceDurationUs"])),
                          speed=float(s.get("speed", 1.0)), volume=float(s.get("volume", 1.0)))
                tv = s.get("transform")
                if tv:
                    kw["clip_settings"] = ClipSettings(
                        scale_x=tv["scale"], scale_y=tv["scale"],
                        transform_x=(tv["x"] - 0.5) * cw / (cw / 2),
                        transform_y=(tv["y"] - 0.5) * chh / (chh / 2))
                seg = VideoSegment(mat, target, **kw)
                script.add_segment(seg, tid)
                tr_in = s.get("transitionIn")
                if tr_in and prev_seg is not None:
                    prev_seg.add_transition(getattr(TransitionType, tr_in["map"]),
                                            duration=int(tr_in["durationUs"]))
                fd = s.get("fadeUs")
                if fd:
                    seg.add_fade(int(fd["in"]), int(fd["out"]))
                prev_seg = seg
            elif kind == "audio":
                mat = AudioMaterial(s["src"])
                seg = AudioSegment(mat, target, volume=float(s.get("volume", 1.0)))
                script.add_segment(seg, tid)
                prev_seg = seg
            else:
                st = text_style or {}
                seg = TextSegment(s["text"], target, style=st.get("style"),
                                  border=st.get("border"), clip_settings=st.get("clip"))
                script.add_segment(seg, tid)

    script.duration = int(plan["durationUs"])

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
            "segments": sum(len(t["segments"]) for t in plan["tracks"])}


def verify_written_draft(content_path: Path, plan: dict) -> list[str]:
    """落盘后复核:草稿文件结构必须与草稿计划一致(J3 的写后回读闸)。"""
    errs: list[str] = []
    cj = json.loads(Path(content_path).read_text(encoding="utf-8"))
    dtracks = cj.get("tracks") or []
    ptracks = plan.get("tracks") or []
    if len(dtracks) != len(ptracks):
        errs.append(f"轨道数不符:草稿 {len(dtracks)} vs 计划 {len(ptracks)}")
    else:
        for pt, dt in zip(ptracks, dtracks):
            n_plan, n_draft = len(pt.get("segments") or []), len(dt.get("segments") or [])
            if n_plan != n_draft:
                errs.append(f"轨道 {pt['trackId']} 片段数不符:草稿 {n_draft} vs 计划 {n_plan}")
    if int(cj.get("duration") or 0) != int(plan.get("durationUs") or 0):
        errs.append(f"时长不符:草稿 {cj.get('duration')}μs vs 计划 {plan.get('durationUs')}μs")
    return errs


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--name", default=None)
    ap.add_argument("--subtitles", default=None, help="字幕源(tts manifest/transcript json),覆盖 IR")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只编译草稿计划并打印「IR 片段 → 草稿片段」映射表(人读),"
                         "不写任何文件、不查剪映进程;门禁照跑,失败仍非零退出")
    ap.add_argument("--open", action="store_true", help="生成后启动剪映 5.9")
    a = ap.parse_args()
    p = Path(a.project)
    if not p.is_file():
        return emit(False, "NO_PROJECT", f"IR 不存在:{p}", exit_code=2)
    doc = json.loads(p.read_text(encoding="utf-8"))
    if a.subtitles:
        doc.setdefault("subtitle", {})["source"] = a.subtitles
    cfg = load_config()
    if not cfg.get("jianying59"):
        return emit(False, "NO_CONFIG",
                    "config.json 缺 jianying59 段 —— 本工具只写剪映 5.9 明文草稿,"
                    "11.3+ 加密草稿永不读写", exit_code=3)
    warnings: list[str] = []
    plan = compile_draft_plan(doc, p, cfg, warnings)
    gates = plan_gates(plan)
    if a.dry_run:
        print(format_mapping_table(doc, plan))
        if gates:
            return emit(False, "PLAN_GATE_FAIL",
                        f"草稿计划门禁未过({len(gates)} 项),详情见上方映射表:"
                        f"{gates[0]}",
                        {"gates": gates, "warnings": warnings}, exit_code=4)
        return emit(True, "JY_PLAN_DRY_RUN",
                    f"草稿计划编译通过:{len(plan['tracks'])} 轨 / "
                    f"{sum(len(t['segments']) for t in plan['tracks'])} 段 / "
                    f"{plan['durationUs'] / 1e6:.3f}s(未写任何文件)",
                    {"plan": {"fps": plan["fps"], "canvas": plan["canvas"],
                              "durationUs": plan["durationUs"],
                              "durationFrames": plan["durationFrames"],
                              "expects": plan["expects"],
                              "tracks": [{"trackId": t["trackId"], "kind": t["kind"],
                                          "role": t["role"], "segments": len(t["segments"])}
                                         for t in plan["tracks"]]},
                    "warnings": warnings})
    if gates:
        return emit(False, "PLAN_GATE_FAIL",
                    f"草稿计划门禁未过({len(gates)} 项),拒绝写草稿:{';'.join(gates[:3])}",
                    {"gates": gates, "warnings": warnings}, exit_code=4)
    assert_jianying_closed()
    name = a.name or f"cutflow_{time.strftime('%m%d_%H%M')}"
    data = build_draft_from_plan(plan, name, cfg, warnings)
    write_errs = verify_written_draft(Path(data["draft_dir"]) / "draft_content.json", plan)
    if write_errs:
        return emit(False, "DRAFT_GATE_FAIL",
                    f"落盘草稿与计划不符(拒绝声明成功):{';'.join(write_errs)}",
                    {"gates": write_errs, "warnings": warnings,
                     "draft_dir": data["draft_dir"]}, exit_code=4)
    if a.open:
        subprocess.Popen(["cmd", "/c", "start", "", cfg["jianying59"]["exe"]],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    data["warnings"] = warnings
    data["gates"] = {"trackCount": plan["expects"]["trackCount"],
                     "segmentCount": plan["expects"]["segmentCount"],
                     "durationFrames": plan["durationFrames"], "all": "PASS"}
    return emit(True, "DRAFT_OK", f"草稿已生成:{data['draft_dir']}", data)


if __name__ == "__main__":
    sys.exit(main())
