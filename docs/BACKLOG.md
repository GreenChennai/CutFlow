# CutFlow Backlog(自迭代账本)

> 级别:P0 缺陷(必须马上修)/ P1 优化 / P2 弱项 / IDE 新能力想法。
> 每轮自迭代:取最高优先 1–3 项 → 实现 → 验证 → tag `iter-NN` → push。

## 待办

- [ ] **P1** 卡片时段字幕与卡片文字叠写:卡片显示期间字幕应下沉/半透明/隐藏(ASS 可按时间段改 MarginV/alpha)
- [ ] **P2** rs_render 转场吞时长的自动预扣(目前只警告,音频/字幕 startMs 仍需 Agent 手算)
- [ ] **P2** EchoSmith 音色卡加参考音频时长校验(3-10s 引擎硬限,Jimi 卡曾用 15s+ 参考音频导致 400)
- [ ] **P2** rs_bench 采样点去重与相邻帧合并(首2s/末2s 与剪点±1.5s 常重复)
- [ ] **P2** 纯声音视频(成片 B)背景动态化:静态 PNG 偏单调,可用 artboard 6s 无缝循环 MP4 串联
- [ ] **P3** 真·剪映花字:用户 GUI 挑选花字/贴纸/音效拖入草稿保存后,反解 resource_id 建库(ADR-0005 路线 A)

- [ ] **P2** 关键词高亮字幕:ASS 富文本 span 按 IR.subtitle.highlight 上色(rich_text span 机制)
- [x] iter-02:rs_jy_draft 已写入转场(叠化/向左擦除/向上擦除/左移映射+回退)与 clip.fade 音频淡入淡出
- [ ] **P2** rs_jy_draft 写入位置/缩放关键帧(KeyframeProperty:位置滑动、缩放推拉)
- [ ] **P2** 内置 CC0 BGM 小曲库(3-5 首,带 CREDITS)+ `--bgm auto` 按情绪选曲
- [ ] **P2** MomentShift 上游 bug:asr_server._normalize_wav 直接把 build_extract_audio_cmd 结果(不含 ffmpeg 二进制)喂 subprocess → WinError 2。客户端已绕开;上游修复建议开分支提 PR(跨项目,需用户点头)
- [ ] **P2** 教程 16:9 样片第 5 格头顶留白≈0:需在原片确认未裁头皮(judge 备注)
- [ ] **P3** 剪映 GUI 自动导出时改"导出至"目录(目前用默认 Videos 再归档)
- [x] iter-02:README 已加端到端使用示例
- [ ] **P3** rs_bench 网格加时间码标签(需解决 Windows drawtext fontconfig 依赖,可用 Pillow 事后标注)
- [ ] **IDEA** 双后端能力对齐矩阵文档:哪些 IR 特性 FFmpeg 版有/5.9 版有,交付时展示
- [ ] **IDEA** 卡拉OK 式逐字字幕(需字级时间戳,当前 FunASR 只有句级;可由 Agent 按字数插值)

## 已完成

- [x] iter-03:P1 转场吞时长的音画漂移风险 → schema 写明语义约定 + rs_render 渲染前警告;clip.fade 字段入 schema

- [x] iter-02:修复 rs_jy_draft 时间单位错误(ms 误作 μs,历史草稿需重新生成);转场+音频淡入淡出实测落盘正确(17.5s/叠化 500ms 挂前段)

- [x] iter-01:P0 install.ps1 PowerShell 5.1 解析炸裂(无 BOM UTF-8 + 中文)→ 改 ASCII 版并实测通过;
- [x] iter-01:P1 渲染器补齐 IR 承诺的 transition(xfade 链 + acrossfade,17.53s→17.03s 实测消耗正确);
- [x] iter-01:P2 config.example.json 补 momentshift_dir 键。

## 已知非阻塞瑕疵(judge 备注)

- 画中画面板顶部带源素材灰色标题条(用户素材固有,非管线问题)
- 滑入动画方向无法从静帧核验(终帧位置已验证合规)
