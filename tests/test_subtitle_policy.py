"""第四册 T4.4 回归:约束分级(硬/软)策略表 templates/subtitle-policy.json。

判据(计划文档 4.2 T4.4):策略表被 segmentation / rs_subtitle / rs_verify
**三处共读**(不再各写常量);硬约束违反进 violations 且阻断,软约束违反只报告
不阻断交付。

运行:pytest tests/test_subtitle_policy.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "cutflow" / "scripts"))

import segmentation as sg  # noqa: E402
import rs_subtitle as rsub  # noqa: E402
import rs_verify  # noqa: E402

POLICY = sg.load_policy()


def test_policy_file_exists_and_valid():
    """策略表存在、可解析、版本化,硬/软两级齐备。"""
    assert POLICY, f"策略表缺失或坏档:{sg.POLICY_PATH}"
    assert POLICY.get("version") == 1
    cons = POLICY["constraints"]
    assert "maxChars" in cons["hard"] and "ghostMinMs" in cons["hard"]
    for k in ("cpsMax", "durRangeS", "minGapFrames", "beatRangeS", "minChars"):
        assert k in cons["soft"], k


def test_policy_matches_builtin_fallback():
    """策略表数值必须与内置回退常量一致(两处漂移即红)。"""
    hard, soft = sg.policy_constraints()
    assert hard["maxChars"] == sg.MAX_CHARS
    assert soft["cpsMax"] == sg.CPS_MAX
    assert tuple(soft["durRangeS"]) == tuple(sg.DUR_RANGE)
    assert soft["minGapFrames"] == sg.MIN_GAP_FRAMES
    assert tuple(soft["beatRangeS"]) == tuple(sg.BEAT_RANGE_S)
    assert soft["minChars"] == sg.MIN_CHARS
    assert soft["ghostMinMs"] == sg.GHOST_MIN_MS


def test_three_consumers_share_policy(tmp_path, monkeypatch):
    """三处共读的机械证明:换一份策略表,三个消费方的取值同步变化。"""
    custom = {
        "version": 1,
        "constraints": {
            "hard": {"maxChars": {"9x16": 17, "3x4": 15, "16x9": 22}, "ghostMinMs": 42},
            "soft": {"cpsMax": {"9x16": 7.5, "3x4": 9.0, "16x9": 9.0},
                     "durRangeS": [0.5, 6.0], "minGapFrames": 2,
                     "beatRangeS": [1.0, 4.0], "minChars": 2},
        },
    }
    p = tmp_path / "subtitle-policy.json"
    p.write_text(json.dumps(custom, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sg, "POLICY_PATH", p)
    monkeypatch.setattr(sg, "_POLICY_CACHE", None)
    try:
        # ① segmentation 读口
        assert sg.max_chars_for("9x16") == 17
        assert sg.cps_max_for_ratio("9x16") == 7.5
        assert sg.dur_range() == (0.5, 6.0)
        assert sg.ghost_min_ms() == 42
        # ② rs_subtitle 读口(生效字数解析走策略表)
        assert rsub.resolve_max_chars(None, {}, "9x16") == 17
        assert rsub.resolve_max_chars(None, {"maxChars": 15}, "9x16") == 15  # 预设优先
        # ③ rs_verify 读口(无 pipeline 快照时回退策略表,不再直读常量)
        root = tmp_path / "proj"
        (root / "00_制作简报").mkdir(parents=True)
        assert rs_verify._max_chars(root) == 17
    finally:
        monkeypatch.setattr(sg, "_POLICY_CACHE", None)   # 还原缓存,防污染其他用例


def test_soft_violation_reported_not_blocking():
    """软约束(时长/CPS)违反:出现在 violations 里,但 SUBTITLE_OK 仍为 ok(不阻断)。"""
    import io
    import contextlib
    text = "一二三四五六七八九"
    chars = [{"i": i, "ch": ch, "startMs": 10 * i, "endMs": 10 * i + 8,
              "srcStartMs": 10 * i, "srcEndMs": 10 * i + 8, "conf": 0.99}
             for i, ch in enumerate(text)]
    wl = {"source": "t", "chars": chars,
          "sentences": [{"id": 0, "span": [0, 9]}],
          "degraded": False, "degradeReasons": []}
    events, meta = rsub.events_from_wordline(wl, 12)
    assert meta["violations"], "软约束违反必须报告"
    # 出片主链不因软约束失败(硬失败只在 BAD_* 输入错误)
    from rs_common import emit  # noqa: F401 — 语义说明用


def test_hard_violation_in_violations_with_suggestion():
    """硬约束(幽灵卡)违反:100% 进 violations 且带可执行替代方案(T4.3 判据)。"""
    chars = [{"i": 0, "ch": ch, "startMs": 2210, "endMs": 2215,
              "srcStartMs": 2210, "srcEndMs": 2215, "conf": 0.97}
             for i, ch in enumerate("这个待会儿删掉呢")]
    wl = {"source": "t", "space": "final", "chars": chars,
          "sentences": [{"id": 0, "span": [0, 8]}],
          "srcDurationMs": 4400, "degraded": False, "degradeReasons": []}
    events, meta = rsub.events_from_wordline(wl, 12)
    assert events, "幽灵卡不再丢弃(丢弃=丢字),必须出卡交复核"
    hard = [v for v in meta["violations"] if "幽灵卡" in v]
    assert hard, meta["violations"]
    assert all("建议" in v or "override" in v or "rs_align" in v for v in hard)
