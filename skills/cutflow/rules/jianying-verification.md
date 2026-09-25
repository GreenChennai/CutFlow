# Jianying 出口 · 诚实验收说明(5.9 草稿后端)

> **方法声明**:本文档的写法——「已验证 / 未验证 / 拒绝」三分、逐项写证据与范围、
> 未跑过的绝不写成跑过——学自 jianying-headless 项目 VERIFICATION 文档的**纪律**
> (ADR-X-05:只学方法,不抄内容/字段/代码)。
> 规则:**凡本文档「已验证」区没有的,一律不得对外宣称"已验证"。** 每次动剪映出口
> 相关代码,按实际影响增补本文件,不为交付赶工而降低或含糊边界。

## 2026-09-20:编译层 + 保护区 + 草稿门禁(阶段六 J1–J6)

本次改动:`rs_jy_draft.py` 抽出「IR → 草稿计划(帧对齐)→ 门禁 → 写盘」编译层,
`--dry-run` 映射表,`rs_cut.py` protect 保护区,与 cutforge `export_jianying` 对拍。

### 已验证(本机实跑,证据 = 测试与命令输出)

| 检查 | 结果与范围 |
|---|---|
| 编译层 | IR → 草稿计划:帧/微秒双记法、边界量化、相邻段精确铺贴、转场/淡入淡出/画中画位置缩放/音量/音效 gainDb/`assets_sfx:` 伪协议/BGM 均经编译层映射并有断言(`pytest tests/test_v22_jy_backend.py -q` 的 J1/J4 组) |
| `--dry-run` | 打印「IR 片段 → 草稿片段」映射表,不写任何文件;门禁失败时非零退出且映射表照印(J1 组) |
| 草稿门禁 | 故意构造**帧错位(±100μs)/ 主轨重叠 / 黑场间隙 / 首段非零 / 轨道数漂移 / 时长和漂移**的坏计划,逐一被拒;门禁失败拒绝写草稿(PLAN_GATE_FAIL,退出码 4),且不触发剪映进程检测(J3 组) |
| JY_RUNNING | 剪映进程运行中 → 写盘前拒绝(JY_RUNNING,退出码 4);检测逻辑独立成纯函数并单测(J3 组) |
| 落盘回读 | 真素材(ffmpeg 合成)写入沙箱草稿根后,draft_content.json 与草稿计划逐轨逐段一致、时长一致、root_meta 注册一致(J6 e2e);**该验证在临时草稿根完成,未动用户真实草稿根** |
| protect 保护区 | 切点(remove/review 候选)侵入 protect → 报错不降级;--apply 对人工改刀复验;触边不算侵入;TDD 先红后绿(`tests/test_v22_jy_backend.py` J2 组) |
| 两仓对拍 | cutforge `export_jianying` 编排同一 `rs_jy_draft.py`、scriptArgs 原样透传(源断言 + schema 断言);同一 IR 经 export_jianying 调用形态与直跑 CLI 得到逐键相同的草稿计划(cutforge `tests/test_jy_bridge.py` + CutFlow J6 组) |
| 能力目录/手册 | capabilities.json 再生成零漂移;手册命令 ↔ argparse 机械对拍全绿 |

### 未验证(如实列出,不装作已验证)

- **未在真机剪映 5.9 GUI 中打开产物**:本机自动化只做到结构级断言(draft_content.json 轨道/片段/时长)+ 注册表级(首页可见的 root_meta 条目);"草稿在剪映里打开、可播放、可编辑"需要 GUI 人工操作,本次未执行。
- 其他机器 / 其他 Windows 版本 / 其他剪映 5.9 小版本未跑;`config.jianying59` 路径形态只在本机验证。
- 转场映射只覆盖 IR 封闭集(fade / wipeleft / wipeup / slideleft / circleopen → 叠化族);剪映数百种转场的表现未逐个验证。
- 音量映射 `10^(gainDb/20)` 与 BGM 音量在真机混音中的**听感**未验证(结构写入已断言)。
- 变速(speed)片段在剪映内的音调/时长表现未验证。
- Python 3.14 注解补丁在 3.9–3.13 的行为未逐版本验证(本机 3.14 实跑通过)。
- 画中画 transform 归一化公式沿用 v1 口径,在剪映 UI 中的精确落位未做像素级对照。

### 拒绝(明确不做,写明原因)

| 拒绝项 | 原因 |
|---|---|
| **剪映双向回环**(草稿改 → 工程区) | ADR-0052:5.9 明文草稿会被剪映**规范化重排**、元素 id 不稳定,无法建立可信的内容寻址锚点;6.0+ 加密永不读写。详见下节「为什么不做回环」 |
| **剪映 11.3+ 加密草稿** | 草稿格式加密,读写不可行;铁律:只动 5.9,永不读写 11.3+(config 不含 jianying59 段时直接 NO_CONFIG 拒绝) |
| 原生引擎导出(让剪映无头渲染出片) | 5.9 无受支持的自动化导出通路;GUI computer-use 为辅,失败降级"请用户手动导出"(rules/jianying.md 通道二) |
| 复合片段 / 嵌套草稿 | 实验特性,与 cutforge M12 同口径暂缓 |
| 视觉动效关键帧(motion / reframe / punchIn)写草稿 | v1 草稿后端不写关键帧;IR 已表达但草稿不承载,编译层留显式 warning(不静默丢弃);视觉动效仅 rs_render 支持 |
| BGM ducking(人声闪避)写草稿 | 剪映草稿侧该特性未在 5.9 验证;编译层留 warning,请用户在剪映内手动开启 |

### 为什么不做回环(ADR-0052,单向出口的诚实依据)

用户可以在剪映里精修草稿,但**改动不回流工程区**——这不是没做完,是权衡后的拒绝:

1. **规范化重排**:5.9 明文草稿虽可解析,剪映打开/保存会对草稿做内部规范化重排
   (轨道顺序、字段补全、素材引用重写),盘上字节与语义都不再是写出时的原样;
2. **元素 id 不稳定**:剪映为片段/素材重新分配内部 id,无法把"剪映里改的那一刀"
   可靠映射回 IR 的 clip/wordline/卡片锚点;
3. **锚点不可信**:内容寻址回环要求"同内容必得同地址"。前两条导致任何 diff 都混入
   重排噪声,建立不出**可信**锚点——误报的回环比没有回环更危险;
4. **6.0+ 加密**:新版草稿加密,永不读写(铁律)。

因此:需要"编辑可被 Agent 感知"的修改走 **CutForge 主通道**(`rules/editing-roundtrip.md`);
剪映草稿定位为**半成品单向出口**,落点在工程区 `05_时间线工程/导出/剪映59/`,
是可编辑半成品,**不是交付物、不进 `成品/`**。

### 证据保留

- 测试:`pytest tests/test_v22_jy_backend.py -q`(本文件各表项的机械证据);
- 门禁:`python tests/check_manual_cmds.py`;能力目录:`rs_caps.py check`;
- cutforge 侧:`cargo test` + `python -m pytest tests/test_jy_bridge.py` + `python tools/check_doc_counts.py` + `cutforge-cli check-write-paths`。
