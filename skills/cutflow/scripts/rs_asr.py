"""语音转写(v0.6.0 起为兼容薄壳):委托自带 ASR 运行器 tools/fun_asr.py。

用法:python rs_asr.py <媒体> --out <目录> [--backend auto|pkg|onnx|server]
产出:<out>/transcript_raw.json(分段,pkg 后端带字级 timestamp)+ transcript_raw.md。
历史:本文件曾直连 FunASR HTTP 服务;现统一走 fun_asr.py 三后端链(pkg→onnx→server),
不再依赖任何外部服务器。新代码请直接用 tools/fun_asr.py 或 rs_align build。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import emit  # noqa: E402

RUNNER = Path(__file__).resolve().parents[3] / "tools" / "fun_asr.py"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("--out", required=True)
    ap.add_argument("--backend", default="auto", choices=["auto", "pkg", "onnx", "server"])
    a = ap.parse_args()
    media = Path(a.media)
    if not media.is_file():
        return emit(False, "NO_MEDIA", f"文件不存在:{media}", exit_code=2)
    if not RUNNER.is_file():
        return emit(False, "NO_ASR_RUNNER", f"找不到自带 ASR 运行器:{RUNNER}", exit_code=3)
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "asr_raw.json"
    p = subprocess.run([sys.executable, str(RUNNER), str(media), "--json",
                        "--backend", a.backend, "--out", str(raw)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    doc: dict = {}
    try:
        doc = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return emit(False, "ASR_NO_OUTPUT",
                    f"fun_asr 无有效输出(exit {p.returncode}):{(p.stderr or p.stdout or '')[-300:]}",
                    exit_code=3)
    if p.returncode != 0 or not doc.get("ok"):
        return emit(False, "ASR_FAILED", doc.get("message") or "转写失败",
                    exit_code=3 if p.returncode == 3 else 4)
    data = doc.get("data") or {}
    segs = data.get("segments") or []
    md = "\n".join(f"[{float(s.get('start') or 0):.1f}s] {s.get('text', '')}" for s in segs)
    (out_dir / "transcript_raw.md").write_text(md, encoding="utf-8")
    (out_dir / "transcript_raw.json").write_text(
        json.dumps({"source": str(media), "segments": segs}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    chars = sum(len(s.get("text", "")) for s in segs)
    return emit(True, "ASR_OK",
                f"转写完成:{len(segs)} 段 / {chars} 字(后端 {data.get('backend')};"
                "本命令是 fun_asr.py 的兼容薄壳,新代码请直接用 fun_asr.py)",
                {"segments": len(segs), "chars": chars,
                 "json": str(out_dir / "transcript_raw.json"),
                 "charTimestamps": any(s.get("timestamp") for s in segs),
                 "deprecated": True})


if __name__ == "__main__":
    sys.exit(main())
