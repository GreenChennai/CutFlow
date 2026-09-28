"""图片/镜头感知(第三册 T3.6/T3.8):OCR+VQA 单图备选件 + 三级感知预算逐镜描述。

用法:
  rs_sense.py <图片> --out <目录> [--prompt ...] [--force]
  rs_sense.py shots --vision <shots.json> --out <工程根|目录>
            [--tier L0|L1|L2] [--budget-sec 120] [--max-shots 0] [--media <视频>]

三级感知预算(T3.8,rules/sense.md 同口径;机械强制):
  L0 结构  纯读 shots.json 结构,零网络零模型零解码(代码路径保证,测试可断言);
  L1 标签  本地模型(cv2 Haar 人脸/安全带占用),可选件,默认开、缺失显式降级;
  L2 描述  VLM 逐镜一句 ≤20 字中文描述,默认关(--tier L2 显式开),时间预算
           --budget-sec 机械强制:超预算**停并留痕**(stopped="budget",不静默降级)。

L2 结果并进 05_时间线工程/vision.json 的 caption/tags 字段(单一产物,T3.7),
并经 rs_vision.validate_vision 校验后落盘;VLM 缺失 → 显式降级留痕,CI 无 VLM 全绿。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import (EXIT_INPUT, EXIT_OK, emit, ffmpeg_bin, load_config,  # noqa: E402
                       run, write_text_atomic)
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量

# ---------------------------------------------------------------- 参数常量

CAPTION_MAX_CHARS = 20        # T3.6 判据:每镜描述 ≤20 字
DEFAULT_TIER = "L1"           # 三级感知默认档:L0/L1 开,L2 关(预算显式开)
DEFAULT_BUDGET_SEC = 120.0    # L2 时间预算缺省(超限停并留痕)
FRAME_W = 480                 # 逐镜抽帧宽度(VLM 输入,小图省时)
TAG_RE = re.compile(r"(主体|场景|构图)\s*[:：]\s*(\S{1,12})")


def ocr_image(image: Path, out_txt: Path, cfg: dict) -> str:
    p = run([cfg["ocr_exe"], str(image), "-o", str(out_txt)], timeout=120)
    if p.returncode != 0 or not out_txt.is_file():
        return ""
    return out_txt.read_text(encoding="utf-8-sig").strip()


def vqa_image(image: Path, out_txt: Path, cfg: dict, prompt: str) -> str:
    # 优先 vqa_exe(Rust 直连模式,fetch_deps.py vqa 部署);回退 vqa_python+vqa_cli(本地项目模式)
    exe = cfg.get("vqa_exe")
    if exe and Path(exe).is_file():
        p = run([exe, "--image", str(image), "--prompt", prompt, "--no-think"], timeout=300)
        if p.returncode != 0:
            return ""
        return (p.stdout or b"").decode("utf-8", errors="ignore").strip()
    if cfg.get("vqa_python") and cfg.get("vqa_cli"):
        p = run([cfg["vqa_python"], cfg["vqa_cli"], str(image),
                 "--prompt", prompt, "--no-think", "-o", str(out_txt)], timeout=300)
        if p.returncode == 0 and out_txt.is_file():
            return out_txt.read_text(encoding="utf-8-sig").strip()
    return ""


# ---------------------------------------------------------------- 单图模式(原备选件,ADR-0008)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", nargs="?", default=None)
    ap.add_argument("--shots", action="store_true", help="逐镜描述模式(T3.6,--vision 给 shots.json)")
    ap.add_argument("--vision", default=None, help="shots.json 路径(逐镜描述模式)")
    ap.add_argument("--media", default=None, help="逐镜描述模式的素材视频(缺省从 manifest 推)")
    ap.add_argument("--tier", default=DEFAULT_TIER, choices=["L0", "L1", "L2"],
                    help="感知预算档:T0 结构零模型 / L1 本地标签 / L2 VLM 描述(默认关)")
    ap.add_argument("--budget-sec", dest="budget_sec", type=float, default=DEFAULT_BUDGET_SEC,
                    help="L2 时间预算秒(超限停并留痕,不静默降级)")
    ap.add_argument("--max-shots", dest="max_shots", type=int, default=0,
                    help="L2 最多描述的镜头数(0=不限,只受预算)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--prompt", default="用中文详细描述这张图片的内容:主体、文字、风格、配色。")
    ap.add_argument("--force", action="store_true", help="跳过 force_local 检查强制执行")
    a = ap.parse_args()

    if a.shots or a.vision:
        return cmd_shots(a)
    if not a.image:
        return emit(False, "NO_INPUT", "需要 <图片> 或 --shots --vision <shots.json>",
                    exit_code=EXIT_INPUT)
    if not a.out:
        return emit(False, "BAD_INPUT", "单图模式需要 --out <目录>", exit_code=EXIT_INPUT)
    image = Path(a.image)
    if not image.is_file():
        return emit(False, "NO_IMAGE", f"图片不存在:{image}", exit_code=2)
    cfg = load_config()
    if not a.force and not cfg.get("sense", {}).get("force_local", False):
        print("提示:rs_sense 是备选件(ADR-0008)——Agent 自带视觉时应优先自己看图;")
        print("      批量图处理/无视觉环境/需要留档时才用本工具;或 config.sense.force_local=true。")
    cfg.setdefault("sense", {})
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ocr_txt = ocr_image(image, out_dir / f"{image.stem}_ocr.txt", cfg)
    vqa_txt = vqa_image(image, out_dir / f"{image.stem}_vqa.txt", cfg, a.prompt)
    result = {"image": str(image), "ocr": ocr_txt, "vqa": vqa_txt}
    (out_dir / f"{image.stem}_sensed.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return emit(True, "SENSE_OK", f"OCR {len(ocr_txt)} 字 / VQA {len(vqa_txt)} 字", result)


# ---------------------------------------------------------------- 逐镜模式(T3.6/T3.8)

def _extract_frame(media: Path, t_ms: int, png: Path, cfg: dict) -> bool:
    """镜头中点抽帧(ffmpeg;中文路径走宽字符 API,rs_vision 同纪律)。"""
    png.parent.mkdir(parents=True, exist_ok=True)
    p = run([ffmpeg_bin(cfg), "-v", "error", "-y", "-ss", f"{t_ms / 1000.0:.3f}",
             "-i", str(media), "-frames:v", "1", "-vf", f"scale={FRAME_W}:-2",
             str(png)], timeout=120)
    return p.returncode == 0 and png.is_file()


def _parse_tags(text: str) -> dict:
    """VLM 输出 → {主体,场景,构图} 标签(解析不出→空,留 note 不硬造)。"""
    return {m.group(1): m.group(2).rstrip("。,，") for m in TAG_RE.finditer(text)}


def cmd_shots(a) -> int:
    shots_p = Path(a.vision) if a.vision else None
    if shots_p is None or not shots_p.is_file():
        return emit(False, "NO_SHOTS", "逐镜模式需要 --vision <shots.json>",
                    exit_code=EXIT_INPUT)
    try:
        doc = json.loads(shots_p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return emit(False, "BAD_SHOTS", f"shots.json 解析失败:{exc}", exit_code=EXIT_INPUT)
    shots = doc.get("shots") or []
    if not shots:
        return emit(False, "NO_SHOTS", "shots.json 无镜头", exit_code=EXIT_INPUT)
    cfg = load_config()
    root = shots_p.resolve().parent.parent          # shots.json → 工程根(目录约定)
    media = Path(a.media) if a.media else None
    if media is None or not media.is_file():
        media = rs_paths.resolve(root, "materials") / str(doc.get("source") or "")
    has_media = media.is_file()
    tier = a.tier
    out_dir = Path(a.out) if a.out else root
    out_dir.mkdir(parents=True, exist_ok=True)
    degrade: list[str] = []
    tiers: dict = {"L0": {"state": "READY", "note": "纯结构,零网络零模型"}}

    # ---- L0:零模型(只读结构;代码路径保证不触解码/模型,测试可断言)----
    if tier == "L0":
        result = {"mode": "shots", "tier": tier, "shotCount": len(shots),
                  "shots": [{"index": int(s["index"]), "startMs": int(s["startMs"]),
                             "endMs": int(s["endMs"]), "durMs": int(s["durMs"])}
                            for s in shots],
                  "tiers": tiers, "degradeReasons": []}
        return emit(True, "SENSE_SHOTS_OK",
                    f"L0 结构档:{len(shots)} 镜(零模型零解码)", result)

    # ---- L1:本地标签(cv2;缺失显式降级)----
    l1: dict[int, dict] = {}
    if tier in ("L1", "L2"):
        if not has_media:
            tiers["L1"] = {"state": "MISSING", "degraded": True,
                           "note": "素材缺失,L1 标签未生成"}
            degrade.append("L1:素材缺失")
        else:
            try:
                import rs_vision  # noqa: PLC0415 — 感知层共享库
                l1, l1_tiers = rs_vision.l1_shot_fields(media, shots, cfg)
                tiers["L1"] = l1_tiers.get("L1", {})
                if tiers["L1"].get("degraded"):
                    degrade.append(f"L1:{tiers['L1'].get('note')}")
            except (OSError, ValueError) as exc:
                tiers["L1"] = {"state": "MISSING", "degraded": True,
                               "note": f"L1 失败({exc}),显式降级"}
                degrade.append(f"L1:{exc}")

    # ---- L2:VLM 逐镜描述(默认关;时间预算机械强制,超限停并留痕)----
    captions: dict[int, dict] = {}
    stopped = ""
    if tier == "L2":
        has_vlm = bool(cfg.get("vqa_exe") or (cfg.get("vqa_python") and cfg.get("vqa_cli")))
        if not has_vlm:
            tiers["L2"] = {"state": "MISSING", "degraded": True,
                           "note": "VLM 组件缺失(config 缺 vqa_exe/vqa_python+vqa_cli);"
                                   "L2 描述未生成(显式降级,不静默)"}
            degrade.append("L2:VLM 组件缺失")
        elif not has_media:
            tiers["L2"] = {"state": "MISSING", "degraded": True,
                           "note": "素材缺失,无法抽帧,L2 未生成"}
            degrade.append("L2:素材缺失")
        else:
            deadline = time.monotonic() + max(float(a.budget_sec), 0.0)
            done = 0
            for s in shots:
                if a.max_shots and done >= a.max_shots:
                    stopped = "max-shots"
                    break
                if time.monotonic() > deadline:
                    stopped = "budget"          # 超预算:停并留痕,不静默降级
                    break
                i = int(s["index"])
                mid = (int(s["startMs"]) + int(s["endMs"])) // 2
                png = out_dir / f"_shot_{i:04d}.png"
                if not _extract_frame(media, mid, png, cfg):
                    captions[i] = {"caption": "", "tags": {},
                                   "note": "抽帧失败,本镜无描述(留痕)"}
                    degrade.append(f"L2:镜 {i} 抽帧失败")
                    continue
                txt = vqa_image(png, out_dir / f"_shot_{i:04d}_vqa.txt", cfg,
                                "用不超过18个字描述画面,然后另起一行依次给出"
                                "主体:xx 场景:xx 构图:xx(近景/中景/远景)。")
                lines = [x.strip() for x in txt.splitlines() if x.strip()]
                cap = lines[0][:CAPTION_MAX_CHARS] if lines else ""
                captions[i] = {"caption": cap, "tags": _parse_tags(txt)}
                done += 1
            tiers["L2"] = {"state": "READY" if done else "FAILED",
                           "degraded": bool(stopped or done < len(shots)),
                           "done": done, "total": len(shots),
                           "budgetSec": float(a.budget_sec)}
            if stopped == "budget":
                tiers["L2"]["note"] = (f"时间预算 {a.budget_sec}s 超限,完成 {done}/{len(shots)}"
                                       "镜即停(留痕,不静默降级);可加 --budget-sec 续跑")
                degrade.append(f"L2:预算超限停({done}/{len(shots)})")
            elif stopped == "max-shots":
                tiers["L2"]["note"] = f"--max-shots {a.max_shots} 封顶,完成 {done} 镜"
            elif done == 0 and not degrade:
                tiers["L2"]["degraded"] = True

    # ---- L2 描述并进 vision.json(T3.7 单一产物;校验后落盘)----
    if captions:
        vis_p = rs_paths.resolve(root, "timeline") / "vision.json"
        try:
            vis = json.loads(vis_p.read_text(encoding="utf-8")) if vis_p.is_file() else None
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            vis = None
        if vis and vis.get("shots"):
            for row in vis["shots"]:
                cap = captions.get(int(row["index"]))
                if cap:
                    row["caption"] = cap.get("caption") or ""
                    if cap.get("tags"):
                        row["tags"] = cap["tags"]
                    if cap.get("note"):
                        row["captionNote"] = cap["note"]
            vis.setdefault("tiers", {})["L2"] = tiers.get("L2", {})
            for d in degrade:
                if d.startswith("L2") and d not in vis.setdefault("degradeReasons", []):
                    vis["degradeReasons"].append(d)
            import rs_vision  # noqa: PLC0415
            errs = rs_vision.validate_vision(vis)
            if errs:
                return emit(False, "VISION_SCHEMA",
                            "vision.json 并入 L2 后校验失败:" + ";".join(errs[:5]),
                            {"errors": errs}, exit_code=4)
            write_text_atomic(vis_p, json.dumps(vis, ensure_ascii=False,
                                                separators=(",", ":")))
        else:
            degrade.append("L2:vision.json 缺失,描述未并入(先 rs_vision.py vision)")

    result = {"mode": "shots", "tier": tier, "shotCount": len(shots),
              "captions": {str(k): v for k, v in captions.items()},
              "l1": {str(k): {kk: vv for kk, vv in v.items() if kk != "_band"}
                     for k, v in l1.items()}, "tiers": tiers, "degradeReasons": degrade,
              "stopped": stopped}
    n_cap = sum(1 for v in captions.values() if v.get("caption"))
    msg = (f"tier={tier} 镜={len(shots)}"
           + (f" L1镜={len(l1)}" if l1 else "")
           + (f" 描述={n_cap}/{len(shots)}" if captions else "")
           + (f" 停因={stopped}" if stopped else "")
           + (f" 降级={len(degrade)}" if degrade else ""))
    return emit(True, "SENSE_SHOTS_OK", msg, result)


if __name__ == "__main__":
    sys.exit(main())
