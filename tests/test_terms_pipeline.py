"""第四册 T4.6 回归:--terms 端到端接线(brief 术语表 → rs_run S7 → 断句 → termsHit)。

判据(计划文档 4.2 T4.6 / BACKLOG I2):
  · `rs_run --only S7` 的命令行含 `--terms`(brief 术语表自动喂入);
  · `segments_candidates.json`(candidates)有 `termsHit` 字段。

运行:pytest tests/test_terms_pipeline.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import rs_run  # noqa: E402
import rs_subtitle as rsub  # noqa: E402


# ---------------------------------------------------------------- candidates termsHit

def _wordline(text: str, per: int = 150) -> dict:
    chars = [{"i": i, "ch": ch, "startMs": per * i, "endMs": per * i + per - 30,
              "srcStartMs": per * i, "srcEndMs": per * i + per - 30, "conf": 0.99}
             for i, ch in enumerate(text)]
    return {"source": "test", "chars": chars,
            "sentences": [{"id": 0, "span": [0, len(text)]}],
            "degraded": False, "degradeReasons": []}


def test_candidates_carry_termshit_field():
    """candidates 每句必须输出 termsHit(命中术语列表,未命中为空表)。"""
    text = "安信德GEO优化服务已经正式上线了"
    wl = _wordline(text)
    events, meta = rsub.events_from_wordline(wl, 12, terms=("安信德", "GEO优化"))
    assert meta["candidates"], meta
    for c in meta["candidates"]:
        assert "termsHit" in c, c
    assert meta["candidates"][0]["termsHit"] == ["安信德", "GEO优化"]


def test_candidates_termshit_empty_without_terms():
    text = "安信德GEO优化服务已经正式上线了"
    events, meta = rsub.events_from_wordline(_wordline(text), 12)
    assert meta["candidates"][0]["termsHit"] == []


def test_terms_change_segmentation():
    """术语经 --terms 进 DP = 禁切保护:有 terms 时专名不得被拆。"""
    text = "安信德的GEO优化服务覆盖了全网主要平台"
    ev_no, meta_no = rsub.events_from_wordline(_wordline(text), 12)
    ev_t, meta_t = rsub.events_from_wordline(_wordline(text), 12, terms=("GEO优化", "安信德"))
    cards_no = [c for p in meta_no["candidates"][0]["plans"] for c in p["cards"]]
    cards_t = [c for p in meta_t["candidates"][0]["plans"] for c in p["cards"]]
    # 无保护时术语可能被拆(「GEO优化」拆词);有保护时必须完整出现在同一张卡
    for t in ("GEO优化", "安信德"):
        assert any(t in card.replace(" ", "") for card in cards_t), (t, cards_t)
    assert "".join(cards_no) == "".join(cards_t)  # 内容一致,只是分组可能不同


# ---------------------------------------------------------------- rs_run S7 接线

def _mk_project(tmp_path: Path) -> Path:
    root = tmp_path / "dev-terms工程"
    (root / "00_制作简报").mkdir(parents=True)
    (root / "01_原始素材").mkdir(parents=True)
    (root / "05_时间线工程").mkdir(parents=True)
    (root / "06_成片输出").mkdir(parents=True)
    (root / "00_制作简报" / "brief.md").write_text("# brief\n- 平台:douyin\n", encoding="utf-8")
    return root


def test_s7_command_contains_terms_from_brief(tmp_path):
    """S7 命令行含 --terms,值来自 00_制作简报/terms.txt(rs_intent 落盘格式:每行一词)。"""
    root = _mk_project(tmp_path)
    (root / "00_制作简报" / "terms.txt").write_text("安信德\nGEO优化\n# 注释行\n抖店\n",
                                                    encoding="utf-8")
    st = next(s for s in rs_run.spec() if s["id"] == "S7")
    cmd = rs_run.build_cmd(root, st)
    assert "--terms" in cmd, cmd
    val = cmd[cmd.index("--terms") + 1]
    assert val == "安信德,GEO优化,抖店", val


def test_s7_command_terms_empty_without_terms_file(tmp_path):
    """无术语表 → --terms 空串(合法空表,行为与接线前一致)。"""
    root = _mk_project(tmp_path)
    st = next(s for s in rs_run.spec() if s["id"] == "S7")
    cmd = rs_run.build_cmd(root, st)
    assert cmd[cmd.index("--terms") + 1] == ""


def test_s7_paramkeys_declare_terms(tmp_path):
    """terms 进 S7 的 paramKeys(改术语表 = 缓存变脏,字幕重跑)。"""
    st = next(s for s in rs_run.spec() if s["id"] == "S7")
    assert "terms" in (st.get("paramKeys") or [])


def test_terms_file_change_dirties_s7(tmp_path):
    """改 terms.txt → S7 必须变脏(P12-1 同款对账,经 params.terms)。"""
    root = _mk_project(tmp_path)
    (root / "00_制作简报" / "terms.txt").write_text("安信德\n", encoding="utf-8")
    st = next(s for s in rs_run.spec() if s["id"] == "S7")
    params1 = rs_run.params_of(root)
    parts1 = rs_run.stage_parts(root, st, params1, {})
    (root / "00_制作简报" / "terms.txt").write_text("安信德\n抖店\n", encoding="utf-8")
    params2 = rs_run.params_of(root)
    assert params1.get("terms") != params2.get("terms")
    parts2 = rs_run.stage_parts(root, st, params2, {})
    assert parts1["params"] != parts2["params"]
