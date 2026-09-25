# -*- coding: utf-8 -*-
"""M8 第二波 · 录屏教程(screen-recording)端到端验收。

伪录屏夹具(cv2 逐帧绘制 + ffmpeg 单次编码,全合成、不进仓库):
  · 30s / 640x360 / 60fps(60fps 使光标轨迹采样粒度 50ms < 80ms 落拍阈值);
  · 动效底(弹跳色块,模拟 testsrc2 式"画面在动")+ 三段**完全静止**的等待段
    (占比 ≥40%),等待段时刻已知,可精确核对压缩率;
  · 已知时刻的点击(光标滞留→跳变)并注入 2 帧短暂高亮圈 = 已知"视觉反馈时刻",
    供「点击与反馈延迟 ≤80ms」断言;
  · 一段**有人声的静止画面**(21–23s:画面静止 + 解说持续)——guard 反误删的真值;
  · 已知坐标的"密钥"文本(5–26s 常驻,redact 中点抽帧必可见)。

链路:S0 摄取 → rs_screen analyze(--cursor --zoom --keys --redact --waiting)
      → rs_cut --detect waiting(消费 screen.json;另测无 screen.json 的内联降级)
      → --apply → remap → S3 IR → S7 字幕(bilibili 16:9/22字) → 对齐自检
      → S8 渲染 → L0 自检 → §5.5.4 验收七条逐条机械检查。

运行:pytest tests/test_screen_e2e.py -q(ffmpeg 或 cv2 不可用时整模块 SKIP)
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_cut  # noqa: E402
import rs_common  # noqa: E402
import rs_paths  # noqa: E402
import rs_sync  # noqa: E402  — parse_ass(字幕合规机械检查,与 rs_verify 同口径)


def _ffmpeg_bin() -> str:
    try:
        cfg = rs_common.load_config()
        p = rs_common.ffmpeg_bin(cfg)
        if Path(p).is_file():
            return p
    except SystemExit:
        pass
    return shutil.which("ffmpeg") or ""


FF = _ffmpeg_bin()
try:
    import cv2  # noqa: F401 — READY 档(光标/点击/打码真实现)必需
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
pytestmark = pytest.mark.skipif(not FF or not HAS_CV2,
                                reason="ffmpeg / opencv 不可用(合成夹具与 READY 档必需)")

# ---------------------------------------------------------------- 夹具真值(时间轴唯一真相)

W, H, FPS, DUR_MS = 640, 360, 60, 30000
ACTIVE_SPANS = [(0, 5000), (15000, 17000), (26000, 30000)]     # 动效段(画面在动)
# 静止等待段(≥40%):[5000,15000) + [17000,21000) + [23000,26000) = 17s / 56.7%
WAITING_TRUE = [(5000, 15000), (17000, 21000), (23000, 26000)]
# 其中 [21200,22800) 是「画面静止但有人声」——waiting 判据必须排除它(guard 真值;
# 21.0–21.2s 静音且静止,按判据属可等待区,不算人声)
SPEECHY_STATIC = (21200, 22800)
SECRET_TEXT = "API_KEY=sk-1234567890abcd"
SECRET_RECT = (18, 296, 205, 314)                              # 已知"密钥"区(px)
UI_RECTS = {"title": (20, 12, 220, 40),                        # 被讲解的 UI 元素
            "btnSettings": (400, 150, 560, 190),
            "btnSave": (400, 210, 560, 250)}
CIRCLE_R = 18                                                  # 高亮圈半径(px)
RING_MS = 34                                                   # 高亮圈只亮 2 帧(≈33ms,"短暂高亮圈")
# 点击 = 光标滞留 ≥400ms 后跳变;(时刻, 出发点=点击点, 到达点)。追踪器记录「离点」、
# 时间戳取跳变可见的首个采样;环消失帧会在离点补一个同位点。几何约束(与 rs_screen
# 光标追踪器对齐,实测校准):①每跳位移 d²=(dx/W·100)²+(dy/H·100)² ≤ CURSOR_JUMP_
# MAX_PCT²(40²=1600),否则轨迹点被跳变上限丢弃;②离点必须比到达点更靠近上一个
# 记录点(最近邻链),离点才会被记录;③首跳的离点在画面最上方(None 首点按扫描序取);
# ④第 1 个离点只建立轨迹基点(dwell 不足不注册),第 2/3 个离点被下一次移动注册成点击。
CLICKS = [(3000, (50, 70), (220, 160)),
          (16000, (220, 160), (340, 230)),
          (27000, (340, 230), (480, 150)),
          (28500, (480, 150), (50, 70))]
REAL_CLICKS = [(16000, (220, 160)), (27000, (340, 230))]       # 期望被检出的两次点击
# 动效底:色块按 7 相位逐帧跳变(仅动效段)。7 相位使 2fps 等待采样的相位差恒非零
# (30%7=2,静止判据不会误吞动效段);逐帧大跳变让帧差连通域恒为"整块"(~2700px@轨迹
# 分辨率,超过追踪器 2000px 上限被滤除),不会像慢速移动那样留边缘残影被误当光标。
BOX_POSITIONS = [(120, 90), (300, 90), (480, 80), (600, 160),
                 (480, 300), (300, 320), (620, 320)]
# 解说词(字级时间戳手写,等价 ASR 产物;全部 ≤22 字且 CPS ≤9)
SEGS = [("大家好今天我们演示屏幕操作", 500, 300),
        ("点击设置按钮", 15200, 250),
        ("这一步请耐心等待加载", 21200, 150),
        ("教程结束感谢观看", 26200, 300)]


def _cursor_at(t_ms: int) -> tuple[int, int]:
    pos = CLICKS[0][1]
    for t, _dep, arr in CLICKS:
        if t_ms >= t:
            pos = arr
        else:
            break
    return pos


def _is_active(t_ms: int) -> bool:
    return any(a <= t_ms < b for a, b in ACTIVE_SPANS)


def _synth_screen_media(out: Path) -> Path:
    """逐帧绘制伪录屏,rawvideo 管道进 ffmpeg 单次编码(视频 + 拼接的解说音轨)。"""
    import cv2
    import numpy as np

    font = cv2.FONT_HERSHEY_SIMPLEX

    def base_frame(t_ms: int):
        img = np.full((H, W, 3), (28, 28, 36), np.uint8)
        cv2.putText(img, "CutFlow Screen Demo", (20, 34), font, 0.6, (240, 240, 240), 1)
        cv2.rectangle(img, (400, 150), (560, 190), (70, 70, 90), -1)
        cv2.putText(img, "Settings", (415, 175), font, 0.45, (230, 230, 230), 1)
        cv2.rectangle(img, (400, 210), (560, 250), (70, 70, 90), -1)
        cv2.putText(img, "Save", (415, 235), font, 0.45, (230, 230, 230), 1)
        if 5000 <= t_ms < 26000:                     # 密钥文本(redact 真值,含 t=15s 中点帧)
            cv2.putText(img, SECRET_TEXT, (20, 310), font, 0.5, (220, 220, 220), 1)
        return img

    # 音轨:静音 + 440Hz 解说音爆(0.5*sin ≈ -6dBFS,远高于 -35dB 判据),合计 30.0s
    audio_parts = [("sil", 0.5), ("tone", 4.0), ("sil", 10.7), ("tone", 1.6),
                   ("sil", 4.4), ("tone", 1.6), ("sil", 3.4), ("tone", 3.3), ("sil", 0.5)]
    cmd = [FF, "-y", "-v", "error", "-nostats",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-"]
    for kind, d in audio_parts:
        src = ("anullsrc=r=48000:cl=mono" if kind == "sil" else
               f"aevalsrc=0.5*sin(440*2*PI*t):s=48000") + f":d={d:g}"
        cmd += ["-f", "lavfi", "-i", src]
    fc = "".join(f"[{i}:a]" for i in range(1, len(audio_parts) + 1)) \
        + f"concat=n={len(audio_parts)}:v=0:a=1[aout]"
    cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(out)]
    import tempfile
    with tempfile.TemporaryFile() as errf:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
        try:
            for k in range(DUR_MS * FPS // 1000):
                t = k * 1000 // FPS
                img = base_frame(t)
                if _is_active(t):                    # 动效底:7 相位逐帧跳变色块(仅动效段)
                    bx, by = BOX_POSITIONS[k % 7]
                    cv2.rectangle(img, (bx - 40, by - 30), (bx + 40, by + 30), (80, 180, 255), -1)
                px, py = _cursor_at(t)               # 光标(静止帧内坐标恒定 → 等待段帧差为零)
                cv2.circle(img, (px, py), 7, (0, 0, 0), -1)
                cv2.circle(img, (px, py), 5, (255, 255, 255), -1)
                for ct, dep, _arr in CLICKS:         # 已知时刻的短暂高亮圈 = 已知视觉反馈时刻
                    if ct <= t < ct + RING_MS:
                        cv2.circle(img, dep, CIRCLE_R, (0, 255, 255), 3)
                proc.stdin.write(img.tobytes())
        finally:
            proc.stdin.close()
            rc = proc.wait(timeout=600)
        errf.seek(0)
        assert rc == 0, f"伪录屏合成失败:{errf.read()[-300:]!r}"
    return out


def _seg(text: str, start_ms: int, per: int) -> dict:
    ts = [[start_ms + i * per, start_ms + (i + 1) * per - 20] for i in range(len(text))]
    return {"start": start_ms / 1000.0, "end": (start_ms + len(text) * per) / 1000.0,
            "text": text, "timestamp": ts, "conf": 0.95}


def _run(*args: str, cwd: Path, allow_fail: bool = False) -> dict:
    """跑脚本取最后一行 JSON 协议(与 test_v7_e2e 同款)。"""
    p = subprocess.run([sys.executable, str(SCRIPTS / args[0]), *args[1:]], cwd=cwd,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip().splitlines()
    doc = json.loads(out[-1]) if out else {"ok": False, "code": "NO_OUTPUT", "message": p.stderr}
    if not allow_fail:
        assert doc.get("ok"), f"{args[0]} 失败:{doc.get('code')} {doc.get('message')}"
    return doc


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _rects_intersect(a: tuple, b: tuple) -> bool:
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _box_px(box: dict) -> tuple:
    x0, y0 = box["xPct"] / 100 * W, box["yPct"] / 100 * H
    return (x0, y0, x0 + box["wPct"] / 100 * W, y0 + box["hPct"] / 100 * H)


# ---------------------------------------------------------------- 工程夹具(模块级,只合成/分析一次)

@pytest.fixture(scope="module")
def screen_proj(tmp_path_factory):
    root = tmp_path_factory.mktemp("screen_e2e") / "proj"
    for key in ("00_制作简报", "01_原始素材", "02_转写与校对"):
        (root / key).mkdir(parents=True)
    (root / "00_制作简报" / "brief.md").write_text(
        "# Brief — screen-e2e\n- videoType:`screen-recording`\n- 比例/平台预设:`bilibili`\n",
        encoding="utf-8")
    media = _synth_screen_media(root / "01_原始素材" / "recording.mp4")
    segs = [_seg(t, s, per) for t, s, per in SEGS]
    (root / "02_转写与校对" / "transcript.json").write_text(
        json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8")
    _run("rs_ingest.py", "scan", str(root), cwd=root)
    _run("rs_align.py", "build", "--from-transcript", "02_转写与校对/transcript.json",
         "--src", "01_原始素材/recording.mp4", "--out", "05_时间线工程/wordline.json", cwd=root)
    # S2 录屏分析:READY 档真跑(光标/点击/打码)+ waiting(帧差+静音)
    doc = _run("rs_screen.py", "analyze", str(media), "--out", str(root),
               "--cursor", "--zoom", "--keys", "--redact", "--waiting", cwd=root)
    assert doc["code"] == "SCREEN_OK"
    return {"root": root, "media": media, "screen": _read(root / "04_粗剪决策" / "screen.json")}


# ---------------------------------------------------------------- 单元:reason 枚举 / guard / 防误删

def test_waiting_unit_enum_guard_and_speech_filter(tmp_path):
    """waiting 枚举/guard 档位/防误删过滤:纯函数直测,不依赖 ffmpeg。"""
    assert "waiting" in rs_cut.REASONS, "reason 封闭枚举必须含 waiting(共 10 项)"
    assert len(rs_cut.REASONS) == 10
    assert rs_cut.GUARD_REQUIRED["waiting"] == ("wordClipped", "tailKeep"), \
        "waiting 段无语音:inSilence/outSilence 天然满足,只硬要求不切字 + 后留余量"
    screen = tmp_path / "screen.json"
    screen.write_text(json.dumps({"waiting": [
        {"startMs": 10000, "endMs": 14000, "ms": 4000},     # 干净等待段
        {"startMs": 20000, "endMs": 23000, "ms": 3000}      # 区间内有人声(转写字)
    ]}, ensure_ascii=False), encoding="utf-8")
    wl = {"chars": [{"ch": "你", "startMs": 20500, "endMs": 20700}], "srcDurationMs": 30000}
    cuts = rs_cut.detect_waiting(wl, screen=str(screen))
    assert len(cuts) == 1, "有人声的等待段必须整段放弃(宁可漏删)"
    c = cuts[0]
    assert (c["inMs"], c["outMs"]) == (10000 + rs_cut.WAITING_MARGIN_IN_MS,
                                       14000 - rs_cut.WAITING_MARGIN_OUT_MS)
    assert c["reason"] == "waiting" and c["conf"] >= rs_cut.CONF_REMOVE
    cl = rs_cut.build_cutlist(wl, [dict(c)], {})
    cut = cl["cuts"][0]
    assert cut["action"] == "remove" and rs_common.guard_passed(cut["guard"])
    # 无 screen.json 且无 media → 检出 0 刀(宁可漏检,不猜)
    assert rs_cut.detect_waiting(wl, wl_path=str(tmp_path / "x" / "wordline.json")) == []


# ---------------------------------------------------------------- §1 rs_screen 产物(验收 2/3/5 证据)

def test_screen_analysis_clicks_zoom_delay_and_redact(screen_proj):
    doc = screen_proj["screen"]
    # waiting:三段静止等待段全部检出,占比 ≥40%(方案夹具真值 17s/56.7%)
    waiting = doc["waiting"]
    assert len(waiting) == 3, waiting
    assert doc["waitingTotalMs"] >= 0.40 * DUR_MS, doc["waitingTotalMs"]
    for (ts, te), w in zip(WAITING_TRUE, sorted(waiting, key=lambda x: x["startMs"])):
        assert abs(w["startMs"] - ts) <= 1000 and abs(w["endMs"] - te) <= 1000, (w, (ts, te))
    # 有人声的静止画面绝不能进 waiting(半开区间:可贴到 21200=语音起点,不得真重叠)
    for w in waiting:
        assert not _rects_intersect((w["startMs"], 0, w["endMs"], 1),
                                    (SPEECHY_STATIC[0], 0, SPEECHY_STATIC[1], 1)), w
    # 验收②:两次真实点击被检出,且与已知注入高亮时刻偏差 ≤80ms
    clicks = doc["clickPoints"]
    for t_expect, (ex, ey) in REAL_CLICKS:
        hit = min(clicks, key=lambda c: abs(c["tMs"] - t_expect))
        assert abs(hit["tMs"] - t_expect) <= 80, (hit, t_expect)
        assert abs(hit["xPct"] - ex / W * 100) <= 5 and abs(hit["yPct"] - ey / H * 100) <= 5, hit
    # 验收②:zoomPlan 覆盖全部 clickPoints,同源同毫秒 → 反馈延迟 0(≤80ms)
    zoom = doc["zoomPlan"]
    assert {z["tMs"] for z in zoom} == {c["tMs"] for c in clicks} and len(zoom) == len(clicks)
    assert all(z["scale"] == 1.4 for z in zoom)
    # 验收⑤:打码候选区覆盖注入的"密钥"文本区
    boxes = doc["redactBoxes"]
    assert boxes, "密钥文本必须被圈进候选区"
    assert any(_rects_intersect(_box_px(b), SECRET_RECT) for b in boxes), boxes[:3]
    assert "L1" in doc["tiers"]["redact"].get("note", ""), "红档 L1 目测提醒必须留痕"


# ---------------------------------------------------------------- §2 rs_cut --detect waiting

def test_cut_waiting_consumes_screen_json_then_apply(screen_proj):
    root = screen_proj["root"]
    cut = _run("rs_cut.py", "05_时间线工程/wordline.json", "--detect", "waiting",
               "--out", "04_粗剪决策", "--media", "01_原始素材/recording.mp4", cwd=root)
    assert cut["data"]["detectors"]["waiting"] == 3
    cl = _read(root / "04_粗剪决策" / "cutlist.json")
    wcuts = [c for c in cl["cuts"] if c["reason"] == "waiting"]
    assert len(wcuts) == 3 and all(c["action"] == "remove" for c in wcuts)
    assert 12000 <= cl["removedMs"] <= 17500, f"夹具已知等待 ≈17s,实测 {cl['removedMs']}"
    wl = _read(root / "05_时间线工程" / "wordline.json")
    chars = wl["chars"]
    for c in wcuts:                                      # 防误删:任何一刀不得碰转写字/有人声静止段
        for ch in chars:
            assert not (ch["endMs"] > c["inMs"] and ch["startMs"] < c["outMs"]), (c["id"], ch)
        # 刀口可停在语音起点前(出点留白 120ms),但不得真侵入解说区间 [21200,22800)
        assert not _rects_intersect((c["inMs"], 0, c["outMs"], 1),
                                    (SPEECHY_STATIC[0], 0, SPEECHY_STATIC[1], 1)), c["id"]
    applied = _run("rs_cut.py", "--apply", "04_粗剪决策/cutlist.json", cwd=root)
    assert applied["data"]["removedMs"] == cl["removedMs"]
    assert (root / "04_粗剪决策" / "cutlist.applied.json").is_file()


def test_cut_waiting_inline_fallback_without_screen_json(screen_proj, tmp_path):
    """无 screen.json → --media 内联帧差+静音检测,结论与 screen.json 消费一致(±1.5s)。"""
    root = screen_proj["root"]
    fb = tmp_path / "fb_proj"
    (fb / "05_时间线工程").mkdir(parents=True)
    shutil.copy2(root / "05_时间线工程" / "wordline.json", fb / "05_时间线工程" / "wordline.json")
    base = _read(root / "04_粗剪决策" / "cutlist.json")["removedMs"]
    cut = _run("rs_cut.py", "05_时间线工程/wordline.json", "--detect", "waiting",
               "--out", "04_粗剪决策", "--media", str(screen_proj["media"]), cwd=fb)
    cl = _read(fb / "04_粗剪决策" / "cutlist.json")
    assert cut["data"]["detectors"]["waiting"] == 3
    assert abs(cl["removedMs"] - base) <= 1500, (cl["removedMs"], base)


# ---------------------------------------------------------------- §3 全管线到成片(验收 1/6)

def test_screen_full_pipeline_reduction_and_subtitle(screen_proj):
    root = screen_proj["root"]
    _run("rs_align.py", "remap", "05_时间线工程/wordline.json",
         "--cutlist", "04_粗剪决策/cutlist.applied.json",
         "--out", "05_时间线工程/wordline.final.json", cwd=root)
    _run("rs_ir.py", "build", "--from-cutlist", "04_粗剪决策/cutlist.applied.json",
         "--slug", "screen-e2e", "--ratio", "16x9", "--out", "05_时间线工程/project.json", cwd=root)
    ir = _read(root / "05_时间线工程" / "project.json")
    assert ir["canvas"] == {"width": 1920, "height": 1080} and ir["outputs"] == ["16x9"]
    sub = _run("rs_subtitle.py", "--from-wordline", "05_时间线工程/wordline.final.json",
               "--platform", "bilibili", "--out", "06_成片输出", cwd=root)
    assert sub["data"]["ratio"] == "16x9" and sub["data"]["maxChars"] == 22
    sync = _run("rs_sync.py", "--wordline", "05_时间线工程/wordline.final.json",
                "--ass", "06_成片输出/subtitles.ass", "--out", "06_成片输出",
                "--ir", "05_时间线工程/project.json", cwd=root)
    assert sync["data"]["pass"] is True
    _run("rs_render.py", "05_时间线工程/project.json", "--ratio", "16x9",
         "--profile", "draft", cwd=root)
    mp4s = sorted((root / "06_成片输出").glob("*.mp4"))
    assert mp4s, "必须产出成片"
    final_s = rs_common.media_duration_s(mp4s[-1])
    src_s = rs_common.media_duration_s(screen_proj["media"])
    reduction = 1.0 - final_s / src_s
    # 验收①:总时长较源减少 ≥30%(夹具预期 ≈52%;带宽覆盖检测边界抖动)
    assert reduction >= 0.30, f"压缩率 {reduction:.1%} < 30%"
    assert 12.5 <= final_s <= 17.5, f"成片 {final_s:.2f}s 偏离夹具预期(≈14.3s)"
    # 验收⑥:字幕每卡 ≤22 字(16:9 放宽档)、CPS ≤9(rs_verify 同口径复算)
    events = rs_sync.parse_ass(str(root / "06_成片输出" / "subtitles.ass"))
    assert len(events) >= 4
    for e in events:
        txt = e["text"].replace(" ", "")
        dur_ms = (e["end"] - e["start"]) * 1000
        assert len(txt) <= 22, e["text"]
        assert len(txt) / (dur_ms / 1000.0) <= 9.0, e["text"]
    ver = _run("rs_verify.py", str(root), cwd=root)
    assert ver["data"]["pass"] is True, ver["data"].get("failed")


# ---------------------------------------------------------------- §4 方案 §5.5.4 验收七条逐条落检

def test_acceptance_checklist_7_items(screen_proj):
    """能机械的断言;不能机械的如实标 L1 并断言「提示/机制存在」。"""
    root, doc = screen_proj["root"], screen_proj["screen"]
    rule = (REPO / "skills" / "cutflow" / "rules" / "video-types" / "录屏教程.md").read_text(
        encoding="utf-8")

    # ① 等待废段已压缩,总时长较源减少 ≥30%(以 applied cutlist 机械复算)
    cl = _read(root / "04_粗剪决策" / "cutlist.applied.json")
    assert cl["removedMs"] / cl["srcTotalMs"] >= 0.30, cl["removedMs"]

    # ② 每次点击有视觉反馈;点击与反馈延迟 ≤80ms(zoomPlan 全覆盖 + fixture 已知时刻)
    clicks, zoom = doc["clickPoints"], doc["zoomPlan"]
    assert {z["tMs"] for z in zoom} >= {c["tMs"] for c in clicks}
    for t_expect, _ in REAL_CLICKS:
        assert min(abs(c["tMs"] - t_expect) for c in clicks) <= 80

    # ③ 光标高亮不遮挡被讲解的 UI 元素(几何可断言:高亮圈 bbox × UI bbox = 空)
    for t_expect, (ex, ey) in REAL_CLICKS:
        hit = min(clicks, key=lambda c: abs(c["tMs"] - t_expect))
        ring = (hit["xPct"] / 100 * W - CIRCLE_R - 3, hit["yPct"] / 100 * H - CIRCLE_R - 3,
                hit["xPct"] / 100 * W + CIRCLE_R + 3, hit["yPct"] / 100 * H + CIRCLE_R + 3)
        for name, ui in UI_RECTS.items():
            assert not _rects_intersect(ring, ui), (name, ring)
    assert "不遮挡" in rule and "可见" in rule                        # 可见性属 L1:提示存在

    # ④ 章节卡齐全(每个逻辑段一张)——机制机检:风格包声明 section 模板;逐卡目测 L1
    pack = (REPO / "skills" / "cutflow" / "templates" / "styles" / "packs"
            / "screen-tutorial" / "params.yaml").read_text(encoding="utf-8")
    assert "section" in pack and "artboard" in pack, "风格包必须声明章节卡模板(机制存在)"
    assert "章节卡" in rule and "每个逻辑段一张" in rule               # L1:提示存在

    # ⑤ 密钥/隐私信息已打码——机检候选区覆盖 + L1 提醒在(最终判定 L1 目测)
    assert any(_rects_intersect(_box_px(b), SECRET_RECT) for b in doc["redactBoxes"])
    assert "L1" in doc["tiers"]["redact"].get("note", "")

    # ⑥ 字幕每卡合规;CPS ≤9(成片管线内已逐卡断言;此处核"规则存在")
    assert "CPS" in rule and "22" in rule and "15 字" in rule

    # ⑦ 画中画人脸未遮挡关键 UI——夹具无画中画 → 如实豁免(N/A),规则与几何口径在册
    assert "画中画" in rule
    assert not any(_rects_intersect(UI_RECTS[k], UI_RECTS[j])
                   for k in UI_RECTS for j in UI_RECTS if k < j), "fixture 无 PiP,UI 互不遮挡"
