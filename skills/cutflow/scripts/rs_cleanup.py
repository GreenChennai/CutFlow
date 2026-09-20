"""工程清理与归档(ADR-0007):完工必跑。默认 dry-run,--apply 才真删。

用法:
  python rs_cleanup.py <工程目录>            # 列出将删除/保留的清单(dry-run)
  python rs_cleanup.py <工程目录> --apply    # 真删
规则:
  必删: 06_output/_build、dev-*、*_probe*、99_试算、*.tmp、_asr_16k.wav、preview_*/draft_*
  必留(06_output 顶层,BUGREPORT B4 修订):final_*.mp4、subtitles.ass、master.srt、
        metadata.*、*report*.md、cards.json、deliverables、中文命名产物、bench_*.png;
        rebuild.py、REBUILD.md(P16:一键重建脚本与其说明书)、_variants/ 目录(P16);
        branded/、final/ 目录(P10b-1:S5/S8 交付成片的独占子目录);
        只删 `_`-前缀探针件与非交付物 —— 旧版会把成片/字幕/报告全列进删除名单。
  删除如实记账(P13-2):删失败(文件被占用等)逐项上报 failed 并非零退出,
  "已释放 N MB"只计真正删掉的。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import COVER_PNG, emit  # noqa: E402  — P17-1:封面名共用同一常量

DELETE_PATTERNS = ["06_output/_build", "99_试算"]
DELETE_GLOBS = ["dev-*", "*_probe*", "*.log", "*.tmp", "**/_asr_16k.wav", "**/*.tmp",
                "v_b_*.png", "check_*.png"]
# P17-1:封面必留判定派生自 rs_common.COVER_PNG(封面.png 及 封面_变体.png 前缀同源)
KEEP_PREFIX = ("成片", COVER_PNG.removesuffix(".png"), "字幕", "配音稿", "master.srt")
OUT_KEEP_EXACT = {"subtitles.ass", "master.srt", "cards.json", "sync_rows.json",
                  "segments_candidates.json", "deliverables.md",
                  # BUGREPORT P16:rebuild.py 由 rs_run --init 种下、SKILL.md 手册让用户跑,
                  # REBUILD.md 是它的说明书 —— 清掉会让"改字幕→一键重建"必然 FileNotFoundError。
                  "rebuild.py", "REBUILD.md", "verify_report.md", COVER_PNG}
# 06_output 下保留的目录前缀:sub_*(字幕 ASS,重渲依赖)、_variants(rs_brand 变体 IR,同 P16)、
# branded/ 与 final/(P10b-1:S5 品牌变体成片、S8 烧录导出的独占子目录,全是交付物)
KEEP_DIR_PREFIX = ("sub_", "_variants", "branded", "final")


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
                # sub_*/ 是字幕 ASS(重渲依赖);_variants/ 是品牌变体 IR(P16),均保留
                if p.name.startswith(KEEP_DIR_PREFIX):
                    keep.append(p)
                else:
                    delete.append(p)
            elif _out_keep(p.name):
                keep.append(p)
            else:
                delete.append(p)   # 06_output 顶层非交付物
    return delete, keep


def _tree_size(p: Path) -> int:
    """文件或目录的字节数(逐文件 stat;stat 不到的按 0 计)。"""
    if p.is_file():
        return p.stat().st_size
    if p.is_dir():
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return 0


def _apply_delete(root: Path, delete: list[Path]) -> tuple[int, list[dict]]:
    """真删,如实记账(P13-2):返回 (实际释放字节数, 失败清单)。

    此前 rmtree(ignore_errors=True)/unlink(missing_ok=True) 把删除失败静默吞掉,
    报告里"已删 N MB"可能不实 —— 现在失败逐项上报,释放量只计真删掉的。
    """
    freed, failed = 0, []
    for d in delete:
        try:
            size = _tree_size(d)
            if d.is_dir():
                shutil.rmtree(d)
            elif d.is_file():
                d.unlink()
            else:
                continue    # 已不存在(并发清理):不算失败也不计释放
        except OSError as exc:
            failed.append({"path": str(d.relative_to(root)), "error": str(exc)})
            continue
        freed += size
    return freed, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    root = Path(a.project)
    if not root.is_dir():
        return emit(False, "NO_PROJECT", f"工程不存在:{root}", exit_code=2)
    delete, keep = classify(root)
    size = sum(_tree_size(d) for d in delete)
    if a.apply:
        freed, failed = _apply_delete(root, delete)
        if failed:
            return emit(False, "CLEANUP_PARTIAL",
                        f"删除失败 {len(failed)} 项(文件被占用等);实际释放 {freed/1e6:.1f}MB"
                        "(失败项不计入释放量)",
                        {"failed": failed,
                         "freed_mb": round(freed / 1e6, 1),
                         "delete": [str(d.relative_to(root)) for d in delete],
                         "keep": [str(k.relative_to(root)) for k in keep]},
                        exit_code=4)
        return emit(True, "CLEANUP_OK",
                    f"已删除: 删 {len(delete)} 项 / 留 {len(keep)} 项 / 释放 {freed/1e6:.1f}MB",
                    {"delete": [str(d.relative_to(root)) for d in delete],
                     "keep": [str(k.relative_to(root)) for k in keep],
                     "size_mb": round(freed / 1e6, 1)})
    return emit(True, "CLEANUP_DRYRUN",
                f"dry-run(加 --apply 执行): 删 {len(delete)} 项 / 留 {len(keep)} 项 / "
                f"将释放 {size/1e6:.1f}MB",
                {"delete": [str(d.relative_to(root)) for d in delete],
                 "keep": [str(k.relative_to(root)) for k in keep],
                 "size_mb": round(size / 1e6, 1)})


if __name__ == "__main__":
    sys.exit(main())
