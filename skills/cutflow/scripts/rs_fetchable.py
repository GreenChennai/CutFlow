"""懒加载依赖体系(ADR-0049):能力→组件映射与三态管理,初始零环境用到才下载。

用法:
  rs_fetchable.py state [--json]            八项能力组件三态清单(READY/MISSING/FAILED)
  rs_fetchable.py install <module>          现场下载(进度可见,可中断续传)
  rs_fetchable.py update --check|--apply    清单版本比对(**不自动更新**,--apply 才动手)
  rs_fetchable.py rollback <module>         清理该组件写进 config 的回写项

全局旗标:
  --no-fetch      离线:**绝不触发网络**(socket 层面禁用;缺组件按降级档处置)
  --auto          无人值守:缺组件**默认降级不阻塞**;显式 --auto --allow-fetch 才自动下载

三态协议(任何消费能力的操作先查三态,详见 rules/deps-lazy.md):
  READY   probe 通过,正常执行
  MISSING probe 不通过 → 允许下载则现场下载;不允许则降级到描述符声明的 degrade 档
  FAILED  下载/probe 失败 → 同上降级,并保留失败日志路径(tools/deps/<module>/.failed.json)

降级必留痕:产物侧写 {"degraded": true, "degradeReason", "missingComponent"}
(消费方用 degrade_record() 取标准块)。重依赖装独立 venv(tools/.venv-dsp/.venv-torch),
沿用 tools/.venv-asr 既有模式,绝不污染主解释器。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import EXIT_DEP, EXIT_EXEC, EXIT_INPUT, EXIT_OK, REPO_ROOT, emit, write_text_atomic  # noqa: E402

# ---------------------------------------------------------------- 落点(环境变量可注入,门禁零环境测试用)

def tools_dir() -> Path:
    return REPO_ROOT / "tools"


def deps_dir() -> Path:
    """可选件统一落点 tools/deps/<module>/;CUTFLOW_DEPS_DIR 可注入(零环境测试)。"""
    return Path(os.environ.get("CUTFLOW_DEPS_DIR") or tools_dir() / "deps")


def manifest_path() -> Path:
    return tools_dir() / "deps-manifest.json"


def config_path() -> Path:
    """config.json(CUTFLOW_CONFIG 可注入,rollback/install 回写测试用)。"""
    return Path(os.environ.get("CUTFLOW_CONFIG") or REPO_ROOT / "config.json")


VENV_DIRS = {
    "venv-dsp": tools_dir() / ".venv-dsp",    # 节拍/分离类(torch + BeatNet/madmom/Demucs)
    "venv-torch": tools_dir() / ".venv-torch",  # 视觉类(torch + RVM/CLIP)
}


# ---------------------------------------------------------------- 能力 → 组件映射(ADR-0049 唯一真相源)

# 字段:modules=pip/发行名;imports=probe 的 import 名;probe=声明探针表达式(语法门禁用,
# 轻探测实际走 find_spec/子进程 import,绝不真 import 重包);degrade=降级档 id;
# degradeText=给人看的降级说明。probe 字符串与 docs/adr/0049 逐字一致,不得改写。
CAPABILITY_DEPS: dict[str, dict] = {
    "audio.beat": {
        "label": "节拍检测", "use": "卡点",
        "modules": ["beatnet"], "imports": ["beatnet"],
        "size_mb": 320, "backend": "venv-dsp",
        "probe": "import beatnet", "degrade": "onset-energy",
        "degradeText": "能量起音(onset-energy,精度下降)",
    },
    "audio.downbeat": {
        "label": "强拍/弱拍网格", "use": "卡点进阶(下拍对齐)",
        "modules": ["madmom"], "imports": ["madmom"],
        "size_mb": 210, "backend": "venv-dsp",
        "probe": "import madmom", "degrade": "beat-only",
        "degradeText": "仅节拍无下拍(beat-only,网格粒度下降)",
    },
    "audio.stem": {
        "label": "人声/伴奏分离", "use": "BGM 独立控制",
        "modules": ["demucs"], "imports": ["demucs"],
        "size_mb": 350, "backend": "venv-dsp",
        "probe": "import demucs", "degrade": "none",
        "degradeText": "不可降级(degrade none):缺组件即不启用分离",
    },
    "vision.shot": {
        "label": "镜头切分", "use": "混剪/多镜头叙事",
        "modules": ["scenedetect"], "imports": ["scenedetect"],
        "size_mb": 45, "backend": "py",
        "probe": "import scenedetect", "degrade": "frame-diff",
        "degradeText": "帧差检测(frame-diff,切点精度下降)",
    },
    "vision.matting": {
        "label": "视频抠像(时序记忆)", "use": "人物分离/背景可控",
        "modules": ["rvm"], "imports": ["torch"],
        "size_mb": 480, "backend": "venv-torch",
        "probe": "import torch; rvm", "degrade": "none(gate)",
        "degradeText": "不可降级且过质量门禁才启用(none/gate,ADR-0050)",
    },
    "vision.track": {
        "label": "目标跟踪", "use": "跟拍/贴纸跟随",
        "modules": ["bytetrack"], "imports": ["bytetrack"],
        "size_mb": 20, "backend": "py",
        "probe": "import bytetrack", "degrade": "static-center",
        "degradeText": "静态中心锚点(static-center,跟随变固定)",
    },
    "vision.cv": {
        "label": "OpenCV 基础视觉", "use": "帧差/黑场/剪贴检测",
        "modules": ["opencv"], "imports": ["cv2"],
        "size_mb": 60, "backend": "py",
        "probe": "import cv2", "degrade": "none",
        "degradeText": "不可降级(degrade none):缺组件即不启用对应检测",
    },
    "text.clip": {
        "label": "CLIP 图文语义", "use": "语义选卡/素材匹配",
        "modules": ["clip"], "imports": ["clip"],
        "size_mb": 600, "backend": "venv-torch",
        "probe": "import clip", "degrade": "keyword-match",
        "degradeText": "关键词匹配(keyword-match,语义精度下降)",
    },
}

MODULE_TO_CAP = {m: cid for cid, d in CAPABILITY_DEPS.items() for m in d["modules"]}
LEGACY_MODULES = {"ocr": ["ocr_exe"], "vqa": ["vqa_exe"], "asr": ["asr.models_dir"]}
MANIFEST_STALE_DAYS = 180   # D4 债机械覆盖:清单超此天数即提示陈旧(非 fatal)


# ---------------------------------------------------------------- 网络闸(--no-fetch 在 socket 层面禁网)

_NET_ALLOWED = True


class NetworkDisabled(RuntimeError):
    """--no-fetch 下的网络禁用闸:任何网络调用先撞这堵墙。"""


def set_net_allowed(allowed: bool) -> None:
    global _NET_ALLOWED
    _NET_ALLOWED = allowed


def _require_net() -> None:
    if not _NET_ALLOWED:
        raise NetworkDisabled("--no-fetch:离线模式,禁止一切网络访问(ADR-0049)")


# ---------------------------------------------------------------- 轻探测(probe)

def _find_spec(name: str):
    """import 名 → spec(None=缺失)。间接层:门禁测试 mock 此函数模拟缺失。"""
    try:
        return importlib.util.find_spec(name)
    except (ImportError, ValueError, ModuleNotFoundError):
        return None


def venv_python(backend: str) -> Path | None:
    """backend → 该 venv 的解释器(未创建返回 None)。沿用 .venv-asr 模式。"""
    base = VENV_DIRS.get(backend)
    if base is None:
        return None
    for rel in ("Scripts/python.exe", "bin/python"):
        p = base / rel
        if p.is_file():
            return p
    return None


def _module_ok(py: Path, name: str) -> bool:
    """子进程轻探测 venv 内是否有该模块(不真 import 重包进本进程)。"""
    try:
        p = subprocess.run([str(py), "-c", f"import {name}"],
                           capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def _probe_ok(entry: dict) -> bool:
    """backend=py → 主解释器 find_spec;venv-* → venv 子进程探测(不存在即缺)。"""
    if entry["backend"] == "py":
        return all(_find_spec(m) is not None for m in entry["imports"])
    py = venv_python(entry["backend"])
    if py is None:
        return False
    return all(_module_ok(py, m) for m in entry["imports"])


# ---------------------------------------------------------------- 三态协议

def _module_dir(module: str) -> Path:
    return deps_dir() / module


def _installed_marker(module: str) -> Path:
    return _module_dir(module) / ".installed.json"


def _failed_marker(module: str) -> Path:
    return _module_dir(module) / ".failed.json"


def state(component_id: str) -> dict:
    """能力组件三态(对 M4/rs_edit 的公开 API,签名保持稳定)。

    返回 {"component","modules","state": READY|MISSING|FAILED,"degrade",
          "message","size_mb","backend","installDir"}。
    """
    entry = CAPABILITY_DEPS[component_id]
    module = entry["modules"][0]
    base = {"component": component_id, "modules": list(entry["modules"]),
            "degrade": entry["degrade"], "size_mb": entry["size_mb"],
            "backend": entry["backend"], "installDir": str(_module_dir(module))}
    failed = _failed_marker(module)
    if failed.is_file():
        try:
            info = json.loads(failed.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            info = {}
        return {**base, "state": "FAILED",
                "message": f"上次部署失败,日志:{failed};{info.get('message', '')}"}
    if _probe_ok(entry):
        return {**base, "state": "READY", "message": "probe 通过"}
    py = venv_python(entry["backend"]) if entry["backend"] != "py" else None
    why = (f"venv 未创建:{VENV_DIRS[entry['backend']]}" if entry["backend"] != "py" and py is None
           else f"缺模块:{'/'.join(entry['imports'])}(安装:rs_fetchable.py install {module})")
    return {**base, "state": "MISSING", "message": why}


def state_all() -> list[dict]:
    """八项组件三态清单(声明顺序;rs_doctor 能力部署清单节用)。"""
    return [state(cid) for cid in CAPABILITY_DEPS]


def degrade_record(component_id: str) -> dict:
    """降级留痕标准块(消费方原样写进产物 JSON;旧消费者忽略未知字段,向后兼容)。"""
    entry = CAPABILITY_DEPS[component_id]
    return {"degraded": True,
            "degradeReason": entry["degrade"],
            "missingComponent": "+".join(entry["modules"])}


def _eta_minutes(size_mb: int) -> tuple[int, int]:
    """下载时长预估(分钟):按 0.7–1.4MB/s 粗估,给人期待值用,不承诺。"""
    lo = max(1, size_mb // 100)
    return lo, max(lo + 1, round(size_mb / 40))


def ensure_ready(component_id: str, *, auto: bool = False, allow_fetch: bool = False,
                 no_fetch: bool = False, assume_yes: bool = False) -> dict:
    """消费能力前的统一入口(M4/rs_edit 等调用):查三态 → 处置 → 返回结果。

    策略(--auto 无人值守:默认降级,不阻塞,ADR-0049/方案 §5.7):
      READY                       → action="ready"
      MISSING + --no-fetch        → action="degraded"(绝不触网)
      MISSING + --auto(无 --allow-fetch) → action="degraded"(不阻塞)
      MISSING + --auto --allow-fetch → 现场下载 → "installed"/失败 "degraded"
      MISSING + 交互(非 auto 且 TTY)    → 三选项:下载/降级/取消
      MISSING + 非交互              → action="degraded"
    降级时返回 record=degrade_record() 标准块,消费方写进产物。
    """
    if no_fetch:
        set_net_allowed(False)
    st = state(component_id)
    if st["state"] == "READY":
        return {**st, "action": "ready", "record": None}
    entry = CAPABILITY_DEPS[component_id]
    module = entry["modules"][0]
    interactive = (not auto and not no_fetch and not assume_yes
                   and sys.stdin is not None and sys.stdin.isatty())
    if interactive:
        lo, hi = _eta_minutes(entry["size_mb"])
        target = VENV_DIRS.get(entry["backend"])
        where = f", 装到 {target}" if target else ""
        print(f"[cutflow] 需要「{entry['label']}」能力以支持{entry['use']}(当前未部署)")
        print(f"  → 组件: {module} (约 {entry['size_mb']}MB{where})")
        print("  → 下载后长期保留,不会重复下载")
        print(f"  [1] 现在下载(预计 {lo}\u2013{hi} 分钟,视网络)  "
              f"[2] 降级为{entry['degradeText']}  [3] 取消")
        try:
            choice = input("选择 [1/2/3]: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = "3"
        if choice == "1":
            set_net_allowed(True)
            return _finish_install(component_id)
        if choice == "2":
            return _degraded(component_id, "用户选择降级")
        return {**st, "action": "cancelled", "record": None,
                "message": "用户取消,本次不消费该能力"}
    if auto and allow_fetch and not no_fetch:
        return _finish_install(component_id)
    why = ("离线(--no-fetch)" if no_fetch
           else "无人值守(--auto 默认降级,不阻塞;要自动下载加 --allow-fetch)")
    return _degraded(component_id, why)


def _degraded(component_id: str, why: str) -> dict:
    st = state(component_id)
    print(f"[cutflow] 能力 {component_id} 未部署({why})→ 降级为"
          f"{CAPABILITY_DEPS[component_id]['degradeText']};已留痕 degraded 块")
    return {**st, "action": "degraded", "record": degrade_record(component_id),
            "message": why}


def _finish_install(component_id: str) -> dict:
    st = state(component_id)
    module = CAPABILITY_DEPS[component_id]["modules"][0]
    set_net_allowed(True)
    rc = install_component(module)
    if rc == EXIT_OK:
        return {**state(component_id), "action": "installed", "record": None}
    record = degrade_record(component_id)
    record["fetchLog"] = str(_failed_marker(module))
    return {**st, "action": "degraded", "record": record,
            "message": f"自动下载失败(exit {rc}),已降级并保留日志"}


# ---------------------------------------------------------------- 下载/安装(真实下载只在用户触发时发生;门禁测试全 mock)

def _download(url: str, dest: Path, label: str = "") -> None:
    """带进度与断点续传(.part)的下载;_require_net 先过网络闸。"""
    _require_net()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    headers = {"User-Agent": "Mozilla/5.0 CutFlow"}
    done = part.stat().st_size if part.is_file() else 0
    if done:
        headers["Range"] = f"bytes={done}-"       # 中断续传:服务端 206 则续,200 则重下
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 — 清单 URL 为静态种子
        total_hdr = int(resp.headers.get("Content-Length") or 0)
        resume = getattr(resp, "status", 200) == 206 and done > 0
        pos = done if resume else 0
        total = total_hdr + (done if resume else 0)
        mode = "ab" if resume else "wb"
        tag = f"{label} " if label else ""
        with open(part, mode) as f:
            while True:
                chunk = resp.read(512 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                pos += len(chunk)
                if total:
                    print(f"\r  下载{tag}{pos / 1e6:.0f}/{total / 1e6:.0f} MB", end="", flush=True)
    print()
    part.replace(dest)


def _ensure_venv(backend: str) -> Path:
    """确保重依赖独立 venv 存在(沿 tools/.venv-asr 的 fetch_deps.install_asr 模式)。"""
    _require_net()
    py = venv_python(backend)
    if py is not None:
        return py
    base = VENV_DIRS[backend]
    print(f"创建 venv:{base}")
    p = subprocess.run([sys.executable, "-m", "venv", str(base)])
    if p.returncode != 0:
        raise RuntimeError("venv 创建失败(检查 Python 安装)")
    py = venv_python(backend)
    if py is None:
        raise RuntimeError("venv 创建后仍找不到 python")
    return py


def _pip_install(py: Path, spec: str, index_url: str = "") -> None:
    _require_net()
    print(f"  pip install {spec} …")
    cmd = [str(py), "-m", "pip", "install", "-q", "-U", "pip"]
    subprocess.run(cmd, check=False)
    cmd = [str(py), "-m", "pip", "install", spec]
    if index_url:
        cmd += ["--index-url", index_url]
    p = subprocess.run(cmd)
    if p.returncode != 0:
        raise RuntimeError(f"pip install {spec} 失败(exit {p.returncode})")


def _manifest_modules() -> dict:
    p = manifest_path()
    if not p.is_file():
        raise FileNotFoundError(f"缺清单:{p}(先补 tools/deps-manifest.json)")
    return json.loads(p.read_text(encoding="utf-8")).get("modules", {})


def _write_installed(module: str, meta: dict) -> None:
    write_text_atomic(_installed_marker(module), json.dumps(
        {"module": module, "version": meta.get("version", ""),
         "backend": meta.get("backend", ""), "installedAt": _now()},
        ensure_ascii=False, indent=1))


def _write_failed(module: str, message: str) -> None:
    d = _module_dir(module)
    d.mkdir(parents=True, exist_ok=True)
    write_text_atomic(_failed_marker(module), json.dumps(
        {"module": module, "message": message[:400], "failedAt": _now()},
        ensure_ascii=False, indent=1))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def install_component(module: str) -> int:
    """单组件安装:venv(重依赖)→ pip → 权重下载 → 回写 config.deps。

    本里程碑(ADR-0049 落地 M5)只实现安装逻辑与目录约定;真实下载仅在用户
    显式触发且网络可用时发生。任何失败写 .failed.json(FAILED 态留痕)。
    """
    cap_id = MODULE_TO_CAP.get(module) or (module if module in CAPABILITY_DEPS else None)
    if cap_id is None:
        print(f"未知组件:{module}(可选:{'/'.join(sorted(MODULE_TO_CAP))})")
        return EXIT_INPUT
    entry = CAPABILITY_DEPS[cap_id]
    module = entry["modules"][0]        # 能力 id 形式的入参归一成发行模块名
    if _probe_ok(entry):
        print(f"{module} 已部署(probe 通过),无需下载。")
        return EXIT_OK
    if not _NET_ALLOWED:
        # 离线闸前显式短路:不读清单、不建 venv、不落任何盘上痕迹
        print(f"--no-fetch:离线模式不下载;将按降级档「{entry['degradeText']}」处置并留痕。")
        return EXIT_DEP
    try:
        meta = _manifest_modules()[module]
    except (FileNotFoundError, KeyError) as exc:
        print(f"清单缺条目:{exc}(tools/deps-manifest.json 须先登记)")
        return EXIT_INPUT
    try:
        backend = entry["backend"]
        py = sys.executable if backend == "py" else _ensure_venv(backend)
        if meta.get("pip"):
            _pip_install(Path(py), meta["pip"], meta.get("pipIndex", ""))
        if meta.get("weights"):
            dest = _module_dir(module) / Path(meta["weights"].split("?")[0]).name
            if not dest.is_file():
                print(f"  权重 → {dest}")
                _download(meta["weights"], dest, label=module)
        else:
            _module_dir(module).mkdir(parents=True, exist_ok=True)
        _write_installed(module, meta)
        _write_config_deps(module, meta)
    except NetworkDisabled as exc:
        print(f"离线拦截:{exc}\n→ 将按降级档「{entry['degradeText']}」处置。")
        return EXIT_DEP
    except Exception as exc:  # noqa: BLE001 — 失败必须转 FAILED 留痕,绝不崩主流程
        msg = f"部署 {module} 失败:{type(exc).__name__}: {exc}"
        print(msg)
        _write_failed(module, msg)
        return EXIT_EXEC
    print(f"部署完成:{_module_dir(module)}(长期保留,不重复下载)\n"
          f"验证:rs_fetchable.py state --json")
    return EXIT_OK


def _write_config_deps(module: str, meta: dict) -> None:
    """安装成功回写 config.deps.<module>(rollback 可清;回写失败不阻断)。"""
    p = config_path()
    try:
        cfg = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        cfg = {}
    cfg.setdefault("deps", {})[module] = {
        "capability": MODULE_TO_CAP.get(module, ""),
        "version": meta.get("version", ""), "installedAt": _now()}
    write_text_atomic(p, json.dumps(cfg, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- 子命令

def cmd_state(as_json: bool) -> int:
    rows = state_all()
    if as_json:
        return emit(True, "FETCHABLE_STATE",
                    f"{sum(1 for r in rows if r['state'] == 'READY')}/{len(rows)} 就绪",
                    {"components": rows})
    print("能力组件三态(ADR-0049;安装: rs_fetchable.py install <module>):")
    for r in rows:
        mark = {"READY": "READY ", "MISSING": "MISSING", "FAILED": "FAILED "}[r["state"]]
        print(f"  {mark}  {r['component']:<16} {'/'.join(r['modules']):<12} "
              f"~{r['size_mb']}MB {r['backend']:<10} 降级档 {r['degrade']}")
        if r["state"] != "READY":
            print(f"          {r['message']}")
    return EXIT_OK


def cmd_install(module: str) -> int:
    if not module:
        print("用法: rs_fetchable.py install <module>"
              f"(可选:{'/'.join(sorted(MODULE_TO_CAP))})")
        return EXIT_INPUT
    return install_component(module)


def cmd_update(check: bool, apply_: bool, no_fetch: bool) -> int:
    """清单版本 ↔ 本地 .installed.json 比对;**不自动更新**(ADR-0049 决策 4)。"""
    if no_fetch:
        set_net_allowed(False)
    mods = _manifest_modules()
    if not (check or apply_):
        print("update 需要 --check(只比对)或 --apply(显式更新);绝不自动更新。")
        check = True
    local: dict[str, str] = {}
    for m in mods:
        p = _installed_marker(m)
        if p.is_file():
            try:
                local[m] = json.loads(p.read_text(encoding="utf-8")).get("version", "")
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                local[m] = ""
    rows, stale = [], []
    for m, meta in sorted(mods.items()):
        lv = local.get(m)
        row = {"module": m, "manifestVersion": meta.get("version", ""),
               "localVersion": lv, "status": "missing" if lv is None else
               ("stale" if lv != meta.get("version", "") else "current")}
        rows.append(row)
        if row["status"] in ("missing", "stale"):
            stale.append(row)
    print("模块              清单版本    本地版本    状态")
    for r in rows:
        print(f"  {r['module']:<16} {r['manifestVersion']:<10} "
              f"{str(r['localVersion']):<10} {r['status']}")
    print(f"差异 {len(stale)} 个;"
          + ("--apply 才更新(显式触发)。" if stale else "全部一致。"))
    if apply_ and stale:
        if no_fetch:
            print("--apply 与 --no-fetch 互斥:离线不更新。")
            return EXIT_DEP
        rc = EXIT_OK
        for r in stale:
            rc = max(rc, install_component(r["module"]))
        return rc
    return EXIT_OK


def cmd_rollback(module: str) -> int:
    """清理组件写进 config 的回写项(下载件保留不删,ADR-0049 回滚语义)。"""
    if not module:
        print("用法: rs_fetchable.py rollback <module>")
        return EXIT_INPUT
    p = config_path()
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    removed: list[str] = []
    if module in LEGACY_MODULES:                     # 兼容既有三件的回写键
        for key in LEGACY_MODULES[module]:
            node: dict = cfg
            parts = key.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            if isinstance(node, dict) and parts[-1] in node:
                node.pop(parts[-1])
                removed.append(key)
    if isinstance(cfg.get("deps"), dict) and module in cfg["deps"]:
        cfg["deps"].pop(module)
        removed.append(f"deps.{module}")
    if not removed:
        print(f"{module} 无 config 回写项(无需回滚)。")
        return EXIT_OK
    write_text_atomic(p, json.dumps(cfg, ensure_ascii=False, indent=2))
    print(f"已清理 config 回写项:{', '.join(removed)}(下载件保留于 {deps_dir()})。")
    return EXIT_OK


def manifest_summary() -> dict:
    """清单新鲜度摘要(D4 债机械覆盖;rs_doctor 报「清单新鲜度」用)。"""
    p = manifest_path()
    if not p.is_file():
        return {"present": False, "path": str(p)}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return {"present": False, "path": str(p), "error": f"读取失败({type(exc).__name__})"}
    mods = doc.get("modules", {})
    seeded = str(doc.get("seededAt", ""))
    age_days = None
    if seeded:
        try:
            age_days = (date.today() - date.fromisoformat(seeded)).days
        except ValueError:
            pass
    return {"present": True, "path": str(p),
            "manifestVersion": str(doc.get("manifestVersion", "")),
            "seededAt": seeded, "ageDays": age_days,
            "pending": sum(1 for m in mods.values() if m.get("verify") == "pending"),
            "modules": len(mods)}


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="rs_fetchable.py",
        description="懒加载依赖体系(ADR-0049):初始零环境,用到才下载,缺失必降级留痕")
    ap.add_argument("command", choices=["state", "install", "update", "rollback"],
                    help="state=三态清单;install=现场下载;update=清单比对(不自动);rollback=清 config 回写项")
    ap.add_argument("module", nargs="?", help="组件模块名或能力 id(beatnet/madmom/demucs/scenedetect/rvm/bytetrack/opencv/clip)")
    ap.add_argument("--json", action="store_true", help="state:输出 JSON(agent 用)")
    ap.add_argument("--check", action="store_true", help="update:只比对不更新")
    ap.add_argument("--apply", action="store_true", help="update:显式更新有差异模块")
    ap.add_argument("--no-fetch", action="store_true", help="离线:绝不触发网络(缺组件按降级档处置)")
    ap.add_argument("--auto", action="store_true", help="无人值守:缺组件默认降级不阻塞")
    ap.add_argument("--allow-fetch", action="store_true", help="与 --auto 连用才允许自动下载")
    a = ap.parse_args(argv)

    if a.no_fetch:
        set_net_allowed(False)
    if a.command == "state":
        return cmd_state(a.json)
    if a.command == "install":
        return cmd_install(a.module or "")
    if a.command == "update":
        return cmd_update(a.check, a.apply, a.no_fetch)
    return cmd_rollback(a.module or "")


def is_known_module(name: str) -> bool:
    """tools/fetch_deps.py 兼容委托的判别口:是否为本体系可接管的组件名。"""
    return name in MODULE_TO_CAP or name in CAPABILITY_DEPS


if __name__ == "__main__":
    sys.exit(main())
