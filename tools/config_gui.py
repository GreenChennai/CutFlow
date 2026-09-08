"""CutFlow 配置编辑器(纯 tkinter,零第三方依赖;PyInstaller onefile 打包为 exe)。

为不熟悉 JSON 的小白用户设计:分组表单 + 中文说明 + 路径浏览 + 保存时合法化 JSON。
用法:python config_gui.py [config.json 路径](默认仓库根 config.json)
"""
from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

REPO = Path(__file__).resolve().parents[1]

# 字段定义:(分组, 键路径, 中文标签, 说明, 类型)
# 类型: path(带浏览) / text / bool / textlist(逗号分隔)
FIELD_DEFS = [
    ("渲染", "ffmpeg_dir", "FFmpeg 程序目录", "ffmpeg.exe 所在的 bin 文件夹。没有装过?关掉本窗口,运行 tools\\fetch_ffmpeg.py 一键下载部署。", "path"),
    ("渲染", "workdir_root", "视频工程根目录", "所有视频工程的存放位置,每个工程会自动建 00_brief~06_output 子目录。建议放在空间大的盘。", "path"),
    ("感知服务", "asr.url", "语音识别服务地址", "FunASR 转写服务的地址。默认 http://127.0.0.1:8000,由 MomentShift 软件或 tools\\start_asr.py 提供,保持默认即可。", "text"),
    ("感知服务", "asr.model", "语音识别模型", "转写用模型名,默认 paraformer-large(中文效果最好),保持默认即可。", "text"),
    ("感知本地", "ocr_exe", "OCR 文字识别程序", "OCR.exe 的完整路径。没有?运行 tools\\fetch_deps.py ocr 自动下载(约 110MB)。留空表示不用本地 OCR。", "path"),
    ("感知本地", "vqa_python", "VQA 解释器路径", "VQA 用的 python.exe。没有?运行 tools\\fetch_deps.py vqa 自动下载(约 630MB)。留空表示不用本地 VQA。", "path"),
    ("感知本地", "vqa_cli", "VQA 入口脚本", "qora_cli.py 的完整路径,和 VQA 解释器配套下载。", "path"),
    ("感知本地", "sense.force_local", "强制用本地 OCR/VQA", "勾选=总是调用本地 OCR/VQA 程序;不勾=优先用 AI 自带的看图能力,本地程序只是备选。推荐不勾。", "bool"),
    ("声音", "tts.url", "语音合成服务地址", "GPT-SoVITS 引擎地址,默认 http://127.0.0.1:9885。引擎由 EchoSmith 技能或手动启动 api_v2.py 提供。", "text"),
    ("声音", "tts.engine_dir", "语音合成引擎目录", "GPT-SoVITS 整合包目录(里面有 api_v2.py 和 runtime 文件夹)。跟随 EchoSmith 技能安装。", "path"),
    ("声音", "tts.voices_dir", "音色卡目录", "存放各音色 card.json 的文件夹(每个子文件夹一个音色)。", "path"),
    ("声音", "tts.default_voice", "默认音色名", "配音默认使用的音色。填音色卡目录名或权重名,如 koubo-test、Jimi。", "text"),
    ("声音", "tts.disabled_voices", "禁用音色列表", "暂时不可用的音色名,英文逗号分隔,如: Jimi。可用的音色不要填在这里。", "textlist"),
    ("剪映 5.9", "jianying59.exe", "剪映 5.9 主程序", "JianyingPro.exe 完整路径。注意:只有 5.9 版本能自动生成草稿,6.0 以上版本草稿加密,请勿填写新版路径。", "path"),
    ("剪映 5.9", "jianying59.draft_root", "剪映草稿根目录", "剪映保存草稿的文件夹。在剪映全局设置里可以查到,默认 C 盘用户目录下。", "path"),
    ("剪映 5.9", "jianying59.root_meta", "草稿清单文件", "root_meta_info.json 的完整路径(通常在 AppData\\Local\\JianyingPro\\User Data\\Projects\\com.lveditor.draft\\ 下)。用 5.9 打开一次剪映后就会生成。", "path"),
    ("扩展", "momentshift_dir", "MomentShift 目录", "MomentShift 项目的本地路径(提供语音识别服务)。", "path"),
    ("扩展", "artboard_dir", "artboard 技能目录", "artboard 海报技能路径(用于生成片头/封面/动画卡)。", "path"),
    ("扩展", "proxy", "网络代理", "下载模型/素材用的代理,如 http://127.0.0.1:7890。没有代理就留空。", "text"),
]


def get_nested(cfg: dict, key: str):
    cur = cfg
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def set_nested(cfg: dict, key: str, value):
    parts = key.split(".")
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


class App(tk.Tk):
    def __init__(self, config_path: Path):
        super().__init__()
        self.title("CutFlow 配置编辑器")
        self.geometry("860x640")
        self.config_path = config_path
        self.cfg: dict = {}
        if config_path.is_file():
            self.cfg = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            example = config_path.parent / "config.example.json"
            if example.is_file():
                self.cfg = json.loads(example.read_text(encoding="utf-8"))
                messagebox.showinfo("提示", "还没有 config.json,已从 config.example.json 载入默认值。\n填好后点【保存】即生成 config.json。")
        self.vars: dict[str, tk.Variable] = {}

        head = ttk.Frame(self); head.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(head, text=f"配置文件: {config_path}", foreground="#666").pack(side="left")
        ttk.Button(head, text="保存", command=self.save).pack(side="right")
        ttk.Button(head, text="另存为…", command=self.save_as).pack(side="right", padx=(0, 8))

        canvas = tk.Canvas(self, highlightthickness=0)
        scroll = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas, padding=10)
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=body, anchor="nw", width=820)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(12, 0))
        scroll.pack(side="right", fill="y")

        cur_group = None
        for group, key, label, hint, typ in FIELD_DEFS:
            if group != cur_group:
                cur_group = group
                ttk.Label(body, text=f"【{group}】", font=("", 12, "bold")).pack(anchor="w", pady=(14, 4))
            row = ttk.Frame(body); row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=18, anchor="e").pack(side="left", padx=(0, 8))
            val = get_nested(self.cfg, key)
            if typ == "bool":
                var = tk.BooleanVar(value=bool(val))
                ttk.Checkbutton(row, variable=var).pack(side="left")
            else:
                if typ == "textlist" and isinstance(val, list):
                    val = ", ".join(str(v) for v in val)
                var = tk.StringVar(value="" if val is None else str(val))
                entry = ttk.Entry(row, textvariable=var, width=52)
                entry.pack(side="left", fill="x", expand=True)
                if typ == "path":
                    ttk.Button(row, text="浏览…", width=8,
                               command=lambda v=var: self.browse(v)).pack(side="left", padx=(6, 0))
            self.vars[key] = var
            hrow = ttk.Frame(body); hrow.pack(fill="x")
            ttk.Label(hrow, text="    " + hint, foreground="#888", wraplength=700,
                      justify="left").pack(anchor="w")

        ttk.Label(self, text="保存时会自动生成合法 JSON,不需要手动敲引号和逗号。",
                  foreground="#2f8c5c").pack(anchor="w", padx=12, pady=6)

    def browse(self, var: tk.StringVar):
        p = filedialog.askdirectory() or filedialog.askopenfilename()
        if p:
            var.set(p.replace("/", "\\"))

    def save(self):
        for group, key, label, hint, typ in FIELD_DEFS:
            var = self.vars[key]
            if typ == "bool":
                set_nested(self.cfg, key, bool(var.get()))
            elif typ == "textlist":
                txt = var.get().strip()
                set_nested(self.cfg, key, [x.strip() for x in txt.split(",") if x.strip()] if txt else [])
            else:
                set_nested(self.cfg, key, var.get().strip())
        try:
            self.config_path.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        messagebox.showinfo("已保存", f"配置已写入:\n{self.config_path}\n\n运行 rs_doctor.py --report 可验证是否就绪。")

    def save_as(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", initialfile="config.json")
        if p:
            self.config_path = Path(p)
            self.save()


def main():
    if len(sys.argv) > 1:
        cfg_path = Path(sys.argv[1])
    elif getattr(sys, "frozen", False):  # exe 模式:配置跟 exe 放一起
        cfg_path = Path(sys.executable).parent / "config.json"
    else:
        cfg_path = REPO / "config.json"
    app = App(cfg_path)
    app.mainloop()


if __name__ == "__main__":
    main()
