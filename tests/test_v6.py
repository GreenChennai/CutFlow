"""CutFlow v6 回归测试:seg 级缓存 / retext 校对回灌 / karaoke 逐字字幕 / 基轨绿幕 / P2 清理。

对应 docs/OPTIMIZATION-v6.md 验收(P0 三项 + P2 清理)。
运行:pytest tests/ -q
"""
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
TOOLS = REPO / "tools"
sys.path.insert(0, str(SCRIPTS))

import rs_align  # noqa: E402
import rs_asr  # noqa: E402
import rs_ir  # noqa: E402
import rs_render  # noqa: E402
import rs_run  # noqa: E402
import rs_subtitle as rsub  # noqa: E402


def _load_fun_asr():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fun_asr_v6", TOOLS / "fun_asr.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _last_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _mk_wl(text: str = "大家好,今天讲蓝屏。", per: int = 200) -> dict:
    """真实锚点的 source 域 wordline:第 k 字时间 [k*per, k*per+per-20],不降级。"""
    ts = [[k * per, k * per + per - 20] for k in range(len(text))]
    seg = {"start": 0.0, "end": len(text) * per / 1000.0, "text": text,
           "timestamp": ts, "conf": 0.95}
    return rs_align.build_wordline([seg], "a.mp4")


# ================================================================ R1 fun_asr 对齐

def test_wordline_coverage_is_span_based():
    """dev-jj2815 实测修正:覆盖率 = 时间跨度/转写声明区间;字间停顿不算未覆盖。

    旧公式(字时长和/末字时间)对真实字级时间戳必然 ~75%(停顿占比),
    对降级均分反而 100% —— 语义颠倒,门禁(≥99%)形同虚设。
    """
    seg = {"start": 0.0, "end": 10.0, "text": "你好世界",
           "timestamp": [[0, 500], [1000, 1500], [5000, 5500], [9000, 9500]]}
    wl = rs_align.build_wordline([seg], "a.mp4")
    assert wl["stats"]["coverage"] == pytest.approx(0.95, abs=0.01)
    wl2 = rs_align.build_wordline([{"start": 0.0, "end": 10.0, "text": "你好世界"}], "a.mp4")
    assert wl2["stats"]["coverage"] == pytest.approx(1.0, abs=0.01)
    assert rs_align.time_coverage([{"ch": " ", "srcStartMs": 0, "srcEndMs": 0}], 100) == 0.0


def test_align_ts_exact_chinese():
    """纯中文:发音字数 == ts 条数 → 一一对应,零损耗。"""
    fa = _load_fun_asr()
    out = fa._align_ts_to_text("桌面运维", [[0, 300], [300, 600], [600, 900], [900, 1200]])
    assert out == [[0, 300], [300, 600], [600, 900], [900, 1200]]


def test_align_ts_punct_backfill():
    """标点不在 timestamp 里 → 零宽继承相邻发音字,绝不让链条断掉。"""
    fa = _load_fun_asr()
    out = fa._align_ts_to_text("你好,世界。", [[0, 200], [200, 400], [400, 600], [600, 800]])
    assert out[0] == [0, 200] and out[1] == [200, 400]
    assert out[2] == [400, 600]                 # 逗号
    assert out[3] == [400, 600] and out[4] == [600, 800]
    assert out[5] == [600, 800]                 # 句号继承前字


def test_align_ts_ascii_word_shares_one_ts():
    """2026-09-11 全片实测:英文词组(AI)整体只占 1 条 ts,不得整段丢弃时间戳。"""
    fa = _load_fun_asr()
    ts = [[100, 200], [200, 300], [300, 400], [400, 500], [500, 600]]
    out = fa._align_ts_to_text("这是AI工具", ts)   # 6 发音字 / 5 条 ts,缺口=AI
    assert out and all(v for v in out)
    assert out[0] == [100, 200] and out[1] == [200, 300]
    assert out[2] == [300, 400]                 # A 吃整条
    assert out[3] == [350, 400]                 # I 在词内均分
    assert out[4] == [400, 500] and out[5] == [500, 600]


def test_align_ts_proportional_fallback():
    """对不上且无词组解释 → 锚点兜底的局部摊派,而不是整体丢弃(旧行为的坑)。"""
    fa = _load_fun_asr()
    out = fa._align_ts_to_text("你好世界", [[0, 1000], [1000, 2000]])
    assert out[0] == [0, 500] and out[1] == [500, 1000]
    assert out[2] == [1000, 1500] and out[3] == [1500, 2000]


def test_align_ts_punct_only_is_none():
    fa = _load_fun_asr()
    assert fa._align_ts_to_text(",。,", [[0, 100]]) is None


# ================================================================ R2 re-exec 安全

def test_reexec_windows_safe_subprocess():
    """Windows 后台/重定向下 os.execv 丢输出句柄(EXIT=0 但零输出)→ 必须走 subprocess。"""
    src = (TOOLS / "fun_asr.py").read_text(encoding="utf-8")
    body = src.split("def maybe_reexec")[1].split("\ndef ")[0]
    code = body.split('"""')[2]                     # 剥掉 docstring(里面也提到 execv)
    assert "subprocess.run" in code and "raise SystemExit(p.returncode)" in code
    assert code.index('os.name == "nt"') < code.index("os.execv("), "nt 分支必须先于 execv"


def test_reexec_flag_short_circuits(monkeypatch):
    fa = _load_fun_asr()
    monkeypatch.setenv(fa.REEXEC_FLAG, "1")
    fa.maybe_reexec()          # 已带标记 → 直接返回,绝不 re-exec/退出测试进程


# ================================================================ R3 retext 校对回灌

def test_retext_noop_zero_drift():
    wl = _mk_wl()
    doc, stats = rs_align.retext_wordline(wl, "大家好,今天讲蓝屏。")
    assert stats == {"replace": 0, "delete": 0, "insert": 0, "editChars": 0}
    assert doc["retext"]["similarity"] == 1.0
    for a, b in zip(wl["chars"], doc["chars"]):
        assert (a["startMs"], a["endMs"]) == (b["startMs"], b["endMs"])
    assert doc["space"] == "source" and doc["degraded"] is False


def test_retext_replace_anchored():
    """替换:新字锚定在旧字的 [start,end] 区间内;equal 区间零漂移。"""
    wl = _mk_wl()
    doc, stats = rs_align.retext_wordline(wl, "大家好,今天讲死机。")
    assert stats["editChars"] == 4
    assert doc["retext"]["similarity"] == pytest.approx(0.8, abs=0.05)
    chs = {c["ch"]: c for c in doc["chars"]}
    assert chs["大"]["startMs"] == 0 and chs["。"]["startMs"] == 1800   # 零漂移
    assert 1380 <= chs["死"]["startMs"] < chs["机"]["endMs"] <= 1801    # 旧区间兜底
    assert chs["死"]["conf"] == rs_align.INSERT_CONF                    # 置信度降级标注


def test_retext_insert_keeps_anchors():
    """中间插入:挤进 [prev.end, next.start] 空隙,**绝不平移既有锚点**。"""
    wl = _mk_wl()
    doc, stats = rs_align.retext_wordline(wl, "大家好,今天讲一讲蓝屏。")
    assert stats["insert"] == 1 and stats["editChars"] == 2
    chs = {c["ch"]: c for c in doc["chars"]}
    assert chs["大"]["startMs"] == 0
    assert chs["蓝"]["startMs"] == 1400 and chs["。"]["startMs"] == 1800  # 锚点不动
    ins = [c for c in doc["chars"] if c["conf"] == rs_align.INSERT_CONF]
    assert [c["ch"] for c in ins] == ["一", "讲"]
    assert ins[0]["startMs"] >= 1380 and ins[-1]["endMs"] <= 1400
    starts = [c["startMs"] for c in doc["chars"]]
    assert starts == sorted(starts), "回灌后必须保持单调"


def test_retext_delete_keeps_neighbors():
    wl = _mk_wl()
    doc, stats = rs_align.retext_wordline(wl, "大家好,今天讲屏。")
    assert stats["delete"] == 1
    assert "蓝" not in [c["ch"] for c in doc["chars"]]
    assert len(doc["chars"]) == 9
    chs = {c["ch"]: c for c in doc["chars"]}
    assert (chs["屏"]["startMs"], chs["屏"]["endMs"]) == (1600, 1780)   # 邻字锚点原样


def test_retext_similarity_guard():
    """校对稿与原稿相似度过低 → 拒绝回灌(疑似拿错文件)。"""
    wl = _mk_wl()
    with pytest.raises(ValueError):
        rs_align.retext_wordline(wl, "这段话和原稿完全没有任何关系对吧")


def test_retext_rejects_empty():
    with pytest.raises(ValueError):
        rs_align.retext_wordline({"chars": []}, "任意文本")


def test_resplit_sentences_after_retext():
    """校对改变标点 → 旧句 span 作废,按强标点重切(DP 断句的前置)。"""
    wl = _mk_wl()
    doc, _ = rs_align.retext_wordline(wl, "大家好。今天讲蓝屏。")
    sents = doc["sentences"]
    assert len(sents) == 2
    assert sents[0]["text"] == "大家好。" and sents[1]["text"] == "今天讲蓝屏。"


def test_resplit_orphan_punct_joins_prev():
    """dev-jj2815 实测:零宽继承时间的孤立标点在 gap 切句后不得单独成句。

    source 域标点继承后字时间 → 与前字 gap 巨大 → gap 切句,标点被孤立;
    单标点句会让下游 DP 产卡缺 startMs(event 以 0.0s 污染排序并合并首卡)。
    """
    times = [(0, 180), (300, 480), (12000, 12000), (12200, 12380), (12600, 12780),
             (12780, 12780)]
    chars = [{"ch": ch, "startMs": s, "endMs": e}
             for ch, (s, e) in zip("ab?cd。", times)]
    sents = rs_align._resplit_sentences(chars)
    assert len(sents) == 2
    assert sents[0]["text"] == "ab?"          # ? 并入前句
    assert sents[0]["punc"] == "?"
    assert sents[1]["text"] == "cd。"


def test_events_skip_punct_only_sentence():
    """防御:纯标点句直接跳过,绝不产卡(DP 对它缺 startMs → 0.0s 幽灵卡)。"""
    wl = {"chars": [{"ch": "?", "startMs": 1000, "endMs": 1000},
                    {"ch": "你", "startMs": 2000, "endMs": 2200}],
          "sentences": [{"id": 0, "span": [0, 1], "text": "?"},
                        {"id": 1, "span": [1, 2], "text": "你"}],
          "degraded": False}
    events, meta = rsub.events_from_wordline(wl, 12)
    assert [e["text"] for e in events] == ["你"]
    assert not any(e.get("degraded") for e in events)


def test_retext_cli_flow(tmp_path, monkeypatch, capsys):
    wl = _mk_wl()
    wlp = tmp_path / "05_ir" / "wordline.json"
    wlp.parent.mkdir(parents=True)
    wlp.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    proof = tmp_path / "proof.txt"
    proof.write_text("大家好,今天讲死机。", encoding="utf-8")

    # dry-run:只报告,不写盘
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "retext", str(wlp),
                                      "--text", str(proof), "--dry-run"])
    assert rs_align.main() == 0
    assert _last_json(capsys)["code"] == "RETEXT_DRYRUN"
    assert json.loads(wlp.read_text(encoding="utf-8"))["chars"][7]["ch"] == "蓝"

    # 正式回灌到 --out
    out2 = tmp_path / "05_ir" / "wl2.json"
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "retext", str(wlp),
                                      "--text", str(proof), "--out", str(out2)])
    assert rs_align.main() == 0
    assert _last_json(capsys)["code"] == "RETEXT_OK"
    doc = json.loads(out2.read_text(encoding="utf-8"))
    assert doc["space"] == "source" and doc["retext"]["editChars"] == 4

    # final 域守卫:粗剪后的 wordline 不得直接回灌
    wlf = dict(wl, space="final")
    p3 = tmp_path / "wl_final.json"
    p3.write_text(json.dumps(wlf, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "retext", str(p3), "--text", str(proof)])
    assert rs_align.main() == 2
    assert _last_json(capsys)["code"] == "NOT_SOURCE_SPACE"


# ================================================================ R4 karaoke 逐字字幕

def _kar_event() -> dict:
    return {"text": "你好", "start": 0.0, "end": 1.0,
            "chars": [{"ch": "你", "startMs": 0, "endMs": 400},
                      {"ch": "好", "startMs": 400, "endMs": 1000}]}


def test_kar_text_kf_tags():
    """每字一个 \\kf,厘秒 = 字级时间戳;末字吃到卡尾,总时长吻合。"""
    r = rsub._kar_text(_kar_event())
    assert r == "{\\kf40}你{\\kf60}好"          # 40+60=100cs=1.0s


def test_kar_text_fallback_without_chars():
    ev = {"text": "你好", "start": 0.0, "end": 1.0}
    assert rsub._kar_text(ev) == "你好"


def test_attach_karaoke_chars_bisect_by_card_start():
    """字按「卡起点分界」归属事件:卡间距压缩后尾部字符不丢。"""
    chars = [{"ch": chr(65 + i), "startMs": i * 200, "endMs": i * 200 + 180}
             for i in range(10)]
    events = [{"start": 0.0, "end": 1.0, "text": "ABCDE"},
              {"start": 1.0, "end": 2.0, "text": "FGHIJ"}]
    n = rsub.attach_karaoke_chars(events, {"chars": chars})
    assert n == 10
    assert [c["ch"] for c in events[0]["chars"]] == ["A", "B", "C", "D", "E"]
    assert [c["ch"] for c in events[1]["chars"]] == ["F", "G", "H", "I", "J"]


def test_attach_karaoke_empty_wordline_clears():
    events = [{"start": 0.0, "end": 1.0, "text": "x"}]
    assert rsub.attach_karaoke_chars(events, {"chars": []}) == 0
    assert events[0]["chars"] == []


def test_karaoke_ready_gate():
    ok, why = rsub._karaoke_ready({"degraded": True, "chars": [{"ch": "a"}]})
    assert not ok and "降级" in why
    ok, why = rsub._karaoke_ready({"degraded": False, "chars": []})
    assert not ok and "chars" in why
    ok, _ = rsub._karaoke_ready({"degraded": False, "chars": [{"ch": "a"}]})
    assert ok


def test_write_ass_karaoke_colors(tmp_path):
    """卡拉OK:已唱=暖黄(Primary),未唱=白(Secondary);普通字幕保持原配色。"""
    p = tmp_path / "kar.ass"
    rsub.write_ass([_kar_event()], p, "subtitle-white", "9x16", "1080x1920", karaoke=True)
    t = p.read_text(encoding="utf-8")
    assert "&H0000E5FF" in t and "&H00FFFFFF" in t and "{\\kf40}你" in t

    p2 = tmp_path / "plain.ass"
    rsub.write_ass([_kar_event()], p2, "subtitle-white", "9x16", "1080x1920")
    t2 = p2.read_text(encoding="utf-8")
    assert "&H000000FF" in t2 and "\\kf" not in t2


def test_karaoke_cli_wordline_end_to_end(tmp_path, monkeypatch, capsys):
    """正常 wordline + --karaoke → ASS 含 \\kf 染色(小闭环)。"""
    wlp = tmp_path / "wl.json"
    wlp.write_text(json.dumps(_mk_wl(), ensure_ascii=False), encoding="utf-8")
    outd = tmp_path / "06_output"
    monkeypatch.setattr(sys, "argv", ["rs_subtitle.py", "--from-wordline", str(wlp),
                                      "--style", "subtitle-white", "--ratio", "9x16",
                                      "--out", str(outd), "--karaoke"])
    assert rsub.main() == 0
    assert _last_json(capsys)["ok"] is True
    ass = (outd / "subtitles.ass").read_text(encoding="utf-8")
    assert "\\kf" in ass and "&H0000E5FF" in ass


def test_karaoke_cli_requires_wordline(tmp_path, monkeypatch, capsys):
    tr = tmp_path / "tr.json"
    tr.write_text(json.dumps({"segments": [{"start": 0.0, "end": 2.0, "text": "你好世界"}]}),
                  encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_subtitle.py", "--from-transcript", str(tr),
                                      "--out", str(tmp_path / "o"), "--karaoke"])
    assert rsub.main() == 2
    assert _last_json(capsys)["code"] == "KARAOKE_NEEDS_WORDLINE"


def test_karaoke_cli_degraded_gates(tmp_path, monkeypatch, capsys):
    """降级 wordline:默认拒绝;--allow-degraded 才降级为普通字幕(不静默回禁区)。"""
    wl = rs_align.build_wordline([{"start": 0.0, "end": 2.0, "text": "大家好,今天讲蓝屏。"}], "a.mp4")
    assert wl["degraded"]
    wlp = tmp_path / "wl.json"
    wlp.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    outd = tmp_path / "06_output"

    monkeypatch.setattr(sys, "argv", ["rs_subtitle.py", "--from-wordline", str(wlp),
                                      "--out", str(outd), "--karaoke"])
    assert rsub.main() == 2
    assert _last_json(capsys)["code"] == "KARAOKE_NEEDS_WORD_TS"

    monkeypatch.setattr(sys, "argv", ["rs_subtitle.py", "--from-wordline", str(wlp),
                                      "--out", str(outd), "--karaoke", "--allow-degraded"])
    assert rsub.main() == 0
    data = _last_json(capsys)["data"]
    assert any("卡拉OK 已降级" in r for r in data["degradeReasons"])
    assert "\\kf" not in (outd / "subtitles.ass").read_text(encoding="utf-8")


# ================================================================ R5 rs_render 缓存与绿幕

def test_script_hash_stable():
    h1, h2 = rs_render.script_hash(), rs_render.script_hash()
    assert h1 == h2 and len(h1) == 16


def test_media_fingerprint(tmp_path):
    p = tmp_path / "a.mp4"
    p.write_bytes(b"A" * (1 << 20) + b"B" * (1 << 20))
    f1 = rs_render.media_fingerprint(p)
    assert f1 == rs_render.media_fingerprint(p)
    raw = p.read_bytes()
    p.write_bytes(raw[:-1] + b"C")                      # 尾部 1 字节变化 → 指纹变
    assert rs_render.media_fingerprint(p) != f1
    p.write_bytes(raw)
    os.utime(p, (1234567890, 1234567890))               # mtime 变化 → 指纹变
    assert rs_render.media_fingerprint(p) != f1


def test_seg_key_excludes_index_and_tracks_params():
    doc = {"fps": 30}
    clip = {"src": "a.mp4", "durationMs": 2000, "sourceInMs": 100}
    k1 = rs_render.seg_key(clip, doc, 1080, 1920, "fp")
    assert k1 == rs_render.seg_key(clip, doc, 1080, 1920, "fp")
    assert k1 == rs_render.seg_key(dict(clip, startMs=2000), doc, 1080, 1920, "fp"), \
        "键不含时间轴位置:重排 clip 仍命中"
    assert rs_render.seg_key(dict(clip, src="b.mp4"), doc, 1080, 1920, "fp") != k1
    assert rs_render.seg_key(dict(clip, chroma={"color": "0x00FF00"}),
                             doc, 1080, 1920, "fp") != k1
    assert rs_render.seg_key(clip, {"fps": 60}, 1080, 1920, "fp") != k1
    assert rs_render.seg_key(clip, doc, 1920, 1080, "fp") != k1


def test_step_key_chain():
    k = rs_render.step_key("concat", "u1", {"x": 1})
    assert k == rs_render.step_key("concat", "u1", {"x": 1})
    assert rs_render.step_key("concat", "u2", {"x": 1}) != k
    assert rs_render.step_key("mix", "u1", {"x": 1}) != k
    assert rs_render.step_key("concat", "u1", {"x": 2}) != k


def test_chroma_hex_explicit_and_auto(tmp_path, monkeypatch):
    assert rs_render.chroma_hex({"color": "0x2AA81E"}, tmp_path, {}) == "0x2AA81E"
    assert rs_render.chroma_hex({"color": "green"}, tmp_path, {}) == "0x00FF00"
    assert rs_render.chroma_hex({"color": "blue"}, tmp_path, {}) == "0x0000FF"
    monkeypatch.setattr(rs_render, "sample_chroma", lambda src, cfg: "0x123456")
    assert rs_render.chroma_hex({"color": "auto"}, tmp_path, {}) == "0x123456"
    assert rs_render.chroma_hex({}, tmp_path, {}) == "0x123456"      # 缺省 = auto


def test_crop_pct_chain_and_cover_crop():
    ch = rs_render._crop_pct_chain({"cropTopPct": 0.09, "cropBottomPct": 0.10})
    assert ch == ["crop=iw:ih*0.9100:0:ih*0.0900", "crop=iw:ih*0.9000:0:0"]
    assert rs_render._crop_pct_chain({}) == []
    cc = rs_render.cover_crop(1920, 1080, 1080, 1920, 0.3)
    assert "scale=1080:1920:force_original_aspect_ratio=increase" in cc
    assert "crop=1080:1920:(iw-1080)/2:(ih-1920)*0.3" in cc


def test_render_dryrun_reports_cache_state(tmp_path):
    """--explain 干跑:只报命中,不碰 ffmpeg、不写任何缓存文件。"""
    proj = tmp_path / "proj"
    (proj / "05_ir").mkdir(parents=True)
    (proj / "03_assets").mkdir()
    (proj / "03_assets" / "a.png").write_bytes(b"png")
    ir = {"version": 1, "slug": "t", "fps": 30, "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"kind": "video", "clips": [
              {"src": "03_assets/a.png", "startMs": 0, "durationMs": 1000}]}],
          "subtitle": {}}
    ir_path = proj / "05_ir" / "project.json"
    ir_path.write_text(json.dumps(ir), encoding="utf-8")
    res = rs_render.render(ir, ir_path, "9x16", "draft", dry_run=True)
    assert res["dryRun"] is True
    assert res["segTotal"] == 1 and res["segHits"] == 0
    assert set(res["steps"]) == {"concat", "compose", "mix", "subtitle", "encode"}
    build = proj / "06_output" / "_build" / "9x16"
    assert not (build / "segcache").exists() and not (build / "step_keys.json").exists()


def test_prune_seg_cache_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(rs_render, "SEG_CACHE_KEEP", 3)
    d = tmp_path / "segcache"
    d.mkdir()
    for i in range(5):
        p = d / f"{i}.mp4"
        p.write_bytes(b"x")
        os.utime(p, (1_000_000 + i * 100,) * 2)
    rs_render.prune_seg_cache(d)
    assert sorted(p.name for p in d.glob("*.mp4")) == ["2.mp4", "3.mp4", "4.mp4"]


def test_s1_wordline_is_registry_source_of_truth():
    """S1 必须产出 05_ir/wordline.json —— 全片时间唯一真相源(ADR-0011)。"""
    s1 = next(s for s in rs_run.spec() if s["id"] == "S1")
    assert "wordline.json" in " ".join(s1["cmd"])


# ================================================================ R6 rs_ir 绿幕校验

def test_ir_chroma_bg_validation_unit(tmp_path):
    errs: list[str] = []
    good = {"chroma": {"color": "0x2AA81E", "similarity": 0.24, "blend": 0.12,
                       "cropTopPct": 0.09, "cropBottomPct": 0.10},
            "background": {"type": "gradient", "from": "0x0F2027", "to": "0x2C5364"}}
    rs_ir._validate_chroma_bg("t", good, tmp_path, errs)
    assert errs == []

    bad_cases = [
        ({"chroma": {"color": "red"}}, "chroma.color"),
        ({"chroma": {"similarity": 1.5}}, "chroma.similarity"),
        ({"chroma": {"blend": -0.1}}, "chroma.blend"),
        ({"chroma": {"cropTopPct": 0.95}}, "cropTopPct"),
        ({"background": {"type": "holo"}}, "background.type"),
        ({"background": {"type": "color"}}, "必须与 chroma"),
    ]
    for clip, frag in bad_cases:
        errs = []
        rs_ir._validate_chroma_bg("t", clip, tmp_path, errs)
        assert any(frag in e for e in errs), (clip, errs)


def test_ir_bg_image_src_resolution(tmp_path):
    (tmp_path / "bg.png").write_bytes(b"x")
    errs: list[str] = []
    rs_ir._validate_chroma_bg("t", {"chroma": {"color": "auto"},
                                    "background": {"type": "image", "src": "bg.png"}},
                              tmp_path, errs)
    assert errs == []


def test_ir_validate_end_to_end_with_chroma(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    doc = {"version": 1, "slug": "t", "fps": 30, "canvas": {"width": 1080, "height": 1920},
           "tracks": [{"kind": "video", "clips": [{
               "src": "a.mp4", "startMs": 0, "durationMs": 2000,
               "chroma": {"color": "auto", "similarity": 0.24, "blend": 0.12,
                          "cropTopPct": 0.09, "cropBottomPct": 0.10},
               "background": {"type": "gradient", "from": "0x0F2027", "to": "0x2C5364"}}]}],
           "subtitle": {}}
    assert rs_ir.validate(doc, tmp_path) == []
    bad = json.loads(json.dumps(doc))
    bad["tracks"][0]["clips"][0]["background"] = {"type": "image", "src": "ghost.png"}
    assert any("background.src" in e for e in rs_ir.validate(bad, tmp_path))


# ================================================================ R7 rs_asr 兼容薄壳

def test_rs_asr_is_thin_wrapper():
    """rs_asr.py 必须委托 tools/fun_asr.py,且输出标注 deprecated。"""
    assert rs_asr.RUNNER == REPO / "tools" / "fun_asr.py"
    assert rs_asr.RUNNER.is_file()
    src = (SCRIPTS / "rs_asr.py").read_text(encoding="utf-8")
    assert '"deprecated": True' in src and "charTimestamps" in src


def test_rs_asr_no_media(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rs_asr.py", str(tmp_path / "no.mp4"),
                                      "--out", str(tmp_path / "o")])
    assert rs_asr.main() == 2
    assert _last_json(capsys)["code"] == "NO_MEDIA"


def test_rs_asr_no_runner(tmp_path, monkeypatch, capsys):
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    monkeypatch.setattr(rs_asr, "RUNNER", tmp_path / "ghost.py")
    monkeypatch.setattr(sys, "argv", ["rs_asr.py", str(media), "--out", str(tmp_path / "o")])
    assert rs_asr.main() == 3
    assert _last_json(capsys)["code"] == "NO_ASR_RUNNER"


# ================================================================ R8 问题#5 回归:段命令输出侧 -t

def test_seg_cmd_has_output_t_clamp(tmp_path, monkeypatch):
    """ffmpeg git-master(2026-07-30)回归:filter_complex 含 overlay 且视频输入
    -ss 为 0/缺省时,输入 -t 被按"帧数 = t × time_base_den"解释(tb=1/600 膨胀 20×,
    tb=1/15360 膨胀 512×;单输入 -vf 或 -ss>0 不触发)。段命令必须带输出侧 -t 钳制。"""
    src = tmp_path / "fake.mp4"
    src.write_bytes(b"0" * 64)
    doc = {"fps": 30, "tracks": [{"kind": "video", "clips": [{
        "src": str(src), "durationMs": 4260, "sourceInMs": 0,
        "chroma": {"color": "0x2AA81E", "similarity": 0.24, "blend": 0.12},
        "background": {"type": "gradient", "from": "0x0F2027", "to": "0x2C5364"},
    }]}]}
    monkeypatch.setattr(rs_render, "probe_clip", lambda clip, base_dir, cfg: {
        "type": "video", "path": str(src),
        "width": 1920, "height": 1080, "has_audio": True})

    class _P:
        returncode, stderr = 0, ""

    captured: dict = {}

    def fake_run(cmd, timeout=None):
        captured["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"x")  # 供 tmp.replace(cached)
        return _P()

    monkeypatch.setattr(rs_render, "run", fake_run)
    rs_render.step_segment(doc, "9x16", tmp_path / "build", tmp_path, {}, [])
    cmd = captured["cmd"]
    # 输入侧应有 2 个 -t(视频 + gradient 背景),输出侧 1 个
    assert cmd.count("-t") == 3
    idx_out = max(i for i, x in enumerate(cmd) if x == "-t")
    assert cmd[idx_out + 1] == "4.260"          # = take_s(durationMs/speed/1000)
    # 输出侧 -t 必须位于 filter_complex/map 之后、输出文件之前
    assert idx_out > cmd.index("-filter_complex")
    assert idx_out > cmd.index("-map")
    assert idx_out == len(cmd) - 13             # -t + 值 + 10 个编码参数 + 输出路径


# ================================================================ R9 问题#7/#8 回归:标点领头卡

def test_segmentation_never_cuts_before_punct():
    """JJAV2815 实测:DP 曾把「吗?」的 ? 切给下一卡(「?关于店群运营」)。
    禁止边界必须覆盖"标点前"(含校对稿混入的半角),候选只保留"标点后"。"""
    import segmentation as seg
    text = "拆分收入吗?关于店群运营"
    forb = seg.forbidden_positions(text)
    k = text.index("?")
    assert k in forb                      # ? 之前禁止切
    assert (k + 1) not in forb            # ? 之后允许切
    cand = seg.candidate_positions(text, {})
    assert (k + 1) in cand                # 标点后是候选
    assert k not in cand                  # 标点前不再是候选
    half = seg.forbidden_positions("货物,快递")
    assert half == seg.forbidden_positions("货物,快递")  # 半角逗号同规则


def test_karaoke_attach_punct_follows_prev_card():
    """retext 给插入标点分了 gap 中段时间(?[4.41,4.59]),按中心 bisect 会把
    尾标点划给下一卡;attach 必须让标点跟随前字所在卡。"""
    wl = {"chars": [
        {"ch": "吗", "startMs": 1800, "endMs": 2000},
        {"ch": "?", "startMs": 2200, "endMs": 2400},   # gap 中段(复现数据)
        {"ch": "关", "startMs": 2400, "endMs": 2600},
        {"ch": "于", "startMs": 2600, "endMs": 2800},
    ]}
    events = [
        {"start": 1.0, "end": 2.0, "text": "吗"},
        {"start": 2.2, "end": 2.8, "text": "关于"},
    ]
    n = rsub.attach_karaoke_chars(events, wl)
    assert n == 4
    assert [c["ch"] for c in events[0]["chars"]] == ["吗", "?"]
    assert [c["ch"] for c in events[1]["chars"]] == ["关", "于"]


# ================================================================ R9 问题#7/#8 回归:标点领头卡

def test_segmentation_never_cuts_before_punct():
    """JJAV2815 实测:DP 曾把「吗?」的 ? 切给下一卡(「?关于店群运营」)。
    禁止边界必须覆盖"标点前"(含校对稿混入的半角),候选只保留"标点后"。"""
    import segmentation as seg
    text = "拆分收入吗?关于店群运营"
    forb = seg.forbidden_positions(text)
    k = text.index("?")
    assert k in forb                      # ? 之前禁止切
    assert (k + 1) not in forb            # ? 之后允许切
    cand = seg.candidate_positions(text, {})
    assert (k + 1) in cand                # 标点后是候选
    assert k not in cand                  # 标点前不再是候选
    half = seg.forbidden_positions("货物,快递")
    assert half == seg.forbidden_positions("货物,快递")  # 半角逗号同规则


def test_karaoke_attach_punct_follows_prev_card():
    """retext 给插入标点分了 gap 中段时间(?[4.41,4.59]),按中心 bisect 会把
    尾标点划给下一卡;attach 必须让标点跟随前字所在卡。"""
    wl = {"chars": [
        {"ch": "吗", "startMs": 1800, "endMs": 2000},
        {"ch": "?", "startMs": 2200, "endMs": 2400},   # gap 中段(复现数据)
        {"ch": "关", "startMs": 2400, "endMs": 2600},
        {"ch": "于", "startMs": 2600, "endMs": 2800},
    ]}
    events = [
        {"start": 1.0, "end": 2.0, "text": "吗"},
        {"start": 2.2, "end": 2.8, "text": "关于"},
    ]
    n = rsub.attach_karaoke_chars(events, wl)
    assert n == 4
    assert [c["ch"] for c in events[0]["chars"]] == ["吗", "?"]
    assert [c["ch"] for c in events[1]["chars"]] == ["关", "于"]


# ================================================================ R10 问题#9 回归:rs_sync 解析卡拉OK ASS

def test_rs_sync_parse_ass_strips_karaoke_tags(tmp_path):
    """JJAV2815 实测:rs_sync 曾不剥 {\kf..} override 标签,84/84 unmatched
    → SYNC_FAIL。解析必须还原纯文本后再与 Wordline 匹配。"""
    import rs_sync
    ass = tmp_path / "subtitles.ass"
    ass.write_text(
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.97,0:00:02.12,Main,,0,0,0,,"
        "{\kf28}店{\kf14}群{\kf14}运{\kf20}营{\kf1}?\n",
        encoding="utf-8")
    ev = rs_sync.parse_ass(ass)
    assert len(ev) == 1
    assert ev[0]["text"] == "店群运营?"
    assert abs(ev[0]["start"] - 0.97) < 1e-6 and abs(ev[0]["end"] - 2.12) < 1e-6


def test_rs_sync_karaoke_ass_sync_ok(tmp_path, monkeypatch, capsys):
    """卡拉OK ASS + Wordline 端到端 → SYNC_OK(修复前 84/84 unmatched)。"""
    import rs_sync
    wl = {"chars": [{"ch": ch, "i": i, "startMs": 990 + i * 280, "endMs": 1270 + i * 280}
                    for i, ch in enumerate("店群运营")]}
    wlp = tmp_path / "wordline.json"
    wlp.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    ass = tmp_path / "subtitles.ass"
    ass.write_text(
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.97,0:00:02.12,Main,,0,0,0,,"
        "{\kf28}店{\kf14}群{\kf14}运{\kf20}营\n",
        encoding="utf-8")
    outd = tmp_path / "06_output"
    monkeypatch.setattr(sys, "argv", ["rs_sync.py", "--wordline", str(wlp),
                                      "--ass", str(ass), "--out", str(outd)])
    assert rs_sync.main() == 0
    res = _last_json(capsys)
    assert res["code"] == "SYNC_OK" and res["data"]["pass"] is True
    assert res["data"]["chars"] == 1 and res["data"]["medianMs"] == 0.0  # chars=匹配卡数
    rows = json.loads((outd / "sync_rows.json").read_text(encoding="utf-8"))["rows"]
    assert rows[0]["chars"] == 4 and rows[0]["offsetMs"] == 0.0          # 单卡 4 字、零偏移


# ================================================================ R11 问题#10/#11 回归:必并死区 + 卡拉OK字形预算

def test_merge_short_aligns_min_dur_line():
    """JJAV2815 实测「成为电商」0.81s:必并线曾是 0.8s,与 DUR_MIN=0.83s 之间
    有死区——既不并又延不满(下一卡 2 帧间隙就到)。必并线必须 = MIN_DUR_S。"""
    events = [
        {"start": 0.0, "end": 0.81, "text": "成为电商"},
        {"start": 0.88, "end": 2.5, "text": "行业的经营常规"},
    ]
    out, merged = rsub._merge_short(events, 12)
    assert merged == 1 and len(out) == 1
    assert out[0]["text"] == "成为电商行业的经营常规"
    assert abs(out[0]["end"] - 2.5) < 1e-9      # 说出时间不动,只并卡


def test_merge_short_second_pass_absorbs_next():
    """上一卡预算放不下(9+4=13>12)时,第二遍把短卡吞给下一卡(4+7=11)。"""
    events = [
        {"start": 0.0, "end": 2.0, "text": "因此的一店一照已经"},
        {"start": 2.07, "end": 2.88, "text": "成为电商"},
        {"start": 2.95, "end": 4.5, "text": "行业的经营常规"},
    ]
    out, merged = rsub._merge_short(events, 12)
    assert merged == 1 and len(out) == 2
    assert out[1]["text"] == "成为电商行业的经营常规"
    assert abs(out[1]["start"] - 2.07) < 1e-9   # 起点取短卡(对齐精度)


def test_karaoke_display_glyph_budget():
    """卡拉OK:挂字必须在必并/合规校验之前,以「显示字形」(含标点)为预算——
    否则 _clean_card 剥掉的标点经 chars 带回,ASS 冒出 13-14 字卡。"""
    text = "拆分收入这个动作带有主观能动性,确实操纵了含义"
    chars = []
    t = 0
    for i, ch in enumerate(text):
        chars.append({"ch": ch, "i": i, "startMs": t, "endMs": t + 180})
        t += 180 + (120 if ch == "," else 0)
    wl = {"source": "test", "chars": chars, "degraded": False,
          "sentences": [{"id": 0, "span": [0, len(chars)]}]}
    events, meta = rsub.events_from_wordline(wl, 12, karaoke=True)
    assert meta["karaokeAttached"] > 0
    assert events, "必须有事件"
    for e in events:
        assert len(e["text"].replace(" ", "")) <= 12, f"超预算:{e['text']}"
        if e.get("chars"):
            assert e["text"] == "".join(c["ch"] for c in e["chars"])  # 文本=显示字形
