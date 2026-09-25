"""阶段路径唯一真相源(ADR-0045 / ADR-0046)。全仓禁止阶段目录字符串字面量。

体例与 `rs_common.RATIOS`(比例唯一真相源)、`rs_subtitle.STYLES`(字幕样式唯一
真相源)同构:目录名只在本文件出现一次,日后改名 = 改一个常量,门禁
(`tests/test_paths_gate.py`)机械保证其他文件零字面量。

调用纪律:
  · 新代码取路径**只有 `p()` / `resolve()` 一族入口**,绝不手写目录名;
  · 纯展示文本(报错消息、文档)用 `p()` 拼 f-string,不落旧英文名;
  · 过渡期兼容:`resolve()` 新名优先、旧名兜底并 WARN —— 旧工程不必立刻迁移
    (跑 `tools/migrate_paths.py` 一键转正,见 ADR-0045 §四问)。
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path

# ---------------------------------------------------------------- 唯一真相源

# 逻辑键 → 中文目录名(ADR-0045 §4.2 映射表)。**目录中文,文件名 ASCII**。
STAGE_DIRS: dict[str, str] = {
    "brief":     "00_制作简报",
    "materials": "01_原始素材",
    "sensed":    "02_转写与校对",
    "assets":    "03_创作素材",
    "cut":       "04_粗剪决策",
    "timeline":  "05_时间线工程",
    "output":    "06_成片输出",
    "state":     "_内部状态",
    "deliver":   "成品",
}

# 旧英文名 → 逻辑键(迁移与过渡期读取用;迁移工具与 resolve 兜底共用)
LEGACY_ALIASES: dict[str, str] = {
    "00_brief": "brief", "01_materials": "materials", "02_sensed": "sensed",
    "03_assets": "assets", "04_cut": "cut", "05_ir": "timeline",
    "06_output": "output", "_state": "state",
}

READONLY = {"materials"}    # 只读阶段(写入即违规,rs_verify/rs_ingest 把守)
INTERNAL = {"state"}        # 内部阶段(清理白名单之外不外露)
NEVER_CLEAN = {"deliver"}   # 成品区永不被清理(ADR-0052;rs_cleanup 硬跳过)

# 已废弃目录:不再创建;迁移工具遇见即移入备份(不迁入新结构)
RETIRED = {"04_ai_prompts", "02_sensed/frames"}

# 新增子目录(ADR-0045):剪映 5.9 草稿落点,随迁移/首次落盘创建
JIANYING_SUB = ("导出", "剪映59")

# 旧名→新名 对照的逆向表(迁移工具回写内引用/回滚用)
NEW_TO_LEGACY: dict[str, str] = {v: k for k, v in LEGACY_ALIASES.items()}

# 遍历跳过用:state 目录的新旧名(rs_run 的 SKIP_DIRS 等;不含全部阶段目录 ——
# 输入 hash 要扫素材/产物目录,绝不能把它们整个跳掉)
STATE_DIR_NAMES = {STAGE_DIRS["state"]} | {
    old for old, key in LEGACY_ALIASES.items() if key == "state"}

# 旧 state 目录名(迁移工具的备份宿主选择用)
LEGACY_STATE_NAME = next(old for old, key in LEGACY_ALIASES.items() if key == "state")


# ---------------------------------------------------------------- 访问函数

def p(key: str) -> str:
    """逻辑键 → 目录名(唯一查询口)。未知键报错,绝不静默猜。"""
    try:
        return STAGE_DIRS[key]
    except KeyError:
        raise KeyError(f"未知阶段逻辑键 {key!r}(可选:{'/'.join(STAGE_DIRS)})") from None


def root(project: str | Path) -> Path:
    """工程根(str/Path 均可)。"""
    return Path(project)


def of(project: str | Path, key: str) -> Path:
    """阶段目录绝对路径(新名;写盘/新建结构用)。"""
    return root(project) / p(key)


def _legacy_name(key: str) -> str | None:
    return next((old for old, k in LEGACY_ALIASES.items() if k == key), None)


def is_stage_dirname(name: str) -> bool:
    """目录名是否为阶段目录(新名或旧名)。判断「某路径的父目录是不是阶段目录」用。"""
    return name in STAGE_DIRS.values() or name in LEGACY_ALIASES


_WARNED: set[tuple[str, str]] = set()


def _warn_legacy(rp: Path, key: str, old: str) -> None:
    """旧目录兜底 WARN(每 (工程, 键) 每进程只报一次,防刷屏)。"""
    marker = (str(rp), key)
    if marker in _WARNED:
        return
    _WARNED.add(marker)
    warnings.warn(
        f"[rs_paths] 工程 {rp.name} 仍是旧目录结构:{old}/ → 应为 {p(key)}/;"
        "本次按旧目录兜底读取,请跑 tools/migrate_paths.py 转正",
        LegacyPathWarning, stacklevel=3)


class LegacyPathWarning(UserWarning):
    """旧目录结构兜底告警(过渡期;迁移后不再出现)。"""


def resolve_name(project: str | Path, key: str) -> str:
    """目录名解析:新名优先;新名不存在而旧名存在 → 旧名 + WARN(过渡期)。

    两者都不存在(全新工程)→ 新名(后续 mkdir 即新结构)。
    """
    new = p(key)
    rp = root(project)
    if (rp / new).exists():
        return new
    old = _legacy_name(key)
    if old and (rp / old).exists():
        _warn_legacy(rp, key, old)
        return old
    return new


def resolve(project: str | Path, key: str) -> Path:
    """阶段目录解析(读盘口径):新名优先,旧名兜底 + WARN。"""
    return root(project) / resolve_name(project, key)


def rel(project: str | Path, key: str, *parts: str) -> str:
    """工程内相对路径字符串(按 resolve 的实际目录名),供注册表/命令行拼接。

    例:rel(root, "timeline", "wordline.json") → "05_时间线工程/wordline.json"
    (旧结构工程上 → "05_ir/wordline.json")。
    """
    return "/".join((resolve_name(project, key), *parts))


# ---------------------------------------------------------------- 常用落点

def pipeline_json(project: str | Path) -> Path:
    """05_时间线工程/pipeline.json(rs_run 聚合账)。"""
    return resolve(project, "timeline") / "pipeline.json"


def project_json(project: str | Path) -> Path:
    """05_时间线工程/project.json(CutForge 同源工程)。"""
    return resolve(project, "timeline") / "project.json"


def wordline_json(project: str | Path, final: bool = False) -> Path:
    """wordline.json / wordline.final.json(final=成片空间 remap 产物)。"""
    return resolve(project, "timeline") / ("wordline.final.json" if final else "wordline.json")


def jianying_draft(project: str | Path) -> Path:
    """05_时间线工程/导出/剪映59/(半成品出口落点,ADR-0052 单向出口)。"""
    d = resolve(project, "timeline")
    for part in JIANYING_SUB:
        d = d / part
    return d


def final_videos(project: str | Path) -> list[Path]:
    """全部成片路径:S8 新账在 06_成片输出/final/,S5 变体在 branded/;
    旧工程顶层遗留 final_*.mp4 兼容读取(按 mtime 升序,[-1] 即最新)。"""
    out = resolve(project, "output")
    vids: list[Path] = []
    if out.is_dir():
        for pat in ("final/final_*.mp4", "final_*.mp4"):
            vids.extend(out.glob(pat))
    return sorted(vids, key=lambda q: q.stat().st_mtime)


def manifest_json(project: str | Path) -> Path:
    """01_原始素材/manifest.json(S0 产物)。"""
    return resolve(project, "materials") / "manifest.json"


def verify_json(project: str | Path) -> Path:
    """_内部状态/verify.json(分级自检台账)。"""
    return resolve(project, "state") / "verify.json"


def brief_md(project: str | Path) -> Path:
    """00_制作简报/brief.md。"""
    return resolve(project, "brief") / "brief.md"


def backup_dir(project: str | Path, tag: str = "") -> Path:
    """_内部状态/backup/[tag](阶段备份 / migrate / publish 共用父目录)。"""
    d = resolve(project, "state") / "backup"
    return d / tag if tag else d


# ---------------------------------------------------------------- 结构操作

def ensure(project: str | Path, with_deliver: bool = False) -> Path:
    """铺工程目录骨架(新结构;旧结构工程按 resolve 落回旧名,不造混存)。

    成品/(deliver)是交付容器,按需创建(--publish / 迁移不代建),默认不铺。
    RETIRED 目录绝不创建(ADR-0045 决策 3)。
    """
    rp = root(project)
    keys = set(STAGE_DIRS) - {"deliver"} if not with_deliver else set(STAGE_DIRS)
    for key in sorted(keys):
        (rp / resolve_name(rp, key)).mkdir(parents=True, exist_ok=True)
    return rp


def check(project: str | Path) -> dict:
    """结构自检:缺失阶段 / 旧目录残留 / 新旧混存 / 废弃目录。

    返回 {"ok", "missing", "legacy", "mixed", "retired"};ok=False 当且仅当
    新旧混存(同名新旧并存是迁移事故,必须人工处置,见 tools/migrate_paths.py 预检)。
    junction 兼容别名是目录联接而非真实目录,不计入 legacy/mixed。
    """
    rp = root(project)

    def real(old: str) -> bool:
        q = rp / old
        return q.is_dir() and not os.path.isjunction(str(q))

    missing = [p(k) for k in sorted(STAGE_DIRS) if not (rp / p(k)).exists()]
    legacy = sorted(old for old in LEGACY_ALIASES if real(old))
    mixed = sorted(old for old in legacy if (rp / p(LEGACY_ALIASES[old])).exists())
    retired = sorted(r for r in RETIRED if (rp / r).exists())
    return {"ok": not mixed, "missing": missing, "legacy": legacy,
            "mixed": mixed, "retired": retired,
            "project": str(rp)}
