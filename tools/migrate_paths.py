# -*- coding: utf-8 -*-
"""工程目录迁移工具(ADR-0045 §4.6):旧英文结构 → 中文新结构,安全/可回滚/可验证。

用法:
  python tools/migrate_paths.py <工程>              # 迁移(默认写 junction 兼容别名)
  python tools/migrate_paths.py <工程> --dry-run    # 只打印计划,不动盘
  python tools/migrate_paths.py <工程> --rollback   # 回滚到最近一次迁移前
  python tools/migrate_paths.py <工程> --no-alias   # 迁移但不写 junction 别名
  python tools/migrate_paths.py <工程> --keep-alias # 显式声明保留别名(默认行为)

七步(§4.6):①预检(新旧同名并存 → 报错停)②备份到 _内部状态/backup/migrate-<ts>/
③改名(RETIRED 目录移入备份,不迁入新结构)④改写工程内 JSON/MD/ASS 的旧路径引用
⑤junction 别名(旧名 → 新名,失败降级 WARN 不阻断)⑥写迁移报告 _内部状态/migrate-<ts>.md
⑦自检(rs_paths.check;失败提示 --rollback,不自动继续)。

幂等:工程已是新结构(无旧目录)→ 报「无需迁移」并以 0 退出。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "cutflow" / "scripts"))
import rs_paths  # noqa: E402

TEXT_EXTS = {".json", ".md", ".ass"}
SKIP_DIR_NAMES = {".git", "__pycache__"}


def _now_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def _dir_pairs() -> list[tuple[str, str]]:
    """旧名 → 新名 的完整改名对照(保序:state 最后,保证备份随迁)。"""
    out = []
    for old, key in rs_paths.LEGACY_ALIASES.items():
        out.append((old, rs_paths.p(key)))
    # state 排最后:备份放进旧 _state,改名后自然落到 _内部状态/backup/
    out.sort(key=lambda t: t[1] == rs_paths.p("state"))
    return out


# ---------------------------------------------------------------- 步骤 ① 预检

def precheck(root: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """返回 (改名计划 [(旧, 新)], 退役目录清单)。新旧同名并存 → SystemExit。"""
    plan: list[tuple[str, str]] = []
    retired: list[str] = []
    for old, new in _dir_pairs():
        old_ok = (root / old).is_dir() and not os.path.isjunction(str(root / old))
        new_ok = (root / new).is_dir()
        if old_ok and new_ok:
            raise SystemExit(
                f"[ABORT] 新旧目录并存(拒绝合并,请人工确认取哪边):\n"
                f"  {root / old}\n  {root / new}\n"
                f"处理:确认保留新结构后手工移走 {old}/,或反向处理后重跑。")
        if old_ok:
            plan.append((old, new))
    for r in rs_paths.RETIRED:
        if (root / r).is_dir():
            retired.append(r)
    return plan, retired


# ---------------------------------------------------------------- 步骤 ② 备份

def make_backup(root: Path, plan: list[tuple[str, str]], retired: list[str]) -> Path:
    """备份所有将被动到的目录到 _内部状态/backup/migrate-<ts>/(可回滚的唯一保障)。

    旧结构工程把备份放进旧 <state> 目录 —— 随目录一起改名,最终位置即契约位置
    _内部状态/backup/migrate-<ts>/。无旧 state 目录的工程直接建 _内部状态。
    """
    host = root / (rs_paths.LEGACY_STATE_NAME if (root / rs_paths.LEGACY_STATE_NAME).is_dir()
                   else rs_paths.p("state"))
    base = host / "backup"
    base.mkdir(parents=True, exist_ok=True)
    ts = _now_ts()
    dest = base / f"migrate-{ts}"
    n = 1
    while dest.exists():                       # 同秒重跑:追加序号,绝不覆盖旧备份
        n += 1
        dest = base / f"migrate-{ts}-{n}"
    dest.mkdir(parents=True)
    for old, _ in plan:
        src = root / old
        if old == rs_paths.LEGACY_STATE_NAME:
            # 备份宿主就是旧 state 目录:排除 backup/ 自身,否则把备份复制进自己(递归爆栈)
            shutil.copytree(src, dest / old, ignore=shutil.ignore_patterns("backup"))
        else:
            shutil.copytree(src, dest / old)
    for r in retired:
        shutil.copytree(root / r, dest / r.replace("/", "_"))
    (dest / "_manifest.json").write_text(json.dumps(
        {"kind": "migrate-backup", "at": dest.name, "dirs": [o for o, _ in plan],
         "retired": retired}, ensure_ascii=False, indent=1), encoding="utf-8")
    return dest


# ---------------------------------------------------------------- 步骤 ③ 改名

def rename_dirs(root: Path, plan: list[tuple[str, str]], retired: list[str]) -> None:
    """RETIRED 目录先删(可能嵌在待改名目录里,如 <sensed>/frames);再逐一 rename;
    最后补建剪映草稿落点子目录。"""
    for r in retired:
        shutil.rmtree(root / r)   # 内容已在备份(目录名里的 / 以 _ 展平)
    for old, new in plan:
        try:
            (root / old).rename(root / new)
        except OSError as exc:
            raise SystemExit(f"[ABORT] 改名失败 {root / old} → {root / new}:{exc}"
                             "(备份未动,可整目录手工复原)") from exc
    # 新增子目录落点(ADR-0045):剪映 5.9 半成品出口
    draft = rs_paths.jianying_draft(root)
    if draft.parent.parent.is_dir():
        draft.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 步骤 ④ 改内引用

def ref_patterns() -> list[tuple[re.Pattern, str]]:
    """旧目录名 → 新目录名 的正则对:仅匹配路径段边界(前后不得是字母/数字/下划线)。

    只动路径语境(名字后跟 `/` 续接,或处于引用收尾位)—— 语义字段里的普通单词不碰。
    """
    out = []
    for old, key in rs_paths.LEGACY_ALIASES.items():
        new = rs_paths.p(key)
        out.append((re.compile(r"(?<![A-Za-z0-9_])" + re.escape(old) + r"(?=/)"), new))
        out.append((re.compile(r"(?<![A-Za-z0-9_])" + re.escape(old)
                               + r"(?=[\"'\s)\],;}]|$)"), new))
    return out


def _skipped(f: Path, root: Path) -> bool:
    """备份/内部状态/环境目录里的文件一律不改(备份是回滚证据链,必须保持原样)。"""
    rel = f.relative_to(root)
    if any(part in SKIP_DIR_NAMES for part in rel.parts[:-1]):
        return True
    if "backup" in rel.parts:
        return True
    state_names = rs_paths.STATE_DIR_NAMES
    if any(part in state_names for part in rel.parts[:-1]):
        return True
    return False


def rewrite_refs(root: Path, pats: list[tuple[re.Pattern, str]]) -> list[dict]:
    """扫描工程内 .json/.md/.ass,按映射表改写旧路径引用。返回逐文件改写统计。"""
    changes: list[dict] = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in TEXT_EXTS or _skipped(f, root):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue   # 非 UTF-8 文本:不碰(报告口径 = 未识别即不改写)
        new_text, n = text, 0
        for pat, repl in pats:
            new_text, k = pat.subn(repl, new_text)
            n += k
        if n and new_text != text:
            f.write_text(new_text, encoding="utf-8")
            changes.append({"file": f.relative_to(root).as_posix(), "replacements": n})
    return changes


# ---------------------------------------------------------------- 步骤 ⑤ junction 别名

def make_junctions(root: Path, plan: list[tuple[str, str]]) -> list[str]:
    """为每个改名目录建「旧名 → 新名」junction(过渡期只读别名);失败 WARN 不阻断。"""
    made: list[str] = []
    for old, new in plan:
        link, target = root / old, root / new
        if link.exists():
            continue
        p = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, text=True)
        if p.returncode == 0 and link.is_dir():
            made.append(old)
        else:
            _warn(f"junction 别名创建失败({old} → {new}):"
                  f"{(p.stderr or p.stdout).strip()[:120]};降级为不写别名"
                  "(未升级的外部工具将读不到旧路径)")
    return made


# ---------------------------------------------------------------- 步骤 ⑥ 报告 / 清单

def write_report(root: Path, ts: str, plan: list[tuple[str, str]], retired: list[str],
                 changes: list[dict], junctions: list[str], backup: Path) -> Path:
    lines = [f"# 迁移报告(migrate-{ts})", "",
             f"- 工程:`{root.name}`", f"- 时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "",
             f"## 目录改名({len(plan)})", ""]
    lines += [f"- `{old}/` → `{new}/`" for old, new in plan] or ["- (无)"]
    lines += ["", f"## 废弃目录移入备份({len(retired)})", ""]
    lines += [f"- `{r}/`(已移入备份,不再迁入新结构)" for r in retired] or ["- (无)"]
    lines += ["", f"## 内引用改写({len(changes)} 个文件)", ""]
    lines += [f"- `{c['file']}`:{c['replacements']} 处" for c in changes] or ["- (无)"]
    lines += ["", f"## junction 别名({len(junctions)})", ""]
    lines += [f"- `{j}/` → 对应新目录(过渡期只读别名;移除时点见 CHANGELOG 与 rs_doctor)"
              for j in junctions] or ["- (无;创建失败或 --no-alias)"]
    lines += ["", "## 备份", "", f"- `{backup.relative_to(root)}/`"
              "(回滚:`python tools/migrate_paths.py <工程> --rollback`)", ""]
    path = rs_paths.resolve(root, "state") / f"migrate-{ts}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (rs_paths.resolve(root, "state") / f"migrate-{ts}.json").write_text(json.dumps(
        {"kind": "cutflow-migrate", "ts": ts, "renames": plan, "retired": retired,
         "rewrites": changes, "junctions": junctions, "backup": str(backup)},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 步骤 ⑦ 自检

def self_check(root: Path) -> int:
    """rs_paths.check(结构/混存)→ rs_run --status(账本可读)→(有 CutForge 痕迹时)
    rs_editor check(四桥真检,缺件只 WARN —— 半成品工程合法地没有全部真相文件)。"""
    rep = rs_paths.check(root)
    if not rep["ok"]:
        print(json.dumps({"ok": False, "code": "MIGRATE_CHECK_FAIL", "data": rep},
                         ensure_ascii=False))
        _warn("自检发现新旧混存;请执行 --rollback 回滚")
        return 5
    p = subprocess.run([sys.executable, str(REPO / "skills" / "cutflow" / "scripts" / "rs_run.py"),
                        "--root", str(root), "--status"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        _warn(f"rs_run --status 未过(exit {p.returncode}):{(p.stderr or p.stdout)[-160:]};"
              "请执行 --rollback 回滚")
        return 5
    if (root / ".cutforge").is_dir():
        q = subprocess.run([sys.executable, str(REPO / "skills" / "cutflow" / "scripts" / "rs_editor.py"),
                            "check", str(root)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if q.returncode != 0:
            _warn(f"四桥冒烟(rs_editor check)缺件(半成品工程属正常,不阻断):"
                  f"{(q.stdout or '').strip().splitlines()[-1][:140] if (q.stdout or '').strip() else q.returncode}")
    return 0


# ---------------------------------------------------------------- 回滚

def find_manifest(root: Path, at: str = "") -> Path:
    state = rs_paths.resolve(root, "state")
    cands = sorted(state.glob("migrate-*.json")) if state.is_dir() else []
    if not cands:
        raise SystemExit(f"[ABORT] 找不到迁移清单({state}/migrate-*.json):无回滚目标")
    if at:
        want = next((c for c in cands if c.stem == f"migrate-{at}"), None)
        if want is None:
            raise SystemExit(f"[ABORT] 找不到迁移记录 migrate-{at}"
                             f"(可用:{[c.stem for c in cands]})")
        return want
    return cands[-1]


def rollback(root: Path, at: str = "") -> int:
    mpath = find_manifest(root, at)
    man = json.loads(mpath.read_text(encoding="utf-8"))
    state_new = rs_paths.p("state")
    # 备份路径按「当前 state 目录」重算:清单里记的是迁移时路径(经旧名 junction),
    # 而回滚第一步就是摘 junction —— 先摘就找不到备份了。junction 已摘/未摘都稳。
    backup = rs_paths.resolve(root, "state") / "backup" / Path(man["backup"]).name
    if not backup.is_dir():
        backup = Path(man["backup"])
    if not backup.is_dir():
        raise SystemExit(f"[ABORT] 备份目录不存在:{backup}(无法回滚)")
    # ① junction 先摘(摘的是链接本身,不动目标目录)
    for old, _ in man["renames"]:
        link = root / old
        if link.is_dir() and os.path.isjunction(str(link)):
            subprocess.run(["cmd", "/c", "rmdir", "/q", str(link)], check=True)
    # ② 改名目录:删新 → 从备份还原旧(_内部状态 自己最后处理 —— 备份就在它肚子里)
    for old, new in man["renames"]:
        if new == state_new:
            continue
        newp = root / new
        if newp.exists():
            shutil.rmtree(newp)
        if (backup / old).is_dir():
            shutil.copytree(backup / old, root / old)
    # ③ 废弃目录还原(若已随父目录从备份还原,跳过)
    for r in man.get("retired", []):
        if (root / r).is_dir():
            continue
        src = backup / r.replace("/", "_")
        if src.is_dir():
            shutil.copytree(src, root / r)
    # ④ _内部状态 改回旧名(备份随之回到 <旧state>/backup/migrate-<ts>/,留作证据)
    if any(n == state_new for _, n in man["renames"]):
        old_state = next(o for o, n in man["renames"] if n == state_new)
        (root / state_new).rename(root / old_state)
    # ⑤ 内引用反向改写(新名 → 旧名;备份与内部状态文件不动)
    inv: list[tuple[re.Pattern, str]] = []
    for old, new in man["renames"]:
        inv.append((re.compile(r"(?<![A-Za-z0-9_])" + re.escape(new) + r"(?=/)"), old))
        inv.append((re.compile(r"(?<![A-Za-z0-9_])" + re.escape(new)
                               + r"(?=[\"'\s)\],;}]|$)"), old))
    n_files = rewrite_refs(root, inv)
    print(f"[OK] 已回滚 migrate-{man['ts']}:目录与内引用均已还原"
          f"(改写 {n_files} 个文件;备份保留于 {backup})")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="CutFlow 工程目录迁移(旧英文 → 中文新结构)")
    ap.add_argument("project")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", help="只打印计划,不动盘")
    ap.add_argument("--rollback", action="store_true", help="回滚最近一次迁移")
    ap.add_argument("--at", default="", help="--rollback 指定迁移时间戳(缺省取最近)")
    ap.add_argument("--keep-alias", dest="keep_alias", action="store_true", default=True,
                    help="保留 junction 兼容别名(默认;计划保留 2 个发布周期)")
    ap.add_argument("--no-alias", dest="keep_alias", action="store_false",
                    help="不写 junction 别名")
    a = ap.parse_args()
    root = Path(a.project).resolve()
    if not root.is_dir():
        print(f"工程目录不存在:{root}")
        return 2

    if a.rollback:
        return rollback(root, a.at)

    plan, retired = precheck(root)
    if not plan and not retired:
        print("[OK] 无需迁移:工程已是新目录结构(或空工程)")
        return 0

    ts = _now_ts()
    print(f"迁移计划(migrate-{ts}):")
    for old, new in plan:
        print(f"  {old}/  →  {new}/")
    for r in retired:
        print(f"  {r}/  →  移入备份(废弃,不迁入新结构)")
    print("  内引用改写:*.json / *.md / *.ass")
    print(f"  junction 别名:{'保留(--keep-alias)' if a.keep_alias else '不写(--no-alias)'}")
    if a.dry_run:
        print("[DRY-RUN] 未动盘。去掉 --dry-run 执行。")
        return 0

    backup = make_backup(root, plan, retired)                        # ②
    rename_dirs(root, plan, retired)                                 # ③
    changes = rewrite_refs(root, ref_patterns())                     # ④
    junctions = make_junctions(root, plan) if a.keep_alias else []   # ⑤
    report = write_report(root, ts, plan, retired, changes,
                          junctions, backup)                         # ⑥
    rc = self_check(root)                                            # ⑦
    print(json.dumps({"ok": rc == 0,
                      "code": "MIGRATED" if rc == 0 else "MIGRATE_CHECK_FAIL",
                      "data": {"renames": len(plan), "retired": len(retired),
                               "rewrittenFiles": len(changes), "junctions": junctions,
                               "report": str(report), "backup": str(backup)}},
                     ensure_ascii=False))
    if rc:
        print("[HINT] 自检未过 —— 请执行 --rollback 回滚(备份未动)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
