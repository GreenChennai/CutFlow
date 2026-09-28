"""第四册 T4.14 回归:断句「异常换句」上下文审计器。

判据(计划文档 4.2 T4.14):对每张卡检查「是否把固定搭配 / 动宾 / 引文切断」,
输出可读报告;接入 S9(rs_sync --segment-audit)与 rs_diagnose(复用同一实现)。
嫌疑条目 100% 附可读理由(可解释性判据)。

运行:pytest tests/test_segment_audit.py -q
"""
from __future__ import annotations

import io
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import rs_sync  # noqa: E402
import rs_diagnose  # noqa: E402
import segmentation as sg  # noqa: E402


# ---------------------------------------------------------------- 核心审计(segmentation)

def test_word_span_crossing_boundary_flagged():
    """A 类:词/固定搭配跨卡(「周转 | 归还」动宾被斩断)→ 嫌疑 + 可读理由。"""
    issues = sg.audit_boundaries(["借款时发生纳税年度内周转", "归还依据财税〔2003〕158号文件"])
    assert len(issues) == 1
    reasons = "".join(issues[0]["reasons"])
    assert "周转归还" in reasons and "切断" in reasons


def test_negation_tail_flagged():
    """B 类:否定词收尾(「不」被甩到卡尾,被否定对象在下一卡)。"""
    issues = sg.audit_boundaries(["这件事我根本不", "同意你们的看法"])
    assert len(issues) == 1
    assert any("否定词" in r for r in issues[0]["reasons"])


def test_conj_tail_flagged():
    """B 类:连词收尾(「而 | 被」式固定搭配被拆)。"""
    issues = sg.audit_boundaries(["可能因为其中一家店铺违规而", "被连带处理最终一同遭殃"])
    assert any("连词" in r for r in issues[0]["reasons"])


def test_citation_cut_flagged():
    """C 类:引文切断——半括号挂卡首(20260920 实测事故形态)。"""
    issues = sg.audit_boundaries(["依据财税〔2003", "〕158号文件的规定办理"])
    reasons = "".join(issues[0]["reasons"])
    assert "引文" in reasons or "〕" in reasons


def test_clean_boundaries_pass():
    """语义完整边界(标点处/从句处)不得误报。"""
    assert sg.audit_boundaries(["先做人群画像", "再设计脚本才是正确顺序"]) == []
    assert sg.audit_boundaries(["这个方案便宜而且好用", "所以我们决定采用它"]) == []


# ---------------------------------------------------------------- rs_sync 接线(S9)

def _events_from_texts(texts, start=0.0, dur=2.0):
    return [{"start": start + i * dur, "end": start + (i + 1) * dur, "text": t}
            for i, t in enumerate(texts)]


def test_segment_audit_report_shape_and_warn_only():
    """审计结果:WARN 级不翻转总判定;每条嫌疑附可读理由(可解释性 100%)。"""
    events = _events_from_texts(["借款时发生纳税年度内周转", "归还依据财税的规定办理"])
    res = rs_sync.segment_audit(events)
    assert res["warnOnly"] is True and res["pass"] is False
    assert res["suspects"] == len(res["items"])
    for f in res["findings"]:
        assert isinstance(f, str) and f.strip()


def test_segment_audit_clean_events_pass():
    events = _events_from_texts(["先做人群画像", "再设计脚本才是正确顺序"])
    res = rs_sync.segment_audit(events)
    assert res["pass"] is True and res["suspects"] == 0


def test_rs_sync_cli_segment_audit_flag(tmp_path, monkeypatch):
    """--segment-audit 进 S9 判据流:sync_report.md 出审计小节,sync_rows.json 留账。"""
    text = "他非常努力地准备但是没有成功最后还是失败了"
    per = 150
    chars = [{"i": i, "ch": ch, "startMs": per * i, "endMs": per * i + per - 30,
              "srcStartMs": per * i, "srcEndMs": per * i + per - 30, "conf": 0.99}
             for i, ch in enumerate(text)]
    wl = {"source": "t", "chars": chars,
          "sentences": [{"id": 0, "span": [0, len(text)]}],
          "srcDurationMs": per * len(text), "degraded": False, "degradeReasons": []}
    wl_path = tmp_path / "wordline.json"
    wl_path.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    events, meta = __import__("rs_subtitle").events_from_wordline(wl, 12)
    ass_path = tmp_path / "subtitles.ass"
    __import__("rs_subtitle").write_ass(events, ass_path, "subtitle-white", "9x16",
                                        "1080x1920")
    out = tmp_path / "out"
    argv = ["rs_sync.py", "--wordline", str(wl_path), "--ass", str(ass_path),
            "--out", str(out), "--segment-audit"]
    monkeypatch.setattr(sys, "argv", argv)
    code = rs_sync.main()
    assert code in (0, 4)
    report = (out / "sync_report.md").read_text(encoding="utf-8")
    assert "断句上下文审计" in report
    rows = json.loads((out / "sync_rows.json").read_text(encoding="utf-8"))
    assert "segmentAudit" in rows["summary"]


# ---------------------------------------------------------------- rs_diagnose 复用

def test_diagnose_d3c_reuses_same_implementation(tmp_path):
    """rs_diagnose 的 D3c 与 rs_sync.segment_audit 同源(口径不漂移)。"""
    issues = sg.audit_boundaries(["借款时发生纳税年度内周转", "归还依据财税的规定办理"])
    assert issues, "前置:该样例必须先在核心审计里命中"
    ass = tmp_path / "subtitles.ass"
    events = _events_from_texts(["借款时发生纳税年度内周转", "归还依据财税的规定办理"])
    body = "".join(f"Dialogue: 0,0:00:{int(e['start']):02d},0:00:{int(e['end']):02d},Main,"
                   f",0,0,0,,{e['text']}\n" for e in events)
    ass.write_text("[Script Info]\n[Events]\nFormat: Layer, Start, End, Style, Name,"
                   "MarginL, MarginR, MarginV, Effect, Text\n" + body, encoding="utf-8")
    check = rs_diagnose.d3c_segment_context(ass)
    assert check["id"] == "D3c"
    assert check["status"] == "warn"
    assert check["suspects"] >= 1 and check["findings"]


def test_diagnose_d3c_skipped_without_ass(tmp_path):
    check = rs_diagnose.d3c_segment_context(tmp_path / "nope.ass")
    assert check["status"] == "skipped"
