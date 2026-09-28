# -*- coding: utf-8 -*-
"""C 组(T2.16b)回归:S9 终点偏移闸与卡片可读性下限(MIN_DUR_S)契约对齐。

缺陷(assetB 78s 实测):3 字短卡「到这里/下期见」语音 ≈0.6s,断句器按
MIN_DUR_S=0.83s 契约延长卡片(+160/+220ms),rs_sync 终点期望只看
「末字+LEAD」→ 契约内的正常延长被误计为「滞留」,终点 p95 160ms 撞穿
120ms 闸;END_* 闸注释「允许可读性延长,故放宽」与实现不符。

修复:终点偏移双轨——
· endOffsetMs 对「末字+LEAD」(语音覆盖域),继续把守「切掉语音」(early_end);
· releaseOffsetMs 对「合法终点窗」= max(末字+LEAD, 起点期望+MIN_DUR_S),再被
  下一卡起点封顶(v17 验收口径:延长被下一卡锚点顶住时不算欠延长);
  终点统计与「滞留」(over_end,END_MAX_RELEASE_MS=350ms 硬闸)看本轨。
**不放松**:语音覆盖域早退判据一字未动。

运行:pytest tests/test_rs_sync_min_dur_pin.py -q(纯逻辑,离线)
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_align  # noqa: E402
import rs_subtitle  # noqa: E402
import rs_sync  # noqa: E402

TEXT = "到这里"
SPEECH_START, SPEECH_END = 59.88, 60.48          # 0.6s 语音(短于 0.83s 下限)


def _wordline(next_sentence_start: float | None = None) -> dict:
    """经 build_wordline 构造真实形态 wordline(字级时间戳)。

    next_sentence_start:追加一句后续语音(模拟下一卡的锚点位置)。
    """
    per = int((SPEECH_END - SPEECH_START) * 1000 / len(TEXT))
    ts = [[int(SPEECH_START * 1000) + i * per,
           int(SPEECH_START * 1000) + (i + 1) * per - 20] for i in range(len(TEXT))]
    entries = [{"start": SPEECH_START, "end": SPEECH_END, "text": TEXT,
                "timestamp": ts, "conf": 0.95}]
    if next_sentence_start is not None:
        ts2 = [[int(next_sentence_start * 1000) + i * 200,
                int(next_sentence_start * 1000) + i * 200 + 180] for i in range(4)]
        entries.append({"start": next_sentence_start, "end": next_sentence_start + 0.8,
                        "text": "喜欢的话", "timestamp": ts2, "conf": 0.95})
    return rs_align.build_wordline(entries, "b.mp4")


def _event(end_s: float, start_s: float | None = None) -> dict:
    """一条 ASS 卡:起点缺省 = 首字-LEAD,终点参数化。"""
    return {"start": round(start_s if start_s is not None
                           else SPEECH_START - rs_sync.LEAD_MS / 1000.0, 3),
            "end": round(end_s, 3), "text": TEXT}


def test_min_dur_pin_with_rs_subtitle():
    """rs_sync 的可读性下限 = rs_subtitle.MIN_DUR_S(单一真相源,防漂移)。"""
    assert rs_subtitle.MIN_DUR_S == 0.83
    assert rs_sync._min_card_dur_ms() == int(rs_subtitle.MIN_DUR_S * 1000) == 830


def test_contract_extension_not_flagged_as_lingering():
    """契约内延长(卡长 0.85s,下一卡在 60.9s):release 偏移 ≈0,不进 over_end。"""
    wl = _wordline(next_sentence_start=60.9)
    ev = _event(SPEECH_START - 0.02 + 0.85)          # 卡长 0.85s ≥ 下限 0.83s
    row = rs_sync.check_offsets([ev], wl)[0]
    assert row["matched"], row
    assert row["endOffsetMs"] > 100, "语音覆盖域应如实记录延长(+160ms 量级)"
    assert abs(row["releaseOffsetMs"]) <= 50, row     # 合法终点窗内 ≈ 0
    res = rs_sync.summarize([row], [ev])
    assert res["overLongEnd"] == [] and res["pass"] is True, res


def test_blocked_extension_not_penalized():
    """延长被下一卡起点顶住(v17 验收口径):release 偏移不为负计,闸通过。"""
    wl = _wordline(next_sentence_start=SPEECH_START + 0.67)   # 下一卡在语音后、下限前
    ev = _event(SPEECH_START - 0.02 + 0.69)          # 卡被顶到下一卡起点(0.69s < 下限)
    nxt = {"start": SPEECH_START + 0.67, "end": SPEECH_START + 1.47, "text": "喜欢的话"}
    rows = rs_sync.check_offsets([ev, nxt], wl)
    row = rows[0]
    assert row["matched"] and abs(row["releaseOffsetMs"]) <= 30, row
    res = rs_sync.summarize(rows, [ev, nxt])
    assert res["overLongEnd"] == [] and res["earlyEnd"] == [] and res["pass"] is True, res


def test_true_lingering_still_fails():
    """真滞留(末卡拖到 1.3s,超出合法终点窗 +0.45s):仍被 350ms 硬闸拦下。"""
    wl = _wordline()
    ev = _event(SPEECH_START - 0.02 + 1.30)
    rows = rs_sync.check_offsets([ev], wl)
    assert rows[0]["releaseOffsetMs"] > rs_sync.END_MAX_RELEASE_MS, rows[0]
    res = rs_sync.summarize(rows, [ev])
    assert res["overLongEnd"] and res["pass"] is False, res


def test_speech_early_end_still_fails():
    """语音覆盖域不放松:卡在语音结束前 100ms 早退,仍硬失败。"""
    wl = _wordline(next_sentence_start=61.3)
    ev = _event(SPEECH_END - 0.10)                   # 比末字早 100ms 退场
    rows = rs_sync.check_offsets([ev], wl)
    assert rows[0]["endOffsetMs"] < -rs_sync.END_TOL_MS, rows[0]
    res = rs_sync.summarize(rows, [ev])
    assert res["earlyEnd"] and res["pass"] is False, res
