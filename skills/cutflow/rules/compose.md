# Compose — IR 与渲染

## IR 书写(templates/project.schema.json)

- 毫秒单位;canvas 1080x1920 或 1920x1080;fps 30 默认。
- tracks[0] 必须是主视频轨(顺序拼接);后续 video 轨是画中画/信息卡(支持 png/jpg 静图 + chroma 绿幕)。
- 音频轨 clip 用 role 区分 voice/sfx;bgm 顶层字段(ducking 自动闪避)。
- motion 枚举:fadeIn/fadeOut/slideInLeft/slideInRight/zoomIn(基轨 zoom 退化为 fadeIn 已警告)。
- transition 挂前一片段(fade/wipeleft/wipeup/slideleft/circleopen)。
- reframe.anchorY:比例转换时人物锚点(0=贴顶)。

## 流程

1. `rs_ir.py validate` 全绿才渲染;
2. `rs_render.py <ir> --ratio 9x16 --profile final`(迭代期用 --profile preview 提速);
3. 双出:对 outputs 里每个比例各渲一次(第二比例记得带 reframe)。

## 中间件

06_output/_build/<ratio>/ 下 seg_*/base/composed/mixed/subtitled 可复用;改了字幕只重跑 step6-7(重调 rs_render 会全跑,手改时可复用 mixed.mkv)。

## 渲染后自检

rs_doctor 语义校验 + `rs_bench.py <成片> --ir <ir> --out 06_output/bench_<ratio>.png` → 目测:
黑帧/绿幕残留/字幕压脸或出安全区/跳变/信息卡错位。修复 ≤3 轮,仍败上报。
