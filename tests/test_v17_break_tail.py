"""副文档 07 专项回归:断句与片尾截断修复(P26–P30)。

来源:《07-副文档-专项:断句与片尾截断修复》+《20260920-纯口播字幕断句与片尾截断修复经验》。

P26 wordline 时长以 ffprobe 实测为准 + 钳制检测 + rs_cut keep 末段保底 + rs_sync 实测基准
P27 rs_cut --apply 自动同步 wordline 时长账(src−removed==final)+ rs_ir S3 门禁
P28 remap 本体丢弃幽灵字符(重建 span、重排 i)+ rs_subtitle 幽灵卡保险 + prune-ghost
P29 二次 remap 拦截 + refresh-durations(只改时长不动字符时间)
P30 末卡回吸 + override 余字自动重组 + 用户断句方案预检 + 文件引文/动宾禁切 + 回归固化

运行:pytest tests/test_v17_break_tail.py -q
"""
from __future__ import annotations

import io
import json
import contextlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_align  # noqa: E402
import rs_cut  # noqa: E402
import rs_ir  # noqa: E402
import rs_common  # noqa: E402
import rs_sync  # noqa: E402
import rs_subtitle as rsub  # noqa: E402
import segmentation as sg  # noqa: E402


def _capture(fn, *args, **kw):
    """跑返回 (exit_code, 最后一个 JSON 输出)——rs_* 的 emit 协议。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    doc = json.loads(lines[-1]) if lines else {}
    return code, doc


def _seg(text: str, start_ms: int, per: int = 200) -> dict:
    """带真实字级时间戳的转写段(等价 ASR 产物)。"""
    ts = [[start_ms + i * per, start_ms + i * per + per - 20] for i in range(len(text))]
    return {"start": start_ms / 1000.0, "end": (start_ms + len(text) * per) / 1000.0,
            "text": text, "timestamp": ts, "conf": 0.95}


def _wordline(text: str, start_ms: int = 0, per: int = 200,
              source: str = "01_materials/a.mp4") -> dict:
    return rs_align.build_wordline([_seg(text, start_ms, per)], source)


def _ffmpeg() -> str | None:
    try:
        cand = rs_common.ffmpeg_bin()
    except SystemExit:
        cand = "ffmpeg"
    return shutil.which(cand) or (cand if Path(cand).is_file() else None)


FFMPEG = _ffmpeg()


# ================================================================ P26-1 ffprobe 实测

def test_p26_1_build_uses_measured_duration_not_char_tail():
    """P26-1:给了 ffprobe 实测值,srcDurationMs = 实测,不再取 max(末字 endMs, seg_end)。"""
    # 转写声明 6s,末字 endMs=5980;实测媒体 6.8s(末字后还有 0.8s 底噪)
    wl = rs_align.build_wordline([_seg("大家好今天讲片尾", 0)], "a.mp4",
                                 media_duration_ms=6800)
    assert wl["srcDurationMs"] == 6800, wl["srcDurationMs"]
    assert wl["durationProvenance"] == "ffprobe"
    # 不给实测 → 旧行为兜底(记录值),但来源显式标注
    wl2 = rs_align.build_wordline([_seg("大家好今天讲片尾", 0)], "a.mp4")
    assert wl2["durationProvenance"] == "asr-chain"
    assert wl2["srcDurationMs"] == 1600   # 8 字 × 200ms


def test_p26_1_probe_media_duration_ms_with_real_media(tmp_path):
    """P26-1:probe_media_duration_ms 对真实合成媒体返回 ffprobe 实测(无 ffmpeg 跳过)。"""
    if not FFMPEG:
        pytest.skip("本机没有 ffmpeg")
    wav = tmp_path / "a.wav"
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", "2", str(wav)], check=True)
    ms = rs_align.probe_media_duration_ms(wav, {"ffmpeg_dir": "E:\\Tools\\ffmpeg\\bin"})
    assert ms is not None and 1900 <= ms <= 2200, ms
    # 探测失败(坏文件)返回 None,绝不抛
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio")
    assert rs_align.probe_media_duration_ms(bad) is None


# ================================================================ P26-2 钳制检测

def test_p26_2_end_clamp_suspect_flagged_and_numbers_untouched():
    """P26-2:末字 endMs == 记录总时长(<1 帧)→ 标疑似钳制,只告警不改数(经验贴场景)。"""
    segs = [_seg("务必高度重视", 0, per=200)]        # 末字 endMs = 980 ≈ 1000(记录总时长)
    wl = rs_align.build_wordline(segs, "a.mp4")      # recorded = 1000 = seg_end
    # 末字 endMs 与记录总时长差 <1 帧(33ms @30fps)→ 命中
    wl["chars"][-1]["srcEndMs"] = 1000
    wl["chars"][-1]["endMs"] = 1000
    clamp = rs_align.detect_end_clamp(wl["chars"], 1000, fps=30)
    assert clamp and clamp["suspect"] is True
    assert "疑似按错误长度喂给 ASR" in clamp["hint"]
    assert clamp["lastCharEndMs"] == 1000 and clamp["recordedDurationMs"] == 1000
    # 末字与总时长差 1s → 不误报
    assert rs_align.detect_end_clamp(wl["chars"], 2000, fps=30) is None


def test_p26_2_build_attaches_clamp_suspect():
    """P26-2:build_wordline 把钳制疑点写进 endClampSuspect(供报告/验收消费)。"""
    segs = [_seg("说完了", 0, per=100)]              # recorded = 300 = 末字 endMs(被钳制形态)
    wl = rs_align.build_wordline(segs, "a.mp4")
    assert wl.get("endClampSuspect"), wl.get("endClampSuspect")
    # 实测比记录长 780ms(NCLM1605 场景)→ 数字不被改写,仍标注疑点
    wl2 = rs_align.build_wordline(segs, "a.mp4", media_duration_ms=1080)
    assert wl2["srcDurationMs"] == 1080 and wl2.get("endClampSuspect")


# ================================================================ P26-3 keep 末段终点保底

def test_p26_3_tail_keep_end_measured_is_truth():
    """P26-3:有实测 → keep 末段终点 = 实测时长(物理上限);记录值被钳制时实测救回尾巴。"""
    chars = [{"ch": "视", "endMs": 372645}]
    assert rs_cut.tail_keep_end_ms(chars, 372645, 373434) == 373434
    # 无实测 → 记录值被字尾钳制(差 ≤40ms 签名)时外推一个尾余量
    assert rs_cut.tail_keep_end_ms(chars, 372645, None) == 372645 + rs_cut.TAIL_RESERVE_MS
    # 无实测、记录值与末字差距大(尾后确有静音)→ 信任记录值,不外推越界
    assert rs_cut.tail_keep_end_ms([{"ch": "好", "endMs": 3000}], 6000, None) == 6000
    # 记录值本就是 ffprobe 产物(provenance=ffprobe)→ 信任,不外推
    assert rs_cut.tail_keep_end_ms(chars, 372645, None, recorded_is_measured=True) == 372645
    assert 500 <= rs_cut.TAIL_RESERVE_MS <= 800, "口播尾余量必须落在 0.5–0.8s"


def test_p26_3_build_cutlist_extends_last_keep_to_measured():
    """P26-3:build_cutlist 用实测时长兜底 keep 末段,srcTotalMs 同步为有效源时长。"""
    wl = _wordline("大家好今天讲片尾截断", 0)          # 末字 endMs=4180,记录总长 4400
    wl["srcDurationMs"] = 4400
    cl = rs_cut.build_cutlist(wl, [], {"measuredMs": 5189})   # 实测 5.189s(末字后 0.78s 底噪)
    assert cl["srcTotalMs"] == 5189
    assert cl["keep"][-1][1] == 5189
    assert cl["tail"]["measuredMs"] == 5189 and cl["tail"]["recordedTotalMs"] == 4400
    # finalize(apply 路径)后 keep 仍须完整覆盖 [0, srcTotalMs]
    rs_cut.finalize_cutlist(cl)
    assert cl["keep"][-1][1] == cl["srcTotalMs"] == 5189


# ================================================================ P26-4 rs_sync 实测基准

def test_p26_4_sync_expected_duration_prefers_probe(tmp_path, monkeypatch):
    """P26-4:成片总时长断言期望值 = ffprobe 实测源媒体 − removedMs;探测失败退回记录值。"""
    media = tmp_path / "a.mp4"
    media.write_bytes(b"fake")                     # 存在性检查用;时长靠 monkeypatch
    monkeypatch.setattr(rs_sync, "media_duration_s", lambda p, cfg=None: 373.434)
    wl = {"source": str(media), "removedMs": 3266,
          "finalDurationMs": 369379, "srcDurationMs": 372645}
    expected, basis = rs_sync.expected_duration_s(wl)
    assert basis["basis"] == "ffprobe"
    assert abs(expected - (373.434 - 3.266)) < 1e-6
    # 源文件不存在 → 退回 wordline 记录值,基准显式标注
    wl2 = {"source": "X:/nope.mp4", "finalDurationMs": 369379}
    expected2, basis2 = rs_sync.expected_duration_s(wl2)
    assert basis2["basis"] == "wordline" and abs(expected2 - 369.379) < 1e-6  # 369379ms


# ================================================================ P27 时长账自动同步

def test_p27_2_ledger_assertion_and_sync_helper():
    """P27-2:srcDurationMs − removedMs == finalDurationMs 断言;sync 助手由构造保平账。"""
    assert rs_common.duration_ledger_error(
        {"srcDurationMs": 373434, "removedMs": 3266, "finalDurationMs": 370168}) is None
    err = rs_common.duration_ledger_error(
        {"srcDurationMs": 372645, "removedMs": 3266, "finalDurationMs": 370168})
    assert err and "时长账不平" in err
    # 老工程缺 removedMs → 按 0 计;缺 src/final → 不判
    assert rs_common.duration_ledger_error({"srcDurationMs": 100, "finalDurationMs": 100}) is None
    assert rs_common.duration_ledger_error({"removedMs": 5}) is None
    wl = rs_common.sync_wordline_durations({}, 373434, 3266)
    assert (wl["srcDurationMs"], wl["removedMs"], wl["finalDurationMs"]) == (373434, 3266, 370168)
    assert rs_common.duration_ledger_error(wl) is None


def _mk_project(tmp_path: Path, wl: dict | None = None) -> tuple[Path, Path, dict]:
    """最小工程:05_ir/wordline.json + 04_cut/cutlist.json(含 1 刀 remove)。"""
    root = tmp_path / "proj"
    (root / "05_ir").mkdir(parents=True)
    (root / "04_cut").mkdir()
    if wl is None:
        wl = _wordline("大家好今天我们来讲桌面运维先看蓝屏", 0)
        wl["source"] = "01_materials/a.mp4"
    (root / "05_ir" / "wordline.json").write_text(
        json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    total = int(wl["srcDurationMs"])
    cut_out = int(total * 0.5)
    cl = {"version": 1, "source": wl.get("source", "a.mp4"),
          "cuts": [{"id": "c001", "inMs": cut_out - 600, "outMs": cut_out,
                    "reason": "silence", "conf": 0.95, "action": "remove", "note": "",
                    "guard": {"inSilence": True, "outSilence": True, "wordClipped": False,
                              "tailKeepMs": 600, "ok": True, "okByReason": True}}],
          "keep": [[0, cut_out - 600], [cut_out, total]],
          "removedMs": 600, "srcTotalMs": total}
    (root / "04_cut" / "cutlist.json").write_text(
        json.dumps(cl, ensure_ascii=False, indent=1), encoding="utf-8")
    return root, (root / "04_cut" / "cutlist.json"), cl


def test_p27_1_apply_auto_syncs_wordline_durations(tmp_path, monkeypatch):
    """P27-1:rs_cut --apply 后 wordline 三字段自动改平,无需手改(20260920 的手工人肉步骤)。"""
    root, cl_path, cl = _mk_project(tmp_path)
    # 模拟 P26-3:改 keep 后 srcTotalMs 变了(如片尾保底延长),wordline 还停在旧值
    cl["srcTotalMs"] = 9000
    cl["keep"][-1][1] = 9000
    cl_path.write_text(json.dumps(cl, ensure_ascii=False, indent=1), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_cut.py", "--apply", str(cl_path)])
    code, doc = _capture(rs_cut.main)
    assert code == 0 and doc["ok"], doc
    assert doc["data"]["wordlineSync"]["wordline"], doc["data"]["wordlineSync"]
    wl = json.loads((root / "05_ir" / "wordline.json").read_text(encoding="utf-8"))
    assert wl["srcDurationMs"] == 9000 and wl["removedMs"] == 600
    assert wl["finalDurationMs"] == 8400
    assert rs_common.duration_ledger_error(wl) is None


def test_p27_1_apply_refuses_manual_edit_wordline(tmp_path, monkeypatch):
    """P27-1/B8 同源:wordline 带手工编辑痕迹 → 拒绝自动改写并告警;--force 可越过。"""
    root, cl_path, cl = _mk_project(tmp_path)
    wlp = root / "05_ir" / "wordline.json"
    wl = json.loads(wlp.read_text(encoding="utf-8"))
    wl["manualEdit"] = "手改过时间锚"
    wlp.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_cut.py", "--apply", str(cl_path)])
    code, doc = _capture(rs_cut.main)
    assert code == 2 and doc["code"] == "WORDLINE_MANUAL_EDIT", doc
    # --force:显式放弃手改
    monkeypatch.setattr(sys, "argv", ["rs_cut.py", "--apply", str(cl_path), "--force"])
    code, doc = _capture(rs_cut.main)
    assert code == 0 and doc["ok"], doc


def test_p27_3_ir_build_gates_on_broken_ledger(tmp_path, monkeypatch):
    """P27-3:改 keep 但 wordline 时长字段未同步 → rs_ir build 在 S3 入口报错并给修复命令。"""
    root, cl_path, cl = _mk_project(tmp_path)
    cl["srcTotalMs"] = 9000                       # keep 改了,wordline 没同步(旧值)
    cl["keep"][-1][1] = 9000
    cl_path.write_text(json.dumps(cl, ensure_ascii=False, indent=1), encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, "argv", ["rs_ir.py", "build", "--from-cutlist",
                                      "04_cut/cutlist.json", "--slug", "t",
                                      "--out", "05_ir/project.json"])
    code, doc = _capture(rs_ir.main)
    assert code == 2 and doc["code"] == "DURATION_LEDGER", doc
    assert "rs_cut.py --apply" in doc["message"]
    # 平账后(等价 --apply 的同步效果)→ 通过
    wl = json.loads((root / "05_ir" / "wordline.json").read_text(encoding="utf-8"))
    wl = rs_common.sync_wordline_durations(wl, 9000, 600)
    (root / "05_ir" / "wordline.json").write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_ir.py", "build", "--from-cutlist",
                                      "04_cut/cutlist.json", "--slug", "t",
                                      "--out", "05_ir/project.json"])
    code, doc = _capture(rs_ir.main)
    assert doc["ok"], doc


# ================================================================ P28 幽灵字符

def _wl_two_sentences() -> dict:
    """两句工程:第二句「这个待会儿删掉呢」将被整句删除(复录/口误场景)。"""
    s1 = _seg("大家好今天讲第一句", 0)
    s2 = _seg("这个待会儿删掉呢", 3000)
    return rs_align.build_wordline([s1, s2], "a.mp4")


def test_p28_1_remap_drops_whole_removed_sentence_and_renumbers():
    """P28-1:整句被删 → remap 后 chars[].i 连续、span 自洽、被删句消失、留痕 pruned。"""
    wl = _wl_two_sentences()
    assert len(wl["sentences"]) == 2
    total = int(wl["srcDurationMs"])
    # 整句删除:第二句 [3000, 4400) 落在 remove 区间 [2600, total]
    cl = {"removedMs": total - 2600, "srcTotalMs": total}
    doc = rs_align.remap_wordline(wl, [[0, 2600]], cl)
    assert doc["space"] == "final"
    # i 必须连续(下游契约)
    assert [c["i"] for c in doc["chars"]] == list(range(len(doc["chars"])))
    # 第一句 9 字完整保留,第二句 8 字全部丢弃
    assert len(doc["chars"]) == 9
    text = "".join(c["ch"] for c in doc["chars"])
    assert "大家好今天讲第一句" in text and "删掉" not in text
    # span 自洽:唯一一句的 span 覆盖全部字符,句文本与 chars 一致
    assert len(doc["sentences"]) == 1
    sp = doc["sentences"][0]["span"]
    assert sp == [0, 9]
    assert doc["sentences"][0]["text"] == text[:sp[1] - sp[0]]
    # 时长账:src − removed == final(P27-2 口径在 remap 产物上同样成立)
    assert doc["srcDurationMs"] == total
    assert doc["removedMs"] == total - 2600
    assert doc["finalDurationMs"] == 2600
    assert rs_common.duration_ledger_error(doc) is None
    assert doc["pruned"]["ghostChars"] == 8 and doc["pruned"]["ghostSentences"] == 1
    # CPS 合规(卡时长来自真实字级锚,17 内容字 / ~2.6s 远低于 9)
    events, meta = rsub.events_from_wordline(doc, 12)
    for e in events:
        dur = e["end"] - e["start"]
        assert len(e["text"].replace(" ", "")) / dur <= 9.0, e


def test_p28_1_remap_keeps_partially_overlapping_char():
    """P28-1:src 区间跨切点的字保留(吸附映射),只有完全落入删除区间的字才丢。"""
    wl = _wordline("ABCDEFGH", 0, per=100)         # 8 字,每字 100ms
    doc = rs_align.remap_wordline(wl, [[0, 350]], {"removedMs": 450})
    # D [300,380) 跨切点 → 保留(终点吸附到 350);E..H 完全落入删除区间 → 丢弃
    assert "".join(c["ch"] for c in doc["chars"]) == "ABCD"
    assert [c["i"] for c in doc["chars"]] == list(range(4))
    assert doc["chars"][-1]["endMs"] == 350
    assert doc["finalDurationMs"] == 350


def test_p28_3_prune_ghost_official_entry(tmp_path, monkeypatch):
    """P28-3:prune-ghost 官方子命令替代 _drop_ghost_chars.py;source 域拒绝并指路 remap。"""
    wl = _wl_two_sentences()
    total = int(wl["srcDurationMs"])
    cl = {"keep": [[0, 2600]], "removedMs": total - 2600, "srcTotalMs": total}
    final = rs_align.remap_wordline(wl, cl["keep"], cl)
    # 伪造"幽灵字还在"的旧态(临时脚本时代的手工链):被删句字符坍缩在删除边界上
    ghosts = []
    for c in wl["chars"][9:]:
        g = dict(c)
        g["srcStartMs"], g["srcEndMs"] = c["startMs"], c["endMs"]
        g["startMs"] = g["endMs"] = 2600            # 坍缩在删除边界 → 0ms 卡来源
        ghosts.append(g)
    final["chars"] = final["chars"] + ghosts
    final["sentences"] = final["sentences"] + [
        {"id": 1, "span": [9, 17], "punc": "", "text": "这个待会儿删掉呢"}]
    for k, c in enumerate(final["chars"]):
        c["i"] = k
    p = tmp_path / "wordline.final.json"
    p.write_text(json.dumps(final, ensure_ascii=False), encoding="utf-8")
    clp = tmp_path / "cutlist.applied.json"
    clp.write_text(json.dumps(cl, ensure_ascii=False), encoding="utf-8")
    # source 域拒绝
    src_p = tmp_path / "wl_src.json"
    src_p.write_text(json.dumps({**final, "space": "source"}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "prune-ghost", str(src_p),
                                      "--cutlist", str(clp)])
    code, doc = _capture(rs_align.main)
    assert code == 2 and doc["code"] == "NOT_FINAL_SPACE", doc
    # final 域:官方入口清掉幽灵字,i 连续、span 自洽
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "prune-ghost", str(p),
                                      "--cutlist", str(clp)])
    code, doc = _capture(rs_align.main)
    assert code == 0 and doc["ok"], doc
    out = json.loads(p.read_text(encoding="utf-8"))
    assert [c["i"] for c in out["chars"]] == list(range(len(out["chars"])))
    assert len(out["chars"]) == 9 and len(out["sentences"]) == 1
    assert doc["data"]["ghostChars"] == 8


def test_p28_2_subtitle_never_emits_sub100ms_card():
    """P28-2:内容字有效时长 <100ms 的卡必须并卡或丢弃(不静默生成 0.06s 卡)。"""
    # 构造:正常句 + 一段坍缩在删除边界上的幽灵句(全部字挤在 60ms 内)
    chars = [{"i": k, "ch": ch, "startMs": 200 * k, "endMs": 200 * k + 160,
              "srcStartMs": 200 * k, "srcEndMs": 200 * k + 160, "conf": 0.97}
             for k, ch in enumerate("大家好今天讲第一句")]
    base = len(chars)
    ghost_chars = [{"i": base + k, "ch": ch, "startMs": 2210, "endMs": 2270,
                    "srcStartMs": 2210, "srcEndMs": 2270, "conf": 0.4}
                   for k, ch in enumerate("这个待会儿删掉呢")]
    wl = {"source": "a.mp4", "space": "final", "fps": 30,
          "chars": chars + ghost_chars,
          "sentences": [{"id": 0, "span": [0, 9], "punc": "", "text": "大家好今天讲第一句"},
                        {"id": 1, "span": [9, 17], "punc": "", "text": "这个待会儿删掉呢"}],
          "srcDurationMs": 4400, "finalDurationMs": 2210, "removedMs": 2190,
          "degraded": False, "degradeReasons": []}
    events, meta = rsub.events_from_wordline(wl, 12)
    # 任何产出的卡,内容字有效时长都不得 <100ms
    for e in events:
        assert rsub._ghost_span_ms(e) >= rsub.GHOST_MIN_MS, (e["text"], e["start"], e["end"])
    gc = meta["ghostCards"]
    assert gc["merged"] + len(gc["dropped"]) >= 1, "幽灵句必须被并卡或丢弃并留痕"
    if gc["dropped"]:
        assert any("这个待会儿删掉呢" in d for d in gc["dropped"])
        assert any("幽灵卡" in r for r in meta["degradeReasons"]), meta["degradeReasons"]


# ================================================================ P29 二次 remap 防护

def test_p29_1_remap_rejects_final_space_unless_forced(tmp_path, monkeypatch):
    """P29-1:对 space=final 的 wordline remap → 拒绝(ALREADY_FINAL_SPACE);--force-remap 越过。"""
    wl = _wl_two_sentences()
    total = int(wl["srcDurationMs"])
    cl = {"keep": [[0, 2600]], "removedMs": total - 2600, "srcTotalMs": total}
    final = rs_align.remap_wordline(wl, cl["keep"], cl)
    p = tmp_path / "wlf.json"
    p.write_text(json.dumps(final, ensure_ascii=False), encoding="utf-8")
    clp = tmp_path / "cl.json"
    clp.write_text(json.dumps(cl, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "remap", str(p), "--cutlist", str(clp),
                                      "--out", str(tmp_path / "x.json")])
    code, doc = _capture(rs_align.main)
    assert code == 2 and doc["code"] == "ALREADY_FINAL_SPACE", doc
    assert "二次重映射会整体错位" in doc["message"]
    assert "refresh-durations" in doc["message"]
    # 显式 --force-remap 可越过(后果自负)
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "remap", str(p), "--cutlist", str(clp),
                                      "--out", str(tmp_path / "x.json"), "--force-remap"])
    code, doc = _capture(rs_align.main)
    assert doc["ok"], doc
    # source 域不受影响
    src = tmp_path / "wls.json"
    src.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "remap", str(src), "--cutlist", str(clp),
                                      "--out", str(tmp_path / "y.json")])
    code, doc = _capture(rs_align.main)
    assert doc["ok"], doc


def test_p29_2_refresh_durations_touches_only_duration_fields(tmp_path, monkeypatch):
    """P29-2:refresh-durations 只改时长字段,字符时间/chars/gaps/sentences 一个不动。"""
    wl = _wl_two_sentences()
    total = int(wl["srcDurationMs"])
    cl = {"keep": [[0, 2600]], "removedMs": total - 2600, "srcTotalMs": total}
    final = rs_align.remap_wordline(wl, cl["keep"], cl)   # space=final
    p = tmp_path / "wlf.json"
    p.write_text(json.dumps(final, ensure_ascii=False), encoding="utf-8")
    media = tmp_path / "a.mp4"
    media.write_bytes(b"fake")
    monkeypatch.setattr(rs_align, "probe_media_duration_ms", lambda m, cfg=None: 373434)
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "refresh-durations", str(p),
                                      "--media", str(media)])
    code, doc = _capture(rs_align.main)
    assert code == 0 and doc["code"] == "REFRESH_DURATIONS_OK", doc
    out = json.loads(p.read_text(encoding="utf-8"))
    # 只动时长三字段
    assert out["srcDurationMs"] == 373434
    assert out["removedMs"] == cl["removedMs"]
    assert out["finalDurationMs"] == 373434 - cl["removedMs"]
    assert out["chars"] == final["chars"], "字符时间绝不能动"
    assert out["gaps"] == final["gaps"] and out["sentences"] == final["sentences"]
    assert out["space"] == "final"
    assert rs_common.duration_ledger_error(out) is None
    assert doc["data"]["ledgerOk"] is True


# ================================================================ P30 断句收敛

def test_p30_1_reabsorb_repairs_word_split():
    """P30-1 回吸·触发 A:切点把保护词拦腰截断 → 边界移到词尾(整词回吸)。"""
    text = "只有一个财务费用科目按净额填列"
    # 切点 8 = 费用|科目(财务费用科目 是保护词跨度)
    cuts, moved = sg.reabsorb_cuts(text, [8], 12)
    assert moved == 1 and cuts == [10], (cuts, moved)
    cards = [c["text"] for c in sg.cards_from_cuts(text, cuts)]
    assert "只有一个财务费用科目" in cards


def test_p30_1_reabsorb_pulls_single_char_head():
    """P30-1 回吸·触发 B:字数墙甩出的单字词头(「利润表|中」的「中」)回吸进上一卡。"""
    text = "小企业会计准则的利润表中只有一个财务"
    cuts, moved = sg.reabsorb_cuts(text, [11], 12)
    assert moved == 1 and cuts == [12], (cuts, moved)
    cards = [c["text"] for c in sg.cards_from_cuts(text, cuts)]
    assert cards[0] == "小企业会计准则的利润表中"


def test_p30_1_reabsorb_never_breaks_word_or_overfill():
    """P30-1 硬边界:回吸永不超 maxChars、不吞连词领起的卡首。"""
    text2 = "小企业会计准则的利润表中只有一个财务费用科目"
    cuts, moved = sg.reabsorb_cuts(text2, [11], 11)
    assert cuts == [11], "回吸后 12 字 > maxChars=11,必须不动"
    # 连词领起的单字头不吞(「而且」的「而」领起从句)
    text3 = "这个方案很好而且我们都同意"
    cuts3, moved3 = sg.reabsorb_cuts(text3, [6], 12)
    assert moved3 == 0 and cuts3 == [6], "连词领起字头不得被回吸"


def test_p30_4_forbidden_citation_and_digit_hao():
    """P30-4:文件引文括号内侧禁切;数字+号(158号文件)禁切。"""
    s = "依据财税〔2003〕158号文件的规定"
    forb = sg.forbidden_positions(s)
    assert s.index("〕") in forb, "闭括号前禁切(防「〕158」挂卡首)"
    assert s.index("〔") + 1 in forb, "开括号后禁切"
    h = s.index("号")
    assert h in forb, "数字+号之间禁切"
    book = "这是一份《指导文件》的内容"
    fb = sg.forbidden_positions(book)
    assert book.index("《") + 1 in fb and book.index("》") in fb, "书名号内侧禁切"


def test_p30_4_protected_words_in_word_spans():
    """P30-4:动宾/名词搭配(周转归还/资金往来等)始终在词跨度内。"""
    for w, ctx in (("周转归还", "发生纳税年度内周转归还依据财税"),
                   ("资金往来", "规范股东与公司之间的资金往来务必高度重视"),
                   ("财务费用科目", "只有一个财务费用科目按净额填列")):
        spans = sg.word_spans(ctx)
        assert any(ctx[a:b] == w for a, b in spans), (w, spans)
        k = ctx.index(w)
        for i in range(k + 1, k + len(w)):
            assert any(a < i < b for a, b in spans), (w, i)


def test_p30_5_regression_cases_from_incident():
    """P30-5:昨日 4 断句案例固化进 REGRESSION,默认 DP 不得复现甩字/稀碎。"""
    assert len(sg.REGRESSION) >= 11
    for r in sg.REGRESSION[-3:]:
        res = sg.segment(r["text"], 12, terms=r.get("terms", ()))
        cards = [c["text"] for c in res["cards"]]
        assert not res["violations"], (r["text"], res["violations"])
        for term in r["must_not_split"]:
            assert any(term in "".join(c.split()) for c in cards), (r["text"], term, cards)
    # 案例 #1 的默认 DP 结果必须与昨日人工裁决一致(「利润表中」收上卡)
    res = sg.segment("小企业会计准则的利润表中只有一个财务费用科目", 12, terms=("利润表中",))
    assert [c["text"] for c in res["cards"]] == ["小企业会计准则的利润表中", "只有一个财务费用科目"]


def test_p30_2_override_residual_autoregroup_no_char_loss():
    """P30-2:override 压住 DP 卡的一部分时,余字自动成卡,不丢字(20260920 坑 #2)。

    DP 默认把 17 字句切成「只有一个财务费用科目|按净额填列只有」;override 提交
    「科目按净额填列只有」(跨卡界)→ 卡一被压剩「只有一个财务费用」,该余字区间
    必须自动重组为卡,而不是静默消失。
    """
    wl = _wordline("只有一个财务费用科目按净额填列只有", 0, per=200)
    ov = {"cards": [{"text": "科目按净额填列只有", "note": "科目起后段并为一卡"}]}
    events, meta = rsub.events_from_override(wl, ov, 12)
    got = "".join(e["text"] for e in events).replace(" ", "")
    assert "科目按净额填列只有" in got and "只有一个财务费用" in got, got
    # 全部 17 个内容字一个不丢
    src = "".join(c["ch"] for c in wl["chars"] if c["ch"] not in sg.PUNCT_WS)
    assert len(got) >= len(src) and all(ch in got for ch in src), (src, got)
    assert meta["overrideMode"] == "partial"
    assert meta["residualRegrouped"] >= 1
    assert any("余字" in r for r in meta["degradeReasons"]), meta["degradeReasons"]


def test_p30_3_override_precheck_splits_overlong_at_pause():
    """P30-3:19 字超长卡自动拆分(每片 ≤maxChars),拆分留痕(20260920 坑 #3)。"""
    text = "依据财税〔2003〕 158号文件的规定"          # 19 内容字 + 用户文案原有的空格停顿
    wl = _wordline(text, 0, per=200)
    ov = {"cards": [{"text": text, "note": "用户断句方案"}]}
    events, meta = rsub.events_from_override(wl, ov, 12)
    for e in events:
        assert len(e["text"].replace(" ", "")) <= 12, e["text"]
    notes = meta["overridePrecheck"]
    assert notes and notes[0]["chars"] == 19 and notes[0]["maxChars"] == 12
    assert notes[0]["splitInto"] == [10, 9], notes[0]      # 恰在空格停顿处拆 10+9(经验贴原案)
    assert notes[0]["strategy"] == "pause"
    # 内容字一个不丢
    got = "".join(e["text"] for e in events).replace(" ", "")
    assert "依据财税" in got and "文件的规定" in got


def test_p30_3_precheck_keeps_card_when_no_pause():
    """P30-3:文案里没有自然停顿 → 不强拆(强拆破坏词边界),留痕交 rs_verify 硬闸。"""
    text = "这一段话从头到尾没有任何停顿符号超长"          # 18 内容字,无空格/标点
    wl = _wordline(text, 0, per=200)
    ov = {"cards": [{"text": text}]}
    events, meta = rsub.events_from_override(wl, ov, 12)
    notes = meta["overridePrecheck"]
    assert notes and notes[0]["strategy"] == "none" and notes[0]["splitInto"] == []
    assert meta["overrideCards"] == 1, "无停顿不得强拆 request"


# ================================================================ 端到端验收(§5,需 ffmpeg)

@pytest.mark.skipif(not FFMPEG, reason="本机没有 ffmpeg,跳过端到端")
def test_p26_end_to_end_tail_reserve_and_ledger(tmp_path, monkeypatch):
    """端到端复现片尾案例(经验贴 #5):钳制 wordline → 实测保底 → 同步 → remap → L0。

    合成素材:正弦语音段(9.22s)+ 0.78s 静音底噪 = 10.0s;wordline 被钳制在
    9.22s(末字 endMs == 记录总时长)。全链走完后:keep 末段 = 10.0s、时长账自洽、
    二次 remap 被拦、rs_sync 总时长差 ≤0.05s、rs_verify L0 通过。
    """
    root = tmp_path / "proj"
    for d in ("01_materials", "02_sensed", "04_cut", "05_ir", "06_output"):
        (root / d).mkdir(parents=True)
    media = root / "01_materials" / "a.mp4"
    # 正弦"语音" 9.22s + 静音底噪 0.78s → 真实媒体 10.0s(尾底噪即验收的"自然收尾")
    subprocess.run([FFMPEG, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=9.22",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.78",
                    "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=10",
                    "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                    "-map", "[a]", "-map", "2:v", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-t", "10", str(media)], check=True)
    measured = rs_align.probe_media_duration_ms(media)
    assert measured and 9900 <= measured <= 10150, measured

    # 1) wordline 被钳制:末字 endMs == 记录总时长 9220(P26-2 检测应报疑点)
    segs = [_seg("大家好今天我们讲片尾截断的教训务必高度重视", 100, per=200)]
    (root / "02_sensed" / "transcript.json").write_text(
        json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "build", "--from-transcript",
                                      "02_sensed/transcript.json",
                                      "--src", "01_materials/a.mp4",
                                      "--out", "05_ir/wordline.json"])
    code, doc = _capture(rs_align.main)
    assert doc["ok"], doc
    wl = json.loads((root / "05_ir" / "wordline.json").read_text(encoding="utf-8"))
    # P26-1:--src 指向真实素材 → build 时直接 ffprobe 实测,ASR 记录值不再采用
    assert wl["srcDurationMs"] == pytest.approx(measured, abs=50)
    assert wl["durationProvenance"] == "ffprobe"
    # P26-2:转写段终点(4300)与末字 endMs(4280)重合(<1 帧)→ 钳制疑点照报
    assert wl.get("endClampSuspect"), "钳制检测必须报出疑点"

    # 模拟经验贴现场:历史工程的 wordline 记录值被字尾钳制(9220 < 实测 10000),
    # 接下来 rs_cut 的实测保底 + --apply 自动同步就是这次的修复主链
    wl["srcDurationMs"] = 9220
    wl["finalDurationMs"] = 9220
    (root / "05_ir" / "wordline.json").write_text(
        json.dumps(wl, ensure_ascii=False), encoding="utf-8")

    # 2) rs_cut --detect --media:P26-3 keep 末段保底到实测 10.0s
    monkeypatch.setattr(sys, "argv", ["rs_cut.py", "05_ir/wordline.json", "--detect", "all",
                                      "--out", "04_cut", "--media", "01_materials/a.mp4"])
    code, doc = _capture(rs_cut.main)
    assert doc["ok"], doc
    tail = doc["data"].get("tail") or {}
    assert tail.get("keepEndMs", 0) >= 9900, tail
    cl = json.loads((root / "04_cut" / "cutlist.json").read_text(encoding="utf-8"))
    assert cl["srcTotalMs"] == doc["data"]["srcTotalMs"]

    # 3) --apply:自动同步 wordline 时长账(P27-1),不再手改两字段
    monkeypatch.setattr(sys, "argv", ["rs_cut.py", "--apply", "04_cut/cutlist.json"])
    code, doc = _capture(rs_cut.main)
    assert doc["ok"] and doc["data"]["wordlineSync"]["wordline"], doc
    wl = json.loads((root / "05_ir" / "wordline.json").read_text(encoding="utf-8"))
    assert wl["srcDurationMs"] >= 9900 and wl["removedMs"] == 0
    assert wl["finalDurationMs"] == wl["srcDurationMs"]
    applied = json.loads((root / "04_cut" / "cutlist.applied.json").read_text(encoding="utf-8"))
    assert applied["keep"][-1][1] >= 9900                   # 末段保住 ~0.78s 底噪

    # 4) remap(P28 本体丢幽灵字)→ 二次 remap 被拦(P29-1)
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "remap", "05_ir/wordline.json",
                                      "--cutlist", "04_cut/cutlist.applied.json",
                                      "--out", "05_ir/wordline.final.json"])
    code, doc = _capture(rs_align.main)
    assert doc["ok"], doc
    wlf = json.loads((root / "05_ir" / "wordline.final.json").read_text(encoding="utf-8"))
    assert wlf["space"] == "final"
    assert [c["i"] for c in wlf["chars"]] == list(range(len(wlf["chars"])))
    monkeypatch.setattr(sys, "argv", ["rs_align.py", "remap", "05_ir/wordline.final.json",
                                      "--cutlist", "04_cut/cutlist.applied.json",
                                      "--out", "05_ir/wordline.final2.json"])
    code, doc = _capture(rs_align.main)
    assert code == 2 and doc["code"] == "ALREADY_FINAL_SPACE", doc

    # 5) S3 IR → S7 字幕 → 渲染 → rs_sync(实测基准,总时长差 ≤0.05s)
    monkeypatch.setattr(sys, "argv", ["rs_ir.py", "build", "--from-cutlist",
                                      "04_cut/cutlist.applied.json", "--slug", "tail",
                                      "--out", "05_ir/project.json"])
    code, doc = _capture(rs_ir.main)
    assert doc["ok"], doc
    monkeypatch.setattr(sys, "argv", ["rs_subtitle.py", "--from-wordline",
                                      "05_ir/wordline.final.json", "--ratio", "9x16",
                                      "--out", "06_output"])
    code, doc = _capture(rsub.main)
    assert doc["ok"], doc
    monkeypatch.setattr(sys, "argv", ["rs_render.py", "05_ir/project.json",
                                      "--ratio", "9x16", "--profile", "draft"])
    import rs_render
    code, doc = _capture(rs_render.main)
    assert doc["ok"], doc
    videos = sorted((root / "06_output").glob("*.mp4"))
    assert videos, "必须产出成片"
    monkeypatch.setattr(sys, "argv", ["rs_sync.py", "--wordline", "05_ir/wordline.final.json",
                                      "--ass", "06_output/subtitles.ass", "--out", "06_output",
                                      "--video", str(videos[-1])])
    code, doc = _capture(rs_sync.main)
    assert doc["ok"], (doc.get("message"), (doc.get("data") or {}).get("video"))
    vchk = doc["data"]["video"]
    assert vchk["basis"] == "ffprobe", vchk
    assert abs(vchk["diff"]) <= 0.05, f"总时长差 {vchk['diff']}s > 0.05s"

    # 6) rs_verify L0 通过
    monkeypatch.setattr(sys, "argv", ["rs_verify.py", str(root)])
    import rs_verify
    code, doc = _capture(rs_verify.main)
    assert doc["ok"], doc.get("failed")

# ================================================================ 验收 §5-5:短卡软告警如实入报告

def _seg_gap(text: str, start_ms: int, per: int = 160, gap_after: dict | None = None) -> dict:
    """带字级时间戳与指定字后停顿的转写段(gap_after = {字下标: 额外停顿 ms})。"""
    ts, t = [], start_ms
    for i, _ in enumerate(text):
        ts.append([t, t + per - 20])
        t += per + (int((gap_after or {}).get(i, 0)))
    return {"start": start_ms / 1000.0, "end": t / 1000.0, "text": text,
            "timestamp": ts, "conf": 0.95}


def test_acceptance_sync_report_records_two_explained_short_cards(tmp_path, monkeypatch):
    """验收 §5-5:override 通道产出的 2 张短卡(借款时 0.76s / 规范股东与 0.80s)如实记入
    sync_report.md 软告警,总判定仍通过(L0 不因短卡硬失败,坑 #4 的规范口径)。"""
    root = tmp_path / "proj"
    (root / "05_ir").mkdir(parents=True)
    (root / "06_output").mkdir()
    # 紧凑时间轴(句间无间隙):短卡的延长被下一卡锚点顶住(经验贴同款约束)
    t1 = _seg_gap("借款时", 0)
    t2 = _seg_gap("发生纳税年度内周转归还", int(t1["end"] * 1000))
    t3 = _seg_gap("依据财税〔2003〕158号文件的规定", int(t2["end"] * 1000))
    t4 = _seg_gap("规范股东与公司之间的资金往来务必高度重视", int(t3["end"] * 1000))
    wl = rs_align.build_wordline([t1, t2, t3, t4], "a.mp4")
    (root / "05_ir" / "wordline.json").write_text(
        json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    ov = root / "06_output" / "subtitles_override.json"
    ov.write_text(json.dumps({"cards": [
        {"text": "借款时"}, {"text": "发生纳税年度内周转归还"},
        {"text": "依据财税〔2003〕"}, {"text": "158号文件的规定"},
        {"text": "规范股东与"}, {"text": "公司之间的资金往来"},
        {"text": "务必高度重视"}]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(sys, "argv", ["rs_subtitle.py", "--from-wordline",
                                      "05_ir/wordline.json", "--override",
                                      "06_output/subtitles_override.json",
                                      "--ratio", "9x16", "--out", "06_output"])
    code, doc = _capture(rsub.main)
    assert doc["ok"], doc
    monkeypatch.setattr(sys, "argv", ["rs_sync.py", "--wordline", "05_ir/wordline.json",
                                      "--ass", "06_output/subtitles.ass", "--out", "06_output"])
    code, doc = _capture(rs_sync.main)
    assert doc["ok"], (doc.get("message"), doc["data"].get("shortDuration"))
    short = doc["data"]["shortDuration"]
    assert len(short) == 2, short
    assert any("借款时" in x for x in short) and any("规范股东与" in x for x in short)
    report = (root / "06_output" / "sync_report.md").read_text(encoding="utf-8")
    assert "时长过短明细" in report and "借款时" in report
    # rs_verify 的字幕检查:短卡是软告警,不是 L0 硬失败(坑 #4)
    import rs_verify
    chk = rs_verify.check_subtitles(root)
    assert chk["ok"] is True and chk.get("softWarnings"), chk
