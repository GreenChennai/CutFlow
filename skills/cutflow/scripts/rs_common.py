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

# 画幅唯一真相源(OPTIMIZATION-v7 #4):新增画幅只改这里 + templates/platforms.json
RATIOS: dict[str, tuple[int, int]] = {
    "9x16": (1080, 1920),      # 抖音 / 视频号
    "3x4": (1080, 1440),       # 小红书
    "16x9": (1920, 1080),      # B站 / YouTube
}


def p95(values: list[float]) -> float:
    """95 分位(小样本取上界)。各处自检报告统一口径,别再各写一遍索引式。"""
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * 0.95))]


def guard_passed(guard: dict | None) -> bool:
    """粗剪 guard 是否通过(`okByReason` 优先,兼容老工程的 `ok`)。粗剪/自检共用一套口径。"""
    g = guard or {}
    return bool(g.get("okByReason", g.get("ok")))


def canvas_for(ratio: str) -> tuple[int, int]:
    """比例 → (宽, 高);未知比例报错而不是静默猜。"""
    if ratio not in RATIOS:
        raise ValueError(f"未知比例 {ratio!r}(可选 {'/'.join(RATIOS)})")
    return RATIOS[ratio]


def ratio_for_canvas(width: int, height: int) -> str:
    """(宽, 高) → 比例;查不到时报错(旧实现是字符串比较,新画幅必然漏)。"""
    for name, (w, h) in RATIOS.items():
        if (w, h) == (int(width), int(height)):
            return name
    raise ValueError(f"画布 {width}x{height} 不对应任何已知比例({'/'.join(RATIOS)})")


def ensure_utf8() -> None:
    """把 stdout/stderr 切到 UTF-8(带 replace 兜底)。

    Windows 控制台默认 cp936,打印 `✓`/`↔`/emoji 会 UnicodeEncodeError 直接崩脚本。
    已被重定向或被测试框架替换的流没有 reconfigure → 静默跳过(绝不因它抛异常)。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 — 无 reconfigure / 已关闭 → 不阻塞
            pass


# 导入即兜底:任何 rs_*.py 只要 import rs_common,就不再因控制台编码把脚本打崩
# (实测踩坑:rs_verify 的检查名含 `↔`,GBK 控制台下 emit() 直接 UnicodeEncodeError)
ensure_utf8()


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
    hits, broken = [], []
    for card_path in voices.glob("*/card.json"):
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            broken.append(f"{card_path.parent.name}({type(exc).__name__})")   # 不静默吞掉坏卡
            continue
        hay = " ".join([card.get("name", ""), card.get("gpt", ""), card.get("sovits", "")]).lower()
        if voice.lower() == card.get("name", "").lower() or voice.lower() in hay:
            hits.append(card_path)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        extra = f";另有 {len(broken)} 张卡读取失败:{','.join(broken)}" if broken else ""
        raise FileNotFoundError(f"找不到音色卡:{voice}(voices_dir={voices}){extra}")
    raise ValueError(f"音色名 {voice} 命中多张卡:{[h.parent.name for h in hits]}")


def ensure_workdir(slug: str) -> Path:
    """视频工程工作目录固定结构(见 PLAN.md §10)。"""
    cfg = load_config()
    root = Path(cfg["workdir_root"]) / slug
    for sub in ("00_brief", "01_materials", "02_sensed/frames", "03_assets",
                "04_ai_prompts", "05_ir", "06_output"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
