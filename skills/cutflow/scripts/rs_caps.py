"""能力目录生成器(T2-1,副文档 05 §3 T2:Token 与上下文治理;第一册 T1.2 增检索)。

用法:
  rs_caps.py generate [--out <path>]    重新生成 capabilities.json(缺省 skills/cutflow/capabilities.json)
  rs_caps.py check    [--out <path>]    再生成并与盘上逐字节对比,漂移即退出 2(门禁/CI 用)
  rs_caps.py search   <关键词> [更多词…]  按命令名/用途/关键词/阶段检索目录(空格分词 = AND)

能力目录 = 「Agent 先查目录、不读源码」的单一真相源:每个工具一行用途 + 参数摘要,
细节留给各脚本 --help。内容只从三个机械源生成,**禁止手改目录**(防漂移测试会红,
目录与实现漂移时只能改源码/注册表后重新生成,不能手抄):

  1. argparse 装配(AST 重放,与 tests/check_manual_cmds.py 同一实现)——
     子命令 / 旗标 + 缺省 / 最小可用 argv(probe,T3 对拍用);
  2. rs_run.spec() 阶段注册表 —— stage 归属与声明产物 outputs;
  3. SKILL.md §2 管线表 —— 每阶段的门禁文案(gate)。

「给定输入必得同一输出」:同一份脚本源码,生成的目录字节级一致(无时间戳、排序稳定)。
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import REPO_ROOT, emit, write_text_atomic  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
CAPABILITIES = REPO_ROOT / "skills" / "cutflow" / "capabilities.json"
SKILL_MD = REPO_ROOT / "skills" / "cutflow" / "SKILL.md"

# 目录不收编的对象:无 CLI 的公共库;生成器自身(它不是剪辑能力,是目录的保养工具)
EXCLUDED = {"rs_common.py"}

# 库工具登记(ADR-0046):无 argparse 的公共库以 library=true 进目录,
# 不占命令配额(commands/args 为空)。登记只在此表 —— 禁手改 capabilities.json。
LIBRARIES = {
    "rs_paths.py": "阶段路径唯一真相源(ADR-0046):STAGE_DIRS/p/resolve/check/ensure;"
                   "全仓禁止目录字面量,取路径只有查表一族入口",
    # T2.13:退出码与结果 code 唯一注册表;rs_common.emit/die 强制校验 code 已登记
    "rs_codes.py": "结果 code 唯一注册表(T2.13):ExitCode 协议 + DOMAINS 域分组清单;"
                   "新 code 先登记再用,未登记 emit 即红",
}

# 解析器装配重放只认这几种方法调用(手册脚本的实际写法超不出这个集合;
# 与 tests/check_manual_cmds.py 共用 —— 那边 import 本模块的 build_parser)
_REPLAY_ATTRS = {"add_argument", "add_parser", "add_subparsers"}


# ---------------------------------------------------------------- argparse 装配重放(自 check_manual_cmds 收编)

def _load_module(script: Path):
    name = f"_manual_gate_{script.stem}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)          # rs_*.py 顶层只有 import/常量,安全
    return mod


def _eval_node(node: ast.expr, g: dict):
    """字面量直接 literal_eval;表达式(list(RATIOS) 等)在模块命名空间里求值。"""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        pass
    return eval(compile(ast.Expression(node), "<manual-gate>", "eval"), g)  # noqa: S307


def _recv_attr(call: ast.Call) -> tuple[str, str] | None:
    if not isinstance(call.func, ast.Attribute):
        return None
    recv = ast.unparse(call.func.value)
    return recv, call.func.attr


def build_parser(script: Path) -> argparse.ArgumentParser:
    """AST 重放脚本的 argparse 装配(与源码同一套 add_argument,不跑 main)。"""
    tree = ast.parse(script.read_text(encoding="utf-8"))
    g = _load_module(script).__dict__
    shadows: dict[str, object] = {}

    def _apply(call: ast.Call) -> object | None:
        ra = _recv_attr(call)
        if ra is None:
            return None
        recv, attr = ra
        if recv not in shadows or attr not in _REPLAY_ATTRS:
            return None
        args = [_eval_node(a, g) for a in call.args]
        kwargs = {kw.arg: _eval_node(kw.value, g) for kw in call.keywords}
        target = shadows[recv]
        result = getattr(target, attr)(*args, **kwargs)
        return result if attr != "add_argument" else target

    handled: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            call = node.value
            ra = _recv_attr(call)
            if ra and ra == ("argparse", "ArgumentParser"):
                # 解析器构造:kwargs(description 等)与装配无关,重放时忽略
                handled.add(id(call))
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        shadows[t.id] = argparse.ArgumentParser()
                continue
            res = _apply(call)
            if res is not None:
                handled.add(id(call))
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        shadows[t.id] = res
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and id(node) not in handled:
            _apply(node)

    parsers = [v for v in shadows.values() if isinstance(v, argparse.ArgumentParser)]
    if not parsers:
        raise RuntimeError(f"{script.name}:AST 重放没找到 ArgumentParser 装配")
    # 主解析器 = 未被 add_parser 产出的那个(多解析器脚本罕见;有则取第一个)
    return parsers[0]


# ---------------------------------------------------------------- 解析器 → 目录条目

def _split_subcommands(parser: argparse.ArgumentParser) -> tuple[list[str], str | None]:
    """(子命令列表, 子命令所在 action 的 dest)。

    两仓现状全部是「choices 位置参数」风格(rs_align/rs_ir/rs_ingest/…);
    add_subparsers 风格同样支持,以备后续脚本改用。
    """
    for act in parser._actions:  # noqa: SLF001 — argparse 内省无公开 API
        if isinstance(act, argparse._SubParsersAction):  # noqa: SLF001
            return list(act.choices.keys()), act.dest
        if act.option_strings:
            continue
        if act.choices and isinstance(act.choices, dict):
            return list(act.choices.keys()), act.dest
        if act.choices:
            return [str(c) for c in act.choices], act.dest
    return [], None


def _is_subcommand_action(act: argparse.Action, sub_dest: str | None) -> bool:
    return sub_dest is not None and act.dest == sub_dest and not act.option_strings


def _sample_value(act: argparse.Action) -> str:
    """probe 占位值:必须能被 argparse 的 type/choices 接受。"""
    if act.choices:
        return str(act.choices[0])
    if act.type is int:
        return "1"
    if act.type is float:
        return "1.5"
    return "P"


def _args_summary(parser: argparse.ArgumentParser, sub_dest: str | None) -> list[str]:
    """参数摘要(旗标 + 缺省;细节留给 --help,目录保持短)。"""
    out: list[str] = []
    for act in parser._actions:  # noqa: SLF001
        if act.dest in ("help", "version") or _is_subcommand_action(act, sub_dest):
            continue
        if act.option_strings:
            flag = act.option_strings[0]
            if getattr(act, "required", False):
                out.append(f"{flag}(必需)")
            elif act.nargs == 0:
                out.append(flag)
            elif act.choices:
                out.append(f"{flag}={'|'.join(str(c) for c in act.choices)}")
            elif act.default is None or act.default is False or act.default == "":
                out.append(flag)
            else:
                dv = str(act.default)
                # 仓库内绝对路径默认值按仓库相对形式入目录——否则目录随
                # checkout 路径漂移,防漂移门禁在异机/CI 必红
                if dv.startswith(str(REPO_ROOT)):
                    dv = dv[len(str(REPO_ROOT)):].lstrip("/\\")
                out.append(f"{flag}={dv}")
        elif act.nargs is None and act.dest != sub_dest:
            out.append(f"<{act.dest}>(必需)")
    return out


def _probe_argv(parser: argparse.ArgumentParser, sub: str | None,
                sub_dest: str | None) -> list[str]:
    """最小可用 argv(T3 对拍用):argparse 必须接受它,否则目录声明即失真。"""
    toks = [sub] if sub else []
    for act in parser._actions:  # noqa: SLF001
        if act.dest in ("help", "version"):
            continue
        if _is_subcommand_action(act, sub_dest):
            continue
        if not act.option_strings and act.nargs is None:
            toks.append(_sample_value(act))
    for act in parser._actions:  # noqa: SLF001
        if act.option_strings and getattr(act, "required", False):
            toks += [act.option_strings[0], _sample_value(act)]
    return toks


def _doc_summary(script: Path) -> str:
    """一行用途:模块 docstring 首行(各脚本的 --help 摘要事实上都在这里)。"""
    m = re.search(r'"""(.*)', script.read_text(encoding="utf-8"), re.S)
    if not m:
        return ""
    line = m.group(1).strip().splitlines()
    return line[0].strip() if line else ""


# 检索关键词上限(防目录膨胀;超出部分细节归 --help / search 全文匹配)
KEYWORDS_CAP = 16


def _keywords_from_text(joined: str) -> list[str]:
    """文本 → 检索关键词:CJK 连续段在前(用户检索主要打中文),组内排序保证确定性。"""
    cjk = sorted(set(re.findall(r"[\u4e00-\u9fff]{2,}", joined)))
    ascii_words = sorted(set(re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", joined)))
    out: list[str] = []
    for tok in cjk + ascii_words:
        if tok not in out:
            out.append(tok)
    return out[:KEYWORDS_CAP]


def _keywords_from_help(parser: argparse.ArgumentParser) -> list[str]:
    """从 argparse 的 help 文本抽检索关键词(第一册 T1.2)。

    口径:CJK 连续段(取词组形态,如「字幕样式」「热词」)+ ASCII 标识符(如 douyin、
    BAD_PLATFORM)+ choices 字面值(子命令/枚举名)。截断到 KEYWORDS_CAP,防目录长成
    要读的文件;无 help 的位置参数不算(信息在 commands 里)。
    """
    texts: list[str] = []
    for act in parser._actions:  # noqa: SLF001 — argparse 内省无公开 API
        if act.dest in ("help", "version"):
            continue
        if act.help:
            texts.append(act.help)
        for c in (act.choices or []):
            if isinstance(c, str):
                texts.append(c)
    return _keywords_from_text(" ".join(texts))


def _stage_and_outputs() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """rs_run.spec() 阶段注册表 → (script → [stage], script → [声明产物])。"""
    import rs_run  # noqa: PLC0415 — 同目录,延迟导入避免无谓开销
    stages: dict[str, list[str]] = {}
    outputs: dict[str, list[str]] = {}
    for st in rs_run.spec():
        for sc in st.get("scripts", []):
            stages.setdefault(sc, []).append(st["id"])
            outputs.setdefault(sc, []).extend(
                o for o in st.get("outputs", []) if o not in outputs.get(sc, []))
    return stages, outputs


def _gate_map() -> dict[str, str]:
    """SKILL.md §2 管线表 → script → 门禁文案(阶段顺序拼接、去重)。"""
    text = SKILL_MD.read_text(encoding="utf-8")
    gates: dict[str, list[str]] = {}
    for m in re.finditer(r"^\|\s*\*\*(S\d+)\*\*\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|",
                         text, re.M):
        sid, scripts_cell, gate_cell = m.group(1), m.group(3), m.group(5)
        for sc in sorted(set(re.findall(r"rs_[a-z_]+", scripts_cell))):
            gates.setdefault(f"{sc}.py", []).append((sid, gate_cell.strip()))
    out = {}
    for sc, items in gates.items():
        seen: list[str] = []
        for _, g in sorted(items):
            if g and g not in seen:
                seen.append(g)
        out[sc] = ";".join(seen)
    return out


def build_catalog() -> dict:
    """三个机械源 → 能力目录(排序稳定,字节级可复现)。"""
    stages, outputs = _stage_and_outputs()
    gates = _gate_map()
    tools: list[dict] = []
    for script in sorted(SCRIPTS.glob("rs_*.py")):
        if script.name in EXCLUDED:
            continue
        try:
            parser = build_parser(script)
        except Exception:  # noqa: BLE001 — 无 argparse 装配的辅助脚本不进目录
            continue
        subs, sub_dest = _split_subcommands(parser)
        tool = {
            "script": script.name,
            "purpose": _doc_summary(script),
            "stage": stages.get(script.name, []),
            "outputs": outputs.get(script.name, []),
            "gate": gates.get(script.name, ""),
            "args": _args_summary(parser, sub_dest),
            "keywords": _keywords_from_help(parser),   # 第一册 T1.2:检索字段,从 argparse help 抽取
        }
        if subs:
            tool["commands"] = [
                {"name": f"{script.name} {s}", "sub": s,
                 "probe": _probe_argv(parser, s, sub_dest)}
                for s in subs
            ]
        else:
            tool["commands"] = [{"name": script.name, "sub": None,
                                 "probe": _probe_argv(parser, None, sub_dest)}]
        tools.append(tool)
    # 库工具(LIBRARIES 静态表):无 CLI,只登记用途,排序稳定
    for name, purpose in sorted(LIBRARIES.items()):
        tools.append({"script": name, "purpose": purpose, "library": True,
                      "stage": [], "outputs": [], "gate": "", "args": [], "commands": [],
                      "keywords": _keywords_from_text(purpose)})
    return {
        "version": 2,
        "_doc": "能力目录(单一真相源):python skills/cutflow/scripts/rs_caps.py generate "
                "从 argparse + rs_run 阶段注册表 + SKILL.md 管线表自动生成;禁止手改,漂移即测试红。"
                "查能力先读这里(短、机器可读),细节跑 <script> --help;probe = 最小可用 argv(T3 对拍)。"
                "检索:rs_caps.py search <关键词>(按命令名/用途/keywords/阶段过滤,空格分词 AND);"
                "keywords 字段从 argparse help 自动抽取(第一册 T1.2)。"
                "命令粒度 = argparse 可见的子命令(choices/subparsers);旗标式脚本(如 rs_artboard 的 "
                "gen-cards/export-fallback)不拆条,能力看 args 与各分册示例。",
        "tools": tools,
    }


def dumps_catalog(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=1) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["generate", "check", "search"],
                    help="generate=重新生成目录;check=再生成并与盘上对比(漂移退出 2);"
                         "search=按关键词检索目录")
    ap.add_argument("keyword", nargs="?", default=None,
                    help="search 用:关键词(可空格分多个,AND 语义;按命令名/用途/keywords/阶段匹配)")
    ap.add_argument("--out", default=str(CAPABILITIES), help="目录落点(缺省 skills/cutflow/capabilities.json)")
    a = ap.parse_args()

    if a.command == "search":
        if not a.keyword:
            return emit(False, "CAPS_SEARCH_USAGE",
                        "用法:rs_caps.py search <关键词> [更多词…](空格分词 = AND)", exit_code=2)
        doc = build_catalog()
        words = a.keyword.split()
        hits: list[dict] = []
        for t in doc["tools"]:
            hay = json.dumps(t, ensure_ascii=False).lower()
            if all(w.lower() in hay for w in words):
                hits.append({"script": t["script"], "purpose": t["purpose"],
                             "stage": t["stage"],
                             "commands": [c["name"] for c in t["commands"]],
                             "keywords": t.get("keywords", [])})
        names = ", ".join(h["script"] for h in hits[:10]) or "(无)"
        return emit(True, "CAPS_SEARCH",
                    f"检索「{a.keyword}」:{len(hits)}/{len(doc['tools'])} 个工具命中 → {names}",
                    {"query": a.keyword, "matches": len(hits), "tools": hits})

    doc = build_catalog()
    out = Path(a.out)
    if a.command == "generate":
        write_text_atomic(out, dumps_catalog(doc))
        n_cmds = sum(len(t["commands"]) for t in doc["tools"])
        return emit(True, "CAPS_GENERATED",
                    f"能力目录已生成:{len(doc['tools'])} 工具 / {n_cmds} 条命令 → {out}",
                    {"path": str(out), "tools": len(doc["tools"]), "commands": n_cmds})
    if not out.is_file():
        return emit(False, "CAPS_MISSING",
                    f"能力目录不存在:{out}(先跑 rs_caps.py generate)", exit_code=2)
    disk = out.read_text(encoding="utf-8")
    if disk != dumps_catalog(doc):
        return emit(False, "CAPS_DRIFT",
                    "能力目录与再生成结果不一致(手改了?实现变了没再生成?)——"
                    "只允许 rs_caps.py generate 再生成,禁止手抄", exit_code=2)
    return emit(True, "CAPS_MATCH", "能力目录与再生成结果一致(零漂移)",
                {"path": str(out)})


if __name__ == "__main__":
    sys.exit(main())
