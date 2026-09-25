"""M7 · artboard 片头尾/标题卡产品化(rs_artboard gen-frames)回归。

覆盖面(方案 §5.10 / M7 验收线):
· 五段式时间轴:各类 kind 的缺省持住/总长/--max-wait;durationMs 反推与夹限告警
· 场景卡 HTML 硬约束:五段变量、全 finite(无 infinite 字样)、入场/出场两层嵌套、
  各画幅安全区数值、逐类结构(opener/outro/title/section/stat/compare)、确定性
· 计划解析:kind 白名单与 endcard 别名、durationMs 类型、白名单外字段仍硬报错
· gen-frames 子命令(子进程 + 假 artboard 技能):FRAMES_OK 全链、manifest 登记、
  幂等重跑、--kind 筛选、pack 缺失 WARN stylePackMissing、安全区机检门禁
  (ok:false 即整条失败)、导出失败的诚实降级、IR 挂轨按探测时长传播
· hash_source 排除 scaffold 联接目录(fonts/vendor)

运行:pytest tests/test_artboard_frames.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_artboard as ab  # noqa: E402


# ================================================================ 五段式时间轴

def test_five_segment_defaults_per_kind():
    """六类缺省持住 → 导出片长(≈持住+1.6s)落在方案 §5.10 各类规格窗内。"""
    expect_total = {"opener": 4.2, "outro": 4.8, "title": 6.0,
                    "section": 4.0, "stat": 6.8, "compare": 7.8}
    for kind in ab.FRAME_KINDS:
        seg, warns = ab.five_segment(kind)
        assert warns == [], (kind, warns)
        # 五段固定段:前置静置 ≥2.0 / 入场 0.5–0.8 / 出场 0.4–0.6 / 收尾 ≥0.3
        assert seg["t0"] >= 2.0 and seg["p1"] >= 0.3, seg
        assert 0.5 <= seg["in"] <= 0.8 and 0.4 <= seg["out"] <= 0.6, seg
        d_hold, h_min, h_max = ab.FRAME_SPECS[kind]
        assert h_min <= seg["hold"] <= h_max, (kind, seg)
        assert seg["total"] == pytest.approx(expect_total[kind]), (kind, seg)
        # --max-wait = 五段总和 + 1.5s;单卡 ≤15s(artboard 录制上限)
        assert seg["maxWait"] == pytest.approx(seg["total"] + 1.5), (kind, seg)
        assert seg["maxWait"] <= 15.0, (kind, seg)


def test_five_segment_duration_ms_reverse_and_clamp():
    """durationMs(挂轨时间窗)按「导出片长 ≈ 持住 + 1.6s」反推;越档夹限必告警。"""
    seg, warns = ab.five_segment("title", 8000)
    assert seg["hold"] == pytest.approx(6.4) and seg["total"] == pytest.approx(9.8)
    assert warns == []
    # 比下限还短 → 夹到下限 + WARN
    seg2, warns2 = ab.five_segment("compare", 1000)
    assert seg2["hold"] == pytest.approx(1.6) and len(warns2) == 1, warns2
    # 超上限 → 夹到上限 + WARN(stat/compare 导出 ≤8s ⇒ 持住 ≤6.4)
    seg3, warns3 = ab.five_segment("stat", 60_000)
    assert seg3["hold"] == pytest.approx(6.4) and len(warns3) == 1, warns3
    assert seg3["maxWait"] <= 15.0


# ================================================================ 场景卡 HTML

def _card(kind: str, **over) -> dict:
    base = {"id": f"k_{kind}", "kind": kind, "title": "测试标题",
            "accent": "#4F8CFF", "kicker": "章节 01",
            "lines": ["第一行要点", "第二行要点"], "durationMs": None}
    if kind == "compare":
        base["kicker"] = "错/对"
    base.update(over)
    return base


def _render(kind: str, ratio: str = "9x16", **over) -> str:
    seg, _ = ab.five_segment(kind)
    return ab.render_frame_html(_card(kind, **over), (1080, 1920), seg, ratio)


def test_frame_html_five_segment_values_and_finite():
    """每类产物:五段变量原样落 CSS;全文无 infinite(全 finite,禁无限循环)。"""
    hold_by_kind = {k: ab.FRAME_SPECS[k][0] for k in ab.FRAME_KINDS}
    for kind in ab.FRAME_KINDS:
        h = _render(kind)
        assert "infinite" not in h.lower(), f"{kind} 出现无限循环"
        assert "--t0: 2s;" in h and "--in: 0.6s;" in h, kind
        assert "--out: 0.5s;" in h and "--p1: 0.3s;" in h, kind
        assert f"--hold: {hold_by_kind[kind]}s;" in h, kind
        # 机检/测试用的数据锚:五段写入 data-five-seg
        seg, _ = ab.five_segment(kind)
        assert f'data-five-seg="{"/".join(str(seg[k]) for k in ("t0", "in", "hold", "out", "p1"))}"' in h


def test_frame_html_two_layer_nesting():
    """【硬】入场与出场分属两层嵌套:外层 .si 只挂入场、内层 .so 只挂出口(§十二)。"""
    h = _render("opener")
    assert 'class="si" style="--i:0"' in h
    # 每个 .si 的直接子层是 .so(嵌套,不是同元素双动画)
    assert 'style="--i:0"><div class="so" style="--j:' in h
    # 入场用 decelerate、出场用 accelerate(M3 缓动 token)
    assert "--ease-in:  cubic-bezier(0, 0, 0, 1);" in h
    assert "--ease-out: cubic-bezier(.3, 0, .8, .15);" in h


def test_frame_html_safe_area_per_ratio():
    """安全区标准档写入 .safe 层:9:16 左右 184/顶 230/底 576;16:9 与 3:4 各用各值。"""
    expect = {"9x16": ("184px", "230px", "576px"),
              "16x9": ("154px", "108px", "173px"),
              "3x4": ("76px", "144px", "259px")}
    for ratio, (side, top, bottom) in expect.items():
        h = _render("title", ratio=ratio)
        assert f"left:{side}; right:{side};" in h, ratio
        assert f"top:{top}; bottom:{bottom};" in h, ratio


def test_frame_html_kind_structures():
    """逐类结构锚:标题逐行/数据大字/对比两态/片头骨架先行/片尾 CTA/章节居中。"""
    t = _render("title")
    assert t.count('<span class="tl si"') == 2 and t.count('<span class="tl so"') == 2
    assert "var(--c-accent)" in t  # 断行=换色=stagger 三合一
    s = _render("stat")
    assert 'class="stat-num"' in s and "tabular-nums" in s
    c = _render("compare")
    assert 'class="cmp-cell so-a"' in c and 'class="cmp-cell si-b"' in c  # 态A退场/态B入场
    assert ">错</div>" in c and 'class="badge badge-b"' in c
    o = _render("opener")
    assert 'class="rule"' in o and 'class="kicker"' in o  # 骨架先行 + kicker
    d = _render("outro")
    assert 'class="ctas"' in d
    x = _render("section")
    assert 'class="section"' in x


def test_frame_html_deterministic_and_escapes():
    """同一份输入必得同一字节;文案 HTML 转义,注入不透。"""
    a, b = _render("opener"), _render("opener")
    assert a == b
    h = _render("opener", title="<script>x</script>")
    assert "<script>" not in h and "&lt;script&gt;" in h


# ================================================================ 计划解析

def test_parse_card_plan_kind_alias_and_validation():
    """endcard 别名归一 outro;非法 kind / 非整 durationMs / 白名单外字段硬报错。"""
    entries, _ = ab.parse_card_plan([{"id": "a", "title": "T", "kind": "endcard"}])
    assert entries[0]["kind"] == "outro"
    with pytest.raises(ValueError, match="kind 非法"):
        ab.parse_card_plan([{"id": "a", "title": "T", "kind": "ending"}])
    with pytest.raises(ValueError, match="durationMs"):
        ab.parse_card_plan([{"id": "a", "title": "T", "kind": "stat", "durationMs": "x"}])
    with pytest.raises(ValueError, match="白名单外字段"):
        ab.parse_card_plan([{"id": "a", "title": "T", "kind": "stat", "ghost": 1}])


def test_load_style_pack_missing_warns_and_fallback():
    """M6 前缺省回退:未指定/不存在 → 内置默认 + WARN stylePackMissing,行为可复现。"""
    css, warns = ab.load_style_pack("")
    assert css == "" and len(warns) == 1 and "stylePackMissing" in warns[0]
    css2, warns2 = ab.load_style_pack("no-such-pack")
    assert css2 == "" and "stylePackMissing" in warns2[0]


def test_hash_source_excludes_junction_dirs():
    """scaffold 的 fonts/vendor 联接目录不参与源码 hash(500MB 字体库不可每卡重读)。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        (src / "fonts").mkdir(parents=True)
        (src / "index.html").write_text("<html></html>", encoding="utf-8")
        h1 = ab.hash_source(src)
        (src / "fonts" / "BigFont.ttf").write_bytes(os.urandom(1024))
        assert ab.hash_source(src) == h1, "fonts/ 下文件不得影响 hash"
        (src / "vendor").mkdir()
        (src / "vendor" / "lib.js").write_text("x", encoding="utf-8")
        assert ab.hash_source(src) == h1, "vendor/ 下文件不得影响 hash"
        (src / "index.html").write_text("<html>v2</html>", encoding="utf-8")
        assert ab.hash_source(src) != h1


# ================================================================ gen-frames 子命令(假 artboard 技能)

# 假产物 MP4:真实 ffmpeg 造 0.4s testsrc(挂轨探测要 ffprobe 出真实时长)
def _make_tiny_mp4(path: Path, seconds: float = 0.4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"testsrc=duration={seconds}:size=320x540:rate=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                   check=True, capture_output=True, timeout=120)


def _stub_artboard(tmp_path: Path, *, check_ok: bool = True, export_ok: bool = True) -> Path:
    """假 artboard 技能:scaffold 建目录、check 出指定 JSON、export 出真 MP4(可令其失败)。"""
    art = tmp_path / "artboard-skill"
    (art / "scripts").mkdir(parents=True)
    check_line = '{"ok": true}' if check_ok else \
        '{"ok": false, "issues": [{"type": "C", "selector": "h1", "hint": "越出安全区(9x16):bottom 40px"}]}'
    export_body = f"""
import argparse, json, pathlib, subprocess, sys
p = argparse.ArgumentParser()
p.add_argument('--source', required=True)
p.add_argument('--output', required=True)
p.add_argument('--width', type=int, default=1080)
p.add_argument('--height', type=int, default=0)
p.add_argument('--format', default='PNG')
p.add_argument('--fps', type=int, default=25)
p.add_argument('--max-wait', type=float, default=15.0)
p.add_argument('--scale', type=int, default=1)
p.add_argument('--transparent', action='store_true')
a = p.parse_args()
out = pathlib.Path(a.output)
assert '--max-wait' in sys.argv, 'gen-frames 必须显式传 --max-wait(五段总和+1.5s)'
if not {export_ok}:
    print(json.dumps({{'ok': False, 'error': 'STUB_FAIL'}})); sys.exit(1)
subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
                '-i', f'testsrc=duration=0.4:size=1080x1920:rate=25',
                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(out)], check=True)
print(json.dumps({{'ok': True, 'format': a.format, 'engine': 'stub'}}))
"""
    (art / "scripts" / "export.py").write_text(export_body, encoding="utf-8")
    (art / "scripts" / "check_overflow.py").write_text(
        "import json\n"
        f"CHECK = {check_line!r}\n"
        "print(CHECK)\n", encoding="utf-8")
    (art / "scripts" / "scaffold.py").write_text(
        "import argparse, json, os, pathlib\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('slug')\n"
        "p.add_argument('--size', default='vertical')\n"
        "p.add_argument('--fonts', default='')\n"
        "p.add_argument('--force', action='store_true')\n"
        "a = p.parse_args()\n"
        "studio = pathlib.Path(os.environ['ARTBOARD_STUDIO'])\n"
        "proj = studio / a.slug\n"
        "(proj / 'src').mkdir(parents=True, exist_ok=True)\n"
        "(proj / 'export').mkdir(parents=True, exist_ok=True)\n"
        "(proj / 'src' / 'index.html').write_text('<!-- scaffold 模板 -->', encoding='utf-8')\n"
        "(proj / 'project.json').write_text(json.dumps({'slug': a.slug, 'width': 1080, 'height': 1920}), encoding='utf-8')\n"
        "print(json.dumps({'ok': True, 'project': str(proj)}))\n", encoding="utf-8")
    return art


def _run_gen_frames(root: Path, stub: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "CUTFLOW_ARTBOARD_DIR": str(stub)}
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "rs_artboard.py"), "gen-frames", str(root), *extra],
        cwd=root, env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600)


def _last_json(p: subprocess.CompletedProcess) -> dict:
    lines = [ln for ln in p.stdout.splitlines() if ln.strip().startswith("{")]
    assert lines, f"无 JSON 输出:\n{p.stdout}\n{p.stderr}"
    return json.loads(lines[-1])


def _frames_plan() -> list[dict]:
    return [
        {"id": "op01", "kind": "opener", "title": "三分钟看懂粗剪", "kicker": "效率剪辑",
         "accent": "#4F8CFF"},
        {"id": "tt01", "kind": "title", "title": "标题卡", "lines": ["断行即换色", "stagger"],
         "accent": "#FF6B4A"},
        {"id": "sc01", "kind": "section", "title": "第一章", "kicker": "SECTION 01",
         "accent": "#22C55E"},
        {"id": "st01", "kind": "stat", "title": "38%", "lines": ["线索成本下降"],
         "accent": "#F59E0B"},
        {"id": "cp01", "kind": "compare", "title": "对比", "kicker": "错/对",
         "lines": ["手动对轴", "锚点自动"], "accent": "#A78BFA"},
        {"id": "ou01", "kind": "outro", "title": "关注看下期", "lines": ["点赞收藏"],
         "accent": "#4F8CFF", "durationMs": 4000},
    ]


def _mk_proj(tmp_path: Path, plan: list[dict] | None = None) -> Path:
    root = tmp_path / "proj"
    (root / "00_制作简报").mkdir(parents=True)
    (root / "00_制作简报" / "cards.json").write_text(
        json.dumps(plan if plan is not None else _frames_plan(), ensure_ascii=False, indent=1),
        encoding="utf-8")
    return root


def test_gen_frames_full_chain_registers_and_exports(tmp_path):
    """全链:六卡生成 → 机检全过 → manifest 登记(kind=mp4/fiveSeg/maxWait)→ 导出成功。"""
    if not shutil_which("ffmpeg"):
        pytest.skip("无 ffmpeg:假产物造不出")
    root = _mk_proj(tmp_path)
    stub = _stub_artboard(tmp_path)
    r = _run_gen_frames(root, stub, "--pack", "no-such-pack")
    assert r.returncode == 0, r.stdout + r.stderr
    out = _last_json(r)
    assert out["code"] == "FRAMES_OK" and out["ok"]
    assert sorted(out["data"]["written"]) == ["cp01", "op01", "ou01", "sc01", "st01", "tt01"]
    assert any("stylePackMissing" in w for w in out["data"]["warnings"])
    assert all(c["ok"] for c in out["data"]["checks"]) and len(out["data"]["checks"]) == 6
    # manifest 登记:kind=mp4 + 五段 + maxWait + 工程根相对路径
    man = json.loads((root / "03_创作素材/artboard/manifest.json").read_text(encoding="utf-8"))
    items = {i["id"]: i for i in man["items"]}
    it = items["op01"]
    assert it["kind"] == "mp4" and it["frameKind"] == "opener" and it["fps"] == 25
    assert it["output"] == "03_创作素材/artboard/op01/export/op01.mp4"
    assert it["fiveSeg"]["t0"] == 2.0
    assert it["maxWait"] == pytest.approx(it["fiveSeg"]["total"] + 1.5)
    assert it["sourceHash"], "导出成功必须回写 sourceHash"
    # HTML 落盘且是五段式场景卡(不是 scaffold 模板)
    html = (root / "03_创作素材/artboard/op01/src/index.html").read_text(encoding="utf-8")
    assert "data-kind=\"opener\"" in html and "infinite" not in html.lower()
    # 样片在盘
    for cid in items:
        assert (root / items[cid]["output"]).is_file()
    # durationMs=4000 → 持住 = 4.0-1.6 = 2.4s
    assert items["ou01"]["fiveSeg"]["hold"] == pytest.approx(2.4)


def shutil_which(name: str) -> str | None:
    return shutil.which(name)


def test_gen_frames_idempotent_rerun(tmp_path):
    """重跑:HTML 字节一致(kept 全部)、导出全部在盘未变、manifest 逐字节稳定。"""
    if not shutil_which("ffmpeg"):
        pytest.skip("无 ffmpeg:假产物造不出")
    root = _mk_proj(tmp_path)
    stub = _stub_artboard(tmp_path)
    assert _run_gen_frames(root, stub).returncode == 0
    man1 = (root / "03_创作素材/artboard/manifest.json").read_text(encoding="utf-8")
    html1 = (root / "03_创作素材/artboard/op01/src/index.html").read_text(encoding="utf-8")
    r2 = _run_gen_frames(root, stub)
    assert r2.returncode == 0
    out2 = _last_json(r2)
    assert sorted(out2["data"]["kept"]) == ["cp01", "op01", "ou01", "sc01", "st01", "tt01"]
    assert sorted(out2["data"]["upToDate"]) == ["cp01", "op01", "ou01", "sc01", "st01", "tt01"]
    assert html1 == (root / "03_创作素材/artboard/op01/src/index.html").read_text(encoding="utf-8")
    assert man1 == (root / "03_创作素材/artboard/manifest.json").read_text(encoding="utf-8")


def test_gen_frames_refuses_hand_edited_card(tmp_path):
    """手改保护:与计划不一致的 index.html 不静默冲掉,CARD_EXISTS 非零,--force 才覆盖。"""
    if not shutil_which("ffmpeg"):
        pytest.skip("无 ffmpeg:假产物造不出")
    root = _mk_proj(tmp_path)
    stub = _stub_artboard(tmp_path)
    assert _run_gen_frames(root, stub).returncode == 0
    target = root / "03_创作素材/artboard/op01/src/index.html"
    target.write_text("<!-- 手改 -->", encoding="utf-8")
    r = _run_gen_frames(root, stub)
    assert r.returncode == 2 and _last_json(r)["code"] == "CARD_EXISTS"
    assert target.read_text(encoding="utf-8") == "<!-- 手改 -->"
    r2 = _run_gen_frames(root, stub, "--force")
    assert r2.returncode == 0 and "data-kind=\"opener\"" in target.read_text(encoding="utf-8")


def test_gen_frames_input_errors(tmp_path):
    """坏输入硬报错:--kind 非法 / 计划缺失 / 条目无 kind / compare 少 lines。"""
    stub = _stub_artboard(tmp_path)
    root = _mk_proj(tmp_path)
    r = _run_gen_frames(root, stub, "--kind", "ending")
    assert r.returncode == 2 and _last_json(r)["code"] == "BAD_KIND"

    root2 = tmp_path / "p2"
    (root2 / "00_制作简报").mkdir(parents=True)
    r2 = _run_gen_frames(root2, stub)
    assert r2.returncode == 2 and _last_json(r2)["code"] == "NO_PLAN"

    root3 = _mk_proj(tmp_path / "d3", [{"id": "c1", "template": "info", "title": "普通卡"}])
    r3 = _run_gen_frames(root3, stub)
    assert r3.returncode == 2 and _last_json(r3)["code"] == "NO_FRAMES"

    root4 = _mk_proj(tmp_path / "d4", [{"id": "c1", "kind": "compare", "title": "对比",
                                        "lines": ["只有一条"], "accent": "#4F8CFF"}])
    r4 = _run_gen_frames(root4, stub)
    assert r4.returncode == 2 and _last_json(r4)["code"] == "BAD_PLAN"
    # 普通卡 + 场景卡混排:--kind 筛选跳过普通卡而不报错
    root5 = _mk_proj(tmp_path / "d5", _frames_plan() + [
        {"id": "plain", "template": "info", "title": "普通卡"}])
    r5 = _run_gen_frames(root5, stub, "--kind", "opener,outro")
    assert r5.returncode == 0, r5.stdout + r5.stderr
    out5 = _last_json(r5)
    assert sorted(out5["data"]["written"]) == ["op01", "ou01"]
    assert any(s["id"] == "plain" for s in out5["data"]["skipped"])


def test_gen_frames_safe_check_gate(tmp_path):
    """机检门禁:check_overflow ok:false ⇒ 整条命令失败(SAFE_CHECK_FAILED),不导出不登记。"""
    root = _mk_proj(tmp_path)
    stub = _stub_artboard(tmp_path, check_ok=False)
    r = _run_gen_frames(root, stub)
    assert r.returncode == 4 and _last_json(r)["code"] == "SAFE_CHECK_FAILED"
    out = _last_json(r)
    assert out["data"]["failed"] and out["data"]["failed"][0]["issues"]
    assert not (root / "03_创作素材/artboard/manifest.json").exists(), "机检未过不得登记 manifest"


def test_gen_frames_export_failure_honest(tmp_path):
    """导出失败诚实降级:非零退出 + failed 明细;HTML/机检结果留盘,manifest 留空 hash 待补导。"""
    root = _mk_proj(tmp_path)
    stub = _stub_artboard(tmp_path, export_ok=False)
    r = _run_gen_frames(root, stub)
    assert r.returncode == 4 and _last_json(r)["code"] == "EXPORT_FAILED"
    out = _last_json(r)
    assert len(out["data"]["failed"]) == 6
    man = json.loads((root / "03_创作素材/artboard/manifest.json").read_text(encoding="utf-8"))
    assert all(i["sourceHash"] == "" for i in man["items"]), "失败不得回写 hash(留待 --export 重试)"


def test_gen_frames_pack_frames_css_injected(tmp_path):
    """包命中且含 frames.css → 注入产物样式;缺失回退已由 stylePackMissing 用例覆盖。"""
    if not shutil_which("ffmpeg"):
        pytest.skip("无 ffmpeg:假产物造不出")
    root = _mk_proj(tmp_path)
    # M8:rs_artboard 探测位已修正为 skills/cutflow/templates/styles/packs/(与真身同源),
    # 桩包直接写真身目录,收尾只清桩包本体(绝不动真身 packs/ 目录)
    packs = REPO / "skills" / "cutflow" / "templates" / "styles" / "packs"
    pack = packs / "m7-stub"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "frames.css").write_text("/* 测试包样式 */ .poster{--probe:1;}\n", encoding="utf-8")
    try:
        stub = _stub_artboard(tmp_path)
        r = _run_gen_frames(root, stub, "--pack", "m7-stub")
        assert r.returncode == 0, r.stdout + r.stderr
        out = _last_json(r)
        assert not any("stylePackMissing" in w for w in out["data"]["warnings"])
        html = (root / "03_创作素材/artboard/op01/src/index.html").read_text(encoding="utf-8")
        assert "/* 测试包样式 */" in html
    finally:
        shutil.rmtree(pack, ignore_errors=True)


def test_gen_frames_attach_propagates_probed_duration(tmp_path):
    """挂轨:IR 引用产物时按 ffprobe 探测时长回填,后续 clip 平移,给出需重跑下游。"""
    if not shutil_which("ffmpeg"):
        pytest.skip("无 ffmpeg:假产物造不出")
    root = _mk_proj(tmp_path)
    stub = _stub_artboard(tmp_path)
    # 预置 IR:overlay 轨引用三张产物,名义 3000ms(故意错,真产物 400ms)
    ir = {"version": 1, "slug": "m7", "fps": 25,
          "canvas": {"width": 1080, "height": 1920},
          "tracks": [
              {"kind": "video", "name": "main", "clips": [
                  {"src": "01_原始素材/a.mp4", "startMs": 0, "durationMs": 20000,
                   "sourceInMs": 0}]},
              {"kind": "video", "name": "overlay", "clips": [
                  {"src": "03_创作素材/artboard/op01/export/op01.mp4",
                   "startMs": 0, "durationMs": 3000},
                  {"src": "03_创作素材/artboard/tt01/export/tt01.mp4",
                   "startMs": 3000, "durationMs": 3000},
                  {"src": "03_创作素材/artboard/ou01/export/ou01.mp4",
                   "startMs": 6000, "durationMs": 3000}]}]}
    (root / "05_时间线工程").mkdir()
    (root / "05_时间线工程" / "project.json").write_text(
        json.dumps(ir, ensure_ascii=False, indent=1), encoding="utf-8")
    r = _run_gen_frames(root, stub)
    assert r.returncode == 0, r.stdout + r.stderr
    out = _last_json(r)
    attach = out["data"]["attach"]
    assert attach["done"] and attach["staleStages"] == ["S4", "S5", "S6", "S7", "S8", "S9"]
    by_id = {c["id"]: c for c in attach["changes"] if "oldDurationMs" in c}
    assert by_id["op01"]["newDurationMs"] == 400, "挂轨必须按 ffprobe 探测时长,不吃名义值"
    assert by_id["op01"]["shiftedClips"] == 2, "时长变化必须平移后续 clip"
    assert by_id["tt01"]["oldDurationMs"] == 3000
    # usedIn 回写 manifest
    man = json.loads((root / "03_创作素材/artboard/manifest.json").read_text(encoding="utf-8"))
    op01 = {i["id"]: i for i in man["items"]}["op01"]
    assert op01["usedIn"], "挂轨成功必须回写 usedIn"


def test_gen_frames_size_mismatch_hard_fail(tmp_path):
    """尺寸不符报错不拉伸:IR 画幅与卡片画幅不一致 ⇒ APPLY_ISSUES 非零,不回填 IR。"""
    if not shutil_which("ffmpeg"):
        pytest.skip("无 ffmpeg:假产物造不出")
    root = _mk_proj(tmp_path)
    stub = _stub_artboard(tmp_path)
    # IR 画幅是 16:9,卡片按 9:16 生成 ⇒ 尺寸不符必须硬失败(gen-frames 内部走 apply_to_ir)
    ir = {"version": 1, "slug": "m7", "fps": 25,
          "canvas": {"width": 1920, "height": 1080},
          "tracks": [{"kind": "video", "name": "overlay", "clips": [
              {"src": "03_创作素材/artboard/op01/export/op01.mp4",
               "startMs": 0, "durationMs": 400}]}]}
    (root / "05_时间线工程").mkdir()
    (root / "05_时间线工程" / "project.json").write_text(
        json.dumps(ir, ensure_ascii=False, indent=1), encoding="utf-8")
    r = _run_gen_frames(root, stub)
    assert r.returncode == 4 and _last_json(r)["code"] == "APPLY_ISSUES"
    issues = _last_json(r)["data"]["issues"]
    assert any("不符" in s and "拉伸" in s for s in issues), issues
    # IR 未被回填(不带着坏输入往下跑)
    ir_txt = (root / "05_时间线工程" / "project.json").read_text(encoding="utf-8")
    assert json.loads(ir_txt)["canvas"]["width"] == 1920
