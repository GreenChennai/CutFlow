# 懒加载依赖体系规范（ADR-0049 / 方案 §5.7）

> 一句话：**初始零环境，用到才下载，缺失必降级留痕。**
> 能力缺失**永不静默**；基础档（FFmpeg + 纯标准库）永远可出片。

## 1. 能力 → 组件 → 降级 映射表（唯一真相源：`skills/cutflow/scripts/rs_fetchable.py` 的 `CAPABILITY_DEPS`）

| 能力 id | 组件 | 约体积 | 后端 | 降级档 | 用途 |
|---|---|---|---|---|---|
| `audio.beat` | beatnet | ~320MB | venv-dsp | `onset-energy`（能量起音，精度下降） | 卡点 |
| `audio.downbeat` | madmom | ~210MB | venv-dsp | `beat-only`（仅节拍无下拍） | 卡点进阶（下拍对齐） |
| `audio.stem` | demucs | ~350MB | venv-dsp | `none`（不可降级，缺即不启用） | 人声/伴奏分离，BGM 独立控制 |
| `vision.shot` | scenedetect | ~45MB | py | `frame-diff`（帧差，切点精度下降） | 混剪/多镜头叙事 |
| `vision.matting` | rvm | ~480MB | venv-torch | `none(gate)`（不可降级且须过质量门禁，ADR-0050） | 人物分离/背景可控 |
| `vision.track` | bytetrack | ~20MB | py | `static-center`（跟随变固定中心） | 跟拍/贴纸跟随 |
| `vision.cv` | opencv | ~60MB | py | `none`（不可降级） | 帧差/黑场/剪贴检测 |
| `text.clip` | clip | ~600MB | venv-torch | `keyword-match`（关键词匹配，语义精度下降） | 语义选卡/素材匹配 |

- 组件三态随时可查：`rs_fetchable.py state --json`。
- 后端 `py` = 主解释器（轻依赖）；`venv-dsp` / `venv-torch` = 独立虚拟环境（重依赖，
  绝不装进主解释器），沿用 `tools/.venv-asr` 既有模式。

## 2. 三态协议（任何消费能力的操作先查三态）

| 态 | 触发 | 行为 | 留痕字段 |
|---|---|---|---|
| `READY` | probe 通过 | 正常执行 | — |
| `MISSING`（允许下载） | probe 失败 + 允许 | 现场下载（进度可见，可中断续传 `.part` 续传） | `fetchTriggered: true` |
| `MISSING`（不允许） | probe 失败 + `--no-fetch`／离线／`--auto` 无 `--allow-fetch`／用户拒绝 | **降级**到映射表声明的降级档 | `degraded: true` / `degradeReason` / `missingComponent` |
| `FAILED` | 下载或 probe 失败 | **降级** + 保留日志路径（`tools/deps/<module>/.failed.json`） | 同上 + `fetchLog` |

- 消费方统一入口：`rs_fetchable.ensure_ready(component_id, auto=…, allow_fetch=…, no_fetch=…)`
  （对 M4/rs_edit 的公开 API，返回 `state` + `action` + `record`）。
- 降级留痕标准块由 `rs_fetchable.degrade_record(component_id)` 产出，
  消费方**原样写进产物 JSON**：`{"degraded": true, "degradeReason": …, "missingComponent": …}`。
  旧消费者忽略未知字段，向后兼容（ADR-0049 影响 §4）。
- probe 是**轻探测**：主解释器 `importlib.util.find_spec`、venv 后端子进程 `import`，
  绝不真 import 重包进当前进程。

## 3. 常用命令

```bash
rs_fetchable.py state --json           # 三态清单（agent 用）
rs_fetchable.py install <module>       # 现场下载（beatnet/madmom/demucs/scenedetect/rvm/bytetrack/opencv/clip）
rs_fetchable.py update --check         # 清单版本比对（只读）
rs_fetchable.py update --apply         # 显式更新有差异模块（绝不自动更新）
rs_fetchable.py rollback <module>      # 清理 config 回写项（下载件保留不删）
```

兼容入口 `python tools/fetch_deps.py`：`ocr` / `vqa` / `asr` / `subtitle` 四个既有用法
行为不变；`state` / `install` / `update` / `rollback` 与八组件名委托 `rs_fetchable.py`。

## 4. `--no-fetch` 离线保证（门禁断言）

- `--no-fetch` 下**绝不触发网络**：网络闸在模块层（`_require_net`），下载与 pip 先撞闸，
  socket 层面也不许——`tests/test_fetchable.py` 用 mock 断言零网络调用。
- 缺组件时按映射表降级档处置并留痕，不阻塞主流程。

## 5. `--auto` 无人值守策略（方案 §5.7）

- **默认降级，不阻塞**：`--auto` 下缺组件 → 直接降级 + 留痕，绝不卡在下载上。
- 显式 `--auto --allow-fetch` 才允许自动下载；下载失败自动转降级并保留日志。
- 交互模式（非 auto 且 TTY）走下面的三选项；非交互环境等同 `--auto` 降级。

## 6. 首次体验三选项（文案定式，改动须同步 `ensure_ready`）

```text
[cutflow] 需要「节拍检测」能力以支持卡点(当前未部署)
  → 组件: beatnet (约 320MB, 装到 tools\.venv-dsp)
  → 下载后长期保留,不会重复下载
  [1] 现在下载(预计 3–8 分钟,视网络)  [2] 降级为能量起音(onset-energy,精度下降)  [3] 取消
```

预计时间按体积粗估（0.7–1.4MB/s）：45MB≈1–2 分钟，320MB≈3–8 分钟，600MB≈6–15 分钟。

## 7. 存储与回滚

- 下载落点统一 `tools/deps/<module>/`（.gitignore，绝不入库）；安装标记
  `.installed.json`、失败标记 `.failed.json` 同目录。
- 重依赖独立 venv：`tools/.venv-dsp`（BeatNet/madmom/Demucs，节拍与分离）、
  `tools/.venv-torch`（RVM/CLIP，视觉重依赖）、既有 `tools/.venv-asr`（FunASR）。
- 回滚：`rs_fetchable.py rollback <module>` 只清 config 回写项（`config.deps.<module>`，
  既有三件清 `ocr_exe` / `vqa_exe` / `asr.models_dir`）；**下载过的文件保留不删**，
  删除 `tools/deps/<module>/` 即回到缺失态。

## 8. 环境变量清单（懒加载相关 + 导出链路有效项）

| 变量 | 作用 | 备注 |
|---|---|---|
| `WPI_FFMPEG` | **MP4 导出有效项**：artboard 的 MP4 导出走 WPI 浏览器车道，只认它 | **`ARTBOARD_FFMPEG` 对 MP4 导出无效**（安信德 #13）；两个都设（rules/intake.md 环境 checklist） |
| `ARTBOARD_STUDIO` | artboard 进工程目录用 | 与 MP4 导出无关 |
| `CUTFLOW_DEPS_DIR` | 可选件落点注入（缺省 `tools/deps/`） | 门禁零环境测试用；日常不设 |
| `CUTFLOW_CONFIG` | config.json 路径注入（缺省仓库根） | rollback/install 回写测试用；日常不设 |

venv 不是环境变量但同属部署面：`.venv-asr`＝自带 FunASR；`.venv-dsp`＝节拍/分离类
（torch + BeatNet/madmom/Demucs）；`.venv-torch`＝视觉重依赖（torch + RVM/CLIP）。

## 9. 清单与新鲜度（D4 债的机械覆盖）

- `tools/deps-manifest.json` 是静态种子清单：模块/版本/大小/sha256/下载源/适用能力。
  **sha256 留空 + `verify: "pending"` = 采用前须一手核实**（版本、pip 名、URL、权重直链），
  核实一条转 verified 一条；发版时更新一次。
- `rs_doctor.py --report` 末尾「能力部署清单」节报三态 + 影响 videoType 与手法
  （读 registry 的 capabilities 字段映射）+ 清单新鲜度；**零可选件非 fatal**（报「基础档可用」）。
