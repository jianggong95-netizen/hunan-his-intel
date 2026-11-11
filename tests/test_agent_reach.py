"""Agent-Reach 适配层测试：链接提取、Reader 头部剥离、未安装/禁用时的降级行为、入库回退。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import agent_reach, config
from app.db import db, init_db


class FileLinksTest(unittest.TestCase):
    def test_extract_and_dedupe(self):
        text = (
            "正文见附件 [招标文件](https://ccgp.hunan.gov.cn/f/zb.pdf) 和 "
            "https://ccgp.hunan.gov.cn/f/zb.pdf 重复、https://x.cn/a.docx 与 https://x.cn/b.zip。\n"
            "非文件 https://x.cn/page.html 不提取。"
        )
        links = agent_reach.file_links(text)
        urls = [a["url"] for a in links]
        self.assertEqual(urls, [
            "https://ccgp.hunan.gov.cn/f/zb.pdf",
            "https://x.cn/a.docx",
            "https://x.cn/b.zip",
        ])
        self.assertTrue(all(a["direct"] and a["source"] == "agent_reach" for a in links))

    def test_no_links(self):
        self.assertEqual(agent_reach.file_links("纯文本，没有任何附件"), [])


class StripReaderMetaTest(unittest.TestCase):
    def test_strip(self):
        raw = "Title: 公告\nURL Source: https://a\nMarkdown Content:\n# 正文\n内容"
        self.assertEqual(agent_reach._strip_reader_meta(raw), "# 正文\n内容")

    def test_no_meta(self):
        self.assertEqual(agent_reach._strip_reader_meta("直接正文"), "直接正文")


class DegradationTest(unittest.TestCase):
    def test_disabled(self):
        with mock.patch.object(config, "AGENT_REACH_ENABLED", False):
            self.assertIsNone(agent_reach.cli_path())
            self.assertFalse(agent_reach.is_available())
            text, err = agent_reach.read_page("https://x")
            self.assertEqual(text, "")
            self.assertIn("未安装", err)
            row = agent_reach.status_row()
            self.assertEqual(row["status"], "disabled")

    def test_not_installed(self):
        with mock.patch.object(config, "AGENT_REACH_ENABLED", True), \
             mock.patch.object(agent_reach.shutil, "which", return_value=None):
            row = agent_reach.status_row()
            self.assertEqual(row["status"], "not_installed")
            self.assertIn("Panniantong/Agent-Reach", row["note"])

    def test_available_channels(self):
        with mock.patch.object(config, "AGENT_REACH_ENABLED", True), \
             mock.patch.object(agent_reach.shutil, "which", return_value="/usr/bin/agent-reach"), \
             mock.patch.object(agent_reach, "_run", return_value=(True, "web: ok\nexa: ok")):
            row = agent_reach.status_row()
            self.assertEqual(row["status"], "available")


class PipelineFallbackTest(unittest.TestCase):
    """详情直连失败时，Agent-Reach read 成功应使 fetch_status=ok 并记录回退标记。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch_db = mock.patch.object(config, "DB_PATH", str(Path(self.tmp.name) / "test.db"))
        self.patch_db.start()
        init_db()

    def tearDown(self):
        self.patch_db.stop()
        self.tmp.cleanup()

    def test_fallback_ok(self):
        from app.pipeline import ingest_item

        class DeadFetcher:
            def get(self, *a, **k):
                raise ConnectionError("blocked")

            def download(self, url, dest):
                return False, "", 0

        item = {
            "source_id": "hunan_gov_search", "source_name": "省站群", "url": "https://hospital.example/a.html",
            "title": "某县人民医院医院信息系统（HIS）采购项目中标公告", "notice_type": "中标公告",
            "publish_date": "2025-06-01",
        }
        ar_text = "正文：中标供应商：东软集团\n金额：1234567元\n附件 https://x.cn/a.pdf"
        with mock.patch.object(agent_reach, "is_available", return_value=True), \
             mock.patch.object(agent_reach, "read_page", return_value=(ar_text, "")):
            with db() as conn:
                status, ann_id = ingest_item(conn, DeadFetcher(), item, lambda m, l="info": None)
        with db() as conn:
            row = conn.execute("SELECT fetch_status, extract_meta FROM announcements WHERE id=?", (ann_id,)).fetchone()
        self.assertEqual(row["fetch_status"], "ok")
        self.assertIn('"agent_reach_fallback": true', row["extract_meta"])


if __name__ == "__main__":
    unittest.main()
