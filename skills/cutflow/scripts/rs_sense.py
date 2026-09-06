"""图片感知:OCR(文字)+ VQA(语义)合并输出。用法:python rs_sense.py <图片> --out <目录>"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import die, emit, load_config, run  # noqa: E402


def ocr_image(image: Path, out_txt: Path, cfg: dict) -> str:
    p = run([cfg["ocr_exe"], str(image), "-o", str(out_txt)], timeout=120)
    if p.returncode != 0 or not out_txt.is_file():
        return ""
    return out_txt.read_text(encoding="utf-8-sig").strip()


def vqa_image(image: Path, out_txt: Path, cfg: dict, prompt: str) -> str:
    p = run([cfg["vqa_python"], cfg["vqa_cli"], str(image),
             "--prompt", prompt, "--no-think", "-o", str(out_txt)], timeout=300)
    if p.returncode != 0 or not out_txt.is_file():
        return ""
    return out_txt.read_text(encoding="utf-8-sig").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--out", required=True)
    ap.add_argument("--prompt", default="用中文详细描述这张图片的内容:主体、文字、风格、配色。")
    a = ap.parse_args()
    image = Path(a.image)
    if not image.is_file():
        return emit(False, "NO_IMAGE", f"图片不存在:{image}", exit_code=2)
    cfg = load_config()
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ocr_txt = ocr_image(image, out_dir / f"{image.stem}_ocr.txt", cfg)
    vqa_txt = vqa_image(image, out_dir / f"{image.stem}_vqa.txt", cfg, a.prompt)
    result = {"image": str(image), "ocr": ocr_txt, "vqa": vqa_txt}
    (out_dir / f"{image.stem}_sensed.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return emit(True, "SENSE_OK", f"OCR {len(ocr_txt)} 字 / VQA {len(vqa_txt)} 字", result)


if __name__ == "__main__":
    sys.exit(main())
