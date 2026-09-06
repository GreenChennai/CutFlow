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
