"""CutFlow 环境体检:检查所有依赖并输出 JSON 清单。用法:python rs_doctor.py [--json]"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit, load_config, run, ffprobe_bin  # noqa: E402


def _check(name: str, ok: bool, detail: str, fatal: bool = True) -> dict:
    return {"name": name, "ok": ok, "fatal": fatal, "detail": detail}


def main() -> int:
    checks: list[dict] = []
    try:
        cfg = load_config()
    except SystemExit:
        print(); return 3

    ff = Path(cfg["ffmpeg_dir"]) / "ffmpeg.exe"
    checks.append(_check("ffmpeg", ff.is_file(), str(ff)))
    fp = Path(cfg["ffmpeg_dir"]) / "ffprobe.exe"
    checks.append(_check("ffprobe", fp.is_file(), str(fp)))

    # ASR 服务探活(非致命:可按需拉起)
    try:
        import urllib.request
        with urllib.request.urlopen(cfg["asr"]["url"] + "/health", timeout=3) as r:
            info = r.read().decode()
        checks.append(_check("asr_service", '"ok"' in info, f"{cfg['asr']['url']} {info[:80]}", fatal=False))
    except Exception as exc:
        checks.append(_check("asr_service", False, f"{cfg['asr']['url']} 不可达:{exc}", fatal=False))

    # TTS 引擎探活(非致命;4xx 也算存活——仅参数缺失)
    try:
        import urllib.request, urllib.error
        try:
            urllib.request.urlopen(urllib.request.Request(cfg["tts"]["url"] + "/tts", data=b"{}",
                                    headers={"Content-Type": "application/json"}), timeout=3)
            checks.append(_check("tts_engine", True, f"{cfg['tts']['url']} 应答", fatal=False))
        except urllib.error.HTTPError as exc:
            checks.append(_check("tts_engine", exc.code < 500, f"{cfg['tts']['url']} HTTP {exc.code}(服务在)", fatal=False))
    except Exception as exc:
        checks.append(_check("tts_engine", False, f"{cfg['tts']['url']} 不可达({type(exc).__name__}),需先启动 api_v2.py", fatal=False))

    for key in ("ocr_exe", "vqa_python", "vqa_cli"):
        checks.append(_check(key, Path(cfg[key]).is_file(), cfg[key]))

    jy = cfg.get("jianying59", {})
    checks.append(_check("jianying59_exe", Path(jy.get("exe", "")).is_file(), jy.get("exe", "")))
    checks.append(_check("jianying59_draft_root", Path(jy.get("draft_root", "")).is_dir(), jy.get("draft_root", "")))

    from rs_common import resolve_voice
    try:
        card = resolve_voice(cfg["tts"]["default_voice"], cfg)
        checks.append(_check("tts_voice_card", True, str(card)))
    except Exception as exc:
        checks.append(_check("tts_voice_card", False, str(exc)))

    disabled = {v.lower() for v in cfg["tts"].get("disabled_voices", [])}
    if cfg["tts"]["default_voice"].lower() in disabled:
        checks.append(_check("default_voice_enabled", False, f"默认音色 {cfg['tts']['default_voice']} 在 disabled_voices 中"))

    fatal_bad = [c for c in checks if c["fatal"] and not c["ok"]]
    return emit(all(c["ok"] for c in checks if c["fatal"]),
                "DOCTOR_OK" if not fatal_bad else "DOCTOR_FAIL",
                f"{len(checks)-len(fatal_bad)}/{len(checks)} 项通过(致命失败 {len(fatal_bad)})",
                {"checks": checks},
                exit_code=0 if not fatal_bad else 3)


if __name__ == "__main__":
    sys.exit(main())
