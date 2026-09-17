# -*- coding: utf-8 -*-
"""卡完整性核对生效性分析:FIX3 的 ASS 卡文本设计得太保守(第2卡'遇到蓝屏'
恰好= ASR 完整子句),没有产生截断卡。真正被腰斩的『先不要慌。』被剪掉,
但它从未出现在 ASS 卡里(fixtures 的 ass 设计成 5 张卡,第3卡是'慌')。
看 fix3 ass 卡列表确认,然后修 fixtures ass:让卡文本覆盖完整句『遇到蓝屏先不要慌』,
被剪后 ASR 里只剩『遇到蓝屏』→ matched=4 < cardLen=7×0.8 → 截断卡检出。"""
from pathlib import Path

REPO = Path(r"E:\平日资料\GitHub\CutFlow")
ass = (REPO / "tests/fixtures/diagnosis/fix3_bad_cut.ass").read_text(encoding="utf-8")
cards = [l.split(",")[-1] for l in ass.splitlines() if l.startswith("Dialogue:")]
out = [repr(c) for c in cards]
(REPO / ".cluster" / "fix3_cards.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
