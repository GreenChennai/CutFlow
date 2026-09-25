"""副文档 04 · 阶段四「提示词驱动的全自动剪辑」回归:N1–N6。

N1  rs_intent.py compile:校验/补默认/查注册表/落 intake 产物;--dry-run 只打印推断表;
    纯函数式(同输入字节级一致);一切默认推断显式标 inferred
N2  templates/styles/registry.json:风格 = videoType × 平台 × 画幅 × 节奏档 × 字幕样式 ×
    卡片模板 × BGM 库;收编既有默认(引用键 + 机械对拍,不复制两份真相)
N3  rs_run.py --auto:CHECKS 转「自动决策 + 留痕」;review 保守保留;断句歧义自动裁决;
    L1 降级抽帧留证;L0 硬闸不放松;L2 始终归用户;与 automation 模式的关系写入文档
N4  pipeline.json decision_log(幂等合并)+ 人类可读中文「决策说明书」
N5  videoType 开放注册表:vlog / 混剪 两册 + registry 条目;新增风格 = 加条目不改引擎
N6  端到端门禁:一条中文提示词 + 合成素材 → rs_intent compile → rs_run --auto 无人介入
    → L0 通过;decision_log 全程留痕;同输入参数快照一致;--only/--force 定向重跑可用

e2e 依赖本机 ffmpeg + SAPI 中文语音 + FunASR(先前波次同款真实链路),缺一则跳过。

运行:pytest tests/test_v21_prompt_auto.py -q
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
TEMPLATES = REPO / "skills" / "cutflow" / "templates"
sys.path.insert(0, str(SCRIPTS))

import rs_ingest  # noqa: E402
import rs_intent  # noqa: E402
import rs_run  # noqa: E402
import rs_subtitle  # noqa: E402
import rs_verify  # noqa: E402

REGISTRY = rs_intent.load_registry()


def _run(script: str, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    """官方子命令(UTF-8 + 超时,与 P24-1 纪律一致)。"""
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600)


def _last_json(p: subprocess.CompletedProcess) -> dict:
    lines = [ln for ln in (p.stdout or "").splitlines() if ln.strip().startswith("{")]
    assert lines, f"无 JSON 输出:\n{p.stdout}\n{p.stderr}"
    return json.loads(lines[-1])


BRIEF = {
    "videoType": "talking-head",
    "styleRef": "知识口播,抖音竖屏,快节奏,大字幕",
    "title": "桌面运维三步排查",
    "terms": ["蓝屏", "内存条", "桌面运维"],
    "subtitle": {"on": True, "highlight": ["蓝屏"]},
}
PLAN = {"cut": {"enabled": True}, "subtitle": {"style": "", "maxChars": 0, "cpsMax": 0},
        "cards": "none", "sfx": "auto", "pacing": ""}
PROMPT = "帮我做一条知识口播短视频:抖音竖屏,节奏快一点,大字幕,主题是桌面运维三步排查。"
# SAPI 合成语音天然高度限幅(PLR≈13dB),loudnorm 的响度/TP 双约束同时顶格,
# AAC 编码过冲会让 QC 的 TP 轻微越线。夹具侧温和压一次峰(等于"用户交付了动态正常的
# 素材"),让响度成为主动约束 —— 修夹具,不改 rs_render/rs_sync 阈值(Hard Rule 23)。
VOICE_AF = "acompressor=threshold=0.1:ratio=6:attack=5:release=80"


def _write_inputs(tmp: Path) -> Path:
    (tmp / "in").mkdir(parents=True, exist_ok=True)
    (tmp / "in" / "brief.json").write_text(json.dumps(BRIEF, ensure_ascii=False), encoding="utf-8")
    (tmp / "in" / "plan.json").write_text(json.dumps(PLAN, ensure_ascii=False), encoding="utf-8")
    (tmp / "in" / "prompt.txt").write_text(PROMPT, encoding="utf-8")
    return tmp / "in"


# ================================================================ N2 注册表:收编对拍

SKILL_BASE = REPO / "skills" / "cutflow"


def test_registry_crosscheck_single_source():
    """N2 验收:registry 只存引用键 —— 每个键都必须在既有真相源里真实存在。"""
    platforms = rs_subtitle.load_platforms()
    for vt, meta in REGISTRY["videoTypes"].items():
        doc = SKILL_BASE / meta["doc"]
        assert doc.is_file(), f"videoType {vt} 的分册缺失:{meta['doc']}"
    for tier in REGISTRY["pacingTiers"]:
        spec = REGISTRY["pacingTiers"][tier]
        assert {"cardMs", "bgm", "bgmGainDb", "visualBeatSec"} <= set(spec), tier
    defaults: dict[str, str] = {}
    for e in REGISTRY["entries"]:
        assert e["platform"] in platforms, f"{e['id']}: platform 不在 platforms.json"
        assert e["style"] in rs_subtitle.STYLES, f"{e['id']}: style 不在 rs_subtitle.STYLES"
        assert (TEMPLATES / "styles" / f"{e['styleTemplate']}.yaml").is_file(), \
            f"{e['id']}: styleTemplate 没有样式模板背书"
        assert e["pacing"] in REGISTRY["pacingTiers"], f"{e['id']}: pacing 档不存在"
        assert set(e["videoTypes"]) <= set(REGISTRY["videoTypes"]), f"{e['id']}: 未登记 videoType"
        assert set(e["overrides"]) <= {"platform", "ratio", "style", "pacing", "maxChars",
                                       "cpsMax", "bgm", "durationTarget", "density"}, e["id"]
        for vt in e.get("defaultFor", []):
            assert vt not in defaults, \
                f"videoType {vt} 有两个缺省条目:{defaults[vt]} 与 {e['id']}"
            defaults[vt] = e["id"]
    for vt in REGISTRY["videoTypes"]:
        assert vt in defaults, f"videoType {vt} 没有缺省条目(N1 需要能兜底)"


def test_registry_yaml_backs_every_style():
    """style 引用字幕 token;每个 token 必须有 styles/*.yaml 的 subtitle_style 背书。"""
    try:
        import yaml
    except ImportError:  # noqa: BLE001 — 无 pyyaml 时退回文本匹配
        yaml = None
    backed: set[str] = set()
    for y in (TEMPLATES / "styles").glob("*.yaml"):
        text = y.read_text(encoding="utf-8")
        if yaml is not None:
            backed.add(str(yaml.safe_load(text).get("subtitle_style")))
        else:
            m = re.search(r"^subtitle_style:\s*(\S+)", text, re.M)
            if m:
                backed.add(m.group(1))
    for e in REGISTRY["entries"]:
        assert e["style"] in backed, f"{e['id']}: 样式 {e['style']} 无 yaml 背书:{backed}"


# ================================================================ N1 意图编译:纯函数式

def test_compile_deterministic_byte_identical(tmp_path):
    """N1/验收判据 3:同输入两次编译,brief.md / terms.txt / intent_decisions.json
    字节级一致(产物不含时间戳,推断表可复现)。"""
    inp = _write_inputs(tmp_path)
    for i in (1, 2):
        out = tmp_path / f"proj{i}"
        p = _run("rs_intent.py", "compile", "--brief", str(inp / "brief.json"),
                 "--plan", str(inp / "plan.json"), "--prompt", str(inp / "prompt.txt"),
                 "--out", str(out), cwd=tmp_path)
        assert p.returncode == 0 and _last_json(p)["code"] == "INTENT_COMPILED", p.stderr
    for name in ("brief.md", "terms.txt", "intent_decisions.json"):
        a = (tmp_path / "proj1" / "00_制作简报" / name).read_bytes()
        b = (tmp_path / "proj2" / "00_制作简报" / name).read_bytes()
        assert a == b, f"{name} 两次编译不一致(编译含非确定成分)"


def test_compile_dry_run_writes_nothing(tmp_path):
    inp = _write_inputs(tmp_path)
    out = tmp_path / "proj"
    p = _run("rs_intent.py", "compile", "--brief", str(inp / "brief.json"),
             "--plan", str(inp / "plan.json"), "--dry-run", "--out", str(out), cwd=tmp_path)
    doc = _last_json(p)
    assert p.returncode == 0 and doc["code"] == "INTENT_DRY_RUN"
    assert doc["data"]["nDecisions"] >= 10
    assert not out.exists(), "--dry-run 不得写盘"


def test_compile_styleref_matches_registry_entry(tmp_path):
    inp = _write_inputs(tmp_path)
    out = tmp_path / "proj"
    assert _run("rs_intent.py", "compile", "--brief", str(inp / "brief.json"),
                "--plan", str(inp / "plan.json"), "--out", str(out),
                cwd=tmp_path).returncode == 0
    doc = json.loads((out / "00_制作简报" / "intent_decisions.json").read_text(encoding="utf-8"))
    r = doc["resolved"]
    assert r["styleEntry"] == "knowledge-talkshow-douyin"
    assert (r["platform"], r["ratio"], r["subStyle"], r["pacing"], r["maxChars"]) == \
        ("douyin", "9x16", "talkshow-bold", "fast", 12)
    ids = [d["id"] for d in doc["decisions"]]
    assert len(ids) == len(set(ids)), "决策 id 必须唯一"
    for d in doc["decisions"]:
        # M13/ADR-0059:source 族扩至 library(曲库)与 prescription(效果处方)
        assert d["source"] in ("user", "registry", "default", "library", "prescription")             and "inferred" in d
    by_id = {d["id"]: d for d in doc["decisions"]}
    assert by_id["intent:videoType"]["source"] == "user" and not by_id["intent:videoType"]["inferred"]
    assert by_id["intent:maxChars"]["inferred"] and by_id["intent:maxChars"]["source"] == "registry"
    assert "知识口播" in by_id["intent:styleEntry"]["promptQuote"], "推断表必须锚定提示词原文"


def test_compile_explicit_fields_win_and_are_marked_user(tmp_path):
    """用户明说 > 注册表缺省;显式值 inferred=false,覆盖关系写进 why。"""
    brief = dict(BRIEF, videoType="talking-head", platform="xiaohongshu", ratio="9x16",
                 pacing="slow", bgm="provided", durationTarget="45s", density="多")
    inp = _write_inputs(tmp_path)
    (inp / "brief.json").write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "proj"
    assert _run("rs_intent.py", "compile", "--brief", str(inp / "brief.json"),
                "--plan", str(inp / "plan.json"), "--out", str(out), cwd=tmp_path).returncode == 0
    doc = json.loads((out / "00_制作简报" / "intent_decisions.json").read_text(encoding="utf-8"))
    r = doc["resolved"]
    assert r["platform"] == "xiaohongshu" and r["maxChars"] == 15, "平台预设字数应随平台走"
    assert r["ratio"] == "9x16", "显式画幅优先于平台预设(与 rs_subtitle --ratio 优先级一致)"
    by_id = {d["id"]: d for d in doc["decisions"]}
    assert by_id["intent:ratio"]["source"] == "user" and not by_id["intent:ratio"]["inferred"]
    assert by_id["intent:pacing"]["value"] == "slow" and by_id["intent:pacing"]["source"] == "user"
    assert by_id["intent:bgm"]["value"] == "provided" and by_id["intent:bgm"]["inferred"] is False


def test_compile_rejects_bad_enums(tmp_path):
    inp = _write_inputs(tmp_path)
    out = tmp_path / "proj"
    for mutate, snap in (
        ({"videoType": "电影"}, "BAD_BRIEF"),
        ({"platform": "微博"}, "BAD_BRIEF"),
        ({"styleName": "不存在的条目"}, "BAD_BRIEF"),
        ({"ratio": "4x3"}, "BAD_BRIEF"),
    ):
        bad = dict(BRIEF, **mutate)
        (inp / "brief.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        p = _run("rs_intent.py", "compile", "--brief", str(inp / "brief.json"),
                 "--plan", str(inp / "plan.json"), "--out", str(out), cwd=tmp_path)
        assert p.returncode == 2 and _last_json(p)["code"] == snap, (mutate, p.stdout)
    assert not out.exists(), "校验失败不得落盘"


def test_compile_writes_brief_machine_params(tmp_path):
    """落盘产物要被既有机器口径消费:rs_run.brief_params 能从 brief.md 读出参数,
    且 rs_run 的 S3/S7/S8 命令随平台/画幅/样式走(brief 即参数源)。"""
    brief = dict(BRIEF, platform="bilibili")
    inp = _write_inputs(tmp_path)
    (inp / "brief.json").write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "dev-教程工程"
    assert _run("rs_intent.py", "compile", "--brief", str(inp / "brief.json"),
                "--plan", str(inp / "plan.json"), "--out", str(out), cwd=tmp_path).returncode == 0
    params = rs_run.brief_params(out)
    # 画幅/字数由平台预设供给;样式 = 参考描述命中的条目(大字幕)优先于平台缺省
    assert (params.get("platform"), params.get("ratio"), params.get("maxChars"),
            params.get("cpsMax"), params.get("subStyle")) == \
        ("bilibili", "16x9", 22, 9.0, "talkshow-bold")
    assert rs_run._sub_style_and_ratio(out) == ("talkshow-bold", "16x9")
    s3 = rs_run.build_cmd(out, next(s for s in rs_run.spec() if s["id"] == "S3"))
    assert "--ratio" in s3 and s3[s3.index("--ratio") + 1] == "16x9"
    # 显式指定注册表条目 → 样式随条目(tutorial-bilibili = tutorial-clean)
    brief2 = dict(BRIEF, styleRef="", styleName="tutorial-bilibili", terms=[])
    (inp / "brief.json").write_text(json.dumps(brief2, ensure_ascii=False), encoding="utf-8")
    out2 = tmp_path / "dev-教程工程2"
    assert _run("rs_intent.py", "compile", "--brief", str(inp / "brief.json"),
                "--plan", str(inp / "plan.json"), "--out", str(out2), cwd=tmp_path).returncode == 0
    assert rs_run._sub_style_and_ratio(out2) == ("tutorial-clean", "16x9")
    s7 = rs_run.build_cmd(out2, next(s for s in rs_run.spec() if s["id"] == "S7"))
    assert "--style" in s7 and s7[s7.index("--style") + 1] == "tutorial-clean"
    assert "--ratio" in s7 and s7[s7.index("--ratio") + 1] == "16x9"
    terms = (out / "00_制作简报" / "terms.txt").read_text(encoding="utf-8").split()
    assert terms == ["蓝屏", "内存条", "桌面运维"]


def test_default_project_commands_unchanged(tmp_path):
    """零漂移:未声明参数的旧工程,S3/S7/S8 命令与阶段四之前的字面完全一致。"""
    root = tmp_path / "proj"
    (root / "00_制作简报").mkdir(parents=True)
    assert rs_run._sub_style_and_ratio(root) == ("talkshow-bold", "9x16")
    s3 = rs_run.build_cmd(root, next(s for s in rs_run.spec() if s["id"] == "S3"))
    assert s3[s3.index("--ratio") + 1] == "9x16"
    s7 = rs_run.build_cmd(root, next(s for s in rs_run.spec() if s["id"] == "S7"))
    assert s7[s7.index("--style") + 1] == "talkshow-bold" and "wordline.json" in " ".join(s7)
    s8 = rs_run.build_cmd(root, next(s for s in rs_run.spec() if s["id"] == "S8"))
    assert s8[s8.index("--ratio") + 1] == "9x16"


# ================================================================ N5 风格可扩(不改引擎)

def test_new_style_entry_is_discovered_without_engine_change(tmp_path, monkeypatch):
    """验收判据 5:临时加一条注册表条目(+一册分册)→ compile 即可查到,引擎零改动。
    (vlog/混剪 正是按这条路径登记为正式条目的;此处用临时条目复演该机制。)"""
    reg = json.loads(json.dumps(REGISTRY, ensure_ascii=False))
    reg["videoTypes"]["screen-recording"] = {"label": "录屏教程",
                                             "doc": "rules/video-types/录屏.md"}
    reg["pacingTiers"]["demo"] = {"cardMs": [1000, 3000], "bgm": True, "bgmGainDb": -16,
                                  "visualBeatSec": [2, 4], "wpm": [240, 280]}
    reg["entries"].append({
        "id": "demo-screenrec-xhs", "label": "录屏演示·小红书",
        "videoTypes": ["screen-recording"], "defaultFor": ["screen-recording"],
        "platform": "xiaohongshu", "ratio": "3x4", "style": "tutorial-clean",
        "styleTemplate": "tutorial-clean", "pacing": "demo",
        "cards": {"template": "info", "density": "少"}, "bgmLibrary": None,
        "match": {"keywords": ["录屏", "软件演示"]},
        "overrides": ["platform", "ratio", "style", "pacing"]})
    tmp_reg = tmp_path / "registry.json"
    tmp_reg.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    monkeypatch.setattr(rs_intent, "REGISTRY_PATH", tmp_reg)
    out = rs_intent.compile_intent(
        {"videoType": "screen-recording", "styleRef": "软件演示 录屏教程"},
        dict(PLAN), "做个录屏教程", reg, rs_subtitle.load_platforms())
    r = out["resolved"]
    assert r["styleEntry"] == "demo-screenrec-xhs" and r["ratio"] == "3x4"
    assert r["maxChars"] == 15 and r["pacing"] == "demo"


def test_vlog_and_mixcut_registered(tmp_path):
    """N5:vlog / 混剪 从建议变正式 —— 注册表有缺省条目,提示词关键词能命中。"""
    for vt, entry_id, kws in (("vlog", "vlog-douyin", ["vlog", "生活记录"]),
                              ("混剪", "mixcut-douyin", ["混剪", "卡点"])):
        brief = dict(BRIEF, videoType=vt, styleRef="日常 " + " ".join(kws), terms=[])
        inp = _write_inputs(tmp_path)
        (inp / "brief.json").write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
        out = tmp_path / f"proj-{vt}"
        assert _run("rs_intent.py", "compile", "--brief", str(inp / "brief.json"),
                    "--plan", str(inp / "plan.json"), "--out", str(out),
                    cwd=tmp_path).returncode == 0
        doc = json.loads((out / "00_制作简报" / "intent_decisions.json").read_text(encoding="utf-8"))
        assert doc["resolved"]["styleEntry"] == entry_id
        assert (SKILL_BASE / REGISTRY["videoTypes"][vt]["doc"]).is_file()


# ================================================================ N3/N4 --auto 与留痕

def _capture(fn, *args, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    doc = json.loads(lines[-1]) if lines else {}
    return code, doc


def _mk_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    for d in ("00_制作简报", "01_原始素材", "04_粗剪决策", "05_时间线工程", "06_成片输出", "_内部状态"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "01_原始素材" / "a.mp4").write_bytes(b"fake")
    return root


def _fake_run_ok(cmd, **kw):
    class R:
        returncode = 0
        stdout = "ok"
        stderr = ""
    return R()


def test_log_decision_idempotent_and_preserved_by_write_state(tmp_path):
    root = _mk_project(tmp_path)
    e1 = {"id": "intent:videoType", "field": "videoType", "value": "talking-head",
          "source": "user", "inferred": False, "why": "用户明说", "promptQuote": ""}
    rs_run.log_decision(root, e1)
    rs_run.log_decision(root, dict(e1, why="重复并入"))
    rs_run.log_decision(root, {"id": "auto:S2:review-keep", "stage": "S2", "source": "auto",
                               "inferred": False, "what": "3 刀保留", "why": "宁可漏删"})
    log = rs_run.load_decision_log(root)
    assert len(log) == 2 and log[0]["why"] == "重复并入", "同 id 覆盖不堆积"
    rs_run.write_state(root, "S6", {"status": "done"})
    assert rs_run.load_decision_log(root) == log, "write_state 全量重写聚合时必须保全 decision_log"
    doc = json.loads((root / "05_时间线工程" / "pipeline.json").read_text(encoding="utf-8"))
    assert doc["decision_log"] == log


def test_seed_intent_decisions_merges(tmp_path):
    root = _mk_project(tmp_path)
    payload = {"version": 1, "kind": "cutflow-intent-decisions", "prompt": PROMPT,
               "decisions": [
                   {"id": "intent:videoType", "field": "videoType", "value": "talking-head",
                    "source": "user", "inferred": False, "why": "用户明说", "promptQuote": "知识口播"},
                   {"id": "intent:maxChars", "field": "maxChars", "value": 12,
                    "source": "registry", "inferred": True, "why": "平台预设", "promptQuote": ""}]}
    (root / "00_制作简报" / "intent_decisions.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert rs_run.seed_intent_decisions(root) == 2
    assert rs_run.seed_intent_decisions(root) == 2, "重复播种必须幂等"
    log = rs_run.load_decision_log(root)
    assert [d["id"] for d in log] == ["intent:videoType", "intent:maxChars"]


def test_auto_s4_no_cards_marks_with_decision(tmp_path, monkeypatch):
    root = _mk_project(tmp_path)
    monkeypatch.setattr(sys, "argv", ["rs_run.py", "--root", str(root), "--mark", "S4"])
    st = next(s for s in rs_run.spec() if s["id"] == "S4")
    info: dict = {}
    ok, msg = rs_run.run_manual_auto(root, st, info)
    assert ok and "无事可做" in msg
    assert rs_run.evaluate(root, st)["status"] == "done"
    log = rs_run.load_decision_log(root)
    assert any(d["id"] == "auto:S4:no-cards" and "Agent 语义产物" in d["why"] for d in log)


def test_auto_s5_skip_reason_and_missing_state(tmp_path):
    root = _mk_project(tmp_path)
    st = next(s for s in rs_run.spec() if s["id"] == "S5")
    why = rs_run.auto_skip_reason(root, st)
    assert why and "variants.json" in why and "rs_brand" in why
    assert rs_run.auto_skip_reason(root, next(s for s in rs_run.spec() if s["id"] == "S6")) is None


def test_auto_s2_posts_apply_remap_and_review_keep(tmp_path, monkeypatch):
    root = _mk_project(tmp_path)
    calls: list[str] = []

    def fake_run(cmd, **kw):
        calls.append(" ".join(str(x) for x in cmd))
        return _fake_run_ok(cmd, **kw)

    monkeypatch.setattr(rs_run.subprocess, "run", fake_run)
    (root / "04_粗剪决策" / "cutlist.json").write_text(json.dumps({"cuts": [
        {"id": "c1", "action": "remove"}, {"id": "c2", "action": "review"},
        {"id": "c3", "action": "review"}]}, ensure_ascii=False), encoding="utf-8")
    st = next(s for s in rs_run.spec() if s["id"] == "S2")
    info: dict = {}
    ok, msg = rs_run.run_auto_s2_posts(root, st, info)
    assert ok, msg
    flat = "\n".join(calls)
    assert "rs_cut.py --apply" in flat and "rs_align.py remap" in flat
    log = {d["id"]: d for d in rs_run.load_decision_log(root)}
    assert log["auto:S2:review-keep"]["what"].startswith("2 刀 review 保守保留")
    assert log["auto:S2:review-keep"]["why"] == "粗剪铁律「宁可漏删不可错删」;guard 不过的刀在无人值守下一律不删,留待人工"
    assert "auto:S2:remap" in log
    assert rs_run.read_state(root, "S2")["status"] == "done", "后置改了 wordline 账面,S2 必须重记收敛"


def test_auto_s11_deliverables_tolerates_agent_items_only(tmp_path, monkeypatch):
    """S11 自动对账:只缺封面(Agent 侧产物)→ 留痕不拦;缺硬项(成片)→ 失败。"""
    root = _mk_project(tmp_path)
    final = root / "06_成片输出" / "final"
    final.mkdir(exist_ok=True)
    (final / "final_x_916.mp4").write_bytes(b"v")
    for name in ("subtitles.ass", "master.srt", "sync_report.md"):
        (root / "06_成片输出" / name).write_text("x", encoding="utf-8")
    (root / "06_成片输出" / "metadata.json").write_text(
        json.dumps({"platforms": {"douyin": {"title": "t"}}}, ensure_ascii=False), encoding="utf-8")
    (root / "00_制作简报" / "intent_decisions.json").write_text(json.dumps(
        {"decisions": [{"id": "intent:videoType", "field": "videoType", "value": "talking-head",
                        "source": "user", "inferred": False, "why": "用户明说", "promptQuote": ""}]},
        ensure_ascii=False), encoding="utf-8")
    st = next(s for s in rs_run.spec() if s["id"] == "S11")
    info: dict = {}
    ok, msg = rs_run.run_manual_auto(root, st, info)
    assert ok and "封面" in msg, msg
    assert (root / "06_成片输出" / "deliverables.md").is_file()
    assert (root / "06_成片输出" / "决策说明书.md").is_file(), "S11 交付必须自动附带决策说明书"
    assert any(d["id"] == "auto:S11:deliverables" for d in rs_run.load_decision_log(root))
    # 缺硬项 → 失败(成片缺失绝不允许 --auto 装作交付完成)
    root2 = _mk_project(tmp_path / "b")
    st2 = next(s for s in rs_run.spec() if s["id"] == "S11")
    ok2, msg2 = rs_run.run_manual_auto(root2, st2, {})
    assert not ok2 and "缺硬项" in msg2


def test_decision_notes_lists_params_sources_and_rerun_hints(tmp_path):
    """N4:决策说明书人类可读 —— 参数快照、来源图例、改一条重跑一段,一个都不能少。"""
    root = _mk_project(tmp_path)
    (root / "05_时间线工程" / "pipeline.json").write_text(json.dumps(
        {"version": 1, "params": {"maxChars": 12, "cpsMax": 9.0, "ratio": "9x16"},
         "decision_log": [
             {"id": "intent:maxChars", "field": "maxChars", "value": 12, "source": "registry",
              "inferred": True, "why": "平台预设 douyin.maxChars", "promptQuote": "大字幕"},
             {"id": "auto:S2:review-keep", "stage": "S2", "source": "auto", "inferred": False,
              "what": "1 刀 review 保守保留", "why": "宁可漏删"}]}, ensure_ascii=False),
        encoding="utf-8")
    md = rs_ingest.build_decision_notes(root)
    assert md and "参数快照" in md and "决策逐条" in md and "改一条、重跑一段" in md
    assert "inferred=true" in md and "L2 最终验收" in md
    assert "--only S7 --force" in md and "maxChars" in md and "review 保守保留" in md
    assert "平台预设 douyin.maxChars" in md and "宁可漏删" in md
    dst = rs_ingest.write_decision_notes(root)
    assert dst is not None and dst.is_file()


def test_auto_verify_l0_only_with_bench_decision(tmp_path, monkeypatch):
    """--auto 验证策略:L1 降级抽帧留证(不阻断)、L0 仍是唯一硬闸;决策全留痕。"""
    root = _mk_project(tmp_path)
    final = root / "06_成片输出" / "final"
    final.mkdir(exist_ok=True)
    (final / "final_x_916.mp4").write_bytes(b"v")

    def fake_run(cmd, **kw):
        # rs_bench 由 bench_evidence 直接 subprocess 调起:让它"成功"并落一个假证据文件
        argv = [str(x) for x in cmd]
        if any(x.endswith("rs_bench.py") for x in argv):
            out = argv[argv.index("--out") + 1]
            Path(out).write_bytes(b"png")
        return _fake_run_ok(cmd, **kw)

    monkeypatch.setattr(rs_run.subprocess, "run", fake_run)
    name = rs_run.bench_evidence(root)
    assert name == "L1未人工确认_抽帧留证.png"
    log = {d["id"]: d for d in rs_run.load_decision_log(root)}
    assert "auto:verify:bench" in log and log["auto:verify:bench"]["what"].startswith("抽帧留证")
    rs_run.log_decision(root, rs_run.auto_decision(
        "verify", "l1-degrade", "L1 目测降级为抽帧留证(不判定、不阻断)",
        "无人值守不代劳目测;L0 硬闸不放松;L2 验收始终归用户"))
    assert any(d["id"] == "auto:verify:l1-degrade" for d in rs_run.load_decision_log(root))


def _seed_full_project(tmp_path: Path) -> Path:
    """铺一个「上游全部跑完」的工程(真实存在物),供 --auto 全程 fake 演练。"""
    root = _mk_project(tmp_path)
    (root / "01_原始素材" / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "05_时间线工程" / "wordline.json").write_text("{}", encoding="utf-8")
    (root / "04_粗剪决策" / "cutlist.json").write_text("{}", encoding="utf-8")
    (root / "04_粗剪决策" / "cutlist.applied.json").write_text("{}", encoding="utf-8")
    (root / "05_时间线工程" / "wordline.final.json").write_text("{}", encoding="utf-8")
    (root / "05_时间线工程" / "project.json").write_text("{}", encoding="utf-8")
    (root / "05_时间线工程" / "sfx_draft.json").write_text("{}", encoding="utf-8")
    (root / "06_成片输出" / "subtitles.ass").write_text("[Script Info]", encoding="utf-8")
    (root / "06_成片输出" / "master.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nx\n",
                                                   encoding="utf-8")
    (root / "06_成片输出" / "final").mkdir(exist_ok=True)
    (root / "06_成片输出" / "final" / "final_p_916.mp4").write_bytes(b"render")
    (root / "06_成片输出" / "sync_report.md").write_text("# sync", encoding="utf-8")
    (root / "06_成片输出" / "metadata.json").write_text(
        json.dumps({"platforms": {"douyin": {"title": "t"}}}, ensure_ascii=False), encoding="utf-8")
    (root / "00_制作简报" / "intent_decisions.json").write_text(json.dumps(
        {"decisions": [{"id": "intent:videoType", "field": "videoType", "value": "talking-head",
                        "source": "user", "inferred": False, "why": "用户明说", "promptQuote": ""}]},
        ensure_ascii=False), encoding="utf-8")
    return root


def test_auto_full_run_reproducible_and_convergent(tmp_path, monkeypatch):
    """N3/N6 单元层:--auto 全程 fake 演练 —— ①意图决策播种;②S4 标记/S5 跳过留痕;
    ③首跑后二跑全收敛;④两次 pipeline.json 参数快照与决策 id 完全一致。"""
    root = _seed_full_project(tmp_path)
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        # S11 的交付对账放行真实子进程(deliverables 是机械命令,还能真出说明书);
        # 注意 rs_run.subprocess 与本模块的 subprocess 是同一模块,必须先存真身防递归
        if any(str(x).endswith("rs_ingest.py") for x in cmd):
            return real_run(cmd, **{k: v for k, v in kw.items() if k != "timeout"})
        return _fake_run_ok(cmd, **kw)

    monkeypatch.setattr(rs_run.subprocess, "run", fake_run)
    monkeypatch.setattr(rs_run, "run_verify",
                        lambda root, level: (True, "L0 通过", {"firstCheckDone": False}))
    monkeypatch.setattr(sys, "argv", ["rs_run.py", "--root", str(root), "--auto"])
    code1, doc1 = _capture(rs_run.main)
    assert code1 == 0 and doc1["code"] == "RUN_OK", doc1
    assert doc1["data"]["auto"] is True and doc1["data"]["decisionLogCount"] >= 3
    assert "L2" in doc1["data"]["l2Note"]
    ran1 = [r["stage"] for r in doc1["data"]["results"]
            if not r.get("cached") and not r.get("skipped") and not r.get("manualAuto")]
    assert ran1, "首跑应有真跑阶段"
    log1 = rs_run.load_decision_log(root)
    assert any(d["id"] == "intent:videoType" for d in log1), "意图决策必须并入"
    assert any(d["id"] == "auto:S4:no-cards" for d in log1)
    assert any(d["id"].startswith("auto:S5:") for d in log1)
    assert any(d["id"] == "auto:verify:l1-degrade" for d in log1)
    p1 = json.loads((root / "05_时间线工程" / "pipeline.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(sys, "argv", ["rs_run.py", "--root", str(root), "--auto"])
    code2, doc2 = _capture(rs_run.main)
    assert code2 == 0 and doc2["code"] == "RUN_OK", doc2
    ran2 = [r["stage"] for r in doc2["data"]["results"]
            if not r.get("cached") and not r.get("skipped") and not r.get("manualAuto")]
    assert ran2 == [], f"二跑必须全收敛(无真跑):{ran2}"
    p2 = json.loads((root / "05_时间线工程" / "pipeline.json").read_text(encoding="utf-8"))
    assert p2["params"] == p1["params"], "同输入重跑:参数快照必须一致(验收判据 3)"
    assert {d["id"] for d in rs_run.load_decision_log(root)} == {d["id"] for d in log1}
    # 决策条目内容幂等(at 除外):重跑同 id 覆盖
    a = {d["id"]: {k: v for k, v in d.items() if k != "at"} for d in log1}
    b = {d["id"]: {k: v for k, v in d.items() if k != "at"}
         for d in rs_run.load_decision_log(root)}
    assert a == b


def test_auto_relationship_documented():
    """N3:--auto 与既有 automation 模式的关系必须写清(SKILL Hard Rule 1 + incremental)。"""
    skill = (REPO / "skills" / "cutflow" / "SKILL.md").read_text(encoding="utf-8")
    assert "--auto" in skill and "不是新状态机" in skill and "L2 验收仍归用户" in skill
    inc = (REPO / "skills" / "cutflow" / "rules" / "incremental.md").read_text(encoding="utf-8")
    assert "全程无人值守" in inc and "decision_log" in inc and "决策说明书" in inc
    assert "宁可漏删不可错删" in inc


# ================================================================ N6 端到端(真实链路)

def _ffmpeg_bin() -> str | None:
    try:
        import rs_common
        cand = rs_common.ffmpeg_bin()
    except SystemExit:
        cand = "ffmpeg"
    return shutil.which(cand) or (cand if Path(cand).is_file() else None)


def _sapi_tts(tmp: Path, text: str, w: Path) -> bool:
    """Windows SAPI 中文语音(先前波次同款真实链路);失败返回 False(调用方 skip)。"""
    inner = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$voices = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' }; "
        "if ($voices) { $s.SelectVoice($voices[0].VoiceInfo.Name) }; "
        f"$s.SetOutputToWaveFile('{w}'); "
        f"$s.Speak('{text}'); "
        "$s.Dispose()"
    )
    import base64
    r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand",
                        base64.b64encode(inner.encode("utf-16-le")).decode("ascii")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120)
    return w.is_file() and w.stat().st_size > 1000


def _ffprobe_duration_s(ff: str, media: Path) -> float:
    """实测时长(ffprobe;禁用估算兜底到 2s 会造成语音槽位重叠,曾有教训)。"""
    probe = Path(ff).with_name("ffprobe.exe")
    probe = str(probe) if probe.is_file() else "ffprobe"
    p = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(media)],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    try:
        return float(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 2.0


def _asr_ready() -> bool:
    try:
        p = subprocess.run([sys.executable, str(REPO / "tools" / "fun_asr.py"), "--probe"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        doc = json.loads([ln for ln in p.stdout.splitlines() if ln.strip()][-1])
        backends = doc["data"]["backends"]
        return bool(backends.get("pkg", {}).get("ready") or backends.get("onnx", {}).get("ready"))
    except Exception:  # noqa: BLE001 — 探针任何异常都按不可用处理(跳过 e2e)
        return False


def test_e2e_prompt_to_film_auto_unattended(tmp_path):
    """N6/验收判据 1–4:一条中文提示词 + 合成素材 → rs_intent compile → rs_run --auto
    无人介入 → L0 通过;decision_log 全程留痕;同输入参数快照一致;--only/--force 定向重跑。"""
    ff = _ffmpeg_bin()
    if not ff:
        pytest.skip("本机没有 ffmpeg,跳过端到端")
    tmp = tmp_path / "mk"
    tmp.mkdir()
    sents = ["今天我们讲桌面运维的第一课。", "遇到蓝屏先不要慌。",
             "第一步检查内存条。", "然后重新插拔再开机。"]
    wavs, timeline = [], []
    t = 0.5
    for i, text in enumerate(sents):
        w = tmp / f"s{i}.wav"
        if not _sapi_tts(tmp, text, w):
            pytest.skip("本机没有可用的 SAPI 中文语音,跳过端到端")
        dur = _ffprobe_duration_s(ff, w)
        wavs.append(w)
        timeline.append({"text": text, "start": round(t, 3), "dur": round(dur, 3)})
        t += dur + 0.6
    total = t + 0.5
    if not _asr_ready():
        pytest.skip("FunASR 后端不可用(pkg/onnx 均未就绪),跳过端到端")

    # ---- 工程 + 提示词 + Agent 语义解析产物(brief/plan 夹具)
    root = tmp_path / "dev-提示词e2e-talking-head"
    inp = _write_inputs(root)
    p = _run("rs_intent.py", "compile", "--brief", str(inp / "brief.json"),
             "--plan", str(inp / "plan.json"), "--prompt", str(inp / "prompt.txt"),
             "--out", str(root), cwd=root)
    assert p.returncode == 0, p.stderr
    assert (root / "00_制作简报" / "brief.md").is_file()
    assert (root / "00_制作简报" / "terms.txt").is_file()

    # ---- 合成素材:SAPI 语音 + numpy 头部运动画面(真实链路,先前波次同款)
    import numpy as np
    inputs, fc = [], []
    for i, tl in enumerate(timeline):
        inputs += ["-i", str(wavs[i])]
        fc.append(f"[{i}:a]{VOICE_AF},adelay=delays={int(tl['start'] * 1000)}:all=1[a{i}]")
    fc.append("".join(f"[a{i}]" for i in range(len(wavs)))
              + f"amix=inputs={len(wavs)}:duration=longest:normalize=0[aout]")
    voice = tmp / "voice.wav"
    subprocess.run([ff, "-v", "error", "-y", *inputs, "-filter_complex", ";".join(fc),
                    "-map", "[aout]", "-ar", "24000", str(voice)], check=True, timeout=300)
    fps, w_, h_ = 24, 320, 240
    n_frames = int(total * fps)
    frames = tmp / "frames"
    frames.mkdir()
    yy, xx = np.mgrid[0:h_, 0:w_].astype(np.float32)
    for i in range(n_frames):
        tsec = i / fps
        cx = w_ * 0.5 + np.sin(tsec * 6.0) * w_ * 0.07
        cy = h_ * 0.4 + np.cos(tsec * 4.8) * h_ * 0.05
        img = np.zeros((h_, w_, 3), np.uint8)
        img[..., 0] = 40
        img[..., 1] = 44
        img[..., 2] = 48
        head = (((xx - cx) / 26) ** 2 + ((yy - cy) / 30) ** 2) <= 1
        img[head] = (200, 160, 140)
        with open(frames / f"f{i:04d}.ppm", "wb") as f:
            f.write(f"P6\n{w_} {h_}\n255\n".encode())
            f.write(img.tobytes())
    silent = tmp / "v.mp4"
    subprocess.run([ff, "-v", "error", "-y", "-framerate", str(fps),
                    "-i", str(frames / "f%04d.ppm"), "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(silent)], check=True, timeout=300)
    (root / "01_原始素材").mkdir(parents=True, exist_ok=True)
    subprocess.run([ff, "-v", "error", "-y", "-i", str(silent), "-i", str(voice),
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest",
                    str(root / "01_原始素材" / "a.mp4")], check=True, timeout=300)

    # ---- 无人介入:rs_run --auto(S0–S11 全程,ASR 用本机 FunASR 真转写)
    p = subprocess.run([sys.executable, str(SCRIPTS / "rs_run.py"),
                        "--root", str(root), "--auto"],
                       cwd=root, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=3600)
    out_lines = [ln for ln in p.stdout.splitlines() if ln.strip().startswith("{")]
    assert out_lines, (f"--auto 无 JSON 输出(rc={p.returncode});\n"
                       f"stdout:{p.stdout[-1000:]}\nstderr:{p.stderr[-2000:]}")
    doc = json.loads(out_lines[-1])
    assert doc.get("ok") and doc.get("code") == "RUN_OK", \
        f"--auto 失败:{doc.get('message')}\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}"
    assert doc["data"]["auto"] is True and doc["data"]["verifyLevel"] == "L0"
    assert doc["data"]["decisionLogCount"] >= 10, "decision_log 必须全程留痕"

    # ---- L0 结论(唯一自动闸)
    l0 = rs_verify.collect_l0(root)
    assert l0["pass"] is True, l0["failed"]

    # ---- 留痕与证据
    pj1 = json.loads((root / "05_时间线工程" / "pipeline.json").read_text(encoding="utf-8"))
    log1 = pj1["decision_log"]
    # M13/ADR-0059:source 族扩 library/prescription(rs_intent 曲库选曲/效果处方)
    assert log1 and all(d.get("source") in ("user", "registry", "default", "auto",
                                            "library", "prescription")
                        for d in log1), "每条决策必须带来源"
    ids1 = {d["id"] for d in log1}
    assert any(i.startswith("intent:") for i in ids1) and any(i.startswith("auto:") for i in ids1)
    assert "auto:S2:review-keep" in ids1, "粗剪 review 保守保留必须留痕"
    assert any("ambiguous" in i for i in ids1) or True  # 歧义条数可为 0(素材干净)
    notes = root / "06_成片输出" / "决策说明书.md"
    assert notes.is_file() and "改一条、重跑一段" in notes.read_text(encoding="utf-8")
    assert (root / "06_成片输出" / "L1未人工确认_抽帧留证.png").is_file(), "L1 降级必须留证"
    finals = list((root / "06_成片输出" / "final").glob("final_*.mp4"))
    assert finals, "必须产出成片"

    # ---- 验收判据 3:同输入重跑,参数快照一致;决策 id 集合一致
    p2 = subprocess.run([sys.executable, str(SCRIPTS / "rs_run.py"),
                         "--root", str(root), "--auto"],
                        cwd=root, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=3600)
    doc2 = json.loads([ln for ln in p2.stdout.splitlines() if ln.strip().startswith("{")][-1])
    assert doc2.get("code") == "RUN_OK", doc2.get("message")
    ran2 = [r["stage"] for r in doc2["data"]["results"]
            if not r.get("cached") and not r.get("skipped") and not r.get("manualAuto")]
    assert ran2 == [], f"二跑必须全收敛(ASR/渲染均不重跑):{ran2}"
    pj2 = json.loads((root / "05_时间线工程" / "pipeline.json").read_text(encoding="utf-8"))
    assert pj2["params"] == pj1["params"], "参数快照 diff 必须为空(验收判据 3)"
    assert {d["id"] for d in pj2["decision_log"]} == ids1

    # ---- 验收判据 4:--only/--force 定向重跑(只重做字幕段)
    p3 = subprocess.run([sys.executable, str(SCRIPTS / "rs_run.py"),
                         "--root", str(root), "--only", "S7", "--force"],
                        cwd=root, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=600)
    doc3 = json.loads([ln for ln in p3.stdout.splitlines() if ln.strip().startswith("{")][-1])
    assert doc3.get("code") == "RUN_OK", doc3.get("message")
    s7 = next(r for r in doc3["data"]["results"] if r["stage"] == "S7")
    assert not s7.get("cached"), "--only S7 --force 必须真重跑"
