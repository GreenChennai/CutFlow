"""fxId 注册表(M13/ADR-0054,分册02 §3):fxId → 渲染配方唯一真相源。

设计(分册02 §2.2/§9 回滚纪律):
  · 转场:TRANSITIONS 表,kind ∈ xfade(单滤镜拼接)/ concatvideo(视频硬切+音频交叉,
    J/L-Cut 家族)/ glsl(T2 走 t2_glsl 预渲重叠区)。既有 6 个 fxId(tr.fade/tr.wipe.left/
    tr.wipe.up/tr.slide.left/tr.circle.open/tr.cut)映射不变 —— 旧工程零影响;
  · 单片段特效:CLIP_FX 表,生成器产出滤镜片段(声明式 spec 绑定参数,同滤镜多档
    合并为一条 fxId + 参数档 —— 分册02 §1.1 判级注意 1);
  · **未注册 fxId 报错不静默**(FxError FX_UNREGISTERED;分册02 §9「渲染端按 fxId
    查表,未注册报错而非静默」)。T2 缺依赖 → 降级表里声明的最接近 T1 + 留痕;
  · 本模块**纯数据 + 字符串拼装**:不 import ffmpeg/moderngl,不碰工程路径,可单测;
  · flash/频闪类参数档内置光敏安全上限(单次时长 ≤0.3s,WCAG 2.3.1 同口径,
    分册02 §3.1)—— 注册表是底线,rs_verify 的 FLASH_UNSAFE 判据是全片闸。

域标注(domain):render=rs_render 段级滤镜链消费;subtitle=S7 字幕链消费(卡拉OK
逐字/花字 \\t 动画/分行 Dialogue,分册01 花字基础档);audio=concat 音频侧消费;
element/artboard=落点在元素轨/预渲染(当前登记待实现,fxId 仍注册占位以保契约唯一)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- 基础常量

FLASH_MAX_S = 0.3          # 光敏安全:单次闪变时长上限(分册02 §3.1;安全底线不可回滚)
SHAKE_MAX_PX = 4           # 抖动幅度上限(分册02 §3.2:幅度过大 = 廉价感)
STRETCH_MAX = 0.3          # 拉伸变形量上限(分册02 §3.2)
ZOOM_OVERSHOOT_MAX = 1.1   # 弹跳超调上限(artboard 动画十二法则一致)

GLSL_DIR_DEFAULT = "templates/effects/glsl"   # 仓库内收录的 gl-transitions(MIT)


class FxError(Exception):
    """fxId 契约违规:code 与给人看的 message 随身(渲染端 die 前转结构化错误)。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ---------------------------------------------------------------- 转场表
#
# 字段:kind=xfade|concatvideo|glsl;xfade=xfade transition 名(kind=xfade 时必填);
# glsl=收录的 GLSL 名(kind=glsl);fallback=依赖缺失/被关闭时的 T1 回退 xfade;
# durMs=缺省时长;flashy=花哨类(处方 flashy_max 管辖,分册06 §5.1);
# audio=音频衔接语义说明;boundary=段边界副效(闪入/闪出/定格,见 resolve_transition);
# note=诚实标注(近似档必须注明)。
_T = lambda fxid, **kw: {"fxId": fxid, **kw}  # noqa: E731 —— 表项构造缩写

TRANSITIONS: dict[str, dict] = {}
for _spec in [
    # ---- 既有 6 值(M13 前的直通表,映射一字不改;旧工程零影响) ----
    _T("tr.cut", kind="cut", durMs=0, label="硬切", flashy=False,
       note="显式硬切(tr.cut=无技巧转场的显式声明)"),
    _T("tr.fade", kind="xfade", xfade="fade", durMs=300, label="叠化"),
    _T("tr.wipe.left", kind="xfade", xfade="wipeleft", durMs=400, label="向左擦除"),
    _T("tr.wipe.up", kind="xfade", xfade="wipeup", durMs=400, label="向上擦除"),
    _T("tr.slide.left", kind="xfade", xfade="slideleft", durMs=400, label="左移"),
    _T("tr.circle.open", kind="xfade", xfade="circleopen", durMs=500, label="圆形展开"),
    # ---- 基础明暗(分册02 §3.4 用户转场清单 → 映射) ----
    _T("tr.fade.black", kind="xfade", xfade="fadeblack", durMs=400, label="黑场过渡",
       usage="时空跳跃/章节切换(分册06 §3)"),
    _T("tr.fade.white", kind="xfade", xfade="fadewhite", durMs=400, label="白场过渡"),
    _T("tr.fade.grays", kind="xfade", xfade="fadegrays", durMs=400, label="灰度过渡"),
    _T("tr.fade.fast", kind="xfade", xfade="fadefast", durMs=200, label="频闪", flashy=True),
    _T("tr.fade.slow", kind="xfade", xfade="fadeslow", durMs=800, label="慢叠化"),
    _T("tr.dissolve.cross", kind="xfade", xfade="fade", durMs=400, label="交叉溶解",
       note="NLE 默认转场(分册02 §6.1);与 tr.fade 同滤镜,fxId 分立保语义"),
    _T("tr.dissolve.noise", kind="xfade", xfade="dissolve", durMs=400, label="噪点溶解"),
    _T("tr.dissolve.pixel", kind="xfade", xfade="pixelize", durMs=400, label="像素溶解"),
    # ---- 擦除家族 ----
    _T("tr.wipe.right", kind="xfade", xfade="wiperight", durMs=400, label="向右擦除"),
    _T("tr.wipe.down", kind="xfade", xfade="wipedown", durMs=400, label="向下擦除"),
    _T("tr.wipe.tl", kind="xfade", xfade="wipetl", durMs=400, label="左上擦除"),
    _T("tr.wipe.tr", kind="xfade", xfade="wipetr", durMs=400, label="右上擦除"),
    _T("tr.wipe.bl", kind="xfade", xfade="wipebl", durMs=400, label="左下擦除"),
    _T("tr.wipe.br", kind="xfade", xfade="wipebr", durMs=400, label="右下擦除"),
    _T("tr.wipe.diag.tl", kind="xfade", xfade="diagtl", durMs=400, label="对角擦除(左上)"),
    _T("tr.wipe.diag.tr", kind="xfade", xfade="diagtr", durMs=400, label="对角擦除(右上)"),
    _T("tr.wipe.diag.bl", kind="xfade", xfade="diagbl", durMs=400, label="对角擦除(左下)"),
    _T("tr.wipe.diag.br", kind="xfade", xfade="diagbr", durMs=400, label="对角擦除(右下)"),
    # ---- 滑移/推移/卷动(两画面同时移动) ----
    _T("tr.slide.right", kind="xfade", xfade="slideright", durMs=400, label="右移"),
    _T("tr.slide.up", kind="xfade", xfade="slideup", durMs=400, label="上移"),
    _T("tr.slide.down", kind="xfade", xfade="slidedown", durMs=400, label="下移"),
    _T("tr.push.left", kind="xfade", xfade="slideleft", durMs=400, label="向左推移",
       note="推移=两画面同时移动;与 slide 同滤镜,fxId 分立保语义(分册02 §6.1)"),
    _T("tr.push.right", kind="xfade", xfade="slideright", durMs=400, label="向右推移"),
    _T("tr.push.up", kind="xfade", xfade="slideup", durMs=400, label="向上推移"),
    _T("tr.push.down", kind="xfade", xfade="slidedown", durMs=400, label="向下推移"),
    _T("tr.roll.left", kind="xfade", xfade="smoothleft", durMs=400, label="向左卷动(软边)",
       note="卷动=slide 的软边整幅版(分册02 §6.1)"),
    _T("tr.roll.right", kind="xfade", xfade="smoothright", durMs=400, label="向右卷动(软边)"),
    _T("tr.roll.up", kind="xfade", xfade="smoothup", durMs=400, label="向上卷动(软边)"),
    _T("tr.roll.down", kind="xfade", xfade="smoothdown", durMs=400, label="向下卷动(软边)"),
    # ---- 划像/开合 ----
    _T("tr.circle.close", kind="xfade", xfade="circleclose", durMs=500, label="圆形收拢"),
    _T("tr.circle.crop", kind="xfade", xfade="circlecrop", durMs=400, label="圆形遮罩"),
    _T("tr.rect.crop", kind="xfade", xfade="rectcrop", durMs=400, label="矩形遮罩"),
    _T("tr.iris.circle", kind="xfade", xfade="circleopen", durMs=500, label="圈入圈出",
       note="NLE Iris(分册02 §6.1);复古/萌系,风格不符会出戏(分册06 §3)"),
    _T("tr.iris.close", kind="xfade", xfade="circleclose", durMs=500, label="圈出收拢"),
    _T("tr.vert.open", kind="xfade", xfade="vertopen", durMs=400, label="纵向展开"),
    _T("tr.vert.close", kind="xfade", xfade="vertclose", durMs=400, label="纵向收拢"),
    _T("tr.horz.open", kind="xfade", xfade="horzopen", durMs=400, label="横向展开"),
    _T("tr.horz.close", kind="xfade", xfade="horzclose", durMs=400, label="横向收拢"),
    # ---- 切片/风切/覆盖/揭示 ----
    _T("tr.slice.hl", kind="xfade", xfade="hlslice", durMs=400, label="左亮切片"),
    _T("tr.slice.hr", kind="xfade", xfade="hrslice", durMs=400, label="右亮切片"),
    _T("tr.slice.vu", kind="xfade", xfade="vuslice", durMs=400, label="上亮切片"),
    _T("tr.slice.vd", kind="xfade", xfade="vdslice", durMs=400, label="下亮切片"),
    _T("tr.glitch.slice", kind="xfade", xfade="hrslice", durMs=300, label="故障切片",
       flashy=True, note="近似档:切片族承担故障语义;RGB 错位版待 T2 故障类"),
    _T("tr.wind.hl", kind="xfade", xfade="hlwind", durMs=400, label="左风切"),
    _T("tr.wind.hr", kind="xfade", xfade="hrwind", durMs=400, label="右风切"),
    _T("tr.wind.vu", kind="xfade", xfade="vuwind", durMs=400, label="上风切"),
    _T("tr.wind.vd", kind="xfade", xfade="vdwind", durMs=400, label="下风切"),
    _T("tr.cover.left", kind="xfade", xfade="coverleft", durMs=400, label="左覆盖"),
    _T("tr.cover.right", kind="xfade", xfade="coverright", durMs=400, label="右覆盖"),
    _T("tr.cover.up", kind="xfade", xfade="coverup", durMs=400, label="上覆盖"),
    _T("tr.cover.down", kind="xfade", xfade="coverdown", durMs=400, label="下覆盖"),
    _T("tr.reveal.left", kind="xfade", xfade="revealleft", durMs=400, label="左揭示"),
    _T("tr.reveal.right", kind="xfade", xfade="revealright", durMs=400, label="右揭示"),
    _T("tr.reveal.up", kind="xfade", xfade="revealup", durMs=400, label="上揭示"),
    _T("tr.reveal.down", kind="xfade", xfade="revealdown", durMs=400, label="下揭示"),
    _T("tr.squeeze.h", kind="xfade", xfade="squeezeh", durMs=400, label="横向挤压"),
    _T("tr.squeeze.v", kind="xfade", xfade="squeezev", durMs=400, label="纵向挤压"),
    # ---- 变焦/甩镜 ----
    _T("tr.punch.zoom", kind="xfade", xfade="zoomin", durMs=200, label="冲击变焦",
       flashy=True, usage="情绪爆发点(分册06 §3:极短 150–250ms,全片 ≤2 处)"),
    _T("tr.whip.pan", kind="xfade", xfade="hblur", durMs=300, label="甩镜", flashy=True,
       usage="动感接动感(分册06 §3:静态镜头之间不可用)"),
    _T("tr.blur.dissolve", kind="xfade", xfade="hblur", durMs=500, label="模糊溶解",
       note="比硬溶解高级(分册02 §6.1)"),
    # ---- 单滤镜杂项 ----
    _T("tr.distance", kind="xfade", xfade="distance", durMs=400, label="距离溶解"),
    _T("tr.radial", kind="xfade", xfade="radial", durMs=500, label="放射扫描"),
    # ---- 复合(段边界副效 + xfade;boundary 由 resolve_transition 展开) ----
    _T("tr.flash.zoom", kind="xfade", xfade="zoomin", durMs=250, label="闪白变焦",
       flashy=True, boundary={"inFlash": {"color": "white", "frac": 0.7}},
       note="闪白由入段头部 fade(color=white)承担,变焦由 zoomin 承担"),
    _T("tr.light.leak", kind="xfade", xfade="fade", durMs=500, label="漏光",
       boundary={"inFlash": {"color": "0xFFD9A0", "frac": 0.6}},
       note="近似档:暖色闪入近似漏光;真漏光素材叠加待 T2"),
    _T("tr.film.burn", kind="xfade", xfade="fade", durMs=500, label="烧片",
       boundary={"outFlash": {"color": "white", "frac": 0.9}},
       note="近似档:出段尾部烧白近似胶片烧蚀;颗粒叠加入待 T2(FilmBurn GLSL 已收录)"),
    _T("tr.hold.frame", kind="xfade", xfade="fade", durMs=400, label="定格过渡",
       boundary={"outFreeze": True},
       note="出段尾帧冻结(复用 freezeMs 机制)+ 溶解(分册02 §6.1)"),
    # ---- 音频域(J/L-Cut 家族:视频硬切 + 音频交叉;concatvideo) ----
    _T("tr.lcut", kind="concatvideo", durMs=400, label="L-Cut(画面先切、声音延续)",
       domain="audio",
       note="视频 concat 硬切 + 音频 acrossfade 交叉;leadMs/lagMs 错位量待 IR 升版"
            "(rs_edit U7 已登记),本轮以交叉淡变承担语义"),
    _T("tr.jcut", kind="concatvideo", durMs=400, label="J-Cut(声音先入、画面后切)",
       domain="audio", note="同 tr.lcut(声画先后由素材本身承担,机制=音频交叉)"),
    _T("tr.audio.crossfade", kind="concatvideo", durMs=400, label="音频交叉淡变",
       domain="audio", note="acrossfade=tri 双曲线(既有实现;分册02 §6.1)"),
    _T("tr.invisible.cut", kind="concatvideo", durMs=120, label="无缝转场",
       domain="audio", usage="碎片缝合(分册06 §7.4 vlog)",
       note="短交叉(≤4 帧量级)+ 素材运动匹配;运动匹配是 Agent 语义工作(分册06 §2.1 第 10 类)"),
    _T("tr.match.cut", kind="concatvideo", durMs=200, label="匹配剪辑", domain="audio",
       usage="关键动作(分册06 §7.6 drama)",
       note="构图/动作匹配靠选材;机制=极短交叉(分册02 §6.1)"),
]:
    if _spec["fxId"] in TRANSITIONS:
        raise RuntimeError(f"转场 fxId 重复登记:{_spec['fxId']}")
    TRANSITIONS[_spec["fxId"]] = _spec

# ---- T2:gl-transitions(MIT)收录子集;逐条真渲验证(冒烟),失败者移回「登记待实现」。
# fallback = moderngl 缺失 / effects.glsl=false / 编译失败时的最接近 T1(分册02 §1.2)。
_T2 = lambda fxid, glsl, fallback, durMs, label, **kw: _T(  # noqa: E731
    fxid, kind="glsl", glsl=glsl, fallback=fallback, durMs=durMs, tier="T2",
    license="MIT", label=label, **kw)
for _spec in [
    _T2("tr.glsl.dissolve", "dissolve", "dissolve", 500, "GLSL 噪点溶解"),
    _T2("tr.gradient.wipe", "dissolve", "dissolve", 500, "渐变擦除",
        note="灰度渐变控制擦除进度;以噪声阈值溶解近似(分册02 §6.1 T2)"),
    _T2("tr.shape.wipe", "CircleCrop", "circlecrop", 500, "形状擦除",
        note="圆/星等形状遮罩;圆形档以 CircleCrop 承担,自定义路径待收录"),
    _T2("tr.glsl.circle.crop", "CircleCrop", "circlecrop", 500, "GLSL 圆形划像"),
    _T2("tr.glsl.rect.crop", "RectangleCrop", "rectcrop", 500, "GLSL 矩形划像"),
    _T2("tr.glsl.cross.zoom", "CrossZoom", "zoomin", 600, "GLSL 交叉变焦", flashy=True),
    _T2("tr.glsl.dreamy.zoom", "DreamyZoom", "hblur", 600, "GLSL 梦幻变焦"),
    _T2("tr.glsl.simple.zoom", "SimpleZoom", "zoomin", 500, "GLSL 简单变焦", flashy=True),
    _T2("tr.glsl.zoom.circles", "ZoomInCircles", "radial", 600, "GLSL 同心圆变焦"),
    _T2("tr.glsl.directional", "Directional", "wipeleft", 500, "GLSL 方向位移"),
    _T2("tr.glsl.linear.blur", "LinearBlur", "hblur", 500, "GLSL 线性模糊"),
    _T2("tr.glsl.mosaic", "Mosaic", "pixelize", 600, "GLSL 马赛克块"),
    _T2("tr.glsl.grid.flip", "GridFlip", "vdslice", 600, "GLSL 网格翻转"),
    _T2("tr.glsl.windowblinds", "windowblinds", "vuslice", 600, "GLSL 百叶窗"),
    _T2("tr.glsl.squares", "Squareswire", "pixelize", 600, "GLSL 方块走线"),
    _T2("tr.glsl.glitch", "GlitchDisplace", "hrslice", 400, "GLSL 故障位移", flashy=True),
    _T2("tr.glsl.film.burn", "FilmBurn", "fadewhite", 700, "GLSL 烧片"),
    _T2("tr.glsl.overexposure", "Overexposure", "fadewhite", 600, "GLSL 过曝"),
    _T2("tr.glsl.water.drop", "WaterDrop", "dissolve", 600, "GLSL 水滴波纹"),
    _T2("tr.glsl.swirl.radial", "Radial", "radial", 600, "GLSL 径向旋涡"),
    _T2("tr.glsl.swap", "swap", "slideleft", 500, "GLSL 交换滑动"),
    _T2("tr.glsl.pinwheel", "pinwheel", "radial", 600, "GLSL 风车"),
    _T2("tr.glsl.hexagonalize", "hexagonalize", "pixelize", 600, "GLSL 蜂巢溶解"),
    _T2("tr.glsl.butterfly", "ButterflyWaveScrawler", "dissolve", 600, "GLSL 蝶波扒蚀"),
]:
    if _spec["fxId"] in TRANSITIONS:
        raise RuntimeError(f"转场 fxId 重复登记:{_spec['fxId']}")
    TRANSITIONS[_spec["fxId"]] = _spec


# ---------------------------------------------------------------- 单片段特效
#
# 条目:{"gen": 生成器名, "slot": in|out|combo|any, "label", "tier"(缺省 T1),
#        "params": 绑定参数档(生成器再吃用户 params 覆盖), "domain": 消费域,
#        "note": 诚实标注(近似档必须注明), "flashy": bool, "usage": [videoType…]}
# 生成器签名:g(ctx: FxContext, p: dict) -> FxPlan(p = 条目 params ⊕ 用户 params)。

def _xf(v: str) -> str:
    """表达式 → filtergraph 安全(逗号转义;与 rs_render._piecewise_expr 同纪律)。"""
    return v.replace(",", "\\,")


@dataclass
class FxContext:
    """段级上下文(渲染端 step_segment 构造;单位:像素/秒/帧)。"""
    cw: int
    ch: int
    fps: float
    out_s: float          # 名义内容时长(speed 折算后)
    take_s: float         # 实际读取时长(含转场尾帧)
    speed: float = 1.0
    tdur: float = 0.0     # 尾帧扩展量(转场重叠区)


@dataclass
class FxPlan:
    """生成器产物:vf 片段 / filter_complex 模板 / 滑入滑出指令 / 参数副作用。"""
    vf: list[str] = field(default_factory=list)
    fc: list[str] = field(default_factory=list)      # 模板含 {IN}/{OUT} 占位
    slides: list[tuple] = field(default_factory=list)  # (kind, dir, dur) kind∈in|out
    params: dict = field(default_factory=dict)        # 副作用参数(如 freezeMs)
    notes: list[str] = field(default_factory=list)


# ---- 生成器库(全部纯字符串拼装;时间锚:t=段内秒,in/on=帧号) ----

def _fade_d(p: dict, ctx: FxContext, slot: str) -> float:
    d = float(p.get("durMs", p.get("dMs", 500))) / 1000.0
    return max(0.05, min(d, max(ctx.out_s * 0.8, 0.05)))


def _g_fade(ctx: FxContext, p: dict) -> FxPlan:
    """渐显/渐隐(fade.in/fade.out;soft 档追加中间调提亮,分册02 §3.1)。"""
    plan = FxPlan()
    d = _fade_d(p, ctx, p.get("_slot", "in"))
    color = p.get("color")
    alpha = ":alpha=1" if p.get("alpha") else ""
    if p.get("_slot", "in") == "out":
        plan.vf.append(f"fade=t=out:st={max(0.0, ctx.out_s - d):.3f}:d={d:.3f}"
                       + (f":color={color}" if color else "") + alpha)
    else:
        plan.vf.append(f"fade=t=in:st=0:d={d:.3f}"
                       + (f":color={color}" if color else "") + alpha)
        if p.get("soft"):
            plan.vf.append("curves=all='0/0 0.5/0.55 1/1'")
    return plan


def _g_flash(ctx: FxContext, p: dict) -> FxPlan:
    """闪白/闪黑(flash.in.* / flash.out.*;D≤0.3s 光敏安全)。"""
    plan = FxPlan()
    d = min(float(p.get("durMs", 240)) / 1000.0, FLASH_MAX_S)
    color = str(p.get("color", "white"))
    if p.get("_slot") == "out":
        plan.vf.append(f"fade=t=out:st={max(0.0, ctx.out_s - d):.3f}:d={d:.3f}:color={color}")
    else:
        plan.vf.append(f"fade=t=in:st=0:d={d:.3f}:color={color}")
    plan.notes.append(f"闪变 {d * 1000:.0f}ms ≤{FLASH_MAX_S * 1000:.0f}ms(光敏安全)")
    return plan


def _g_zoompan(ctx: FxContext, p: dict) -> FxPlan:
    """zoompan 家族:轻微放大/缩小/推近/拉远/缩放 pop/锚点缩放/慢推/呼吸/弹跳。
    z 表达式锚 in=输入帧号;d=1 每输入帧出一帧;s 显式画幅(分册02 §3.2)。"""
    plan = FxPlan()
    n_frames = max(int(ctx.out_s * ctx.fps), 1)
    z0 = float(p.get("z0", 1.0))
    z1 = float(p.get("z1", 1.06))
    slot = p.get("_slot", "in")
    amp = p.get("amp")
    curve = str(p.get("curve", "linear"))
    if curve == "breathe":                       # 呼吸:微幅正弦(≤2%)
        amp = min(float(amp if amp is not None else 0.02), 0.02)
        z = f"1+{amp:.4f}*sin(2*PI*in/{max(ctx.fps * 3, 1):.2f})"
    elif curve == "pop":                         # 弹跳:超调 ≤1.1 的阻尼曲线
        u = f"min(in/{n_frames},1)"
        z = f"1.0-(0.18*(1-{u}))-0.08*{u}*(1-{u})*sin(3.14159*{u})"
    else:                                        # 线性 ramp:入场 z0→z1,出场锚末端
        u = f"min(in/{n_frames},1)"
        if slot == "out":
            # 出场:z0 保持到只剩 N 帧,最后 N 帧回到 z1(与入场镜像)
            z = (f"if(lt(in,{n_frames}),{z0:.4f},"
                 f"{z1:.4f}+({z0:.4f}-{z1:.4f})*min((in-{n_frames})/{n_frames},1))")
        else:
            z = f"{z0:.4f}+({z1:.4f}-{z0:.4f})*{u}"
    ax = float(p.get("anchorX", 0.5))
    ay = float(p.get("anchorY", 0.5))
    plan.vf.append(
        f"zoompan=z='{_xf(z)}':x='iw*{ax:.3f}-(iw/zoom/2)':y='ih*{ay:.3f}-(ih/zoom/2)'"
        f":d=1:s={ctx.cw}x{ctx.ch}:fps={ctx.fps}")
    plan.vf.append("setsar=1")
    return plan


def _g_blur(ctx: FxContext, p: dict) -> FxPlan:
    """模糊渐入/渐出(离散 3 步 gblur enable 阶梯;gblur 支持 timeline)。"""
    plan = FxPlan()
    d = _fade_d(p, ctx, p.get("_slot", "in"))
    s = float(p.get("sigma", 12))
    steps = [(0.0, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 1.01)]
    for i, (a, b) in enumerate(steps):
        sig = s * (1 - (i + 1) / len(steps)) if p.get("_slot", "in") == "in" \
            else s * (i + 1) / len(steps)
        if sig < 0.3:
            continue
        if b > 1.0:
            plan.vf.append(f"gblur=sigma={sig:.2f}:enable='gte(t,{a * d:.3f})'")
        else:
            plan.vf.append(f"gblur=sigma={sig:.2f}:enable='between(t,{a * d:.3f},{b * d:.3f})'")
    fade = _g_fade(ctx, {**p, "_slot": p.get("_slot", "in")})
    plan.vf.extend(fade.vf)
    plan.notes.append("离散 3 步模糊阶梯(近似连续渐变;gblur 无时变 sigma)")
    return plan


def _g_slide(ctx: FxContext, p: dict) -> FxPlan:
    """滑入/滑出(复用段级 slides 机制;8 向 = 一个 fxId + dir 参数)。"""
    plan = FxPlan()
    dur = max(float(p.get("durMs", 400)) / 1000.0, 0.001)
    kind = p.get("_slot", "in")
    plan.slides.append((kind, str(p.get("dir", "up")), dur))
    fade = _g_fade(ctx, {**p, "durMs": dur * 500, "_slot": kind})
    plan.vf.extend(fade.vf)
    return plan


def _g_rotate(ctx: FxContext, p: dict) -> FxPlan:
    """旋转入/出场(perspective 近似的平面旋转;ow/oh 锁画幅,分册02 §3.2)。"""
    plan = FxPlan()
    d = _fade_d(p, ctx, p.get("_slot", "in"))
    a = float(p.get("angle", 0.3))
    slot = p.get("_slot", "in")
    if slot == "out":
        expr = f"{a * 0.4:.4f}*min(t/{d:.3f},1)"
    else:
        expr = f"(1-min(t/{d:.3f},1))*(-{a:.4f})"
    # 先放大 8% 再转再裁回:避免旋转露角
    plan.vf.append(f"scale={int(ctx.cw * 1.08) // 2 * 2}:{int(ctx.ch * 1.08) // 2 * 2}")
    plan.vf.append(f"rotate=a='{_xf(expr)}':c=black:ow=iw:oh=ih")
    plan.vf.append(f"crop={ctx.cw}:{ctx.ch}:(iw-ow)/2:(ih-oh)/2")
    fade = _g_fade(ctx, {**p, "_slot": slot})
    plan.vf.extend(fade.vf)
    return plan


def _g_pixel(ctx: FxContext, p: dict) -> FxPlan:
    """像素化入/出场(pixelize 支持 timeline,离散 3 档阶梯)。"""
    plan = FxPlan()
    d = _fade_d(p, ctx, p.get("_slot", "in"))
    blocks = [int(ctx.cw // f) for f in p.get("blocks", (64, 32, 16))]
    plan.vf.append(f"scale={ctx.cw}:{ctx.ch}")   # 保证下游窗口尺寸稳定
    for i, b in enumerate(blocks):
        a, z = d * i / 3.0, d * (i + 1) / 3.0
        if p.get("_slot", "in") == "in":
            en = f"between(t,{a:.3f},{z:.3f})"
        else:
            en = f"between(t,{ctx.out_s - d + a:.3f},{ctx.out_s - d + z:.3f})"
        plan.vf.append(f"pixelize=w={max(b, 2)}:h={max(int(ctx.ch * b / ctx.cw), 2)}"
                       f":mode=avg:enable='{en}'")
    fade = _g_fade(ctx, {**p, "_slot": p.get("_slot", "in")})
    plan.vf.extend(fade.vf)
    return plan


def _g_glitch(ctx: FxContext, p: dict) -> FxPlan:
    """RGB 故障(rgbashift + noise,enable 窗口限制在入/出场段)。"""
    plan = FxPlan()
    d = min(_fade_d(p, ctx, p.get("_slot", "in")), 0.6)
    slot = p.get("_slot", "in")
    en = f"lte(t,{d:.3f})" if slot == "in" else f"gte(t,{ctx.out_s - d:.3f})"
    rh = int(p.get("shift", 8))
    plan.vf.append(f"rgbashift=rh={rh}:bh=-{rh}:enable='{en}'")
    plan.vf.append(f"noise=alls={int(p.get('noise', 20))}:allf=t:enable='{en}'")
    return plan


def _g_chroma(ctx: FxContext, p: dict) -> FxPlan:
    """色散重影(rgbashift+chromashift,静态弱档;combo 域整段生效)。"""
    plan = FxPlan()
    shift = int(p.get("shift", 4))
    plan.vf.append(f"rgbashift=rh={shift}:bh=-{shift}")
    plan.vf.append(f"chromashift=cbh={max(shift // 2, 1)}:crh=-{max(shift // 2, 1)}")
    return plan


def _g_tvold(ctx: FxContext, p: dict) -> FxPlan:
    """老电视(噪声+微模糊+暗角,enable 窗口;出场变体加纵向收缩近似)。"""
    plan = FxPlan()
    slot = p.get("_slot", "in")
    d = min(_fade_d(p, ctx, slot), 0.8)
    en = f"lte(t,{d:.3f})" if slot == "in" else f"gte(t,{ctx.out_s - d:.3f})"
    plan.vf.append("noise=alls=30:allf=t:enable='" + en + "'")
    plan.vf.append(f"gblur=sigma=1.5:enable='{en}'")
    plan.vf.append(f"vignette=PI/4:enable='{en}'")
    return plan


def _g_bloom(ctx: FxContext, p: dict) -> FxPlan:
    """过曝/辉光家族(eq 提亮 + 大 sigma 模糊,enable 窗口;screen 混合待 fc 扩展)。"""
    plan = FxPlan()
    slot = p.get("_slot", "in")
    d = min(_fade_d(p, ctx, slot), 0.8)
    en = f"lte(t,{d:.3f})" if slot == "in" else f"gte(t,{ctx.out_s - d:.3f})"
    plan.vf.append(f"eq=brightness={float(p.get('brightness', 0.35)):.2f}:enable='{en}'")
    plan.vf.append(f"gblur=sigma={float(p.get('sigma', 15)):.1f}:enable='{en}'")
    plan.notes.append("近似档:eq 提亮 + 模糊近似过曝;screen 混合待 filter_complex 扩展")
    return plan


def _g_streak(ctx: FxContext, p: dict) -> FxPlan:
    """流光/拉光(avgblur 各向异性单方向,阶梯收缩;gblur 做不了单方向)。"""
    plan = FxPlan()
    slot = p.get("_slot", "in")
    d = min(_fade_d(p, ctx, slot), 0.6)
    horizontal = str(p.get("dir", "h")) == "h"
    for i, f in enumerate((1.0, 0.6, 0.3)):
        size = max(int(30 * f), 1)
        sx, sy = (size, 1) if horizontal else (1, size)
        a, z = d * i / 3.0, d * (i + 1) / 3.0
        if slot == "in":
            en = f"between(t,{a:.3f},{z:.3f})" if i < 2 else f"lte(t,{z:.3f})"
        else:
            base = ctx.out_s - d
            en = f"between(t,{base + a:.3f},{base + z:.3f})" if i < 2 else \
                f"gte(t,{base + a:.3f})"
        plan.vf.append(f"avgblur=sizeX={sx}:sizeY={sy}:enable='{en}'")
    fade = _g_fade(ctx, {**p, "_slot": slot})
    plan.vf.extend(fade.vf)
    return plan


def _g_frost(ctx: FxContext, p: dict) -> FxPlan:
    """冰霜/水墨(去饱和+冷调近似;真水墨 T2,分册02 §3.3 近似档允许存在)。"""
    plan = FxPlan()
    slot = p.get("_slot", "in")
    d = min(_fade_d(p, ctx, slot), 0.8)
    en = f"lte(t,{d:.3f})" if slot == "in" else f"gte(t,{ctx.out_s - d:.3f})"
    plan.vf.append(f"eq=saturation=0.55:brightness=0.04:enable='{en}'")
    plan.vf.append(f"gblur=sigma={float(p.get('sigma', 6)):.1f}:enable='{en}'")
    fade = _g_fade(ctx, {**p, "_slot": slot})
    plan.vf.extend(fade.vf)
    plan.notes.append("近似档:去饱和+模糊近似冰晶/水墨;真水墨待 T2")
    return plan


def _g_flip(ctx: FxContext, p: dict) -> FxPlan:
    """翻页(perspective 弱量近似;真 3D 翻页 T2,分册02 §3.3)。"""
    plan = FxPlan()
    d = _fade_d(p, ctx, p.get("_slot", "in"))
    slot = p.get("_slot", "in")
    u = (f"min(t/{d:.3f},1)" if slot == "in"
         else f"min(max((t-{ctx.out_s - d:.3f})/{d:.3f},0),1)")
    en = f"lte(t,{d:.3f})" if slot == "in" else f"gte(t,{ctx.out_s - d:.3f})"
    top = f"IH*0.06*(1-min(max(1-{_xf(u)},0),1))"       # 上边随进度压下的弱透视
    plan.vf.append(
        f"perspective=x0='0':y0='{_xf(top)}':x1='IW':y1='0':x2='0':y2='IH'"
        f":x3='IW':y3='{_xf(top)}':interpolation=bilinear:enable='{en}'")
    fade = _g_fade(ctx, {**p, "_slot": slot})
    plan.vf.extend(fade.vf)
    plan.notes.append("近似档:弱透视近似翻页;真 3D 翻页 T2(BookFlip GLSL 登记待收录)")
    return plan


def _g_wipe(ctx: FxContext, p: dict) -> FxPlan:
    """擦除揭示(drawbox 动态遮盖;入=遮盖收缩,出=遮盖扩张)。"""
    plan = FxPlan()
    d = _fade_d(p, ctx, p.get("_slot", "in"))
    slot = p.get("_slot", "in")
    u = f"min(t/{d:.3f},1)" if slot == "in" else f"min(max((t-{ctx.out_s - d:.3f})/{d:.3f},0),1)"
    color = str(p.get("color", "black"))
    dirn = str(p.get("dir", "left"))           # 遮盖条退出的方向
    if dirn in ("left", "right"):
        x = f"W*{u}" if dirn == "left" else "0"
        plan.vf.append(f"drawbox=x='{_xf(x)}':y=0:w='W*(1-{_xf(u)})':h=H:color={color}:t=fill")
    else:
        y = f"H*{u}" if dirn == "up" else "0"
        plan.vf.append(f"drawbox=x=0:y='{_xf(y)}':w=W:h='H*(1-{_xf(u)})':color={color}:t=fill")
    return plan


def _g_shake(ctx: FxContext, p: dict) -> FxPlan:
    """抖动(crop 表达式正弦位移 + 回填画幅;幅度 ≤4px)。"""
    plan = FxPlan()
    amp = min(float(p.get("amp", 3)), SHAKE_MAX_PX)
    freq = float(p.get("freq", 9))
    slot = p.get("_slot", "combo")
    d = min(_fade_d(p, ctx, slot), 0.8) if slot in ("in", "out") else ctx.out_s
    en = (f"lte(t,{d:.3f})" if slot == "in"
          else (f"gte(t,{ctx.out_s - d:.3f})" if slot == "out" else None))
    enx = f":enable='{en}'" if en else ""
    m = min(amp, 6)
    x_expr = _xf(f"{m + 1}+{amp}*sin(2*PI*t*{freq})")
    y_val = amp * 0.6
    f2 = freq * 1.3
    y_expr = _xf(f"{m + 1}+{y_val:.2f}*sin(2*PI*t*{f2:.2f}+1.3)")
    plan.vf.append(
        f"crop=iw-{2 * (m + 1)}:ih-{2 * (m + 1)}:"
        f"x='{x_expr}':y='{y_expr}'{enx}")
    plan.vf.append(f"scale={ctx.cw}:{ctx.ch}")
    plan.notes.append(f"幅度 ≤{SHAKE_MAX_PX}px(分册02 §3.2:幅度过大 = 廉价感)")
    return plan


def _g_swing(ctx: FxContext, p: dict) -> FxPlan:
    """钟摆(rotate 正弦摆动,中心锚)。"""
    plan = FxPlan()
    amp = min(float(p.get("angle", 0.05)), 0.08)
    plan.vf.append(f"scale={int(ctx.cw * 1.08) // 2 * 2}:{int(ctx.ch * 1.08) // 2 * 2}")
    plan.vf.append(f"rotate=a='{_xf(f'{amp:.4f}*sin(2*PI*t/3)')}':c=black:ow=iw:oh=ih")
    plan.vf.append(f"crop={ctx.cw}:{ctx.ch}:(iw-ow)/2:(ih-oh)/2")
    return plan


def _g_grain(ctx: FxContext, p: dict) -> FxPlan:
    """胶片颗粒(combo 域整段轻噪声)。"""
    return FxPlan(vf=[f"noise=alls={int(p.get('alls', 7))}:allf=t+u"])


def _g_vignette(ctx: FxContext, p: dict) -> FxPlan:
    """暗角(combo 域)。"""
    return FxPlan(vf=[f"vignette={p.get('angle', 'PI/5')}"])


def _g_lut(ctx: FxContext, p: dict) -> FxPlan:
    """暖/冷调(colorbalance 静态;combo 域整段生效)。"""
    warm = str(p.get("temp", "warm")) == "warm"
    b, r, g = ("0.15", "0.10", "-0.06") if warm else ("-0.12", "-0.06", "0.05")
    return FxPlan(vf=[f"colorbalance=rs={r}:gs={g}:bs={b}:rm={r}:gm={g}:bm={b}"])


def _g_freeze(ctx: FxContext, p: dict) -> FxPlan:
    """定格(freeze.hold → 复用既有 freezeMs 机制,ADR-0027 同一能力合并)。"""
    ms = float(p.get("freezeMs", 600))
    return FxPlan(params={"freezeMs": ms},
                  notes=["与 freezeMs(ADR-0027)同一能力:注册表只做参数落地"])


def _g_pip(ctx: FxContext, p: dict) -> FxPlan:
    """画中画(split 缩小贴角;fc 模板,占位 {IN}/{OUT} 由渲染端灌实标签)。"""
    sc = float(p.get("scale", 0.3))
    w = max(int(ctx.cw * sc) // 2 * 2, 16)
    pos = str(p.get("pos", "br"))
    ox = f"main_w-overlay_w-{int(ctx.cw * 0.05)}" if "r" in pos else str(int(ctx.cw * 0.05))
    oy = f"main_h-overlay_h-{int(ctx.ch * 0.05)}" if "b" in pos else str(int(ctx.ch * 0.05))
    plan = FxPlan()
    plan.fc = [f"[{{IN}}]split=2[pip_bg][pip_fg];"
               f"[pip_fg]scale={w}:-2[pip_small];"
               f"[pip_bg][pip_small]overlay=x='{ox}':y='{oy}'[{{OUT}}]"]
    plan.notes.append("画中画:自身缩小贴角;双源画中画待 overlay 轨(rs_edit overlay.add 已可表达)")
    return plan


def _g_split_screen(ctx: FxContext, p: dict) -> FxPlan:
    """分屏对比(左右各半;fx.split.screen / fx.compare.slider 前后对比)。"""
    plan = FxPlan()
    graded = bool(p.get("graded", True))       # compare.slider:右半做调色示意"后"
    eq = "eq=brightness=0.08:saturation=1.25," if graded else ""
    plan.fc = [f"[{{IN}}]split=2[cmp_l][cmp_r];"
               f"[cmp_l]crop=iw/2:ih:0:0[cmp_lc];"
               f"[cmp_r]{eq}crop=iw/2:ih:iw/2:0[cmp_rc];"
               f"[cmp_lc][cmp_rc]hstack=inputs=2[{{OUT}}]"]
    plan.notes.append("静态分隔线(可拖滑块交互待交付端);教程/测评对比语义(分册02 §6.4)")
    return plan


def _g_collapse(ctx: FxContext, p: dict) -> FxPlan:
    """收拢/收缩出场(fc:动态缩到中心 + 黑底)。"""
    plan = FxPlan()
    d = _fade_d(p, ctx, "out")
    u = f"min(max((t-{ctx.out_s - d:.3f})/{d:.3f},0),1)"
    plan.fc = [f"[{{IN}}]split=2[cps_bg][cps_fg];"
               f"[cps_bg]drawbox=c=black:t=fill[cps_black];"
               f"[cps_fg]scale=w='max(iw*(1-0.35*{_xf(u)}),2)'"
               f":h='max(ih*(1-0.35*{_xf(u)}),2)':eval=frame[cps_small];"
               f"[cps_black][cps_small]overlay=x='(W-w)/2':y='(H-h)/2'[{{OUT}}]"]
    fade = _g_fade(ctx, {**p, "_slot": "out"})
    plan.vf.extend(fade.vf)
    return plan


def _g_letterbox(ctx: FxContext, p: dict) -> FxPlan:
    """信箱收黑(上下黑边合拢 + 淡出)。"""
    plan = FxPlan()
    d = _fade_d(p, ctx, "out")
    u = f"min(max((t-{ctx.out_s - d:.3f})/{d:.3f},0),1)"
    band = float(p.get("band", 0.12))
    plan.vf.append(f"drawbox=x=0:y=0:w=W:h='H*{band}*{_xf(u)}':color=black:t=fill")
    plan.vf.append(f"drawbox=x=0:y='H-H*{band}*{_xf(u)}':w=W:h='H*{band}*{_xf(u)}'"
                   ":color=black:t=fill")
    fade = _g_fade(ctx, {**p, "_slot": "out"})
    plan.vf.extend(fade.vf)
    return plan


def _g_stretch(ctx: FxContext, p: dict) -> FxPlan:
    """拉伸消散(fc:动态横拉/纵压 + 黑底;变形量 ≤30%,分册02 §3.2)。"""
    plan = FxPlan()
    d = _fade_d(p, ctx, p.get("_slot", "out"))
    slot = p.get("_slot", "out")
    amp = min(float(p.get("amp", 0.3)), STRETCH_MAX)
    if slot == "out":
        u = f"(1-min(max((t-{ctx.out_s - d:.3f})/{d:.3f},0),1))"
    else:
        u = f"(1-min(t/{d:.3f},1))"
    w_expr = f"max(iw*(1+{amp}*(1-{u})),2)" if False else \
        f"max(iw*(1+{amp}*{u}),2)"
    h_expr = f"max(ih*(1-{amp}*{u}),2)"
    plan.fc = [f"[{{IN}}]split=2[str_bg][str_fg];"
               f"[str_bg]drawbox=c=black:t=fill[str_bgb];"
               f"[str_fg]scale=w='trunc({w_expr}/2)*2':h='trunc({h_expr}/2)*2'"
               f":eval=frame[str_small];"
               f"[str_bgb][str_small]overlay=x='(W-w)/2':y='(H-h)/2'[{{OUT}}]"]
    fade = _g_fade(ctx, {**p, "_slot": slot})
    plan.vf.extend(fade.vf)
    plan.notes.append(f"变形量 ≤{STRETCH_MAX:.0%}(分册02 §3.2:超过则面目全非)")
    return plan


def _g_axisblur(ctx: FxContext, p: dict) -> FxPlan:
    """纵拉/横拉模糊(avgblur 各向异性;gblur 做不了单方向,分册02 §3.2)。"""
    plan = FxPlan()
    slot = p.get("_slot", "in")
    d = min(_fade_d(p, ctx, slot), 0.8)
    vertical = str(p.get("dir", "v")) == "v"
    for i, f in enumerate((1.0, 0.6, 0.3)):
        size = max(int(15 * f), 1)
        sx, sy = (1, size) if vertical else (size, 1)
        a, z = d * i / 3.0, d * (i + 1) / 3.0
        if slot == "in":
            en = f"between(t,{a:.3f},{z:.3f})" if i < 2 else f"lte(t,{z:.3f})"
        else:
            base = ctx.out_s - d
            en = (f"between(t,{base + a:.3f},{base + z:.3f})" if i < 2
                  else f"gte(t,{base + a:.3f})")
        plan.vf.append(f"avgblur=sizeX={sx}:sizeY={sy}:enable='{en}'")
    fade = _g_fade(ctx, {**p, "_slot": slot})
    plan.vf.extend(fade.vf)
    return plan


GENERATORS = {
    "fade": _g_fade, "flash": _g_flash, "zoompan": _g_zoompan, "blur": _g_blur,
    "slide": _g_slide, "rotate": _g_rotate, "pixel": _g_pixel, "glitch": _g_glitch,
    "chroma": _g_chroma, "tvold": _g_tvold, "bloom": _g_bloom, "streak": _g_streak,
    "frost": _g_frost, "flip": _g_flip, "wipe": _g_wipe, "shake": _g_shake,
    "swing": _g_swing, "grain": _g_grain, "vignette": _g_vignette, "lut": _g_lut,
    "freeze": _g_freeze, "pip": _g_pip, "split_screen": _g_split_screen,
    "collapse": _g_collapse, "letterbox": _g_letterbox,
    "stretch": _g_stretch, "axisblur": _g_axisblur,
}


def _C(fxid, gen, slot, label, params=None, domain="render", tier="T1", note=None,
       usage=None, flashy=False):
    """CLIP_FX 表项构造(缺省 render 域 T1)。"""
    return {"fxId": fxid, "gen": gen, "slot": slot, "label": label,
            "params": dict(params or {}), "domain": domain, "tier": tier,
            "note": note, "usage": list(usage or []), "flashy": flashy}


CLIP_FX: dict[str, dict] = {}
for _spec in [
    # ---- 基础(分册02 §3.1) ----
    _C("fade.in", "fade", "any", "渐显", {"durMs": 500},
       usage=["talking-head", "tutorial", "vlog"]),
    _C("fade.out", "fade", "any", "渐隐", {"durMs": 500}),
    _C("fade.in.soft", "fade", "in", "柔光渐显", {"durMs": 600, "soft": True},
       note="中间调提亮近似柔光"),
    _C("dissolve.in", "fade", "in", "叠化入场", {"durMs": 500, "alpha": True},
       note="叠化本质是转场;单片段 alpha 版,拼接场景用 tr.dissolve.cross"),
    _C("flash.in.white", "flash", "in", "闪白入场", {"durMs": 240, "color": "white"}),
    _C("flash.out.white", "flash", "out", "闪白出场", {"durMs": 240, "color": "white"}),
    _C("flash.in.black", "flash", "in", "闪黑入场", {"durMs": 240, "color": "black"}),
    _C("flash.out.black", "flash", "out", "闪黑出场", {"durMs": 240, "color": "black"}),
    _C("dither.out", "blur", "out", "飘散消散", {"durMs": 600, "sigma": 12},
       note="近似档:淡出+模糊;真飘散粒子 T2"),
    # ---- 运镜(分册02 §3.2) ----
    _C("zoompan.in.slight", "zoompan", "in", "轻微放大", {"z0": 1.0, "z1": 1.06},
       usage=["talking-head", "tutorial"]),
    _C("zoompan.out.shrink", "zoompan", "out", "缩小出场", {"z0": 1.0, "z1": 0.94},
       note="旋转版走 T2(登记待实现)"),
    _C("push.in", "zoompan", "in", "推近", {"z0": 1.0, "z1": 1.12},
       note="与 punch-in 手法共用参数;密度受 15s/≤3 约束"),
    _C("pull.out", "zoompan", "out", "拉远", {"z0": 1.12, "z1": 1.0}),
    _C("in.scale.pop", "zoompan", "in", "缩放入场(无位移)", {"z0": 0.9, "z1": 1.0},
       usage=["talking-head", "drama"]),
    _C("out.scale.shrink", "zoompan", "out", "收缩消失", {"z0": 1.0, "z1": 0.92},
       note="分册02 §6.3:与 fade 成对"),
    _C("in.anchor.scale", "zoompan", "in", "锚点缩放", {"z0": 0.94, "z1": 1.0, "anchorX": 0.5, "anchorY": 0.5},
       note="锚点默认中心(center-framing,分册06 §6.1)"),
    _C("fx.kenburns", "zoompan", "combo", "Ken Burns 慢推", {"z0": 1.0, "z1": 1.08},
       domain="render", usage=["vlog", "talking-head"]),
    _C("in.breathe", "zoompan", "combo", "呼吸/浮动", {"curve": "breathe", "amp": 0.02},
       note="幅度 ≤2%(分册02 §6.2)"),
    _C("fx.float", "zoompan", "combo", "悬浮微动", {"curve": "breathe", "amp": 0.015}),
    _C("in.bounce.pop", "zoompan", "in", "弹跳 Pop", {"curve": "pop"},
       note="超调 ≤1.1(与 artboard 动画十二法则一致)"),
    _C("in.blur.in", "blur", "in", "模糊淡入", {"durMs": 600, "sigma": 12},
       usage=["talking-head", "vlog"]),
    _C("out.blur.out", "blur", "out", "模糊淡出", {"durMs": 600, "sigma": 12}),
    _C("blur.in.focus", "blur", "in", "模糊聚焦", {"durMs": 700, "sigma": 14},
       note="模糊+缩放组合的 T1 近似(纯模糊档)"),
    _C("in.slide.left", "slide", "in", "左滑入场", {"dir": "left", "durMs": 400}),
    _C("in.slide.right", "slide", "in", "右滑入场", {"dir": "right", "durMs": 400}),
    _C("in.slide.up", "slide", "in", "上滑入场", {"dir": "up", "durMs": 400}),
    _C("in.slide.down", "slide", "in", "下滑入场", {"dir": "down", "durMs": 400}),
    _C("out.slide.left", "slide", "out", "左滑出场", {"dir": "left", "durMs": 400}),
    _C("out.slide.right", "slide", "out", "右滑出场", {"dir": "right", "durMs": 400}),
    _C("out.slide.up", "slide", "out", "上滑出场", {"dir": "up", "durMs": 400}),
    _C("out.slide.off", "slide", "out", "滑出画面", {"dir": "right", "durMs": 450},
       note="分册02 §6.3;方向延续入场(artboard §九)"),
    _C("in.fade.up", "slide", "in", "上浮淡入", {"dir": "up", "durMs": 500},
       usage=["talking-head", "vlog", "drama"],
       note="文字/卡片最常用入场(分册02 §6.2:从下入=浮现,最中性)"),
    _C("in.fade.down", "slide", "in", "下沉淡入", {"dir": "down", "durMs": 500}),
    _C("in.fade.left", "slide", "in", "左移淡入", {"dir": "left", "durMs": 500}),
    _C("in.fade.right", "slide", "in", "右移淡入", {"dir": "right", "durMs": 500}),
    _C("out.fade.up", "slide", "out", "上浮出场", {"dir": "up", "durMs": 400}),
    _C("out.fade.down", "slide", "out", "下沉出场", {"dir": "down", "durMs": 400}),
    _C("out.fade.left", "slide", "out", "左移出场", {"dir": "left", "durMs": 400}),
    _C("out.fade.right", "slide", "out", "右移出场", {"dir": "right", "durMs": 400}),
    _C("rotate.in", "rotate", "in", "旋转入场", {"angle": 0.3, "durMs": 500}),
    _C("rotate.out", "rotate", "out", "旋转出场", {"angle": 0.3, "durMs": 450}),
    _C("out.spin.fade", "rotate", "out", "旋转淡出", {"angle": 0.25, "durMs": 450}),
    _C("in.wipe.dir", "wipe", "in", "擦除入场", {"dir": "left", "durMs": 500}),
    _C("out.wipe.dir", "wipe", "out", "擦除出场", {"dir": "right", "durMs": 450}),
    _C("in.clip.reveal", "wipe", "in", "裁切揭示", {"dir": "right", "durMs": 550}),
    _C("effect.wipe.scan", "wipe", "in", "雨刷扫光", {"dir": "up", "durMs": 500},
       note="单片段 crop/遮罩版;双片段拼接用 tr.wipe.* / tr.slice.*"),
    _C("jitter.shake", "shake", "combo", "轻微抖动", {"amp": 3, "freq": 9}),
    _C("fx.shake.emphasis", "shake", "in", "强调抖动", {"amp": 4, "freq": 12, "durMs": 400},
       flashy=True),
    _C("swing.pendulum", "swing", "combo", "钟摆", {"angle": 0.05}),
    _C("stretch.zoom", "stretch", "out", "拉伸消散", {"amp": 0.3, "durMs": 500},
       note="fc 动态横拉;变形量 ≤30%"),
    _C("blur.axis", "axisblur", "in", "纵拉模糊", {"dir": "v", "durMs": 500},
       note="avgblur 各向异性(sizeX/sizeY);gblur 无方向"),
    _C("blur.axis.h", "axisblur", "out", "横拉模糊出场", {"dir": "h", "durMs": 500}),
    # ---- 特效类(分册02 §3.3) ----
    _C("effect.pixel.reveal", "pixel", "in", "像素扩散入场", {"durMs": 600},
       note="像素族统一一条(含 马赛克/块状消融 同族合并)"),
    _C("effect.mosaic.dissolve", "pixel", "out", "马赛克消融出场", {"durMs": 600}),
    _C("effect.glitch.rgb", "glitch", "in", "RGB 故障入场", {"durMs": 500, "shift": 8},
       flashy=True),
    _C("effect.glitch.exit", "glitch", "out", "故障退出", {"durMs": 500, "shift": 8},
       flashy=True),
    _C("effect.chroma.split", "chroma", "combo", "色散重影", {"shift": 4}),
    _C("fx.chromatic", "chroma", "combo", "色差(整段)", {"shift": 3}),
    _C("effect.tv.old", "tvold", "in", "老电视", {"durMs": 700}),
    _C("effect.tv.off", "tvold", "out", "电视息屏", {"durMs": 600},
       note="纵向收缩版待 fc 扩展;先落噪声+暗角近似档"),
    _C("effect.exposure.bloom", "bloom", "in", "过曝消散", {"durMs": 700}),
    _C("effect.glow.halo", "bloom", "in", "辉光光晕", {"durMs": 600, "sigma": 20, "brightness": 0.15}),
    _C("in.glow", "bloom", "in", "辉光入场", {"durMs": 500, "sigma": 10, "brightness": 0.12}),
    _C("effect.light.streak", "streak", "in", "流光扫过", {"dir": "h", "durMs": 600}),
    _C("effect.ice.frost", "frost", "in", "冰晶(近似)", {"durMs": 700},
       note="近似档(分册02 §3.3):真冰晶属拟物类不实现;此为冷调去饱和近似"),
    _C("effect.drip.ink", "frost", "in", "水墨(近似)", {"durMs": 800, "sigma": 10},
       note="近似档;真水墨 T2(分册02 §3.3 注明近似)"),
    _C("effect.page.flip", "flip", "in", "翻页(透视近似)", {"durMs": 600},
       note="近似档;真 3D 翻页 T2"),
    _C("effect.freeze.frame", "freeze", "any", "拍照定格", {"freezeMs": 600},
       note="与 freezeMs(ADR-0027)同一能力,合并登记"),
    _C("fx.freeze.hold", "freeze", "combo", "定格持住", {"freezeMs": 800}),
    # ---- combo 域整段效果(§6.4 归口) ----
    _C("fx.film.grain", "grain", "combo", "胶片颗粒", {"alls": 7}),
    _C("fx.vignette", "vignette", "combo", "暗角", {"angle": "PI/5"}),
    _C("fx.lut.warm", "lut", "combo", "暖调", {"temp": "warm"}),
    _C("fx.lut.cool", "lut", "combo", "冷调", {"temp": "cool"}),
    _C("fx.pip", "pip", "combo", "画中画", {"scale": 0.3, "pos": "br"}),
    _C("fx.split.screen", "split_screen", "combo", "分屏对比", {"graded": False}),
    _C("fx.compare.slider", "split_screen", "combo", "对比滑块(前后对比)", {"graded": True},
       usage=["tutorial", "screen-recording"],
       note="右半调色示意『后』;教程/测评/带货最高频(分册02 §6.4)"),
    _C("out.collapse", "collapse", "out", "收拢", {"durMs": 500},
       note="squeezeh/squeezev 的出场近似(动态收缩)"),
    _C("out.letterbox", "letterbox", "out", "信箱收黑", {"durMs": 600, "band": 0.12}),
    # ---- 字幕域(S7 消费;fxId 注册占位保契约唯一,分册01 花字基础档) ----
    _C("in.text.char", "fade", "in", "逐字入场", {"durMs": 400}, domain="subtitle",
       note="落点 S7 卡拉OK 逐字(\\kf);effects_plan 把它映射为 subtitle.karaoke"),
    _C("in.text.word", "fade", "in", "逐词入场", {"durMs": 400}, domain="subtitle",
       note="落点 S7 逐字时间的词级聚合(近似)"),
    _C("in.text.line", "fade", "in", "逐行入场", {"durMs": 400}, domain="subtitle",
       note="落点 S7 分行 Dialogue(断行=stagger 三合一)"),
    _C("in.typewriter", "fade", "in", "打字机", {"durMs": 400}, domain="subtitle",
       note="落点 S7 花字 \\t 逐字动画(huazi 基础档)"),
    _C("in.count.up", "fade", "in", "数字滚动", {"durMs": 600}, domain="subtitle",
       note="落点 分册01 huazi.data.count 数据卡"),
    _C("in.lower.third", "fade", "in", "人名条", {"durMs": 400}, domain="subtitle",
       note="落点 S7 文本轨两行 lower third(标题+副题,访谈/新闻)"),
    # ---- T2 占位(需着色器/素材/字幕侧逐元素表达,本轮未落地;fxId 注册保契约唯一,
    # 目录标「登记待实现」;渲染端 build 时 fxDegraded 留痕) ----
    _C("in.mask.reveal", "fade", "in", "遮罩揭示", {"durMs": 500}, tier="T2", domain="element",
       note="任意路径遮罩需 GLSL/形状遮罩;clip 级 T2 未落地"),
    _C("out.mask.close", "fade", "out", "遮罩收拢", {"durMs": 500}, tier="T2", domain="element"),
    _C("in.stroke.draw", "fade", "in", "描边绘制", {"durMs": 600}, tier="T2", domain="artboard",
       note="SVG stroke-dashoffset → artboard 更合适(分册02 §6.2)"),
    _C("effect.speed.ramp", "fade", "any", "变速时空", {"durMs": 400}, tier="T2", domain="render",
       note="setpts 变速改段时长,与零漂移硬约束冲突;待 IR 变速区间表达升版"),
    _C("in.stagger", "fade", "in", "错峰入场", {"durMs": 400}, tier="T2", domain="subtitle",
       note="多元素逐个 delay 80–150ms;需字幕/卡片侧逐元素动画表达(未接),"
            "总量 ≤800ms 纪律见分册02 §6.2"),
    _C("out.stagger", "fade", "out", "错峰出场(反序)", {"durMs": 400}, tier="T2", domain="subtitle",
       note="后进先出,骨架最后退(artboard §九);落点同 in.stagger"),
    _C("fx.spotlight", "vignette", "combo", "聚光灯跟随", {}, tier="T2", domain="render",
       note="radial 遮罩位移需 fc/mask;禁 geq(ADR-0022)→ 待 fc 遮罩扩展"),
    _C("in.progress.bar", "fade", "in", "进度条生长", {"durMs": 500}, tier="T2", domain="element",
       note="落点 elements/ 进度条元素 + 元素轨动画表达(未接,分册02 §6.2)"),
    _C("in.ring.progress", "fade", "in", "环形进度生长", {"durMs": 600}, tier="T2", domain="element"),
    _C("in.cursor.click", "fade", "in", "点击涟漪", {"durMs": 400}, tier="T2", domain="element",
       note="落点 elements/ 涟漪序列帧(对应 v1 screen-recording);元素轨表达未接"),
    _C("in.keystroke", "fade", "in", "按键提示弹出", {"durMs": 400}, tier="T2", domain="element",
       note="screen.keys 卡已存在(v1);作为通用 fxId 的元素轨表达未接"),
    _C("in.shadow.drop", "fade", "in", "投影落下", {"durMs": 450}, tier="T2", domain="element"),
    _C("effect.split.grid", "fade", "in", "分屏网格入场", {"durMs": 600}, tier="T2",
       domain="render",
       note="xstack 分块错峰(>6 块成本显著上升,分册02 §3.3);待 fc 分块扩展"),
    _C("in.skew", "flip", "in", "斜切入场", {"durMs": 450},
       note="perspective 弱量档(与翻页同生成器,低幅度)"),
]:
    if _spec["fxId"] in CLIP_FX:
        raise RuntimeError(f"特效 fxId 重复登记:{_spec['fxId']}")
    CLIP_FX[_spec["fxId"]] = _spec


def transition_ids() -> set[str]:
    return set(TRANSITIONS)


def clip_fx_ids() -> set[str]:
    return set(CLIP_FX)


def registered_ids() -> set[str]:
    return transition_ids() | clip_fx_ids()


def glsl_enabled(doc: dict) -> bool:
    """IR 顶层 effects.glsl 开关(schema 已声明;false = 全部 T2 降级 T1)。"""
    return bool((doc.get("effects") or {}).get("glsl", True))


# ---------------------------------------------------------------- 解析

def resolve_transition(tr: dict, *, glsl_on: bool = True,
                       deps_ready: bool | None = None) -> dict:
    """transition 对象 → 解析结果(整表注册版;替代 M14 的 6 条直通表)。

    返回:{kind, xfade|glsl, fxId, durMs, flashy, note, boundary, tier, fallback}
      kind ∈ cut|none|xfade|concatvideo|glsl;cut/none 语义不变(显式硬切);
      tr 无 fx 时按 type 枚举回退(既有 IR 行为零变化)。
    deps_ready=T2 组件(moderngl)部署态:False → glsl 条目回退 fallback 并给 note。
    """
    fx = str(tr.get("fx") or "").strip()
    base = str(tr.get("type", "fade")).lower()
    if not fx:
        if base in ("cut", "none"):
            return {"kind": base, "fxId": None, "durMs": 0, "flashy": False}
        if base in TRANSITIONS:                     # type 枚举撞表(tr.fade 等)
            e = TRANSITIONS[base]
            return _resolve_entry(e, tr, glsl_on=True, deps_ready=True)
        return {"kind": "xfade", "xfade": base, "fxId": None,
                "durMs": float(tr.get("durMs", 500)), "flashy": False}
    e = TRANSITIONS.get(fx)
    if e is None:
        raise FxError("FX_UNREGISTERED",
                      f"转场 fxId 未注册:{fx}(rs_fx.registry 整表查无此键;"
                      "先 rs_effects.py search 确认,再 rs_edit transition.set --fx)")
    return _resolve_entry(e, tr, glsl_on, deps_ready)


def _resolve_entry(e: dict, tr: dict, glsl_on: bool, deps_ready: bool | None) -> dict:
    kind = e["kind"]
    res = {"fxId": e["fxId"], "tier": e.get("tier", "T1"), "label": e.get("label", ""),
           "flashy": bool(e.get("flashy")), "note": e.get("note"),
           "boundary": e.get("boundary"), "domain": e.get("domain"),
           "durMs": float(tr.get("durMs", e.get("durMs", 500)))}
    if kind in ("cut", "none"):
        res.update(kind=kind)
        return res
    if kind == "glsl":
        if glsl_on and deps_ready is not False:
            res.update(kind="glsl", glsl=e["glsl"])
            return res
        fb = e.get("fallback", "fade")
        res.update(kind="xfade", xfade=fb,
                   degraded=True,
                   note=(f"T2({e['glsl']})不可用"
                         + ("" if glsl_on else "(effects.glsl=false)")
                         + ("" if deps_ready is not False else "(moderngl 缺失)")
                         + f" → 降级 T1 xfade={fb};fxDegraded 留痕"))
        return res
    if kind == "concatvideo":
        res.update(kind="concatvideo")
        return res
    res.update(kind="xfade", xfade=e["xfade"])
    return res


def resolve_transition_type(tr: dict, *, glsl_on: bool = True,
                            deps_ready: bool | None = None) -> tuple[str, str | None]:
    """向后兼容的 (生效类型, 留痕) 口径 —— rs_render._transition_type 委托到此。

    生效类型 ∈ cut|none|xfade 名|concatvideo|"glsl:<名>"(T2 就绪时);
    留痕 = fx/type 并存说明 或 降级说明。
    """
    fx = str(tr.get("fx") or "").strip()
    base = str(tr.get("type", "fade")).lower()
    r = resolve_transition(tr, glsl_on=glsl_on, deps_ready=deps_ready)
    note = None
    if fx and base and r.get("kind") not in ("cut", "none"):
        expect = TRANSITIONS.get(fx, {}).get("xfade")
        if expect and base != expect:
            note = f"fx({fx})与 type({base})并存,以 fx 为准"
    if r.get("degraded"):
        note = r.get("note")
    if r["kind"] == "glsl":
        return f"glsl:{r['glsl']}", note
    if r["kind"] in ("cut", "none"):
        return r["kind"], note
    if r["kind"] == "concatvideo":
        return "concatvideo", note
    return str(r["xfade"]), note


def build_clip_fx(decls: list[tuple[str, str, dict]], ctx: FxContext) -> tuple[FxPlan, list[str]]:
    """单片段特效声明 → FxPlan(渲染端 plan 阶段消费)。

    decls = [(slot, fxId, params)];slot ∈ in|out|combo。
    未注册 → FxError(FX_UNREGISTERED) 报错不静默;字幕/元素域 fxId 返回空计划 +
    域留痕(消费点在 S7 字幕链/元素轨,渲染端不吞也不渲)。
    """
    plan, notes = FxPlan(), []
    for slot, fxid, user_params in decls:
        e = CLIP_FX.get(fxid)
        if e is None:
            raise FxError("FX_UNREGISTERED",
                          f"特效 fxId 未注册:{fxid}(rs_fx.registry 查无此键;"
                          "先 rs_effects.py search 确认目录状态)")
        if e["tier"] == "T2":
            notes.append(f"fxId {fxid} 为 T2/{e['domain']} 域,本轮未落地 → fxDegraded 留痕")
            plan.notes.append(fxid)
            continue
        if e["domain"] != "render":
            notes.append(f"fxId {fxid} 消费域={e['domain']}(S7 字幕链/元素轨),渲染端不重复消费")
            continue
        p = {**e["params"], **(user_params or {})}
        p["_slot"] = slot if slot in ("in", "out") else _default_slot(e["slot"])
        gen = GENERATORS.get(e["gen"])
        if gen is None:
            raise FxError("FX_GENERATOR_MISSING", f"fxId {fxid} 的生成器缺失:{e['gen']}")
        sub = gen(ctx, p)
        plan.vf.extend(sub.vf)
        plan.fc.extend(sub.fc)
        plan.slides.extend((s[0], s[1], s[2]) for s in sub.slides)
        plan.params.update(sub.params)
        for n in ([e["note"]] if e.get("note") else []) + sub.notes:
            if n:
                plan.notes.append(n)
    return plan, notes


def _default_slot(entry_slot: str) -> str:
    return entry_slot if entry_slot in ("in", "out") else "combo"


# ---------------------------------------------------------------- fc 模板灌入

def apply_fc_templates(plan: FxPlan, in_label: str, alloc) -> tuple[list[str], str]:
    """FxPlan.fc 模板 → 实标签 filter_complex 段;返回(段列表, 最终输出标签)。

    模板内占位:{IN}=当前链输入标签,{OUT}=分配的新输出标签。
    alloc = 标签分配器(callable() -> str),渲染端保证唯一。
    """
    parts: list[str] = []
    cur = in_label
    for tpl in plan.fc:
        out = alloc()
        parts.append(tpl.replace("{IN}", cur).replace("{OUT}", out))
        cur = out
    return parts, cur
