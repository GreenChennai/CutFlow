#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rs_pixabay.py —— Pixabay 在线素材源(图片/视频/音乐),剪辑素材的在线获取通道。

授权与 key(ADR-0053 许可四字段纪律):
  · key 读取顺序:环境变量 ARTBOARD_PIXABAY_KEY → CutFlow config.json `pixabay_key`
    → artboard 技能 config.json `pixabay_key`(只读借用;用户 2026-09-26 指定 artboard 为 key 来源)。
  · Pixabay Content License:免费商用、免署名。四字段:
    source=Pixabay / license=Pixabay Content License / commercial=true /
    attribution="Pixabay (pixabay.com) · 作者 <user>"。

通道:
  · photo / video:官方 API(https://pixabay.com/api[/videos])——纯 HTTP;
  · music / sound_effect:官方 API 不含音频,走 playwright 真浏览器
    (搜索页 → 歌曲 slug → 详情页 JSON-LD contentUrl 直链;403 反爬必须真浏览器)。
    playwright 为可选件(ADR-0049 懒加载;缺失时 music 通道报 MUSIC_CHANNEL_MISSING,
    photo/video 不受影响)。

子命令:
  search --kind music|sound_effect|photo|video --query "..." [--limit 6] [--json]
  fetch  --kind ... --query "..." [--pick 1] [--out 目录] [--json]
         下载第 pick 条到 --out 目录,输出含许可四字段的登记块(可接 rs_asset add)。

许可纪律:Pixabay Content License 属 ADR-0053 白名单(明确可商用授权);
交付归因由 rs_ingest deliverables --publish 的素材归因清单自动携带。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rs_common  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
LICENSE = "Pixabay Content License"


def pixabay_key() -> str:
    """key 三级读取(env → 本仓 config → artboard config 只读借用)。"""
    k = os.environ.get("ARTBOARD_PIXABAY_KEY", "").strip()
    if k:
        return k
    for cfg in (Path(__file__).resolve().parents[3] / "config.json",
                Path(__file__).resolve().parents[2] / "config.json",
                Path(r"E:\平日资料\GitHub\.agents\skills\artboard\config.json")):
        try:
            v = str(json.loads(cfg.read_text(encoding="utf-8")).get("pixabay_key", "") or "")
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if v:
            return v
    return ""


def _http_json(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _pt_duration_to_ms(iso: str) -> int:
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$", iso or "")
    if not m:
        return 0
    h, mi, s = int(m.group(1) or 0), int(m.group(2) or 0), float(m.group(3) or 0)
    return int((h * 3600 + mi * 60 + s) * 1000)


def _license_block(author: str, page_url: str) -> dict:
    return {"source": "Pixabay", "license": LICENSE, "commercial": True,
            "attribution": f"Pixabay (pixabay.com) · 作者 {author}"
                           + (f" · 来源页 {page_url}" if page_url else "")}


# ---------------- 图片 / 视频(官方 API) ----------------

def _api_search(kind: str, query: str, limit: int) -> list[dict]:
    key = pixabay_key()
    if not key:
        return [{"error": "NO_KEY", "message": "pixabay key 未配置(ARTBOARD_PIXABAY_KEY 或 config.json)"}]
    base = "https://pixabay.com/api/" if kind == "photo" else "https://pixabay.com/api/videos/"
    url = (base + "?key=" + urllib.parse.quote(key) + "&q=" + urllib.parse.quote(query)
           + f"&per_page={min(limit, 50)}&safesearch=true")
    try:
        d = _http_json(url)
    except Exception as exc:  # noqa: BLE001 — 网络/限流/权限结构化上报(视频 API 对部分 key 未开通)
        return [{"error": "PIXABAY_API_FAIL",
                 "message": f"{type(exc).__name__}: {str(exc)[:120]}(该 kind 的 API 可能未对此 key 开通;"
                            "music 通道走 playwright 不受影响)"}]
    out = []
    for h in d.get("hits", [])[:limit]:
        if kind == "photo":
            out.append({"id": f"px{h.get('id')}", "name": (h.get("tags") or "").split(",")[0],
                        "url": h.get("largeImageURL", ""), "width": h.get("imageWidth"),
                        "height": h.get("imageHeight"), "author": h.get("user", "unknown"),
                        "pageUrl": h.get("pageURL", ""), **_license_block(h.get("user", "unknown"), h.get("pageURL", ""))})
        else:
            vids = h.get("videos") or {}
            best = vids.get("large") or vids.get("medium") or vids.get("small") or (
                next(iter(vids.values())) if vids else {})
            out.append({"id": f"pxv{h.get('id')}", "name": (h.get("tags") or "").split(",")[0],
                        "url": (best or {}).get("url", ""), "width": (best or {}).get("width"),
                        "height": (best or {}).get("height"),
                        "durationMs": int(float(h.get("duration", 0)) * 1000),
                        "author": h.get("user", "unknown"), "pageUrl": h.get("pageURL", ""),
                        **_license_block(h.get("user", "unknown"), h.get("pageURL", ""))})
    return out


# ---------------- 音乐 / 音效(playwright 真浏览器通道) ----------------

def _playwright_ready() -> bool:
    try:
        import playwright  # noqa: F401,PLC0415
        return True
    except ImportError:
        return False


def _music_search_urls(kind: str, query: str) -> list[str]:
    """搜索页 → 歌曲 slug → 详情页 URL 列表(sound_effect 走 sound-effects 搜索)。"""
    if not _playwright_ready():
        return [{"error": "MUSIC_CHANNEL_MISSING",
                 "message": "playwright 未安装(music 通道需真浏览器绕 403 反爬;"
                            "pip install playwright && python -m playwright install chromium)。"
                            "photo/video 通道不受影响"}]
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    path = "sound-effects/search" if kind == "sound_effect" else "music/search"
    url = f"https://pixabay.com/{path}/{urllib.parse.quote(query)}/"
    out = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        pg = b.new_page(user_agent=UA)
        pg.goto(url, timeout=45000, wait_until="domcontentloaded")
        pg.wait_for_timeout(6000)
        html = pg.content()
        b.close()
    for slug in dict.fromkeys(re.findall(r'href="/music/([a-z0-9-]{10,80})/"', html)):
        out.append(f"https://pixabay.com/music/{slug}/")
        if len(out) >= 24:
            break
    return out


def _music_meta(song_url: str) -> dict | None:
    """歌曲详情页 JSON-LD → name/contentUrl/duration/author。"""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        pg = b.new_page(user_agent=UA)
        pg.goto(song_url, timeout=45000, wait_until="domcontentloaded")
        pg.wait_for_timeout(3500)
        html = pg.content()
        b.close()
    for j in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            d = json.loads(j)
        except json.JSONDecodeError:
            continue
        cu = d.get("contentUrl") or ""
        if cu:
            author = d.get("byArtist", {}).get("name", "unknown") if isinstance(
                d.get("byArtist"), dict) else "unknown"
            return {"name": d.get("name") or song_url.rstrip("/").rsplit("/", 1)[-1],
                    "url": cu, "durationMs": _pt_duration_to_ms(d.get("duration", "")),
                    "author": author or "unknown", "pageUrl": song_url,
                    **_license_block(author or "unknown", song_url)}
    return None


def music_search(kind: str, query: str, limit: int) -> list[dict]:
    """音乐/音效搜索:搜索页 slug → 逐详情页取 JSON-LD(限 limit 条,浏览器复用)。"""
    if not _playwright_ready():
        return [{"error": "MUSIC_CHANNEL_MISSING",
                 "message": "playwright 未安装(music 通道需真浏览器)"}]
    from playwright.sync_api import sync_playwright  # noqa: PLC0415
    path = "sound-effects/search" if kind == "sound_effect" else "music/search"
    base = f"https://pixabay.com/{path}/{urllib.parse.quote(query)}/"
    out = []
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        pg = b.new_page(user_agent=UA)
        pg.goto(base, timeout=45000, wait_until="domcontentloaded")
        pg.wait_for_timeout(6000)
        html = pg.content()
        slugs = list(dict.fromkeys(re.findall(r'href="/music/([a-z0-9-]{10,80})/"', html)))
        for slug in slugs:
            if len(out) >= limit:
                break
            song_url = f"https://pixabay.com/music/{slug}/"
            pg.goto(song_url, timeout=45000, wait_until="domcontentloaded")
            pg.wait_for_timeout(2500)
            page_html = pg.content()
            for j in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page_html, re.S):
                try:
                    d = json.loads(j)
                except json.JSONDecodeError:
                    continue
                cu = d.get("contentUrl") or ""
                if not cu:
                    continue
                author = (d.get("byArtist") or {}).get("name", "unknown") \
                    if isinstance(d.get("byArtist"), dict) else "unknown"
                out.append({"id": slug, "name": d.get("name") or slug,
                            "url": cu,
                            "durationMs": _pt_duration_to_ms(d.get("duration", "")),
                            "author": author or "unknown", "pageUrl": song_url,
                            **_license_block(author or "unknown", song_url)})
                break
        b.close()
    return out


def search(kind: str, query: str, limit: int) -> list[dict]:
    if kind in ("music", "sound_effect"):
        return music_search(kind, query, limit)
    return _api_search(kind, query, limit)


def fetch(kind: str, query: str, out_dir: Path, pick: int = 1,
          limit: int = 8) -> tuple[int, dict]:
    """search → 下载第 pick 条到 out_dir,返回 (exit_code, 结果块, 登记建议)。"""
    items = search(kind, query, max(limit, pick))
    items = [i for i in items if i.get("url")]
    if not items:
        return 4, {"code": "PIXABAY_NO_RESULTS",
                   "message": f"无可用结果({kind}: {query});网络/反爬/key 均可能,见 search 输出"}
    if pick > len(items):
        pick = 1
    it = items[pick - 1]
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = ".mp3" if kind in ("music", "sound_effect") else (
        ".mp4" if kind == "video" else ".jpg")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", it["id"] if str(it["id"]).startswith("px") else str(it["id"]))[:60]
    dst = out_dir / f"pixabay_{kind}_{safe}{ext}"
    req = urllib.request.Request(it["url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r, open(dst, "wb") as f:
        f.write(r.read())
    size = dst.stat().st_size
    if size < 5000:
        dst.unlink(missing_ok=True)
        return 4, {"code": "PIXABAY_DOWNLOAD_TOO_SMALL",
                   "message": f"下载内容过小({size}B),疑似反爬拦截页;稍后重试或人工下载"}
    reg = {"kind": {"music": "bgm", "sound_effect": "sfx", "video": "element",
                    "photo": "element"}.get(kind, kind),
           "file": str(dst), "label": it.get("name", "")[:40],
           "source": it.get("source"), "license": it.get("license"),
           "commercial": it.get("commercial"), "attribution": it.get("attribution"),
           "durationMs": it.get("durationMs")}
    return 0, {"file": str(dst), "bytes": size, "register": reg,
               "hint": "入库素材库:rs_asset.py add --file <本文件> --kind <kind> ... "
                       "(四许可字段已备好,见 register)"}


# ---------------- CLI ----------------

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="rs_pixabay.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["search", "fetch"])
    ap.add_argument("--kind", default="photo",
                    choices=["music", "sound_effect", "photo", "video"])
    ap.add_argument("--query", required=True)
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--pick", type=int, default=1)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    if a.cmd == "search":
        items = search(a.kind, a.query, a.limit)
        errs = [i for i in items if i.get("error")]
        ok = bool(items) and not errs
        return rs_common.emit(ok, "PIXABAY_SEARCH" if ok else "PIXABAY_SEARCH_FAIL",
                              f"{a.kind} 搜索:{len(items) - len(errs)} 条"
                              + (f";异常 {errs}" if errs else ""),
                              {"items": items, "errors": errs},
                              exit_code=0 if ok else 2)

    out_dir = Path(a.out) if a.out else Path.cwd() / "03_创作素材" / "pixabay"
    code, doc = fetch(a.kind, a.query, out_dir, pick=a.pick, limit=a.limit)
    ok = code == 0
    return rs_common.emit(ok, "PIXABAY_FETCH_OK" if ok else "PIXABAY_FETCH_FAIL",
                          doc.get("message") or json.dumps(doc.get("file", ""), ensure_ascii=False),
                          doc, exit_code=code)


if __name__ == "__main__":
    rs_common.ensure_utf8()
    sys.exit(main())
