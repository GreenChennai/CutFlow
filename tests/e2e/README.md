# tests/e2e —— 端到端回归层(T5.3c)

CI **默认不跑**这一层(`pytest.ini` 的 addopts `-m "not e2e"`);按需或定时跑:

```bash
pytest -m e2e                # 只跑 e2e 层(CLI 的 -m 覆盖 addopts)
pytest                       # 默认全量 = 单元 + 工程层,e2e 被排除
pytest tests/e2e/test_smoke_e2e.py -m e2e -v
```

## marker 用法

- 每个测试文件顶部:`pytestmark = [pytest.mark.e2e, pytest.mark.skipif(...环境缺失...)]`;
- 环境缺失(如本机没有 ffmpeg)必须 **skipif 显式跳过**,不许伪装通过;
- 断言失败的输出要带上下文(rs_* 协议的 `code` / `message`),禁止裸 assert。

## 素材路径约定

- **真实素材**(后续 C 组填入):放
  `C:\Users\Velon\AppData\Local\Temp\cutflow-e2e-assets\<案名>\`,
  测试用 `pytest.mark.skipif(not ASSET.exists(), reason="本机无该真实素材")`
  声明依赖;素材不入仓、不进 git;
- **合成素材**(冒烟级):ffmpeg lavfi 现合成,产物一律写 `tmp_path`
  (pytest 夹具),绝不写进仓库、绝不污染 `cutflow-e2e-assets`;
- 禁止大渲染:合成素材 ≤10s、分辨率 ≤640x480、draft/final 档单次渲染。

## 验收口径(C 组接手后)

- 每条真实素材走 S0→S11 核心链(经 `rs_run.py --auto` 阶段引擎,不走散装命令);
- 过 S9 三重闸(机械对账)后,`rs_verify --score` 评分卡总分 ≥75(T5.1/T5.2
  交付后接入;接入前先落 S9 机械结论);
- 冒烟级(本目录 `test_smoke_e2e.py`)只需证明框架与核心链可用:
  合成素材 + 手写转写稿(离线 S1 通道)→ `--auto` 全链 → 幂等收敛
  (二次运行全缓存命中)→ 关键产物在盘。

## 运行时长预算

冒烟级单文件 ≤5 分钟;真实素材级由 C 组各自声明(建议单条 ≤15 分钟,
超时用 `--timeout` 类机制显式限制,不让 CI 挂死)。
