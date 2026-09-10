# Compose — IR 与渲染

## IR 书写(templates/project.schema.json)

- 毫秒单位;canvas 1080x1920 或 1920x1080;fps 30 默认。
- tracks[0] 必须是主视频轨(顺序拼接);后续 video 轨是画中画/信息卡(支持 png/jpg 静图 + chroma 绿幕)。
- 音频轨 clip 用 role 区分 voice/sfx;bgm 顶层字段(ducking 自动闪避)。
- motion 枚举:fadeIn/fadeOut/slideInLeft/slideInRight/zoomIn(基轨 zoom 退化为 fadeIn 已警告)。
- transition 挂前一片段(fade/wipeleft/wipeup/slideleft/circleopen)。
- reframe.anchorY:比例转换时人物锚点(0=贴顶)。
- **`subtitle.source` 应指向 `05_ir/wordline.json`**(不再是句级 transcript);旧工程可指 manifest.json。
- **显式 Gap**:粗剪后 keep 区间之间的空隙用 `{"kind":"gap","durationMs":N}` 表达,不再靠"没有 clip"隐式表示(语义对齐 OTIO 的 Gaps / Filler)。

## IR 不再手写(关键改动)

主视频/音频轨由 **CutList 自动生成**,消除「Agent 手写毫秒」这一整类误差:

```
04_cut/cutlist.json ──► rs_ir.py build --from-cutlist ──► 05_ir/project.json(主轨)
                                                              │
Agent 只需补:overlay 卡片 / 字幕 / 音效 / Logo(全部从 Wordline 取时)
```

```powershell
python skills/cutflow/scripts/rs_ir.py build --from-cutlist 04_cut/cutlist.json `
    --slug 20260910-demo-口播 --ratio 9x16 --out 05_ir/project.json
```

- keep 区间 → `clips[]`,时间由 `map_src_to_final()` 换算(`sourceInMs` 仍指源素材位置);
- 切点处自动带 `transition` 或 8ms afade(由 `--xfade ms` 控制,默认 8)。

## 流程

1. `rs_ir.py validate` 全绿才渲染;
2. `rs_render.py <ir> --ratio 9x16 --profile final`(迭代期用 `--profile preview` 提速);
3. 双出:对 outputs 里每个比例各渲一次(第二比例记得带 reframe);
4. 多变体:见 rules/branding.md,`rs_brand.py` 在最后一步分叉,**共享中间件**。

## 中间件与增量(改写旧「人肉复用」)

**旧写法**:`06_output/_build/<ratio>/` 下 `seg_*/base/composed/mixed/subtitled` 可复用;改了字幕只重跑 step6-7(重调 rs_render 会全跑,**手改时可复用 mixed.mkv**)。

**新写法(ADR-0013)**:产物复用**由声明式缓存决定,不由人肉记忆决定**。

| 目录 | 对应阶段 | 增量粒度 |
|---|---|---|
| `_build/<ratio>/seg_*/base/` | S3 基础合成 | segment |
| `_build/<ratio>/seg_*/composed/` | S4 动画信息 | 卡片 |
| `_build/<ratio>/branded/<variantId>/` | S5 品牌 | 变体 |
| `_build/<ratio>/mixed/` | S6 音效 | 单条音效 |
| `_build/<ratio>/subtitled/` | S7 字幕 | 单卡 |

**操作前先问缓存,不要凭记忆**:

```powershell
python skills/cutflow/scripts/rs_run.py --status      # 哪几步 stale
python skills/cutflow/scripts/rs_run.py --only S7     # 只重做字幕(秒级)
python skills/cutflow/scripts/rs_run.py --explain S3  # 为什么 stale
```

**反模式**:直接重调 `rs_render.py` 会按当前 hash 全量重算——**想省时间就先 `--status` 再 `--from/--only`**。细节见 rules/incremental.md。

## 渲染后自检

`rs_doctor` 语义校验 + **`rs_sync.py` 三对齐断言** + `rs_bench.py <成片> --ir <ir> --out 06_output/bench_<ratio>.png` → 目测:

黑帧 / 绿幕残留 / 字幕压脸或出安全区 / 跳变 / 信息卡错位 / Logo 压字幕。修复 ≤3 轮,仍败上报。

## 实测战报(v0.6.0 · JJAV2815 一条龙)

| 问题 | 根因 | 对策(已固化) |
|---|---|---|
| drawtext 信息卡丢字形 | `msyh*.ttc` 是 TTC 多 face 集合,freetype 部分汉字取错 face(「群/两/攻/险」残缺、①②变豆腐) | 文字渲染一律**单 face TTF**(simhei.ttf / Deng.ttf);中文文案走 `textfile=` 读 UTF-8 文件,避开命令行 GBK 乱码 |
| 渲染中途被环境 hook 杀进程 | auto 色度采样的 `.pam` 临时文件 unlink 触发批量删除监控 | 绿幕 `chroma.color` **显式写十六进制**,不走 auto;确需 auto 时采样一次后把结果固化进 IR |
| concat 段路径双重拼接 | IR 相对路径 → concat.txt 写相对段路径 → ffmpeg 以 list 文件所在目录为基准再拼一次 | 已修:`render()` 入口 `base_dir = project_path.parent.parent.resolve()` 全程绝对化 |
| **段 0 视频膨胀**(4.26s→85.33s) | ffmpeg git-master(2026-07-30 gyan)回归:filter_complex 含 overlay 且视频输入 `-ss` 为 0/缺省时,输入 `-t` 被按「帧数 = t × time_base_den」解释——tb=1/600 手机 HEVC ×20、合成源 tb=1/15360 ×512;`-ss>0` 或单输入 `-vf` 不触发 | 已修:段命令加**输出侧 `-t {take_s}`** 在编码器层钳制(与怪癖解耦)+ 回归测试 `test_seg_cmd_has_output_t_clamp`。诊断法:ffprobe 段文件 nb_frames ÷ 预期帧数 = tb_den/fps 即此 bug |
| **绿幕人物变幽灵**(半透明叠底) | 同构建第二处回归:**chromakey 输出的 alpha 全坏**——人物区域 α≈0(alphaextract 实测 YAVG 2.07/255,正常应 255);`-vf` 单输入 + JPG 导出因"丢弃 alpha"完全掩盖,只有 overlay 合成才现形 | 已修:基轨与 compose 全部 **chromakey → colorkey**(RGB 距离键控,alpha 正常:人物 255/绿幕 0)。诊断法:`format=rgba,alphaextract` + signalstats 看 YAVG;参数扫描在 `-vf` 下无效是因为根本看不到 alpha |
| 字幕「?关于…」式领头标点 | 双因:①DP 候选边界误把"标点前"当候选;②retext 给插入标点分了 gap 中段时间([4.41,4.59]),attach 按时间中心 bisect 把尾标点划进下一卡窗口 | 已修:segmentation 禁止"标点前"切(候选只留"标点后",含半角 `,.;?!`);attach 改为**标点跟随前字所在卡**;回归测试 `test_segmentation_never_cuts_before_punct` / `test_karaoke_attach_punct_follows_prev_card` |
| 顶部残留源片白墙 | 绿幕上方墙面不在键控范围内,cover 裁切后仍入画 | IR 侧调 `chroma.cropTopPct`(0.09→0.13 实测去净且不切头);这是**构图参数**,不是键控问题 |
| **rs_sync 对卡拉OK ASS 全量 unmatched** | `parse_ass` 直接取 Dialogue 第 10 列原文,卡拉OK 行文是 `{\kf28}店{\kf14}群…`,不剥 override 标签 → 与 Wordline 纯文本 84/84 对不上 → SYNC_FAIL | 已修:`parse_ass` 剥 `{...}` 标签后再匹配;回归 `test_rs_sync_parse_ass_strips_karaoke_tags` / `test_rs_sync_karaoke_ass_sync_ok`。诊断线索:unmatched 列表里 event 文本裸带 `\kf` 即是此症 |
| **卡拉OK 字卡 13-14 字超预算** | `_clean_card` 剥掉的标点在 `\kf` 显示层经 chars 原样带回;必并/合规校验只数清洗文本,漏了这笔字形预算 | 已修:挂字**前移到必并/校验之前**(`events_from_wordline(karaoke=True)` 内置),`e["text"]` 刷新为 chars 拼接,预算与 rs_verify 同口径;合并必须同步拼 `chars`(否则 `_kar_text` 丢字);回归 `test_karaoke_display_glyph_budget` |
| **0.81s 卡既不并也延不满** | 必并线 0.8s 与 DUR_MIN 0.83s 之间有 0.03s 死区;延长被下一卡 2 帧间隙收回(`_enforce_gaps`),L0 硬失败 | 已修:必并线 = MIN_DUR_S(0.83);新增第二遍「向下一卡吞并」(起点取短卡,说出时间不动);回归 `test_merge_short_aligns_min_dur_line` / `test_merge_short_second_pass_absorbs_next` |

环境教训(跨项目通用):Bash 每条命令 cwd 可能漂移,长命令显式 `cd`;`/tmp` 等 POSIX 路径对 Windows 版 ffmpeg.exe 无效,输出一律写盘符路径;同一文件严禁并行 Edit(竞态覆盖)。
