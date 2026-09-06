"""TTS:音色卡驱动,逐句合成+断点续传,产出 wav 序列与句级时间轴。

用法:python rs_tts.py --script 文案.txt --out <目录> [--voice koubo-test] [--speed 1.0]
产出:<out>/tts_0001.wav...、manifest.json(句级 start/end/text)、voice_48k_full.wav(整条)。
音色卡:<voices_dir>/<voice>/card.json,权重路径经 /set_gpt_weights 切换,情感前缀【xx】可选。
铁律:先全量合成落盘,再进时间线(渲染前置条件)。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import die, emit, ffmpeg_bin, load_config, media_duration_s, run  # noqa: E402

EMO_RE = re.compile(r"^【(.+?)】\s*")
MAX_LINES = 200


def http_bytes(url: str, body: dict, timeout: float = 1800) -> bytes:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def engine_ready(cfg: dict) -> bool:
    """4xx 也算存活(FastAPI 参数校验拒绝=服务在)。"""
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(cfg["tts"]["url"] + "/tts", data=b"{}",
                               headers={"Content-Type": "application/json"}), timeout=4)
        return True
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:
        return False


def set_weights(card: dict, cfg: dict) -> None:
    base = cfg["tts"]["url"]
    for path, key in (("set_gpt_weights", "gpt"), ("set_sovits_weights", "sovits")):
        if card.get(key):
            urllib.request.urlopen(
                urllib.request.Request(f"{base}/{path}?weights_path={urllib.parse.quote(card[key])}"),
                timeout=300).read()


def split_sentences(text: str) -> list[dict]:
    """按换行/中英句末标点切句,保留【情感】前缀。"""
    out = []
    for raw in re.split(r"[\n]+", text.strip()):
        raw = raw.strip()
        if not raw:
            continue
        emo = None
        m = EMO_RE.match(raw)
        if m:
            emo = m.group(1)
            raw = raw[m.end():]
        for part in re.split(r"(?<=[。!?;,.!?;])\s*", raw):
            part = part.strip()
            if part:
                out.append({"emotion": emo, "text": part})
    return out[:MAX_LINES]


def synth(sentences: list[dict], voice: str, speed: float, out_dir: Path, cfg: dict) -> dict:
    from rs_common import resolve_voice
    try:
        card_path = resolve_voice(voice, cfg)
    except FileNotFoundError as exc:
        die(2, "NO_VOICE", str(exc))
    except ValueError as exc:
        die(2, "VOICE_AMBIGUOUS", str(exc))
    if voice in {v.lower() for v in cfg["tts"].get("disabled_voices", [])}:
        die(2, "VOICE_DISABLED", f"音色 {voice} 已标记不可用,请换音色")
    card = json.loads(card_path.read_text(encoding="utf-8"))
    emo_name = card.get("default_emotion", "平静")
    emo = card["emotions"][emo_name]

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    manifest = {"voice": voice, "speed": speed, "sentences": []}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        done_texts = {s["text"] for s in manifest["sentences"]}
    else:
        done_texts = set()

    if engine_ready(cfg):
        pass  # 已就绪
    else:
        die(3, "TTS_DOWN", f"TTS 引擎不可达({cfg['tts']['url']}),先启动:engine_dir/api_v2.py -a 127.0.0.1 -p 9885")
    set_weights(card, cfg)

    base = cfg["tts"]["url"]
    idx = len(manifest["sentences"])
    for item in sentences:
        if item["text"] in done_texts:
            continue
        body = {"text": item["text"],
                "text_lang": "zh", "prompt_lang": "zh",
                "ref_audio_path": emo["ref"].replace("\\", "/"),
                "prompt_text": emo["prompt_text"],
                "text_split_method": "cut5",
                "speed_factor": speed,
                "aux_ref_audio_paths": emo.get("aux_refs", [])}
        wav_bytes, t0 = None, time.time()
        for attempt in (1, 2, 3):
            try:
                wav_bytes = http_bytes(base + "/tts", body)
                break
            except Exception as exc:
                if attempt == 3:
                    die(4, "TTS_FAIL", f"第 {idx} 句合成失败:{exc}")
                time.sleep(3 * attempt)
        idx += 1
        name = f"tts_{idx:04d}.wav"
        (out_dir / name).write_bytes(wav_bytes)
        dur = media_duration_s(out_dir / name, cfg)
        manifest["sentences"].append({
            "i": idx, "text": item["text"], "emotion": item["emotion"],
            "wav": name, "dur_s": round(dur, 3)})
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  [{idx}/{len(sentences)}] {dur:.2f}s {item['text'][:24]}", file=sys.stderr)

    # 句级时间轴 + 整条 48k 立体声
    t = 0.0
    for s in manifest["sentences"]:
        s["start_s"] = round(t, 3)
        t += s["dur_s"] + 0.12  # 句间自然垫 120ms
        s["end_s"] = round(t, 3)
    manifest["total_s"] = round(t, 3)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    full = out_dir / "voice_48k_full.wav"
    inputs, files = [], [s["wav"] for s in manifest["sentences"]]
    if files:
        cmd = [ffmpeg_bin(cfg), "-v", "error", "-y"]
        for f in files:
            cmd += ["-i", str(out_dir / f)]
        n = len(files)
        fc = "".join(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}];" for i in range(n))
        fc += "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
        cmd += ["-filter_complex", fc, "-map", "[out]", str(full)]
        p = run(cmd, timeout=600)
        if p.returncode != 0:
            die(4, "CONCAT_FAIL", f"拼接失败:{(p.stderr or '')[:300]}")
    return {"sentences": len(manifest["sentences"]), "total_s": manifest["total_s"],
            "manifest": str(manifest_path), "full_wav": str(full)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", required=True, help="文案 txt(支持【情感】前缀,按行/句切分)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--voice", default=None)
    ap.add_argument("--speed", type=float, default=1.0)
    a = ap.parse_args()
    text = Path(a.script).read_text(encoding="utf-8-sig")
    sentences = split_sentences(text)
    if not sentences:
        return emit(False, "EMPTY_SCRIPT", "文案为空", exit_code=2)
    cfg = load_config()
    voice = a.voice or cfg["tts"]["default_voice"]
    data = synth(sentences, voice, a.speed, Path(a.out), cfg)
    return emit(True, "TTS_OK", f"合成 {data['sentences']} 句,总长 {data['total_s']}s", data)


if __name__ == "__main__":
    import urllib.parse  # noqa: F401 (set_weights 引用)
    sys.exit(main())
