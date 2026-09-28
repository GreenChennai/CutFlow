# 工程目录契约(目录中文,文件名 ASCII)

> 唯一真相源:`skills/cutflow/scripts/rs_paths.py` 的 `STAGE_DIRS`(ADR-0045 目录中文化 / ADR-0046 路径门禁)。
> 本文件是给人看的对照表;`tests/test_context_budget.py` 会把本表与 `rs_paths.STAGE_DIRS` 逐条对拍,漂移即红。
> 代码里**禁止目录字符串字面量**,取路径只有 `rs_paths.p()/resolve()/check()/ensure()` 一族入口。

## 阶段目录(逻辑键 → 目录名)

| 逻辑键 | 目录 | 用途 | 关键内容 |
|---|---|---|---|
| brief | `00_制作简报` | 问卷与意图编译产物 | brief.md / terms.txt / intent_decisions.json / cards.json |
| materials | `01_原始素材` | 原始素材(**只读**,rs_paths.READONLY) | manifest.json / MANIFEST.md |
| sensed | `02_转写与校对` | 转写与校对件 | transcript_raw/corrected、wordline 校对 diff |
| assets | `03_创作素材` | 创作素材 | artboard/manifest.json、tts/、branding/logos/ |
| cut | `04_粗剪决策` | 粗剪决策表 | cutlist.json / cut_report.md / review/ / rebuild.py |
| timeline | `05_时间线工程` | 时间线工程 | project.json / wordline.json / variants.json / pipeline.json / rebuild.py / 导出/剪映59/ |
| state | `_内部状态` | 阶段状态(内部,不外露) | S*.json / verify.json / backup/\<ts\>/ |
| output | `06_成片输出` | 成片输出 | final_*.mp4 / subtitles.ass / metadata.* / sync_report.md / rebuild.py |
| deliver | `成品` | **交付容器(只读语义;不编号=非阶段;按需创建,清理永不触碰,rs_paths.NEVER_CLEAN)** | 成片/字幕/封面/文案/对账 |

## 铁律与附注

- 工程目录命名:`<YYYYMMDD>-<中文标题>-<类型>`(详见 `rules/archive.md`)。
- `01_原始素材` 只读;大文件与 `models/` 不进 git;测试件一律 `dev-` 前缀,交付前 `rs_cleanup` 必删。
- 剪映 5.9 草稿落点 = `05_时间线工程/导出/剪映59/`(rs_paths.JIANYING_SUB;半成品出口,ADR-0052,不进 `成品/`)。
- 顶层另有根级 `rebuild.py`(全量重建;`rs_run.py --init` 生成;旧工程迁移见 `tools/migrate_paths.py`)。
- 旧英文目录名(`00_brief`…`_state`)经 `rs_paths.LEGACY_ALIASES` 兼容读取,迁移用 `tools/migrate_paths.py` 一键转正。
- IR 最小样例见 `references/ir-sample.json`(schema:`templates/project.schema.json`)。
