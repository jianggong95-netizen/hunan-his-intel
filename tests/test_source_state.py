"""采集源状态测试：失败归因、冷却阶梯、断点游标、Fetcher 错误登记。"""
import sqlite3
import unittest
from types import SimpleNamespace

from app import source_state
from app.collectors.base import Fetcher


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    source_state.ensure_tables(conn)
    return conn


class ClassifyTest(unittest.TestCase):
    def test_status_mapping(self):
        self.assertEqual(source_state.classify_status(429), "rate_limited")
        self.assertEqual(source_state.classify_status(403), "blocked")
        self.assertEqual(source_state.classify_status(503), "upstream_broken")
        self.assertEqual(source_state.classify_status(404), "http")

    def test_exception_mapping(self):
        self.assertEqual(source_state.classify_exception(TimeoutError("timed out")), "timeout")
        self.assertEqual(source_state.classify_exception(ConnectionError("connection reset")), "network")
        self.assertEqual(source_state.classify_exception(ValueError("bad json")), "http")

    def test_block_page_markers(self):
        self.assertEqual(source_state.classify_body("<html>访问过于频繁，请稍后再试</html>"), "blocked")
        self.assertEqual(source_state.classify_body("<html>请输入验证码</html>"), "blocked")
        self.assertIsNone(source_state.classify_body("<html>中标结果公告</html>"))

    def test_pick_kind_uses_priority(self):
        self.assertEqual(source_state.pick_kind(["http", "timeout", "blocked"]), "blocked")
        self.assertEqual(source_state.pick_kind(["http", "parse"]), "parse")


class CooldownTest(unittest.TestCase):
    def test_failure_sets_cooldown_and_escalates(self):
        conn = _conn()
        at = "2026-11-10 10:00:00"
        first = source_state.record_failure(conn, "hunan_ccgp", "timeout", "timed out", at=at)
        self.assertEqual(first["cooldown_until"], "2026-11-10 10:01:00")
        cooling, reason = source_state.is_cooling(conn, "hunan_ccgp", at="2026-11-10 10:00:30")
        self.assertTrue(cooling)
        self.assertIn("请求超时", reason)
        second = source_state.record_failure(conn, "hunan_ccgp", "timeout", "timed out", at=at)
        self.assertEqual(second["cooldown_until"], "2026-11-10 10:02:00")
        self.assertFalse(source_state.is_cooling(conn, "hunan_ccgp", at="2026-11-10 10:03:00")[0])

    def test_blocked_cools_for_one_hour(self):
        conn = _conn()
        info = source_state.record_failure(conn, "ccgp_national", "blocked", "403", at="2026-11-10 10:00:00")
        self.assertEqual(info["cooldown_until"], "2026-11-10 11:00:00")
        self.assertTrue(source_state.is_cooling(conn, "ccgp_national", at="2026-11-10 10:59:59")[0])

    def test_success_clears_failure_and_cooldown(self):
        conn = _conn()
        source_state.record_failure(conn, "hunan_ccgp", "blocked", "403", at="2026-11-10 10:00:00")
        source_state.record_success(conn, "hunan_ccgp", at="2026-11-10 11:30:00")
        runtime = source_state.get_runtime(conn, "hunan_ccgp")
        self.assertEqual(runtime["fail_count"], 0)
        self.assertIsNone(runtime["cooldown_until"])
        self.assertEqual(runtime["last_success_at"], "2026-11-10 11:30:00")
        self.assertFalse(source_state.is_cooling(conn, "hunan_ccgp", at="2026-11-10 11:30:00")[0])

    def test_reset_manual(self):
        conn = _conn()
        source_state.record_failure(conn, "hunan_ccgp", "blocked", "403", at="2026-11-10 10:00:00")
        self.assertEqual(source_state.reset(conn, ["hunan_ccgp"]), 1)
        self.assertFalse(source_state.is_cooling(conn, "hunan_ccgp", at="2026-11-10 10:00:01")[0])


class CursorTest(unittest.TestCase):
    def test_save_get_clear(self):
        conn = _conn()
        self.assertEqual(source_state.get_cursor(conn, "hunan_ccgp", "kw:HIS"), {})
        source_state.save_cursor(conn, "hunan_ccgp", "kw:HIS", {"page": 3})
        self.assertEqual(source_state.get_cursor(conn, "hunan_ccgp", "kw:HIS"), {"page": 3})
        self.assertEqual(len(source_state.list_cursors(conn)), 1)
        source_state.clear_cursor(conn, "hunan_ccgp", "kw:HIS")
        self.assertEqual(source_state.list_cursors(conn), [])

    def test_bad_cursor_json_returns_empty(self):
        conn = _conn()
        conn.execute(
            "INSERT INTO crawl_cursor(source, scope_key, cursor, updated_at) VALUES (?,?,?,?)",
            ("hunan_ccgp", "kw:HIS", "not-json", "2026-11-10 10:00:00"),
        )
        self.assertEqual(source_state.get_cursor(conn, "hunan_ccgp", "kw:HIS"), {})


class FetcherErrorTest(unittest.TestCase):
    def _fetcher(self):
        return Fetcher(logger=lambda *a, **k: None)

    def test_http_status_recorded(self):
        fetcher = self._fetcher()
        fetcher.begin_stage()
        fetcher._note_response(SimpleNamespace(status_code=403, headers={}, text=""), "http://a/1", False)
        fetcher._note_response(SimpleNamespace(status_code=429, headers={}, text=""), "http://a/1", False)
        kinds = [e["kind"] for e in fetcher.take_stage_errors()]
        self.assertEqual(kinds, ["blocked", "rate_limited"])

    def test_block_page_recorded(self):
        fetcher = self._fetcher()
        fetcher.begin_stage()
        resp = SimpleNamespace(status_code=200, headers={"Content-Type": "text/html; charset=utf-8"},
                               text="<html>访问过于频繁</html>")
        fetcher._note_response(resp, "http://a/1", False)
        self.assertEqual(fetcher.take_stage_errors()[0]["kind"], "blocked")

    def test_normal_page_not_recorded(self):
        fetcher = self._fetcher()
        fetcher.begin_stage()
        resp = SimpleNamespace(status_code=200, headers={"Content-Type": "text/html"},
                               text="<html>中标结果公告</html>")
        fetcher._note_response(resp, "http://a/1", False)
        self.assertEqual(fetcher.take_stage_errors(), [])

    def test_begin_stage_clears(self):
        fetcher = self._fetcher()
        fetcher.record_error("timeout", "x")
        fetcher.begin_stage()
        self.assertEqual(fetcher.stage_errors, [])


if __name__ == "__main__":
    unittest.main()
