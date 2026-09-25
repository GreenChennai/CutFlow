# -*- coding: utf-8 -*-
"""M8 第二波(方案 §5.5.3)短剧/影视解说门禁:双 Style 字幕 + 版权门禁 + 钩子/废帧判据。

覆盖面:
· rs_subtitle --dual-style:ASS 双 Style 段(解说体 Main / 原声对白体 Quote 暖黄斜体),
  对白自动包中文引号;对白卡切分预算预留引号位(QUOTE_RESERVE_CHARS);并卡/吞卡
  不跨声轨(断句保护);无 voice 标记降级单 Style 留痕;既有字幕对账(parse_ass ↔
  wordline)不被引号破坏;
· rs_ingest copyright:引用素材必须登记(缺登记非零退出);条目校验;素材侧占比预检
  (单部 ≤30% / 总引用 ≤70%,超阈 WARN,权威门禁在 rs_verify);
· rs_verify L0 版权判据:登记完整性 + 单部占比 + 总引用占比 + 原创解说轨占比(解说型,
  原声对白句不计入);阈值 CLI/params 可覆盖;「转化性/纯剧透替代/AI 标识」机械不可判
  → l1Pending 显式标注(WARN 非 PASS,方案 R7 不假绿);
· rs_sync drama.hook:3s 钩子/5s 反转/前 7s 锚定密度判据(WARN 级,不翻转总判定);
  ≥1.2s 全画面无变化即废帧(硬判);经能力声明启用(ADR-0047 数据驱动);
· 端到端合成验收:解说型夹具工程(testsrc2 + 字幕条模拟对白 × 3 素材 + 合成解说音轨口径
  的 wordline)跑 S7/S8(等价)/S9 → 双 Style 生效、占比真值、废帧报警、成片产出;
  短剧型小夹具注入 3s/5s 节奏标记验证钩子密度输出。

运行:pytest tests/test_drama_commentary.py -q(§e2e 需 ffmpeg,不可用时整段 SKIP)
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_paths  # noqa: E402
import rs_align  # noqa: E402
import rs_common  # noqa: E402
import rs_edit  # noqa: E402
import rs_ingest  # noqa: E402
import rs_subtitle  # noqa: E402
import rs_sync  # noqa: E402
import rs_verify  # noqa: E402


# ---------------------------------------------------------------- 环境与夹具

def _ffmpeg_bin() -> str:
    try:
        cfg = rs_common.load_config()
        p = rs_common.ffmpeg_bin(cfg)
        if Path(p).is_file():
            return p
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


FF = _ffmpeg_bin()


def _run_ff(*args: str, timeout: int = 300) -> None:
    p = subprocess.run([FF, "-v", "error", "-y", *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout)
    assert p.returncode == 0, f"ffmpeg 合成失败:{p.stderr[-300:]}"


def _capture(fn, *args, **kw):
    """跑 main() 收 stdout 里的 JSON 契约(与既有测试同一套路)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip().startswith("{")]
    doc = json.loads(lines[-1]) if lines else {}
    return code, doc


def _capture_argv(fn, argv: list[str]):
    """main() 都走 argparse(sys.argv):替换后跑,还原,收 JSON。"""
    old = sys.argv
    sys.argv = argv
    try:
        return _capture(fn)
    finally:
        sys.argv = old


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _mk_project(tmp_path: Path, tag: str, video_type: str = "drama",
                capabilities: list[str] | None = None) -> Path:
    """最小工程:阶段目录 + 意图编译产物(videoType/capabilities 声明)。"""
    root = tmp_path / tag
    rs_paths.ensure(root)
    d = root / rs_paths.p("brief")
    (d / "intent_decisions.json").write_text(json.dumps({
        "version": 1, "kind": "cutflow-intent-decisions",
        "resolved": {"videoType": video_type,
                     "capabilities": capabilities if capabilities is not None
                     else ["sub.pair", "drama.hook"]},
    }, ensure_ascii=False), encoding="utf-8")
    return root


def _mk_wordline(root: Path, segments: list[dict], *, media_ms: int | None = None) -> Path:
    """segments: [{start, end, text, voice?}] → 05_时间线工程/wordline.json(句级 voice 落账)。"""
    wl = rs_align.build_wordline(
        [{k: v for k, v in s.items() if k != "voice"} for s in segments],
        "test-fixture", degraded="测试夹具(句级时间,无字级戳)",
        media_duration_ms=media_ms)
    by_text = {s["text"]: s for s in wl.get("sentences") or []}
    for s in segments:
        sent = by_text.get(s["text"])
        if sent and s.get("voice"):
            sent["voice"] = s["voice"]
    p = rs_paths.wordline_json(root)
    p.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def _style_lines(ass_path: Path) -> dict[str, str]:
    out = {}
    for line in ass_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("Style:"):
            out[line.split(",")[0].partition(":")[2].strip()] = line
    return out


# ================================================================ §1 rs_subtitle --dual-style

def _dual_wordline(root: Path) -> Path:
    """解说 + 原声对白混合 wordline(影视解说的典型形态)。"""
    return _mk_wordline(root, [
        {"start": 0.0, "end": 2.4, "text": "这个反转没有人猜到。"},
        {"start": 2.4, "end": 4.4, "text": "你以为我会放过你吗", "voice": "dialogue"},
        {"start": 4.4, "end": 7.2, "text": "导演在这里埋了三层伏笔。"},
    ])


def test_dual_style_ass_two_styles_and_quotes(tmp_path):
    """双 Style:Main(解说体)+ Quote(原声对白体,斜体暖黄);对白自动包中文引号。"""
    root = _mk_project(tmp_path, "sub")
    wl_path = _dual_wordline(root)
    wl = _read(wl_path)
    events, meta = rs_subtitle.events_from_wordline(wl, 12, dual_style=True)
    assert meta["dualStyle"]["applied"] is True
    assert meta["dualStyle"]["dialogueCards"] >= 1
    dlg = [e for e in events if e.get("voice") == rs_subtitle.VOICE_DIALOGUE]
    assert dlg and all(len(e["text"].replace(" ", "")) <= 12 - rs_subtitle.QUOTE_RESERVE_CHARS
                       for e in dlg), "对白卡原始文本必须给引号预留 2 字预算"
    ass = tmp_path / "subtitles.ass"
    assert rs_subtitle.write_ass(events, ass, "subtitle-white", "9x16", "1080x1920",
                                 dual_style=True) is True
    text = ass.read_text(encoding="utf-8")
    styles = _style_lines(ass)
    assert "Main" in styles and rs_subtitle.QUOTE_STYLE_NAME in styles, \
        "ASS 必须含两个 Style 段(解说体 + 原声对白体)"
    # 原声对白体:斜体(Italic=1)+ 暖黄,与解说体视觉可区分
    assert styles[rs_subtitle.QUOTE_STYLE_NAME].split(",")[8].strip() == "1"
    assert rs_subtitle.QUOTE_COLOR in styles[rs_subtitle.QUOTE_STYLE_NAME]
    for line in text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        body = line.split(",", 9)[9]
        if ",Quote," in line:
            assert body.startswith(rs_subtitle.QUOTE_OPEN) \
                and body.endswith(rs_subtitle.QUOTE_CLOSE), f"对白卡必须包引号:{body}"
        else:
            assert rs_subtitle.QUOTE_OPEN not in body, f"解说卡不得带引号:{body}"


def test_dual_style_quotes_idempotent(tmp_path):
    """已带前引号的对白不重复包引号(quote_text 幂等)。"""
    e = {"text": f"{rs_subtitle.QUOTE_OPEN}台词{rs_subtitle.QUOTE_CLOSE}"}
    assert rs_subtitle.quote_text(e) == e["text"]
    e2 = {"text": "台词"}
    wrapped = rs_subtitle.quote_text(e2)
    assert wrapped.startswith(rs_subtitle.QUOTE_OPEN) and wrapped.endswith(rs_subtitle.QUOTE_CLOSE)


def test_dual_style_degrades_without_voice(tmp_path):
    """无 voice 标记 → 降级单 Style(sub.pair 降级档:单 Style + 引号标注)并留痕。"""
    root = _mk_project(tmp_path, "subdeg")
    wl_path = _mk_wordline(root, [
        {"start": 0.0, "end": 2.0, "text": "全程解说没有对白标记。"},
        {"start": 2.0, "end": 4.0, "text": "第二句也按解说处理。"},
    ])
    events, meta = rs_subtitle.events_from_wordline(_read(wl_path), 12, dual_style=True)
    assert meta["dualStyle"]["applied"] is False
    assert any("降级" in r for r in meta["degradeReasons"]), "降级必须留痕"
    ass = tmp_path / "subtitles.ass"
    assert rs_subtitle.write_ass(events, ass, "subtitle-white", "9x16", "1080x1920",
                                 dual_style=True) is False
    styles = _style_lines(ass)
    assert rs_subtitle.QUOTE_STYLE_NAME not in styles, "无对白不得出现 Quote Style 段"


def test_dual_style_merge_never_crosses_voice():
    """断句保护:必并/幽灵卡并卡不跨声轨(解说卡与对白卡不合并)。"""
    mk = lambda t, v: {"start": 0.0, "end": 0.5, "text": t, "voice": v}  # noqa: E731
    events = [mk("甲", "commentary"), mk("乙", "dialogue"), mk("丙", "dialogue")]
    merged, n = rs_subtitle._merge_short([dict(e) for e in events], 12)
    voices = [e.get("voice") for e in merged]
    assert "commentary" not in voices[1:], "解说卡不得与对白卡合并"
    assert any(e.get("voice") == "dialogue" and len(e["text"]) >= 2 for e in merged), \
        "同声轨的过短卡仍应正常必并"


def test_dual_style_quotes_survive_sync_matching(tmp_path):
    """既有对账不回归:引号在 rs_sync.norm 的剥离表内,ASS ↔ wordline 全量匹配。"""
    root = _mk_project(tmp_path, "subsync")
    wl_path = _dual_wordline(root)
    wl = _read(wl_path)
    events, _ = rs_subtitle.events_from_wordline(wl, 12, dual_style=True)
    ass = tmp_path / "subtitles.ass"
    rs_subtitle.write_ass(events, ass, "subtitle-white", "9x16", "1080x1920", dual_style=True)
    rows = rs_sync.check_offsets(rs_sync.parse_ass(ass), wl)
    assert rows and all(r.get("matched") for r in rows), \
        "引号不得破坏字幕 ↔ wordline 文本匹配"


def test_dual_style_cli(tmp_path):
    """CLI 契约:--dual-style 落盘 ASS 双 Style,data 带dualStyleApplied;无 wordline 报错。"""
    root = _mk_project(tmp_path, "subcli")
    wl_path = _dual_wordline(root)
    out = tmp_path / "out"
    code, doc = _capture_argv(rs_subtitle.main, [
        "rs_subtitle.py", "--from-wordline", str(wl_path), "--dual-style",
        "--out", str(out), "--no-snap"])
    assert code == 0 and doc["code"] == "SUBTITLE_OK", doc
    assert doc["data"]["dualStyle"] is True and doc["data"]["dualStyleApplied"] is True
    assert rs_subtitle.QUOTE_STYLE_NAME in _style_lines(out / "subtitles.ass")
    cards = _read(out / "cards.json")["cards"]
    assert any(c["voice"] == "dialogue" for c in cards), "cards.json 审计件带 voice"
    code2, doc2 = _capture_argv(rs_subtitle.main, [
        "rs_subtitle.py", "--from-tts", str(wl_path), "--dual-style", "--out", str(out)])
    assert code2 == 2 and doc2["code"] == "DUAL_NEEDS_WORDLINE"


# ================================================================ §2 rs_ingest copyright 登记

def _mk_materials(root: Path, items: list[dict]) -> None:
    man = rs_paths.manifest_json(root)
    man.write_text(json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=1),
                   encoding="utf-8")


def test_ingest_copyright_missing_registration(tmp_path):
    """缺登记表 → 非零退出(方案:引用素材必须登记,不 WARN 放行)。"""
    root = _mk_project(tmp_path, "cpmiss")
    _mk_materials(root, [{"file": "a.mp4", "probe": "ok", "durationMs": 6000}])
    res = rs_ingest.check_copyright_registration(root)
    assert res["ok"] is False and res["code"] == "COPYRIGHT_REGISTRATION_MISSING"
    code, doc = _capture_argv(rs_ingest.main, ["rs_ingest.py", "copyright", str(root)])
    assert code == 4 and doc["ok"] is False, "缺登记必须非零退出"


def test_ingest_copyright_ok_with_known_ratios(tmp_path):
    """登记完整 → 占比预检;fixture 真值:总素材 10s,引用 6s(单部 60% → WARN,硬闸在 verify)。"""
    root = _mk_project(tmp_path, "cpok")
    _mk_materials(root, [
        {"file": "a.mp4", "probe": "ok", "durationMs": 6000},
        {"file": "b.mp4", "probe": "ok", "durationMs": 4000},
    ])
    (rs_ingest.copyright_json(root)).write_text(json.dumps({
        "version": 1, "kind": "commentary",
        "transformNote": "含原创锐评与结构重组",
        "aiDisclosure": True,
        "items": [
            {"file": "a.mp4", "source": "《剧甲》第一集", "usage": "quote", "quotedMs": 6000},
            {"file": "b.mp4", "source": "原创自拍", "usage": "original"},
        ]}, ensure_ascii=False), encoding="utf-8")
    res = rs_ingest.check_copyright_registration(root)
    assert res["ok"] is True, res
    stats = res["stats"]
    assert stats["quoteRatio"] == 0.6, "总引用占比真值 6s/10s"
    assert stats["perSource"]["《剧甲》第一集"]["ratio"] == 0.6, "单部占比真值"
    assert any("单部" in w for w in res["warns"]), "单部 60% > 30% 预检 WARN"
    code, doc = _capture_argv(rs_ingest.main, ["rs_ingest.py", "copyright", str(root)])
    assert code == 0 and doc["ok"] is True


def test_ingest_copyright_invalid_entry(tmp_path):
    """quote 条目缺 source / file 不在清单 → 登记表校验失败非零退出。"""
    root = _mk_project(tmp_path, "cpbad")
    _mk_materials(root, [{"file": "a.mp4", "probe": "ok", "durationMs": 6000}])
    (rs_ingest.copyright_json(root)).write_text(json.dumps({
        "items": [{"file": "a.mp4", "usage": "quote", "quotedMs": 1000},
                  {"file": "ghost.mp4", "source": "x", "usage": "quote", "quotedMs": 1}]},
        ensure_ascii=False), encoding="utf-8")
    res = rs_ingest.check_copyright_registration(root)
    assert res["ok"] is False and res["code"] == "COPYRIGHT_REGISTRATION_INVALID"
    assert len(res["errors"]) >= 2


def test_ingest_scan_ignores_copyright_json(tmp_path):
    """copyright.json 是登记表不是素材:scan 不收进 manifest,MANIFEST.md 带版权节。"""
    root = _mk_project(tmp_path, "cpscan")
    (rs_paths.resolve(root, "materials") / "a.txt").write_text("x", encoding="utf-8")
    (rs_ingest.copyright_json(root)).write_text(
        json.dumps({"items": [{"file": "a.mp4", "usage": "original"}]}), encoding="utf-8")
    res = rs_ingest.scan(root, "cpscan")
    assert res["ok"] is True, res
    names = [i["file"] for i in res["items"]]
    assert "copyright.json" not in names, "登记表不得混进素材清单"
    md = (rs_paths.resolve(root, "materials") / "MANIFEST.md").read_text(encoding="utf-8")
    assert "版权登记" in md and "已登记" in md


# ================================================================ §3 rs_verify L0 版权判据

def _mk_copyright_ir(root: Path, *, clips: list[dict], kind: str = "commentary",
                     transform: str | None = "含原创锐评,结构重组,非剧情替代",
                     ai: bool = True, sources: dict[str, str] | None = None):
    """IR(在用素材时长)+ 登记表 + 解说 wordline。clips: [{file, ms, usage, source}]。"""
    tl = rs_paths.resolve(root, "timeline")
    ir = {"version": 1, "slug": root.name, "fps": 30,
          "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"id": "V1", "kind": "video", "name": "main",
                      "clips": [{"id": f"V1-{i + 1:03d}",
                                 "src": f"{rs_paths.p('materials')}/{c['file']}",
                                 "startMs": 0, "durationMs": c["ms"], "sourceInMs": 0}
                                for i, c in enumerate(clips)]}],
          "outputs": ["9x16"]}
    rs_paths.project_json(root).write_text(rs_edit.dump_json(ir), encoding="utf-8")
    items = []
    for c in clips:
        if c.get("usage") == "original":
            items.append({"file": c["file"], "source": "原创自拍", "usage": "original"})
        else:
            items.append({"file": c["file"], "source": c.get("source") or c["file"],
                          "usage": "quote", "quotedMs": c["ms"]})
    (rs_ingest.copyright_json(root)).write_text(json.dumps({
        "version": 1, "kind": kind,
        "transformNote": transform, "aiDisclosure": ai, "items": items},
        ensure_ascii=False), encoding="utf-8")
    speech = 0.0
    segs = []
    cursor = 0.0
    for c in clips:
        if c.get("voice") == "dialogue":
            segs.append({"start": cursor, "end": cursor + c["ms"] / 1000,
                         "text": "你居然敢骗我", "voice": "dialogue"})
        else:
            segs.append({"start": cursor, "end": cursor + c["ms"] / 1000,
                         "text": "解说词" * max(2, int(c["ms"] / 500))})
        cursor += c["ms"] / 1000
    _mk_wordline(root, segs)
    return clips


def test_verify_copyright_ratios_known_truth(tmp_path):
    """三判据真值:单部 3s/12s=25%、总引用 6s/12s=50%、解说轨(不含对白)≈81.7%。"""
    root = _mk_project(tmp_path, "vok")
    clips = [
        {"file": "a.mp4", "ms": 3000, "usage": "quote", "source": "《剧甲》"},
        {"file": "b.mp4", "ms": 3000, "usage": "quote", "source": "《剧乙》"},
        {"file": "c.mp4", "ms": 4000, "usage": "original"},
        {"file": "d.mp4", "ms": 2000, "usage": "original", "voice": "dialogue"},
    ]
    _mk_copyright_ir(root, clips=clips)
    chk = rs_verify.check_copyright(root)
    assert chk["ok"] is True, chk
    r = chk["ratios"]
    assert abs(r["singleSource"] - 0.25) < 1e-6, f"单部占比真值 0.25,得 {r}"
    assert abs(r["quoteTotal"] - 0.5) < 1e-6, "总引用占比真值 0.5"
    assert r["commentary"] is not None and r["commentary"] >= 0.25
    assert chk["thresholds"] == {"maxSingleSource": 0.30, "maxQuoteTotal": 0.70,
                                 "minCommentary": 0.25}, "初值 = 方案建议档"
    # 原声对白句不计入原创解说轨:解说轨 ms = 3000+3000+4000 = 10000
    assert abs(rs_verify._wordline_commentary_ms(root) - 10000) < 120, \
        "原声对白句必须从原创解说轨时长中剔除"


def test_verify_copyright_single_source_over_threshold_fails(tmp_path):
    """单部 60% > 30% → ok=False,L0 非零退出(不达标非零退出)。"""
    root = _mk_project(tmp_path, "vbad")
    _mk_copyright_ir(root, clips=[
        {"file": "a.mp4", "ms": 7200, "usage": "quote", "source": "《剧甲》"},
        {"file": "b.mp4", "ms": 4800, "usage": "original"},
    ])
    chk = rs_verify.check_copyright(root)
    assert chk["ok"] is False
    assert any("单部" in v and "60" in v for v in chk["violations"]), chk["violations"]
    res = rs_verify.collect_l0(root)
    assert res["pass"] is False and "版权门禁" in "".join(res["failed"])


def test_verify_copyright_unregistered_material_fails(tmp_path):
    """声明 drama 工程缺登记 / IR 在用素材未登记 → 硬失败(必须登记)。"""
    root = _mk_project(tmp_path, "vnoreg")
    rs_paths.project_json(root).write_text(rs_edit.dump_json({
        "version": 1, "slug": "x", "fps": 30, "canvas": {"width": 1080, "height": 1920},
        "tracks": [{"kind": "video", "name": "main", "clips": [
            {"src": f"{rs_paths.p('materials')}/a.mp4", "startMs": 0,
             "durationMs": 4000, "sourceInMs": 0}]}], "outputs": ["9x16"]}),
        encoding="utf-8")
    chk = rs_verify.check_copyright(root)
    assert chk["ok"] is False and any("未登记" in v for v in chk["violations"])
    # 口播工程(未声明 drama,无登记表)→ skipped,不误伤全库
    root2 = _mk_project(tmp_path, "vplain", video_type="talking-head", capabilities=[])
    chk2 = rs_verify.check_copyright(root2)
    assert chk2["ok"] is True and chk2.get("skipped")


def test_verify_copyright_l1_items_never_fake_green(tmp_path):
    """转化性/纯剧透/AI 标识机械不可判 → l1Pending 显式标注;报告落 ⚠,不标 PASS(方案 R7)。"""
    root = _mk_project(tmp_path, "vl1")
    _mk_copyright_ir(root, clips=[
        {"file": "a.mp4", "ms": 3000, "usage": "quote", "source": "《剧甲》"},
        {"file": "b.mp4", "ms": 9000, "usage": "original"}], transform=None, ai=False)
    chk = rs_verify.check_copyright(root)
    assert chk["ok"] is True, "机械判据过 = ok,但 L1 项必须显式存在"
    l1 = chk["l1Pending"]
    assert any("转化性" in x for x in l1) and any("剧透" in x for x in l1) \
        and any("AI" in x for x in l1), l1
    out = root / rs_paths.p("output")
    out.mkdir(parents=True, exist_ok=True)
    res = rs_verify.collect_l0(root)
    rs_verify.write_report(res, out / "verify_report.md")
    md = (out / "verify_report.md").read_text(encoding="utf-8")
    assert "L1/用户项" in md and "⚠" in md, "L1 项必须显式落进报告(WARN 非 PASS)"
    payload = rs_verify.l1_payload(root)
    assert any("转化性" in x for x in payload["checklist"]), "L1 清单并入版权人工项"


def test_verify_copyright_threshold_overridable(tmp_path):
    """初值可配:CLI 入参 > params > 常量(方案:实测定档)。"""
    root = _mk_project(tmp_path, "vthr")
    _mk_copyright_ir(root, clips=[
        {"file": "a.mp4", "ms": 3000, "usage": "quote", "source": "《剧甲》"},
        {"file": "b.mp4", "ms": 9000, "usage": "original"}])
    assert rs_verify.check_copyright(root)["ok"] is True
    tight = rs_verify.check_copyright(root, max_single=0.10)
    assert tight["ok"] is False and tight["thresholds"]["maxSingleSource"] == 0.10
    # params 通道:pipeline.json params.copyrightMaxSingleSource
    pj = rs_paths.pipeline_json(root)
    pj.write_text(json.dumps({"params": {"copyrightMaxSingleSource": 0.05}}), encoding="utf-8")
    chk = rs_verify.check_copyright(root)
    assert chk["ok"] is False and chk["thresholds"]["maxSingleSource"] == 0.05


# ================================================================ §4 rs_sync drama.hook(ffmpeg)

@pytest.mark.skipif(not FF, reason="ffmpeg 不可用(合成输入必需)")
def test_waste_frames_detects_static_insert(tmp_path):
    """≥1.2s 全画面无变化即废帧:动态画面中注入 2s 静止段 → 命中 [3,5]。"""
    v = tmp_path / "static.mp4"
    _run_ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=3",
            "-f", "lavfi", "-i", "color=c=gray:size=320x240:rate=15:duration=2",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=3",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]",
            "-map", "[out]", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(v))
    wf = rs_sync.detect_waste_frames(v)
    assert not wf.get("skipped")
    assert wf["pass"] is False and wf["spans"], "注入的 2s 静止段必须报警"
    a, b = wf["spans"][0]
    assert a < 4.0 and b > 4.0 and (b - a) >= rs_sync.WASTE_FRAME_MIN_S, wf["spans"]


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用(合成输入必需)")
def test_hook_density_three_second_rhythm(tmp_path):
    """3s 一切(钩子节奏)→ 前 7s 有锚定、间隔 ≤5s,密度判据 pass;6.5s 长镜 → WARN。"""
    fast = tmp_path / "fast.mp4"
    # 场景检测只看亮度平面(Y):四色必须亮度错开,纯色度切换(红→绿)会得 0 分漏检
    parts = ["color=c=black:size=320x240:rate=10:duration=3",
             "color=c=red:size=320x240:rate=10:duration=3",
             "color=c=yellow:size=320x240:rate=10:duration=3",
             "color=c=blue:size=320x240:rate=10:duration=3"]
    ins, fc = [], []
    for i, src in enumerate(parts):
        ins += ["-f", "lavfi", "-i", src]
        fc.append(f"[{i}:v]")
    _run_ff(*ins, "-filter_complex", "".join(fc) + "concat=n=4:v=1:a=0[out]",
            "-map", "[out]", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(fast))
    hd = rs_sync.check_hook_density(fast)
    assert not hd.get("skipped") and hd["applied"]
    assert hd["anchorFirst7s"] is True, "前 7s 必须检出画面变化(情绪锚定)"
    assert hd["changeCount"] >= 3 and not hd["gapsOverTwist"], hd
    assert hd["pass"] is True and hd["warnOnly"] is True
    # 慢节奏(6.5s 单镜):前 7s 无变化 + 间隔 6.5s > 5s → pass=False(WARN 级)
    slow = tmp_path / "slow.mp4"
    _run_ff("-f", "lavfi", "-i", "color=c=red:size=320x240:rate=10:duration=6.5",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(slow))
    hd2 = rs_sync.check_hook_density(slow)
    assert hd2["pass"] is False
    assert hd2["anchorFirst7s"] is False and hd2["gapsOverTwist"]


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用(合成输入必需)")
def test_sync_main_wires_drama_hook(tmp_path):
    """能力声明启用判据(ADR-0047):drama.hook 声明 → sync_rows/报告带钩子+废帧;
    钩子 WARN 不翻转总判定,废帧命中翻转(硬判)。

    视频用 hue 移位的 testsrc2 四段拼接:画面持续运动(不触废帧)且段界即
    场景变化(3s 钩子节奏)—— 一条素材同时喂两条判据的正面口径。
    """
    root = _mk_project(tmp_path, "hookproj")
    fast = tmp_path / "fast.mp4"
    # 场景检测只看亮度平面:用亮度阶梯(eq=brightness)而非 hue(hue 只转色度,切换不可见);
    # testsrc2 持续运动 → 不触废帧,段界亮度跳变 → 恰为 3s 钩子节奏
    ins, fc = [], []
    for i, b in enumerate((0, 0.3, 0.6, -0.4)):
        ins += ["-f", "lavfi", "-i",
                f"testsrc2=size=320x240:rate=10:duration=3,eq=brightness={b}"]
        fc.append(f"[{i}:v]")
    _run_ff(*ins, "-filter_complex", "".join(fc) + "concat=n=4:v=1:a=0[out]",
            "-map", "[out]", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(fast))
    wl_path = _mk_wordline(root, [{"start": 0.0, "end": 10.0, "text": "钩子反转样句。" * 2}],
                           media_ms=12000)
    wl = _read(wl_path)
    events, _ = rs_subtitle.events_from_wordline(wl, 12)
    ass = root / rs_paths.p("output") / "subtitles.ass"
    rs_subtitle.write_ass(events, ass, "subtitle-white", "9x16", "1080x1920")
    out = root / rs_paths.p("output")
    code, doc = _capture_argv(rs_sync.main, [
        "rs_sync.py", "--wordline", str(wl_path), "--ass", str(ass),
        "--out", str(out), "--video", str(fast)])
    assert code == 0 and doc["data"]["pass"] is True, doc
    assert doc["data"]["dramaHooks"]["applied"] is True and doc["data"]["dramaHooks"]["pass"] is True
    assert doc["data"]["wasteFrames"]["pass"] is True, "持续运动的成片不得误报废帧"
    rows = _read(out / "sync_rows.json")["summary"]
    assert rows["dramaHooks"]["changeCount"] >= 3, "3s 节奏标记必须在 sync_rows 留痕"
    assert "钩子" in (out / "sync_report.md").read_text(encoding="utf-8")
    # 无能力声明的普通工程 → 判据不启用(不误伤)
    root2 = _mk_project(tmp_path, "plainproj", video_type="talking-head", capabilities=[])
    wl2 = _mk_wordline(root2, [{"start": 0.0, "end": 10.0, "text": "钩子反转样句。" * 2}])
    events2, _ = rs_subtitle.events_from_wordline(_read(wl2), 12)
    ass2 = root2 / rs_paths.p("output") / "subtitles.ass"
    rs_subtitle.write_ass(events2, ass2, "subtitle-white", "9x16", "1080x1920")
    code2, doc2 = _capture_argv(rs_sync.main, [
        "rs_sync.py", "--wordline", str(wl2), "--ass", str(ass2),
        "--out", str(root2 / rs_paths.p("output")), "--video", str(fast)])
    assert code2 == 0 and doc2.get("dramaHooks") is None


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用(合成输入必需)")
def test_hook_density_is_warn_only(tmp_path):
    """WARN 级实证:6s 长镜(间隔 >5s)→ 钩子判据 pass=False 但 sync 总判定不翻转;
    对照:废帧命中 → 总判定翻转。两条判据的严重度分层必须可见。"""
    root = _mk_project(tmp_path, "warnproj")
    slow = tmp_path / "slowmove.mp4"
    _run_ff("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=6",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=5.5,hue=h=180",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[out]",
            "-map", "[out]", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(slow))
    assert rs_sync.check_hook_density(slow)["pass"] is False, "6s 间隔必须 WARN"
    assert rs_sync.detect_waste_frames(slow)["pass"] is True, "运动画面不是废帧"
    wl_path = _mk_wordline(root, [{"start": 0.0, "end": 10.0, "text": "钩子反转样句。" * 2}],
                           media_ms=11500)
    events, _ = rs_subtitle.events_from_wordline(_read(wl_path), 12)
    ass = root / rs_paths.p("output") / "subtitles.ass"
    rs_subtitle.write_ass(events, ass, "subtitle-white", "9x16", "1080x1920")
    code, doc = _capture_argv(rs_sync.main, [
        "rs_sync.py", "--wordline", str(wl_path), "--ass", str(ass),
        "--out", str(root / rs_paths.p("output")), "--video", str(slow)])
    assert code == 0 and doc["data"]["pass"] is True, "钩子密度 WARN 不得翻转总判定"
    assert doc["data"]["dramaHooks"]["pass"] is False and doc["data"]["dramaHooks"]["warnOnly"] is True


# ================================================================ §5 端到端合成验收(ffmpeg)

def _synth_footage(path: Path, dur: float) -> Path:
    """竖屏「影视片段」:testsrc2(动态)+ 底部字幕条(drawbox 模拟原声对白条)。"""
    _run_ff("-f", "lavfi", "-i",
            f"testsrc2=size=540x960:rate=15:duration={dur:g},"
            "drawbox=x=0:y=ih-200:w=iw:h=120:color=black@0.7:t=fill",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path))
    return path


@pytest.mark.skipif(not FF, reason="ffmpeg 不可用(合成输入必需)")
def test_e2e_commentary_pipeline(tmp_path):
    """解说型工程端到端:素材 → 登记 → S7 双 Style → 成片 → S9 判据 → L0 版权门禁。

    fixture 已知真值:成片 12s = 甲 3s + 乙 3s + 原创 6s;单部引用 25%、总引用 50%;
    解说轨(wordline 句级,含 1 句原声对白并剔除)≈10s/12s ≥25%。
    """
    root = _mk_project(tmp_path, "e2ecom", capabilities=["sub.pair", "drama.hook"])
    mat = rs_paths.resolve(root, "materials")
    clip_a = _synth_footage(mat / "clip_a.mp4", 3.0)
    clip_b = _synth_footage(mat / "clip_b.mp4", 3.0)
    clip_c = _synth_footage(mat / "clip_c.mp4", 6.0)
    _mk_materials(root, [
        {"file": "clip_a.mp4", "probe": "ok", "durationMs": 3000, "width": 540, "height": 960},
        {"file": "clip_b.mp4", "probe": "ok", "durationMs": 3000, "width": 540, "height": 960},
        {"file": "clip_c.mp4", "probe": "ok", "durationMs": 6000, "width": 540, "height": 960}])
    # S0 侧版权登记(来源/时长/单部标识):单部 3s/12s=25%,总引用 6s/12s=50%
    (rs_ingest.copyright_json(root)).write_text(json.dumps({
        "version": 1, "kind": "commentary",
        "transformNote": "原创锐评+倒叙重组,非剧情替代",
        "aiDisclosure": True,
        "items": [
            {"file": "clip_a.mp4", "source": "《剧甲》第一集", "usage": "quote", "quotedMs": 3000},
            {"file": "clip_b.mp4", "source": "《剧乙》第二集", "usage": "quote", "quotedMs": 3000},
            {"file": "clip_c.mp4", "source": "原创自拍", "usage": "original"},
        ]}, ensure_ascii=False), encoding="utf-8")
    code, doc = _capture_argv(rs_ingest.main, ["rs_ingest.py", "copyright", str(root)])
    assert code == 0 and doc["data"]["stats"]["quoteRatio"] == 0.5, doc

    # IR(成片时间轴 = 三段拼接)+ 解说 wordline(含 1 句原声对白,7s 处)
    rs_paths.project_json(root).write_text(rs_edit.dump_json({
        "version": 1, "slug": "e2ecom", "fps": 30,
        "canvas": {"width": 540, "height": 960},
        "tracks": [{"kind": "video", "name": "main", "clips": [
            {"src": f"{rs_paths.p('materials')}/clip_a.mp4", "startMs": 0,
             "durationMs": 3000, "sourceInMs": 0},
            {"src": f"{rs_paths.p('materials')}/clip_b.mp4", "startMs": 3000,
             "durationMs": 3000, "sourceInMs": 0},
            {"src": f"{rs_paths.p('materials')}/clip_c.mp4", "startMs": 6000,
             "durationMs": 6000, "sourceInMs": 0}]}], "outputs": ["9x16"]}),
        encoding="utf-8")
    wl_path = _mk_wordline(root, [
        {"start": 0.0, "end": 2.8, "text": "这段开场就封神了。"},
        {"start": 2.8, "end": 5.6, "text": "注意墙上的钟是道具。"},
        {"start": 5.6, "end": 6.8, "text": "你居然敢骗我", "voice": "dialogue"},
        {"start": 6.8, "end": 11.0, "text": "导演把答案藏在第一帧。"},
    ], media_ms=12000)

    # S7:双 Style 字幕
    out = root / rs_paths.p("output")
    code, sub = _capture_argv(rs_subtitle.main, [
        "rs_subtitle.py", "--from-wordline", str(wl_path), "--dual-style",
        "--out", str(out), "--no-snap"])
    assert code == 0 and sub["data"]["dualStyleApplied"] is True, sub
    ass = out / "subtitles.ass"
    styles = _style_lines(ass)
    assert rs_subtitle.QUOTE_STYLE_NAME in styles and "Main" in styles
    ass_text = ass.read_text(encoding="utf-8")
    quote_lines = [ln for ln in ass_text.splitlines()
                   if ln.startswith("Dialogue:") and ",Quote," in ln]
    assert quote_lines and all(
        ln.split(",", 9)[9].startswith(rs_subtitle.QUOTE_OPEN) for ln in quote_lines), \
        "对白卡必须以引号上屏(解说全程上屏 + 原声对白引号区分)"

    # S8(等价):合成成片 = 素材拼接(解说音轨口径由 wordline 承载,ASR 对账按既有 e2e 处理)
    final_dir = out / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    final = final_dir / "final_9x16_default.mp4"
    _run_ff("-i", str(clip_a), "-i", str(clip_b), "-i", str(clip_c),
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]",
            "-map", "[out]", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(final))
    assert abs(rs_sync.media_duration_s(final) - 12.0) < 0.5, "成片产出且时长=12s"

    # S9:三对齐 + drama.hook 判据(干净成片 → 全过)
    code, sync = _capture_argv(rs_sync.main, [
        "rs_sync.py", "--wordline", str(wl_path), "--ass", str(ass),
        "--out", str(out), "--video", str(final)])
    assert code == 0 and sync["data"]["pass"] is True, sync
    assert sync["data"]["dramaHooks"]["applied"] is True
    assert sync["data"]["wasteFrames"]["pass"] is True, "干净成片不得误报废帧"

    # 废帧报警:注入 2s 静止段的变体 → S9 硬判报警(非零退出)
    static_v = tmp_path / "static_insert.mp4"
    _run_ff("-i", str(clip_a), "-f", "lavfi", "-i",
            "color=c=gray:size=540x960:rate=15:duration=2", "-i", str(clip_c),
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]",
            "-map", "[out]", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(static_v))
    code2, sync2 = _capture_argv(rs_sync.main, [
        "rs_sync.py", "--wordline", str(wl_path), "--ass", str(ass),
        "--out", str(out), "--video", str(static_v)])
    assert code2 == 4 and sync2["data"]["pass"] is False, "废帧命中必须非零退出"
    assert sync2["data"]["wasteFrames"]["spans"], "注入的 2s 静止段必须被废帧检测报警"

    # L0 版权门禁:占比真值 + L1 项显式标注
    chk = rs_verify.check_copyright(root)
    assert chk["ok"] is True, chk
    assert abs(chk["ratios"]["singleSource"] - 0.25) < 1e-6, "单部 3s/12s=25% 真值"
    assert abs(chk["ratios"]["quoteTotal"] - 0.5) < 1e-6, "总引用 6s/12s=50% 真值"
    assert chk["ratios"]["commentary"] >= 0.25, "解说轨 ≥25%(解说型)"
    assert chk["l1Pending"], "转化性等 L1 项必须显式标注(不假绿)"
    res = rs_verify.collect_l0(root)
    cr = next(c for c in res["checks"] if c["name"].startswith("版权门禁"))
    assert cr["ok"] is True
