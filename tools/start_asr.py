"""无头拉起 FunASR 服务(MomentShift 引擎,端口 8000)。

用法:python tools/start_asr.py   (保持前台运行;后台用请自行 nohup/计划任务)
说明:PATH 需含 ffmpeg(服务端 demux 依赖);若当前解释器缺 MomentShift 依赖
     (qfluentwidgets),自动用 MomentShift .venv 重执行自身。
"""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "cutflow" / "scripts"))
cfg = __import__("rs_common").load_config()

os.environ["PATH"] = cfg["ffmpeg_dir"] + os.pathsep + os.environ.get("PATH", "")
MS = Path(cfg.get("momentshift_dir", r"E:\平日资料\GitHub\MomentShift"))

try:
    import qfluentwidgets  # noqa: F401
except ImportError:
    venv_py = MS / ".venv" / "Scripts" / "python.exe"
    if not venv_py.is_file():
        raise SystemExit(f"缺依赖且找不到 MomentShift venv:{venv_py}")
    os.execv(str(venv_py), [str(venv_py), __file__])

sys.path.insert(0, str(MS / "src"))

from momentshift.core.asr_server import AsrServer  # noqa: E402

srv = AsrServer(log_cb=lambda line: print(line, flush=True))
ok, msg = srv.start(port=cfg["asr"].get("port", 8000), model=cfg["asr"]["model"], structured=True)
print("start:", ok, msg, flush=True)
while True:
    import time
    time.sleep(3600)
