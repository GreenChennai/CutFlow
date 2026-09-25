"""T2 GLSL 渲染器 —— B3 路径(分册02 §1.2,用户 2026-09-26 定案):
Python `moderngl` + gl-transitions(MIT)的 GLSL → 只渲**转场重叠区**中间 MP4 →
交给现有 concat 链。

零漂移模型(与 joinCrossfadeMs 尾帧扩展共享,ADR-0023):
  xfade 语义:输入1 的第 qa 帧起与输入2 的第 0 帧交叉,持续 tdur 帧;
  B3 替换:out = in1[0:qa] + overlap(tdur 帧) + in2[td:qb]
        → 总长 = qa + td + (qb - td) = qa + qb(恒等,零漂移)。
  本模块只负责 overlap 段:输入两段视频文件与帧区间,输出恰为 tdur 帧的 MP4。

依赖纪律(ADR-0049 懒加载三态):moderngl 缺失/GL 不可用/着色器编译失败 →
available() 返回 False 或 render_overlap 抛 GLUnavailable;调用方(rs_render)
降级到注册表声明的最接近 T1 xfade 并 fxDegraded 留痕,绝不半途假绿。

本模块不 import rs_render/rs_common(被 rs_render 单向引用,防循环);
ffmpeg 二进制路径由调用方注入。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

# gl-transitions 统一头:提供 progress/getFromColor/getToColor 与约定 uniforms。
# 兼容层:老源用 texture2D/gl_FragColor,330 语义下宏替换;main() 调 transition()。
_HEADER = """#version 330
in vec2 v_uv;
out vec4 fragColor;
uniform sampler2D from;
uniform sampler2D to;
uniform float progress;
uniform float ratio;      // 画幅宽高比(部分 gl-transition 源直接引用)
#define texture2D texture
vec4 getFromColor(vec2 uv) { return texture(from, v_uv); }
vec4 getToColor(vec2 uv)   { return texture(to, v_uv); }
"""


class GLUnavailable(Exception):
    """moderngl 缺失 / 无 GL 上下文 / 着色器编译失败(调用方降级 T1 + 留痕)。"""


def _import_moderngl():
    try:
        import moderngl  # noqa: PLC0415 — ADR-0049 懒加载
        return moderngl
    except Exception as exc:  # noqa: BLE001 — ImportError 及上下文层失败统一三态
        raise GLUnavailable(f"moderngl 不可用:{exc}") from exc


def available() -> bool:
    """组件部署探针(轻量:只探 import 与 standlone 上下文创建,不渲帧)。"""
    try:
        mgl = _import_moderngl()
        ctx = mgl.create_standalone_context()
        ctx.release()
        return True
    except Exception:  # noqa: BLE001 — 缺组件/无 GL → 三态 MISSING
        return False


# ---------------------------------------------------------------- GLSL 装配

_UNIFORM_RE = re.compile(r"uniform\s+(\w+)\s+(\w+)\s*;\s*//\s*=\s*([^;\n]+)")


def load_glsl_source(name: str, glsl_dir: Path) -> str:
    """读收录的 gl-transition 源(GLSL 目录缺失/文件缺失 → GLUnavailable)。"""
    p = glsl_dir / f"{name}.glsl"
    if not p.is_file():
        raise GLUnavailable(f"GLSL 未收录:{p}")
    return p.read_text(encoding="utf-8", errors="replace")


def build_fragment_shader(name: str, glsl_dir: Path, params: dict | None = None) -> str:
    """gl-transition 源 → 可编译 fragment shader:
    源内 uniform(带 `// = value` 默认注释)编译期常量化为 const(渲染期不可变,
    params 可在构建期覆写);声明行剔除后与统一头拼装;main() 调 transition(v_uv)。
    用 const 而非 #define:个别源(CrossZoom)会以同名局部变量遮蔽 uniform,
    const 全局 + 局部遮蔽是合法 GLSL,#define 则会把声明处炸成语法错。"""
    src = load_glsl_source(name, glsl_dir)
    consts, kept = [], []
    for line in src.splitlines():
        m = _UNIFORM_RE.search(line)
        if m:
            typ, uname, val = m.group(1), m.group(2), m.group(3).strip()
            if typ == "sampler2D":
                consts.append(f"// skip sampler2D {uname}(外置贴图,不支持)")
                continue                       # 声明行不保留(const 已替代)
            v = str((params or {}).get(uname, val))
            consts.append(f"const {typ} {uname} = {v};")
            continue
        if re.match(r"\s*uniform\s+\w+\s+\w+\s*;", line):
            continue                           # 无默认值的 uniform 声明:剔除
        kept.append(line)
    frag = (_HEADER + "\n".join(consts) + "\n"
            + _strip_source_pragmas("\n".join(kept))
            + "\nvoid main() { fragColor = transition(v_uv); }\n")
    return frag


def _strip_source_pragmas(src: str) -> str:
    """去掉源里的 #ifdef GL_ES precision 头(我们固定 #version 330)。"""
    out = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("#ifdef") or s.startswith("#endif") or s.startswith("precision"):
            continue
        out.append(line)
    return "\n".join(out)


def validate_shader(name: str, glsl_dir: Path) -> tuple[bool, str]:
    """逐条验证:真实编译一次着色器(不渲帧)。返回 (ok, message)。

    分册02 纪律:T2 落地必须逐条验证,不得整批声明支持;编译不过的条目回
    「登记待实现」。"""
    try:
        mgl = _import_moderngl()
        ctx = mgl.create_standalone_context()
    except Exception as exc:  # noqa: BLE001
        return False, f"GL 上下文不可用:{exc}"
    try:
        frag = build_fragment_shader(name, glsl_dir)
        ctx.program(vertex_shader=_VERT, fragment_shader=frag)   # 编译失败抛异常
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        return False, f"编译失败:{msg[:200]}"
    finally:
        try:
            ctx.release()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 重叠区渲染

def render_overlap(seg_a: Path, seg_b: Path, *, fps: float, tdur_s: float,
                   out_mp4: Path, glsl_name: str, glsl_dir: Path,
                   ffmpeg_bin: str, params: dict | None = None,
                   qa_frames: int | None = None, qb_frames: int | None = None,
                   width: int = 0, height: int = 0) -> int:
    """渲染一个转场重叠区 → out_mp4(恰为 n_td 帧的 H.264 MP4)。返回渲出帧数。

    输入:seg_a 的尾部 n_td 帧(从 qa_frames 起)、seg_b 的头部 n_td 帧;
    progress 0→1 逐帧跑 fragment shader;rawvideo 管道进出,不经磁盘帧序列。
    帧数口径:qa/qb 缺省时按各文件实测帧数;out 长度 = n_td = round(tdur_s*fps)。
    """
    mgl = _import_moderngl()
    try:
        ctx = mgl.create_standalone_context()
    except Exception as exc:  # noqa: BLE001
        raise GLUnavailable(f"GL 上下文创建失败:{exc}") from exc
    proc = None
    try:
        # --- 帧数口径(零漂移:n_td 由调用方的名义 tdur 决定,不猜) ---
        n_td = max(int(round(tdur_s * fps)), 1)
        qa = _probe_frames(seg_a, ffmpeg_bin) if qa_frames is None else qa_frames
        qb = _probe_frames(seg_b, ffmpeg_bin) if qb_frames is None else qb_frames
        # 重叠必须真实存在:输入可用帧不足即报错(调用方在选择 T2 前已保证尾帧扩展)
        if qa < n_td:
            raise GLUnavailable(f"seg_a 可用重叠帧不足({qa}<{n_td})")
        w, h = _probe_size(seg_a, ffmpeg_bin) if not (width and height) else (width, height)
        # --- 解码管道:两条 rawvideo ---
        dec_a = subprocess.Popen(
            [ffmpeg_bin, "-v", "error", "-ss", f"{max(qa - n_td, 0) / fps:.6f}",
             "-i", str(seg_a), "-frames:v", str(n_td),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        dec_b = subprocess.Popen(
            [ffmpeg_bin, "-v", "error", "-i", str(seg_b), "-frames:v", str(n_td),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        enc = subprocess.Popen(
            [ffmpeg_bin, "-v", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", f"{fps:g}",
             "-i", "-", "-vf", "vflip",
             # FBO.read 的首行=屏幕底(GL 惯例),编码器首行须是视频顶 → vflip 归位
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "18", "-video_track_timescale", "15360", str(out_mp4)],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        frag = build_fragment_shader(glsl_name, glsl_dir, params)
        prog = ctx.program(fragment_shader=frag,
                           vertex_shader=_VERT)
        # 采样器绑定必须显式:两个 sampler 缺省都指 unit 0,不设则 to 恒等于 from
        prog["from"].value = 0
        prog["to"].value = 1
        if "ratio" in prog:                        # 未被引用的 uniform 会被优化掉
            prog["ratio"].value = w / h
        tex_from = ctx.texture((w, h), 3, dtype="f1")
        tex_to = ctx.texture((w, h), 3, dtype="f1")
        tex_from.filter, tex_to.filter = (mgl.LINEAR, mgl.LINEAR), (mgl.LINEAR, mgl.LINEAR)
        vbo = ctx.buffer(_vertices().tobytes())
        vao = ctx.simple_vertex_array(prog, vbo, "in_pos")
        rendered = 0
        frame_bytes = w * h * 3
        for i in range(n_td):
            ba = dec_a.stdout.read(frame_bytes)
            bb = dec_b.stdout.read(frame_bytes)
            if len(ba) < frame_bytes or len(bb) < frame_bytes:
                break                              # 解码短供:以实得帧数为准(不补帧)
            tex_from.write(ba)
            tex_to.write(bb)
            tex_from.use(0)
            tex_to.use(1)
            prog["progress"].value = (i + 1) / n_td   # 首帧即有微混合,末帧完全落在 to
            fbo = ctx.simple_framebuffer((w, h))
            fbo.use()
            vao.render()
            enc.stdin.write(fbo.read(components=3, alignment=1))
            rendered += 1
        enc.stdin.close()
        enc.wait(timeout=300)
        dec_a.wait(timeout=60)
        dec_b.wait(timeout=60)
        if enc.returncode != 0:
            raise GLUnavailable(f"重叠区编码失败:{(enc.stderr.read() or b'')[-200:]!r}")
        return rendered
    finally:
        try:
            ctx.release()
        except Exception:  # noqa: BLE001
            pass


_VERT = """#version 330
in vec2 in_pos;
out vec2 v_uv;
void main() {
  v_uv = vec2((in_pos.x + 1.0) / 2.0, 1.0 - (in_pos.y + 1.0) / 2.0);
  gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""


def _vertices():
    """全屏两个三角形(NDC);numpy 懒加载(ADR-0049:渲染路径才需要)。"""
    import numpy as np  # noqa: PLC0415
    return np.array([
        -1.0, -1.0, 1.0, -1.0, -1.0, 1.0,
        -1.0, 1.0, 1.0, -1.0, 1.0, 1.0,
    ], dtype="float32")


def _ffprobe_bin(ffmpeg_bin: str) -> str:
    """由 ffmpeg 路径推 ffprobe 路径(只换 basename —— 目录名含 ffmpeg 时不能误伤)。"""
    p = Path(ffmpeg_bin)
    cand = p.with_name(p.name.replace("ffmpeg", "ffprobe"))
    return str(cand) if cand.is_file() else str(p)


def _probe_frames(media: Path, ffmpeg_bin: str) -> int:
    """实测视频帧数(ffprobe 不可用时的兜底:解码计数)。"""
    try:
        p = subprocess.run(
            [_ffprobe_bin(ffmpeg_bin),
             "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=nb_frames", "-of", "default=nw=1:nk=1", str(media)],
            capture_output=True, text=True, timeout=60)
        if p.returncode == 0 and p.stdout.strip().isdigit():
            return int(p.stdout.strip())
    except Exception:  # noqa: BLE001
        pass
    p = subprocess.run([ffmpeg_bin, "-v", "error", "-i", str(media), "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300)
    m = re.findall(r"frame=\s*(\d+)", p.stderr or "")
    return int(m[-1]) if m else 0


def _probe_size(media: Path, ffmpeg_bin: str) -> tuple[int, int]:
    """实测画幅(解码首帧兜底)。"""
    try:
        p = subprocess.run(
            [_ffprobe_bin(ffmpeg_bin),
             "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0:s=x", str(media)],
            capture_output=True, text=True, timeout=60)
        if p.returncode == 0 and "x" in p.stdout:
            w, h = p.stdout.strip().split("x")
            return int(w), int(h)
    except Exception:  # noqa: BLE001
        pass
    raise GLUnavailable(f"画幅探测失败:{media}")
