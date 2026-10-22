# -*- coding: utf-8 -*-
"""打包入口（PyInstaller 用的就是它）。

同一个 exe 兼顾两种身份：

    作业汇总助手.exe                 打开主界面
    作业汇总助手.exe --run           执行一次提醒检查（计划任务调用）
    作业汇总助手.exe --test          立刻发一条测试通知
    作业汇总助手.exe --install       注册计划任务
    作业汇总助手.exe --uninstall     删除计划任务
    作业汇总助手.exe --status        查看提醒状态（弹窗显示）
    作业汇总助手.exe --settings      打开提醒设置
    作业汇总助手.exe --reset         清空提醒记录

这里把两个模块都放在顶层 import，是为了让 PyInstaller 的静态分析一定能发现它们
（`作业汇总助手.py` 里对 notify 的引用在函数内部，单靠它分析可能漏掉）。
"""
from __future__ import annotations

import sys

import 作业汇总助手 as app
import notify


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in notify.CLI_FLAGS:
        return notify.main(argv)
    return app.main(argv)


if __name__ == "__main__":
    sys.exit(main())
