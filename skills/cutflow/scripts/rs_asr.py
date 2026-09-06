"""语音转写:媒体文件 → 16k 单声道 wav → FunASR 服务 → 结构化分段。

用法:python rs_asr.py <媒体> --out <目录> [--slug dev]
产出:<out>/transcript_raw.json(分段)+ transcript_raw.md(带时间戳文本)。
铁律:客户端永远自己抽 16k wav 再上传(绕开服务端视频 demux 依赖)。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import die, emit, load_config, ffmpeg_bin, ffprobe_json  # noqa: E402

LINE_RE = re.compile(r"^\[(\d+(?:\.\d+)?)s\]\s*(?:说话人(\d+):)?\s*(.*)$")


def normalize_to_wav16k(media: Path, out_wav: Path, cfg: dict) -> None:
    p = __import__("rs_common").run([ffmpeg_bin(cfg), "-v", "error", "-y", "-i", str(media),
                                     "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                                     str(out_wav)], timeout=600)
    if p.returncode != 0 or not out_wav.is_file():
        die(4, "NORMALIZE_FAIL", f"音频抽取失败:{(p.stderr or '')[:300]}")


def parse_structured(text: str) -> list[dict]:
    """`[1.5s] 说话人0: 文本` 行 → 分段列表。"""
    segs, last = [], None
    for line in text.splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            if last and line.strip():
                last["text"] += line.strip()  # 软换行续行
            continue
        start, spk, txt = float(m.group(1)), m.group(2), m.group(3).strip()
        if last:
            last["end"] = start
        if txt:
            last = {"start": start, "end": None, "spk": int(spk) if spk is not None else 0, "text": txt}
            segs.append(last)
    if last and last["end"] is None:
        last["end"] = last["start"] + 3.0
    return segs


def transcribe(media: Path, out_dir: Path, cfg: dict) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = out_dir / "_asr_16k.wav"
    normalize_to_wav16k(media, wav, cfg)

    boundary = "----CutFlowBoundary"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n"
            f"{cfg['asr']['model']}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"structured\"\r\n\r\n1\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{wav.name}\"\r\nContent-Type: audio/wav\r\n\r\n").encode() + wav.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(cfg["asr"]["url"] + "/v1/audio/transcriptions", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        die(3, "ASR_UNREACHABLE", f"FunASR 服务失败:{exc}(先拉起 ASR 服务,见 rules/sense.md)")

    if "error" in result:
        die(4, "ASR_ERROR", str(result["error"]))
    text = result.get("text", "")
    segs = parse_structured(text)
    (out_dir / "transcript_raw.json").write_text(
        json.dumps({"source": str(media), "segments": segs}, ensure_ascii=False, indent=1), encoding="utf-8")
    md = "\n".join(f"[{s['start']:.1f}s] 说话人{s['spk']}: {s['text']}" for s in segs)
    (out_dir / "transcript_raw.md").write_text(md, encoding="utf-8")
    return {"segments": len(segs), "chars": len(text), "json": str(out_dir / "transcript_raw.json")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    media = Path(a.media)
    if not media.is_file():
        return emit(False, "NO_MEDIA", f"文件不存在:{media}", exit_code=2)
    data = transcribe(media, Path(a.out), load_config())
    return emit(True, "ASR_OK", f"转写完成:{data['segments']} 段 / {data['chars']} 字", data)


if __name__ == "__main__":
    sys.exit(main())
