# Jianying — 剪映 5.9 双通道

## 边界(铁律)

- 只操作 **5.9**(E:\Jianying\5.9JianyingPro);**11.3 草稿加密,永不读写其草稿**。
- 写草稿前剪映必须未运行(rs_jy_draft 内置检测);5.9 禁自动更新。

## 通道一:草稿直写(主)

`rs_jy_draft.py 05_ir/project.json --name <草稿名> [--subtitles 03_assets/tts/manifest.json] [--open]`

- 落位 `<draft_root>/<名>/draft_content.json` + `draft_meta_info.json`,并注册 root_meta_info.json(首页可见)。
- 能力映射:视频/音频/文本字幕/位置缩放;**chroma 绿幕无 5.9 对应→警告跳过**(该类项目交付以 FFmpeg 成片为准);
- 交付时告诉用户:在剪映首页找到同名草稿即可继续精修。

## 通道二:GUI 自动导出(辅,用 computer-use)

流程:启动 5.9 → 关弹窗(推送通知=暂不)→ 首页双击目标草稿 → 等编辑器就绪 → 右上"导出" →
确认分辨率/帧率 → 点导出 → 等待完成(默认 20min 超时)→ 产物在导出目录,移入 06_output/。
锚点与状态机见 references/jianying-gui-anchors.md;AX 树匿名,以截图坐标为主,每步截图留证写 project.md。
失败降级:草稿已交付,请用户手动导出。

## 已知坑

- 首启弹"开启推送通知"→点"暂不";未登录不影响本地剪辑与导出。
- 草稿根是用户自定义的(E:\Jianying\JianyingPro Drafts),root_meta_info.json 在 %LocalAppData% 的 com.lveditor.draft 下。
