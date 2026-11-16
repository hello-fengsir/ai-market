#!/usr/bin/env python3
"""标讯配置台回归自测。

覆盖的都是历史上真实踩过的坑，改动代码后请务必重跑：
    python3 selftest.py
全绿（OK）才算可发布。
"""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import channels as channels_mod  # noqa: E402
import engine  # noqa: E402
import fetcher  # noqa: E402
import render  # noqa: E402
import scheduler as scheduler_mod  # noqa: E402
import server  # noqa: E402


class TimeWindowTest(unittest.TestCase):
    """时间基准必须是“当前时间”，不能是模块加载时刻。"""

    def test_time_is_not_frozen(self):
        self.assertTrue(callable(engine.now))
        # 两次调用之间必须能取到各自的时间对象
        a, b = engine.now(), engine.now()
        self.assertIsInstance(a, dt.datetime)
        self.assertIsInstance(b, dt.datetime)

    def test_expired_item_excluded(self):
        past = (dt.datetime.now() - dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
        item = {"id": "x", "deadline_dt": past}
        cond = {"field": "deadline_dt", "op": "window", "value": {"from": "now"}}
        self.assertFalse(engine._cond_match(item, cond), "已过期公告不应通过“未截止”条件")

    def test_future_item_included(self):
        future = (dt.datetime.now() + dt.timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
        item = {"id": "x", "deadline_dt": future}
        cond = {"field": "deadline_dt", "op": "window", "value": {"from": "now"}}
        self.assertTrue(engine._cond_match(item, cond))

    def test_window_works_without_budget_num(self):
        """无 budget_num 字段时，时间窗条件仍必须生效（旧实现会直接 return False）。"""
        future = (dt.datetime.now() + dt.timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
        item = {"id": "x", "deadline_dt": future}  # 故意不给 budget_num
        cond = {"field": "deadline_dt", "op": "window", "value": {"from": "now"}}
        self.assertTrue(engine._cond_match(item, cond))
        self.assertNotIn("budget_num", item)

    def test_file_end_window_also_works(self):
        future = (dt.datetime.now() + dt.timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        item = {"id": "x", "file_end_dt": future}
        cond = {"field": "file_end_dt", "op": "window", "value": {"from": "now"}}
        self.assertTrue(engine._cond_match(item, cond))

    def test_chinese_date_format_parsed(self):
        item = {"id": "x", "deadline_dt": "2026年12月31日 10:00"}
        self.assertIsNotNone(engine.parse_dt(item["deadline_dt"]))


class RuleEngineTest(unittest.TestCase):
    def test_invalid_regex_does_not_crash(self):
        cond = {"field": "title", "op": "regex", "value": "([unclosed"}
        self.assertFalse(engine._cond_match({"title": "服务器采购"}, cond))

    def test_text_pseudo_field(self):
        item = {"title": "GPU 服务器采购", "summary": "算力中心", "purchaser": "某大学"}
        cond = {"field": "text", "op": "contains_any", "value": ["大学"]}
        self.assertTrue(engine._cond_match(item, cond))

    def test_budget_with_commas(self):
        item = {"budget": "1,200,000元"}
        cond = {"field": "budget", "op": "gte", "value": 1000000}
        self.assertTrue(engine._cond_match(item, cond))

    def test_between_bad_value_returns_false(self):
        cond = {"field": "budget_num", "op": "between", "value": "oops"}
        self.assertFalse(engine._cond_match({"budget_num": 5}, cond))

    def test_exclude_or_logic(self):
        rule = {
            "include": [],
            "exclude": [{"field": "purchaser", "op": "contains_any", "value": ["公安局", "政府"]}],
        }
        ok, reasons = engine.evaluate({"purchaser": "某市公安局"}, rule)
        self.assertFalse(ok)
        self.assertTrue(reasons)


class DedupeAndLimitTest(unittest.TestCase):
    """去重必须先于 limit 截断，否则真实数据下新项永远被老项挤掉。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _items(self, n):
        return [
            {"id": f"i{k}", "title": f"标讯{k}",
             "deadline_dt": (dt.datetime.now() + dt.timedelta(days=k + 1)).isoformat()}
            for k in range(n)
        ]

    def test_limit_applied_after_dedupe(self):
        task = {"id": "t-limit", "limit": 3, "rule": {"include": [], "exclude": []}}
        items = self._items(10)
        # 前 7 条已发送，最新的是 i7..i9
        st = {"sent": [f"i{k}" for k in range(7)]}
        engine.save_state(self.state / "t-limit.json", st)

        res = engine.run_task(task, items, self.state)
        fresh_ids = [m["id"] for m in res["fresh"]]
        self.assertEqual(len(fresh_ids), 3, f"应取到 3 条新项，实际 {fresh_ids}")
        self.assertEqual(set(fresh_ids), {"i7", "i8", "i9"})
        self.assertEqual(res["matched_total"], 10)

    def test_commit_sent_marks_items(self):
        task = {"id": "t-commit", "limit": 8, "rule": {"include": [], "exclude": []}}
        items = self._items(2)
        res = engine.run_task(task, items, self.state)
        self.assertEqual(len(res["fresh"]), 2)
        engine.commit_sent(task, res["fresh"], self.state)
        res2 = engine.run_task(task, items, self.state)
        self.assertEqual(len(res2["fresh"]), 0, "已推送项不应再次进入 fresh")

    def test_force_ignores_dedupe(self):
        task = {"id": "t-force", "limit": 8, "rule": {"include": [], "exclude": []}}
        items = self._items(2)
        engine.commit_sent(task, items, self.state)
        res = engine.run_task(task, items, self.state, force=True)
        self.assertEqual(len(res["fresh"]), 2)

    def test_path_traversal_rejected(self):
        task = {"id": "../../etc/passwd", "limit": 1, "rule": {}}
        with self.assertRaises(ValueError):
            engine.run_task(task, [], self.state)


class CronTest(unittest.TestCase):
    def test_valid_expression(self):
        self.assertTrue(scheduler_mod.validate("0 9 * * 1-5"))
        self.assertTrue(scheduler_mod.validate("*/15 8-18 * * *"))

    def test_invalid_expressions_rejected(self):
        for bad in ["", "0 9 * *", "60 9 * * *", "0 9 * * 9", "a b c d e", "0 0 0 * *"]:
            with self.assertRaises(scheduler_mod.CronError, msg=f"应拒绝: {bad}"):
                scheduler_mod.validate(bad)

    def test_matches_weekday(self):
        friday = dt.datetime(2026, 11, 13, 9, 0)
        saturday = dt.datetime(2026, 11, 14, 9, 0)
        self.assertTrue(scheduler_mod.cron_matches("0 9 * * 1-5", friday))
        self.assertFalse(scheduler_mod.cron_matches("0 9 * * 1-5", saturday))

    def test_invalid_expr_returns_false_not_crash(self):
        self.assertFalse(scheduler_mod.cron_matches("bad expr", dt.datetime.now()))

    def test_status_exposes_scheduler_observability(self):
        s = scheduler_mod.Scheduler(lambda: [], lambda tid: None)
        status = s.status()
        self.assertFalse(status["running"])
        self.assertIsNone(status["last_check"])
        self.assertEqual(status["last_result"], {})

    def test_same_minute_fires_once(self):
        fired = []

        class T:
            pass

        s = scheduler_mod.Scheduler(lambda: [], lambda tid: fired.append(tid))
        now = dt.datetime(2026, 11, 13, 9, 0, 5)
        self.assertTrue(s._fire_once("t1", now))
        now2 = dt.datetime(2026, 11, 13, 9, 0, 55)  # 同一分钟
        self.assertFalse(s._fire_once("t1", now2))
        nxt = dt.datetime(2026, 11, 13, 9, 1, 5)
        self.assertTrue(s._fire_once("t1", nxt))


class RenderTest(unittest.TestCase):
    def test_message_has_inline_contacts_without_summary_block(self):
        text = render.render_tender([{
            "title": "GPU服务器采购",
            "purchaser": "某大学",
            "purchaser_contact": "张老师 13800000000",
            "agent_contact": "李老师 021-12345678",
            "url": "https://example.invalid/tender/1",
        }])
        self.assertIn("采购人联系方式", text)
        self.assertIn("代理联系方式", text)
        self.assertIn("联系人1：张老师  13800000000", text)
        self.assertIn("联系人1：李老师  021-12345678", text)
        self.assertNotIn("联系方式汇总", text)
        self.assertNotIn("本批命中", text)

    def test_contact_details_are_split_and_deduplicated(self):
        text = render.render_tender([{
            "title": "算力服务器采购",
            "purchaser_contact": (
                "某公司 地址：上海市静安区南京西路768号 "
                "联系人：李洁、张明 电话：021-38031327、13800138000 "
                "电子邮件：lijie@example.com，李洁 电话：021-38031327"
            ),
            "url": "https://example.invalid/tender/2",
        }])
        self.assertIn("地址：上海市静安区南京西路768号", text)
        self.assertIn("联系人1：李洁  021-38031327  lijie@example.com", text)
        self.assertIn("联系人2：张明  13800138000", text)
        self.assertEqual(text.count("021-38031327"), 1)
        self.assertNotIn("地址：上海市静安区南京西路768号 联系人", text)


class ChannelTest(unittest.TestCase):
    """业务码必须校验：HTTP 200 但 errcode!=0 属于失败。"""

    def setUp(self):
        self._orig = channels_mod._post
        self._orig_log = channels_mod.ADAPTERS.copy()

    def tearDown(self):
        channels_mod._post = self._orig
        channels_mod.ADAPTERS = self._orig_log

    def _fake(self, resp):
        channels_mod._post = lambda url, body, retries=1: {
            "ok": True, "status": 200, "resp": resp,
        }

    def test_dingtalk_business_error_detected(self):
        self._fake({"errcode": 310000, "errmsg": "keywords not in content"})
        r = channels_mod.dispatch("dingtalk", "https://oapi.dingtalk.com/robot/send?access_token=x",
                                  None, "标题", "正文")
        self.assertFalse(r["ok"], "钉钉业务报错必须判定为失败")
        self.assertIn("310000", r["error"])

    def test_dingtalk_success(self):
        self._fake({"errcode": 0, "errmsg": "ok"})
        r = channels_mod.dispatch("dingtalk", "https://oapi.dingtalk.com/robot/send?access_token=x",
                                  None, "标题", "正文")
        self.assertTrue(r["ok"])

    def test_wecom_business_error_detected(self):
        self._fake({"errcode": 93000, "errmsg": "invalid webhook url"})
        r = channels_mod.dispatch("wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
                                  None, "标题", "正文")
        self.assertFalse(r["ok"])

    def test_feishu_new_and_old_code_fields(self):
        self._fake({"code": 19021, "msg": "sign match fail"})
        r = channels_mod.dispatch("feishu", "https://open.feishu.cn/open-apis/bot/v2/hook/x",
                                  None, "标题", "正文")
        self.assertFalse(r["ok"])
        self._fake({"StatusCode": 0, "StatusMessage": "success"})
        r = channels_mod.dispatch("feishu", "https://open.feishu.cn/open-apis/bot/v2/hook/x",
                                  None, "标题", "正文")
        self.assertTrue(r["ok"])

    def test_no_webhook_rejected(self):
        r = channels_mod.dispatch("dingtalk", "", None, "t", "x")
        self.assertFalse(r["ok"])

    def test_unknown_channel(self):
        r = channels_mod.dispatch("telegram", "https://x", None, "t", "x")
        self.assertFalse(r["ok"])

    def test_truncate_keeps_utf8_valid(self):
        long_text = "服务器采购公告" * 5000
        out = channels_mod._truncate_bytes(long_text, 1000)
        self.assertLessEqual(len(out.encode("utf-8")), 1200)
        out.encode("utf-8")  # 不应抛 UnicodeDecodeError


class ServerValidationTest(unittest.TestCase):
    def test_bad_task_id_rejected(self):
        for bad in ["../x", "a/b", "", "a b", "x" * 65]:
            with self.assertRaises(server.ApiError, msg=f"应拒绝 id={bad!r}"):
                server._valid_id(bad, "任务 id")

    def test_webhook_scheme_enforced(self):
        with self.assertRaises(server.ApiError):
            server._valid_webhook("file:///etc/passwd")
        with self.assertRaises(server.ApiError):
            server._valid_webhook("javascript:alert(1)")
        self.assertEqual(server._valid_webhook("https://example.com/hook"),
                         "https://example.com/hook")

    def test_task_rejects_bad_cron(self):
        body = {"id": "t-x", "name": "测试", "auto_enabled": True, "schedule": "99 99 * * *",
                "channel_ids": [], "rule": {"include": [], "exclude": []}}
        with self.assertRaises(server.ApiError):
            server._valid_task(body, [])

    def test_task_rejects_unknown_channel(self):
        server.CHANNELS_CACHE["list"] = [{"id": "wh-1"}]
        body = {"id": "t-x", "name": "测试", "channel_ids": ["wh-nope"],
                "rule": {"include": [], "exclude": []}}
        with self.assertRaises(server.ApiError):
            server._valid_task(body, [])

    def test_task_rejects_bad_limit(self):
        server.CHANNELS_CACHE["list"] = []
        for bad in [0, 101, "abc"]:
            body = {"id": "t-x", "name": "测试", "limit": bad, "channel_ids": [],
                    "rule": {"include": [], "exclude": []}}
            with self.assertRaises(server.ApiError, msg=f"limit={bad}"):
                server._valid_task(body, [])

    def test_rule_rejects_bad_op(self):
        with self.assertRaises(server.ApiError):
            server._valid_rule({"include": [{"field": "title", "op": "shell"}], "exclude": []})

    def test_rule_rejects_bad_number(self):
        with self.assertRaises(server.ApiError):
            server._valid_rule({"include": [{"field": "budget_num", "op": "gte", "value": "abc"}]})

    def test_partial_update_preserves_schedule(self):
        server.CHANNELS_CACHE["list"] = [{"id": "wh-1"}]
        old = {"id": "t-old", "name": "旧任务", "auto_enabled": True,
               "schedule": "30 9 * * 1-5", "channel_ids": ["wh-1"]}
        body = {"id": "t-old", "name": "改名后的任务", "channel_ids": ["wh-1"],
                "rule": {"include": [], "exclude": []}}
        task = server._valid_task(body, [old])
        self.assertTrue(task["auto_enabled"])
        self.assertEqual(task["schedule"], "30 9 * * 1-5")

    def test_explicit_disable_clears_schedule(self):
        server.CHANNELS_CACHE["list"] = []
        old = {"id": "t-old", "name": "旧任务", "auto_enabled": True,
               "schedule": "30 9 * * 1-5", "channel_ids": []}
        body = {"id": "t-old", "name": "旧任务", "auto_enabled": False,
                "channel_ids": [], "rule": {"include": [], "exclude": []}}
        task = server._valid_task(body, [old])
        self.assertFalse(task["auto_enabled"])
        self.assertEqual(task["schedule"], "")

    def test_valid_task_passes(self):
        server.CHANNELS_CACHE["list"] = [{"id": "wh-1"}]
        body = {"id": "t-ok", "name": "正常任务", "limit": 10, "channel_ids": ["wh-1"],
                "auto_enabled": True, "schedule": "0 9 * * 1-5",
                "rule": {"include": [{"field": "region", "op": "in", "value": ["上海"]}],
                         "exclude": []}}
        t = server._valid_task(body, [])
        self.assertEqual(t["id"], "t-ok")
        self.assertEqual(t["schedule"], "0 9 * * 1-5")
        self.assertTrue(t["auto_enabled"])


class LiveUpdateTest(unittest.TestCase):
    def test_normalize_adds_region_and_budget_num(self):
        item = fetcher.normalize_item({"budget": "123.5万元", "published": "2026-11-16"})
        self.assertEqual(item["region"], "上海")
        self.assertEqual(item["budget_num"], 1_235_000)
        self.assertEqual(item["published_dt"], "2026-11-16")

    def test_budget_units(self):
        self.assertEqual(fetcher._budget_number("1,200,000元"), 1_200_000)
        self.assertEqual(fetcher._budget_number("0.8亿元"), 80_000_000)
        self.assertEqual(fetcher._budget_number("包1-1266300.00元"), 1_266_300)
        self.assertIsNone(fetcher._budget_number("公告未披露"))

    def test_missing_collector_is_hard_failure(self):
        with self.assertRaises(RuntimeError):
            fetcher._load_collector("Z:/definitely/missing/collector.py")

    def test_update_status_missing_is_empty(self):
        old = fetcher.UPDATE_STATUS
        tmp = tempfile.TemporaryDirectory()
        try:
            fetcher.UPDATE_STATUS = Path(tmp.name) / "missing.json"
            self.assertEqual(fetcher.get_update_status(), {})
        finally:
            fetcher.UPDATE_STATUS = old
            tmp.cleanup()

    def test_server_exposes_llm_config_without_secret(self):
        state = server.Handler._update_state()
        self.assertIn("llm", state)
        self.assertNotIn("api_key", state["llm"])
        self.assertIn("api_key_configured", state["llm"])


class OrderingTest(unittest.TestCase):
    def test_matched_sorted_by_deadline(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            items = [
                {"id": "a", "deadline_dt": "2026-12-31 10:00"},
                {"id": "b", "deadline_dt": "2026-11-20 10:00"},
                {"id": "c", "deadline_dt": ""},
            ]
            task = {"id": "t-sort", "limit": 10, "rule": {}}
            res = engine.run_task(task, items, Path(tmp.name))
            self.assertEqual([m["id"] for m in res["matched"]], ["b", "a", "c"])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
