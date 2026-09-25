# -*- coding: utf-8 -*-
"""性能预算门禁(计划书 §6.2/§6.3 门禁 16;全部 @pytest.mark.perf,可 CI 单独跑):

· rs_edit context 对夹具工程 ≤1.5s、输出 ≤12KB、绝不含视频帧路径(性能设计守门)
· rs_edit apply 20 条 op 序列总耗时(§6.2 预算 ≤300ms/op,实测数字打印留档)
· rs_run --status 在无改动工程上的耗时(§6.2 预算 ≤1s,实测数字打印留档)

运行:pytest tests/test_perf_budget.py -q(或 -m perf 单独跑)
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.perf   # 整模块性能档:不在每次提交阻塞,CI 可 -m perf 单独跑

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_paths  # noqa: E402
import rs_edit  # noqa: E402
import rs_run  # noqa: E402

BUDGET_S = 1.5                  # §6.2:context ≤1.5s(CI 抖动余量见各断言)
BUDGET_BYTES = 12 * 1024        # §6.2:context 视图 ≤12KB(硬上限)
FRAME_PATH_RE = re.compile(r"\.(png|jpe?g)\b", re.I)
# 帧目录字样(词边界:reframe/-transition 这类 op 名里的 "frame" 不算)
FRAME_DIR_RE = re.compile(r"(?<![A-Za-z])frames?[/_\\]|抽帧|帧目录", re.I)


def _capture(fn, *args, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    doc = json.loads(lines[-1]) if lines else {}
    return code, doc, buf.getvalue()


def _capture_argv(fn, argv: list[str]):
    old = sys.argv
    sys.argv = list(argv)
    try:
        return _capture(fn)
    finally:
        sys.argv = old


def _mk_project(tmp_path: Path, n_clips: int, tag: str) -> Path:
    """带完整阶段目录的夹具工程(主轨 n 段 + 音频 + bgm),供视图/状态评估。"""
    root = tmp_path / tag
    for d in ("00_制作简报", "01_原始素材", "02_转写与校对", "03_创作素材",
              "04_粗剪决策", "05_时间线工程", "06_成片输出", "_内部状态"):
        (root / d).mkdir(parents=True)
    (root / "01_原始素材" / "a.mp4").write_bytes(b"fake")
    clips = [{"id": f"V1-{i + 1:03d}", "src": "01_原始素材/a.mp4",
              "startMs": i * 4000, "durationMs": 4000, "sourceInMs": i * 4000,
              "text": f"第{i + 1}段:这是一条用于撑起视图规模的测试旁白文本"}
             for i in range(n_clips)]
    ir = {
        "version": 1, "slug": tag, "fps": 30,
        "canvas": {"width": 1080, "height": 1920},
        "tracks": [
            {"id": "V1", "kind": "video", "name": "main", "clips": clips},
            {"id": "A1", "kind": "audio", "clips": [
                {"id": "A1-001", "src": "02_转写与校对/voice.wav", "startMs": 0,
                 "durationMs": n_clips * 4000, "role": "voice"}]},
        ],
        "bgm": {"src": "03_创作素材/BGM/原曲.mp3", "gainDb": -18, "ducking": True},
        "outputs": ["9x16"],
    }
    rs_paths.project_json(root).write_text(rs_edit.dump_json(ir), encoding="utf-8")
    (root / rs_paths.p("cut") / "cutlist.json").write_text(
        rs_edit.dump_json({"version": 1, "source": "perf", "detector": "rs_cut",
                           "cuts": [], "keep": [], "removedMs": 0,
                           "srcTotalMs": n_clips * 4000, "script": "rs_cut"}),
        encoding="utf-8")
    return root


# ================================================================ 门禁 16:性能预算

def test_perf_context_time_size_and_no_frame_paths(tmp_path):
    """context:≤1.5s、≤12KB、零视频帧路径(把「抽帧塞回视图」的门)。"""
    root = _mk_project(tmp_path, n_clips=24, tag="perf-ctx")
    t0 = time.perf_counter()
    code, doc, _ = _capture(rs_edit.main, ["context", str(root), "--json"])
    elapsed = time.perf_counter() - t0
    assert code == 0 and doc["code"] == "CONTEXT_OK"
    markdown = doc["data"]["markdown"]
    print(f"\n[perf] rs_edit context:{elapsed * 1000:.0f} ms "
          f"(预算 ≤{BUDGET_S}s);输出 {doc['data']['bytes']} B(预算 ≤{BUDGET_BYTES} B)")
    assert elapsed <= BUDGET_S, f"context 耗时 {elapsed:.2f}s 超出 §6.2 预算 1.5s"
    assert doc["data"]["bytes"] <= BUDGET_BYTES, \
        f"视图 {doc['data']['bytes']} B 超出 12KB 预算"
    hits = FRAME_PATH_RE.findall(markdown) + FRAME_DIR_RE.findall(markdown)
    assert not hits, f"context 视图混入了视频帧路径/帧目录字样:{hits[:5]}"


def test_perf_context_default_budget_cap(tmp_path):
    """默认预算(12KB)下,超大工程的视图也必须被裁进预算内且声明被裁内容。"""
    root = _mk_project(tmp_path, n_clips=120, tag="perf-ctx-big")
    code, doc, _ = _capture(rs_edit.main, ["context", str(root), "--json"])
    assert code == 0
    assert doc["data"]["bytes"] <= BUDGET_BYTES
    assert doc["data"]["trimmed"], "超限必须声明被裁内容(不静默截断)"


def test_perf_apply_20_ops(tmp_path):
    """apply 20 条 op 序列:记录总耗时与每 op 均摊(§6.2 预算 ≤300ms/op)。"""
    root = _mk_project(tmp_path, n_clips=24, tag="perf-apply")
    ops_body = []
    for i in range(20):
        cid = f"V1-{(i % 24) + 1:03d}"
        ops_body.append({"op": "clip.trim", "target": cid,
                         "after": {"durationMs": 3500 + i},
                         "reason": f"perf 第 {i + 1} 刀"})
    for i in range(20):
        cid = f"V1-{(i % 24) + 1:03d}"
        ops_body.append({"op": "clip.motion", "target": cid,
                         "after": {"in": "fadeIn", "inMs": 300},
                         "reason": f"perf 入场 {i + 1}"})
    p = tmp_path / "ops.json"
    p.write_text(json.dumps({"baseRev": 0, "ops": ops_body}, ensure_ascii=False),
                 encoding="utf-8")
    t0 = time.perf_counter()
    code, doc, _ = _capture(rs_edit.main, ["apply", str(root), "--ops", str(p)])
    elapsed = time.perf_counter() - t0
    assert code == 0 and doc["code"] == "APPLY_OK", doc
    per_op = elapsed / len(ops_body)
    print(f"\n[perf] rs_edit apply 40 op:{elapsed * 1000:.0f} ms 总耗,"
          f"均摊 {per_op * 1000:.1f} ms/op(§6.2 预算 ≤300ms/op)")
    assert per_op <= 1.0, f"apply 均摊 {per_op * 1000:.0f}ms/op 明显劣化(预算 300ms/op)"


def test_perf_rs_run_status_unchanged_project(tmp_path):
    """rs_run --status 在无改动工程上的耗时(§6.2 预算 ≤1s;实测留档)。"""
    root = _mk_project(tmp_path, n_clips=8, tag="perf-status")
    # 落两个已完成阶段的状态账(模拟跑过 S1/S2 的工程;账内含 outHash)
    for sid in ("S1", "S2"):
        st = next(s for s in rs_run.spec() if s["id"] == sid)
        parts = rs_run.stage_parts(root, st, rs_run.params_of(root), {})
        rs_run.write_state(root, sid, {"status": "done", "key": rs_run.key_of(parts),
                                       "parts": parts,
                                       "outHash": rs_run.outputs_hash(root, st)})
    t0 = time.perf_counter()
    code, doc, _ = _capture_argv(rs_run.main,
                                 ["rs_run.py", "--root", str(root), "--status"])
    elapsed = time.perf_counter() - t0
    assert code == 0, doc
    print(f"\n[perf] rs_run --status(无改动):{elapsed * 1000:.0f} ms(§6.2 预算 ≤1s)")
    assert elapsed <= 5.0, f"--status 耗时 {elapsed:.2f}s 明显劣化(§6.2 预算 1s)"
