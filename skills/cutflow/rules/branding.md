# Branding — Logo 与变体矩阵(S5)

> **ADR-0014**。一句话:**一次编辑,多产物。变体只在最后一步分叉,中间件全部共享。**

## 1. 场景

同一条素材要发给不同渠道 / 不同客户时,常常只差一个 Logo,或只差一个比例:

- 「这一版放 A 品牌的角标,那一版放 B 品牌的」
- 「竖版发抖音,横版发 B站」
- 「客户版不要片尾,自营版带片尾」

旧管线的做法是**跑两遍**——第二遍 20 分钟,只为了换一张右上角的 PNG。这不是能力问题,是**没有变体概念**。

## 2. 变体矩阵(`05_ir/variants.json`)

```json
{
  "version": 1,
  "logos": [
    {"id": "brandA", "src": "03_assets/branding/logos/a.png",
     "anchor": "topRight", "scale": 0.12, "opacity": 0.9, "inMs": 0, "outMs": null},
    {"id": "brandB", "src": "03_assets/branding/logos/b.png",
     "anchor": "topRight", "scale": 0.12, "opacity": 0.9}
  ],
  "ratios": ["9x16", "16x9"],
  "durations": ["full"],
  "matrix": [
    {"id": "A_9x16", "logo": "brandA", "ratio": "9x16", "backends": ["ffmpeg"]},
    {"id": "A_16x9", "logo": "brandA", "ratio": "16x9", "backends": ["ffmpeg", "jianying59"]},
    {"id": "B_9x16", "logo": "brandB", "ratio": "9x16", "backends": ["ffmpeg"]},
    {"id": "B_16x9", "logo": "brandB", "ratio": "16x9", "backends": ["ffmpeg", "jianying59"]}
  ]
}
```

- `matrix` 可由 `logos × ratios × durations` 笛卡尔积自动生成(`rs_brand.py --expand`);
- 手动 `matrix` 优先,用于排除个别组合。

## 3. 落点与安全区

| `anchor` | 说明 |
|---|---|
| `topLeft` / `topRight` | 常规角标位置 |
| `bottomLeft` / `bottomRight` | 避免与字幕带冲突时使用 |
| `watermark` | 全屏半透明水印(低 alpha,居中或平铺) |

**硬约束**:

- **默认避开字幕带**(9:16 底部 25%,ADR-0009);
- 不压**顶部 12%** 安全区外的关键信息;
- `scale` 为**占画幅宽度比例**,不写死像素——同一份配置在 9:16 / 16:9 下都成立;
- 字幕卡与 Logo 时间窗重叠时,**字幕优先**(Logo 降 alpha 或临时移位)。

新增 `03_assets/branding/logos/`,每个 Logo 一个条目。片头/片尾板也归 S5(用 artboard 生成)。

## 4. 渲染策略(成本的关键)

**共享中间件,只分叉最后一步:**

```
seg_*/base ─► composed ─► mixed ─► subtitled ──┬─► + logo(brandA) ─► A_9x16.mp4
                                               └─► + logo(brandB) ─► B_9x16.mp4
```

`base/` `composed/` `mixed/` `subtitled/` **各算一次**;每个变体只做 logo overlay + encode。

> **2 Logo × 2 比例 = 4 个成片,成本约等于 1.3 个。**

## 5. 用法

```powershell
python skills/cutflow/scripts/rs_brand.py --expand --logos brandA,brandB --ratios 9x16,16x9 `
    --out 05_ir/variants.json

python skills/cutflow/scripts/rs_brand.py 05_ir/project.json --variants 05_ir/variants.json `
    --out 06_output --profile final
```

产物命名**必须带 variantId**(ADR-0007):`成片_竖版_A_最终.mp4` / `成片_横版_B_最终.mp4`。

## 6. 后端能力矩阵

`matrix[].backends` 声明该变体支持的渲染后端;`rs_brand.py` 会:

1. 只对声明支持的后端执行;
2. 剪映 5.9 无对应字段的特性(如 `chroma`)自动**降级**并在 `deliverables.md` 中标注「本变体的 X 特性在剪映版缺失」;
3. 交付说明里列出「特性 × 后端」对照表(对应 BACKLOG 的 IDEA)。

## 7. 门禁与验收

| 检查 | 通过线 |
|---|---|
| 变体齐全 | `matrix` 每个条目都有对应产物文件 |
| 成本 | 2 Logo × 2 比例 = 4 成片,总耗时 ≤ **1.5×** 单变体 |
| 安全区 | 无 Logo 进入字幕带 / 压关键信息 |
| 命名 | 每个产物名含 variantId,可反查 `variants.json` |
| 降级标注 | 后端缺特性时 `deliverables.md` 有记录 |
