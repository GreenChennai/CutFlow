# -*- coding: utf-8 -*-
"""M12 素材库门禁(ADR-0053 / 分册01 §8):

①清单完整性:四许可字段齐全、file 在盘、sha256 匹配、id 命名法合法且唯一、
  aiGenerated(R45)在场、时长实测、体积预算(R38:音效 ≤200KB / BGM ≤3MB / 元素 ≤300KB);
②商用合规:commercial:false 的素材不得被风格包/提示词模板引用(R42);
③索引门禁:`rs_asset.py check --strict` 退出码 0;
④归因一致:工程引用 → 归因清单 → rs_verify L0 双向对拍;
⑤add/scan 原子写与三态对账(在 tmp 沙箱,不污染真实索引)。

运行:pytest tests/test_assets.py -q
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_asset  # noqa: E402
import rs_common  # noqa: E402
import rs_verify  # noqa: E402

MANIFEST = REPO / "skills" / "cutflow" / "assets" / "manifest.json"
ASSETS = MANIFEST.parent


# ================================================================ ① 清单完整性

def _assets() -> list[dict]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["assets"]


def test_manifest_four_license_fields_complete():
    """每条素材四许可字段(source/license/commercial/attribution)缺一即失败(硬约束)。"""
    for a in _assets():
        for f in rs_asset.LICENSE_FIELDS:
            v = a.get(f)
            assert v is not None and v != "", f"{a.get('id')} 缺许可字段 {f}"
        assert isinstance(a.get("commercial"), bool), f"{a['id']} commercial 非布尔"
        assert isinstance(a.get("aiGenerated"), bool), f"{a['id']} 缺 aiGenerated(R45)"


def test_manifest_ids_unique_and_wellformed():
    ids = [str(a.get("id", "")) for a in _assets()]
    assert len(ids) == len(set(ids)), "素材 id 必须唯一"
    import re
    for i in ids:
        segs = i.split(".")
        assert len(segs) >= 3 and segs[0] in rs_asset.KINDS, f"id 命名法非法:{i}"
        assert re.fullmatch(r"[a-z0-9_.]+", i), f"id 必须 ASCII 小写:{i}"


def test_manifest_files_exist_and_sha_matches():
    for a in _assets():
        p = ASSETS / str(a["file"])
        assert p.is_file(), f"{a['id']} file 不存在:{a['file']}"
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        assert a.get("sha256") == digest, f"{a['id']} sha256 与盘面不符"


def test_manifest_size_budgets():
    """R38 体积预算:音效单条 ≤200KB / BGM ≤3MB / 元素单图 ≤300KB。"""
    for a in _assets():
        p = ASSETS / str(a["file"])
        budget = rs_asset.SIZE_BUDGET.get(a.get("kind"))
        if budget and p.is_file():
            assert p.stat().st_size <= budget, \
                f"{a['id']} 超体积预算:{p.stat().st_size}B > {budget}B"
    # 元素三档变体同样受预算约束
    for a in _assets():
        for v in (a.get("variants") or []):
            vp = ASSETS / str(v)
            if vp.is_file():
                assert vp.stat().st_size <= 300 * 1024, f"{a['id']} 变体超预算:{v}"


def test_manifest_durations_measured_and_counts_pass_acceptance():
    counts: dict[str, int] = {}
    for a in _assets():
        counts[a["kind"]] = counts.get(a["kind"], 0) + 1
        if a["kind"] in ("sfx", "bgm"):
            assert int(a.get("durationMs") or 0) > 0, \
                f"{a['id']} durationMs 必须实测(sfx/bgm 禁止估算)"
    # 分册01 §8.2 验收线
    assert counts.get("sfx", 0) >= 40, counts
    assert counts.get("element", 0) >= 48, counts
    assert counts.get("huazi", 0) >= 14, counts
    assert counts.get("bgm", 0) >= 16, counts


def test_manifest_ai_generated_honesty():
    """R45:自产素材 aiGenerated=true;外部来源(Mixkit)必须 false —— 不虚报。"""
    for a in _assets():
        if a.get("source") == "自产":
            assert a["aiGenerated"] is True, f"{a['id']} 自产素材未标 aiGenerated"
        elif a.get("source"):
            assert a["aiGenerated"] is False, \
                f"{a['id']} 外部来源({a.get('source')})不得标 aiGenerated"


def test_elements_are_transparent_png_no_seq_bleeding():
    """元素技术规格:静态 PNG 带 alpha 通道;序列帧全 finite(帧数固定)。"""
    from PIL import Image
    seqs = [a for a in _assets() if a.get("seqDir")]
    assert len(seqs) >= 6, "数据可视化入场序列帧 ≥6 组"
    for a in seqs:
        d = ASSETS / str(a["seqDir"])
        frames = sorted(d.glob("*.png"))
        assert len(frames) == int(a["frameCount"]), f"{a['id']} 序列帧数不符(必须 finite)"
    pngs = [a for a in _assets()
            if a["kind"] == "element" and str(a["file"]).endswith(".png")]
    sample = pngs[:8]
    for a in sample:
        im = Image.open(ASSETS / str(a["file"]))
        assert im.mode == "RGBA", f"{a['id']} 静态元素必须透明 PNG(RGBA)"


# ================================================================ ② 商用合规(R42)

def test_no_noncommercial_asset_referenced_by_stylepacks(monkeypatch):
    """commercial:false 的素材被风格包/模板引用 → check 必须红。"""
    bad = [a for a in _assets() if a["id"] == "sfx.whoosh.01"]
    assert bad, "前置:sfx.whoosh.01 在索引"
    doc = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for a in doc["assets"]:
        if a["id"] == "sfx.whoosh.01":
            a["commercial"] = False
    monkeypatch.setattr(rs_asset, "_scan_stylepack_references",
                        lambda: [("styles/packs/x/params.yaml", "sfx.whoosh.01")])
    res = rs_asset.check_manifest(doc)
    assert not res["ok"], "不可商用素材被风格包引用必须门禁红"
    assert any("不可商用" in e for e in res["errors"]), res["errors"]
    # 无引用时:登记本身允许(只 WARN 不得入交付),不算 error
    monkeypatch.setattr(rs_asset, "_scan_stylepack_references", lambda: [])
    res2 = rs_asset.check_manifest(doc)
    assert res2["ok"] and any("commercial:false" in w for w in res2["warnings"])


def test_stylepack_reference_scan_finds_real_ids():
    """真实风格包中登记的素材 id 都能解析且可商用(现状健康对拍)。"""
    refs = rs_asset._scan_stylepack_references()
    for _src, ref in refs:
        hit = rs_asset.find(ref)
        assert hit is not None, f"风格包/模板引用了不存在的素材:{ref}"
        assert hit.get("commercial") is True, f"引用了不可商用素材:{ref}"


# ================================================================ ③ 索引门禁 CLI

def _run_cli(*args: str) -> tuple[int, dict]:
    r = subprocess.run([sys.executable, str(SCRIPTS / "rs_asset.py"), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip().startswith("{")]
    return r.returncode, (json.loads(lines[-1]) if lines else {})


def test_cli_check_strict_exit_zero():
    code, doc = _run_cli("check", "--strict")
    assert code == 0 and doc["ok"] is True, doc.get("errors")
    assert doc["data"]["counts"]["sfx"] >= 40


def test_cli_list_search_get():
    code, doc = _run_cli("list", "--kind", "bgm", "--json")
    assert code == 0 and len(doc["data"]["assets"]) >= 16
    code, doc = _run_cli("list", "--usage", "transition", "--json")
    assert code == 0 and all("transition" in a["usage"] for a in doc["data"]["assets"])
    code, doc = _run_cli("search", "转场", "--kind", "sfx", "--limit", "3", "--json")
    assert code == 0 and 1 <= len(doc["data"]["assets"]) <= 3
    code, doc = _run_cli("get", "sfx.whoosh.01")
    assert code == 0
    assert Path(doc["data"]["absPath"]).is_file(), "get 必须返回可用的落盘引用"
    # 旧组名兼容:assets_sfx:riser 时代的老引用仍可解析
    code, doc = _run_cli("get", "riser")
    assert code == 0 and doc["data"]["id"] == "sfx.riser.01"
    code, doc = _run_cli("get", "sfx.nope.99")
    assert code == 2 and doc["code"] == "ASSET_NOT_FOUND"


# ================================================================ ④ 归因一致

def _mk_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    for d in ("00_制作简报", "05_时间线工程"):
        (root / d).mkdir(parents=True)
    return root


def test_attribution_matches_references_bidirectionally(tmp_path):
    """引用 → 归因清单 → rs_verify.check_assets 三方一致;商用判据生效。"""
    root = _mk_project(tmp_path)
    ir = {"version": 1, "canvas": {"width": 1080, "height": 1920},
          "tracks": [{"kind": "video", "name": "main", "clips": []},
                     {"kind": "audio", "clips": [
                         {"src": "assets_sfx:whoosh", "startMs": 0, "role": "sfx"},
                         {"src": "assets_sfx:ding", "startMs": 1200, "role": "sfx"}]}]}
    (rs_paths_project_json(root)).write_text(json.dumps(ir, ensure_ascii=False),
                                             encoding="utf-8")
    refs = rs_asset.collect_project_refs(root)
    assert {r["id"] for r in refs} == {"sfx.whoosh.01", "sfx.ding.01"}
    md = rs_asset.build_attribution_md(root, refs)
    assert rs_asset.asset_ids_in(md) == {r["id"] for r in refs}, "归因条目必须与引用一致"
    assert "Mixkit" in md and "Mixkit Sound Effects Free License" in md
    assert "可商用" in md or "| 是 |" in md, "归因清单必须标注可商用性"
    # rs_verify L0 判据:归因文件落位后对拍绿
    (rs_paths_deliver(root)).mkdir(parents=True, exist_ok=True)
    (rs_paths_deliver(root) / "说明书").mkdir(exist_ok=True)
    (rs_paths_deliver(root) / "说明书" / "素材归因.md").write_text(md, encoding="utf-8")
    res = rs_verify.check_assets(root)
    assert res["ok"] is True, res.get("problems")
    # 篡改归因(删一条)→ 双向对拍红
    broken = md.replace("`sfx.whoosh.01`", "`sfx.swipe.01`")
    (rs_paths_deliver(root) / "说明书" / "素材归因.md").write_text(broken, encoding="utf-8")
    res2 = rs_verify.check_assets(root)
    assert res2["ok"] is False and any("归因" in p for p in res2["problems"])


def test_verify_assets_skips_projects_without_refs(tmp_path):
    root = _mk_project(tmp_path)
    res = rs_verify.check_assets(root)
    assert res["ok"] is True and res.get("skipped"), "无引用工程必须 skipped 不误伤"


def rs_paths_project_json(root: Path) -> Path:
    return root / "05_时间线工程" / "project.json"


def rs_paths_deliver(root: Path) -> Path:
    return root / "成品"


# ================================================================ ⑤ add / scan(沙箱)

def _call_main(argv: list[str]) -> tuple[int, dict]:
    """in-process 调 rs_asset.main(monkeypatch 生效的前提;test_markers 同法)。"""
    import contextlib
    import io
    old = sys.argv
    sys.argv = ["rs_asset.py", *argv]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = rs_asset.main()
    finally:
        sys.argv = old
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip().startswith("{")]
    return code, (json.loads(lines[-1]) if lines else {})


def test_add_and_scan_are_atomic_and_deterministic(tmp_path, monkeypatch):
    """add/scan 在索引副本上登记新素材:新增/变更/移除三态,绝不碰真实索引。"""
    sandbox = tmp_path / "assets"
    shutil.copytree(ASSETS, sandbox)
    real_manifest = MANIFEST.read_text(encoding="utf-8")
    monkeypatch.setattr(rs_asset, "ASSETS_DIR", sandbox)
    monkeypatch.setattr(rs_asset, "MANIFEST_PATH", sandbox / "manifest.json")
    src = tmp_path / "my_tone.mp3"
    shutil.copyfile(sandbox / "sfx" / "tick_01.mp3", src)
    lic = ["--source", "自产", "--license", "自产", "--attribution", "测试,无需归因"]

    code, doc = _call_main(["add", "--kind", "sfx", "--file", str(src),
                            "--group", "mytone", "--label", "测试音", "--tags", "测试",
                            "--usage", "ui", *lic])
    assert code == 0 and doc["data"]["action"] == "added", doc
    assert doc["data"]["asset"]["id"] == "sfx.mytone.01"
    assert (sandbox / "sfx" / "my_tone.mp3").is_file()
    # 真实索引未被触碰(原子写只发生在沙箱副本)
    assert MANIFEST.read_text(encoding="utf-8") == real_manifest
    # 变更:同文件名不同内容 → changed
    shutil.copyfile(sandbox / "sfx" / "tap_01.mp3", src)
    code, doc = _call_main(["add", "--kind", "sfx", "--file", str(src),
                            "--group", "mytone", "--label", "测试音", "--tags", "测试",
                            "--usage", "ui", *lic])
    assert code == 0 and doc["data"]["action"] == "changed", doc
    # scan:目录批量登记(added)
    bulk = tmp_path / "bulk"
    bulk.mkdir()
    shutil.copyfile(sandbox / "sfx" / "tick_01.mp3", bulk / "mytick_01.mp3")
    code, doc = _call_main(["scan", str(bulk), "--kind", "sfx", "--tags", "批量",
                            "--usage", "ui", *lic])
    assert code == 0 and doc["data"]["stats"]["added"] == 1, doc
    # 移除:删盘面文件 → scan 三态对账出 removed
    (sandbox / "sfx" / "mytick_01.mp3").unlink()
    code, doc = _call_main(["scan", str(bulk), "--kind", "sfx", "--tags", "批量",
                            "--usage", "ui", *lic])
    assert code == 0 and doc["data"]["stats"]["removed"] == 1, doc


def test_legacy_projects_without_manifest_fall_back(monkeypatch, tmp_path):
    """ADR-0053 回滚档:manifest 缺失 → 旧 7 名字仍解析(硬编码兜底不删)。"""
    monkeypatch.setattr(rs_asset, "MANIFEST_PATH", tmp_path / "absent.json")
    p = rs_asset.resolve_sfx_ref("whoosh")
    assert p is not None and p.is_file(), "无索引时旧名必须走 assets/sfx 兜底"
