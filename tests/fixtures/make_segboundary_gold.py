"""生成 tests/fixtures/segboundary_gold.json(T4.9 分词金标,允许切点口径)。

标注约定:
  · 每条样本以**空格标注词界**(「我们 今天 讲讲 账号 定位」);
  · 「允许切点」= 词与词之间的位置 − 领域禁切表(数量词+单位 / 的得了着之后 /
    ASCII 词内 / 引文括号内侧 / 标点前 —— 与 segmentation.forbidden_positions 同源);
  · 金标用于引擎比选(边界 F1)与防退化:引擎若把两个词并为一个跨度(该切不切)
    或把一个词拆开(不该切却切)都会掉分。

领域:口播 / 电商 / 财税 / 教程。样本 = 词库组合 + 前后缀包装,确定性生成,
条目固定(同版本重跑字节一致)。

运行:python tests/fixtures/make_segboundary_gold.py   (在仓库根执行亦可)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "skills" / "cutflow" / "scripts"))

import segmentation as sg  # noqa: E402

# ---------------------------------------------------------------- 词库(带空格词界)

PREFIXES = [
    "大家好", "其实", "我 告诉 你", "很多 新手 商家", "注意", "其次",
    "今天 我们 来 讲讲", "首先", "另外", "实话 说", "重点 是",
    "最后 再 提醒 一遍", "一定 要 记住", "别 急着", "真正 的 关键 在于",
]
SUFFIXES = [
    "", "吗", "呢", "了", "对 吧", "是不是", "记 住 了 吗",
    "这 一点 非常 重要", "别 再 踩 坑 了", "建议 收藏", "你 学会 了 吗",
]

# 每条 = 已按词界空格标注的完整句(核心题库,跨领域)
CORE_SENTENCES = [
    # ---- 口播 / 电商 ----
    "账号 定位 决定 了 系统 给 你 推 什么 样 的 人群",
    "很 多 新手 一 上来 就 急着 投 流量 结果 全 打 水漂",
    "先 做 人群 画像 再 设计 脚本 才 是 正确 的 顺序",
    "直播间 的 人气 上来 了 转化率 却 一直 上 不 去",
    "选品 的 核心 逻辑 是 先 看 需求 再 看 供给",
    "老 客户 复购 的 成本 远远 低于 拉新 的 成本",
    "发布 时间 对 流量池 的 突破 有 明显 影响",
    "这 期 视频 我们 聊聊 店群 企业 怎么 提升 运营效率",
    "平台 算法 正在 改写 流量 的 市场版图",
    "三 家 门店 的 客流量差异 非常 明显",
    "没有 经营主体 资质 就 无法 开通 小店",
    "提高 运营效率 的 核心 是 优化 流量 结构",
    "账号 没有 违反 规则 却 被 限流 了 半 个 月",
    "这种 做法 直接 影响 了 店群企业 的 整体 评分",
    "这些 内容 根本 不 属于 合规 的 经营范畴",
    "抖店 和 抖音 小店 的 保证金 政策 并 不 相同",
    "主图 点击率 低 先 检查 前 三 秒 的 钩子",
    "短视频 的 完播率 是 系统 判定 质量 的 第一 指标",
    "把 前 三 秒 的 痛点 讲 清楚 观众 才 愿意 留 下来",
    "评论区 的 关键词 会 影响 系统 对 内容 的 判断",
    "带货 佣金 一般 在 确认 收货 之后 才 结算",
    "直播 回放 也 能 持续 带 来 自然 流量",
    "蓝 V 认证 的 企业号 有 更多 营销 权限",
    "千川 投放 要 看 整体 的投产比 而 不是 单 次 成交",
    # ---- 财税 ----
    "小规模纳税人 的 增值税 申报 流程 有 新 变化",
    "企业所得税 的 汇算清缴 截止 到 五月 三十一 日",
    "专用 发票 和 普通 发票 的 抵扣 方式 完全 不 一样",
    "开具 发票 时 必须 填写 正确 的 纳税人识别号",
    "小企业会计准则 下 不 需要 报送 现金流量表",
    "净利润 等于 收入 总额 减去 各项 成本费用",
    "逾期 申报 会 产生 滞纳金 还 会 影响 信用等级",
    "借款 时 发生 在 纳税 年度 内 周转归还 依据 财税 文件 的 规定",
    "规范 股东 与 公司 之间 的 资金往来 务必 高度重视",
    "小企业会计准则 的 利润表 中 只 有 一 个 财务费用科目",
    "跨 年 发票 要 在 汇算清缴 之前 处理 干净",
    "个体户 也 要 记账 报税 不 然 会 上 经营异常 名录",
    "电子 发票 的 抬头 和 税号 必须 与 营业执照 一致",
    "进项 发票 认证 之后 才 能 抵扣 销项 税额",
    "一般纳税人 可以 开具 专用 发票 给 客户 抵扣",
    "社保 入税 之后 企业 的 用工 成本 更 透明 了",
    # ---- 教程 / 工具 ----
    "我们 先 把 素材 导入 时间线 再 做 粗剪 决策",
    "字幕 的 断句 要 跟着 语音 的 停顿 走 而 不是 随机 切",
    "导出 之前 务必 检查 响度 和 真峰 这 两 项 指标",
    "每 张 字幕卡 的 时长 最好 控制 在 一 秒半 到 三 秒半 之间",
    "这 个 功能 需要 先 把 模型 下载 到 本地 才能 离线 使用",
    "用 GPT-SoVITS 克隆 声音 需要 先 准备 语料",
    "渲染 的 时候 选择 硬件 编码 速度 能 快 一倍",
    "时间线 上 的 每 一 段 都 可以 单独 调 整 速度",
    "抠像 之前 先 把 绿幕 的 亮度 打 均匀",
    "关键帧 之 间 的 曲线 决定 了 动画 的 手感",
    "字幕 的 字号 要 根据 画面 宽度 自动 适配",
    "转写 完成 之后 记得 校对 同音 字 错别字",
    "给 视频 加 水印 不如 在 画面 里 融入 品牌 元素",
    "音量 标准化 之后 整条 视频 的 响度 保持 一致",
    "闪 帧 一般 出现 在 两 段 素材 的 拼 接 处",
    # ---- 综合 叙事 ----
    "他 非常 努力 地 准备 但是 还是 没有 成功",
    "这个 方案 便宜 而且 好用 所以 我们 决定 采用 它",
    "可能 因为 其中 一 家 店铺 违规 而 被 连带 处理",
    "被 连带 处理 最终 一同 遭殃 的 案例 不 在 少数",
    "今天 的 内容 就 讲 到 这里 我们 下期 再 见",
]

NUM_UNIT = "个岁次天年月日时秒分元块毛角米厘斤吨度倍页条第名位件台只张片章节课号"


def _allowed_positions(words: list[str]) -> tuple[str, list[int]]:
    """词表 → (拼接文本, 允许切点位置表)。切点 = 词边界 − 领域禁切表。"""
    text = "".join(words)
    bounds: list[int] = []
    acc = 0
    for w in words[:-1]:
        acc += len(w)
        bounds.append(acc)
    forb = sg.forbidden_positions(text)
    # 领域补禁:数量词/数字 + 单位(forbidden_positions 已含,双保险显式再扫一遍)
    for i in range(1, len(text)):
        a, b = text[i - 1], text[i]
        if b in NUM_UNIT and (a.isdigit() or a in sg.CN_DIGITS):
            forb.add(i)
    allowed = sorted(p for p in bounds if p not in forb)
    return text, allowed


def build_entries() -> list[dict]:
    """确定性组合生成:核心句 + 轮转选定的前后缀包装;条目按生成序去重。

    每条核心句产 6 个变体(裸句 + 3 前缀 + 2 后缀),61 句 ≈ 366 条,
    落在 300–500 的金标区间内;组合全部按轮转取,同版本重跑字节一致。
    """
    entries: list[dict] = []
    seen: set[str] = set()

    def add(words: list[str]) -> None:
        text, allowed = _allowed_positions(words)
        if not text or text in seen or not allowed:
            return
        seen.add(text)
        entries.append({"text": text, "allowed": allowed})

    for ci, sent in enumerate(CORE_SENTENCES):
        core = sent.split()
        add(core)                                            # 裸句
        for k in range(3):                                   # 3 个前缀包装(轮转)
            pre = PREFIXES[(ci * 3 + k) % len(PREFIXES)]
            sfx = SUFFIXES[(ci + k * 4) % len(SUFFIXES)]
            add(pre.split() + core + (sfx.split() if sfx else []))
        for k in range(2):                                   # 2 个后缀包装(轮转)
            sfx = SUFFIXES[(ci * 2 + k * 3) % len(SUFFIXES)]
            pre = PREFIXES[(ci + k * 5) % len(PREFIXES)]
            add(pre.split() + core + (sfx.split() if sfx else []))
    return entries


def main() -> int:
    entries = build_entries()
    if not (300 <= len(entries) <= 500):
        print(f"WARN: 条目数 {len(entries)} 不在 300–500 区间(仍落盘,请补题库)")
    doc = {
        "version": 1,
        "description": "T4.9 分词金标:领域内(口播/电商/财税/教程)「允许切点」标注集。"
                       "allowed = 词边界位置 − 领域禁切表(pos = 在 text[pos-1] 与 "
                       "text[pos] 之间切)。用于引擎比选(边界 F1)与防退化;"
                       "由 tests/fixtures/make_segboundary_gold.py 确定性生成。",
        "count": len(entries),
        "entries": entries,
    }
    out = Path(__file__).resolve().parent / "segboundary_gold.json"
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"金标已生成:{out}({len(entries)} 条)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
