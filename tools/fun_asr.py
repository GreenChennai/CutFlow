"""CutFlow 自带 ASR:一条命令转写,**不需要任何外部服务器**。

用法:
  python tools/fun_asr.py <媒体|wav> [--json] [--out 02_sensed/asr_raw.json]
  python tools/fun_asr.py <媒体> --backend onnx     # 轻量(无字级时间戳)
  python tools/fun_asr.py <媒体> --backend pkg      # 官方 funasr 包(有字级时间戳)
  python tools/fun_asr.py --probe                   # 只报告后端就绪状态

后端(自动选择,不需要用户记):

  pkg    官方 funasr 包(CutFlow 自己的 venv)→ **原生字级 timestamp** ⭐ 精度优先
  onnx   tools/asr_vendor 的 ONNX 推理层      → 仅 VAD 段边界,capabilities.charTimestamps=false
  server 兼容保留的 HTTP 服务(config.asr.url)

⚠️ 事实说明:本地 Paraformer 的 ONNX 导出只有 `logits` + `token_num` 两个输出,
   **没有 timestamp**。CIF 在导出阶段已压掉时间轴,事后无法恢复。所以 onnx 后端
   只能给到 VAD 段边界,字级对齐会被标记 degraded。要字级时间戳请装 pkg 后端。

输出协议(与 rs_common 一致):{"ok","code","message","data"},退出码 0/2/3/4。
数据落在 `data.segments = [{start, end, text, timestamp?}]`,可直接喂 rs_align.build_wordline。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "cutflow" / "scripts"))

ASR_VENV = REPO / "tools" / ".venv-asr"
DEFAULT_MODELS = REPO / "models" / "funasr"
DEFAULT_MODEL, DEFAULT_VAD, DEFAULT_PUNC = "paraformer-large", "fsmn-vad", "ct-punc"
SEGMENT_SEC = 60.0
# VAD 静音阈值(ms):本机实测 68s 口播素材 —— 800(默认)→5 段/中位 15.9s;
# 400 → 27 段/中位 2.1s。ONNX 后端没有字级时间戳,段切得越细,段内均分误差越小,
# 且 VAD 只占总耗时约 7%(0.4s/5.8s)。400ms 是拐点(300 起收益递减)。
VAD_MAX_END_SIL = 400
REEXEC_FLAG = "CUTFLOW_ASR_REEXEC"

EXIT_OK, EXIT_INPUT, EXIT_DEP, EXIT_EXEC = 0, 2, 3, 4


# ---------------------------------------------------------------- 基础设施

def emit(ok: bool, code: str, message: str, data=None, exit_code: int = EXIT_OK) -> int:
    print(json.dumps({"ok": ok, "code": code, "message": message, "data": data},
                     ensure_ascii=False, default=str))
    return exit_code


def load_cfg() -> dict:
    p = REPO / "config.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def asr_cfg() -> dict:
    return (load_cfg().get("asr") or {})


def models_dir() -> Path:
    raw = asr_cfg().get("models_dir")
    return Path(raw) if raw else DEFAULT_MODELS


def venv_python(venv: Path = ASR_VENV) -> Path | None:
    for rel in ("Scripts/python.exe", "bin/python"):
        p = venv / rel
        if p.is_file():
            return p
    return None


def has_module(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def maybe_reexec() -> None:
    """优先用 CutFlow 自己的 ASR venv 运行(它是被授权的 ASR 运行时)。"""
    if os.environ.get(REEXEC_FLAG) == "1":
        return
    py = venv_python()
    if py is None:
        return                                  # 没建 venv → 就地跑(用户环境可能已够)
    try:
        if Path(sys.executable).resolve() == py.resolve():
            return
    except OSError:
        pass
    os.environ[REEXEC_FLAG] = "1"
    os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])


def ffmpeg_exe() -> str:
    d = load_cfg().get("ffmpeg_dir")
    if d:
        p = Path(d) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if p.is_file():
            return str(p)
    return shutil.which("ffmpeg") or "ffmpeg"


def to_wav16k(src: Path, dst: Path) -> None:
    cmd = [ffmpeg_exe(), "-y", "-v", "error", "-i", str(src),
           "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", str(dst)]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 or not dst.is_file():
        raise RuntimeError(f"ffmpeg 抽音频失败:{(p.stderr or '')[-300:]}")


def wav_duration(path: Path) -> float:
    import struct
    with path.open("rb") as f:
        data = f.read(4096)
    # 标准 PCM wav:走简单的 RIFF 解析,避免依赖 ffprobe
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return 0.0
    i, rate, bits, ch = 12, 16000, 16, 1
    while i + 8 <= len(data):
        cid, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        if cid == b"fmt ":
            _fmt, ch, rate = struct.unpack("<HH I", data[i + 8:i + 16])[0:3]
            bits = struct.unpack("<H", data[i + 22:i + 24])[0]
        if cid == b"data":
            return size / max(1, rate * ch * bits // 8)
        i += 8 + size + (size & 1)
    return 0.0


# ---------------------------------------------------------------- 后端探测

def onnx_ready() -> tuple[bool, str]:
    if not (has_module("numpy") and has_module("onnxruntime")):
        return False, "缺 numpy / onnxruntime(装:python tools/fetch_deps.py asr --onnx)"
    md = models_dir()
    if not (md / DEFAULT_MODEL).is_dir():
        return False, f"缺模型 {DEFAULT_MODEL}(播种:python tools/fetch_deps.py asr --seed-models <目录>)"
    if not (md / DEFAULT_VAD).is_dir():
        return False, f"缺 VAD 模型 {DEFAULT_VAD}(同上)"
    return True, "就绪"


def pkg_ready() -> tuple[bool, str]:
    if not has_module("funasr"):
        return False, "未安装官方 funasr 包(装:python tools/fetch_deps.py asr --pkg)"
    if not (has_module("torchaudio") or has_module("kaldi_native_fbank")):
        return False, ("缺 fbank 特征后端:funasr 抽特征需要 torchaudio 或 kaldi-native-fbank。"
                       "装:python tools/fetch_deps.py asr --pkg(已含 torchaudio),"
                       "或单独 pip install torchaudio")
    md = models_dir()
    has_local = (md / DEFAULT_MODEL).is_dir() and bool(torch_model_file(md / DEFAULT_MODEL))
    if has_local:
        return True, "就绪(本地 torch 权重)"
    return True, "就绪;本地模型是 ONNX 导出,首次运行将从 ModelScope 下载 torch 权重(~1GB,一次性)"


def server_ready() -> tuple[bool, str]:
    url = (asr_cfg().get("url") or "").rstrip("/")
    if not url:
        return False, "未配置 config.asr.url"
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
            return (r.status == 200), ("可达" if r.status == 200 else f"HTTP {r.status}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"不可达({exc})"


BACKENDS = {"pkg": pkg_ready, "onnx": onnx_ready, "server": server_ready}
CAPS = {"pkg": {"charTimestamps": True, "hotwords": True, "speaker": False},
        "onnx": {"charTimestamps": False, "hotwords": False, "speaker": False},
        "server": {"charTimestamps": False, "hotwords": False, "speaker": True}}


def resolve_backend(requested: str) -> tuple[str | None, dict]:
    report = {name: {"ready": fn()[0], "detail": fn()[1]} for name, fn in BACKENDS.items()}
    if requested and requested != "auto":
        if requested not in BACKENDS:
            return None, report
        ok, detail = BACKENDS[requested]()
        return (requested if ok else None), report if ok else {**report, "_fail": detail}
    if not requested or requested == "auto":
        cfg_backend = asr_cfg().get("backend")
        if cfg_backend and cfg_backend != "auto":
            ok, _ = BACKENDS.get(cfg_backend, (lambda: (False, "")))()
            return (cfg_backend if ok else None), report
        for name in ("pkg", "onnx", "server"):          # 精度优先 → 轻量 → 兜底
            if BACKENDS[name]()[0]:
                return name, report
    return None, report


# ---------------------------------------------------------------- 转写实现

def _norm_ts(ts, duration_ms: float):
    """把字级时间戳归一化到毫秒。

    FunASR 不同版本/模型可能返回 10ms 帧或 ms;若最大值明显小于音频时长,按帧(×10)处理。
    """
    if not isinstance(ts, list) or not ts:
        return None
    flat = [v for pair in ts if isinstance(pair, (list, tuple)) and len(pair) >= 2 for v in pair[:2]]
    if not flat:
        return None
    mx = max(flat)
    if duration_ms > 0 and mx < duration_ms * 0.4:
        return [[int(a) * 10, int(b) * 10] for a, b in ts]
    return [[int(a), int(b)] for a, b in ts]


def _quant_flag(model_dir: Path) -> bool:
    """模型目录里是 model_quant.onnx 就用量化版(否则推理器会去找不存在的 model.onnx)。"""
    if (model_dir / "model_quant.onnx").is_file():
        return True
    return False


HUB_ALIAS = {"paraformer-large": "paraformer-zh", "paraformer": "paraformer-zh",
             "fsmn-vad": "fsmn-vad", "ct-punc": "ct-punc"}


def torch_model_file(d: Path) -> Path | None:
    """目录里有官方 funasr(torch 引擎)能加载的权重吗:仅认 model.pt / model.pb。

    ⚠ ONNX 导出(model_quant.onnx / model.onnx)是 funasr_onnx 的格式,torch 引擎读不了。
    """
    for name in ("model.pt", "model.pb"):
        p = d / name
        if p.is_file():
            return p
    return None


def pick(model: str) -> str:
    """本地模型目录 → 仅当含 torch 权重时才用;否则回落 modelscope 短名。

    短名(paraformer-zh / fsmn-vad / ct-punc)首次运行由 funasr 自动从 ModelScope
    下载到 ~/.cache/modelscope(~1GB,一次性),之后离线可用。
    """
    md = models_dir()
    d = md / model
    if d.is_dir() and torch_model_file(d):
        return str(d)
    return HUB_ALIAS.get(model, model)


def transcribe_onnx(wav: Path, model: str, vad_model: str, punc_model: str,
                    threads: int, max_end_sil: int = VAD_MAX_END_SIL) -> tuple[list[dict], dict]:
    sys.path.insert(0, str(REPO / "tools"))
    from asr_vendor.paraformer_bin import Paraformer
    from asr_vendor.vad_bin import Fsmn_vad
    from asr_vendor.utils.wav_io import load_wav

    md = models_dir()
    t0 = time.time()
    vad = Fsmn_vad(str(md / vad_model), quantize=_quant_flag(md / vad_model),
                   intra_op_num_threads=threads, max_end_sil=max_end_sil)
    raw_segs = vad(str(wav)) or []
    vad_sec = time.time() - t0

    spans = []
    for item in raw_segs:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            a, b = float(item[0]), float(item[1])
        elif isinstance(item, dict):
            a, b = float(item.get("start", 0)), float(item.get("end", 0))
        else:
            continue
        if b > a:
            spans.append((a, b))
    if not spans:
        return [], {"vadSec": round(vad_sec, 2), "vadSpans": 0}

    t1 = time.time()
    quant = _quant_flag(md / model)
    asr = Paraformer(str(md / model), quantize=quant, intra_op_num_threads=threads)
    samples = load_wav(str(wav))
    sr = 16000
    segments = []
    for a_ms, b_ms in spans:
        a, b = int(a_ms / 1000 * sr), int(b_ms / 1000 * sr)
        chunk = samples[a:b]
        if len(chunk) < sr // 4:                       # <0.25s 忽略
            continue
        res = asr(chunk)
        text = "".join(r.get("preds", "") for r in res if isinstance(r, dict)).strip()
        if text:
            segments.append({"start": round(a_ms / 1000, 3), "end": round(b_ms / 1000, 3),
                             "text": text, "timestamp": None})
    asr_sec = time.time() - t1

    punc_note = "未启用"
    if punc_model and (md / punc_model).is_dir():
        try:
            from asr_vendor.punc_bin import CT_Transformer
            pun = CT_Transformer(str(md / punc_model), intra_op_num_threads=threads)
            for s in segments:
                out = pun(s["text"])
                # CT_Transformer 返回 (加标点文本, 标点 id 序列) —— 取 [0]
                val = None
                if isinstance(out, (list, tuple)) and out and isinstance(out[0], str):
                    val = out[0]
                elif isinstance(out, str):
                    val = out
                if val:
                    s["text"] = "".join(val) if isinstance(val, (list, tuple)) else str(val)
            punc_note = "已启用"
        except Exception as exc:  # noqa: BLE001 — 标点失败不该让转写失败
            punc_note = f"失败({exc})"

    return segments, {"vadSec": round(vad_sec, 2), "asrSec": round(asr_sec, 2),
                      "vadSpans": len(spans), "quantized": quant, "punc": punc_note}


PUNCT_CHARS = "，。、；：！？…,.!?;:“”‘’（()《》<>【】[]「」『』—－–-·~～"


def _align_ts_to_text(text: str, ts: list) -> list | None:
    """把 funasr 的 timestamp(**只覆盖发音字**)对齐到**含标点**的 text。

    实测(funasr 1.4.15 + SeACo-Paraformer):text 去空格 334 字含 31 个标点,
    timestamp 恰好 303 条 = 334 - 31。标点字符继承前一个发音字的时间(零宽);
    开头的标点继承后一个发音字。对不上(英文混排等)返回 None,由调用方降级。
    """
    dense = text.replace(" ", "")
    if not ts:
        return None
    if len(ts) != sum(1 for c in dense if c not in PUNCT_CHARS):
        return None
    out, it, prev = [], iter(ts), None
    for ch in dense:
        if ch in PUNCT_CHARS:
            out.append(list(prev) if prev else None)
            continue
        t = next(it, None)
        if t is None:
            return None
        pair = [int(t[0]), int(t[1])]
        out.append(pair)
        prev = pair
    nxt = None                                  # 回填开头标点
    for i in range(len(out) - 1, -1, -1):
        if out[i] is None:
            out[i] = list(nxt) if nxt else list(prev or [0, 0])
        else:
            nxt = out[i]
    return out


def transcribe_pkg(wav: Path, model: str, vad_model: str, punc_model: str,
                   hotwords: str) -> tuple[list[dict], dict]:
    from funasr import AutoModel

    t0 = time.time()
    m = AutoModel(model=pick(model), vad_model=pick(vad_model),
                  punc_model=pick(punc_model), disable_update=True, device="cpu")
    kwargs = {"input": str(wav), "batch_size_s": SEGMENT_SEC}
    if hotwords:
        kwargs["hotword"] = hotwords
    res = m.generate(**kwargs)
    elapsed = time.time() - t0

    dur_ms = wav_duration(wav) * 1000
    segments: list[dict] = []
    for item in res if isinstance(res, list) else [res]:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        ts = _norm_ts(item.get("timestamp"), dur_ms)
        dense = text.replace(" ", "")
        aligned = _align_ts_to_text(text, ts) if ts else None
        if aligned:
            # 字级时间戳已对齐到含标点的文本(标点零宽,继承相邻发音字)
            segments.append({"start": aligned[0][0] / 1000.0, "end": aligned[-1][1] / 1000.0,
                             "text": dense, "timestamp": aligned})
        else:
            segments.append({"start": float(item.get("start", 0) or 0),
                             "end": float(item.get("end", dur_ms / 1000) or dur_ms / 1000),
                             "text": text, "timestamp": None})
    return segments, {"pkgSec": round(elapsed, 2), "raw": len(res) if isinstance(res, list) else 1}


def transcribe_server(wav: Path, model: str) -> tuple[list[dict], dict]:
    import mimetypes
    import uuid

    url = (asr_cfg().get("url") or "http://127.0.0.1:8000").rstrip("/")
    boundary = uuid.uuid4().hex
    ctype = mimetypes.guess_type(wav.name)[0] or "application/octet-stream"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{wav.name}"\r\n'.encode(),
        f"Content-Type: {ctype}\r\n\r\n".encode(), wav.read_bytes(), b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="model"\r\n\r\n', model.encode(), b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="structured"\r\n\r\n', b"1", b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="char_timestamps"\r\n\r\n', b"1", b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(f"{url}/v1/audio/transcriptions", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"ASR 服务调用失败({url}):{exc}") from exc
    segs = doc.get("segments") if isinstance(doc, dict) else None
    if segs:
        return segs, {"server": url}
    if isinstance(doc, dict) and doc.get("text"):
        return [{"start": 0.0, "end": float(doc.get("duration") or 0),
                 "text": doc["text"], "timestamp": None}], {"server": url}
    raise RuntimeError(f"ASR 返回结构无法识别:{str(doc)[:200]}")


# ---------------------------------------------------------------- CLI

def probe() -> int:
    report = {}
    for name, fn in BACKENDS.items():
        ok, detail = fn()
        report[name] = {"ready": ok, "detail": detail, "capabilities": CAPS[name]}
    order = [n for n in ("pkg", "onnx", "server") if report[n]["ready"]]
    if order:
        msg = f"可用后端:{' → '.join(order)}(默认 {order[0]})"
        if order[0] == "onnx" and "pkg" not in order:
            msg += ";注意 onnx 后端无字级时间戳(装 pkg:python tools/fetch_deps.py asr --pkg)"
    else:
        msg = "无可用后端。至少装一个:python tools/fetch_deps.py asr --onnx(轻量)或 --pkg(有字级)"
    return emit(bool(order), "PROBE_OK" if order else "NO_BACKEND", msg,
                {"backends": report, "modelsDir": str(models_dir()),
                 "venv": str(ASR_VENV), "python": sys.executable})


def main() -> int:
    maybe_reexec()
    ap = argparse.ArgumentParser()
    ap.add_argument("media", nargs="?")
    ap.add_argument("--backend", default="auto", choices=["auto", "pkg", "onnx", "server"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--vad", default=DEFAULT_VAD)
    ap.add_argument("--punc", default=DEFAULT_PUNC)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--max-end-sil", dest="max_end_sil", type=int, default=VAD_MAX_END_SIL,
                    help=f"VAD 静音切分阈值 ms(默认 {VAD_MAX_END_SIL};越小段越细,时间粒度越好)")
    ap.add_argument("--hotwords", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--keep-wav", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--json", action="store_true",
                    help="兼容其他 rs_*.py 的约定;stdout 本来就始终是 JSON 协议")
    a = ap.parse_args()

    if a.probe or not a.media:
        return probe()

    src = Path(a.media)
    if not src.is_file():
        return emit(False, "NO_MEDIA", f"素材不存在:{src}", exit_code=EXIT_INPUT)
    model = a.model or asr_cfg().get("model") or DEFAULT_MODEL

    backend, report = resolve_backend(a.backend)
    if backend is None:
        fail = report.pop("_fail", None)
        hint = fail or "；".join(f"{k}: {v['detail']}" for k, v in report.items())
        return emit(False, "NO_BACKEND",
                    f"没有可用的 ASR 后端({a.backend})。{hint}", {"backends": report},
                    exit_code=EXIT_DEP)

    tmp = Path(tempfile.mkdtemp(prefix="cutflow-asr-"))
    wav = tmp / "audio16k.wav"
    t0 = time.time()
    try:
        to_wav16k(src, wav)
    except RuntimeError as exc:
        return emit(False, "FFMPEG_FAIL", str(exc), exit_code=EXIT_EXEC)

    try:
        if backend == "onnx":
            segments, meta = transcribe_onnx(wav, model, a.vad, a.punc, a.threads,
                                             a.max_end_sil)
        elif backend == "pkg":
            segments, meta = transcribe_pkg(wav, model, a.vad, a.punc, a.hotwords)
        else:
            segments, meta = transcribe_server(wav, model)
    except ModuleNotFoundError as exc:
        return emit(False, "DEP_MISSING",
                    f"{backend} 后端缺依赖:{exc.name}。装:python tools/fetch_deps.py asr "
                    f"--{backend if backend != 'server' else 'onnx'}", exit_code=EXIT_DEP)
    except Exception as exc:  # noqa: BLE001
        return emit(False, "ASR_FAIL", f"{backend} 后端推理失败:{exc}", exit_code=EXIT_EXEC)
    finally:
        if not a.keep_wav:
            shutil.rmtree(tmp, ignore_errors=True)

    chars = sum(len(s["text"]) for s in segments)
    dur = wav_duration(wav) if a.keep_wav else 0.0
    caps = CAPS[backend]
    degraded = not caps["charTimestamps"]
    reasons = ["onnx 后端无字级时间戳(ONNX 导出未含 timestamp),字级对齐将降级为段内均分"] \
        if degraded and backend == "onnx" else []
    if not segments:
        reasons.append("VAD 未检出有效语音段")

    elapsed = round(time.time() - t0, 2)
    data = {"backend": backend, "model": model, "capabilities": caps,
            "segments": segments, "text": "".join(s["text"] for s in segments),
            "degraded": degraded, "degradeReasons": reasons,
            "charCount": chars, "segmentCount": len(segments),
            "elapsedSec": elapsed, "python": sys.executable, **meta}
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        data["out"] = str(out)

    msg = (f"[{backend}] {len(segments)} 段 / {chars} 字,耗时 {elapsed}s"
           + (f"·{dur:.1f}s 音频" if dur else ""))
    if degraded:
        msg += " ⚠ 无字级时间戳(降级)"
    return emit(True, "ASR_OK", msg, data)


if __name__ == "__main__":
    sys.exit(main())
