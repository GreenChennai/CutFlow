"""T2.15 门禁:全部 rs_* 脚本的 --json 输出协议同构,且退出码与 ok 一致。

协议(rs_common 文档口径):命令结果输出 = `{"ok","code","message","data"}` 末行 JSON,
`退出码 == 0 ⟺ ok == true`,`code` 必须在 rs_codes 注册表内。

三级最小调用(避免真实渲染;产物/网络副作用为零):
  L1 `--help` 冒烟:每个脚本 exit 0(抓 import 崩溃 / argparse 装配损坏);
  L2 裸调用:要么末行是同构 JSON,要么被 argparse 必选参拒绝(rc==2,命令未执行,
     不属于命令结果,不算违约);
  L3 子命令最小调用:经 rs_caps 的 argparse 重放枚举子命令(旗标式脚本无子命令则
     跳过),逐个 `<script> <sub>` 裸跑,分类同 L2。

白名单:确实无法安全最小调用的子命令(写仓库文件的生成器),逐条带理由。

运行:pytest tests/test_json_contract.py -q
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_codes  # noqa: E402

SCRIPTS_LIST = sorted(SCRIPTS.glob("rs_*.py"))
# 无 CLI 的公共库(rs_caps 目录里登记为 library/excluded):没有 argparse 主命令
CLI_LESS = {"rs_codes.py", "rs_common.py", "rs_paths.py"}
# 子命令白名单:{(脚本, 子命令): 理由} —— 无法安全最小调用者
SUB_WHITELIST: dict[tuple[str, str], str] = {
    ("rs_caps.py", "generate"): "会再生仓库 capabilities.json(写文件),由维护者显式调用",
}

PROC_TIMEOUT = 90


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=PROC_TIMEOUT)


def _classify(p: subprocess.CompletedProcess) -> tuple[str, dict]:
    """→ (类别, json 文档);类别 ∈ json-ok / json-MISMATCH / argparse-reject / other。"""
    lines = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    if last.startswith("{"):
        try:
            doc = json.loads(last)
        except json.JSONDecodeError:
            return "other", {}
        if not (set(doc) >= {"ok", "code", "message"}):
            return "json-MISMATCH", doc
        if (p.returncode == 0) != bool(doc.get("ok")):
            return "json-MISMATCH", doc
        if doc.get("code") not in rs_codes.REGISTERED_CODES:
            return "json-MISMATCH", {**doc, "message": f"code 未注册:{doc.get('code')}"}
        return "json-ok", doc
    if p.returncode == 2 and re.search(r"error:|usage:", (p.stderr or ""), re.I):
        return "argparse-reject", {}
    return "other", {}


@pytest.fixture(scope="module")
def empty_cwd(tmp_path_factory):
    d = tmp_path_factory.mktemp("emptyproj")
    return d


# ---------------------------------------------------------------- L1 --help 冒烟

@pytest.mark.parametrize("script", SCRIPTS_LIST, ids=lambda p: p.name)
def test_help_smoke(script, empty_cwd):
    if script.name in CLI_LESS:
        pytest.skip("无 CLI 公共库(rs_caps 目录登记为 library)")
    p = _run([str(script), "--help"], empty_cwd)
    assert p.returncode == 0, f"{script.name} --help 退出 {p.returncode}:{(p.stderr or '')[-300:]}"
    assert "Traceback" not in (p.stderr or ""), f"{script.name} --help 崩溃"


# ---------------------------------------------------------------- L2 裸调用

@pytest.mark.parametrize("script", SCRIPTS_LIST, ids=lambda p: p.name)
def test_bare_invocation_contract(script, empty_cwd):
    if script.name in CLI_LESS:
        pytest.skip("无 CLI 公共库")
    p = _run([str(script)], empty_cwd)
    kind, doc = _classify(p)
    assert kind in ("json-ok", "argparse-reject"), \
        f"{script.name} 裸调用破坏 --json 协议(kind={kind}, rc={p.returncode}, " \
        f"out={doc or (p.stdout or '')[-120:]!r}, err={(p.stderr or '')[-200:]!r})"


# ---------------------------------------------------------------- L3 子命令最小调用

def _subcommands(script: Path) -> list[str]:
    """argparse 重放枚举子命令(rs_caps 实现);重放失效的脚本用 add_parser 正则兜底。

    两类"子命令"都收:① subparsers 的 choices;② 位置参数的 choices(旗标式脚本的
    `command` 位)。全大写/非字符串选项(如 RATIOS 元组)排除。
    """
    import rs_caps
    try:
        parser = rs_caps.build_parser(script)
        subs: list[str] = []
        for act in parser._actions:
            if act.__class__.__name__ == "_SubParsersAction":
                subs += list(act.choices)
            elif not getattr(act, "option_strings", None) \
                    and getattr(act, "choices", None):
                subs += [c for c in act.choices if isinstance(c, str) and c.islower()]
        return sorted(set(subs))
    except Exception:
        text = script.read_text(encoding="utf-8")
        return sorted(set(re.findall(r'add_parser\(\s*["\']([a-z0-9\-]+)["\']', text)))


SUBCASES = [(s.name, sub) for s in SCRIPTS_LIST for sub in _subcommands(s)]


@pytest.mark.parametrize(["name", "sub"], SUBCASES)
def test_subcommand_minimal_contract(name, sub, empty_cwd):
    if (name, sub) in SUB_WHITELIST:
        pytest.skip(f"白名单:{SUB_WHITELIST[(name, sub)]}")
    script = SCRIPTS / name
    p = _run([str(script), sub], empty_cwd)
    kind, doc = _classify(p)
    assert kind in ("json-ok", "argparse-reject"), \
        f"{name} {sub} 破坏 --json 协议(kind={kind}, rc={p.returncode}, " \
        f"out={doc or (p.stdout or '')[-120:]!r}, err={(p.stderr or '')[-200:]!r})"


def test_subcommand_matrix_nonempty():
    """子命令矩阵不许为空(枚举器失灵会静默放空);关键脚本必须在矩阵内。"""
    assert len(SUBCASES) >= 40, f"子命令枚举仅 {len(SUBCASES)} 条,扫描器失灵?"
    scripts_with_subs = {name for name, _ in SUBCASES}
    assert {"rs_asset.py", "rs_ingest.py", "rs_editor.py"} <= scripts_with_subs
