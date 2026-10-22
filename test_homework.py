# -*- coding: utf-8 -*-
"""作业汇总助手 单元测试（不联网）：py -m unittest -v test_homework"""
import importlib.util
import tempfile
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("hwmod", HERE / "作业汇总助手.py")
hw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hw)

DAY = hw.DAY_MS
NOW = int(time.time() * 1000)


class FakeResp:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self._payload = payload
        self.ok = 200 <= status < 400

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, routes=None, cookies=None):
        self.routes = routes or {}
        self.cookies = cookies or []
        self.calls = []

    def _route(self, url, params=None):
        r = self.routes[url]
        if callable(r):
            r = r(params or {})
        if isinstance(r, tuple):
            return FakeResp(r[1], r[0])
        return FakeResp(r)

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append(("GET", url, dict(params or {})))
        return self._route(url, params)

    def post(self, url, data=None, json=None, headers=None, timeout=None,
             allow_redirects=None, **kw):
        self.calls.append(("POST", url, data if data is not None else json))
        return self._route(url)


class TestHelpers(unittest.TestCase):
    def test_to_ms(self):
        self.assertEqual(hw.to_ms(1760000000000), 1760000000000)
        self.assertEqual(hw.to_ms(1760000000), 1760000000000)
        self.assertEqual(hw.to_ms("1760000000"), 1760000000000)
        self.assertEqual(hw.to_ms("1760000000000"), 1760000000000)
        self.assertEqual(hw.to_ms(""), 0)
        self.assertEqual(hw.to_ms(None), 0)
        self.assertEqual(hw.to_ms(False), 0)
        self.assertEqual(hw.to_ms(True), 0)
        self.assertEqual(hw.to_ms("不是时间"), 0)

    def test_to_ms_iso(self):
        got = hw.to_ms("2026-10-08T12:00:00")
        self.assertEqual(got, int(hw.datetime(2026, 10, 8, 12, 0).timestamp() * 1000))

    def test_normalize_states(self):
        course = {"id": "101", "name": "高等数学", "teacher": "张老师"}
        self.assertEqual(hw.normalize({"id": 1, "title": "作业一", "state": 0,
                                       "endTime": NOW + DAY}, course)["kind"], "todo")
        self.assertEqual(hw.normalize({"id": 2, "title": "作业二", "state": 1}, course)["kind"], "review")
        self.assertEqual(hw.normalize({"id": 3, "title": "作业三", "state": 2}, course)["kind"], "review")
        self.assertIsNone(hw.normalize({"id": 4, "title": "作业四", "state": 3}, course))
        self.assertIsNone(hw.normalize({"id": 5, "title": "作业五", "state": "已提交"}, course))
        self.assertEqual(hw.normalize({"id": 6, "title": "作业六", "state": "1"}, course)["kind"], "review")
        self.assertEqual(hw.normalize({"id": 7, "title": "作业七", "state": "未提交"}, course)["kind"], "todo")
        self.assertEqual(hw.normalize({"id": 8, "title": "作业八", "state": "互评中"}, course)["kind"], "review")

    def test_normalize_aliases_and_key(self):
        course = {"id": "101", "name": "高等数学", "teacher": "张老师"}
        item = hw.normalize({"relationId": 9, "title": "英文标题", "status": "0",
                             "deadline": NOW + DAY}, course)
        self.assertEqual(item["kind"], "todo")
        self.assertEqual(item["key"], "9|101")
        self.assertEqual(item["courseName"], "高等数学")
        self.assertEqual(hw.normalize({"state": 0}, course)["title"], "未命名作业")

    def test_pick_list(self):
        payload = {"code": 1, "result": {"courseList": [{"id": 1}, {"id": 2}]}}
        self.assertEqual(len(hw.pick_list(payload, "courseList")), 2)
        payload = {"code": 1, "rows": {"records": [{"id": 1}]}}
        self.assertEqual(len(hw.pick_list(payload, "homeworkList")), 1)
        payload = {"code": 1, "list": [{"id": 1}]}
        self.assertEqual(len(hw.pick_list(payload, "homeworkList")), 1)
        self.assertEqual(hw.pick_list({"code": 1}, "homeworkList"), [])
        self.assertEqual(hw.pick_list(None, "courseList"), [])

    def test_looks_auth_error(self):
        self.assertTrue(hw.looks_auth_error({"code": 401, "message": "未登录"}))
        self.assertTrue(hw.looks_auth_error({"code": 500, "msg": "登录已过期"}))
        self.assertFalse(hw.looks_auth_error(
            {"code": 1, "result": {"courseList": [{"message": "未登录"}]}}))
        self.assertFalse(hw.looks_auth_error({"code": 1, "result": {}}))


class TestFilter(unittest.TestCase):
    def setUp(self):
        self.item = {"key": "k1", "title": "高等数学第一周作业", "kind": "todo",
                     "endMs": NOW + 2 * DAY, "courseId": "101", "courseName": "高等数学"}
        self.cfg = {"hidden": [], "exclude": "", "days": 14}

    def test_basic_visible(self):
        self.assertTrue(hw.is_visible(self.item, self.cfg, NOW))

    def test_hidden(self):
        self.cfg["hidden"] = ["k1"]
        self.assertFalse(hw.is_visible(self.item, self.cfg, NOW))

    def test_exclude_keyword(self):
        self.cfg["exclude"] = "英语, 实验"
        self.assertTrue(hw.is_visible(self.item, self.cfg, NOW))
        self.item["title"] = "大学英语听说作业"
        self.assertFalse(hw.is_visible(self.item, self.cfg, NOW))

    def test_days_filter(self):
        self.cfg["days"] = 1
        self.assertFalse(hw.is_visible(self.item, self.cfg, NOW))
        self.item["endMs"] = NOW + 12 * 3600000
        self.assertTrue(hw.is_visible(self.item, self.cfg, NOW))
        self.cfg["days"] = 0
        self.assertTrue(hw.is_visible(self.item, self.cfg, NOW))

    def test_expired_and_no_deadline_always_shown(self):
        self.cfg["days"] = 1
        self.item["endMs"] = NOW - 3 * DAY
        self.assertTrue(hw.is_visible(self.item, self.cfg, NOW))
        self.item["endMs"] = 0
        self.assertTrue(hw.is_visible(self.item, self.cfg, NOW))


class TestFormat(unittest.TestCase):
    def test_fmt_left(self):
        self.assertEqual(hw.fmt_left(90 * 60000), "还剩 1小时30分")
        self.assertEqual(hw.fmt_left(2 * DAY + 3600000), "还剩 2天1小时")
        self.assertTrue(hw.fmt_left(-60000).startswith("已过期"))
        self.assertTrue(hw.fmt_left(-(3 * DAY + 3600000)).startswith("已过期 3天"))

    def test_countdown_no_deadline(self):
        item = {"endMs": 0}
        self.assertEqual(hw.countdown_text(item, NOW), "无截止时间")

    def test_fmt_datetime(self):
        self.assertEqual(hw.fmt_datetime(0), "")
        self.assertEqual(len(hw.fmt_datetime(NOW)), 16)


COURSES = {"code": 1, "result": {"courseList": [
    {"id": 101, "name": "高等数学", "teacherName": "张老师"},
    {"id": 102, "name": "大学英语", "teacherName": "李老师"},
]}}


class TestApi(unittest.TestCase):
    def test_fetch_courses(self):
        hw_list_101 = {"code": 1, "data": {"homeworkList": [
            {"id": 1, "homeworkTitle": "预习作业", "state": 0, "endTime": NOW + DAY},
            {"id": 2, "homeworkTitle": "口语互评", "state": 1, "endTime": NOW + 5 * DAY},
            {"id": 3, "homeworkTitle": "已交作业", "state": 9},
        ]}}
        routes = {
            hw.COURSES_URL: COURSES,
            hw.HOMEWORKS_URL: lambda p: (hw_list_101 if str(p.get("ocId")) == "101"
                                         else {"code": 1, "data": {"homeworkList": []}}),
        }
        session = FakeSession(routes)
        courses = hw.fetch_courses(session)
        self.assertEqual(len(courses), 2)
        self.assertEqual(courses[0]["name"], "高等数学")
        self.assertEqual(courses[0]["id"], "101")
        items = []
        for c in courses:
            lst, _ = hw.fetch_homeworks(session, c)
            for raw in lst:
                item = hw.normalize(raw, c)
                if item:
                    items.append(item)
        self.assertEqual([i["kind"] for i in items], ["todo", "review"])

    def test_scan(self):
        hw_list = {"code": 1, "data": {"homeworkList": [
            {"id": 1, "homeworkTitle": "作业一", "state": 0, "endTime": NOW + DAY},
            {"id": 4, "homeworkTitle": "已过期作业", "state": 0, "endTime": NOW - DAY},
            {"id": 5, "homeworkTitle": "无期限作业", "state": 2},
            {"id": 6, "homeworkTitle": "完成的", "state": 3},
        ]}}
        routes = {
            hw.COURSES_URL: COURSES,
            hw.HOMEWORKS_URL: lambda p: (hw_list if str(p.get("ocId")) == "101"
                                         else {"code": 1, "data": {"homeworkList": []}}),
        }
        notes = []
        result = hw.scan(FakeSession(routes), progress=notes.append)
        self.assertEqual(len(result["courses"]), 2)
        self.assertEqual(len(result["items"]), 3)
        self.assertTrue(any("读取" in n for n in notes))
        # 有截止时间的排前面，无期限排最后
        self.assertEqual(result["items"][-1]["title"], "无期限作业")
        self.assertEqual(result["failures"], [])

    def test_auth_error_raises(self):
        routes = {hw.COURSES_URL: (401, {"message": "未登录"})}
        with self.assertRaises(hw.ApiError) as ctx:
            hw.fetch_courses(FakeSession(routes))
        self.assertTrue(ctx.exception.auth)

    def test_auth_error_in_body(self):
        routes = {hw.COURSES_URL: {"code": 401, "message": "登录已过期"}}
        with self.assertRaises(hw.ApiError) as ctx:
            hw.fetch_courses(FakeSession(routes))
        self.assertTrue(ctx.exception.auth)

    def test_login_success(self):
        session = FakeSession({hw.LOGIN_URL: {"code": 1}}, cookies=[
            types.SimpleNamespace(name="AUTHORIZATION", value="a%2Fb%3Dc"),
        ])
        token = hw.login(session, "20230000", "secret")
        self.assertEqual(token, "a/b=c")
        method, url, data = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(data["loginName"], "20230000")

    def test_login_no_token(self):
        session = FakeSession({hw.LOGIN_URL: {"code": 0, "msg": "需要验证码"}})
        with self.assertRaises(hw.ApiError) as ctx:
            hw.login(session, "u", "p")
        self.assertIn("AUTHORIZATION", str(ctx.exception))

    def test_verify_ok(self):
        session = FakeSession({hw.IDENTITY_URL: {
            "code": 1, "result": {"userId": 12345, "realName": "王小明"}}})
        ident = hw.verify(session, "token")
        self.assertEqual(ident["userId"], 12345)
        self.assertEqual(ident["name"], "王小明")

    def test_verify_failures(self):
        for payload in ({"code": 500}, {"code": 1, "result": {"userId": "abc"}},
                        {"code": 1, "result": {}}):
            session = FakeSession({hw.IDENTITY_URL: payload})
            with self.assertRaises(hw.ApiError) as ctx:
                hw.verify(session, "token")
            self.assertTrue(ctx.exception.auth)


class TestStorage(unittest.TestCase):
    def test_account_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "account.json"
            hw.write_account(path, "20230000", "pw中文", "tok/中文=值")
            acc = hw.read_account(path)
            self.assertEqual(acc["username"], "20230000")
            self.assertEqual(acc["password"], "pw中文")
            self.assertEqual(acc["token"], "tok/中文=值")
            # 密文不是明文
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("pw中文", raw)
            hw.write_account(path, "u", "", "")
            acc = hw.read_account(path)
            self.assertEqual(acc["password"], "")
            self.assertEqual(acc["token"], "")

    def test_account_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            acc = hw.read_account(Path(td) / "nope.json")
            self.assertEqual(acc, {"username": "", "password": "", "token": ""})

    def test_cfg_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            hw.write_cfg(path, {**hw.DEFAULT_CFG, "days": 7, "exclude": "英语",
                                "hidden": ["a"], "tab": "review"})
            cfg = hw.read_cfg(path)
            self.assertEqual(cfg["days"], 7)
            self.assertEqual(cfg["exclude"], "英语")
            self.assertEqual(cfg["hidden"], ["a"])
            self.assertEqual(cfg["tab"], "review")
            self.assertTrue(cfg["show_expired"])

    def test_cfg_defaults_on_bad_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text("not json", encoding="utf-8")
            cfg = hw.read_cfg(path)
            self.assertEqual(cfg["days"], hw.DEFAULT_CFG["days"])
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text('{"days": "abc", "hidden": "x"}', encoding="utf-8")
            cfg = hw.read_cfg(path)
            self.assertEqual(cfg["days"], hw.DEFAULT_CFG["days"])
            self.assertEqual(cfg["hidden"], [])


class TestGuiSmoke(unittest.TestCase):
    def test_offline_app_render(self):
        with tempfile.TemporaryDirectory() as td:
            app = hw.App(offline=True, base_dir=Path(td))
            app.items = [
                {"key": "a|1", "title": "待交作业", "kind": "todo", "state": 0,
                 "rawState": "0", "endMs": NOW + 2 * DAY, "courseId": "1",
                 "courseName": "高等数学", "teacher": ""},
                {"key": "b|1", "title": "互评作业", "kind": "review", "state": 1,
                 "rawState": "1", "endMs": NOW + 3 * DAY, "courseId": "1",
                 "courseName": "高等数学", "teacher": ""},
            ]
            app.courses = [{"id": "1", "name": "高等数学", "teacher": ""}]
            app._by_key = {i["key"]: i for i in app.items}
            app._render()
            self.assertEqual(len(app.trees["todo"].get_children()), 1)
            self.assertEqual(len(app.trees["review"].get_children()), 1)
            self.assertIn("待提交 (1)", app.notebook.tab(app.tab_frames["todo"], "text"))

            # 隐藏后重渲染
            app.cfg["hidden"] = ["a|1"]
            app._render()
            self.assertEqual(len(app.trees["todo"].get_children()), 0)
            app.cfg["hidden"] = []
            app._render()

            app.root.update()
            app.root.destroy()
            # 配置已写入临时目录
            self.assertTrue((Path(td) / "config.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
