# -*- coding: utf-8 -*-
"""声明式编辑层门禁(ADR-0048 / 计划书 §7.1 门禁 6-9 + M4 验收线):

门禁 6  幂等:同 op 两次 → rev 不变、OpLog 不增
门禁 7  确定性:同 ops 序列在相同工程副本 → IR 字节级一致
门禁 8  寻址:数组下标(clips[3])必须 BAD_ADDRESS 退出码 2
门禁 9  白名单:越界字段 BAD_FIELD 退出码 2;U7 字段/Op → OP_UNSUPPORTED
附加   dry-run 不落盘;undo 字节级复原;文件级 op 不进 project.json;
       冲突即停(CF-*);锁(LOCKED/--force);beat.snap BEATS_MISSING;
       一句自然语言 → ops.json → dry-run → apply → undo 全链路演示(M4 验收)。

运行:pytest tests/test_edit_op.py -q
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_paths  # noqa: E402
import rs_edit  # noqa: E402
import rs_oplog  # noqa: E402


# ---------------------------------------------------------------- 夹具与工具

def _capture(fn, *args, **kw):
    """跑返回 (exit_code, 最后一个 JSON 输出)——rs_* 的 emit 协议。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    doc = json.loads(lines[-1]) if lines else {}
    return code, doc


def _capture_argv(fn, argv: list[str]):
    """sys.argv 注入版(读 sys.argv 的 main,如 rs_oplog/rs_run)。argv[0] = 程序名。"""
    old = sys.argv
    sys.argv = list(argv)
    try:
        return _capture(fn)
    finally:
        sys.argv = old


def _ir(n_clips: int = 3, fps: int = 30) -> dict:
    clips = [{"id": f"V1-{i + 1:03d}", "src": "01_原始素材/a.mp4",
              "startMs": i * 4000, "durationMs": 4000, "sourceInMs": i * 4000,
              "text": f"第{i + 1}段测试旁白"} for i in range(n_clips)]
    return {
        "version": 1, "slug": "demo", "fps": fps,
        "canvas": {"width": 1080, "height": 1920},
        "tracks": [
            {"id": "V1", "kind": "video", "name": "main", "clips": clips},
            {"id": "A1", "kind": "audio", "clips": [
                {"id": "A1-001", "src": "02_转写与校对/voice.wav",
                 "startMs": 0, "durationMs": n_clips * 4000, "role": "voice"}]},
        ],
        "bgm": {"src": "03_创作素材/BGM/原曲.mp3", "gainDb": -18, "ducking": True},
        "outputs": ["9x16"],
    }


def _mk_project(tmp_path: Path, ir: dict | None = None) -> Path:
    """最小工程(中文新结构,ADR-0045)+ IR + 文件级真相源(notes/cutlist)。"""
    root = tmp_path / "proj"
    for d in ("00_制作简报", "01_原始素材", "02_转写与校对", "03_创作素材",
              "04_粗剪决策", "05_时间线工程", "06_成片输出", "_内部状态"):
        (root / d).mkdir(parents=True)
    (root / "01_原始素材" / "a.mp4").write_bytes(b"fake")
    doc = ir if ir is not None else _ir()
    rs_paths.project_json(root).write_text(rs_edit.dump_json(doc), encoding="utf-8")
    (root / "notes.json").write_text(
        rs_edit.dump_json({"version": 1, "items": []}), encoding="utf-8")
    cut = root / rs_paths.p("cut") / "cutlist.json"
    cut.write_text(rs_edit.dump_json(
        {"version": 1, "source": "fixture", "detector": "rs_cut", "cuts": [],
         "keep": [], "removedMs": 0, "srcTotalMs": 12000, "script": "rs_cut"}),
        encoding="utf-8")
    return root


def _write_ops(tmp_path: Path, name: str, ops: list[dict], base_rev: int | None = 0,
               request_id: str | None = None) -> Path:
    doc: dict = {"ops": ops}
    if base_rev is not None:
        doc["baseRev"] = base_rev
    if request_id:
        doc["requestId"] = request_id
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def _oplog_lines(root: Path) -> list[dict]:
    return rs_oplog.load_ops(root)


def _seed_human_op(root: Path, *, rev: int, path: str, before, after,
                   summary: str, op_id: str = "op-900", kind: str = "set") -> None:
    """伪造一条人侧(cutforge 口径,蛇形键)OpLog 行 + 推进 rev 文件。"""
    d = root / ".cutforge" / "oplog"
    d.mkdir(parents=True, exist_ok=True)
    entry = {"op_id": op_id, "ts": "2026-09-25T10:00:00+08:00",
             "actor": {"kind": "user", "id": "user-ui"},
             "target": {"file": "project.json", "path": path},
             "op_kind": kind, "before": before, "after": after,
             "base_rev": f"rev-{rev - 1}", "rev": rev, "summary": summary}
    with (d / "20260925.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    rs_edit.write_rev(root, rev)


# ================================================================ 门禁 6:幂等

def test_gate6_idempotent_same_op_twice(tmp_path):
    """同 op 两次 → 第二次 IDEMPOTENT:rev 不变、OpLog 不增、盘面字节不变。"""
    root = _mk_project(tmp_path)
    pj = rs_paths.project_json(root)
    before_bytes = pj.read_bytes()
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.trim", "target": "V1-002", "after": {"durationMs": 2600},
         "reason": "剪短一点", "source": "user"}], base_rev=0)

    code1, doc1 = _capture(rs_edit.main,
                           ["apply", str(root), "--ops", str(ops)])
    assert code1 == 0 and doc1["code"] == "APPLY_OK"
    rev1 = rs_edit.read_rev(root)
    n1 = len(_oplog_lines(root))
    after_first = pj.read_bytes()

    # 不重新 context,直接重放同一份 ops(baseRev 已旧,但全幂等 → 允许短路)
    code2, doc2 = _capture(rs_edit.main,
                           ["apply", str(root), "--ops", str(ops)])
    assert code2 == 0 and doc2["code"] == "IDEMPOTENT", doc2
    assert rs_edit.read_rev(root) == rev1, "幂等短路不得升 rev"
    assert len(_oplog_lines(root)) == n1, "幂等短路不得产 Op"
    assert pj.read_bytes() == after_first

    # 换一个新值再改 → 正常生效(证明不是全局短路)
    ops3 = _write_ops(tmp_path, "ops3.json", [
        {"op": "clip.trim", "target": "V1-002", "after": {"durationMs": 2000},
         "reason": "再剪短些"}], base_rev=rev1)
    code3, doc3 = _capture(rs_edit.main,
                           ["apply", str(root), "--ops", str(ops3)])
    assert code3 == 0 and doc3["code"] == "APPLY_OK"
    assert rs_edit.read_rev(root) == rev1 + 1
    assert before_bytes != pj.read_bytes()


# ================================================================ 门禁 7:确定性

def test_gate7_deterministic_same_ops_same_bytes(tmp_path):
    """同 ops 序列在相同工程副本 → IR 字节级一致(OpLog 的 ts 不进 IR)。"""
    ir = _ir(4)
    results = []
    for k in (1, 2):
        root = _mk_project(tmp_path / f"copy{k}", ir=json.loads(json.dumps(ir)))
        ops = _write_ops(tmp_path / f"copy{k}", "ops.json", [
            {"op": "clip.trim", "target": "V1-001", "after": {"durationMs": 2600},
             "reason": "剪短"},
            {"op": "transition.set", "target": "V1-001|V1-002",
             "after": {"kind": "dissolve", "durMs": 300}, "reason": "加转场"},
            {"op": "clip.motion", "target": "V1-002",
             "after": {"in": "fadeIn", "inMs": 400}, "reason": "入场"},
            {"op": "bgm.set", "target": "bgm", "after": {"gainDb": -24}, "reason": "小声"},
            {"op": "output.set", "target": "output",
             "after": {"ratios": ["9x16", "16x9"]}, "reason": "出横版"},
        ], base_rev=0)
        code, doc = _capture(rs_edit.main,
                             ["apply", str(root), "--ops", str(ops)])
        assert code == 0 and doc["code"] == "APPLY_OK", doc
        results.append(rs_paths.project_json(root).read_bytes())
    assert results[0] == results[1], "同 (工程 rev, ops 序列) 必得字节级同一 IR"


def test_gate7_frame_grid_snapping(tmp_path):
    """时间写入吸附帧网格(fps=30 → 33.33ms 格;1650 → 1650?1650/33.33=49.5 → 1650)。
    断言:同输入吸附结果确定,且与手工帧换算一致。"""
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.move", "target": "V1-002", "after": {"startMs": 4167},
         "reason": "挪一点"}], base_rev=0)
    code, _ = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 0
    doc = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    moved = doc["tracks"][0]["clips"][1]["startMs"]
    frames = round(4167 * 30 / 1000.0)
    assert moved == int(round(frames * 1000.0 / 30)), \
        f"startMs 未按 fps=30 帧网格吸附:{moved}"


# ================================================================ 门禁 8:寻址

def test_gate8_subscript_addressing_rejected(tmp_path):
    """数组下标寻址必须报 BAD_ADDRESS,退出码 2;盘面不动。"""
    root = _mk_project(tmp_path)
    pj = rs_paths.project_json(root)
    before = pj.read_bytes()
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.trim", "target": "clips[3]", "after": {"durationMs": 100},
         "reason": "下标寻址"}], base_rev=0)
    code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 2 and doc["code"] == "BAD_ADDRESS", doc
    assert pj.read_bytes() == before, "契约违规必须零写盘"
    # ops-validate 也要抓到(不落盘的静态校验)
    code2, doc2 = _capture(rs_edit.main, ["ops-validate", str(ops)])
    assert code2 == 2 and doc2["code"] == "BAD_ADDRESS"
    assert rs_edit.read_rev(root) == 0


def test_gate8_unknown_clip_is_not_found(tmp_path):
    """clipId 不存在(上下文过期)→ NOT_FOUND,提示重新 context。"""
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.trim", "target": "V1-999", "after": {"durationMs": 100},
         "reason": "过期的锚点"}], base_rev=0)
    code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 2 and doc["code"] == "NOT_FOUND", doc
    assert "context" in doc["message"]


# ================================================================ 门禁 9:白名单 + U7

def test_gate9_out_of_whitelist_field(tmp_path):
    """越界字段 → BAD_FIELD 退出码 2(ops-validate 与 apply 双口)。"""
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.trim", "target": "V1-001",
         "after": {"durationMs": 100, "ghostKey": 1}, "reason": "幽灵键"}], base_rev=0)
    code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 2 and doc["code"] == "BAD_FIELD", doc
    code2, doc2 = _capture(rs_edit.main, ["ops-validate", str(ops)])
    assert code2 == 2 and doc2["code"] == "BAD_FIELD"


def test_gate9_value_range_violation(tmp_path):
    """值域:rate=9 越界 → BAD_VALUE;durationMs=0 非法 → BAD_VALUE。"""
    root = _mk_project(tmp_path)
    for ops_body, target_code in (
            ({"op": "clip.speed", "target": "V1-001", "after": {"rate": 9},
              "reason": "超速"}, "BAD_VALUE"),
            ({"op": "clip.trim", "target": "V1-001", "after": {"durationMs": 0},
              "reason": "零时长"}, "BAD_VALUE"),
            ({"op": "bgm.set", "target": "bgm", "after": {"gainDb": -120},
              "reason": "静音"}, "BAD_VALUE")):
        ops = _write_ops(tmp_path, "ops.json", [ops_body], base_rev=0)
        code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
        assert code == 2 and doc["code"] == target_code, doc


def test_gate9_u7_unsupported_op_and_fields(tmp_path):
    """U7 诚实条款:schema 未覆盖的 op/字段必须 OP_UNSUPPORTED(退出码 2),
    绝不静默写入 schema 外字段。"""
    root = _mk_project(tmp_path)
    pj = rs_paths.project_json(root)
    before = pj.read_bytes()
    cases = [
        {"op": "keyframe.set", "target": "V1-001",
         "after": {"field": "x", "points": []}, "reason": "关键帧"},
        {"op": "subtitle.highlight", "target": "V1-001",
         "after": {"words": ["蓝屏"]}, "reason": "重音"},
        {"op": "style.pacing", "target": "style", "after": {"pacing": "fast"},
         "reason": "调节奏"},
        {"op": "clip.speed", "target": "V1-001",
         "after": {"rate": 1.5, "keepPitch": True}, "reason": "变速"},
        {"op": "clip.reframe", "target": "V1-001",
         "after": {"anchorX": 0.5}, "reason": "横向重构"},
        {"op": "bgm.set", "target": "bgm", "after": {"fadeInMs": 300}, "reason": "淡入"},
        {"op": "audio.gain", "target": "A1-001",
         "after": {"gainDb": -6, "leadMs": 100}, "reason": "J-cut"},
        {"op": "output.set", "target": "output",
         "after": {"ratios": ["9x16"], "logos": ["a.png"]}, "reason": "logo"},
        {"op": "transition.set", "target": "V1-001|V1-002",
         "after": {"kind": "match"}, "reason": "匹配剪辑"},
    ]
    for body in cases:
        ops = _write_ops(tmp_path, "ops.json", [body], base_rev=0)
        code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
        assert code == 2 and doc["code"] == "OP_UNSUPPORTED", (body, doc)
        assert "U7" in doc["message"], (body, doc["message"])
        code2, _ = _capture(rs_edit.main, ["ops-validate", str(ops)])
        assert code2 == 2
    assert pj.read_bytes() == before, "U7 拒绝必须零写盘"


# ================================================================ dry-run / undo / 文件级

def test_dry_run_writes_nothing(tmp_path):
    """--dry-run 出人话差异表(§5.4 样式)但盘面字节级不变、rev 不动、无 Op。"""
    root = _mk_project(tmp_path)
    pj = rs_paths.project_json(root)
    before = pj.read_bytes()
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.trim", "target": "V1-003", "after": {"durationMs": 2600},
         "reason": "第 3 段剪短一点", "source": "user"},
        {"op": "transition.set", "target": "V1-002|V1-003",
         "after": {"kind": "dissolve", "durMs": 300}, "reason": "这里加个转场"},
        {"op": "bgm.set", "target": "bgm", "after": {"gainDb": -24},
         "reason": "音乐小一点"},
    ], base_rev=0)
    code, doc = _capture(rs_edit.main,
                         ["apply", str(root), "--ops", str(ops), "--dry-run"])
    assert code == 0 and doc["code"] == "DRY_RUN", doc
    human = "\n".join(doc["data"]["human"])
    assert "将执行 3 处改动" in human and "rev 0 → 1" in human
    assert "V1-003" in human and "300" in human
    assert pj.read_bytes() == before, "dry-run 不得写盘"
    assert rs_edit.read_rev(root) == 0 and not _oplog_lines(root)
    # 正式 apply 后,结果与 dry-run 预告一致(确认态 = 执行态)
    code2, doc2 = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code2 == 0 and doc2["code"] == "APPLY_OK"
    assert doc2["data"]["applied"] == doc["data"]["applied"]


def test_undo_restores_ir_byte_identical(tmp_path):
    """apply → undo --last n → IR 字节级回到原状(回滚主路径)。"""
    root = _mk_project(tmp_path)
    pj = rs_paths.project_json(root)
    original = pj.read_bytes()
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.trim", "target": "V1-001", "after": {"durationMs": 2600},
         "reason": "剪短"},
        {"op": "clip.motion", "target": "V1-002", "after": {"in": "fadeIn"},
         "reason": "入场"},
        {"op": "bgm.set", "target": "bgm", "after": {"gainDb": -30}, "reason": "小声"},
        {"op": "clip.speed", "target": "V1-003", "after": {"rate": 1.25},
         "reason": "快一点"},
    ], base_rev=0)
    code, _ = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 0
    assert pj.read_bytes() != original
    code2, doc2 = _capture(rs_edit.main, ["undo", str(root), "--last", "4"])
    assert code2 == 0 and doc2["code"] == "UNDONE", doc2
    assert doc2["data"]["undone"] == 4
    assert pj.read_bytes() == original, "undo 必须字节级复原 IR"
    # 撤销也留痕:oplog 出现 4 条 undo 类 Op,rev 前进
    kinds = [str(o.get("op_kind")) for o in _oplog_lines(root)]
    assert kinds.count("undo") == 4
    assert rs_edit.read_rev(root) == 2


def test_undo_split_is_lossless(tmp_path):
    """split 的逆必须无损(两段合回原片段,字节级回到切前)。"""
    root = _mk_project(tmp_path)
    pj = rs_paths.project_json(root)
    original = pj.read_bytes()
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.split", "target": "V1-002", "after": {"atMs": 6000},
         "reason": "切一刀"}], base_rev=0)
    code, _ = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 0
    doc = json.loads(pj.read_text(encoding="utf-8"))
    assert len(doc["tracks"][0]["clips"]) == 4, "切一刀后主轨 3→4 段"
    code2, _ = _capture(rs_edit.main, ["undo", str(root), "--last", "1"])
    assert code2 == 0
    assert pj.read_bytes() == original, "split 逆写必须无损"


def test_file_level_ops_never_touch_project_json(tmp_path):
    """note.add → notes.json;segment.protect → cutlist.json;project.json 不动。"""
    root = _mk_project(tmp_path)
    pj = rs_paths.project_json(root)
    before = pj.read_bytes()
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "note.add", "target": "t5000",
         "after": {"text": "这里语气好", "author": "user"}, "reason": "加标注"},
        {"op": "segment.protect", "target": "protect",
         "after": {"startMs": 1200, "endMs": 1580, "note": "术语「蓝屏」必须完整"},
         "reason": "保护术语"},
    ], base_rev=0)
    code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 0 and doc["code"] == "APPLY_OK", doc
    assert pj.read_bytes() == before, "文件级 op 绝不进 project.json"
    notes = json.loads((root / "notes.json").read_text(encoding="utf-8"))
    assert len(notes["items"]) == 1
    item = notes["items"][0]
    assert item["id"] == "n-0001" and item["body"] == "这里语气好"
    assert item["anchor"]["kind"] == "time" and item["state"] == "open"
    cut = json.loads((root / rs_paths.p("cut") / "cutlist.json").read_text(encoding="utf-8"))
    assert cut["protect"] == [{"startMs": 1200, "endMs": 1567,
                               "note": "术语「蓝屏」必须完整"}]  # 1580 吸附 fps30 帧格
    # undo 也逆写回各自真相源
    code2, _ = _capture(rs_edit.main, ["undo", str(root), "--last", "2"])
    assert code2 == 0
    notes2 = json.loads((root / "notes.json").read_text(encoding="utf-8"))
    cut2 = json.loads((root / rs_paths.p("cut") / "cutlist.json").read_text(encoding="utf-8"))
    assert notes2["items"] == [] and not cut2.get("protect")
    assert pj.read_bytes() == before


def test_file_level_op_missing_truth_source(tmp_path):
    """cutlist 缺失时 segment.protect → DEP_MISSING(退出码 3),不伪造落点。"""
    root = _mk_project(tmp_path)
    (root / rs_paths.p("cut") / "cutlist.json").unlink()
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "segment.protect", "target": "protect",
         "after": {"startMs": 100, "endMs": 900}, "reason": "保护"}], base_rev=0)
    code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 3 and doc["code"] == "DEP_MISSING", doc


# ================================================================ 冲突即停

def test_conflict_stops_write_cf001(tmp_path):
    """人侧 OpLog 推进 rev 后,apply 旧 baseRev 且目标相撞 → CF-001,
    非零退出且盘面字节级不变(禁最后写入者获胜)。"""
    root = _mk_project(tmp_path)
    pj = rs_paths.project_json(root)
    before = pj.read_bytes()
    # 人侧(cutforge 口径):把 rev 推到 1,改了 V1-002 的 durationMs
    _seed_human_op(root, rev=1, path="/tracks/0/clips/1/durationMs",
                   before=4000, after=3800, summary="人改了第 2 段时长")
    # agent 还基于 rev0 的视图改同一段 → 冲突
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.trim", "target": "V1-002", "after": {"durationMs": 3000},
         "reason": "agent 也想剪第 2 段"}], base_rev=0)
    code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 4 and doc["code"] == "CF-001", doc
    assert doc["data"]["conflicts"][0]["theirs"] == "/tracks/0/clips/1/durationMs"
    assert pj.read_bytes() == before, "冲突即停:盘面字节级不变"
    assert rs_edit.read_rev(root) == 1 and len(_oplog_lines(root)) == 1


def test_conflict_nonoverlapping_is_precondition(tmp_path):
    """baseRev 过期但目标不重叠 → PRECONDITION_FAILED(要求重新 context)。"""
    root = _mk_project(tmp_path)
    _seed_human_op(root, rev=1, path="/tracks/0/clips/2/durationMs",
                   before=4000, after=3900, summary="人改了第 3 段")
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.trim", "target": "V1-001", "after": {"durationMs": 3500},
         "reason": "改第 1 段"}], base_rev=0)
    code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 2 and doc["code"] == "PRECONDITION_FAILED", doc
    assert "context" in doc["message"]


def test_apply_requires_baserev(tmp_path):
    """没有 baseRev(裸数组且无 --base-rev)→ PRECONDITION_FAILED,不放行。"""
    root = _mk_project(tmp_path)
    p = tmp_path / "bare.json"
    p.write_text(json.dumps([{"op": "clip.trim", "target": "V1-001",
                              "after": {"durationMs": 100}, "reason": "r"}]),
                 encoding="utf-8")
    code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(p)])
    assert code == 2 and doc["code"] == "PRECONDITION_FAILED"
    assert "baseRev" in doc["message"]


def test_lock_blocks_and_force_takes_over(tmp_path):
    """锁被占 → LOCKED(含 pid);过期/死进程 → --force 接管成功。"""
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "bgm.set", "target": "bgm", "after": {"gainDb": -30},
         "reason": "小声"}], base_rev=0)
    (root / ".cutforge").mkdir()
    (root / ".cutforge" / "edit.lock").write_text(
        json.dumps({"pid": 999999, "ts": "2026-09-25T09:00:00+08:00",
                    "actor": "agent"}), encoding="utf-8")
    code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 4 and doc["code"] == "LOCKED", doc
    assert "pid" in doc["message"] and "999999" in doc["message"]
    code2, doc2 = _capture(rs_edit.main,
                           ["apply", str(root), "--ops", str(ops), "--force"])
    assert code2 == 0 and doc2["code"] == "APPLY_OK", doc2
    assert not (root / ".cutforge" / "edit.lock").exists(), "收工必须放锁"


# ================================================================ beat.snap / 杂项

def test_beat_snap_missing_beats_file(tmp_path):
    """beats.json 缺失(M8 未跑)→ BEATS_MISSING 退出码 3,不伪造吸附。"""
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "beat.snap", "target": "V1-001", "after": {"windowMs": 60},
         "reason": "卡点"}], base_rev=0)
    code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 3 and doc["code"] == "BEATS_MISSING", doc


def test_beat_snap_out_of_window_warns_and_stays(tmp_path):
    """吸不到(超窗)→ 不动 + WARN,禁强制吸附。"""
    root = _mk_project(tmp_path)
    beats = root / rs_paths.p("cut") / "beats.json"
    beats.write_text(json.dumps({"beats": [0, 500, 1000, 1500]}), encoding="utf-8")
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "beat.snap", "target": "V1-002", "after": {"windowMs": 60},
         "reason": "卡点"}], base_rev=0)     # V1-002 在 4000,最近节拍 1500,超窗
    code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 0 and doc["code"] == "IDEMPOTENT", doc
    assert any("不动" in w for w in doc["data"]["warnings"]), doc


def test_beat_snap_snaps_within_window(tmp_path):
    """窗内吸附:4005 → 4000(节拍);已对齐则零改动。"""
    root = _mk_project(tmp_path)
    (root / rs_paths.p("cut") / "beats.json").write_text(
        json.dumps({"beats": [0, 1000, 4000, 8000]}), encoding="utf-8")
    doc0 = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    doc0["tracks"][0]["clips"][1]["startMs"] = 4005
    rs_paths.project_json(root).write_text(rs_edit.dump_json(doc0), encoding="utf-8")
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "beat.snap", "target": "V1-002", "after": {"windowMs": 60},
         "reason": "卡点"}], base_rev=0)
    code, doc = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 0 and doc["code"] == "APPLY_OK"
    doc1 = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    assert doc1["tracks"][0]["clips"][1]["startMs"] == 4000


def test_context_view_and_scopes(tmp_path):
    """context:Markdown 视图含可改字段白名单与手法清单;--json 出 rev/bytes。"""
    root = _mk_project(tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rs_edit.main(["context", str(root)])
    text = buf.getvalue()
    assert code == 0
    assert "clipId" in text and "V1-001" in text
    assert "clip.trim" in text and "可改字段" in text
    assert "keyframe.set" in text, "U7 不承诺项必须在视图里如实声明"
    code2, doc = _capture(rs_edit.main, ["context", str(root), "--json",
                                         "--scope", "audio"])
    assert code2 == 0 and doc["code"] == "CONTEXT_OK"
    assert "A1-001" in doc["data"]["markdown"]
    assert "V1-001" not in doc["data"]["markdown"], "scope=audio 不出视频轨"


def test_oplog_report_reads_rs_edit_entries(tmp_path):
    """人机同一条 OpLog:rs_oplog report 能直接读出 rs_edit 写的行(reason 可读)。"""
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.trim", "target": "V1-001", "after": {"durationMs": 2600},
         "reason": "开头剪短一点", "source": "user"}], base_rev=0)
    code, _ = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 0
    entries = _oplog_lines(root)
    assert len(entries) == 1
    e = entries[0]
    assert e["actor"]["kind"] == "agent" and e["rev"] == 1
    assert e["base_rev"] == "rev-0" and e["op_id"] == "op-1"
    assert e["summary"] == "开头剪短一点"
    assert e["target"]["file"] == "project.json"
    assert e["target"]["path"].startswith("/tracks/0/clips/0/")
    code2, doc2 = _capture_argv(rs_oplog.main, ["rs_oplog.py", "report", str(root)])
    assert code2 == 0
    assert doc2["data"]["byActor"].get("agent") == 1
    assert any("开头剪短一点" in h for h in doc2["data"]["agentChanges"])


def test_diff_between_revs(tmp_path):
    """diff --rev a --rev b:人话差异,无差异时如实说。"""
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.trim", "target": "V1-001", "after": {"durationMs": 2600},
         "reason": "剪短"}], base_rev=0)
    code, _ = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code == 0
    code2, doc = _capture(rs_edit.main,
                          ["diff", str(root), "--rev", "0", "--rev", "1"])
    assert code2 == 0 and doc["code"] == "DIFF_OK" and doc["data"]["changed"]
    assert any("2600" in h for h in doc["data"]["human"])
    code3, doc3 = _capture(rs_edit.main,
                           ["diff", str(root), "--rev", "1", "--rev", "1"])
    assert code3 == 0 and not doc3["data"]["changed"]


def test_context_budget_trims_and_declares(tmp_path):
    """预算硬上限:超小预算时裁剪并声明被裁内容(不静默截断)。"""
    root = _mk_project(tmp_path, ir=_ir(30))
    code, doc = _capture(rs_edit.main,
                         ["context", str(root), "--json", "--budget", "1KB"])
    assert code == 0 and doc["code"] == "CONTEXT_OK"
    data = doc["data"]
    assert data["bytes"] <= 1024, "输出必须 ≤ 预算"
    assert data["trimmed"], "被裁内容必须显式声明"
    assert "预算裁剪声明" in data["markdown"]


# ================================================================ M4 验收:全链路演示

def test_m4_full_chain_nl_to_undo(tmp_path):
    """验收线 §2.3-B:一句自然语言 → context → ops.json → dry-run → apply →
    rs_oplog 审计 → undo 复原。全程零渲染、无视频帧读取。"""
    root = _mk_project(tmp_path)
    pj = rs_paths.project_json(root)
    original = pj.read_bytes()

    # ① Agent 读 context(12KB 文本视图),拿 baseRev 与 clipId
    code, ctx = _capture(rs_edit.main, ["context", str(root), "--json"])
    assert code == 0
    base_rev = ctx["data"]["rev"]
    assert "V1-003" in ctx["data"]["markdown"]

    # ② 用户:「第 3 段剪短一点,这里加个转场,配乐小声一点,结尾加个标注」
    #    → Agent 语义解析写 ops.json(语义工作到此为止,下面全是机械臂)
    ops = _write_ops(tmp_path, "ops.json", [
        {"op": "clip.trim", "target": "V1-003", "after": {"durationMs": 2600},
         "reason": "第 3 段剪短一点", "source": "user"},
        {"op": "transition.set", "target": "V1-002|V1-003",
         "after": {"kind": "dissolve", "durMs": 300},
         "reason": "这里加个转场", "source": "user"},
        {"op": "bgm.set", "target": "bgm", "after": {"gainDb": -24},
         "reason": "配乐小声一点", "source": "user"},
        {"op": "note.add", "target": "t12000", "after": {"text": "结尾再收一下"},
         "reason": "结尾加个标注", "source": "user"},
    ], base_rev=base_rev, request_id="req-m4-demo")

    # ③ ops-validate → 通过
    code_v, doc_v = _capture(rs_edit.main, ["ops-validate", str(ops)])
    assert code_v == 0 and doc_v["code"] == "VALIDATED"

    # ④ dry-run → 人话差异表,不写盘
    code_d, doc_d = _capture(rs_edit.main,
                             ["apply", str(root), "--ops", str(ops), "--dry-run"])
    assert code_d == 0 and doc_d["code"] == "DRY_RUN"
    assert pj.read_bytes() == original
    assert any("转场" in h and "300" in h for h in doc_d["data"]["human"])

    # ⑤ apply → IR + OpLog + 重建建议
    code_a, doc_a = _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops)])
    assert code_a == 0 and doc_a["code"] == "APPLY_OK"
    assert doc_a["data"]["applied"] == 4
    assert doc_a["data"]["revTo"] == base_rev + 1
    advice = json.dumps(doc_a["data"]["rebuild"], ensure_ascii=False)
    assert "S3" in advice and "S6" in advice, "必须给最小重建建议"
    ir = json.loads(pj.read_text(encoding="utf-8"))
    assert ir["tracks"][0]["clips"][2]["durationMs"] == 2600
    assert ir["tracks"][0]["clips"][2]["transition"]["type"] == "fade"
    assert ir["bgm"]["gainDb"] == -24
    assert "protect" not in ir, "note.add 是文件级 op,不得漏进 IR"

    # ⑥ 审计:rs_oplog report 直接可读
    code_r, doc_r = _capture_argv(rs_oplog.main, ["rs_oplog.py", "report", str(root)])
    assert code_r == 0
    agent_changes = "\n".join(doc_r["data"]["agentChanges"])
    assert "第 3 段剪短一点" in agent_changes

    # ⑦ undo 复原(project.json 字节级;notes.json 清空)
    code_u, doc_u = _capture(rs_edit.main, ["undo", str(root), "--last", "4"])
    assert code_u == 0 and doc_u["code"] == "UNDONE"
    assert pj.read_bytes() == original, "全链路终点:undo 字节级复原"
    notes = json.loads((root / "notes.json").read_text(encoding="utf-8"))
    assert notes["items"] == []
