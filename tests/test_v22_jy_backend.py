"""副文档 06 · 阶段六回归:剪映原生草稿后端(J1–J6)。

J1  rs_jy_draft 编译层:IR → 草稿计划(draft plan,帧对齐)→ 校验 → 写盘;
    --dry-run 只打印「IR 片段 → 草稿片段」映射表,不写任何文件
J2  rs_cut protect 保护区:切点不得侵入「必须保留发音」区间;优先级 protect >
    既有 guard 三项,冲突即报错而非降级(TDD:先写侵入必败用例,演示红→绿)
J3  草稿计划门禁:轨道数/片段时长和/主轨首段从 0 且不重叠/无黑场间隙/
    帧对齐断言;门禁失败拒绝写草稿(故意构造坏计划演示)
J4  能力扩展核查:IR 已表达且草稿可承载的(转场/淡入淡出/位置缩放/变速/音量/
    音效 gainDb/assets_sfx 伪协议/BGM)经编译层映射并有测试;motion 关键帧等
    v1 不写,计划里留显式 warning(诚实在 J5 文档「未支持」区)
J5  rules/jianying-verification.md 诚实验收文档:已验证/未验证/拒绝 三类
J6  与 cutforge export_jianying 对拍:同一脚本同一旗标同一编译层(两仓测试都绿,
    cutforge 侧见 cutforge/tests/test_jy_bridge.py)

运行:pytest tests/test_v22_jy_backend.py -q
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_cut  # noqa: E402
import rs_jy_draft  # noqa: E402


def _capture(fn, *args, **kw):
    """跑返回 (exit_code, 最后一个 JSON 输出)——rs_* 的 emit 协议。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kw)
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    doc = json.loads(lines[-1]) if lines else {}
    return code, doc


def _wordline(text: str, start_ms: int = 0, per: int = 200) -> dict:
    """带字级时间戳的 wordline(与 test_v17 同构)。"""
    ts = [[start_ms + i * per, start_ms + i * per + per - 20] for i in range(len(text))]
    seg = {"start": start_ms / 1000.0, "end": (start_ms + len(text) * per) / 1000.0,
           "text": text, "timestamp": ts, "conf": 0.95}
    import rs_align
    return rs_align.build_wordline([seg], "01_materials/a.mp4")


# ================================================================ J2 protect 保护区(TDD 先红)
# 夹具口径:14 字 × 200ms/字 →「蓝」[1200,1380]、「屏」[1400,1580],
# protect 区取「蓝屏」完整发音 [1200,1580](与 keep 同单位,ms,左闭右开)。

PROTECT_BLUE = [{"startMs": 1200, "endMs": 1580, "note": "术语「蓝屏」必须完整"}]


def test_j2_cut_intruding_protect_must_fail():
    """J2 验收判据 2:切点侵入 protect 区必须报错,而不是降级 review。

    protect 区 =「必须保留发音」的词的有效发音区间。这刀 remove 候选
    [0,2400] 把「蓝屏」整段切进去 → build_cutlist 抛 ValueError(冲突即报错)。
    """
    wl = _wordline("大家好今天讲蓝屏修复的第一课")
    cuts = [{"inMs": 0, "outMs": 2400, "reason": "manual", "conf": 0.95}]
    with pytest.raises(ValueError, match="PROTECT"):
        rs_cut.build_cutlist(wl, cuts, {"protect": PROTECT_BLUE})


def test_j2_cut_outside_protect_passes_and_records_zones():
    """不侵入 protect 的刀照常执行;protect 区原样落进 cutlist(可审计、可复验)。

    刀 [1580,2800](「蓝屏」之后的全部)与 protect [1200,1580] 无交。
    """
    wl = _wordline("大家好今天讲蓝屏修复的第一课")
    cuts = [{"inMs": 1580, "outMs": 2800, "reason": "manual", "conf": 0.95}]
    cl = rs_cut.build_cutlist(wl, cuts, {"protect": PROTECT_BLUE})
    assert cl["protect"] == PROTECT_BLUE
    assert [c["action"] for c in cl["cuts"] if c["reason"] == "manual"] == ["remove"]


def test_j2_protect_edge_touch_is_not_intrusion():
    """触边不算侵入:半开区间判定 —— 刀止于区间起点 / 起于区间终点都不冲突。"""
    before = [{"id": "x", "inMs": 0, "outMs": 1200, "action": "remove"}]     # 止于起点
    after = [{"id": "y", "inMs": 1580, "outMs": 2800, "action": "remove"}]   # 起于终点
    inside = [{"id": "z", "inMs": 0, "outMs": 1300, "action": "remove"}]     # 真侵入
    assert not rs_cut.protect_violations(before, PROTECT_BLUE, actions=("remove",))
    assert not rs_cut.protect_violations(after, PROTECT_BLUE, actions=("remove",))
    assert rs_cut.protect_violations(inside, PROTECT_BLUE, actions=("remove",))


def test_j2_apply_rejects_hand_edited_intruding_cut():
    """J2:--apply 是最后一道闸 —— 人工把侵入 protect 的刀改成 remove 也必须被拒。"""
    wl = _wordline("大家好今天讲蓝屏修复的第一课")
    cl = rs_cut.build_cutlist(wl, [], {})
    cl["protect"] = PROTECT_BLUE
    cl["cuts"].append({"id": "c901", "inMs": 1000, "outMs": 1600, "reason": "manual",
                       "conf": 0.95, "action": "remove", "note": "人工批注",
                       "guard": {"okByReason": True}})
    errs = rs_cut.protect_violations(cl["cuts"], cl["protect"])
    assert errs and "c901" in errs[0]


def test_j2_protect_priority_over_guard_downgrade():
    """优先级 protect > guard 三项:guard 本可降级 review 的刀,遇 protect 直接报错。"""
    wl = _wordline("大家好今天讲蓝屏修复的第一课")
    # 这刀 guard 必不过(wordClipped:切点落在「蓝」字内)——若按旧逻辑只是降级 review;
    # 但它同时侵入 protect → 必须报错,不允许「降级了事」
    cuts = [{"inMs": 1300, "outMs": 1500, "reason": "filler", "conf": 0.95}]
    with pytest.raises(ValueError, match="PROTECT"):
        rs_cut.build_cutlist(wl, cuts, {"protect": PROTECT_BLUE})


def test_j2_cli_protect_flag_lands_in_cutlist(tmp_path, monkeypatch):
    """CLI 面:--protect "a-b,c-d"(ms,与 keep 同单位)→ cutlist.protect 落盘可复验。"""
    wl = _wordline("大家好今天讲蓝屏修复的第一课")
    src = tmp_path / "wordline.json"
    src.write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "04_cut"
    monkeypatch.setattr(sys, "argv", [
        "rs_cut.py", str(src), "--detect", "all", "--out", str(out),
        "--protect", "1200-1580,2000-2800"])
    code, doc = _capture(rs_cut.main)
    assert code == 0 and doc["code"] == "CUT_OK", doc
    cl = json.loads((out / "cutlist.json").read_text(encoding="utf-8"))
    assert cl["protect"] == [{"startMs": 1200, "endMs": 1580},
                             {"startMs": 2000, "endMs": 2800}]
    assert not rs_cut.protect_violations(cl["cuts"], cl["protect"], actions=("remove",))


# ================================================================ J1/J3/J4 夹具

import copy  # noqa: E402
import re  # noqa: E402

FPS = 30
DUMMY_MP4 = b"not-a-real-mp4"      # 编译/门禁/干跑只查存在性;真素材只有写盘 e2e 需要


def _mk_project(tmp: Path, media_bytes: bytes | None = DUMMY_MP4) -> Path:
    """夹具工程:1 条 video 轨(2 段连续,第 2 段带转场)+ 1 条 audio 轨 + 字幕源。

    时间轴 0–2000–4400ms @30fps 全部落在帧网格上(60/132 帧整)。
    media_bytes=None 时不写素材(调用方自备真素材,写盘 e2e 用)。
    """
    root = tmp / "proj"
    (root / "01_materials").mkdir(parents=True)
    (root / "05_ir").mkdir(parents=True)
    if media_bytes:
        (root / "01_materials" / "a.mp4").write_bytes(media_bytes)
        (root / "01_materials" / "voice.mp3").write_bytes(media_bytes)
        (root / "01_materials" / "bgm.mp3").write_bytes(b"dummy-bgm")
    ir = {
        "version": 1, "slug": "dev-jy", "fps": FPS,
        "canvas": {"width": 1080, "height": 1920},
        "tracks": [
            {"kind": "video", "clips": [
                {"src": "01_materials/a.mp4", "startMs": 0, "durationMs": 2000,
                 "sourceInMs": 0, "fade": {"inMs": 200, "outMs": 0}},
                {"src": "01_materials/a.mp4", "startMs": 2000, "durationMs": 2400,
                 "sourceInMs": 0, "speed": 1.0, "volume": 0.5,
                 "transition": {"type": "fade", "durMs": 300}},
            ]},
            {"kind": "audio", "clips": [
                {"src": "01_materials/voice.mp3", "startMs": 0, "durationMs": 4400,
                 "role": "voice"},
            ]},
        ],
        "bgm": {"src": "01_materials/bgm.mp3", "gainDb": -18},
        "subtitle": {"source": "05_ir/wordline.json"},
    }
    (root / "05_ir" / "project.json").write_text(json.dumps(ir, ensure_ascii=False),
                                                 encoding="utf-8")
    (root / "05_ir" / "wordline.json").write_text(json.dumps({
        "segments": [
            {"start": 0.2, "end": 1.8, "text": "大家好今天讲蓝屏"},
            {"start": 2.2, "end": 4.3, "text": "修复的第一课"},
        ]}, ensure_ascii=False), encoding="utf-8")
    return root / "05_ir" / "project.json"


def _cfg(tmp: Path) -> dict:
    d = tmp / "drafts"
    cfg: dict = {"jianying59": {"draft_root": str(d),
                                "root_meta": str(d / "root_meta_info.json"),
                                "exe": ""}}
    real = REPO / "config.json"
    if real.is_file():     # 素材探测需要 ffmpeg_dir;沿用仓库本机配置
        cfg["ffmpeg_dir"] = json.loads(real.read_text(encoding="utf-8")).get("ffmpeg_dir", "")
    return cfg


def _good_plan(tmp: Path) -> tuple[dict, Path]:
    ir_path = _mk_project(tmp)
    doc = json.loads(ir_path.read_text(encoding="utf-8"))
    return rs_jy_draft.compile_draft_plan(doc, ir_path, None, []), ir_path


def _run_main(monkeypatch, argv: list[str]):
    """跑 rs_jy_draft.main,返回 (exit_code 或 SystemExit 码, 最后一个 JSON, 全部输出)。"""
    monkeypatch.setattr(sys, "argv", ["rs_jy_draft.py", *argv])
    buf = io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(buf):
            code = rs_jy_draft.main()
    except SystemExit as exc:                      # die() 的通道(JY_RUNNING 等)
        code = exc.code
    out = buf.getvalue()
    doc = json.loads([ln for ln in out.splitlines() if ln.strip().startswith("{")][-1])
    return code, doc, out


# ================================================================ J1 编译层

def test_j1_plan_structure_and_frame_math(tmp_path):
    """J1/判据 1:IR → 草稿计划(帧对齐中间表示);边界帧/微秒双记法且互相一致。"""
    plan, _ = _good_plan(tmp_path)
    assert plan["kind"] == "cutflow-jy-draft-plan" and plan["planVersion"] == 1
    assert plan["target"] == {"backend": "jianying", "version": "5.9",
                              "draftFormat": "plain"}
    assert rs_jy_draft.frames_of(100, 30) == 3
    assert rs_jy_draft.frame_to_us(252, 30) == 8_400_000
    assert rs_jy_draft._q(4400, 30) == (132, 4_400_000)
    v1 = plan["tracks"][0]
    assert v1["trackId"] == "V1" and v1["role"] == "main" and len(v1["segments"]) == 2
    s1, s2 = v1["segments"]
    assert (s1["startFrame"], s1["endFrame"], s1["durationFrames"]) == (0, 60, 60)
    assert s1["startUs"] == 0 and s1["durationUs"] == 2_000_000
    # 相邻段精确铺贴:前段终点 == 后段起点(帧与微秒同时成立)
    assert s2["startUs"] == s1["endUs"] and s2["startFrame"] == s1["endFrame"]
    assert s2["irRef"] == {"track": 0, "clip": 1, "startMs": 2000, "durationMs": 2400}
    assert plan["durationFrames"] == 132 and plan["durationUs"] == 4_400_000
    assert plan["expects"] == {"trackCount": 3, "segmentCount": 5, "mainDurationFrames": 132}
    assert rs_jy_draft.plan_gates(plan) == [], "好计划必须全门禁通过"


def test_j1_dry_run_prints_table_and_writes_nothing(tmp_path, monkeypatch):
    """J1/判据 1:--dry-run 打印「IR 片段 → 草稿片段」映射表,不写任何文件。"""
    ir_path = _mk_project(tmp_path)
    monkeypatch.setattr(rs_jy_draft, "load_config", lambda: _cfg(tmp_path))
    code, doc, out = _run_main(monkeypatch, [str(ir_path), "--dry-run"])
    assert code == 0 and doc["code"] == "JY_PLAN_DRY_RUN", doc
    table = out[:out.index('{"ok"')]
    assert "草稿计划映射" in table and "IR 片段" in table
    assert "tracks[0].clips[0]" in table and "V1-001" in table
    assert "转场←fade(叠化" in table, "转场映射必须出现在映射表里"
    assert "字幕 T1 共 2 条" in table
    assert not (tmp_path / "drafts").exists() or not any((tmp_path / "drafts").iterdir()), \
        "--dry-run 不得写任何文件"
    assert doc["data"]["plan"]["expects"]["trackCount"] == 3   # V1 + A1 + T1(bgm 假素材降级跳过)


def test_j1_dry_run_gate_fail_still_nonzero(tmp_path, monkeypatch):
    """--dry-run 门禁失败也非零退出(映射表照印,便于人读排障)。"""
    ir_path = _mk_project(tmp_path)
    (ir_path.parent.parent / "01_materials" / "a.mp4").unlink()   # 拆掉素材 → 门禁必挂
    monkeypatch.setattr(rs_jy_draft, "load_config", lambda: _cfg(tmp_path))
    code, doc, out = _run_main(monkeypatch, [str(ir_path), "--dry-run"])
    assert code == 4 and doc["code"] == "PLAN_GATE_FAIL"
    assert any("素材不存在" in g for g in doc["data"]["gates"])
    assert "草稿计划映射" in out, "失败时也必须给映射表"


# ================================================================ J3 草稿门禁

def _mutate(plan: dict, fn) -> dict:
    p = copy.deepcopy(plan)
    fn(p)
    return p


def test_j3_gate_rejects_frame_misalignment(tmp_path):
    """判据 3:故意把一个边界挪离帧网格(+100μs)→ 门禁必须拒绝。"""
    plan, _ = _good_plan(tmp_path)
    bad = _mutate(plan, lambda p: p["tracks"][0]["segments"][1].__setitem__(
        "startUs", p["tracks"][0]["segments"][1]["startUs"] + 100))
    errs = rs_jy_draft.plan_gates(bad)
    assert any("帧网格" in e for e in errs), errs


def test_j3_gate_rejects_overlap_and_gap(tmp_path):
    """判据 3:主轨重叠 / 黑场间隙都要被拒(转场不产生间隙,主轨必须无缝)。"""
    plan, _ = _good_plan(tmp_path)

    def mk_overlap(p):
        p["tracks"][0]["segments"][1]["startUs"] -= 100_000
        p["tracks"][0]["segments"][1]["startFrame"] -= 3

    errs = rs_jy_draft.plan_gates(_mutate(plan, mk_overlap))
    assert any("重叠" in e for e in errs), errs

    def mk_gap(p):
        s = p["tracks"][0]["segments"][1]
        s["startUs"] += 200_000
        s["startFrame"] += 6
        s["endUs"] += 200_000
        s["endFrame"] += 6

    errs = rs_jy_draft.plan_gates(_mutate(plan, mk_gap))
    assert any("黑场间隙" in e for e in errs), errs


def test_j3_gate_rejects_first_segment_off_zero_and_track_drift(tmp_path):
    """判据 3:主轨首段必须从 0 开始;轨道数/片段数/时长和漂移即拒绝。"""
    plan, _ = _good_plan(tmp_path)
    errs = rs_jy_draft.plan_gates(_mutate(
        plan, lambda p: p["tracks"][0]["segments"][0].__setitem__("startFrame", 1)))
    assert any("不从 0 开始" in e for e in errs), errs
    errs = rs_jy_draft.plan_gates(_mutate(
        plan, lambda p: p["expects"].__setitem__("trackCount", 99)))
    assert any("轨道数" in e for e in errs), errs
    errs = rs_jy_draft.plan_gates(_mutate(
        plan, lambda p: p["expects"].__setitem__("segmentCount", 2)))
    assert any("片段总数" in e for e in errs), errs
    errs = rs_jy_draft.plan_gates(_mutate(
        plan, lambda p: p["expects"].__setitem__("mainDurationFrames", 131)))
    assert any("时长和" in e for e in errs), errs


def test_j3_write_path_refuses_on_gate_fail(tmp_path, monkeypatch):
    """判据 3:门禁失败 = 拒绝写草稿,错误码非零(PLAN_GATE_FAIL,退出码 4)。"""
    ir_path = _mk_project(tmp_path)
    (ir_path.parent.parent / "01_materials" / "a.mp4").unlink()
    monkeypatch.setattr(rs_jy_draft, "load_config", lambda: _cfg(tmp_path))
    called = {"tasklist": False}
    real_run = rs_jy_draft.subprocess.run

    def spy_run(cmd, **kw):
        if cmd and cmd[0] == "tasklist":
            called["tasklist"] = True
        return real_run(cmd, **kw)

    monkeypatch.setattr(rs_jy_draft.subprocess, "run", spy_run)
    code, doc, _ = _run_main(monkeypatch, [str(ir_path)])
    assert code == 4 and doc["code"] == "PLAN_GATE_FAIL", doc
    assert not (tmp_path / "drafts").exists() or not any((tmp_path / "drafts").iterdir()), \
        "门禁失败不得写草稿目录"
    assert not called["tasklist"], "门禁在写盘前失败,不应走到剪映进程检测"


def test_j3_jy_running_guard_still_blocks_write(tmp_path, monkeypatch):
    """判据 4:写草稿前剪映未运行(JY_RUNNING 保留,门禁之后、写盘之前)。"""
    ir_path = _mk_project(tmp_path)
    monkeypatch.setattr(rs_jy_draft, "load_config", lambda: _cfg(tmp_path))

    class _R:
        returncode = 0
        stdout = "映像名称: JianyingPro.exe  PID: 1234".encode("utf-8")
        stderr = b""

    monkeypatch.setattr(rs_jy_draft.subprocess, "run", lambda cmd, **kw: _R())
    assert rs_jy_draft._jianying_running(_R().stdout.decode("utf-8"))
    code, doc, _ = _run_main(monkeypatch, [str(ir_path)])
    assert code == 4 and doc["code"] == "JY_RUNNING"
    assert not (tmp_path / "drafts").exists() or not any((tmp_path / "drafts").iterdir())


def test_j3_only_59_plain_drafts_allowed(tmp_path, monkeypatch):
    """判据 4(版本铁律):config 缺 jianying59 段 → 拒绝(NO_CONFIG,退出码 3)。
    本工具只写 5.9 明文草稿;11.3+ 加密草稿永不读写。"""
    ir_path = _mk_project(tmp_path)
    bad_cfg = dict(_cfg(tmp_path))
    bad_cfg.pop("jianying59")
    monkeypatch.setattr(rs_jy_draft, "load_config", lambda: bad_cfg)
    code, doc, _ = _run_main(monkeypatch, [str(ir_path), "--name", "dev_x"])
    assert code == 3 and doc["code"] == "NO_CONFIG"
    assert "11.3" in doc["message"] and "5.9" in doc["message"]


def test_j3_verify_written_draft_detects_drift(tmp_path):
    """写后回读闸:草稿结构与计划不符(轨道/片段数/时长)必须被指认。"""
    plan, _ = _good_plan(tmp_path)
    fake = tmp_path / "draft_content.json"
    fake.write_text(json.dumps({"tracks": [], "duration": 1}), encoding="utf-8")
    errs = rs_jy_draft.verify_written_draft(fake, plan)
    assert any("轨道数" in e for e in errs) and any("时长" in e for e in errs)


# ================================================================ J4 能力扩展核查

def test_j4_ir_expressed_fields_mapped_through_compile(tmp_path, monkeypatch):
    """J4:IR 已表达且草稿可承载的必须经编译层映射:转场/淡入淡出/画中画位置缩放/
    音量/音效 gainDb/BGM;变速的源时长换算走同一量化口径。"""
    plan, _ = _good_plan(tmp_path)
    s2 = plan["tracks"][0]["segments"][1]
    assert s2["transitionIn"] == {"irType": "fade", "map": "叠化", "durationUs": 300_000}
    assert s2["volume"] == 0.5
    s1 = plan["tracks"][0]["segments"][0]
    assert s1["fadeUs"] == {"in": 200_000, "out": 0}
    a1 = plan["tracks"][1]
    assert a1["trackId"] == "A1" and a1["segments"][0]["role"] == "voice"
    # BGM:ffprobe 实测以替身代替(真实探测路径由写盘 e2e 覆盖),这里验证映射口径
    monkeypatch.setattr(rs_jy_draft, "_probe_duration_ms",
                        lambda path, cfg, errors, where: 4400)
    plan2 = rs_jy_draft.compile_draft_plan(
        json.loads((tmp_path / "proj" / "05_ir" / "project.json").read_text(encoding="utf-8")),
        tmp_path / "proj" / "05_ir" / "project.json", None, [])
    bgm = next(t for t in plan2["tracks"] if t["role"] == "bgm")
    assert bgm["trackId"] == "A2"
    assert abs(bgm["segments"][0]["volume"] - 10 ** (-18 / 20)) < 1e-9
    assert bgm["segments"][0]["durationFrames"] == plan2["durationFrames"], "bgm 铺满全时间线"
    # 变速:源时长 = IR 时长 / speed,过同一帧量化
    assert rs_jy_draft.frames_of(2400 / 2.0, 30) == 36


def test_j4_bgm_degrades_with_warning_when_unavailable(tmp_path):
    """J4 降级面:bgm 素材不可探测时跳过 A2 但必须留 warning(不静默、不毁草稿)。"""
    plan, _ = _good_plan(tmp_path)     # 夹具 bgm.mp3 是假字节,ffprobe 必失败
    assert not any(t["role"] == "bgm" for t in plan["tracks"])
    assert any("bgm 不可用" in w for w in plan["warnings"])
    assert plan["compileErrors"] == [] and rs_jy_draft.plan_gates(plan) == []


def test_j4_sfx_pseudo_protocol_resolved(tmp_path):
    """J4:音效 assets_sfx: 伪协议 → 内置音效库真实路径;gainDb → 线性音量。"""
    root = tmp_path / "proj"
    (root / "05_ir").mkdir(parents=True)
    ir = root / "05_ir" / "project.json"
    fake_mov = root / "05_ir" / "main.mp4"
    fake_mov.write_bytes(b"x")
    ir.write_text(json.dumps({
        "version": 1, "slug": "sfx", "fps": 30,
        "canvas": {"width": 1080, "height": 1920},
        "tracks": [
            {"kind": "video", "clips": [{"src": str(fake_mov),
                                         "startMs": 0, "durationMs": 1000}]},
            {"kind": "audio", "clips": [{"src": "assets_sfx:whoosh", "startMs": 100,
                                         "role": "sfx", "gainDb": -6}]},
        ]}, ensure_ascii=False), encoding="utf-8")
    doc = json.loads(ir.read_text(encoding="utf-8"))
    plan = rs_jy_draft.compile_draft_plan(doc, ir, None, [])
    seg = plan["tracks"][1]["segments"][0]
    assert seg["src"] == str(rs_jy_draft.resolve_src("assets_sfx:whoosh", root))
    assert seg["src"].endswith("whoosh.mp3") and Path(seg["src"]).is_file()
    assert abs(seg["volume"] - 10 ** (-6 / 20)) < 1e-9


def test_j4_motion_and_ducking_recorded_as_unsupported(tmp_path):
    """J4:IR 表达了但 v1 草稿不承载的(motion 关键帧/ducking)→ 计划留显式 warning,
    不静默丢弃(J5 诚实验收「未支持」区的机器口径来源)。"""
    ir_path = _mk_project(tmp_path)
    doc = json.loads(ir_path.read_text(encoding="utf-8"))
    doc["tracks"][0]["clips"][0]["motion"] = {"in": "fadeIn", "inMs": 400}
    doc["bgm"]["ducking"] = True
    plan = rs_jy_draft.compile_draft_plan(doc, ir_path, None, [])
    assert any("motion.in=fadeIn" in w and "无草稿映射" in w for w in plan["warnings"])
    assert any("ducking" in w for w in plan["warnings"])
    assert rs_jy_draft.plan_gates(plan) == [], "warning 不影响门禁"


# ================================================================ J5 诚实验收文档

def test_j5_verification_doc_three_categories():
    """判据 5:诚实验收说明存在,且明确「已验证 / 未验证 / 拒绝」三类。"""
    doc = (REPO / "skills" / "cutflow" / "rules" / "jianying-verification.md")
    assert doc.is_file(), "缺 rules/jianying-verification.md"
    text = doc.read_text(encoding="utf-8")
    for section in ("已验证", "未验证", "拒绝"):
        assert section in text, f"诚实验收缺「{section}」类"
    assert "11.3" in text and "5.9" in text, "版本边界必须写明"


# ================================================================ J6 与 CutForge 出口对拍

CUTFORGE = REPO.parent / "cutforge"


def test_j6_cutforge_export_jianying_uses_same_script():
    """判据 6:cutforge export_jianying 必须编排同一个 rs_jy_draft.py(单一映射真相),
    不得自带第二套映射;scriptArgs 原样透传、不注旗标。"""
    lib = CUTFORGE / "crates" / "cutforge-mcp" / "src" / "lib.rs"
    if not lib.is_file():
        pytest.skip("cutforge 仓库不在同级(CI 由 cutforge 侧桥测试覆盖)")
    src = lib.read_text(encoding="utf-8")
    m = re.search(r'"stage_run"[^\n]*\n(?:.*\n){0,12}?.*_ => "rs_jy_draft\.py"', src)
    assert m, "export_jianying 编排组必须落 rs_jy_draft.py(同一脚本同一映射)"
    assert "export_jianying" in m.group(0), "export_jianying 必须在同一编排组内"
    # scriptArgs 原样透传(orchestrate 不注 --json;--json 白名单仅 rs_verify)
    assert 'let supports_json = matches!(script, "rs_verify.py");' in src
    schema = json.loads((CUTFORGE / "schemas" / "mcp-tools.json").read_text(encoding="utf-8"))
    tool = next(t for t in schema["tools"] if t["name"] == "export_jianying")
    assert "rs_jy_draft.py" in tool["description"], "schema 描述必须指向同一脚本"


def test_j6_same_ir_same_plan_via_bridge_invocation(tmp_path, monkeypatch):
    """判据 6:export_jianying 的调用形态(cwd=工程根,scriptArgs=
    ['05_ir/project.json','--name','x'])与直跑 CLI 得到同一计划、同一门禁结论
    —— 映射只此一层,两处入口不漂移(实写路径由真素材 e2e 覆盖)。"""
    ir_path = _mk_project(tmp_path)
    monkeypatch.setattr(rs_jy_draft, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.chdir(ir_path.parent.parent)     # orchestrate 以工程根为 cwd
    code, doc, _ = _run_main(monkeypatch, ["05_ir/project.json", "--name", "dev_bridge",
                                           "--dry-run"])
    assert code == 0 and doc["code"] == "JY_PLAN_DRY_RUN"
    # 直跑形态(绝对路径)与 export_jianying 调用形态 → 计划逐键一致
    code2, doc2, _ = _run_main(monkeypatch, [str(ir_path), "--dry-run"])
    assert code2 == 0 and doc2["code"] == "JY_PLAN_DRY_RUN"
    assert doc2["data"]["plan"] == doc["data"]["plan"], "同一 IR 必得同一草稿计划"


# ================================================================ J6 端到端(真素材写盘)

def _ffmpeg_bin() -> str | None:
    import rs_common
    try:
        cand = rs_common.ffmpeg_bin()
    except SystemExit:
        cand = "ffmpeg"
    return shutil.which(cand) or (cand if Path(cand).is_file() else None)


def test_j6_e2e_real_media_write_and_gates(tmp_path, monkeypatch):
    """J6 端到端:真素材工程真跑一次(剪映未运行时)产出 5.9 草稿 → 新门禁全过 →
    产物结构级断言(轨道/片段/时长);剪映运行中则诚实跳过(JY_RUNNING 铁律)。"""
    ff = _ffmpeg_bin()
    if not ff:
        pytest.skip("本机没有 ffmpeg,无法造真素材,跳过写盘 e2e")
    ir_path = _mk_project(tmp_path, media_bytes=None)
    mat = tmp_path / "proj" / "01_materials"
    a = mat / "a.mp4"
    subprocess.run([ff, "-y", "-v", "error",
                    "-f", "lavfi", "-i", "color=c=red:s=320x240:d=5:r=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:d=5",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(a)], check=True, timeout=300)
    # 音频素材必须不含视频轨(AudioMaterial 硬约束):人声与 bgm 用纯音轨
    voice = mat / "voice.mp3"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi",
                    "-i", "sine=frequency=600:d=5", "-c:a", "libmp3lame",
                    str(voice)], check=True, timeout=300)
    shutil.copy(voice, mat / "bgm.mp3")          # bgm 复用同一纯音频
    monkeypatch.setattr(rs_jy_draft, "load_config", lambda: _cfg(tmp_path))
    code, doc, _ = _run_main(monkeypatch, [str(ir_path), "--name", "dev_jy_e2e"])
    if doc.get("code") == "JY_RUNNING":
        pytest.skip("剪映正在运行:写盘 e2e 按铁律拒绝,跳过(诚实边界)")
    assert code == 0 and doc["code"] == "DRAFT_OK", doc
    assert doc["data"]["gates"]["all"] == "PASS"
    fold = Path(doc["data"]["draft_dir"])
    cj = json.loads((fold / "draft_content.json").read_text(encoding="utf-8"))
    assert Path(fold / "draft_meta_info.json").is_file()
    # 写后回读闸:落盘草稿与草稿计划逐轨逐段一致
    plan = rs_jy_draft.compile_draft_plan(
        json.loads(ir_path.read_text(encoding="utf-8")), ir_path, _cfg(tmp_path), [])
    assert rs_jy_draft.verify_written_draft(fold / "draft_content.json", plan) == []
    kinds = sorted(t["type"] for t in cj["tracks"])
    assert kinds == sorted(["video", "audio", "audio", "text"]), "V1 + A1 + A2(bgm) + T1"
    assert len(next(t for t in cj["tracks"] if t["type"] == "text")["segments"]) == 2
    v1 = next(t for t in cj["tracks"] if t["type"] == "video")
    assert len(v1["segments"]) == 2
    assert cj["duration"] == 4_400_000, "草稿时长 = 计划时长(μs)"
    assert cj.get("id") and cj.get("name") == "dev_jy_e2e"
    root_meta = json.loads((tmp_path / "drafts" / "root_meta_info.json")
                           .read_text(encoding="utf-8"))
    assert any(e["draft_name"] == "dev_jy_e2e" for e in root_meta["all_draft_store"])
