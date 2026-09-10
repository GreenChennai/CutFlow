# ADR-0017:artboard 闭环(manifest 映射表 + 导出回填)

状态:已采纳 ｜ 日期:2026-09-10

## 背景

用户要求:「海报接入的 Skill artboard,要能够用户/AI 手动修改之后能够用脚本触发导出→合成视频一条龙完成,快速高效」。

旧 `rules/artboard.md` 只讲了"怎么调 artboard 的导出脚本",**没有回填机制** —— 导出完还是 Agent 手动把 PNG/MP4 塞进 IR:重新找轨道、改路径、算时间。用户改一张卡片,要重来一遍。

## 决策

1. 新增 `03_assets/artboard/manifest.json` 作为**唯一映射表**:

   ```json
   {"items": [{"id": "card_definition",
               "project": "03_assets/artboard/card_definition/src",
               "sourceHash": "ab31...", "output": ".../export/card_definition.png",
               "kind": "png", "size": [1080, 1920], "usedIn": [{"track": 1, "clipIndex": 0}]}]}
   ```

   - **`sourceHash`** —— 改编没改图,比 hash 就知道,不用 Agent 看图;
   - **`usedIn`** —— 产物挂在 IR 的哪条轨/哪个 clip,闭环的最后一公里;
   - **`size`** —— 导出尺寸,`--apply` 时校验。

2. 新增 `rs_artboard.py`,三个动作:`--scan`(生成清单)/ `--export`(按 sourceHash 只重导变了的)/ `--apply`(回填 IR);
3. **尺寸不符直接报错,不拉伸**:导出尺寸必须等于画幅(9:16=1080×1920 / 16:9=1920×1080);
4. **时长变化会传播**:动画卡时长变了,`--apply` 自动平移同轨后续 clip,并给出需重跑的下游阶段
   (时长变 → S4–S9;仅路径变 → S4/S5/S8);
5. 清单里有卡片没挂进 IR → **点名提醒**,不静默丢弃;
6. `rs_run.py --init` 为 `03_assets/artboard/` 生成**专用** `rebuild.py`:
   `--export` → `--apply` → `rs_run --from S4 --force`,一条龙。

## 后果

- 用户/AI 改完卡片 → 点一个脚本 → 只重导变了的卡片 → IR 更新 → 出片;
- 时长变更不再需要 Agent 手算平移 —— 而字幕时间**自动正确**(字幕来自 Wordline,不受卡片时长影响),只需重渲染,不需要重对齐。这是 v4「单一时间源」设计开始还利息的地方;
- 代价:多一份需要维护的清单;清单与实际 IR 不一致时会报警(而不是静默出错);
- 用户若直接改导出图片而不改工程源码,`sourceHash` 不变 → 不会重导。需 `--force`,或改工程源码(推荐)。

## 关联

- `rules/artboard.md`、`rs_artboard.py`
- 依赖 ADR-0013(增量)、ADR-0016(一键重建)
- `docs/OPTIMIZATION-v5.md` §5
