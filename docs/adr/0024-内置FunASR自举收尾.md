# ADR-0024:内置 FunASR 自举收尾(杀 HTTP server 时代残留)

状态:已采纳 ｜ 日期:2026-09-14(v0.10,用户反馈#3)

## 背景

用户反复遇到"ASR 经常没有启动"。排查发现真凶不是环境:`tools/fun_asr.py` 的 `ensure_backend()` 调用了**从未定义的 `any_backend_ready()`**,每次转写必然 `NameError` 崩溃,上游只能报"ASR 无输出/未启动"——症状被误读为服务问题。同时两处 server 时代残留持续误导:`rs_doctor.py` 仍在探测已废弃的 `config.asr.url` HTTP 端点(永远"不可达",诊断必红);`rules/asr.md` 未写死处置话术,Agent 偶发让用户"手装依赖/启动服务"。

## 决策

1. **补定义 `any_backend_ready()`**:遍历 `BACKENDS`,任一后端就绪即 True;删除其后一段死代码。`ensure_backend()` 逻辑恢复设计意图:无就绪后端 → 自动部署。
2. **ASR 就绪语义 = 自动部署,永不停在"让用户装"**:`fun_asr.py --ensure` 自动补齐依赖与权重(pkg 后端首次从 ModelScope 拉 ~1GB torch 权重,落 `~/.cache/modelscope`,一次性);`fun_asr.py --probe` 轻量探测就绪状态。禁止任何"启动 ASR 服务"话术——server 模式已废弃,当前是进程内/子进程直跑。
3. **诊断对齐真实架构**:`rs_doctor.py` 删 HTTP `_check`,改 `probe_asr_local()`(子进程跑 `--probe`);不可就绪时 hint 指向 `--ensure`,不出现任何手装指引。
4. **规则固化**:`rules/asr.md` 写死"未就绪 = 自动部署,禁止让用户手装/启动服务",并记录 pkg 后端首次下载行为(让 Agent 能向用户解释首次转写为何慢)。

## 后果

- `rs_align build --media` 一条命令出字级 wordline,零手工部署步骤(验收线#3);
- 首次使用有一次性 ~1GB 下载成本;预暖已完成,本机后续转写零下载;
- "ASR 未启动"类问题从环境问题降格为不存在的问题——诊断不再报假红;
- 若未来 ModelScope 权重路径变更,`--probe` 会显式失败并提示 `--ensure`,不会静默。

## 关联

- `tools/fun_asr.py`:`any_backend_ready()`、`ensure_backend()`、`--probe`/`--ensure`
- `rs_doctor.py`:`probe_asr_local()`(取代 HTTP `_check`)
- `rules/asr.md`、`SKILL.md` 硬规则 22
- 取代 ADR-0015 的 server 运行器路线(其"自带运行器"目标不变,实现收敛为子进程直跑)
