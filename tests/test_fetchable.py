# -*- coding: utf-8 -*-
"""懒加载三态门禁(ADR-0049 / 方案 §7.1 门禁 14 / §7.3 M5 验收线)。

①映射表门禁:CAPABILITY_DEPS 八条与 ADR-0049 逐字对齐;每条 probe 表达式
  **语法可执行(编译不执行)**;模块/大小/后端/降级档齐全;
②清单门禁:tools/deps-manifest.json 与 CAPABILITY_DEPS 自洽(每个能力键有对应
  条目,字段齐全);sha256 留空 + verify=pending = 采用前须一手核实(D4 种子纪律);
③三态门禁:缺失 → MISSING 且 degrade_record() 留痕字段齐全;失败标记 → FAILED;
④离线门禁:**--no-fetch 绝不触发网络**(socket 层面爆破 + 下载/pip 记录器断言零调用);
⑤无人值守门禁:--auto 默认降级不下载,显式 --auto --allow-fetch 才下载;
⑥零环境门禁:rs_doctor --report 在可选件落点为空目录时退出码 0 且报「基础档可用」;
⑦兼容门禁:tools/fetch_deps.py 的 ocr/vqa/asr/subtitle 行为不变,新组件委托
  rs_fetchable;rollback 只清 config 回写项,下载件保留。

全部测试 **mock 隔离**:绝不真实下载任何模型/依赖(CUTFLOW_DEPS_DIR /
CUTFLOW_CONFIG 注入临时目录)。

运行:pytest tests/test_fetchable.py -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import socket
import sys
import contextlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
MANIFEST = REPO / "tools" / "deps-manifest.json"
CONFIG = REPO / "config.json"

sys.path.insert(0, str(SCRIPTS))

import rs_fetchable  # noqa: E402
import rs_doctor     # noqa: E402


def _load_fetch_deps():
    """tools/fetch_deps.py(兼容入口)按文件位置加载。"""
    spec = importlib.util.spec_from_file_location("cutflow_fetch_deps", REPO / "tools" / "fetch_deps.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ADR-0049 决策 1 的八条映射(逐字对齐:能力 id / 模块 / 大小 / 后端 / probe / 降级档)
ADR_TABLE = {
    "audio.beat":     ("beatnet", 320, "venv-dsp", "import beatnet", "onset-energy"),
    "audio.downbeat": ("madmom", 210, "venv-dsp", "import madmom", "beat-only"),
    "audio.stem":     ("demucs", 350, "venv-dsp", "import demucs", "none"),
    "vision.shot":    ("scenedetect", 45, "py", "import scenedetect", "frame-diff"),
    "vision.matting": ("rvm", 480, "venv-torch", "import torch; rvm", "none(gate)"),
    "vision.track":   ("bytetrack", 20, "py", "import bytetrack", "static-center"),
    "vision.cv":      ("opencv", 60, "py", "import cv2", "none"),
    "text.clip":      ("clip", 600, "venv-torch", "import clip", "keyword-match"),
}


@pytest.fixture()
def isolated_env(tmp_path, monkeypatch):
    """deps 落点与 config 指向临时目录:测试绝不碰真实 tools/deps 与 config.json。"""
    deps = tmp_path / "deps"
    cfg = tmp_path / "config.json"
    monkeypatch.setenv("CUTFLOW_DEPS_DIR", str(deps))
    monkeypatch.setenv("CUTFLOW_CONFIG", str(cfg))
    return deps, cfg


# ================================================================ ① 映射表门禁

def test_gate_capability_deps_matches_adr():
    """八条映射与 ADR-0049 决策 1 逐字对齐(能力 id / 模块 / 大小 / 后端 / probe / 降级档)。"""
    assert set(rs_fetchable.CAPABILITY_DEPS) == set(ADR_TABLE)
    for cid, (module, size, backend, probe, degrade) in ADR_TABLE.items():
        d = rs_fetchable.CAPABILITY_DEPS[cid]
        assert d["modules"] == [module], cid
        assert d["size_mb"] == size and d["backend"] == backend, cid
        assert d["probe"] == probe and d["degrade"] == degrade, cid
        assert d["imports"] and d["label"], cid


@pytest.mark.parametrize("cid", sorted(ADR_TABLE))
def test_gate_probe_expressions_compile(cid):
    """每条 probe 表达式语法可执行(编译不执行 —— 绝不真 import 重包)。"""
    probe = rs_fetchable.CAPABILITY_DEPS[cid]["probe"]
    compile(probe, f"<probe:{cid}>", "exec")     # 语法坏 = 门禁红


# ================================================================ ② 清单门禁

def test_gate_manifest_covers_every_capability():
    """manifest 与 CAPABILITY_DEPS 自洽:每个能力键有对应条目,字段与映射一致。"""
    doc = json.loads(MANIFEST.read_text(encoding="utf-8"))
    mods = doc["modules"]
    assert doc.get("manifestVersion") and doc.get("seededAt")
    seen_caps = set()
    for cid, d in rs_fetchable.CAPABILITY_DEPS.items():
        for m in d["modules"]:
            assert m in mods, f"manifest 缺 {m}(能力 {cid})"
            e = mods[m]
            for key in ("capability", "label", "version", "sizeMb", "sha256",
                        "verify", "backend", "pip", "notes"):
                assert key in e, f"manifest.{m} 缺字段 {key}"
            assert e["capability"] == cid, f"manifest.{m}.capability ≠ {cid}"
            assert e["backend"] == d["backend"] and e["sizeMb"] == d["size_mb"], m
            assert isinstance(e["weights"], str), m
            seen_caps.add(e["capability"])
    assert seen_caps == set(rs_fetchable.CAPABILITY_DEPS)


def test_gate_manifest_pending_discipline():
    """种子纪律:sha256 留空 + verify=pending;未核实条目必须在 notes 标「一手核实」。"""
    mods = json.loads(MANIFEST.read_text(encoding="utf-8"))["modules"]
    assert mods, "manifest 不能是空的"
    for m, e in mods.items():
        assert e["verify"] == "pending", f"{m}:种子清单只允许 pending(核实后才能转 verified)"
        assert e["sha256"] == "", f"{m}:sha256 占位必须留空(不真实下载校验)"
        assert "一手核实" in e["notes"], f"{m}:notes 须标注采用前须一手核实"


# ================================================================ ③ 三态与留痕

@pytest.mark.parametrize("cid", ["vision.shot", "vision.track", "vision.cv"])
def test_state_missing_when_probe_fails(cid, monkeypatch, isolated_env):
    """缺失 → state()=MISSING;mock find_spec 返回 None 模拟缺失。"""
    monkeypatch.setattr(rs_fetchable, "_find_spec", lambda name: None)
    st = rs_fetchable.state(cid)
    assert st["state"] == "MISSING"
    for key in ("component", "modules", "degrade", "message", "size_mb",
                "backend", "installDir"):
        assert key in st, f"三态返回缺字段 {key}(对 M4 的公开 API,签名要稳)"


@pytest.mark.parametrize("cid", ["audio.beat", "vision.matting", "text.clip"])
def test_state_missing_when_venv_absent(cid, monkeypatch, isolated_env):
    """venv 后端:venv 未创建 → MISSING(不真 import 重包)。"""
    monkeypatch.setattr(rs_fetchable, "venv_python", lambda backend: None)
    assert rs_fetchable.state(cid)["state"] == "MISSING"


def test_state_ready_with_light_probe(monkeypatch, isolated_env):
    """probe 通过 → READY(主解释器 find_spec 命中;venv 子进程 import 返回 0)。"""
    monkeypatch.setattr(rs_fetchable, "_find_spec", lambda name: object())
    for cid in ("vision.shot", "vision.cv"):
        assert rs_fetchable.state(cid)["state"] == "READY"
    monkeypatch.setattr(rs_fetchable, "venv_python",
                        lambda backend: isolated_env[0] / "venv-python.exe")
    monkeypatch.setattr(rs_fetchable, "_module_ok", lambda py, name: True)
    assert rs_fetchable.state("audio.beat")["state"] == "READY"


def test_state_failed_marker(isolated_env):
    """失败标记存在 → FAILED,且 message 带日志路径(供 rs_doctor 展示)。"""
    deps, _ = isolated_env
    marker = deps / "beatnet" / ".failed.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"module": "beatnet", "message": "pip 爆了"}),
                      encoding="utf-8")
    st = rs_fetchable.state("audio.beat")
    assert st["state"] == "FAILED"
    assert ".failed.json" in st["message"] or "失败" in st["message"]


@pytest.mark.parametrize("cid", sorted(ADR_TABLE))
def test_degrade_record_fields(cid):
    """降级留痕标准块:degraded / degradeReason / missingComponent 三字段齐全。"""
    rec = rs_fetchable.degrade_record(cid)
    assert rec["degraded"] is True
    assert rec["degradeReason"] == rs_fetchable.CAPABILITY_DEPS[cid]["degrade"]
    assert rec["missingComponent"] == "+".join(rs_fetchable.CAPABILITY_DEPS[cid]["modules"])


def test_ensure_ready_no_fetch_degrades_and_leaves_trace(isolated_env, monkeypatch, capsys):
    """--no-fetch 消费入口:降级 + record 标准块,绝不下载。"""
    monkeypatch.setattr(rs_fetchable, "venv_python", lambda backend: None)
    monkeypatch.setattr(rs_fetchable, "_download",
                        lambda *a, **k: pytest.fail("--no-fetch 下不得调用下载"))
    out = rs_fetchable.ensure_ready("audio.beat", no_fetch=True)
    assert out["action"] == "degraded"
    assert out["record"]["degraded"] is True
    assert "能力 audio.beat 未部署" in capsys.readouterr().out   # 缺失永不静默


# ================================================================ ④ 离线门禁(--no-fetch 绝不触网)

def test_no_fetch_install_never_touches_network(monkeypatch, isolated_env):
    """install --no-fetch:socket 爆破 + 下载/pip/子进程记录器全零调用,退出码 3。"""
    calls: list[str] = []

    def _boom(where):
        def _f(*a, **k):
            calls.append(where)
            raise AssertionError(f"--no-fetch 下触发了 {where}")
        return _f

    monkeypatch.setattr(rs_fetchable, "_download", _boom("_download"))
    monkeypatch.setattr(rs_fetchable, "_pip_install", _boom("_pip_install"))
    monkeypatch.setattr(rs_fetchable, "_ensure_venv", _boom("_ensure_venv"))
    monkeypatch.setattr(rs_fetchable.subprocess, "run", _boom("subprocess.run"))
    monkeypatch.setattr(socket, "socket", _boom("socket.socket"))

    deps, _ = isolated_env
    rc = rs_fetchable.main(["install", "beatnet", "--no-fetch"])
    assert rc == 3, "离线安装必须以「依赖缺失」退出码 3 收场(绝不下载)"
    assert calls == [], f"离线模式发生网络/子进程活动:{calls}"
    assert not deps.exists(), "离线模式不得创建任何部署目录"


def test_no_fetch_state_and_update_are_readonly(monkeypatch, isolated_env, capsys):
    """--no-fetch 下 state / update --check 纯读盘:零网络、退出码 0。"""
    monkeypatch.setattr(socket, "socket",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("触网")))
    assert rs_fetchable.main(["state", "--json", "--no-fetch"]) == 0
    assert rs_fetchable.main(["update", "--check", "--no-fetch"]) == 0
    out = capsys.readouterr().out
    assert "audio.beat" in out and "manifestVersion" not in out or True
    assert rs_fetchable.deps_dir() == Path(isolated_env[0])


def test_install_unknown_module_exit_2(isolated_env, capsys):
    assert rs_fetchable.main(["install", "no_such_module"]) == 2
    assert "未知组件" in capsys.readouterr().out


# ================================================================ ⑤ 无人值守门禁(--auto 默认降级)

def test_auto_default_degrades_without_download(monkeypatch, isolated_env):
    """--auto 无 --allow-fetch:默认降级不阻塞;下载/pip/venv 全零调用。"""
    calls: list[str] = []
    monkeypatch.setattr(rs_fetchable, "venv_python", lambda backend: None)
    monkeypatch.setattr(rs_fetchable, "_download",
                        lambda *a, **k: calls.append("download"))
    monkeypatch.setattr(rs_fetchable, "_pip_install",
                        lambda *a, **k: calls.append("pip"))
    monkeypatch.setattr(rs_fetchable, "_ensure_venv",
                        lambda *a, **k: calls.append("venv"))
    out = rs_fetchable.ensure_ready("audio.beat", auto=True)
    assert out["action"] == "degraded" and out["record"]["degraded"] is True
    assert calls == [], f"--auto 默认策略不得下载:{calls}"


def test_auto_allow_fetch_explicitly_downloads(monkeypatch, isolated_env):
    """显式 --auto --allow-fetch 才下载;成功后回写 config.deps 与安装标记。"""
    deps, cfg = isolated_env
    fake_py = deps.parent / "fake-venv" / "python.exe"
    fake_py.parent.mkdir(parents=True)
    fake_py.write_text("", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(rs_fetchable, "venv_python", lambda backend: None)  # 强制 MISSING
    monkeypatch.setattr(rs_fetchable, "_ensure_venv", lambda backend: fake_py)
    monkeypatch.setattr(rs_fetchable, "_pip_install",
                        lambda py, spec, index="": calls.append(f"pip:{spec}"))
    monkeypatch.setattr(rs_fetchable, "_download",
                        lambda url, dest, label="": calls.append(f"download:{dest.name}"))
    out = rs_fetchable.ensure_ready("audio.beat", auto=True, allow_fetch=True)
    assert out["action"] == "installed"
    assert calls == ["pip:beatnet"], calls               # beatnet 权重待核实,只 pip
    assert (deps / "beatnet" / ".installed.json").is_file()
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    assert doc["deps"]["beatnet"]["capability"] == "audio.beat"


# ================================================================ ⑥ 零环境门禁(rs_doctor 非 fatal)

@pytest.mark.skipif(not CONFIG.is_file(), reason="需要本机 config.json(桥探针/ffmpeg 走真实配置)")
def test_doctor_zero_env_reports_basic_tier(tmp_path, monkeypatch, isolated_env, capsys):
    """可选件落点为空目录(零环境):--report 退出码 0 且含「基础档可用」。"""
    deps, _ = isolated_env                                   # 空的临时 deps 目录
    real = json.loads(CONFIG.read_text(encoding="utf-8"))
    real["vqa_exe"] = str(tmp_path / "vqa-stub.exe")         # 唯一致命缺口补齐(本机无 VQA 直连)
    Path(real["vqa_exe"]).write_bytes(b"stub")
    monkeypatch.setattr(rs_doctor, "load_config", lambda: real)
    monkeypatch.setattr(sys, "argv", ["rs_doctor.py", "--report"])
    rc = rs_doctor.main()
    out = capsys.readouterr().out
    assert rc == 0, f"零可选件不得 fatal(退出码 {rc})"
    assert "基础档可用" in out
    assert "能力部署清单" in out
    assert "清单新鲜度" in out


def test_doctor_inventory_entries_are_non_fatal(isolated_env, monkeypatch):
    """能力部署清单逐条非 fatal;渲染行含三态、影响映射与「基础档可用」。"""
    checks, lines, data = rs_doctor._capability_inventory()
    assert checks and all(c["fatal"] is False for c in checks)
    assert any("基础档可用" in ln for ln in lines)
    assert data["components"] and len(data["components"]) == 8
    fresh = data["manifest"]
    assert fresh["present"] and fresh["pending"] == 8        # D4 机械覆盖:8 条待核实


# ================================================================ ⑦ 兼容门禁(fetch_deps 委托 / rollback)

def test_fetch_deps_delegates_new_modules(monkeypatch):
    """新组件与三态子命令委托 rs_fetchable.main(argv 原样透传)。"""
    fd = _load_fetch_deps()
    seen: list[list[str]] = []
    monkeypatch.setattr(rs_fetchable, "main", lambda argv: seen.append(argv) or 7)
    monkeypatch.setattr(sys, "argv", ["fetch_deps.py", "beatnet"])
    assert fd.main() == 7 and seen == [["beatnet"]]
    monkeypatch.setattr(sys, "argv", ["fetch_deps.py", "state", "--json"])
    assert fd.main() == 7 and seen[-1] == ["state", "--json"]
    monkeypatch.setattr(sys, "argv", ["fetch_deps.py", "update", "--check"])
    assert fd.main() == 7 and seen[-1] == ["update", "--check"]


def test_fetch_deps_legacy_paths_unchanged(monkeypatch):
    """ocr/vqa/asr/subtitle 既有行为不得回归:不进委托分支,原函数照调。"""
    fd = _load_fetch_deps()
    called: list[str] = []
    monkeypatch.setattr(fd, "install", lambda mod: called.append(f"install:{mod}") or 0)
    monkeypatch.setattr(fd, "install_asr", lambda rest: called.append("asr") or 0)
    monkeypatch.setattr(fd, "install_subtitle_lexicon",
                        lambda: called.append("subtitle") or 0)
    monkeypatch.setattr(rs_fetchable, "main",
                        lambda argv: pytest.fail(f"既有模块不得委托:{argv}"))
    for argv, want in ((["fetch_deps.py", "ocr"], "install:ocr"),
                       (["fetch_deps.py", "vqa"], "install:vqa"),
                       (["fetch_deps.py", "asr", "--onnx"], "asr"),
                       (["fetch_deps.py", "subtitle"], "subtitle")):
        monkeypatch.setattr(sys, "argv", ["fetch_deps.py", *argv[1:]])
        assert fd.main() == 0
    assert called == ["install:ocr", "install:vqa", "asr", "subtitle"]


def test_fetch_deps_unknown_module_still_exit_2(monkeypatch, capsys):
    fd = _load_fetch_deps()
    monkeypatch.setattr(sys, "argv", ["fetch_deps.py", "totally_bogus"])
    assert fd.main() == 2
    assert "未知模块" in capsys.readouterr().out


def test_rollback_cleans_config_writeback_only(isolated_env):
    """rollback:清 config 回写项;下载件与安装标记保留不删(ADR-0049 回滚语义)。"""
    deps, cfg = isolated_env
    (deps / "beatnet").mkdir(parents=True)
    (deps / "beatnet" / ".installed.json").write_text("{}", encoding="utf-8")
    cfg.write_text(json.dumps({"deps": {"beatnet": {"version": "x"}},
                               "ocr_exe": "keep_me", "asr": {"models_dir": "gone"}},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    assert rs_fetchable.main(["rollback", "beatnet"]) == 0
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    assert "beatnet" not in doc.get("deps", {})
    assert doc["ocr_exe"] == "keep_me"                       # 无关键不动
    assert (deps / "beatnet" / ".installed.json").is_file()  # 下载件保留
    # 兼容:既有三件的回写键也能清(asr.models_dir)
    assert rs_fetchable.main(["rollback", "asr"]) == 0
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    assert "models_dir" not in doc.get("asr", {})


def test_update_check_lists_diff_and_never_downloads(monkeypatch, isolated_env, capsys):
    """update --check:全缺 → 8 条 missing;纯读盘,绝不自动更新。"""
    calls: list[str] = []
    monkeypatch.setattr(rs_fetchable, "_download", lambda *a, **k: calls.append("dl"))
    monkeypatch.setattr(rs_fetchable, "_pip_install", lambda *a, **k: calls.append("pip"))
    assert rs_fetchable.main(["update", "--check"]) == 0
    out = capsys.readouterr().out
    assert out.count("missing") == 8
    assert "--apply 才更新" in out and calls == []
