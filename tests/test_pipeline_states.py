"""T2.11 判据:八种典型盘面的阶段状态机确定结论(rules/pipeline-state.md §3)。

在一个全 done 的构造盘面上施加 8 种典型变更(+2 种声明内降级盘面),断言:
  · 每个受影响阶段的 status 有**唯一确定结论**(不许"拿不准");
  · --explain 的三态 verdict(status → keep/rerun/stop/degrade-continue)与
    rules/pipeline-state.md 的映射表逐格一致;
  · stale 的 staleReason 能指到具体证据(带外改写 / params / tool / inputs)。

产物全部写 tempdir;脚本 hash 用 fake 脚本目录(monkeypatch SCRIPTS_DIR),
绝不触碰仓库内真实脚本。运行:pytest tests/test_pipeline_states.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_run  # noqa: E402
import rs_paths  # noqa: E402

FAKE_SCRIPTS = ("rs_ingest.py", "rs_greenscreen.py", "rs_align.py", "rs_cut.py",
                "rs_ir.py", "rs_render.py", "rs_brand.py", "rs_sfx.py",
                "rs_subtitle.py", "textopt.py", "segmentation.py", "rs_sync.py",
                "rs_meta.py")


def _w(root: Path, rel: str, content="x") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content if isinstance(content, str) else bytes(content),
                 encoding="utf-8" if isinstance(content, str) else None)
    return p


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """全 done 构造盘面:真实文件产物 + 真实 done 账(fake 脚本 hash)。"""
    fake = tmp_path / "fakescripts"
    fake.mkdir()
    for name in FAKE_SCRIPTS:
        (fake / name).write_text(f"# fake {name}\n", encoding="utf-8")
    monkeypatch.setattr(rs_run, "SCRIPTS_DIR", fake)

    root = tmp_path / "proj"
    for key in ("brief", "materials", "assets", "cut", "timeline", "output"):
        (root / rs_paths.p(key)).mkdir(parents=True)
    _w(root, f"{rs_paths.p('brief')}/brief.md", "# 简报\n")
    _w(root, f"{rs_paths.p('materials')}/a.mp4")
    _w(root, f"{rs_paths.p('materials')}/manifest.json", "{}")
    _w(root, f"{rs_paths.p('cut')}/cutlist.json", '{"cuts": []}')
    _w(root, f"{rs_paths.p('cut')}/cutlist.applied.json", '{"cuts": []}')
    _w(root, f"{rs_paths.p('timeline')}/wordline.json", '{"chars": []}')
    _w(root, f"{rs_paths.p('timeline')}/project.json", '{"version": 1}')
    _w(root, f"{rs_paths.p('timeline')}/variants.json", '{"variants": []}')
    _w(root, f"{rs_paths.p('timeline')}/sfx_draft.json", '{"events": []}')
    _w(root, f"{rs_paths.p('assets')}/artboard/manifest.json",
       '{"version": 1, "appliedAt": "2026-09-27T00:00:00+08:00"}')
    _w(root, f"{rs_paths.p('output')}/subtitles.ass", "[Script Info]\n")
    _w(root, f"{rs_paths.p('output')}/final/final_916.mp4")
    _w(root, f"{rs_paths.p('output')}/branded/成片_916_logo.mp4")
    _w(root, f"{rs_paths.p('output')}/sync_report.md", "# sync\n")
    _w(root, f"{rs_paths.p('output')}/sync_rows.json", "[]")
    _w(root, f"{rs_paths.p('output')}/metadata.json", '{"title": "t"}')
    _w(root, f"{rs_paths.p('output')}/deliverables.md", "# 交付\n")
    for st in rs_run.spec(root):
        rs_run.record_stage_done(root, st)
    return root


def _statuses(root: Path) -> dict[str, str]:
    return {st["id"]: rs_run.evaluate(root, st)["status"] for st in rs_run.spec(root)}


def _stage(root: Path, sid: str) -> dict:
    return next(s for s in rs_run.spec(root) if s["id"] == sid)


def _mutate(root: Path, rel: str, content="changed-by-outside-hand") -> None:
    (root / rel).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------- 8 盘面参数化
# 每盘面: (名称, 变更函数, 受影响阶段 → 确定结论;未列出的阶段必须保持 done)

def _m_subtitle(root):     # 盘面 1:改字幕(手改产物)
    _mutate(root, f"{rs_paths.p('output')}/subtitles.ass")


def _m_ir(root):           # 盘面 2:改 IR(带外改 project.json)
    _mutate(root, f"{rs_paths.p('timeline')}/project.json", '{"version": 1, "x": 1}')


def _m_cutlist(root):      # 盘面 3:改 cutlist(带外改 cutlist.json)
    _mutate(root, f"{rs_paths.p('cut')}/cutlist.json", '{"cuts": [{"action": "keep"}]}')


def _m_cards(root):        # 盘面 4:改卡片(带外改 artboard manifest)
    doc = json.loads((root / rs_paths.p("assets") / "artboard" / "manifest.json")
                     .read_text(encoding="utf-8"))
    doc["items"] = [{"id": "k1"}]
    (root / rs_paths.p("assets") / "artboard" / "manifest.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _m_script(root):       # 盘面 5:只改脚本(改本阶段工具文件)
    (rs_run.SCRIPTS_DIR / "rs_subtitle.py").write_text("# fake rs_subtitle.py v2\n",
                                                       encoding="utf-8")


def _m_brief_text(root):   # 盘面 6:只改 brief 正文(不动参数行)
    _mutate(root, f"{rs_paths.p('brief')}/brief.md", "# 简报(改了措辞)\n")


def _m_delete_out(root):   # 盘面 7:外删产物
    (root / rs_paths.p("output") / "subtitles.ass").unlink()


def _m_meta(root):         # 盘面 8:外改产物(改 metadata.json)
    _mutate(root, f"{rs_paths.p('output')}/metadata.json", '{"title": "t2"}')


EXPECTED = [
    pytest.param(
        "改字幕", _m_subtitle,
        {"S7": "stale", "S8": "stale", "S9": "stale"},
        id="盘面1-改字幕"),
    pytest.param(
        "改IR", _m_ir,
        {"S3": "stale", "S4": "stale", "S5": "stale", "S6": "stale", "S8": "stale"},
        id="盘面2-改IR"),
    pytest.param(
        "改cutlist", _m_cutlist,
        {"S2": "stale"},                # applied 副本未动 → S3 不脏(诚实口径)
        id="盘面3-改cutlist"),
    pytest.param(
        "改卡片", _m_cards,
        {"S4": "stale"},
        id="盘面4-改卡片"),
    pytest.param(
        "只改脚本", _m_script,
        {"S7": "stale"},
        id="盘面5-只改脚本"),
    pytest.param(
        "只改brief正文", _m_brief_text,
        {"S0": "stale", "S10": "stale"},  # brief 文本不是 S7 输入 → S7 不算 stale
        id="盘面6-只改brief"),
    pytest.param(
        "外删产物", _m_delete_out,
        {"S7": "missing", "S8": "stale", "S9": "stale"},
        id="盘面7-外删产物"),
    pytest.param(
        "外改产物", _m_meta,
        {"S10": "stale", "S11": "stale"},
        id="盘面8-外改产物"),
]


@pytest.mark.parametrize(["label", "mutate", "expected"], EXPECTED)
def test_board_conclusion_is_deterministic(board, label, mutate, expected):
    """8 盘面:受影响阶段结论唯一确定;未列出阶段一律保持 done(不许误伤)。"""
    mutate(board)
    got = _statuses(board)
    for sid, want in expected.items():
        assert got[sid] == want, f"{label}:{sid} 期望 {want},实际 {got[sid]}"
    assert {k: v for k, v in got.items() if v != "done"} == expected, \
        f"{label}:结论集不唯一(多打/漏打了阶段)"


@pytest.mark.parametrize(["label", "mutate", "expected"], EXPECTED)
def test_board_verdict_follows_pipeline_state_md(board, label, mutate, expected):
    """三态 verdict 与 rules/pipeline-state.md 映射表逐格一致:
    stale→rerun / missing→stop / done→keep。"""
    mutate(board)
    for sid, status in expected.items():
        p = rs_run.explain_payload(board, _stage(board, sid))
        assert p["status"] == status
        assert p["verdict"] == {"stale": "rerun", "missing": "stop",
                                "done": "keep"}[status], \
            f"{label}:{sid} verdict 与状态机文档不一致"


# ---------------------------------------------------------------- 证据链断言

def test_stale_reason_points_to_out_of_band_evidence(board):
    """盘面 1/8 的 staleReason 必须给出"带外改写"证据(旧 outHash → 新 outHash)。"""
    _mutate(board, f"{rs_paths.p('output')}/subtitles.ass")
    r = rs_run.evaluate(board, _stage(board, "S7"))
    assert any("带外改写" in w for w in r["staleReason"]), r["staleReason"]
    p = rs_run.explain_payload(board, _stage(board, "S7"))
    assert p["compared"]["outputs"]["outOfBand"] is True


def test_stale_reason_points_to_script_hash(board):
    """盘面 5:staleReason 指到具体脚本文件(tool 组)。"""
    (rs_run.SCRIPTS_DIR / "rs_subtitle.py").write_text("# v3\n", encoding="utf-8")
    r = rs_run.evaluate(board, _stage(board, "S7"))
    assert any("tool" in w and "rs_subtitle.py" in w for w in r["staleReason"]), \
        r["staleReason"]


def test_brief_param_change_dirties_s7_but_text_does_not(board):
    """盘面 6 细分:brief 正文不脏 S7;参数行(每卡字数)经 paramKeys 脏 S7(P12-1)。"""
    _m_brief_text(board)
    assert _statuses(board)["S7"] == "done"
    _mutate(board, f"{rs_paths.p('brief')}/brief.md", "# 简报\n- 每卡字数: 10\n")
    r = rs_run.evaluate(board, _stage(board, "S7"))
    assert r["status"] == "stale"
    assert any("params" in w and "maxChars" in w for w in r["staleReason"]), r["staleReason"]


def test_manual_stage_without_marker_is_stop_not_keep(board, tmp_path):
    """盘面 4 反面(两个确定结论,P11-1):
    ① 有账+有标记的 S4 去掉 appliedAt → 产物带外改写 = stale → rerun(outHash 证据);
    ② 无账、无 manifest、但 cards.json 已声明 → 人工无标记 = missing → stop(无 skip 通道)。"""
    mp = board / rs_paths.p("assets") / "artboard" / "manifest.json"
    doc = json.loads(mp.read_text(encoding="utf-8"))
    doc.pop("appliedAt")
    mp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    r = rs_run.evaluate(board, _stage(board, "S4"))
    assert r["status"] == "stale" and any("带外改写" in w for w in r["staleReason"])
    assert rs_run.explain_payload(board, _stage(board, "S4"))["verdict"] == "rerun"
    # 无账盘面:missing + 有卡片计划(cards.json 存在)→ 不给 skip 通道 → 必须停
    root = tmp_path / "p4b"
    for key in ("brief", "materials", "assets", "cut", "timeline", "output"):
        (root / rs_paths.p(key)).mkdir(parents=True)
    _w(root, f"{rs_paths.p('brief')}/cards.json", '{"cards": []}')
    _w(root, f"{rs_paths.p('timeline')}/project.json", '{"version": 1}')
    p2 = rs_run.explain_payload(root, _stage(root, "S4"))
    assert p2["status"] == "missing" and p2["verdict"] == "stop"


# ---------------------------------------------------------------- 声明内降级盘面

def test_degrade_continue_s4_without_cards_plan(tmp_path, monkeypatch):
    """降级盘面 A:S4 无卡片计划(cards.json 缺)→ missing → degrade-continue。"""
    root = tmp_path / "p4"
    for key in ("brief", "materials", "assets", "cut", "timeline", "output"):
        (root / rs_paths.p(key)).mkdir(parents=True)
    _w(root, f"{rs_paths.p('timeline')}/project.json", '{"version": 1}')
    p = rs_run.explain_payload(root, _stage(root, "S4"))
    assert p["status"] == "missing"
    assert p["verdict"] == "degrade-continue"
    assert p["verdictLabel"] == "可降级继续(按声明留痕)"


def test_degrade_continue_s5_without_variants(board):
    """降级盘面 B:S5 从未跑过账(rec 清空)+ 无 variants.json → missing → degrade-continue。

    (盘面构造:直接删 S5 状态账,模拟"工程未声明品牌变体"的新盘面;
     有账时删 variants.json 是"输入移除"= stale,另一确定性结论,上面盘面族已覆盖口径。)"""
    (board / rs_paths.p("state") / "S5.json").unlink(missing_ok=True)
    (board / rs_paths.p("timeline") / "variants.json").unlink()
    p = rs_run.explain_payload(board, _stage(board, "S5"))
    assert p["status"] == "missing"
    assert p["verdict"] == "degrade-continue"


def test_explain_keep_payload_shows_why_not_stale(board):
    """done 阶段的 --explain 给出"为什么不算 stale":逐组对账明细全空。"""
    p = rs_run.explain_payload(board, _stage(board, "S7"))
    assert p["verdict"] == "keep"
    for group in ("inputs", "tool", "external", "params"):
        assert p["compared"][group]["changed"] == [], (group, p["compared"][group])
    assert p["compared"]["outputs"]["outOfBand"] is False
    assert p["contract"]["timeoutPolicy"]["base"] > 0      # 契约随载荷回带(T2.10)
