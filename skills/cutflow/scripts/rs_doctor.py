"""CutFlow 环境自检:检查所有依赖,输出 JSON 或人读报告。

用法:
  python rs_doctor.py             # JSON(agent 用)
  python rs_doctor.py --report    # 人读环境自检报告
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit, ensure_utf8, load_config, ffprobe_bin, resolve_voice  # noqa: E402


def _check(name: str, ok: bool, detail: str, fatal: bool = True, group: str = "通用", hint: str = "") -> dict:
    return {"name": name, "ok": ok, "fatal": fatal, "detail": detail,
            "group": group, "hint": hint}


def probe_tts(url: str) -> tuple[bool, str]:
    import urllib.request, urllib.error
    try:
        urllib.request.urlopen(urllib.request.Request(url + "/tts", data=b"{}",
                               headers={"Content-Type": "application/json"}), timeout=3)
        return True, "应答正常"
    except urllib.error.HTTPError as exc:
        return exc.code < 500, f"HTTP {exc.code}(服务在)"
    except Exception as exc:
        return False, f"不可达({type(exc).__name__})——先启动引擎: <engine_dir>\\api_v2.py -a 127.0.0.1 -p 9885"


def probe_asr_local() -> tuple[bool, str]:
    """本地探测自带 ASR(v0.10):子进程跑 tools/fun_asr.py --probe。

    旧实现探测 config.asr.url 的 HTTP 服务(ADR-0015 之前的遗留),自带 ASR 时代
    永远显示「不可达——先运行 tools/start_asr.py」,把用户引向错误修法(用户反馈#3)。
    """
    runner = Path(__file__).resolve().parents[3] / "tools" / "fun_asr.py"
    if not runner.is_file():
        return False, f"缺自带 ASR 运行器:{runner}"
    try:
        p = subprocess.run([sys.executable, str(runner), "--probe"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, cwd=str(runner.parents[2]))
        doc = json.loads((p.stdout or "").strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001 — 自检本身绝不致命
        return False, f"探测失败({type(exc).__name__}: {exc})"
    if doc.get("ok"):
        return True, str(doc.get("message") or "就绪")
    return False, str(doc.get("message") or "无可用后端")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="打印人读环境自检报告")
    a = ap.parse_args()

    checks: list[dict] = []
    try:
        cfg = load_config()
    except SystemExit:
        return 3

    ff = Path(cfg["ffmpeg_dir"]) / "ffmpeg.exe"
    checks.append(_check("ffmpeg", ff.is_file(), str(ff), group="渲染"))
    fp = Path(cfg["ffmpeg_dir"]) / "ffprobe.exe"
    checks.append(_check("ffprobe", fp.is_file(), str(fp), group="渲染"))

    ok, msg = probe_asr_local()
    checks.append(_check("自带 FunASR", ok, msg, fatal=False,
                         group="感知服务",
                         hint="python tools/fun_asr.py --ensure(自动部署,禁止让用户手装)"))
    ok, msg = probe_tts(cfg["tts"]["url"])
    checks.append(_check("GPT-SoVITS 引擎", ok, f"{cfg['tts']['url']} {msg}", fatal=False,
                         group="感知服务", hint="启动 EchoSmith 引擎 api_v2.py"))

    for key, label, group in (("ocr_exe", "OCR 可执行文件", "感知本地"),
                              ("vqa_python", "VQA 解释器", "感知本地"),
                              ("vqa_cli", "VQA 入口脚本", "感知本地")):
        checks.append(_check(label, Path(cfg[key]).is_file(), cfg[key], group=group))

    jy = cfg.get("jianying59", {})
    checks.append(_check("剪映 5.9 主程序", Path(jy.get("exe", "")).is_file(),
                         jy.get("exe", ""), group="剪映 5.9"))
    checks.append(_check("剪映草稿根", Path(jy.get("draft_root", "")).is_dir(),
                         jy.get("draft_root", ""), group="剪映 5.9"))
    checks.append(_check("草稿清单 root_meta", Path(jy.get("root_meta", "")).is_file(),
                         jy.get("root_meta", "(未配置,将写入草稿根)"), fatal=False, group="剪映 5.9",
                         hint="首次用 5.9 打开一次剪映后回填 config.jianying59.root_meta"))

    try:
        card = resolve_voice(cfg["tts"]["default_voice"], cfg)
        checks.append(_check("默认音色卡", card.is_file(), f"{cfg['tts']['default_voice']} → {card}", group="声音"))
    except Exception as exc:
        checks.append(_check("默认音色卡", False, str(exc), group="声音"))

    sfx_dir = Path(__file__).parents[3] / "assets" / "sfx"
    sfx_n = len(list(sfx_dir.glob("*.mp3"))) if sfx_dir.is_dir() else 0
    checks.append(_check("内置音效库", sfx_n >= 5, f"{sfx_n} 个音效({sfx_dir})", fatal=False, group="趣味素材"))

    # 字幕词边界分词(ADR-0020):jieba 可选,缺了降级内置高频词表
    try:
        import segmentation as _sg
        has_jieba = _sg.jieba_available()
        checks.append(_check("jieba 分词器", has_jieba,
                             "可用(词边界优先 jieba)" if has_jieba else "未安装(降级内置高频词表兜底)",
                             fatal=False, group="字幕",
                             hint="python tools/fetch_deps.py subtitle(可选;不装也能出片,冷门词可能被切)"))
    except Exception as exc:  # noqa: BLE001 — 自检本身绝不致命
        checks.append(_check("jieba 分词器", False, f"检查失败:{exc}", fatal=False, group="字幕"))
    wpi = Path(cfg.get("artboard_dir", "")) if cfg.get("artboard_dir") else None
    checks.append(_check("artboard 桥", bool(wpi and wpi.is_dir()), str(wpi or "(未配置 artboard_dir)"),
                         fatal=False, group="趣味素材"))

    fatal_bad = [c for c in checks if c["fatal"] and not c["ok"]]
    all_ok = not fatal_bad

    if a.report:
        W = 64
        print("=" * W)
        print("CutFlow 环境自检报告".center(W))
        print("生成时间:" + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        print("=" * W)
        cur = None
        for c in checks:
            if c["group"] != cur:
                cur = c["group"]
                print(f"\n【{cur}】")
            mark = "√ 就绪" if c["ok"] else ("× 未就绪" if c["fatal"] else "△ 未就绪(可后补)")
            print(f"  {mark}  {c['name']}")
            print(f"          {c['detail']}")
            if not c["ok"] and c["hint"]:
                print(f"          ↳ 提示: {c['hint']}")
        ready = sum(1 for c in checks if c["ok"])
        print("\n" + "-" * W)
        print(f"就绪度: {ready}/{len(checks)}   致命缺失: {len(fatal_bad)}   "
              f"结论: {'[OK] 可开工' if all_ok else '[FAIL] 有致命缺失,先修复上方 × 项'}")
        print("-" * W)
        return emit(all_ok, "DOCTOR_OK" if all_ok else "DOCTOR_FAIL",
                    f"{ready}/{len(checks)} 项就绪", {"checks": checks},
                    exit_code=0 if all_ok else 3)

    return emit(all_ok, "DOCTOR_OK" if all_ok else "DOCTOR_FAIL",
                f"{len(checks)-len(fatal_bad)}/{len(checks)} 项通过(致命失败 {len(fatal_bad)})",
                {"checks": checks}, exit_code=0 if all_ok else 3)


if __name__ == "__main__":
    ensure_utf8()
    sys.exit(main())
