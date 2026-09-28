# -*- coding: utf-8 -*-
"""T2.5 / H5 回归:pixabay 音乐/音效通道的 slug 选择器必须按 kind 选正则。

缺陷:music_search 对 `--kind sound_effect` 打开 `/sound-effects/search` 页,
却仍用 `href="/music/(...)"/` 抓 slug → 恒不命中,静默返回空列表;且"0 结果"
与"选择器失配/反爬"混为一谈。
修复:slug 正则与详情页 URL 一律按 kind 选表;0 结果区分"真无结果"(空列表)
与"选择器失配"(PIXABAY_SELECTOR_MISMATCH 结构化错误)。顺手 M4:删除
`_music_search_urls` / `_music_meta` 死函数(与 music_search 逐行重复)。

全部用例离线(内联 HTML 样本),不联网、不起浏览器。

运行:pytest tests/test_pixabay_selector.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_pixabay  # noqa: E402

MUSIC_HTML = """
<html><body>
 <a href="/music/epic-cinematic-trailer-12345/">Epic Trailer</a>
 <a href="/music/soft-piano-loop-9876543210/">Soft Piano</a>
 <img src="/static/x.png"><a href="/photos/mooncake-7349262/">photo</a>
</body></html>
"""

SFX_HTML = """
<html><body>
 <a href="/sound-effects/click-sound-effect-112233/">Click</a>
 <a href="/sound-effects/whoosh-transition-445566778899/">Whoosh</a>
</body></html>
"""

# 选择器失配样本:音效搜索页被改版后只挂 /music/ 链接(旧实现在此静默返回空)
SFX_PAGE_WITH_MUSIC_LINKS = """
<html><body>
 <a href="/music/epic-cinematic-trailer-12345/">Epic Trailer</a>
</body></html>
"""

NO_RESULT_HTML = """
<html><body><main><div class="container">
 <p>No results found for “qqqqzzzz”</p>
</div></main></body></html>
"""

JSONLD_HTML = """
<html><head>
<script type="application/ld+json">{"@type":"AudioObject","name":"Click Effect",
 "contentUrl":"https://cdn.pixabay.com/audio/click.mp3","duration":"PT4S",
 "byArtist":{"@type":"Person","name":"PixabayAuthor"}}</script>
</head><body></body></html>
"""


# ---------------------------------------------------------------- 选择器(离线)

def test_music_kind_hits_music_slugs():
    """music:本地样本命中 ≥1,且 slug 形态正确。"""
    slugs, status = rs_pixabay.extract_slugs("music", MUSIC_HTML)
    assert status == "ok"
    assert len(slugs) >= 1
    assert "epic-cinematic-trailer-12345" in slugs


def test_sound_effect_kind_hits_sfx_slugs():
    """sound_effect:音效页样本命中 ≥1(旧实现在此恒空 —— H5 核心复现场景)。"""
    slugs, status = rs_pixabay.extract_slugs("sound_effect", SFX_HTML)
    assert status == "ok"
    assert len(slugs) >= 1
    assert "click-sound-effect-112233" in slugs


def test_selector_mismatch_is_structured_not_empty():
    """页面有结果条目但选择器失配 → selector_mismatch(不并入真无结果)。"""
    slugs, status = rs_pixabay.extract_slugs("sound_effect", SFX_PAGE_WITH_MUSIC_LINKS)
    assert slugs == [] and status == "selector_mismatch"


def test_true_no_results_distinguished():
    """带"无结果"标记的页面 → no_results(真无结果)。"""
    slugs, status = rs_pixabay.extract_slugs("music", NO_RESULT_HTML)
    assert slugs == [] and status == "no_results"


# ---------------------------------------------------------------- 详情页元数据(离线)

def test_meta_from_html_parses_jsonld():
    item = rs_pixabay._meta_from_html(JSONLD_HTML, "https://pixabay.com/sound-effects/x/",
                                      "click-sound-effect-112233")
    assert item is not None
    assert item["url"] == "https://cdn.pixabay.com/audio/click.mp3"
    assert item["durationMs"] == 4000
    assert item["author"] == "PixabayAuthor"
    assert item["license"] == "Pixabay Content License" and item["commercial"] is True


# ---------------------------------------------------------------- 路由表契约

def test_kind_paths_and_detail_base():
    """搜索/详情 URL 按 kind 分流:sound_effect 走 /sound-effects/(H5 修复锚点)。"""
    assert rs_pixabay._SEARCH_PATH["sound_effect"] == "sound-effects/search"
    assert rs_pixabay._SEARCH_PATH["music"] == "music/search"
    assert rs_pixabay._DETAIL_BASE["sound_effect"] == "https://pixabay.com/sound-effects/"
    assert rs_pixabay._DETAIL_BASE["music"] == "https://pixabay.com/music/"


def test_dead_functions_removed_m4():
    """M4:`_music_search_urls` / `_music_meta` 死函数已删除,不再两套实现。"""
    assert not hasattr(rs_pixabay, "_music_search_urls")
    assert not hasattr(rs_pixabay, "_music_meta")


# ---------------------------------------------------------------- 通道异常结构化

def test_music_channel_missing_still_structured(monkeypatch):
    """playwright 缺失 → 结构化 MUSIC_CHANNEL_MISSING(既有契约不回退)。"""
    monkeypatch.setattr(rs_pixabay, "_playwright_ready", lambda: False)
    items = rs_pixabay.music_search("music", "click", 3)
    assert items and items[0].get("error") == "MUSIC_CHANNEL_MISSING"


def test_search_dispatcher_routes_music_kinds(monkeypatch):
    """search() 对 music/sound_effect 走 music_search,其余走官方 API(分流不变)。"""
    seen = []
    monkeypatch.setattr(rs_pixabay, "music_search",
                        lambda kind, q, limit: seen.append(kind) or [])
    monkeypatch.setattr(rs_pixabay, "_api_search", lambda kind, q, limit: [])
    rs_pixabay.search("sound_effect", "click", 2)
    rs_pixabay.search("photo", "moon", 2)
    assert seen == ["sound_effect"]
