# -*- coding: utf-8 -*-
"""可插拔能力注册表门禁(ADR-0047 / 方案 §7.1 门禁 4·5 / §7.3 M3 验收线)。

①能力描述符门禁:每个描述符字段齐全(方案附录 C 必填)、detector/verify 真实存在、
  artifact 以 rs_paths 逻辑键开头、appliesTo 键真实存在于 registry.videoTypes、
  stage ∈ S0–S11;诚实纪律:detector 未落地的算法能力(rs_beat/rs_shot 等 M8/M9)
  不得提前登记 —— 参数必须与 detector 脚本常量同源;
②引擎无类型分支门禁:rs_run / rs_render / rs_cut / rs_ir 源码不得出现 videoType
  字符串比较分支,甚至不得出现 videoType 字样(引擎只认识「阶段」与「能力」);
③扩展成本证明(M3 硬验收):动态注册新类型 interview(仅三处数据:registry 条目 /
  分册 / 能力声明复用既有描述符)→ rs_intent 能编译 resolved、rs_run 阶段解析跑通,
  且 rs_run.py 源码 hash 前后一致(引擎零改动);
④能力缺失降级留痕:detector 不存在 = 能力未部署 → 产物含 degraded + trace +
  message;degrade.to=none → 阻断;旧工程缺 capabilities → registry 兜底 + WARN。

运行:pytest tests/test_capabilities.py -q
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
SKILL_BASE = REPO / "skills" / "cutflow"
CAPS_TEMPLATES = SKILL_BASE / "templates" / "capabilities"
RS_RUN_SRC = SCRIPTS / "rs_run.py"

sys.path.insert(0, str(SCRIPTS))

import rs_paths  # noqa: E402
import rs_run  # noqa: E402
import rs_intent  # noqa: E402
import rs_subtitle  # noqa: E402

REGISTRY = rs_intent.load_registry()
DESCRIPTORS = rs_run.load_capability_descriptors()
STAGES = {f"S{i}" for i in range(12)}          # S0–S11

# 附录 C 必填字段(可选仅 cacheable / notes)
REQUIRED_FIELDS = ("id", "label", "appliesTo", "stage", "detector", "depends",
                   "degrade", "params", "artifact", "verify")


def _descriptor_files() -> list[Path]:
    return [f for f in sorted(CAPS_TEMPLATES.glob("*.json")) if not f.name.startswith("_")]


def _sha1(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def _write_intent(root: Path, resolved: dict) -> None:
    d = root / rs_paths.p("brief")
    d.mkdir(parents=True, exist_ok=True)
    (d / "intent_decisions.json").write_text(
        json.dumps({"version": 1, "kind": "cutflow-intent-decisions", "resolved": resolved},
                   ensure_ascii=False), encoding="utf-8")


# ================================================================ ① 能力描述符门禁

def test_registry_declares_only_registered_descriptors():
    """registry 的 videoTypes.<id>.capabilities 必须指向已登记的描述符(诚实纪律)。"""
    for vt, meta in REGISTRY["videoTypes"].items():
        caps = meta.get("capabilities")
        assert isinstance(caps, list), f"videoTypes.{vt} 缺 capabilities 字段"
        for cid in caps:
            assert cid in DESCRIPTORS, \
                f"videoTypes.{vt} 声明了未登记能力 {cid}(先在 templates/capabilities/ 落描述符)"


@pytest.mark.parametrize("f", _descriptor_files(), ids=lambda f: f.stem)
def test_descriptor_schema_complete(f: Path):
    """附录 C 必填字段齐全且类型正确;id 与文件名一致(每能力一文件)。"""
    desc = json.loads(f.read_text(encoding="utf-8"))
    assert f.stem == desc.get("id"), f"{f.name}: 文件名须与能力 id 一致"
    for k in REQUIRED_FIELDS:
        assert k in desc, f"{f.name}: 缺必填字段 {k}(附录 C)"
    assert isinstance(desc["label"], str) and desc["label"]
    assert isinstance(desc["appliesTo"], list) and desc["appliesTo"]
    assert isinstance(desc["depends"], list)
    assert isinstance(desc["params"], dict) and desc["params"]
    dg = desc["degrade"]
    assert isinstance(dg, dict) and {"to", "trace", "message"} <= set(dg)
    assert all(isinstance(dg[k], str) and dg[k] for k in ("to", "trace", "message"))
    if "cacheable" in desc:
        assert isinstance(desc["cacheable"], bool)
    if "notes" in desc:
        assert isinstance(desc["notes"], str) and desc["notes"]


@pytest.mark.parametrize("f", _descriptor_files(), ids=lambda f: f.stem)
def test_descriptor_refs_are_real(f: Path):
    """门禁 #4 诚实纪律:detector / verify 真实存在;artifact 以 rs_paths 逻辑键开头。"""
    desc = json.loads(f.read_text(encoding="utf-8"))
    assert (SCRIPTS / desc["detector"]).is_file(), \
        f"{desc['id']}: detector 不存在({desc['detector']})—— 未落地的算法能力不得登记"
    assert (REPO / desc["verify"]).is_file(), f"{desc['id']}: verify 测试不存在"
    key, _, rest = str(desc["artifact"]).partition("/")
    assert key in rs_paths.STAGE_DIRS, f"{desc['id']}: artifact 必须以 rs_paths 逻辑键开头"
    assert rest, f"{desc['id']}: artifact 缺子路径"
    assert rs_run.capability_artifact_path(Path("."), desc["artifact"]) is not None


@pytest.mark.parametrize("f", _descriptor_files(), ids=lambda f: f.stem)
def test_descriptor_applies_to_and_stage_valid(f: Path):
    """appliesTo 键真实存在于 registry.videoTypes;stage ∈ S0–S11。"""
    desc = json.loads(f.read_text(encoding="utf-8"))
    unknown = [vt for vt in desc["appliesTo"] if vt not in REGISTRY["videoTypes"]]
    assert not unknown, f"{desc['id']}: appliesTo 含未登记 videoType:{unknown}"
    assert desc["stage"] in STAGES, f"{desc['id']}: stage 越界({desc['stage']})"


def test_descriptor_ids_unique():
    ids = [json.loads(f.read_text(encoding="utf-8")).get("id") for f in _descriptor_files()]
    assert len(ids) == len(set(ids)), "能力 id 必须全局唯一"


@pytest.mark.parametrize(("cap_id", "script", "pairs"), [
    # 诚实纪律加严:声明 params 必须与 detector 脚本内常量同源,不许账面漂移
    ("vision.greenscreen", "rs_greenscreen",
     [("borderFrac", "BORDER_FRAC"), ("overallFrac", "OVERALL_FRAC"), ("maxCv", "MAX_CV")]),
    ("cut.dead-air", "rs_cut", [("minMs", "DEAD_AIR_MIN_MS")]),
    ("qc.black-frame", "rs_sync",
     [("blackMinSec", "QC_BLACK_MIN_S"), ("freezeMinSec", "QC_FREEZE_MIN_S")]),
    # M8 五能力脚本(方案 §5.5.1/§5.5.2/§5.5.4;常量与描述符同源,改一处必改两处)
    ("audio.beat", "rs_beat",
     [("onsetWindowMs", "ONSET_WINDOW_MS"), ("onsetMinGapMs", "ONSET_MIN_GAP_MS"),
      ("onsetThresholdK", "ONSET_THRESHOLD_K"), ("pcmRate", "PCM_RATE"),
      ("bpmMin", "BPM_MIN"), ("bpmMax", "BPM_MAX"), ("phaseTol", "PHASE_TOL")]),
    ("audio.downbeat", "rs_beat", [("meter", "DEFAULT_METER")]),
    ("audio.stem", "rs_beat", [("onsetMinGapMs", "ONSET_MIN_GAP_MS")]),
    ("vision.shot", "rs_shot",
     [("threshold", "SCENE_THRESHOLD"), ("minShotMs", "MIN_SHOT_MS"),
      ("sampleFps", "SAMPLE_FPS"), ("downscaleWidth", "DOWNSCALE_W")]),
    ("vision.track", "rs_reframe",
     [("smoothWindow", "SMOOTH_WIN"), ("maxShiftPxPerSec", "MAX_SHIFT_PX_PER_SEC")]),
    ("vision.reframe", "rs_reframe",
     [("defaultScale", "DEFAULT_SCALE"), ("smoothWindow", "SMOOTH_WIN"),
      ("maxShiftPxPerSec", "MAX_SHIFT_PX_PER_SEC")]),
    ("text.broll", "rs_broll",
     [("topK", "TOP_K"), ("filenameWeight", "FILENAME_WEIGHT"),
      ("minTokenLen", "MIN_TOKEN_LEN")]),
    ("screen.cursor", "rs_screen",
     [("cursorTraceWidth", "CURSOR_TRACE_W"), ("cursorStride", "CURSOR_STRIDE"),
      ("cursorJumpMaxPct", "CURSOR_JUMP_MAX_PCT")]),
    ("screen.zoom", "rs_screen",
     [("zoomFactor", "ZOOM_FACTOR"), ("cursorDwellMs", "CURSOR_DWELL_MS")]),
    ("screen.keys", "rs_screen", [("keyCardMinMs", "KEY_CARD_MIN_MS")]),
    ("screen.redact", "rs_screen",
     [("redactMaxBoxes", "REDACT_MAX_BOXES"), ("candidateConf", "REDACT_CAND_CONF")]),
    # M8 第二波(方案 §5.5.3 短剧/影视解说;常量与描述符同源,改一处必改两处)
    ("sub.pair", "rs_subtitle",
     [("quoteReserveChars", "QUOTE_RESERVE_CHARS")]),
    ("drama.hook", "rs_sync",
     [("hookEverySec", "HOOK_EVERY_SEC"), ("twistEverySec", "TWIST_EVERY_SEC"),
      ("anchorWindowSec", "ANCHOR_WINDOW_SEC"), ("wasteFrameMinSec", "WASTE_FRAME_MIN_S")]),
])
def test_descriptor_params_match_detector_constants(cap_id, script, pairs):
    import importlib
    mod = importlib.import_module(script)
    desc = DESCRIPTORS[cap_id]
    for pname, cname in pairs:
        assert pname in desc["params"], f"{cap_id}: params 缺 {pname}"
        assert desc["params"][pname] == getattr(mod, cname), \
            f"{cap_id}: params.{pname} 与 {script}.{cname} 漂移"


# ================================================================ ② 引擎无类型分支门禁

def test_engine_has_no_videotype_branch():
    """门禁 #5:rs_run / rs_render / rs_cut / rs_ir 无 videoType 字符串比较分支。

    引擎只认识「阶段」与「能力」。videoType 唯一合法触点是【数据字段的键】
    (如 resolved.get("videoType") 作 registry 查表键 —— 数据驱动路由);禁止它
    进任何比较(==/!=/is)。另按 registry 类型键 + 预留键扫字面量比较,兜住
    不经 videoType 命名的隐蔽分支(如 `if vt == "混剪"`)。
    """
    engine_files = ["rs_run.py", "rs_render.py", "rs_cut.py", "rs_ir.py"]
    type_keys = (set(REGISTRY["videoTypes"])
                 | {"interview", "product", "screen-recording", "drama", "film-commentary"})
    esc = "|".join(re.escape(k) for k in sorted(type_keys))
    cmp_literal_re = re.compile(
        r"(==|!=)\s*['\"](" + esc + r")['\"]|['\"](" + esc + r")['\"]\s*(==|!=)")
    offenders: list[str] = []
    for name in engine_files:
        src = (SCRIPTS / name).read_text(encoding="utf-8")
        for m in re.finditer("videoType", src):
            window = " ".join(src[max(0, m.start() - 40):m.end() + 40].split())
            if re.search(r"(==|!=|\bis\b)", window):
                offenders.append(f"{name}: videoType 参与比较:…{window}…")
        for m in cmp_literal_re.finditer(src):
            offenders.append(f"{name}: 疑似按类型字面量分支:{m.group(0)!r}")
    assert not offenders, "引擎不得按 videoType 分支(ADR-0018/0047):" + ";".join(offenders)


# ================================================================ ③ 扩展成本证明(M3 硬验收)

def _interview_registry() -> dict:
    """三处数据之一:registry 加 videoTypes.interview + 一条 entries(缺省条目)。"""
    reg = copy.deepcopy(REGISTRY)
    reg["videoTypes"]["interview"] = {
        "label": "访谈", "doc": "rules/video-types/访谈.md",
        "capabilities": ["qc.black-frame", "cut.dead-air"]}
    reg["entries"].append({
        "id": "interview-default", "label": "访谈·通用",
        "videoTypes": ["interview"], "defaultFor": ["interview"],
        "platform": "douyin", "ratio": "9x16", "style": "talkshow-bold",
        "styleTemplate": "talkshow-bold", "pacing": "normal",
        "cards": {"template": "info", "density": "少"}, "bgmLibrary": None,
        "match": {"keywords": ["访谈", "对话", "采访"]},
        "overrides": ["platform", "ratio", "style", "pacing"]})
    return reg


def test_new_video_type_interview_needs_no_engine_change(tmp_path, monkeypatch):
    """M3 硬验收:新增 interview 只动三处数据 → 编译/挂载全跑通,rs_run.py 零改动。"""
    h0 = _sha1(RS_RUN_SRC)
    reg = _interview_registry()
    # 数据之一:registry 条目(临时副本上注册,不动仓库真相源)
    tmp_reg = tmp_path / "registry.json"
    tmp_reg.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    monkeypatch.setattr(rs_intent, "REGISTRY_PATH", tmp_reg)
    # 数据之二:分册 rules/video-types/访谈.md(落到真实目录,验完即清)
    doc = SKILL_BASE / "rules" / "video-types" / "访谈.md"
    doc.write_text("# 访谈(测试动态注册的类型)\n\n验收清单:说话人分离(暂缓,见 5.5.5)。\n",
                   encoding="utf-8")
    # 数据之三:capabilities 复用既有描述符(已在 _interview_registry 里声明,无新增文件)
    try:
        assert doc.is_file(), "第三处数据(分册)应真实落盘"
        platforms = rs_subtitle.load_platforms()
        brief = {"videoType": "interview", "platform": "douyin", "title": "访谈工程"}
        assert rs_intent.validate(brief, {}, reg, platforms) == [], "interview 应通过编译校验"
        out = rs_intent.compile_intent(brief, {}, "", reg, platforms)
        r = out["resolved"]
        assert r["videoType"] == "interview"
        assert r["capabilities"] == ["qc.black-frame", "cut.dead-air"]
        cap_dec = [d for d in out["decisions"] if d["id"].startswith("intent:capability.")]
        assert len(cap_dec) == 2 and all(d["source"] == "registry" for d in cap_dec), \
            "能力声明须逐条进 decision_log(source=registry)"

        # rs_run 阶段解析跑通:intent_decisions 落盘 → resolved_capabilities → 按 stage 挂载
        root = tmp_path / "proj"
        rs_paths.ensure(root)
        _write_intent(root, r)
        caps, warns = rs_run.resolved_capabilities(root)
        assert caps == ["qc.black-frame", "cut.dead-air"] and not warns
        s9 = next(s for s in rs_run.spec(root) if s["id"] == "S9")
        assert [d["id"] for d in rs_run.capabilities_for_stage(root, s9)] == ["qc.black-frame"]
        s2 = next(s for s in rs_run.spec(root) if s["id"] == "S2")
        assert [d["id"] for d in rs_run.capabilities_for_stage(root, s2)] == ["cut.dead-air"]
    finally:
        doc.unlink(missing_ok=True)
    assert _sha1(RS_RUN_SRC) == h0, "新增类型不得改动 rs_run.py(引擎零改动是 M3 验收线)"


# ================================================================ ④ 降级留痕 / 阻断 / 兜底

@pytest.fixture()
def cap_project(tmp_path):
    root = tmp_path / "proj"
    rs_paths.ensure(root)
    return root


def _mock_caps_dir(tmp_path: Path, degrade_to: str) -> Path:
    d = tmp_path / "caps"
    d.mkdir(exist_ok=True)
    (d / "mock.cap.json").write_text(json.dumps({
        "id": "mock.cap", "label": "mock", "appliesTo": ["talking-head"], "stage": "S0",
        "detector": "rs_nonexistent.py", "depends": [],
        "degrade": {"to": degrade_to, "trace": "mockDegraded", "message": "组件未部署"},
        "params": {"x": 1}, "artifact": "materials/manifest.json",
        "verify": "tests/test_capabilities.py"}, ensure_ascii=False), encoding="utf-8")
    return d


def test_missing_detector_degrades_with_trace(cap_project, tmp_path, monkeypatch):
    """④ detector 文件不存在 = 能力未部署 → 降级留痕,不崩。"""
    _write_intent(cap_project, {"videoType": "talking-head",
                                "capabilities": ["mock.cap"]})
    monkeypatch.setattr(rs_run, "CAPS_DIR", _mock_caps_dir(tmp_path, "manual-check"))
    st = {"id": "S0", "name": "基础素材", "scripts": ["rs_ingest.py"]}
    info: dict = {}
    ok, msg = rs_run.run_stage_capabilities(cap_project, st, info)
    assert ok and "降级" in msg
    rep = json.loads(rs_run.capabilities_report_path(cap_project).read_text(encoding="utf-8"))
    entry = rep["stages"]["S0"][0]
    assert entry["degraded"] is True and entry["status"] == "degraded"
    assert entry["trace"] == "mockDegraded", "产物须带描述符 trace 字段"
    assert entry["message"] == "组件未部署", "产物须带描述符 message"
    assert info["capabilities"][0]["reason"].startswith("detector 未部署")
    # 留痕同步进 pipeline.json 的 decision_log(重跑幂等覆盖)
    log = rs_run.load_decision_log(cap_project)
    hit = [d for d in log if d.get("id") == "cap:S0:mock.cap"]
    assert hit and hit[0]["trace"] == "mockDegraded"


def test_undegradable_capability_blocks(cap_project, tmp_path, monkeypatch):
    """④ degrade.to = none 的能力缺失 → 阻断(调用方按阶段失败非零退出处置)。"""
    _write_intent(cap_project, {"videoType": "talking-head",
                                "capabilities": ["mock.cap"]})
    monkeypatch.setattr(rs_run, "CAPS_DIR", _mock_caps_dir(tmp_path, "none"))
    ok, msg = rs_run.run_stage_capabilities(cap_project, {"id": "S0", "name": "x"})
    assert not ok and "阻断" in msg


def test_capability_ok_when_artifact_present(cap_project, tmp_path, monkeypatch):
    """④ 对照组:detector 是阶段自有脚本且产物在盘 → status=ok,零降级零告警。"""
    _write_intent(cap_project, {"videoType": "talking-head",
                                "capabilities": ["mock.cap"]})
    monkeypatch.setattr(rs_run, "CAPS_DIR", _mock_caps_dir(tmp_path, "manual-check"))
    # mock.cap 的 detector 指向不存在的脚本;对照组把 detector 换成真实阶段自有脚本
    (tmp_path / "caps" / "mock.cap.json").write_text(json.dumps({
        "id": "mock.cap", "label": "mock", "stage": "S0", "detector": "rs_ingest.py",
        "degrade": {"to": "manual-check", "trace": "mockDegraded", "message": "x"},
        "params": {}, "artifact": "materials/manifest.json"}, ensure_ascii=False),
        encoding="utf-8")
    (cap_project / rs_paths.p("materials") / "manifest.json").write_text("{}", encoding="utf-8")
    ok, msg = rs_run.run_stage_capabilities(
        cap_project, {"id": "S0", "name": "x", "scripts": ["rs_ingest.py"]})
    assert ok and msg == ""
    rep = json.loads(rs_run.capabilities_report_path(cap_project).read_text(encoding="utf-8"))
    assert rep["stages"]["S0"][0] == {"id": "mock.cap", "stage": "S0",
                                      "artifact": "materials/manifest.json",
                                      "status": "ok", "degraded": False}


def test_legacy_project_falls_back_to_registry(cap_project, capsys):
    """旧工程缺 resolved.capabilities → 按 registry 兜底 + WARN capabilitiesMissing。"""
    caps, warns = rs_run.resolved_capabilities(cap_project)
    assert warns == ["capabilitiesMissing"]
    _write_intent(cap_project, {"videoType": "talking-head"})   # 旧编译产物无 capabilities
    caps, warns = rs_run.resolved_capabilities(cap_project)
    assert caps == REGISTRY["videoTypes"]["talking-head"]["capabilities"]
    assert warns == ["capabilitiesMissing"]
    # 挂载路径同样兜底:registry 声明的 qc.black-frame 正确挂到 S9,并打印 WARN
    s9 = next(s for s in rs_run.spec(cap_project) if s["id"] == "S9")
    mounted = rs_run.capabilities_for_stage(cap_project, s9)
    assert [d["id"] for d in mounted] == ["qc.black-frame"]
    assert "capabilitiesMissing" in capsys.readouterr().err


def test_descriptor_without_stage_match_is_not_mounted(cap_project):
    """能力声明了但 stage 不在本阶段 → 不挂载(引擎按 stage 装配,不按类型)。"""
    _write_intent(cap_project, {"videoType": "talking-head",
                                "capabilities": ["qc.black-frame"]})
    s0 = next(s for s in rs_run.spec(cap_project) if s["id"] == "S0")
    assert rs_run.capabilities_for_stage(cap_project, s0) == []
