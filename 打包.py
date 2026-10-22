# -*- coding: utf-8 -*-
"""把项目打包成一个单文件 exe（发布/作业汇总助手.exe）。

用法（在项目目录下）：

    python 打包.py

会做这几件事：
  1. 在系统临时目录建一个**一次性**构建环境，装 PyInstaller（不污染项目里的 .venv）
  2. 调 PyInstaller 打出 `发布/作业汇总助手.exe`
  3. 删掉构建中间产物（build/、*.spec、临时环境）

产物是单文件、免安装 Python 的 exe，直接发给别人即可。
exe 的配置（config.json / account.json / notify.json）会写在 **exe 旁边**，
所以别把 exe 放进 C:\\Program Files 这类没有写权限的目录。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "作业汇总助手"
ICON = HERE / "通知图标.ico"
ICON_PNG = HERE / "通知图标.png"
ENTRY = HERE / "main.py"
DIST = HERE / "发布"

EXCLUDES = ["numpy", "pandas", "PIL", "matplotlib", "scipy", "PyQt5", "PySide2",
            "pytest", "setuptools", "unittest", "pydoc", "doctest"]

README_TEXT = """作业汇总助手 · 优学院  ——  使用说明
================================================

这是什么
  东莞理工学院优学院 / 强智教学平台的作业汇总工具：一键汇总所有课程里
  最近要交的作业和待互评任务，按截止时间排序倒计时。
  程序关掉之后也能自动提醒临期作业。

怎么用（三步）
  1. 双击「作业汇总助手.exe」
  2. 登录：输入学号密码。密码用 Windows 加密存在本机，只有你这台电脑能解开。
     如果提示需要验证码 / 统一身份认证，改用下面的「粘贴登录凭据」：
       浏览器登录优学院 → 按 F12 → Application → Cookies → lms.dgut.edu.cn
       → 复制 AUTHORIZATION 的值 → 粘到程序里
  3. 点上方「提醒设置…」→ 直接点「保存并启用」。搞定。

关于自动提醒
  · 不需要一直开着这个程序，它注册的是 Windows 计划任务
  · 默认提前 3 天开始提醒，每天 08:30 起每 3 小时检查一次
  · 提醒是「提醒式」通知，不点掉会一直停在屏幕上，不会一晃眼错过
  · 找不到通知？按 Win + N 打开「通知中心」，历史通知都在里面
  · 设置里还能开微信推送（手机也能收到）和邮件，需要自己填一下

重要：不要用管理员身份运行
  本程序不需要管理员权限（计划任务是以你的身份注册的，不需要提权）。
  用管理员身份运行反而会有副作用：点「打开作业页 / 打开配置页面」时，
  浏览器会跟着带上管理员权限，Edge / Chrome 会弹
  「现有实例正在以提升的权限运行」。如果已经这样启动了，右键快捷方式 →
  属性 → 兼容性 → 取消勾选「以管理员身份运行」即可。

遇到问题
  双击运行 作业汇总助手.exe --diag（或命令行里跑），会生成一份
  「自检报告.txt」，把运行权限、计划任务能不能用、各通知通道通不通
  全部列出来，方便定位。

配置放在哪
  都在 exe 旁边的文件里（config.json / notify.json / account.json）。
  所以：
    · 别把 exe 放进 C:\\Program Files 这类没有写权限的目录
    · 别随便挪动位置——挪了计划任务就失效，重新点一次「保存并启用」即可

不想要了
  「提醒设置…」→「停用提醒」，然后删掉整个文件夹就行。

免责
  仅供个人学习使用；所有请求只发往学校服务器，没有任何外部上报。
  使用本程序产生的后果自负。
"""


def write_readme() -> None:
    (DIST / "使用说明.txt").write_text(README_TEXT, encoding="utf-8")
    print(f"写入 {DIST / '使用说明.txt'}")


def run(args, **kw):
    print("+", " ".join(str(a) for a in args))
    return subprocess.run(args, **kw)


def find_uv() -> str:
    for cand in (shutil.which("uv"),
                 Path.home() / ".local" / "bin" / "uv.exe",
                 Path.home() / ".cargo" / "bin" / "uv.exe"):
        if cand and Path(cand).exists():
            return str(cand)
    return ""


def build_python(env_dir: Path, cache_dir: Path) -> str:
    """准备一个装了 PyInstaller 的临时解释器，返回它的路径。

    优先用 uv（快）；uv 不可用或失败时退回标准库的 venv + pip。
    """
    uv = find_uv()
    if uv:
        env = dict(os.environ)
        # uv 默认把缓存和临时文件放在 %LOCALAPPDATA% / %TEMP%，
        # 受限账户或安全软件下可能整个写不进去，一律改到构建目录里
        cache_dir.mkdir(parents=True, exist_ok=True)
        env["UV_CACHE_DIR"] = str(cache_dir)
        env["TEMP"] = str(cache_dir)
        env["TMP"] = str(cache_dir)
        try:
            run([uv, "venv", str(env_dir), "--python", "3.12"], check=True, env=env)
            py = env_dir / "Scripts" / "python.exe"
            run([uv, "pip", "install", "--python", str(py), "pyinstaller", "requests"],
                check=True, env=env)
            return str(py)
        except (subprocess.CalledProcessError, OSError) as e:
            print(f"!! uv 方式失败（{e}），改用 venv + pip")
    run([sys.executable, "-m", "venv", str(env_dir)], check=True)
    py = env_dir / "Scripts" / "python.exe"
    run([str(py), "-m", "ensurepip", "--upgrade"], check=True)
    run([str(py), "-m", "pip", "install", "-q", "--no-cache-dir",
         "pyinstaller", "requests"], check=True)
    return str(py)


def make_ico() -> None:
    """没有 .ico 时用 Pillow 从 png 生成（可选，缺 Pillow 就跳过）。"""
    if ICON.exists():
        return
    try:
        from PIL import Image
    except ImportError:
        print(f"!! 缺少 {ICON.name}，且没装 Pillow，exe 将使用默认图标")
        return
    Image.open(ICON_PNG).convert("RGBA").save(
        ICON, format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"生成 {ICON.name}")


def main() -> int:
    if not ENTRY.exists():
        print(f"找不到入口 {ENTRY}")
        return 1
    make_ico()

    # 构建目录放在项目里而不是系统临时目录：
    # 有些环境（受限账户、安全软件）不让往 %TEMP% 根目录写，uv 会直接失败；
    # 而且放在项目里也方便出问题时自己看一眼。跑完 finally 会删掉。
    work = HERE / "_build_tmp"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    env_dir = work / "env"
    try:
        py = build_python(env_dir, work / "uvcache")
        DIST.mkdir(exist_ok=True)
        args = [py, "-m", "PyInstaller", "--noconfirm", "--onefile", "--noconsole",
                "--name", NAME,
                "--add-data", f"{ICON_PNG}{os.pathsep}.",
                "--hidden-import", "notify", "--hidden-import", "作业汇总助手",
                "--distpath", str(DIST), "--workpath", str(work / "work"),
                "--specpath", str(work)]
        if ICON.exists():
            args += ["--icon", str(ICON)]
        for mod in EXCLUDES:
            args += ["--exclude-module", mod]
        args.append(str(ENTRY))
        if run(args).returncode != 0:
            print("打包失败")
            return 1

        exe = DIST / f"{NAME}.exe"
        if not exe.exists():
            print("没找到产物 exe")
            return 1
        write_readme()
        print(f"\n完成：{exe}  ({exe.stat().st_size / 1048576:.1f} MB)")
        # 如果你自己在 发布 目录里跑过 exe，这里会留下你的账号配置，发人前必须删掉
        keep = {f"{NAME}.exe", "使用说明.txt"}
        stale = [p.name for p in DIST.iterdir() if p.name not in keep]
        if stale:
            print(f"!! 发布目录里还有：{'、'.join(stale)}")
            print("!! 这些可能是你自己的账号/配置，发人前请手动删掉")
        print("把「发布」文件夹里的 exe + 使用说明.txt 发给别人即可。")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)      # 中间产物一律不留


if __name__ == "__main__":
    sys.exit(main())
