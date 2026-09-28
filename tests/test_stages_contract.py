"""T2.10 + T2.12 门禁:stages.json 阶段契约 与 超时策略统一。

  1. 生成器零 diff(硬门禁):tools/gen_stages.py 再生结果与盘上文件逐字节一致;
  2. 契约与实现逐字段一致:stages.json 的 id/name/scripts/inputs/outputs/manual/
     marker/paramKeys/cmd/post == rs_run.spec();timeoutPolicy.base ==
     rs_run.DEFAULT_STAGE_TIMEOUT_SEC / STAGE_TIMEOUTS;
  3. `--explain` 一致性:rs_run.explain_payload 的 data.contract 与 stages.json
     对应条目逐字段一致(T2.11 判据的实现侧);
  4. 超时统一(T2.12):全仓不再新增裸 `timeout=数字` 常量 —— 主链(rs_run /
     rs_render / rs_align / rs_greenscreen)必须为零,其余散点按白名单计数,
     超出白名单即红(收敛一处就应把白名单减一)。

运行:pytest tests/test_stages_contract.py -q
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_common  # noqa: E402
import rs_run  # noqa: E402
import rs_paths  # noqa: E402

# ---------------------------------------------------------------- 生成器(以模块方式加载)

def _gen_stages():
    spec = importlib.util.spec_from_file_location(
        "_t2b_gen_stages", REPO / "tools" / "gen_stages.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_t2b_gen_stages", mod)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 1. 生成器零 diff

def test_generator_zero_diff():
    """硬门禁:重跑生成器必须零 diff(盘上 stages.json == 再生结果,逐字节)。"""
    gen = _gen_stages()
    on_disk = gen.STAGES_OUT.read_text(encoding="utf-8")
    assert gen.serialize(gen.build_contract()) == on_disk, (
        "stages.json 与生成器输出不一致 —— 改了 rs_run 阶段表/L0 门禁后必须重跑 "
        "`python tools/gen_stages.py`")


def test_generated_check_mode_exits_zero():
    """`gen_stages.py --check`(CI 口径)对当前盘面必须退出 0。"""
    import subprocess
    p = subprocess.run([sys.executable, str(REPO / "tools" / "gen_stages.py"), "--check"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0, p.stdout + p.stderr


def test_contract_shape():
    """契约形状:T2.10 规定的字段一个不少;阶段数 = 12(S0–S11)。"""
    gen = _gen_stages()
    doc = gen.build_contract()
    need = {"id", "name", "scripts", "inputs", "outputs", "preconditions", "gates",
            "timeoutPolicy", "degradePolicy", "rerunPolicy"}
    assert [s["id"] for s in doc["stages"]] == [f"S{i}" for i in range(12)]
    for s in doc["stages"]:
        missing = need - set(s)
        assert not missing, f"{s['id']} 缺契约字段:{missing}"
        tp = s["timeoutPolicy"]
        assert set(tp) >= {"base", "factor", "env"}, f"{s['id']} timeoutPolicy 形状残缺"
        assert isinstance(tp["base"], int) and tp["base"] > 0
        for poly_key in ("degradePolicy", "rerunPolicy"):
            assert s[poly_key] and s[poly_key].get("source"), \
                f"{s['id']} {poly_key} 必须带来源注释(T2.10 计划原文)"
        for g in s["gates"]:
            assert g["check"].startswith("check_") and g["source"]


# ---------------------------------------------------------------- 2. 契约 == 实现(逐字段)

def test_contract_matches_rs_run_spec():
    """stages.json 的机械字段与 rs_run.spec() 逐字段一致(不许有第二份漂移表)。"""
    gen = _gen_stages()
    doc = gen.build_contract()
    by_id = {s["id"]: s for s in doc["stages"]}
    for st in rs_run.spec():
        c = by_id[st["id"]]
        assert c["name"] == st["name"]
        assert c["scripts"] == list(st.get("scripts") or [])
        assert c["inputs"] == list(st["inputs"])
        assert c["outputs"] == list(st["outputs"])
        assert c["manual"] == bool(st.get("manual"))
        assert c["marker"] == st.get("marker")
        assert c["paramKeys"] == list(st.get("paramKeys") or [])
        assert c["cmd"] == (list(st["cmd"]) if st.get("cmd") else None)
        assert c["post"] == (list(st["post"]) if st.get("post") else None)


def test_contract_gates_cover_all_l0_checks():
    """rs_verify.L0_CHECKS 的每个门禁都恰好落进一个阶段的 gates(零静默)。"""
    import rs_verify
    gen = _gen_stages()
    doc = gen.build_contract()
    placed = [g["check"].removeprefix("check_") for s in doc["stages"] for g in s["gates"]]
    assert sorted(placed) == sorted(fn.__name__.removeprefix("check_") for fn in rs_verify.L0_CHECKS)


def test_timeout_policy_matches_rs_run_constants():
    """timeoutPolicy.base 与 rs_run 超时常量零漂移;env 声明齐全。"""
    gen = _gen_stages()
    doc = gen.build_contract()
    for s in doc["stages"]:
        expect = rs_run.STAGE_TIMEOUTS.get(s["id"], rs_run.DEFAULT_STAGE_TIMEOUT_SEC)
        assert s["timeoutPolicy"]["base"] == expect, \
            f"{s['id']} timeoutPolicy.base 与 rs_run 常量不一致"
        assert s["timeoutPolicy"]["env"] == "CUTFLOW_STAGE_TIMEOUT_SEC"
    # 辅助策略键:实现经 rs_common.policy_timeout 消费,表必须给出正 base
    for k, v in doc["policies"].items():
        assert isinstance(v.get("base"), int) and v["base"] > 0, f"policies.{k} base 非法"
        assert v.get("source"), f"policies.{k} 必须带来源(T2.10 计划原文)"


def test_policy_timeout_behavior_unchanged():
    """policy_timeout 行为零漂移:阶段值 / 比例键 / env 覆盖 / 未登记回落。"""
    monkey = pytest.MonkeyPatch()
    try:
        assert rs_common.policy_timeout("S1") == 4 * 3600
        assert rs_common.policy_timeout("S7") == 3600
        assert rs_common.policy_timeout("SEG", 100) == 1800          # max(1800, 400)
        assert rs_common.policy_timeout("SEG", 1000) == 4000         # 比例放宽
        assert rs_common.policy_timeout("ENCODE", 100) == 7200
        monkey.setenv("CUTFLOW_STAGE_TIMEOUT_SEC", "77")
        assert rs_common.policy_timeout("S7") == 77                  # env 完全覆盖
        monkey.delenv("CUTFLOW_STAGE_TIMEOUT_SEC")
        monkey.setenv("CUTFLOW_SEG_TIMEOUT", "55")
        assert rs_common.policy_timeout("SEG", 10000) == 55          # 辅助键同规则
        monkey.delenv("CUTFLOW_SEG_TIMEOUT")
        assert rs_common.policy_timeout("NOT_REGISTERED_KEY") == 3600  # 回落缺省
    finally:
        monkey.undo()


# ---------------------------------------------------------------- 3. --explain 契约一致

def _mk_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    for d in ("00_制作简报", "01_原始素材", "04_粗剪决策", "05_时间线工程", "06_成片输出",
              "_内部状态"):
        (root / d).mkdir(parents=True)
    (root / "01_原始素材" / "a.mp4").write_bytes(b"fake")
    return root


def test_explain_contract_matches_stages_json(tmp_path):
    """`rs_run --explain Sx` 的 data.contract 与 stages.json 对应条目逐字段一致。"""
    gen = _gen_stages()
    doc = gen.build_contract()
    by_id = {s["id"]: s for s in doc["stages"]}
    root = _mk_project(tmp_path)
    for sid in ("S3", "S7", "S11"):
        st = next(s for s in rs_run.spec(root) if s["id"] == sid)
        p = rs_run.explain_payload(root, st)
        c = by_id[sid]
        assert p["contract"]["preconditions"] == c["preconditions"]
        assert p["contract"]["gates"] == c["gates"]
        assert p["contract"]["timeoutPolicy"] == c["timeoutPolicy"]
        assert p["contract"]["degradePolicy"] == c["degradePolicy"]
        assert p["contract"]["rerunPolicy"] == c["rerunPolicy"]
        assert p["stage"] == sid and p["status"] in ("missing", "stale", "done", "corrupt")
        assert p["verdict"] in ("keep", "rerun", "stop", "degrade-continue")


# ---------------------------------------------------------------- 4. 超时统一扫描(T2.12)

# 主链文件:不允许任何裸 timeout=数字(全部走 rs_common.policy_timeout)
MAIN_CHAIN = ("rs_run.py", "rs_render.py", "rs_align.py", "rs_greenscreen.py", "rs_common.py")

# 散点白名单:{文件名: (允许处数, 理由)}。收敛一处就把计数减一;新增裸超时即红。
# 理由口径:这些是「非流水线直调脚本 / 桥层 / 探针」的局部 ffmpeg/HTTP 上限,
# 无 stages.json 阶段身份,统一收益低、改动面大 —— 计划允许留白名单(报告说明)。
BARE_TIMEOUT_WHITELIST: dict[str, tuple[int, str]] = {
    "rs_sync.py": (6, "S9 内部 ffmpeg 提取/静音检测/探测 5 步 + 语音活动独立对账探测"
                     "(T5.6c,与 silencedetect 同族的局部上限)"),
    "rs_sense.py": (4, "第三册感知件:OCR/VQA 单图推理秒级上限"),
    "fun_asr.py": (3, "tools 自带 ASR 本体(独立 venv;健康探针 2s/转写 1800s)"),
    "rs_tts.py": (3, "TTS 探针 4s/合成 300s/试听 600s"),
    "rs_pixabay.py": (3, "playwright 页加载 ms 级 + 文件下载"),
    "rs_doctor.py": (3, "环境体检:HTTP 探针 3s/子命令 60s/30s"),
    "rs_shot.py": (2, "帧差检测 ffmpeg 步(独立直调)"),
    "rs_screen.py": (2, "录屏/帧差 ffmpeg 步(独立直调)"),
    "rs_frames.py": (2, "单帧抽取(秒级)"),
    "rs_fetchable.py": (2, "能力清单下载"),
    "rs_diagnose.py": (2, "旁路诊断件:WAV 转码短任务"),
    "rs_brand.py": (2, "ffmpeg 探测 30s + 渲染 3600s(独立直调)"),
    "t2_glsl.py": (7, "GLSL 预渲管线内部等待(进程 wait/子步)"),
    "rs_bench.py": (1, "抽帧网格(短任务)"),
    "rs_beat.py": (1, "节拍检测 ffmpeg 步"),
    "rs_broll.py": (1, "B-roll ffmpeg 步"),
    "rs_gate.py": (1, "gate 渲染(桥层直调)"),
    "rs_matting.py": (1, "抠像长任务(独立 venv 引擎)"),
    "rs_verify.py": (1, "成片流时长 ffprobe 秒级探测"),
    "fetch_deps.py": (1, "依赖下载"),
}
BARE_TIMEOUT_RE = re.compile(r"\btimeout\s*=\s*\d+")


def test_no_new_bare_timeouts():
    """全仓裸 timeout=数字:主链必须为 0;散点不得超出白名单计数(T2.12 判据)。"""
    problems: list[str] = []
    counts: dict[str, int] = {}
    for base in (SCRIPTS, REPO / "tools"):
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (
                "__pycache__", ".venv-asr", "vendor", "dist", "asr_vendor")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                path = Path(dirpath) / f
                text = path.read_text(encoding="utf-8", errors="replace")
                n = len(BARE_TIMEOUT_RE.findall(text))
                if not n:
                    continue
                if f in MAIN_CHAIN:
                    problems.append(f"{path.name}: 主链文件出现 {n} 处裸 timeout(必须走 policy_timeout)")
                else:
                    counts[f] = counts.get(f, 0) + n
    for f, n in sorted(counts.items()):
        allow, _why = BARE_TIMEOUT_WHITELIST.get(f, (0, ""))
        if n > allow:
            problems.append(f"{f}: 裸 timeout {n} 处 > 白名单 {allow}"
                            "(新收敛请改走 rs_common.policy_timeout 并把白名单减一)")
    # 白名单不许虚挂:某文件已清零却仍占名额 → 提示收窄(白名单只减不增)
    for f in BARE_TIMEOUT_WHITELIST:
        if counts.get(f, 0) == 0:
            problems.append(f"{f}: 裸 timeout 已清零,请把白名单条目删除(只减不增)")
    assert not problems, "T2.12 超时统一破口:\n" + "\n".join(problems)
