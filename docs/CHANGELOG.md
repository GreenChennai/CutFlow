# Changelog

## v0.1.0 (2026-09-07)

首个交付版本。核心链路本机实测通过:

- **cutflow 总控技能**:intake 问卷 → brief.md 契约;感知(FunASR `structured=1` 句级转写 + OCR/VQA + Agent 校对);合成(GPT-SoVITS 音色卡配音,默认 koubo-test,断点续传;artboard 片头/封面桥);剪辑(毫秒级 IR → FFmpeg 七步管线:统一帧率/逐段提取 8ms afade/concat/overlay 含绿幕抠像/混音 ducking + loudnorm -14 LUFS/ASS 字幕最后叠);自评(ffprobe 断言 + rs_bench 抽帧网格目测 ≤3 轮)。
- **剪映 5.9 双通道**:rs_jy_draft 直写明文草稿(draft_content.json + draft_meta_info.json + root_meta_info.json 注册);computer-use GUI 自动导出(Ctrl+E)。
- **cutflow-prompt 技能**:Seedance 2.0 方法论的 AI 首帧图提示词 + 首帧标注式 5-10s i2v 提示词。
- 验收:2 风格 × 2 比例样片全部通过视觉裁决(2 轮修复:画中画按画幅适配、字幕条安全区、整词断行)。

### iter-01 (2026-09-07)

- fix: install.ps1 在 PowerShell 5.1 下解析失败(无 BOM UTF-8 含中文)→ 改 ASCII 版,实测通过
- feat: 渲染器补齐 transition(xfade 链 + acrossfade;时长消耗 0.5s 实测正确)
- chore: config.example.json 补 momentshift_dir

### iter-02 (2026-09-07)

- fix: rs_jy_draft 时间单位错误(ms 误作 μs)——此前生成的草稿片段时长缩短 1000 倍,已修正并重生成验证
- feat: 5.9 草稿写入转场(fade→叠化/wipeleft→向左擦除/slideleft→左移,未映射回退叠化并警告)与 clip.fade 音频淡入淡出
- docs: README 增加端到端使用示例

### iter-03 (2026-09-07)

- fix(P1): 转场吞时长导致的音频/字幕时间轴漂移风险——schema 写明"使用转场时 startMs 需预扣转场消耗"的约定,rs_render 渲染前主动警告
- chore: project.schema.json 补 clip.fade 字段(音频淡入淡出)
