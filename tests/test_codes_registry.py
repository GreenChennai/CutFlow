"""T2.13 门禁:全仓 emit/die 的 code 都必须在 rs_codes 注册表内;未登记即红。

三道断言:
  1. 静态对拍(AST 全仓扫描 skills/cutflow/scripts + tools):
     emit()/die() 的 code 实参(字面量 / 三元分支 / 常量名回解 / f-string 模板)
     一一核对注册表;EditError/IrError 的首参错误码同查;
  2. 运行时校验:rs_common.emit / rs_common.die / fun_asr.emit 对未登记 code
     抛 UnregisteredCodeError(登记后即正常);
  3. 注册表自洽:退出码协议 0/2/3/4;域展开无重复(注册表自身矛盾即红)。

运行:pytest tests/test_codes_registry.py -q
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys
import contextlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
TOOLS = REPO / "tools"
sys.path.insert(0, str(SCRIPTS))

import rs_codes  # noqa: E402
from rs_codes import UnregisteredCodeError, require_registered  # noqa: E402

# 参加静态对拍的目录:脚本层 + tools(排除虚拟环境/第三方/缓存)
# 形态允许连字符:冲突码 CF-001/CF-002 是 schema 既有口径(CONTEXT.md)
CODE_LIKE = re.compile(r"[A-Z][A-Z0-9_-]*\Z")   # 结果 code 形态(禁止尾下划线,挡 f-string 碎片)


def _scan_files() -> list[Path]:
    out: list[Path] = []
    for base in (SCRIPTS, TOOLS):
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in (
                "__pycache__", ".venv-asr", "vendor", "dist", "asr_vendor")]
            for f in files:
                if f.endswith(".py"):
                    out.append(Path(root) / f)
    return out


def _module_const(mod_node: ast.Module, name: str) -> list[str]:
    """回解模块级常量赋值 `NAME = "X"` / `NAME = ("X", "Y")` 的字符串值。"""
    vals: list[str] = []
    for node in mod_node.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            continue
        for el in ast.walk(node.value):
            if isinstance(el, ast.Constant) and isinstance(el.value, str):
                vals.append(el.value)
    return vals


def _code_values(arg: ast.expr, mod_node: ast.Module) -> tuple[set[str], list[str]]:
    """code 位置实参 → (确定值集合, f-string 模板集合)。

    · Constant "X"            → 确定值 X
    · 三元/联合("A" if c else "B")→ 展开全部常量分支
    · Name(模块级常量)      → 回解赋值
    · JoinedStr f"MATTE_{x}"  → 模板 "MATTE_{}"(动态拼法:注册表须有此前缀族)
    """
    exact: set[str] = set()
    tmpl: list[str] = []
    if isinstance(arg, ast.Constant):
        if isinstance(arg.value, str) and CODE_LIKE.fullmatch(arg.value or ""):
            exact.add(arg.value)
        return exact, tmpl
    if isinstance(arg, ast.Name):
        for v in _module_const(mod_node, arg.id):
            if CODE_LIKE.fullmatch(v or ""):
                exact.add(v)
        return exact, tmpl
    if isinstance(arg, ast.JoinedStr):
        parts = []
        for v in arg.values:
            if isinstance(v, ast.Constant):
                parts.append(str(v.value))
            else:
                parts.append("{}")     # FormattedValue → 通配
        tmpl.append("".join(parts))
        return exact, tmpl
    # 三元 / 其它组合表达式:展开其中全部字符串常量
    for el in ast.walk(arg):
        if isinstance(el, ast.Constant) and isinstance(el.value, str) \
                and CODE_LIKE.fullmatch(el.value or ""):
            exact.add(el.value)
    return exact, tmpl


def _collect_emit_codes(tree: ast.Module) -> list[tuple[int, set[str], list[str]]]:
    """收集本模块 emit/die 调用的 code 位置值 + EditError/IrError 首参。

    另收 `code`/`code_name` 局部赋值的字面量分支(rs_asset/rs_reframe/rs_matting
    的 `code = "X_OK" if ok else "X_FAIL"` 拼法)—— 局部变量静态回解不到的盲区,
    以赋值右值穷举兜底(emit 运行时校验是第二道闸)。
    """
    found: list[tuple[int, set[str], list[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", getattr(node.func, "attr", ""))
        if name in ("emit", "die") and len(node.args) >= 2:
            # emit(ok, code, …) / die(exit_code, code, …) —— code 都在位置 1
            found.append((node.lineno, *_code_values(node.args[1], tree)))
        elif name in ("EditError", "IrError") and node.args:
            found.append((node.lineno, *_code_values(node.args[0], tree)))
        elif name in ("_fail", "fail") and node.args:
            # rs_edit._fail(code, …) 一族的字面量调用点(与 EditError 同义)
            found.append((node.lineno, *_code_values(node.args[0], tree)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in ("code", "code_name", "code_str") \
                and isinstance(node.value, (ast.IfExp, ast.Constant, ast.Dict, ast.Name)):
            # 右值限字符串形态(IfExp 三元/字面量/映射/常量名):
            # 排除 `code = f"import …"` 这类子进程脚本文本(变量恰名 code,非结果码)
            exact, tmpl = _code_values(node.value, tree)
            if exact or tmpl:
                found.append((node.lineno, exact, tmpl))
    return found


# ---------------------------------------------------------------- 1. 静态全仓对拍

def test_all_repo_emit_codes_registered():
    """全仓每个 emit/die/EditError/IrError 的 code 都必须在注册表内(含动态拼法前缀)。"""
    problems: list[str] = []
    n_calls = 0
    for path in _scan_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            problems.append(f"{path}: 解析失败 {exc}")
            continue
        for lineno, exact, tmpl in _collect_emit_codes(tree):
            n_calls += 1
            for code in exact:
                if not rs_codes.is_registered(code):
                    problems.append(f"{path}:{lineno} 未登记 code:{code}")
            for t in tmpl:
                # 动态模板:注册表里必须存在匹配该形态(通配 {} 化)的登记码
                pat = re.compile(re.escape(t).replace(r"\{\}", "[A-Z0-9_]+") + r"\Z")
                if not any(pat.fullmatch(c) for c in rs_codes.REGISTERED_CODES):
                    problems.append(f"{path}:{lineno} 未登记动态 code 模板:{t}")
    assert n_calls >= 200, f"emit 调用扫描量异常(仅 {n_calls} 处),扫描器失灵?"
    assert not problems, "以下 emit/die 的 code 未在 rs_codes 注册:\n" + "\n".join(problems)


def test_registry_self_consistent():
    """注册表自身自洽:域展开无重复、全大写形态、退出码协议 0/2/3/4。"""
    seen: dict[str, str] = {}
    for domain, codes in rs_codes.DOMAINS.items():
        for c in codes:
            assert c not in seen, f"code {c} 同时登记于 {seen[c]} 与 {domain}"
            seen[c] = domain
            assert CODE_LIKE.fullmatch(c), f"code 形态非法:{c}"
    assert (rs_codes.EXIT_OK, rs_codes.EXIT_INPUT, rs_codes.EXIT_DEP, rs_codes.EXIT_EXEC) \
        == (0, 2, 3, 4)
    assert len(rs_codes.REGISTERED_CODES) >= 250, "注册表规模骤降,疑似误删"


# ---------------------------------------------------------------- 2. 运行时校验

def test_emit_rejects_unregistered_code():
    """emit 用未登记 code → UnregisteredCodeError(红),不许静默外放。"""
    import rs_common
    buf = io.StringIO()
    with pytest.raises(UnregisteredCodeError):
        with contextlib.redirect_stdout(buf):
            rs_common.emit(True, "TOTALLY_NOT_A_CODE", "x")
    assert buf.getvalue() == "", "未登记 code 不得产生任何输出"


def test_emit_accepts_registered_code():
    import rs_common
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = rs_common.emit(True, "RUN_OK", "消息", {"k": 1}, exit_code=0)
    assert rc == 0
    import json
    doc = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert doc == {"ok": True, "code": "RUN_OK", "message": "消息", "data": {"k": 1}}


def test_require_registered_message_points_to_registry():
    with pytest.raises(UnregisteredCodeError) as ei:
        require_registered("NO_SUCH_CODE_XYZ")
    assert "rs_codes" in str(ei.value)


def test_domain_lookup():
    assert rs_codes.domain_of("STATUS_OK") == "阶段编排 rs_run"
    assert rs_codes.domain_of("IR_INVALID") == "IR 与渲染"
    with pytest.raises(UnregisteredCodeError):
        rs_codes.domain_of("NOT_REGISTERED_QQ")
