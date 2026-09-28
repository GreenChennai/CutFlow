# -*- coding: utf-8 -*-
"""C 组(T2.16a)回归:IR 段长帧网格量化 —— 拼接漂移归零。

缺陷(真实素材 assetA 364s/41 段实测):keep 段长非帧整数倍时,拼接端逐段把
视频 round 到整帧,误差随段数单调累积(41 段实测 +68.7ms;音频按名义毫秒裁剪、
视频按帧走,二者渐行渐远)——S7 的帧网格补偿(align_events_to_frame_grid)把
ASS 对到被拉伸的画面上,S9 却拿名义域 wordline 对账 → 终点字幕-语音偏移 90ms+,
撞穿对齐闸(中位 ≤40ms)。冒烟级 8s 单段素材 delta≈0,故此前全绿不被发现。

修复:rs_ir.build_from_cutlist / build_from_cards 把段长量化到帧网格
(frame_quantize_ms:起点不动,时长取整帧),delta≡0 恒等路径成立,渲染不再拉伸;
cards 路径末卡吸收残差,总时长不变。残差 ≤0.34ms/段(int ms 表示 1/30s 帧周期)。

运行:pytest tests/test_frame_exact_segments.py -q(纯逻辑,离线)
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_ir  # noqa: E402
import rs_subtitle  # noqa: E402

FPS = 30


# ---------------------------------------------------------------- 量化函数本体

def test_frame_quantize_ms_values():
    """整帧取整(半升):起点不动、亚帧下限 1 帧、30fps 帧周期 33.33ms。"""
    assert rs_ir.frame_quantize_ms(4320) == 4333      # 129.6 帧 → 130 帧
    assert rs_ir.frame_quantize_ms(830) == 833        # 24.9 帧 → 25 帧
    assert rs_ir.frame_quantize_ms(10) == 33          # 亚帧 → 至少 1 帧
    assert rs_ir.frame_quantize_ms(1000) == 1000      # 恰 30 帧 → 原值
    assert rs_ir.frame_quantize_ms(1350) == 1367      # 40.5 帧 → 半升 41 帧(勿银行家舍入)
    # 往返一致:量化值 round 回同一帧数
    for ms in (1, 33, 500, 1237, 989, 1503, 60000):
        q = rs_ir.frame_quantize_ms(ms)
        assert int(q * FPS / 1000 + 0.5) == max(1, int(ms * FPS / 1000 + 0.5)), ms


# ---------------------------------------------------------------- cutlist 路径

def _random_keeps(n: int, seed: int = 20260927) -> list[list[int]]:
    rng = random.Random(seed)
    keep, cursor = [], 0
    for _ in range(n):
        dur = rng.randint(500, 12000)
        keep.append([cursor, cursor + dur])
        cursor += dur + rng.randint(300, 2000)        # 挖刀(粗剪移除区间)
    return keep


def test_build_from_cutlist_no_cumulative_drift():
    """41 段随机 keep(assetA 同规模):拼接帧网格累计漂移远低于 40ms 闸。

    修复前同分布 41 段累计 +68.7ms;修复后残差只剩 int ms 表示帧周期的
    截断,上界 = 段数 × 0.34ms。
    """
    keep = _random_keeps(41)
    doc = rs_ir.build_from_cutlist({"version": 1, "source": "a.mp4",
                                    "keep": keep}, slug="t", ratio="9x16")
    clips = doc["tracks"][0]["clips"]
    assert len(clips) == 41
    segs = rs_subtitle.frame_grid_deltas(clips, FPS)
    assert segs and segs[0][0] == 0
    drift = abs(segs[-1][2])
    assert drift <= 41 * 34 // 100 + 1, f"累计漂移 {drift}ms 超预期(应 ≤15ms)"


def test_align_events_near_identity_on_quantized_ir():
    """量化后的 IR:帧网格补偿近恒等(每个累计 delta ≤1ms/段 截断量级)。"""
    keep = _random_keeps(41)
    doc = rs_ir.build_from_cutlist({"version": 1, "source": "a.mp4",
                                    "keep": keep}, slug="t", ratio="9x16")
    clips = doc["tracks"][0]["clips"]
    segs = rs_subtitle.frame_grid_deltas(clips, FPS)
    for _a, _b, d in segs:
        assert abs(d) <= 41 * 34 // 100 + 1, f"段间 delta {d}ms 异常"


# ---------------------------------------------------------------- cards 路径

def test_build_from_cards_total_preserved():
    """cards 路径:量化不改变总时长(末卡吸收残差),起点 = 量化后累计。"""
    text = "大家好今天讲桌面运维很实用"
    wl = {"chars": [{"ch": c, "i": i, "startMs": i * 200, "endMs": i * 200 + 180}
                    for i, c in enumerate(text)],
          "srcDurationMs": 3000, "finalDurationMs": 3000}
    manifest = {"items": [{"id": f"c{i}", "output": f"cards/c{i}/index.html"}
                          for i in (1, 2, 3)]}
    anchors = [{"card": "c1", "match": "大家好"},
               {"card": "c2", "match": "今天"},
               {"card": "c3", "match": "桌面"}]
    doc = rs_ir.build_from_cards(manifest, wl, anchors, slug="t", ratio="9x16",
                                 voice_ms=3000)
    clips = doc["tracks"][0]["clips"]
    assert len(clips) == 3
    assert sum(c["durationMs"] for c in clips) == 3000, "末卡吸收残差后总长须不变"
    acc = 0
    for c in clips:
        assert c["startMs"] == acc, "卡起点须等于量化后累计(不得用原始 bounds)"
        acc += c["durationMs"]
    # 前两张卡应为帧网格量化值(整帧);末卡是吸收残差的收尾值
    for c in clips[:-1]:
        frames = round(c["durationMs"] * FPS / 1000)
        assert abs(c["durationMs"] - frames * 1000 / FPS) < 1, c
