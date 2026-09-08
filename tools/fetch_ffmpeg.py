"""FFmpeg 一键下载部署(多镜像回退,参考 MomentShift 实现)。

用法:python tools/fetch_ffmpeg.py [目标目录](默认 E:\\Tools\\ffmpeg)
流程:下载压缩包 → 抽取 ffmpeg.exe/ffprobe.exe → 回写 config.ffmpeg_dir。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]

SOURCES = [
    ("GitHub 直连", "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"),
    ("ghproxy 镜像", "https://ghproxy.net/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"),
    ("gyan.dev", "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"),
]
WANTED = {"ffmpeg.exe", "ffprobe.exe"}
MIN_SIZE = 10 * 1024 * 1024  # 正主都是几十 MB,过滤同名杂项


def stream_download(url: str, dest: Path, timeout: int = 60) -> None:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 CutFlow"})
    with urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  下载 {done/1e6:.0f}/{total/1e6:.0f} MB", end="", flush=True)
    print()


def extract_bins(zip_path: Path, dest_bin: Path) -> int:
    n = 0
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            base = info.filename.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if base in WANTED and info.file_size >= MIN_SIZE:
                dest_bin.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(dest_bin / base, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                n += 1
    return n


def write_config(dest_bin: Path) -> None:
    cfg_path = REPO / "config.json"
    cfg = {}
    if cfg_path.is_file():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["ffmpeg_dir"] = str(dest_bin)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    dest_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("E:/Tools/ffmpeg")
    dest_bin = dest_dir / "bin"
    if (dest_bin / "ffmpeg.exe").is_file():
        print(f"ffmpeg 已存在:{dest_bin / 'ffmpeg.exe'}(如需重装请先删除)")
        return 0

    last_err = ""
    for name, url in SOURCES:
        print(f"[{name}] 下载中…")
        tmp_zip = Path(tempfile.gettempdir()) / "cutflow_ffmpeg.zip"
        try:
            stream_download(url, tmp_zip)
            n = extract_bins(tmp_zip, dest_bin)
            if n < 2:
                raise RuntimeError(f"解压出的二进制不足({n}/2)")
        except Exception as exc:  # noqa: BLE001
            last_err = f"{name}: {exc}"
            print(f"  失败:{exc},换下一个源…")
            continue
        finally:
            tmp_zip.unlink(missing_ok=True)
        try:
            write_config(dest_bin)
        except Exception:
            pass
        print(f"部署完成:{dest_bin}\n已写入 config.ffmpeg_dir。运行 rs_doctor.py --report 验证。")
        return 0
    print(f"全部源失败。最后错误:{last_err}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
