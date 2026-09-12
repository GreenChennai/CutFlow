"""v7 端到端集成测试(lavfi 合成素材,不需要外部 ASR 模型)。

覆盖:S0 摄取 → S1 对齐 → S2 粗剪 → S3 IR → S7 字幕(3:4 平台预设) → S8 渲染 → S8 对齐自检
      → L0 自检 → 交付清单。

没有 ffmpeg 时整模块跳过(CI/裸机不会红)。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _ffmpeg() -> str | None:
    try:
        import rs_common
        cand = rs_common.ffmpeg_bin()
    except SystemExit:                     # 缺 config.json
        cand = "ffmpeg"
    return shutil.which(cand) or (cand if Path(cand).is_file() else None)


FFMPEG = _ffmpeg()
pytestmark = pytest.mark.skipif(not FFMPEG, reason="本机没有 ffmpeg,跳过端到端")


def _run(*args: str, cwd: Path) -> dict:
    p = subprocess.run([sys.executable, str(SCRIPTS / args[0]), *args[1:]], cwd=cwd,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip().splitlines()
    doc = json.loads(out[-1]) if out else {"ok": False, "code": "NO_OUTPUT", "message": p.stderr}
    assert doc.get("ok"), f"{args[0]} 失败:{doc.get('code')} {doc.get('message')}"
    return doc


def _seg(text: str, start_ms: int, per: int = 200) -> dict:
    ts = [[start_ms + i * per, start_ms + i * per + per - 20] for i in range(len(text))]
    return {"start": start_ms / 1000.0, "end": (start_ms + len(text) * per) / 1000.0,
            "text": text, "timestamp": ts, "conf": 0.95}


def test_end_to_end_3x4_pipeline(tmp_path):
    root = tmp_path / "e2e"
    (root / "01_materials").mkdir(parents=True)
    (root / "00_brief").mkdir(parents=True)
    (root / "02_sensed").mkdir(parents=True)
    (root / "00_brief" / "brief.md").write_text(
        "# Brief — e2e\n- videoType:`talking-head`\n- 比例/平台预设:`xiaohongshu`\n", encoding="utf-8")

    # 1) lavfi 合成素材(8s,带音轨 —— 让 dead_air 的音频路径真的跑一遍)
    media = root / "01_materials" / "sample.mp4"
    subprocess.run([FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", "8", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(media)], check=True)

    # 2) 手写转写稿(等价于 ASR 产物:句级 + 字级时间戳)
    segs = [_seg("大家好今天我们讲桌面运维", 0),
            _seg("先看蓝屏这是最常见的故障", 2600),
            _seg("然后把内存条拔下来再插回去", 5200)]
    (root / "02_sensed" / "transcript.json").write_text(
        json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8")

    # 3) S0 摄取
    ing = _run("rs_ingest.py", "scan", str(root), cwd=root)
    assert ing["data"]["count"] == 1 and not ing["data"]["failed"]
    assert (root / "01_materials" / "manifest.json").is_file()

    # 4) S1 对齐(--src 把 wordline.source 指到真实素材,否则 IR 会拿转写稿当视频)
    _run("rs_align.py", "build", "--from-transcript", "02_sensed/transcript.json",
         "--src", "01_materials/sample.mp4", "--out", "05_ir/wordline.json", cwd=root)

    # 5) S2 粗剪(带 --media 走音频能量探测)
    cut = _run("rs_cut.py", "05_ir/wordline.json", "--detect", "all", "--out", "04_cut",
               "--media", "01_materials/sample.mp4", cwd=root)
    assert "srcTotalMs" in cut["data"]
    _run("rs_cut.py", "--apply", "04_cut/cutlist.json", cwd=root)

    # 6) 重映射 + S3 IR
    _run("rs_align.py", "remap", "05_ir/wordline.json",
         "--cutlist", "04_cut/cutlist.applied.json", "--out", "05_ir/wordline.final.json", cwd=root)
    _run("rs_ir.py", "build", "--from-cutlist", "04_cut/cutlist.applied.json",
         "--slug", "e2e", "--ratio", "3x4", "--out", "05_ir/project.json", cwd=root)
    ir = json.loads((root / "05_ir" / "project.json").read_text(encoding="utf-8"))
    assert ir["canvas"] == {"width": 1080, "height": 1440} and ir["outputs"] == ["3x4"]

    # 7) S7 字幕(平台预设 xiaohongshu → 3:4 / 15 字)
    sub = _run("rs_subtitle.py", "--from-wordline", "05_ir/wordline.final.json",
               "--platform", "xiaohongshu", "--out", "06_output", cwd=root)
    assert sub["data"]["ratio"] == "3x4" and sub["data"]["maxChars"] == 15
    assert sub["data"]["degraded"] is False, "本用例给的是真实字级时间戳,不该降级"

    # 8) 对齐自检(起点 + 终点 + 动画卡重叠)
    sync = _run("rs_sync.py", "--wordline", "05_ir/wordline.final.json",
                "--ass", "06_output/subtitles.ass", "--out", "06_output",
                "--ir", "05_ir/project.json", cwd=root)
    assert sync["data"]["pass"] is True, sync["data"]

    # 9) S8 渲染(3:4 画幅真出片)
    mp4 = list((root / "06_output").glob("*.mp4"))
    _run("rs_render.py", "05_ir/project.json", "--ratio", "3x4", "--profile", "draft", cwd=root)
    mp4 = sorted(p for p in (root / "06_output").glob("*.mp4"))
    assert mp4, "必须产出成片"

    # 10) L0 自检 + 交付清单
    ver = _run("rs_verify.py", str(root), cwd=root)
    assert ver["data"]["pass"] is True, ver["data"].get("failed")
    deliv = _run("rs_ingest.py", "deliverables", str(root), cwd=root)
    assert deliv["data"]["ratio"] == "3x4"
    assert (root / "06_output" / "deliverables.md").is_file()
