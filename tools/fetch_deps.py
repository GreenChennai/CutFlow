"""OCR / VQA / ASR 模块一键部署。

用法:
  python tools/fetch_deps.py ocr    # ~113MB,RapidOCR 单文件
  python tools/fetch_deps.py vqa    # ~630MB,QORA 看图问答(Rust 引擎,免 Python)
  python tools/fetch_deps.py        # 查看部署状态

自带 ASR(见 docs/OPTIMIZATION-v5.md R1):
  python tools/fetch_deps.py asr                      # ASR 部署状态
  python tools/fetch_deps.py asr --onnx               # 轻量:tools/.venv-asr + numpy/onnxruntime/jieba(~200MB)
  python tools/fetch_deps.py asr --pkg                # 精度:上面再装官方 funasr(含 torch-cpu,~1-2GB,有字级时间戳)
  python tools/fetch_deps.py asr --seed-models DIR    # 从已有目录播种模型(优先目录联接,零拷贝)
  python tools/fetch_deps.py asr --update-vendor DIR  # 用新版本覆盖 tools/asr_vendor/

自动解压到 tools\\deps\\ 并回写 config(ocr_exe / vqa_exe / asr.models_dir)。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
DEPS = REPO / "tools" / "deps"
RELEASE_BASE = "https://github.com/GreenChennai/CutFlow/releases/download/v0.3-dependencies"

ASR_VENV = REPO / "tools" / ".venv-asr"
ASR_VENDOR = REPO / "tools" / "asr_vendor"
MODELS_DEFAULT = REPO / "models" / "funasr"
MODELS_NEEDED = {
    "paraformer-large": "ASR 主模型(量化后约 237MB)— 必需",
    "fsmn-vad": "语音活动检测(分段必需)— 必需",
    "ct-punc": "标点恢复(强烈建议,断句质量依赖它)",
}

MODULES = {
    "ocr": {
        "zip": f"{RELEASE_BASE}/ocr-module.zip",
        "probe": "OCR.exe",
        "config_key": "ocr_exe",
        "size_mb": 113,
    },
    "vqa": {
        "zip": f"{RELEASE_BASE}/vqa-module.zip",
        "probe": "qora_assets/qor08b.exe",
        "config_key": "vqa_exe",
        "size_mb": 630,
    },
}


def download(url: str, dest: Path) -> None:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 CutFlow"})
    with urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(512 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  下载 {done/1e6:.0f}/{total/1e6:.0f} MB", end="", flush=True)
    print()


def status() -> None:
    cfg_path = REPO / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    print("部署状态:")
    ocr = cfg.get("ocr_exe", "")
    print(f"  OCR  {'✓ ' + ocr if ocr and Path(ocr).is_file() else '✗ 未部署(运行 python tools/fetch_deps.py ocr)'}")
    vqa_exe = cfg.get("vqa_exe", "")
    vqa_old = cfg.get("vqa_python", "")
    if vqa_exe and Path(vqa_exe).is_file():
        print(f"  VQA  ✓ {vqa_exe}")
    elif vqa_old:
        print(f"  VQA  ✓ {vqa_old}(本地项目模式)")
    else:
        print("  VQA  ✗ 未部署(运行 python tools/fetch_deps.py vqa)")
    print()
    asr_status()


def install(mod: str) -> int:
    spec = MODULES[mod]
    dest = DEPS / mod
    probe = dest / spec["probe"]
    if probe.is_file():
        print(f"{mod} 已部署:{probe}")
        write_config(mod, probe)
        return 0
    print(f"[{mod}] 下载中(约 {spec['size_mb']}MB)…")
    tmp_zip = DEPS / f"{mod}.zip.part"
    tmp_zip.parent.mkdir(parents=True, exist_ok=True)
    try:
        download(spec["zip"], tmp_zip)
        with zipfile.ZipFile(tmp_zip) as z:
            z.extractall(dest)
        if not probe.is_file():
            # 兼容带顶层目录的包:向下探一层
            for sub in dest.iterdir():
                if sub.is_dir() and (sub / spec["probe"]).is_file():
                    shutil.move(str(sub), str(dest / f"{mod}_pkg"))
                    break
        if not probe.is_file():
            raise RuntimeError(f"解压后未找到 {spec['probe']},包结构异常")
    except Exception as exc:  # noqa: BLE001
        print(f"部署失败:{exc}")
        tmp_zip.unlink(missing_ok=True)
        return 1
    tmp_zip.unlink(missing_ok=True)
    write_config(mod, probe)
    print(f"部署完成:{probe}\n已写入 config.{spec['config_key']}。运行 rs_doctor.py --report 验证。")
    return 0


def write_config(mod: str, probe: Path) -> None:
    cfg_path = REPO / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    if mod == "ocr":
        cfg["ocr_exe"] = str(probe)
    elif mod == "asr":
        cfg.setdefault("asr", {})["models_dir"] = str(probe)
    else:
        cfg["vqa_exe"] = str(probe)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 自带 ASR

def asr_venv_python() -> Path | None:
    for rel in ("Scripts/python.exe", "bin/python"):
        p = ASR_VENV / rel
        if p.is_file():
            return p
    return None


def asr_models_dir() -> Path:
    cfg_path = REPO / "config.json"
    if cfg_path.is_file():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        raw = (cfg.get("asr") or {}).get("models_dir")
        if raw:
            return Path(raw)
    return MODELS_DEFAULT


def _module_ok(py: Path, name: str) -> bool:
    p = subprocess.run([str(py), "-c", f"import {name}"], capture_output=True)
    return p.returncode == 0


def _link_dir(src: Path, dst: Path) -> bool:
    """优先目录联接/软链(零拷贝);失败返回 False 由调用方决定是否复制。

    注意:mklink 在中文 Windows 上输出 GBK,capture_output 不能带 text=True(会 UnicodeDecodeError)。
    """
    if os.name == "nt":
        p = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                           capture_output=True)
        return p.returncode == 0 and dst.exists()
    try:
        dst.symlink_to(src, target_is_directory=True)
        return True
    except OSError:
        return False


def asr_status() -> None:
    py = asr_venv_python()
    md = asr_models_dir()
    print("自带 ASR 状态:")
    if py is None:
        print("  venv  ✗ 未创建(运行 python tools/fetch_deps.py asr --onnx)")
    else:
        pkgs = {m: _module_ok(py, m) for m in ("numpy", "onnxruntime", "jieba", "funasr",
                                               "torch", "torchaudio")}
        marks = " ".join(f"{k}{'✓' if v else '✗'}" for k, v in pkgs.items())
        print(f"  venv  ✓ {py}")
        print(f"  依赖  {marks}")
        if pkgs["funasr"]:
            print("  后端  可用:pkg(有字级时间戳) / onnx")
        elif pkgs["onnxruntime"]:
            print("  后端  可用:onnx(⚠ 无字级时间戳)。要字级:--pkg")
    print(f"  模型  {'✓' if md.is_dir() else '✗'} {md}")
    for name, desc in MODELS_NEEDED.items():
        d = md / name
        n = len(list(d.glob("*.onnx"))) if d.is_dir() else 0
        print(f"        {'✓' if n else '✗'} {name:<20} {desc}")
    if not md.is_dir() or not (md / "paraformer-large").is_dir():
        print("  播种  python tools/fetch_deps.py asr --seed-models \"<已有 funasr 模型目录>\"")


def asr_seed_models(src: Path, copy: bool = False) -> int:
    if not src.is_dir():
        print(f"源目录不存在:{src}")
        return 2
    dst = asr_models_dir()
    dst.mkdir(parents=True, exist_ok=True)
    for name in MODELS_NEEDED:
        s, d = src / name, dst / name
        if not s.is_dir():
            print(f"  跳过 {name}:源目录无此项")
            continue
        if d.exists():
            print(f"  已有 {name}(跳过)")
            continue
        if not copy and _link_dir(s, d):
            print(f"  ✓ {name} → 目录联接(零拷贝)")
            continue
        print(f"  复制 {name} …", end="", flush=True)
        shutil.copytree(s, d)
        print(" 完成")
    write_config("asr", dst)
    print(f"模型目录:{dst}\n已写入 config.asr.models_dir。验证:python tools/fun_asr.py --probe")
    return 0


def asr_update_vendor(src: Path) -> int:
    """从给定的 funasr 目录覆盖 tools/asr_vendor(仅推理层文件)。"""
    if not (src / "paraformer_bin.py").is_file():
        print(f"源目录不像 funasr 推理层(缺 paraformer_bin.py):{src}")
        return 2
    keep = ("__init__.py", "paraformer_bin.py", "vad_bin.py", "punc_bin.py")
    for f in keep:
        if (src / f).is_file():
            shutil.copy2(src / f, ASR_VENDOR / f)
    if (src / "utils").is_dir():
        shutil.rmtree(ASR_VENDOR / "utils", ignore_errors=True)
        shutil.copytree(src / "utils", ASR_VENDOR / "utils",
                        ignore=shutil.ignore_patterns("__pycache__"))
    print(f"已更新 tools/asr_vendor ← {src}")
    print("⚠ 请同步更新 tools/asr_vendor/NOTICE.md 的『复制日期』与出处")
    return 0


def install_asr(args: list[str]) -> int:
    want_onnx = "--onnx" in args
    want_pkg = "--pkg" in args
    seed = _opt_value(args, "--seed-models")
    vendor = _opt_value(args, "--update-vendor")
    use_copy = "--copy" in args

    if vendor:
        return asr_update_vendor(Path(vendor))
    if seed:
        return asr_seed_models(Path(seed), copy=use_copy)
    if not (want_onnx or want_pkg):
        asr_status()
        return 0

    py = asr_venv_python()
    if py is None:
        print(f"创建 venv:{ASR_VENV}")
        p = subprocess.run([sys.executable, "-m", "venv", str(ASR_VENV)])
        if p.returncode != 0:
            print("venv 创建失败(检查 Python 安装)")
            return 1
        py = asr_venv_python()
        if py is None:
            print("venv 创建后仍找不到 python")
            return 1
    subprocess.run([str(py), "-m", "pip", "install", "-q", "-U", "pip"])

    print("[onnx 后端] 安装 numpy / onnxruntime / jieba …")
    p = subprocess.run([str(py), "-m", "pip", "install", "-q",
                        "numpy", "onnxruntime", "jieba"])
    if p.returncode != 0:
        print("onnx 依赖安装失败")
        return 1

    if want_pkg:
        print("[pkg 后端] 安装 torch + torchaudio(CPU 版)…约 200MB,耐心等")
        subprocess.run([str(py), "-m", "pip", "install", "-q", "torch", "torchaudio",
                        "--index-url", "https://download.pytorch.org/whl/cpu"])
        print("[pkg 后端] 安装 funasr / modelscope …")
        p = subprocess.run([str(py), "-m", "pip", "install", "-q", "funasr", "modelscope"])
        if p.returncode != 0:
            print("funasr 安装失败;可只用 --onnx 后端")
            return 1

    md = asr_models_dir()
    if not (md / "paraformer-large").is_dir():
        print("\n⚠ 还缺模型。两个办法:")
        print("  a) 从已有副本播种(推荐,零拷贝):")
        print("     python tools/fetch_deps.py asr --seed-models \"<某处的 funasr 模型目录>\"")
        print(f"  b) 手动把模型目录放到 {md}/<模型名>/")
    print("\n完成。验证:python tools/fun_asr.py --probe")
    asr_status()
    return 0


def _opt_value(args: list[str], flag: str) -> str:
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            return args[i + 1]
    return ""


def main() -> int:
    if len(sys.argv) <= 1:
        status()
        return 0
    mod = sys.argv[1].lower().strip()
    rest = sys.argv[2:]
    if mod == "asr":
        return install_asr(rest)
    if mod not in MODULES:
        print(f"未知模块:{mod}(可选 ocr / vqa / asr)")
        return 2
    return install(mod)


if __name__ == "__main__":
    sys.exit(main())
