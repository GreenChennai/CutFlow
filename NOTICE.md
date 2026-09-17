# NOTICE

> 本文件自 CutFlow v0.15 起生效。CutFlow v0.1.0–v0.12 等已发布 tag 的 MIT 授权
> 继续有效、不可撤回;许可变更仅对 v0.15 及之后的新版本生效。

本文件列出 CutForge 所依赖、借鉴或派生的全部第三方作品及其许可。
本文件必须随所有分发副本一并提供，不得删除或改写其中内容。

## 一、派生来源（MIT 许可）

### OpenCut（重写版）
- 仓库：https://github.com/OpenCut-app/OpenCut
- 许可：MIT License
- 版权：OpenCut
- 使用方式：CutForge 的**目录结构、工具链 pin（proto / moon / bun / rust）、
  插件优先架构方向、桌面壳技术栈（GPUI）与公开能力规划**参考了该项目。
  其 MIT 许可全文见 `LICENSE-OPENCUT.MIT`。

### OpenCut Classic（经典版）
- 仓库：https://github.com/OpenCut-app/opencut-classic
- 许可：MIT License
- 版权：OpenCut
- 状态：上游已于 2026-05-17 归档（archived）。
- 使用方式：CutForge 的**领域模型与交互设计**（时间线、轨道、元素、
  命令与撤销、选择、吸附、变速、波纹编辑、工程持久化等概念划分）
  参考了该项目的设计与文档。**CutForge 为独立实现的 Rust 代码，
  未复制该项目的源代码文件。** 其 MIT 许可全文见 `LICENSE-OPENCUT.MIT`。

> 声明：**CutForge 不是 OpenCut 的官方版本，与 OpenCut 项目及其维护者
> 不存在隶属、赞助或背书关系。** CutForge 是独立项目。

### 关于许可不可撤回的说明
CutFlow 在其历史版本（含 `v0.1.0` 至 `v0.12` 等已发布 tag）中
以 MIT 许可发布。**已发布版本上他人已获得的 MIT 授权继续有效，
不得也不会被撤回。** 本项目的许可变更仅对其后发布的新版本生效。

### 未修改上游许可的声明
`LICENSE-OPENCUT.MIT` 为从上游 `OpenCut-app/OpenCut` 仓库 LICENSE 文件
**逐字复制**的原文（1,060 字节），未作任何修改。依据 MIT 许可与本文件
第一节，任何分发副本必须同时保留上游的版权声明与许可全文。

## 二、渲染与媒体
- FFmpeg / ffprobe —— 依据其采用的 LGPL/GPL 许可使用（作为外部可执行程序调用，
  未链接入本项目二进制）。用户需自行遵守所安装构建的相应许可。
- Ghostscript —— GNU Affero GPL v3（**仅在用户本机可选使用，不随本项目分发**）
- poppler —— GPL v2（cMap 数据另受 Adobe 许可；部分 Makefile 为 MIT）
  （**仅在用户本机可选使用，不随本项目分发**）
- WPI（WPI-noGUI-cli.exe）—— **上游未提供许可声明，本项目不分发该程序**，
  仅由用户自行在本地指定路径使用。

## 三、语音与感知（CutFlow 侧，不随 CutForge 分发）
- FunASR —— MIT License（模型与推理层来源，见 `tools/asr_vendor/NOTICE.md`）
- GPT-SoVITS —— 见其上游许可（作为外部 HTTP 服务调用）

## 四、剪映相关
- pyJianYingDraft —— MIT License，Copyright (c) 2026 luoluo luo22
  （CutFlow 侧 vendored 于 `scripts/vendor/pyJianYingDraft/`）
- 剪映（JianyingPro）为第三方商业软件，本项目仅生成其明文草稿文件（5.9），
  不包含、不修改、不分发其任何程序或资源。

## 五、规范与数据来源
- EBU R128（响度）/ EBU R37（音画同步）—— 欧洲广播联盟技术规范
- Netflix Simplified Chinese Timed Text Style Guide —— 字幕规范参考
- BBC Subtitle Guidelines —— 字幕规范参考
- auto-editor —— 公共领域（margin / smooth 防碎切语义参考）
- Mixkit —— 免费音效许可（音效素材来源）
- 其余致谢完整列表见 CutFlow 的 `README.md` "致谢" 章节

## 六、本项目许可
本项目原创部分适用 **Artboard Reciprocal License v1.0（ARL-1.0）**，
全文见 `LICENSE`。核心文件的修改须按该协议第 3 条回传开源；
插件、壳与商业应用可按你自己的条款分发。

## 七、名称主张
`CutForge` 名称由本项目权利人 GreenChennai 首先用于本软件作品。
本声明不构成商标注册；本项目暂不进行正式商标注册。
