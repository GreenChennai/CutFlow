"""混剪(卡点/音乐驱动)端到端验收 —— M8 第二波(方案 §5.5.1 验收清单的机械落地)。

覆盖四块:
· 真值节拍夹具:纯 Python 合成精确 120 BPM 点击轨(sine 突发每 500ms 一次)→
  rs_beat detect 降级档(onset-energy)的 beats 必须与 500ms 真值网格对齐,
  confidence/degraded 留痕齐备(验收 1);
· beat.snap 集成演示:把 IR 切点批量吸附到拍点(≥3 个切点真位移落拍)+
  1 个超窗切点不动 + WARN(禁强制吸附,editing-grammar.md 手法 15 / ADR-0047);
· 端到端成片:S0 摄取(16 条 lavfi 合成素材,含 2 条纯色静态)→ 选材筛静态 →
  S2 节拍 → S3 IR(--bgm auto 曲库口径增益 −12dB)→ beat.snap 序列 → 结尾定格 →
  S7 lyricOnly 字幕 → S8 渲染 → 交付发布(说明书版权节 + AI 标识);
· 验收清单 8 条(方案 §5.5.1)逐条机械断言(见 test_mixcut_e2e_full_pipeline)。

关键设计约束(实测 rs_render 校验反推):beat.snap 只改 startMs 不级联邻段,
故夹具把被吸附切点设计成「目标网格 −50ms」的**连续链**——逐段 +50 吸附后
各边界恰好无缝,不触发渲染端「时间重叠」校验;唯一超窗样本(4350ms)两侧
邻居不吸附,原样保持连续。

没有 ffmpeg 时整模块跳过(与 test_v7_e2e 同口径);合成素材全在 tmp,不进仓库。
"""
from __future__ import annotations

import json
import math
import re
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "cutflow" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import rs_paths  # noqa: E402  — 阶段路径唯一真相源,测试同样不走目录字面量
import rs_beat   # noqa: E402
import rs_editor  # noqa: E402  — contentId 回退口径与 rs_edit 同源(P22-1)


def _ffmpeg() -> str | None:
    try:
        import rs_common
        cand = rs_common.ffmpeg_bin()
    except SystemExit:                     # 缺 config.json 时兜底
        cand = "ffmpeg"
    return shutil.which(cand) or (cand if Path(cand).is_file() else None)


FFMPEG = _ffmpeg()
pytestmark = pytest.mark.skipif(not FFMPEG, reason="本机没有 ffmpeg,跳过混剪端到端")


# ================================================================ 夹具设计表
# 出处:方案 §5.5.1(卡 0.8–2.4s,副歌可加密到 0.5s 须显式留痕;BGM −12dB;
# 结尾定格 1–2s;全片 ≤45s)+ rules/video-types/混剪.md §2/§3。

BPM = 120                                 # 点击轨真值:每 500ms 一拍
CLICK_SEC = 120                           # 2 分钟,拍点网格远超成片长度
HOOK = 2                                  # 强钩子 2 镜头(前 2 拍)
N_KEPT, N_STATIC = 14, 2                  # 16 条素材,2 条纯色静态供选材筛掉
FREEZE_MS = 1500                          # 结尾定格(混剪.md §3:1–2s)

# 14 条保留卡:时长(ms,50ms=1 帧@20fps 整倍)。段落:钩子2 + 铺垫3 + 副歌7(含
# 收密卡) + 定格收尾2。**原始切点 = 落拍网格 −50ms 的连续链**(见模块 docstring),
# beat.snap 后逐段 +50 回网格;唯一例外 WARN 样本(索引 4)故意偏离 150ms。
CARD_DURS = [1000, 1000, 1000, 1350, 1600, 500, 500, 500, 500, 500, 500, 1000, 1500, 2550]
WARN_IDX = 4                              # 该卡切点超窗不吸附(4350 → 最近拍 4500,+150ms)
CLIMAX_RANGE = (6000, 10_000)             # 副歌段(IR 终态坐标):0.5s 加密显式留痕
CLIP_SRC = ["testsrc", "testsrc2", "mandelbrot", "life", "cellauto", "testsrc2",
            "mandelbrot", "smptebars", "life", "cellauto", "mandelbrot",
            "testsrc", "testsrc2", "testsrc"]       # 运动图案各异;smptebars=色卡
STATIC_SRC = {"m16.mp4": "color=c=gray", "m17.mp4": "color=c=orange"}  # 纯色静态(非蓝绿,避开幕布闸)

# lyricOnly 点睛短句(≤12 字;进出场对齐乐句边界=2 拍=1000ms,均在 500ms 网格上)
HOOK_PHRASES = [("这一刻", 1000, 2000), ("心跳同频", 4000, 5500),
                ("燃到极致", 7000, 8500), ("定格此刻", 11500, 13000)]

SRC_START_MS = 200                        # 每条原始素材内保留段的入点余量
SRC_TAIL_MS = 200                         # 出点余量(转场尾帧放得下,不触发整链弃用)


def _starts_of(durs: list[int]) -> list[int]:
    """连续链的原始切点(粗剪排布坐标系,startMs 逐卡累加)。"""
    out, cur = [], 0
    for d in durs:
        out.append(cur)
        cur += d
    return out


CARD_STARTS = _starts_of(CARD_DURS)
# beat.snap 后的期望切点 = 全部吸附到 500ms 网格;超窗样本(WARN_IDX)保持原值
EXPECTED_STARTS = [s if i == WARN_IDX else round(s / 500) * 500
                   for i, s in enumerate(CARD_STARTS)]


def _synth_clicks_wav(path: Path, seconds: int = CLICK_SEC) -> None:
    """精确 120 BPM 点击轨(真值已知):8kHz 单声道,每个 500ms 的前 20ms 放
    1kHz sine 突发。20ms=8kHz 下恰 160 样本、500ms=4000 样本,与 rs_beat 降级档
    20ms 能量窗**整窗对齐**,起音检测零量化偏差。"""
    rate = 8000
    click = [int(0.85 * 32767 * math.sin(2 * math.pi * 1000 * i / rate))
             for i in range(rate // 50)]              # 160 样本 = 20ms
    buf = bytearray(rate * seconds * 2)
    for k in range(seconds * 2):                       # 每 500ms 一击
        base = k * (rate // 2)
        for i, v in enumerate(click):
            off = (base + i) * 2
            buf[off] = v & 0xFF
            buf[off + 1] = (v >> 8) & 0xFF
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(buf))


def _synth_clip(dst: Path, src_expr: str, frames: int) -> None:
    """lavfi 合成一条素材:20fps、帧数精确(-frames:v),时长 = 帧数×50ms。

    src_expr 形如 `testsrc`(具名源,需 `=` 接参数)或 `color=c=gray`(已带参数,
    用 `:` 续接)。
    """
    joiner = "=" if "=" not in src_expr else ":"
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi",
           "-i", f"{src_expr}{joiner}size=320x240:rate=20",
           "-frames:v", str(frames), "-vf", "format=yuv420p",
           "-c:v", "libx264", "-preset", "ultrafast",
           "-video_track_timescale", "15360", str(dst)]
    subprocess.run(cmd, check=True, capture_output=True)


@pytest.fixture(scope="session")
def mixcut_assets(tmp_path_factory) -> dict:
    """会话级合成素材(仓外 tmp,不进仓库):16 条素材 + 120BPM 点击轨 + 素材带。"""
    base = tmp_path_factory.mktemp("mixcut_assets")
    raws = base / "raws"
    raws.mkdir()

    kept_names = [f"m{i:02d}.mp4" for i in range(1, N_KEPT + 1)]
    raw_lens = [d + SRC_START_MS + SRC_TAIL_MS for d in CARD_DURS]
    for name, src, frames in zip(kept_names, CLIP_SRC, raw_lens):
        _synth_clip(raws / name, src, frames // 50)
    static_names = list(STATIC_SRC)
    for name, expr in STATIC_SRC.items():              # 静态素材:0.8s / 1.2s
        _synth_clip(raws / name, expr, {"m16.mp4": 16, "m17.mp4": 24}[name])

    # 素材带:保留素材按卡序无损拼接(参数一致,copy 零漂移)→ 粗剪 keep 的源
    strip = base / "strip.mp4"
    lst = base / "concat.txt"
    lst.write_text("".join(f"file '{(raws / n).as_posix()}'\n" for n in kept_names),
                   encoding="utf-8")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(strip)], check=True,
                   capture_output=True)
    p = subprocess.run([FFMPEG.replace("ffmpeg", "ffprobe") if FFMPEG == "ffmpeg" else "ffprobe",
                        "-v", "error", "-show_entries", "format=duration", "-of", "json",
                        str(strip)], check=True, capture_output=True, text=True)
    strip_ms = round(float(json.loads(p.stdout)["format"]["duration"]) * 1000)
    assert abs(strip_ms - sum(raw_lens)) <= 60, "素材带帧数必须逐帧精确(concat 丢帧则夹具失效)"

    clicks = base / "bgm_120bpm.wav"
    _synth_clicks_wav(clicks)

    # 粗剪 keep:每张卡落在对应素材内,前后各留 200ms 余量(邻卡源间隙恒 400ms
    # <1s → jumpcut 亚帧软切,视觉硬切,符合混剪「硬切为主」语法)
    keeps, cursor = [], 0
    for length, dur in zip(raw_lens, CARD_DURS):
        keeps.append([cursor + SRC_START_MS, cursor + SRC_START_MS + dur])
        cursor += length
    return {"raws": raws, "kept": kept_names, "statics": static_names,
            "strip": strip, "strip_ms": strip_ms, "clicks": clicks,
            "keeps": keeps, "src_total_ms": cursor}


# ================================================================ 工具

def _run(*args: str, cwd: Path | None = None, allow_fail: bool = False) -> dict:
    """跑一个 skills 脚本,取末行协议 JSON(v7 e2e 同款驱动)。"""
    p = subprocess.run([sys.executable, str(SCRIPTS / args[0]), *args[1:]],
                       cwd=str(cwd or REPO), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "").strip().splitlines()
    doc = json.loads(out[-1]) if out else {"ok": False, "code": "NO_OUTPUT",
                                           "message": p.stderr[-400:]}
    if not allow_fail:
        assert doc.get("ok"), f"{args[0]} 失败:{doc.get('code')} {doc.get('message')}"
    return doc


def _mk_project(root: Path) -> None:
    """铺工程骨架 + brief(混剪型)。"""
    for key in ("brief", "materials", "sensed", "assets", "cut", "timeline",
                "output", "state"):
        (root / rs_paths.p(key)).mkdir(parents=True, exist_ok=True)
    (root / rs_paths.p("brief") / "brief.md").write_text(
        "# Brief — mixcut e2e\n- videoType:`混剪`\n- 平台:`douyin`\n"
        "- BGM:`自产 120BPM 点击轨(授权=自产)`\n", encoding="utf-8")


def _put_cutlist(root: Path, assets: dict) -> Path:
    p = root / rs_paths.p("cut") / "cutlist.applied.json"
    p.write_text(json.dumps(
        {"version": 1, "source": str(assets["strip"]), "keep": assets["keeps"],
         "cuts": [], "removedMs": assets["src_total_ms"] - sum(CARD_DURS),
         "srcTotalMs": assets["src_total_ms"]}, ensure_ascii=False), encoding="utf-8")
    return p


def _build_ir(root: Path, cutlist: Path) -> dict:
    """S3:CutList → IR(9x16 抖音竖屏;无现场人声轨 → 渲染端 ducking 自动关)。"""
    _run("rs_ir.py", "build", "--from-cutlist", str(cutlist), "--slug", "mixcut",
         "--ratio", "9x16", "--no-audio",
         "--out", str(rs_paths.project_json(root)))
    return json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))


def _video_clips(ir: dict) -> list[dict]:
    return [t for t in ir["tracks"] if t["kind"] == "video"][0]["clips"]


def _snap_beat_drift(beats: list[float], ms: int) -> float:
    """ms 到最近拍点的带符号距离。"""
    return min(beats, key=lambda b: abs(b - ms)) - ms


# ================================================================ ① 真值网格

def test_beat_detect_aligns_true_grid(tmp_path, mixcut_assets):
    """验收 1(前置):rs_beat 降级档对 120BPM 真值点击轨的输出必须机械对齐。

    真值:beats ⊆ 500ms 网格、bpm≈120;留痕:confidence/degraded/degradeReason
    (beatnet 未部署是本环境事实,降级必须诚实标注,方案 §5.5.1 验收 1)。
    """
    proj = tmp_path / "beat_truth"
    _mk_project(proj)
    doc = _run("rs_beat.py", "detect", str(mixcut_assets["clicks"]), "--out", str(proj))
    assert doc["data"]["engine"] == "onset-energy" and doc["data"]["degraded"] is True

    beats = json.loads(rs_beat.beats_path(proj).read_text(encoding="utf-8"))
    assert beats["unit"] == "ms", "毫秒口径(rs_edit beat.snap 消费契约)"
    assert abs(beats["bpm"] - 120.0) <= 1.0, beats["bpm"]
    assert isinstance(beats["confidence"], (int, float)) and 0.9 <= beats["confidence"] <= 1.0
    assert beats["degraded"] is True and beats["degradeReason"] and beats["missingComponent"]
    # 真值断言:拍点 ≥ 230 个(120s/0.5s),且全部落在 500ms 网格 ±40ms 内
    grid = beats["beats"]
    assert len(grid) >= 230, len(grid)
    assert all(abs(b % 500) <= 40 or abs(b % 500) >= 460 for b in grid), "拍点偏离 500ms 真值网格"


# ================================================================ ② beat.snap 集成演示

def test_beat_snap_sequence_and_out_of_window_warn(tmp_path, mixcut_assets):
    """人话「把这几个镜头卡在鼓点上」→ beat.snap 序列 → IR 切点落拍。

    · ≥3 个切点真位移落拍(±60ms 容差内吸附,editing-grammar.md 手法 15);
    · 1 个超窗切点(+150ms)不动且 WARN(禁强制吸附,ADR-0047);
    · 切点吸附后不产生渲染端拒绝的时间重叠(夹具链式设计,见模块 docstring)。
    """
    proj = tmp_path / "snap_demo"
    _mk_project(proj)
    cutlist = _put_cutlist(proj, mixcut_assets)
    _run("rs_beat.py", "detect", str(mixcut_assets["clicks"]), "--out", str(proj))
    beats = json.loads(rs_beat.beats_path(proj).read_text(encoding="utf-8"))["beats"]

    ir = _build_ir(proj, cutlist)
    clips = _video_clips(ir)
    assert [c["startMs"] for c in clips] == CARD_STARTS, "夹具排布应逐卡连续"

    # beat.snap 序列:一次 apply 批量吸附全部切点(锚点 = 原生 id;rs_ir v0.20 起产
    # 原生 id,ADR-0048 寻址设计:原生 id 优先,cf- 仅无 id 旧 IR 回退)
    ops = [{"op": "beat.snap", "target": c.get("id") or c.get("id") or (c.get("id") or rs_editor.content_id(c)),
            "after": {"windowMs": 60},
            "reason": "卡点:吸附最近拍点(60ms 窗,超窗不动)", "source": "agent"}
           for c in clips]
    ops_path = proj / "ops_snap.json"
    ops_path.write_text(json.dumps({"baseRev": 0, "ops": ops,
                                    "requestId": "mixcut-e2e-snap"},
                                   ensure_ascii=False), encoding="utf-8")
    out = _run("rs_edit.py", "apply", str(proj), "--ops", str(ops_path), "--actor", "agent")
    data = out["data"]
    moved = [i for i, c in enumerate(_video_clips(
        json.loads(rs_paths.project_json(proj).read_text(encoding="utf-8"))))
        if c["startMs"] != CARD_STARTS[i]]
    assert len(moved) >= 3, f"至少 3 个切点被吸附,实际 {len(moved)}"
    assert data["idempotent"] >= 1 and len(data["warnings"]) == 1, data

    # 落拍断言:每个被移动切点距最近拍 ≤60ms;超窗样本原样不动 + WARN 留痕
    ir = json.loads(rs_paths.project_json(proj).read_text(encoding="utf-8"))
    starts = [c["startMs"] for c in _video_clips(ir)]
    for i in moved:
        assert abs(_snap_beat_drift(beats, starts[i])) <= 60, f"切点 {i} 未落拍"
    wi = WARN_IDX
    assert starts[wi] == CARD_STARTS[wi], "超窗切点必须原样保留(禁强制吸附)"
    assert abs(_snap_beat_drift(beats, starts[wi])) > 60
    warn = data["warnings"][0]
    assert "超出吸附窗" in warn and "不动" in warn, warn

    # 链式无缝:吸附后任一切点不得早于前卡结束(渲染端时间重叠校验的同口径;
    # 前卡结束 = 原始切点 + 原始时长,durationMs 不随 snap 改变)
    ends = [CARD_STARTS[i] + CARD_DURS[i] for i in range(len(clips))]
    for i in range(1, len(starts)):
        assert starts[i] >= ends[i - 1] - 1, f"边界 {i} 出现时间重叠({starts[i]} < {ends[i - 1]})"


# ================================================================ ③ 端到端成片 + 验收 8 条

def test_mixcut_e2e_full_pipeline(tmp_path, mixcut_assets):
    """混剪全管线 → 成片 mp4;方案 §5.5.1 验收 8 条逐条机械断言(断言处标注条号)。"""
    root = tmp_path / "mixcut_e2e"
    _mk_project(root)
    assets = mixcut_assets

    # ---- S0 摄取:16 条素材全部 probe 通过 ----
    for n in assets["kept"] + assets["statics"]:
        shutil.copy2(assets["raws"] / n, root / rs_paths.p("materials") / n)
    ing = _run("rs_ingest.py", "scan", str(root), cwd=root)
    assert ing["data"]["count"] == 16 and not ing["data"]["failed"]

    # ---- 选材:静态素材筛掉(混剪.md §1:静态卡点效果差;卡点验收 2 的前置) ----
    selection = {
        "videoType": "混剪", "pacing": "music",
        "kept": [{"file": n, "role": "card"} for n in assets["kept"]],
        "dropped": [{"file": n, "reason": "static:纯色无运动信息,卡点效果差,选材筛掉"
                     } for n in assets["statics"]],
        "climaxExplicitDense": True,
        "decision": f"副歌段 {CLIMAX_RANGE} 卡时长加密到 0.5s —— 显式改,留痕"
                    "(混剪.md §2 / registry pacingTiers.music.cardMs=[800,2400] 的显式例外)",
    }
    (root / rs_paths.p("cut") / "selection.json").write_text(
        json.dumps(selection, ensure_ascii=False, indent=1), encoding="utf-8")
    dropped = {d["file"] for d in selection["dropped"]}
    assert dropped == set(assets["statics"])
    assert all(d["reason"].startswith("static") for d in selection["dropped"])

    # ---- 版权登记 + AI 标识留痕(S0 侧,方案 §5.5.3;验收 6/7 的素材侧) ----
    (root / rs_paths.p("materials") / "copyright.json").write_text(json.dumps({
        "version": 1, "aiDisclosure": True,
        "transformNote": "全部素材为本测试 lavfi 合成自产(转化性:卡点节奏重组),无第三方影视引用",
        "items": [{"file": n, "usage": "original", "source": "自产(lavfi 合成)"}
                  for n in assets["kept"] + assets["statics"]],
    }, ensure_ascii=False), encoding="utf-8")
    cp = _run("rs_ingest.py", "copyright", str(root))
    assert cp["ok"] is True and cp["code"] == "COPYRIGHT_OK"
    assert cp["data"]["aiDisclosure"] is True, "AI 参与必须标识(验收 7)"

    # ---- S2 节拍:BGM 单独登记在 03_创作素材,detect 显式入口 ----
    bgm = root / rs_paths.p("assets") / "bgm_120bpm.wav"
    shutil.copy2(assets["clicks"], bgm)
    _run("rs_beat.py", "detect", str(bgm), "--out", str(root))
    # 【验收 1】beats.json 存在且 confidence 已记录;降级档 degraded=true 有因
    bdoc = json.loads(rs_beat.beats_path(root).read_text(encoding="utf-8"))
    assert isinstance(bdoc.get("confidence"), (int, float))
    assert bdoc["degraded"] is True and bdoc["degradeReason"] == "onset-energy"
    beats = bdoc["beats"]

    # ---- S3 IR:bgm=auto 曲库口径接线(增益真相源 = 节奏档/风格包 −12dB) ----
    (root / rs_paths.p("brief") / "intent_decisions.json").write_text(json.dumps({
        "version": 1, "kind": "cutflow-intent-decisions",
        "resolved": {"videoType": "混剪", "pacing": "music",
                     "bgm": {"mode": "auto", "enabled": True, "gainDb": -12,
                             "src": str(bgm)}}}, ensure_ascii=False), encoding="utf-8")
    cutlist = _put_cutlist(root, assets)
    ir = _build_ir(root, cutlist)
    # 【验收 4】BGM 增益 = −12dB(music 档全库最高);无对白 → ducking 关
    assert ir["bgm"]["gainDb"] == -12, ir["bgm"]
    assert Path(ir["bgm"]["src"]).is_file()
    voice = [c for t in ir["tracks"] if t["kind"] == "audio" for c in t["clips"]]
    assert not voice, "混剪无对白:无现场人声 clip,BGM 直给(ducking 自动关)"

    # ---- beat.snap 序列(9 处落拍 + 1 处超窗 WARN)→ 结尾定格(两批 apply:
    #      snap 会改 startMs,内容 id 随之变化,定格必须基于吸附后视图重新寻址) ----
    clips = _video_clips(ir)
    ops = [{"op": "beat.snap", "target": c.get("id") or (c.get("id") or rs_editor.content_id(c)),
            "after": {"windowMs": 60},
            "reason": "卡点:吸附最近拍点(60ms 窗,超窗不动)", "source": "agent"}
           for c in clips]
    ops_path = root / "ops_snap.json"
    ops_path.write_text(json.dumps({"baseRev": 0, "ops": ops,
                                    "requestId": "mixcut-e2e-snap"},
                                   ensure_ascii=False), encoding="utf-8")
    out = _run("rs_edit.py", "apply", str(root), "--ops", str(ops_path), "--actor", "agent")
    assert len(out["data"]["warnings"]) == 1 and out["data"]["applied"] >= 3
    rev = out["data"]["revTo"]

    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    clips = _video_clips(ir)
    assert [c["startMs"] for c in clips] == EXPECTED_STARTS, "吸附后切点应逐卡落拍"
    last = clips[-1]
    ops2 = [{"op": "freeze.set", "target": (last.get("id") or rs_editor.content_id(last)),
             "after": {"freezeMs": FREEZE_MS},
             "reason": "结尾定格收尾(混剪.md §3:定格 1–2s 不拖沓)", "source": "agent"}]
    ops2_path = root / "ops_freeze.json"
    ops2_path.write_text(json.dumps({"baseRev": rev, "ops": ops2,
                                     "requestId": "mixcut-e2e-freeze"},
                                    ensure_ascii=False), encoding="utf-8")
    _run("rs_edit.py", "apply", str(root), "--ops", str(ops2_path), "--actor", "agent")
    ir = json.loads(rs_paths.project_json(root).read_text(encoding="utf-8"))
    clips = _video_clips(ir)

    # ================= 方案 §5.5.1 验收清单 · 机械判定 =================

    # 【验收 2】副歌段抽查重拍:切点为样本、beats 为网格,±60ms 内算对齐,≥8/10
    cut_points = [c["startMs"] for c in clips][1:]            # 切点 = 各卡 startMs(首卡除外)
    lo, hi = CLIMAX_RANGE
    climax_cuts = [t for t in cut_points if lo <= t < hi]
    step = max(1, len(cut_points) // 10)
    sample = (climax_cuts + [t for t in cut_points if t not in climax_cuts])[:10] \
        if len(climax_cuts) >= 10 else cut_points[::step][:10]
    aligned = sum(1 for t in sample if abs(_snap_beat_drift(beats, t)) <= 60)
    assert len(sample) >= 10 and aligned >= 8, f"重拍抽查 {aligned}/{len(sample)}(<8)"

    # 【验收 3】无 0.3s 内连续双转场(机械查 IR:相邻切点间距 ≥300ms)
    gaps = [b - a for a, b in zip(cut_points, cut_points[1:])]
    assert min(gaps) >= 300, f"存在 {min(gaps)}ms 的连续双转场"

    # 【验收 5 前置 / 显式留痕】卡时长纪律:出格时长必须有出处
    for i, c in enumerate(clips):
        dur, s = c["durationMs"], c["startMs"]
        if lo <= s < hi:
            assert dur <= 1000, f"副歌卡 {i} 时长 {dur}ms 超加密上界"
        elif dur < 800:
            assert selection["climaxExplicitDense"], "sub-800ms 卡必须显式留痕"
        elif i < len(clips) - 1:
            assert 800 <= dur <= 2400, f"非副歌卡 {i} 时长 {dur}ms 出 music 档"
    # 【验收 8】结尾定格存在:末卡 freezeMs ≥1s,静置尾 1–2s;全片 ≤45s
    assert clips[-1].get("freezeMs", 0) >= 1000
    tail = clips[-1]["durationMs"] - clips[-1]["freezeMs"]
    assert 1000 <= tail <= 2000, f"定格静置 {tail}ms 应在 1–2s"
    assert clips[-1]["startMs"] + clips[-1]["durationMs"] <= 45_000

    # ---- S7 字幕:lyricOnly 点睛短句(wordline 只放钩子,不逐句) ----
    chars, sentences, off = [], [], 0
    for pi, (txt, a, b) in enumerate(HOOK_PHRASES):
        n, w = len(txt), (b - a) // len(txt)
        for i, ch in enumerate(txt):                  # 字间连续(无停顿伪影,DP 不拆卡)
            chars.append({"ch": ch, "startMs": a + i * w, "endMs": a + (i + 1) * w})
        sentences.append({"id": pi, "span": [off, off + n]})
        off += n
    wl_path = root / rs_paths.p("timeline") / "wordline.json"
    wl_path.write_text(json.dumps({"source": "lyric-hooks", "degraded": False,
                                   "chars": chars, "sentences": sentences},
                                  ensure_ascii=False), encoding="utf-8")
    _run("rs_subtitle.py", "--from-wordline", str(wl_path), "--platform", "douyin",
         "--max-chars", "12", "--out", str(root / rs_paths.p("output")))
    ass_path = root / rs_paths.p("output") / "subtitles.ass"
    assert ass_path.is_file() and (root / rs_paths.p("output") / "master.srt").is_file()

    # 【验收 5】字幕仅点睛短句:每卡 ≤12 字、CPS ≤9、密度≤1 卡/2s(不逐句刷屏)、
    #           进出场对齐乐句边界(±60ms,与拍点同容差族)
    dialogues = [l for l in ass_path.read_text(encoding="utf-8").splitlines()
                 if l.startswith("Dialogue:")]

    def _t(stamp: str) -> float:
        h, m, sec = stamp.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)

    cards = []
    for l in dialogues:
        f = l.split(",")
        cards.append((_t(f[1]), _t(f[2]), f[9]))
    assert cards, "ASS 无字幕事件"
    total_ms = clips[-1]["startMs"] + clips[-1]["durationMs"]
    assert len(cards) <= total_ms / 2000, "字幕刷屏(点睛短句 ≠ 逐句歌词)"
    beat_grid = [float(x) for x in range(0, total_ms + 1000, 500)]
    for a, b, text in cards:
        body = re.sub(r"\{[^}]*\}", "", text).strip()
        assert 0 < len(body) <= 12, f"卡「{body}」超 12 字"
        assert len(body) / max(b - a, 0.01) <= 9, f"卡「{body}」CPS 超 9"
        assert abs(_snap_beat_drift(beat_grid, round(a * 1000))) <= 60, \
            f"卡「{body}」进点偏离乐句边界"

    # ---- S8 渲染:成片 mp4(视频+BGM 音轨),时长 ≈ 设计总长 ----
    _run("rs_render.py", str(rs_paths.project_json(root)), "--ratio", "9x16",
         "--profile", "draft")
    vids = rs_paths.final_videos(root) or sorted(
        (root / rs_paths.p("output")).glob("*.mp4"))
    assert vids, "必须产出成片 mp4"
    probe = subprocess.run([FFMPEG.replace("ffmpeg", "ffprobe") if FFMPEG == "ffmpeg"
                            else "ffprobe", "-v", "error", "-show_entries",
                            "format=duration", "-of", "json", str(vids[-1])],
                           check=True, capture_output=True, text=True)
    dur_s = float(json.loads(probe.stdout)["format"]["duration"])
    total_ms = clips[-1]["startMs"] + clips[-1]["durationMs"]
    assert 0.9 * total_ms / 1000 <= dur_s <= 1.1 * total_ms / 1000 + 1.0, \
        f"成片 {dur_s:.1f}s 偏离设计 {total_ms / 1000:.1f}s"

    # ---- 交付发布:验收 6/7 —— 说明书必含「版权与 AI 标识」节;S10 未跑,
    #      缺封面/文案属预期(INCOMPLETE 非零退出,说明书照常生成) ----
    pub = _run("rs_ingest.py", "deliverables", str(root), "--publish", allow_fail=True)
    assert pub["code"] == "DELIVERABLES_INCOMPLETE"
    missing = " ".join(pub["data"]["missing"])
    assert "封面.png" in missing and "metadata.json" in missing, pub["data"]["missing"]
    notes = (root / rs_paths.p("deliver") / "说明书" / "交付说明书.md").read_text(
        encoding="utf-8")
    # 【验收 6】版权:来源在交付说明留痕 —— 说明书含版权节标题
    assert "版权与 AI 标识" in notes, "交付说明书缺版权节"
    # 【验收 7】AI 生成/参与已标识
    assert "AI 生成内容标识" in notes, "交付说明书缺 AI 标识确认项"
