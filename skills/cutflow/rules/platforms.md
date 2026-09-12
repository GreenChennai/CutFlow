# Platforms — 平台字幕预设与画幅(argv0 之外的"第二真相源")

> **ADR-0019**。一句话:**平台 ≠ 换个尺寸;平台 = 画幅 + 安全区 + 字号/位置 + 每卡字数 + 风格的整组预设,写在 `templates/platforms.json` 一处,由字幕/渲染/变体/校验共读。**

## 1. 为什么需要它

老版本把"9:16 / 16:9"写死在三处以上(`rs_render.RATIO`、`rs_subtitle.STYLES[ratio]`、`rs_brand.variant_ir` 画布字典、`project.schema.json` 枚举),且**校验口径错**:`rs_subtitle.py` 与 `rs_verify.py` 无论什么比例都取 `CPS_MAX["9x16"]`。结果是:

- 想加小红书(3:4)时,得改 7 个文件,漏一个就静默出错;
- 平台差异(安全区/字号/字数)只能靠人记,不在任何数据里;
- "字幕是重中之重"却没有任何一处能回答"这版字幕该多大、该躲哪里"。

## 2. 单一事实源

| 事实 | 位置 | 谁消费 |
|---|---|---|
| **画幅(比例 → 宽高)** | `rs_common.RATIOS`(代码里唯一一处) | 脚本(硬) |
| **平台(画幅 + 字数 + 风格 + CPS 上限 + 安全区 + 封面)** | `templates/platforms.json` | `rs_subtitle` 读 `ratio/canvas/style/maxChars/cpsMax`;`safeArea/coverSize/durationHint/note` 供 Agent 与封面/目测清单参考 |
| 字号 / 位置 | `rs_subtitle.STYLES[style][size/margin_v][ratio]` | 脚本(硬) |
| 每卡字数上限 | `segmentation.MAX_CHARS[ratio]` | 脚本(硬) |

> **新画幅要改三处**:`rs_common.RATIOS`(比例→宽高)+ `segmentation.MAX_CHARS/CPS_MAX`(每卡字数与语速上限)+ `rs_subtitle.STYLES[*].size/.margin_v`(三档样式的字号与位置)。**其余模块一律查表**,禁止再写 `1080x1920` 字符串比较 —— `tests/test_v7.py::test_ratio_tables_are_consistent` 会在漏改时红。

## 3. 首发四平台

| 平台 | key | 比例 | 画布 | 风格 | 每卡字数 | 安全区(顶/底) |
|---|---|---|---|---|---|---|
| 抖音 | `douyin` | 9x16 | 1080×1920 | talkshow-bold | 12 | 12% / 25% |
| 视频号 | `shipinhao` | 9x16 | 1080×1920 | talkshow-bold | 12 | 12% / 25%(左右 9%) |
| 小红书 | `xiaohongshu` | **3x4** | **1080×1440** | talkshow-bold | **15** | **10% / 18%** |
| B站 | `bilibili` | 16x9 | 1920×1080 | tutorial-clean | 22 | 8% / 16% |

- **小红书为什么能放宽到 15 字**:3:4 屏宽介于 9:16 与 16:9 之间,且底部没有抖音那样的评论遮挡带,安全区下探更深。
- **B站**字幕贴底走半透明条(弹幕默认在顶部 1/3,不冲突)。
- 平台只定义"默认值";**brief 里写死的值优先**。

## 4. 用法

```powershell
# 一条命令出平台版字幕(比例/画布/字数/风格都从预设取)
rs_subtitle.py --from-wordline 05_ir/wordline.json --platform xiaohongshu --out 06_output

# 显式参数优先于预设(临时试一版)
rs_subtitle.py --from-wordline 05_ir/wordline.json --platform douyin --max-chars 10 --out 06_output
```

**优先级**:显式 `--style / --ratio / --canvas / --max-chars` > `--platform` 预设 > 内置默认。
未知平台名 → **直接报错**(`BAD_PLATFORM`),不静默退回默认 —— 否则会静默出一版错规格的片子。

变体矩阵支持多比例:`rs_brand.py --expand --logos brandA --ratios 9x16,3x4,16x9`。

## 5. 门禁与验收

| 检查 | 通过线 |
|---|---|
| 平台覆盖 | 抖音 / 视频号 / 小红书 / B站 各出一版 |
| 安全区 | 字幕不压脸、不出安全区;3:4 用 10%/18% |
| 字数 / CPS | 每卡 ≤ 平台 `maxChars`;CPS ≤ `cpsMax`(默认 9) |
| 口径一致 | `CPS_MAX` 必须按当前比例取(`cps_max_for`),不得写死 9x16 |
| 裁定 | `rs_verify` L1 目测清单四画幅通过(判定权在 Agent/用户) |

## 6. 已知坑

| 坑 | 后果 | 对策 |
|---|---|---|
| 用 `canvas == "1080x1920"` 反推比例 | 1080×1440 被当成 16x9 | 一律 `rs_common.ratio_for_canvas()` |
| `CPS_MAX["9x16"]` 写死 | 16x9 / 三方比例校验口径错 | `segmentation.cps_max_for(max_chars)`,平台 `cpsMax` 可覆盖 |
| `project.schema.json` 枚举漏加 | IR 校验直接拒绝合法画幅 | canvas 枚举加 1440、outputs 加 `3x4` |
| 平台名拼错 | 静默退回默认,出片规格不对 | `resolve_platform()` 直接抛错 → `BAD_PLATFORM` |
| 只改 `RATIOS` 就以为加完画幅了 | `STYLES[*].size/margin_v` 缺 key → `write_ass` KeyError | 三处一起改(见 §2),一致性用例会拦 |
