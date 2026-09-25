"""CutFlow 公共库:CLI 结果协议 / 配置加载 / ffmpeg 调用。

所有 rs_*.py 的输出统一为 `{"ok": bool, "code": str, "message": str, "data": ...}`,
退出码:0 成功 / 2 输入错 / 3 依赖缺失 / 4 执行失败。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import segmentation  # noqa: E402  — 纯标准库,提供统一的标点口径(PUNCT_WS)
import rs_paths  # noqa: E402  — 阶段路径唯一真相源(ADR-0046);目录名禁止字面量

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO_ROOT / "config.json"

EXIT_OK, EXIT_INPUT, EXIT_DEP, EXIT_EXEC = 0, 2, 3, 4

# artboard 技能路径锁定(M12/ADR-0053,用户 2026-09-26 确认):工作区级为唯一准绳。
# 用户级 codebuddy 副本与它内容不一致,**不作准**;rs_doctor 检出 config.artboard_dir
# 与本路径不一致时报错(锁定目录缺席的机器——如 CI——跳过此判据)。
ARTBOARD_LOCKED_DIR = Path(r"E:\平日资料\GitHub\.agents\skills\artboard")

# IR 版本唯一真相源(R09/R41):project.json 的 version 恒为 1(与两仓 schema 的
# const 一致);schema 自身演进用 schemaVersion 表达,不动这个整数。
IR_VERSION = 1


class IrError(Exception):
    """统一 IR 载入错误(R41):code/message/data/exit_code 随身,由各入口转 emit 协议。

    rs_edit 包成 EditError、rs_render 直接 emit——两处共用同一份版本校验口径,
    不再「各脚本各自解读」(R09 的病根)。
    """

    def __init__(self, code: str, message: str, data=None, exit_code: int = EXIT_INPUT):
        super().__init__(message)
        self.code, self.message, self.data, self.exit_code = code, message, data, exit_code

# P17-1:封面文件名唯一真相源(硬规则 15「产物中文命名」)。清理白名单、交付清单、
# 文档口径(SKILL.md S10 产物 / rules/cover.md)共用此常量;旧 `cover.png` 不再产出、
# 不再被清成"缺失"。改名只能改这里。
COVER_PNG = "封面.png"

# 画幅唯一真相源(OPTIMIZATION-v7 #4):新增画幅只改这里 + templates/platforms.json
RATIOS: dict[str, tuple[int, int]] = {
    "9x16": (1080, 1920),      # 抖音 / 视频号
    "3x4": (1080, 1440),       # 小红书
    "16x9": (1920, 1080),      # B站 / YouTube
}


# ---------------------------------------------------------------- 文本锚定(v0.12 共享)

def content_text(t: str) -> str:
    """归一化到内容字:去标点/空白。锚定口径与断句(PUNCT_WS)全链一致。"""
    return "".join(ch for ch in (t or "") if ch.strip() and ch not in segmentation.PUNCT_WS)


def content_index(chars: list[dict]) -> tuple[str, list[int]]:
    """wordline.chars → (内容串 S, 内容串位置 → chars 下标)。

    文本锚定共享实现(v0.12):rs_subtitle override / rs_cut --from-text /
    rs_ir build --from-cards 同族消费者一律用这里,绝不做"按内容字数算术偏移"
    (raw 下标含标点/空白条目,偏移必切错位)。
    """
    s: list[str] = []
    idx: list[int] = []
    for i, c in enumerate(chars):
        ch = str(c.get("ch", ""))
        if ch.strip() and ch not in segmentation.PUNCT_WS:
            s.append(ch)
            idx.append(i)
    return "".join(s), idx


def anchor_span(s: str, idx: list[int], text: str, cursor: int = 0) -> tuple[int, int]:
    """在内容串 S 上**顺序锚定**引文 → chars 区间 [start, end)。

    顺序 + 游标:引文必须按原文出现,找不到即 ValueError(绝不模糊匹配/乱序搜索);
    返回的 chars 区间可直接喂 _span_ms 类函数换时间。
    """
    t = content_text(text)
    if not t:
        raise ValueError(f"锚定文本归一化后为空:{text!r}")
    pos = s.find(t, cursor)
    if pos < 0:
        raise ValueError(f"无法在内容串中顺序锚定「{t[:20]}」(游标 {cursor}/{len(s)});"
                         "引文必须按原文出现且逐字一致")
    return idx[pos], idx[pos + len(t) - 1] + 1


def p95(values: list[float]) -> float:
    """95 分位(小样本取上界)。各处自检报告统一口径,别再各写一遍索引式。"""
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * 0.95))]


def guard_passed(guard: dict | None) -> bool:
    """粗剪 guard 是否通过(`okByReason` 优先,兼容老工程的 `ok`)。粗剪/自检共用一套口径。"""
    g = guard or {}
    return bool(g.get("okByReason", g.get("ok")))


def duration_ledger_error(wl: dict) -> str | None:
    """P27-2 时长账一致性断言:`srcDurationMs − removedMs == finalDurationMs`。

    任一环节写盘后都必须平账(20260920 教训:改了 keep 边界而 wordline 时长字段
    不同步,rs_sync 的「成片总时长」断言才挂红叉——正解是把账改平,不是解释红叉)。
    老工程缺 removedMs 字段时按 0 计;srcDurationMs/finalDurationMs 缺失则不判(无从判起)。
    """
    src, fin = wl.get("srcDurationMs"), wl.get("finalDurationMs")
    if src is None or fin is None:
        return None
    removed = int(wl.get("removedMs") or 0)
    if int(src) - removed != int(fin):
        return (f"时长账不平:srcDurationMs({int(src)}) − removedMs({removed}) ≠ "
                f"finalDurationMs({int(fin)});"
                "修复:rs_cut.py --apply <cutlist>(自动同步),"
                "或 rs_align.py refresh-durations --media <素材>")


def sync_wordline_durations(wl: dict, src_total_ms: int, removed_ms: int) -> dict:
    """P27-1:按 cutlist 同步 wordline 的三个时长字段(不动任何字符时间)。

    srcDurationMs = 实测/有效源总时长;removedMs = 粗剪裁掉量;
    finalDurationMs = src − removed(P27-2 账目恒等式由构造保证)。
    """
    out = dict(wl)
    out["srcDurationMs"] = int(src_total_ms)
    out["removedMs"] = int(removed_ms)
    out["finalDurationMs"] = int(src_total_ms) - int(removed_ms)
    return out


def canvas_for(ratio: str) -> tuple[int, int]:
    """比例 → (宽, 高);未知比例报错而不是静默猜。"""
    if ratio not in RATIOS:
        raise ValueError(f"未知比例 {ratio!r}(可选 {'/'.join(RATIOS)})")
    return RATIOS[ratio]


def ratio_for_canvas(width: int, height: int) -> str:
    """(宽, 高) → 比例;查不到时报错(旧实现是字符串比较,新画幅必然漏)。"""
    for name, (w, h) in RATIOS.items():
        if (w, h) == (int(width), int(height)):
            return name
    raise ValueError(f"画布 {width}x{height} 不对应任何已知比例({'/'.join(RATIOS)})")


def ensure_utf8() -> None:
    """把 stdout/stderr 切到 UTF-8(带 replace 兜底)。

    Windows 控制台默认 cp936,打印 `✓`/`↔`/emoji 会 UnicodeEncodeError 直接崩脚本。
    已被重定向或被测试框架替换的流没有 reconfigure → 静默跳过(绝不因它抛异常)。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 — 无 reconfigure / 已关闭 → 不阻塞
            pass


# 导入即兜底:任何 rs_*.py 只要 import rs_common,就不再因控制台编码把脚本打崩
# (实测踩坑:rs_verify 的检查名含 `↔`,GBK 控制台下 emit() 直接 UnicodeEncodeError)
ensure_utf8()


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        die(EXIT_DEP, "NO_CONFIG", f"缺少 config.json,请从 config.example.json 复制并填写:{CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def write_text_atomic(path: str | Path, text: str) -> None:
    """UTF-8 原子写:临时文件 + os.replace,中断/掉电不留半个文件(P15-1 同纪律)。

    gen-cards / add-overlay / export-fallback 等生成式写盘一律走这里;
    同目录已有同名临时残件也会被覆盖,不留垃圾。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


def emit(ok: bool, code: str, message: str, data=None, exit_code: int = EXIT_OK) -> int:
    print(json.dumps({"ok": ok, "code": code, "message": message, "data": data},
                     ensure_ascii=False, default=str))
    return exit_code


def die(exit_code: int, code: str, message: str, data=None) -> "None":
    print(json.dumps({"ok": False, "code": code, "message": message, "data": data},
                     ensure_ascii=False, default=str))
    sys.exit(exit_code)


def ffmpeg_bin(cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    p = Path(cfg.get("ffmpeg_dir", "")) / "ffmpeg.exe"
    return str(p) if p.is_file() else "ffmpeg"


def ffprobe_bin(cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    p = Path(cfg.get("ffmpeg_dir", "")) / "ffprobe.exe"
    return str(p) if p.is_file() else "ffprobe"


def load_ir_path(ir_path: str | Path) -> dict:
    """统一 IR 载入入口·路径版(R09/R41):解析 + version 校验 + 迁移引导。

    version ≠ IR_VERSION 时给结构化错误 IR_VERSION_UNSUPPORTED(含当前值、期望值
    与迁移指引),绝不「各脚本各自解读」;版本相符才返回 doc。缺文件 → NO_PROJECT,
    坏 JSON → IR_INVALID(退出码语义与各入口既有口径一致)。
    """
    p = Path(ir_path)
    if not p.is_file():
        raise IrError("NO_PROJECT", f"IR 不存在:{p}", {"ir": str(p)}, EXIT_DEP)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise IrError("IR_INVALID", f"project.json 解析失败:{exc}",
                      {"ir": str(p)}, EXIT_EXEC)
    if not isinstance(doc, dict):
        raise IrError("IR_INVALID", f"project.json 顶层须为对象,得到 {type(doc).__name__}",
                      {"ir": str(p)}, EXIT_INPUT)
    ver = doc.get("version")
    if isinstance(ver, bool) or not isinstance(ver, int):
        raise IrError("IR_VERSION_UNSUPPORTED",
                      f"IR 缺合法 version 字段(期望整数 {IR_VERSION}):{p}",
                      {"ir": str(p), "version": ver, "expected": IR_VERSION}, EXIT_INPUT)
    if ver != IR_VERSION:
        raise IrError(
            "IR_VERSION_UNSUPPORTED",
            f"IR version {ver} 与当前支持的 {IR_VERSION} 不一致:{p};"
            "迁移指引:用 cutforge 侧迁移器升版(cutforge-cli 的 project 迁移器,"
            "schemas/project.schema.json 的 schemaVersion 描述当前契约),"
            "或确认工程未损坏后重试;各脚本不再各自解读版本(R09/R41)",
            {"ir": str(p), "version": ver, "expected": IR_VERSION}, EXIT_INPUT)
    return doc


def load_ir(project: str | Path) -> dict:
    """统一 IR 载入入口(R09/R41):传工程根,内部定位 05_时间线工程/project.json。

    rs_render / rs_edit 已改走本入口;新脚本一律用它,禁止再手写
    `json.loads(project_json.read_text())` 各自解读版本。
    """
    return load_ir_path(rs_paths.project_json(project))


def proportional_timeout(duration_s: float, *, factor: float = 4.0, floor: int = 1800,
                         env: str = "") -> int:
    """R40 最小实现:子进程超时按素材时长比例计算 —— max(常量下限, 时长×系数)。

    · `env` 给出环境变量名(如 CUTFLOW_SEG_TIMEOUT):显式设置且为正整数时完全
      覆盖(手工兜底优先于自动推算);
    · floor 是保守下限(小时级素材也不会低于既有常量),factor 覆盖慢机器余量。
    """
    if env:
        raw = os.environ.get(env, "").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
    try:
        dur = max(float(duration_s or 0.0), 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    return int(max(float(floor), dur * factor))


def run(cmd: list[str], timeout: int = 3600, quiet: bool = True,
        cwd: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    # cwd:mate 探针等需要相对输出路径的场景(Windows 盘符冒号在 filtergraph
    # 里无法可靠转义,cwd+相对名是唯一稳解,v0.11 实测)。
    return subprocess.run(cmd, env=env, capture_output=True, timeout=timeout,
                          text=True if quiet is False else True,
                          encoding="utf-8", errors="replace", cwd=cwd)


def ffprobe_json(media: str | Path, cfg: dict | None = None) -> dict:
    p = run([ffprobe_bin(cfg), "-v", "error", "-show_format", "-show_streams",
             "-of", "json", str(media)])
    if p.returncode != 0:
        die(EXIT_EXEC, "PROBE_FAIL", f"ffprobe 失败:{(p.stderr or '')[:300]}")
    return json.loads(p.stdout)


def media_duration_s(media: str | Path, cfg: dict | None = None) -> float:
    info = ffprobe_json(media, cfg)
    return float(info.get("format", {}).get("duration") or 0)


def http_json(url: str, timeout: float = 10, method: str = "GET",
              body: dict | None = None) -> dict:
    import urllib.request
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_markers(markers) -> list[dict]:
    """IR markers → [{ms:int, label:str}]（R01 统一口,ADR-0058/v2 M11）。

    schema 契约是 `{ms, label}`;历史工程/内部产物曾写 `atMs` —— 此处统一归一:
    `ms` 优先,缺则回退 `atMs`,再缺视为 0 并保留 label。label 缺省回退 `title`。
    消费方(rs_sfx / rs_meta)一律经本函数取 markers,禁止各自直读字段。
    """
    out: list[dict] = []
    for m in markers or []:
        if not isinstance(m, dict):
            continue
        ms = m.get("ms", m.get("atMs", 0))
        try:
            ms = int(ms)
        except (TypeError, ValueError):
            ms = 0
        out.append({"ms": ms, "label": str(m.get("label") or m.get("title") or "")})
    return out


def resolve_voice(voice: str, cfg: dict | None = None) -> Path:
    """音色名 → card.json 路径。支持:目录名 / card.name / 权重文件名关键词(koubo-test→口播声线)。"""
    cfg = cfg or load_config()
    voices = Path(cfg["tts"]["voices_dir"])
    direct = voices / voice / "card.json"
    if direct.is_file():
        return direct
    hits, broken = [], []
    for card_path in voices.glob("*/card.json"):
        try:
            card = json.loads(card_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            broken.append(f"{card_path.parent.name}({type(exc).__name__})")   # 不静默吞掉坏卡
            continue
        hay = " ".join([card.get("name", ""), card.get("gpt", ""), card.get("sovits", "")]).lower()
        if voice.lower() == card.get("name", "").lower() or voice.lower() in hay:
            hits.append(card_path)
    if len(hits) == 1:
        return hits[0]
    if not hits:
        extra = f";另有 {len(broken)} 张卡读取失败:{','.join(broken)}" if broken else ""
        raise FileNotFoundError(f"找不到音色卡:{voice}(voices_dir={voices}){extra}")
    raise ValueError(f"音色名 {voice} 命中多张卡:{[h.parent.name for h in hits]}")


def ensure_workdir(slug: str) -> Path:
    """视频工程工作目录固定结构(目录契约唯一真相源 rs_paths,ADR-0045)。

    新工程产中文新结构;旧结构工程 resolve 兜底落回旧名(不造新旧混存);
    废弃目录(rs_paths.RETIRED 两项)不再创建。
    """
    cfg = load_config()
    root = Path(cfg["workdir_root"]) / slug
    rs_paths.ensure(root)
    return root
