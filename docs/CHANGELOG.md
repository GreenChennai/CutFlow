# Changelog

## v0.2.0 (2026-09-08) — 质量精进迭代

十条用户反馈全部落地,成片质量从"链路通"升级到"可观看":

- **字幕轻改写引擎 textopt.py**(ADR-0001/0002):Netflix 简体中文规范(句号/逗号不入屏、?!保留、断行 ≤16 字)+ 抖音红线(无错别字/时间轴同步);删口水词;语义断行;ASCII 词禁切;时间轴按字符插值。9 单测覆盖。
- **绿幕合成管线**(ADR-0003):`chroma.cropTopPct` 裁顶部非绿幕区(实测素材白墙占 0-9.5%)→ chromakey+despill → artboard 虚拟演播室背景;修 overlay **中心点定位**语义(此前卡片/人物下坠半屏)与 overlay fade 时间基准。
- **穿插动画体系**(ADR-0004):知识/对比/章节/观点四类 artboard 动画卡(MP4 25fps),2-3 秒视觉节拍原则;实测对比卡/风控卡/定义卡/AI 卡全屏插入正确节拍。
- **成片 B:Jimi 纯声音动画视频**(ADR-0006):EchoSmith 已合并为 Skill,Jimi 重训权重(e12+s552)全程配音 51 句 152s;修复 Jimi 卡过期 sovits 路径与超长参考音频(3-10s 硬限)。
- **趣味素材**(ADR-0005):Mixkit 免费音效 7 个入库(assets/sfx + CREDITS),IR 支持 `assets_sfx:` 伪协议;剪映云端素材确认不可离线用,本地字体花字感为替代。
- **真人封面**:抽帧 → rembg 抠像(isnet)→ PIL 去绿边/裁杂物 → artboard 合成标题+人物+播放键封面。
- **rs_doctor --report**:人读环境自检报告(分组/就绪度/修复提示),12 项检查。
- 修复:rs_asr 行内时间戳解析、rs_ir assets_sfx 校验、Jimi 首句"电子"→"电商"(音频+字幕同步重合成)。
- 验收:judge 两轮,成片 A/B 全部通过(成片 A 167.6s,成片 B 152.5s,均 1080×1920@30)。


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
