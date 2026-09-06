# CutFlow — AI 视频制作技能库

**素材进,成片出。** 一个让 AI Agent 端到端制作视频的 Agent Skill 库:接收口播视频 / 纯文案 / 剧本分镜 / 图片,经「感知 → 合成 → 剪辑 → 自评」流水线直接产出成片,并同时交付**可在剪映 5.9 继续精修的草稿工程**。

- 语言:中文驱动,脚本零第三方 Python 依赖(纯标准库 + FFmpeg)
- 比例:9:16(1080×1920)/ 16:9(1920×1080),支持双出
- 视频:口播 / 教程 / 动画信息流;AI 生视频环节只产提示词,不调任何生成 API

## 两个技能

| 技能 | 触发场景 |
|------|----------|
| `cutflow` | 做视频、剪视频、口播视频、教程视频、加字幕、绿幕抠像、配音、出剪映草稿 |
| `cutflow-prompt` | 只要 AI 生视频提示词:首帧图提示词 + 首帧标注式 5–10s 图生视频提示词(Seedance 2.0 方法论) |

## 架构

```
素材(口播/文案/分镜/图片)
  → 感知:FunASR 转写(句级时间戳)+ OCR + VQA → Agent 校对
  → 合成:GPT-SoVITS 配音(音色卡驱动) / artboard 片头封面 / AI 提示词
  → 剪辑:毫秒级 IR(project.json)──┬─→ rs_render(FFmpeg 七步管线直出成片)
                                      └─→ rs_jy_draft(剪映 5.9 明文草稿)
  → 自评:ffprobe 断言 + 抽帧网格目测,≤3 轮
  → 交付:final_*.mp4 + master.srt + 封面 + 可编辑剪映工程
```

**渲染铁律**(固化在 rs_render):统一帧率 → 逐段提取(8ms afade 防爆音)→ 无损拼接 → overlay 合成 → 混音(人声归一 + BGM 自动闪避 + 总线 -14 LUFS)→ 字幕最后叠 → 编码。

## 安装

```powershell
# 1. 准备:FFmpeg、剪映专业版 5.9(草稿明文,可自动化;6.0+ 已加密不可直写)、
#    FunASR 服务(MomentShift)、GPT-SoVITS 引擎、OCR/VQA 可执行文件
# 2. 复制 config.example.json → config.json,填入本机路径
# 3. 安装技能(用户级 junction)——或手动:
#    mklink /J "%USERPROFILE%\.agents\skills\cutflow" "<repo>\skills\cutflow"
#    mklink /J "%USERPROFILE%\.agents\skills\cutflow-prompt" "<repo>\skills\cutflow-prompt"
# 4. 体检
python skills/cutflow/scripts/rs_doctor.py
```

详见 `tools/install.ps1` 与 `docs/PLAN.md`。

## 使用示例(一条真实链路)

```bash
# 0. 体检(10 项依赖检查)
python skills/cutflow/scripts/rs_doctor.py

# 1. 感知:口播视频转写(自动抽 16k wav → FunASR → 句级时间戳)
python skills/cutflow/scripts/rs_asr.py 01_materials/talk.mp4 --out 02_sensed
#    …Agent 校对转写稿 → 02_sensed/transcript_corrected.md(硬规则,不可跳过)

# 2. 合成:纯文案用 koubo-test 音色配音(逐句落盘、断点续传)
python skills/cutflow/scripts/rs_tts.py --script 00_brief/copy.txt --out 03_assets/tts

# 3. 字幕(智能断行 + 风格模板 + 安全区)
python skills/cutflow/scripts/rs_subtitle.py --from-tts 03_assets/tts/manifest.json \
    --style talkshow-bold --ratio 9x16 --out 06_output

# 4. 剪辑:写 IR(毫秒级时间线)→ 校验 → FFmpeg 直出(可 9:16/16:9 双出)
python skills/cutflow/scripts/rs_ir.py validate 05_ir/project.json
python skills/cutflow/scripts/rs_render.py 05_ir/project.json --ratio 9x16 --profile final

# 5. 剪映 5.9 草稿(同一份 IR → 可编辑工程,注册进首页)
python skills/cutflow/scripts/rs_jy_draft.py 05_ir/project.json --name 我的草稿 --open

# 6. 自评:抽帧网格 → Agent 目测(黑帧/压脸/安全区/跳变),≤3 轮修复
python skills/cutflow/scripts/rs_bench.py 06_output/final_slug_916.mp4 \
    --ir 05_ir/project.json --out 06_output/bench.png
```

## 目录

```
skills/cutflow/            总控技能:SKILL.md 路由 + rules/ 八篇域规则 + scripts/ 原语
  scripts/                 rs_doctor / rs_asr / rs_sense / rs_frames / rs_tts /
                           rs_subtitle / rs_ir / rs_render / rs_jy_draft / rs_bench
  templates/               brief 契约 / IR schema / 风格 YAML / 5.9 空草稿模板(脱敏)
  references/              5.9 草稿 schema 备忘 / FFmpeg 配方簿 / 剪映 GUI 锚点
skills/cutflow-prompt/     AI 生视频提示词技能(Seedance 方法论)
tools/install.ps1          安装脚本
docs/PLAN.md               完整方案笔记(架构/里程碑/风险)
```

## 致谢

- [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft)(MIT,经 jianying-editor-skill vendored)——剪映 5.9 草稿生成
- [kbcut](https://github.com/starboom/kbcut) / [video-use](https://github.com/browser-use/video-use) / [lingji-cut](https://github.com/yoqu/lingji-cut) / [Generative-Media-Skills](https://github.com/SamurAIGPT/Generative-Media-Skills) / [hyperframes](https://github.com/heygen-com/hyperframes) / [make-prompt-seedance2](https://github.com/liangdabiao/make-prompt-seedance2) ——方法论参考

## License

MIT(含 vendor 的第三方组件见各自 LICENSE)
