"""阶段缓存与增量编排(ADR-0013 / rules/incremental.md)。

用法(在工程根目录执行):
  rs_run.py --status                 阶段状态灯 ✓ / ⚠ stale / ✗ missing(含 corrupt 告警)
  rs_run.py --explain S3             为什么 stale(逐个输入/参数/脚本 hash 对比)
  rs_run.py --only S7                只跑 S7(已 done 时报错,提示补 --force —— P14-1)
  rs_run.py --from S3                从 S3 起重跑
  rs_run.py --dirty                  只跑 stale 的阶段(连跑两次,第二次全 cached)
  rs_run.py --mark S4                人工介入后标记为 done(S4 的自动判定只认
                                     artboard manifest --apply 后的 appliedAt —— P11-1)
  rs_run.py --plan --from S3         只打印将要执行的命令,不执行
  rs_run.py --force --from S3        强制重跑起点阶段;--force 必须搭配 --from/--only
  rs_run.py --auto                   无人值守(副文档 04·阶段四 N3):人工阶段自动执行/标记,
                                     所有 CHECKS 转「自动决策 + 理由留痕」(05_时间线工程/
                                     pipeline.json 的 decision_log);粗剪 review 刀保守保留;
                                     断句歧义取 DP 最优并留候选(segments_candidates.json);
                                     L1 目测降级为抽帧留证(标注 L1 未人工确认);L0 硬闸
                                     不放松;L2 验收始终归用户,auto 不代劳。

缓存键 = sha1(上游产物内容 hash + 本阶段消费的参数快照(paramKeys) + **本阶段脚本文件
hash** + 外部服务版本)。粒度到 segment(见 rules/incremental.md §2)。
状态与记账写盘为原子写(P15-1);CutForge 编辑器运行时(.cutforge/lock)自动只读降级
并告警(O7-2);每个真正写盘的阶段跑前先备份(P13-1)。

能力挂载(ADR-0047):每阶段跑完自有命令后,通用挂载器(run_stage_capabilities)读
resolved.capabilities → 查 templates/capabilities/ 描述符 → 按 stage 执行/校验
detector → 产物缺失或未部署按 degrade 降级留痕(_内部状态/capabilities_report.json
+ decision_log);degrade.to="none" 缺失则阻断。引擎只认识「阶段」与「能力」,
永远不认识 videoType。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rs_common import COVER_PNG, RATIOS, emit  # noqa: E402
from rs_subtitle import STYLES, load_platforms  # noqa: E402
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046),本文件禁止目录字面量
import segmentation  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))
# 遍历跳过:内部状态目录(新旧名)+ 环境目录。阶段目录绝不能整目录跳过 ——
# 输入/产物 hash 要扫素材与产物(ADR-0046:名字一律查 rs_paths)。
SKIP_DIRS = rs_paths.STATE_DIR_NAMES | {".git", "__pycache__"}

ONLY = "only"
FROM = "from"
S13 = "all"


def spec(root: Path | None = None) -> list[dict]:
    """S0–S11 阶段注册表(rules/incremental.md §2 / SKILL.md §1)。

    路径一律经 rs_paths(ADR-0046):root 给定时按工程解析新旧目录名
    (旧结构工程 resolve 兜底 → 注册表指向真实存在的目录);root=None 用新名
    (rs_caps 能力目录等静态口径)。
    """
    if root is not None:
        def d(key: str, *parts: str) -> str:
            return rs_paths.rel(root, key, *parts)
    else:
        def d(key: str, *parts: str) -> str:
            return "/".join((rs_paths.p(key), *parts))
    return [
        {"id": "S0", "name": "基础素材", "manual": True,
         "inputs": [d("brief", "brief.md"), d("materials", "*")],
         "outputs": [d("materials", "manifest.json")],
         # rs_greenscreen 在册(ADR-0047):S0 的幕布检测逻辑在 rs_ingest 内联引用,
         # 其脚本 hash 属于 S0 工具集(改检测规则须打脏 S0),也是能力
         # vision.greenscreen 的 detector —— 挂载器据此识别为阶段自有,不重复执行。
         "scripts": ["rs_ingest.py", "rs_greenscreen.py"],
         "cmd": ["rs_ingest.py", "scan", ".", "--slug", "{slug}"]},
        {"id": "S1", "name": "转写与字级对齐",
         "inputs": [d("materials", "*")], "outputs": [d("timeline", "wordline.json")],
         "scripts": ["rs_align.py"],
         "cmd": ["rs_align.py", "build", "--media", "{first_material}",
                 "--out", d("timeline", "wordline.json")],
         "post": ["rs_align.py", "calibrate", d("timeline", "wordline.json"),
                  "--media", "{first_material}", "--out", d("timeline", "wordline.json")]},
        {"id": "S2", "name": "粗剪处理",
         "inputs": [d("timeline", "wordline.json")], "outputs": [d("cut", "cutlist.json")],
         "scripts": ["rs_cut.py"],
         "cmd": ["rs_cut.py", d("timeline", "wordline.json"), "--detect", "all",
                 "--out", d("cut")]},
        {"id": "S3", "name": "基础合成",
         "inputs": [d("cut", "cutlist.applied.json"), d("timeline", "wordline.json")],
         "outputs": [d("timeline", "project.json")], "scripts": ["rs_ir.py", "rs_render.py"],
         # 阶段四 N1:画幅进缓存键(brief.md「画幅:」声明即改参数源);{ratio} 缺省 9x16,
         # 未声明参数的旧工程命令与字面完全一致(零漂移)。
         "paramKeys": ["ratio"],
         "cmd": ["rs_ir.py", "build", "--from-cutlist", d("cut", "cutlist.applied.json"),
                 "--slug", "{slug}", "--ratio", "{ratio}",
                 "--out", d("timeline", "project.json")]},
        {"id": "S4", "name": "动画/信息卡", "manual": True,
         "inputs": [d("timeline", "project.json")],
         # P11-1:S4 的产物标记 = artboard manifest 经 `rs_artboard --apply` 写入的
         # appliedAt(真实存在物)。不再声明 timeline/project.json —— 那是 S3 的产物,
         # 曾让 S4 在 S3 跑完后被自动判 done,--mark S4 形同虚设。
         # 无卡片的工程跑 `rs_run --mark S4` 显式记录"无事可做"。
         "outputs": [d("assets", "artboard", "manifest.json")],
         "marker": d("assets", "artboard", "manifest.json") + ":appliedAt",
         "scripts": []},
        {"id": "S5", "name": "品牌(Logo 变体)",
         "inputs": [d("timeline", "project.json"), d("timeline", "variants.json")],
         # BUGREPORT P10:rs_brand 实际产 `成片_<ratio>_<logo>_<profile>.mp4`,
         # 声明须与之一致;此前误写 final_*.mp4,与 S8 同 glob 互相打脏、--dirty 永不收敛。
         # P10b-1:落 成片输出/branded/ 独占子目录,从根上消除与 S8 的 glob 交叠。
         "outputs": [d("output", "branded", "成片_*.mp4")], "scripts": ["rs_brand.py"],
         "cmd": ["rs_brand.py", d("timeline", "project.json"), "--variants",
                 d("timeline", "variants.json"), "--out", d("output", "branded")]},
        {"id": "S6", "name": "音效",
         "inputs": [d("timeline", "project.json")],
         # BUGREPORT P10:命令落点是 timeline/sfx_draft.json,声明必须同点。
         "outputs": [d("timeline", "sfx_draft.json")], "scripts": ["rs_sfx.py"],
         "cmd": ["rs_sfx.py", d("timeline", "project.json"), "--auto",
                 "--out", d("timeline", "sfx_draft.json")]},
        {"id": "S7", "name": "字幕",
         "inputs": [d("timeline", "wordline.json")],
         "outputs": [d("output", "subtitles.ass")],
         "scripts": ["rs_subtitle.py", "textopt.py", "segmentation.py"],
        # P12-1:S7 消费的参数进缓存键,且经 {max_chars} 真正进入命令行 ——
        # 改 brief 里的每卡字数,字幕重跑产出的卡就真的不一样(不只是账面变脏)。
        # 阶段四 N1:{sub_style}/{ratio} 同理(平台预设/brief 驱动);{final_wordline}
        # 让有粗剪的工程用 remap 后的成片空间 wordline 出字幕(与 S9 对账同一约定)。
        "paramKeys": ["maxChars", "cpsMax", "ratio"],
        "cmd": ["rs_subtitle.py", "--from-wordline", "{final_wordline}",
                "--style", "{sub_style}", "--ratio", "{ratio}",
                "--max-chars", "{max_chars}", "--out", d("output")]},
        # S8 是"手改字幕"的落点:它**只用现有 ass 重新烧录导出**,不重新生成字幕。
        # 没有这一段,改完字幕的一键重建会把用户的修改冲掉(见 OPTIMIZATION-v5 §4.2)。
        {"id": "S8", "name": "烧录导出",
         "inputs": [d("output", "subtitles.ass"), d("timeline", "project.json")],
         # P10b-1:落 成片输出/final/ 独占子目录(旧工程顶层遗留的 final_*.mp4 不追改,
         # rs_run 的 {final_video} 映射与 rs_cleanup 白名单两处都兼容新旧两落点)。
        "outputs": [d("output", "final", "final_*.mp4")],
        "scripts": ["rs_render.py"],
        # 阶段四 N1:画幅进缓存键({ratio} 缺省 9x16,旧行为零漂移)
        "paramKeys": ["ratio"],
        "cmd": ["rs_render.py", d("timeline", "project.json"), "--ratio", "{ratio}",
                "--profile", "final"]},
        {"id": "S9", "name": "自评与对齐断言",
         "inputs": [d("output", "subtitles.ass")],
         # sync_rows.json 在册(ADR-0047):能力 qc.black-frame 的产物落点进产物图,
         # rs_sync 每次都写(report + rows 两件),纳入 outHash/缺失检测。
         "outputs": [d("output", "sync_report.md"), d("output", "sync_rows.json")],
         "scripts": ["rs_sync.py"],
         "cmd": ["rs_sync.py", "--wordline", "{final_wordline}",
                 "--ass", d("output", "subtitles.ass"), "--out", d("output"),
                 "--video", "{final_video}", "--audio-content", "--qc"]},
        {"id": "S10", "name": "封面与文案",
         "inputs": [d("timeline", "wordline.json"), d("brief", "brief.md")],
         "outputs": [d("output", "metadata.json")], "scripts": ["rs_meta.py"],
         "cmd": ["rs_meta.py", "--wordline", d("timeline", "wordline.json"),
                 "--brief", d("brief", "brief.md"), "--platform", "douyin,bili",
                 "--out", d("output")]},
        {"id": "S11", "name": "交付", "manual": True,
         "inputs": [d("output", "metadata.json")],
         "outputs": [d("output", "deliverables.md")],
         "scripts": []},
    ]


# ---------------------------------------------------------------- hash

def sha1_file(p: Path) -> str:
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def expand(root: Path, patterns: list[str]) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        for p in sorted(root.glob(pat)):
            if p.is_file() and not any(part in SKIP_DIRS for part in p.parts):
                out.append(p)
    return out


def stage_parts(root: Path, st: dict, params: dict, external: dict) -> dict:
    inputs, tool = {}, {}
    for p in expand(root, st["inputs"]):
        inputs[str(p.relative_to(root))] = sha1_file(p)
    for name in st["scripts"]:
        sp = SCRIPTS_DIR / name
        if sp.is_file():
            tool[name] = sha1_file(sp)
    # P12-1:缓存键只计入该阶段声明消费的参数(paramKeys);未声明 paramKeys 的阶段
    # params 记空表 —— 改字幕参数不该触发重转写。全量参数快照仍存 pipeline.json 供复现。
    pk = st.get("paramKeys") or ()
    p = {k: params.get(k) for k in pk} if pk else {}
    return {"inputs": inputs, "params": p, "tool": tool, "external": external}


def key_of(parts: dict) -> str:
    return sha1_text(json.dumps(parts, sort_keys=True, ensure_ascii=False))


# ---------------------------------------------------------------- 状态

def state_path(root: Path, sid: str) -> Path:
    return rs_paths.resolve(root, "state") / f"{sid}.json"


def atomic_write_text(p: Path, text: str) -> None:
    """P15-1:状态/记账写盘统一「临时文件 + os.replace」原子写。

    此前直接 write_text,中断/掉电会留下半截 JSON;坏状态文件被静默当"从未运行",
    下次 --dirty 全量重跑(含最贵的 ASR)。对齐 CutForge 侧 atomic.rs 纪律。
    """
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def load_state(root: Path, sid: str) -> tuple[dict | None, str]:
    """读阶段状态,返回 (rec, problem);problem ∈ "" | "corrupt"。

    P15-1:解析失败必须与"从未运行"(missing)区分 —— corrupt 要显式告警,
    不许静默当从未跑过全量重跑。
    """
    p = state_path(root, sid)
    if not p.is_file():
        return None, ""
    try:
        return json.loads(p.read_text(encoding="utf-8")), ""
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None, "corrupt"


def read_state(root: Path, sid: str) -> dict | None:
    """兼容旧调用:损坏与缺失都返回 None;需要区分时用 load_state。"""
    return load_state(root, sid)[0]


def editor_lock_held(root: Path) -> bool:
    """O7-2:探测 CutForge 编辑器是否在运行。

    锁文件的创建/删除协议归 cutforge 管,这里只做 `.cutforge/lock` 存在性探测,
    绝不创建、绝不删除 —— 编辑器在跑时 rs_run 对状态账本只读降级,避免两仓互踩。
    """
    return (root / ".cutforge" / "lock").exists()


def write_state(root: Path, sid: str, doc: dict) -> str | None:
    """写阶段状态 + pipeline.json 汇总(均原子写)。

    返回 None = 已写盘;返回字符串 = 只读降级原因(O7-2:.cutforge/lock 被编辑器
    持有,本次运行结果不进缓存账,明确告警、不崩、不静默)。
    """
    if editor_lock_held(root):
        reason = (f"CutForge 编辑器在运行(检测到 .cutforge/lock),rs_run 只读降级:"
                  f"{sid} 状态未写盘,本次产物不进缓存账")
        print(f"[WARN] {reason}", file=sys.stderr)
        return reason
    d = rs_paths.resolve(root, "state")
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(state_path(root, sid), json.dumps(doc, ensure_ascii=False, indent=1))
    stages = {}
    for s in spec(root):
        rec, prob = load_state(root, s["id"])
        stages[s["id"]] = rec if rec is not None else {"status": prob or "missing"}
    agg = {"version": 1, "slug": root.name,
           "updatedAt": datetime.now(CST).isoformat(timespec="seconds"),
           # P12-1:params 快照随写状态落账 —— 首次运行即回填,旧工程缺 params 也在此补一次
           "params": params_of(root),
           "stages": stages,
           # 阶段四 N4:决策留痕(--auto 写入;write_state 全量重写聚合时原样保全,
           # 不清账 —— 账本只有 log_decision 一个写入口)
           "decision_log": load_decision_log(root)}
    tl = rs_paths.resolve(root, "timeline")
    tl.mkdir(parents=True, exist_ok=True)
    atomic_write_text(tl / "pipeline.json", json.dumps(agg, ensure_ascii=False, indent=1))
    return None


# ---------------------------------------------------------------- 决策留痕(N4)

def load_decision_log(root: Path) -> list:
    """pipeline.json 的 decision_log(缺文件/坏 JSON → 空表,不阻塞主流程)。"""
    p = rs_paths.pipeline_json(root)
    if not p.is_file():
        return []
    try:
        v = json.loads(p.read_text(encoding="utf-8")).get("decision_log")
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    return v if isinstance(v, list) else []


def log_decision(root: Path, entry: dict) -> None:
    """追加/替换一条决策(按 id 幂等:同 id 重跑覆盖,不重复堆积)。

    entry 形如 {"id": "auto:S2:review-keep", "stage": "S2", "source": "auto",
    "inferred": false, "what": …, "why": …};意图编译决策(00_制作简报/
    intent_decisions.json)的条目带 "id": "intent:<字段>"、source ∈ user/registry/
    default,原样并入。时间戳只记在运行时条目的 at;意图决策无 at —— 编译是纯函数,
    同输入字节级可复现(验收判据 3)。
    """
    p = rs_paths.pipeline_json(root)
    log = load_decision_log(root)
    by_id = {d.get("id"): i for i, d in enumerate(log) if isinstance(d, dict) and d.get("id")}
    if entry.get("id") in by_id:
        log[by_id[entry["id"]]] = entry
    else:
        log.append(entry)
    doc: dict = {}
    if p.is_file():
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            doc = {}
    doc["decision_log"] = log
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(doc, ensure_ascii=False, indent=1))


def intent_decisions_of(root: Path) -> list:
    """读 00_制作简报/intent_decisions.json(rs_intent compile 产物);坏文件返回空表。"""
    p = rs_paths.resolve(root, "brief") / "intent_decisions.json"
    if not p.is_file():
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    out = doc.get("decisions") if isinstance(doc, dict) else None
    return out if isinstance(out, list) else []


def seed_intent_decisions(root: Path) -> int:
    """--auto 开场:把意图编译决策并入 pipeline.json 的 decision_log(幂等)。"""
    n = 0
    for d in intent_decisions_of(root):
        if isinstance(d, dict) and d.get("id"):
            log_decision(root, d)
            n += 1
    return n


# ---------------------------------------------------------------- 能力挂载(ADR-0047)
#
# 可插拔能力注册表:类型(数据)──声明──► capabilities(数据)──► 能力描述符
# (templates/capabilities/*.json,数据)──► 本挂载器按 stage 挂到阶段(引擎通用机制)。
# 引擎只认识「阶段」与「能力」,类型只作为 registry 查表键存在,绝不进分支
# (ADR-0018:禁止按类型字符串硬编码;tests/test_capabilities.py 门禁把守)。
# 与 capabilities.json(能力目录:Agent 可调用的脚本/命令清单)是两个东西 —— 本目录
# templates/capabilities/ 是「算法能力表」:引擎的算法能力声明,能力靠登记生效。

# 能力描述符目录(templates/capabilities/);与 rs_intent.load_capability_descriptors 同源
CAPS_DIR = SCRIPTS_DIR.parent / "templates" / "capabilities"


def load_capability_descriptors() -> dict[str, dict]:
    """算法能力注册表:templates/capabilities/*.json,每能力一文件。

    坏文件(坏 JSON / 缺 id / id 重复)WARN 跳过,绝不崩主流程;`_` 前缀文件是
    体例样板,不进注册表。rs_intent 从本加载器 import(单一真相源)。
    """
    out: dict[str, dict] = {}
    if not CAPS_DIR.is_dir():
        return out
    for f in sorted(CAPS_DIR.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            desc = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            print(f"[WARN] 能力描述符 {f.name} 解析失败,已跳过:{exc}", file=sys.stderr)
            continue
        cid = desc.get("id") if isinstance(desc, dict) else None
        if not cid:
            print(f"[WARN] 能力描述符 {f.name} 缺 id,已跳过", file=sys.stderr)
            continue
        if str(cid) in out:
            print(f"[WARN] 能力描述符 id 重复:{cid}({f.name}),后者已跳过", file=sys.stderr)
            continue
        out[str(cid)] = desc
    return out


def registry_video_types() -> dict:
    """registry 的 videoTypes(经 rs_intent 单一真相源;rs_intent 顶层 import 本模块,
    故此处只能函数内懒加载防循环)。registry 不可读 → 空表,兜底退化为「无能力」,不崩。"""
    try:
        from rs_intent import load_registry  # noqa: PLC0415 — 懒加载防循环导入
        return load_registry().get("videoTypes") or {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ImportError) as exc:
        print(f"[WARN] registry.json 不可读,能力兜底退化为空:{exc}", file=sys.stderr)
        return {}


def intent_resolved_of(root: Path) -> dict:
    """intent_decisions.json 的 resolved(缺失/坏文件 → 空表)。"""
    p = rs_paths.resolve(root, "brief") / "intent_decisions.json"
    if not p.is_file():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    return doc.get("resolved") if isinstance(doc.get("resolved"), dict) else {}


def resolved_capabilities(root: Path) -> tuple[list[str], list[str]]:
    """本工程声明的能力列表(挂载器入口)。

    优先 intent_decisions.json 的 resolved.capabilities(rs_intent compile 产物);
    缺失(旧工程)→ 按 registry 的 videoTypes.<videoType>.capabilities 兜底 +
    WARN capabilitiesMissing(ADR-0047 四问口径:旧工程行为不变)。
    返回 (能力 id 列表, 告警列表)。
    """
    resolved = intent_resolved_of(root)
    if isinstance(resolved.get("capabilities"), list):
        return [str(c) for c in resolved["capabilities"]], []
    vts = registry_video_types()
    meta = vts.get(str(resolved.get("videoType") or "")) or {}
    caps = [str(c) for c in meta.get("capabilities") or []]
    return caps, ["capabilitiesMissing"]


def capabilities_for_stage(root: Path, st: dict) -> list[dict]:
    """解析出挂载在阶段 st 上的能力描述符(按声明顺序;无描述符的能力 WARN 跳过)。"""
    caps, warns = resolved_capabilities(root)
    for w in warns:
        print(f"[WARN] {w}:工程缺 resolved.capabilities(旧工程),按 registry 兜底",
              file=sys.stderr)
    descs = load_capability_descriptors()
    out: list[dict] = []
    for cid in caps:
        desc = descs.get(cid)
        if desc is None:
            print(f"[WARN] 能力 {cid} 无描述符,无法挂载(先在 templates/capabilities/ 登记)",
                  file=sys.stderr)
            continue
        if desc.get("stage") == st["id"]:
            out.append(desc)
    return out


def capability_artifact_path(root: Path, artifact: str) -> Path | None:
    """描述符 artifact(「rs_paths 逻辑键/子路径」)→ 工程内绝对路径;逻辑键不合法 → None。"""
    key, _, rest = str(artifact).partition("/")
    if key not in rs_paths.STAGE_DIRS or not rest:
        return None
    return rs_paths.resolve(root, key) / rest


def capabilities_report_path(root: Path) -> Path:
    """_内部状态/capabilities_report.json(能力挂载留痕账,按阶段覆盖)。"""
    return rs_paths.resolve(root, "state") / "capabilities_report.json"


def record_capability_report(root: Path, sid: str, entries: list[dict]) -> None:
    """把本阶段能力挂载结果并入留痕账(原子写;坏旧账直接重建,不崩)。"""
    p = capabilities_report_path(root)
    doc: dict = {}
    if p.is_file():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("stages"), dict):
                doc = loaded
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
    stages = doc.setdefault("stages", {})
    stages[sid] = entries
    doc["version"] = 1
    doc["updatedAt"] = datetime.now(CST).isoformat(timespec="seconds")
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(doc, ensure_ascii=False, indent=1))


def run_stage_capabilities(root: Path, st: dict, info: dict | None = None) -> tuple[bool, str]:
    """通用能力挂载器(ADR-0047 §5.2):阶段跑完自家命令后,挂载声明在该阶段的能力。

    流程:读 resolved.capabilities → 查描述符 → 按 stage 执行 detector →
    校验 artifact 存在 → 缺失/未部署按 degrade 降级并留痕。
      · detector 文件不存在 = 能力未部署:走降级,不崩;degrade.to=="none" → 阻断;
      · detector 已是本阶段自有脚本(st["scripts"])时不重复执行,只校验产物 ——
        已登记的三条能力(greenscreen/dead-air/black-frame)都属此类;M8/M9 的
        算法能力(rs_beat/rs_shot 等)落地时随脚本与描述符一起登记,走执行分支;
      · 降级留痕:_内部状态/capabilities_report.json 逐条 {degraded:true, trace,
        message},并进 pipeline.json 的 decision_log(id 幂等,重跑覆盖);
      · 返回 (False, msg) = 有不可降级能力缺失,阶段按失败处置(非零退出)。
    """
    if info is None:
        info = {}
    descs = capabilities_for_stage(root, st)
    if not descs:
        return True, ""
    entries: list[dict] = []
    blocked: list[str] = []
    for desc in descs:
        cid = str(desc.get("id"))
        degrade = desc.get("degrade") or {}
        trace = str(degrade.get("trace") or "capabilityDegraded")
        message = str(degrade.get("message") or "")
        art_rel = str(desc.get("artifact") or "")
        art = capability_artifact_path(root, art_rel)
        entry: dict = {"id": cid, "stage": st["id"], "artifact": art_rel}
        # ① detector 未部署/执行失败;② 产物校验(缺一即按 degrade 处置)
        reason = ""
        detector = SCRIPTS_DIR / str(desc.get("detector") or "")
        if not detector.is_file():
            reason = f"detector 未部署:{desc.get('detector')}"
        elif str(desc.get("detector")) not in (st.get("scripts") or []):
            # 阶段外置 detector:通用执行(M8/M9 算法能力的挂载路径)
            p, terr = _run_subprocess([sys.executable, str(detector)], root, st)
            if terr:
                reason = f"detector 执行超时:{terr[-160:]}"
            elif p is not None and p.returncode != 0:
                reason = (f"detector 执行失败(exit {p.returncode}):"
                          f"{(p.stderr or p.stdout or '')[-160:]}")
        if not reason and (art is None or not art.is_file()):
            reason = "产物缺失:" + art_rel if art_rel else "产物缺失(artifact 配置非法)"
        if reason:
            if degrade.get("to") == "none":
                entry.update({"status": "blocked", "degraded": True, "trace": trace,
                              "message": message or reason, "reason": reason})
                blocked.append(f"{cid}({reason})")
            else:
                entry.update({"status": "degraded", "degraded": True, "trace": trace,
                              "message": message or reason, "reason": reason,
                              "degradeTo": degrade.get("to")})
                log_decision(root, {"id": f"cap:{st['id']}:{cid}", "stage": st["id"],
                                    "source": "auto", "inferred": False,
                                    "what": f"能力 {cid} 降级:{reason}",
                                    "why": message, "trace": trace,
                                    "at": datetime.now(CST).isoformat(timespec="seconds")})
        else:
            entry.update({"status": "ok", "degraded": False})
        entries.append(entry)
    record_capability_report(root, st["id"], entries)
    info["capabilities"] = entries
    if blocked:
        return False, f"{st['id']} 能力阻断(不可降级):{';'.join(blocked)}"
    n_deg = sum(1 for e in entries if e.get("degraded"))
    if n_deg:
        return True, (f"能力降级 {n_deg} 条(留痕:{rs_paths.p('state')}/"
                      "capabilities_report.json)")
    return True, ""


def auto_decision(sid: str, kind: str, what: str, why: str, **extra) -> dict:
    """--auto 的自动决策条目工厂:决策内容 + 理由,一律留痕。"""
    d = {"id": f"auto:{sid}:{kind}", "stage": sid, "source": "auto", "inferred": False,
         "what": what, "why": why, "at": datetime.now(CST).isoformat(timespec="seconds")}
    d.update(extra)
    return d


DEFAULT_CPS_MAX = 9

_PLATFORM_ALIASES = {"抖音": "douyin", "douyin": "douyin",
                     "视频号": "shipinhao", "微信": "shipinhao", "shipinhao": "shipinhao",
                     "小红书": "xiaohongshu", "xiaohongshu": "xiaohongshu",
                     "b站": "bilibili", "bilibili": "bilibili"}

# P12-1:brief.md 可显式声明管线参数(改 brief 即改参数源)。逐行匹配,未声明不臆测。
_BRIEF_PARAM_RULES = [
    (re.compile(r"^\s*(?:[-*]\s*)?(?:每卡字数|maxChars)\s*[:：=]\s*(\d{1,2})\s*$"),
     "maxChars", int),
    (re.compile(r"^\s*(?:[-*]\s*)?(?:cps|CPS|cpsMax)\s*[:：=]\s*([\d.]+)\s*$"),
     "cpsMax", float),
    (re.compile(r"^\s*(?:[-*]\s*)?(?:画幅|比例)\s*[:：=]\s*(9x16|3x4|16x9)\s*$"),
     "ratio", str),
    (re.compile(r"^\s*(?:[-*]\s*)?(?:平台|发布平台)\s*[:：=]\s*([^,,，;;\s]+)\s*$"),
     "platform", lambda s: _PLATFORM_ALIASES.get(s.strip(), _PLATFORM_ALIASES.get(
         s.strip().lower(), s.strip()))),
    # 阶段四 N1:风格 token(brief「风格 token:」行)驱动 S7 的 {sub_style};
    # 值不合法时 _sub_style_and_ratio 退回平台预设/字面缺省,不臆测。
    (re.compile(r"^\s*(?:[-*]\s*)?(?:风格 token|字幕风格|风格)\s*[:：=]\s*(\S+)\s*$"),
     "subStyle", str),
]


def default_params() -> dict:
    """params 缺失时的回退默认(与旧版 params_of 缺省完全一致,保证旧账可比)。"""
    return {"maxChars": dict(segmentation.MAX_CHARS), "cpsMax": DEFAULT_CPS_MAX}


def brief_params(root: Path) -> dict:
    """brief.md 里显式声明的参数(仅声明了的键):maxChars / cpsMax / ratio / platform。"""
    p = rs_paths.brief_md(root)
    if not p.is_file():
        return {}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict = {}
    for line in text.splitlines():
        for pat, key, cast in _BRIEF_PARAM_RULES:
            m = pat.match(line)
            if m:
                try:
                    out[key] = cast(m.group(1))
                except (ValueError, TypeError):
                    pass
    return out


def params_of(root: Path) -> dict:
    """阶段参数快照(P12-1:pipeline.json 的 params 是单一真相源)。

    优先读已落账的 params(仅缺失时回退默认),再叠加 brief 显式声明(声明键覆盖)。
    落账由 write_state 完成:首次运行回填一次,此后改 brief 里的参数即改快照。
    """
    stored: dict | None = None
    p = rs_paths.pipeline_json(root)
    if p.is_file():
        try:
            v = json.loads(p.read_text(encoding="utf-8")).get("params")
            if isinstance(v, dict) and v:
                stored = v
        except (json.JSONDecodeError, OSError):
            pass
    params = dict(stored) if stored else default_params()
    params.update(brief_params(root))
    return params


def outputs_hash(root: Path, st: dict) -> str | None:
    """阶段产物内容指纹(M9-3):识别带外改写——产物被 rs_run 之外的工具
    (如 CutForge 编辑器)改过时,parts(输入/参数 hash)依旧全等,只有它能发现。"""
    outs = expand(root, st["outputs"])
    if not outs:
        return None
    h = hashlib.sha256()
    for pth in sorted(outs):
        try:
            h.update(str(pth.relative_to(root)).encode("utf-8"))
        except ValueError:
            h.update(str(pth).encode("utf-8"))
        try:
            h.update(pth.read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:16]


def manual_marker_ok(root: Path, st: dict) -> bool:
    """P11-1:人工阶段的「真实产物标记」。

    显式 marker 形如 `"路径:JSON键"`(JSON 键支持点号下钻),如 S4 的
    `03_创作素材/artboard/manifest.json:appliedAt`;未声明 marker 的人工阶段
    (S0/S11)退回 outputs 存在性 —— 它们声明的产物本来就是本阶段的真实产物。
    """
    marker = st.get("marker")
    if not marker:
        return bool(expand(root, st["outputs"]))
    rel, _, jkey = marker.partition(":")
    f = root / rel
    if not f.is_file():
        return False
    if not jkey:
        return True
    try:
        cur = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False
    for k in jkey.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return False
        cur = cur[k]
    return True


def evaluate(root: Path, st: dict) -> dict:
    """返回 {"status": done|stale|missing|corrupt|manual, "staleReason": [...]}"""
    rec, problem = load_state(root, st["id"])
    if problem == "corrupt":
        # P15-1:状态文件损坏 ≠ 从未运行 —— 显式告警,按 missing 处理重跑但留痕
        return {"status": "corrupt",
                "staleReason": [f"状态文件损坏({rs_paths.p('state')}/{st['id']}.json 无法解析,按缺失重跑)"],
                "outs": len(expand(root, st["outputs"]))}
    outs = expand(root, st["outputs"])
    # P25-1:failed 是真实状态(incremental.md §2 从文档承诺变成实现):
    # 上次执行失败的阶段显式显示 failed + 原因,重跑成功后自然回 done。
    if rec and rec.get("status") == "failed":
        return {"status": "failed",
                "staleReason": [f"上次执行失败:{str(rec.get('error') or '')[:60]}"],
                "outs": len(outs)}
    if st.get("manual") and not rec:
        # P11-1:人工阶段一律认「真实产物标记」,绝不借上游产物自动 done
        if manual_marker_ok(root, st):
            return {"status": "done", "staleReason": [], "manual": True}
        why = (f"人工阶段无产物标记({st['marker'].split(':')[0]} 缺 --apply 写入的 appliedAt)"
               if st.get("marker") else "产物缺失")
        return {"status": "missing", "staleReason": [why], "manual": True}
    if not rec:
        return {"status": "missing", "staleReason": ["从未记录状态"], "outs": len(outs)}
    oh = outputs_hash(root, st)
    if st["outputs"] and rec.get("outHash") and oh and rec["outHash"] != oh:
        return {"status": "stale",
                "staleReason": [f"产物带外改写(outHash {rec['outHash']} → {oh};"
                                "rs_run 之外的工具改过产物)"],
                "outs": len(outs)}
    live = params_of(root)
    pk = st.get("paramKeys") or ()
    cur = stage_parts(root, st, live, rec.get("parts", {}).get("external", {}))
    # P12-1:params 按该阶段声明的 paramKeys 与「当前生效参数」对账;
    # 未声明 paramKeys 的阶段两侧同为空表,不因全局参数变化误伤。
    old_parts = dict(rec.get("parts") or {})
    old_cmp = dict(old_parts)
    old_cmp["params"] = {k: (old_parts.get("params") or {}).get(k) for k in pk}
    diff = diff_parts(old_cmp, cur)
    if diff:
        return {"status": "stale", "staleReason": diff, "outs": len(outs)}
    if not outs and st["outputs"]:
        # P11-1:声明 marker 的人工阶段(如无卡片工程的 S4)合法地没有 marker 文件,
        # --mark 的显式记录即真相;其余阶段产物缺失照旧判 missing。
        if st.get("marker") and st.get("manual"):
            pass
        else:
            return {"status": "missing", "staleReason": ["产物缺失"], "outs": 0}
    return {"status": "done", "staleReason": [], "outs": len(outs)}


def diff_parts(old: dict, new: dict) -> list[str]:
    why: list[str] = []
    for group in ("inputs", "tool", "external", "params"):
        o, n = old.get(group) or {}, new.get(group) or {}
        for k in sorted(set(o) | set(n)):
            if o.get(k) != n.get(k):
                if k not in o:
                    why.append(f"{group} 新增 {k}")
                elif k not in n:
                    why.append(f"{group} 移除 {k}")
                else:
                    why.append(f"{group} 变化 {k}({str(o[k])[:10]} → {str(n[k])[:10]})")
    return why


STATUS_ICON = {"done": "✓", "stale": "⚠", "missing": "✗", "corrupt": "✗",
               "failed": "✗", "blocked": "⚠"}


def editor_session_summary(root: Path) -> dict | None:
    """RT-2:读 cutforge RT-1 会话摘要(.cutforge/session-summary.json,actor=human
    的 Op 清单 + rev 区间)。文件不存在/损坏/非摘要文件 → 静默跳过(返回 None)——
    不是每个工程都被编辑器打开过;有摘要则把「编辑器这次改了什么」带进 --status。"""
    p = root / ".cutforge" / "session-summary.json"
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict) or doc.get("kind") != "cutforge-session-summary":
        return None
    return doc


def cmd_status(root: Path) -> int:
    lines, data = [], []
    evals = [(st, evaluate(root, st)) for st in spec(root)]
    ids = [st["id"] for st, _ in evals]
    failed_ids = [st["id"] for st, r in evals if r["status"] == "failed"]
    for st, r in evals:
        # P25-1:上游 failed 时下游显式 blocked(不再装作无事发生)
        up = next((f for f in failed_ids if ids.index(f) < ids.index(st["id"])), None)
        if up and r["status"] != "failed":
            r = {"status": "blocked",
                 "staleReason": [f"上游 {up} 处于 failed,先修复/重跑它"],
                 "outs": r.get("outs", 0)}
        icon = STATUS_ICON.get(r["status"], "?")
        extra = f"  ({r['staleReason'][0][:60]})" if r.get("staleReason") else ""
        extra += "  [人工阶段]" if st.get("manual") else ""
        lines.append(f"{st['id']} {icon} {r['status']:<8} {st['name']}{extra}")
        data.append({"id": st["id"], "name": st["name"], **r})
    payload: dict = {"stages": data}
    msg = f"{sum(1 for d in data if d['status'] == 'done')}/{len(data)} 阶段已完成"
    if failed_ids:
        msg += f";失败 {len(failed_ids)} 个({','.join(failed_ids)})"
    # RT-2:编辑器会话摘要(存在才显示;缺失/损坏静默跳过)
    sess = editor_session_summary(root)
    if sess:
        ops = sess.get("ops") or []
        lines.append(f"✎ 编辑器会话(cutforge):rev {sess.get('revFrom')}→{sess.get('revTo')},"
                     f"人工改动 {len(ops)} 条 —— 详见 `rs_editor.py diff <工程>`")
        for op in ops[-5:]:
            kind = op.get("opKind") or op.get("op_kind") or "?"
            path = (op.get("target") or {}).get("path", "")
            lines.append(f"    · [{kind}] {path} {str(op.get('summary') or '')[:48]}".rstrip())
        payload["editorSession"] = {"revFrom": sess.get("revFrom"),
                                    "revTo": sess.get("revTo"),
                                    "userOpCount": len(ops)}
    print("\n".join(lines))
    return emit(True, "STATUS_OK", msg, payload)


def cmd_explain(root: Path, sid: str) -> int:
    st = next((s for s in spec(root) if s["id"] == sid), None)
    if not st:
        return emit(False, "BAD_STAGE", f"未知阶段:{sid}", exit_code=2)
    r = evaluate(root, st)
    return emit(True, "EXPLAIN_OK",
                f"{sid} = {r['status']}" + ("" if not r["staleReason"] else ": " + "; ".join(r["staleReason"])),
                {"stage": sid, **r})


# ---------------------------------------------------------------- 备份 / 一键重建

BACKUP_KEEP = 5


def backup_paths(root: Path, st: dict, errors: list[str] | None = None) -> Path | None:
    """把该阶段将覆盖的产物备份到 _内部状态/backup/<时间戳>/(手改成果的唯一保险)。

    P13-2:备份与旧备份清理失败不再被 ignore_errors 掩盖 —— 失败项收进 errors
    由调用方上报;errors=None 时直接抛出(严格调用方)。
    """
    files = expand(root, st["outputs"])
    if not files:
        return None
    ts = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    dest = rs_paths.backup_dir(root, ts)
    for f in files:
        rel = f.relative_to(root)
        tgt = dest / rel
        tgt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, tgt)
    atomic_write_text(dest / "_manifest.json", json.dumps(
        {"stage": st["id"], "at": ts, "files": [str(f.relative_to(root)) for f in files]},
        ensure_ascii=False, indent=1))
    _prune_backups(root, errors)
    return dest


def _prune_backups(root: Path, errors: list[str] | None = None) -> None:
    bdir = rs_paths.backup_dir(root)
    if not bdir.is_dir():
        return
    items = sorted([d for d in bdir.iterdir() if d.is_dir()], key=lambda d: d.name)
    for old in items[:-BACKUP_KEEP]:
        try:
            shutil.rmtree(old)
        except OSError as exc:
            # P13-2:删除失败如实上报,不再 ignore_errors=True 静默吞掉
            if errors is None:
                raise
            errors.append(f"{rs_paths.p('state')}/backup/{old.name}: {exc}")


def rollback(root: Path, at: str = "") -> tuple[bool, str]:
    bdir = rs_paths.backup_dir(root)
    if not bdir.is_dir():
        return False, "没有可用的备份"
    items = sorted([d for d in bdir.iterdir() if d.is_dir()], key=lambda d: d.name)
    if not items:
        return False, "没有可用的备份"
    target = next((d for d in reversed(items) if d.name == at), None) if at else items[-1]
    if target is None:
        return False, f"找不到备份 {at}(可用:{', '.join(d.name for d in items)})"
    n = 0
    for f in target.rglob("*"):
        if f.is_file() and f.name != "_manifest.json":
            rel = f.relative_to(target)
            dst = root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            n += 1
    return True, f"已从备份 {target.name} 还原 {n} 个文件"


REBUILD_TMPL = '''"""一键重建 —— {label}

改完 `{folder}/` 里的东西后运行本脚本。它会:
  1) 备份将被覆盖的产物到 {state}/backup/
  2) 从 {sid} 级联重跑(上游命中缓存,所以很快)
  3) 跑完输出成片 + 自检报告

只改这一个文件夹 → 只点这一个脚本。不要手动去调 rs_render。
{extra_note}
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[{depth}]
RUNNER = r"{runner}"
sys.exit(subprocess.run(
    [sys.executable, RUNNER, "--root", str(ROOT), "--from", "{sid}", "--force"],
    cwd=str(ROOT)).returncode)
'''

# B8(BUGREPORT-20260913):timeline 的级联起点是 S3 —— 会重新生成 project.json,
# 手注的单 clip 音频/转场修正全被冲掉。必须在脚本头部写明正确出路。
# 文案里的 {cut}/{timeline}/{output} 由 init_rebuild 按工程解析后填入(旧结构工程出旧名)。
REBUILD_EXTRA_NOTES = {
    "cut": """⚠ 例外:若你只改了 cuts[].action(删/留决策),不要跑本脚本 ——
   S2 会从 wordline 重新 detect 并**重写 cutlist.json**,把触发重建的那次编辑冲掉。
   正确做法:`python <scripts>/rs_cut.py --apply {cut}/cutlist.json`(只重算 keep/removedMs)。
   CutForge 侧的编辑同理:cut_apply 已服务端重算 keep,无需重跑 S2。""",
    "timeline": """⚠ 例外:若你**手改过 {timeline}/project.json**(手注单 clip 音频/
   转场修正等),不要跑本脚本 —— S3 会重新生成 IR 把手注冲掉。
   正确做法:改跑 `{output}/rebuild.py`(S8:只用现有 ass 重烧录导出,不碰 IR)。
   (rs_ir build 也会检测手注痕迹并拒绝覆盖,除非显式 --force。)""",
}

# (逻辑键, 子路径, 阶段, 标签) —— 目录名经 rs_paths 按工程解析(ADR-0046)
INIT_MAP = [("cut", "", "S2", "粗剪决策(CutList)"), ("timeline", "", "S3", "IR / Wordline"),
            ("assets", "artboard", "S4", "artboard 卡片"),
            ("output", "", "S8", "字幕(改完只重烧录导出,不重新生成字幕)")]


ARTBOARD_REBUILD_TMPL = '''"""一键重建 —— artboard 卡片

改完卡片源码后运行本脚本。它会:
  1) 只重导出**源码变了**的卡片(内容寻址)
  2) 把新产物回填 IR(尺寸/时长校验;尺寸不符会停住而不是拉伸)
  3) 从 S4 级联重跑(上游走缓存)→ 出片 + 自检
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = Path(r"{scripts}")
MANIFEST = ROOT / {assets_dir} / "artboard" / "manifest.json"


def run(*args):
    p = subprocess.run([sys.executable, *[str(x) for x in args]], cwd=str(ROOT))
    if p.returncode != 0:
        sys.exit(p.returncode)


if not MANIFEST.is_file():
    run(SCRIPTS / "rs_artboard.py", "--root", ROOT, "--scan",
        ROOT / {assets_dir} / "artboard", "--out", MANIFEST)
run(SCRIPTS / "rs_artboard.py", "--root", ROOT, MANIFEST, "--export")
run(SCRIPTS / "rs_artboard.py", "--root", ROOT, MANIFEST, "--apply",
    ROOT / {timeline_dir} / "project.json")
run(SCRIPTS / "rs_run.py", "--root", ROOT, "--from", "S4", "--force")
'''


def init_rebuild(root: Path) -> list[str]:
    """在每个阶段文件夹里放一个 rebuild.py(薄壳),用户只需知道"改哪点哪"。

    目录名经 rs_paths 按工程解析(ADR-0046):旧结构工程把脚本种进旧目录、
    模板里也回填旧相对路径,保证生成的脚本在该工程上可直接运行。
    """
    runner = Path(__file__).resolve()
    made = []
    names = {k: rs_paths.resolve_name(root, k) for k in ("brief", "materials", "assets",
                                                         "cut", "timeline", "output", "state")}
    fmt = {"cut": names["cut"], "timeline": names["timeline"], "output": names["output"]}
    for key, sub, sid, label in INIT_MAP:
        d = rs_paths.resolve(root, key) / sub
        if not d.is_dir() and key != "output":
            continue
        d.mkdir(parents=True, exist_ok=True)
        if key == "assets" and sub == "artboard":
            body = ARTBOARD_REBUILD_TMPL.format(scripts=SCRIPTS_DIR,
                                                assets_dir=repr(names["assets"]),
                                                timeline_dir=repr(names["timeline"]))
        else:
            body = REBUILD_TMPL.format(label=label, folder=d.name, sid=sid, runner=str(runner),
                                       depth=1, state=names["state"],
                                       extra_note=REBUILD_EXTRA_NOTES.get(key, "").format(**fmt))
        atomic_write_text(d / "rebuild.py", body)
        made.append(f"{d.relative_to(root).as_posix()}/rebuild.py")
    body = REBUILD_TMPL.format(label="全量重建", folder="工程根", sid="S0", runner=str(runner),
                               depth=0, extra_note="", state=names["state"])
    atomic_write_text(root / "rebuild.py", body)
    made.append("rebuild.py")
    readme = ["# 改了东西怎么办?", "",
              "| 你改了什么 | 运行哪个脚本 |", "|---|---|",
              f"| 字幕({fmt['output']}/subtitles.ass) | `python {fmt['output']}/rebuild.py` |",
              f"| IR 或 wordline({fmt['timeline']}/) | `python {fmt['timeline']}/rebuild.py` |",
              f"| 粗剪决策 action 改动({fmt['cut']}/cutlist.json) | `python <scripts>/rs_cut.py --apply {fmt['cut']}/cutlist.json`(重算 keep;**不要**重跑 S2 detect——会冲掉 action 编辑) |",
              f"| artboard 卡片({names['assets']}/artboard/) | `python {names['assets']}/artboard/rebuild.py` |",
              "| 拿不准 | `python rebuild.py`(全量) |", "",
              "每个脚本都会**先备份**再重跑,跑砸了可以 `--rollback` 还原。"]
    atomic_write_text(root / "REBUILD.md", "\n".join(readme) + "\n")
    made.append("REBUILD.md")
    return made


# ---------------------------------------------------------------- 验证

def run_verify(root: Path, level: str) -> tuple[bool, str, dict]:
    """按状态决定跑 L0 还是 L0+L1。**输出必须带 verifyLevel / firstCheckDone。**"""
    cmd = [sys.executable, str(SCRIPTS_DIR / "rs_verify.py"), str(root)]
    if level == "L1":
        cmd += ["--level", "L1"]
    # R25(v2 M11):子进程此前无超时 —— --auto 可能永久挂起(其余阶段都走
    # _run_subprocess + stage_timeout)。L1 抽帧留证耗时更长,给 2× 阶段超时。
    _v_timeout = int(__import__("os").environ.get("CUTFLOW_VERIFY_TIMEOUT",
                                                  "3600" if level == "L1" else "1800"))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=_v_timeout)
    except subprocess.TimeoutExpired:
        return False, f"自检超时(>{_v_timeout}s,可配 CUTFLOW_VERIFY_TIMEOUT)", {}
    try:
        doc = json.loads((p.stdout or "").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return False, f"自检脚本无有效输出:{(p.stderr or p.stdout or '')[-200:]}", {}
    data = doc.get("data") or {}
    return bool(doc.get("ok")), doc.get("message", ""), data


def verify_policy(root: Path) -> tuple[str, str]:
    """返回 (该跑的级别, 原因)。"""
    vp = rs_paths.verify_json(root)
    first_done = False
    if vp.is_file():
        try:
            first_done = bool((json.loads(vp.read_text(encoding="utf-8"))
                               .get("firstCheck") or {}).get("done"))
        except json.JSONDecodeError:
            pass
    if not first_done:
        return "L1", "首次检查(未做过全量验证)"
    # 只有"曾经做过、现在失效"才算画面变更;从未记录状态(没跑过)不算
    changed = []
    for sid in ("S3", "S4", "S5"):
        if read_state(root, sid) is None:
            continue
        st = next(s for s in spec(root) if s["id"] == sid)
        if evaluate(root, st)["status"] != "done":
            changed.append(sid)
    if changed:
        return "L1", f"画面相关阶段失效:{','.join(changed)}"
    return "L0", "非首次且画面未变(改字幕/文案只跑机械自检)"


# ---------------------------------------------------------------- 执行

# P24-1:阶段子进程限时 —— 卡住的 ffmpeg/ASR 曾让 rs_run 永久挂起。
# 默认 3600s;长 ASR 阶段(S1)按阶段放宽;环境变量 CUTFLOW_STAGE_TIMEOUT_SEC 可统一调大。
# 超时 = 阶段明确失败(记 failed 状态、非零退出),绝不静默。
DEFAULT_STAGE_TIMEOUT_SEC = 3600
STAGE_TIMEOUTS = {"S1": 4 * 3600}


def stage_timeout(st: dict) -> int:
    env = os.environ.get("CUTFLOW_STAGE_TIMEOUT_SEC", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    return int(STAGE_TIMEOUTS.get(st["id"], DEFAULT_STAGE_TIMEOUT_SEC))


def _run_subprocess(cmd: list[str], root: Path, st: dict) -> tuple[subprocess.CompletedProcess | None, str]:
    """P24-1:阶段子进程统一 UTF-8 解码 + 限时;超时返回明确错误(不静默)。"""
    limit = stage_timeout(st)
    try:
        return subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=limit), ""
    except subprocess.TimeoutExpired as exc:
        tail = exc.stderr or exc.stdout or ""
        if isinstance(tail, (bytes, bytearray)):
            tail = bytes(tail).decode("utf-8", "replace")
        return None, (f"{st['id']} 超时({limit}s 限时;长任务用 CUTFLOW_STAGE_TIMEOUT_SEC "
                      f"或阶段注册表 STAGE_TIMEOUTS 调大):{str(tail)[-200:]}")


def _record_failed(root: Path, st: dict, msg: str, info: dict) -> None:
    """P25-1:阶段失败落 failed 状态(--status 可见、下游 blocked)。
    只读降级时写不进去也要带上 stateSkipped,不许静默丢账。"""
    skip = write_state(root, st["id"], {"status": "failed", "error": msg[-300:],
                                        "ts": datetime.now(CST).isoformat(timespec="seconds")})
    if skip:
        info["stateSkipped"] = skip

# S1 的 --media 候选:可抽音频的容器。01_原始素材 里还躺着 manifest.json/MANIFEST.md(S0 产物)
# 与图片素材,且 Windows 下 Path 排序大小写不敏感(manifest.json 会排在 MANIFEST.md 之前),
# 不过滤会把 manifest 喂给 ffmpeg(v0.10 店群工程实测 S1 必崩)。
ASR_MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".ts", ".flv",
                  ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}


def pick_asr_media(mats: list[Path]) -> Path | None:
    return next((p for p in mats if p.suffix.lower() in ASR_MEDIA_EXTS), None)


def _final_videos(root: Path) -> list[Path]:
    """S8 成片池:P10b-1 起新账落 成片输出/final/;旧工程顶层遗留的 final_*.mp4
    不追改历史,兼容读取。按 mtime 取「最新成片」—— 变体名(final_*_916_logoA.mp4)
    字典序与产出顺序无关,按名取会把 Logo 变体误当最新主成片。"""
    return rs_paths.final_videos(root)


def _max_chars_for(root: Path) -> int:
    """P12-1:生效的每卡字数 —— params.maxChars 支持按画幅字典或 brief 声明的全局整数。"""
    params = params_of(root)
    ratio = params.get("ratio") if params.get("ratio") in segmentation.MAX_CHARS else "9x16"
    mc = params.get("maxChars")
    v = mc.get(ratio) if isinstance(mc, dict) else mc
    if not isinstance(v, int) or not 4 <= v <= 40:
        v = segmentation.MAX_CHARS[ratio]
    return v


def _sub_style_and_ratio(root: Path) -> tuple[str, str]:
    """阶段四 N1:生效的字幕样式与画幅(brief/平台预设 → S3/S7/S8 的 {sub_style}/{ratio})。

    优先级:brief 显式声明 > 平台预设(templates/platforms.json)> 旧字面缺省
    (talkshow-bold / 9x16)。未声明任何参数时返回值与 v0.20 之前的字面命令完全一致
    —— 意图编译层是「前置补层」,不改既有工程的行为。
    """
    params = params_of(root)
    preset = load_platforms().get(str(params.get("platform") or ""), {})
    ratio = params.get("ratio")
    if ratio not in RATIOS:
        ratio = preset.get("ratio") if preset.get("ratio") in RATIOS else "9x16"
    style = params.get("subStyle")
    if style not in STYLES:
        style = preset.get("style") if preset.get("style") in STYLES else "talkshow-bold"
    return str(style), str(ratio)


def _token_mapping(root: Path) -> dict:
    """st["cmd"] 占位符 → 实际值(build_cmd / build_cmd_from_argv 共用,防两份漂移)。"""
    mats = expand(root, [rs_paths.rel(root, "materials", "*")])
    media = pick_asr_media(mats)
    finals = _final_videos(root)
    # S9 对账必须用**成片空间**的 wordline(remap 产物,rs_verify 同一约定);
    # wordline.json 始终是源空间 —— 拿它对账时长必然差一个粗剪裁剪量。
    fw = rs_paths.resolve(root, "timeline") / "wordline.final.json"
    final_wl = str(fw.relative_to(root)) if fw.is_file() else rs_paths.rel(root, "timeline", "wordline.json")
    sub_style, ratio = _sub_style_and_ratio(root)
    return {"{first_material}": str(media.relative_to(root)) if media
            else rs_paths.rel(root, "materials") + "/",
            "{slug}": root.name,
            "{final_wordline}": final_wl,
            "{final_video}": str(finals[-1].relative_to(root)) if finals
            else rs_paths.rel(root, "output", "final", "final_latest.mp4"),
            "{max_chars}": str(_max_chars_for(root)),
            "{sub_style}": sub_style,
            "{ratio}": ratio}


def build_cmd_from_argv(root: Path, argv: list[str]) -> list[str] | None:
    """argv 形如 [script, tok...] —— 与 st["cmd"] 同构,套同一映射规则。"""
    mapping = _token_mapping(root)
    out = [sys.executable, str(SCRIPTS_DIR / argv[0])]
    for tok in argv[1:]:
        out.append(mapping.get(tok, tok))
    return out


def build_cmd(root: Path, st: dict) -> list[str] | None:
    if not st.get("cmd"):
        return None
    mapping = _token_mapping(root)
    out = [sys.executable, str(SCRIPTS_DIR / st["cmd"][0])]
    for tok in st["cmd"][1:]:
        out.append(mapping.get(tok, tok))
    return out


def record_stage_done(root: Path, st: dict) -> str | None:
    """按当前盘面给阶段落 done 账(run_stage 成功尾与 --auto S2 后置步骤共用)。"""
    parts = stage_parts(root, st, params_of(root), external_versions())
    return write_state(root, st["id"], {"status": "done", "key": key_of(parts), "parts": parts,
                                        "outHash": outputs_hash(root, st),
                                        "ts": datetime.now(CST).isoformat(timespec="seconds")})


def run_stage(root: Path, st: dict, info: dict | None = None) -> tuple[bool, str]:
    """跑一个非 cached 阶段。

    P13-1:备份从「只覆盖 --force 起点」移进这里 —— 每个真正写盘的非 cached 阶段
    (含 --dirty 级联里被判 stale 的)跑之前都先备份将覆盖的产物,"唯一保险"保全程。
    P24-1:子进程统一 UTF-8 解码 + 按 stage_timeout(st) 限时,超时即明确失败。
    P25-1:任何失败路径都落 failed 状态(--status 可见、下游 blocked)。
    info(可选)带回:backup=备份目录;pruneFailed=旧备份清理失败项(P13-2);
    stateSkipped=只读降级原因(O7-2)。
    """
    if info is None:
        info = {}
    cmd = build_cmd(root, st)
    if cmd is None:
        return False, f"{st['id']} 是人工阶段(Agent 介入),完成后用 --mark {st['id']}"
    berr: list[str] = []
    try:
        bp = backup_paths(root, st, errors=berr)
    except OSError as exc:
        # 备份是覆盖前唯一的保险 —— 备不下就中止,绝不无保险覆盖
        return False, f"{st['id']} 备份失败,已中止(不无保险覆盖产物):{exc}"
    info["backup"] = str(bp) if bp else None
    if berr:
        # P13-2:旧备份清理失败不阻断本次运行,但必须如实上报、最终非零退出
        info["pruneFailed"] = berr
    p, timeout_err = _run_subprocess(cmd, root, st)
    if timeout_err:
        _record_failed(root, st, timeout_err, info)
        return False, timeout_err
    if p.returncode != 0:
        msg = f"{st['id']} 失败(exit {p.returncode}):{(p.stderr or p.stdout or '')[-300:]}"
        _record_failed(root, st, msg, info)
        return False, msg
    if st.get("post"):
        # v0.13:主命令成功后的附加步骤(如 S1 的能量校准);任一失败即阶段失败。
        # st["post"] 与 st["cmd"] 同构(首元素是脚本名)——BUGREPORT P9:
        # 此前置过 st["cmd"][0],曾拼出 "rs_align.py rs_align.py calibrate" 令 S1 永远失败。
        post_cmd = build_cmd_from_argv(root, st["post"])
        if post_cmd is not None:
            pp, post_timeout = _run_subprocess(post_cmd, root, st)
            if post_timeout:
                _record_failed(root, st, post_timeout, info)
                return False, post_timeout
            if pp.returncode != 0:
                msg = f"{st['id']} post 步骤失败:{(pp.stderr or pp.stdout or '')[-300:]}"
                _record_failed(root, st, msg, info)
                return False, msg
    # ADR-0047:阶段自有命令成功后,通用挂载器挂载声明在该阶段的能力
    # (detector 执行/产物校验/降级留痕;degrade.to=none 缺失 → 阶段失败)
    ok_caps, caps_msg = run_stage_capabilities(root, st, info)
    if not ok_caps:
        _record_failed(root, st, caps_msg, info)
        return False, caps_msg
    skip = record_stage_done(root, st)
    if skip:
        info["stateSkipped"] = skip
        return True, f"{st['id']} ✓ {st['name']}(⚠ 状态未写盘:只读降级)"
    if caps_msg:
        return True, f"{st['id']} ✓ {st['name']}(⚠ {caps_msg})"
    return True, f"{st['id']} ✓ {st['name']}"


# ---------------------------------------------------------------- 无人值守(--auto,N3)

# --auto 的 S2 后置:保守落盘(全部复用既有命令,不改 rs_cut/rs_align 逻辑)
#   ① --apply:review 刀一律保留(宁可漏删),keep 重算 + 时长账同步(P27-1);
#   ② remap:粗剪后出成片空间 wordline.final.json(S7 出字幕 / S9 对账同一约定)。
# 路径经 rs_paths 按工程解析(ADR-0046)。
def _auto_s2_posts(root: Path) -> list[list[str]]:
    cut = rs_paths.resolve_name(root, "cut")
    tl = rs_paths.resolve_name(root, "timeline")
    return [
        ["rs_cut.py", "--apply", f"{cut}/cutlist.json"],
        ["rs_align.py", "remap", f"{tl}/wordline.json", "--cutlist", f"{cut}/cutlist.applied.json",
         "--out", f"{tl}/wordline.final.json"],
    ]


def run_auto_s2_posts(root: Path, st: dict, info: dict) -> tuple[bool, str]:
    """--auto:S2 检测后的自动裁决 —— apply(保守保留 review)+ remap,决策留痕。

    记账注意:apply 会同步 wordline 时长账,S2 的输入 hash 随之变化 —— 后置跑完
    必须重落一次 S2 的账(record_stage_done),否则重跑永远不收敛。
    """
    review_n = 0
    cl_path = rs_paths.resolve(root, "cut") / "cutlist.json"
    if cl_path.is_file():
        try:
            cl = json.loads(cl_path.read_text(encoding="utf-8"))
            review_n = sum(1 for c in cl.get("cuts", []) if c.get("action") == "review")
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
    for argv in _auto_s2_posts(root):
        cmd = build_cmd_from_argv(root, argv)
        if cmd is None:
            return False, f"{st['id']} auto 后置命令构建失败:{argv[0]}"
        p, terr = _run_subprocess(cmd, root, st)
        if terr:
            _record_failed(root, st, terr, info)
            return False, terr
        if p.returncode != 0:
            msg = f"{st['id']} auto 后置失败({argv[0]}):{(p.stderr or p.stdout or '')[-300:]}"
            _record_failed(root, st, msg, info)
            return False, msg
    log_decision(root, auto_decision(
        "S2", "review-keep", f"{review_n} 刀 review 保守保留(不自动删)",
        "粗剪铁律「宁可漏删不可错删」;guard 不过的刀在无人值守下一律不删,留待人工",
        reviewCount=review_n))
    log_decision(root, auto_decision(
        "S2", "auto-apply", "cutlist 已自动 --apply(keep 重算 + 时长账同步)",
        "无人值守下按保守口径落盘;可 rs_run.py --rollback 回退"))
    log_decision(root, auto_decision(
        "S2", "remap", "已 remap 出成片空间 wordline.final.json",
        "粗剪改了时间轴;S7 出字幕与 S9 对账都必须用 final 空间(map_src_to_final 纪律)"))
    record_stage_done(root, st)
    return True, f"{st['id']} auto 后置完成(apply + remap)"


def refresh_stage_outhash(root: Path, sid: str, kind: str, why: str) -> None:
    """--auto:上游产物被后置步骤合法改写时按当前盘面重记其账(否则 outHash/输入
    hash 护栏判 stale,触发不必要的重跑,如 S1 重转写)。重记必须留痕 —— 账不许
    静默改;带外改写(编辑器/人手)不走这里,依旧判 stale。"""
    rec = read_state(root, sid)
    if rec is None or rec.get("status") != "done":
        return
    st = next(s for s in spec(root) if s["id"] == sid)
    parts = stage_parts(root, st, params_of(root), external_versions())
    new_key, new_hash = key_of(parts), outputs_hash(root, st)
    if rec.get("key") == new_key and rec.get("outHash") == new_hash:
        return
    skip = record_stage_done(root, st)
    if not skip:
        log_decision(root, auto_decision(sid, kind, f"{sid} 账面已按当前产物重记(key/outHash 刷新)", why))


def run_manual_auto(root: Path, st: dict, info: dict | None = None) -> tuple[bool, str]:
    """--auto:人工阶段(S0/S4/S11)的自动处置,决策全部留痕。

    S0  注册 cmd 是机械命令(素材摄取)→ 照常执行,不问人;
    S4  无卡片计划(00_制作简报/cards.json 缺)→ 标记「无事可做」;有计划 → 走
        artboard 三连(gen-cards --force / --export / --apply,与卡片 rebuild 同链),
        文案内容是 Agent 语义产物,--auto 不生成、只保证模板/导出/安全区;
    S11 跑交付对账并生成决策说明书(成片输出/决策说明书.md);缺项凡属 Agent 语义
        产物(封面/占位文案)如实留痕不代劳,其余缺项 = 失败(成片/字幕等早已失败)。
    """
    if info is None:
        info = {}
    sid = st["id"]
    if sid == "S4" and not (rs_paths.resolve(root, "brief") / "cards.json").is_file():
        skip = record_stage_done(root, st)
        write_rec = read_state(root, sid) or {}
        write_rec["manual"] = True
        if not skip:
            atomic_write_text(state_path(root, sid), json.dumps(write_rec, ensure_ascii=False, indent=1))
        msg = (f"S4 无卡片计划({rs_paths.p('brief')}/cards.json 不存在),"
               "--auto 标记无事可做")
        log_decision(root, auto_decision(sid, "no-cards", msg,
                                         "卡片文案是 Agent 语义产物,--auto 不代劳"))
        return True, msg
    if sid == "S4":
        manifest = rs_paths.rel(root, "assets", "artboard", "manifest.json")
        ir = rs_paths.rel(root, "timeline", "project.json")
        chain = [["rs_artboard.py", "gen-cards", "--from",
                  rs_paths.rel(root, "brief", "cards.json"), "--force"],
                 ["rs_artboard.py", manifest, "--export"],
                 ["rs_artboard.py", manifest, "--apply", ir]]
        for argv in chain:
            cmd = build_cmd_from_argv(root, argv)
            if cmd is None:
                msg = f"S4 artboard 命令构建失败:{argv[0]}"
                _record_failed(root, st, msg, info)
                return False, msg
            p, terr = _run_subprocess(cmd, root, st)
            if terr:
                _record_failed(root, st, terr, info)
                return False, terr
            if p.returncode != 0:
                msg = f"S4 artboard 步骤失败({argv[0]}):{(p.stderr or p.stdout or '')[-300:]}"
                _record_failed(root, st, msg, info)
                return False, msg
        skip = record_stage_done(root, st)
        write_rec = read_state(root, sid) or {}
        write_rec["manual"] = True
        if not skip:
            atomic_write_text(state_path(root, sid), json.dumps(write_rec, ensure_ascii=False, indent=1))
        msg = "S4 卡片三连完成(gen-cards --force / --export / --apply)"
        log_decision(root, auto_decision(sid, "cards-chain", msg,
                                         "brief 声明了卡片计划;模板/导出/安全区由脚本保证"))
        return True, msg
    if sid == "S11":
        cmd = [sys.executable, str(SCRIPTS_DIR / "rs_ingest.py"), "deliverables", str(root)]
        p, terr = _run_subprocess(cmd, root, st)
        if terr:
            _record_failed(root, st, terr, info)
            return False, terr
        try:
            doc = json.loads((p.stdout or "").strip().splitlines()[-1])
            data = doc.get("data") or {}
        except (json.JSONDecodeError, IndexError):
            msg = f"S11 交付对账无有效输出:{(p.stderr or p.stdout or '')[-200:]}"
            _record_failed(root, st, msg, info)
            return False, msg
        missing = [str(m) for m in (data.get("missing") or [])]
        agent_missing = [m for m in missing if COVER_PNG in m]     # 封面=Agent/用户侧产物
        hard_missing = [m for m in missing if COVER_PNG not in m]
        if hard_missing or p.returncode not in (0, 4):
            msg = f"S11 交付对账缺硬项:{'、'.join(hard_missing) or doc.get('message', '')[:160]}"
            _record_failed(root, st, msg, info)
            return False, msg
        skip = record_stage_done(root, st)
        write_rec = read_state(root, sid) or {}
        write_rec["manual"] = True
        if not skip:
            atomic_write_text(state_path(root, sid), json.dumps(write_rec, ensure_ascii=False, indent=1))
        what = "交付对账完成" + (f";缺 {len(missing)} 项:{'、'.join(missing[:3])}" if missing else "(清单齐全)")
        log_decision(root, auto_decision(
            sid, "deliverables", what,
            "封面/占位文案是 Agent 语义产物,--auto 不代劳、如实留痕" if agent_missing
            else "交付对账全部齐备"))
        info["deliverablesMissing"] = missing
        return True, f"S11 ✓ 交付({what})"
    # S0:注册 cmd 本就是机械命令 → 通用执行路径
    return run_stage(root, st, info)


def auto_skip_reason(root: Path, st: dict) -> str | None:
    """--auto 的阶段前置检查:缺声明宁可漏做不猜(留痕跳过,不装作无事)。"""
    if st["id"] == "S5" and not (rs_paths.resolve(root, "timeline") / "variants.json").is_file():
        return (f"工程未声明品牌变体({rs_paths.p('timeline')}/variants.json 不存在):"
                "S5 无事可做;需要 Logo 变体时先 rs_brand.py --expand 再重跑")
    return None


def bench_evidence(root: Path) -> str | None:
    """--auto:L1 目测降级为「抽帧留证」—— rs_bench 网格图,文件名即标注
    「L1 未人工确认」;只作证据,不作判定,失败不阻断(留证尽力而为)。"""
    finals = _final_videos(root)
    if not finals:
        return None
    out = rs_paths.resolve(root, "output") / "L1未人工确认_抽帧留证.png"
    cmd = [sys.executable, str(SCRIPTS_DIR / "rs_bench.py"), str(finals[-1]),
           "--ir", str(rs_paths.project_json(root)), "--out", str(out)]
    try:
        p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log_decision(root, auto_decision("verify", "bench", f"抽帧留证失败:{str(exc)[:120]}",
                                         "留证尽力而为,不阻断交付"))
        return None
    if p.returncode != 0 or not out.is_file():
        log_decision(root, auto_decision(
            "verify", "bench", f"抽帧留证失败(exit {p.returncode}):{(p.stderr or p.stdout or '')[-120:]}",
            "留证尽力而为,不阻断交付"))
        return None
    log_decision(root, auto_decision("verify", "bench", f"抽帧留证 → {out.name}",
                                     "网格图仅作证据不构成目测判定;L1 未人工确认",
                                     evidence=out.name))
    return out.name


def external_versions() -> dict:
    """外部服务版本(缓存键的一部分,防模型/服务升级后误命中缓存)。"""
    ext = {}
    try:
        cfg = json.loads((SCRIPTS_DIR.parents[2] / "config.json").read_text(encoding="utf-8"))
        ext["asr_model"] = cfg.get("asr", {}).get("model", "")
        ext["asr_url"] = cfg.get("asr", {}).get("url", "")
    except Exception:  # noqa: BLE001
        pass
    ext["detector"] = "cutflow-1.0"
    return ext


def select(root: Path, mode: str, target: str | None) -> list[dict]:
    stages = spec(root)
    ids = [s["id"] for s in stages]
    if mode == ONLY:
        return [s for s in stages if s["id"] == target]
    if mode == FROM:
        if target not in ids:
            return []
        return stages[ids.index(target):]
    # dirty(P25-1):上游 failed 的下游本轮 blocked 不选 —— 先让失败的上游重跑,
    # 它成功后下轮收敛循环自然会把下游选进来;避免拿坏/缺的上游产物硬跑下游。
    failed_ids = [s["id"] for s in stages if evaluate(root, s)["status"] == "failed"]
    out = []
    for st in stages:
        r = evaluate(root, st)
        if r["status"] == "done":
            continue
        if any(ids.index(f) < ids.index(st["id"]) for f in failed_ids):
            continue
        out.append(st)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--from", dest="from_stage")
    ap.add_argument("--only", dest="only_stage")
    ap.add_argument("--dirty", action="store_true")
    ap.add_argument("--explain")
    ap.add_argument("--mark")
    ap.add_argument("--plan", action="store_true", help="只打印将执行的命令")
    ap.add_argument("--force", action="store_true",
                    help="强制重跑 --from/--only 指定的起点阶段(上游仍走缓存);"
                         "必须搭配 --from/--only,单独使用报错(P13-1)")
    ap.add_argument("--init", action="store_true", help="在阶段文件夹生成 rebuild.py")
    ap.add_argument("--rollback", action="store_true", help="还原最近一次备份")
    ap.add_argument("--at", default="", help="--rollback 指定备份时间戳")
    ap.add_argument("--verify", action="store_true", help="按策略跑 L0 / L1 自检")
    ap.add_argument("--verify-full", dest="verify_full", action="store_true",
                    help="强制跑 L1(含待目测清单)")
    ap.add_argument("--auto", action="store_true",
                    help="无人值守(阶段四 N3):人工阶段自动执行/标记,CHECKS 自动决策+留痕"
                         "(decision_log);L1 降级抽帧留证;L0 硬闸不放松;L2 验收归用户")
    a = ap.parse_args()

    root = Path(a.root).resolve()

    if a.init:
        if not root.is_dir():
            return emit(False, "NO_PROJECT", f"工程目录不存在:{root}", exit_code=2)
        made = init_rebuild(root)
        return emit(True, "INIT_OK", f"已生成 {len(made)} 个一键重建脚本", {"files": made})

    if a.rollback:
        ok, msg = rollback(root, a.at)
        return emit(ok, "ROLLBACK_OK" if ok else "ROLLBACK_FAIL", msg,
                    exit_code=0 if ok else 2)

    if a.verify or a.verify_full:
        level, why = verify_policy(root)
        if a.verify_full:
            level, why = "L1", "显式要求全量(L1)"
        ok, msg, data = run_verify(root, level)
        payload = {"verifyLevel": level, "firstCheckDone": data.get("firstCheckDone"),
                   "reason": why, "l0Pass": data.get("pass"),
                   "failed": data.get("failed") or [], "report": data.get("report")}
        if level == "L1":
            payload["l1"] = data.get("l1")
            payload["needsAgentReview"] = True
        return emit(ok, "VERIFY_OK" if ok else "VERIFY_FAIL",
                    f"[{level}] {msg} —— 原因:{why}", payload,
                    exit_code=0 if ok else 4)

    if a.explain:
        return cmd_explain(root, a.explain)
    if a.mark:
        st = next((s for s in spec(root) if s["id"] == a.mark), None)
        if not st:
            return emit(False, "BAD_STAGE", f"未知阶段:{a.mark}", exit_code=2)
        parts = stage_parts(root, st, params_of(root), external_versions())
        skip = write_state(root, a.mark, {"status": "done", "key": key_of(parts), "parts": parts,
                                          "outHash": outputs_hash(root, st),
                                          "manual": True,
                                          "ts": datetime.now(CST).isoformat(timespec="seconds")})
        if skip:
            # O7-2:标记写不进去必须响(否则"做没做过"又没人知道了)
            return emit(False, "STATE_READONLY", skip, {"stage": a.mark}, exit_code=4)
        return emit(True, "MARK_OK", f"{a.mark} 已标记完成", {"stage": a.mark})
    if a.status or not (a.from_stage or a.only_stage or a.dirty or a.auto):
        return cmd_status(root)

    mode, target = (FROM, a.from_stage) if a.from_stage else \
                   ((ONLY, a.only_stage) if a.only_stage else (S13, None))
    # P13-1:--force 没有 --from/--only target 时曾静默 no-op —— 现在显式拒绝
    if a.force and target is None:
        return emit(False, "FORCE_NEEDS_TARGET",
                    "--force 需要搭配 --from <Sx> 或 --only <Sx>(单独使用只会静默失效);"
                    "全量强制重建请用 --from S0 --force", exit_code=2)
    todo = select(root, mode, target)
    if not todo:
        return emit(False, "BAD_STAGE", f"未知阶段:{target}", exit_code=2)

    if a.plan:
        plan = [{"stage": s["id"], "name": s["name"],
                 "cmd": " ".join(build_cmd(root, s) or ["<人工阶段>"])} for s in todo]
        for item in plan:
            print(f"{item['stage']} {item['name']}: {item['cmd']}")
        return emit(True, "PLAN_OK", f"{len(plan)} 个阶段待执行", {"plan": plan})

    forced_id = target if a.force else None
    t0 = time.time()
    results: list[dict] = []
    accounted: set[str] = set()
    prune_failed: list[str] = []
    deliverables_missing: list[str] = []
    auto = bool(a.auto)
    if auto:
        # N4:意图编译决策(00_制作简报/intent_decisions.json)先并入 decision_log,
        # 运行时自动决策随后追加 —— 「每个参数从哪句话推出来」全程可审计。
        seed_intent_decisions(root)
    # 收敛式 --dirty(P13-1/§4.1 验收):上游重跑会把下游打脏(如 S7 改字 → S8 输入变),
    # 只在起点选一次会漏掉"运行中途变脏"的下游,收敛要拖到下一次调用。
    # 这里逐轮重选直到没有非 done 阶段;--from/--only 单轮即可(列表覆盖全部后续阶段,
    # 级联由循环内 evaluate 驱动)。
    rounds = len(spec(root)) + 1 if mode == S13 else 1
    for _ in range(rounds):
        todo = select(root, mode, target)
        if mode == S13:
            todo = [s for s in todo if s["id"] not in accounted]
        for st in todo:
            if st["id"] in accounted:
                continue
            if st.get("manual"):
                accounted.add(st["id"])
                if not auto:
                    results.append({"stage": st["id"], "ok": True, "skipped": "人工阶段"})
                    continue
                # N3:无人值守 —— 人工阶段按声明处置(执行/标记/对账),决策留痕
                info_m: dict = {}
                ok_m, msg_m = run_manual_auto(root, st, info_m)
                entry_m = {"stage": st["id"], "ok": ok_m, "message": msg_m, "manualAuto": True}
                if info_m.get("deliverablesMissing") is not None:
                    deliverables_missing = info_m["deliverablesMissing"]
                    entry_m["missing"] = deliverables_missing
                if info_m.get("stateSkipped"):
                    entry_m["stateReadOnly"] = info_m["stateSkipped"]
                results.append(entry_m)
                if not ok_m:
                    payload = {"results": results, "elapsedSec": round(time.time() - t0, 2)}
                    if prune_failed:
                        payload["pruneFailed"] = prune_failed
                    return emit(False, "STAGE_FAILED", msg_m, payload, exit_code=4)
                continue
            if auto and (skip_why := auto_skip_reason(root, st)):
                accounted.add(st["id"])
                log_decision(root, auto_decision(st["id"], "skip", skip_why,
                                                 "缺前置声明宁可漏做不猜;状态保持 missing,如实亮灯"))
                results.append({"stage": st["id"], "ok": True, "skipped": skip_why})
                continue
            r = evaluate(root, st)
            forced = (forced_id is not None and st["id"] == forced_id)
            if r["status"] == "done" and not forced:
                if mode == ONLY:
                    # P14-1:--only 命中已 done 必须报错 —— 静默 cached 会吞掉带外改动
                    #(rules/incremental.md §4:要重跑已完成的阶段,显式补 --force)
                    return emit(False, "PRECONDITION_FAILED",
                                f"{st['id']} 已是 done,--only 不重跑已完成的阶段;"
                                "确认要强制重跑请加 --force",
                                {"stage": st["id"], "status": r["status"]}, exit_code=2)
                accounted.add(st["id"])
                results.append({"stage": st["id"], "ok": True, "cached": True})
                continue
            accounted.add(st["id"])
            info: dict = {}
            ok, msg = run_stage(root, st, info)
            if ok and auto and st["id"] == "S2":
                # N3:粗剪自动裁决 —— review 保守保留 + apply + remap;账面刷新留痕
                pok, pmsg = run_auto_s2_posts(root, st, info)
                if not pok:
                    payload = {"results": results, "elapsedSec": round(time.time() - t0, 2)}
                    if prune_failed:
                        payload["pruneFailed"] = prune_failed
                    return emit(False, "STAGE_FAILED", pmsg, payload, exit_code=4)
                msg += ";" + pmsg
                refresh_stage_outhash(
                    root, "S1", "outHash-refresh",
                    "S2 --apply 只同步 wordline 时长账三字段(P27-1),转写字符时间未动,"
                    "刷新 outHash 避免无谓重转写")
            prune_failed += info.get("pruneFailed") or []
            entry = {"stage": st["id"], "ok": ok, "message": msg, "forced": forced,
                     "backup": info.get("backup")}
            if info.get("stateSkipped"):
                entry["stateReadOnly"] = info["stateSkipped"]
            results.append(entry)
            if not ok:
                payload = {"results": results, "elapsedSec": round(time.time() - t0, 2)}
                if prune_failed:
                    payload["pruneFailed"] = prune_failed
                return emit(False, "STAGE_FAILED", msg, payload, exit_code=4)

    elapsed = round(time.time() - t0, 2)
    hit = sum(1 for r in results if r.get("cached"))
    bench_name = None
    if auto:
        # N3:断句歧义自动裁决留痕(候选清单 rs_subtitle 本就落盘,事后可人工复核)
        cand = rs_paths.resolve(root, "output") / "segments_candidates.json"
        amb = 0
        if cand.is_file():
            try:
                doc_c = json.loads(cand.read_text(encoding="utf-8"))
                # rs_subtitle 落盘的是候选 list(每条带 ambiguous 旗标);兼容 dict 计数
                if isinstance(doc_c, dict):
                    amb = int(doc_c.get("ambiguous") or 0)
                elif isinstance(doc_c, list):
                    amb = sum(1 for c in doc_c if isinstance(c, dict) and c.get("ambiguous"))
            except (json.JSONDecodeError, OSError, ValueError, UnicodeDecodeError):
                amb = 0
        if amb:
            log_decision(root, auto_decision(
                "S7", "ambiguous-auto", f"{amb} 句切分歧义,自动取 DP 最优",
                "断句 DP + 硬约束由脚本兜底;候选见 "
                f"{rs_paths.p('output')}/segments_candidates.json,可人工复核",
                ambiguous=amb, candidates=cand.name))
        log_decision(root, auto_decision(
            "verify", "l1-degrade", "L1 目测降级为抽帧留证(不判定、不阻断)",
            "无人值守不代劳目测(rules/verify.md 归属铁律);L0 硬闸不放松;L2 验收始终归用户"))
        level = "L0"
        why = ("--auto 无人值守:L1 目测降级为抽帧留证(标注 L1 未人工确认),"
               "L0 硬闸不放松;L2 验收始终归用户")
        okv, vmsg, vdata = run_verify(root, level)
        bench_name = bench_evidence(root)
        log_decision(root, auto_decision(
            "verify", "l0", f"L0 机械自检{'通过' if okv else '未通过'}:{vmsg[:80]}",
            "L0 是唯一自动验收闸,无人值守也不放松"))
    else:
        level, why = verify_policy(root)
        okv, vmsg, vdata = run_verify(root, level)
    first_done = bool(vdata.get("firstCheckDone"))
    decision_count = len(load_decision_log(root)) if auto else 0
    msg = f"{len(results)} 阶段({hit} 命中缓存),耗时 {elapsed}s;[{level}] {vmsg}"
    if auto:
        msg += ";--auto 决策留痕 " + str(decision_count) + " 条(L2 验收归用户)"
    if level == "L1":
        msg += f"(需 Agent 目测;详见 {rs_paths.p('output')}/verify_report.md)"
    read_only = sorted({r["stateReadOnly"] for r in results if r.get("stateReadOnly")})
    if read_only:
        msg += f";⚠ 状态只读降级({read_only[0]})"
    if prune_failed:
        msg += f";⚠ {len(prune_failed)} 个旧备份目录删除失败(P13-2 如实上报)"
    payload = {"results": results, "cached": hit, "elapsedSec": elapsed,
               "verifyLevel": level, "firstCheckDone": first_done,
               "verifyReason": why,
               "report": vdata.get("report"), "failed": vdata.get("failed") or []}
    if auto:
        # N4:决策留痕计数与证据随运行结果带回;L2 验收归用户,不写 firstCheck
        payload["auto"] = True
        payload["decisionLogCount"] = decision_count
        payload["l2Note"] = ("L2 最终验收归用户(--auto 不代劳);首次 L1 目测未人工确认,"
                             f"抽帧留证在 {rs_paths.p('output')}/")
        if bench_name:
            payload["benchEvidence"] = bench_name
        if deliverables_missing:
            payload["deliverablesMissing"] = deliverables_missing
    if read_only:
        payload["stateReadOnly"] = read_only
    if prune_failed:
        payload["pruneFailed"] = prune_failed
    # P13-2:备份清理失败 → 整体非零退出(阶段本身可能都成功,但账不许假绿)
    if not okv:
        return emit(False, "RUN_VERIFY_FAIL", msg, payload, exit_code=4)
    if prune_failed:
        return emit(False, "RUN_PRUNE_FAILED", msg, payload, exit_code=4)
    return emit(True, "RUN_OK", msg, payload, exit_code=0)


if __name__ == "__main__":
    sys.exit(main())
