"""「最近 2 次」口径测试：同日多包、流标、跨年、年份筛选、缺日期。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import aggregate, config
from app.db import db, init_db


class CaliberTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch_db = patch.object(config, "DB_PATH", str(Path(self.tmp.name) / "test.db"))
        self.patch_db.start()
        init_db()

    def tearDown(self):
        self.patch_db.stop()
        self.tmp.cleanup()

    def _hospital(self, name, city="430500"):
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO hospitals(name, norm_name, city_code, ownership, aliases, first_seen_at) "
                "VALUES (?,?,?,?,?,?)",
                (name, name, city, "公立", "[]", "2025-01-01"),
            )
            return cur.lastrowid

    def _ann(self, hid, url, date, title="某医院信息系统中标公告", void=0):
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO announcements(url, title, hospital_id, publish_date, first_seen_at, is_void) "
                "VALUES (?,?,?,?,?,?)",
                (url, title, hid, date, "2025-01-01", void),
            )
            return cur.lastrowid

    def test_same_day_multi_package_counts_once(self):
        hid = self._hospital("甲医院")
        a1 = self._ann(hid, "https://e/1", "2025-05-01")
        a2 = self._ann(hid, "https://e/2", "2025-05-01")
        self._ann(hid, "https://e/3", "2024-01-01")
        with db() as conn:
            for aid, sup in ((a1, "A公司"), (a2, "B公司")):
                conn.execute(
                    "INSERT INTO awards(announcement_id, supplier_raw, brand_name, brand_confidence) "
                    "VALUES (?,?,?,?)", (aid, sup, "未识别", 0),
                )
        rows = aggregate.latest_two({})
        same_day = [r for r in rows if r["announcement_ids"] and len(r["announcement_ids"]) == 2]
        self.assertEqual(len(same_day), 1)
        self.assertEqual(same_day[0]["package_count"], 2)
        self.assertIn("A公司", same_day[0]["supplier"])
        self.assertIn("B公司", same_day[0]["supplier"])
        # 该医院共 2 次（同日合并 + 2024 年一次）
        self.assertEqual(len([r for r in rows if r["hospital_id"] == hid]), 2)

    def test_void_excluded(self):
        hid = self._hospital("乙医院")
        self._ann(hid, "https://e/v1", "2025-06-01", void=1)
        self._ann(hid, "https://e/v2", "2025-05-01")
        rows = aggregate.latest_two({})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://e/v2")

    def test_cross_year_keeps_two(self):
        hid = self._hospital("丙医院")
        self._ann(hid, "https://e/y1", "2024-12-30")
        self._ann(hid, "https://e/y2", "2025-01-05")
        rows = aggregate.latest_two({})
        self.assertEqual([r["url"] for r in rows], ["https://e/y2", "https://e/y1"])

    def test_year_filter_ranks_within_year(self):
        hid = self._hospital("丁医院")
        self._ann(hid, "https://e/z1", "2024-06-01")
        self._ann(hid, "https://e/z2", "2025-06-01")
        rows = aggregate.latest_two({"year": "2024"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://e/z1")
        self.assertEqual(rows[0]["rank"], 1)

    def test_empty_date_not_collapsed(self):
        hid = self._hospital("戊医院")
        self._ann(hid, "https://e/n1", "")
        self._ann(hid, "https://e/n2", "")
        rows = aggregate.latest_two({})
        # 空日期不能合并成同一天，按 id 各自保留
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r["rank"] for r in rows), [1, 2])


if __name__ == "__main__":
    unittest.main()
