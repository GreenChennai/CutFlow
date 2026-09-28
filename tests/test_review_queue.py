"""第四册 T4.12/T4.13 回归:Agent 复核通道优先(review_queue)+ override 回灌不强拆。

判据(计划文档 4.2 T4.12/T4.13):
  · DP ambiguous / violations 非空 / I1 命中 / 预检未拆动 → review_queue.json;
    --auto 下必定产出(有歧义/违规时非空);
  · _precheck_split_requests 优先按字级停顿(gap ≥200ms)拆;拆不动 → 进复核队列
    + 给出最长可容字卡数,不再等 rs_verify 才红;
  · rules/subtitles.md:237 的 19 字 request 案例,回灌后无需人工二次处理即合规。

运行:pytest tests/test_review_queue.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import rs_align  # noqa: E402
import rs_subtitle as rsub  # noqa: E402


def _seg(text: str, start_ms: int, per: int = 200) -> dict:
    ts = [[start_ms + i * per, start_ms + i * per + per - 20] for i in range(len(text))]
    return {"start": start_ms / 1000.0, "end": (start_ms + len(text) * per) / 1000.0,
            "text": text, "timestamp": ts, "conf": 0.95}


def _wordline(text: str, start_ms: int = 0, per: int = 200) -> dict:
    return rs_align.build_wordline([_seg(text, start_ms, per)], "a.mp4")


# ---------------------------------------------------------------- T4.13 预检拆分

def test_precheck_prefers_char_level_gap_over_pause_chars():
    """T4.13:字级停顿(gap ≥200ms)优先于停顿符——字级时间总是存在,更可靠。"""
    # 文案无停顿符;pos 11 前后注入 400ms 字级停顿(后续字整体平移)
    text = "这是一段超长的用户断句方案第二句从这里开始"
    wl = _wordline(text, per=150)
    for k, c in enumerate(wl["chars"]):
        if k >= 11:
            c["startMs"] += 400
            c["endMs"] += 400
    chars = wl["chars"]
    s, idx = rsub._content_index(chars)
    requests = [{"content": [0, len(s)]}]
    out, notes = rsub._precheck_split_requests(requests, chars, idx, 12)
    assert notes and notes[0]["strategy"] == "gap", notes
    pieces = notes[0]["splitInto"]
    assert len(pieces) >= 2 and all(p <= 12 for p in pieces)
    assert pieces[0] == 11, "第一刀必须落在字级停顿处"


def test_precheck_gap_threshold_is_200ms():
    """gap 阈值 = 200ms:小于阈值的字间空隙不构成拆分点(退化到停顿符/review)。"""
    text = "这一段话从头到尾没有任何停顿符号超长无刀点"
    wl = _wordline(text, per=200)          # 相邻字 gap = 20ms < 200
    chars = wl["chars"]
    s, idx = rsub._content_index(chars)
    out, notes = rsub._precheck_split_requests([{"content": [0, len(s)]}], chars, idx, 12,
                                               allow_gap=True)
    assert notes[0]["strategy"] == "review", notes


def test_precheck_unsplittable_goes_to_review_queue_not_verify():
    """T4.13:拆不动 → 就地进复核队列 + 最长可容字卡数,编译期即红(不再等 rs_verify)。"""
    text = "这一段话从头到尾没有任何停顿符号超长"
    wl = _wordline(text)
    ov = {"cards": [{"text": text}]}
    events, meta = rsub.events_from_override(wl, ov, 12)
    notes = meta["overridePrecheck"]
    assert notes and notes[0]["strategy"] == "review"
    assert notes[0]["maxFeasibleChars"] == 12
    assert meta["violations"], "编译期违规必须可见(不等 rs_verify)"
    assert any("字数" in v for v in meta["violations"]), meta["violations"]
    # 复核队列带同一条目
    types = [q["type"] for q in meta["reviewQueue"]]
    assert "overrideUnsplittable" in types, meta["reviewQueue"]
    # 卡内容一个不丢
    got = "".join(e["text"] for e in events).replace(" ", "")
    assert got == text


def test_rules_237_19char_request_compliant_after_replay():
    """rules/subtitles.md:237 原案:19 字 request 回灌后无需人工二次处理即合规。"""
    text = "依据财税〔2003〕 158号文件的规定"          # 19 内容字 + 用户文案原有空格停顿
    wl = _wordline(text)
    ov = {"cards": [{"text": text, "note": "用户断句方案"}]}
    events, meta = rsub.events_from_override(wl, ov, 12)
    notes = meta["overridePrecheck"]
    assert notes and notes[0]["chars"] == 19
    assert notes[0]["splitInto"] == [10, 9], notes          # 恰在空格停顿处拆 10+9
    assert notes[0]["strategy"] in ("gap", "pause")
    # 合规 = 每卡 ≤ maxChars 且内容字一个不丢,无需人工二次处理
    for e in events:
        assert len(e["text"].replace(" ", "")) <= 12, e["text"]
    got = "".join(e["text"] for e in events).replace(" ", "")
    assert "依据财税" in got and "文件的规定" in got
    unsat = [u for u in meta["unsatisfied"] if u["issue"] == "ghostCard"]
    assert not unsat, unsat


# ---------------------------------------------------------------- T4.12 review queue

def test_review_queue_nonempty_when_violations_or_ambiguous():
    """有违规/歧义的工程 → reviewQueue 非空(硬约束:不许静默)。"""
    # 违规:短卡(10ms/字,延长余量耗尽)
    text = "一二三四五六七八九"
    chars = [{"i": i, "ch": ch, "startMs": 10 * i, "endMs": 10 * i + 8,
              "srcStartMs": 10 * i, "srcEndMs": 10 * i + 8, "conf": 0.99}
             for i, ch in enumerate(text)]
    wl = {"source": "t", "chars": chars,
          "sentences": [{"id": 0, "span": [0, 9]}],
          "degraded": False, "degradeReasons": []}
    events, meta = rsub.events_from_wordline(wl, 12)
    assert meta["violations"]
    assert meta["reviewQueue"], "违规工程必须产非空复核队列"
    types = {q["type"] for q in meta["reviewQueue"]}
    assert types & {"shortCard", "ghostCard", "violation"}, meta["reviewQueue"]


def test_review_queue_semantic_hits_from_i1():
    """I1 语义命中(罚分位置被逼着切)→ reviewQueue 带 semantic(I1) 条目。"""
    text = "他说不属于这个部门的人一律不能进入机房重地"
    per = 120
    chars = [{"i": i, "ch": ch, "startMs": per * i, "endMs": per * i + per - 20,
              "srcStartMs": per * i, "srcEndMs": per * i + per - 20, "conf": 0.99}
             for i, ch in enumerate(text)]
    wl = {"source": "t", "chars": chars,
          "sentences": [{"id": 0, "span": [0, len(text)]}],
          "degraded": False, "degradeReasons": []}
    # max_chars=4 逼出多刀,必压语义单元(「他说不|属于」)→ semanticHits 非空
    events, meta = rsub.events_from_wordline(wl, 4)
    seg_types = {q["type"] for q in meta["reviewQueue"]}
    assert any(t.startswith("semantic") for t in seg_types), meta["reviewQueue"]


def test_cli_auto_writes_review_queue_file(tmp_path, monkeypatch):
    """rs_subtitle --auto:必定落盘 review_queue.json;有待复核项时非空。"""
    out = tmp_path / "06_成片输出" / "proj"
    text = "他非常努力地准备但是没有成功最后还是失败了这是最后的结论"
    wl = _wordline(text)
    wl_path = tmp_path / "wordline.json"
    wl_path.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    argv = ["rs_subtitle.py", "--from-wordline", str(wl_path),
            "--out", str(out), "--auto", "--no-snap"]
    monkeypatch.setattr(sys, "argv", argv)
    code = rsub.main()
    assert code == 0
    qf = out / "review_queue.json"
    assert qf.is_file(), "‼ --auto 必定落盘 review_queue.json"
    doc = json.loads(qf.read_text(encoding="utf-8"))
    assert doc["auto"] is True
    # 正常无违规工程队列为空也落盘(--auto 语义);改成违规工程再验非空
    wl2_path = tmp_path / "wordline2.json"
    chars = [{"i": i, "ch": ch, "startMs": 2210, "endMs": 2215,
              "srcStartMs": 2210, "srcEndMs": 2215, "conf": 0.97}
             for i, ch in enumerate("这个待会儿删掉呢这个待会儿删掉呢")]
    wl2 = {"source": "t", "space": "final", "chars": chars,
           "sentences": [{"id": 0, "span": [0, 16]}],
           "srcDurationMs": 4400, "degraded": False, "degradeReasons": []}
    wl2_path.write_text(json.dumps(wl2, ensure_ascii=False), encoding="utf-8")
    out2 = tmp_path / "06_成片输出" / "proj2"
    monkeypatch.setattr(sys, "argv", ["rs_subtitle.py", "--from-wordline", str(wl2_path),
                                      "--out", str(out2), "--auto", "--no-snap"])
    assert rsub.main() == 0
    doc2 = json.loads((out2 / "review_queue.json").read_text(encoding="utf-8"))
    assert doc2["items"], "幽灵卡工程 --auto 复核队列必须非空"


def test_cli_without_auto_writes_queue_only_when_nonempty(tmp_path, monkeypatch):
    """默认(无 --auto):队列非空才写盘(不制造空文件噪声)。"""
    text = "他非常努力地准备但是没有成功最后还是失败了这是最后的结论"
    wl = _wordline(text)
    wl_path = tmp_path / "wordline.json"
    wl_path.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "06_成片输出" / "clean"
    monkeypatch.setattr(sys, "argv", ["rs_subtitle.py", "--from-wordline", str(wl_path),
                                      "--out", str(out), "--no-snap"])
    code = rsub.main()
    assert code == 0
    qf = out / "review_queue.json"
    # 契约:文件存在 ⟺ 队列非空(无 --auto 不写空文件)
    if qf.exists():
        assert json.loads(qf.read_text(encoding="utf-8"))["items"]
    else:
        assert not qf.exists()
