"""CutFlow 环境自检:检查所有依赖,输出 JSON 或人读报告。

用法:
  python rs_doctor.py             # JSON(agent 用)
  python rs_doctor.py --report    # 人读环境自检报告
"""
from __future__ import annotations

import argparse
import json
import os
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


def _check_path_encoding() -> tuple[bool, str]:
    """路径编码自检(ADR-0045):在含中文的临时路径下写读一个文件。

    目录契约中文化后,全部产物落点都在中文目录名下 —— 临时目录若写不进/读不回
    中文路径,整条产物链都会崩;这里用最小探测提前暴露(非致命,但要点名)。
    """
    import shutil
    import tempfile
    base = Path(tempfile.gettempdir()) / f"cutflow编码自检-{os.getpid()}"
    d = base / "中文目录·05_时间线工程"
    try:
        d.mkdir(parents=True, exist_ok=True)
        f = d / "探测·封面.png"
        payload = "中文内容ABC123".encode("utf-8")
        f.write_bytes(payload)
        ok = f.read_bytes() == payload
        return ok, f"{d}(写读{'一致' if ok else '不一致'})"
    except Exception as exc:  # noqa: BLE001 — 自检本身绝不致命
        return False, f"探测失败({type(exc).__name__}: {exc})"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _registry_capability_map() -> dict[str, list[str]]:
    """registry.videoTypes.<id>.capabilities → {能力 id: [videoType,…]}(影响面映射)。"""
    try:
        from rs_intent import load_registry  # noqa: PLC0415 — 懒加载,registry 读不出不致命
        reg = load_registry()
    except Exception:  # noqa: BLE001 — 自检本身绝不致命
        return {}
    out: dict[str, list[str]] = {}
    for vt, meta in (reg.get("videoTypes") or {}).items():
        for cid in (meta or {}).get("capabilities") or []:
            out.setdefault(str(cid), []).append(vt)
    return out


def _capability_inventory() -> tuple[list[dict], list[str], dict]:
    """能力部署清单(ADR-0049 懒加载):八项可选件三态 + 清单新鲜度(D4 机械覆盖)。

    全部**非 fatal**:初始零环境是硬承诺,FFmpeg + 纯标准库的基础档必须可开工。
    返回 (checks, 报告渲染行, JSON 数据)。
    """
    group = "能力部署"
    try:
        import rs_fetchable  # noqa: PLC0415 — 懒加载:doctor 本体不背它
    except Exception as exc:  # noqa: BLE001
        chk = _check("能力部署清单", False, f"rs_fetchable 加载失败({type(exc).__name__}: {exc})",
                     fatal=False, group=group, hint="重拉 skills/cutflow/scripts/rs_fetchable.py")
        return [chk], [f"  × 能力部署清单不可用:{chk['detail']}"], {}
    try:
        rows = rs_fetchable.state_all()
        manifest = rs_fetchable.manifest_summary()
    except Exception as exc:  # noqa: BLE001
        chk = _check("能力部署清单", False, f"三态探测失败({type(exc).__name__}: {exc})",
                     fatal=False, group=group)
        return [chk], [f"  × 能力部署清单不可用:{chk['detail']}"], {}
    impacts = _registry_capability_map()
    checks: list[dict] = []
    n_ready = sum(1 for r in rows if r["state"] == "READY")
    lines = [f"【能力部署清单】(ADR-0049 懒加载:已部署 {n_ready}/{len(rows)},"
             f"缺失/失败 {len(rows) - n_ready}/{len(rows)};详情 rs_fetchable.py state --json)"]
    for r in rows:
        cid = r["component"]
        label = rs_fetchable.CAPABILITY_DEPS[cid]["label"]
        hit = ",".join(impacts.get(cid, []))
        impact = hit if hit else "(暂无 videoType 引用;M8 手法库/M9 抠像预留)"
        ok = r["state"] == "READY"
        mark = "√ 就绪" if ok else ("△ 未部署" if r["state"] == "MISSING" else "× 失败")
        detail = (f"{'/'.join(r['modules'])}(~{r['size_mb']}MB,{r['backend']})"
                  f" 降级档 {r['degrade']} — {r['message']}")
        checks.append(_check(f"能力:{cid} {label}", ok, detail, fatal=False, group=group,
                             hint="" if ok else f"rs_fetchable.py install {r['modules'][0]}"
                                                "(或降级并留痕 degraded 块)"))
        lines.append(f"  {mark}  {cid} {label}")
        lines.append(f"          {detail}")
        lines.append(f"          ↳ 影响: {impact}")
    # 零可选件硬承诺(ADR-0049 决策 3):基础档永远可用,缺失永不静默
    lines.append("  基础档可用:FFmpeg + 纯标准库路径即可出片;"
                 "可选件缺失永不静默(降级 + degraded 留痕),用到才下载。")
    fresh_ok = bool(manifest.get("present"))
    if fresh_ok and isinstance(manifest.get("ageDays"), int):
        fresh_ok = manifest["ageDays"] <= rs_fetchable.MANIFEST_STALE_DAYS
    fresh_detail = (f"种子 {manifest.get('seededAt', '?')}(manifestVersion "
                    f"{manifest.get('manifestVersion', '?')});verify=pending "
                    f"{manifest.get('pending', '?')} 条(采用前须一手核实)")
    fresh_hint = ""
    if not manifest.get("present"):
        fresh_hint = "清单缺失或损坏,update --check 前须补"
    elif fresh_ok is False:
        fresh_hint = f"清单已超 {rs_fetchable.MANIFEST_STALE_DAYS} 天,发版时更新一次"
    checks.append(_check("deps-manifest 新鲜度", fresh_ok,
                         f"{manifest.get('path', '?')} — {fresh_detail}",
                         fatal=False, group=group, hint=fresh_hint))
    lines.append(f"  清单新鲜度: {fresh_detail}(D4 债机械覆盖;不自动更新)")
    data = {"components": rows, "manifest": manifest}
    return checks, lines, data


def _check_forge_path_contract() -> tuple[bool, str]:
    """两仓目录契约一致性(ADR-0045 §4.5):cutforge 仓的 paths.rs 应含同名中文目录常量。

    cutforge 仓不在并置路径(未 checkout / 独立部署)→ SKIP 不红;
    存在但常量缺失 → 不一致(非致命:另一个仓库的改造进度不由本仓自检拦截)。
    """
    forge = (Path(__file__).resolve().parents[3].parent / "cutforge" /
             "crates" / "cutforge-io" / "src" / "paths.rs")
    if not forge.is_file():
        return True, f"SKIP(cutforge 仓不在并置路径:{forge})"
    try:
        text = forge.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"读取失败:{exc}"
    missing = [d for d in ("00_制作简报", "05_时间线工程", "_内部状态") if d not in text]
    if missing:
        return False, f"paths.rs 缺目录常量:{'、'.join(missing)}({forge})"
    return True, f"目录常量与 rs_paths.STAGE_DIRS 同源口径({forge.name})"


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

    checks.append(_check("OCR 可执行文件", Path(cfg["ocr_exe"]).is_file(),
                         cfg["ocr_exe"], group="感知本地"))
    # v0.3.0 vqa_exe 直连模式优先;无直连时回退 vqa_python+vqa_cli 本地项目模式
    vqa_exe_ok = bool(cfg.get("vqa_exe")) and Path(cfg["vqa_exe"]).is_file()
    checks.append(_check("VQA 直连 exe", vqa_exe_ok, cfg.get("vqa_exe", ""),
                         group="感知本地"))
    legacy_vqa = Path(cfg["vqa_python"]).is_file() and Path(cfg["vqa_cli"]).is_file()
    checks.append(_check("VQA 解释器+入口(直连缺失时的回退)",
                         vqa_exe_ok or legacy_vqa,
                         f"vqa_exe={cfg.get('vqa_exe', '')}" if vqa_exe_ok
                         else f"{cfg['vqa_python']} + {cfg['vqa_cli']}",
                         group="感知本地"))

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
    # P21-1:artboard 桥不再只看目录存在(假名副其实)—— 下面的探针循环统一跑
    # rs_artboard.py --probe 真检「配置了?目录在?导出脚本找得到?」,且提为 fatal。
    checks.append(_check("artboard_dir 已配置", bool(wpi and wpi.is_dir()),
                         str(wpi or "(未配置 artboard_dir)"), fatal=False, group="趣味素材",
                         hint="config.json 的 artboard_dir 指向 artboard 技能目录"))

    # CutForge/artboard 桥脚本(v0.15 计划书 M4;P21-1 起 --probe 自检**提为 fatal**):
    # 桥断链曾只显示「可后补」,DOCTOR_OK 照发 —— 四桥是编排链路,断了必须先修。
    forge_scripts = Path(__file__).parent
    for name in ("rs_editor.py", "rs_notes.py", "rs_oplog.py", "rs_gate.py", "rs_artboard.py"):
        script = forge_scripts / name
        ok, detail = False, f"{script}"
        if script.is_file():
            try:
                p = subprocess.run([sys.executable, str(script), "--probe"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=30)
                ok = p.returncode == 0
                lines = (p.stdout or "").strip().splitlines()
                detail = lines[-1] if lines else f"exit={p.returncode}"
            except Exception as exc:  # noqa: BLE001 — 自检本身绝不致命
                detail = f"探测失败({type(exc).__name__})"
        else:
            detail = "脚本缺失"
        checks.append(_check(f"桥探针:{name}", ok, detail, fatal=True,
                             group="桥探针",
                             hint="缺失则重拉 CutFlow 仓库 skills/cutflow/scripts/;"
                                  "artboard 桥还需 config.artboard_dir 指向技能目录"))

    # ---- 路径契约(ADR-0045/0046):中文路径编码自检 + 两仓目录契约一致性
    ok, msg = _check_path_encoding()
    checks.append(_check("中文路径编码自检", ok, msg, fatal=False, group="路径契约",
                         hint="产物全落在中文目录名下;写读失败先查 TEMP 与系统区域设置"))
    ok, msg = _check_forge_path_contract()
    checks.append(_check("cutforge 目录契约一致", ok, msg, fatal=False, group="路径契约",
                         hint="cutforge 的 crates/cutforge-io/src/paths.rs 需含同名中文目录常量"))

    # ---- 能力部署清单(ADR-0049):八项可选件三态 + 清单新鲜度;全部非 fatal,
    # 零可选件时报「基础档可用」,绝不因缺失可选件变 fatal。
    cap_checks, cap_lines, cap_data = _capability_inventory()
    checks.extend(cap_checks)

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
            if c["group"] == "能力部署":
                continue    # 该组不在通用清单里渲染 —— 末尾「能力部署清单」节统一呈现
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
        for line in cap_lines:                  # 「能力部署清单」节(report 末尾,非 fatal)
            print(line)
        print("-" * W)
        data = {"checks": checks, "capabilities": cap_data}
        return emit(all_ok, "DOCTOR_OK" if all_ok else "DOCTOR_FAIL",
                    f"{ready}/{len(checks)} 项就绪", data,
                    exit_code=0 if all_ok else 3)

    data = {"checks": checks, "capabilities": cap_data}
    return emit(all_ok, "DOCTOR_OK" if all_ok else "DOCTOR_FAIL",
                f"{len(checks)-len(fatal_bad)}/{len(checks)} 项通过(致命失败 {len(fatal_bad)})",
                data, exit_code=0 if all_ok else 3)


if __name__ == "__main__":
    ensure_utf8()
    sys.exit(main())
