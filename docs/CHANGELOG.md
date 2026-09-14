# Changelog

## v0.12 (2026-09-14) — 安信德 GEO 纯动画实测批修(B1-B8)· 纯动画 IR 组装器(I7)· 热词(I1)· 文本裁片(I2)

来源:安信德 GEO 品牌宣传工程(pure-animation,16:9 / 19 卡 / 158.93s)实机复盘;对账 `docs/HANDOFF-v0.12-安信德GEO实测迭代与修复.md`。**测试 250 → 281 全绿**(新增 `tests/test_v12.py` 31 用例)。

### 必修 Bug(现象 → 根因 → 修法)

- **B1 字幕系统性提前收字(致命)**:终点偏移中位 -170ms、59/68 卡早退(个别 -680~-1420ms);同一卡在 ass / cards.json / wordline 三处时间不一致。根因:`segment()` 对 text 做 `.strip()` 剥掉句首空白后,`index_map`(句内位置 → chars 下标)未同步裁剪,此后一切按位取值系统性偏移 1。修法:**双侧对称同步裁剪**(句尾全角空格同样致命;末尾切片用 `len-map - trail_n`)。⚠ **本修复会改变断句结果**(复盘实测卡数 68→79、节奏 ≈2s/卡)——早退字幕是硬伤,卡数变化是修复副产,预期行为。回归:`test_segment_strip_keeps_index_map_aligned`(前导/尾随全角空格,charSpan 首末下标精确断言)。
- **B2 音频内容闸误报(高)**:`--audio-content` 相似度 0.888 < 0.90 判 FAIL,人工对账差异几乎全是良性(`1500`↔`一千五百`、`10800`↔`一万零八百`、`2980`↔`两千九百八十`、`4.3`↔`四点三`、`四九八零`↔`4980`)。根因:`_norm_hard` 只做 lower+去标点,无中文数字折叠。修法:值读/位读/小数三种读法折叠,两侧同口径(ref 与 ASR 一起变,单侧语义误折叠无害)。**未调 `AUDIO_SIM_MIN`**。回归:`test_norm_hard_folds_cn_numbers`(表驱动 12 例)+ `test_audio_content_sim_improves_with_fold`(折叠后 ≥0.94)。
- **B3 BGM ducking 滤镜图标签二次消费(致命)**:`ducking:true` 必报 `MIX_FAIL: Stream specifier 'a1' matches no streams`(该工程靠 IR 写 `ducking:false` 绕开,bug 仍在)。根因:ffmpeg filtergraph 标签只能被消费一次,旧图把 `[a1]` 同时喂给 sidechaincompress 与 amix。修法:**全部人声先合成一条总线 `[voice]`**,再 `asplit=2` 出闪避侧链与正式混音两路(`duration=first` 语义保留,以人声为准)。ducking:false 分支不动;历史 IR 不批量改回(rules/compose.md 说明"现在可以开")。回归:`test_mix_ducking_graph_has_asplit`(字符串护栏)+ **实机** `test_mix_ducking_renders`(真跑 step_mix,断言时长 ≈ 人声)。
- **B4 字幕静默不烧(高)**:第一版"成功渲染"的成片**没有字幕**,靠 L1 抽帧才发现。根因:IR 只写了 `subtitle.source` 没写 `subtitle.ass`,`step_subtitle` 遇 ass 缺失无声返回。修法:`subtitle.source` 存在而 `ass` 缺失 → 渲染 warnings 显式 WARN「本次不会烧录字幕」;`rules/compose.md` 改写契约——**`subtitle.ass` 才是烧录字段,source 仅作溯源**。回归:`test_render_warns_when_ass_missing`。
- **B5 QC 体检四处口径不一致(中)**:rules/verify.md 列 QC 为 L0 判据、rs_run S9 有 `--qc`、README 写了,但 `rs_verify` 的 `L0_CHECKS` 不含 QC、SKILL.md 命令速查漏 `--qc`——只跑 rs_verify 的路径永远不体检。修法:`check_qc` 进 L0(成片存在即对最新成片跑 `rs_sync.run_qc`;QC 不可用 → skipped 留痕不硬失败,ADR-0021 失败语义);SKILL.md 补 `--qc`。注意:有成片的工程 L0 会因此变慢(全片扫描 + 响度测量),且 QC FAIL 会让 L0 失败——这是 rules 早已承诺的行为。回归:`test_verify_l0_includes_qc_when_video_present`。
- **B6 3 字孤卡(中)**:破折/短语收尾切出 3 字孤卡「说谁好」。根因:`_card_penalty` 对 <4 字只罚 -1.2,DP 在长破折句上仍会选出孤卡方案。修法:保持 `MIN_CHARS=2` 不变(调 4 会制造超字数无解),新增**后处理孤卡合并**:末卡 <4 字且并入前卡 ≤ max_chars → 并入(时间按合并后首末字重新锚定,对齐精度不动);并不下 → violations 显式记 `orphan-card` 不静默。⚠ **卡数会变**(孤卡并入前卡)。回归:`test_no_three_char_orphan_card_on_dash_sentence` + REGRESSION 固化 + `test_orphan_card_reported_in_violations`。
- **B7 wordline 平滑上游化(中)**:纯动画工程自写 `_smooth_wordline.py` 把标点设零宽(endMs=startMs)违反 rs_verify 单调门禁(147 字 endMs≤startMs 硬失败)。修法:新增 `rs_align.py smooth` 子命令——标点零宽 `end=start+1`(+1ms 恰好严格单调)、起点单调化、内容字重叠钳制(前字 end 收到后字 start)、最小宽度保底;**不动 build 路径的 `max(b, a+20)` 保底**(单调门禁第一道护栏)。回归:`test_align_smooth_punct_zero_width_monotonic` + `test_align_smooth_clamps_overlap`。
- **B8 ffmpeg `-t` 位置陷阱(高,丢 20.4s)**:10 张补长卡各短一截(视频流 138.77s ≠ 预期 159.17s),靠 rs_render 音画对齐断言抓到。根因:`-t` 放输出侧会把 tpad 补的帧整段截掉。修法:`references/ffmpeg-recipes.md` 补正确/错误对照;`rules/selfcheck.md` 写明「渲染后时长断言 ≠ 通过即停下」;I7 冻结帧实现即按输入侧 `-t` 落地。

### 新能力

- **I7 纯动画 IR 组装器(ADR-0027)**:`rs_ir.py build --from-cards <manifest> --anchors <cards.json> --wordline <wl> [--voice --bgm]`——字符级锚点分组(命中消费/标点继承/同卡相邻合并/空组显式报错/hit 标志防不前进)+ 停顿中点切卡 + 冻结帧补长(`clip.freezeMs`,渲染端 tpad;`CACHE_VER` v4→**v5**,旧 seg 缓存失效一次)+ `_manual_edits` 护栏照旧 + subtitle.ass 写全。**消灭"每个纯动画工程重写三个脚本"**(安信德 `_build_ir.py` 等已上游化);文本锚定抽 `rs_common.content_index/anchor_span` 共享(rs_subtitle override 委托同源)。回归:`test_assign_card_groups_*` / `test_build_from_cards_end_to_end`(3 卡最小工程一条命令出 IR、validate 全绿)/ `test_freeze_frame_extends_segment`(实机)。
- **I1 ASR 热词链路(ADR-0028)**:`rs_align build --hotwords "安信德 GEO优化"` / `--terms-file 00_brief/terms.txt`(每行一词,# 注释)透传自带 ASR;wordline `asr.hotwords` 留痕。**实测结论:默认模型 paraformer-zh 即 SeACo-Paraformer(funasr 源码映射),热词原生生效,无需切档**;专名错 → 先补热词重跑,不要手工改字(rules/asr.md §5.1)。回归:`test_align_passes_hotwords_to_asr`(monkeypatch 断言命令行)。
- **I2 按文本裁片**:`rs_cut.py <wordline> --from-text "引文"`(或 `--from-text-file`)——引文顺序锚定(rs_common.anchor_span,乱序拒绝)→ 只保留引文区间;引文外两刀走现有 guard(窄 gap 出点借不进 → 保守 review,宁可漏删);`cutlist.fromText` 留痕。回归:`test_cut_from_text_builds_keep_intervals`。
- **I6 制作端 checklist**:`rules/intake.md` 补绿幕四问(离幕距离/幕面亮度差/服装对比/快门)+ 纯动画四问(比例/卡数节奏/旁白来源/卡定稿)+ 环境 checklist(`WPI_FFMPEG` 与 `ARTBOARD_FFMPEG` 都要设、ASR probe)。

### 交叉技能条目(只在本仓库留规则,不改 artboard 本体)

`rules/artboard.md`:单一外层统一出场/内层只挂入场 + 计数器内容为空(#7);定宽网格先算总宽再定起点(#8);`WPI_FFMPEG` 才是 MP4 导出有效环境变量(#13);Mode S 录制 t0=2.0s 起算/片长 ≈ 时间轴 −1.8s/`--max-wait` ≤15s(#16,ADR-0012 补全);scaffold 落盘由 `config.studio_dir` 决定、进工程目录用 `ARTBOARD_STUDIO`。`references/ffmpeg-recipes.md`:Windows/Git Bash 引号与路径纪律(#15);chromakey→colorkey 与 BGM 闪避 asplit 版写法勘误。

### 文档

ADR-0027(纯动画 IR 组装器)、ADR-0028(ASR 热词链路)落盘;CONTEXT.md 补 5 个术语;BACKLOG 更新(I4 说话人分离 P2 不做、I5 音频事件 IDEA、W5 锚定共享已部分完成);SKILL.md 命令速查 + 硬规则 23;README 命令速查;rules:compose / verify / asr / intake / artboard / selfcheck / 纯动画分册。

## v0.11 (2026-09-14) — QA 闭环 · 转场三级语法 · punch-in · 粗剪智能化 · 文本化(ITERATION-GUIDE R1-R5)

- **R1 QA 闭环**:`rs_sync --qc` 成片体检——黑帧 ≥0.3s / 冻结 ≥2.5s / VFR 混帧 = FAIL,片内静音 ≥2s 告警(首尾白名单);响度测量进报告(I∈[-15,-13] LUFS、TP ≤ -0.9 FAIL)。**final 档双 pass loudnorm**(linear=true,先测后编;preview/draft 仍单 pass)。**matte 探针**:绿幕段渲染时第二输出抽 alpha 前景占比,<1% 或 >70% → `matte_suspect` 告警(geq 字节域事故的机械护栏);CACHE_VER v4。
- **R2 转场三级语法(ADR-0026)**:`rs_ir` 按源间隙标注 `transition.reason`——<1s=jumpcut(1 帧软切:视觉即硬切,吃掉姿态/alpha pop 与爆音);≥1s=topic(300ms 溶解,Reisz 语法);无 reason 亚帧仍提升 joinCrossfadeMs(ADR-0023 不回退);cap 与整链回退不变。
- **R3 punch-in**(opt-in):渲染端 clip `punchIn.factor`(1.0-2.0,anchorY 纵向偏置);`rs_ir --punch-in-auto` 启发式:真剪辑点(移除 ≥1.2s)后 1.4x,密度成片时间轴 15s/≤3 处(Hitchcock:紧构图只给重点)。
- **R4 粗剪智能化**:词表外置 `templates/fillers.json`(brief 阶段补口癖,缺省回退内建);margin 不对称(前 150/后 300ms);防碎切 smooth(间隙 <100ms 并刀、<120ms 碎刀放弃——宁可漏删);新 `hesitate` 检测器(0.3-1.2s 无字段能量谷 → review,需 `--media`)。
- **R5 文本化**:每刀带 `text` 前后 1.2s 上下文;`cut_report.md` 尾部产出**删改稿**(~~remove~~/**review**/keep 分行)——机器粗剪、人读稿精修(对齐 Descript/Premiere TBE 心智)。
- schema:transition.reason / clip.punchIn;docs:ADR-0026、rules/roughcut、rules/compose、rules/verify L0 表、SKILL.md。测试 239 → **250 全绿**(新增 test_v11.py 11 用例)。

### v0.11 实机全管线跑批修(dev 工程 S0→S9 实测)

- **xfade 链截断(致命)**:段文件尾帧被编码取整吃掉 1-2 帧,按名义长度递推 offset 会在链上累积缺口——该 ffmpeg 构建在 offset+dur 贴齐/越过 input1 长度时 xfade **整段坍缩**(实测:视频 44.7s/音频 145s)。修复:concat 用**实测段长**递推,offset 保持名义值(零漂移不变),duration 按 `cum - offset - 1帧安全边际` 夹紧;回归 `test_concat_no_truncation_with_short_tails`。
- **matte 探针路径**:`metadata=print:file=` 的绝对路径冒号在 filtergraph 单/双转义均解析失败 → 改相对文件名 + `run(cwd=segcache)`(`rs_common.run` 增 cwd 参数)。
- **rs_verify 短卡误报**:规范明文 <0.83s 是软告警(subtitles.md §8),L0 却硬失败 → 短卡归 `softWarnings`,硬失败仅保留 >7s/字数/CPS/重叠。

## v0.10.1 (2026-09-14) — 整片验收批修(店群工程实测,4 个真 Bug)

整片重渲验收中暴露、均已修复并补像素级/占位符回归:

- **geq 字节域腐蚀(致命,ADR-0022 修订)**:`_chroma_fg_chain` 旧式 `clip((alpha-t)/(1-t),0,1)` 把 geq 的 0-255 原始 alpha 当 [0,1] 算 → 恒输出 1 → **人物整帧透明,全片无人**(音轨正常,极具欺骗性)。修复:换算到字节域 `clip((alpha−t×255)/((1−t)×255)×255,0,255)`;新增实机回归 `test_chroma_chain_preserves_opaque_subject`(真跑 ffmpeg 断言像素 alpha)。
- **S1 `{first_material}` 选中 manifest(致命)**:Windows Path 排序大小写不敏感,`manifest.json` 排到素材前 → S1 把 manifest 喂给 ffmpeg,凡 CJK/大写命名的素材新工程必崩。修复:`pick_asr_media()` 按音视频扩展名过滤(manifest/图片不再可能入选)。
- **S9 对账用了源空间 wordline(假失败)**:`wordline.json` 恒为源空间,S9 拿它对账成片时长必差一个粗剪裁剪量(-28.1s)。修复:S9 改用 `{final_wordline}`(优先 `wordline.final.json`,与 rs_verify 同一约定)。
- **`rs_sync --video` 契约漂移**:文档/S9 spec 传 `--video`,argparse 只认位置参数 → S9 必崩。修复:两种都收,`--video` 优先。
- **Logo 透明 padding 稀释视觉尺寸(ADR-0025 补,e2e 实测 12%→3.7%)**:`variant_ir` 现在把带 padding 的 Logo 预裁剪到内容包围盒(`crop_to_content`,按 bbox 缓存派生文件),可见 Logo 精确等于 `scale` 承诺;e2e 差分实测落点 ±1px、宽度 12.0%。
- 测试 236 → **242 全绿**;`{final_video}` 改按 mtime 取最新(防变体字典序误选)。

## v0.10 (2026-09-14) — 边缘精修 · 零漂移交叉溶解 · 内置 ASR 收尾 · Logo 真实尺寸(ADR-0022/0023/0024/0025)

- **边缘精修(ADR-0022,反馈#1)**:`_chroma_fg_chain()` 重写 —— alpha 腐蚀收缩(大 sigma 模糊+偏高频阈值硬化)→ 细羽化,治发丝锯齿与黑边;`killRects` 默认 `killRectMode=green`(框内只清 `lt(cb,116)*lt(cr,116)` 绿幕主导像素,不再连人体一起抹,店群工程"左下角闪烁黑影"根因);`CHROMA_DEFAULTS`(similarity .15/blend .12)三处统一真相源;overlay 层同链;`CACHE_VER` v2→v3。
- **零漂移交叉溶解(ADR-0023,反馈#2)**:`0<durMs<1帧` 的转场自动提升为 `joinCrossfadeMs`(默认 120ms,doc/config 可覆,0 禁用)交叉溶解;**尾帧扩展法**——段 i 多渲 `tails[i]` 尾帧,xfade `offset` 恒等于后段名义起点,成片时长与字幕时间零漂移,末尾 `-t` 裁齐;段边界帧量化;仅"源间隙放不下尾帧"才整链回退 concat。
- **内置 FunASR 收尾(ADR-0024,反馈#3)**:修 `ensure_backend()` 调用未定义 `any_backend_ready()` 的必崩 NameError("ASR 未启动"真凶);`rs_doctor` 删已废弃 HTTP server 探测改本地 `--probe`;`rules/asr.md` 写死"未就绪=自动部署,禁止让用户手装/启动服务"。
- **Logo 真实尺寸(ADR-0025,反馈#4)**:`probe_logo()` 像素宽高+alpha 内容包围盒(修 rgba 子串匹配 bug);`logo_rect()` 按真实宽高比缩放、高上限 8% 画高、anchor 扩 6 位含 topCenter/bottomCenter、bottom 系自动避开字幕带(`lifted` 留痕);`variant_ir` 产 `overlay={x,y,w,h,opacity}` 绝对落点,`rs_render.step_compose` 消费(修"产而不消致 Logo 贴满画布"潜伏致命);`--analyze` 子命令;`check_safe_area` 补 x/四边。
- 测试 214 → **236 全绿**(新增 `tests/test_v10.py` 22 用例);ADR-0022/0023/0024/0025 落盘;tmp 实验残留清理。

## v0.8.2 (2026-09-13) — 纯口播复测 B 系列批修(BUGREPORT-20260913 B1–B10 / ADR-0021)

- **#B1 `rs_render step_mix` 消费 `sourceInMs`**(致命):多段人声每段从源 0s 取 → 片头反复、音画错位;改为输入侧 `-ss` 寻址,`sourceInMs` 纳入 mix 缓存键。voice_full 绕过法不再必需。
- **#B2 转场字段统一 `durMs`**(致命):`rs_ir build` 改写 `durMs`(旧 `ms` 静默按 500ms 吞时长);validate 拦截旧字段;`step_concat` 对 **tdur<1 帧**的转场整链弃用走无损 concat(防 xfade 坍缩);渲染后新增视频流时长 vs IR 预期的音画对齐断言(>1.5 帧告警)。
- **#B3 `rs_subtitle` snap 后重跑 `_enforce_gaps`**:floor/ceil 推回的 30–40ms 卡片重叠在锚点余量内消掉(rs_sync 1 帧容差不再被逼 FAIL)。
- **#B4 `rs_cleanup` keep 规则重写**(ADR-0007 回归):`final_*.mp4`/`subtitles.ass`/`master.srt`/`metadata.*`/`*report*.md`/`cards.json` 等交付物默认必留,只删探针件与非交付物。
- **#B5 cards.json 最终时间快照**:`finalTimes` 标注 + 落盘兜底 `_finalize_events`(防倒挂/漂移);回归断言 cards.json == ass 逐毫秒一致。
- **#B6 `rs_bench` 假成功修复**:按**视频流时长**布点(音频垫尾不再撑长采样区间);产物存在且非空才报 OK,否则 `BENCH_EMPTY`。
- **#B7 override 三种定位 + 部分替换**:`text` / `textPrefix+textSuffix` 文本锚定(去标点顺序定位,杜绝按内容字数算术偏移切错位);覆盖不足时以 DP 分组为基底部分替换;卡尾标点至多带一个。契约:`rules/subtitles.md` §10.3。
- **#B8 rebuild 起点陷阱**:`rs_ir build` 检测手注痕迹(chroma/background/manualEdit)拒绝覆盖(`IR_MANUAL_EDITS`,`--force` 显式确认);`05_ir/rebuild.py` 模板写明"手改 IR → 跑 `06_output/rebuild.py`"。
- **#B9 L0 中间态不误报**:阶段式运行后字幕缺失 → skipped(未涉及),不再 ✗。
- **#B10 S9 成片音频内容闸(ADR-0021)**:`rs_sync --video 成片 --audio-content` —— 音轨 ASR 与 wordline 对账(片头句=1 次 / 相似度 ≥0.90 / 无重复段),结果缓存;B1 级"时长正常但内容损坏"从此有机械闸。
- 测试 195 → **214 全绿**;新增 `tests/test_v9.py`(19 用例);修复记录:`docs/BUGFIX-20260913-B1-B10.md`。

## v0.8.1 (2026-09-13) — 词边界切分 · Agent 复核 override · v2 重跑 bug 批修（ADR-0020）

**R1 分词彻底修复(#W1,ADR-0020)**

- `segmentation` 新增**词边界层**:`word_spans()`(jieba 优先,失败降级内置高频词表 COMMON_WORDS + terms/idioms 并入);词内位置**强禁切**;校对稿**空格前禁切/后强候选**(词组边界)。
- **两阶段 DP**:词内全禁 → 无可行解才降级词内强惩罚(−3.0)重跑,`wordFallback`/`wordFallbackSentences` 如实留痕,不静默切词。
- `REGRESSION` 增两字词用例(非常/但是/最后/失败 不跨卡);门禁 §8 增「两字词不跨卡 = 0」。
- jieba 初始化日志会污染 `--json` 契约 → 重定向 stdout 静默初始化;`rs_doctor` 增 jieba 非致命检查;`fetch_deps.py subtitle` 安装(可选)。

**R2 Agent 复核修正闭环(#W2)**

- `rs_subtitle` 卡片输出补 **`charSpan`**(wordline 内容字全局索引),新落盘 **`cards.json`**;新增 **`--override`**:按 Agent 修正文件的 span 从 wordline 字级锚重建卡片,重跑必并/延长/间距/帧对齐/硬约束,`meta.audit` 逐卡留痕;span 非法显式 `BAD_OVERRIDE` 报错。契约见 `rules/subtitles.md` §10。

**R3 v2 重跑 bug 批修**

- **#B1 `rs_render step_mix` 音频调度**:旧链 `adelay` 在前、`atrim` 在后,startMs>0 的段被裁错(实测音轨缩到 13.9s)→ 抽出 `_voice_chain()`,**先裁后延**;lavfi 双正弦实测混音时长 ≈6s。
- **#B2 `rs_artboard` 三连**:①`scan` 的 `project` 统一写 `<卡片>/src`,hash 口径四处(scan/changed/export/apply)一致(旧版 scan 按 src、changed/export 按卡片目录 → 永远误报"已变化");旧清单兼容(`_src_dir` 归一);②`apply` 未引用卡片**默认跳过+告警**(`skipped`),`--strict` 恢复硬失败、`--only` 只校验子集(多变体不必拆 manifest);③clip src ↔ output 匹配改为**归一化绝对路径**(相对/绝对/反斜杠一视同仁)。
- **#B3 junction config 错位**:`rs_artboard` config 查找改**仓库根优先、`skills/config.json` 兜底只补缺**;新增 `skills/config.example.json`(`skills/config.json` 已被 .gitignore 的 `config.json` 规则覆盖)。
- **#B5 `rs_sync` 伪重叠**:`snap_events_to_frames` 后追加热碰消解(锚点有余量时推迟后卡起点/收早前卡终点);`summarize` 重叠判定容差放宽到 **1 帧**(`--frame-ms` 可调,默认 34ms),3ms 级帧取整伪影放行、真实重叠照常 FAIL,报告如实计数。

**质量与验证**

- 测试 170 → **195 全绿**;新增 `tests/test_v8.py`(词边界/override/step_mix 链序+实测/artboard 口径/rs_sync 容差,25 用例)。
- ebur128 项:经用户对比确认为 ffmpeg 内置滤镜且输出正常,**跳过不改**。

## v0.8.0 (2026-09-12) — 鲁棒性 · S0 摄取 · 配音对齐 · 动画卡重叠（OPTIMIZATION-v7 #7/#9/#10/#12）

**R1 消灭静默降级(#7)**

- `rs_cut._extract_clip` 改为返回 `bool`;审查包抽音频失败写 `review/_DEGRADED.md`,并在该刀 md 里标注 —— 不再 `except: pass`。
- `textopt.card_split / build_cards` 新增 `degrade` 列表:退回长度算法时记录原因;`rs_subtitle.events_from_wordline` 把**单句 DP 失败**降级为"该句退回长度算法"(不再让一句炸掉整条字幕),原因写进 `degradeReasons`。
- `rs_common.resolve_voice`:坏掉的音色卡不再静默 `continue`,找不到音色时点名"另有 N 张卡读取失败"。
- `rs_common.ensure_utf8()` + `rs_doctor` 报告符号 GBK 安全(修 cp936 下 `--report` 崩溃)。

**R2 S0 素材摄取 + 交付清单(#9)**

- 新增 `rs_ingest.py scan <工程>`:`01_materials/` → probe → `manifest.json` + `MANIFEST.md`(probe 失败写 `failed/unavailable` 并点名,不阻塞),并生成 `05_ir/project.skeleton.json`(**不覆盖**已有 `project.json`)。
- 新增 `rs_ingest.py deliverables <工程>` → `06_output/deliverables.md`(成片清单 / 画幅 / 验证等级 / 粗剪与对齐摘要 / 缺失项点名)。
- `rs_run` 的 S0 登记 `rs_ingest.py`;`SKILL.md` 阶段表与命令表同步。

**R3 配音强制对齐 `rs_dub`(#10)**

- 新增 `rs_dub.py align --wordline … (--audio … | --from-asr …) [--write]`:自带 ASR 转写**配音音频**取真实字级时间戳作为参考轴 → `rs_align.retime_to_reference()` 把目标文本锚上去(equal 区间零漂移)→ 逐句漂移报告 `06_output/dub_report.md`;`--write` 写回并置 `charTimingEstimated=False / degraded=False`。
- **ASR 拿不到字级时间戳时拒绝写回**(`DUB_NO_WORD_TS`,退出码 3)并给出 `--backend pkg` 修复提示 —— TTS 路径不再有"估算当字级"的空子。
- `rs_align` 抽出 `_apply_opcodes`(retext 与 dub 共用)+ 新增 `retime_to_reference()`。

**R4 字幕 ↔ 动画卡重叠检查(#12)**

- `rs_sync` 新增 `check_card_overlap()` 与 `--ir / --strict-cards`:artboard 卡片时间窗压住字幕卡时在报告点名(默认告警,`--strict-cards` 才判未通过);普通素材轨(口播/绿幕)不算重叠。

**质量与验证**

- 测试 152 → **165 全绿**；新增 `tests/test_v7_e2e.py`（lavfi 合成素材端到端：S0 摄取 → S1 对齐 → S2 粗剪(带音频探测) → S3 IR → S7 字幕(3:4 平台预设) → S8 真渲染 → 对齐自检 → L0 自检 → 交付清单；无 ffmpeg 时整模块跳过）。

**R5 复核整改（code-review 两轴）**

- 端到端抓到并修掉：`rs_verify` 检查名含 `↔`，GBK 控制台下 `emit()` 崩脚本 → `rs_common` **导入即** `ensure_utf8()`。
- 文档失真修正：`rules/platforms.md` 的"新画幅改两处"→ **三处**（补 `rs_subtitle.STYLES[*].size/margin_v`，并加一致性用例说明）；`rules/align.md` 的 TTS 条目改为描述现状（句级实测 + 句内估算**显式标注**，字级由 `rs_dub` 补）。
- 代码去重：新增 `rs_common.guard_passed()`（粗剪/自检共用一套 guard 口径，替换 3 处重复的 `okByReason` 兜底）与 `rs_common.p95()`（替换 2 处索引式分位）。
- `rs_dub` 报告不再失真：未加 `--write` 时明写"**未写回**"。
- `detect_dead_air` 补 `reason` 分档（短 `breath` / 长 `silence`）。
- 补齐规格项：`rs_sync --legacy-end`（历史工程终点门禁只告警）；平台预设 `cpsMax` 真正被 `rs_subtitle` 消费（此前是死数据）。
- 测试 165 → **170 全绿**（连词 20 句抽样、`--legacy-end`、平台 `cpsMax` 消费、dead_air 分档）。

## v0.7.2 (2026-09-12) — videoType 三类型取代 genres · 技能组精简（OPTIMIZATION-v7 #5/#6/#8）

**R1 删除第二个技能组(#5)**

- `skills/cutflow-prompt/` 内容归档到 `docs/archive/cutflow-prompt/SKILL.md` 后删除;`tools/install.ps1` 现在只安装 `cutflow`。
- 同步清理引用:`skills/cutflow/SKILL.md` 的"AI 生视频"指引改为指向归档;`docs/PLAN.md` 加停用横幅并标注 §6.3(历史段落不改写);`docs/CHANGELOG.md` 的历史记录保留不动。

**R2 `videoType` 取代 `rules/genres/`(#6)**

- **一级枚举**:`talking-head`(纯口播)/ `talking-head+animation`(口播+动画)/ `pure-animation`(纯动画);**预留扩展位** `screen-recording` / `interview` / `drama` / `film-commentary`(只写注释,不建空文件)。
- 新增 `rules/video-types/` 四册,每册固定结构「管线分支 → 节奏参数表 → 结构模板 → CutFlow 对应 → 红线」:
  - `纯口播.md`:绿幕必抠(`cropTopPct` → `colorkey`+`despill`,禁裸 `chromakey`)、虚拟背景、字幕重中之重、动画密度少/零、粗剪必做;
  - `口播+动画.md`:继承纯口播 + artboard 卡片体系,新增**流畅性硬线**(缓动/200–300ms/禁线性匀速/文字 1s·13 字/最短停留 1.5s)与**贴合性硬线**(卡片时间窗必须落在所解说句子的时间窗内、错位 >1 卡不合格、人物与卡片切换时口播不停);
  - `纯动画.md`:场景卡 + 6s 循环背景,**声音来源二选一**(音色卡 TTS / 视频中人物原声),TTS 无字级戳时标 `charTimingEstimated`;
  - `_通用规则.md`:响度/安全区/字幕三定律/节奏/混音/版权 + **类型补充**(原新闻采访·短剧·影视解说的题材红线,标注"暂停维护")。
- `rules/genres/` 六册**先并入再删除**;同步 `templates/brief.md`(videoType + 声音来源 + 平台预设)、`rules/intake.md` 项 0、`SKILL.md` 路由表与 Hard Rule 17、`README.md`、`CONTEXT.md`(新增"视频类型""平台与画幅"两节)。
- **ADR**:新增 `docs/adr/0018-videoType取代genres.md`、`docs/adr/0019-平台字幕预设.md`;`0010` 顶部标注被 0018 取代。

**质量与验证**

- 测试 146 → **152 全绿**。

## v0.7.1 (2026-09-12) — 平台字幕预设 · 新增 1080×1440（3:4）（OPTIMIZATION-v7 #4）

- 新增 `templates/platforms.json`：抖音 / 视频号 / 小红书 / B站 四平台预设（比例、画布、风格、每卡字数、安全区、封面尺寸、时长倾向）。**字幕规格终于有数据可查**，不再靠人记。
- **画幅单一事实源** `rs_common.RATIOS`（9x16 / **3x4** / 16x9）+ `canvas_for()` / `ratio_for_canvas()`。`rs_render`、`rs_brand`、`rs_ir`、`rs_artboard`、`rs_jy_draft`、`rs_verify` 的硬编码/字符串比较全部收敛为查表。
- **新增画幅 1080×1440（小红书 3:4）**：`segmentation.MAX_CHARS` 增 `3x4=15`（可调）、`CPS_MAX` 增 `3x4`；`rs_subtitle.STYLES` 三档样式补 `3x4` 字号与 `marginV`；`project.schema.json` 的 canvas 枚举加 `1440`、outputs 枚举加 `3x4`；`rs_ir.validate` 画布白名单改查表。
- `rs_subtitle` 新增 **`--platform`**（+ `--max-chars`）：**显式 `--style/--ratio/--canvas/--max-chars` > 平台预设 > 内置默认**；未知平台直接 `BAD_PLATFORM` 报错（不静默退回默认，避免悄悄出一版错规格的片子）；`--style` 默认值改为 None 以便让预设生效；输出 data 增 `ratio/platform/canvas/charTimingEstimated`。
- **修掉 CPS 口径错**：`rs_subtitle` / `rs_verify` 原先无论什么比例都取 `CPS_MAX["9x16"]`，现改用 `segmentation.cps_max_for(max_chars)`；`rs_verify` 从 IR 画布反查比例取 `maxChars`。
- 新增 `rules/platforms.md`（平台预设说明 + 安全区表 + 优先级 + 坑位），并在 `SKILL.md` 路由表/命令表登记；新增硬规则 20（卡时间只在释放余量内调整）与 21（画幅/平台只查表）。
- 测试 139 → **146 全绿**：四平台预设覆盖、比例表一致性（新增画幅防漏改）、CPS 按比例取值、schema 允许 3:4、`--platform` 生效、显式参数优先、未知平台报错。

## v0.7.0 (2026-09-12) — 字幕同步 · 断句连词 · 粗剪废片段（OPTIMIZATION-v7 #1/#2/#3/#11）

针对用户三大成片问题（字幕与声音对不上 / 断句切词 / 口播废片段没剪掉）落地。测试 118 → **137 全绿**（新增 `tests/test_v7.py` 19 项）。基线方案见 `docs/OPTIMIZATION-v7.md`。

**R1 字幕↔音频同步三件套(#1)**

- `rs_sync` 补**终点偏移**校验：`endOffsetMs = 卡尾 − (末字 endMs + 20ms)`；新增两个硬失败项「**早退**（终点早于末字 >25ms = 切掉语音）」与「**滞留过久**（终点晚于末字 >350ms）」，外加终点中位数/95 分位（60/120ms）。报告与 `sync_rows.json` 同步扩展。
- `rs_subtitle._enforce_gaps` 改为**锚点有界**：只在「释放余量」内调整（起点 ≤ 首字 `startMs`、终点 ≥ 末字 `endMs`），余量耗尽仍不足 2 帧 → **保持字级精确时间**（对齐精度 > 卡间距）。不再为凑间距切掉末字语音（"偏快"根因）。
- `_extend_short` 设上限 = 末字 `endMs` + **0.30s**，且不越过下一卡（"字幕滞留到停顿里"根因）。
- `_merge_short` 合并同步锚点；`_kar_text` 末字结束时间改取**末字真实 `endMs`**（卡尾可能被可读性延长）。
- 新增 `snap_events_to_frames`：ASS 时间量化到帧（起点向下、终点向上）；`rs_subtitle --fps / --no-snap`。
- `rs_align.build_wordline` 无字级时间戳时置 `charTimingEstimated=True`、逐字打 `estimated`，`degradeReasons` 明写"卡内位置为估算(不可当字级用)"。**落地修订**：原计划的"整句一卡"实测会让长句超字数、直接伤观感，故**保留按 `max_chars` 出卡**，但卡内位置不再被当成字级（显式标注 + 拒绝用于卡拉OK + 报告点名）；真字级由 #10 `rs_dub align` 补齐。
- `segmentation._relax_gaps` 同样改为锚点有界。
- `CPS_MAX` 取值新增 `segmentation.cps_max_for(max_chars)`，替代 `rs_subtitle` / `rs_verify` 里写死的 `CPS_MAX["9x16"]`（为 #4 多画幅铺路）。

**R2 断句连词切词(#2)**

- `cut_score` 方向纠正：**以连词/引导字（`NO_TAIL`）收尾 −2.0 强惩罚**；**以连词（`CONJ_HEAD`）起首 +0.5**（从句边界优先，与本节引用的 BBC 一致）。旧实现是 `not in CONJ_HEAD` 才加分，方向正好相反 —— 用户实例「…店铺违规**而** / 被连带处理…」即由此产生。
- 新增 `CUT_COST = −1.0`（每刀固定代价）：治"过度切分"（同一句被切成一片 4 字卡同样是断句拉跨）。
- `NO_TAIL` 覆盖 `而但并且或及与则却故因若虽如由然所`；`REGRESSION` 新增 3 条连词用例。
- 用户实例实测修正为：`可能因为其中一家店铺违规 / 而被连带处理最终一同遭殃`。

**R3 粗剪废片段(#3/#11)**

- `detect_retake` 重写：**滑动窗口内任意两句**（句数 ≤6 或间隔 ≤30s）比对；一刀删掉**全部旧尝试**（`_chain_merge` 把连续重录刀串成一刀，不留几十毫秒碎片）；出点取「最后一次尝试起点 − 60ms」留自然起音并满足 `tailKeep`。
- 新增 `detect_retake_block`（整段重来：连续 ≥8 字逐字相同 → `false_start`）。
- 新增 `detect_dead_air`：给 `--media <源素材>` 时用 ffmpeg `silencedetect` 探音频能量（含纯函数 `parse_silencedetect`），否则退回字间 gap（≥1.2s）。
- 新增 `detect_self_negative`（`说错了/再来一遍…` → `off_topic`，**仅 review，不自动删**）。
- **guard 按 `reason` 分档**：`silence/breath/filler` 保持四项全过；`retake/false_start/stumble/repetition/off_topic/manual` 只硬要求「不切断字内音素 + 后留 ≥60ms」。**`wordClipped` 永不放松**。`guard` 新增 `okByReason`/`required`，`classify` 改用它，`cut_report.md` 分列硬过项/告警项，`rs_verify.check_cutlist` 同步。
- `rhetorical_suspect` 反向保护**只作用于 `silence`/`breath`**（重录/整段重来删的是一整段内容，不是修辞停顿）——此前它会把单刀/末刀一律降级为 review。
- 清理 `detect_filler` 死代码；`DETECTORS` 扩为 silence/dead_air/filler/repetition/retake/retake_block/self_negative；`rs_cut` 新增 `--media` / `--retake-ratio`。

**测试与夹具**

- 新增 `tests/test_v7.py`（19 项）：终点偏移/早退/滞留、锚点有界、估算标注、连词不落卡尾、跨句重录、多次旧尝试合并、guard 分档、段落重来、`silencedetect` 解析。
- 修正 `tests/test_v6.py::test_rs_sync_karaoke_ass_sync_ok` 夹具：该 ASS 终点相对其 wordline 末字多出 870ms（旧口径不校验终点才成立），把夹具的字级间隔调为 280ms 使其自洽；测试意图（override 标签剥离 → SYNC_OK）不变。

**文档**

- `rules/subtitles.md`：连词禁切 / 打分函数 / 释放余量边界 / 终点门禁。
- `rules/roughcut.md`：新检测器、guard 分档表、门禁。
- `rules/align.md`：`charTimingEstimated` 坑位。
- `docs/OPTIMIZATION-v7.md`：#1 方案 3 记录落地修订。

**R4 附带（部分 #7 鲁棒性）**

- 新增 `rs_common.ensure_utf8()`：stdout/stderr 切 UTF-8（`errors="replace"` 兜底）；已被重定向/被测试框架替换的流**静默跳过**，绝不因它抛异常。
- `rs_doctor --report` 的符号改为 GBK 安全（`√ / × / △` + `[OK] / [FAIL]`）—— 此前在 cp936 控制台会因 `✓` / `✅` 触发 `UnicodeEncodeError` 直接崩掉报告。

## v0.6.0 (2026-09-11) — seg 缓存 · retext 回灌 · 卡拉OK · JJAV2815 一条龙实测

P0 三件套(OPTIMIZATION-v6.md)+ 真实素材一条龙实测驱动的 11 项修复。测试 68 → **118 全绿**(test_v6.py 50 项)。

**R1 rs_render seg 级缓存(ADR-0013 落地)**

- 内容寻址段缓存:`seg_key = hash(clip 内容指纹 + chroma/bg + fps/画布 + rs_render 脚本哈希)`,落 `06_output/_build/<ratio>/segcache/`,保留最近 3 代(SEG_CACHE_KEEP=3)
- 上层步骤亦有 `step_keys.json` 门禁:只改字幕时 seg 8/8 命中、concat/compose/mix 全跳过,重出片 174s → **54s**
- `--explain` 逐段显示命中/重渲;`--clear-cache` / `--no-cache`

**R2 校对回灌 `rs_align retext`**

- `rs_align retext --take N --file proof.txt [--dry-run]`:SequenceMapper char 级 opcodes 把校对稿对回 wordline;equal 零漂移、replace 区间均分、insert 挤进 [prev.end, next.start];相似度 <0.5 拒绝
- 统计平铺在 `doc["retext"]`(editChars/similarity/inserted/deleted);重跑 resplit + remap 一条龙
- 实测:623→625 字、相似度 94%、句子 23→17,下游字幕/切点全量跟进

**R3 卡拉OK 逐字字幕**

- `rs_subtitle.py --karaoke`(需 pkg 后端字级时间戳,`--allow-degraded` 可降级):每字 ASS `\kf`,字间停顿计入前字;已唱 `&H0000E5FF` 暖黄(BGR)/未唱白
- 实测:625 字 77 卡全 `\kf`,无缺 startMs、无标点孤卡、无领头标点卡、无超 12 字形卡

**R4 JJAV2815 一条龙实测修复(286MB 真实素材,S0–S11 全链路)**

- **coverage 语义修正**:旧公式(字时长和÷末字时间)对真实字级时间戳恒判 ~75%,语义颠倒;改为**跨度覆盖率** =(首字起点→末字终点)÷转写声明区间,<0.99 软警告
- **段 0 视频膨胀**(4.26s→85.33s):ffmpeg git-master 回归,overlay filtergraph 且 `-ss` 0/缺省时输入 `-t` 按「帧数 = t × time_base_den」解释(tb=1/600 ×20);修复 = 段命令输出侧 `-t` 钳制 + 回归测试
- **绿幕人物半透明幽灵**:同构建 chromakey 输出 alpha 全坏(人物 α≈0);修复 = 全部改 **colorkey**;`-vf`+JPG 丢 alpha 会掩盖此 bug
- **字幕领头标点**:DP 候选边界去掉"标点前"并入禁止集(含半角);karaoke attach 标点跟随前字所在卡
- **rs_sync × 卡拉OK**:`parse_ass` 不剥 ASS override 标签(`{\kf28}店…`)→ 与 Wordline 84/84 unmatched → SYNC_FAIL;修复 = 解析时剥 `{...}` 后匹配
- **卡拉OK 字形预算**:挂字前移到必并/合规校验之前(以"显示字形"为唯一口径,`_clean_card` 剥掉的标点会在 `\kf` 层经 chars 带回,曾冒出 13-14 字卡);合并同步拼 `chars`
- **必并死区**:必并线 0.8s → MIN_DUR_S(0.83s)对齐,0.81s 卡不再"既不并也延不满";新增第二遍向下一卡吞并(起点取短卡);84 卡收紧到 77 卡,L0 验证全过
- resplit 孤立标点并入前句;`_PUNCT_ONLY` 显式跳过纯标点卡(`_clean_card("?")` 保留语气是设计,不能当 skip 判据)
- drawtext 中文必须单 face TTF(simhei),msyh*.ttc 多 face 集合丢字形;中文文案走 `textfile=`
- concat 相对路径双重拼接:`render()` 入口 base_dir 绝对化
- 实测战报全文见 `rules/compose.md` §实测战报

**R5 P2 清理**

- `rs_asr.py` 降级为 fun_asr.py 薄壳(标 deprecated);fun_asr `maybe_reexec` 剥 docstring 断言修复

## v0.5.0 (2026-09-10) — 自主 · 分级 · 省 Token · 可手改 · 闭环

针对用户 5 条新需求迭代。新增 ADR-0015~0017,阶段表扩为 **S0–S11**。

**R1 自带 FunASR,砍掉外在服务器(ADR-0015)**

- 新增 `tools/fun_asr.py`:一条命令转写,**不需要任何外部服务器**;后端自动选择 `pkg`(官方 funasr,有字级时间戳)→ `onnx`(轻量)→ `server`(仅兼容)
- **vendored** FuASR 裁剪版 ONNX 推理层到 `tools/asr_vendor/`(MIT,带 NOTICE 记录出处与升级方式)
- **实测关键结论**:本地 Paraformer ONNX 导出**只有 `logits` + `token_num`,没有 `timestamp`**(CIF 在导出阶段压掉了时间轴)→ onnx 后端拿不到字级时间戳,产出显式标 `degraded`
- **实测免费改善**:VAD 静音阈值 800→**400ms**,68s 口播从 5 段(中位 15.9s)变成 **27 段(中位 2.1s)**,接近句级粒度;整体约 **12× 实时**
- 模型**播种**而非下载:`fetch_deps.py asr --seed-models` 优先目录联接(零拷贝);`models/` 已 gitignore
- `rs_align.py` 改为调自带运行器;HTTP 降为兜底
- **pkg 后端实测通过(2026-09-11)**:字级时间戳 334 字 ↔ 334 条一一对应(标点零宽对齐进 `_align_ts_to_text`);
  同素材对比 onnx 降级路径 —— conf 0.95 vs 0.40、断句歧义卡 1 vs 9、违规 0 vs 1,
  「再加上一/点耐心」式断错消失;推理 RTF ≈0.08(11× 实时)。修复三处:`pick()` 不再把 ONNX 目录喂给 torch 引擎、
  `--pkg` 补装 torchaudio(fbank 必需)、probe 前置 fbank 检查

**R2 检查分级:首次全检,之后只跑代码自检(ADR-0016)**

- 新增 `rs_verify.py` + `_state/verify.json`:L0 机械自检(每次,秒级)/ L1 语义自检(首次 + 画面构图变更,Agent 看图)/ L2 人工验收(用户触发)
- **L1 只产出"待目测清单 + 抽帧命令",不自动判定** —— 判定权在 Agent/用户
- `missing`(从未跑过)**不算**画面变更;只有 `stale/failed` 才算
- **输出契约**:任何交付输出必须带 `verifyLevel` 与 `firstCheckDone`,缺失即视为未验证

**R3 省 Token:脚本优先 + 只读一个规则文件**

- SKILL.md 重写:新增「**谁来做**」表(判据:给定输入必得同一输出 → 脚本;需判断/创造/审美 → Agent)与「**读哪个文件(读完就停)**」路由表(覆盖全部 18 个 rules)
- 硬纪律:禁止一次读两个以上规则文件;SKILL.md 体量上限(有测试锁)

**R4 手动改阶段 + 一键重建(ADR-0016)**

- `rs_run.py --init`:在每个阶段文件夹生成 `rebuild.py` + 根目录 `REBUILD.md` 速查表
- 每个脚本固定四步:**备份 → 校验 → 级联 → 导出 + 自检**;校验不过**停住并指出具体行**
- `--force` 只作用于**起点阶段**,上游走缓存(这是"改字幕只要几秒"的前提)
- `--rollback` 还原备份(保留最近 5 次);`_state/backup/<ts>/`
- **新增 S8「烧录导出」阶段**:手改字幕走 S8(用现有 ass 重新烧录),**不会重新生成字幕把用户改动冲掉**

**R5 artboard 闭环(ADR-0017)**

- 新增 `rs_artboard.py` + `03_assets/artboard/manifest.json`(工程 ↔ 产物 ↔ IR 挂点 的唯一映射表)
- `--scan` / `--export`(按 `sourceHash` 只重导变了的)/ `--apply`(尺寸校验 + 时长变更自动平移下游 clip)
- **尺寸不符直接报错、不拉伸**;未挂进 IR 的卡片会被点名
- `03_assets/artboard/rebuild.py` 一条龙:导出 → 回填 → 从 S4 级联

**质量与验证**

- 测试 40 → **67 条全绿**(新增 `tests/test_v5.py`:后端能力声明、自带 ASR 不依赖 HTTP、VAD 阈值、验证分级与策略、备份/回滚、`--force` 语义、S8 不重新生成字幕、artboard hash/尺寸/时长传播、SKILL.md 路由覆盖与体量纪律)
- 真实素材端到端:68s 中文口播 → 自带 ASR(335 字/5.7s)→ Wordline → 40 张字幕卡,仅 1 张低于软性最短时长(合并会超字数上限,按设计降级为告警)

## v0.4.0 (2026-09-10) — 从「一次性出片」到「可增量 · 可对齐 · 一条龙」

针对用户提出的 5 条痛点(粗剪不合格 / 三对齐失效 / 各阶段不可编辑 / 断句不合理 / 不够一条龙)做架构级重构。按 `docs/OPTIMIZATION-v4.md` 落地,新增 ADR-0011~0014。

**P1 粗剪不合格 → 新增 S2 粗剪阶段(ADR-0012)**

- 旧管线**根本没有 cut 这一步**;现新增 `rules/roughcut.md` + `rs_cut.py`
- 三路检测器(静音 / 口头禅 / 口吃重复 / 重录)融合 → **CutList 决策表**(`reason` 9 项封闭枚举 + `conf` 三级 → `action` 三态)
- **guard 三重校验**(切点在静音区 / 不切断字内音素 / 后留 ≥60ms),不过即降级 review,绝不放宽
- **审查包**:每刀切点前后各 1.5s 音频 + 说明,「听 20 分钟整片」→「听 30 个 3 秒片段」
- 反向保护 `rhetorical_pause_suspect`;硬线:**宁可漏删,不可错删**

**P2 三对齐失效 → Wordline 字级对齐为唯一真相源(ADR-0011)**

- 根因是**三个独立时间源** + 「按字符数比例插值」;新增 `rules/align.md` + `rs_align.py`
- 产出 `05_ir/wordline.json`(双坐标:`final` 域 / `source` 域);导出唯一时间换算入口 `map_src_to_final()`
- 字幕卡时间 = 首字/末字时间戳聚合(**彻底废除比例插值**);首尾标点不计入时间跨度
- 新增 `rs_sync.py` 对齐断言:偏移中位数 ≤40ms、95 分位 ≤80ms,并给出可一键平移的系统偏差(合成素材实测 0ms / 3ms)
- 修订 `sense.md`:「校对只改文本不动时间戳」→「改文本后按**字级锚点重聚合**」

**P3 各阶段不可编辑 → 阶段清单与内容寻址缓存(ADR-0013)**

- 新增 `rules/incremental.md` + `rs_run.py`:缓存键 = 上游产物 hash + 参数快照 + **脚本文件 hash** + 外部服务版本
- `--status / --from / --only / --dirty / --explain / --mark`;`--explain` 能指出具体是哪个输入或脚本变了
- 改写 `compose.md` 的「中间件」一节:从**人肉判断复用**升级为**声明式缓存**
- 改一个字幕:20min+ → 秒级;换背景:只重渲受影响 seg

**P4 断句不合理 → 卡切分与行断开两层分离(ADR-0001 修订)**

- 新增 `segmentation.py`:**约束最优 DP** 卡切分(候选边界 / 禁切表 / 打分函数 / 硬约束)+ top-3 候选 + `ambiguous` 标记
- 禁切表:专名、成语/固定搭配、数量词+量词、数字+单位、货币+数字、`的得了着之`之后、**ASCII token 内**(含 `GPT-SoVITS` / `v2.1.0` 这类连字符 token)
- 硬约束:竖屏每卡 **16 → 12 字**、CPS ≤9 字/秒、单卡 0.83–7s、卡间距 ≥2 帧;`<0.8s 必并`、`>4s 必切`
- 行断开保留评分算法并补「金字塔形、禁顶行 1–2 字」
- 断句回归测试集 5 用例固化进 `tests/`

**P5 不够一条龙 → S0–S10 阶段注册表 + 三块空白补齐(ADR-0014)**

- 补齐 **S5 品牌**(`rules/branding.md` + `rs_brand.py`:Logo 变体矩阵,共享中间件,2 Logo × 2 比例 ≈ 1.3× 成本,默认避开字幕带)
- 补齐 **S6 音效**(`rules/sfx.md` + `rs_sfx.py`:转场/强调/列举/章节/结尾五类落点,密度 ≤2 个/15s,丢弃项留痕)
- 补齐 **S9 文案**(`rules/meta.md` + `rs_meta.py`:抖音/B站/视频号标题·简介·Tag 规范,**B站章节时间戳由 markers 自动生成**);封面正式纳入编排
- `rs_ir.py` 新增 `build --from-cutlist`:CutList → IR 主轨,**消灭「Agent 手写毫秒」这一整类误差**
- SKILL.md 重写为阶段注册表;新增反模式一节(5 条架构级 + 操作级)

**质量与验证**

- 测试 9 → **40 条**(新增 `tests/test_v4.py`:断句回归、禁切表、CPS/时长约束、guard 三重校验、重映射单调性、缓存键含脚本 hash、变体安全区、音效密度、文案截断与章节、对齐自检、文档防漂移)
- 完整链路冒烟通过:S1 对齐 → S2 粗剪 → apply → remap → S3 生成 IR → S7 字幕(DP) → S8 对齐自检 → S6 音效 → S9 文案

## v0.3.0 (2026-09-09) — 工程化与类型知识库迭代

12 条用户反馈全落地:

- **开工前提问环节**(SKILL 第 0.5 步):companion 模式逐项问,automation 模式按 genres 分册默认值自查;brief 增加类型与动画密度字段
- **工程归档规范**(ADR-0007):目录 `<YYYYMMDD>-<中文标题>-<类型>`;产物中文化命名;`rs_cleanup.py` 完工清理(dry-run 默认);旧工程已迁移
- **Config 图形编辑器**:tools/config_gui.py(tkinter 零依赖)+ PyInstaller onefile exe(CutFlowConfigEditor.exe,12MB);分组表单/中文说明/路径浏览/frozen 路径
- **小白教程**:README 重写(新手五步 + Config 参数全表:作用/填什么/去哪获取)
- **动画安全区**(ADR-0009):视频卡内容带垂直 250–1290px,底部 576px 字幕带留白;四卡全部重做;背景升级 6s 无缝循环动画(clip.loop 支持);纯动画视频场景全覆盖
- **类型剪辑知识库**(ADR-0010):rules/genres/ 六册(口播/动画教程/新闻采访/短剧/影视解说/通用),全部带权威来源(广电总局/BBC/NBCU/MD3/AES/YouTube 官方)
- **依赖发行为**(ADR-0008):Release v0.3-dependencies 提供 ocr-module.zip(113MB)/vqa-module.zip(529MB,相对路径修复版);tools/fetch_deps.py 一键部署;tools/fetch_ffmpeg.py 多镜像一键部署
- **感知备选策略**:Agent 视觉优先,本地 OCR/VQA 兜底(config.sense.force_local);rs_sense 支持 vqa_exe 直连模式
- **自动封面**:rules/cover.md 固化抽帧+抠像+合成三路线,产物为标准交付物


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

### iter-04 (2026-09-08)

- fix: 断行评分统一收敛到 textopt.card_split(BAD_END 含数字防"一/个"切断、虚词收尾加分、候选范围 ≤max_chars);
- fix: Jimi 首句"电子运营"→"电商运营"(TTS 音频+manifest 时间轴+整轨重拼接+字幕同步);
- 验收: judge 终验两片 pass,遗留项(悬"一"字)已全分辨率帧闭环。
