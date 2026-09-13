"""工程清理与归档(ADR-0007):完工必跑。默认 dry-run,--apply 才真删。

用法:
  python rs_cleanup.py <工程目录>            # 列出将删除/保留的清单(dry-run)
  python rs_cleanup.py <工程目录> --apply    # 真删
规则:
  必删: 06_output/_build、dev-*、*_probe*、99_试算、*.tmp、_asr_16k.wav、preview_*/draft_*
  必留(06_output 顶层,BUGREPORT B4 修订):final_*.mp4、subtitles.ass、master.srt、
        metadata.*、*report*.md、cards.json、deliverables、中文命名产物、bench_*.png;
        只删 `_`-前缀探针件与非交付物 —— 旧版会把成片/字幕/报告全列进删除名单。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit  # noqa: E402

DELETE_PATTERNS = ["06_output/_build", "99_试算"]
DELETE_GLOBS = ["dev-*", "*_probe*", "*.log", "*.tmp", "**/_asr_16k.wav", "**/*.tmp",
                "v_b_*.png", "check_*.png"]
KEEP_PREFIX = ("成片", "封面", "字幕", "配音稿", "master.srt")
OUT_KEEP_EXACT = {"subtitles.ass", "master.srt", "cards.json", "sync_rows.json",
                  "segments_candidates.json", "deliverables.md"}


def _out_keep(name: str) -> bool:
    """06_output 顶层文件的保留判定(B4):默认必留交付物,只删探针/中间件。"""
    if name.startswith(("_", "dev-", "preview_", "draft_")):
        return False
    if name.endswith((".tmp", ".log")):
        return False
    if name.startswith(KEEP_PREFIX) or name.startswith("字幕_"):
        return True
    if name.startswith("final_") and name.endswith(".mp4"):
        return True
    if name in OUT_KEEP_EXACT or name.startswith("metadata."):
        return True
    if "report" in name.lower() and name.lower().endswith((".md", ".json")):
        return True
    if name.startswith("bench") and name.endswith(".png"):
        return True
    return False


def classify(root: Path) -> tuple[list[Path], list[Path]]:
    delete, keep = [], []
    for pat in DELETE_PATTERNS:
        p = root / pat
        if p.exists():
            delete.append(p)
    for pat in DELETE_GLOBS:
        for p in root.glob(pat):
            delete.append(p)
    delete = sorted(set(delete))
    delete_set = {d.resolve() for d in delete}
    out = root / "06_output"
    if out.is_dir():
        for p in out.iterdir():
            if p.resolve() in delete_set:
                continue
            if p.is_dir():
                # sub_*/ 是字幕 ASS(重渲依赖),保留
                if not p.name.startswith("sub_"):
                    delete.append(p)
                else:
                    keep.append(p)
            elif _out_keep(p.name):
                keep.append(p)
            else:
                delete.append(p)   # 06_output 顶层非交付物
    return delete, keep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    root = Path(a.project)
    if not root.is_dir():
        return emit(False, "NO_PROJECT", f"工程不存在:{root}", exit_code=2)
    delete, keep = classify(root)
    size = sum(f.stat().st_size for f in delete if f.is_file())
    for d in delete:
        if d.is_dir():
            size += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    if a.apply:
        for d in delete:
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
            elif d.is_file():
                d.unlink(missing_ok=True)
    mode = "已删除" if a.apply else "dry-run(加 --apply 执行)"
    return emit(True, "CLEANUP_OK" if a.apply else "CLEANUP_DRYRUN",
                f"{mode}: 删 {len(delete)} 项 / 留 {len(keep)} 项 / 释放 {size/1e6:.1f}MB",
                {"delete": [str(d.relative_to(root)) for d in delete],
                 "keep": [str(k.relative_to(root)) for k in keep],
                 "size_mb": round(size / 1e6, 1)})


if __name__ == "__main__":
    sys.exit(main())
