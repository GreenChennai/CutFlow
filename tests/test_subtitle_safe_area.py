# -*- coding: utf-8 -*-
"""C 组(T2.16c)回归:字幕带不得进平台底部安全区(16x9/B站实测暴露)。

缺陷(assetA/assetC 16x9 bilibili 实测):STYLES 的 16x9 margin_v(90/110/120)
全部低于 B站 safeArea.bottom=0.16 × 1080 = 173px,L0 安全区硬校验
「字幕底边进入底部禁区」直接失败——9x16/3x4 档一直有平台对拍,16x9 档
从没被真素材走到过。

修复:三个样式的 16x9 margin_v → 180(≥173 + 余量);并加不变量门禁——
对 platforms.json 每个平台预设,其 (style, ratio) 的 margin_v 必须 ≥
safeArea.bottom × canvas 高(几何对拍,平台表是单一事实源)。

运行:pytest tests/test_subtitle_safe_area.py -q(纯逻辑,离线)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_subtitle  # noqa: E402

PLATFORMS = REPO / "skills" / "cutflow" / "templates" / "platforms.json"


def test_margin_v_clears_platform_safe_area():
    """不变量:每个平台预设的 (style,ratio) margin_v ≥ safeArea.bottom × 画高。"""
    doc = json.loads(PLATFORMS.read_text(encoding="utf-8"))
    violations = []
    for pid, p in doc["platforms"].items():
        ratio, canvas = p["ratio"], p["canvas"]
        need = p["safeArea"]["bottom"] * canvas[1]
        st = rs_subtitle.STYLES[p["style"]]
        have = st["margin_v"][ratio]
        if have < need:
            violations.append(f"{pid}/{p['style']}/{ratio}: margin_v={have} < 底部禁区 {need:.0f}px")
    assert not violations, violations


def test_bilibili_16x9_concrete():
    """实测案例固化:B站 16x9 底部禁区 173px,三样式 margin_v 全部 ≥180。"""
    doc = json.loads(PLATFORMS.read_text(encoding="utf-8"))
    p = doc["platforms"]["bilibili"]
    need = p["safeArea"]["bottom"] * p["canvas"][1]
    assert round(need) == 173
    for name, st in rs_subtitle.STYLES.items():
        assert st["margin_v"]["16x9"] >= need, (name, st["margin_v"]["16x9"])
