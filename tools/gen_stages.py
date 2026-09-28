"""stages.json 生成器(T2.10 阶段契约表,第二册 B 组)。

用法:
  python tools/gen_stages.py            # 再生 skills/cutflow/templates/stages.json
  python tools/gen_stages.py --check    # 只对拍:盘上文件与再生结果不一致即退出 2(门禁/CI)

一句话:**阶段契约不从手写来** —— 机械抽取三处既有真相,再拼上少量带来源注释的
人工补条目,重跑必须零 diff(硬门禁,tests/test_stages_contract.py):

  1. rs_run.spec()          → id/name/scripts/inputs/outputs/cmd/paramKeys/manual/marker
                              (阶段注册表本身就是唯一真相,绝不抄第二份);
  2. rs_verify.L0_CHECKS    → gates(L0 门禁名 + 消费产物所属阶段;
                              check→阶段映射为人工表,每条带来源注释);
  3. rs_run 超时常量         → timeoutPolicy.base(DEFAULT_STAGE_TIMEOUT_SEC /
                              STAGE_TIMEOUTS,机械搬运,零漂移)。

人工补条目(计划允许"抽不全的条目人工补,但必须带来源注释"):
  · 每阶段 degradePolicy / rerunPolicy —— 源自 rules/incremental.md §3/§4/§4.7
    (缓存键公式 / 命令语义 / --auto 决策表),HUMAN_POLICIES 表逐条注明;
  · 顶层 `policies` 辅助超时键(VERIFY/SEG/STEP/ENCODE/PROBE/ASR/GREEN/BENCH)——
    直调脚本(policy_timeout)与 rs_run 共用的非阶段超时口径,值 = v0.20 各脚本
    手抄常量的原值搬运(零行为漂移),env 名 = 既有 CUTFLOW_*_TIMEOUT。
    再生时人工段原样保留;若既有文件缺失则用本文件的内置缺省。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
STAGES_OUT = REPO / "skills" / "cutflow" / "templates" / "stages.json"

sys.path.insert(0, str(SCRIPTS))
import rs_run  # noqa: E402   — 阶段注册表 + 超时常量的唯一机械源
import rs_verify  # noqa: E402   — L0 门禁名的唯一机械源

# ---------------------------------------------------------------- 门禁映射(人工表,带来源)

# rs_verify.L0_CHECKS 的 check 函数 → 所属阶段。来源 = 各 check 消费的产物路径
# (rs_verify.py)+ rules/verify.md 判据表;新 check 落地必须在此登记,否则
# 本生成器直接报错(零静默:门禁没进契约表 = 等于没挂)。
GATE_STAGES: dict[str, tuple[str, str]] = {
    "check_greenscreen": ("S0", "幕布检测(rs_ingest 摄取判据;rules/intake.md)"),
    "check_copyright": ("S0", "版权登记占比判据(01_原始素材/copyright.json;rules/verify.md)"),
    "check_assets": ("S0", "素材四字段 source/license/commercial/attribution"),
    "check_matte": ("S0", "抠像质量门禁五指标(ADR-0050)"),
    "check_wordline": ("S1", "wordline 字段/时长账自洽(P27-2)"),
    "check_cutlist": ("S2", "cutlist 结构与 keep 账(S2 产物)"),
    "check_ir": ("S3", "IR schema/画幅/轨契约(project.json)"),
    "check_effects_usage": ("S3", "效果使用率九判据(消费 IR;rules/editing-grammar.md)"),
    "check_subtitles": ("S7", "字幕卡字数/CPS/时长(templates/subtitle-policy.json)"),
    "check_safe_area": ("S7", "ASS 安全区口径(9:16 底 25%/顶 12%)"),
    "check_safe_area_content": ("S8", "成片字幕内容安全区(T4.4 判据)"),
    "check_alignment": ("S9", "对齐偏移中位 ≤40ms(rules/align.md §6)"),
    "check_replay_remap": ("S9", "Wordline 推导链重放对账(独立对账·产物路,T5.6c)"),
    "check_subtitle_speech": ("S9", "字幕↔成片语音活动独立对账(独立对账·成片实测路,T5.6c)"),
    "check_content_gate": ("S9", "B10 内容闸三态回读(pass/fail/degraded 降级留痕,T5.6a)"),
    "check_qc": ("S9", "成片 QC:黑帧/冻结/VFR/响度 -14 LUFS"),
    "check_artifacts": ("S11", "交付清单齐套(成片/字幕/封面/文案/说明书)"),
}

# ---------------------------------------------------------------- 人工补:降级 / 重跑策略
# 来源:rules/incremental.md §3(缓存键)/§4(命令语义)/§4.7(--auto 决策表)。
# 再生时若盘上已有同阶段条目则原样保留(允许维护者微调措辞,值不许丢)。

HUMAN_STAGE_POLICIES: dict[str, dict] = {
    "S0": {
        "degradePolicy": {"action": "stop", "when": "素材缺失或幕布未预处理",
                          "note": "无素材不能往下;rs_ingest 幕布残留显式报错",
                          "source": "rules/intake.md"},
        "rerunPolicy": {"mode": "content-key", "force": "rs_run --force --from S0",
                        "source": "rules/incremental.md §3/§4"},
    },
    "S1": {
        "degradePolicy": {"action": "stop", "when": "转写/对齐失败或无可抽音频素材",
                          "note": "ASR 超时即明确失败落 failed 账(P24-1)",
                          "source": "rules/incremental.md §2"},
        "rerunPolicy": {"mode": "content-key", "force": "rs_run --force --from S1",
                        "source": "rules/incremental.md §3/§4"},
    },
    "S2": {
        "degradePolicy": {"action": "stop", "when": "检测失败;review 刀非 auto 一律停机问人",
                          "note": "--auto 下 review 刀保守保留并留痕(auto:S2:review-keep)",
                          "source": "rules/incremental.md §4.7"},
        "rerunPolicy": {"mode": "content-key", "force": "rs_run --force --from S2",
                        "note": "只改 cuts[].action 时改跑 rs_cut --apply,勿重跑 S2 detect",
                        "source": "rules/incremental.md §7 例外注"},
    },
    "S3": {
        "degradePolicy": {"action": "stop", "when": "IR 校验失败或检测到手注痕迹",
                          "note": "IR_MANUAL_EDITS 护栏拒绝覆盖手注,除非显式 --force",
                          "source": "rs_ir build --from-cutlist 护栏"},
        "rerunPolicy": {"mode": "content-key", "force": "rs_run --force --from S3",
                        "source": "rules/incremental.md §3/§4"},
    },
    "S4": {
        "degradePolicy": {"action": "skip", "when": "无卡片计划(00_制作简报/cards.json 缺)",
                          "note": "--auto 标记无事可做并留痕(auto:S4:no-cards);"
                                  "无 marker 的人工阶段一律判 missing,绝不借上游产物自动 done(P11-1)",
                          "source": "rules/incremental.md §2.5/§4.7"},
        "rerunPolicy": {"mode": "marker", "marker": "assets/artboard/manifest.json:appliedAt",
                        "force": "rs_artboard --export/--apply 后 rs_run --from S4 --force",
                        "source": "rules/incremental.md §2.5(P11-1)"},
    },
    "S5": {
        "degradePolicy": {"action": "skip", "when": "工程未声明品牌变体(timeline/variants.json 缺)",
                          "note": "缺声明宁可漏做不猜;状态保持 missing 如实亮灯(auto:S5:skip)",
                          "source": "rules/incremental.md §4.7"},
        "rerunPolicy": {"mode": "content-key", "force": "rs_run --force --from S5",
                        "source": "rules/incremental.md §3/§4"},
    },
    "S6": {
        "degradePolicy": {"action": "stop", "when": "音效草稿生成失败",
                          "note": "失败落 failed 账,下游 blocked(P25-1)",
                          "source": "rules/incremental.md §2"},
        "rerunPolicy": {"mode": "content-key", "force": "rs_run --force --from S6",
                        "source": "rules/incremental.md §3/§4"},
    },
    "S7": {
        "degradePolicy": {"action": "stop", "when": "断句硬约束无解",
                          "note": "--auto 下断句歧义取 DP 最优并留候选"
                                  "(06_成片输出/segments_candidates.json,auto:S7:ambiguous-auto)",
                          "source": "rules/incremental.md §4.7"},
        "rerunPolicy": {"mode": "content-key", "force": "rs_run --force --from S7",
                        "note": "手改 subtitles.ass 后勿重跑 S7(会冲掉手改),改跑 06_成片输出/rebuild.py(S8)",
                        "source": "rules/incremental.md §7"},
    },
    "S8": {
        "degradePolicy": {"action": "stop", "when": "烧录/编码失败",
                          "note": "只用现有 ass 重烧录,不重新生成字幕(S8 是手改字幕落点)",
                          "source": "rules/incremental.md §7"},
        "rerunPolicy": {"mode": "content-key", "force": "rs_run --force --from S8",
                        "source": "rules/incremental.md §3/§4"},
    },
    "S9": {
        "degradePolicy": {"action": "stop", "when": "L0 机械自检未通过",
                          "note": "L0 硬闸不放松(--auto 亦然);L1 目测 --auto 降级为抽帧留证"
                                  "(不判定不阻断,auto:verify:l1-degrade);L2 验收归用户",
                          "source": "rules/incremental.md §4.7 / rules/verify.md"},
        "rerunPolicy": {"mode": "content-key", "force": "rs_run --force --from S9",
                        "source": "rules/incremental.md §3/§4"},
    },
    "S10": {
        "degradePolicy": {"action": "stop", "when": "封面文案生成失败",
                          "note": "失败落 failed 账(P25-1)",
                          "source": "rules/incremental.md §2"},
        "rerunPolicy": {"mode": "content-key", "force": "rs_run --force --from S10",
                        "source": "rules/incremental.md §3/§4"},
    },
    "S11": {
        "degradePolicy": {"action": "stop", "when": "交付对账缺硬项(成片/字幕/文案)",
                          "note": "封面/占位文案属 Agent 语义产物:--auto 不代劳、留痕"
                                  "(auto:S11:deliverables);硬项缺失即失败",
                          "source": "rules/incremental.md §4.7"},
        "rerunPolicy": {"mode": "content-key", "force": "rs_run --force --from S11",
                        "note": "S11 无注册命令,人工交付后 rs_run --mark S11",
                        "source": "rules/incremental.md §4"},
    },
}

# ---------------------------------------------------------------- 人工补:辅助超时键(policies)
# 直调脚本与 rs_run 共用的非阶段超时口径;值 = v0.20 各脚本手抄常量**原值搬运**
# (零行为漂移),env 名 = 既有 CUTFLOW_*_TIMEOUT。消费入口 = rs_common.policy_timeout。

HUMAN_POLICIES: dict[str, dict] = {
    "VERIFY": {"base": 1800, "factor": 0.0, "env": "CUTFLOW_VERIFY_TIMEOUT",
               "note": "rs_run --verify 的 L0 机械自检",
               "source": "rs_run.run_verify v0.20 字面 1800"},
    "VERIFY_L1": {"base": 3600, "factor": 0.0, "env": "CUTFLOW_VERIFY_TIMEOUT",
                  "note": "rs_run --verify 的 L1(含抽帧留证,耗时更长)",
                  "source": "rs_run.run_verify v0.20 字面 3600"},
    "SEG": {"base": 1800, "factor": 4.0, "env": "CUTFLOW_SEG_TIMEOUT",
            "note": "rs_render 段渲超时(时长×4,下限 1800s;R40)",
            "source": "rs_render 段计划 v0.20 手抄常量"},
    "STEP": {"base": 3600, "factor": 4.0, "env": "CUTFLOW_STEP_TIMEOUT",
             "note": "rs_render 拼接/合成/混音/字幕烧录步(R40)",
             "source": "rs_render 五处 v0.20 手抄 floor=3600"},
    "ENCODE": {"base": 7200, "factor": 4.0, "env": "CUTFLOW_ENCODE_TIMEOUT",
               "note": "rs_render 终编码(最慢一步,下限抬高到 7200s)",
               "source": "rs_render 编码步 v0.20 手抄 floor=7200"},
    "PROBE": {"base": 3600, "factor": 0.0, "env": "",
              "note": "rs_render loudnorm 探测 / 抠像预合成的平超时",
              "source": "rs_render v0.20 字面 3600(有意不平超时比例化)"},
    "ASR": {"base": 3600, "factor": 0.0, "env": "CUTFLOW_ASR_TIMEOUT",
            "note": "rs_align 自带 ASR 子进程",
            "source": "rs_align v0.20 字面 3600"},
    "GREEN": {"base": 60, "factor": 0.0, "env": "CUTFLOW_GREEN_TIMEOUT",
              "note": "rs_greenscreen 幕布检测单帧判定",
              "source": "rs_greenscreen v0.20 字面 60"},
    "BENCH": {"base": 600, "factor": 0.0, "env": "",
              "note": "rs_run --auto 抽帧留证(rs_bench 网格图)",
              "source": "rs_run.bench_evidence v0.20 字面 600"},
}

# timeoutPolicy 的 env 声明:rs_run 阶段级全局覆盖(P24-1 既有口径,值不变)
STAGE_TIMEOUT_ENV = "CUTFLOW_STAGE_TIMEOUT_SEC"


# ---------------------------------------------------------------- 机械抽取

def _compute_preconditions(stages: list[dict]) -> dict[str, list[str]]:
    """每阶段前置 = ① 线性链前驱(rules/incremental.md §4 --from 语义)
    ∪ ② 输入文件的产物归属(本阶段 inputs 与各阶段 outputs 的 glob 匹配,机械)。"""
    owners: dict[str, set[str]] = {}
    for st in stages:
        for out in st["outputs"]:
            owners.setdefault(st["id"], set()).add(out)
    pre: dict[str, list[str]] = {}
    for i, st in enumerate(stages):
        found: set[str] = set()
        if i > 0:
            found.add(stages[i - 1]["id"])          # ① 线性链前驱
        for inp in st["inputs"]:
            for sid, pats in owners.items():
                if sid == st["id"]:
                    continue
                if any(fnmatch.fnmatch(inp, pat) for pat in pats):
                    found.add(sid)                   # ② 产物归属匹配
        pre[st["id"]] = sorted(found)
    return pre


def build_contract() -> dict:
    """机械抽取 + 人工补段 → stages.json 文档(键序固定,字节级可复现)。"""
    stages = rs_run.spec()                          # 唯一阶段真相(rs_paths 相对路径口径)
    timeouts = dict(rs_run.STAGE_TIMEOUTS)          # {"S1": 14400}
    default_to = int(rs_run.DEFAULT_STAGE_TIMEOUT_SEC)
    gate_names = [fn.__name__ for fn in rs_verify.L0_CHECKS]
    unmapped = [g for g in gate_names if g not in GATE_STAGES]
    if unmapped:
        raise SystemExit(f"[gen_stages] L0 门禁未映射到阶段(补 GATE_STAGES):{unmapped}")

    pre = _compute_preconditions(stages)
    # 人工段:优先沿用盘上既有条目(维护者措辞),缺则用内置缺省
    prev_stages: dict[str, dict] = {}
    prev_policies: dict = {}
    if STAGES_OUT.is_file():
        try:
            doc = json.loads(STAGES_OUT.read_text(encoding="utf-8"))
            prev_stages = {s["id"]: s for s in doc.get("stages", []) if isinstance(s, dict)}
            prev_policies = doc.get("policies") or {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass

    out_stages = []
    for st in stages:
        sid = st["id"]
        prev = prev_stages.get(sid) or {}
        human = HUMAN_STAGE_POLICIES.get(sid) or {}
        gates = [{"check": g, "level": "L0", "source": f"rs_verify.{g}",
                  "note": GATE_STAGES[g][1]}
                 for g in gate_names if GATE_STAGES[g][0] == sid]
        base = int(timeouts.get(sid, default_to))
        entry = {
            "id": sid,
            "name": st["name"],
            "manual": bool(st.get("manual")),
            "scripts": list(st.get("scripts") or []),
            "inputs": list(st["inputs"]),
            "outputs": list(st["outputs"]),
            "preconditions": pre[sid],
            "gates": gates,
            "timeoutPolicy": {"base": base, "factor": 0.0, "env": STAGE_TIMEOUT_ENV,
                              "note": f"rs_run 阶段子进程限时(P24-1;{sid} base)"
                              if sid in timeouts else
                              f"rs_run 阶段子进程限时(P24-1;缺省 {default_to}s)"},
            "degradePolicy": prev.get("degradePolicy") or human.get("degradePolicy"),
            "rerunPolicy": prev.get("rerunPolicy") or human.get("rerunPolicy"),
            # 以下为机械抽取的注册表附加字段(--plan/--explain 与契约对账用)
            "paramKeys": list(st.get("paramKeys") or []),
            "marker": st.get("marker"),
            "cmd": list(st["cmd"]) if st.get("cmd") else None,
            "post": list(st["post"]) if st.get("post") else None,
        }
        out_stages.append(entry)

    policies = dict(prev_policies) if prev_policies else dict(HUMAN_POLICIES)
    return {
        "version": 1,
        "generatedBy": ("tools/gen_stages.py —— 除 policies 与各阶段 "
                        "degradePolicy/rerunPolicy 外均机械抽取,禁止手改;"
                        "漂移即 tests/test_stages_contract.py 红"),
        "stageCount": len(out_stages),
        "stages": out_stages,
        "policies": policies,
    }


def serialize(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=1) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="stages.json 生成器(T2.10;重跑零 diff 是硬门禁)")
    ap.add_argument("--check", action="store_true",
                    help="只对拍:盘上文件与再生结果不一致即退出 2(门禁/CI 用)")
    a = ap.parse_args()
    text = serialize(build_contract())
    if a.check:
        cur = STAGES_OUT.read_text(encoding="utf-8") if STAGES_OUT.is_file() else ""
        if cur != text:
            sys.stderr.write(f"[gen_stages] 漂移:{STAGES_OUT} 与再生结果不一致;"
                             "请重跑 python tools/gen_stages.py\n")
            return 2
        print(f"[gen_stages] 零 diff:{STAGES_OUT}")
        return 0
    STAGES_OUT.parent.mkdir(parents=True, exist_ok=True)
    STAGES_OUT.write_text(text, encoding="utf-8")
    n = len(json.loads(text)["stages"])
    print(f"[gen_stages] 已再生 {STAGES_OUT}({n} 阶段)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
