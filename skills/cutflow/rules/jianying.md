# Jianying — 剪映 5.9 双通道

## 边界(铁律)

- 只操作 **5.9**(E:\Jianying\5.9JianyingPro);**11.3 草稿加密,永不读写其草稿**。
- 写草稿前剪映必须未运行(rs_jy_draft 内置检测);5.9 禁自动更新。

## 通道一:草稿直写(主)

`rs_jy_draft.py 05_ir/project.json --name <草稿名> [--subtitles 03_assets/tts/manifest.json] [--open]` / 预检:`rs_jy_draft.py 05_ir/project.json --dry-run`

- **编译层(阶段六)**:先 IR → 草稿计划(帧对齐中间表示,帧/微秒双记法)→ 门禁 → 才写盘;门禁含轨道数/片段时长和/主轨首段从 0 且不重叠/无黑场间隙/帧对齐断言,任一失败 `PLAN_GATE_FAIL` 拒写(退出码 4)。
- `--dry-run` 只打印「IR 片段 → 草稿片段」映射表(人读),不写任何文件、不查剪映进程;排查"为什么开不了草稿"先跑它。
- 落位 `<draft_root>/<名>/draft_content.json` + `draft_meta_info.json`,并注册 root_meta_info.json(首页可见);写后自动回读对账(`DRAFT_GATE_FAIL` = 落盘与计划不符)。
- 能力映射:视频/音频/文本字幕/位置缩放/变速/音量/转场/淡入淡出/音效 gainDb/BGM;主轨 V1 + 画中画 V2+ + 音频 A1(+BGM A2)+ 字幕 T1(≤120 条)。视觉动效关键帧(motion/reframe)与 ducking 不写,计划留 warning(见诚实验收「拒绝」区)。
- 交付时告诉用户:在剪映首页找到同名草稿即可继续精修。
- **诚实验收说明**(已验证/未验证/拒绝三类,含"未在真机剪映打开验证"的边界声明):`rules/jianying-verification.md`。

## 通道二:GUI 自动导出(辅,用 computer-use)

流程:启动 5.9 → 关弹窗(推送通知=暂不)→ 首页双击目标草稿 → 等编辑器就绪 → 右上"导出" →
确认分辨率/帧率 → 点导出 → 等待完成(默认 20min 超时)→ 产物在导出目录,移入 06_output/。
锚点与状态机见 references/jianying-gui-anchors.md;AX 树匿名,以截图坐标为主,每步截图留证写 project.md。
失败降级:草稿已交付,请用户手动导出。

## 已知坑

- 首启弹"开启推送通知"→点"暂不";未登录不影响本地剪辑与导出。
- 草稿根是用户自定义的(E:\Jianying\JianyingPro Drafts),root_meta_info.json 在 %LocalAppData% 的 com.lveditor.draft 下。
