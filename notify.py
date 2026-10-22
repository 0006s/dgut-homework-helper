# -*- coding: utf-8 -*-
"""作业汇总助手 · 临期提醒

由 Windows 计划任务定时调用，不需要主程序一直开着。
所有配置持久化在 notify.json（密钥用 Windows DPAPI 加密），提醒记录在 notify_state.json。

命令行：
    pythonw notify.py --run         执行一次检查并按配置提醒（计划任务用）
    pythonw notify.py --test        忽略免打扰/间隔，立即发一条测试提醒
    python  notify.py --install     注册/更新计划任务并启用
    python  notify.py --uninstall   删除计划任务（停用）
    python  notify.py --status      查看配置与计划任务状态
    pythonw notify.py --settings    打开图形化提醒设置
    python  notify.py --reset       清空提醒记录（下次会重新提醒一遍）
    python  notify.py --diag        自检：提醒功能卡在哪一步
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests

def _app_dir() -> Path:
    """配置/日志放在程序旁边；打包成 exe 后要用 sys.executable 定位，
    因为 `__file__` 会指向 PyInstaller 退出即删的临时解包目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """随程序打包进去的只读资源（图标）所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", _app_dir()))
    return _app_dir()


BASE = _app_dir()
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import 作业汇总助手 as core  # noqa: E402  （复用登录/扫描/解析逻辑）

NOTIFY_FILE = BASE / "notify.json"
STATE_FILE = BASE / "notify_state.json"
LOG_FILE = BASE / "提醒日志.log"
TASK_NAME = "作业汇总助手-临期提醒"
PS5 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                   r"System32\WindowsPowerShell\v1.0\powershell.exe")
# 系统通知的来源（AUMID）。
#
# 踩过的坑：用自己的 AUMID `DGUT.HomeworkReminder` 时，注册表和开始菜单快捷方式
# 都配好了、Show() 也不报错、通知平台里还记了几十条，
# 但**通知永远不显示，事件日志里也查不到投递记录**——
# 而用系统自带的这个 PowerShell AUMID 就正常投递。
# （网上说桌面程序只要写注册表 + 建快捷方式就行，在你这台机器上不成立。）
#
# 所以直接用能用的那个。代价：通知左下角的来源名会显示成「Windows PowerShell」。
# 以后如果要换回自己的名字，改回 OWN_APP_ID 再测即可。
TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
OWN_APP_ID = "DGUT.HomeworkReminder"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
CLI_FLAGS = ("--run", "-r", "--test", "--install", "--uninstall",
             "--status", "--settings", "--reset", "--diag")


def default_icon_path() -> Path:
    return resource_dir() / "通知图标.png"

DEFAULT = {
    "enabled": False,
    "toast_app_name": "周一天女仆",   # 系统通知里显示的来源名称
    "toast_icon": "",          # 通知图标，留空 = 项目目录下的 通知图标.png
    "toast_sticky": True,      # 提醒式通知：不点掉就一直停在屏幕上
    "days_before": 3,          # 提前几天开始提醒
    "daily_at": "08:30",       # 每天第一次检查的时间
    "interval_hours": 3,       # 检查间隔（小时）
    "quiet_start": "22:30",    # 免打扰开始（留空则不启用）
    "quiet_end": "07:30",      # 免打扰结束
    # 免打扰时段里，剩余时间少于这个小时数的作业照样提醒（界面上不再暴露，
    # 固定 2 小时：真快截止了还是得叫醒你）
    "quiet_ignore_within_hours": 2,
    "daily_digest": True,      # 每天汇总提醒一次
    "repeat_hours": [24, 6, 1],       # 剩余不足这些小时数时额外提醒
    "min_gap_minutes": 20,     # 两次提醒的最小间隔，防重复轰炸
    "max_items": 8,            # 邮件/微信里最多列几条
    "toast_max_items": 3,      # 系统通知里最多列几条（多了会被 Windows 截断）
    "popup_seconds": 30,       # 弹窗自动关闭秒数（0=不自动关）
    "snooze_minutes": 30,      # 弹窗「稍后提醒」间隔
    "channels": {
        # 默认只开系统通知：提醒式通知本身就不会一晃而过，新装的机器不用配任何东西就能用
        "toast": True,         # Windows 系统通知
        "popup": False,        # 屏幕弹窗
        "sound": False,        # 提示音
        "wechat": False,       # 微信推送（PushPlus / Server酱）
        "email": False,        # 邮件
    },
    "wechat": {"provider": "wecom", "token": "", "topic": ""},
    "email": {"preset": "QQ邮箱", "host": "", "port": 465, "ssl": True,
              "user": "", "password": "", "to": ""},
}

CHANNEL_LABELS = {
    "toast": "Windows 系统通知",
    "popup": "屏幕弹窗",
    "sound": "提示音",
    "wechat": "微信推送",
    "email": "邮件",
}

# 常见邮箱的 SMTP 预设：服务器 / 端口 / 是否 SSL / 配置页面 / 提示
MAIL_PRESETS = {
    "QQ邮箱": ("smtp.qq.com", 465, True, "https://mail.qq.com/",
               "登录后：设置 → 账号 → 开启「POP3/IMAP/SMTP服务」→ 生成授权码"),
    "163邮箱": ("smtp.163.com", 465, True, "https://mail.163.com/",
                "登录后：设置 → POP3/SMTP/IMAP → 开启服务 → 新增授权密码"),
    "Gmail": ("smtp.gmail.com", 587, False, "https://myaccount.google.com/apppasswords",
              "需先开两步验证，再生成「应用专用密码」当密码用"),
    "Outlook": ("smtp.office365.com", 587, False, "https://account.microsoft.com/security",
                "在「安全」里设置应用密码"),
    "自定义": ("", 465, True, "", "自己填服务器、端口，密码填服务商给的授权码"),
}


def detect_mail_preset(email_cfg: dict) -> str:
    """猜当前配置对应哪个邮箱服务商（配置里存了就用存的，否则按服务器名反查）。"""
    saved = str((email_cfg or {}).get("preset") or "").strip()
    if saved in MAIL_PRESETS:
        return saved
    host = str((email_cfg or {}).get("host") or "").strip().lower()
    for name, preset in MAIL_PRESETS.items():
        if preset[0] and preset[0].lower() == host:
            return name
    return "自定义"

SECRET_PATHS = (("wechat", "token"), ("email", "password"))

# 推送服务商：配置页面 / 要填什么 / 怎么拿
# 前三个（企业微信、钉钉、飞书）是**群机器人 Webhook，完全免费且不需要实名**，
# PushPlus 现在要实名收费，所以把免费的放前面。
WX_SITES = {
    "wecom": ("https://work.weixin.qq.com/", "Webhook 地址",
              "企业微信里建个群（个人也能免费建）→ 群右上角 → 群机器人 → 添加 → 复制 Webhook 地址"),
    "dingtalk": ("https://www.dingtalk.com/", "Webhook 地址",
                 "钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义 → 安全设置选「自定义关键词」并填「作业」→ 复制 Webhook"),
    "feishu": ("https://www.feishu.cn/", "Webhook 地址",
               "飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制 Webhook 地址"),
    "serverchan": ("https://sct.ftqq.com/", "SendKey",
                   "微信扫码登录 → 复制 SendKey（免费额度有限）"),
    "pushplus": ("https://www.pushplus.plus/", "token",
                 "微信扫码登录 → 一对一推送 → 复制 token（⚠️ 现在要实名才能发消息）"),
    "自定义": ("", "地址或 token", "填服务商给你的完整 Webhook 地址"),
}


# ==================== 基础工具 ====================
def log(msg: str, quiet: bool = False) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 256 * 1024:
            tail = LOG_FILE.read_text(encoding="utf-8", errors="replace")[-65536:]
            LOG_FILE.write_text(tail, encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if not quiet and sys.stdout is not None:
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"))


def _deep_merge(base: dict, data) -> dict:
    out = dict(base)
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        if k not in base:
            continue
        if isinstance(base[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(base[k], v)
        else:
            out[k] = v
    return out


def _atomic_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def parse_hm(text):
    """'08:30' -> (8, 30)；非法返回 None。"""
    if not text:
        return None
    parts = str(text).strip().replace("：", ":").split(":")
    if len(parts) < 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


# ==================== 配置读写（密钥 DPAPI 加密） ====================
def read_cfg(path: Path = NOTIFY_FILE) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    cfg = _deep_merge(DEFAULT, data)
    for section, field in SECRET_PATHS:
        raw = cfg.get(section, {}).get(field) or ""
        if raw:
            try:
                cfg[section][field] = core.dpapi_unprotect(raw)
            except Exception:
                cfg[section][field] = ""
    try:
        cfg["days_before"] = max(1, int(cfg["days_before"]))
    except (TypeError, ValueError):
        cfg["days_before"] = DEFAULT["days_before"]
    for key in ("interval_hours", "max_items", "popup_seconds", "snooze_minutes"):
        try:
            cfg[key] = max(0, int(cfg[key]))
        except (TypeError, ValueError):
            cfg[key] = DEFAULT[key]
    if not parse_hm(cfg.get("daily_at")):
        cfg["daily_at"] = DEFAULT["daily_at"]
    name = str(cfg.get("toast_app_name") or "").strip()
    cfg["toast_app_name"] = name or DEFAULT["toast_app_name"]
    try:
        cfg["repeat_hours"] = sorted({int(h) for h in cfg.get("repeat_hours") or [] if int(h) > 0},
                                     reverse=True)
    except (TypeError, ValueError):
        cfg["repeat_hours"] = list(DEFAULT["repeat_hours"])
    return cfg


def write_cfg(cfg: dict, path: Path = NOTIFY_FILE) -> None:
    data = _deep_merge(DEFAULT, cfg)
    for section, field in SECRET_PATHS:
        raw = (data.get(section) or {}).get(field) or ""
        data[section][field] = core.dpapi_protect(raw) if raw else ""
    data["enabled"] = bool(data.get("enabled"))
    _atomic_json(path, data)


def read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("items"), dict):
        data["items"] = {}
    return data


def write_state(state: dict) -> None:
    try:
        _atomic_json(STATE_FILE, state)
    except OSError as e:
        log(f"写入提醒记录失败：{e}")


# ==================== 取数 ====================
def fetch_items() -> tuple[list, str]:
    """返回 (items, 错误信息)。错误信息非空表示没拿到数据。"""
    acc_path = BASE / "account.json"
    acc = core.read_account(acc_path)
    user, pwd, token = acc.get("username", ""), acc.get("password", ""), acc.get("token", "")
    last_err = ""
    session = None
    if token:
        try:
            session, _ident, _t = core.token_login(token)
        except Exception as e:
            last_err = f"已保存的登录凭据失效（{e}）"
    if session is None and user and pwd:
        try:
            session, _ident, new_token = core.password_login(user, pwd)
        except Exception as e:
            return [], f"用账号密码自动登录失败：{e}"
        try:
            core.write_account(acc_path, user, pwd, new_token)
        except Exception:
            pass
        last_err = ""
    if session is None:
        return [], last_err or "本机没有保存登录凭据，请先打开作业汇总助手登录一次"
    try:
        return core.scan(session)["items"], ""
    except core.ApiError as e:
        if not e.auth:
            return [], f"读取作业失败：{e}"
        if not (user and pwd):
            return [], f"登录已失效：{e}"
        try:
            session, _ident, new_token = core.password_login(user, pwd)
            core.write_account(acc_path, user, pwd, new_token)
            return core.scan(session)["items"], ""
        except Exception as e2:
            return [], f"登录失效且重新登录失败：{e2}"
    except Exception as e:
        return [], f"读取作业失败：{e}"


def fetch_items_retry(attempts: int = 3, delay: int = 20) -> tuple[list, str]:
    """学校服务器会间歇性 401 / 超时，多试几次再判定失败，避免误报「无法获取作业」。"""
    last = ""
    for i in range(attempts):
        items, err = fetch_items()
        if not err:
            if i:
                log(f"第 {i + 1} 次尝试成功")
            return items, ""
        last = err
        if i < attempts - 1:
            log(f"读取失败（第 {i + 1}/{attempts} 次）：{err}；{delay} 秒后重试")
            time.sleep(delay)
    return [], last


def window_items(items: list, cfg: dict, now_ms: int) -> list:
    """筛出「还没过期 + 在提前提醒窗口内」的作业，并沿用主程序的隐藏/排除关键词设置。"""
    try:
        gui_cfg = core.read_cfg(BASE / "config.json")
    except Exception:
        gui_cfg = {"hidden": [], "exclude": ""}
    gui_cfg["days"] = 0  # 天数以提醒设置为准
    limit = int(cfg["days_before"]) * core.DAY_MS
    out = []
    for it in items:
        if it.get("kind") not in ("todo", "review"):
            continue
        end = it.get("endMs") or 0
        if not end:
            continue
        left = end - now_ms
        if left <= 0 or left > limit:
            continue
        if not core.is_visible(it, gui_cfg, now_ms):
            continue
        out.append(it)
    out.sort(key=lambda x: x["endMs"])
    return out


def thresholds(cfg: dict) -> list:
    """提醒档位（小时，降序）。例如提前 3 天 + [24,6,1] -> [72,24,6,1]。"""
    top = int(cfg["days_before"]) * 24
    hs = {top} | {h for h in cfg.get("repeat_hours") or [] if 0 < h <= top}
    return sorted(hs, reverse=True)


def stage_of(left_ms: int, hs: list):
    """当前作业处于哪一档：返回「小于等于剩余小时数的最小档位」。"""
    hours = left_ms / 3600000.0
    cur = None
    for h in hs:  # 降序，最后一个命中的就是最小的
        if hours <= h:
            cur = h
    return cur


# ==================== 消息组装 ====================
# 课程名/作业名里的冗余前缀，压缩后系统通知一行才放得下
_TAG = re.compile(r"^\s*[【\[][^】\]]*[】\]]\s*")
_YEAR = re.compile(r"^\s*(?:19|20)\d{2}\s*[-—－]?\s*")
_LEAD_DATE = re.compile(
    r"^\s*\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}日?"
    r"(\s+\d{1,2}\s*[:：]\s*\d{2})?\s*")
_PARENS = re.compile(r"[（(][^）)]*[）)]")
_TAIL_SEP = re.compile(r"[\s\-—_·、,，]+$")


def short_course(name: str) -> str:
    """'【2026秋】概率论与数理统计' -> '概率论与数理统计'；'2026工程力学A' -> '工程力学A'"""
    raw = (name or "").strip()
    return _TAIL_SEP.sub("", _YEAR.sub("", _TAG.sub("", raw))).strip() or raw


def short_title(title: str) -> str:
    """'2026-10-05 23:25 作业' -> '作业'；'第三章作业（占总成绩2%）' -> '第三章作业'"""
    raw = (title or "").strip()
    return _TAIL_SEP.sub("", _PARENS.sub("", _LEAD_DATE.sub("", raw))).strip() or raw


def short_left(ms: int) -> str:
    """core.fmt_left 去掉「还剩 」前缀，省两个字"""
    text = core.fmt_left(ms)
    return text[3:] if text.startswith("还剩 ") else text


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:max(1, n - 1)] + "…"


def _left_text(left_ms: int) -> str:
    return core.fmt_left(left_ms)


def build_message(items: list, cfg: dict, now_ms: int, prefix: str = ""):
    """返回 (标题, 完整纯文本, 系统通知用的精简分行, HTML)

    系统通知只给 1 行标题 + 最多 3 行正文，所以每行都按「课程·作业·剩X」压到最短；
    邮件/微信/文件用完整信息，不省。
    """
    max_items = max(1, int(cfg.get("max_items") or 8))
    toast_max = max(1, min(6, int(cfg.get("toast_max_items") or 3)))
    head = items[:max_items]
    todo = sum(1 for it in items if it["kind"] == "todo")
    review = sum(1 for it in items if it["kind"] == "review")
    count = f"待提交 {todo}" + (f" · 待互评 {review}" if review else "")

    title = f"{prefix}{len(items)} 项作业临近截止"

    lines = []
    for it in items[:toast_max]:
        lines.append("{}·{}·剩{}".format(
            clip(short_course(it["courseName"]), 8),
            clip(short_title(it["title"]), 9),
            short_left(it["endMs"] - now_ms)))
    rest = len(items) - min(len(items), toast_max)
    if rest > 0:
        lines.append(f"另有 {rest} 项")

    detail = []
    for it in head:
        detail.append(f"{it['courseName']} · {it['title']} — {core.fmt_left(it['endMs'] - now_ms)}"
                      f"（截止 {core.fmt_datetime(it['endMs'])}）")
    more = len(items) - len(head)
    if more > 0:
        detail.append(f"…另有 {more} 项")

    text = f"{title}\n" + "\n".join(detail) + f"\n\n{count}"
    rows = "".join(
        f"<tr><td>{_esc(it['courseName'])}</td><td>{_esc(it['title'])}</td>"
        f"<td>{_esc(core.fmt_datetime(it['endMs']))}</td>"
        f"<td>{_esc(_left_text(it['endMs'] - now_ms))}</td>"
        f"<td>{'待互评' if it['kind'] == 'review' else '待提交'}</td></tr>"
        for it in head
    )
    html = (f"<h3>{_esc(title)}</h3><p>{_esc(count)}</p>"
            "<table border='1' cellspacing='0' cellpadding='4'>"
            "<tr><th>课程</th><th>作业</th><th>截止时间</th><th>剩余</th><th>类型</th></tr>"
            f"{rows}</table>")
    if more > 0:
        html += f"<p>…另有 {more} 项</p>"
    return title, text, lines, html


def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ==================== 通道：系统通知 ====================
_PS_TEMPLATE = r'''
$ErrorActionPreference = "Stop"
$raw = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String("__PAYLOAD__"))
$data = $raw | ConvertFrom-Json
$title = [string]$data.title
$lines = @($data.lines)
$aumid = [string]$data.app_id
$appname = [string]$data.app_name
$icon = [string]$data.icon

# 注册通知来源的显示名（只在用我们自己的 AUMID 时才做）。
# 系统 AUMID 形如 {包族名}\路径\xxx.exe，含反斜杠，在注册表里会变成一串嵌套键，
# 写了也没用（实测来源名仍显示「Windows PowerShell」），所以直接跳过。
try {
  if ($aumid -notmatch '\\') {
  $key = "HKCU:\Software\Classes\AppUserModelId\$aumid"
  if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }
  $cur = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
  if ($cur.DisplayName -ne $appname) {
    Set-ItemProperty -Path $key -Name "DisplayName" -Value $appname -Type String
  }
  if ($icon -ne "" -and (Test-Path $icon) -and $cur.IconUri -ne $icon) {
    Set-ItemProperty -Path $key -Name "IconUri" -Value $icon -Type String
  }
  }
} catch { }

# 图标不再走 toast XML 的 <image>（那条路径会让通知静默丢失），
# 留着只为兼容旧调用；实际图标由 AUMID 提供。
$iconUri = ""
if ($icon -ne "" -and (Test-Path $icon)) { $iconUri = ([System.Uri]$icon).AbsoluteUri }

# 提醒式通知：不点掉就一直留在屏幕上，避免一晃眼就错过
$scen = ""
if ($data.sticky) { $scen = " scenario='reminder'" }

function Show-Toast([string]$useId) {
  [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
  [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]
  $sb = New-Object System.Text.StringBuilder
  # 不再往 toast XML 里塞 <image>。
  # 踩过的坑：图标路径是 file:///E:/项目-vibe coding/作业汇总助手/通知图标.png
  # （含中文和空格），Windows 会**静默丢弃整个 toast**——Show() 不报错，
  # 屏幕上也什么都不出现，所以「无图标回退」永远等不到异常、永远不触发。
  # 图标改由 AUMID 注册表项里的 IconUri 提供，显示效果一样。
  [void]$sb.Append("<toast" + $scen + " duration='long'><visual><binding template='ToastGeneric'>")
  [void]$sb.Append("<text>")
  [void]$sb.Append([System.Security.SecurityElement]::Escape($title))
  [void]$sb.Append("</text>")
  foreach ($l in $lines) {
    [void]$sb.Append("<text>")
    [void]$sb.Append([System.Security.SecurityElement]::Escape([string]$l))
    [void]$sb.Append("</text>")
  }
  [void]$sb.Append("</binding></visual></toast>")
  $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
  $doc.LoadXml($sb.ToString())
  $toast = New-Object Windows.UI.Notifications.ToastNotification $doc
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($useId).Show($toast)
}

$ok = $false
try { Show-Toast $aumid; Write-Output "TOAST_OK"; $ok = $true }
catch { Write-Output ("TOAST_FAIL " + $_.Exception.Message) }
if (-not $ok) {
  try { Show-Toast "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"; Write-Output "TOAST_FALLBACK_OK"; $ok = $true }
  catch { Write-Output ("TOAST_FALLBACK_FAIL " + $_.Exception.Message) }
}
if ($ok) { exit 0 }
try {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  $ni = New-Object System.Windows.Forms.NotifyIcon
  $ni.Icon = [System.Drawing.SystemIcons]::Information
  $ni.Visible = $true
  $ni.ShowBalloonTip(10000, $title, ($lines -join [Environment]::NewLine), [System.Windows.Forms.ToolTipIcon]::Info)
  Start-Sleep -Seconds 11
  $ni.Dispose()
  Write-Output "BALLOON_OK"
  exit 0
} catch {
  Write-Output ("BALLOON_FAIL " + $_.Exception.Message)
  exit 1
}
'''


def _run(args, timeout=40, encoding="utf-8", merge=True, hide=True):
    """跑一个外部命令。

    **不要用 creationflags=CREATE_NO_WINDOW。**
    踩过的坑：带上它以后 PowerShell 进程「没有创建窗口标识」，
    `ToastNotificationManager::CreateToastNotifier` 会直接抛
    HRESULT 0x80073D54「调用线程没有创建窗口标识」——系统通知完全发不出去，
    而进程看起来还是正常返回 0。

    改用 STARTUPINFO + SW_HIDE 隐藏控制台窗口：效果一样（看不到黑框），
    但不会破坏窗口标识，toast 能正常弹。
    """
    si = None
    if hide and os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, startupinfo=si)
    except Exception as e:
        return 1, str(e)
    raw = (p.stdout or b"") if not merge else ((p.stdout or b"") + (p.stderr or b""))
    try:
        text = raw.decode(encoding, "replace")
    except LookupError:
        text = raw.decode("utf-8", "replace")
    return p.returncode, text.strip()


def icon_cache_path() -> Path:
    """系统通知图标放在纯 ASCII 路径下，避免中文/空格路径在 toast URI 里出问题。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "HomeworkReminder" / "toast_icon.png"


def ensure_icon(cfg: dict) -> str:
    """把图标同步到缓存路径并返回它；没有图标文件就返回空串。"""
    raw = str(cfg.get("toast_icon") or "").strip()
    src = Path(raw).expanduser() if raw else default_icon_path()
    if not src.is_file():
        return ""
    dst = icon_cache_path()
    try:
        data = src.read_bytes()
        if not dst.is_file() or dst.read_bytes() != data:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(dst)
    except OSError:
        return ""
    return str(dst)


def send_toast(title: str, lines: list, app_name: str = "", icon: str = "",
               sticky: bool = True) -> str:
    """Windows 系统通知（自定义 AUMID 失败自动回退，再失败退回托盘气泡）。返回空串表示成功。

    sticky=True 时用「提醒式」通知：不点掉就一直停在屏幕上，避免一晃眼就错过。
    """
    if not os.path.exists(PS5):
        return "找不到 Windows PowerShell，无法发送系统通知"
    payload = base64.b64encode(json.dumps(
        {"title": title, "lines": lines[:4], "app_id": TOAST_APP_ID,
         "app_name": app_name or DEFAULT["toast_app_name"], "icon": icon or "",
         "sticky": bool(sticky)},
        ensure_ascii=False).encode("utf-8")).decode("ascii")
    script = _PS_TEMPLATE.replace("__PAYLOAD__", payload)
    # 用 -File 跑临时脚本，**不要**用 -EncodedCommand。
    # 踩过的坑：-EncodedCommand 下 Show() 同样返回成功、脚本也输出 TOAST_OK，
    # 但屏幕上什么都不出现（静默丢弃）；换成 -File 就正常弹出来。
    # 临时脚本写成 UTF-8 带 BOM，否则 PowerShell 5.1 会把中文注释当 ANSI 读。
    path = ""
    try:
        fd, path = tempfile.mkstemp(prefix="toast_", suffix=".ps1")
        with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
            f.write(script)
    except Exception as e:
        return f"系统通知失败：写临时脚本出错 {e}"
    try:
        rc, out = _run([PS5, "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-File", path],
                       timeout=40, merge=False)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if any(k in out for k in ("TOAST_OK", "TOAST_OK_NOICON", "TOAST_FALLBACK_OK", "BALLOON_OK")):
        return ""
    return f"系统通知失败：{(out or f'rc={rc}')[:200]}"


def send_sound() -> str:
    try:
        import winsound
        winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
        return ""
    except Exception as e:
        return f"提示音失败：{e}"


# ==================== 通道：微信推送 ====================
def _webhook_ok(provider: str, resp) -> str:
    """群机器人 Webhook 的返回：errcode/code 为 0 才算成功。"""
    try:
        data = resp.json()
    except ValueError:
        return f"{provider} 返回了非 JSON 内容：{resp.text[:160]}"
    code = data.get("errcode", data.get("code", data.get("StatusCode")))
    if code in (0, "0", None) and data.get("msg", "ok") in ("ok", "success", None):
        return ""
    return f"{provider} 返回异常：{str(data)[:200]}"


def send_wechat(title: str, html: str, text: str, cfg: dict) -> str:
    w = cfg.get("wechat") or {}
    token = (w.get("token") or "").strip()
    if not token:
        return "推送未配置（token / Webhook 地址是空的）"
    provider = (w.get("provider") or "wecom").lower()
    try:
        if provider == "serverchan":
            r = requests.post(f"https://sctapi.ftqq.com/{token}.send",
                              data={"title": title, "desp": text}, timeout=20)
            code = r.json().get("code") if r.ok else r.status_code
            if str(code) not in ("0", "SUCCESS"):
                return f"Server酱返回异常：{r.text[:200]}"
        elif provider == "wecom":
            r = requests.post(token, json={"msgtype": "markdown",
                                           "markdown": {"content": text}}, timeout=20)
            err = _webhook_ok("企业微信", r)
            if err:
                return err
        elif provider == "dingtalk":
            r = requests.post(token, json={"msgtype": "markdown",
                                           "markdown": {"title": title, "text": text}},
                              timeout=20)
            err = _webhook_ok("钉钉", r)
            if err:
                return err
        elif provider == "feishu":
            r = requests.post(token, json={"msg_type": "text",
                                           "content": {"text": f"{title}\n{text}"}}, timeout=20)
            err = _webhook_ok("飞书", r)
            if err:
                return err
        elif provider == "pushplus":
            body = {"token": token, "title": title, "content": html, "template": "html"}
            if w.get("topic"):
                body["topic"] = str(w["topic"])
            r = requests.post("https://www.pushplus.plus/send", json=body, timeout=20)
            data = r.json()
            if data.get("code") != 200:
                return f"PushPlus 返回异常：{str(data)[:200]}"
        else:
            # 自定义：当成一个普通 Webhook，发 markdown
            r = requests.post(token, json={"msgtype": "markdown",
                                           "markdown": {"content": text}}, timeout=20)
            err = _webhook_ok("自定义 Webhook", r)
            if err:
                return err
    except Exception as e:
        return f"推送失败：{e}"
    return ""


# ==================== 通道：邮件 ====================
def send_email(subject: str, text: str, html: str, cfg: dict) -> str:
    import smtplib
    from email.header import Header
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    e = cfg.get("email") or {}
    host = (e.get("host") or "").strip()
    user = (e.get("user") or "").strip()
    pwd = e.get("password") or ""
    to = (e.get("to") or "").strip()
    if not host or not to:
        return "邮件未配置 SMTP 服务器或收件人"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = user or to
    msg["To"] = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        port = int(e.get("port") or (465 if e.get("ssl", True) else 587))
    except (TypeError, ValueError):
        port = 465
    try:
        if e.get("ssl", True):
            srv = smtplib.SMTP_SSL(host, port, timeout=25)
        else:
            srv = smtplib.SMTP(host, port, timeout=25)
            try:
                srv.starttls()
            except smtplib.SMTPNotSupportedError:
                if user:
                    return ("邮件发送失败：服务器不支持 STARTTLS，为避免明文发送账号密码已中止。"
                            "请勾选 SSL 并改用 465 端口")
    except Exception as ex:
        return f"邮件连接失败：{ex}"
    try:
        if user:
            srv.login(user, pwd)
        srv.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        return "邮件认证被拒：QQ / 163 邮箱这里要填「授权码」而不是登录密码"
    except Exception as ex:
        return f"邮件发送失败：{ex}"
    finally:
        try:
            srv.quit()
        except Exception:
            pass
    return ""


# ==================== 通道：弹窗 ====================
def show_popup(title: str, items: list, cfg: dict, now_ms: int) -> str:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as e:
        return f"弹窗失败：{e}"
    seconds = int(cfg.get("popup_seconds") or 0)
    snooze_min = max(1, int(cfg.get("snooze_minutes") or 30))
    state = {"left": seconds, "snooze": 0, "paused": False}

    try:
        root = tk.Tk()
    except Exception as e:
        return f"弹窗失败：{e}"
    root.title("临期作业提醒")
    root.attributes("-topmost", True)
    root.resizable(True, True)

    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text=title, font=("Microsoft YaHei UI", 11, "bold"),
              foreground="#b71c1c", wraplength=560, justify="left").pack(anchor="w")

    tree = ttk.Treeview(frame, columns=("course", "title", "deadline", "left"),
                        show="headings", height=min(10, max(3, len(items))))
    for col, text, width in (("course", "课程", 150), ("title", "作业", 260),
                             ("deadline", "截止时间", 130), ("left", "剩余", 110)):
        tree.heading(col, text=text)
        tree.column(col, width=width, anchor="w" if col in ("course", "title") else "center")
    tree.tag_configure("soon", foreground="#e65100")
    for it in items:
        left = it["endMs"] - now_ms
        tree.insert("", "end", iid=it["key"],
                    values=(it["courseName"], it["title"],
                            core.fmt_datetime(it["endMs"]), _left_text(left)),
                    tags=("soon",) if left < core.DAY_MS else ())
    tree.pack(fill="both", expand=True, pady=(8, 8))

    bar = ttk.Frame(frame)
    bar.pack(fill="x")
    var_tip = tk.StringVar(value="")
    ttk.Label(bar, textvariable=var_tip, foreground="#666").pack(side="left")
    ttk.Button(bar, text="关闭", command=root.destroy).pack(side="right")
    btn_snooze = ttk.Button(bar, text=f"稍后提醒({snooze_min}分钟)")
    btn_snooze.pack(side="right", padx=(0, 6))

    def open_selected(_event=None):
        for iid in tree.selection():
            for it in items:
                if it["key"] == iid:
                    try:
                        import webbrowser
                        core.open_url(core.HOMEWORK_PAGE.format(courseId=it["courseId"]))
                    except Exception:
                        pass
                    break

    tree.bind("<Double-1>", open_selected)

    def snooze():
        if state["snooze"] >= 3:
            btn_snooze.state(["disabled"])
            return
        state["snooze"] += 1
        state["paused"] = True
        if state["snooze"] >= 3:
            btn_snooze.state(["disabled"])
        root.withdraw()
        root.after(snooze_min * 60000, wake)

    def wake():
        state["paused"] = False
        state["left"] = seconds
        root.deiconify()
        root.attributes("-topmost", True)
        root.lift()
        try:
            root.bell()
        except Exception:
            pass

    btn_snooze.config(command=snooze)

    def tick():
        if state["paused"]:
            root.after(1000, tick)
            return
        if seconds <= 0:
            var_tip.set("双击条目可打开作业页")
            return
        if state["left"] <= 0:
            root.destroy()
            return
        state["left"] -= 1
        var_tip.set(f"双击条目可打开作业页 · {state['left']} 秒后自动关闭")
        root.after(1000, tick)

    w, h = 700, min(560, 190 + 26 * len(items))
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{max(0, sw - w - 24)}+{max(0, sh - h - 80)}")
    root.lift()
    try:
        root.attributes("-topmost", True)
        root.bell()
    except Exception:
        pass
    if seconds > 0:
        tick()
    else:
        var_tip.set("双击条目可打开作业页")
    root.mainloop()
    return ""


# ==================== 提醒主流程 ====================
def _sample_items() -> list:
    now = int(time.time() * 1000)
    return [
        {"key": "sample-1", "title": "实验三报告（测试数据）", "kind": "todo", "state": 0,
         "rawState": "0", "endMs": now + 27 * 3600 * 1000, "courseId": "0",
         "courseName": "数据结构", "teacher": ""},
        {"key": "sample-2", "title": "第 4 次作业（测试数据）", "kind": "review", "state": 1,
         "rawState": "1", "endMs": now + 50 * 3600 * 1000, "courseId": "0",
         "courseName": "计算机网络", "teacher": ""},
    ]


def _in_quiet(cfg: dict, now_ms: int) -> bool:
    a, b = parse_hm(cfg.get("quiet_start")), parse_hm(cfg.get("quiet_end"))
    if not a or not b:
        return False
    dt = datetime.fromtimestamp(now_ms / 1000)
    cur, s, e = dt.hour * 60 + dt.minute, a[0] * 60 + a[1], b[0] * 60 + b[1]
    if s == e:
        return False
    return (s <= cur < e) if s < e else (cur >= s or cur < e)


# 最近一次发送时各通道的结果 [(通道名, 错误信息)]，给界面上的「立即测试通知」显示用
LAST_RESULTS: list = []


def _dispatch(title: str, text: str, lines: list, html: str,
              items: list, cfg: dict, now_ms: int) -> list:
    global LAST_RESULTS
    ch = cfg.get("channels") or {}
    results = []
    if ch.get("sound"):
        err = send_sound()
        results.append(("提示音", err))
    if ch.get("toast"):
        err = send_toast(title, lines, cfg.get("toast_app_name"), ensure_icon(cfg),
                         bool(cfg.get("toast_sticky", True)))
        results.append(("系统通知", err))
    if ch.get("wechat"):
        err = send_wechat(title, html, text, cfg)
        results.append(("微信推送", err))
    if ch.get("email"):
        err = send_email(title, text, html, cfg)
        results.append(("邮件", err))
    if ch.get("popup"):
        err = show_popup(title, items, cfg, now_ms)
        results.append(("弹窗", err))
    LAST_RESULTS = results
    return results


def run_once(test: bool = False) -> int:
    now_ms = int(time.time() * 1000)
    cfg = read_cfg()
    state = read_state()
    prefix = "【测试】" if test else ""

    if not cfg.get("enabled") and not test:
        log("提醒未启用，跳过（可在主程序里点「提醒设置」开启）")
        return 0

    items, err = fetch_items() if test else fetch_items_retry()
    if err and not test:
        log(f"读取失败：{err}")
        last = float(state.get("auth_ts") or 0)
        if now_ms - last > 12 * 3600 * 1000:
            title = f"{prefix}作业汇总助手 · 无法获取作业"
            text = f"{err}\n\n请打开作业汇总助手重新登录。"
            lines = [err, "请打开作业汇总助手重新登录"]
            for name, e in _dispatch(title, text, lines, f"<p>{_esc(err)}</p>", [], cfg, now_ms):
                log(f"  {name}：{'正常' if not e else e}")
            state["auth_ts"] = now_ms
            write_state(state)
        return 1
    if err:
        log(f"读取失败（测试模式改用示例数据）：{err}")

    state["auth_ts"] = 0
    live = window_items(items, cfg, now_ms)

    if test:
        use = live or _sample_items()
        if not live:
            log("本次没取到临期作业，改用测试数据演示通知效果")
        title, text, lines, html = build_message(use, cfg, now_ms, prefix)
        log(f"发送测试通知（{len(use)} 项）")
        for name, e in _dispatch(title, text, lines, html, use, cfg, now_ms):
            log(f"  {name}：{'正常' if not e else e}")
        return 0

    if _in_quiet(cfg, now_ms):
        urgent = [it for it in live
                  if it["endMs"] - now_ms <= int(cfg.get("quiet_ignore_within_hours") or 0) * 3600000]
        if not urgent:
            log(f"处于免打扰时段（{cfg['quiet_start']}-{cfg['quiet_end']}），跳过本次提醒")
            return 0
        live = urgent

    if not live:
        log("没有临期作业")
        state["items"] = {}
        state["last_run"] = now_ms
        write_state(state)
        return 0

    hs = thresholds(cfg)
    tracked = state.get("items") or {}
    due = []
    for it in live:
        stage = stage_of(it["endMs"] - now_ms, hs)
        if tracked.get(it["key"], {}).get("stage") != stage:
            due.append(it)

    digest = False
    if cfg.get("daily_digest"):
        today = datetime.fromtimestamp(now_ms / 1000).strftime("%Y-%m-%d")
        hhmm = parse_hm(cfg["daily_at"])
        now_hm = datetime.fromtimestamp(now_ms / 1000)
        if state.get("last_digest") != today and (now_hm.hour, now_hm.minute) >= hhmm:
            digest = True

    if not due and not digest:
        log(f"没有新的提醒（{len(live)} 项在提醒窗口内，档位未变化）")
        state["items"] = {it["key"]: tracked.get(it["key"], {}) for it in live}
        state["last_run"] = now_ms
        write_state(state)
        return 0

    gap = int(cfg.get("min_gap_minutes") or 0) * 60000
    if gap and now_ms - float(state.get("last_notify") or 0) < gap:
        log(f"距上次提醒不足 {cfg['min_gap_minutes']} 分钟，本次跳过")
        return 0

    use = live if digest else due
    reason = "每日汇总" if digest else f"档位变化（{len(due)} 项）"
    title, text, lines, html = build_message(use, cfg, now_ms, prefix)
    log(f"发送提醒（{reason}，{len(use)} 项）：{title}")
    ok_any = False
    for name, e in _dispatch(title, text, lines, html, use, cfg, now_ms):
        log(f"  {name}：{'正常' if not e else e}")
        if not e:
            ok_any = True

    if ok_any:
        for it in use:
            tracked[it["key"]] = {"stage": stage_of(it["endMs"] - now_ms, hs), "ts": now_ms}
        state["items"] = {it["key"]: tracked.get(it["key"], {}) for it in live}
        state["last_notify"] = now_ms
        if digest:
            state["last_digest"] = datetime.fromtimestamp(now_ms / 1000).strftime("%Y-%m-%d")
    state["last_run"] = now_ms
    write_state(state)
    return 0


# ==================== 计划任务 ====================
def _pythonw() -> Path:
    """无控制台窗口的解释器 / 可执行文件。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    cand = BASE / ".venv" / "Scripts" / "pythonw.exe"
    if cand.exists():
        return cand
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        return exe.parent / "pythonw.exe"
    return exe


def _task_command() -> str:
    """计划任务要执行的命令行：打包后直接调 exe，源码运行时调 pythonw + notify.py。"""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable)}" --run'
    return f'"{_pythonw()}" "{BASE / "notify.py"}" --run'


def active_hours(cfg: dict) -> int:
    """计划任务每天持续跑多久：从「每天几点起」一直排到免打扰开始。

    原来是界面上一个「持续 N 小时」的输入框，但用户根本不会去想这个概念，
    而且它跟免打扰时段本来就该是一致的，所以改成自动推算。
    """
    start = parse_hm(cfg.get("daily_at"))
    quiet = parse_hm(cfg.get("quiet_start"))
    if start and quiet:
        span = (quiet[0] * 60 + quiet[1]) - (start[0] * 60 + start[1])
        if 1 <= span <= 24 * 60:
            return max(1, min(24, round(span / 60)))
    return 14


def _fix_task_settings() -> str:
    """把计划任务的默认设置改成适合提醒用的，返回警告信息（空串=没问题）。

    schtasks 命令行建出来的任务默认带这两个开关：
        DisallowStartIfOnBatteries = true
        StopIfGoingOnBatteries     = true
    也就是「用电池时根本不跑」——笔记本一拔电源提醒就没了，必须改掉。
    顺便打开 StartWhenAvailable：错过了（比如当时关着机）之后一开机就补跑一次。
    """
    if not os.path.exists(PS5):
        return "找不到 Windows PowerShell，无法调整任务设置"
    ps = ("$ErrorActionPreference='Stop';"
          "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
          "-DontStopIfGoingOnBatteries -StartWhenAvailable "
          "-ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew;"
          f"Set-ScheduledTask -TaskName '{TASK_NAME}' -Settings $s | Out-Null")
    enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    rc, out = _run([PS5, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                   timeout=40, merge=False)
    if rc != 0:
        return f"任务设置没能改（{out[:120] or rc}），用电池时可能不会提醒"
    return ""


def install_task(cfg: dict | None = None) -> tuple[bool, str]:
    cfg = cfg or read_cfg()
    if os.name != "nt":
        return False, "仅支持 Windows"
    hm = parse_hm(cfg.get("daily_at")) or (8, 30)
    interval = max(5, int(cfg.get("interval_hours") or 3) * 60)
    active = active_hours(cfg)
    tr = _task_command()
    st = f"{hm[0]:02d}:{hm[1]:02d}"
    # Windows 要求 /DU（持续时长）必须**严格大于** /RI（重复间隔）。
    # 而「检查间隔」上限是 24h、活跃窗口由「每天几点」到「免打扰开始」推算，
    # 两者一撞（比如间隔 24h、窗口 15h）就构造不出合法组合，
    # schtasks 会直接报「持续时间 (900m) 必须大于重复间隔时间 (1440m)」。
    # 而且间隔 ≥ 窗口时，一天本来也只会在起始点跑一次，所以直接用「每天一次」。
    if interval >= active * 60:
        args = ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/TR", tr,
                "/SC", "DAILY", "/ST", st]
        once_a_day = True
    else:
        args = ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/TR", tr,
                "/SC", "DAILY", "/ST", st,
                "/RI", str(interval), "/DU", f"{active}:00"]
        once_a_day = False
    rc, out = _run(args, timeout=30, encoding="mbcs")
    if rc != 0:
        return False, f"注册计划任务失败（{rc}）：{out[:300]}"
    warn = _fix_task_settings()
    if once_a_day:
        msg = f"计划任务已注册：每天 {st} 检查一次"
        if interval // 60 > 1:
            msg += f"（检查间隔 {interval // 60} 小时 ≥ 活跃时段 {active} 小时，只能每天跑一次）"
    else:
        msg = f"计划任务已注册：每天 {st} 起，每 {interval // 60} 小时检查一次"
    if warn:
        msg += f"（注意：{warn}）"
    return True, msg


def uninstall_task() -> tuple[bool, str]:
    rc, out = _run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], timeout=30, encoding="mbcs")
    if rc == 0:
        return True, "计划任务已删除"
    if _task_unreachable(out):
        # 原来这里把任何报错都当成「任务本来就不存在」，等于假装删成功了
        return False, f"删除计划任务失败：{out.strip()[:160]}"
    if "cannot find" in out.lower() or "找不到" in out or "不存在" in out:
        return True, "计划任务本来就不存在"
    return False, f"删除计划任务失败（{rc}）：{out[:300]}"


def _task_unreachable(out: str) -> bool:
    """schtasks 是不是因为环境问题压根没跑起来（而不是「任务不存在」）。

    任务不存在时 schtasks 说的是「找不到指定的**文件**」；而进程拿不到计划任务
    服务时会说「找不到指定的**路径**」——两者必须分开，否则会把「查询失败」
    误报成「未注册」，把「删除失败」误报成「本来就不存在」。
    """
    low = (out or "").lower()
    return "path specified" in low or "指定的路径" in (out or "")


def task_info() -> str:
    if os.name != "nt":
        return "非 Windows 系统"
    rc, out = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
                   timeout=20, encoding="mbcs")
    if rc != 0:
        if _task_unreachable(out):
            return "查询失败（当前环境拿不到计划任务信息）"
        return "未注册"
    info = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    nxt = info.get("Next Run Time") or info.get("下次运行时间") or ""
    status = info.get("Status") or info.get("状态") or ""
    return f"已注册{' · 下次运行 ' + nxt if nxt else ''}{' · ' + status if status else ''}"


def enable(cfg: dict) -> tuple[bool, str]:
    ok, msg = install_task(cfg)
    if ok:
        cfg["enabled"] = True
        write_cfg(cfg)
    return ok, msg


def disable(cfg: dict) -> tuple[bool, str]:
    ok, msg = uninstall_task()
    if ok:
        cfg["enabled"] = False
        write_cfg(cfg)
    return ok, msg


# ==================== 图形设置 ====================
def open_settings_dialog(parent=None, base_dir=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    owns_root = parent is None
    root = tk.Tk() if owns_root else tk.Toplevel(parent)
    root.title("提醒设置 · 作业汇总助手")
    root.resizable(False, False)
    core.apply_theme(root)                      # 勾选框用对勾，不用 clam 自带的叉
    if not owns_root:
        root.transient(parent)
        root.grab_set()

    cfg = read_cfg()
    ch = cfg["channels"]
    var = {
        "toast_app_name": tk.StringVar(value=cfg["toast_app_name"]),
        "toast_icon": tk.StringVar(value=cfg["toast_icon"]),
        "toast_sticky": tk.BooleanVar(value=bool(cfg["toast_sticky"])),
        "days_before": tk.StringVar(value=str(cfg["days_before"])),
        "daily_at": tk.StringVar(value=cfg["daily_at"]),
        "interval_hours": tk.StringVar(value=str(cfg["interval_hours"])),
        "quiet_start": tk.StringVar(value=cfg["quiet_start"]),
        "quiet_end": tk.StringVar(value=cfg["quiet_end"]),
        "daily_digest": tk.BooleanVar(value=bool(cfg["daily_digest"])),
        # 「临近时再提醒我」用三个勾选框代替原来那串「24,6,1（逗号分隔）」
        "rep_24": tk.BooleanVar(value=24 in cfg["repeat_hours"]),
        "rep_6": tk.BooleanVar(value=6 in cfg["repeat_hours"]),
        "rep_1": tk.BooleanVar(value=1 in cfg["repeat_hours"]),
        "m_preset": tk.StringVar(value=detect_mail_preset(cfg["email"])),
        "mail_tip": tk.StringVar(value=""),
        "wx_provider": tk.StringVar(value=cfg["wechat"]["provider"]),
        "wx_token": tk.StringVar(value=cfg["wechat"]["token"]),
        "wx_topic": tk.StringVar(value=cfg["wechat"]["topic"]),
        "wx_tip": tk.StringVar(value=""),
        "wx_field": tk.StringVar(value="Webhook"),
        "wx_topic_label": tk.StringVar(value=""),
        "m_host": tk.StringVar(value=cfg["email"]["host"]),
        "m_port": tk.StringVar(value=str(cfg["email"]["port"])),
        "m_ssl": tk.BooleanVar(value=bool(cfg["email"]["ssl"])),
        "m_user": tk.StringVar(value=cfg["email"]["user"]),
        "m_pwd": tk.StringVar(value=cfg["email"]["password"]),
        "m_to": tk.StringVar(value=cfg["email"]["to"]),
        "status": tk.StringVar(value=""),
    }
    ch_var = {k: tk.BooleanVar(value=bool(ch.get(k))) for k in CHANNEL_LABELS}

    outer = ttk.Frame(root, padding=12)
    outer.pack(fill="both", expand=True)

    # —— 定时 ——
    # 布局原则：左边一列固定宽度的短标签，右边放控件，一眼能扫完。
    # 原来把 4 个输入框塞进一句话里（"提前 [3] 天开始提醒 每天 [08:30] 起，每 [3] 小时
    # 检查一次，持续 [14] 小时"），读起来很费劲，所以拆成一行一件事。
    box1 = ttk.LabelFrame(outer, text="什么时候提醒", padding=(12, 8))
    box1.pack(fill="x")
    box1.columnconfigure(1, weight=1)

    def _row(r, label):
        ttk.Label(box1, text=label, width=7, anchor="e").grid(
            row=r, column=0, sticky="e", padx=(0, 10), pady=3)
        f = ttk.Frame(box1)
        f.grid(row=r, column=1, sticky="w")
        return f

    f = _row(0, "提前")
    ttk.Spinbox(f, from_=1, to=60, width=4, textvariable=var["days_before"]).pack(side="left")
    ttk.Label(f, text="天开始提醒").pack(side="left", padx=(6, 0))

    f = _row(1, "检查")
    ttk.Label(f, text="每天").pack(side="left")
    ttk.Entry(f, width=7, textvariable=var["daily_at"]).pack(side="left", padx=4)
    ttk.Label(f, text="起，每").pack(side="left")
    ttk.Spinbox(f, from_=1, to=24, width=4, textvariable=var["interval_hours"]).pack(side="left", padx=4)
    ttk.Label(f, text="小时看一次").pack(side="left")

    f = _row(2, "免打扰")
    ttk.Entry(f, width=7, textvariable=var["quiet_start"]).pack(side="left")
    ttk.Label(f, text="到").pack(side="left", padx=4)
    ttk.Entry(f, width=7, textvariable=var["quiet_end"]).pack(side="left")
    ttk.Label(f, foreground="#777", text="（这段时间不打扰，除非作业快截止了）").pack(side="left", padx=(6, 0))

    f = _row(3, "临近时")
    ttk.Label(f, text="再提醒我：").pack(side="left")
    ttk.Checkbutton(f, text="剩 24 小时", variable=var["rep_24"]).pack(side="left", padx=(6, 0))
    ttk.Checkbutton(f, text="剩 6 小时", variable=var["rep_6"]).pack(side="left", padx=(6, 0))
    ttk.Checkbutton(f, text="剩 1 小时", variable=var["rep_1"]).pack(side="left", padx=(6, 0))

    f = _row(4, "汇总")
    ttk.Checkbutton(f, text="每天一次，把这段时间要交的作业汇总成一条通知",
                    variable=var["daily_digest"]).pack(side="left")

    # —— 通道 ——
    box2 = ttk.LabelFrame(outer, text="用什么方式提醒", padding=(10, 6))
    box2.pack(fill="x", pady=(8, 0))
    row = ttk.Frame(box2)
    row.pack(fill="x")
    for i, (key, label) in enumerate(CHANNEL_LABELS.items()):
        ttk.Checkbutton(row, text=label, variable=ch_var[key]).grid(
            row=i // 3, column=i % 3, sticky="w", padx=(0, 18), pady=2)
    for col in range(3):
        row.columnconfigure(col, weight=1)
    r = ttk.Frame(box2)
    r.pack(fill="x", pady=(4, 0))
    ttk.Label(r, text="系统通知里显示的名字").pack(side="left")
    ttk.Entry(r, width=22, textvariable=var["toast_app_name"]).pack(side="left", padx=4)
    ttk.Label(r, foreground="#777", text="（改完发一次通知就生效）").pack(side="left")
    r = ttk.Frame(box2)
    r.pack(fill="x", pady=(2, 0))
    ttk.Label(r, text="通知图标").pack(side="left")
    ttk.Entry(r, width=46, textvariable=var["toast_icon"]).pack(side="left", padx=4)
    ttk.Button(r, text="浏览…", command=lambda: _pick_icon()).pack(side="left")
    ttk.Label(r, foreground="#777", text="留空 = 项目目录下的 通知图标.png").pack(side="left", padx=4)
    ttk.Checkbutton(box2, text="提醒式通知：不点掉就一直停在屏幕上（免得一晃眼错过）",
                    variable=var["toast_sticky"]).pack(anchor="w", pady=(4, 0))

    # —— 推送 ——
    box3 = ttk.LabelFrame(outer, text="推送到手机（人不在电脑旁也能收到）", padding=(10, 6))
    box3.pack(fill="x", pady=(8, 0))
    r = ttk.Frame(box3)
    r.pack(fill="x")
    ttk.Label(r, text="服务").pack(side="left")
    cb_wx = ttk.Combobox(r, width=11, state="readonly", textvariable=var["wx_provider"],
                         values=tuple(WX_SITES))
    cb_wx.pack(side="left", padx=4)
    cb_wx.bind("<<ComboboxSelected>>", lambda e: _on_wx_provider())
    ttk.Button(r, text="打开配置页面 ↗", command=lambda: _open_wx_site()).pack(side="left", padx=4)
    ttk.Label(r, foreground="#777", textvariable=var["wx_tip"]).pack(side="left", padx=4)
    r = ttk.Frame(box3)
    r.pack(fill="x", pady=(4, 0))
    ttk.Label(r, textvariable=var["wx_field"], width=10, anchor="e").pack(side="left")
    ttk.Entry(r, width=52, textvariable=var["wx_token"]).pack(side="left", padx=4)
    ttk.Label(r, textvariable=var["wx_topic_label"]).pack(side="left")
    ttk.Entry(r, width=10, textvariable=var["wx_topic"]).pack(side="left", padx=4)

    # —— 邮件 ——
    box4 = ttk.LabelFrame(outer, text="邮件", padding=(10, 6))
    box4.pack(fill="x", pady=(8, 0))
    r = ttk.Frame(box4)
    r.pack(fill="x")
    ttk.Label(r, text="邮箱").pack(side="left")
    cb_mail = ttk.Combobox(r, width=10, state="readonly", textvariable=var["m_preset"],
                           values=tuple(MAIL_PRESETS))
    cb_mail.pack(side="left", padx=4)
    cb_mail.bind("<<ComboboxSelected>>", lambda e: _on_mail_preset())
    ttk.Button(r, text="获取授权码 ↗", command=lambda: _open_mail_site()).pack(side="left", padx=4)
    ttk.Label(r, foreground="#777", textvariable=var["mail_tip"]).pack(side="left", padx=4)

    r = ttk.Frame(box4)
    r.pack(fill="x", pady=(4, 0))
    ttk.Label(r, text="服务器").pack(side="left")
    ttk.Entry(r, width=20, textvariable=var["m_host"]).pack(side="left", padx=4)
    ttk.Label(r, text="端口").pack(side="left")
    ttk.Entry(r, width=7, textvariable=var["m_port"]).pack(side="left", padx=4)
    ttk.Checkbutton(r, text="SSL", variable=var["m_ssl"]).pack(side="left", padx=4)

    r = ttk.Frame(box4)
    r.pack(fill="x", pady=(4, 0))
    ttk.Label(r, text="账号").pack(side="left")
    ttk.Entry(r, width=22, textvariable=var["m_user"]).pack(side="left", padx=4)
    ttk.Label(r, text="授权码").pack(side="left")
    ttk.Entry(r, width=16, show="*", textvariable=var["m_pwd"]).pack(side="left", padx=4)
    ttk.Label(r, text="收件人").pack(side="left")
    ttk.Entry(r, width=20, textvariable=var["m_to"]).pack(side="left", padx=4)

    # —— 状态 + 按钮 ——
    ttk.Label(outer, textvariable=var["status"], foreground="#0a6", wraplength=640,
              justify="left").pack(anchor="w", pady=(10, 0))
    bar = ttk.Frame(outer)
    bar.pack(fill="x", pady=(8, 0))
    btn_test = ttk.Button(bar, text="立即测试通知")
    btn_test.pack(side="left")
    ttk.Button(bar, text="关闭", command=root.destroy).pack(side="right")
    btn_disable = ttk.Button(bar, text="停用提醒")
    btn_disable.pack(side="right", padx=6)
    btn_save = ttk.Button(bar, text="保存并启用")
    btn_save.pack(side="right")

    def _pick_icon():
        p = filedialog.askopenfilename(parent=root, title="选择通知图标",
                                       filetypes=[("图片", "*.png *.jpg *.jpeg *.ico"),
                                                  ("所有文件", "*.*")])
        if p:
            var["toast_icon"].set(p)

    def _on_wx_provider():
        """换推送服务商时，更新「填什么」和提示；群组编码只对 PushPlus 有意义。"""
        _url, field, tip = WX_SITES.get(var["wx_provider"].get(), WX_SITES["自定义"])
        var["wx_field"].set(field)
        var["wx_tip"].set(tip)
        var["wx_topic_label"].set("群组编码(可空)" if var["wx_provider"].get() == "pushplus" else "")

    def _open_wx_site():
        name = var["wx_provider"].get()
        url, _field, tip = WX_SITES.get(name, WX_SITES["自定义"])
        if not url:
            var["status"].set(f"「{name}」没有可跳转的页面 —— {tip}")
            return
        core.open_url(url)
        var["status"].set(f"已打开 {url} —— {tip}" + _browser_hint())

    def _browser_hint() -> str:
        """有提权浏览器卡着时，提前告诉用户会弹什么、怎么处理。"""
        try:
            if core.elevated_browsers():
                return ("\n（提示：检测到有以管理员权限运行的浏览器，Edge 可能会弹"
                        "「现有实例正在以提升的权限运行」—— 把那个浏览器关掉再试）")
        except Exception:
            pass
        return ""

    def _on_mail_preset():
        """换邮箱服务商时，顺手把服务器/端口/SSL 填好，省得记。"""
        host, port, ssl, _url, tip = MAIL_PRESETS[var["m_preset"].get()]
        if host:
            var["m_host"].set(host)
            var["m_port"].set(str(port))
            var["m_ssl"].set(ssl)
        var["mail_tip"].set(tip)

    def _open_mail_site():
        name = var["m_preset"].get()
        host, _port, _ssl, url, tip = MAIL_PRESETS.get(name, MAIL_PRESETS["自定义"])
        if not url:
            var["status"].set(f"「{name}」没有可跳转的页面 —— {tip}")
            return
        try:
            core.open_url(url)
            var["status"].set(f"已打开 {url} —— {tip}" + _browser_hint())
        except Exception as e:
            var["status"].set(f"打开浏览器失败：{e}，请手动访问 {url}")

    def _collect() -> dict:
        cfg["toast_app_name"] = var["toast_app_name"].get().strip() or DEFAULT["toast_app_name"]
        cfg["toast_icon"] = var["toast_icon"].get().strip()
        cfg["toast_sticky"] = bool(var["toast_sticky"].get())
        cfg["days_before"] = _int(var["days_before"].get(), 3, 1, 60)
        cfg["daily_at"] = var["daily_at"].get().strip() or "08:30"
        cfg["interval_hours"] = _int(var["interval_hours"].get(), 3, 1, 24)
        cfg["quiet_start"] = var["quiet_start"].get().strip()
        cfg["quiet_end"] = var["quiet_end"].get().strip()
        cfg["daily_digest"] = bool(var["daily_digest"].get())
        cfg["repeat_hours"] = [h for h, k in ((24, "rep_24"), (6, "rep_6"), (1, "rep_1"))
                               if var[k].get()]
        for k, v in ch_var.items():
            cfg["channels"][k] = bool(v.get())
        cfg["wechat"] = {"provider": var["wx_provider"].get(), "token": var["wx_token"].get().strip(),
                         "topic": var["wx_topic"].get().strip()}
        cfg["email"] = {"preset": var["m_preset"].get(),
                        "host": var["m_host"].get().strip(),
                        "port": _int(var["m_port"].get(), 465, 1, 65535),
                        "ssl": bool(var["m_ssl"].get()),
                        "user": var["m_user"].get().strip(),
                        "password": var["m_pwd"].get(),
                        "to": var["m_to"].get().strip()}
        return cfg

    def _persist() -> bool:
        """只把设置写盘，不碰计划任务、也不改启用状态。

        测试通知走这里：原来它调 _save(True) 会顺带去注册计划任务，
        注册一旦失败（比如参数不合法）就弹错误框，把「测一下能不能收到」
        这件事给挡住了——测试通知根本不该关心计划任务。
        """
        data = _collect()
        if not parse_hm(data["daily_at"]):
            messagebox.showerror("提醒设置", "「每天」时间格式应为 HH:MM，例如 08:30", parent=root)
            return False
        write_cfg(data)
        return True

    def _save(enable_it=True):
        if not _persist():
            return
        data = read_cfg()
        if enable_it:
            ok, msg = install_task(data)
            if ok:
                data["enabled"] = True
            # 注册失败时**保持原值**：计划任务可能本来就注册着、好好跑着，
            # 不能因为这一次注册失败（比如被安全软件挡了）就把提醒悄悄关掉。
            write_cfg(data)
            var["status"].set(("✅ " if ok else "❌ ") + msg)
            if not ok:
                messagebox.showerror(
                    "提醒设置",
                    msg + "\n\n提醒的启用状态没有改动。可以跑一次自检看看卡在哪：\n"
                          "作业汇总助手.exe --diag", parent=root)
        else:
            var["status"].set("已保存（提醒未启用）")

    def _on_save():
        _save(True)

    def _on_disable():
        ok, msg = uninstall_task()
        _collect()
        if ok:
            cfg["enabled"] = False          # 删失败就别谎报「已停用」
        write_cfg(cfg)
        var["status"].set(("✅ " if ok else "❌ ") + msg)

    def _on_test():
        # 只保存设置，不去注册计划任务——测试通知不该被计划任务的问题挡住
        if not _persist():
            return
        btn_test.state(["disabled"])
        var["status"].set("正在发送测试通知…")

        def work():
            try:
                run_once(test=True)
                # 把每个通道的真实结果列出来——原来不管成没成都只说「已发出」，
                # 出了问题根本看不出是哪一步挂的
                rows = [f"{name}：{err or '正常'}" for name, err in LAST_RESULTS]
                ok = any(not err for _n, err in LAST_RESULTS)
                head = "✅ 测试完成，能看到通知就算成功：" if ok else "❌ 所有通道都没发出去："
                msg = head + "\n" + ("\n".join(rows) if rows else "（一个通道都没开，去上面勾一个）")
            except Exception as e:
                msg = f"测试失败：{e}"
            root.after(0, lambda: (btn_test.state(["!disabled"]), var["status"].set(msg)))

        import threading
        threading.Thread(target=work, daemon=True).start()

    btn_save.config(command=_on_save)
    btn_disable.config(command=_on_disable)
    btn_test.config(command=_on_test)

    var["status"].set(f"当前状态：{'已启用' if cfg.get('enabled') else '未启用'} · 计划任务 {task_info()}")
    var["mail_tip"].set(MAIL_PRESETS[var["m_preset"].get()][4])
    # 选了预设但服务器栏还是空的（第一次用最容易踩），自动补上，省得用户自己去查
    if not var["m_host"].get().strip():
        _p = MAIL_PRESETS.get(var["m_preset"].get())
        if _p and _p[0]:
            var["m_host"].set(_p[0])
            var["m_port"].set(str(_p[1]))
            var["m_ssl"].set(_p[2])
    _on_wx_provider()
    root.update_idletasks()
    root.geometry(f"+{max(0, (root.winfo_screenwidth() - root.winfo_reqwidth()) // 2)}"
                  f"+{max(0, (root.winfo_screenheight() - root.winfo_reqheight()) // 3)}")
    if owns_root:
        root.mainloop()
    return root


def _int(text, default, lo, hi):
    try:
        return max(lo, min(hi, int(str(text).strip())))
    except (TypeError, ValueError):
        return default


# ==================== 命令行 ====================
def cmd_diag() -> int:
    """排查用：把「提醒功能到底卡在哪」一次性列清楚。

    最常见的是「计划任务注册失败：系统找不到指定的路径」——那说明当前进程
    拿不到计划任务服务（多半是被安全软件 / 受限环境挡住了），不是配置问题。
    """
    import tempfile

    out = []
    out.append(f"程序目录：{BASE}")
    out.append(f"运行方式：{'打包的 exe' if getattr(sys, 'frozen', False) else '源码（Python）'}")
    elevated = core.is_elevated()
    out.append(f"运行权限：{'管理员（不推荐）' if elevated else '普通用户'}")
    if elevated:
        out.append("  → 提醒功能不需要管理员权限。提权运行时点「打开作业页 / 打开配置页面」，")
        out.append("    浏览器会跟着带上管理员权限，Edge / Chrome 会弹「现有实例正在以提升的")
        out.append("    权限运行」。建议：右键快捷方式 → 属性 → 兼容性 → 取消「以管理员身份运行」。")
    out.append(f"系统临时目录：{tempfile.gettempdir()}")
    browsers = core.elevated_browsers()
    if browsers:
        names = "、".join(f"{n}(PID {p})" for p, n in browsers)
        out.append(f"⚠️ 检测到以管理员权限运行的浏览器：{names}")
        out.append("  → 这就是点「打开配置页面」时 Edge 弹「现有实例正在以提升的权限运行」")
        out.append("    的原因：普通权限的进程没法把链接交给一个提权的浏览器实例。")
        out.append("    把这个浏览器窗口整个关掉（任务管理器结束 msedge.exe）再重开即可。")
    out.append("")
    out.append(f"配置文件：{NOTIFY_FILE}（{'有' if NOTIFY_FILE.exists() else '没有'}）")
    acc = BASE / "account.json"
    out.append(f"登录凭据：{acc}（{'有' if acc.exists() else '没有'}）")
    out.append(f"提醒日志：{LOG_FILE}（{'有' if LOG_FILE.exists() else '没有'}）")

    cfg = read_cfg()
    out.append("")
    out.append(f"提醒是否启用：{'是' if cfg.get('enabled') else '否'}")
    out.append(f"当前设置：提前 {cfg['days_before']} 天，每天 {cfg['daily_at']} 起每 "
               f"{cfg['interval_hours']} 小时看一次，持续 {active_hours(cfg)} 小时")
    used = [lbl for k, lbl in CHANNEL_LABELS.items() if cfg["channels"].get(k)]
    out.append("通知方式：" + ("、".join(used) if used else "（无）"))

    out.append("")
    out.append("—— 计划任务服务能不能用 ——")
    rc, text = _run(["schtasks", "/Query", "/FO", "LIST"], timeout=20, encoding="mbcs")
    if rc == 0:
        out.append("schtasks：可用")
    else:
        out.append(f"schtasks：不可用（{text.strip()[:100]}）")
        out.append("  → 如果是「找不到指定的路径」，说明当前进程被安全软件或受限环境")
        out.append("    挡住了计划任务服务。换一个方式启动本程序（直接双击 bat / exe，")
        out.append("    不要从某个终端或沙箱里启动）再试。")
    rc2, text2 = _run([PS5, "-NoProfile", "-NonInteractive", "-Command",
                       "(Get-ScheduledTask | Measure-Object).Count"],
                      timeout=40, encoding="utf-8", merge=False)
    out.append(f"PowerShell 计划任务模块：{'可用（共 ' + text2.strip() + ' 个任务）' if rc2 == 0 else '不可用'}")
    out.append(f"本程序的任务：{task_info()}")

    out.append("")
    out.append("—— 各通道自检 ——")
    if cfg["channels"].get("toast"):
        out.append(f"系统通知：{send_toast('周一天女仆 · 自检', ['能看到这条说明系统通知正常'], cfg['toast_app_name'], ensure_icon(cfg)) or '正常'}")
    if cfg["channels"].get("wechat"):
        err = send_wechat("周一天女仆 · 自检", "<p>微信推送自检</p>", "微信推送自检", cfg)
        out.append(f"微信推送：{err or '正常'}")

    text = "\n".join(out)
    log("自检结果：\n" + text, quiet=True)      # 只进日志文件，别在控制台重复打一遍
    report = BASE / "自检报告.txt"
    try:
        report.write_text(text, encoding="utf-8")
        text += f"\n\n（以上内容已存到 {report}）"
    except OSError:
        pass
    if sys.stdout is None:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("作业汇总助手 · 自检", text)
            root.destroy()
        except Exception:
            pass
    else:
        print(text)
    return 0


def cmd_status() -> int:
    """打印当前配置与计划任务状态。

    打包成无控制台的 exe 后 sys.stdout 是 None，这时改用弹窗展示。
    """
    cfg = read_cfg()
    out = []
    out.append(f"配置文件：{NOTIFY_FILE}")
    out.append(f"提醒记录：{STATE_FILE}")
    out.append(f"提醒日志：{LOG_FILE}")
    out.append(f"计划任务：{TASK_NAME} -> {task_info()}")
    out.append(f"是否启用：{'是' if cfg.get('enabled') else '否'}")
    out.append(f"提前提醒：{cfg['days_before']} 天；每天 {cfg['daily_at']} 起每 {cfg['interval_hours']} 小时检查，"
               f"持续 {active_hours(cfg)} 小时")
    out.append(f"免打扰：{cfg['quiet_start'] or '无'}-{cfg['quiet_end'] or '无'}"
               f"（剩余 {cfg['quiet_ignore_within_hours']} 小时内仍提醒）")
    out.append(f"额外节点：剩余不足 {cfg['repeat_hours']} 小时；"
               f"每日汇总：{'开' if cfg['daily_digest'] else '关'}")
    used = [lbl for k, lbl in CHANNEL_LABELS.items() if cfg["channels"].get(k)]
    out.append("通知方式：" + ("、".join(used) if used else "（无，请到提醒设置里勾选）"))
    if cfg["channels"].get("toast"):
        out.append(f"  系统通知来源名：{cfg['toast_app_name']}"
                   f"（图标 {cfg['toast_icon'] or default_icon_path()}）")
    if cfg["channels"].get("wechat"):
        out.append(f"  微信：{cfg['wechat']['provider']} token "
                   f"{'已配置' if cfg['wechat']['token'] else '未配置'}")
    if cfg["channels"].get("email"):
        out.append(f"  邮件：{cfg['email']['host'] or '（未填服务器）'} -> "
                   f"{cfg['email']['to'] or '未填收件人'}")
    st = read_state()
    out.append(f"上次运行：{datetime.fromtimestamp(st['last_run'] / 1000).strftime('%Y-%m-%d %H:%M:%S')}"
               if st.get("last_run") else "上次运行：从未")
    out.append(f"上次提醒：{datetime.fromtimestamp(st['last_notify'] / 1000).strftime('%Y-%m-%d %H:%M:%S')}"
               if st.get("last_notify") else "上次提醒：从未")

    text = "\n".join(out)
    if sys.stdout is None:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("作业汇总助手 · 提醒状态", text)
            root.destroy()
        except Exception:
            pass
    else:
        print(text)
    return 0


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    cmd = args[0] if args else "--run"
    try:
        if cmd in ("--run", "-r"):
            return run_once(test=False)
        if cmd == "--test":
            return run_once(test=True)
        if cmd == "--install":
            ok, msg = enable(read_cfg())
            print(msg)
            return 0 if ok else 1
        if cmd == "--uninstall":
            ok, msg = disable(read_cfg())
            print(msg)
            return 0 if ok else 1
        if cmd == "--status":
            return cmd_status()
        if cmd == "--diag":
            return cmd_diag()
        if cmd == "--settings":
            open_settings_dialog()
            return 0
        if cmd == "--reset":
            write_state({})
            print("提醒记录已清空")
            return 0
        print(__doc__)
        return 0
    except Exception:
        log("运行出错：\n" + traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
