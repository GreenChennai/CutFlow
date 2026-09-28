"""rs_codes — 退出码与结果 code 字符串唯一注册表(T2.13,第二册 B 组)。

一句话:**每个 CLI 结果的 `code` 字符串只能在这里登记,`emit()`/`die()` 强制校验,
未登记即抛 `UnregisteredCodeError`(红),绝不静默外放新 code。**

背景(第二册 T2.13 / BACKLOG R08/R36 顺延):此前 `ExitCode` 在 rs_common、
`code` 字符串散落在 47 个脚本的 emit 调用里,没有一份对账总表;新脚本随手
编一个 code 也没人拦,Agent 只能靠字符串猜语义。本文件把两件事收敛为一:

  · 退出码整数协议:0 成功 / 2 输入错 / 3 依赖缺失 / 4 执行失败(rs_common 共用);
  · 结果 code 注册表:按「子系统域」分组的全量清单,rs_common.emit/die 在输出前
    逐次校验,未登记即抛错(门禁 tests/test_codes_registry.py 另做全仓静态对拍)。

登记纪律(ADR-0055 零静默的姊妹规约):
  · 新脚本/新分支要产新 code → 先在本文件对应域里登记一行,再用;
  · code 命名:全大写下划线,`<域前缀>_<结果>`(如 `IR_INVALID`);对账型成功
    用 `<域>_OK`,失败用 `<域>_FAIL` / 专用语义词,禁止中英混写;
  · 动态拼 code(如 `f"MATTE_{verdict.upper()}"`)必须把全部可能取值登记,
    并在本文件注释里写明拼法出处;
  · 删除某个 code 前,先全仓 grep 确认零调用(门禁测试会对拍 emit 调用点)。

本文件**只依赖标准库**(rs_common 反向 import 本文件,方向不许反过来)。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 退出码整数协议

EXIT_OK = 0        # 成功(ok=true)
EXIT_INPUT = 2     # 输入/参数错(ok=false;BAD_ARGS / BAD_INPUT 一族)
EXIT_DEP = 3       # 依赖缺失(ok=false;NO_CONFIG / DEP_MISSING / NO_ASR 一族)
EXIT_EXEC = 4      # 执行失败(ok=false;阶段失败 / 自检未过 / 交付缺项)

# ---------------------------------------------------------------- code 注册表(按子系统域)
#
# 每域括注:主要产出脚本。域的划分只为可读性与 diff 定位;成员资格判定与域无关。

DOMAINS: dict[str, tuple[str, ...]] = {
    # 全仓共用的协议级 code(任何脚本都可能产出)
    "通用协议": (
        "OK", "CACHED", "DRY_RUN", "IDEMPOTENT", "INTERNAL", "VALIDATED",
        "BAD_ARGS", "BAD_INPUT", "BAD_JSON", "BAD_VALUE", "BAD_COMMAND", "BAD_SLUG",
        "BAD_STAGE", "BAD_REASON",
        "NO_CONFIG", "NO_INPUT", "NO_PROJECT", "NO_ACTION", "NO_BACKEND", "NO_BASELINE",
        "DEP_MISSING", "PROBE_FAIL", "PROBE_OK",
        "EMPTY_SCRIPT", "NEED_REASON", "SOURCE_MISSING", "UNDONE",
        "FORCE_NEEDS_TARGET", "PRECONDITION_FAILED",
        "DURATION_LEDGER", "SAFE_CHECK_FAILED", "READONLY_ZONE_VIOLATED",
        "WORDLINE_MANUAL_EDIT",
        "ALREADY_FINAL_SPACE", "NOT_FINAL_SPACE", "NOT_SOURCE_SPACE",
        "KEEP_INCONSISTENT",
    ),
    # rs_run.py(阶段缓存与增量编排)
    "阶段编排 rs_run": (
        "STATUS_OK", "EXPLAIN_OK", "MARK_OK", "INIT_OK",
        "RUN_OK", "RUN_VERIFY_FAIL", "RUN_PRUNE_FAILED", "STAGE_FAILED",
        "STATE_READONLY", "ROLLBACK_OK", "ROLLBACK_FAIL",
        "VERIFY_OK", "VERIFY_FAIL", "VERIFY_STATUS", "FIRST_CHECK_MARKED",
    ),
    # rs_align.py / rs_asr.py(转写与字级对齐)
    "对齐与转写": (
        "ALIGN_OK", "CALIBRATE_OK", "REMAP_OK",
        "ASR_OK", "ASR_FAIL", "ASR_FAILED", "ASR_BAD_JSON", "ASR_NO_OUTPUT",
        "ASR_TIMEOUT",
    ),
    # rs_cut.py / rs_beat.py / rs_shot.py / rs_screen.py(粗剪与前置检测)
    "粗剪与检测": (
        "CUT_OK", "CUT_APPLIED", "CUT_FROM_TEXT", "PROTECT_INTRUDED", "PRUNE_GHOST_OK",
        "BEATS_OK", "BEATS_FAIL", "BEATS_CACHED", "BEATS_MISSING",
        "SHOTS_OK", "SHOTS_CACHED",
        "SCREEN_OK", "SCREEN_CACHED",
    ),
    # rs_ir.py / rs_render.py / rs_fx(时间线与渲染)
    "IR 与渲染": (
        "IR_BUILT", "IR_VALID", "IR_INVALID", "IR_UNREADABLE", "IR_MANUAL_EDITS",
        "IR_VERSION_UNSUPPORTED",
        "RENDER_OK", "RENDER_EXPLAIN",
        "COMPOSE_FAIL", "CONCAT_FAIL", "CONCAT_SPLICE_FAIL", "CONCAT_XFADE_FAIL",
        "ENCODE_FAIL", "MIX_FAIL", "SEGMENT_FAIL", "PCM_EXTRACT_FAIL",
        "FFMPEG_FAIL", "BUILD_FAIL", "SMOOTH_OK", "FX_REGISTRY_MISSING",
    ),
    # rs_subtitle.py(字幕)
    "字幕": (
        "SUBTITLE_OK", "SUBTITLE_FAIL", "ANCHOR_FAIL",
        "NO_ASS", "NO_CHARS", "NO_EVENTS", "NO_TERMS_FILE",
        "OVERRIDE_NEEDS_WORDLINE", "DUAL_NEEDS_WORDLINE",
        "KARAOKE_NEEDS_WORDLINE", "KARAOKE_NEEDS_WORD_TS",
    ),
    # rs_sync.py / rs_verify.py / rs_diagnose.py / rs_sense.py / rs_vision.py /
    # rs_bench.py / rs_frames.py / rs_matting.py / rs_fetchable.py(对账与自检)
    "对账与自检": (
        "SYNC_OK", "SYNC_FAIL",
        "COPYRIGHT_OK", "COPYRIGHT_REGISTRATION_MISSING", "COPYRIGHT_REGISTRATION_INVALID",
        "DIAGNOSIS_OK", "DIAGNOSIS_ISSUES", "DIAGNOSIS_INDETERMINED",
        # 评分卡(T5.1/T5.2):rs_verify --score 总分 ≥ 及格线 = SCORE_OK,< 及格线 = SCORE_FAIL
        "SCORE_OK", "SCORE_FAIL",
        "SENSE_OK", "SENSE_REPORT_OK", "SENSE_SHOTS_OK",
        "VISION_OK", "VISION_SCHEMA", "FETCHABLE_STATE",
        "BENCH_OK", "BENCH_FAIL", "BENCH_EMPTY", "BENCH_NO_VIDEO",
        "FRAMES_OK", "FRAMES_FAIL",
        # rs_matting 动态拼法出处:rs_matting.py `f"MATTE_{verdict.upper()}"`
        # (verdict ∈ pass/warn/fail/blocked)与 QUALITY_*/ENGINE_MISSING 常量。
        "MATTE_PASS", "MATTE_WARN", "MATTE_FAIL", "MATTE_BLOCKED",
        "MATTE_QUALITY_PASS", "MATTE_QUALITY_WARN", "MATTE_QUALITY_FAIL",
        "MATTE_ENGINE_MISSING",
        "MATTE_APPLIED", "MATTE_NOT_NEEDED", "MATTE_NO_REPORT",
    ),
    # rs_ingest.py / rs_asset.py / rs_greenscreen.py / rs_pixabay.py(素材与资产)
    "素材与资产": (
        "INGEST_OK", "SCAN_OK", "MIGRATE_CHECK_FAIL",
        "GREEN_CHECK", "GREEN_OVERRIDE", "GREEN_SCREEN_INPUT",
        "ASSET_LIST_OK", "ASSET_GET_OK", "ASSET_ADD_OK", "ASSET_ADD_NO_FILE",
        "ASSET_NOT_FOUND", "ASSET_SCAN_OK", "ASSET_SCAN_NO_DIR", "ASSET_SEARCH_OK",
        "ASSET_ATTRIBUTION_OK", "ASSET_ATTRIBUTION_NONCOMMERCIAL",
        "ASSET_CHECK_OK", "ASSET_CHECK_FAIL",
        "PIXABAY_SEARCH", "PIXABAY_SEARCH_FAIL", "PIXABAY_FETCH_OK",
        "PIXABAY_FETCH_FAIL", "PIXABAY_NO_RESULTS", "PIXABAY_DOWNLOAD_TOO_SMALL",
    ),
    # rs_ingest publish / rs_effects show(交付发布与效果检索)
    "发布与检索": (
        "PUBLISH_OK", "DELIVERABLES_INCOMPLETE",
        "EFFECT_FOUND", "EFFECT_NOT_AVAILABLE",
        "REFRAME_CLIP_SUBJECT",   # 主体切破错误码(rs_reframe 方案 §5.5.2;渲染端留痕)
    ),
    # rs_brand.py / rs_meta.py / rs_artboard.py(品牌、文案与卡片交付)
    "品牌与交付": (
        "BRAND_OK", "BRAND_PARTIAL", "BRAND_PLAN", "VARIANTS_OK", "META_OK",
        "CARD_EXISTS", "CARDS_GENERATED", "NO_ARTBOARD",
        "EXPORT_OK", "EXPORT_PARTIAL", "EXPORT_FAILED", "EXPORT_SKIP",
        "FALLBACK_OK", "FALLBACK_PARTIAL", "OVERLAY_OK", "OVERLAY_ISSUES",
        "LOGO_ANALYZED", "LOGO_INVALID", "REVIEW_PACK_OK", "DECISIONS_OK",
    ),
    # rs_tts.py / rs_dub.py(配音)
    "TTS 与配音": (
        "TTS_OK", "TTS_FAIL", "TTS_DOWN",
        "DUB_OK", "DUB_REJECT", "DUB_NO_WORD_TS",
        "NO_VOICE", "VOICE_AMBIGUOUS", "VOICE_DISABLED",
    ),
    # rs_sfx.py(音效)
    "音效": ("SFX_DRAFT_OK", "SFX_APPLIED"),
    # rs_intent.py / rs_caps.py / rs_stylepack.py / rs_doctor.py(意图与能力)
    "意图与能力": (
        "INTENT_COMPILED", "INTENT_DRY_RUN",
        "CAPS_GENERATED", "CAPS_MATCH", "CAPS_DRIFT", "CAPS_MISSING",
        "CAPS_SEARCH", "CAPS_SEARCH_USAGE",
        "TEMPLATE_LIST", "TEMPLATE_MATCH", "TEMPLATE_SHOWN",
        "TEMPLATE_NOT_FOUND", "TEMPLATE_NO_MATCH",
        "PACK_SCAFFOLDED", "PACK_EXISTS", "PACK_SHOWN",
        "STYLEPACK_CHECK", "STYLEPACK_CHECK_FAILED",
        "PLAN_OK", "PLAN_GATE_FAIL", "SKELETON_OK", "SKELETON_BUDGET",
        "ENSURE_OK", "ENSURE_FAIL", "DOCTOR_OK", "DOCTOR_FAIL",
    ),
    # rs_edit.py / rs_editor.py / rs_effects.py(编辑桥;冲突码 CF-001/002 见 rs_edit)
    "编辑桥": (
        "APPLY_OK", "APPLY_ISSUES", "CONTEXT_OK",
        "DIFF_OK", "DIFF_BASE", "DIFF_SESSION",
        "CF-001", "CF-002",
        # rs_edit._fail(动态入口,字面量在调用点:静态扫描按 _fail 首参对拍)
        "BAD_ADDRESS", "BAD_FIELD", "BAD_OP", "LOCKED", "NOT_FOUND",
        "MISSING_SNAPSHOT", "OP_UNSUPPORTED",
        "EFFECT_SHOWN", "EFFECT_NOT_FOUND", "EFFECT_REMOVED",
        "EFFECTS_LIST", "EFFECTS_COVERAGE", "EFFECTS_NORMALIZED",
        "EFFECTS_NO_CHANGE", "EFFECTS_CHECK_OK", "EFFECTS_CHECK_FAIL",
    ),
    # rs_jy_draft.py(剪映桥)
    "剪映桥": ("DRAFT_OK", "DRAFT_GATE_FAIL", "JY_PLAN_DRY_RUN", "JY_RUNNING"),
    # rs_reframe.py / rs_broll.py / rs_cleanup.py(其余阶段脚本)
    "其余阶段脚本": (
        "REFRAME_OK", "REFRAME_FAIL", "REFRAME_CACHED",
        "REFRAME_RATIO", "REFRAME_PLAN_INVALID",
        "REFRESH_DURATIONS_OK", "BROLL_OK",
        "RETEXT_OK", "RETEXT_REJECT", "RETEXT_DRYRUN",
        "CLEANUP_OK", "CLEANUP_PARTIAL", "CLEANUP_DRYRUN",
    ),
    # 输入校验细分码(多脚本共用的领域校验失败)
    "输入校验细分": (
        "BAD_ANCHORS", "BAD_BRIEF", "BAD_CANVAS", "BAD_DETECTOR", "BAD_HUAZI",
        "BAD_KIND", "BAD_OVERRIDE", "BAD_OVERRIDE_FILE", "BAD_PLAN",
        "BAD_PLATFORM", "BAD_PROTECT", "BAD_RANGE", "BAD_RATIO", "BAD_SHOTS",
        "NO_ASR", "NO_ASR_RUNNER", "NO_AUDIO", "NO_CLIP", "NO_CUTLIST",
        "NO_DECISIONS", "NO_DURATION", "NO_FRAMES", "NO_IMAGE", "NO_IR",
        "NO_KEEP", "NO_LOGO", "NO_MANIFEST", "NO_MATERIALS", "NO_MEDIA",
        "NO_PACK", "NO_PLAN", "NO_POINTS", "NO_QUOTE", "NO_QUOTE_FILE",
        "NO_SHOTS", "NO_SOURCE", "NO_TARGET_TEXT", "NO_TEMPLATE", "NO_VIDEO",
        "NO_VIDEO_TRACK", "NO_WORDLINE",
    ),
}


def _build() -> dict[str, str]:
    """展开域表 → {code: 域};同 code 跨域重复 = 注册表自身矛盾,直接崩(左移)。"""
    out: dict[str, str] = {}
    for domain, codes in DOMAINS.items():
        for c in codes:
            prev = out.get(c)
            if prev is not None and prev != domain:
                raise ValueError(f"rs_codes 注册表内 code 重复:{c}({prev} 与 {domain})")
            out[c] = domain
    return out


CODE_DOMAIN: dict[str, str] = _build()
REGISTERED_CODES: frozenset[str] = frozenset(CODE_DOMAIN)


class UnregisteredCodeError(ValueError):
    """emit/die 使用了未登记的 code(T2.13:未登记即红,绝不静默外放)。"""


def is_registered(code: str) -> bool:
    """code 是否已登记。"""
    return code in REGISTERED_CODES


def require_registered(code: str) -> None:
    """未登记即抛 UnregisteredCodeError(带登记指引)。"""
    if code not in REGISTERED_CODES:
        raise UnregisteredCodeError(
            f"未登记的结果 code:{code!r} —— 请先在 rs_codes.py 的 DOMAINS 对应域登记,"
            "再让它出现在 emit()/die() 里(T2.13 纪律:code 唯一注册表)")


def domain_of(code: str) -> str:
    """code 所属域;未登记抛 UnregisteredCodeError。"""
    require_registered(code)
    return CODE_DOMAIN[code]
