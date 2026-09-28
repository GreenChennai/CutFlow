# 迭代方案 v0.9

## T1 绿幕
实验:blend 0.05 太硬→锯齿;阴影 sim0.15 键不掉、0.24 键透头发(hair_v2)→组合:
1) 默认 sim.15/blend.12 2) alpha 羽化 gblur(chroma.edgeBlur) 3) 新增 chroma.killRects
(geq 强制透明)去阴影;4) validate 新字段;5) 新增 tests(test_v10)。

## T2 衔接闪烁(根因已锁定)
seg 前景流(-ss 寻址)首帧比背景环晚一拍 → overlay 首帧只显背景 → 每个 sourceIn>0
的段第一帧没人 → concat 即闪烁(v6_head 实测复现)。
修:step_segment fg 链首加 setpts=PTS-STARTPTS;step_compose overlay 同查;
验证:v7 首帧已有人物;再整片重渲 + 抽帧核验全部 7 个衔接点。

## T3 内置 FunASR(自举,全程离线)
现状:pkg 后端本就本地 venv+本地模型,但"未启动"时 Agent 只会报错不会自愈。
修:fun_asr.py 增加 --ensure 与**转写前自动自举**(venv/模型缺失→自动跑
fetch_deps asr --seed-models→re-exec,进度落 stderr);rs_doctor 报修复指引;
rules/asr.md 写死:ASR 未就绪=自动部署,禁止让用户手装;仅离线路径,不用在线 API。

## T4 Logo(实测 rs_brand 现为坏的)
variant_ir 写 clip.overlay{} 但 rs_render 不消费(step_compose 只认 scale/position)
→ 现 Logo 会贴满画布,潜伏 bug。修:1) analyze 子命令(ffprobe 真实宽高+alpha+宽高比);
2) logo_rect 按真实宽高比(旧版假定方形);3) anchor 扩 6 位;4) 安全区按 ratio 查表;
5) variant_ir 改产 scale/position/opacity;6) branding.md/schema/tests 同步。

## 实施顺序与验收
1. T2→T1 改 rs_render+schema,整片重渲店群工程,抽 7 衔接点+边缘帧目测;
2. T3 自举验证;3. T4 analyze+变体渲染;4. tests/test_v10;5. ADR-0022+CHANGELOG+清 tmp。
