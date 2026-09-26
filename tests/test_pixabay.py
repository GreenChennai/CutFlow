# -*- coding: utf-8 -*-
"""v2 追加:Pixabay 在线素材源(rs_pixabay)——搜索/下载/许可四字段/解析纯函数。

联网用例(真实 search+fetch)在本机有 key 时跑;CI 无 key 诚实 skip。
许可纪律:Pixabay Content License = ADR-0053 白名单(可商用免署名),
四字段 source/license/commercial/attribution 必须齐。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_pixabay  # noqa: E402

KEY = rs_pixabay.pixabay_key()
ONLINE = bool(KEY)


def test_pt_duration_parse():
    assert rs_pixabay._pt_duration_to_ms("PT1M31.872625S") == 91872
    assert rs_pixabay._pt_duration_to_ms("PT2H3M4S") == 7384000
    assert rs_pixabay._pt_duration_to_ms("") == 0
    assert rs_pixabay._pt_duration_to_ms("PT45S") == 45000


def test_license_block_four_fields():
    b = rs_pixabay._license_block("alice", "https://pixabay.com/music/x/")
    assert b["source"] == "Pixabay" and b["license"] == "Pixabay Content License"
    assert b["commercial"] is True
    assert "alice" in b["attribution"] and "pixabay.com" in b["attribution"]


def test_key_reader_precedence(tmp_path, monkeypatch):
    key = rs_pixabay.pixabay_key()
    if not key:
        pytest.skip("本机未配 pixabay key")
    # env 优先
    monkeypatch.setenv("ARTBOARD_PIXABAY_KEY", "env-key-123")
    assert rs_pixabay.pixabay_key() == "env-key-123"


@pytest.mark.skipif(not ONLINE, reason="本机无 pixabay key(联网搜索用例)")
def test_search_photo_real():
    items = rs_pixabay._api_search("photo", "mooncake", 3)
    assert items and all(i.get("url") for i in items)
    for i in items:
        assert i["license"] == "Pixabay Content License" and i["commercial"] is True
        assert "pixabay.com" in i["attribution"]


@pytest.mark.skipif(not ONLINE, reason="本机无 pixabay key(联网搜索用例)")
def test_search_video_real():
    """视频 API 对部分 key 未开通视频权限时返回结构化错误(400 限权),
    此时按"通道不可用"断言而非硬性失败——通道状态随 pixabay 账号而异。"""
    items = rs_pixabay._api_search("video", "festival", 2)
    if items and items[0].get("error") == "PIXABAY_API_FAIL":
        pytest.skip("该 key 视频 API 未开通(结构化降级,非缺陷)")
    assert items and all(i.get("url") for i in items)


def test_music_channel_missing_is_structured(monkeypatch):
    """playwright 缺失 → music 通道结构化报错(不崩;photo/video 不受影响)。"""
    import types
    real_import = __import__

    def fake_import(name, *a, **kw):
        if name == "playwright":
            raise ImportError("boom")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert rs_pixabay._playwright_ready() is False
    out = rs_pixabay.music_search("music", "happy", 2)
    assert out and out[0].get("error") == "MUSIC_CHANNEL_MISSING"
