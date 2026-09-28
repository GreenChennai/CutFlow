"""T5.3b 判据:12 个真实工程快照的 --status / --explain / rebuild 结论固化,零 diff。

盘面矩阵(视频类型 × 改动状态,详见 tests/snapshots/README.md):
  口播/混剪/vlog/教程 × 干净盘面 / 单阶段 stale / 多阶段 stale /
  参数 stale / 进行中新盘面 / 声明内降级 / 产物缺失必须停。

固化内容(每盘面一份 tests/snapshots/<盘面>.json):
  · status  —— rs_run.cmd_status 的完整载荷(经真实 emit 协议);
  · explain —— rs_run.cmd_explain 的逐阶段载荷(verdict / verdictLabel /
    compared 逐组对账;contract 块盘面无关不入快照,另行门禁);
  · rebuild —— 级联起点 / 必须停 / 降级继续 / 一键重建入口 的决策结论。

归一化纪律(不为快照改产线代码):
  · hash(≥8 位十六进制,含 staleReason 里的 10 位前缀)→ `<hash>`;
  · 工程 tmp 根路径 → `<ROOT>`;路径分隔符统一 `/`;
  · 脚本 hash 用每盘面独立的 fake 脚本目录(monkeypatch rs_run.SCRIPTS_DIR,
    同 tests/test_pipeline_states.py 惯例),绝不触碰仓库内真实脚本;
  · external_versions 冻结(ASR 配置是环境量,不属于盘面状态)。

再生:python tests/test_snapshots.py(何时应更新见 tests/snapshots/README.md)。
运行:pytest tests/test_snapshots.py -q
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
sys.path.insert(0, str(SCRIPTS))

import rs_common  # noqa: E402
import rs_paths  # noqa: E402
import rs_run  # noqa: E402

ORDER = [f"S{i}" for i in range(12)]
FAKE_SCRIPTS = ("rs_ingest.py", "rs_greenscreen.py", "rs_align.py", "rs_cut.py",
                "rs_ir.py", "rs_render.py", "rs_brand.py", "rs_sfx.py",
                "rs_subtitle.py", "textopt.py", "segmentation.py", "rs_sync.py",
                "rs_meta.py")
# 一键重建薄壳落点(INIT_MAP 语义:改哪个文件夹点哪个 rebuild.py)
REBUILD_SHELL = {
    "S2": rs_paths.p("cut") + "/rebuild.py",
    "S3": rs_paths.p("timeline") + "/rebuild.py",
    "S4": rs_paths.p("assets") + "/artboard/rebuild.py",
    "S8": rs_paths.p("output") + "/rebuild.py",
}

# ---------------------------------------------------------------- 盘面构造


def _w(root: Path, rel: str, content="x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        p.write_text(content, encoding="utf-8")
    else:
        p.write_bytes(content)


BRIEF = {
    "talking-head": "# 简报 — 口播\n- videoType:`talking-head`\n- 每卡字数: 12\n",
    "mixcut": "# 简报 — 混剪\n- videoType:`mixcut`\n- 每卡字数: 12\n",
    "vlog": "# 简报 — vlog\n- videoType:`vlog`\n- 每卡字数: 12\n",
    "tutorial": "# 简报 — 教程\n- videoType:`tutorial`\n- 每卡字数: 12\n",
}


def _build_board(root: Path, *, video_type: str, materials: int, has_cards: bool,
                 has_variants: bool, has_terms: bool = False,
                 done_upto: int = 11) -> None:
    """按盘面参数铺一个最小合成工程:文件存在性跟随阶段完成度(done_upto),
    账只落到完成过的阶段 ——「进行中新盘面」不会有未来的产物。"""
    rs_paths.ensure(root)
    _w(root, f"{rs_paths.p('brief')}/brief.md", BRIEF[video_type])
    if has_cards:
        _w(root, f"{rs_paths.p('brief')}/cards.json", '{"cards": [{"id": "c1"}]}')
        if done_upto >= 4:      # S4 产物:--apply 写入的 appliedAt 标记
            _w(root, f"{rs_paths.p('assets')}/artboard/manifest.json",
               '{"version": 1, "appliedAt": "2026-09-27T00:00:00+08:00"}')
    if has_terms:
        _w(root, f"{rs_paths.p('brief')}/terms.txt", "CutFlow\n剪映\n")
    items = []
    for i in range(materials):
        name = f"m{i}.mp4"
        _w(root, f"{rs_paths.p('materials')}/{name}", f"fake-video-bytes-{i}")
        items.append({"file": name, "probe": "ok"})
    _w(root, f"{rs_paths.p('materials')}/manifest.json",
       json.dumps({"version": 1, "items": items}, ensure_ascii=False))
    if done_upto >= 2:          # S2 产物(apply 副本随粗剪完成落盘)
        _w(root, f"{rs_paths.p('cut')}/cutlist.json", '{"cuts": []}')
        _w(root, f"{rs_paths.p('cut')}/cutlist.applied.json", '{"cuts": []}')
    if done_upto >= 1:          # S1 产物
        _w(root, f"{rs_paths.p('timeline')}/wordline.json",
           '{"chars": [], "sentences": []}')
    if done_upto >= 3:          # S3 产物
        _w(root, f"{rs_paths.p('timeline')}/project.json", '{"version": 1}')
    if has_variants and done_upto >= 5:      # S5 产物
        _w(root, f"{rs_paths.p('timeline')}/variants.json", '{"variants": ["logo"]}')
    if done_upto >= 6:          # S6 产物
        _w(root, f"{rs_paths.p('timeline')}/sfx_draft.json", '{"events": []}')
    if done_upto >= 7:          # S7 产物
        _w(root, f"{rs_paths.p('output')}/subtitles.ass", "[Script Info]\n")
    if done_upto >= 8:          # S8 产物
        _w(root, f"{rs_paths.p('output')}/final/final_916.mp4", b"\x00\x01fake-mp4")
    if has_variants and done_upto >= 5:      # S5 产物(品牌变体成片)
        _w(root, f"{rs_paths.p('output')}/branded/成片_916_logo.mp4", b"\x00\x02fake-mp4")
    if done_upto >= 9:          # S9 产物
        _w(root, f"{rs_paths.p('output')}/sync_report.md", "# sync\n")
        _w(root, f"{rs_paths.p('output')}/sync_rows.json", "[]")
    if done_upto >= 10:         # S10 产物
        _w(root, f"{rs_paths.p('output')}/metadata.json", '{"title": "t"}')
    if done_upto >= 11:         # S11 产物
        _w(root, f"{rs_paths.p('output')}/deliverables.md", "# 交付\n")
    for st in rs_run.spec(root):
        sid_num = int(st["id"][1:])
        if sid_num > done_upto:
            continue
        if st["id"] == "S5" and not has_variants:
            continue          # 无变体声明:S5 保持无账(声明内降级盘面)
        rs_run.record_stage_done(root, st)


# 每盘面的改动函数(在落账之后施加)

def _m_subtitle(root):     # 手改字幕产物(带外改写)
    _w(root, f"{rs_paths.p('output')}/subtitles.ass", "[Script Info]\n; 手改\n")


def _m_ir(root):           # 带外改 IR
    _w(root, f"{rs_paths.p('timeline')}/project.json", '{"version": 1, "x": 1}')


def _m_cutlist(root):      # 改 cutlist(applied 副本未动)
    _w(root, f"{rs_paths.p('cut')}/cutlist.json", '{"cuts": [{"action": "keep"}]}')


def _m_delete_out(root):   # 外删产物
    (root / rs_paths.p("output") / "subtitles.ass").unlink()


def _m_meta(root):         # 外改产物
    _w(root, f"{rs_paths.p('output')}/metadata.json", '{"title": "t2"}')


def _m_script(root):       # 工具升级(本盘面 fake 脚本目录内的 rs_subtitle.py 变更)
    (rs_run.SCRIPTS_DIR / "rs_subtitle.py").write_text("# fake rs_subtitle.py v2\n",
                                                       encoding="utf-8")


def _m_brief_param(root):  # 改 brief 参数行(每卡字数 12 → 10)
    _w(root, f"{rs_paths.p('brief')}/brief.md",
       BRIEF["vlog"].replace("每卡字数: 12", "每卡字数: 10"))


# (盘面名, 类型中文名, 状态中文名, 描述, 构造参数, 改动函数)
BOARDS = [
    ("01-koubo-clean", "口播", "干净盘面",
     "单素材口播工程,12 阶段账实相符。",
     dict(video_type="talking-head", materials=1, has_cards=False, has_variants=True),
     None),
    ("02-koubo-stale-subtitle", "口播", "单阶段 stale(手改字幕)",
     "字幕产物被 rs_run 之外改写 → S7 带外 stale,S8/S9 级联。",
     dict(video_type="talking-head", materials=1, has_cards=False, has_variants=True),
     _m_subtitle),
    ("03-koubo-stale-ir", "口播", "多阶段 stale(带外改 IR)",
     "project.json 带外改 → S3 起多阶段 stale(含人工 S4)。",
     dict(video_type="talking-head", materials=1, has_cards=False, has_variants=True),
     _m_ir),
    ("04-koubo-fresh-s3", "口播", "进行中新盘面 + 声明内降级",
     "仅跑到 S3:S4 无卡片计划 / S5 无变体声明 → 降级继续;S6 起缺账必停。",
     dict(video_type="talking-head", materials=1, has_cards=False,
          has_variants=False, done_upto=3),
     None),
    ("05-hunjian-clean", "混剪", "干净盘面",
     "三素材混剪工程(含品牌变体),账实相符。",
     dict(video_type="mixcut", materials=3, has_cards=False, has_variants=True),
     None),
    ("06-hunjian-stale-cutlist", "混剪", "单阶段 stale(改 cutlist)",
     "cutlist.json 带外改而 applied 副本未动 → 仅 S2 stale(诚实口径)。",
     dict(video_type="mixcut", materials=3, has_cards=False, has_variants=True),
     _m_cutlist),
    ("07-hunjian-multistale", "混剪", "多阶段 stale(外删 + 外改产物)",
     "外删字幕 + 外改 metadata → S7 missing,S8/S9/S10/S11 连带。",
     dict(video_type="mixcut", materials=3, has_cards=False, has_variants=True),
     lambda root: (_m_delete_out(root), _m_meta(root))),
    ("08-hunjian-stale-script", "混剪", "单阶段 stale(工具升级)",
     "rs_subtitle.py 脚本 hash 变化 → 仅 S7 stale(tool 组证据)。",
     dict(video_type="mixcut", materials=3, has_cards=False, has_variants=True),
     _m_script),
    ("09-vlog-clean-degrade", "vlog", "干净盘面 + S5 降级",
     "工程未声明品牌变体:S5 无账 + missing → 按声明降级继续。",
     dict(video_type="vlog", materials=4, has_cards=False, has_variants=False),
     None),
    ("10-vlog-stale-brief-param", "vlog", "参数 stale(改 brief 参数行)",
     "每卡字数 12→10:S0/S10 因 brief.md 变 stale,S7 因 params 变 stale。",
     dict(video_type="vlog", materials=4, has_cards=False, has_variants=False),
     _m_brief_param),
    ("11-jiaocheng-clean", "教程", "干净盘面(卡片 + 术语表)",
     "教程工程:卡片计划 + appliedAt 标记 + terms.txt 进参数快照。",
     dict(video_type="tutorial", materials=2, has_cards=True, has_variants=True,
          has_terms=True),
     None),
    ("12-jiaocheng-missing-stop", "教程", "产物缺失必须停",
     "外删 subtitles.ass → S7 missing 无降级通道 → stop,S8/S9 连带 stale。",
     dict(video_type="tutorial", materials=2, has_cards=True, has_variants=True,
          has_terms=True),
     _m_delete_out),
]
BOARD_NAMES = [b[0] for b in BOARDS]

# ---------------------------------------------------------------- 捕获与归一化


def _isolate(fake_dir: Path):
    """隔离环境:fake 脚本目录 + 冻结外部服务版本(返回还原函数)。"""
    for name in FAKE_SCRIPTS:
        (fake_dir / name).write_text(f"# fake {name}\n", encoding="utf-8")
    old_scripts, old_ext = rs_run.SCRIPTS_DIR, rs_run.external_versions
    rs_run.SCRIPTS_DIR = fake_dir
    rs_run.external_versions = lambda: {"detector": "cutflow-1.0"}
    return lambda: (setattr(rs_run, "SCRIPTS_DIR", old_scripts),
                    setattr(rs_run, "external_versions", old_ext))


def _cli_json(fn, *args) -> dict:
    """跑真实 cmd_* 入口,经统一 emit 协议捕获 JSON 载荷与退出码。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(*args)
    lines = [ln for ln in buf.getvalue().strip().splitlines() if ln.startswith("{")]
    doc = json.loads(lines[-1])
    assert doc["ok"] is True, doc
    return {"exitCode": rc, "code": doc["code"], "data": doc["data"]}


def rebuild_decision(explains: dict) -> dict:
    """由逐阶段 verdict 推导重建决策结论(与 rules/pipeline-state.md 同口径):
    停点必须先处置;stale 从最早阶段级联重跑;降级项按声明跳过。"""
    rerun = [sid for sid in ORDER if explains[sid]["verdict"] == "rerun"]
    stops = [sid for sid in ORDER if explains[sid]["verdict"] == "stop"]
    degrades = [sid for sid in ORDER if explains[sid]["verdict"] == "degrade-continue"]
    keep_n = sum(1 for sid in ORDER if explains[sid]["verdict"] == "keep")
    parts: list[str] = []
    if stops:
        parts.append(f"先处置停点 {'、'.join(stops)}(缺账/失败:补跑或修复)")
    if rerun:
        start = rerun[0]
        shell = REBUILD_SHELL.get(start, "rebuild.py(根,全量)")
        parts.append(f"从 {start} 级联重跑(rs_run --from {start};一键入口 {shell})")
    if parts:
        if degrades:
            parts.append(f"降级项 {'、'.join(degrades)} 按声明跳过")
        action = ";".join(parts)
    elif keep_n == len(ORDER):
        action = "缓存全命中,无需重跑"
    else:
        action = "按声明降级继续,无需重跑"
    return {"cascadeFrom": rerun[0] if rerun else None, "rerun": rerun,
            "stop": stops, "degrade": degrades, "keepCount": keep_n,
            "action": action}


def capture_board(root: Path) -> dict:
    """捕获一个盘面的 status / explain(S0..S11)/ rebuild 决策结论。"""
    status = _cli_json(rs_run.cmd_status, root)
    explains = {sid: _cli_json(rs_run.cmd_explain, root, sid)["data"] for sid in ORDER}
    for p in explains.values():
        p.pop("contract")     # 契约盘面无关(test_stages_contract.py 另行门禁)
    return {"status": status, "explain": explains, "rebuild": rebuild_decision(explains)}


_HEX = re.compile(r"[0-9a-f]{8,}")


def normalize(obj, root: Path):
    """快照归一化:hash → <hash>,工程根 → <ROOT>,分隔符统一 /。"""
    if isinstance(obj, str):
        s = obj.replace("\\", "/")
        s = _HEX.sub("<hash>", s)
        return s.replace(str(root).replace("\\", "/"), "<ROOT>")
    if isinstance(obj, list):
        return [normalize(x, root) for x in obj]
    if isinstance(obj, dict):
        return {k: normalize(v, root) for k, v in obj.items()}
    return obj


def build_and_capture(workdir: Path, name: str) -> dict:
    """构造一个盘面 → 施加改动 → 捕获 → 归一化(返回可入库的快照文档)。"""
    board = next(b for b in BOARDS if b[0] == name)
    _name, vtype, state, desc, kwargs, mutate = board
    meta = {"board": name, "videoType": vtype, "state": state, "desc": desc,
            "regen": "python tests/test_snapshots.py"}
    fake = workdir / f"{name}-fakescripts"
    fake.mkdir(parents=True)
    restore = _isolate(fake)
    try:
        root = workdir / name
        _build_board(root, **kwargs)
        if mutate is not None:
            mutate(root)
        doc = {"meta": meta, **capture_board(root)}
    finally:
        restore()
    return normalize(doc, workdir / name)


# ---------------------------------------------------------------- pytest 门禁

@pytest.mark.parametrize("name", BOARD_NAMES)
def test_snapshot_zero_diff(name):
    """快照零 diff:重算盘面结论与固化 JSON 完全一致(键序无关)。"""
    snap_file = SNAPSHOTS / f"{name}.json"
    assert snap_file.is_file(), f"快照缺失:{snap_file}(python tests/test_snapshots.py 再生)"
    want = json.loads(snap_file.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="cutflow-snap-") as td:
        got = build_and_capture(Path(td), name)
    assert got == want, (
        f"盘面 {name} 状态机结论漂移 —— 若为有意变更,审查 diff 后运行 "
        "`python tests/test_snapshots.py` 再生快照")


def test_snapshot_matrix_complete():
    """矩阵完整性:12 份快照在库、盘面名与 BOARDS 一致、meta 三键齐全。"""
    files = sorted(SNAPSHOTS.glob("*.json"))
    assert [f.stem for f in files] == sorted(BOARD_NAMES), \
        "快照文件与 BOARDS 矩阵不一致(多了/少了盘面)"
    for f in files:
        meta = json.loads(f.read_text(encoding="utf-8"))["meta"]
        assert {"board", "videoType", "state", "desc", "regen"} <= set(meta), f
        assert meta["board"] == f.stem, f


def test_stripped_contract_matches_stages_json(tmp_path):
    """被剥离的 contract 块确实盘面无关且与 stages.json 逐字段一致(剥离无损)。"""
    name = BOARD_NAMES[0]
    with tempfile.TemporaryDirectory(prefix="cutflow-snap-") as td:
        workdir = Path(td)
        fake = workdir / "fakescripts"
        fake.mkdir()
        restore = _isolate(fake)
        try:
            root = workdir / name
            kwargs = BOARDS[0][4]
            _build_board(root, **kwargs)
            raw = {sid: rs_run.explain_payload(
                       root, next(s for s in rs_run.spec(root) if s["id"] == sid))
                   for sid in ORDER}
        finally:
            restore()
    for sid, p in raw.items():
        raw_entry = rs_common.stages_contract().get(sid) or {}
        reduced = {"source": "templates/stages.json(tools/gen_stages.py 机械抽取)",
                   "preconditions": raw_entry.get("preconditions") or [],
                   "gates": raw_entry.get("gates") or [],
                   "timeoutPolicy": raw_entry.get("timeoutPolicy"),
                   "degradePolicy": raw_entry.get("degradePolicy"),
                   "rerunPolicy": raw_entry.get("rerunPolicy")}
        assert p["contract"] == reduced, sid


# ---------------------------------------------------------------- 再生入口

if __name__ == "__main__":
    n = 0
    for board, *_ in BOARDS:
        with tempfile.TemporaryDirectory(prefix="cutflow-snap-") as td:
            doc = build_and_capture(Path(td), board)
        out = SNAPSHOTS / f"{board}.json"
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
        n += 1
        print(f"[regen] {out.name}")
    print(f"共再生 {n} 份快照 → {SNAPSHOTS}")
