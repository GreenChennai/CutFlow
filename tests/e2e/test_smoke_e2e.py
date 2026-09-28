# -*- coding: utf-8 -*-
"""T5.3c 冒烟级端到端:合成素材走 S0→S11 核心链(经 rs_run 阶段引擎),验证 e2e
框架与幂等收敛。真实素材全链由 C 组按 tests/e2e/README.md 约定填入。

链路约定(与 test_v7_e2e 同口径,但全程经 rs_run 阶段引擎而非散装命令):
  1. lavfi 合成 8s 带音轨素材(320x240,≤10s 红线);
  2. `rs_run --only S0 --auto` 先行摄取(必须在 S1 落账前 —— scan 重写
     manifest.json 会打脏 S1 输入组),再以手写转写稿走离线 S1 通道
     (rs_align --from-transcript,不依赖 ASR 服务)+ `--mark S1` 落账
     (账是诚实的:key/outHash 按盘上真实 wordline 计算);
  3. `rs_run --from S2 --auto` 跑主链:S2 自动 apply+remap,S4 无卡片计划自动
     标记无事可做,S5 无变体声明按留痕跳过(缺声明宁可漏做不猜),S11 交付
     对账(封面属 Agent 语义产物,如实留痕不代劳);
  4. 幂等收敛:`rs_run --from S0 --auto` 全阶段扫描,自动可缓存阶段 8/8 全命中
     (含离线落账的 S1),S5 仍按声明跳过,无任何阶段失败 —— 增量引擎
     "第二次全 cached"的承诺在这里兑现。

产物全部写 pytest tmp_path;标记 e2e(默认套件排除),ffmpeg 缺失整模块 SKIP。
运行:pytest tests/e2e/test_smoke_e2e.py -m e2e -v
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_paths  # noqa: E402


def _ffmpeg_bin() -> str:
    try:
        import rs_common
        cand = rs_common.ffmpeg_bin()
        if Path(cand).is_file():
            return cand
    except SystemExit:                       # 缺 config.json 时回退 PATH
        pass
    return shutil.which("ffmpeg") or ""


FF = _ffmpeg_bin()
pytestmark = [pytest.mark.e2e,
              pytest.mark.skipif(not FF, reason="本机没有 ffmpeg,跳过端到端")]

FPS, DUR_S = 30, 8
SENTENCES = [("大家好今天我们讲桌面运维", 0),
             ("先看蓝屏这是最常见的故障", 2600),
             ("然后把内存条拔下来再插回去", 5200)]


def _run(script: str, *args: str, cwd: Path) -> dict:
    """跑 skills 脚本,断言 emit 协议 ok,返回最后一行 JSON。"""
    p = subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=str(cwd),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=900)
    out = (p.stdout or "").strip().splitlines()
    doc = json.loads(out[-1]) if out else {"ok": False, "code": "NO_OUTPUT",
                                           "message": p.stderr[-300:]}
    assert doc.get("ok"), (f"{script} {' '.join(args)} 失败:{doc.get('code')} "
                           f"{doc.get('message')}\n{p.stderr[-400:]}")
    return doc


def _rs_run(*args: str, cwd: Path) -> dict:
    return _run("rs_run.py", "--root", str(cwd), *args, cwd=cwd)


def _seg(text: str, start_ms: int, per: int = 200) -> dict:
    ts = [[start_ms + i * per, start_ms + i * per + per - 20] for i in range(len(text))]
    return {"start": start_ms / 1000.0, "end": (start_ms + len(text) * per) / 1000.0,
            "text": text, "timestamp": ts, "conf": 0.95}


@pytest.fixture(scope="module")
def smoke_proj(tmp_path_factory) -> dict:
    """冒烟工程:合成素材 + 离线 wordline → --auto 全链 → 二次 --auto 收敛。"""
    root = tmp_path_factory.mktemp("smoke_e2e") / "smoke"
    # 工程名用 ASCII:ffmpeg 命令行直连素材路径,Windows 代码页下非 ASCII
    # tmp 路径会让 lavfi 合成失败;中文路径兼容由 rs_* 层与 test_cn_paths 把守。
    (root / rs_paths.p("brief")).mkdir(parents=True)
    (root / rs_paths.p("sensed")).mkdir(parents=True)
    (root / rs_paths.p("brief") / "brief.md").write_text(
        "# Brief — e2e 冒烟\n- videoType:`talking-head`\n- 比例/平台预设:`xiaohongshu`\n",
        encoding="utf-8")
    # 1) lavfi 合成素材(带音轨,让粗剪/渲染的音频路径真实跑一遍)
    media = root / rs_paths.p("materials") / "sample.mp4"
    media.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FF, "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"testsrc=size=320x240:rate={FPS}",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", str(DUR_S), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(media)], check=True,
                   capture_output=True)
    # 2) 先跑 S0(摄取):rs_ingest scan 会重写 manifest.json —— 必须发生在
    #    S1 落账之前,否则 S1 的输入组(manifest 在 materials/* 内)会被打脏,
    #    触发 S1 重跑(真 ASR 通道)冲掉离线 wordline。
    _rs_run("--only", "S0", "--auto", cwd=root)
    # 3) 手写转写稿(等价 ASR 产物:句级 + 字级时间戳)→ 离线 S1 通道建 wordline
    segs = [_seg(t, s) for t, s in SENTENCES]
    (root / rs_paths.p("sensed") / "transcript.json").write_text(
        json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8")
    _run("rs_align.py", "build",
         "--from-transcript", f"{rs_paths.p('sensed')}/transcript.json",
         "--src", f"{rs_paths.p('materials')}/sample.mp4",
         "--out", f"{rs_paths.p('timeline')}/wordline.json", cwd=root)
    _rs_run("--mark", "S1", cwd=root)
    # 4) --auto 主链(S2→S11;S1 已落账故从 S2 起)
    first = _rs_run("--from", "S2", "--auto", cwd=root)
    # 5) 幂等收敛:--from S0 让完整阶段表进 todo,已完成的阶段必须全部走缓存
    second = _rs_run("--from", "S0", "--auto", cwd=root)
    status = _rs_run("--status", cwd=root)
    return {"root": root, "first": first, "second": second, "status": status}


def test_smoke_full_chain_runs_ok(smoke_proj):
    """主链 RUN_OK:S2→S11 无一失败;S5 无变体声明按留痕跳过(不许静默硬跑);
    S0/S1(先行单独执行/落账)由状态灯与收敛测试覆盖。"""
    results = {r["stage"]: r for r in smoke_proj["first"]["data"]["results"]}
    assert set(results) == {f"S{i}" for i in range(2, 12)}, sorted(results)
    failed = [sid for sid, r in results.items() if not r.get("ok")]
    assert failed == [], {sid: results[sid] for sid in failed}
    s5 = results["S5"]
    assert s5.get("skipped") and "变体" in s5["skipped"], s5


def test_smoke_core_artifacts_on_disk(smoke_proj):
    """核心产物在盘:字幕 / 成片 / 对账 / 元数据 / 交付清单 + 决策留痕。"""
    root = smoke_proj["root"]
    out = root / rs_paths.p("output")
    assert (out / "subtitles.ass").is_file(), "S7 字幕必须产出"
    finals = list((out / "final").glob("final_*.mp4")) or list(out.glob("final_*.mp4"))
    assert finals, "S8 必须烧录出成片"
    assert (out / "sync_report.md").is_file() and (out / "sync_rows.json").is_file(), \
        "S9 对账双产物必须在盘"
    assert (out / "metadata.json").is_file(), "S10 元数据必须产出"
    assert (out / "deliverables.md").is_file(), "S11 交付清单必须产出"
    pipeline = json.loads((root / rs_paths.p("timeline") / "pipeline.json")
                          .read_text(encoding="utf-8"))
    decisions = pipeline.get("decision_log") or []
    assert any(d.get("id", "").startswith("auto:S2") for d in decisions), \
        "--auto 的 S2 自动裁决(apply+remap)必须留痕"


def test_smoke_idempotent_second_run_all_cached(smoke_proj):
    """幂等收敛:--from S0 全阶段扫描无一失败,自动可缓存阶段 9/9 全部命中
    (S0/S4/S11 人工处置、S5 声明跳过,不计入缓存口径);离线落账的 S1 也在
    缓存命中之列 —— 账实相符的直接证据。"""
    second = smoke_proj["second"]
    results = {r["stage"]: r for r in second["data"]["results"]}
    assert set(results) == {f"S{i}" for i in range(12)}, sorted(results)
    assert all(r.get("ok") for r in results.values()), \
        {sid: r for sid, r in results.items() if not r.get("ok")}
    auto_stages = [f"S{i}" for i in range(12) if i not in (0, 4, 5, 11)]
    cached = [sid for sid in auto_stages if results[sid].get("cached")]
    assert len(cached) == len(auto_stages) == 8, \
        f"缓存命中 {len(cached)}/8:{ {sid: results[sid].get('cached') for sid in auto_stages} }"
    assert results["S1"].get("cached"), results["S1"]
    assert results["S5"].get("skipped") and results["S4"].get("manualAuto")
    assert "8 命中缓存" in second["message"], second["message"]


def test_smoke_status_lamps_honest(smoke_proj):
    """状态灯诚实:11/12 done + S5 missing(降级留痕),不许报 12/12 假全绿。"""
    stages = {s["id"]: s for s in smoke_proj["status"]["data"]["stages"]}
    assert stages["S5"]["status"] == "missing", stages["S5"]
    done = [s for s in stages.values() if s["status"] == "done"]
    assert len(done) == 11, {s["id"]: s["status"] for s in stages.values()}
    assert smoke_proj["status"]["code"] == "STATUS_OK"
