# -*- coding: utf-8 -*-
"""作业汇总助手 · 优学院桌面版：汇总待提交/待互评作业，账号本地加密保存，自动登录。"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import requests
import tkinter as tk
from tkinter import messagebox, ttk

VERSION = "1.0.0"
LMS = "https://lms.dgut.edu.cn"
LOGIN_URL = f"{LMS}/courseapi/users/login/v2"
IDENTITY_URL = "https://application.dgut.edu.cn/classroomapi/users/getUserInfo"
COURSES_URL = f"{LMS}/courseapi/courses/students"
HOMEWORKS_URL = f"{LMS}/courseapi/homeworks/student/v2"
HOMEWORK_PAGE = LMS + "/courseweb/ulearning/index.html#/course/homework?courseId={}"
LOGIN_ORIGIN = "https://application.dgut.edu.cn"
LOGIN_REFERER = "https://application.dgut.edu.cn/application"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
DAY_MS = 86400000

BASE_DIR = None  # 见下方 _app_dir()


def _app_dir() -> Path:
    """程序所在目录。

    打包成 exe 后 `__file__` 指向 PyInstaller 的临时解包目录（退出即删），
    所以配置必须放在 exe 旁边，用 sys.executable 定位。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _app_dir()
ACCOUNT_FILE = "account.json"
CONFIG_FILE = "config.json"

DEFAULT_CFG = {
    "days": 14,
    "exclude": "",
    "show_expired": True,
    "auto_refresh": True,
    "hidden": [],
    "tab": "todo",
    "geometry": "",
}


class ApiError(RuntimeError):
    def __init__(self, message: str, auth: bool = False):
        super().__init__(message)
        self.auth = auth


# ==================== DPAPI 加密（仅当前 Windows 账户可解） ====================
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]


def _crypt_func(name: str):
    fn = getattr(ctypes.windll.crypt32, name)
    fn.argtypes = [ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(_DATA_BLOB),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DATA_BLOB)]
    fn.restype = wintypes.BOOL
    return fn


_protect_fn = None
_unprotect_fn = None


def _get_protect():
    global _protect_fn
    if _protect_fn is None:
        _protect_fn = _crypt_func("CryptProtectData")
    return _protect_fn


def _get_unprotect():
    global _unprotect_fn
    if _unprotect_fn is None:
        _unprotect_fn = _crypt_func("CryptUnprotectData")
    return _unprotect_fn


_local_free = ctypes.windll.kernel32.LocalFree
_local_free.argtypes = [ctypes.c_void_p]
_local_free.restype = ctypes.c_void_p


def dpapi_protect(text: str) -> str:
    raw = text.encode("utf-8")
    buf = ctypes.create_string_buffer(raw)
    inp = _DATA_BLOB(len(raw), ctypes.addressof(buf))
    out = _DATA_BLOB()
    if not _get_protect()(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return base64.b64encode(ctypes.string_at(out.pbData, out.cbData)).decode("ascii")
    finally:
        _local_free(out.pbData)


def dpapi_unprotect(b64: str) -> str:
    raw = base64.b64decode(b64)
    buf = ctypes.create_string_buffer(raw)
    inp = _DATA_BLOB(len(raw), ctypes.addressof(buf))
    out = _DATA_BLOB()
    if not _get_unprotect()(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        _local_free(out.pbData)


# ==================== 本地存储 ====================
def read_account(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = {"username": str(data.get("username", "")), "password": "", "token": ""}
    for field in ("password", "token"):
        secret = data.get(field)
        if isinstance(secret, str) and secret:
            try:
                out[field] = dpapi_unprotect(secret)
            except Exception:
                out[field] = ""
    return out


def write_account(path: Path, username: str, password: str, token: str) -> None:
    data = {"username": username, "password": "", "token": ""}
    if password:
        data["password"] = dpapi_protect(password)
    if token:
        data["token"] = dpapi_protect(token)
    _atomic_write_json(path, data)


def read_cfg(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    cfg = dict(DEFAULT_CFG)
    cfg.update({k: v for k, v in data.items() if k in DEFAULT_CFG})
    if not isinstance(cfg["hidden"], list):
        cfg["hidden"] = []
    try:
        cfg["days"] = max(0, int(cfg["days"]))
    except (TypeError, ValueError):
        cfg["days"] = DEFAULT_CFG["days"]
    return cfg


def write_cfg(path: Path, cfg: dict) -> None:
    _atomic_write_json(path, {k: cfg.get(k, DEFAULT_CFG[k]) for k in DEFAULT_CFG})


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ==================== 接口 ====================
def _login_headers() -> dict:
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": LOGIN_ORIGIN,
        "Referer": LOGIN_REFERER,
        "User-Agent": UA,
    }


def _unique_token(session: requests.Session):
    values = []
    for cookie in session.cookies:
        if cookie.name.upper() == "AUTHORIZATION" and cookie.value:
            v = unquote(cookie.value)
            if v not in values:
                values.append(v)
    return values[0] if values else None


def make_session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/json",
        "Authorization": token,
        "Referer": LMS + "/",
    })
    return session


def login(session: requests.Session, username: str, password: str) -> str:
    try:
        resp = session.post(LOGIN_URL, data={"loginName": username, "password": password},
                            headers=_login_headers(), timeout=12, allow_redirects=False)
    except requests.RequestException as e:
        raise ApiError(f"登录网络错误：{e}") from e
    if resp.status_code not in (200, 302):
        raise ApiError(f"登录被服务器拒绝（HTTP {resp.status_code}）")
    token = _unique_token(session)
    if not token:
        raise ApiError("服务器没有下发登录凭据：可能需要验证码或统一身份认证，"
                       "请在登录窗口粘贴浏览器里的 AUTHORIZATION 凭据")
    return token


def verify(session: requests.Session, token: str) -> dict:
    headers = {
        "Accept": "application/json",
        "Authorization": token,
        "Origin": "https://lms.dgut.edu.cn",
        "Referer": "https://lms.dgut.edu.cn/",
        "User-Agent": UA,
    }
    try:
        resp = session.post(IDENTITY_URL, json={}, headers=headers, timeout=12, allow_redirects=False)
    except requests.RequestException as e:
        raise ApiError(f"身份验证网络错误：{e}", auth=True) from e
    if resp.status_code != 200:
        raise ApiError(f"登录身份验证失败（HTTP {resp.status_code}）", auth=True)
    try:
        data = resp.json()
    except ValueError as e:
        raise ApiError("身份接口未返回有效结果", auth=True) from e
    if not isinstance(data, dict) or data.get("code") not in (1, 200):
        raise ApiError("服务器未确认登录身份", auth=True)
    containers = [data] + [data[k] for k in ("result", "data", "userInfo", "user")
                           if isinstance(data.get(k), dict)]
    identity = next((c for c in containers if "userId" in c or "userID" in c), {})
    raw_id = identity.get("userId", identity.get("userID"))
    if not str(raw_id).isdigit() or int(raw_id) <= 0:
        raise ApiError("身份接口未返回有效用户编号", auth=True)
    name = next((str(identity.get(k)) for k in ("realName", "name", "userName", "studentName")
                 if identity.get(k)), "")
    return {"userId": int(raw_id), "name": name}


def password_login(username: str, password: str):
    session = make_session("placeholder")
    token = login(session, username, password)
    session = make_session(token)
    ident = verify(session, token)
    return session, ident, token


def token_login(token: str):
    session = make_session(token)
    ident = verify(session, token)
    return session, ident, token


# ==================== 数据解析 ====================
def pick_list(payload, specific_key: str) -> list:
    def bfs(keys):
        queue_items = [payload]
        guard = 0
        while queue_items and guard < 8:
            guard += 1
            cur = queue_items.pop(0)
            if isinstance(cur, list):
                return [x for x in cur if isinstance(x, dict)]
            if not isinstance(cur, dict):
                continue
            for k in keys:
                if isinstance(cur.get(k), list):
                    return [x for x in cur[k] if isinstance(x, dict)]
            for k in ("result", "data", "rows"):
                if isinstance(cur.get(k), (dict, list)):
                    queue_items.append(cur[k])
        return []

    found = bfs([specific_key])
    return found if found else bfs(["list", "records"])


def looks_auth_error(payload) -> bool:
    try:
        text = json.dumps(payload, ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        return False
    if "courseList" in text or "homeworkList" in text:
        return False
    return bool(re.search(r"未登录|登录失效|请先登录|登录已过期|无权限|授权失效", text))


def to_ms(value) -> int:
    if value is None or value == "" or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        try:
            n = float(value)
        except (TypeError, ValueError):
            return 0
        if n <= 0:
            return 0
        if n < 1e11:
            n *= 1000
        return int(n)
    text = str(value).strip()
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def first_defined(obj: dict, keys):
    for k in keys:
        v = obj.get(k)
        if v is not None and v != "":
            return v
    return None


def kind_of(state_val, raw_state):
    if state_val == 0:
        return "todo"
    if state_val in (1, 2):
        return "review"
    if isinstance(raw_state, str):
        if re.search(r"已提交|已批|已完成|已互评", raw_state):
            return None
        if re.search(r"未提交|待提交", raw_state):
            return "todo"
        if "互评" in raw_state:
            return "review"
    return None


def normalize(item, course: dict):
    if not isinstance(item, dict):
        return None
    raw = first_defined(item, ("state", "homeworkState", "status"))
    state_val = None
    if isinstance(raw, bool):
        pass
    elif isinstance(raw, (int, float)):
        state_val = int(raw)
    elif isinstance(raw, str):
        try:
            state_val = int(raw.strip())
        except ValueError:
            state_val = None
    kind = kind_of(state_val, raw)
    if not kind:
        return None
    end_raw = first_defined(item, ("endTime", "deadline", "dueTime", "end_time",
                                   "closeTime", "submitDeadline"))
    title = str(first_defined(item, ("homeworkTitle", "title", "name")) or "未命名作业").strip()
    id_raw = first_defined(item, ("id", "homeworkId", "relationId"))
    key_part = str(id_raw) if id_raw is not None else title
    return {
        "key": f"{key_part}|{course['id']}",
        "title": title,
        "kind": kind,
        "state": state_val,
        "rawState": "" if raw is None else str(raw),
        "endMs": to_ms(end_raw),
        "courseId": course["id"],
        "courseName": course["name"],
        "teacher": course.get("teacher", ""),
    }


def fetch_courses(session: requests.Session) -> list:
    courses = []
    seen = set()
    for pn in range(1, 6):
        try:
            resp = session.get(COURSES_URL, params={
                "keyword": "", "publishStatus": 1, "type": 1, "pn": pn, "ps": 50,
            }, timeout=15)
        except requests.RequestException as e:
            raise ApiError(f"课程列表网络错误：{e}", auth=True) from e
        if resp.status_code in (401, 403):
            raise ApiError(f"HTTP {resp.status_code}，未登录或登录已失效", auth=True)
        if not resp.ok:
            raise ApiError(f"课程列表 HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise ApiError("课程列表未返回有效 JSON") from e
        if looks_auth_error(payload):
            raise ApiError("登录已失效", auth=True)
        page = pick_list(payload, "courseList")
        for c in page:
            cid = c.get("id")
            if cid is None:
                continue
            cid = str(cid)
            if cid in seen:
                continue
            seen.add(cid)
            courses.append({
                "id": cid,
                "name": str(c.get("name") or "未命名课程"),
                "teacher": str(c.get("teacherName") or ""),
            })
        if len(page) < 50:
            break
    return courses


def fetch_homeworks(session: requests.Session, course: dict):
    try:
        resp = session.get(HOMEWORKS_URL, params={
            "ocId": course["id"], "pn": 1, "ps": 999, "lang": "zh",
        }, timeout=15)
    except requests.RequestException as e:
        raise ApiError(f"作业列表网络错误：{e}", auth=True) from e
    if resp.status_code in (401, 403):
        raise ApiError(f"HTTP {resp.status_code}，未登录或登录已失效", auth=True)
    if not resp.ok:
        raise ApiError(f"作业列表 HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise ApiError("作业列表未返回有效 JSON") from e
    if looks_auth_error(payload):
        raise ApiError("登录已失效", auth=True)
    return pick_list(payload, "homeworkList"), payload


def scan(session: requests.Session, progress=None) -> dict:
    courses = fetch_courses(session)
    items = []
    failures = []
    raws = []
    ignored = set()
    total = len(courses)
    for i, course in enumerate(courses, 1):
        if progress:
            progress(f"读取 {i}/{total}：{course['name']}")
        try:
            homework_list, payload = fetch_homeworks(session, course)
        except ApiError:
            raise
        except Exception as e:
            failures.append(f"{course['name']}：{e}")
            continue
        raws.append({"course": course, "payload": payload})
        seen = set()
        for raw_item in homework_list:
            item = normalize(raw_item, course)
            if not item:
                if isinstance(raw_item, dict):
                    rv = first_defined(raw_item, ("state", "homeworkState", "status"))
                    if rv is not None and rv != "":
                        ignored.add(str(rv))
                continue
            if item["key"] in seen:
                continue
            seen.add(item["key"])
            items.append(item)
        time.sleep(0.1)
    items.sort(key=lambda x: (1, 0) if not x["endMs"] else (0, x["endMs"]))
    return {"courses": courses, "items": items, "failures": failures,
            "raws": raws, "ignored": sorted(ignored)}


# ==================== 过滤 ====================
def is_visible(item: dict, cfg: dict, now_ms: int | None = None) -> bool:
    if item["key"] in cfg.get("hidden", []):
        return False
    words = [w for w in re.split(r"[,，\s]+", str(cfg.get("exclude", ""))) if w]
    if words:
        low = item["title"].lower()
        if any(w.lower() in low for w in words):
            return False
    days = int(cfg.get("days") or 0)
    if days > 0 and item["endMs"]:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        if item["endMs"] - now > days * DAY_MS:
            return False
    return True


# ==================== 时间格式 ====================
def fmt_datetime(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def fmt_left(ms: int) -> str:
    if ms <= 0:
        over = -ms
        d, h, m = over // DAY_MS, (over % DAY_MS) // 3600000, (over % 3600000) // 60000
        if d:
            return f"已过期 {d}天{h}小时"
        if h:
            return f"已过期 {h}小时{m}分"
        return f"已过期 {max(1, m)}分钟"
    d, h, m = ms // DAY_MS, (ms % DAY_MS) // 3600000, (ms % 3600000) // 60000
    if d:
        return f"还剩 {d}天{h}小时"
    if h:
        return f"还剩 {h}小时{m}分"
    return f"还剩 {max(1, m)}分"


def countdown_text(item: dict, now_ms: int) -> str:
    if not item["endMs"]:
        return "无截止时间"
    return fmt_left(item["endMs"] - now_ms)


# ==================== 打开链接 ====================
def is_elevated() -> bool:
    """当前进程是不是以管理员身份运行。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


if os.name == "nt":
    _adv = ctypes.windll.advapi32
    _adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                      ctypes.POINTER(ctypes.c_void_p)]
    _adv.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    _adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    _adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    _adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)


class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


INTEGRITY_NAMES = {0x0000: "Untrusted", 0x1000: "普通", 0x2000: "管理员", 0x3000: "System"}


def process_integrity(pid: int):
    """读进程的完整性级别：0x1000=普通用户，0x2000=管理员。读不到返回 None。"""
    if os.name != "nt":
        return None
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        tok = ctypes.c_void_p()
        if not _adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):   # TOKEN_QUERY
            return None
        try:
            size = wintypes.DWORD()
            _adv.GetTokenInformation(tok, 25, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            if not _adv.GetTokenInformation(tok, 25, buf, size.value, ctypes.byref(size)):
                return None
            sid = ctypes.cast(buf, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents.Label.Sid
            n = _adv.GetSidSubAuthorityCount(sid).contents.value
            return _adv.GetSidSubAuthority(sid, n - 1).contents.value
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(h)


def _all_pids():
    """枚举所有进程 id（直接用系统 API，比 tasklist 快也更不容易被环境限制）。"""
    psapi = ctypes.windll.psapi
    buf = (wintypes.DWORD * 8192)()
    need = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(buf), ctypes.sizeof(buf), ctypes.byref(need)):
        return []
    n = need.value // ctypes.sizeof(wintypes.DWORD)
    return [buf[i] for i in range(n) if buf[i]]


def _proc_name(pid: int) -> str:
    """进程的可执行文件名（小写），读不到返回空串。"""
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
    finally:
        k32.CloseHandle(h)
    return ""


def elevated_browsers(names=("msedge.exe", "chrome.exe", "firefox.exe", "iexplore.exe")):
    """找出**以管理员权限运行的浏览器**进程，返回 [(pid, 进程名)]。

    这类进程是「Edge 未响应：现有实例正在以提升的权限运行」的元凶：
    普通权限的进程没法把链接交给一个提权的浏览器实例，Windows 就会弹那个框。
    只能把那个浏览器整个关掉再重开。
    """
    if os.name != "nt":
        return []
    found = []
    try:
        for pid in _all_pids():
            name = _proc_name(pid)
            if name in names and process_integrity(pid) == 0x2000:
                found.append((pid, name))
    except Exception:
        return []
    return found


def open_url(url: str) -> bool:
    """用系统默认浏览器打开链接。

    提权运行时**不能**直接 webbrowser.open：浏览器会继承管理员权限，
    Edge / Chrome 会拒绝启动并弹「现有实例正在以提升的权限运行，是否用普通权限重启」。
    这种情况交给 explorer.exe 去开 —— 它跑在普通权限下，浏览器就正常了。
    """
    if is_elevated():
        try:
            subprocess.Popen(["explorer.exe", url])
            return True
        except Exception:
            pass
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


# ==================== 主题 / 勾选框样式 ====================
# ttk 的 clam 主题在「已勾选」时画的是一个叉（X），很容易被当成「取消 / 关闭」。
# 这里用自绘的对勾图片替换掉那个指示器，其余布局保持原样。
_CHECK_IMAGES = []          # 必须留引用，否则 Tk 会把 PhotoImage 回收掉


def _check_pixels(size: int) -> set:
    """在 size×size 的格子里光栅化一个对勾（两段粗线）。"""
    def seg(x0, y0, x1, y1, radius=1.25):
        pts = set()
        steps = 120
        for i in range(steps + 1):
            t = i / steps
            cx, cy = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    if dx * dx + dy * dy <= radius * radius:
                        pts.add((int(round(cx)) + dx, int(round(cy)) + dy))
        return pts

    k = size / 14.0
    return (seg(3.6 * k, 7.8 * k, 6.0 * k, 10.3 * k)
            | seg(6.0 * k, 10.3 * k, 10.8 * k, 3.5 * k))


def _check_image(size: int, checked: bool, border="#8f8f8f", bg="#ffffff", mark="#1a7f37"):
    img = tk.PhotoImage(width=size, height=size)
    dots = _check_pixels(size) if checked else set()
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            if x in (0, size - 1) or y in (0, size - 1):
                row.append(border)
            elif (x, y) in dots:
                row.append(mark)
            else:
                row.append(bg)
        rows.append("{" + " ".join(row) + "}")
    img.put(" ".join(rows))
    return img


def apply_theme(root) -> ttk.Style:
    """统一主题，并把「已勾选」的叉换成对勾。界面和提醒设置共用。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    size = 14
    off = _check_image(size, False)
    on = _check_image(size, True)
    _CHECK_IMAGES.extend([off, on])
    name = "Maid.Checkbutton.indicator"
    try:
        # 注意：状态/图片要作为一个元组传进去，不能平铺成两个参数
        style.element_create(name, "image", off, ("selected", on), sticky="")
    except tk.TclError:
        pass                                     # 已经建过了
    else:
        def swap(spec):
            out = []
            for elem, opts in spec:
                opts = dict(opts)
                if elem == "Checkbutton.indicator":
                    elem = name
                if "children" in opts:
                    opts["children"] = swap(opts["children"])
                out.append((elem, opts))
            return out

        try:
            style.layout("TCheckbutton", swap(style.layout("TCheckbutton")))
        except tk.TclError:
            pass
    return style


# ==================== GUI ====================
class App:
    def __init__(self, offline: bool = False, base_dir: Path | None = None):
        self.base = Path(base_dir) if base_dir else BASE_DIR
        self.account_path = self.base / ACCOUNT_FILE
        self.cfg_path = self.base / CONFIG_FILE
        self.cfg = read_cfg(self.cfg_path)
        self.account = read_account(self.account_path)
        self.offline = offline
        self.session = None
        self.identity = None
        self.items = []
        self.courses = []
        self.failures = []
        self.raws = []
        self.updated_at = 0
        self.running = False
        self.queue: queue.Queue = queue.Queue()
        self._by_key = {}
        self._rows = {"todo": {}, "review": {}}

        self.root = tk.Tk()
        self.root.title(f"作业汇总助手 · 优学院 v{VERSION}")
        self.root.geometry(self.cfg.get("geometry") or "960x620")
        self.root.minsize(760, 480)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._afters = []          # 记下定时器 id，关窗时全部取消

        self._build()
        self._restore_filters()

        self._later(200, self._poll)
        self._later(300, self._notify_hint)
        self._later(20000, self._tick)
        self._later(60000, self._auto_tick)

        if offline:
            self._log("离线模式：不连接服务器，仅打开界面")
        else:
            self._later(100, self.start_session)

    # ---------- UI 构建 ----------
    def _build(self):
        style = apply_theme(self.root)
        style.configure("Treeview", rowheight=24, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

        top = ttk.Frame(self.root, padding=(8, 8, 8, 2))
        top.pack(fill="x")
        bar1 = ttk.Frame(top)
        bar1.pack(fill="x")
        bar2 = ttk.Frame(top)
        bar2.pack(fill="x", pady=(4, 0))

        self.btn_refresh = ttk.Button(bar1, text="刷新", command=self.refresh)
        self.btn_refresh.pack(side="left")

        self.var_auto = tk.BooleanVar(value=bool(self.cfg["auto_refresh"]))
        ttk.Checkbutton(bar1, text="自动刷新(10分钟)", variable=self.var_auto,
                        command=self._filters_changed).pack(side="left", padx=(10, 0))

        self.btn_unhide = ttk.Button(bar1, text="恢复隐藏 (0)", command=self._unhide_all)
        self.btn_unhide.pack(side="left", padx=(12, 0))
        self.btn_hide = ttk.Button(bar1, text="隐藏选中", command=self._hide_selected)
        self.btn_hide.pack(side="left", padx=(4, 0))
        ttk.Button(bar1, text="导出原始数据", command=self._export_raw).pack(side="left", padx=(4, 0))
        self.btn_notify = ttk.Button(bar1, text="提醒设置…", command=self._notify_settings)
        self.btn_notify.pack(side="left", padx=(4, 0))

        ttk.Label(bar2, text="天数:").pack(side="left")
        self.var_days = tk.StringVar()
        self.spin_days = ttk.Spinbox(bar2, from_=0, to=365, width=5,
                                     textvariable=self.var_days, command=self._filters_changed)
        self.spin_days.pack(side="left")
        for ev in ("<KeyRelease>", "<FocusOut>", "<<Increment>>", "<<Decrement>>"):
            self.spin_days.bind(ev, self._filters_changed)
        ttk.Label(bar2, text="0=不限").pack(side="left", padx=(2, 0))

        ttk.Label(bar2, text="排除关键词:").pack(side="left", padx=(14, 2))
        self.var_exclude = tk.StringVar()
        self.entry_exclude = ttk.Entry(bar2, textvariable=self.var_exclude, width=22)
        self.entry_exclude.pack(side="left")
        self.entry_exclude.bind("<KeyRelease>", self._filters_changed)

        self.var_expired = tk.BooleanVar(value=bool(self.cfg["show_expired"]))
        ttk.Checkbutton(bar2, text="显示已过期", variable=self.var_expired,
                        command=self._filters_changed).pack(side="left", padx=(12, 0))

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)

        self.trees = {}
        self.tab_frames = {}
        for kind, label in (("todo", "待提交 (0)"), ("review", "待互评 (0)")):
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=label)
            self.tab_frames[kind] = frame
            columns = ("course", "title", "deadline", "left")
            tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
            tree.heading("course", text="课程")
            tree.heading("title", text="作业")
            tree.heading("deadline", text="截止时间")
            tree.heading("left", text="剩余")
            tree.column("course", width=170, minwidth=100)
            tree.column("title", width=380, minwidth=160)
            tree.column("deadline", width=145, minwidth=110, anchor="center")
            tree.column("left", width=120, minwidth=90, anchor="center")
            tree.tag_configure("overdue", foreground="#b71c1c")
            tree.tag_configure("soon", foreground="#e65100")
            vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=vsb.set)
            tree.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")
            tree.bind("<Double-1>", self._open_item)
            tree.bind("<Delete>", self._hide_selected)
            self.trees[kind] = tree

        log_frame = ttk.LabelFrame(self.root, text="日志", padding=4)
        log_frame.pack(fill="x", padx=8, pady=(0, 4))
        self.log_text = tk.Text(log_frame, height=6, state="disabled", wrap="word",
                                font=("Consolas", 9), background="#141414", foreground="#d4d4d4")
        self.log_text.pack(fill="x")

        status_bar = ttk.Frame(self.root, padding=(8, 0, 8, 6))
        status_bar.pack(fill="x")
        self.var_status = tk.StringVar(value="尚未加载")
        ttk.Label(status_bar, textvariable=self.var_status, foreground="#555").pack(side="left")
        self.var_identity = tk.StringVar(value="")
        ttk.Label(status_bar, textvariable=self.var_identity, foreground="#555").pack(side="right")
        ttk.Button(status_bar, text="退出登录", command=self._logout).pack(side="right", padx=(0, 12))

    def _restore_filters(self):
        self.var_days.set(str(self.cfg["days"]))
        self.var_exclude.set(self.cfg["exclude"])
        self.var_expired.set(bool(self.cfg["show_expired"]))
        self.var_auto.set(bool(self.cfg["auto_refresh"]))
        target = self.tab_frames["review"] if self.cfg.get("tab") == "review" else self.tab_frames["todo"]
        try:
            self.notebook.select(target)
        except tk.TclError:
            pass

    # ---------- 登录 ----------
    def start_session(self):
        token = self.account.get("token", "")
        password = self.account.get("password", "")
        username = self.account.get("username", "")
        if token:
            self._log("检测到已保存的登录凭据，正在验证…")
            self.async_call(lambda: token_login(token),
                            on_ok=self._session_ok,
                            on_err=lambda e: self._after_bad_token(str(e)))
            return
        if username and password:
            self._log("正在自动登录…")
            self._auto_password_login()
            return
        self._open_login()

    def _after_bad_token(self, err: str):
        self._log(f"保存的凭据已失效：{err}")
        if self.account.get("username") and self.account.get("password"):
            self._log("尝试用保存的账号密码重新登录…")
            self._auto_password_login()
        else:
            self._open_login("登录凭据已失效，请重新登录")

    def _auto_password_login(self):
        user = self.account.get("username", "")
        pwd = self.account.get("password", "")
        self.async_call(lambda: password_login(user, pwd),
                        on_ok=self._password_login_ok,
                        on_err=lambda e: self._open_login(f"自动登录失败：{e}"))

    def _password_login_ok(self, result):
        session, ident, token = result
        try:
            write_account(self.account_path, self.account.get("username", ""),
                          self.account.get("password", ""), token)
            self.account = read_account(self.account_path)
        except Exception as e:
            self._log(f"保存登录凭据失败：{e}", "err")
        self._session_ok(result)

    def _session_ok(self, result):
        session, ident, token = result
        self.session = session
        self.identity = ident
        name = ident.get("name") or ""
        self.var_identity.set(f"账号：{name}" if name else f"账号ID：{ident.get('userId', '')}")
        self._log(f"登录成功{'：' + name if name else ''}", "ok")
        self.refresh()

    def _open_login(self, error: str = ""):
        if getattr(self, "_login_dlg", None) is not None and self._login_dlg.winfo_exists():
            self._login_dlg.lift()
            self._login_dlg.focus_force()
            return
        dlg = tk.Toplevel(self.root)
        self._login_dlg = dlg
        dlg.title("登录优学院")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()

        frame = ttk.Frame(dlg, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="账号（学号）:").grid(row=0, column=0, sticky="w", pady=4)
        var_user = tk.StringVar(value=self.account.get("username", ""))
        ent_user = ttk.Entry(frame, textvariable=var_user, width=32)
        ent_user.grid(row=0, column=1, pady=4, padx=(8, 0))

        ttk.Label(frame, text="密码:").grid(row=1, column=0, sticky="w", pady=4)
        var_pwd = tk.StringVar()
        ent_pwd = ttk.Entry(frame, textvariable=var_pwd, width=32, show="*")
        ent_pwd.grid(row=1, column=1, pady=4, padx=(8, 0))

        var_remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="记住账号密码（Windows 加密保存，仅本机可用）",
                        variable=var_remember).grid(row=2, column=0, columnspan=2,
                                                    sticky="w", pady=(2, 8))

        sep = ttk.Separator(frame, orient="horizontal")
        sep.grid(row=3, column=0, columnspan=2, sticky="ew", pady=6)

        ttk.Label(frame, text="或粘贴登录凭据:").grid(row=4, column=0, columnspan=2, sticky="w")
        var_token = tk.StringVar()
        ent_token = ttk.Entry(frame, textvariable=var_token, width=32)
        ent_token.grid(row=5, column=0, columnspan=2, sticky="we", pady=4)
        ttk.Label(frame, foreground="#777", wraplength=300, justify="left", text=(
            "密码登录提示需要验证码/统一身份认证时使用。\n"
            "获取方法：浏览器登录优学院 → F12 → Application →\n"
            "Cookies → lms.dgut.edu.cn → 复制 AUTHORIZATION 的值"
        )).grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 8))

        var_err = tk.StringVar(value=error)
        lbl_err = ttk.Label(frame, textvariable=var_err, foreground="#b71c1c",
                            wraplength=300, justify="left")
        lbl_err.grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 8))

        btn_login = ttk.Button(frame, text="登录", command=lambda: _submit())
        btn_login.grid(row=8, column=0, columnspan=2, sticky="we")

        frame.columnconfigure(1, weight=1)

        def _finish_ok(result):
            if not dlg.winfo_exists():
                return
            session, ident, token = result
            user = var_user.get().strip()
            pwd = var_pwd.get()
            try:
                acc = read_account(self.account_path)
                keep_pwd = acc.get("password", "")
                if var_token.get().strip():
                    write_account(self.account_path, user or acc.get("username", ""),
                                  keep_pwd, token)
                else:
                    write_account(self.account_path, user,
                                  pwd if var_remember.get() else "", token)
                self.account = read_account(self.account_path)
            except Exception as e:
                self._log(f"保存登录凭据失败：{e}", "err")
            dlg.destroy()
            self._login_dlg = None
            self._session_ok(result)

        def _finish_err(e):
            if not dlg.winfo_exists():
                return
            btn_login.state(["!disabled"])
            btn_login.config(text="登录")
            var_err.set(str(e))

        def _submit():
            token_val = var_token.get().strip()
            user = var_user.get().strip()
            pwd = var_pwd.get()
            if token_val:
                var_err.set("正在验证凭据…")
                btn_login.state(["disabled"])
                self.async_call(lambda: token_login(token_val),
                                on_ok=_finish_ok, on_err=_finish_err)
                return
            if not user or not pwd:
                var_err.set("请输入账号和密码，或粘贴登录凭据")
                return
            var_err.set("正在登录…")
            btn_login.state(["disabled"])

            def work():
                return password_login(user, pwd)
            self.async_call(work, on_ok=_finish_ok, on_err=_finish_err)

        ent_pwd.bind("<Return>", lambda e: _submit())
        ent_token.bind("<Return>", lambda e: _submit())

    def _logout(self):
        if not messagebox.askyesno("退出登录", "确定退出当前账号？\n（会清除本机保存的密码和凭据）"):
            return
        self.session = None
        self.identity = None
        self.items = []
        self.courses = []
        self.updated_at = 0
        self.account = {"username": self.account.get("username", ""), "password": "", "token": ""}
        try:
            write_account(self.account_path, self.account["username"], "", "")
        except Exception as e:
            self._log(f"清除凭据失败：{e}", "err")
        self.var_identity.set("")
        self._render()
        self._log("已退出登录")
        self._open_login()

    # ---------- 刷新 ----------
    def refresh(self):
        if self.running:
            return
        if not self.session:
            self._open_login("请先登录")
            return
        self.running = True
        self.btn_refresh.state(["disabled"])
        self.var_status.set("刷新中…")

        def work():
            def attempt():
                if self.session is None:
                    raise ApiError("未登录", auth=True)
                return scan(self.session,
                            progress=lambda t: self.queue.put(("note", t, None)))

            try:
                return attempt()
            except ApiError as e:
                if not e.auth:
                    raise
                user = self.account.get("username", "")
                pwd = self.account.get("password", "")
                if not (user and pwd):
                    raise
                self.queue.put(("note", "登录失效，正在重新登录…", None))
                session, ident, token = password_login(user, pwd)
                try:
                    write_account(self.account_path, user, pwd, token)
                except Exception:
                    pass
                self.session, self.identity = session, ident
                return attempt()

        self.async_call(work, on_ok=self._scan_done, on_err=self._scan_failed)

    def _scan_done(self, result):
        self.running = False
        self.btn_refresh.state(["!disabled"])
        self.items = result["items"]
        self.courses = result["courses"]
        self.failures = result["failures"]
        self.raws = result["raws"]
        self.updated_at = int(time.time() * 1000)
        self._by_key = {it["key"]: it for it in self.items}
        self._render()
        todo = sum(1 for it in self.items if it["kind"] == "todo")
        review = sum(1 for it in self.items if it["kind"] == "review")
        summary = (f"读取 {len(self.courses)} 门课 · 待提交 {todo} · 待互评 {review}")
        if result["ignored"]:
            summary += f"（已忽略其他状态 {','.join(result['ignored'][:6])}）"
        self._log(summary, "ok")
        if self.failures:
            self._log(f"{len(self.failures)} 门课读取失败：{self.failures[0]}", "err")

    def _scan_failed(self, e: Exception):
        self.running = False
        self.btn_refresh.state(["!disabled"])
        if isinstance(e, ApiError) and e.auth:
            self.var_status.set("登录已失效")
            self._log(f"登录已失效：{e}", "err")
            self._open_login("登录已失效，请重新登录")
        else:
            self.var_status.set(f"刷新失败：{e}")
            self._log(f"刷新失败：{e}", "err")

    # ---------- 渲染 ----------
    def _shown(self, kind: str) -> list:
        now = int(time.time() * 1000)
        out = []
        for it in self.items:
            if it["kind"] != kind:
                continue
            if not is_visible(it, self.cfg, now):
                continue
            if not self.cfg["show_expired"] and it["endMs"] and it["endMs"] < now:
                continue
            out.append(it)
        return out

    def _render(self):
        now = int(time.time() * 1000)
        for kind, tree in self.trees.items():
            tree.delete(*tree.get_children())
            rows = {}
            for it in self._shown(kind):
                tags = ()
                if it["endMs"] and it["endMs"] < now:
                    tags = ("overdue",)
                elif it["endMs"] and it["endMs"] - now < DAY_MS:
                    tags = ("soon",)
                values = (
                    it["courseName"],
                    it["title"],
                    fmt_datetime(it["endMs"]) or "—",
                    countdown_text(it, now),
                )
                tree.insert("", "end", iid=it["key"], values=values, tags=tags)
                rows[it["key"]] = it
            self._rows[kind] = rows
        todo = len(self._shown("todo"))
        review = len(self._shown("review"))
        self.notebook.tab(self.tab_frames["todo"], text=f"待提交 ({todo})")
        self.notebook.tab(self.tab_frames["review"], text=f"待互评 ({review})")
        hidden = len(self.cfg.get("hidden", []))
        self.btn_unhide.config(text=f"恢复隐藏 ({hidden})")
        if hidden:
            self.btn_unhide.state(["!disabled"])
        else:
            self.btn_unhide.state(["disabled"])
        if self.updated_at:
            when = datetime.fromtimestamp(self.updated_at / 1000).strftime("%H:%M:%S")
            self.var_status.set(f"{len(self.courses)} 门课 · 待提交 {todo} · 待互评 {review} · {when} 更新")
        elif not self.running:
            self.var_status.set("尚未加载，点「刷新」")

    def _tick(self):
        now = int(time.time() * 1000)
        for kind, tree in self.trees.items():
            for iid, it in self._rows.get(kind, {}).items():
                if not tree.exists(iid):
                    continue
                txt = countdown_text(it, now)
                if tree.set(iid, "left") != txt:
                    tree.set(iid, "left", txt)
                tags = ()
                if it["endMs"] and it["endMs"] < now:
                    tags = ("overdue",)
                elif it["endMs"] and it["endMs"] - now < DAY_MS:
                    tags = ("soon",)
                tree.item(iid, tags=tags)
        self._later(20000, self._tick)

    def _auto_tick(self):
        if (self.cfg.get("auto_refresh") and self.session and not self.running
                and not self.offline and self.updated_at
                and time.time() * 1000 - self.updated_at > 600000):
            self.refresh()
        self._later(60000, self._auto_tick)

    # ---------- 交互 ----------
    def _filters_changed(self, _event=None):
        try:
            days = int(str(self.var_days.get()).strip() or "0")
        except ValueError:
            days = 0
        self.cfg["days"] = max(0, days)
        self.cfg["exclude"] = self.var_exclude.get().strip()
        self.cfg["show_expired"] = bool(self.var_expired.get())
        self.cfg["auto_refresh"] = bool(self.var_auto.get())
        write_cfg(self.cfg_path, self.cfg)
        self._render()

    def _tab_changed(self, _event=None):
        try:
            idx = self.notebook.index(self.notebook.select())
        except tk.TclError:
            return
        self.cfg["tab"] = "review" if idx else "todo"
        write_cfg(self.cfg_path, self.cfg)

    def _hide_selected(self, _event=None):
        try:
            idx = self.notebook.index(self.notebook.select())
        except tk.TclError:
            return
        kind = "todo" if idx == 0 else "review"
        tree = self.trees[kind]
        selection = tree.selection()
        if not selection:
            return
        hidden = set(self.cfg.get("hidden", []))
        for iid in selection:
            hidden.add(iid)
        self.cfg["hidden"] = sorted(hidden)
        write_cfg(self.cfg_path, self.cfg)
        self._log(f"已隐藏 {len(selection)} 条（点「恢复隐藏」可找回）")
        self._render()

    def _unhide_all(self):
        if not self.cfg.get("hidden"):
            return
        count = len(self.cfg["hidden"])
        self.cfg["hidden"] = []
        write_cfg(self.cfg_path, self.cfg)
        self._log(f"已恢复 {count} 条隐藏的作业", "ok")
        self._render()

    def _open_item(self, _event=None):
        current = self.notebook.select()
        try:
            idx = self.notebook.index(current)
        except tk.TclError:
            return
        kind = "todo" if idx == 0 else "review"
        tree = self.trees[kind]
        for iid in tree.selection():
            it = self._rows[kind].get(iid)
            if it:
                open_url(HOMEWORK_PAGE.format(courseId=it["courseId"]))
                return

    def _export_raw(self):
        if not self.raws:
            messagebox.showinfo("导出原始数据", "还没有数据，请先点「刷新」")
            return
        path = self.base / f"homework_raw_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        try:
            path.write_text(json.dumps(self.raws, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
        except OSError as e:
            messagebox.showerror("导出原始数据", f"写入失败：{e}")
            return
        self._log(f"原始数据已导出：{path}（含个人课程数据，勿外传）", "ok")

    # ---------- 基础 ----------
    def async_call(self, fn, on_ok=None, on_err=None):
        def run():
            try:
                result = fn()
            except Exception as e:
                self.queue.put(("err", e, on_err))
            else:
                self.queue.put(("ok", result, on_ok))
        threading.Thread(target=run, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, value, callback = self.queue.get_nowait()
                if callback:
                    try:
                        callback(value)
                    except Exception as e:
                        self._log(f"回调执行出错：{e}", "err")
                elif kind == "note":
                    self.var_status.set(str(value))
        except queue.Empty:
            pass
        except Exception as e:
            if sys.stdout is not None:
                print(f"_poll error: {e}")
        finally:
            self._later(200, self._poll)

    # ---------- 定时器 / 退出 ----------
    def _later(self, ms, fn):
        """登记定时器，关窗时统一取消，免得窗口销毁后回调再触发报错。"""
        try:
            self._afters.append(self.root.after(ms, fn))
        except tk.TclError:
            pass

    def shutdown(self):
        for aid in list(self._afters):
            try:
                self.root.after_cancel(aid)
            except Exception:
                pass
        self._afters.clear()
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------- 提醒设置 ----------
    def _notify_hint(self):
        try:
            import notify
            cfg = notify.read_cfg()
        except Exception:
            return
        if cfg.get("enabled"):
            self._log(f"临期提醒已开启：提前 {cfg['days_before']} 天，每天 {cfg['daily_at']} 起每 "
                      f"{cfg['interval_hours']} 小时检查一次（{notify.task_info()}）")
            try:
                if not self.account.get("password"):
                    self._log("注意：本机只存了登录凭据、没存密码。凭据过期后自动提醒会中断，"
                              "建议重新登录时勾选「记住账号密码」", "err")
            except Exception:
                pass
        else:
            self._log("临期提醒未开启：点上方「提醒设置」可让电脑自动提醒临期作业")
        if is_elevated():
            self._log("提示：程序正以管理员身份运行。提醒功能不需要管理员权限，"
                      "而且提权会让「打开作业页 / 打开配置页面」时浏览器弹权限提示。"
                      "建议右键快捷方式 → 属性 → 兼容性 → 取消「以管理员身份运行」", "err")

    def _notify_settings(self):
        try:
            import notify
        except Exception as e:
            messagebox.showerror("提醒设置", f"无法加载提醒模块 notify.py：{e}")
            return
        try:
            notify.open_settings_dialog(self.root, self.base)
        except Exception as e:
            self._log(f"打开提醒设置失败：{e}", "err")
            messagebox.showerror("提醒设置", f"打开提醒设置失败：{e}")

    def _log(self, msg: str, cls: str = ""):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n"
        self.log_text.config(state="normal")
        if cls == "err":
            self.log_text.tag_configure("err", foreground="#ff6b6b")
            self.log_text.insert("end", line, "err")
        elif cls == "ok":
            self.log_text.tag_configure("ok", foreground="#6bcB7f")
            self.log_text.insert("end", line, "ok")
        else:
            self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        if sys.stdout is not None:
            try:
                print(line.rstrip())
            except UnicodeEncodeError:
                print(line.rstrip().encode("ascii", "replace").decode("ascii"))

    def _on_close(self):
        self.cfg["geometry"] = self.root.geometry()
        try:
            write_cfg(self.cfg_path, self.cfg)
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    # 同一个 exe 兼顾两种身份：不带参数开界面，带提醒参数走提醒脚本（计划任务用）
    if argv and argv[0] in ("--run", "-r", "--test", "--install", "--uninstall",
                            "--status", "--settings", "--reset", "--diag"):
        import notify
        return notify.main(argv)
    offline = "--offline" in argv
    app = App(offline=offline)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
