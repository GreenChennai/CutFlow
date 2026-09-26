# -*- coding: utf-8 -*-
"""v2 M14 · 编辑层扩展门禁(分册04 §4/§7.2 + 主册 ADR-0058):

· 10 个新增 EditOp(fx.apply/fx.clear/element.add/element.remove/element.retime/
  huazi.set/huazi.clear/font.set/asset.swap/effect.glsl.enable)与 5 个扩展字段
  (clip.motion.inFx/outFx、transition.set.fx、sfx.add/bgm.set.assetId、
  overlay.add.element、subtitle.set.huazi)的契约与行为;
· 幂等/确定性/寻址/白名单四门禁在新 op 上不破(沿用 test_edit_op 形态);
· U7:schema 未覆盖的一律 OP_UNSUPPORTED,绝不静默写 schema 外字段;
· schema 双仓同步:CutFlow 与 cutforge 的 project.schema.json 字段集一致;
· rs_common.load_ir 的版本校验与迁移引导(R09/R41);
· context 新增「可用特效/可用素材」两段(防御性读取,库未部署显示占位提示)。

运行:pytest tests/test_edit_op_m14.py -q
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

_CUTFORGE = Path(__file__).resolve().parents[1].parent / 'cutforge' / 'schemas' / 'project.schema.json'
_HAS_CUTFORGE = _CUTFORGE.parent.exists()

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_common  # noqa: E402
import rs_edit  # noqa: E402
import rs_oplog  # noqa: E402
import rs_paths  # noqa: E402

CF_SCHEMA = REPO / "skills" / "cutflow" / "templates" / "project.schema.json"
CF_SCHEMA_CUTFORGE = REPO.parent / "cutforge" / "schemas" / "project.schema.json"


# ---------------------------------------------------------------- 夹具与工具

def _capture(fn, *args, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    doc = json.loads(lines[-1]) if lines else {}
    return code, doc


def _ir() -> dict:
    clips = [{"id": f"V1-{i + 1:03d}", "src": "01_原始素材/a.mp4",
              "startMs": i * 4000, "durationMs": 4000, "sourceInMs": i * 4000,
              "text": f"第{i + 1}段测试旁白"} for i in range(3)]
    return {
        "version": 1, "slug": "m14", "fps": 30,
        "canvas": {"width": 1080, "height": 1920},
        "tracks": [
            {"id": "V1", "kind": "video", "name": "main", "clips": clips},
            {"id": "A1", "kind": "audio", "clips": [
                {"id": "A1-001", "src": "02_转写与校对/voice.wav",
                 "startMs": 0, "durationMs": 12000, "role": "voice"}]},
        ],
        "bgm": {"src": "03_创作素材/BGM/原曲.mp3", "gainDb": -18, "ducking": True},
        "outputs": ["9x16"],
    }


def _mk_project(tmp_path: Path, ir: dict | None = None) -> Path:
    root = tmp_path / "proj"
    for d in ("00_制作简报", "01_原始素材", "02_转写与校对", "03_创作素材",
              "04_粗剪决策", "05_时间线工程", "06_成片输出", "_内部状态"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "01_原始素材" / "a.mp4").write_bytes(b"fake")
    doc = ir if ir is not None else _ir()
    rs_paths.project_json(root).write_text(rs_edit.dump_json(doc), encoding="utf-8")
    return root


def _write_ops(tmp_path: Path, name: str, ops: list[dict], base_rev: int | None = 0) -> Path:
    doc: dict = {"ops": ops}
    if base_rev is not None:
        doc["baseRev"] = base_rev
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def _apply(root: Path, ops_path: Path):
    return _capture(rs_edit.main, ["apply", str(root), "--ops", str(ops_path)])


@pytest.fixture()
def asset_lib(tmp_path, monkeypatch):
    """把 rs_edit 的素材库锚点指到测试夹具(绝不写仓内真实 assets/,M12 在并行施工)。"""
    lib = tmp_path / "assets"
    for sub, name in (("elements/png", "arrow_right.png"),
                      ("sfx", "whip_01.mp3"), ("bgm", "tension_120.mp3")):
        f = lib / sub / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"0" * 64)
    manifest = {"version": 1, "assets": [
        {"id": "element.arrow.right", "kind": "element", "label": "右箭头",
         "file": "elements/png/arrow_right.png", "usage": ["decor"],
         "commercial": True, "source": "自产", "license": "CC0",
         "attribution": "无需归因"},
        {"id": "element.paid.bad", "kind": "element", "label": "不可商用",
         "file": "elements/png/arrow_right.png", "usage": ["decor"],
         "commercial": False, "source": "x", "license": "y", "attribution": "z"},
        {"id": "sfx.whip.01", "kind": "sfx", "label": "甩镜",
         "file": "sfx/whip_01.mp3", "usage": ["transition"], "durationMs": 620,
         "commercial": True, "source": "Mixkit", "license": "Mixkit Free License",
         "attribution": "Mixkit"},
        {"id": "bgm.tension.120", "kind": "bgm", "label": "紧张 120bpm",
         "file": "bgm/tension_120.mp3", "usage": ["emotion"], "durationMs": 30000,
         "commercial": True, "source": "自产", "license": "CC0", "attribution": "无需归因"},
    ]}
    (lib / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                       encoding="utf-8")
    monkeypatch.setattr(rs_edit, "ASSETS_DIR", lib)
    monkeypatch.setattr(rs_edit, "ASSETS_MANIFEST_PATH", lib / "manifest.json")
    return lib


# ================================================================ 新 op:fx.apply / fx.clear

def test_fx_apply_and_clear_roundtrip(tmp_path):
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "fx.apply", "target": "V1-002",
         "after": {"slot": "in", "fx": "fx.in.zoom.slight", "params": {"from": 1.0}},
         "reason": "放大入场"}], base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 0 and doc["code"] == "APPLY_OK", doc
    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    assert ir["tracks"][0]["clips"][1]["fx"]["in"] == \
        {"fx": "fx.in.zoom.slight", "params": {"from": 1.0}}
    # 幂等:重放短路
    code2, doc2 = _apply(root, ops)
    assert code2 == 0 and doc2["code"] == "IDEMPOTENT"
    # 清除
    ops_c = _write_ops(tmp_path, "c.json", [
        {"op": "fx.clear", "target": "V1-002", "after": {"slot": "in"},
         "reason": "不要了"}], base_rev=doc["data"]["revTo"])
    code3, doc3 = _apply(root, ops_c)
    assert code3 == 0
    ir3 = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    assert "fx" not in ir3["tracks"][0]["clips"][1], "容器清空必须摘除(不留幽灵键)"


def test_fx_apply_bad_slot_and_ghost_key(tmp_path):
    root = _mk_project(tmp_path)
    for after, code_want in (
            ({"slot": "middle", "fx": "fx.x"}, "BAD_VALUE"),
            ({"slot": "in"}, "BAD_VALUE"),
            ({"slot": "in", "fx": "fx.x", "ghost": 1}, "BAD_FIELD")):
        ops = _write_ops(tmp_path, "o.json", [
            {"op": "fx.apply", "target": "V1-001", "after": after, "reason": "r"}],
            base_rev=0)
        code, doc = _apply(root, ops)
        assert code == 2 and doc["code"] == code_want, (after, doc)


# ================================================================ 新 op:element.*

def test_element_add_requires_manifest(tmp_path, monkeypatch):
    """manifest 缺失(M12 未部署)→ DEP_MISSING,不猜路径、不静默。"""
    monkeypatch.setattr(rs_edit, "ASSETS_MANIFEST_PATH", tmp_path / "nope.json")
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "element.add", "target": "overlay",
         "after": {"element": "element.arrow.right", "startMs": 500, "durationMs": 900,
                   "x": 0.6, "y": 0.3}, "reason": "加个箭头"}], base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 3 and doc["code"] == "DEP_MISSING", doc


def test_element_add_full_semantics(tmp_path, asset_lib):
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "element.add", "target": "overlay",
         "after": {"element": "element.arrow.right", "startMs": 500, "durationMs": 900,
                   "x": 0.6, "y": 0.3, "opacity": 0.8,
                   "motion": {"fx": "fx.in.slide.left"}}, "reason": "加个箭头"}], base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 0 and doc["code"] == "APPLY_OK", doc
    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    ov = next(t for t in ir["tracks"] if t.get("name") == "overlay")
    c = ov["clips"][0]
    assert c["assetId"] == "element.arrow.right"
    assert c["position"] == {"x": 0.6, "y": 0.3}          # 归一化中心点
    assert c["scale"] == rs_edit.ELEMENT_DEFAULT_SCALE    # 未给 w → 兜底贴图宽
    assert c["opacity"] == 0.8
    assert c["motion"] == {"inFx": "fx.in.slide.left"}
    assert c["startMs"] == 500, "500ms 在 fps30 帧网格上 = 15 帧 = 500ms"
    assert c["durationMs"] == 900
    # 插入类 op 的幂等语义(手册 §3):无可比较旧值,由 baseRev 前置 + requestId
    # 去重保证 —— 旧 baseRev 重放必须 PRECONDITION_FAILED(重新 context),绝不重复插入
    code2, doc2 = _apply(root, ops)
    assert code2 == 2 and doc2["code"] == "PRECONDITION_FAILED", doc2
    # undo:覆盖轨回到无元素
    code3, doc3 = _capture(rs_edit.main, ["undo", str(root), "--last", "1"])
    assert code3 == 0
    ir3 = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    assert not [t for t in ir3["tracks"] if t.get("name") == "overlay" and t.get("clips")]


def test_element_add_geometry_w_h_absolute(tmp_path, asset_lib):
    """给 w/h 时走 overlay 绝对落点(以 x/y 为中心折算)。"""
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "element.add", "target": "overlay",
         "after": {"element": "element.arrow.right", "startMs": 0, "durationMs": 900,
                   "x": 0.5, "y": 0.5, "w": 200, "h": 120}, "reason": "指那里"}], base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 0, doc
    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    c = next(t for t in ir["tracks"] if t.get("name") == "overlay")["clips"][0]
    assert c["overlay"]["w"] == 200 and c["overlay"]["h"] == 120
    assert "scale" not in c, "给了 w 就不再写兜底 scale"


def test_element_add_rejects_main_track_and_bad_asset(tmp_path, asset_lib):
    root = _mk_project(tmp_path)
    cases = [
        ({"op": "element.add", "target": "V1",
          "after": {"element": "element.arrow.right", "startMs": 0, "durationMs": 900},
          "reason": "压主轨"}, "BAD_ADDRESS"),
        ({"op": "element.add", "target": "overlay",
          "after": {"element": "element.nope", "startMs": 0, "durationMs": 900},
          "reason": "不存在的 id"}, "BAD_VALUE"),
        ({"op": "element.add", "target": "overlay",
          "after": {"element": "element.paid.bad", "startMs": 0, "durationMs": 900},
          "reason": "不可商用"}, "BAD_VALUE"),
    ]
    for body, want in cases:
        ops = _write_ops(tmp_path, "o.json", [body], base_rev=0)
        code, doc = _apply(root, ops)
        assert code == 2 and doc["code"] == want, (body, doc)


def test_element_retime_and_remove_guard(tmp_path, asset_lib):
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "element.add", "target": "overlay",
         "after": {"element": "element.arrow.right", "startMs": 0, "durationMs": 900},
         "reason": "挂元素"}], base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 0
    # retime 用 content-id 回退找不到 —— 用原生视角:先 context 拿 clipId
    code_c, ctx = _capture(rs_edit.main, ["context", str(root), "--json"])
    assert code_c == 0
    # element.add 未写原生 id → 内容寻址 id;直接解析 overlay clip 的 cf- id
    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    ov = next(t for t in ir["tracks"] if t.get("name") == "overlay")
    import rs_editor
    cid = rs_editor.content_id(ov["clips"][0])
    ops_r = _write_ops(tmp_path, "r.json", [
        {"op": "element.retime", "target": cid,
         "after": {"startMs": 1000, "durationMs": 1500}, "reason": "挪一挪"}], base_rev=1)
    code_r, doc_r = _apply(root, ops_r)
    assert code_r == 0, doc_r
    ir2 = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    c = next(t for t in ir2["tracks"] if t.get("name") == "overlay")["clips"][0]
    assert c["startMs"] == 1000 and c["durationMs"] == 1500
    # 内容寻址 id 随内容变化:retime 后重新取 cf- id(寻址纪律的既定行为)
    import rs_editor as _ed
    cid = _ed.content_id(
        next(t for t in ir2["tracks"] if t.get("name") == "overlay")["clips"][0])
    # 守卫:普通片段(无 assetId)不能走 element.remove/retime
    ops_g = _write_ops(tmp_path, "g.json", [
        {"op": "element.remove", "target": "V1-001", "reason": "误用"}], base_rev=2)
    code_g, doc_g = _apply(root, ops_g)
    assert code_g == 2 and doc_g["code"] == "BAD_ADDRESS"
    # remove 真删
    ops_d = _write_ops(tmp_path, "d.json", [
        {"op": "element.remove", "target": cid, "reason": "去掉"}], base_rev=2)
    code_d, doc_d = _apply(root, ops_d)
    assert code_d == 0
    ir3 = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    assert not [t for t in ir3["tracks"] if t.get("name") == "overlay" and t.get("clips")]


# ================================================================ 新 op:huazi.* / font.set

def test_huazi_set_clear_and_wrong_track(tmp_path):
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "huazi.set", "target": "V1-002",
         "after": {"template": "huazi.keyword.box", "params": {"color": "金"}},
         "reason": "这句做成花字"}], base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 2 and doc["code"] == "BAD_ADDRESS", "花字只挂字幕轨(工程无 text 轨)"
    # 加一条 text 轨再试
    ir = _ir()
    ir["tracks"].append({"id": "T1", "kind": "text", "clips": [
        {"id": "T1-001", "src": "sub.ass", "startMs": 4000, "durationMs": 2000,
         "text": "关键词句"}]})
    root2 = _mk_project(tmp_path / "p2", ir=ir)
    ops2 = _write_ops(tmp_path / "p2", "o.json", [
        {"op": "huazi.set", "target": "T1-001",
         "after": {"template": "huazi.keyword.box"}, "reason": "花字"}], base_rev=0)
    code2, doc2 = _apply(root2, ops2)
    assert code2 == 0, doc2
    ir2 = json.loads(rs_paths.project_json(root2).read_text(encoding="utf-8"))
    tclip = next(t for t in ir2["tracks"] if t.get("kind") == "text")["clips"][0]
    assert tclip["huazi"] == {"template": "huazi.keyword.box"}
    # subtitle.set 的 huazi 扩展
    ops3 = _write_ops(tmp_path / "p2", "o3.json", [
        {"op": "subtitle.set", "target": "T1-001",
         "after": {"huazi": {"template": "huazi.title.pop"}}, "reason": "换花字"}],
        base_rev=1)
    code3, _ = _apply(root2, ops3)
    assert code3 == 0
    ir3 = json.loads(rs_paths.project_json(root2).read_text(encoding="utf-8"))
    tclip3 = next(t for t in ir3["tracks"] if t.get("kind") == "text")["clips"][0]
    assert tclip3["huazi"]["template"] == "huazi.title.pop"
    # clear → 幂等(再 clear 一次也无 Op)
    ops4 = _write_ops(tmp_path / "p2", "o4.json", [
        {"op": "huazi.clear", "target": "T1-001", "reason": "回普通"}], base_rev=2)
    code4, _ = _apply(root2, ops4)
    code5, doc5 = _apply(root2, ops4)
    assert code4 == 0 and code5 == 0 and doc5["code"] == "IDEMPOTENT"
    ir5 = json.loads(rs_paths.project_json(root2).read_text(encoding="utf-8"))
    tclip5 = next(t for t in ir5["tracks"] if t.get("kind") == "text")["clips"][0]
    assert "huazi" not in tclip5, "去花字后不得残留空容器"


def test_font_set_project_and_clip(tmp_path):
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "font.set", "target": "project",
         "after": {"family": "smiley-sans", "scope": "project"}, "reason": "换字体"}],
        base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 0, doc
    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    assert ir["font"] == {"family": "smiley-sans"}
    ops2 = _write_ops(tmp_path, "o2.json", [
        {"op": "font.set", "target": "V1-001",
         "after": {"family": "source-han-sans"}, "reason": "这段用思源"}], base_rev=1)
    code2, _ = _apply(root, ops2)
    assert code2 == 0
    ir2 = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    assert ir2["tracks"][0]["clips"][0]["font"] == {"family": "source-han-sans"}
    # scope=style → U7
    ops3 = _write_ops(tmp_path, "o3.json", [
        {"op": "font.set", "target": "project",
         "after": {"family": "x", "scope": "style"}, "reason": "样式级"}], base_rev=2)
    code3, doc3 = _apply(root, ops3)
    assert code3 == 2 and doc3["code"] == "OP_UNSUPPORTED" and "U7" in doc3["message"]
    # target/scope 矛盾
    ops4 = _write_ops(tmp_path, "o4.json", [
        {"op": "font.set", "target": "project",
         "after": {"family": "x", "scope": "clip"}, "reason": "矛盾"}], base_rev=2)
    code4, doc4 = _apply(root, ops4)
    assert code4 == 2 and doc4["code"] == "BAD_VALUE"


# ================================================================ 新 op:asset.swap / sfx/bgm assetId

def test_asset_swap_bgm_and_sfx(tmp_path, asset_lib):
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "asset.swap", "target": "bgm",
         "after": {"assetId": "bgm.tension.120"}, "reason": "换紧张的"}], base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 0, doc
    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    assert ir["bgm"]["assetId"] == "bgm.tension.120"
    assert ir["bgm"]["src"] == str(asset_lib / "bgm" / "tension_120.mp3")
    assert ir["bgm"]["gainDb"] == -18, "换素材保留参数(gainDb 不动)"
    # kind 不符:用 sfx 素材换 bgm → BAD_VALUE
    ops2 = _write_ops(tmp_path, "o2.json", [
        {"op": "asset.swap", "target": "bgm",
         "after": {"assetId": "sfx.whip.01"}, "reason": "kind 错"}], base_rev=1)
    code2, doc2 = _apply(root, ops2)
    assert code2 == 2 and doc2["code"] == "BAD_VALUE"
    # 音效段 swap
    ir3 = _ir()
    ir3["tracks"][1]["clips"].append(
        {"id": "A1-002", "src": "assets_sfx:whoosh", "startMs": 2000,
         "durationMs": 1000, "role": "sfx"})
    root3 = _mk_project(tmp_path / "p3", ir=ir3)
    ops3 = _write_ops(tmp_path / "p3", "o3.json", [
        {"op": "asset.swap", "target": "A1-002",
         "after": {"assetId": "sfx.whip.01"}, "reason": "换音效"}], base_rev=0)
    code3, doc3 = _apply(root3, ops3)
    assert code3 == 0, doc3
    ir4 = json.loads(rs_paths.project_json(root3).read_text(encoding="utf-8"))
    sfx = ir4["tracks"][1]["clips"][1]
    assert sfx["assetId"] == "sfx.whip.01" and sfx["role"] == "sfx"
    assert sfx["startMs"] == 2000, "换素材保留时间"


def test_sfx_add_and_bgm_set_asset_id(tmp_path, asset_lib):
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "sfx.add", "target": "t1000",
         "after": {"assetId": "sfx.whip.01", "name": "whoosh"}, "reason": "甩镜"},
        {"op": "bgm.set", "target": "bgm",
         "after": {"assetId": "bgm.tension.120"}, "reason": "换曲"}], base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 0, doc
    assert any("以 assetId 为准" in w for w in doc["data"]["warnings"]), \
        "name 与 assetId 并存必须 WARN"
    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    audio = next(t for t in ir["tracks"] if t.get("kind") == "audio")
    sfx = [c for c in audio["clips"] if c.get("role") == "sfx"][0]
    assert sfx["assetId"] == "sfx.whip.01"
    assert sfx["src"] == str(asset_lib / "sfx" / "whip_01.mp3")
    assert sfx["durationMs"] == 633, "assetId 路径取 manifest 实测时长(620ms 吸附 fps30 帧格=633ms)"
    assert ir["bgm"]["assetId"] == "bgm.tension.120"


# ================================================================ 新 op:effect.glsl.enable

def test_effect_glsl_enable(tmp_path):
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "effect.glsl.enable", "target": "project",
         "after": {"on": False}, "reason": "别用 GLSL,怕慢"}], base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 0, doc
    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    assert ir["effects"] == {"glsl": False}
    # wrong target
    ops2 = _write_ops(tmp_path, "o2.json", [
        {"op": "effect.glsl.enable", "target": "V1-001",
         "after": {"on": False}, "reason": "错靶"}], base_rev=1)
    code2, doc2 = _apply(root, ops2)
    assert code2 == 2 and doc2["code"] == "BAD_ADDRESS"


# ================================================================ 扩展字段:motion.fx / transition.fx

def test_motion_fx_extension(tmp_path):
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "clip.motion", "target": "V1-001",
         "after": {"inFx": "fx.in.zoom.slight"}, "reason": "特效入场"}], base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 0, doc
    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    assert ir["tracks"][0]["clips"][0]["motion"]["inFx"] == "fx.in.zoom.slight"
    # 非法 fxId(空白)→ BAD_VALUE
    ops2 = _write_ops(tmp_path, "o2.json", [
        {"op": "clip.motion", "target": "V1-001",
         "after": {"outFx": "bad fx id"}, "reason": "带空格"}], base_rev=1)
    code2, doc2 = _apply(root, ops2)
    assert code2 == 2 and doc2["code"] == "BAD_VALUE"


def test_transition_fx_extension(tmp_path):
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "transition.set", "target": "V1-001|V1-002",
         "after": {"kind": "fade", "fx": "tr.whip.pan", "durMs": 300},
         "reason": "甩镜"}], base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 0, doc
    assert any("以 fx 为准" in w for w in doc["data"]["warnings"]), \
        "kind 与 fx 并存必须 WARN(渲染以 fx 为准)"
    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    tr = ir["tracks"][0]["clips"][1]["transition"]
    assert tr["fx"] == "tr.whip.pan" and tr["type"] == "fade", \
        "两个字段都落契约(渲染端 fx 优先),不静默丢 kind"


# ================================================================ overlay.add element 扩展

def test_overlay_add_element_mutual_exclusion(tmp_path, asset_lib):
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "overlay.add", "target": "any",
         "after": {"card": "卡片A", "element": "element.arrow.right",
                   "startMs": 0, "durationMs": 900}, "reason": "二选一"}], base_rev=0)
    code, doc = _apply(root, ops)
    assert code == 2 and doc["code"] == "BAD_VALUE"
    ops2 = _write_ops(tmp_path, "o2.json", [
        {"op": "overlay.add", "target": "any",
         "after": {"element": "element.arrow.right",
                   "startMs": 0, "durationMs": 900}, "reason": "元素"}], base_rev=0)
    code2, doc2 = _apply(root, ops2)
    assert code2 == 0, doc2
    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    ov = next(t for t in ir["tracks"] if t.get("name") == "overlay")
    assert ov["clips"][0]["assetId"] == "element.arrow.right"


# ================================================================ 四门禁不破:确定性 / undo

def test_new_ops_deterministic_and_undo(tmp_path):
    """同 ops 序列在两份工程副本 → IR 字节级一致;undo 字节级复原。"""
    ops_body = [
        {"op": "fx.apply", "target": "V1-001",
         "after": {"slot": "in", "fx": "fx.in.zoom.slight"}, "reason": "入场"},
        {"op": "clip.motion", "target": "V1-002",
         "after": {"inFx": "fx.in.slide.left"}, "reason": "滑入"},
        {"op": "transition.set", "target": "V1-002|V1-003",
         "after": {"fx": "tr.fade"}, "reason": "转场"},
        {"op": "font.set", "target": "project",
         "after": {"family": "smiley-sans"}, "reason": "字体"},
        {"op": "effect.glsl.enable", "target": "project",
         "after": {"on": False}, "reason": "关 T2"},
        {"op": "bgm.set", "target": "bgm",
         "after": {"gainDb": -24}, "reason": "小声"},
    ]
    pj_bytes = []
    for k in (1, 2):
        root = _mk_project(tmp_path / f"copy{k}")
        ops = _write_ops(tmp_path / f"copy{k}", "o.json", ops_body, base_rev=0)
        code, doc = _apply(root, ops)
        assert code == 0 and doc["code"] == "APPLY_OK", doc
        pj_bytes.append(rs_paths.project_json(root).read_bytes())
    assert pj_bytes[0] == pj_bytes[1], "确定性:同输入必须字节级同 IR"
    # undo 全链复原
    root1 = _mk_project(tmp_path / "undo_root")
    ops = _write_ops(tmp_path / "undo_root", "o.json", ops_body, base_rev=0)
    original = rs_paths.project_json(root1).read_bytes()
    code, _ = _apply(root1, ops)
    assert code == 0
    code_u, doc_u = _capture(rs_edit.main, ["undo", str(root1), "--last", str(len(ops_body))])
    assert code_u == 0 and doc_u["code"] == "UNDONE"
    assert rs_paths.project_json(root1).read_bytes() == original, "undo 必须字节级复原"


# ================================================================ schema 双仓同步

@pytest.mark.skipif(not _HAS_CUTFORGE, reason='cutforge 仓缺席(CI tests job),双仓同步由 rust-gates/本地把守')
def test_schema_sync_between_repos():
    """CutFlow 与 cutforge 的 project.schema.json clip/顶层字段集一致
    (cutforge additionalProperties:false —— 不同步编辑器会拒开新工程)。"""
    cf = json.loads(CF_SCHEMA.read_text(encoding="utf-8"))
    forge = json.loads(CF_SCHEMA_CUTFORGE.read_text(encoding="utf-8"))
    cf_clip = set(cf["$defs"]["clip"]["properties"])
    forge_clip = set(forge["$defs"]["clip"]["properties"])
    assert cf_clip <= forge_clip, f"cutforge schema 缺字段:{cf_clip - forge_clip}"
    cf_top = set(cf["properties"])
    forge_top = set(forge["properties"])
    assert cf_top <= forge_top, f"cutforge 顶层 schema 缺字段:{cf_top - forge_top}"
    for f in ("fx", "huazi", "font", "assetId"):
        assert f in cf_clip and f in forge_clip
    assert {"inFx", "outFx"} <= set(cf["$defs"]["clip"]["properties"]["motion"]["properties"])
    assert "fx" in cf["$defs"]["clip"]["properties"]["transition"]["properties"]
    assert {"font", "effects"} <= cf_top


# ================================================================ load_ir 版本校验(R09/R41)

def test_load_ir_version_check_and_migration_guidance(tmp_path):
    root = _mk_project(tmp_path)
    pj = rs_paths.project_json(root)
    doc = json.loads(pj.read_text(encoding="utf-8"))
    # 版本相符 → 正常载入
    assert rs_common.load_ir(root)["slug"] == "m14"
    # 版本不符 → 结构化错误 + 迁移指引
    doc["version"] = 2
    pj.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(rs_common.IrError) as ei:
        rs_common.load_ir(root)
    err = ei.value
    assert err.code == "IR_VERSION_UNSUPPORTED" and err.exit_code == 2
    assert "迁移" in err.message and err.data["version"] == 2 and err.data["expected"] == 1
    # rs_edit apply 也走统一入口:给出同一错误码
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "clip.trim", "target": "V1-001", "after": {"durationMs": 100},
         "reason": "r"}], base_rev=0)
    code, doc_e = _apply(root, ops)
    assert code == 2 and doc_e["code"] == "IR_VERSION_UNSUPPORTED", doc_e
    # 缺 version → 同样拦截
    doc2 = json.loads(pj.read_text(encoding="utf-8"))
    del doc2["version"]
    pj.write_text(json.dumps(doc2), encoding="utf-8")
    with pytest.raises(rs_common.IrError):
        rs_common.load_ir(root)


# ================================================================ context 两段(分册04 §4.4)

def test_context_effects_assets_sections_defensive(tmp_path, monkeypatch):
    root = _mk_project(tmp_path)
    # ① 库未部署:两段显示占位提示,不崩
    monkeypatch.setattr(rs_edit, "EFFECTS_CATALOG_PATH", tmp_path / "no-catalog.json")
    monkeypatch.setattr(rs_edit, "ASSETS_MANIFEST_PATH", tmp_path / "no-manifest.json")
    code, doc = _capture(rs_edit.main, ["context", str(root), "--json"])
    assert code == 0
    md = doc["data"]["markdown"]
    assert "## 可用特效" in md and "效果目录未部署" in md
    assert "## 可用素材" in md and "manifest 未部署" in md
    # ② 库已部署:列出条目
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps({"effects": [
        {"fxId": "fx.in.zoom.slight", "status": "可执行", "tier": "T1", "label": "轻微放大"},
        {"fxId": "tr.whip.pan", "status": "可执行", "tier": "T1"},
        {"fxId": "tr.water.ripple", "status": "可执行", "tier": "T2"},
        {"fxId": "fx.in.rotate.in", "status": "不实现"},
    ]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rs_edit, "EFFECTS_CATALOG_PATH", cat)
    code2, doc2 = _capture(rs_edit.main, ["context", str(root), "--json"])
    md2 = doc2["data"]["markdown"]
    assert "fx.in.zoom.slight" in md2 and "tr.whip.pan" in md2
    assert "T2 不可用" in md2 and "tr.water.ripple" in md2
    assert "fx.in.rotate.in" not in md2, "只列 status=可执行"
    # ③ 素材 manifest 有条目:按 kind+usage 分组列前 5
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "manifest.json").write_text(json.dumps({"assets": [
        {"id": f"sfx.whip.{i:02d}", "kind": "sfx", "usage": ["transition"]}
        for i in range(1, 8)]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rs_edit, "ASSETS_MANIFEST_PATH", lib / "manifest.json")
    code3, doc3 = _capture(rs_edit.main, ["context", str(root), "--json"])
    md3 = doc3["data"]["markdown"]
    assert "音效·转场" in md3
    assert "sfx.whip.01" in md3 and "sfx.whip.05" in md3
    assert "sfx.whip.06" not in md3, "每组只列前 5 条"
    assert doc3["data"]["bytes"] <= 12 * 1024, "新增两段后仍须守 12KB 预算"


# ================================================================ OpLog 口径

def test_new_ops_write_oplog_and_report(tmp_path, asset_lib):
    root = _mk_project(tmp_path)
    ops = _write_ops(tmp_path, "o.json", [
        {"op": "element.add", "target": "overlay",
         "after": {"element": "element.arrow.right", "startMs": 0, "durationMs": 900},
         "reason": "加个箭头指向那里"}], base_rev=0)
    code, _ = _apply(root, ops)
    assert code == 0
    entries = rs_oplog.load_ops(root)
    assert len(entries) == 1
    e = entries[0]
    assert e["op_id"] == "op-1" and e["op_kind"] == "insert"
    assert e["summary"] == "加个箭头指向那里"
    assert e["actor"]["id"] == "cutflow-rs_edit"
