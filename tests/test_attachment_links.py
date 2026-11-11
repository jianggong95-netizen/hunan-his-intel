"""附件链接抽取与登记测试：直链、下载接口、交易中心包件页、非直链不下载。"""
import sqlite3
import unittest

from app.collectors.detail import attachment_links, parse_detail
from app.db import SCHEMA
from app.pipeline import _save_attachments


HTML = """
<html><body><div class="article_box">
  <p>附件：</p>
  <a href="/f/m/pkg_detail/pkg_6379">包件明细</a>
  <a href="https://oss-jgpt.czt.hunan.gov.cn/jgpt-ggfj/fbmsynfile/uploadfile/20240301/78e5a31e13664319a3d186d52faa20b6.xls">采购需求</a>
  <a href="/downloadFile?fileid=123">下载招标文件</a>
  <a href="/news/2024/list.shtml">返回列表</a>
  <a href="javascript:void(0)">打印</a>
</div></body></html>
"""


class AttachmentLinksTest(unittest.TestCase):
    def test_extracts_direct_api_and_page_links(self):
        links = attachment_links(HTML, "http://www.ccgp-hunan.gov.cn/luban/front/detail?articleId=x")
        urls = [link["url"] for link in links]
        self.assertIn("http://www.ccgp-hunan.gov.cn/f/m/pkg_detail/pkg_6379", urls)
        self.assertTrue(any(u.endswith(".xls") for u in urls))
        self.assertTrue(any("fileid=123" in u for u in urls))
        self.assertFalse(any("list.shtml" in u for u in urls))

        by_url = {link["url"]: link for link in links}
        self.assertFalse(by_url["http://www.ccgp-hunan.gov.cn/f/m/pkg_detail/pkg_6379"]["direct"])
        xls = next(link for link in links if link["url"].endswith(".xls"))
        self.assertTrue(xls["direct"])
        self.assertTrue(by_url["http://www.ccgp-hunan.gov.cn/downloadFile?fileid=123"]["direct"])

    def test_page_link_without_text_named_as_package_page(self):
        html = '<a href="http://changd.ccgp-hunan.gov.cn/f/m/pkg_detail/pkg_1"></a>'
        links = attachment_links(html, "http://www.ccgp-hunan.gov.cn/")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["name"], "交易中心包件页")

    def test_parse_detail_merges_links(self):
        detail = parse_detail(HTML, "http://www.ccgp-hunan.gov.cn/luban/front/detail?articleId=x")
        self.assertGreaterEqual(len(detail["attachments"]), 3)
        self.assertIn("html", detail)


class SaveAttachmentsTest(unittest.TestCase):
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn

    def test_direct_downloaded_page_link_only_registered(self):
        conn = self._conn()
        calls = []

        class FakeFetcher:
            def download(self, url, dest):
                calls.append(url)
                return True, "sha", 12

        attachments = [
            {"name": "采购需求.xls", "url": "http://a/需求.xls", "direct": True, "source": "body_link"},
            {"name": "交易中心包件页", "url": "http://a/pkg_detail/pkg_1", "direct": False,
             "source": "body_link"},
        ]
        added = _save_attachments(conn, FakeFetcher(), 1, attachments, lambda *a, **k: None)
        self.assertEqual(added, 2)
        self.assertEqual(calls, ["http://a/需求.xls"])
        rows = {row["url"]: row for row in conn.execute("SELECT * FROM attachments").fetchall()}
        self.assertEqual(rows["http://a/需求.xls"]["status"], "ok")
        self.assertEqual(rows["http://a/需求.xls"]["source"], "body_link")
        self.assertEqual(rows["http://a/pkg_detail/pkg_1"]["status"], "unverified")
        self.assertIn("非直链", rows["http://a/pkg_detail/pkg_1"]["note"])

    def test_no_download_flag_skips_file(self):
        conn = self._conn()
        calls = []

        class FakeFetcher:
            def download(self, url, dest):
                calls.append(url)
                return True, "sha", 12

        added = _save_attachments(
            conn, FakeFetcher(), 1,
            [{"name": "a.pdf", "url": "http://a/a.pdf", "direct": True}],
            lambda *a, **k: None, download=False,
        )
        self.assertEqual(added, 1)
        self.assertEqual(calls, [])
        row = conn.execute("SELECT * FROM attachments").fetchone()
        self.assertEqual(row["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
