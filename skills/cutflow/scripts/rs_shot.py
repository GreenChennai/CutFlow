"""镜头切分(M8 vlog 能力 vision.shot 的 detector,ADR-0047)。

用法:
  rs_shot.py detect <视频> --out <工程根> [--deep] [--force]
  rs_shot.py                   # 无参 = 工程模式(挂载器契约:cwd=工程根,
                               #   自动选 01_原始素材里第一条视频素材 → 写 shots.json)

双档三态(懒加载体系 rs_fetchable,ADR-0049;本脚本只探测、绝不下载):
  READY    scenedetect probe 通过 → 子进程调 PySceneDetect ContentDetector
  MISSING  降级档 frame-diff:ffmpeg `select='gt(scene,T)'` 场景滤镜(降采样流上判,
           十分钟素材 ≤0.05× 实时预算,方案 §6.2)
  --deep   TransNetV2(深度档,硬切+溶解均准)未登记进懒加载清单(诚实纪律:不声明
           就不假装有)→ 探测不到时如实留痕并退回当前可用档

产物 04_粗剪决策/shots.json:
  {"shots": [{index, startMs, endMs, durMs}], "transitions": [{atMs, kind}],
   "engine", "degraded", "degradeReason"?, "missingComponent"?, "source", "tiers"}
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import (EXIT_INPUT, EXIT_OK, emit, ffmpeg_bin, load_config,  # noqa: E402
                       media_duration_s, run, write_text_atomic)
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量
import rs_fetchable  # noqa: E402  — 三态探测(只 state(),绝不触发下载)

# ---------------------------------------------------------------- 参数常量(描述符 params 同源,改一处必改两处)

SCENE_THRESHOLD = 0.30      # 场景切换判据(ffmpeg scene 分数 0–1;scenedetect ContentDetector 同域)
MIN_SHOT_MS = 250           # 最短镜头(附录 C 示例同值;更短并入前一镜,防闪帧成镜)
SAMPLE_FPS = 10             # 降级档降采样帧率(切点精度 ±100ms,够粗剪用)
DOWNSCALE_W = 160           # 降采样宽度(scene 分数在小图上同样有效,解码省时)
SHOWINFO_RE = re.compile(r"pts_time:([\d.]+)")

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts", ".flv"}


def shots_path(project: Path) -> Path:
    """04_粗剪决策/shots.json(单一落点)。"""
    return rs_paths.resolve(project, "cut") / "shots.json"


# ---------------------------------------------------------------- 降级档 frame-diff

def detect_frame_diff(media: Path, cfg: dict, threshold: float = SCENE_THRESHOLD
                      ) -> tuple[list[int], float]:
    """ffmpeg 场景滤镜取切点:fps 降采样 → select(scene>T) → showinfo 读 pts_time。

    返回 (切点 ms 列表〔不含 0〕, 时长秒)。showinfo 在 stderr,rs_common.run 的
    UTF-8 + replace 解码对它无损(纯文本)。
    """
    total_s = 0.0
    try:
        total_s = media_duration_s(media, cfg)
    except SystemExit:                          # ffprobe die() → 按无时长继续
        total_s = 0.0
    vf = (f"fps={SAMPLE_FPS},scale={DOWNSCALE_W}:-2,"
          f"select='gt(scene,{threshold:.2f})',showinfo")
    p = run([ffmpeg_bin(cfg), "-hide_banner", "-nostats", "-i", str(media),
             "-vf", vf, "-f", "null", "-"], timeout=1800)
    if p.returncode != 0:
        return [], total_s
    cuts_ms = sorted({int(round(float(m.group(1)) * 1000))
                      for m in SHOWINFO_RE.finditer(p.stderr or "")})
    return cuts_ms, total_s


def cuts_to_shots(cuts_ms: list[int], total_ms: int) -> list[dict]:
    """切点 → 镜头列表;<MIN_SHOT_MS 的镜头并入前一镜(防闪帧成镜,附录 C minShotMs)。"""
    bounds = [0] + [c for c in cuts_ms if 0 < c < total_ms] + [max(total_ms, 1)]
    shots: list[dict] = []
    for i in range(len(bounds) - 1):
        start, end = bounds[i], bounds[i + 1]
        if shots and end - start < MIN_SHOT_MS:
            shots[-1]["endMs"] = end            # 过短 → 并入前一镜
            continue
        shots.append({"index": len(shots), "startMs": int(start), "endMs": int(end),
                      "durMs": int(end - start)})
    for i, s in enumerate(shots):               # 并镜后重排 index
        s["index"] = i
    return shots


# ---------------------------------------------------------------- READY 档(PySceneDetect;测试 mock 本函数)

def _ready_shots(media: Path, out_json: Path) -> subprocess.CompletedProcess:
    """子进程调主解释器的 scenedetect CLI,结果落 out_json(ms 口径与产物一致)。

    测试 mock 本函数(绝不真实装包);探测走 rs_fetchable.state("vision.shot")。
    """
    code = ("import json,sys;"
            "from scenedetect import detect, ContentDetector;"
            f"scenes=detect({str(media)!r}, ContentDetector(threshold={SCENE_THRESHOLD}));"
            f"json.dump([[[s[0].get_seconds(),s[1].get_seconds()] for s in scenes]],"
            f"open({str(out_json)!r},'w'))")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=1800)


def ready_tier(media: Path, out_root: Path, deep: bool) -> tuple[list[int] | None, dict]:
    """READY 档:probe 通过 → scenedetect 切点。返回 (切点 ms 或 None, tiers 留痕)。"""
    st = rs_fetchable.state("vision.shot")
    if st["state"] != "READY":
        tiers = {"shot": {"engine": "frame-diff", "degraded": True,
                          **{k: v for k, v in rs_fetchable.degrade_record("vision.shot").items()
                             if k != "degraded"}}}
        if deep:
            tiers["shot"]["note"] = ("--deep 的 TransNetV2 未登记进懒加载清单"
                                     "(诚实纪律:不声明不假装);已用当前可用档")
        return None, tiers
    tmp_json = shots_path(out_root).with_suffix(".infer.json")
    try:
        p = _ready_shots(media, tmp_json)
    except (OSError, subprocess.TimeoutExpired):
        return None, {"shot": {"engine": "scenedetect", "degraded": True,
                               "note": "scenedetect 子进程失败,退回 frame-diff"}}
    if p.returncode != 0 or not tmp_json.is_file():
        return None, {"shot": {"engine": "scenedetect", "degraded": True,
                               "note": "scenedetect 输出缺失,退回 frame-diff"}}
    try:
        pairs = json.loads(tmp_json.read_text(encoding="utf-8"))
        cuts = sorted({int(round(float(end_s) * 1000)) for _, end_s in pairs})
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, TypeError, ValueError):
        return None, {"shot": {"engine": "scenedetect", "degraded": True,
                               "note": "scenedetect 输出解析失败,退回 frame-diff"}}
    tiers = {"shot": {"engine": "scenedetect", "degraded": False}}
    if deep:
        tiers["shot"]["note"] = "--deep 的 TransNetV2 未登记进懒加载清单;已用 scenedetect"
    return cuts, tiers


# ---------------------------------------------------------------- 组装与产物

def analyze(media: Path, out_root: Path, *, deep: bool = False, force: bool = False
            ) -> tuple[dict, int]:
    """单素材全流程 → shots.json。返回 (产物 dict, 退出码)。"""
    out_path = shots_path(out_root)
    cfg = load_config()
    cuts, tiers = ready_tier(media, out_root, deep)
    engine = "scenedetect"
    if cuts is None:                             # 降级档 frame-diff
        cuts, total_s = detect_frame_diff(media, cfg)
        engine = "frame-diff"
        doc = {"shots": cuts_to_shots(cuts, int(total_s * 1000)),
               "transitions": [{"atMs": c, "kind": "cut"} for c in cuts],
               "engine": engine, "source": media.name,
               "durationSec": round(total_s, 3), "tiers": tiers}
        doc.update(rs_fetchable.degrade_record("vision.shot"))
    else:
        total_s = 0.0
        try:
            total_s = media_duration_s(media, cfg)
        except SystemExit:
            total_s = 0.0
        doc = {"shots": cuts_to_shots(cuts, int(total_s * 1000)),
               "transitions": [{"atMs": c, "kind": "cut"} for c in cuts],
               "engine": engine, "source": media.name,
               "durationSec": round(total_s, 3), "tiers": tiers,
               "degraded": False}
    _write(out_path, doc)
    return doc, EXIT_OK


def _write(out_path: Path, doc: dict) -> None:
    write_text_atomic(out_path, json.dumps(doc, ensure_ascii=False, indent=1))


def _pick_project_video(root: Path) -> Path | None:
    """工程模式选素材:manifest probe 通过的视频优先,扩展名兜底。"""
    man = rs_paths.manifest_json(root)
    items: list[dict] = []
    if man.is_file():
        try:
            items = json.loads(man.read_text(encoding="utf-8")).get("items") or []
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            items = []
    mat_dir = rs_paths.resolve(root, "materials")
    for it in items:
        if it.get("probe") == "ok" and Path(str(it.get("file", ""))).suffix.lower() in VIDEO_EXTS:
            q = mat_dir / str(it.get("file", ""))
            if q.is_file():
                return q
    if mat_dir.is_dir():
        for q in sorted(mat_dir.iterdir()):
            if q.is_file() and q.suffix.lower() in VIDEO_EXTS:
                return q
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rs_shot.py",
        description="镜头切分(M8 vlog 能力):READY=scenedetect;降级=frame-diff 场景滤镜,必留痕")
    ap.add_argument("command", nargs="?", choices=["detect"], default=None,
                    help="detect=显式指定素材;缺省 = 工程模式(自动选素材,挂载器契约)")
    ap.add_argument("media", nargs="?", default=None, help="视频素材路径")
    ap.add_argument("--out", default=None, help="工程根(产物 04_粗剪决策/shots.json)")
    ap.add_argument("--deep", action="store_true",
                    help="请求深度档 TransNetV2(未登记进懒加载清单时如实留痕并退回可用档)")
    ap.add_argument("--force", action="store_true", help="工程模式下忽略产物新鲜度强制重算")
    a = ap.parse_args(argv)

    if a.command == "detect":
        if not a.media or not a.out:
            return emit(False, "BAD_INPUT", "detect 需要 <视频> 与 --out <工程根>", exit_code=EXIT_INPUT)
        media = Path(a.media)
        if not media.is_file():
            return emit(False, "NO_MEDIA", f"素材不存在:{media}", exit_code=EXIT_INPUT)
        doc, rc = analyze(media, Path(a.out), deep=a.deep, force=a.force)
        return emit(rc == EXIT_OK, "SHOTS_OK",
                    f"shots={len(doc.get('shots') or [])} engine={doc.get('engine')} "
                    f"degraded={doc.get('degraded')}", doc, exit_code=rc)
    # 工程模式(无参,挂载器/第二波 Agent 的默认入口)
    root = Path.cwd()
    out_path = shots_path(root)
    media = _pick_project_video(root)
    if media is None:
        return emit(False, "NO_MEDIA",
                    f"工程无视频素材({rs_paths.p('materials')}/ 为空或 probe 全败);未写产物",
                    exit_code=EXIT_INPUT)
    if not a.force and out_path.is_file() and out_path.stat().st_mtime >= media.stat().st_mtime:
        try:
            doc = json.loads(out_path.read_text(encoding="utf-8"))
            return emit(True, "SHOTS_CACHED",
                        f"shots.json 新于素材,跳过重算(--force 强制):shots={len(doc.get('shots') or [])}",
                        doc)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass                                # 坏产物 → 落到重算
    doc, rc = analyze(media, root, deep=a.deep, force=a.force)
    return emit(rc == EXIT_OK, "SHOTS_OK",
                f"素材={media.name} shots={len(doc.get('shots') or [])} "
                f"engine={doc.get('engine')} degraded={doc.get('degraded')}",
                doc, exit_code=rc)


if __name__ == "__main__":
    sys.exit(main())
