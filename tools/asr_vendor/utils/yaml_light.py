"""极简 YAML 子集解析器（FunASR 模型 config.yaml 专用）。

为什么需要它：MomentShift 是 PyInstaller 独立应用，运行时只允许
``numpy / onnxruntime / jieba`` 三个第三方依赖，不能引入 PyYAML。
``funasr_onnx`` 原本用 ``yaml.load`` 读取模型的 ``config.yaml``（56KB 训练
配置），这里用纯标准库实现其子集：

- 嵌套映射（缩进）
- 块序列（``- item``）与内联序列（``[a, b]``）
- 标量：整数 / 浮点 / 布尔 / null / 引号字符串
- 注释（``#``）与空行
- ``-   - a`` 这种「序列项本身是内联序列」的写法（FunASR 配置里用于
  ``best_model_criterion`` 等字段）

不保证覆盖完整 YAML 规范；只保证能正确解析 FunASR 导出的模型配置，并正确
提取 ``frontend_conf`` / ``model_conf`` / ``lang`` / ``token_list``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _strip_comment(line: str) -> str:
    """去掉行尾注释，但保留引号内的 ``#``。"""
    in_single = False
    in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            return line[:i]
    return line


def _tokenize(text: str) -> list[tuple[int, str]]:
    """把文本切成 ``(缩进, 内容)`` 序列，去掉空行与注释。"""
    tokens: list[tuple[int, str]] = []
    for raw in text.expandtabs(2).splitlines():
        line = _strip_comment(raw)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        tokens.append((indent, line.strip()))
    return tokens


def _split_inline(s: str) -> list[str]:
    """按逗号切分内联列表，忽略引号内的逗号。"""
    parts: list[str] = []
    cur: list[str] = []
    in_single = False
    in_double = False
    for ch in s:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "," and not in_single and not in_double:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return parts


def _scalar(s: str) -> Any:
    """解析一个标量（或内联列表 / 内联字典）。"""
    s = s.strip()
    if not s or s in ("null", "Null", "NULL", "~"):
        return None
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [] if not inner else [_scalar(x) for x in _split_inline(inner)]
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        if not inner:
            return {}
        result: dict[str, Any] = {}
        for part in _split_inline(inner):
            key, _, val = part.partition(":")
            result[key.strip()] = _scalar(val)
        return result
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _looks_like_mapping_item(rest: str) -> bool:
    """判断序列项内容是否是 ``key: value``（而非普通标量）。

    用「冒号后跟空格」或「以冒号结尾」判定，避免把 URL / 路径里的冒号误判。
    """
    if rest.startswith(("'", '"', "[", "{")):
        return False
    if ": " in rest or rest.endswith(":"):
        key = rest.split(":", 1)[0]
        return bool(key) and " " not in key
    return False


def _parse_map(tokens: list[tuple[int, str]], i: int, indent: int) -> tuple[dict, int]:
    """解析从 ``tokens[i]`` 开始的映射块；返回 ``(dict, 下一个下标)``。"""
    result: dict[str, Any] = {}
    while i < len(tokens) and tokens[i][0] == indent and not tokens[i][1].startswith("- "):
        content = tokens[i][1]
        key, _, rest = content.partition(":")
        key = key.strip()
        rest = rest.strip()
        if not rest:
            # 嵌套块可能是「更深的映射」或「同缩进的序列」（YAML 允许）：
            #   key:
            #       a: 1
            #   key:
            #   - a
            #   - b
            if i + 1 < len(tokens) and (
                tokens[i + 1][0] > indent
                or (tokens[i + 1][0] == indent and tokens[i + 1][1].startswith("- "))
            ):
                result[key], i = _parse_block(tokens, i + 1, tokens[i + 1][0])
            else:
                result[key] = None
                i += 1
        else:
            result[key] = _scalar(rest)
            i += 1
    return result, i


def _parse_seq(tokens: list[tuple[int, str]], i: int, indent: int) -> tuple[list, int]:
    """解析从 ``tokens[i]`` 开始的序列块；返回 ``(list, 下一个下标)``。"""
    result: list[Any] = []
    while i < len(tokens) and tokens[i][0] == indent and tokens[i][1].startswith("- "):
        rest = tokens[i][1][2:].strip()
        if not rest:
            # ``-`` 后面直接换行 → 嵌套块
            if i + 1 < len(tokens) and tokens[i + 1][0] > indent:
                result.append(_parse_block(tokens, i + 1, tokens[i + 1][0])[0])
                i = _parse_block(tokens, i + 1, tokens[i + 1][0])[1]
            else:
                result.append(None)
                i += 1
            continue
        if rest.startswith("- "):
            # ``-   - a``：序列项本身是内联序列，后续更深行继续这个内联序列
            inner: list[Any] = [_scalar(rest[2:].strip())]
            i += 1
            if i < len(tokens) and tokens[i][0] > indent:
                more, i = _parse_seq(tokens, i, tokens[i][0])
                inner.extend(more)
            result.append(inner)
            continue
        if _looks_like_mapping_item(rest):
            key, _, vrest = rest.partition(":")
            key = key.strip()
            vrest = vrest.strip()
            if vrest:
                item: dict[str, Any] = {key: _scalar(vrest)}
                i += 1
            else:
                if i + 1 < len(tokens) and tokens[i + 1][0] > indent:
                    item = {key: _parse_block(tokens, i + 1, tokens[i + 1][0])[0]}
                    i = _parse_block(tokens, i + 1, tokens[i + 1][0])[1]
                else:
                    item = {key: None}
                    i += 1
            # 极少数情况：序列项映射有多行（``- k1: v`` 后跟更深的 ``k2: v``）
            if i < len(tokens) and tokens[i][0] > indent and not tokens[i][1].startswith("- "):
                extra, i = _parse_map(tokens, i, tokens[i][0])
                item.update(extra)
            result.append(item)
            continue
        result.append(_scalar(rest))
        i += 1
    return result, i


def _parse_block(tokens: list[tuple[int, str]], i: int, indent: int) -> tuple[Any, int]:
    """解析一个块（映射或序列）；返回 ``(value, 下一个下标)``。"""
    if i >= len(tokens):
        return None, i
    if tokens[i][0] != indent:
        return None, i
    if tokens[i][1].startswith("- "):
        return _parse_seq(tokens, i, indent)
    return _parse_map(tokens, i, indent)


def parse_yaml(text: str) -> Any:
    """解析 YAML 文本，返回 Python 对象（通常是 dict）。"""
    tokens = _tokenize(text)
    if not tokens:
        return {}
    value, _ = _parse_block(tokens, 0, tokens[0][0])
    return value if isinstance(value, (dict, list)) else {}


def read_yaml(path: str | Path) -> dict[str, Any]:
    """读取 YAML 文件；路径不存在或根不是映射时返回空字典。"""
    p = Path(path)
    if not p.is_file():
        return {}
    value = parse_yaml(p.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}
