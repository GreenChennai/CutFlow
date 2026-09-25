#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""声明式编辑层(ADR-0048):自然语言改片的确定性通道——context 投影 / ops-validate 校验 / apply 原子应用 / undo 逆写 / diff 对比。

五组子命令(计划书 §5.3/§5.4,完整语法见 rules/edit-op.md):

  python rs_edit.py context <工程> [--json] [--scope clip|project|subtitle|audio] [--budget 12KB]
  python rs_edit.py ops-validate <ops.json>
  python rs_edit.py apply <工程> --ops ops.json [--dry-run] [--actor agent|user] [--base-rev N] [--force]
  python rs_edit.py undo <工程> --last <n>
  python rs_edit.py diff <工程> --rev <a> --rev <b>

硬约束(附录 B 通用约束六条):
  · 禁止数组下标寻址(BAD_ADDRESS,退出码 2);一律稳定 clipId / 锚点;
  · after 只允许 schema 白名单字段(BAD_FIELD,退出码 2);
  · 值域校验(BAD_VALUE);时间写入吸附帧网格(fps 来自 IR);
  · 幂等:after == before 短路,不升 rev、不产 Op;
  · 文件级 Op(note.add / segment.protect)逆写回真相源文件(notes.json /
    cutlist.json),不误写 project.json。
  · U7 诚实条款:schema 未覆盖的字段/Op 一律 OP_UNSUPPORTED 显式拒绝
    (subtitle.highlight / keyframe.set / style.pacing 等),绝不静默写 schema 外
    字段;核查结论见 rules/edit-op.md「字段覆盖表」。

与 cutforge 对齐:apply/undo 写同一条 `.cutforge/oplog/<日>.jsonl`(键名口径与
cutforge-io 实际序列化一致:op_id/op_kind/base_rev 蛇形——oplog.schema.json 写的
驼峰与其 Rust 结构体漂移,以「cutforge 能反序列化」为准,见 rules/edit-op.md
对齐节),同时维护 `.cutforge/rev` 与 `.cutforge/bases/<rev>.json` 快照;冲突即停
(CF-*),禁止「最后写入者获胜」。改片只改 IR,零渲染;性能预算 §6.2
(context ≤1.5s/12KB,apply ≤300ms/op)。

退出码:0 成功 / 2 输入错与契约违规 / 3 依赖缺失 / 4 执行受阻(锁/冲突/内部)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import (EXIT_DEP, EXIT_EXEC, EXIT_INPUT, EXIT_OK, IrError, emit,  # noqa: E402
                       ensure_utf8, load_ir, write_text_atomic)
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量
import rs_editor  # noqa: E402  — 复用内容寻址 id(cf-<sha1>)与 diff 人话引擎

EXIT_BLOCKED = EXIT_EXEC          # 锁 / 冲突 / 内部:执行受阻,盘面未变

# ---------------------------------------------------------------- 常量与真相源

CUTFORGE_DIR = ".cutforge"
OPLOG_REL = "oplog"               # .cutforge/oplog/<日>.jsonl(按天切分,append-only)
REV_REL = "rev"                   # .cutforge/rev(当前修订号,纯整数文本)
BASES_REL = "bases"               # .cutforge/bases/<rev>.json(rev 快照,M9-1)
LOCK_REL = "edit.lock"            # rs_edit 自身的写锁(编辑器的 .cutforge/lock 互不影响)
LOCK_TTL_S = 300                  # 锁超时:超龄或持锁进程已死即可 --force 接管
STILL_ACTIVE = 259                # Windows GetExitCodeProcess 的「仍在运行」码

# 素材库(v2 M12/ADR-0053,分册01):统一 manifest 与素材根(skills/cutflow/assets/)。
# 防御性读取:manifest 是 M12 的产物,缺失/坏档时相关 op 显式 DEP_MISSING,绝不猜路径。
ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
ASSETS_MANIFEST_PATH = ASSETS_DIR / "manifest.json"
# 效果目录(v2 M13/ADR-0054,分册02):catalog.json 同样 M13 才落,context 防御性读取。
EFFECTS_CATALOG_PATH = (Path(__file__).resolve().parents[1] / "templates"
                        / "effects" / "catalog.json")
# element.add/overlay.add 挂元素贴图时,未给几何的兜底显示宽度(占画宽比例;
# 元素是贴图不是全幅卡片,scale=1.0 会铺满画布 —— 取贴图惯例的 1/5)。
ELEMENT_DEFAULT_SCALE = 0.2

REQUEST_KEYS = {"baseRev", "ops", "requestId"}   # ops.json 包装对象白名单
OP_KEYS = {"op", "target", "before", "after", "reason", "source", "requestId"}
SOURCES = {"user", "agent", "inferred"}

# 值域(附录 B 通用约束 3;gainDb 统一 [-60, 0],rate 与 schema speed 同域)
RATE_RANGE = (0.25, 4.0)
GAIN_RANGE = (-60.0, 0.0)
SCALE_RANGE = (0.05, 4.0)
MOTION_IN = ["none", "fadeIn", "slideInLeft", "slideInRight", "scaleIn", "zoomIn"]
MOTION_OUT = ["none", "fadeOut", "slideOutLeft", "slideOutRight"]
# 转场:op 层 kind 名 → schema transition.type 枚举(dissolve→fade、slide→slideleft
# 是同义别名;match 不在 schema 枚举 → OP_UNSUPPORTED,U7)
TRANSITION_KIND = {
    "none": "none", "cut": "cut", "dissolve": "fade", "fade": "fade",
    "slide": "slideleft", "slideleft": "slideleft",
    "wipeleft": "wipeleft", "wipeup": "wipeup", "circleopen": "circleopen",
}
RATIOS = ["9x16", "3x4", "16x9"]
SFX_DEFAULT_DURATION_MS = 1000    # 经验值:sfx 一次性触发,schema 必填 durationMs 给占位 1s

# schema 未覆盖 → OP_UNSUPPORTED(字段级;U7 核查结论,详见 rules/edit-op.md 覆盖表)
UNSUPPORTED_FIELDS = {
    ("clip.speed", "keepPitch"): "schema 无 keepPitch 字段",
    ("clip.reframe", "anchorX"): "schema reframe 只有 anchorY,无 anchorX",
    ("bgm.set", "fadeInMs"): "schema bgm 对象无 fadeInMs",
    ("bgm.set", "fadeOutMs"): "schema bgm 对象无 fadeOutMs",
    ("audio.gain", "leadMs"): "schema 无 J-cut 字段",
    ("audio.gain", "lagMs"): "schema 无 L-cut 字段",
    ("output.set", "logos"): "schema outputs 只收比例,logo 走 S5 品牌矩阵",
    ("output.set", "profiles"): "schema 无 profiles 字段",
}
# 整支 op 未承诺(schema 完全无承载字段,U7)
UNSUPPORTED_OPS = {
    "subtitle.highlight": "schema 无高亮字段(ASS 富文本 span 待契约升版,清欠账 #12)",
    "keyframe.set": "schema 无 keyframe 字段(§9.1 U7)",
    "style.pacing": "schema 无 pacing 字段(节奏档归 M6 风格包参数源)",
}

# 脏传播(§5.3.5 映射表):dirty 类 → (范围, 最小重建建议)
DIRTY_ADVICE = {
    "S3": ("IR → S3/S8", "python skills/cutflow/scripts/rs_run.py --from S3 --force"),
    "S4": ("S4 产物", "python skills/cutflow/scripts/rs_run.py --from S4 --force"),
    "S6": ("S6/S8", "python skills/cutflow/scripts/rs_run.py --only S6 之后 --only S8"),
    "S7": ("S7 产物", "python skills/cutflow/scripts/rs_run.py --from S7 --force"),
    "S2": ("粗剪决策", "python skills/cutflow/scripts/rs_cut.py --apply(不重跑 detect)"),
    "S8": ("渲染(效果/T2)", "python skills/cutflow/scripts/rs_run.py --only S8"),
    "OUT": ("参数源", "python skills/cutflow/scripts/rs_run.py --status 后按提示"),
}
OP_DIRTY = {
    "clip.trim": "S3", "clip.move": "S3", "clip.split": "S3", "clip.delete": "S3",
    "clip.speed": "S3", "clip.reframe": "S3", "clip.motion": "S3",
    "transition.set": "S3", "freeze.set": "S3", "beat.snap": "S3",
    "fx.apply": "S3", "fx.clear": "S3",
    "overlay.add": "S4", "overlay.remove": "S4",
    "element.add": "S4", "element.remove": "S4", "element.retime": "S4",
    "sfx.add": "S6", "sfx.remove": "S6", "bgm.set": "S6", "audio.gain": "S6",
    "asset.swap": "S6",
    "subtitle.set": "S7", "subtitle.retime": "S7",
    "huazi.set": "S7", "huazi.clear": "S7",
    "font.set": "S7",
    "segment.protect": "S2", "output.set": "OUT", "effect.glsl.enable": "S8",
}
# context「可执行手法」/裁剪声明的节名
SECTION_LABELS = {"ops": "可执行手法清单", "degrade": "当前降级项", "subtitle": "字幕摘要",
                  "protect": "保护区", "overlay": "覆盖轨明细", "audio": "音频轨明细",
                  "effects": "可用特效", "fxplan": "本工程该用的特效", "assets": "可用素材"}


class EditError(Exception):
    """契约违规/受阻:code 与退出码随身,由 main 统一转 emit 协议。"""

    def __init__(self, code: str, message: str, data=None, exit_code: int = EXIT_INPUT):
        super().__init__(message)
        self.code, self.message, self.data, self.exit_code = code, message, data, exit_code


def _fail(code: str, message: str, data=None, exit_code: int = EXIT_INPUT) -> None:
    raise EditError(code, message, data, exit_code)


# ---------------------------------------------------------------- 基础工具

def dump_json(doc) -> str:
    """rs_edit 写盘唯一口径(确定性门禁:同输入字节级同输出)。"""
    return json.dumps(doc, ensure_ascii=False, indent=1) + "\n"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def snap_ms(ms: float, fps: int) -> int:
    """附录 B 约束 4:时间写入前吸附帧网格(取整到毫秒,误差 ≤0.5ms)。"""
    frames = round(float(ms) * fps / 1000.0)
    return int(round(frames * 1000.0 / fps))


def _fmt_ms(ms) -> str:
    try:
        return f"{float(ms) / 1000:.2f}s"
    except (TypeError, ValueError):
        return "?"


def _short_val(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v)
    return s if len(s) <= 24 else s[:21] + "…"


def cutlist_path(root: Path) -> Path:
    """04_粗剪决策/cutlist.json(粗剪决策真相源;segment.protect 的承载文件)。"""
    return rs_paths.resolve(root, "cut") / "cutlist.json"


def beats_path(root: Path) -> Path:
    """04_粗剪决策/beats.json(M8 混剪能力产物;缺失 → BEATS_MISSING,不伪造吸附)。"""
    return rs_paths.resolve(root, "cut") / "beats.json"


# ---------------------------------------------------------------- .cutforge 记账

def cf_dir(root: Path) -> Path:
    return root / CUTFORGE_DIR


def read_rev(root: Path) -> int:
    """当前修订号(.cutforge/rev;无账的新工程 = 0)。"""
    p = cf_dir(root) / REV_REL
    if not p.is_file():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip() or 0)
    except ValueError:
        return 0


def load_oplog(root: Path) -> list[dict]:
    """与人侧同一条 OpLog(rs_oplog.load_ops 同口径:半行截断即停,按 rev 排序)。"""
    import rs_oplog  # noqa: PLC0415 — 延迟导入,保持 context 轻量
    return rs_oplog.load_ops(root)


def _of(op: dict, name: str):
    """oplog 字段双口径读取(cutforge-io 实际序列化为蛇形,兼容驼峰)。"""
    if name in op:
        return op[name]
    return op.get("_".join(w.capitalize() if i else w
                           for i, w in enumerate(name.split("_"))))


def next_op_id(ops: list[dict]) -> str:
    """续接 op-<n> 单调分配(人机同一条日志,不得撞号)。"""
    mx = 0
    for op in ops:
        oid = str(_of(op, "op_id") or "")
        if oid.startswith("op-"):
            try:
                mx = max(mx, int(oid[3:]))
            except ValueError:
                continue
    return f"op-{mx + 1}"


def append_oplog(root: Path, entries: list[dict]) -> None:
    d = cf_dir(root) / OPLOG_REL
    d.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    with (d / f"{day}.jsonl").open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def write_rev(root: Path, rev: int) -> None:
    write_text_atomic(cf_dir(root) / REV_REL, f"{rev}\n")


def write_base_snapshot(root: Path, rev: int, doc) -> None:
    """rev 快照(cutforge 三路合并的祖先链;失败不致命,只少一个合并祖先)。"""
    try:
        d = cf_dir(root) / BASES_REL
        d.mkdir(parents=True, exist_ok=True)
        write_text_atomic(d / f"{rev}.json", dump_json(doc))
    except OSError:
        pass


# ---------------------------------------------------------------- 工程锁

def _pid_alive(pid: int) -> bool:
    """跨平台存活探测(Windows 禁用 os.kill(pid, 0) —— 它会无条件杀进程)。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes  # noqa: PLC0415
        import ctypes.wintypes  # noqa: PLC0415
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = (ctypes.wintypes.DWORD, ctypes.wintypes.BOOL,
                                    ctypes.wintypes.DWORD)
        h = k32.OpenProcess(0x1000, 0, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.wintypes.DWORD()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(root: Path, actor: str, force: bool = False) -> dict:
    """取 rs_edit 写锁(失败 LOCKED,含 pid 与过期接管提示;--force 显式接管)。"""
    d = cf_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    p = d / LOCK_REL
    me = {"pid": os.getpid(), "ts": now_iso(), "actor": actor, "tool": "rs_edit"}
    if p.is_file():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            old = {}
        opid = int(old.get("pid") or 0)
        age = 0.0
        try:
            age = datetime.now().timestamp() - p.stat().st_mtime
        except OSError:
            pass
        expired = (not _pid_alive(opid)) or age > LOCK_TTL_S
        if expired:
            if not force:
                _fail("LOCKED",
                      f"发现过期锁(pid={opid},已 {age:.0f}s)。"
                      "确认无并发写入后加 --force 接管",
                      {"lock": old}, exit_code=EXIT_BLOCKED)
        else:
            _fail("LOCKED",
                  f"工程被其他写者锁定(pid={opid},活动中)。"
                  "等它结束,或确认死进程后 --force 接管",
                  {"lock": old}, exit_code=EXIT_BLOCKED)
    write_text_atomic(p, json.dumps(me, ensure_ascii=False))
    return me


def release_lock(root: Path) -> None:
    try:
        (cf_dir(root) / LOCK_REL).unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- 目标解析

def iter_clips(doc):
    """yield (track 下标, track, clip 下标, clip)。"""
    for ti, t in enumerate(doc.get("tracks", [])):
        for ci, c in enumerate(t.get("clips", [])):
            yield ti, t, ci, c


def resolve_clip(doc, clip_id: str):
    """稳定 clipId → (track, clip)。原生 id 优先;无原生 id 用内容寻址回退
    (复用 rs_editor.content_id 的 cf-<sha1> 机制,P22-1)。禁止下标。"""
    if "[" in clip_id or "]" in clip_id:
        _fail("BAD_ADDRESS",
              f"禁止数组下标寻址:{clip_id!r};用稳定 clipId(context 视图第一列)")
    hits = [(t, c) for _, t, _, c in iter_clips(doc)
            if c.get("id") and str(c["id"]) == clip_id]
    if not hits:
        hits = [(t, c) for _, t, _, c in iter_clips(doc)
                if not c.get("id") and rs_editor.content_id(c) == clip_id]
    if not hits:
        _fail("NOT_FOUND", f"clipId {clip_id!r} 不存在于当前 IR(工程改过了?)"
                           "——重跑 rs_edit.py context 取新视图")
    if len(hits) > 1:
        _fail("BAD_ADDRESS", f"clipId {clip_id!r} 命中多个片段(锚点必须唯一)")
    return hits[0]


def clip_kind_label(track: dict, ti: int) -> str:
    """人话轨名:主轨(首个 video)→「主轨 V1」,其余 video→「覆盖轨 V2」,A/T 类推。"""
    if track.get("id"):
        base = str(track["id"])
    else:
        letter = {"video": "V", "audio": "A", "text": "T"}.get(track.get("kind"), "X")
        base = f"{letter}{ti + 1}"
    kind = track.get("kind")
    if kind == "audio":
        return f"音频轨 {base}"
    if kind == "text":
        return f"字幕轨 {base}"
    is_main = ti == 0 or track.get("name") == "main"
    return f"主轨 {base}" if is_main else f"覆盖轨 {base}"


def clip_desc(clip: dict) -> str:
    t = str(clip.get("text") or "")
    return (t[:12] + "…") if len(t) > 12 else t


def track_clip_pos(doc, track: dict, clip: dict) -> tuple[int, int]:
    """身份(对象同一性)→ 当前下标(结构变更后仍指同一对象,防下标漂移)。"""
    for ti, t, ci, c in iter_clips(doc):
        if t is track and c is clip:
            return ti, ci
    _fail("NOT_FOUND", "目标片段已在本次批次中被删除,后续 op 无法寻址")


def _identity_index(clips: list, item) -> int:
    return next(i for i, c in enumerate(clips) if c is item)


# ---------------------------------------------------------------- JSON Pointer(undo/冲突用)

def ptr_split(ptr: str) -> list[str]:
    return [p for p in ptr.split("/") if p != ""]


def ptr_get(doc, ptr: str):
    cur = doc
    for part in ptr_split(ptr):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            raise KeyError(ptr)
    return cur


def ptr_set(doc, ptr: str, val) -> None:
    parts = ptr_split(ptr)
    cur = doc
    for part in parts[:-1]:
        cur = cur[int(part)] if isinstance(cur, list) else cur.setdefault(part, {})
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = val
    else:
        cur[last] = val


def ptr_del(doc, ptr: str) -> None:
    parts = ptr_split(ptr)
    cur = doc
    for part in parts[:-1]:
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    last = parts[-1]
    if isinstance(cur, list):
        cur.pop(int(last))
    else:
        cur.pop(last, None)


def ptr_overlap(a: str, b: str) -> bool:
    """两个 JSON Pointer 是否指进同一子树(前缀相含即算,数组父指针也覆盖)。"""
    pa, pb = ptr_split(a), ptr_split(b)
    n = min(len(pa), len(pb))
    return pa[:n] == pb[:n]


def ptr_join_safe(parts: list[str]) -> str:
    """指针片段 → 标准指针串(undo 剪枝用;片段不含 ~0/~1 转义,工程键皆安全)。"""
    return "/" + "/".join(parts) if parts else ""


# ---------------------------------------------------------------- EditOp 静态契约
# OP_AFTER 是 after 白名单与值域的唯一机械真相:ops-validate 与 apply 的处理器
# 共用同一张表(不落两份口径),表外字段一律 BAD_FIELD,U7 字段一律 OP_UNSUPPORTED。

def _v_num(lo=None, hi=None):
    def chk(v, where):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            _fail("BAD_VALUE", f"{where} 须为数字,得到 {v!r}")
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            _fail("BAD_VALUE", f"{where}={v} 超出值域 [{lo}, {hi}]")
        return v
    return chk


def _v_ms(lo=0.0):
    """毫秒时间字段:值域 [lo, ∞),apply 时再吸附帧网格。"""
    return _v_num(lo, None)


def _v_enum(*values):
    def chk(v, where):
        if v not in values:
            _fail("BAD_VALUE", f"{where} ∈ {'|'.join(values)},得到 {v!r}")
        return v
    return chk


def _v_bool(v, where):
    if not isinstance(v, bool):
        _fail("BAD_VALUE", f"{where} 须为布尔,得到 {v!r}")
    return v


def _v_str(v, where):
    s = str(v or "").strip()
    if not s:
        _fail("BAD_VALUE", f"{where} 必填且非空")
    return s


def _v_kind(v, where):
    _v_str(v, where)
    if str(v) == "match":
        _fail("OP_UNSUPPORTED", f"{where}=match:schema 枚举未含,本轮不承诺,见 CONTEXT.md U7")
    if str(v) not in TRANSITION_KIND:
        _fail("BAD_VALUE", f"{where} ∈ {'|'.join(TRANSITION_KIND)},得到 {v!r}")
    return v


def _v_ratios(v, where):
    if not isinstance(v, list) or not v:
        _fail("BAD_VALUE", f"{where} 须为非空数组(比例清单)")
    bad = [r for r in v if r not in RATIOS]
    if bad:
        _fail("BAD_VALUE", f"{where} ∈ {'|'.join(RATIOS)},越界:{bad}")
    return list(dict.fromkeys(v))       # 去重保序(确定性)


def _v_obj(v, where):
    if not isinstance(v, dict):
        _fail("BAD_VALUE", f"{where} 须为对象")
    return v


def _v_fx_id(v, where):
    """fxId(v2 分册04 §4):非空字符串;注册表校验归渲染端/M13,这里只把住类型关。"""
    s = _v_str(v, where)
    if len(s) > 64 or any(ch in s for ch in " \t\r\n"):
        _fail("BAD_VALUE", f"{where} 须为紧凑 fxId(≤64 字符,不含空白),得到 {v!r}")
    return s


def _v_slot(v, where):
    return _v_enum("in", "out", "combo")(v, where)


def _v_huazi(v, where):
    """huazi 挂载对象:{template 必填, params 可选}(schema clip.huazi 同构)。"""
    _v_obj(v, where)
    unknown = sorted(set(v) - {"template", "params"})
    if unknown:
        _fail("BAD_FIELD", f"{where} 有契约外字段 {unknown}(合法:['params', 'template'])")
    if "template" not in v:
        _fail("BAD_VALUE", f"{where}.template 必填(huazi.* 模板 id)")
    _v_fx_id(v["template"], f"{where}.template")
    if "params" in v:
        _v_obj(v["params"], f"{where}.params")
    return v


def _v_element_motion(v, where):
    """element.add 的 motion{fx}:目前只承载 fx 一键(几何动画归渲染端)。"""
    _v_obj(v, where)
    unknown = sorted(set(v) - {"fx"})
    if unknown:
        _fail("BAD_FIELD", f"{where} 有契约外字段 {unknown}(合法:['fx'])")
    _v_fx_id(v["fx"], f"{where}.fx")
    return v


def _v_asset_id(v, where):
    """素材 id(分册01 命名法 <kind>.<组>.<序号>):这里只把住类型关;存在性与
    commercial 校验在 apply 时对着 manifest.json 做(缺失 → DEP_MISSING)。"""
    s = _v_str(v, where)
    if len(s) > 96:
        _fail("BAD_VALUE", f"{where} 过长(≤96):{s[:24]}…")
    return s


OP_AFTER: dict[str, dict[str, object]] = {
    "clip.trim": {"startMs": _v_ms(), "durationMs": _v_ms(0.5), "sourceInMs": _v_ms()},
    "clip.move": {"startMs": _v_ms(), "ripple": _v_bool},
    "clip.split": {"atMs": _v_ms(0.5)},
    "clip.speed": {"rate": _v_num(*RATE_RANGE)},
    "clip.reframe": {"anchorY": _v_num(0, 1), "scale": _v_num(*SCALE_RANGE)},
    "clip.motion": {"in": _v_enum(*MOTION_IN), "inMs": _v_ms(),
                    "out": _v_enum(*MOTION_OUT), "outMs": _v_ms(),
                    "inFx": _v_fx_id, "outFx": _v_fx_id},       # v2 扩展:fxId(分册04 §4.1)
    "transition.set": {"kind": _v_kind, "durMs": _v_ms(0.5), "fx": _v_fx_id},
    "overlay.add": {"card": _v_str, "element": _v_asset_id,
                    "startMs": _v_ms(), "durationMs": _v_ms(0.5),
                    "motion": _v_obj},
    "sfx.add": {"name": _v_str, "gainDb": _v_num(*GAIN_RANGE),
                "assetId": _v_asset_id},                          # v2 扩展:素材 id 取代裸 name
    "bgm.set": {"src": _v_str, "gainDb": _v_num(*GAIN_RANGE), "ducking": _v_bool,
                "assetId": _v_asset_id},                          # v2 扩展:按素材 id 选曲
    "audio.gain": {"gainDb": _v_num(*GAIN_RANGE)},
    "freeze.set": {"freezeMs": _v_ms(1)},
    "subtitle.set": {"text": _v_str, "huazi": _v_huazi},          # v2 扩展:字幕挂花字
    "subtitle.retime": {"startMs": _v_ms(), "endMs": _v_ms(0.5)},
    "segment.protect": {"startMs": _v_ms(), "endMs": _v_ms(0.5), "note": _v_str},
    "note.add": {"text": _v_str, "author": _v_enum("user", "agent")},
    "output.set": {"ratios": _v_ratios},
    "beat.snap": {"windowMs": _v_num(1, 500)},
    # ---- v2 M14 新增 op(分册04 §4.2;schema 契约已双仓同步)----
    "fx.apply": {"slot": _v_slot, "fx": _v_fx_id, "params": _v_obj},
    "fx.clear": {"slot": _v_slot},
    "element.add": {"element": _v_asset_id, "startMs": _v_ms(), "durationMs": _v_ms(0.5),
                    "x": _v_num(0, 1), "y": _v_num(0, 1),
                    "w": _v_num(1, None), "h": _v_num(1, None),
                    "opacity": _v_num(0, 1), "motion": _v_element_motion},
    "element.remove": {},
    "element.retime": {"startMs": _v_ms(), "durationMs": _v_ms(0.5)},
    "huazi.set": {"template": _v_fx_id, "params": _v_obj},
    "huazi.clear": {},
    "font.set": {"family": _v_str, "scope": _v_enum("project", "clip", "style")},
    "asset.swap": {"assetId": _v_asset_id},
    "effect.glsl.enable": {"on": _v_bool},
}
# op → 无 after(值放别处/纯删除/纯清除)
NO_AFTER_OPS = {"clip.delete", "overlay.remove", "sfx.remove",
                "element.remove", "huazi.clear"}


def check_op_shape(op, idx: int) -> None:
    """op 名 / 键集 / target / reason / source 的静态契约(ops-validate 也走这里)。"""
    where = f"ops[{idx}]"
    if not isinstance(op, dict):
        _fail("BAD_OP", f"{where} 不是对象")
    unknown = sorted(set(op) - OP_KEYS)
    if unknown:
        _fail("BAD_OP", f"{where} 有契约外字段 {unknown}(合法:{sorted(OP_KEYS)};"
                        "P8:静默吞字段 = 幽灵键)")
    name = op.get("op")
    if not isinstance(name, str) or (name not in OP_AFTER and name not in NO_AFTER_OPS
                                     and name not in UNSUPPORTED_OPS):
        _fail("BAD_OP", f"{where}.op 未知:{name!r}(见 rules/edit-op.md 34 op 表)")
    target = op.get("target")
    if not isinstance(target, str) or not target.strip():
        _fail("BAD_ADDRESS", f"{where}.target 须为非空字符串(稳定引用)")
    if "[" in target:
        _fail("BAD_ADDRESS", f"{where}.target 禁止下标寻址:{target!r}")
    if not str(op.get("reason") or "").strip():
        _fail("BAD_VALUE",
              f"{where}.reason 必填(人话意图摘要,进 OpLog 供 rs_oplog report 审计)")
    source = op.get("source") or "agent"
    if source not in SOURCES:
        _fail("BAD_VALUE", f"{where}.source ∈ user|agent|inferred,得到 {source!r}")
    after = op.get("after")
    if after is not None and not isinstance(after, dict):
        _fail("BAD_VALUE", f"{where}.after 须为对象")


def check_unsupported(name: str, after: dict) -> None:
    """U7 诚实条款:schema 未覆盖的 op / 字段显式拒绝,绝不静默写 schema 外字段。"""
    if name in UNSUPPORTED_OPS:
        _fail("OP_UNSUPPORTED",
              f"{name}:schema 未含承载字段,本轮不承诺,见 CONTEXT.md U7({UNSUPPORTED_OPS[name]})")
    for key in sorted(after):
        hit = UNSUPPORTED_FIELDS.get((name, key))
        if hit:
            _fail("OP_UNSUPPORTED",
                  f"{name}.{key}:schema 未含该字段,本轮不承诺,见 CONTEXT.md U7({hit})")


def validate_after(op: dict, idx: int) -> dict:
    """after 白名单 + 值域(OP_AFTER 单一来源);返回 规范化键值(帧吸附在 apply 做)。

    ops-validate 无 IR 也走这里——白名单/值域/枚举不需要工程上下文;
    目标存在性、adjacency、素材存在性等 IO/IR 依赖检查仍归 apply。
    """
    name, where = op["op"], f"ops[{idx}]"
    if name in NO_AFTER_OPS:
        after = dict(op.get("after") or {})
        if after:
            _fail("BAD_FIELD", f"{name} 不接受 after 字段")
        return {}
    spec = OP_AFTER.get(name) or {}
    after = dict(op.get("after") or {})
    unknown = sorted(set(after) - set(spec))
    if unknown:
        _fail("BAD_FIELD", f"{where}({name}).after 有白名单外字段 {unknown}"
                           f"(合法:{sorted(spec)};schema 外字段见 U7 → OP_UNSUPPORTED)")
    out = {}
    for k in sorted(set(after) & set(spec)):
        out[k] = spec[k](after[k], f"{where}({name}).{k}")
    return out


# ---------------------------------------------------------------- apply 内存态

class Ctx:
    """一次 apply 批次的内存态与账本(冲突即停:任何落盘都发生在全量校验之后)。

    pending_entries 是处理器产出的待写 Op 行,全部校验通过后才编号成正式
    oplog 条目并落盘。
    """

    def __init__(self, root: Path, doc, fps: int, actor: str, base_rev: int,
                 request_id: str | None):
        self.root = root
        self.doc = doc
        self.fps = fps
        self.actor = actor                     # "user" | "agent"(OpLog actor.kind)
        self.base_rev = base_rev
        self.request_id = request_id
        self.entries: list[dict] = []          # 正式 OpLog 行(落盘前暂存)
        self.pending_entries: list[dict] = []  # {kind,file,before,after,ptrs,human,reason,rid}
        self.human: list[str] = []             # 人话差异表
        self.dirty: set[str] = set()           # 脏传播类
        self.warnings: list[str] = []
        self.file_docs: dict[str, dict | None] = {}  # 文件级 op 的其他真相源

    def file_doc(self, name: str) -> dict | None:
        """惰性加载 notes.json / cutlist.json(文件级 op 的真相源,绝不写进 IR)。"""
        if name not in self.file_docs:
            p = (self.root / "notes.json") if name == "notes.json" else cutlist_path(self.root)
            if not p.is_file():
                self.file_docs[name] = None
            else:
                try:
                    self.file_docs[name] = json.loads(p.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                    _fail("INTERNAL", f"{name} 解析失败:{exc}", exit_code=EXIT_BLOCKED)
        return self.file_docs[name]

    def push_pending(self, *, kind: str, file: str, before, after, ptrs: list[str],
                     human: str, reason: str, rid) -> None:
        self.pending_entries.append({
            "kind": kind, "file": file, "before": before, "after": after,
            "ptrs": ptrs, "human": human, "reason": reason, "rid": rid,
        })
        self.human.append(human)


def _clip_ptr(ti: int, ci: int, field: str = "") -> str:
    return f"/tracks/{ti}/clips/{ci}" + (f"/{field}" if field else "")


def set_fields(ctx: Ctx, op: dict, holder: dict, base_ptr: str,
               plan: list[tuple[str, object]], label: str) -> None:
    """把 [(字段, 新值)] 写进 holder(clip/bgm/transition 等),回填 before、判幂等、
    产 pending Op 行。无变化 = 幂等短路(不产 Op、不写盘)。"""
    rid = op.get("requestId") or ctx.request_id
    reason = str(op.get("reason"))
    changes: list[tuple[str, object, object]] = []
    for field, new in plan:
        old = holder.get(field)
        if old == new and not (old is None and new is not None):
            continue                      # after == before:幂等短路
        changes.append((field, old, new))
    if not changes:
        return
    for field, _, new in changes:
        holder[field] = new
    before_map = {f"{base_ptr}/{f}": old for f, old, _ in changes}
    after_map = {f"{base_ptr}/{f}": new for f, _, new in changes}
    human = f"{label}: " + "、".join(
        f"{f} {('缺' if old is None else _short_val(old))} → {_short_val(new)}"
        for f, old, new in changes)
    ctx.push_pending(kind="set", file="project.json", before=before_map,
                     after=after_map, ptrs=sorted(before_map), human=human,
                     reason=reason, rid=rid)


# ---------------------------------------------------------------- 各 op 处理器
# 约定:处理器先静态校验(validate_after),再做 IR/IO 依赖检查,变更内存 doc,
# 登记 pending entry。结构类 op 的 before/after 是子树;undo 按同一指针逆写。

# (op, after 键) → (目标箱, schema 字段):op 层参数名到 IR 字段的唯一映射
CLIP_FIELD_MAP = {
    ("clip.trim", "startMs"): ("clip", "startMs"),
    ("clip.trim", "durationMs"): ("clip", "durationMs"),
    ("clip.trim", "sourceInMs"): ("clip", "sourceInMs"),
    ("clip.move", "startMs"): ("clip", "startMs"),
    ("clip.speed", "rate"): ("clip", "speed"),
    ("clip.reframe", "anchorY"): ("reframe", "anchorY"),
    ("clip.reframe", "scale"): ("clip", "scale"),
    ("clip.motion", "in"): ("motion", "in"),
    ("clip.motion", "inMs"): ("motion", "inMs"),
    ("clip.motion", "out"): ("motion", "out"),
    ("clip.motion", "outMs"): ("motion", "outMs"),
    ("clip.motion", "inFx"): ("motion", "inFx"),
    ("clip.motion", "outFx"): ("motion", "outFx"),
    ("freeze.set", "freezeMs"): ("clip", "freezeMs"),
    ("audio.gain", "gainDb"): ("clip", "volume"),
}


def op_simple_clip_fields(ctx: Ctx, op: dict, idx: int) -> None:
    """clip.trim / clip.move / clip.speed / clip.reframe / clip.motion / freeze.set /
    audio.gain:白名单字段经 CLIP_FIELD_MAP 写 schema 字段(gainDb→volume 等
    映射见 rules/edit-op.md 覆盖表)。时间字段吸附帧网格。"""
    name = op["op"]
    norm = validate_after(op, idx)
    track, clip = resolve_clip(ctx.doc, op["target"])
    ti, ci = track_clip_pos(ctx.doc, track, clip)
    label = f"{clip_kind_label(track, ti)} · {op['target']}「{clip_desc(clip)}」"
    boxes: dict[str, list[tuple[str, object]]] = {"clip": [], "motion": [], "reframe": []}
    for key, val in norm.items():
        box, field = CLIP_FIELD_MAP[(name, key)]
        if isinstance(val, (int, float)) and not isinstance(val, bool) \
                and key.endswith("Ms"):
            val = snap_ms(val, ctx.fps)        # 附录 B 约束 4:帧网格
        boxes[box].append((field, val))
    if name == "clip.move" and norm.get("ripple"):
        _move_ripple(ctx, op, track, clip, norm["startMs"])
    # 容器只在该 op 真要写它时才创建(setdefault 提前会留 "motion": {} 幽灵键)
    if boxes["motion"]:
        set_fields(ctx, op, clip.setdefault("motion", {}),
                   _clip_ptr(ti, ci, "motion"), boxes["motion"], label)
    if boxes["reframe"]:
        set_fields(ctx, op, clip.setdefault("reframe", {}),
                   _clip_ptr(ti, ci, "reframe"), boxes["reframe"], label)
    if boxes["clip"]:
        set_fields(ctx, op, clip, _clip_ptr(ti, ci), boxes["clip"], label)
    ctx.dirty.add(OP_DIRTY[name])


def _move_ripple(ctx: Ctx, op: dict, track: dict, clip: dict, new_start) -> None:
    """clip.move 的 ripple:同轨后续片段整体平移同一差量(附录 B clip.move 行)。"""
    delta = snap_ms(new_start, ctx.fps) - (clip.get("startMs") or 0)
    if delta == 0:
        return
    _, ci = track_clip_pos(ctx.doc, track, clip)
    clips = track.get("clips", [])
    for j in range(ci + 1, len(clips)):
        c = clips[j]
        if c.get("startMs") is None:
            continue
        shifted = max(0, snap_ms((c["startMs"] or 0) + delta, ctx.fps))
        if shifted != c["startMs"]:
            ti, _ = track_clip_pos(ctx.doc, track, c)
            set_fields(ctx, op, c, _clip_ptr(ti, j), [("startMs", shifted)],
                       f"{clip_kind_label(track, ti)} · 连带重排「{clip_desc(c)}」")


def op_transition_set(ctx: Ctx, op: dict, idx: int) -> None:
    """transition.set:target「clipA|clipB」,转场挂在后一片段(与前一片段的 xfade)。
    v2 扩展(分册04 §4.1/§3.3):after.fx 直写 transition.fx(fxId);kind 与 fx 并存时
    两者都落契约、渲染端以 fx 为准并 WARN —— 这里只留痕,不静默丢任一字段。"""
    norm = validate_after(op, idx)
    parts = op["target"].split("|")
    if len(parts) != 2:
        _fail("BAD_ADDRESS", "transition.set.target 须为「clipA|clipB」(相邻两段)")
    ta, ca = resolve_clip(ctx.doc, parts[0].strip())
    tb, cb = resolve_clip(ctx.doc, parts[1].strip())
    if ta is not tb:
        _fail("BAD_ADDRESS", "转场只定义在同轨相邻片段之间")
    clips = tb.get("clips", [])
    ia, ib = _identity_index(clips, ca), _identity_index(clips, cb)
    if abs(ia - ib) != 1:
        _fail("BAD_ADDRESS", f"{parts[0]} 与 {parts[1]} 不相邻(中间还有别的段)")
    later, later_i = (cb, ib) if ib > ia else (ca, ia)
    if not norm:
        _fail("BAD_VALUE", "transition.set.after 需要 kind / durMs / fx 至少一个")
    plan = []
    if "kind" in norm:
        plan.append(("type", TRANSITION_KIND[str(norm["kind"])]))
    if "durMs" in norm:
        plan.append(("durMs", snap_ms(norm["durMs"], ctx.fps)))
    if "fx" in norm:
        plan.append(("fx", norm["fx"]))
        if "kind" in norm:
            ctx.warnings.append(
                f"transition.set:{parts[0]}→{parts[1]} 同时给了 kind 与 fx,"
                "渲染端以 fx 为准(分册04 §3.3),kind 仅作回退档保留")
    ti, _ = track_clip_pos(ctx.doc, tb, later)
    box = later.setdefault("transition", {})
    label = f"转场 {parts[0]} → {parts[1]}({_fmt_ms(later.get('startMs'))} 起)"
    set_fields(ctx, op, box, _clip_ptr(ti, later_i, "transition"), plan, label)
    ctx.dirty.add("S3")


def op_clip_split(ctx: Ctx, op: dict, idx: int) -> None:
    """clip.split:atMs 落帧网格;左段承袭原片段(含原生 id),右段按内容最小集派生。"""
    norm = validate_after(op, idx)
    if "atMs" not in norm:
        _fail("BAD_VALUE", "clip.split.after.atMs 必填(切在哪)")
    at = snap_ms(norm["atMs"], ctx.fps)
    track, clip = resolve_clip(ctx.doc, op["target"])
    ti, ci = track_clip_pos(ctx.doc, track, clip)
    start, dur = clip.get("startMs") or 0, clip.get("durationMs") or 0
    if not (start < at < start + dur):
        _fail("BAD_VALUE", f"atMs={at} 不在片段 {op['target']} 的时间窗内"
                           f"({_fmt_ms(start)}–{_fmt_ms(start + dur)})")
    right = {k: v for k, v in clip.items() if k in ("src", "speed", "volume", "role", "scale")}
    right["startMs"] = at
    right["durationMs"] = start + dur - at
    if clip.get("sourceInMs") is not None:
        right["sourceInMs"] = (clip["sourceInMs"] or 0) + (at - start)
    left = dict(clip)
    left["durationMs"] = at - start
    clips = track["clips"]
    clips[ci:ci + 1] = [left, right]
    label = (f"{clip_kind_label(track, ti)} · {op['target']}「{clip_desc(clip)}」"
             f" 在 {_fmt_ms(at)} 切成两段")
    ctx.push_pending(kind="split", file="project.json", before=clip,
                     after={"left": left, "right": right}, ptrs=[_clip_ptr(ti, ci)],
                     human=label, reason=str(op.get("reason")),
                     rid=op.get("requestId") or ctx.request_id)
    ctx.dirty.add("S3")


def op_clip_delete(ctx: Ctx, op: dict, idx: int) -> None:
    validate_after(op, idx)
    track, clip = resolve_clip(ctx.doc, op["target"])
    ti, ci = track_clip_pos(ctx.doc, track, clip)
    track["clips"].pop(ci)
    label = (f"{clip_kind_label(track, ti)} · 删除 {op['target']}"
             f"「{clip_desc(clip)}」(原 {_fmt_ms(clip.get('startMs'))}–"
             f"{_fmt_ms((clip.get('startMs') or 0) + (clip.get('durationMs') or 0))})")
    ctx.push_pending(kind="delete", file="project.json", before=clip, after=None,
                     ptrs=[_clip_ptr(ti, ci)], human=label,
                     reason=str(op.get("reason")),
                     rid=op.get("requestId") or ctx.request_id)
    ctx.dirty.add(OP_DIRTY.get(op["op"], "S3"))


def op_overlay_add(ctx: Ctx, op: dict, idx: int) -> None:
    """overlay.add:挂卡片(artboard 桥语义)或元素贴图(v2 扩展,after.element;
    与 card 互斥)。元素走素材库 manifest 解析,复用与 element.add 相同的 clip 组装。"""
    import rs_ir  # noqa: PLC0415 — 复用白名单与 manifest 解析(P8 单一来源)
    norm = validate_after(op, idx)
    if "card" in norm and "element" in norm:
        _fail("BAD_VALUE", "overlay.add 的 card(artboard 卡)与 element(素材库元素)互斥,"
                           "一次只挂一种")
    if not ({"startMs", "durationMs"} <= set(norm)):
        _fail("BAD_VALUE", "overlay.add.after 需要 startMs / durationMs"
                           "(card 或 element 二选一)")
    if "element" in norm:
        entry = _manifest_asset(norm["element"], kind="element")
        clip = _element_clip({**norm, "_src": entry["_src"], "_id": entry["id"]})
        clip["startMs"] = snap_ms(clip["startMs"], ctx.fps)
        clip["durationMs"] = snap_ms(clip["durationMs"], ctx.fps)
        if isinstance(norm.get("motion"), dict) and not norm["motion"].get("fx"):
            _fail("BAD_VALUE", "overlay.add.motion 元素路径只承载 {fx}(几何动画归渲染端)")
        tracks = ctx.doc.setdefault("tracks", [])
        ov = next((t for t in tracks if t.get("kind") == "video"
                   and t.get("name") == "overlay"), None)
        if ov is None:
            ov = {"kind": "video", "name": "overlay", "clips": []}
            tracks.append(ov)
        clips = ov.setdefault("clips", [])
        clips.append(clip)
        clips.sort(key=lambda c: (c.get("startMs") or 0))
        ti, ci = track_clip_pos(ctx.doc, ov, clip)
        label = (f"覆盖轨 · 挂元素 {entry['id']} @ {_fmt_ms(clip['startMs'])}"
                 f"(源 {Path(clip['src']).name})")
        ctx.push_pending(kind="insert", file="project.json", before=None, after=clip,
                         ptrs=[_clip_ptr(ti, ci)], human=label,
                         reason=str(op.get("reason")),
                         rid=op.get("requestId") or ctx.request_id)
        ctx.dirty.add("S4")
        return
    if "card" not in norm:
        _fail("BAD_VALUE", "overlay.add.after 需要 card / element 二选一"
                           "(+ startMs / durationMs)")
    manifest_p = rs_paths.resolve(ctx.root, "assets") / "artboard" / "manifest.json"
    if not manifest_p.is_file():
        _fail("DEP_MISSING",
              f"缺 artboard manifest:{manifest_p}(先跑 rs_artboard --scan / gen-cards)",
              exit_code=EXIT_DEP)
    manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
    plan = [{"card": norm["card"], "startMs": snap_ms(norm["startMs"], ctx.fps),
             "durationMs": snap_ms(norm["durationMs"], ctx.fps),
             "motion": norm.get("motion") or {}}]
    srcs, issues = rs_ir.resolve_card_srcs(plan, manifest, ctx.root, manifest_p.parent)
    if issues:
        _fail("DEP_MISSING", f"卡片 {norm['card']} 不可挂:{';'.join(issues)}",
              exit_code=EXIT_DEP)
    pairs, issues2 = rs_ir.build_overlay_track(plan, srcs)
    if issues2:
        _fail("BAD_VALUE", ";".join(issues2))
    _, new_clip = pairs[0]
    tracks = ctx.doc.setdefault("tracks", [])
    ov = next((t for t in tracks if t.get("kind") == "video"
               and t.get("name") == "overlay"), None)
    if ov is None:
        ov = {"kind": "video", "name": "overlay", "clips": []}
        tracks.append(ov)
    clips = ov.setdefault("clips", [])
    clips.append(new_clip)
    clips.sort(key=lambda c: (c.get("startMs") or 0))
    ti, ci = track_clip_pos(ctx.doc, ov, new_clip)
    label = f"覆盖轨 · 挂卡片 {norm['card']} @ {_fmt_ms(new_clip['startMs'])}(源 {new_clip['src']})"
    ctx.push_pending(kind="insert", file="project.json", before=None, after=new_clip,
                     ptrs=[_clip_ptr(ti, ci)], human=label,
                     reason=str(op.get("reason")),
                     rid=op.get("requestId") or ctx.request_id)
    ctx.dirty.add("S4")


def op_overlay_remove(ctx: Ctx, op: dict, idx: int) -> None:
    op_clip_delete(ctx, op, idx)
    ctx.dirty.add("S4")


def op_sfx_add(ctx: Ctx, op: dict, idx: int) -> None:
    """sfx.add:target「t<毫秒>」锚点。单点挂载;「≤2 个/15s」密度闸由 rs_sfx --auto
    统一裁决(口径见 rules/sfx.md),rs_edit 不重复实现。
    v2 扩展(分册04 §4.1):after.assetId 直指素材库 manifest id(取代裸 name;
    name 作为别名兼容 —— 两者同时给出以 assetId 为准并 WARN)。"""
    norm = validate_after(op, idx)
    tstr = op["target"].strip()
    if not tstr.startswith("t") or not tstr[1:].isdigit():
        _fail("BAD_ADDRESS", "sfx.add.target 须为时间锚点「t<毫秒>」,如 t12345")
    asset_id, name = norm.get("assetId"), norm.get("name")
    if asset_id:
        if name:
            ctx.warnings.append(
                f"sfx.add:name({name})与 assetId({asset_id})同时给出,"
                "以 assetId 为准(分册04 §4.1),name 仅作兼容别名")
        entry = _manifest_asset(asset_id, kind="sfx")
        src = entry["_src"]
    else:
        if not name:
            _fail("BAD_VALUE", "sfx.add.after 需要 assetId(v2 口径)或 name(兼容别名)")
        if "/" in name or "\\" in name or name.endswith(".mp3"):
            src = name
            if not (ctx.root / src).is_file():
                _fail("BAD_VALUE", f"音效素材不存在:{src}")
        else:
            src = f"assets_sfx:{name}"    # 内置音效库伪协议(rs_ir/rs_sfx 同口径)
            if not (Path(__file__).resolve().parents[3] / "assets" / "sfx" / f"{name}.mp3").is_file():
                _fail("BAD_VALUE", f"内置音效库无此名:{name}")
    audio = next((t for t in ctx.doc.get("tracks", []) if t.get("kind") == "audio"), None)
    if audio is None:
        audio = {"kind": "audio", "name": "audio", "clips": []}
        ctx.doc.setdefault("tracks", []).append(audio)
    clip = {"src": src, "startMs": snap_ms(int(tstr[1:]), ctx.fps),
            "durationMs": SFX_DEFAULT_DURATION_MS, "role": "sfx"}
    if asset_id:
        clip["assetId"] = asset_id        # 溯源与 asset.swap 的换素材键(渲染由 src 驱动)
    if "gainDb" in norm:   # 映射:schema 增益字段是线性 volume(0–2),10^(dB/20)
        clip["volume"] = round(10 ** (norm["gainDb"] / 20.0), 6)
    if asset_id and isinstance(entry.get("durationMs"), (int, float)) \
            and entry["durationMs"] > 0:
        clip["durationMs"] = snap_ms(int(entry["durationMs"]), ctx.fps)  # manifest 实测时长
    clips = audio.setdefault("clips", [])
    clips.append(clip)
    clips.sort(key=lambda c: (c.get("startMs") or 0))
    ti, ci = track_clip_pos(ctx.doc, audio, clip)
    label = f"音效 {src if not asset_id else asset_id} 落点 {_fmt_ms(clip['startMs'])}"
    ctx.push_pending(kind="insert", file="project.json", before=None, after=clip,
                     ptrs=[_clip_ptr(ti, ci)], human=label,
                     reason=str(op.get("reason")),
                     rid=op.get("requestId") or ctx.request_id)
    ctx.dirty.add("S6")


def op_sfx_remove(ctx: Ctx, op: dict, idx: int) -> None:
    track, clip = resolve_clip(ctx.doc, op["target"])
    if track.get("kind") != "audio" or clip.get("role") != "sfx":
        _fail("BAD_ADDRESS", f"{op['target']} 不是音效段(role=sfx 的音频片段)")
    op_clip_delete(ctx, op, idx)
    ctx.dirty.add("S6")


def op_bgm_set(ctx: Ctx, op: dict, idx: int) -> None:
    if op["target"] != "bgm":
        _fail("BAD_ADDRESS", "bgm.set.target 固定为「bgm」(顶层配乐对象)")
    norm = validate_after(op, idx)
    if not norm:
        _fail("BAD_VALUE", "bgm.set.after 至少要有一个字段(src/gainDb/ducking/assetId)")
    plan = []
    entry = None
    if "assetId" in norm and "src" in norm:
        ctx.warnings.append("bgm.set:src 与 assetId 同时给出,以 assetId 为准"
                            "(分册04 §4.3),src 仅作兼容别名")
    if "assetId" in norm:
        entry = _manifest_asset(norm["assetId"], kind="bgm")
        plan.append(("assetId", entry["id"]))
        plan.append(("src", entry["_src"]))       # 渲染由 src 驱动;assetId 是溯源/换素材键
    elif "src" in norm:
        src = norm["src"]
        if not (ctx.root / src).is_file():
            _fail("BAD_VALUE", f"配乐素材不存在:{src}(相对工程根)")
        plan.append(("src", src))
    if "gainDb" in norm:
        plan.append(("gainDb", norm["gainDb"]))
    if "ducking" in norm:
        plan.append(("ducking", norm["ducking"]))
    bgm = ctx.doc.setdefault("bgm", {})
    set_fields(ctx, op, bgm, "/bgm", plan, "BGM")
    ctx.dirty.add("S6")


def op_subtitle_set(ctx: Ctx, op: dict, idx: int) -> None:
    """subtitle.set:改文本(Hard Rule 8:时间从 wordline 重建,S7 重建时生效);
    v2 扩展(分册04 §4.1):after.huazi 给该字幕挂花字模板(schema clip.huazi)。"""
    norm = validate_after(op, idx)
    if "text" not in norm and "huazi" not in norm:
        _fail("BAD_VALUE", "subtitle.set.after 需要 text / huazi 至少一个")
    track, clip = resolve_clip(ctx.doc, op["target"])
    if track.get("kind") != "text":
        _fail("BAD_ADDRESS", f"{op['target']} 不在字幕轨(text 轨);"
                             "字幕真相源在 wordline/ASS,改卡文本请确认工程含 text 轨")
    ti, ci = track_clip_pos(ctx.doc, track, clip)
    plan = []
    if "text" in norm:
        plan.append(("text", norm["text"]))
    if "huazi" in norm:
        plan.append(("huazi", norm["huazi"]))
    label = f"{clip_kind_label(track, ti)} · {op['target']} 字幕"
    set_fields(ctx, op, clip, _clip_ptr(ti, ci), plan, label)
    ctx.dirty.add("S7")


def op_subtitle_retime(ctx: Ctx, op: dict, idx: int) -> None:
    """subtitle.retime:startMs/endMs → 字幕卡的 startMs/durationMs(endMs−startMs)。
    release-margin(起点≤首字、终点≥末字)由 S7/S8 重建链把守,此处只做值域与帧网格。"""
    norm = validate_after(op, idx)
    if not norm:
        _fail("BAD_VALUE", "subtitle.retime.after 需要 startMs / endMs 至少一个")
    track, clip = resolve_clip(ctx.doc, op["target"])
    if track.get("kind") != "text":
        _fail("BAD_ADDRESS", f"{op['target']} 不在字幕轨(text 轨)")
    ti, ci = track_clip_pos(ctx.doc, track, clip)
    start = clip.get("startMs") or 0
    new_start, new_end = start, start + (clip.get("durationMs") or 0)
    if "startMs" in norm:
        new_start = snap_ms(norm["startMs"], ctx.fps)
    if "endMs" in norm:
        new_end = snap_ms(norm["endMs"], ctx.fps)
    if new_end <= new_start:
        _fail("BAD_VALUE", f"字幕卡时长须为正:end({new_end}) ≤ start({new_start})")
    label = (f"{clip_kind_label(track, ti)} · {op['target']} 字幕时间 "
             f"{_fmt_ms(start)}–{_fmt_ms(start + (clip.get('durationMs') or 0))} → "
             f"{_fmt_ms(new_start)}–{_fmt_ms(new_end)}")
    set_fields(ctx, op, clip, _clip_ptr(ti, ci),
               [("startMs", new_start), ("durationMs", new_end - new_start)], label)
    ctx.dirty.add("S7")


def op_segment_protect(ctx: Ctx, op: dict, idx: int) -> None:
    """segment.protect(文件级 op):写 04_粗剪决策/cutlist.json 的 protect 区,
    绝不进 project.json;guard 第四条(rs_cut)在下次粗剪 apply 时把守切点。"""
    norm = validate_after(op, idx)
    if op["target"] != "protect" and not op["target"].startswith("t"):
        _fail("BAD_ADDRESS", "segment.protect.target 固定为「protect」(区间值放 after)")
    if "startMs" not in norm or "endMs" not in norm:
        _fail("BAD_VALUE", "segment.protect.after 需要 startMs 与 endMs")
    a = snap_ms(norm["startMs"], ctx.fps)
    b = snap_ms(norm["endMs"], ctx.fps)
    if a < 0 or b <= a:
        _fail("BAD_VALUE", f"protect 区间非法(需 0 ≤ start < end):{a}-{b}")
    zone = {"startMs": a, "endMs": b}
    if norm.get("note"):
        zone["note"] = norm["note"]
    doc = ctx.file_doc("cutlist.json")
    if doc is None:
        _fail("DEP_MISSING", "缺 04_粗剪决策/cutlist.json(segment.protect 的真相源;"
                             "先跑 S2 粗剪)", exit_code=EXIT_DEP)
    protect = doc.setdefault("protect", [])
    for z in protect:
        if isinstance(z, dict) and max(a, z.get("startMs", 0)) < min(b, z.get("endMs", 0)):
            ctx.warnings.append(
                f"新保护区 {a}-{b} 与既有区 {z.get('startMs')}-{z.get('endMs')} 重叠(允许,注意裁决)")
    protect.append(zone)
    label = (f"保护区 {_fmt_ms(a)}–{_fmt_ms(b)}"
             + (f"「{zone['note']}」" if zone.get("note") else "") + " → cutlist.json")
    ctx.push_pending(kind="insert", file="cutlist.json", before=None, after=zone,
                     ptrs=[f"/protect/{len(protect) - 1}"], human=label,
                     reason=str(op.get("reason")),
                     rid=op.get("requestId") or ctx.request_id)
    ctx.dirty.add("S2")


def op_note_add(ctx: Ctx, op: dict, idx: int) -> None:
    """note.add(文件级 op):写 notes.json(cutforge notes schema 同源),
    绝不进 project.json;id 按 n-<序号> 单调分配。"""
    norm = validate_after(op, idx)
    if "text" not in norm:
        _fail("BAD_VALUE", "note.add.after.text 必填")
    author = norm.get("author") or ("user" if ctx.actor == "user" else "agent")
    anchor = _parse_note_anchor(ctx, op["target"])
    doc = ctx.file_doc("notes.json")
    if doc is None:
        doc = {"version": 1, "items": []}
        ctx.file_docs["notes.json"] = doc
    items = doc.setdefault("items", [])
    mx = 0
    for it in items:
        nid = str(it.get("id") or "")
        if nid.startswith("n-"):
            try:
                mx = max(mx, int(nid[2:]))
            except ValueError:
                continue
    item = {"id": f"n-{mx + 1:04d}", "anchor": anchor, "body": norm["text"],
            "author": author, "state": "open", "createdAt": now_iso()}
    items.append(item)
    label = f"标注 {item['id']} @ {_anchor_label(anchor)}「{norm['text'][:20]}」→ notes.json"
    ctx.push_pending(kind="insert", file="notes.json", before=None, after=item,
                     ptrs=[f"/items/{len(items) - 1}"], human=label,
                     reason=str(op.get("reason")),
                     rid=op.get("requestId") or ctx.request_id)


def _parse_note_anchor(ctx: Ctx, target: str) -> dict:
    """note.add target 文法:「t<毫秒>」|「<clipId>」|「<clipId>@<毫秒>」
    → cutforge notes anchor {kind, ref, tMs}(禁下标;五类枚举取 time/clip/subtitleCard)。"""
    t = target.strip()
    if t.startswith("t") and t[1:].isdigit():
        return {"kind": "time", "ref": None, "tMs": snap_ms(int(t[1:]), ctx.fps)}
    cid, _, tail = t.partition("@")
    track, clip = resolve_clip(ctx.doc, cid)
    tms = snap_ms(int(tail), ctx.fps) if tail.isdigit() else (clip.get("startMs") or 0)
    kind = "subtitleCard" if track.get("kind") == "text" else "clip"
    return {"kind": kind, "ref": cid, "tMs": tms}


def _anchor_label(anchor: dict) -> str:
    return f"t={anchor.get('tMs')}" if anchor.get("kind") == "time" \
        else f"{anchor.get('ref')}@{anchor.get('tMs')}"


def op_output_set(ctx: Ctx, op: dict, idx: int) -> None:
    if op["target"] != "output":
        _fail("BAD_ADDRESS", "output.set.target 固定为「output」")
    norm = validate_after(op, idx)
    if not norm:
        _fail("BAD_VALUE", "output.set.after 至少要有一个字段(ratios)")
    seen = norm["ratios"]
    old = ctx.doc.get("outputs")
    ctx.doc["outputs"] = seen
    label = f"输出比例 {(old if old is not None else '缺')} → {seen}"
    ctx.push_pending(kind="set", file="project.json",
                     before={"/outputs": old}, after={"/outputs": seen},
                     ptrs=["/outputs"], human=label, reason=str(op.get("reason")),
                     rid=op.get("requestId") or ctx.request_id)
    ctx.dirty.add("OUT")


def op_beat_snap(ctx: Ctx, op: dict, idx: int) -> None:
    """beat.snap:读 04_粗剪决策/beats.json(M8 产物);缺失 → BEATS_MISSING(不伪造)。
    吸附不到(超窗)不动 + WARN,禁止强制吸附。"""
    norm = validate_after(op, idx)
    window = norm.get("windowMs", 60)
    p = beats_path(ctx.root)
    if not p.is_file():
        _fail("BEATS_MISSING",
              f"缺 {rs_paths.rel(ctx.root, 'cut', 'beats.json')}"
              "(beat.snap 依赖混剪能力的节拍产物,先跑混剪;不伪造吸附)",
              exit_code=EXIT_DEP)
    try:
        beats_doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        _fail("INTERNAL", f"beats.json 解析失败:{exc}", exit_code=EXIT_BLOCKED)
    beats = beats_doc.get("beats", beats_doc.get("beatsMs"))
    if not isinstance(beats, list) or not beats:
        _fail("INTERNAL", "beats.json 无 beats/beatsMs 数组(M8 产物口径)",
              exit_code=EXIT_BLOCKED)
    try:
        beats = [float(b) for b in beats]
    except (TypeError, ValueError) as exc:
        _fail("INTERNAL", f"beats.json 数组含非数字项:{exc}", exit_code=EXIT_BLOCKED)
    track, clip = resolve_clip(ctx.doc, op["target"])
    ti, ci = track_clip_pos(ctx.doc, track, clip)
    start = clip.get("startMs") or 0
    near = min(beats, key=lambda b: abs(b - start))
    delta = near - start
    label = f"{clip_kind_label(track, ti)} · {op['target']} 卡点吸附"
    if abs(delta) > window:
        ctx.warnings.append(
            f"{label}:最近节拍 {near:g} 距 {_fmt_ms(start)} 为 {delta:+.0f}ms,"
            f"超出吸附窗 {window:g}ms —— 不动(禁强制吸附)")
        return
    new_start = snap_ms(near, ctx.fps)
    if new_start == start:
        return
    set_fields(ctx, op, clip, _clip_ptr(ti, ci), [("startMs", new_start)], label)
    ctx.dirty.add("S3")


def _manifest_asset(asset_id: str, kind: str | None = None) -> dict:
    """素材库 manifest.json 按 id 查条目(防御性,分册01/ADR-0053)。

    manifest 缺失/坏档 → DEP_MISSING(M12 素材库未部署,显式提示,不猜路径);
    id 不存在 / kind 不符 / commercial=false → BAD_VALUE(不可商用素材不得入轨)。
    """
    if not ASSETS_MANIFEST_PATH.is_file():
        _fail("DEP_MISSING",
              f"缺素材库清单:{ASSETS_MANIFEST_PATH}(M12 素材库未部署?"
              "先跑 rs_asset.py check 生成 manifest.json)", exit_code=EXIT_DEP)
    try:
        manifest = json.loads(ASSETS_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        _fail("INTERNAL", f"manifest.json 解析失败:{exc}", exit_code=EXIT_BLOCKED)
    for e in manifest.get("assets") or []:
        if isinstance(e, dict) and e.get("id") == asset_id:
            if kind is not None and e.get("kind") != kind:
                _fail("BAD_VALUE", f"素材 {asset_id} 的 kind 是 {e.get('kind')},"
                                   f"此处需要 {kind}")
            if e.get("commercial") is False:
                _fail("BAD_VALUE", f"素材 {asset_id} 标记为不可商用(commercial=false),"
                                   "不得入轨;换可商用的素材")
            file = str(e.get("file") or "")
            src = ASSETS_DIR / file
            if not file or not src.is_file():
                _fail("BAD_VALUE", f"素材 {asset_id} 的文件缺失:{file}(manifest 与目录不同步;"
                                   "重跑 rs_asset.py check)")
            return {**e, "_src": str(src)}
    _fail("BAD_VALUE", f"素材库无此 id:{asset_id}(查 rs_asset.py list;"
                       "id 一经发布不改名,注意别手打错)")


def _element_clip(norm: dict) -> dict:
    """element.add / overlay.add(element) 共用的元素贴图 clip 组装(时间字段由
    调用方统一吸附帧网格)。

    几何语义(人话映射「加个箭头指向那里 --x 0.6 --y 0.3」):x/y 是归一化中心点
    → clip.position;w/h(像素)给出时 → clip.overlay 绝对落点(以 x/y 为中心折算);
    都不给 → scale 兜底(ELEMENT_DEFAULT_SCALE),绝不整幅铺满。
    """
    clip: dict = {"src": norm["_src"], "assetId": norm["_id"],
                  "startMs": float(norm["startMs"]), "durationMs": float(norm["durationMs"])}
    if "w" in norm:
        ov: dict = {"w": int(round(float(norm["w"])))}
        if "h" in norm:
            ov["h"] = int(round(float(norm["h"])))
        clip["overlay"] = ov
    else:
        clip["scale"] = ELEMENT_DEFAULT_SCALE
    if "x" in norm or "y" in norm:
        clip["position"] = {"x": float(norm.get("x", 0.5)), "y": float(norm.get("y", 0.5))}
    if "opacity" in norm:
        clip["opacity"] = float(norm["opacity"])
    if isinstance(norm.get("motion"), dict) and norm["motion"].get("fx"):
        clip["motion"] = {"inFx": norm["motion"]["fx"]}    # 元素入场 fx(M13 注册表消费)
    return clip


def _overlay_track_for(ctx: Ctx, target: str):
    """element.add 的 target 解析:覆盖轨 trackId(如 V2)或「overlay」(自动建)。
    主轨(main/首个 video 轨)不允许挂元素 —— 元素是贴图层,压主轨属于寻址错。"""
    tracks = ctx.doc.setdefault("tracks", [])
    if target == "overlay":
        ov = next((t for t in tracks if t.get("kind") == "video"
                   and t.get("name") == "overlay"), None)
        if ov is None:
            ov = {"kind": "video", "name": "overlay", "clips": []}
            tracks.append(ov)
        return ov
    hits = [t for t in tracks if t.get("kind") == "video"
            and str(t.get("id") or "") == target]
    if not hits:
        _fail("NOT_FOUND", f"trackId {target!r} 不存在(用 context 视图的轨 id;"
                           "或 target=overlay 自动建覆盖轨)")
    ov = hits[0]
    main_i = next((i for i, t in enumerate(tracks)
                   if t.get("kind") == "video"), -1)
    if tracks.index(ov) == main_i or ov.get("name") == "main":
        _fail("BAD_ADDRESS", f"{target} 是主视频轨:元素贴图必须挂覆盖轨"
                             "(target=overlay 自动建,或指到既有覆盖轨 id)")
    return ov


def op_element_add(ctx: Ctx, op: dict, idx: int) -> None:
    """element.add(v2 分册04 §4.2):按素材 id 挂元素贴图(显式几何,比 overlay.add 细)。
    素材经 manifest.json 解析(缺失 → DEP_MISSING);几何见 _element_clip。"""
    norm = validate_after(op, idx)
    for req in ("element", "startMs", "durationMs"):
        if req not in norm:
            _fail("BAD_VALUE", f"element.add.after.{req} 必填")
    entry = _manifest_asset(norm["element"], kind="element")
    ov = _overlay_track_for(ctx, op["target"])
    clip = _element_clip({**norm, "_src": entry["_src"], "_id": entry["id"]})
    for timed in ("startMs", "durationMs"):
        clip[timed] = snap_ms(clip[timed], ctx.fps)      # 附录 B 约束 4:帧网格
    clips = ov.setdefault("clips", [])
    clips.append(clip)
    clips.sort(key=lambda c: (c.get("startMs") or 0))
    ti, ci = track_clip_pos(ctx.doc, ov, clip)
    label = (f"覆盖轨 · 挂元素 {entry['id']} @ {_fmt_ms(clip['startMs'])}"
             f"(源 {Path(clip['src']).name})")
    ctx.push_pending(kind="insert", file="project.json", before=None, after=clip,
                     ptrs=[_clip_ptr(ti, ci)], human=label,
                     reason=str(op.get("reason")),
                     rid=op.get("requestId") or ctx.request_id)
    ctx.dirty.add("S4")


def _element_clip_guard(ctx: Ctx, op: dict):
    """element.remove / element.retime 的目标守卫:必须是元素段(assetId 且在覆盖轨)。"""
    track, clip = resolve_clip(ctx.doc, op["target"])
    if not clip.get("assetId"):
        _fail("BAD_ADDRESS", f"{op['target']} 不是素材元素段(缺 assetId);"
                             "普通片段用 clip.trim,卡片用 overlay.remove")
    return track, clip


def op_element_remove(ctx: Ctx, op: dict, idx: int) -> None:
    validate_after(op, idx)
    _element_clip_guard(ctx, op)
    op_clip_delete(ctx, op, idx)
    ctx.dirty.add("S4")


def op_element_retime(ctx: Ctx, op: dict, idx: int) -> None:
    """element.retime(v2):调元素时间;受帧网格约束,时长零漂移由「必须给正时长」把守。"""
    norm = validate_after(op, idx)
    if not norm:
        _fail("BAD_VALUE", "element.retime.after 需要 startMs / durationMs 至少一个")
    track, clip = _element_clip_guard(ctx, op)
    ti, ci = track_clip_pos(ctx.doc, track, clip)
    plan = []
    if "startMs" in norm:
        plan.append(("startMs", snap_ms(norm["startMs"], ctx.fps)))
    if "durationMs" in norm:
        plan.append(("durationMs", snap_ms(norm["durationMs"], ctx.fps)))
    label = (f"{clip_kind_label(track, ti)} · 元素 {clip.get('assetId')} 重设时间")
    set_fields(ctx, op, clip, _clip_ptr(ti, ci), plan, label)
    ctx.dirty.add("S4")


def op_huazi_set(ctx: Ctx, op: dict, idx: int) -> None:
    """huazi.set(v2 分册04 §4.2/ADR-0057):换/挂花字模板(schema clip.huazi;
    S7 rs_subtitle --huazi 消费,渲染端零新增通道)。"""
    norm = validate_after(op, idx)
    if "template" not in norm:
        _fail("BAD_VALUE", "huazi.set.after.template 必填(huazi.* 模板 id)")
    track, clip = resolve_clip(ctx.doc, op["target"])
    if track.get("kind") != "text":
        _fail("BAD_ADDRESS", f"{op['target']} 不在字幕轨(text 轨);"
                             "花字挂在字幕卡上,先确认目标 clipId")
    huazi: dict = {"template": norm["template"]}
    if "params" in norm:
        huazi["params"] = norm["params"]
    ti, ci = track_clip_pos(ctx.doc, track, clip)
    label = f"{clip_kind_label(track, ti)} · 花字 {norm['template']}"
    set_fields(ctx, op, clip, _clip_ptr(ti, ci), [("huazi", huazi)], label)
    ctx.dirty.add("S7")


def op_huazi_clear(ctx: Ctx, op: dict, idx: int) -> None:
    """huazi.clear(v2):去花字回普通 ASS;原本就没挂 → 幂等短路。"""
    validate_after(op, idx)
    track, clip = resolve_clip(ctx.doc, op["target"])
    ti, ci = track_clip_pos(ctx.doc, track, clip)
    old = clip.pop("huazi", None)
    if old is None:
        return                                    # after == before:幂等短路
    ptr = _clip_ptr(ti, ci, "huazi")
    label = f"{clip_kind_label(track, ti)} · 去花字(回普通 ASS)"
    ctx.push_pending(kind="set", file="project.json", before={ptr: old},
                     after={ptr: None}, ptrs=[ptr], human=label,
                     reason=str(op.get("reason")),
                     rid=op.get("requestId") or ctx.request_id)
    ctx.dirty.add("S7")


def op_font_set(ctx: Ctx, op: dict, idx: int) -> None:
    """font.set(v2 分册04 §4.2):字体切换(字体唯一真相源在 artboard fonts/)。
    target=project → 顶层 font.family(全链标脏 OUT);target=clipId → clip.font.family。
    scope=style 归 S7 字幕样式契约,project.json 无承载 → OP_UNSUPPORTED(U7)。"""
    norm = validate_after(op, idx)
    if "family" not in norm:
        _fail("BAD_VALUE", "font.set.after.family 必填(artboard 字体族名)")
    scope = norm.get("scope")
    target = op["target"].strip()
    if scope == "style":
        _fail("OP_UNSUPPORTED",
              "font.set.scope=style:project.json 无字幕样式容器(样式归 S7 rs_subtitle"
              " 契约),本轮不承诺,见 CONTEXT.md U7 与 rules/edit-op.md 覆盖表")
    if target == "project":
        if scope == "clip":
            _fail("BAD_VALUE", "target=project 与 scope=clip 矛盾;"
                               "clip 范围请把 target 指到 clipId")
        if (ctx.doc.get("font") or {}).get("family") == norm["family"]:
            return                                # after == before:幂等短路(不建容器)
        holder, ptr, label_holder = ctx.doc, "/font", "工程字体"
        dirty = "OUT"
    else:
        if scope == "project":
            _fail("BAD_VALUE", "scope=project 请用 target=project;"
                               "片段级字体请省略 scope 或用 scope=clip")
        track, clip = resolve_clip(ctx.doc, target)
        if (clip.get("font") or {}).get("family") == norm["family"]:
            return                                # after == before:幂等短路
        ti, ci = track_clip_pos(ctx.doc, track, clip)
        holder, ptr = clip, _clip_ptr(ti, ci, "font")
        label_holder = f"{clip_kind_label(track, ti)} · {target} 字体"
        dirty = "S7"
    set_fields(ctx, op, holder.setdefault("font", {}), ptr,
               [("family", norm["family"])], label_holder)
    ctx.dirty.add(dirty)


def op_asset_swap(ctx: Ctx, op: dict, idx: int) -> None:
    """asset.swap(v2 分册04 §4.2):换素材,保留时间与参数(只换 src/assetId)。
    target=clipId(音效段)或 bgm;kind 按被换对象判定,manifest 校验 kind 一致。"""
    norm = validate_after(op, idx)
    if "assetId" not in norm:
        _fail("BAD_VALUE", "asset.swap.after.assetId 必填")
    if op["target"].strip() == "bgm":
        bgm = ctx.doc.get("bgm") or {}
        if not bgm.get("src"):
            _fail("BAD_VALUE", "工程还没有 BGM(bgm.src 缺失);先 bgm.set 再换")
        entry = _manifest_asset(norm["assetId"], kind="bgm")
        plan = [("src", entry["_src"]), ("assetId", entry["id"])]
        set_fields(ctx, op, bgm, "/bgm", plan, f"BGM 换素材 → {entry['id']}")
        ctx.dirty.add("S6")
        return
    track, clip = resolve_clip(ctx.doc, op["target"])
    if track.get("kind") != "audio":
        _fail("BAD_ADDRESS", "asset.swap 只支持音效段(音频轨 clip)或 target=bgm;"
                             "视频/字幕素材请重新组卡")
    entry = _manifest_asset(norm["assetId"], kind="sfx")
    ti, ci = track_clip_pos(ctx.doc, track, clip)
    plan = [("src", entry["_src"]), ("assetId", entry["id"])]
    label = f"音效换素材 {clip.get('assetId') or clip.get('src', '')} → {entry['id']}"
    set_fields(ctx, op, clip, _clip_ptr(ti, ci), plan, label)
    ctx.dirty.add("S6")


def op_fx_apply(ctx: Ctx, op: dict, idx: int) -> None:
    """fx.apply(v2 分册04 §4.2):给单 clip 挂特效(slot in|out|combo;比 clip.motion
    通用,覆盖组合特效)。写 schema clip.fx[slot]={fx, params};M13 注册表消费,
    未注册 fxId 渲染端降级 WARN + fxDegraded 留痕(零静默)。"""
    norm = validate_after(op, idx)
    for req in ("slot", "fx"):
        if req not in norm:
            _fail("BAD_VALUE", f"fx.apply.after.{req} 必填")
    track, clip = resolve_clip(ctx.doc, op["target"])
    ti, ci = track_clip_pos(ctx.doc, track, clip)
    entry: dict = {"fx": norm["fx"]}
    if "params" in norm:
        entry["params"] = norm["params"]
    slot = str(norm["slot"])
    if (clip.get("fx") or {}).get(slot) == entry:
        return                                    # after == before:幂等短路(不建容器)
    fx_box = clip.setdefault("fx", {})
    label = f"{clip_kind_label(track, ti)} · 特效[{norm['slot']}] {norm['fx']}"
    set_fields(ctx, op, fx_box, _clip_ptr(ti, ci, "fx"), [(slot, entry)], label)
    ctx.dirty.add("S3")


def op_fx_clear(ctx: Ctx, op: dict, idx: int) -> None:
    """fx.clear(v2):去特效(slot 必填);原本就没挂 → 幂等短路;容器清空即摘除。"""
    norm = validate_after(op, idx)
    if "slot" not in norm:
        _fail("BAD_VALUE", "fx.clear.after.slot 必填(in|out|combo)")
    track, clip = resolve_clip(ctx.doc, op["target"])
    ti, ci = track_clip_pos(ctx.doc, track, clip)
    fx_box = clip.get("fx") or {}
    slot = str(norm["slot"])
    if slot not in fx_box:
        return                                    # after == before:幂等短路
    old = fx_box.pop(slot)
    if not fx_box:
        clip.pop("fx", None)                      # 空容器摘除(不留幽灵键)
    ptr = f"{_clip_ptr(ti, ci, 'fx')}/{slot}"
    label = f"{clip_kind_label(track, ti)} · 去特效[{slot}]"
    ctx.push_pending(kind="set", file="project.json", before={ptr: old},
                     after={ptr: None}, ptrs=[ptr], human=label,
                     reason=str(op.get("reason")),
                     rid=op.get("requestId") or ctx.request_id)
    ctx.dirty.add("S3")


def op_effect_glsl_enable(ctx: Ctx, op: dict, idx: int) -> None:
    """effect.glsl.enable(v2 分册04 §4.2):显式开关 T2 GLSL 渲染(schema 顶层
    effects.glsl;false → 全部 T2 效果降级最接近的 T1 近似,渲染端留痕)。"""
    norm = validate_after(op, idx)
    if op["target"].strip() != "project":
        _fail("BAD_ADDRESS", "effect.glsl.enable.target 固定为「project」")
    if "on" not in norm:
        _fail("BAD_VALUE", "effect.glsl.enable.after.on 必填(布尔)")
    if (ctx.doc.get("effects") or {}).get("glsl") == norm["on"]:
        return                                    # after == before:幂等短路(不建容器)
    set_fields(ctx, op, ctx.doc.setdefault("effects", {}), "/effects",
               [("glsl", norm["on"])],
               f"T2 GLSL 渲染 {'开启' if norm['on'] else '关闭(降级 T1 近似)'}")
    ctx.dirty.add("S8")


HANDLERS = {
    "clip.trim": op_simple_clip_fields,
    "clip.move": op_simple_clip_fields,
    "clip.speed": op_simple_clip_fields,
    "clip.reframe": op_simple_clip_fields,
    "clip.motion": op_simple_clip_fields,
    "freeze.set": op_simple_clip_fields,
    "audio.gain": op_simple_clip_fields,
    "transition.set": op_transition_set,
    "clip.split": op_clip_split,
    "clip.delete": op_clip_delete,
    "overlay.add": op_overlay_add,
    "overlay.remove": op_overlay_remove,
    "sfx.add": op_sfx_add,
    "sfx.remove": op_sfx_remove,
    "bgm.set": op_bgm_set,
    "subtitle.set": op_subtitle_set,
    "subtitle.retime": op_subtitle_retime,
    "segment.protect": op_segment_protect,
    "note.add": op_note_add,
    "output.set": op_output_set,
    "beat.snap": op_beat_snap,
    # ---- v2 M14 新增 op(分册04 §4.2)----
    "fx.apply": op_fx_apply,
    "fx.clear": op_fx_clear,
    "element.add": op_element_add,
    "element.remove": op_element_remove,
    "element.retime": op_element_retime,
    "huazi.set": op_huazi_set,
    "huazi.clear": op_huazi_clear,
    "font.set": op_font_set,
    "asset.swap": op_asset_swap,
    "effect.glsl.enable": op_effect_glsl_enable,
}


# ---------------------------------------------------------------- ops.json 读取

def load_ops_doc(path: Path) -> tuple[int | None, list, str | None, bool]:
    """ops.json → (baseRev, ops, requestId, wrapped)。裸数组也可以(但 apply 必须带 baseRev)。"""
    if not path.is_file():
        _fail("PRECONDITION_FAILED", f"ops 文件不存在:{path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        _fail("PRECONDITION_FAILED", f"ops.json 解析失败:{exc}")
    if isinstance(raw, list):
        return None, raw, None, False
    if isinstance(raw, dict):
        unknown = sorted(set(raw) - REQUEST_KEYS)
        if unknown:
            _fail("BAD_OP", f"ops.json 有契约外字段 {unknown}(合法:{sorted(REQUEST_KEYS)})")
        ops = raw.get("ops")
        if not isinstance(ops, list):
            _fail("BAD_OP", 'ops.json 须为 {"baseRev", "ops": [...]} 包装对象或裸数组')
        base = raw.get("baseRev")
        if base is not None and (isinstance(base, bool) or not isinstance(base, int)):
            _fail("BAD_VALUE", "baseRev 须为整数(context 视图头部给出)")
        return base, ops, raw.get("requestId"), True
    _fail("BAD_OP", "ops.json 须为数组或包装对象")


# ---------------------------------------------------------------- 冲突检测

def check_conflicts(root: Path, base_rev: int, pending_ptrs: dict[str, list[str]],
                    request_id: str | None) -> list[dict]:
    """与人侧 OpLog 比对:窗口 = rev > baseRev 的既有 Op(任何 actor;人先动即算)。
    指针前缀相含即冲突;删除相撞升 CF-002。同 requestId 的回声不算(幂等重放)。"""
    conflicts: list[dict] = []
    for op in load_oplog(root):
        rev = op.get("rev") or 0
        if rev <= base_rev:
            continue
        rid = _of(op, "request_id")
        if request_id and rid and rid == request_id:
            continue
        target = op.get("target") or {}
        file = str(target.get("file") or "project.json")
        path = str(target.get("path") or "/")
        for mine_file, mine_ptrs in pending_ptrs.items():
            if mine_file != file:
                continue
            for mp in mine_ptrs:
                if ptr_overlap(mp, path):
                    kind = str(_of(op, "op_kind") or "set")
                    conflicts.append({
                        "code": "CF-002" if kind == "delete" else "CF-001",
                        "file": file, "theirs": path, "mine": mp, "rev": rev,
                        "opId": str(_of(op, "op_id") or "?"),
                        "summary": str(op.get("summary") or ""),
                    })
                    break
    return conflicts


# ---------------------------------------------------------------- OpLog 行构造与逆写

def _build_entry(ctx: Ctx, pending: dict, rev: int, op_id: str) -> dict:
    """pending → 与 cutforge-io 同口径的 OpLog 行(蛇形键,能被其 serde 直接反序列化
    ——oplog.schema.json 写的驼峰与 Rust 结构体漂移,宁从实现,断键会触发其半行
    恢复逻辑整文件弃读)。"""
    entry = {
        "op_id": op_id,
        "ts": now_iso(),
        "actor": {"kind": ctx.actor,
                  "id": "user" if ctx.actor == "user" else "cutflow-rs_edit"},
        "target": {"file": pending["file"],
                   "path": pending["ptrs"][0] if pending["ptrs"] else "/"},
        "op_kind": pending["kind"],
        "before": pending["before"],
        "after": pending["after"],
        "base_rev": f"rev-{ctx.base_rev}",
        "rev": rev,
        "summary": pending["reason"],
    }
    if pending.get("rid"):
        entry["request_id"] = str(pending["rid"])
    return entry


def _ptr_map(v) -> dict | None:
    """before/after 是否为「指针 → 值」映射(rs_edit 的 set 行形态)。"""
    if isinstance(v, dict) and v and all(str(k).startswith("/") for k in v):
        return v
    return None


# undo 删叶字段后,这些 schema 可选容器若被清空则一并摘除(免留 "motion": {} 幽灵键)
PRUNABLE_CONTAINERS = {"motion", "reframe", "transition", "overlay", "fade",
                       "punchIn", "position", "fx", "huazi", "font", "effects"}


def _invert(entry_op: dict, docs: dict[str, dict | None]) -> list[str]:
    """把一条既有 Op 按其 before/after 逆写在对应文档上,返回人话行。"""
    target = entry_op.get("target") or {}
    file = str(target.get("file") or "project.json")
    path = str(target.get("path") or "/")
    kind = str(_of(entry_op, "op_kind") or "set")
    doc = docs.get(file)
    if doc is None:
        return []
    before, after = entry_op.get("before"), entry_op.get("after")
    human = str(entry_op.get("summary") or path)

    def _apply_map(m: dict) -> None:
        for ptr, old in m.items():
            if old is None:
                parts = ptr_split(ptr)
                try:
                    ptr_del(doc, ptr)
                except (KeyError, ValueError, IndexError):
                    continue
                # 叶子删掉后,清空的可选容器一并摘除(还原「原本没有该对象」的盘面)
                if len(parts) >= 2 and parts[-2] in PRUNABLE_CONTAINERS:
                    parent_ptr = ptr_join_safe(parts[:-1])
                    try:
                        parent = ptr_get(doc, parent_ptr)
                        if isinstance(parent, dict) and not parent:
                            ptr_del(doc, parent_ptr)
                    except (KeyError, ValueError, IndexError):
                        pass
            else:
                ptr_set(doc, ptr, old)

    try:
        if kind in ("set", "undo", "redo"):
            m = _ptr_map(before)
            if m is not None:
                _apply_map(m)
            else:                      # cutforge 子树形态:整块回写
                ptr_set(doc, path, before)
        elif kind == "insert":
            try:
                ptr_del(doc, path)
            except (KeyError, ValueError, IndexError):
                pass
        elif kind == "delete":
            ptr_set(doc, path, before)
        elif kind == "split":
            # 路径 /tracks/i/clips/j:两段合回原片段(值比对防错位)
            parent = ptr_get(doc, path.rsplit("/", 1)[0])
            j = int(path.rsplit("/", 1)[1])
            pair = after or {}
            if isinstance(parent, list) and j + 1 < len(parent) \
                    and (parent[j], parent[j + 1]) == (pair.get("left"), pair.get("right")):
                parent[j:j + 2] = [before]
        else:
            _fail("INTERNAL",
                  f"OpLog 含 {kind} 类 Op(op_id={_of(entry_op, 'op_id')}),"
                  "本轮 undo 不逆写该形态", exit_code=EXIT_BLOCKED)
    except EditError:
        raise
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        _fail("INTERNAL", f"逆写失败({file}{path}):{exc}", exit_code=EXIT_BLOCKED)
    return [f"[undo] {file}{path} — {human}"]


def _dirty_advice(dirty: set[str]) -> list[dict]:
    return [{"scope": DIRTY_ADVICE[k][0], "cmd": DIRTY_ADVICE[k][1]}
            for k in sorted(dirty)]


# ---------------------------------------------------------------- apply / undo / diff

def _load_ir(root: Path) -> dict:
    """统一 IR 载入(rs_common.load_ir,R09/R41):路径定位 + 解析 + version 校验
    + 迁移引导;IrError → EditError(退出码与 code 语义保持本脚本口径)。"""
    try:
        return load_ir(root)
    except IrError as exc:
        _fail(exc.code, exc.message, exc.data, exit_code=exc.exit_code)


def _write_side_file(root: Path, name: str, doc) -> None:
    """文件级 op 的真相源落盘(notes.json 在工程根;cutlist 在 04_粗剪决策)。"""
    p = (root / "notes.json") if name == "notes.json" else cutlist_path(root)
    write_text_atomic(p, dump_json(doc))


def cmd_apply(root: Path, ops_path: Path, dry_run: bool, actor: str,
              base_rev_arg: int | None, force: bool) -> int:
    """七步(§5.3.4):校验前置 → 锁 → baseRev 前置 → 逐 op 校验+回填 before(幂等短路)
    → 冲突即停 → 原子写 IR+OpLog+升 rev → 脏传播建议。--dry-run 只出人话差异表。"""
    # 1. 校验前置(工程存在、IR 可解析、fps 可读)
    doc = _load_ir(root)
    fps = doc.get("fps")
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        return emit(False, "PRECONDITION_FAILED",
                    f"IR 缺合法 fps(帧网格无从吸附):{rs_paths.project_json(root)}",
                    exit_code=EXIT_INPUT)
    base_rev, ops, request_id, wrapped = load_ops_doc(ops_path)
    if base_rev is None:
        base_rev = base_rev_arg
    if base_rev is None:
        return emit(False, "PRECONDITION_FAILED",
                    "缺少 baseRev:ops.json 用 {\"baseRev\": N, \"ops\": [...]} 包装"
                    "(N 取 context 视图头部的 rev),或加 --base-rev N。"
                    "baseRev 前置校验是防「最后写入者获胜」的硬闸,不可跳过",
                    exit_code=EXIT_INPUT)
    if base_rev_arg is not None and wrapped and base_rev_arg != base_rev:
        return emit(False, "PRECONDITION_FAILED",
                    f"--base-rev {base_rev_arg} 与 ops.json baseRev {base_rev} 不一致",
                    exit_code=EXIT_INPUT)
    if not ops:
        return emit(True, "IDEMPOTENT", "空 ops:无事可做", {"applied": 0}, exit_code=EXIT_OK)

    # 2. 工程锁
    try:
        acquire_lock(root, actor, force)
    except EditError as exc:
        return emit(False, exc.code, exc.message, exc.data, exit_code=exc.exit_code)
    try:
        rev = read_rev(root)
        # rev N 快照(apply 前盘面):diff/undo/cutforge 三路合并的祖先链必须能取到
        # 「你基于的那个 rev」;写在全量校验之前也无妨——它如实记录 rev N 的盘面。
        write_base_snapshot(root, rev, doc)
        ctx = Ctx(root, doc, fps, "user" if actor == "user" else "agent",
                  base_rev, request_id)

        # 3/4. 逐 op 校验 + 回填 before + 幂等判(全内存,不落盘)
        noop_count = 0
        for i, op in enumerate(ops):
            check_op_shape(op, i)
            name = op["op"]
            check_unsupported(name, dict(op.get("after") or {}))
            n0 = len(ctx.pending_entries)
            HANDLERS[name](ctx, op, i)
            if len(ctx.pending_entries) == n0:
                noop_count += 1          # after == before:幂等短路,不产 Op

        # 5. 冲突检测(与人侧 OpLog 比对;先于落盘,命中即停)
        pending_ptrs: dict[str, list[str]] = {}
        for p in ctx.pending_entries:
            pending_ptrs.setdefault(p["file"], []).extend(p["ptrs"])
        conflicts = check_conflicts(root, base_rev, pending_ptrs, request_id) \
            if pending_ptrs else []
        if conflicts:
            codes = sorted({c["code"] for c in conflicts})
            return emit(False, codes[0],
                        f"检测到 {len(conflicts)} 处冲突({','.join(codes)}),已停止写入,"
                        "禁止最后写入者获胜:重跑 rs_edit.py context 看人侧最新改动后再裁决",
                        {"conflicts": conflicts, "baseRev": base_rev, "currentRev": rev},
                        exit_code=EXIT_BLOCKED)
        if rev != base_rev and ctx.pending_entries:
            # 盘面已被推进(人侧或别处写了)且本次有实改 → 必须重新 context。
            # 全幂等的重放不在此列:重放同一份 ops 无副作用,允许短路通过。
            return emit(False, "PRECONDITION_FAILED",
                        f"工程当前 rev {rev},ops 声明 baseRev {base_rev};"
                        "请重跑 rs_edit.py context 取最新视图与 baseRev 后再 apply",
                        {"currentRev": rev, "baseRev": base_rev}, exit_code=EXIT_INPUT)

        applied = len(ctx.pending_entries)
        if applied == 0:
            return emit(True, "IDEMPOTENT",
                        f"全部 {len(ops)} 条 op 与现状一致(after == before),"
                        "幂等短路:rev 不变、OpLog 不增",
                        {"applied": 0, "idempotent": len(ops),
                         "human": ctx.human, "warnings": ctx.warnings}, exit_code=EXIT_OK)

        if dry_run:
            # 人话差异表(§5.4 样式),不写盘
            lines = [f"将执行 {applied} 处改动(rev {rev} → {rev + 1}):"]
            lines += [f"  {'①②③④⑤⑥⑦⑧⑨'[min(i, 8)]} {h}" for i, h in enumerate(ctx.human)]
            advice = _dirty_advice(ctx.dirty)
            if advice:
                lines.append("影响:" + ";".join(f"{a['scope']} 需重建({a['cmd']})"
                                              for a in advice))
            lines.append("冲突检查:" + (
                "发现冲突" if conflicts else
                (f"无(人侧 OpLog 在 rev {base_rev} 之后无改动)" if rev == base_rev else
                 f"窗口有 rev {base_rev + 1}–{rev} 的新改动,但与本次目标不重叠")))
            return emit(True, "DRY_RUN", lines[0],
                        {"human": lines, "applied": applied, "idempotent": noop_count,
                         "revFrom": rev, "revTo": rev + 1, "rebuild": advice,
                         "warnings": ctx.warnings}, exit_code=EXIT_OK)

        # 6. 原子写 IR + 追加 OpLog + 升 rev(全量校验已过,这里才碰盘)
        existing = load_oplog(root)
        entries: list[dict] = []
        for pending in ctx.pending_entries:
            oid = next_op_id(existing + entries)
            entries.append(_build_entry(ctx, pending, rev + 1, oid))
        write_text_atomic(rs_paths.project_json(root), dump_json(doc))
        for file, fdoc in ctx.file_docs.items():
            if fdoc is not None:
                _write_side_file(root, file, fdoc)
        append_oplog(root, entries)
        write_rev(root, rev + 1)
        write_base_snapshot(root, rev + 1, doc)
    finally:
        release_lock(root)

    # 7. 脏传播 + 最小重建建议(§5.3.5;缓存键含上游 hash,失配自动发生,只需如实报)
    advice = _dirty_advice(ctx.dirty)
    return emit(True, "APPLY_OK",
                f"已应用 {applied} 处改动(rev {rev} → {rev + 1}),OpLog +{len(entries)}",
                {"applied": applied, "idempotent": noop_count,
                 "revFrom": rev, "revTo": rev + 1,
                 "opIds": [str(_of(e, "op_id")) for e in entries],
                 "human": ctx.human, "rebuild": advice, "warnings": ctx.warnings},
                exit_code=EXIT_OK)


def cmd_undo(root: Path, last: int, actor: str = "agent", force: bool = False) -> int:
    """undo --last n:OpLog 逆写回 before 复原(n = Op 条数,人机同一条日志一起逆),
    并以 op_kind=undo/redo 的新 Op 记账(撤销也留痕)。"""
    if last <= 0:
        return emit(False, "BAD_VALUE", "--last 须为正整数", exit_code=EXIT_INPUT)
    ops = load_oplog(root)
    if not ops:
        return emit(False, "PRECONDITION_FAILED", "OpLog 为空,无可撤销", exit_code=EXIT_INPUT)
    try:
        acquire_lock(root, actor, force)
    except EditError as exc:
        return emit(False, exc.code, exc.message, exc.data, exit_code=exc.exit_code)
    try:
        rev = read_rev(root)
        window = list(ops[-last:])
        docs: dict[str, dict | None] = {"project.json": _load_ir(root)}
        for e in window:
            f = str((e.get("target") or {}).get("file") or "project.json")
            if f != "project.json" and f not in docs:
                p = (root / "notes.json") if f == "notes.json" else cutlist_path(root)
                if p.is_file():
                    try:
                        docs[f] = json.loads(p.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
                        _fail("INTERNAL", f"{f} 解析失败:{exc}", exit_code=EXIT_BLOCKED)
                else:
                    docs[f] = None
        human: list[str] = []
        for e in reversed(window):        # 逆序展开:指数在回退途中始终有效
            human += _invert(e, docs)
        touched = [f for f, d in docs.items() if d is not None]
        new_rev = rev + 1
        new_entries: list[dict] = []
        for orig in window:
            kind = str(_of(orig, "op_kind") or "set")
            oid = next_op_id(ops + new_entries)
            new_entries.append({
                "op_id": oid, "ts": now_iso(),
                "actor": {"kind": "agent", "id": "cutflow-rs_edit"},
                "target": dict(orig.get("target") or {}),
                "op_kind": "redo" if kind == "undo" else "undo",
                "before": orig.get("after"), "after": orig.get("before"),
                "base_rev": f"rev-{rev}", "rev": new_rev,
                "summary": f"撤销 {_of(orig, 'op_id')}:"
                           f"{orig.get('summary') or ''}".strip(),
            })
        for f in touched:
            if f == "project.json":
                write_text_atomic(rs_paths.project_json(root), dump_json(docs[f]))
            else:
                _write_side_file(root, f, docs[f])
        append_oplog(root, new_entries)
        write_rev(root, new_rev)
        write_base_snapshot(root, new_rev, docs["project.json"])
    finally:
        release_lock(root)
    return emit(True, "UNDONE",
                f"已逆写 {len(window)} 条 Op(rev {rev} → {new_rev});"
                "用 rs_edit.py diff 或 rs_oplog.py tail 复核",
                {"undone": len(window), "revFrom": rev, "revTo": new_rev,
                 "files": touched, "human": human}, exit_code=EXIT_OK)


def cmd_diff(root: Path, rev_a: int, rev_b: int) -> int:
    """diff --rev a --rev b:取 bases 快照(当前 rev 可用盘面)对比,人话摘要。"""

    def load_rev(rev: int) -> dict:
        if rev == read_rev(root):
            return _load_ir(root)
        p = cf_dir(root) / BASES_REL / f"{rev}.json"
        if not p.is_file():
            _fail("MISSING_SNAPSHOT",
                  f"无 rev {rev} 快照:{p}(快照由 apply/cutforge 落盘,LRU 可能已淘汰)",
                  exit_code=EXIT_DEP)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            _fail("INTERNAL", f"rev {rev} 快照解析失败:{exc}", exit_code=EXIT_BLOCKED)

    try:
        a, b = load_rev(rev_a), load_rev(rev_b)
    except EditError as exc:
        return emit(False, exc.code, exc.message, exc.data, exit_code=exc.exit_code)
    human, machine = rs_editor.diff_tracks(a, b)
    changed = bool(human)
    if not changed:
        human = ["两 rev 盘面一致(未发现片段级差异)"]
    return emit(True, "DIFF_OK",
                (f"rev {rev_a} → {rev_b}:{len(human)} 条差异" if changed
                 else f"rev {rev_a} 与 rev {rev_b} 无差异"),
                {"revA": rev_a, "revB": rev_b, "human": human, "diff": machine,
                 "changed": changed}, exit_code=EXIT_OK)


# ---------------------------------------------------------------- ops-validate

def cmd_ops_validate(ops_path: Path) -> int:
    """静态契约校验:op 名 / 寻址文法 / 字段白名单 / 值域 / U7(OP_AFTER 单一来源)。
    不落盘不渲染,也不做目标存在性与帧网格检查(那是 apply 对着 IR 才能做的)。"""
    _, ops, _, _ = load_ops_doc(ops_path)
    rows = []
    for i, op in enumerate(ops):
        name = op.get("op") if isinstance(op, dict) else None
        target = op.get("target") if isinstance(op, dict) else None
        try:
            check_op_shape(op, i)
            check_unsupported(op["op"], dict(op.get("after") or {}))
            validate_after(op, i)
            rows.append({"index": i, "op": name, "target": target, "ok": True})
        except EditError as exc:
            rows.append({"index": i, "op": name, "target": target,
                         "ok": False, "code": exc.code, "message": exc.message})
    bad = [r for r in rows if not r["ok"]]
    if bad:
        first = bad[0]
        return emit(False, first["code"],
                    f"{len(bad)}/{len(rows)} 条 op 未过校验,第一条:{first['message']}",
                    {"ops": rows}, exit_code=EXIT_INPUT)
    return emit(True, "VALIDATED",
                f"{len(rows)} 条 op 全部过校验(op 名/寻址/白名单/值域/U7);"
                "目标存在性与帧网格在 apply 时对 IR 校验",
                {"ops": rows}, exit_code=EXIT_OK)


# ---------------------------------------------------------------- context(投影层)

def _budget_bytes(s: str) -> int:
    t = str(s).strip().upper()
    mult = 1
    for suf, m in (("KIB", 1024), ("KB", 1024), ("K", 1024)):
        if t.endswith(suf):
            mult, t = m, t[:-len(suf)]
            break
    try:
        v = int(float(t) * mult)
    except ValueError:
        _fail("BAD_VALUE", f"--budget 无法解析:{s!r}(例:12KB / 12288)")
    if v <= 0:
        _fail("BAD_VALUE", f"--budget 须为正:{s!r}")
    return v


def _try_fetchable_state(cid: str):
    """M5 rs_fetchable.state 的防御性导入。返回:
    · None        = rs_fetchable 模块不可用(部署态未知);
    · {"state": "UNKNOWN", ...} = 模块在,但该 cid 不归取用系统管(阶段内置型能力);
    · 其余原样透传 state() 的 {"state": READY|MISSING|FAILED, ...}。
    本脚本绝不自己实现组件探测(部署/下载态一律问 rs_fetchable)。"""
    try:
        from rs_fetchable import state  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — 模块缺失 → 部署态未知
        return None
    try:
        return state(cid)
    except Exception:  # noqa: BLE001 — 未知组件(如阶段内置型)不硬探
        return {"state": "UNKNOWN", "message": ""}


def _degrade_rows(root: Path) -> list[dict]:
    """当前降级项:留痕账(capabilities_report)+ 已声明描述符(部署态经 rs_fetchable)。"""
    rows: dict[str, dict] = {}
    rp = rs_paths.resolve(root, "state") / "capabilities_report.json"
    if rp.is_file():
        try:
            doc = json.loads(rp.read_text(encoding="utf-8"))
            for entries in (doc.get("stages") or {}).values():
                for e in entries or []:
                    if e.get("degraded"):
                        rows[str(e.get("id"))] = {
                            "id": str(e.get("id")),
                            "status": str(e.get("status") or "degraded"),
                            "message": str(e.get("message") or e.get("reason") or "")}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
    caps_dir = Path(__file__).resolve().parents[1] / "templates" / "capabilities"
    if caps_dir.is_dir():
        for f in sorted(caps_dir.glob("*.json")):
            try:
                desc = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                continue
            cid = str(desc.get("id") or f.stem)
            if cid in rows:
                continue
            st = _try_fetchable_state(cid)
            if isinstance(st, dict):
                if st.get("state") == "READY":
                    continue
                if st.get("state") == "UNKNOWN":
                    # 取用系统不管的组件 = 阶段内置型:detector 在仓即视为已部署,
                    # 不制造降级噪声(探测部署态仍是 rs_fetchable 的事)
                    detector = Path(__file__).resolve().parent / str(desc.get("detector") or "")
                    if detector.is_file():
                        continue
                    rows[cid] = {"id": cid, "status": "missing",
                                 "message": f"detector 未在仓:{desc.get('detector')}"}
                    continue
                rows[cid] = {"id": cid,
                             "status": str(st.get("state") or "UNKNOWN").lower(),
                             "message": str(st.get("message") or st.get("degrade")
                                            or desc.get("degrade", {}).get("message", ""))}
            else:
                rows[cid] = {"id": cid, "status": "declared",
                             "message": "已声明(部署态未知;rs_fetchable 不可用)"}
    return sorted(rows.values(), key=lambda r: r["id"])


def _subtitle_summary(root: Path, doc: dict) -> str:
    """S7 产物摘要(卡数/CPS 取 wordline stats;缺失则如实说缺)。"""
    lines = ["## 字幕(S7 产物摘要)"]
    wl_p = rs_paths.wordline_json(root)
    n_text_clips = sum(len(t.get("clips", [])) for t in doc.get("tracks", [])
                       if t.get("kind") == "text")
    n_cards, cps = None, None
    if wl_p.is_file():
        try:
            wl = json.loads(wl_p.read_text(encoding="utf-8"))
            stats = wl.get("stats") or {}
            n_cards = len(wl.get("sentences") or [])
            cps = stats.get("cpsPeak") or stats.get("cps_peak") or stats.get("maxCps")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
    if n_cards is not None:
        lines.append(f"卡数 {n_cards}"
                     + (f" / CPS 峰值 {cps}" if cps is not None else "")
                     + (f" / IR text 轨卡 {n_text_clips}" if n_text_clips else ""))
    else:
        lines.append(f"wordline 缺失或不可读;IR text 轨卡 {n_text_clips}"
                     + "(改卡文本/时间走 subtitle.set / subtitle.retime,S7 重建生效)")
    sub = doc.get("subtitle") or {}
    if sub.get("ass"):
        lines.append(f"ASS:{Path(str(sub['ass'])).name}"
                     + (f" / 样式 {sub['style']}" if sub.get("style") else ""))
    return "\n".join(lines)


def _protect_table(root: Path) -> str:
    p = cutlist_path(root)
    zones: list = []
    if p.is_file():
        try:
            zones = json.loads(p.read_text(encoding="utf-8")).get("protect") or []
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
    lines = ["## 保护区(guard 第四条:切点不得侵入)", "| 区间 | note |", "|---|---|"]
    if not zones:
        lines.append("| (无) | segment.protect 可设 |")
    for z in zones:
        if isinstance(z, dict):
            lines.append(f"| {_fmt_ms(z.get('startMs'))}–{_fmt_ms(z.get('endMs'))} "
                         f"| {z.get('note') or '—'} |")
    return "\n".join(lines)


def _ops_available(root: Path) -> str:
    """可执行手法清单:全量支持的 op + beat.snap 动态态 + 显式不承诺的 op(U7)。"""
    static = sorted(op for op in HANDLERS if op != "beat.snap")
    lines = ["## 可执行手法(据 schema 与已部署能力)",
             "- 可用:" + " / ".join(static)]
    if beats_path(root).is_file():
        lines[-1] += " / beat.snap"
    else:
        lines.append("- 不可用:beat.snap(缺 04_粗剪决策/beats.json,先跑混剪能力)")
    lines.append("- 不承诺(U7,schema 未含承载字段):"
                 + " / ".join(sorted(UNSUPPORTED_OPS))
                 + "(ops-validate/apply 一律 OP_UNSUPPORTED)")
    lines.append("- 各 op 的 after 白名单与值域:rules/edit-op.md")
    return "\n".join(lines)


def _degrade_table(root: Path) -> str:
    rows = _degrade_rows(root)
    if not rows:
        return ""
    lines = ["## 当前降级项", "| 能力 | 状态 | 原因 |", "|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {r['status']} | {r['message'][:60]} |")
    return "\n".join(lines)


_EFFECT_GROUP_LABELS = {"in": "入场", "out": "出场", "transition": "转场", "combo": "组合"}
_FX_PREFIX_GROUP = (("fx.in.", "in"), ("fx.out.", "out"), ("tr.", "transition"))
_ASSET_KIND_LABELS = {"sfx": "音效", "element": "元素", "huazi": "花字", "bgm": "BGM"}
_ASSET_USAGE_LABELS = {"transition": "转场", "punchline": "强调", "enumeration": "枚举",
                       "ending": "收尾", "chapter": "章节", "emotion": "情绪",
                       "ui": "界面", "decor": "装饰", "data": "数据"}


def _effects_available() -> str:
    """可用特效段(分册04 §4.4):读效果目录 catalog.json,只列 status=可执行 的条目。

    目录文件 M13 才落 —— 缺失/坏档时显示占位提示(防御性读取,绝不崩 context);
    T2 条目单列一行并标注降级档(ADR-0054 三级分级;fx.glsl 缺失 → T1 近似)。
    """
    lines = ["## 可用特效(据当前风格包 + 已部署能力;只列 status=可执行)"]
    catalog: dict | list | None = None
    if EFFECTS_CATALOG_PATH.is_file():
        try:
            catalog = json.loads(EFFECTS_CATALOG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            catalog = None
    if catalog is None:
        lines.append(
            "- (效果目录未部署:M13 落地后此处列出 status=可执行 的 fxId;"
            "当前 fx.apply / transition.set --fx 可写契约,渲染端未注册 fxId 会降级并留痕)")
        return "\n".join(lines)
    entries = catalog.get("effects") if isinstance(catalog, dict) else catalog
    if not isinstance(entries, list):
        entries = []
    groups: dict[str, list[str]] = {"in": [], "out": [], "transition": [], "combo": []}
    t2: list[str] = []
    for e in entries:
        if not isinstance(e, dict) or str(e.get("status") or "") != "可执行":
            continue
        fx = str(e.get("fxId") or e.get("id") or "")
        if not fx:
            continue
        label = str(e.get("label") or "")
        text = f"{fx}({label})" if label else fx
        for prefix, g in _FX_PREFIX_GROUP:
            if fx.startswith(prefix):
                groups[g].append(text)
                break
        else:
            groups["combo"].append(text)
        if str(e.get("tier") or "") == "T2":
            t2.append(fx)
    for g in ("in", "out", "transition", "combo"):
        if groups[g]:
            lines.append(f"- {_EFFECT_GROUP_LABELS[g]}:" + " / ".join(groups[g][:8])
                         + (" / …" if len(groups[g]) > 8 else ""))
    if t2:
        lines.append("⚠ T2 不可用(fx.glsl 缺失,已降级为 T1 近似):" + " / ".join(t2[:8]))
    return "\n".join(lines)


def _fx_prescription(root: Path) -> dict:
    """工程的效果处方(intent_decisions.json resolved.effectsPrescription;缺 → {})。"""
    p = rs_paths.resolve(root, "brief") / "intent_decisions.json"
    if not p.is_file():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        resolved = doc.get("resolved") if isinstance(doc.get("resolved"), dict) else {}
        pres = resolved.get("effectsPrescription")
        return pres if isinstance(pres, dict) and pres else {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}


def _fx_usage_counts(doc: dict) -> dict:
    """IR 内已用的效果计数(与 rs_verify.check_effects_usage 同口径,轻量版)。"""
    n_tr = n_in = n_out = 0
    tracks = [t for t in doc.get("tracks", []) if t.get("kind") == "video"]
    clips = (tracks[0].get("clips") or []) if tracks else []
    for c in clips:
        tr = c.get("transition") or {}
        if tr and str(tr.get("type", "fade")).lower() not in ("cut", "none", ""):
            n_tr += 1
        elif tr.get("fx") and str(tr.get("fx")) != "tr.cut":
            n_tr += 1
        m = c.get("motion") or {}
        box = c.get("fx") or {}
        if m.get("inFx") or (box.get("in") or {}).get("fx") or (box.get("combo") or {}).get("fx"):
            n_in += 1
        if m.get("outFx") or (box.get("out") or {}).get("fx"):
            n_out += 1
    return {"transitions": n_tr, "in": n_in, "out": n_out}


def _fx_plan_section(root: Path, doc: dict) -> str:
    """「本工程该用的特效」段(ADR-0059/分册06 §9.4):把处方变成 Agent 每轮可见的待办。

    无处方 → 防御性提示(门禁不生效,NO_PRESCRIPTION 同口径);有处方 →
    必备 / 已用进度 / 缺口 / 禁用 四行,进度按 IR 机械计数。"""
    pres = _fx_prescription(root)
    if not pres:
        return ("## 本工程该用的特效\n"
                "- (工程未声明 effects_prescription:风格包无处方或未跑 rs_intent compile,"
                "EFFECTS_* 门禁不生效;可用特效见上节)")
    counts = _fx_usage_counts(doc)
    tr = pres.get("transition") or {}
    ins = pres.get("in") or {}
    outs = pres.get("out") or {}
    lines = ["## 本工程该用的特效(据风格包 effects_prescription,分册06 §9.4)"]
    lines.append(f"- 必备:过渡 {tr.get('min', 0)} 处 → 建议 {'、'.join((tr.get('prefer') or [])[:3]) or '硬切'};"
                 f"入场 {ins.get('min', 0)} 处 → {'、'.join((ins.get('prefer') or [])[:3]) or '—'};"
                 f"出场 {outs.get('min', 0)} 处 → {'、'.join((outs.get('prefer') or [])[:3]) or '—'}")
    lines.append(f"- 已用:过渡 {counts['transitions']}/{tr.get('min', 0)}"
                 f" ｜ 入场 {counts['in']}/{ins.get('min', 0)}"
                 f" ｜ 出场 {counts['out']}/{outs.get('min', 0)}")
    gaps = []
    if counts["transitions"] < int(tr.get("min") or 0):
        gaps.append(f"显式转场尚未达标(处方 ≥{tr.get('min')})")
    if counts["in"] < int(ins.get("min") or 0):
        gaps.append(f"入场动画尚未使用(处方 ≥{ins.get('min')})")
    if counts["out"] < int(outs.get("min") or 0):
        gaps.append(f"出场动画尚未使用(处方 ≥{outs.get('min')})")
    if gaps:
        lines.append("- ⚠ 缺口:" + ";".join(gaps))
    else:
        lines.append("- 缺口:无(已达处方下限;少用且用得准同样合格,分册06 §5.3)")
    forbid = sorted({str(f) for k in ("transition", "in", "out")
                     for f in ((pres.get(k) or {}).get("forbid") or [])})
    flashy = pres.get("flashy_max")
    if forbid or flashy is not None:
        lines.append(f"- 禁用/上限:{'、'.join(forbid) if forbid else '(无显式 forbid)'}"
                     + (f";花哨类全片 ≤{flashy} 处" if flashy is not None else ""))
    return "\n".join(lines)


def _assets_available() -> str:
    """可用素材段(分册04 §4.4):读素材库 manifest.json,按 kind+usage 分组各列前 5。

    manifest 是 M12 的产物 —— 缺失/坏档时显示占位提示(防御性读取,绝不崩 context)。
    """
    lines = ["## 可用素材(按 usage 分组,各列前 5 条)"]
    manifest: dict | None = None
    if ASSETS_MANIFEST_PATH.is_file():
        try:
            doc = json.loads(ASSETS_MANIFEST_PATH.read_text(encoding="utf-8"))
            manifest = doc if isinstance(doc, dict) else None
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            manifest = None
    if manifest is None:
        lines.append(
            "- (素材库 manifest 未部署:M12 落地后此处按 usage 分组列出;"
            "sfx.add --assetId / element.add / asset.swap 届时可引用,"
            "内置 7 音效的 name 别名仍可用)")
        return "\n".join(lines)
    groups: dict[tuple[str, str], list[str]] = {}
    for e in manifest.get("assets") or []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        kind = str(e.get("kind") or "?")
        usages = [str(u) for u in (e.get("usage") or ["?"]) if u]
        for u in usages or ["?"]:
            groups.setdefault((kind, u), []).append(str(e["id"]))
    for (kind, usage), ids in sorted(groups.items()):
        k = _ASSET_KIND_LABELS.get(kind, kind)
        u = _ASSET_USAGE_LABELS.get(usage, usage)
        lines.append(f"- {k}·{u}:" + " / ".join(ids[:5]) + (" / …" if len(ids) > 5 else ""))
    if len(lines) == 1:
        lines.append("- (manifest 存在但无条目:先跑 rs_asset.py scan/add 入库)")
    return "\n".join(lines)


def _clip_params(c: dict) -> str:
    bits = []
    if c.get("speed") is not None:
        bits.append(f"speed {c['speed']}")
    r = (c.get("reframe") or {}).get("anchorY")
    if r is not None:
        bits.append(f"anchorY {r}")
    if c.get("scale") is not None:
        bits.append(f"scale {c['scale']}")
    m = c.get("motion") or {}
    if m.get("in") and m.get("in") != "none":
        bits.append(f"in {m['in']}")
    if m.get("out") and m.get("out") != "none":
        bits.append(f"out {m['out']}")
    if c.get("freezeMs") is not None:
        bits.append(f"freeze {c['freezeMs']}ms")
    tr = c.get("transition") or {}
    if tr.get("type") and tr.get("type") not in ("cut", "none"):
        bits.append(f"xfade {tr['type']} {tr.get('durMs', '')}ms")
    if c.get("volume") is not None:
        bits.append(f"vol {c['volume']}")
    return " ".join(bits) or "—"


def _track_table(ti: int, t: dict, is_main: bool) -> str:
    kind = t.get("kind")
    tid = str(t.get("id") or ({"video": "V", "audio": "A", "text": "T"}
                              .get(kind, "X") + str(ti + 1)))
    title = clip_kind_label(t, ti)
    lines = [f"## {title}({kind})"]

    def cid_of(c: dict) -> str:
        return str(c.get("id") or rs_editor.content_id(c))

    def span_of(c: dict) -> str:
        s, d = c.get("startMs"), c.get("durationMs")
        return f"{_fmt_ms(s)}–{_fmt_ms((s or 0) + (d or 0))}" if s is not None else "?"

    if kind == "text":
        lines += ["| clipId | 时间窗 | 文本 | 可改字段 |", "|---|---|---|---|"]
        for c in t.get("clips", []):
            lines.append(f"| {cid_of(c)} | {span_of(c)} | {clip_desc(c) or '—'} "
                         f"| subtitle.set/retime · huazi.set/clear |")
        return "\n".join(lines)
    if kind == "video":
        lines += ["| clipId | 时间窗 | 源 | 文本 | 参数 | 可改字段 |",
                  "|---|---|---|---|---|---|"]
        editable = ("trim/move/split/delete/speed/reframe/motion/freeze/fx.apply"
                    + ("/overlay.remove" if not is_main else ""))
    else:
        lines += ["| clipId | 时间窗 | 源 | 角色 | 参数 | 可改字段 |",
                  "|---|---|---|---|---|---|"]
        editable = "audio.gain/sfx.remove"
    for c in t.get("clips", []):
        src = Path(str(c.get("src", ""))).name if c.get("src") else "—"
        fourth = (clip_desc(c) if kind == "video" else (c.get("role") or "—")) or "—"
        lines.append(f"| {cid_of(c)} | {span_of(c)} | {src} | {fourth} "
                     f"| {_clip_params(c)} | {editable} |")
    return "\n".join(lines)


def build_context(root: Path, scope: str, budget: int) -> tuple[str, dict]:
    """结构化时间线视图(§5.3.2 形态)。零帧路径;超预算按
    「手法清单→降级项→字幕摘要→保护区→覆盖/音频轨→主轨行」裁剪并声明被裁内容。"""
    doc = _load_ir(root)
    fps = doc.get("fps") or 30
    rev = read_rev(root)
    trimmed: list[str] = []
    tracks = doc.get("tracks", [])
    main_i = next((i for i, t in enumerate(tracks)
                   if t.get("kind") == "video" and t.get("name") == "main"),
                  next((i for i, t in enumerate(tracks)
                        if t.get("kind") == "video"), None))

    # 展示顺序:主轨 → 覆盖轨 → 音频 → 字幕摘要 → 保护区 → 手法 → 降级;
    # 裁剪优先级(小者先裁):ops/degrade=2 → subtitle/protect=3 → overlay/audio=4 → main=5
    sections: list[tuple[str, str, int]] = []      # (键, 正文, 裁剪优先级)
    for ti, t in enumerate(tracks):
        if not t.get("clips"):
            continue
        kind = t.get("kind")
        is_main = ti == main_i
        if scope == "clip" and kind != "video":
            continue
        if scope == "audio" and kind != "audio":
            continue
        if scope == "subtitle" and kind != "text":
            continue
        key = {"video": ("main" if is_main else "overlay"), "audio": "audio",
               "text": "subtitle"}.get(kind, kind)
        pri = {"main": 5, "overlay": 4, "audio": 4, "subtitle": 3}.get(key, 3)
        sections.append((key, _track_table(ti, t, is_main), pri))
    if scope in ("project", "subtitle"):
        s = _subtitle_summary(root, doc)
        if s:
            sections.append(("subtitle", s, 3))
    if scope == "project":
        sections.append(("protect", _protect_table(root), 3))
        sections.append(("ops", _ops_available(root), 2))
        sections.append(("effects", _effects_available(), 2))
        sections.append(("fxplan", _fx_plan_section(root, doc), 2))
        sections.append(("assets", _assets_available(), 2))
        d = _degrade_table(root)
        if d:
            sections.append(("degrade", d, 2))

    header = (f"# 时间线视图 · {root.name} · rev {rev} · fps {fps} · baseRev {rev}\n\n"
              "<!-- 寻址:clipId 列(无原生 id 的旧 IR 用内容寻址回退,见行首 cf- 前缀);"
              "禁止下标。改片走 rs_edit.py apply;本视图不含任何视频帧路径 -->\n")

    # 预算裁剪:优先级小者先裁;主轨是最后防线(逐行裁并声明)。
    # 页脚(裁剪声明)按「最坏全裁」预留入账,保证最终输出(含声明本身)≤ 预算硬上限。
    used = len(header.encode("utf-8"))

    def _trim_cost(label: str) -> int:
        return len(f"- 被裁:{label}\n".encode("utf-8"))

    max_body = max((len(t[1].splitlines()) for t in sections), default=0)
    worst_label = _trim_cost(f"主轨仅显示前 {max_body}/{max_body} 段(预算 {budget} 字节)")
    footer_worst = (len("\n## 预算裁剪声明\n".encode("utf-8"))
                    + (len(sections) + 1) * max(
                        (_trim_cost(SECTION_LABELS[k]) for k in SECTION_LABELS),
                        default=0) + worst_label)
    used += footer_worst
    trimmed: list[str] = []
    kept: set[int] = set()
    for i in sorted(range(len(sections)), key=lambda j: sections[j][2]):
        key, text, pri = sections[i]
        cost = len(text.encode("utf-8")) + 2
        if used + cost <= budget:
            kept.add(i)
            used += cost
        elif pri >= 5:
            rows = text.splitlines()
            head_rows = [r for r in rows if not r.startswith("|")
                         or r.startswith(("| clipId", "|---"))]
            body = [r for r in rows if r.startswith("| ") and not r.startswith("| clipId")]
            fitted: list[str] = []
            for r in body:
                cost_r = len((r + "\n").encode("utf-8"))
                if used + cost_r > budget:
                    break
                fitted.append(r)
                used += cost_r
            if fitted:
                kept.add(i)
                # 主轨行裁剪生效:用「表头 + 放得下的行」替换整节正文
                sections[i] = (key, "\n".join(head_rows + fitted), pri)
            n_cut = len(body) - len(fitted)
            if n_cut:
                trimmed.append(f"主轨仅显示前 {len(fitted)}/{len(body)} 段"
                               f"(预算 {budget} 字节)")
        else:
            trimmed.append(SECTION_LABELS.get(key, key))

    out = header
    for i, (_key, text, _pri) in enumerate(sections):
        if i in kept:
            out += "\n" + text + "\n"
    if trimmed:
        out += "\n## 预算裁剪声明\n" + "".join(f"- 被裁:{t}\n" for t in trimmed)
    info = {"rev": rev, "bytes": len(out.encode("utf-8")), "budget": budget,
            "trimmed": trimmed, "scope": scope,
            "degraded": _degrade_rows(root) if scope == "project" else []}
    return out, info


def cmd_context(root: Path, as_json: bool, scope: str, budget_s: str) -> int:
    budget = _budget_bytes(budget_s)
    try:
        text, info = build_context(root, scope, budget)
    except EditError as exc:
        return emit(False, exc.code, exc.message, exc.data, exit_code=exc.exit_code)
    if not as_json:
        sys.stdout.write(text)
        return EXIT_OK
    return emit(True, "CONTEXT_OK",
                f"时间线视图 rev {info['rev']},{info['bytes'] / 1024:.1f}KB"
                f"/{budget / 1024:.0f}KB"
                + (f",裁 {len(info['trimmed'])} 节" if info["trimmed"] else ""),
                {"markdown": text, **info}, exit_code=EXIT_OK)


# ---------------------------------------------------------------- CLI 装配
# 注意:argparse 装配保持「具名变量 + add_argument」的直写形态(rs_caps 的 AST 重放
# 与手册命令门禁共用同一套重放器,别用循环/工厂生成参数)。

def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="声明式编辑层(ADR-0048):自然语言改片的确定性通道")
    ap.add_argument("cmd", choices=["context", "ops-validate", "apply", "undo", "diff"],
                    help="context=时间线视图;ops-validate=校验 ops;apply=应用;undo=撤销;diff=对比")
    ap.add_argument("root", nargs="?",
                    help="工程目录(context/apply/undo/diff)或 ops 文件(ops-validate)")
    ap.add_argument("--json", action="store_true",
                    help="context 输出协议 JSON(默认 Markdown 视图)")
    ap.add_argument("--scope", choices=["clip", "project", "subtitle", "audio"],
                    default="project", help="context 视图范围(默认 project 全景)")
    ap.add_argument("--budget", default="12KB", help="context 视图字节预算硬上限(默认 12KB)")
    ap.add_argument("--ops", dest="ops_path",
                    help="apply:EditOp 清单 JSON(规则见 rules/edit-op.md)")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="apply:打印人话差异表,不写盘")
    ap.add_argument("--actor", choices=["agent", "user"], default="agent",
                    help="apply/undo 的操作者(进 OpLog actor.kind)")
    ap.add_argument("--base-rev", dest="base_rev", type=int, default=None,
                    help="apply:前置修订号(context 视图头部 rev;ops.json 包装内亦可)")
    ap.add_argument("--force", action="store_true", help="apply/undo:接管过期锁")
    ap.add_argument("--last", type=int, default=None, help="undo:逆写最近 n 条 Op")
    ap.add_argument("--rev", type=int, action="append", default=None,
                    help="diff:对比的两个 rev(给两次:--rev a --rev b)")
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_argparser().parse_args(argv)
    root_arg = a.root
    if a.cmd == "ops-validate":
        if not root_arg:
            return emit(False, "PRECONDITION_FAILED", "需要 ops.json 路径",
                        exit_code=EXIT_INPUT)
        try:
            return cmd_ops_validate(Path(root_arg))
        except EditError as exc:
            return emit(False, exc.code, exc.message, exc.data, exit_code=exc.exit_code)
    if not root_arg:
        return emit(False, "PRECONDITION_FAILED", "需要工程目录参数", exit_code=EXIT_INPUT)
    root = Path(root_arg)
    if not root.is_dir():
        return emit(False, "PRECONDITION_FAILED", f"工程目录不存在:{root}",
                    exit_code=EXIT_INPUT)
    try:
        if a.cmd == "context":
            return cmd_context(root, a.json, a.scope, a.budget)
        if a.cmd == "apply":
            if not a.ops_path:
                return emit(False, "PRECONDITION_FAILED", "apply 需要 --ops ops.json",
                            exit_code=EXIT_INPUT)
            return cmd_apply(root, Path(a.ops_path), a.dry_run, a.actor,
                             a.base_rev, a.force)
        if a.cmd == "undo":
            if a.last is None:
                return emit(False, "PRECONDITION_FAILED", "undo 需要 --last n",
                            exit_code=EXIT_INPUT)
            return cmd_undo(root, a.last, a.actor, a.force)
        if a.cmd == "diff":
            if not a.rev or len(a.rev) != 2:
                return emit(False, "PRECONDITION_FAILED",
                            "diff 需要 --rev a --rev b 各给一次", exit_code=EXIT_INPUT)
            return cmd_diff(root, a.rev[0], a.rev[1])
    except EditError as exc:
        return emit(False, exc.code, exc.message, exc.data, exit_code=exc.exit_code)
    return emit(False, "INTERNAL", f"未知命令:{a.cmd}", exit_code=EXIT_BLOCKED)


if __name__ == "__main__":
    ensure_utf8()
    sys.exit(main())
