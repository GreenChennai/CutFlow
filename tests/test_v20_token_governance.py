"""副文档 05 · 阶段五 T1/T2/T3(Token 与上下文治理)回归。

T1-1  临时脚本升格:gen-cards(原 _gen_cards.py)/ add-overlay(原 _apply_overlay.py)/
      export-fallback(原 artboard export_fallback.py);--export skip 判定改「产物在盘才 skip」(P6-2)
T1-2  SKILL Hard Rule 25:禁止为一次性任务现写剪辑逻辑脚本,能复用必须升格并进能力目录
T2-1  capabilities.json 由 rs_caps.py generate 自动生成(单一真相源);防漂移门禁 = 再生成 diff 为空
T3-1a capabilities 声明的每条命令能被对应 argparse 接受(probe 对拍);argparse 脚本全覆盖目录

运行:pytest tests/test_v20_token_governance.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
CAPABILITIES = REPO / "skills" / "cutflow" / "capabilities.json"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "tests"))

import check_manual_cmds as gate  # noqa: E402
import rs_caps  # noqa: E402

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082")


def _run(script: str, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    """跑官方子命令(子进程口径:UTF-8 + 超时,与 P24-1 纪律一致)。"""
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)


def _last_json(p: subprocess.CompletedProcess) -> dict:
    lines = [ln for ln in p.stdout.splitlines() if ln.strip().startswith("{")]
    assert lines, f"无 JSON 输出:\n{p.stdout}\n{p.stderr}"
    return json.loads(lines[-1])


def _plan() -> list[dict]:
    """卡片计划(内容字段归 gen-cards,时间窗字段归 add-overlay —— 一份计划两处消费)。"""
    return [
        {"id": "c01", "template": "info", "title": "什么是 GEO",
         "lines": ["生成式引擎优化", "让 AI 主动推荐你"], "accent": "#4F8CFF",
         "kicker": "知识点 01", "startMs": 3200, "durationMs": 1800,
         "motion": {"in": "fadeIn", "inMs": 400}},
        {"id": "c02", "template": "stat", "title": "38%",
         "lines": ["线索成本下降"], "startMs": 5000, "durationMs": 2200},
    ]


def _mk_proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    for d in ("00_制作简报", "01_原始素材", "03_创作素材", "05_时间线工程"):
        (root / d).mkdir(parents=True)
    (root / "00_制作简报" / "cards.json").write_text(
        json.dumps(_plan(), ensure_ascii=False, indent=1), encoding="utf-8")
    (root / "01_原始素材" / "a.mp4").write_bytes(b"fake")
    (root / "05_时间线工程" / "project.json").write_text(json.dumps({
        "version": 1, "slug": "demo", "fps": 30,
        "canvas": {"width": 1080, "height": 1920},
        "tracks": [{"kind": "video", "name": "main", "clips": [
            {"src": "01_原始素材/a.mp4", "startMs": 0, "durationMs": 12000,
             "sourceInMs": 0}]}],
    }, ensure_ascii=False), encoding="utf-8")
    return root


def _stub_artboard(tmp_path: Path, engine: str) -> Path:
    """假 artboard 技能目录:导出脚本把 src 落成真 PNG(1px 合法字节),验证管线不断。"""
    art = tmp_path / "artboard-skill"
    (art / "scripts").mkdir(parents=True)
    (art / "scripts" / f"export{'_fallback' if engine == 'fallback' else ''}.py").write_text(
        "import argparse, pathlib, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--source', required=True)\n"
        "p.add_argument('--output', required=True)\n"
        "p.add_argument('--width', type=int, default=1080)\n"
        "p.add_argument('--height', type=int, default=0)\n"
        "p.add_argument('--scale', type=int, default=1)\n"
        "p.add_argument('--transparent', action='store_true')\n"
        "a = p.parse_args()\n"
        "out = pathlib.Path(a.output)\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        f"out.write_bytes({PNG_1PX!r})\n"
        "print('{\"ok\": true}')\n", encoding="utf-8")
    return art


# ================================================================ T1-1a gen-cards

def test_t1_gen_cards_deterministic_idempotent_and_registered(tmp_path):
    """给定输入必得同一输出:两次生成字节一致;manifest 幂等登记;路径写工程根相对。"""
    root = _mk_proj(tmp_path)
    r1 = _run("rs_artboard.py", "gen-cards", "--from", "00_制作简报/cards.json", cwd=root)
    assert r1.returncode == 0, r1.stderr
    doc1 = _last_json(r1)
    assert doc1["ok"] and doc1["code"] == "CARDS_GENERATED"
    html1 = (root / "03_创作素材/artboard/c01/src/index.html").read_text(encoding="utf-8")
    assert "什么是 GEO" in html1 and "1080" in html1
    man = json.loads((root / "03_创作素材/artboard/manifest.json").read_text(encoding="utf-8"))
    it = {i["id"]: i for i in man["items"]}["c01"]
    # P6 口径:artboard 目录在工程根下 → 清单字段写工程根相对,--export/--apply 默认 --root 即命中
    assert it["output"] == "03_创作素材/artboard/c01/export/c01.png"
    assert it["project"] == "03_创作素材/artboard/c01/src"
    assert it["sourceHash"]

    # 再跑一次:字节级一致(确定性),manifest 也不变(幂等)
    r2 = _run("rs_artboard.py", "gen-cards", "--from", "00_制作简报/cards.json", cwd=root)
    assert r2.returncode == 0 and _last_json(r2)["data"]["kept"] == ["c01", "c02"]
    html2 = (root / "03_创作素材/artboard/c01/src/index.html").read_text(encoding="utf-8")
    man2 = (root / "03_创作素材/artboard/manifest.json").read_text(encoding="utf-8")
    assert html1 == html2 and man2 == (root / "03_创作素材/artboard/manifest.json").read_text(encoding="utf-8")


def test_t1_gen_cards_refuses_clobbering_without_force(tmp_path):
    """手改卡片不静默冲掉:与计划不一致即 CARD_EXISTS 非零退出,--force 才覆盖。"""
    root = _mk_proj(tmp_path)
    assert _run("rs_artboard.py", "gen-cards", "--from", "00_制作简报/cards.json", cwd=root).returncode == 0
    target = root / "03_创作素材/artboard/c01/src/index.html"
    target.write_text("<!-- 手改过的卡片 -->", encoding="utf-8")
    r = _run("rs_artboard.py", "gen-cards", "--from", "00_制作简报/cards.json", cwd=root)
    assert r.returncode == 2 and _last_json(r)["code"] == "CARD_EXISTS"
    assert target.read_text(encoding="utf-8").startswith("<!-- 手改")
    r2 = _run("rs_artboard.py", "gen-cards", "--from", "00_制作简报/cards.json",
              "--force", cwd=root)
    assert r2.returncode == 0 and "什么是 GEO" in target.read_text(encoding="utf-8")


def test_t1_gen_cards_bad_plans_are_hard_fail(tmp_path):
    root = _mk_proj(tmp_path)
    (root / "00_制作简报" / "bad1.json").write_text("[]", encoding="utf-8")
    (root / "00_制作简报" / "bad2.json").write_text(
        json.dumps([{"id": "x", "title": "t", "role": "sfx"}], ensure_ascii=False), encoding="utf-8")
    r1 = _run("rs_artboard.py", "gen-cards", "--from", "00_制作简报/bad1.json", cwd=root)
    assert r1.returncode == 2 and _last_json(r1)["code"] == "BAD_PLAN"
    # P8 同源纪律:白名单外字段直接报错,不静默吞(role 曾是幽灵字段的教训)
    r2 = _run("rs_artboard.py", "gen-cards", "--from", "00_制作简报/bad2.json", cwd=root)
    assert r2.returncode == 2 and "role" in _last_json(r2)["message"]


# ================================================================ T1-1c --export skip 判定(P6-2)+ export-fallback

def test_t1_export_must_not_skip_when_product_missing(tmp_path, monkeypatch):
    """P6-2:hash 匹配但产物不在盘 → 必须重导(旧版假 EXPORT_SKIP 漏导)。"""
    import rs_artboard as ab
    root = _mk_proj(tmp_path)
    art = _stub_artboard(tmp_path, "main")
    r = _run("rs_artboard.py", "gen-cards", "--from", "00_制作简报/cards.json", cwd=root)
    assert r.returncode == 0
    man_path = root / "03_创作素材/artboard/manifest.json"
    calls: list[str] = []

    def fake_export(item, r_, a_, timeout=900):
        calls.append(item["id"])
        out = r_ / item["output"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(PNG_1PX)
        return True, str(out)

    monkeypatch.setattr(ab, "_load_artboard_config", lambda: {"artboard_dir": str(art)})
    monkeypatch.setattr(ab, "export_item", fake_export)
    monkeypatch.setattr(sys, "argv", ["rs_artboard.py", str(man_path), "--export",
                                      "--root", str(root)])
    code, _, doc = _capture(ab.main)
    assert code == 0 and sorted(calls) == ["c01", "c02"], "产物缺失必须重导"

    # 产物在盘 + hash 未变 → 才允许 EXPORT_SKIP
    code, _, doc = _capture(ab.main)
    assert code == 0 and doc["code"] == "EXPORT_SKIP" and calls == ["c01", "c02"]

    # hash 未变但产物被删 → 再次重导(回归判定核心)
    (root / "03_创作素材/artboard/c01/export/c01.png").unlink()
    monkeypatch.setattr(sys, "argv", ["rs_artboard.py", str(man_path), "--export",
                                      "--root", str(root)])
    code, _, doc = _capture(ab.main)
    assert doc["code"] == "EXPORT_OK" and calls == ["c01", "c02", "c01"]


def _capture(fn, *args, **kw):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip().startswith("{")]
    doc = json.loads(lines[-1]) if lines else {}
    return code, buf.getvalue(), doc


def test_t1_export_fallback_engine_plumbing(tmp_path, monkeypatch):
    """export-fallback 升格(引擎管线,stub 兜底脚本):同一清单、同一产物落点、
    导完回写 sourceHash;失败卡片 → FALLBACK_PARTIAL 非零。"""
    import rs_artboard as ab
    root = _mk_proj(tmp_path)
    art = _stub_artboard(tmp_path, "fallback")
    assert _run("rs_artboard.py", "gen-cards", "--from", "00_制作简报/cards.json", cwd=root).returncode == 0
    man = root / "03_创作素材/artboard/manifest.json"
    monkeypatch.setattr(ab, "_load_artboard_config", lambda: {"artboard_dir": str(art)})
    monkeypatch.setattr(sys, "argv", ["rs_artboard.py", "export-fallback", str(man),
                                      "--root", str(root)])
    code, _, doc = _capture(ab.main)
    assert code == 0 and doc["code"] == "FALLBACK_OK"
    assert sorted(doc["data"]["exported"]) == ["c01", "c02"]
    out = root / "03_创作素材/artboard/c01/export/c01.png"
    assert out.is_file() and out.read_bytes().startswith(b"\x89PNG")
    man_doc = json.loads(man.read_text(encoding="utf-8"))
    assert all(i["sourceHash"] for i in man_doc["items"]), "兜底导出同样回写 hash"
    # 产物在盘 + hash 未变 → skip;然后弄坏引擎 → PARTIAL(退出 4)
    code, _, doc = _capture(ab.main)
    assert doc["code"] == "EXPORT_SKIP"
    (art / "scripts" / "export_fallback.py").write_text("raise SystemExit(9)\n", encoding="utf-8")
    (root / "03_创作素材/artboard/c01/export/c01.png").unlink()
    monkeypatch.setattr(sys, "argv", ["rs_artboard.py", "export-fallback", str(man),
                                      "--root", str(root), "--only", "c01"])
    code, _, doc = _capture(ab.main)
    assert code == 4 and doc["code"] == "FALLBACK_PARTIAL" and doc["data"]["failed"][0]["id"] == "c01"


# ================================================================ T1-1b add-overlay

def test_t1_add_overlay_mounts_track_usedin_and_manual_edit(tmp_path):
    """O2 升格主链路:挂轨 + usedIn 回写 + manualEdit 标记,IR 校验过、原子落盘。"""
    root = _mk_proj(tmp_path)
    assert _run("rs_artboard.py", "gen-cards", "--from", "00_制作简报/cards.json", cwd=root).returncode == 0
    for cid in ("c01", "c02"):
        out = root / "03_创作素材/artboard" / cid / "export" / f"{cid}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(PNG_1PX)
    r = _run("rs_ir.py", "add-overlay", "05_时间线工程/project.json",
             "--manifest", "03_创作素材/artboard/manifest.json",
             "--plan", "00_制作简报/cards.json", cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr
    doc = _last_json(r)
    assert doc["ok"] and doc["code"] == "OVERLAY_OK"
    ir = json.loads((root / "05_时间线工程/project.json").read_text(encoding="utf-8"))
    overlay = [t for t in ir["tracks"] if t.get("name") == "overlay"]
    assert len(overlay) == 1 and overlay[0]["kind"] == "video"
    clips = overlay[0]["clips"]
    assert [c["startMs"] for c in clips] == [3200, 5000], "按 startMs 升序"
    assert clips[0]["motion"] == {"in": "fadeIn", "inMs": 400}
    assert clips[0]["src"] == "03_创作素材/artboard/c01/export/c01.png"
    assert ir["_meta"]["manualEdit"] is True
    man = json.loads((root / "03_创作素材/artboard/manifest.json").read_text(encoding="utf-8"))
    used = {i["id"]: i.get("usedIn") for i in man["items"]}
    assert used["c01"] == [{"track": 1, "clipIndex": 0, "startMs": 3200, "durationMs": 1800}]

    # 重复挂轨:默认拒绝;--replace 换内容且清陈旧 usedIn
    r2 = _run("rs_ir.py", "add-overlay", "05_时间线工程/project.json",
              "--manifest", "03_创作素材/artboard/manifest.json",
              "--plan", "00_制作简报/cards.json", cwd=root)
    assert r2.returncode == 2 and _last_json(r2)["code"] == "OVERLAY_ISSUES"
    (root / "00_制作简报" / "swap.json").write_text(json.dumps([
        {"card": "c02", "startMs": 100, "durationMs": 900}], ensure_ascii=False), encoding="utf-8")
    r3 = _run("rs_ir.py", "add-overlay", "05_时间线工程/project.json",
              "--manifest", "03_创作素材/artboard/manifest.json",
              "--plan", "00_制作简报/swap.json", "--replace", cwd=root)
    assert r3.returncode == 0
    ir = json.loads((root / "05_时间线工程/project.json").read_text(encoding="utf-8"))
    ov = next(t for t in ir["tracks"] if t.get("name") == "overlay")
    assert len(ov["clips"]) == 1 and ov["clips"][0]["startMs"] == 100
    man = json.loads((root / "03_创作素材/artboard/manifest.json").read_text(encoding="utf-8"))
    used = {i["id"]: i.get("usedIn", []) for i in man["items"]}
    assert used["c01"] == [], "被换下的卡不得留陈旧挂点"
    assert used["c02"] and used["c02"][0]["clipIndex"] == 0


def test_t1_add_overlay_hard_fails_on_missing_product_and_overlap(tmp_path):
    root = _mk_proj(tmp_path)
    assert _run("rs_artboard.py", "gen-cards", "--from", "00_制作简报/cards.json", cwd=root).returncode == 0
    # 产物不在盘 → 硬失败且不落盘
    r = _run("rs_ir.py", "add-overlay", "05_时间线工程/project.json",
             "--manifest", "03_创作素材/artboard/manifest.json",
             "--plan", "00_制作简报/cards.json", cwd=root)
    assert r.returncode == 2 and _last_json(r)["code"] == "OVERLAY_ISSUES"
    assert "产物不存在" in _last_json(r)["data"]["issues"][0]
    assert "manualEdit" not in (root / "05_时间线工程/project.json").read_text(encoding="utf-8")
    # 产物补齐后:时间窗重叠 → 硬失败(重叠检测在产物校验之后仍会拦)
    for cid in ("c01", "c02"):
        out = root / "03_创作素材/artboard" / cid / "export" / f"{cid}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(PNG_1PX)
    (root / "00_制作简报" / "overlap.json").write_text(json.dumps([
        {"card": "c01", "startMs": 1000, "durationMs": 5000},
        {"card": "c02", "startMs": 2000, "durationMs": 1000}], ensure_ascii=False), encoding="utf-8")
    r2 = _run("rs_ir.py", "add-overlay", "05_时间线工程/project.json",
              "--manifest", "03_创作素材/artboard/manifest.json",
              "--plan", "00_制作简报/overlap.json", cwd=root)
    assert r2.returncode == 2 and _last_json(r2)["code"] == "OVERLAY_ISSUES"
    assert any("重叠" in s for s in _last_json(r2)["data"]["issues"])
    assert "manualEdit" not in (root / "05_时间线工程/project.json").read_text(encoding="utf-8")


# ================================================================ T1-2 Hard Rule

def test_t1_hard_rule_and_antipattern_written():
    skill = (REPO / "skills/cutflow/SKILL.md").read_text(encoding="utf-8")
    assert "禁止为一次性任务现写剪辑逻辑脚本" in skill
    assert "给定输入必得同一输出" in skill and "capabilities.json" in skill
    assert "为一次性任务现写剪辑逻辑脚本" in skill.split("## 9. 反模式")[1], "反模式清单须同口径"


# ================================================================ T2-1 能力目录 + T3-1a 对拍

def test_t2_capabilities_matches_regeneration():
    """防漂移门禁:再生成 → 与盘上逐字节一致;手抄/手改必红。"""
    assert CAPABILITIES.is_file(), "capabilities.json 必须在盘上(先 rs_caps.py generate)"
    disk = CAPABILITIES.read_text(encoding="utf-8")
    assert disk == rs_caps.dumps_catalog(rs_caps.build_catalog()), (
        "能力目录漂移:只允许 rs_caps.py generate 再生成,禁止手改")


def test_t2_caps_check_cli_detects_drift(tmp_path):
    """rs_caps check 同一门禁的 CLI 形态:漂移退出 2。"""
    import shutil
    work = tmp_path / "caps.json"
    shutil.copy(CAPABILITIES, work)
    doc = json.loads(work.read_text(encoding="utf-8"))
    doc["tools"][0]["purpose"] = "手抄改过的用途"
    work.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    r = subprocess.run([sys.executable, str(SCRIPTS / "rs_caps.py"), "check", "--out", str(work)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 2 and "CAPS_DRIFT" in r.stdout


def test_t3_every_declared_command_is_argparse_accepted():
    """T3-1a:目录声明的每条命令,probe 必须被对应 argparse 接受(与手册门禁同一重放器)。"""
    doc = json.loads(CAPABILITIES.read_text(encoding="utf-8"))
    bad = []
    for t in doc["tools"]:
        for c in t["commands"]:
            ok, note = gate.validate_one(t["script"], c["probe"])
            if not ok:
                bad.append(f"{c['name']} probe={c['probe']} ← {note}")
    assert not bad, "capabilities ↔ argparse 对拍失败:" + ";".join(bad)


def test_t3_catalog_covers_every_argparse_script():
    """有 argparse 装配的 rs_*.py 必须全部进目录(新增命令不得逃逸能力目录)。"""
    doc = json.loads(CAPABILITIES.read_text(encoding="utf-8"))
    listed = {t["script"] for t in doc["tools"]}
    missing = []
    for p in sorted(SCRIPTS.glob("rs_*.py")):
        if p.name in rs_caps.EXCLUDED:
            continue
        try:
            rs_caps.build_parser(p)
        except Exception:  # noqa: BLE001 — 无 argparse 的辅助库不算
            continue
        if p.name not in listed:
            missing.append(p.name)
    assert not missing, f"未进能力目录的命令:{missing}(rs_caps.py generate 再生成)"


def test_t2_capabilities_is_short_and_machine_readable():
    """目录要短:每工具一行用途 + 参数摘要;总条目量有上界,防目录长成要读的文件。"""
    doc = json.loads(CAPABILITIES.read_text(encoding="utf-8"))
    assert doc["version"] == 1 and isinstance(doc["tools"], list) and doc["tools"]
    for t in doc["tools"]:
        assert set(t) >= {"script", "purpose", "stage", "outputs", "gate", "args", "commands"}
        assert len(t["purpose"]) <= 120
    n_cmds = sum(len(t["commands"]) for t in doc["tools"])
    assert n_cmds >= 40, f"命令覆盖异常地少:{n_cmds}"
    raw = CAPABILITIES.read_text(encoding="utf-8")
    # M8 五能力脚本(rs_beat/rs_shot/rs_reframe/rs_broll/rs_screen)入册后 1235 行;
    # 上界随工具数同比例放宽(1300 ≈ 每 rs_* 脚本 ≤35 行),预算纪律不变:细节归 --help
    assert len(raw.splitlines()) < 1300, "能力目录过长(细节应留给 --help)"


# ================================================================ 端到端:复现「昨天任务」不写新脚本

def test_t1_full_chain_without_ad_hoc_scripts(tmp_path):
    """验收判据(T1):gen-cards → export-fallback → add-overlay 全程官方子命令,
    真浏览器截图(真实兜底引擎),Agent 不写任何新脚本。"""
    pytest.importorskip("playwright", reason="本机无 playwright,真浏览器链路跳过")
    import rs_artboard as ab
    art_dir = Path((ab._load_artboard_config().get("artboard_dir") or ""))
    if not (art_dir / "scripts" / "export_fallback.py").is_file():
        pytest.skip(f"本机未配置 artboard 兜底导出脚本:{art_dir}")
    root = _mk_proj(tmp_path)
    r1 = _run("rs_artboard.py", "gen-cards", "--from", "00_制作简报/cards.json", cwd=root)
    assert r1.returncode == 0, r1.stderr
    r2 = _run("rs_artboard.py", "export-fallback", "03_创作素材/artboard/manifest.json",
              cwd=root)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    png = root / "03_创作素材/artboard/c01/export/c01.png"
    assert png.is_file() and len(png.read_bytes()) > 1000, "真实截图应为非平凡 PNG"
    r3 = _run("rs_ir.py", "add-overlay", "05_时间线工程/project.json",
              "--manifest", "03_创作素材/artboard/manifest.json",
              "--plan", "00_制作简报/cards.json", cwd=root)
    assert r3.returncode == 0, r3.stdout + r3.stderr
    ir = json.loads((root / "05_时间线工程/project.json").read_text(encoding="utf-8"))
    assert ir["_meta"]["manualEdit"] is True
