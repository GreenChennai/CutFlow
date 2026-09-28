# -*- coding: utf-8 -*-
"""第五册 T5.6:假正常闭环 —— 三个「错误传染但旧闸全绿」样例 3/3 报红。

根因(docs/archive/REVIEW-20260916-假正常诊断根因.md):
  根因 1  对照闸全在中间产物之间,管线内错误同时传染 wordline/ass/成片 → 互证全绿;
  根因 3  抽帧过疏(已被第三册价值选帧解决,rs_run._bench_cmd 接证据链)。
闭环(docs/QC-SCORECARD.md 附录 A):
  产物路  check_replay_remap:重放「源 wordline + cutlist → final」纯函数对拍盘面;
  成片路  check_subtitle_speech:成片音轨实测语音活动 ↔ 字幕时间轴(不经中间产物)。

三个样例(改造前旧闸全绿,改造后必须 3/3 红,且旧闸**保持**绿 —— 红必须来自新闸):
  ① 源 wordline 被篡改,cutlist/成片未动          → 产物路红(重放不一致)
  ② 成片域 wordline 整体平移并重出字幕,成片未重烧 → 成片路红(卡起点落实测静默区)
  ③ 成片音频被替换成静音                          → 成片路红(实测无语音活动)

媒体夹具:ffmpeg 现做 7.6s 小视频(4 段 1s 正弦 burst 作语音活动代理),
秒级完成,非真实长渲染;ffmpeg 缺失时 ②③ 诚实跳过(① 是纯产物篡改,不跳)。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_align  # noqa: E402
import rs_verify  # noqa: E402


def _ffmpeg() -> str:
    try:
        rc = __import__("rs_common")
        p = rc.ffmpeg_bin(rc.load_config())
        if Path(p).is_file():
            return p
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


FF = _ffmpeg()
needs_ffmpeg = pytest.mark.skipif(not FF, reason="缺 ffmpeg(成片实测样例需要音轨)")

# 旧闸(改造前就存在的产物间对照闸)—— 样例必须保持全绿,证明红来自新闸
OLD_GATES = ("Wordline 存在且单调", "粗剪 guard 全过", "字幕合规(字数",
             "字幕↔Wordline 对齐", "产物存在")
# 新闸(独立对账双路)
GATE_REPLAY = "Wordline 推导链重放对账"
GATE_SPEECH = "字幕↔成片语音活动独立对账"

# 成片域(final)burst 时刻:语音活动代理(源域经 keep=[[0,3800],[4200,8000]] 压缩而来)
BURSTS_FINAL = [(1.0, 2.0), (2.4, 3.4), (4.2, 5.2), (6.0, 7.0)]
FINAL_DUR_S = 7.6
SENT_NCHARS = 8          # 每句 8 内容字(≤每卡字数上限;卡时长 ~0.97s,CPS 达标)


def _t(ms: int) -> str:
    s = ms / 1000.0
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def _mk_wordlines(root: Path) -> dict:
    """源域 wordline + cutlist(剪 3.8s-4.2s 气口)+ 重放生成 final;返回 final 文档。

    句 ↔ burst(源域):S1@1.0-2.0, S2@2.8-3.8, S3@4.6-5.6, S4@6.4-7.4;
    源总长 8.0s,keep=[[0,3800],[4200,8000]] → final 7.6s
    (S1 不动;S2/S3/S4 -400ms:B@2.4-3.4, C@4.2-5.2, D@6.0-7.0)。
    """
    sents_src = [(1000, 2000), (2800, 3800), (4600, 5600), (6400, 7400)]
    chars = []
    for si, (a, b) in enumerate(sents_src):
        for k in range(SENT_NCHARS):
            chars.append({"i": si * SENT_NCHARS + k,
                          "ch": "关键点讲清楚才能把事情做对"[k],
                          "startMs": a + k * 120, "endMs": a + k * 120 + 90,
                          "srcStartMs": a + k * 120, "srcEndMs": a + k * 120 + 90})
    src = {"source": "a.mp4", "chars": chars, "space": "source", "srcDurationMs": 8000,
           "sentences": [{"id": si, "span": [si * SENT_NCHARS, (si + 1) * SENT_NCHARS],
                          "text": "".join(c["ch"] for c in
                                          chars[si * SENT_NCHARS:(si + 1) * SENT_NCHARS])}
                         for si in range(4)]}
    keep = [[0, 3800], [4200, 8000]]
    cl = {"source": "a.mp4", "keep": keep, "srcTotalMs": 8000, "cuts": [], "removedMs": 400}
    (root / "04_粗剪决策").mkdir(parents=True)
    (root / "04_粗剪决策" / "cutlist.applied.json").write_text(
        json.dumps(cl, ensure_ascii=False), encoding="utf-8")
    (root / "05_时间线工程").mkdir(parents=True)
    (root / "05_时间线工程" / "wordline.json").write_text(
        json.dumps(src, ensure_ascii=False), encoding="utf-8")
    final = rs_align.remap_wordline(src, keep, cl)
    (root / "05_时间线工程" / "wordline.final.json").write_text(
        json.dumps(final, ensure_ascii=False), encoding="utf-8")
    return final


def _write_ass(root: Path, final: dict) -> None:
    lines = ["[Script Info]", "ScriptType: v4.00+", "", "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for sent in final["sentences"]:
        a = final["chars"][sent["span"][0]]
        b = final["chars"][sent["span"][1] - 1]
        lines.append(f"Dialogue: 0,{_t(a['startMs'] - 20)},{_t(b['endMs'] + 20)},,,0,0,0,,{sent['text']}")
    (root / "06_成片输出").mkdir(parents=True, exist_ok=True)
    (root / "06_成片输出" / "subtitles.ass").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mk_media(path: Path, silent: bool = False) -> None:
    """7.6s 成片:黑场视频 + 4 段 1s 正弦 burst(语音活动代理)/ 静音替换样例。"""
    cmd = [FF, "-y", "-v", "error",
           "-f", "lavfi", "-i", f"color=c=black:s=320x568:r=15:d={FINAL_DUR_S}",
           "-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono:d={FINAL_DUR_S}"]
    fc: list[str] = []
    if silent:
        cmd += ["-map", "0:v", "-map", "1:a"]           # 替换音频 = 纯静音轨
    else:
        n_in = 2
        for a, _b in BURSTS_FINAL:
            cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1.0"]
            fc.append(f"[{n_in}:a]adelay={int(a * 1000)}:all=1[t{n_in}]")
            n_in += 1
        fc.append("[1:a]" + "".join(f"[t{i}]" for i in range(2, n_in))
                  + f"amix=inputs={n_in - 1}:duration=first:normalize=0[a]")
        cmd += ["-filter_complex", ";".join(fc), "-map", "0:v", "-map", "[a]"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(path)]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0 and path.is_file(), (p.stderr or "")[-300:]


def _mk_base(tmp: Path, slug: str, media: bool = True, silent: bool = False) -> Path:
    root = tmp / slug
    final = _mk_wordlines(root)
    _write_ass(root, final)
    if media:
        (root / "06_成片输出" / "final").mkdir(parents=True, exist_ok=True)
        _mk_media(root / "06_成片输出" / "final" / "final_x_916.mp4", silent=silent)
    return root


def _check(res: dict, sub: str) -> dict:
    return next(c for c in res["checks"] if sub in c["name"])


def _assert_old_gates_green(res: dict) -> None:
    for c in res["checks"]:
        if any(sub in c["name"] for sub in OLD_GATES):
            if c.get("skipped"):
                continue                      # 中间态缺席不算红也不算绿(显式留痕)
            assert c["ok"], f"旧闸应保持全绿:{c['name']} → {c.get('detail') or c.get('skipped')}"


# ---------------------------------------------------------------- 基线:双路全绿


def test_baseline_both_paths_green(tmp_path):
    """基线(无篡改):产物路与成片路双路全绿(不计误报)。"""
    root = _mk_base(tmp_path, "基线工程")
    res = rs_verify.collect_l0(root)
    assert _check(res, GATE_REPLAY)["ok"], _check(res, GATE_REPLAY)
    assert _check(res, GATE_SPEECH)["ok"], _check(res, GATE_SPEECH)
    _assert_old_gates_green(res)


def test_speech_intervals_probe_matches_bursts(tmp_path):
    """成片实测 sanity:speech_intervals 与夹具 burst 时刻对上(独立证据源真实工作)。"""
    import rs_sync
    video = tmp_path / "probe.mp4"
    _mk_media(video)
    intervals, err = rs_sync.speech_intervals(video)
    assert intervals is not None, err
    assert len(intervals) == len(BURSTS_FINAL), intervals
    for got, (a, b) in zip(intervals, BURSTS_FINAL):
        assert abs(got[0] - a) <= 0.2 and abs(got[1] - b) <= 0.25, (got, (a, b))


# ---------------------------------------------------------------- 样例①:源 wordline 被篡改


def test_sample1_source_wordline_tampered_replay_red(tmp_path):
    """样例①:改源 wordline 但 cutlist/成片未动 → 旧闸全绿(对齐读 final 域),
    产物路重放对账红(remap(篡改源) ≠ 盘上 final)。"""
    root = _mk_base(tmp_path, "样例1工程", media=False)   # 纯产物篡改,无需媒体
    src = json.loads((root / "05_时间线工程" / "wordline.json").read_text(encoding="utf-8"))
    for c in src["chars"][SENT_NCHARS:2 * SENT_NCHARS]:   # 第 2 句整体 +500ms(保持单调)
        c["startMs"] += 500
        c["endMs"] += 500
    (root / "05_时间线工程" / "wordline.json").write_text(
        json.dumps(src, ensure_ascii=False), encoding="utf-8")
    res = rs_verify.collect_l0(root)
    assert _assert_old_gates_green(res) is None
    replay = _check(res, GATE_REPLAY)
    assert not replay["ok"] and replay.get("diffs"), replay
    assert not res["pass"]


# ---------------------------------------------------------------- 样例②:字幕整体平移且 wordline 同步改


@needs_ffmpeg
def test_sample2_shifted_subtitles_rebuilt_artifacts_speech_red(tmp_path):
    """样例②:成片空间 wordline 被整体平移(-400ms)并重出字幕(ass↔wordline 互证一致),
    成片未重烧 → 旧闸全绿;**成片路红**:卡起点全部落实测静默区(错位 400ms);
    **产物路同红**:重放推导链已断(双路互证,不依赖单闸)。"""
    root = _mk_base(tmp_path, "样例2工程")
    final = json.loads((root / "05_时间线工程" / "wordline.final.json").read_text(encoding="utf-8"))
    for c in final["chars"]:                               # 成片域时间轴整体平移 -400ms
        c["startMs"] = max(0, c["startMs"] - 400)
        c["endMs"] = max(c["startMs"] + 90, c["endMs"] - 400)
    (root / "05_时间线工程" / "wordline.final.json").write_text(
        json.dumps(final, ensure_ascii=False), encoding="utf-8")
    _write_ass(root, final)                                # ass 从平移后的 final 出(互证一致)
    res = rs_verify.collect_l0(root)
    _assert_old_gates_green(res)
    speech = _check(res, GATE_SPEECH)
    assert not speech["ok"], speech
    assert any("静默区" in p or "覆盖" in p for p in speech.get("problems", [])), speech
    assert not res["pass"]


# ---------------------------------------------------------------- 样例③:成片音频被替换


@needs_ffmpeg
def test_sample3_audio_replaced_speech_red(tmp_path):
    """样例③:成片音频被替换成静音(产物一字未动,旧闸全绿)→
    成片路红:实测无语音活动(音频被替换/静音的强特征)。"""
    root = _mk_base(tmp_path, "样例3工程", silent=True)
    res = rs_verify.collect_l0(root)
    _assert_old_gates_green(res)
    speech = _check(res, GATE_SPEECH)
    assert not speech["ok"], speech
    assert any("无语音活动" in p for p in speech.get("problems", [])), speech
    assert not res["pass"]


# ---------------------------------------------------------------- 汇总:3/3 红判据对账


def test_three_samples_all_red_new_gates(tmp_path):
    """[机] 判据:3 个错误传染样例改造后 3/3 报红(至少一路新闸红)。"""
    cases = []
    # ① 产物路(无媒体也能判)
    r1 = _mk_base(tmp_path, "汇样1", media=False)
    src = json.loads((r1 / "05_时间线工程" / "wordline.json").read_text(encoding="utf-8"))
    for c in src["chars"][SENT_NCHARS:2 * SENT_NCHARS]:
        c["startMs"] += 500
        c["endMs"] += 500
    (r1 / "05_时间线工程" / "wordline.json").write_text(json.dumps(src, ensure_ascii=False),
                                                        encoding="utf-8")
    res1 = rs_verify.collect_l0(r1)
    cases.append(("①源wordline篡改", res1, GATE_REPLAY))
    if FF:
        # ② 成片路(成片域 wordline 整体平移并重出字幕,成片未重烧)
        r2 = _mk_base(tmp_path, "汇样2")
        f2 = json.loads((r2 / "05_时间线工程" / "wordline.final.json").read_text(encoding="utf-8"))
        for c in f2["chars"]:
            c["startMs"] = max(0, c["startMs"] - 400)
            c["endMs"] = max(c["startMs"] + 90, c["endMs"] - 400)
        (r2 / "05_时间线工程" / "wordline.final.json").write_text(
            json.dumps(f2, ensure_ascii=False), encoding="utf-8")
        _write_ass(r2, f2)
        cases.append(("②字幕平移未重烧", rs_verify.collect_l0(r2), GATE_SPEECH))
        # ③ 成片路
        r3 = _mk_base(tmp_path, "汇样3", silent=True)
        cases.append(("③音频被替换", rs_verify.collect_l0(r3), GATE_SPEECH))
    red = 0
    for name, res, gate in cases:
        assert not res["pass"], f"{name}:整体判定应红"
        if not _check(res, gate)["ok"]:
            red += 1
        else:
            raise AssertionError(f"{name}:{gate} 应红(旧闸之外必须有新闸拦住)")
    assert red == len(cases), f"红判据 {red}/{len(cases)}"
    if FF:
        assert red == 3, "ffmpeg 就绪时必须 3/3"
