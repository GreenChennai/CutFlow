# -*- coding: utf-8 -*-
"""成品/ 交付区门禁(ADR-0052 · M2 成品/半成品分离;方案 §7.1 门禁 10/11/12、§7.3 M2)。

① 只读区断言:成品/ 内出现工程文件(project/wordline/cutlist/notes)+ *.draft_content
   + rebuild.py 必红,publish 前置自检拒发(READONLY_ZONE_VIOLATED 非零退出);
② --publish 幂等:重复执行文件集不变;旧 成品/ 整区备份;发布流水留痕;
③ 缺项非零退出 DELIVERABLES_INCOMPLETE(既有真对账行为保留,publish 同口径);
④ rs_cleanup 永不触碰 成品/(rs_paths.NEVER_CLEAN 硬过滤,逐字节未动);
⑤ 半成品入口.md 指向的路径存在性(指针文件,不复制工程);
⑥ safeArea 硬校验(rs_verify L0 判据,清欠 #A6:界内绿/界外红)+ 对账项接线;
⑦ 剪映单向出口:草稿落点 = 05_时间线工程/导出/剪映59/(rs_paths.jianying_draft),
   root_meta 注册仍在剪映草稿根(首页可见)。

运行:pytest tests/test_deliverables.py -q
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "tests"))

import check_manual_cmds as gate  # noqa: E402
import rs_cleanup  # noqa: E402
import rs_common  # noqa: E402
import rs_ingest  # noqa: E402
import rs_paths  # noqa: E402
import rs_verify  # noqa: E402


def _capture(fn, *args, **kw):
    """跑 main() 抓 stdout 的最后一个 JSON 契约(与 test_v19 同一手法)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    doc = json.loads(lines[-1]) if lines else {}
    return code, buf.getvalue(), doc


def _tree_hash(root: Path) -> dict:
    """目录树逐文件 hash(清理保护断言用:成品/ 逐字节未动)。"""
    out: dict[str, str] = {}
    for f in sorted(root.rglob("*")):
        if f.is_file():
            out[f.relative_to(root).as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def _png(w: int, h: int) -> bytes:
    """最小合法 PNG(仅 IHDR 头,尺寸真实可读;封面尺寸对账用)。"""
    ihdr = struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + ihdr
            + b"\x00\x00\x00\x00IEND\xaeB`\x82")


ASS_TMPL = """[Script Info]
Title: CutFlow subtitles
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,Microsoft YaHei,78,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,0,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Main,,0,0,0,,大家好
Dialogue: 0,0:00:02.10,0:00:04.00,Main,,0,0,0,,今天讲蓝屏
"""


def _mk_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    for d in ("00_制作简报", "01_原始素材", "05_时间线工程", "06_成片输出", "_内部状态"):
        (root / d).mkdir(parents=True)
    return root


def _complete(root: Path, *, margin_v: int = 500, outputs: list[str] | None = None,
              cover_wh: tuple[int, int] = (1080, 1920),
              degraded: bool = False) -> Path:
    """齐装夹具:两路成片 + 字幕对 + 封面 + 文案 + 变体矩阵 + 声明平台 douyin。"""
    out = root / "06_成片输出"
    (out / "final").mkdir(exist_ok=True)
    (out / "final" / "final_a_916.mp4").write_bytes(b"v")
    (out / "branded").mkdir(exist_ok=True)
    (out / "branded" / "成片_9x16_logoA_final.mp4").write_bytes(b"v")
    for n in ("subtitles.ass", "master.srt", "sync_report.md"):
        (out / n).write_text("x" if n != "subtitles.ass" else ASS_TMPL.format(margin_v=margin_v),
                             encoding="utf-8")
    (out / rs_common.COVER_PNG).write_bytes(_png(*cover_wh))
    (out / "metadata.json").write_text(json.dumps(
        {"version": 1, "platforms": {"douyin": {"title": "标题", "desc": "简介", "tags": ["#a"]}}},
        ensure_ascii=False), encoding="utf-8")
    (root / "05_时间线工程" / "project.json").write_text(json.dumps(
        {"version": 1, "slug": "proj", "canvas": {"width": 1080, "height": 1920},
         "outputs": outputs if outputs is not None else ["9x16"]}, ensure_ascii=False),
        encoding="utf-8")
    (root / "05_时间线工程" / "variants.json").write_text(json.dumps(
        {"matrix": [{"id": "logoA_9x16", "logo": "logoA", "ratio": "9x16"}]},
        ensure_ascii=False), encoding="utf-8")
    (root / "05_时间线工程" / "pipeline.json").write_text(json.dumps(
        {"params": {"platform": "douyin", "ratio": "9x16"}}, ensure_ascii=False),
        encoding="utf-8")
    if degraded:
        (root / "05_时间线工程" / "wordline.json").write_text(json.dumps(
            {"chars": [], "degraded": True,
             "degradeReasons": ["ASR 不可用,退回粗对齐"]}, ensure_ascii=False),
            encoding="utf-8")
    return root


def _publish(root: Path):
    """argv 注入跑 deliverables --publish,返回 (退出码, data 契约,code 并入)。"""
    old = sys.argv
    sys.argv = ["rs_ingest.py", "deliverables", str(root), "--publish"]
    try:
        code, _, doc = _capture(rs_ingest.main)
        return code, {**(doc.get("data") or {}), "code": doc.get("code")}
    finally:
        sys.argv = old


def _published_files(root: Path) -> list[str]:
    deliver = root / rs_paths.p("deliver")
    return sorted(p.relative_to(deliver).as_posix() for p in deliver.rglob("*") if p.is_file())


# ================================================================ ① 只读区断言

@pytest.mark.parametrize("name", ["project.json", "wordline.json", "cutlist.json",
                                  "notes.json", "rebuild.py", "草稿.draft_content.json"])
def test_readonly_zone_forbids_project_files(tmp_path, monkeypatch, name):
    """§7.1 门禁 10:工程文件/剪映草稿/重建脚本混进 成品/ 必红;publish 拒发不代删。"""
    root = _complete(_mk_project(tmp_path))
    deliver = root / rs_paths.p("deliver")
    deliver.mkdir()
    (deliver / name).write_text("{}", encoding="utf-8")
    assert rs_ingest.readonly_zone_violations(deliver) == [name]
    monkeypatch.setattr(sys, "argv", ["rs_ingest.py", "deliverables", str(root), "--publish"])
    code, _, doc = _capture(rs_ingest.main)
    assert code == 4 and doc["code"] == "READONLY_ZONE_VIOLATED", doc
    assert (deliver / name).is_file(), "拒发时绝不代删用户文件"


def test_readonly_zone_clean_is_silent(tmp_path):
    root = _mk_project(tmp_path)
    assert rs_ingest.readonly_zone_violations(root / rs_paths.p("deliver")) == []
    (root / rs_paths.p("deliver")).mkdir()
    (root / rs_paths.p("deliver") / "对账.md").write_text("x", encoding="utf-8")
    assert rs_ingest.readonly_zone_violations(root / rs_paths.p("deliver")) == []


# ================================================================ ② publish 幂等 + 备份

def test_publish_layout_and_idempotent(tmp_path):
    """§7.1 门禁 11:--publish 按 ADR-0052 清单出树;重复执行文件集不变、逐字节稳定;
    旧 成品/ 整区备份;发布流水(hash)落 _内部状态/publish.json。"""
    root = _complete(_mk_project(tmp_path))
    code1, doc = _publish(root)  # 首次发布
    assert code1 == 0 and doc["code"] == "PUBLISH_OK", doc
    files1 = _published_files(root)
    for need in ("对账.md", "半成品入口.md", "说明书/交付说明书.md",
                 "字幕/subtitles.ass", "字幕/master.srt", "封面/" + rs_common.COVER_PNG,
                 "文案/metadata.json"):
        assert need in files1, f"成品/ 缺 {need}"
    assert any(n.startswith("成片/") and n.endswith(".mp4") for n in files1)
    # 拷贝不是移动:工程区母版原样保留
    assert (root / "06_成片输出" / "final" / "final_a_916.mp4").is_file()

    rec1 = (root / rs_paths.p("deliver") / "对账.md").read_text(encoding="utf-8")
    hash1 = _tree_hash(root / rs_paths.p("deliver"))

    code2, doc2 = _publish(root)  # 重复发布
    assert code2 == 0 and doc2["code"] == "PUBLISH_OK"
    assert _published_files(root) == files1, "重复 --publish 不得产生重复/多余文件"
    assert (root / rs_paths.p("deliver") / "对账.md").read_text(encoding="utf-8") == rec1, \
        "对账.md 必须无时间戳(幂等)"
    assert _tree_hash(root / rs_paths.p("deliver")) == hash1, "成品/ 应逐字节稳定"
    assert doc2["backup"], "第二次发布必须先备份旧 成品/"
    backup = Path(doc2["backup"])
    assert backup.parent == root / rs_paths.p("state") / "backup"
    assert backup.name.startswith("publish-") and (backup / "对账.md").is_file()
    ledger = json.loads((root / rs_paths.p("state") / "publish.json").read_text(encoding="utf-8"))
    assert set(ledger["files"]) == set(files1), "发布流水必须与成品文件集一致"
    assert all(len(v) == 64 for v in ledger["files"].values()), "流水逐文件 sha256 留痕"


def test_publish_collects_legacy_toplevel_outputs(tmp_path):
    """旧工程交付物散在 06_成片输出 顶层 → publish 照常归集(只读,不改旧结构)。"""
    root = _mk_project(tmp_path)
    out = root / "06_成片输出"
    (out / "final_a.mp4").write_bytes(b"v")          # 顶层散落成片(旧结构)
    for n in ("subtitles.ass", "master.srt", "sync_report.md"):
        (out / n).write_text("x", encoding="utf-8")
    (out / rs_common.COVER_PNG).write_bytes(_png(1080, 1920))
    (out / "metadata.json").write_text(json.dumps(
        {"platforms": {"douyin": {"title": "t", "desc": "d", "tags": []}}},
        ensure_ascii=False), encoding="utf-8")
    code, doc = _publish(root)
    assert code == 0, doc
    assert "成片/final_a.mp4" in _published_files(root)
    assert (out / "final_a.mp4").is_file(), "归集是只读拷贝,不动旧结构"


# ================================================================ ③ 缺项非零退出

def test_incomplete_project_fails_and_marks_red(tmp_path):
    """§7.1 门禁 11:缺项 → DELIVERABLES_INCOMPLETE 非零退出,对账.md 红标点名;
    既有 deliverables(不带 --publish)行为保留。"""
    root = _mk_project(tmp_path)
    (root / "06_成片输出" / "final_a.mp4").write_bytes(b"v")
    # 基线(既有行为):缺封面/字幕/文案 → 非零
    base = rs_ingest.build_deliverables(root)
    assert base["ok"] is False and base["code"] == "DELIVERABLES_INCOMPLETE"
    # publish 同口径:对账不齐非零退出,但红标照写
    code, doc = _publish(root)
    assert code == 4 and doc["code"] == "DELIVERABLES_INCOMPLETE", doc
    rec = (root / rs_paths.p("deliver") / "对账.md").read_text(encoding="utf-8")
    assert "缺失项" in rec and "- ⚠" in rec
    assert rs_common.COVER_PNG in rec, "对账必须点名缺的封面"


def test_declared_outputs_reconciled_against_videos(tmp_path):
    """§4.3 对账项①:画幅集合与 brief.outputs(project.json.outputs)一致。"""
    root = _complete(_mk_project(tmp_path), outputs=["9x16", "3x4"])
    code, doc = _publish(root)
    assert code == 4 and doc["code"] == "DELIVERABLES_INCOMPLETE", doc
    assert any("3x4" in m for m in doc["missing"]), doc["missing"]
    # 补上 3x4 成片 → 对账齐
    (root / "06_成片输出" / "branded" / "成片_3x4_logoA_final.mp4").write_bytes(b"v")
    code2, doc2 = _publish(root)
    assert code2 == 0 and not doc2["missing"], doc2


# ================================================================ ④ cleanup 永不触碰 成品/

def test_cleanup_never_touches_deliver_dir(tmp_path):
    """§7.1 门禁 12:对含 成品/ 的工程跑 cleanup --apply,成品/ 逐字节未动;
    即便 成品/ 内部有通配可命中的 *.tmp 也不进删除名单(NEVER_CLEAN 硬过滤)。"""
    root = _complete(_mk_project(tmp_path))
    code, _doc = _publish(root)
    assert code == 0
    deliver = root / rs_paths.p("deliver")
    (deliver / "x.tmp").write_text("junk", encoding="utf-8")   # 通配 glob 本应命中
    out = root / "06_成片输出"
    (out / "_build").mkdir(exist_ok=True)                      # 必删中间件
    (out / "_build" / "t.json").write_text("{}", encoding="utf-8")
    (out / "dev-probe.tmp").write_text("x", encoding="utf-8")
    (root / "99_试算").mkdir()
    (root / "99_试算" / "a.mp4").write_bytes(b"v")
    before = _tree_hash(deliver)

    delete, _keep = rs_cleanup.classify(root)
    assert not [d for d in delete if d == deliver or deliver in d.parents], \
        "成品/ 及其内部任何路径都不得进删除名单"
    old = sys.argv
    sys.argv = ["rs_cleanup.py", str(root), "--apply"]
    try:
        code2, _, doc2 = _capture(rs_cleanup.main)
    finally:
        sys.argv = old
    assert code2 == 0 and doc2["code"] == "CLEANUP_OK", doc2
    assert _tree_hash(deliver) == before, "成品/ 必须逐字节未动"
    assert (out / "final" / "final_a_916.mp4").is_file(), "工程区交付物照旧保留"
    assert not (out / "_build").exists() and not (root / "99_试算").exists()


# ================================================================ ⑤ 半成品入口指针

def test_pointer_targets_exist_and_engine_not_copied(tmp_path):
    """半成品入口.md 指向的路径必须真实存在;且只做指针,不复制工程(单一真相源)。"""
    root = _complete(_mk_project(tmp_path))
    jy = root / "05_时间线工程" / "导出" / "剪映59" / "dev_jy"
    jy.mkdir(parents=True)
    (jy / "draft_content.json").write_text("{}", encoding="utf-8")  # 模拟已导出的草稿
    code, _doc = _publish(root)
    assert code == 0
    pointer = (root / rs_paths.p("deliver") / "半成品入口.md").read_text(encoding="utf-8")
    assert "05_时间线工程/project.json" in pointer
    assert "05_时间线工程/导出/剪映59" in pointer
    assert (root / "05_时间线工程" / "project.json").is_file(), "入口指向的工程必须存在"
    assert (jy / "draft_content.json").is_file(), "入口指向的剪映草稿必须存在"
    deliver_files = _published_files(root)
    assert not any("project.json" in f for f in deliver_files), "工程文件绝不复制进 成品/"
    assert not any("draft_content" in f for f in deliver_files), "剪映草稿绝不复制进 成品/"


# ================================================================ ⑥ safeArea 硬校验(#A6)

def test_safe_area_registered_in_l0():
    """清欠 #A6:安全区判据必须进 L0 判据表(每一次产出后都跑)。"""
    assert rs_verify.check_safe_area in rs_verify.L0_CHECKS


def test_safe_area_inside_bounds_is_green(tmp_path):
    """界内绿:声明平台 douyin(底部 25% 禁区 = 480px),字幕 MarginV 500 → 过。"""
    root = _complete(_mk_project(tmp_path), margin_v=500)
    res = rs_verify.check_safe_area(root)
    assert res["ok"] is True and not res.get("skipped"), res
    assert res["platforms"] == ["抖音"]
    code, doc = _publish(root)
    assert code == 0, doc
    row = next(i for i in doc["items"] if "安全区" in i["item"])
    assert row["ok"] is True, row


def test_safe_area_out_of_bounds_is_red(tmp_path):
    """界外红:MarginV 300(进入底部 25% 禁区)→ L0 红 + publish 对账红 + 非零退出。"""
    root = _complete(_mk_project(tmp_path), margin_v=300)
    res = rs_verify.check_safe_area(root)
    assert res["ok"] is False and res["violations"], res
    assert "底部禁区" in res["violations"][0]
    code, doc = _publish(root)
    assert code == 4 and doc["code"] == "DELIVERABLES_INCOMPLETE"
    row = next(i for i in doc["items"] if "安全区" in i["item"])
    assert row["ok"] is False


def test_safe_area_overlay_rect_hard_bounds(tmp_path):
    """贴片矩形四边硬界:logo/贴片越出安全区 → 红(IR overlay={x,y,w,h})。"""
    root = _complete(_mk_project(tmp_path))
    pj = root / "05_时间线工程" / "project.json"
    doc = json.loads(pj.read_text(encoding="utf-8"))
    doc["tracks"] = [{"kind": "video", "name": "logo", "clips": [
        {"src": "01_原始素材/a.mp4", "startMs": 0, "durationMs": 100,
         "overlay": {"x": 900, "y": 1700, "w": 120, "h": 120}}]}]   # 底部 25% 禁区内
    pj.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    res = rs_verify.check_safe_area(root)
    assert res["ok"] is False
    assert any("贴片矩形越界" in v for v in res["violations"]), res["violations"]


def test_safe_area_skipped_without_platform(tmp_path):
    """判据缺席必须显式:未声明平台且画布无预设 → skipped(不误伤旧工程)。"""
    root = _mk_project(tmp_path)
    res = rs_verify.check_safe_area(root)
    assert res["ok"] is True and res.get("skipped"), res


# ================================================================ ⑦ 降级诚实 + 封面尺寸

def test_degraded_must_be_listed_in_delivery_notes(tmp_path):
    """§4.3 对账项「降级诚实」:有 degraded 的产物必须在交付说明书列明原因与影响。"""
    root = _complete(_mk_project(tmp_path), degraded=True)
    code, doc = _publish(root)
    assert code == 0, doc
    notes = (root / rs_paths.p("deliver") / "说明书" / "交付说明书.md").read_text(encoding="utf-8")
    assert "降级" in notes and "ASR 不可用,退回粗对齐" in notes, notes
    row = next(i for i in doc["items"] if "降级诚实" in i["item"])
    assert row["ok"] is True


def test_cover_size_must_match_canvas_and_platform(tmp_path):
    """§4.3 对账项③:封面尺寸 = 画幅 且过平台 coverSize(PNG IHDR 机械读)。"""
    root = _complete(_mk_project(tmp_path), cover_wh=(100, 100))
    code, doc = _publish(root)
    assert code == 4 and doc["code"] == "DELIVERABLES_INCOMPLETE", doc
    assert any("封面" in m and ("画幅" in m or "coverSize" in m) for m in doc["missing"]), doc


def test_decision_notes_required_when_decided(tmp_path):
    """§4.3 对账项⑤:留过决策(--auto/意图编译)的工程必须有 决策说明书.md。"""
    root = _complete(_mk_project(tmp_path))
    pj = root / "05_时间线工程" / "pipeline.json"
    doc = json.loads(pj.read_text(encoding="utf-8"))
    doc["decision_log"] = [{"id": "intent:platform", "field": "platform",
                            "what": "douyin", "source": "user", "inferred": False}]
    pj.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    code, res = _publish(root)
    assert code == 0, res
    assert (root / rs_paths.p("deliver") / "说明书" / "决策说明书.md").is_file()
    row = next(i for i in res["items"] if "说明书" in i["item"])
    assert row["ok"] is True and "决策说明书" in row["detail"]


# ================================================================ ⑧ 手册对拍(--publish 可被 argparse 接受)

def test_manual_gate_accepts_publish_flag():
    ok, note = gate.validate_one("rs_ingest.py", ["deliverables", "P", "--publish"])
    assert ok, note


# ================================================================ ⑨ 剪映单向出口落点

def test_jy_draft_fold_is_project_semifinish_exit(tmp_path):
    """草稿落点 = 05_时间线工程/导出/剪映59/(rs_paths.jianying_draft,禁止手写)。"""
    import rs_jy_draft
    root = _mk_project(tmp_path)
    ir = root / "05_时间线工程" / "project.json"
    assert rs_jy_draft.draft_fold(ir, "dev_x") == \
        root / "05_时间线工程" / "导出" / "剪映59" / "dev_x"
    src = (SCRIPTS / "rs_jy_draft.py").read_text(encoding="utf-8")
    assert "draft_fold(project, name)" in src, "写盘必须经 draft_fold(落点唯一入口)"


def test_jy_draft_e2e_lands_in_project_tree(tmp_path, monkeypatch):
    """端到端(真素材):草稿落工程区 05_时间线工程/导出/剪映59/<名>/,
    root_meta 注册仍在剪映草稿根(首页可见,条目指回工程区落点)。"""
    import rs_jy_draft
    ff = shutil.which("ffmpeg")
    if not ff:
        pytest.skip("本机没有 ffmpeg,无法造真素材,跳过写盘 e2e")
    root = _mk_project(tmp_path)
    mat = root / "01_原始素材"
    a = mat / "a.mp4"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=320x240:d=2:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:d=2", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(a)],
                   check=True, timeout=120)
    (root / "05_时间线工程" / "project.json").write_text(json.dumps(
        {"version": 1, "slug": "proj", "fps": 30,
         "canvas": {"width": 1080, "height": 1920},
         "tracks": [{"kind": "video", "name": "main",
                     "clips": [{"src": "01_原始素材/a.mp4", "startMs": 0,
                                "durationMs": 2000, "sourceInMs": 0}]}],
         "outputs": ["9x16"]}, ensure_ascii=False), encoding="utf-8")
    draft_root = tmp_path / "jy_drafts"
    cfg = {"jianying59": {"draft_root": str(draft_root),
                          "root_meta": str(draft_root / "root_meta_info.json"), "exe": ""}}
    monkeypatch.setattr(rs_jy_draft, "load_config", lambda: cfg)
    monkeypatch.setattr(rs_jy_draft, "assert_jianying_closed", lambda: None)
    ir = root / "05_时间线工程" / "project.json"
    monkeypatch.setattr(sys, "argv", ["rs_jy_draft.py", str(ir), "--name", "dev_jy_exit"])
    code, _, doc = _capture(rs_jy_draft.main)
    if doc.get("code") == "JY_RUNNING":
        pytest.skip("剪映正在运行:按铁律拒绝,跳过(诚实边界)")
    assert code == 0 and doc["code"] == "DRAFT_OK", doc
    fold = Path(doc["data"]["draft_dir"])
    assert fold == root / "05_时间线工程" / "导出" / "剪映59" / "dev_jy_exit", fold
    assert (fold / "draft_content.json").is_file()
    assert (fold / "draft_meta_info.json").is_file()
    root_meta = json.loads((draft_root / "root_meta_info.json").read_text(encoding="utf-8"))
    entry = next(e for e in root_meta["all_draft_store"] if e["draft_name"] == "dev_jy_exit")
    assert Path(entry["draft_fold_path"]) == fold, "首页条目必须指向工程区落点"
