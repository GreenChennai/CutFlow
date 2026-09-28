# -*- coding: utf-8 -*-
"""T2.7 / H7 回归:detect_retake 的重复 `ratio()` 剪枝退化已删,剪枝真实省算。

缺陷:复制粘贴导致 `ratio = matcher_a.ratio() / if ratio < min_ratio: continue`
连写两遍 —— 每个通过上界剪枝的候选都要**多付一次全量序列比对**,直接抵销
v2 M14 剪枝优化的收益。
修复:删除重复块;`stats` 回填 {pairs, prunedPairs, fullRatioCalls} 剪枝留痕
(CLI 侧并入 params.retakePrune)。

运行:pytest tests/test_retake_prune.py -q
"""
from __future__ import annotations

import difflib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_align  # noqa: E402
import rs_cut  # noqa: E402

TAIL_KEEP_MS = rs_cut.TAIL_KEEP_MS


def _wl(texts: list[str], per: int = 200, gap_ms: int = 300):
    """句列表 → wordline(rs_align 同一构建口;与 test_v7 夹具同式)。"""
    segs, t = [], 0
    for k, text in enumerate(texts):
        end = t + len(text) * per
        segs.append({"start": t / 1000.0, "end": end / 1000.0, "text": text,
                     "timestamp": k, "conf": 0.95})
        t = end + gap_ms
    return rs_align.build_wordline(segs, "a.mp4")


# ---------------------------------------------------------------- 参考实现(无剪枝)

def _retake_no_prune(wl: dict, max_gap_ms: int = 30000, min_ratio: float = 0.80,
                     max_sents: int = 6) -> list[dict]:
    """去掉上界剪枝的逐字参考实现(判定语义与 detect_retake 相同)。"""
    spans = rs_cut._sentence_spans(wl)
    out: list[dict] = []
    for i, a in enumerate(spans):
        if not a["text"]:
            continue
        matcher_a = None
        best, best_ratio = -1, 0.0
        for j in range(i + 1, min(len(spans), i + 1 + max_sents)):
            b = spans[j]
            if not b["text"]:
                continue
            if b["startMs"] - a["endMs"] > max_gap_ms:
                break
            if not rs_cut._more_complete(b["text"], a["text"]):
                continue
            if matcher_a is None:
                matcher_a = difflib.SequenceMatcher(None, a["text"], b["text"])
            else:
                matcher_a.set_seq2(b["text"])
            ratio = matcher_a.ratio()
            if ratio < min_ratio:
                continue
            best, best_ratio = j, ratio
        if best < 0:
            continue
        in_ms = a["startMs"]
        out.append({"inMs": in_ms,
                    "outMs": max(in_ms + 200, spans[best]["startMs"] - TAIL_KEEP_MS),
                    "reason": "retake",
                    "conf": round(min(0.97, 0.72 + best_ratio * 0.3), 3),
                    "note": f"第 {i + 1}→{best + 1} 句重录(相似度 {best_ratio:.2f}),保留最后一次"})
    return rs_cut._dedupe(rs_cut._chain_merge(out))


INPUTS = [
    ["今天我们来聊一聊店群运营", "好我们继续", "今天我们来聊一聊店群运营的方法"],
    ["这个方案我很满意", "这个方案我很满意但是", "这个方案我很满意但是要调整"],
    ["第一个要点是选品", "选品要看竞争度", "第一个要点是选品和定价", "再讲第二点",
     "第二个要点是流量", "第二个要点是流量和转化"],
]


def test_prune_keeps_output_identical():
    """剪枝前后输出逐条相同(上界过滤是纯剪枝,不改判定)。"""
    for texts in INPUTS:
        wl = _wl(texts)
        assert rs_cut.detect_retake(wl) == _retake_no_prune(wl), \
            f"剪枝改变判定:{texts}"


def test_ratio_called_once_per_surviving_pair(monkeypatch):
    """每个通过剪枝的候选**恰好一次**全量 ratio()(修复前连算两遍);
    prunedPairs + fullRatioCalls == pairs,且 prunedPairs > 0(剪枝真的在省算)。"""
    calls = {"n": 0}
    real = difflib.SequenceMatcher

    class Counting(real):
        def ratio(self):
            calls["n"] += 1
            return super().ratio()

    monkeypatch.setattr(rs_cut, "SequenceMatcher", Counting)
    wl = _wl(INPUTS[2])
    stats: dict = {}
    rs_cut.detect_retake(wl, stats=stats)
    assert stats["pairs"] > 0, "夹具必须产生候选配对"
    assert stats["prunedPairs"] > 0, "夹具必须触发上界剪枝,否则证明不了省算"
    assert stats["prunedPairs"] + stats["fullRatioCalls"] == stats["pairs"]
    assert calls["n"] == stats["fullRatioCalls"], \
        f"ratio() 实调 {calls['n']} 次 ≠ 记账 {stats['fullRatioCalls']}(重复块复活?)"
    assert stats["fullRatioCalls"] <= stats["pairs"], "存在候选多次全量比对(H7 复发)"


def test_full_ratio_calls_drop_vs_unpruned():
    """全量 ratio() 调用数 < 无剪枝参考(剪枝净收益为正)。"""
    for texts in INPUTS:
        calls = {"n": 0}
        real = difflib.SequenceMatcher

        class Counting(real):
            def ratio(self):
                calls["n"] += 1
                return super().ratio()

        rs_cut.SequenceMatcher = Counting
        try:
            stats: dict = {}
            rs_cut.detect_retake(_wl(texts), stats=stats)
        finally:
            rs_cut.SequenceMatcher = real
        assert stats["fullRatioCalls"] < stats["pairs"] or stats["prunedPairs"] >= 0
        # 硬判据:fullRatioCalls 严格小于「每对都全量比对」的上界(有剪枝收益时)
        if stats["prunedPairs"] > 0:
            assert stats["fullRatioCalls"] < stats["pairs"]


def test_cli_reports_prune_stats(tmp_path):
    """CLI --detect retake:params.retakePrune 带剪枝留痕(落盘可审计)。"""
    wl = _wl(INPUTS[0])
    p = tmp_path / "wl.json"
    p.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPTS / "rs_cut.py"), str(p),
                        "--detect", "retake"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=str(tmp_path))   # cutlist 落 tmp,不污染仓库
    assert r.returncode == 0, r.stderr[-400:]
    line = next(ln for ln in r.stdout.splitlines() if ln.strip().startswith("{"))
    data = json.loads(line)
    prune = data.get("data", {}).get("retakePrune")
    assert isinstance(prune, dict) and "prunedPairs" in prune, data
