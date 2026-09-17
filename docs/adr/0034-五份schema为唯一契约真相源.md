# ADR-0034: 五份 schema 为唯一契约真相源 + 双端代码生成

日期: 2026-09-17 · 状态: 已采纳 · 关联: ADR-0013(缓存键)、ADR-0033(文件真相源)、《融合迭代计划书》第 3 部分

## 背景

CutFlow 既有契约大部分是自然语言:只有 IR 有 `templates/project.schema.json`;`wordline.json` 与 `cutlist.json` 无 schema;规则散在 SKILL.md 与 19 个 rules/*.md。且已出现实锤口径缺陷——`rs_jy_draft.py` 实际读写 `subtitle.source` 而 schema 未收录(v0.15 已补)。CutForge(Rust)与 CutFlow(Python)双栈并行后,若各自按"猜"的字段建模,契约漂移必然发生,融合即失败。

## 决策

1. **五份 schema 是唯一手写契约**:`schemas/project.schema.json`(v2,由 v1 升级)、`wordline.schema.json`、`cutlist.schema.json`、`notes.schema.json`、`oplog.schema.json`,全部 `additionalProperties: false`(封闭枚举,新增值必须升版本)。
2. **两端消费物都是生成物**:Rust 结构体由 `cutforge-schema/build.rs` 生成;Python 校验器由 `tools/schema_gen.py` 生成(纯标准库,不引 jsonschema 硬依赖)。生成物文件头标注 AUTO-GENERATED,不得手工编辑。
3. **双向对拍门禁**:同一批真实工程样本(覆盖 talking-head / talking-head+animation / pure-animation 三类),Rust 侧与 Python 侧校验结论必须逐样本一致(不一致数 = 0)。
4. **常量单源**:比例/平台预设/帧率集合由 `tools/gen_constants.py` 从 `rs_common.RATIOS` + `templates/platforms.json` 生成 `constants.ratios.json`,Rust 编译期引入,生成期断言逐键相等。
5. **迁移幂等**:project v1→v2 迁移器(补 id/notes/backends/schemaVersion 等)对同一文件迁移两次结果字节级一致;`version` 整数保持 1 兼容旧读法,`schemaVersion` 字符串表达 schema 自身演进。

## 后果

- 正面:双端字段口径由机器保证一致;AI 改动可自动校验、安全 diff;新人/AI 按 schema 即可正确读写,不必通读 19 册规则。
- 代价:改契约必须三步走(schema → 双端生成 → 对拍回归),单端私改字段无效;回归集样本需随版本维护。
- 纪律:现网样本出现 schema 外字段时,必须逐个查明来源后决定收录或迁移,**不得直接放宽 schema**。

## 替代方案

- **各端各自定义类型,靠人肉对齐**:否决——这正是缺口 2 的现状,漂移只是时间问题。
- **引入完整 JSON Schema 运行时(jsonschema 包)**:否决为硬依赖——违背"纯标准库优先";把约束编译成 stdlib 校验代码即可。
- **只做 Python 校验器,Rust 侧手写结构体**:否决——结构体仍是"第二份手写契约",漂移窗口照旧。
