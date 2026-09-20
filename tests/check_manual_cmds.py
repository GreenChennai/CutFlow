"""手册命令 ↔ argparse 机械对拍门禁(P19-1,副文档 03 §4.3)。

背景:P19 的翻车形态 —— SKILL.md 四桥表写了个不存在的 `rs_notes.py tail`,
Agent 照手册执行 → 退出码 2,审计链断第一步。文档命令从此**机械对拍**,不再靠人眼:

  扫描 SKILL.md + rules/**/*.md 里的 rs_*.py 命令行(行内反引号 span + 围栏代码块,
  含 PowerShell 反引号续行),逐条用「AST 重放该脚本的 argparse 装配」得到的解析器
  做 parse_args —— **解析级验收,绝不执行任何命令**。能被 argparse 接受 = 对拍通过。

T2-1 起,AST 重放实现收编在 skills/cutflow/scripts/rs_caps.py(能力目录生成器与
本门禁共用同一套 build_parser,防两份重放器各自漂移);本文件保留「从手册提取命令」
与「逐条对拍」的门禁职责。

用法:
  python tests/check_manual_cmds.py            # 逐条打印对拍结果,有失败退出码非零
  pytest tests/test_v19_manual_gate.py -q      # 同一门禁的测试形态

提取口径(与手册书写约定一一对应):
  · 自带命令:span 首 token 是 `rs_*.py`(可带 `python` / 路径前缀)且 ≥2 个 token;
    单 token 裸脚本名是「提及」不是命令,不校验;
  · 同行后续 span 以 `--flag` 或纯小写子命令开头 = 「X / Y / Z」备选片段,
    每个片段作为**独立命令**校验(script + 片段);括号(含全角)内的 span 是注释散文,跳过;
  · 占位符归一:`<工程>` / `…` / `...` → `P`;`[--apply]` 剥壳取内容;
  · `--probe` 特例:四桥用 sys.argv 前置拦截、argparse 不认识它 —— 以「源码含 --probe
    字面量」代替 parse(探针不属于业务参数)。
"""
from __future__ import annotations

import argparse
import contextlib
import io as _io
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from rs_caps import build_parser  # noqa: E402  — AST 重放唯一实现(见模块 docstring)

DOCS: list[Path] = [
    REPO / "skills" / "cutflow" / "SKILL.md",
    *sorted((REPO / "skills" / "cutflow" / "rules").rglob("*.md")),
]

SCRIPT_RE = re.compile(r"^(?:.*[\\/])?(rs_[a-z]+\.py)$")
SUBCMD_RE = re.compile(r"^[a-z][a-z0-9-]*$")


# ---------------------------------------------------------------- 提取

def _norm_tokens(toks: list[str]) -> list[str]:
    """占位符归一:<工程>/…/... → P;[...] 与引号剥壳。"""
    out = []
    for t in toks:
        t = t.strip("[](){}\"'“”「」『』,;")
        if t in ("", "…", "...") or "…" in t or "..." in t or re.fullmatch(r"<[^<>]+>", t):
            t = "P"
        out.append(t)
    return out


def _inline_commands(line: str) -> list[tuple[str, list[str]]]:
    """一行内的反引号 span → [(script, tokens)]。括号深度在 span 之外统计。"""
    cmds: list[tuple[str, list[str]]] = []
    depth = 0
    anchor: str | None = None
    parts = line.split("`")
    for i, seg in enumerate(parts):
        if i % 2 == 0:
            depth += seg.count("(") + seg.count("（") - seg.count(")") - seg.count("）")
            continue
        toks = seg.split()
        if not toks:
            continue
        if toks[0] == "python":          # 行内 span 少见,容忍 `python x/rs_cut.py ...`
            toks = toks[1:]
            if not toks:
                continue
        m = SCRIPT_RE.match(toks[0])
        if m:
            if len(toks) >= 2:           # ≥2 token 才是命令;裸脚本名=提及
                cmds.append((m.group(1), _norm_tokens(toks[1:])))
                anchor = m.group(1)
            continue
        if in_paren := (depth > 0):
            continue                     # 括号内的 span 是注释散文
        if anchor is None:
            continue
        if "/" in toks[0]:
            continue                     # `--from/--only` 这类连写是散文,不是单个旗标
        if toks[0].startswith("--") or SUBCMD_RE.match(toks[0]):
            # 「主命令 / 片段 / 片段」排 style:片段 = 独立备选命令
            cmds.append((anchor, _norm_tokens(toks)))
    return cmds


def _fenced_commands(lines: list[str], start: int) -> tuple[list[tuple[int, str, list[str]]], int]:
    """围栏代码块 → 命令列表;处理 PowerShell 反引号续行与行尾注释。

    `start` = lines[0] 在原文档里的 1-based 行号;返回 consumed = 已消费行数
    (不含闭合围栏行 —— 调用方要靠它落回闭合行切换 in_fence)。
    """
    out: list[tuple[int, str, list[str]]] = []
    buf: list[str] = []
    join_start = 0
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("```"):              # 闭合围栏:必须在自增前 break(行号语义)
            break
        here = start + i                     # 当前行的 1-based 行号
        i += 1
        if not s:
            continue
        if s.endswith("`") and not s.endswith("\\`"):    # PowerShell 续行
            if not buf:
                join_start = here
            buf.append(s[:-1].strip())
            continue
        joined = " ".join([*buf, s]) if buf else s
        join_line = join_start if buf else here
        buf, join_start = [], 0
        joined = re.sub(r"\s+#.*$", "", joined).strip()   # 行尾注释
        toks = joined.split()
        if toks and toks[0] == "python":
            toks = toks[1:]
        if len(toks) >= 2 and (m := SCRIPT_RE.match(toks[0])):
            out.append((join_line, m.group(1), _norm_tokens(toks[1:])))
    return out, i


def extract_commands(text: str) -> list[tuple[int, str, list[str]]]:
    """(行号, script, tokens) 列表;行内 span 与围栏块两种载体都扫。"""
    out: list[tuple[int, str, list[str]]] = []
    lines = text.splitlines()
    i = 0
    in_fence = False
    while i < len(lines):
        s = lines[i].strip()
        line_no = i + 1
        if s.startswith("```"):
            in_fence = not in_fence
            if in_fence:
                got, consumed = _fenced_commands(lines[i + 1:], line_no + 1)
                out.extend(got)
                i += 1 + consumed
                continue
            i += 1
            continue
        if not in_fence:
            out.extend((line_no, sc, toks) for sc, toks in _inline_commands(lines[i]))
        i += 1
    return out


# ---------------------------------------------------------------- 对拍

def validate_one(script_name: str, tokens: list[str]) -> tuple[bool, str]:
    """返回 (是否被 argparse 接受, 说明)。解析级,绝不执行。"""
    script = SCRIPTS / script_name
    if not script.is_file():
        return False, "脚本不存在"
    if tokens == ["--probe"]:
        return ('"--probe"' in script.read_text(encoding="utf-8"),
                "probe 探针(源码含 --probe 字面量)")
    try:
        parser = build_parser(script)
    except Exception as exc:  # noqa: BLE001
        return False, f"argparse 重放失败:{type(exc).__name__}: {exc}"
    try:
        with contextlib.redirect_stderr(_io.StringIO()):   # 拒绝时的 usage 别吵到门禁输出
            parser.parse_args(tokens)
    except SystemExit as exc:               # argparse 的报错通道
        return False, f"argparse 拒绝(exit {exc.code})"
    except argparse.ArgumentError as exc:
        return False, f"argparse 参数错:{exc}"
    return True, "接受"


def run_gate(docs: list[Path] | None = None) -> tuple[list[dict], int]:
    """对全部文档跑对拍。返回 (逐条结果, 失败数)。"""
    rows: list[dict] = []
    failed = 0
    seen: set[tuple] = set()
    for doc in (docs or DOCS):
        if not doc.is_file():
            continue
        rel = doc.relative_to(REPO).as_posix()
        for line_no, script, tokens in extract_commands(doc.read_text(encoding="utf-8")):
            key = (script, tuple(tokens))
            dup = key in seen
            seen.add(key)
            ok, note = validate_one(script, tokens)
            failed += 0 if ok else 1
            rows.append({"doc": rel, "line": line_no, "script": script,
                         "tokens": tokens, "ok": ok, "note": note, "dup": dup})
    return rows, failed


def main() -> int:
    rows, failed = run_gate()
    for r in rows:
        mark = "PASS" if r["ok"] else "FAIL"
        dup = "(dup)" if r["dup"] else ""
        print(f"[{mark}] {r['doc']}:{r['line']} {r['script']} {' '.join(r['tokens'])} {dup}"
              + ("" if r["ok"] else f"  ← {r['note']}"))
    print(f"\n对拍 {len(rows)} 条命令,失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
