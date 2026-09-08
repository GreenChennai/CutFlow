"""OCR / VQA 模块一键下载部署(从 CutFlow GitHub Release)。

用法:
  python tools/fetch_deps.py ocr    # ~113MB,RapidOCR 单文件
  python tools/fetch_deps.py vqa    # ~630MB,QORA 看图问答(Rust 引擎,免 Python)
  python tools/fetch_deps.py        # 查看部署状态
自动解压到 tools\\deps\\ 并回写 config(ocr_exe / vqa_exe)。
"""
from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
DEPS = REPO / "tools" / "deps"
RELEASE_BASE = "https://github.com/GreenChennai/CutFlow/releases/download/v0.3-dependencies"

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
    key = MODULES[mod]["config_key"]
    if mod == "ocr":
        cfg["ocr_exe"] = str(probe)
    else:
        cfg["vqa_exe"] = str(probe)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    if len(sys.argv) <= 1:
        status()
        return 0
    mod = sys.argv[1].lower().strip()
    if mod not in MODULES:
        print(f"未知模块:{mod}(可选 ocr / vqa)")
        return 2
    return install(mod)


if __name__ == "__main__":
    sys.exit(main())
