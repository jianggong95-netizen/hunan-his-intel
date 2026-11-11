"""版本管理与更新检查测试：语义化比较、离线容错、缓存。"""
import unittest
from unittest import mock

from app import version


class ParseVersionTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(version.parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(version.parse_version("v1.0"), (1, 0, 0))
        self.assertEqual(version.parse_version("2"), (2, 0, 0))

    def test_compare(self):
        self.assertGreater(version.parse_version("1.0.1"), version.parse_version("1.0.0"))


class UpdateInfoTest(unittest.TestCase):
    def setUp(self):
        version._cache.update({"ts": 0.0, "data": None})

    def test_offline_error(self):
        with mock.patch.object(version, "latest_release", return_value={"error": "timeout"}):
            info = version.update_info()
            self.assertFalse(info["has_update"])
            self.assertEqual(info["current"], version.__version__)
            self.assertEqual(info["error"], "timeout")

    def test_has_update(self):
        newer = {"version": "99.0.0", "url": "https://x/zip", "page": "https://x", "name": "v99",
                 "published": "", "notes": ""}
        with mock.patch.object(version, "latest_release", return_value=newer):
            info = version.update_info()
            self.assertTrue(info["has_update"])
            self.assertEqual(info["url"], "https://x/zip")

    def test_up_to_date(self):
        same = {"version": version.__version__, "url": "", "page": "", "name": "", "published": "", "notes": ""}
        with mock.patch.object(version, "latest_release", return_value=same):
            self.assertFalse(version.update_info()["has_update"])


class LatestReleaseTest(unittest.TestCase):
    def setUp(self):
        version._cache.update({"ts": 0.0, "data": None})

    def test_http_error(self):
        resp = mock.Mock(status_code=403)
        with mock.patch.object(version.requests, "get", return_value=resp):
            self.assertIn("error", version.latest_release())

    def test_network_exception(self):
        with mock.patch.object(version.requests, "get", side_effect=OSError("offline")):
            self.assertIn("error", version.latest_release())

    def test_parse_release_and_cache(self):
        payload = {
            "tag_name": "v1.2.3", "name": "Release 1.2.3", "html_url": "https://gh/r",
            "published_at": "2026-01-01T00:00:00Z", "body": "说明",
            "assets": [{"name": "other.txt", "browser_download_url": "https://a/txt"},
                       {"name": "his-intel-v1.2.3-win64.zip", "browser_download_url": "https://a/zip"}],
        }
        resp = mock.Mock(status_code=200)
        resp.json.return_value = payload
        with mock.patch.object(version.requests, "get", return_value=resp) as g:
            info = version.latest_release()
            self.assertEqual(info["version"], "1.2.3")
            self.assertEqual(info["url"], "https://a/zip")
            # 第二次应命中缓存，不再发请求
            version.latest_release()
            self.assertEqual(g.call_count, 1)


if __name__ == "__main__":
    unittest.main()
