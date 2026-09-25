"""rs_fx —— fxId 注册表与 T2 GLSL 渲染(M13/ADR-0054,分册02)。

模块:
  registry  fxId → 渲染配方唯一真相源(转场表 / 单片段特效生成器);
  t2_glsl   B3 路径:moderngl + gl-transitions 预渲转场重叠区中间 MP4。
"""

from rs_fx.registry import (  # noqa: F401
    CLIP_FX,
    GLSL_DIR_DEFAULT,
    TRANSITIONS,
    FxContext,
    FxError,
    FxPlan,
    build_clip_fx,
    clip_fx_ids,
    glsl_enabled,
    registered_ids,
    resolve_transition,
    resolve_transition_type,
    transition_ids,
)
