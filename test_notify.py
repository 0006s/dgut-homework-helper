# -*- coding: utf-8 -*-
"""notify.py 纯逻辑测试（不联网、不弹窗）：py -m unittest -v test_notify"""
import json
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import notify


class TestTimeHelpers(unittest.TestCase):
    def test_parse_hm(self):
        self.assertEqual(notify.parse_hm("08:30"), (8, 30))
        self.assertEqual(notify.parse_hm(" 8：5 "), (8, 5))
        self.assertIsNone(notify.parse_hm(""))
        self.assertIsNone(notify.parse_hm("25:00"))
        self.assertIsNone(notify.parse_hm("abc"))

    def test_in_quiet_cross_midnight(self):
        cfg = {"quiet_start": "22:30", "quiet_end": "07:30"}

        def at(h, m):
            import datetime
            dt = datetime.datetime(2026, 1, 1, h, m)
            return notify._in_quiet(cfg, int(dt.timestamp() * 1000))

        self.assertTrue(at(23, 0))
        self.assertTrue(at(3, 0))
        self.assertFalse(at(12, 0))
        self.assertFalse(at(7, 30))
        self.assertTrue(at(22, 30))

    def test_in_quiet_disabled(self):
        cfg = {"quiet_start": "", "quiet_end": ""}
        self.assertFalse(notify._in_quiet(cfg, int(time.time() * 1000)))


class TestThresholds(unittest.TestCase):
    def test_levels(self):
        cfg = {"days_before": 3, "repeat_hours": [24, 6, 1]}
        self.assertEqual(notify.thresholds(cfg), [72, 24, 6, 1])

    def test_repeat_beyond_window_is_dropped(self):
        cfg = {"days_before": 1, "repeat_hours": [48, 24, 6, 1]}
        self.assertEqual(notify.thresholds(cfg), [24, 6, 1])

    def test_stage_picks_smallest_matching(self):
        hs = [72, 24, 6, 1]
        h = 3600 * 1000
        self.assertEqual(notify.stage_of(70 * h, hs), 72)
        self.assertEqual(notify.stage_of(50 * h, hs), 72)
        self.assertEqual(notify.stage_of(20 * h, hs), 24)
        self.assertEqual(notify.stage_of(5 * h, hs), 6)
        self.assertEqual(notify.stage_of(0.5 * h, hs), 1)


class TestWindowItems(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time() * 1000)
        self.cfg = dict(notify.DEFAULT)
        self.cfg["days_before"] = 3

    def item(self, key, hours_left, kind="todo", title="作业"):
        return {"key": key, "title": title, "kind": kind, "state": 0, "rawState": "0",
                "endMs": self.now + int(hours_left * 3600 * 1000), "courseId": "1",
                "courseName": "课程", "teacher": ""}

    def test_window_filter(self):
        items = [self.item("a", 10), self.item("b", 100), self.item("c", -5), self.item("d", 0)]
        got = [it["key"] for it in notify.window_items(items, self.cfg, self.now)]
        self.assertEqual(got, ["a"])

    def test_sorted_by_deadline(self):
        items = [self.item("late", 60), self.item("soon", 5)]
        got = [it["key"] for it in notify.window_items(items, self.cfg, self.now)]
        self.assertEqual(got, ["soon", "late"])

    def test_kind_filter(self):
        items = [self.item("x", 5, kind="done"), self.item("y", 5, kind="review")]
        got = [it["key"] for it in notify.window_items(items, self.cfg, self.now)]
        self.assertEqual(got, ["y"])

    def test_no_deadline_skipped(self):
        it = self.item("n", 5)
        it["endMs"] = 0
        self.assertEqual(notify.window_items([it], self.cfg, self.now), [])


class TestShortening(unittest.TestCase):
    def test_short_course(self):
        self.assertEqual(notify.short_course("【2026秋】概率论与数理统计"), "概率论与数理统计")
        self.assertEqual(notify.short_course("2026工程力学A"), "工程力学A")
        self.assertEqual(notify.short_course("数据结构"), "数据结构")
        self.assertEqual(notify.short_course(""), "")

    def test_short_title(self):
        self.assertEqual(notify.short_title("2026-10-05 23:25 作业"), "作业")
        self.assertEqual(notify.short_title("第三章作业（占总成绩2%）"), "第三章作业")
        self.assertEqual(notify.short_title("实验三报告"), "实验三报告")

    def test_short_title_falls_back_when_all_stripped(self):
        self.assertEqual(notify.short_title("（说明）"), "（说明）")

    def test_short_left(self):
        hour = 3600 * 1000
        self.assertEqual(notify.short_left(5 * hour), "5小时0分")
        self.assertEqual(notify.short_left(-2 * hour), "已过期 2小时0分")

    def test_clip(self):
        self.assertEqual(notify.clip("abcdef", 6), "abcdef")
        self.assertEqual(notify.clip("abcdefg", 6), "abcde…")


class TestMessage(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time() * 1000)
        self.cfg = json.loads(json.dumps(notify.DEFAULT))
        self.items = [
            {"key": "a", "title": "实验一", "kind": "todo", "state": 0, "rawState": "0",
             "endMs": self.now + 5 * 3600 * 1000, "courseId": "1", "courseName": "数据结构",
             "teacher": ""},
            {"key": "b", "title": "互评<任务>", "kind": "review", "state": 1, "rawState": "1",
             "endMs": self.now + 30 * 3600 * 1000, "courseId": "2", "courseName": "网络",
             "teacher": ""},
        ]

    def test_title_has_no_app_prefix(self):
        title, _, _, _ = notify.build_message(self.items, self.cfg, self.now)
        self.assertEqual(title, "2 项作业临近截止")
        self.assertNotIn("作业汇总助手", title)

    def test_single_item(self):
        title, _, lines, _ = notify.build_message(self.items[:1], self.cfg, self.now)
        self.assertEqual(title, "1 项作业临近截止")
        self.assertEqual(len(lines), 1)
        self.assertIn("数据结构", lines[0])
        self.assertIn("实验一", lines[0])

    def test_toast_lines_are_compact(self):
        _, _, lines, _ = notify.build_message(self.items, self.cfg, self.now)
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertLessEqual(len(line), 24)
            self.assertNotIn("截止", line)       # 系统通知不带完整截止时间
        self.assertIn("·剩", lines[0])

    def test_toast_line_cap_and_tail(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["toast_max_items"] = 2
        many = [dict(it, key=f"k{i}") for i, it in enumerate(self.items * 4)]
        _, _, lines, _ = notify.build_message(many, cfg, self.now)
        self.assertEqual(len(lines), 3)          # 2 条 + 「另有 6 项」
        self.assertEqual(lines[-1], "另有 6 项")

    def test_detail_keeps_full_info(self):
        _, text, _, html = notify.build_message(self.items, self.cfg, self.now)
        self.assertIn("待提交 1", text)
        self.assertIn("待互评 1", text)
        self.assertIn("截止", text)
        self.assertIn("数据结构", text)

    def test_max_items_only_affects_detail(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["max_items"] = 1
        _, text, lines, _ = notify.build_message(self.items, cfg, self.now)
        self.assertIn("另有 1 项", text)
        self.assertEqual(len(lines), 2)          # 系统通知不受 max_items 影响

    def test_html_escaped(self):
        _, _, _, html = notify.build_message(self.items, self.cfg, self.now)
        self.assertNotIn("互评<任务>", html)
        self.assertIn("互评&lt;任务&gt;", html)


class TestConfigPersistence(unittest.TestCase):
    def setUp(self):
        self.path = Path(__file__).with_name("_test_notify_cfg.json")

    def tearDown(self):
        for p in (self.path, self.path.with_suffix(".json.tmp")):
            if p.exists():
                p.unlink()

    def test_defaults_when_missing(self):
        cfg = notify.read_cfg(self.path)
        self.assertEqual(cfg["days_before"], notify.DEFAULT["days_before"])
        self.assertTrue(cfg["channels"]["toast"])

    def test_roundtrip_and_dpapi(self):
        cfg = notify.read_cfg(self.path)
        cfg["days_before"] = 7
        cfg["channels"]["wechat"] = True
        cfg["wechat"]["token"] = "tk-123456"
        cfg["email"]["password"] = "p@ss"
        cfg["repeat_hours"] = [48, 12]
        notify.write_cfg(cfg, self.path)

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotEqual(raw["wechat"]["token"], "tk-123456")   # 落盘必须加密
        self.assertNotEqual(raw["email"]["password"], "p@ss")

        again = notify.read_cfg(self.path)
        self.assertEqual(again["days_before"], 7)
        self.assertTrue(again["channels"]["wechat"])
        self.assertEqual(again["wechat"]["token"], "tk-123456")
        self.assertEqual(again["email"]["password"], "p@ss")
        self.assertEqual(again["repeat_hours"], [48, 12])

    def test_unknown_keys_ignored(self):
        self.path.write_text(json.dumps({"nonsense": 1, "days_before": 2}), encoding="utf-8")
        cfg = notify.read_cfg(self.path)
        self.assertNotIn("nonsense", cfg)
        self.assertEqual(cfg["days_before"], 2)


class TestTaskErrorReporting(unittest.TestCase):
    """schtasks 跑不起来 ≠ 任务不存在。原来会把两者混为一谈，谎报「已删除 / 未注册」。"""

    def test_unreachable_is_recognised(self):
        self.assertTrue(notify._task_unreachable(
            "错误: 系统找不到指定的路径。"))
        self.assertTrue(notify._task_unreachable(
            "ERROR: The system cannot find the path specified."))

    def test_task_not_found_is_not_unreachable(self):
        self.assertFalse(notify._task_unreachable(
            "错误: 系统找不到指定的文件。"))
        self.assertFalse(notify._task_unreachable(
            "ERROR: The system cannot find the file specified."))
        self.assertFalse(notify._task_unreachable(""))
        self.assertFalse(notify._task_unreachable("SUCCESS: 计划任务已创建"))

    def test_diag_is_wired_up(self):
        self.assertIn("--diag", notify.CLI_FLAGS)
        self.assertTrue(callable(notify.cmd_diag))

    def test_can_read_own_integrity_level(self):
        """完整性级别读取：自己一定是普通用户（测试不会以管理员跑）。"""
        import os
        import 作业汇总助手 as app

        if os.name != "nt":
            self.skipTest("仅 Windows")
        self.assertIn(app.process_integrity(os.getpid()), (0x1000, 0x2000))
        self.assertFalse(app.is_elevated())
        self.assertEqual(app._proc_name(os.getpid()).lower(), "python.exe")
        # 没有提权浏览器时应当返回空列表，而不是抛异常
        self.assertIsInstance(app.elevated_browsers(), list)


class TestMailPresets(unittest.TestCase):
    """邮件服务商预设：选了就自动填服务器/端口/SSL，并给出对应的配置页面。"""

    def test_every_preset_is_wellformed(self):
        for name, preset in notify.MAIL_PRESETS.items():
            self.assertEqual(len(preset), 5, name)
            host, port, ssl, url, tip = preset
            self.assertIsInstance(port, int, name)
            self.assertIsInstance(ssl, bool, name)
            if name != "自定义":
                self.assertTrue(host and url and tip, name)

    def test_detect_by_host(self):
        self.assertEqual(notify.detect_mail_preset({"host": "smtp.qq.com"}), "QQ邮箱")
        self.assertEqual(notify.detect_mail_preset({"host": "SMTP.163.com"}), "163邮箱")
        self.assertEqual(notify.detect_mail_preset({"host": "smtp.example.com"}), "自定义")
        self.assertEqual(notify.detect_mail_preset({}), "自定义")

    def test_saved_preset_wins(self):
        self.assertEqual(
            notify.detect_mail_preset({"preset": "Gmail", "host": "smtp.qq.com"}), "Gmail")

    def test_removed_file_channel_is_gone(self):
        self.assertNotIn("file", notify.DEFAULT["channels"])
        self.assertNotIn("file", notify.CHANNEL_LABELS)
        self.assertFalse(hasattr(notify, "send_file"))
        self.assertFalse(hasattr(notify, "default_report_path"))


class TestRunOnceStateMachine(unittest.TestCase):
    """用假的取数结果验证「什么时候该提醒、什么时候别烦人」。"""

    def setUp(self):
        self.cfg = json.loads(json.dumps(notify.DEFAULT))
        self.cfg.update({"enabled": True, "daily_digest": False, "days_before": 3,
                         "repeat_hours": [24, 6, 1], "quiet_start": "", "quiet_end": "",
                         "min_gap_minutes": 20})
        self.cfg["channels"] = {k: False for k in notify.CHANNEL_LABELS}
        self.sent = []
        self.logs = []
        self.state = {}
        self.now = [1700000000000]

    def _items(self, hours, keys=None):
        keys = keys or [f"k{i}" for i in range(len(hours))]
        return [{"key": key, "title": f"作业{key}", "kind": "todo", "state": 0,
                 "rawState": "0", "endMs": self.now[0] + int(h * 3600 * 1000),
                 "courseId": "1", "courseName": "课程", "teacher": ""}
                for key, h in zip(keys, hours)]

    def _write_state(self, s):
        self.state = json.loads(json.dumps(s))

    def _dispatch(self, title, text, lines, html, items, cfg, now_ms):
        self.sent.append((title, [it["key"] for it in items]))
        return [("测试通道", "")]

    def run_with(self, hours, advance_hours=0.0, keys=None):
        self.now[0] += int(advance_hours * 3600 * 1000)
        items = self._items(hours, keys)
        with mock.patch.object(notify, "read_cfg", return_value=self.cfg), \
                mock.patch.object(notify, "read_state",
                                  side_effect=lambda: json.loads(json.dumps(self.state))), \
                mock.patch.object(notify, "write_state", side_effect=self._write_state), \
                mock.patch.object(notify, "fetch_items", return_value=(items, "")), \
                mock.patch.object(notify, "_dispatch", side_effect=self._dispatch), \
                mock.patch.object(notify, "log", side_effect=self.logs.append), \
                mock.patch.object(notify.time, "time", side_effect=lambda: self.now[0] / 1000):
            return notify.run_once()

    def test_only_notifies_on_stage_change(self):
        self.run_with([50])
        self.assertEqual(len(self.sent), 1)
        self.run_with([50])                      # 同一档位，不重复打扰
        self.assertEqual(len(self.sent), 1)
        self.run_with([20], advance_hours=30)    # 掉到 24 小时档
        self.assertEqual(len(self.sent), 2)
        self.run_with([5], advance_hours=15)     # 掉到 6 小时档
        self.assertEqual(len(self.sent), 3)
        self.run_with([0.5], advance_hours=4.5)  # 掉到 1 小时档
        self.assertEqual(len(self.sent), 4)

    def test_no_notify_outside_window(self):
        self.run_with([100])
        self.assertEqual(self.sent, [])

    def test_expired_not_notified(self):
        self.run_with([-3])
        self.assertEqual(self.sent, [])

    def test_quiet_hours_suppressed(self):
        self.cfg["quiet_start"] = "22:00"
        self.cfg["quiet_end"] = "23:59"
        self.cfg["quiet_ignore_within_hours"] = 0
        import datetime
        # 把“现在”挪到 22:30
        self.now[0] = int(datetime.datetime(2026, 5, 1, 22, 30).timestamp() * 1000)
        self.run_with([50])
        self.assertEqual(self.sent, [])
        self.assertTrue(any("免打扰" in m for m in self.logs))

    def test_quiet_hours_urgent_breaks_through(self):
        self.cfg["quiet_start"] = "22:00"
        self.cfg["quiet_end"] = "23:59"
        self.cfg["quiet_ignore_within_hours"] = 2
        import datetime
        self.now[0] = int(datetime.datetime(2026, 5, 1, 22, 30).timestamp() * 1000)
        self.run_with([1])
        self.assertEqual(len(self.sent), 1)

    def test_min_gap_blocks_too_soon(self):
        self.run_with([50])
        self.assertEqual(len(self.sent), 1)
        self.run_with([20], advance_hours=0.1)   # 10 分钟后又到点，但间隔不足
        self.assertEqual(len(self.sent), 1)

    def test_daily_digest_once_per_day(self):
        self.cfg["daily_digest"] = True
        self.cfg["daily_at"] = "00:00"
        self.run_with([50])
        self.assertEqual(len(self.sent), 1)
        self.run_with([50])                      # 同一天，汇总不再发
        self.assertEqual(len(self.sent), 1)

    def test_state_pruned_when_submitted(self):
        self.run_with([50])
        self.assertIn("k0", self.state["items"])
        self.run_with([40], advance_hours=1, keys=["k1"])   # k0 交掉了，只剩 k1
        self.assertNotIn("k0", self.state["items"])
        self.assertIn("k1", self.state["items"])


class _FakeSMTP(threading.Thread):
    """够 smtplib 用的最小 SMTP 服务器，用来真正验证邮件通道（不含 TLS）。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.messages = []
        self.rcpts = []
        self.error = ""

    def run(self):
        try:
            conn, _ = self.sock.accept()
        except OSError as e:
            self.error = str(e)
            return
        with conn:
            f = conn.makefile("rwb")

            def send(line):
                f.write((line + "\r\n").encode())
                f.flush()

            send("220 fake ESMTP")
            data_mode, buf = False, []
            while True:
                raw = f.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if data_mode:
                    if line == ".":
                        self.messages.append("\n".join(buf))
                        buf, data_mode = [], False
                        send("250 OK")
                    else:
                        buf.append(line[1:] if line.startswith("..") else line)
                    continue
                cmd = line.split(" ", 1)[0].upper()
                if cmd == "EHLO":
                    send("250-fake")
                    send("250-STARTTLS")
                    send("250 SIZE 10485760")
                elif cmd == "HELO":
                    send("250 fake")
                elif cmd == "MAIL":
                    send("250 OK")
                elif cmd == "RCPT":
                    self.rcpts.append(line.split(":", 1)[-1].strip().strip("<>"))
                    send("250 OK")
                elif cmd == "DATA":
                    data_mode = True
                    send("354 go ahead")
                elif cmd == "QUIT":
                    send("221 Bye")
                    break
                else:
                    send("250 OK")


class TestEmailChannel(unittest.TestCase):
    def test_send_email_end_to_end(self):
        import smtplib
        from email import message_from_string
        from email.header import decode_header, make_header

        srv = _FakeSMTP()
        srv.start()
        cfg = json.loads(json.dumps(notify.DEFAULT))
        cfg["email"] = {"host": "127.0.0.1", "port": srv.port, "ssl": False,
                        "user": "", "password": "", "to": "a@example.com, b@example.com"}
        # 假服务器声明了 STARTTLS 但没法真的握手，这里把它打桩成成功
        with mock.patch.object(smtplib.SMTP, "starttls", lambda self, *a, **k: (220, b"OK")):
            err = notify.send_email("【测试】作业提醒", "纯文本正文", "<p>HTML 正文</p>", cfg)
        self.assertEqual(err, "")
        srv.join(timeout=5)

        self.assertEqual(srv.error, "")
        self.assertEqual(sorted(srv.rcpts), ["a@example.com", "b@example.com"])
        self.assertEqual(len(srv.messages), 1)
        msg = message_from_string(srv.messages[0])
        subject = str(make_header(decode_header(msg["Subject"])))
        self.assertIn("作业提醒", subject)
        self.assertIn("a@example.com", msg["To"])
        parts = [p.get_payload(decode=True).decode("utf-8")
                 for p in msg.walk() if p.get_content_type() in ("text/plain", "text/html")]
        self.assertIn("纯文本正文", parts)
        self.assertIn("<p>HTML 正文</p>", parts)

    def test_missing_config_reports_clearly(self):
        cfg = json.loads(json.dumps(notify.DEFAULT))
        self.assertIn("未配置", notify.send_email("x", "y", "<p>z</p>", cfg))


class TestSettingsDialogLayout(unittest.TestCase):
    """设置窗口的布局回归测试。

    踩过的坑：通道勾选框原来写成 `grid(row=0, column=i % 3)`，
    6 个框全挤在第 0 行，「微信推送」正好盖在「Windows 系统通知」上。
    """

    def setUp(self):
        try:
            import tkinter as tk
            r = tk.Tk()
            r.withdraw()
            r.destroy()
        except Exception as e:                       # 无图形环境就跳过
            self.skipTest(f"没有图形环境：{e}")

    def _dialog_widgets(self, dlg):
        out, stack = [], [dlg]
        while stack:
            cur = stack.pop()
            for c in cur.winfo_children():
                stack.append(c)
                out.append(c)
        return out

    def test_channel_checkboxes_do_not_overlap(self):
        import tkinter as tk
        import 作业汇总助手 as app

        a = app.App(offline=True)
        try:
            a.root.update()
            a._notify_settings()
            a.root.update()
            dlg = next(w for w in a.root.winfo_children() if isinstance(w, tk.Toplevel))
            dlg.update_idletasks()

            labels = set(notify.CHANNEL_LABELS.values())
            boxes = []
            for c in self._dialog_widgets(dlg):
                try:
                    if c.winfo_class() == "TCheckbutton" and str(c.cget("text")) in labels:
                        boxes.append((str(c.cget("text")), c.winfo_x(), c.winfo_y(),
                                      c.winfo_width(), c.winfo_height()))
                except Exception:
                    pass

            self.assertEqual(len(boxes), len(notify.CHANNEL_LABELS),
                             "没找齐所有通道勾选框")
            for i in range(len(boxes)):
                for j in range(i + 1, len(boxes)):
                    _, x1, y1, w1, h1 = boxes[i]
                    _, x2, y2, w2, h2 = boxes[j]
                    ox = min(x1 + w1, x2 + w2) - max(x1, x2)
                    oy = min(y1 + h1, y2 + h2) - max(y1, y2)
                    self.assertFalse(ox > 3 and oy > 3,
                                     f"勾选框重叠 {ox}x{oy}px：{boxes[i][0]} <-> {boxes[j][0]}")

            # 整个窗口不能超出屏幕，否则按钮会被挤出可见区域
            self.assertLessEqual(dlg.winfo_reqwidth(), dlg.winfo_screenwidth())
            self.assertLessEqual(dlg.winfo_reqheight(), dlg.winfo_screenheight())
        finally:
            a.shutdown()

    def test_wechat_button_opens_provider_site(self):
        import tkinter as tk
        import 作业汇总助手 as app

        opened = []
        real_open = notify.core.webbrowser.open
        notify.core.webbrowser.open = lambda url: opened.append(url) or True
        a = app.App(offline=True)
        try:
            a.root.update()
            a._notify_settings()
            a.root.update()
            dlg = next(w for w in a.root.winfo_children() if isinstance(w, tk.Toplevel))
            dlg.update_idletasks()

            widgets = self._dialog_widgets(dlg)
            btn = next(c for c in widgets
                       if c.winfo_class() == "TButton"
                       and "打开配置页面" in str(c.cget("text")))
            combo = next(c for c in widgets
                         if c.winfo_class() == "TCombobox"
                         and "serverchan" in str(c.cget("values")))
            for provider in ("pushplus", "serverchan"):
                combo.set(provider)
                dlg.update()
                btn.invoke()
                dlg.update()
            self.assertEqual(opened, [notify.WX_SITES["pushplus"][0],
                                      notify.WX_SITES["serverchan"][0]])
        finally:
            notify.core.webbrowser.open = real_open
            a.shutdown()


class TestCheckmarkStyle(unittest.TestCase):
    """勾选框必须是「对勾」，不能是「叉」。

    ttk 的 clam 主题在已勾选时画的是一个 X，很容易被当成「取消 / 关闭」，
    所以项目自绘了一个对勾图片替换掉它。这里盯住两件事：
    元素确实装进了布局，以及画出来的形状真的是对勾而不是叉。
    """

    def setUp(self):
        try:
            import tkinter as tk
            r = tk.Tk()
            r.withdraw()
            r.destroy()
        except Exception as e:
            self.skipTest(f"没有图形环境：{e}")

    def test_element_installed_into_layout(self):
        import tkinter as tk
        import 作业汇总助手 as app

        root = tk.Tk()
        root.withdraw()
        try:
            style = app.apply_theme(root)
            self.assertEqual(style.theme_use(), "clam")
            spec = str(style.layout("TCheckbutton"))
            self.assertIn("Maid.Checkbutton.indicator", spec)
            self.assertNotIn("Checkbutton.indicator'", spec.replace(
                "Maid.Checkbutton.indicator", ""))
        finally:
            root.destroy()

    def test_shape_is_a_check_not_a_cross(self):
        import 作业汇总助手 as app

        size = 14
        dots = app._check_pixels(size)
        # 对勾：左上、右下是空的；左下（短笔）、右上（长笔）有墨
        for x in range(1, 6):
            for y in range(1, 6):
                self.assertNotIn((x, y), dots, "左上角不该有笔画（那是叉）")
        for x in range(9, 13):
            for y in range(9, 13):
                self.assertNotIn((x, y), dots, "右下角不该有笔画（那是叉）")
        self.assertTrue(any((x, y) in dots for x in range(1, 6) for y in range(8, 13)),
                        "左下缺少短笔画")
        self.assertTrue(any((x, y) in dots for x in range(9, 13) for y in range(3, 7)),
                        "右上缺少长笔画")

    def test_images_are_kept_alive(self):
        import tkinter as tk
        import 作业汇总助手 as app

        root = tk.Tk()
        root.withdraw()
        try:
            before = len(app._CHECK_IMAGES)
            app.apply_theme(root)
            self.assertGreater(len(app._CHECK_IMAGES), before,
                               "PhotoImage 必须留引用，否则会被 Tk 回收")
        finally:
            root.destroy()


class TestActiveHours(unittest.TestCase):
    """计划任务每天跑多久 = 从「每天几点起」到「免打扰开始」，界面上不再暴露这个参数。"""

    def test_derived_from_quiet_start(self):
        self.assertEqual(notify.active_hours({"daily_at": "08:30", "quiet_start": "22:30"}), 14)
        self.assertEqual(notify.active_hours({"daily_at": "09:00", "quiet_start": "21:00"}), 12)
        self.assertEqual(notify.active_hours({"daily_at": "07:00", "quiet_start": "19:00"}), 12)

    def test_falls_back_when_quiet_disabled_or_inverted(self):
        self.assertEqual(notify.active_hours({"daily_at": "08:30", "quiet_start": ""}), 14)
        self.assertEqual(notify.active_hours({"daily_at": "08:30", "quiet_start": "07:00"}), 14)
        self.assertEqual(notify.active_hours({}), 14)

    def test_clamped_to_one_day(self):
        self.assertEqual(notify.active_hours({"daily_at": "00:00", "quiet_start": "23:59"}), 24)


class TestPushProviders(unittest.TestCase):
    """推送服务商：免费的先排前面（PushPlus 现在要实名收费）。"""

    def test_free_webhooks_come_first(self):
        self.assertEqual(list(notify.WX_SITES)[:3], ["wecom", "dingtalk", "feishu"])

    def test_every_provider_is_wellformed(self):
        for name, preset in notify.WX_SITES.items():
            self.assertEqual(len(preset), 3, name)
            url, field, tip = preset
            self.assertTrue(field and tip, name)
            if name != "自定义":
                self.assertTrue(url.startswith("http"), name)

    def test_default_provider_is_free(self):
        self.assertEqual(notify.DEFAULT["wechat"]["provider"], "wecom")

    def test_webhook_reply_check(self):
        class R:
            def __init__(self, payload):
                self._p = payload
                self.text = str(payload)

            def json(self):
                return self._p

        self.assertEqual(notify._webhook_ok("企业微信", R({"errcode": 0, "errmsg": "ok"})), "")
        self.assertIn("异常", notify._webhook_ok("企业微信", R({"errcode": 93000, "errmsg": "bad"})))
        self.assertIn("异常", notify._webhook_ok("钉钉", R({"code": 300001, "message": "invalid"})))
        self.assertEqual(notify._webhook_ok("飞书", R({"StatusCode": 0})), "")

    def test_dispatch_records_last_results(self):
        cfg = json.loads(json.dumps(notify.DEFAULT))
        cfg["channels"] = {k: False for k in notify.CHANNEL_LABELS}
        cfg["channels"]["toast"] = True
        with mock.patch.object(notify, "send_toast", return_value=""), \
                mock.patch.object(notify, "ensure_icon", return_value=""):
            notify._dispatch("t", "x", ["l"], "<p>x</p>", [], cfg, 0)
        self.assertEqual(notify.LAST_RESULTS, [("系统通知", "")])


class TestElevationSafeOpen(unittest.TestCase):
    """提权运行时直接开浏览器，浏览器会继承管理员权限，Edge 会弹
    「现有实例正在以提升的权限运行」——所以要绕开 explorer.exe。"""

    def test_uses_browser_when_not_elevated(self):
        import 作业汇总助手 as app

        browser = []
        with mock.patch.object(app, "is_elevated", return_value=False), \
                mock.patch.object(app.webbrowser, "open",
                                  side_effect=lambda u: browser.append(u) or True):
            self.assertTrue(app.open_url("https://example.com"))
        self.assertEqual(browser, ["https://example.com"])

    def test_uses_explorer_when_elevated(self):
        import 作业汇总助手 as app

        popen, browser = [], []
        with mock.patch.object(app, "is_elevated", return_value=True), \
                mock.patch.object(app.subprocess, "Popen",
                                  side_effect=lambda a, **k: popen.append(a)), \
                mock.patch.object(app.webbrowser, "open",
                                  side_effect=lambda u: browser.append(u) or True):
            self.assertTrue(app.open_url("https://example.com"))
        self.assertEqual(popen, [["explorer.exe", "https://example.com"]])
        self.assertEqual(browser, [], "提权时不该直接调 webbrowser")

    def test_falls_back_to_browser_if_explorer_fails(self):
        import 作业汇总助手 as app

        browser = []
        with mock.patch.object(app, "is_elevated", return_value=True), \
                mock.patch.object(app.subprocess, "Popen", side_effect=OSError("nope")), \
                mock.patch.object(app.webbrowser, "open",
                                  side_effect=lambda u: browser.append(u) or True):
            self.assertTrue(app.open_url("https://example.com"))
        self.assertEqual(browser, ["https://example.com"])


class TestTaskScheduleArgs(unittest.TestCase):
    """计划任务参数：Windows 要求 /DU（持续时长）严格大于 /RI（重复间隔）。

    踩过的坑：检查间隔 24h、活跃窗口 15h 时，原来的代码照样塞
    /RI 1440 /DU 900，schtasks 直接拒绝：
    「错误: 持续时间 (900m) 必须大于重复间隔时间 (1440m)。」
    """

    def _args_for(self, interval_hours, daily_at="08:30", quiet_start="23:59"):
        cfg = dict(notify.DEFAULT)
        cfg["daily_at"] = daily_at
        cfg["quiet_start"] = quiet_start
        cfg["interval_hours"] = interval_hours
        captured = {}

        def fake_run(args, **kw):
            captured["args"] = args
            return 0, "SUCCESS"

        with mock.patch.object(notify, "_run", side_effect=fake_run), \
                mock.patch.object(notify, "_fix_task_settings", return_value=""):
            ok, msg = notify.install_task(cfg)
        self.assertTrue(ok, msg)
        return captured["args"], msg

    def test_never_emits_du_not_greater_than_ri(self):
        """任何组合下，只要同时出现 /RI 和 /DU，就必须 DU > RI。"""
        for interval in range(1, 25):
            for daily, quiet in (("08:30", "23:59"), ("07:00", "22:00"),
                                 ("09:00", "09:30"), ("22:00", "07:00")):
                args, _ = self._args_for(interval, daily, quiet)
                if "/RI" in args:
                    ri = int(args[args.index("/RI") + 1])
                    du = args[args.index("/DU") + 1]
                    h, m = (int(x) for x in du.split(":"))
                    self.assertGreater(h * 60 + m, ri,
                                       f"interval={interval} {daily}->{quiet} RI={ri} DU={du}")

    def test_long_interval_falls_back_to_once_a_day(self):
        """间隔 24h、窗口 15h：不该带 /RI（带了一定非法），改成每天一次。"""
        args, msg = self._args_for(24)
        self.assertNotIn("/RI", args)
        self.assertNotIn("/DU", args)
        self.assertIn("/SC", args)
        self.assertEqual(args[args.index("/SC") + 1], "DAILY")
        self.assertEqual(args[args.index("/ST") + 1], "08:30")
        self.assertIn("每天", msg)

    def test_short_interval_still_repeats(self):
        args, msg = self._args_for(3)
        self.assertIn("/RI", args)
        self.assertEqual(args[args.index("/RI") + 1], "180")
        self.assertIn("/DU", args)
        self.assertIn("每 3 小时", msg)


class TestTestButtonDoesNotTouchTask(unittest.TestCase):
    """「测试通知」只该保存设置并发送，不该去注册计划任务。

    踩过的坑：原来 _on_test 调 _save(True)，注册计划任务一旦失败就弹错误框，
    把「测一下能不能收到通知」这件事整个挡住了。
    """

    def test_on_test_does_not_register_task(self):
        import time
        import tkinter as tk

        import 作业汇总助手 as app

        calls, sent = [], []

        a = app.App(offline=True)
        a.root.update()
        a._notify_settings()
        a.root.update()
        dlg = next(w for w in a.root.winfo_children() if isinstance(w, tk.Toplevel))

        def walk(w):
            yield w
            for c in w.winfo_children():
                yield from walk(c)

        btns = [w for w in walk(dlg) if w.winfo_class() == "TButton"]

        def fake_install(*a_, **k):
            calls.append(1)
            return False, "不该被调用"

        with mock.patch.object(notify, "install_task", side_effect=fake_install), \
                mock.patch.object(notify, "run_once", side_effect=lambda *a_, **k: sent.append(1)), \
                mock.patch("tkinter.messagebox.showerror") as mb:
            for b in btns:
                if "测试" in str(b.cget("text")):
                    b.invoke()
                    break
            else:
                a.shutdown()
                self.fail("没找到「测试通知」按钮")
            for _ in range(60):
                if sent:
                    break
                time.sleep(0.05)
            a.root.update()

        a.shutdown()
        self.assertEqual(calls, [], "测试通知不该去注册计划任务")
        self.assertTrue(sent, "测试通知应该真的发出去")
        self.assertFalse(mb.showerror.called, "测试通知不该弹注册失败的错误框")


if __name__ == "__main__":
    unittest.main()
