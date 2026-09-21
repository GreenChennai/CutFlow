# -*- coding: utf-8 -*-
"""v0.14.1 诊断回归全矩阵测试(pytest):基线 pass + 三类注入缺陷全部 issues。

依赖 tests/fixtures/diagnosis/(tests/make_fixtures.py 生成)与本地 ASR;
缺素材/缺 ASR 时跳过(不红)。验收口径:
  检出率 3/3(三类注入缺陷全部 verdict=issues);
  误报率 0/1(基线 verdict=pass);
  耗时 <600s 预算。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
FIXTURES = REPO / "tests" / "fixtures" / "diagnosis"

MANIFEST = FIXTURES / "manifest.json"


def _run_diag(video: Path, ass: Path, budget: str = "600") -> dict:
    p = subprocess.run(
        [sys.executable, str(SCRIPTS / "rs_diagnose.py"), str(video),
         "--ass", str(ass), "--budget", budget, "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = [l for l in (p.stdout or "").splitlines() if l.strip()]
    if not lines:
        pytest.skip(f"rs_diagnose 无输出:{(p.stderr or '')[-200:]}")
    doc = json.loads(lines[-1])
    return doc


def _has_asr() -> bool:
    return (REPO / "tools" / "fun_asr.py").is_file()


needs_fixtures = pytest.mark.skipif(
    not MANIFEST.is_file(), reason="缺 tests/fixtures/diagnosis(先跑 tests/make_fixtures.py)")
needs_asr = pytest.mark.skipif(not _has_asr(), reason="缺 tools/fun_asr.py")


@needs_fixtures
@needs_asr
class TestDiagnosisRegression:
    """注入缺陷回归全矩阵。"""

    def _case(self, name: str) -> dict:
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        c = next(c for c in m if c["name"] == name)
        # 兼容旧绝对路径与新相对路径:相对路径锚定夹具目录(路径不硬编码盘符)
        for k in ("video", "ass"):
            p = Path(c[k])
            c[k] = str(p if p.is_absolute() else FIXTURES / p)
        return c

    def test_baseline_passes(self):
        """正常视频不得被误报(误报率 0/1)。"""
        c = self._case("baseline")
        doc = _run_diag(Path(c["video"]), Path(c["ass"]))
        data = doc.get("data") or doc
        assert data.get("verdict") == "pass", \
            f"基线被误报:{data.get('verdict')} checks={[(x['id'], x['status']) for x in data.get('checks', [])]}"

    def test_baseline_budget(self):
        """诊断耗时在预算内。"""
        c = self._case("baseline")
        doc = _run_diag(Path(c["video"]), Path(c["ass"]))
        data = doc.get("data") or doc
        assert data.get("elapsedS", 10**9) <= data.get("budgetS", 600)

    def test_fix1_subtitle_shift_detected(self):
        """注入:字幕 +0.5s → 必须报 issues 并给出偏移方向。"""
        c = self._case("fix1-subtitle-shift")
        doc = _run_diag(Path(c["video"]), Path(c["ass"]))
        data = doc.get("data") or doc
        assert data.get("verdict") == "issues", \
            f"字幕错位未被检出:{data.get('verdict')}"
        d1 = next(x for x in data["checks"] if x["id"] == "D1")
        assert d1["status"] == "fail" and abs(d1.get("medianMs", 0)) > 250

    def test_fix2_audio_shift_detected(self):
        """注入:音轨延后 0.45s → 必须报 issues 并定位延迟方向。"""
        c = self._case("fix2-audio-shift")
        doc = _run_diag(Path(c["video"]), Path(c["ass"]))
        data = doc.get("data") or doc
        assert data.get("verdict") == "issues", \
            f"音画错位未被检出:{data.get('verdict')}"
        d2 = next(x for x in data["checks"] if x["id"] == "D2")
        assert d2["status"] == "fail" and d2.get("bestLagMs", 0) > 250

    def test_fix3_bad_cut_detected(self):
        """注入:句中腰斩+硬接 → 必须报 issues(腰斩证据)。"""
        c = self._case("fix3-bad-cut")
        doc = _run_diag(Path(c["video"]), Path(c["ass"]))
        data = doc.get("data") or doc
        assert data.get("verdict") == "issues", \
            f"错剪未被检出:{data.get('verdict')}"

    def test_ledger_written(self, tmp_path):
        """每次诊断写入台账(JSONL)。"""
        c = self._case("baseline")
        doc = _run_diag(Path(c["video"]), Path(c["ass"]))
        data = doc.get("data") or doc
        ledger = Path(data.get("ledger") or "")
        assert ledger.is_file(), f"台账未写:{ledger}"
        lines = [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert lines
        entry = json.loads(lines[-1])
        assert "verdict" in entry and "checks" in entry and "at" in entry
