# -*- coding: utf-8 -*-
"""启动入口：双击本文件（.pyw）以无控制台方式运行作业汇总助手。"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

LOG = os.path.join(HERE, "启动错误.log")


def log_exc(text):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


try:
    import 作业汇总助手 as app
    raise SystemExit(app.main(sys.argv[1:]))
except SystemExit:
    raise
except Exception:
    tb = traceback.format_exc()
    log_exc(tb)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("作业汇总助手启动失败", tb)
        root.destroy()
    except Exception:
        log_exc("无法弹出错误对话框，请手动查看启动错误.log\n" + tb)