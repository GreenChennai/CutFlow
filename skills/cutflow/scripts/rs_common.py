"""CutFlow 公共库:CLI 结果协议 / 配置加载 / ffmpeg 调用。

所有 rs_*.py 的输出统一为 `{"ok": bool, "code": str, "message": str, "data": ...}`,
退出码:0 成功 / 2 输入错 / 3 依赖缺失 / 4 执行失败。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO_ROOT / "config.json"

EXIT_OK, EXIT_INPUT, EXIT_DEP, EXIT_EXEC = 0, 2, 3, 4


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        die(EXIT_DEP, "NO_CONFIG", f"缺少 config.json,请从 config.example.json 复制并填写:{CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def emit(ok: bool, code: str, message: str, data=None, exit_code: int = EXIT_OK) -> int:
    print(json.dumps({"ok": ok, "code": code, "message": message, "data": data},
                     ensure_ascii=False, default=str))
    return exit_code


def die(exit_code: int, code: str, message: str, data=None) -> "None":
    print(json.dumps({"ok": False, "code": code, "message": message, "data": data},
                     ensure_ascii=False, default=str))
    sys.exit(exit_code)


def ffmpeg_bin(cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    p = Path(cfg["ffmpeg_dir"]) / "ffmpeg.exe"
    return str(p) if p.is_file() else "ffmpeg"


def ffprobe_bin(cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    p = Path(cfg["ffmpeg_dir"]) / "ffprobe.exe"
    return str(p) if p.is_file() else "ffprobe"


def run(cmd: list[str], timeout: int = 3600, quiet: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          text=False if quiet is False else True)


def ffprobe_json(media: str | Path, cfg: dict | None = None) -> dict:
    p = run([ffprobe_bin(cfg), "-v", "error", "-show_format", "-show_streams",
             "-of", "json", str(media)])
    if p.returncode != 0:
        die(EXIT_EXEC, "PROBE_FAIL", f"ffprobe 失败:{(p.stderr or '')[:300]}")
    return json.loads(p.stdout)


def media_duration_s(media: str | Path, cfg: dict | None = None) -> float:
    info = ffprobe_json(media, cfg)
    return float(info.get("format", {}).get("duration") or 0)


def http_json(url: str, timeout: float = 10, method: str = "GET",
              body: dict | None = None) -> dict:
    import urllib.request
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_voice(voice: str, cfg: dict | None = None) -> Path:
    """音色名 → card.json 路径。支持:目录名 / card.name / 权重文件名关键词(koubo-test→口播声线)。"""
    cfg = cfg or load_config()
    voices = Path(cfg["tts"]["voices_dir"])
    direct = voices / voice / "card.json"
    if direct.is_file():
        return direct
    hits = []
    for card_path in voices.glob("*/card.json"):
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        hay = " ".join([card.get("name", ""), card.get("gpt", ""), card.get("sovits", "")]).lower()
        if voice.lower() == card.get("name", "").lower() or voice.lower() in hay:
            hits.append(card_path)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError(f"找不到音色卡:{voice}(voices_dir={voices})")
    raise ValueError(f"音色名 {voice} 命中多张卡:{[h.parent.name for h in hits]}")


def ensure_workdir(slug: str) -> Path:
    """视频工程工作目录固定结构(见 PLAN.md §10)。"""
    cfg = load_config()
    root = Path(cfg["workdir_root"]) / slug
    for sub in ("00_brief", "01_materials", "02_sensed/frames", "03_assets",
                "04_ai_prompts", "05_ir", "06_output"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
