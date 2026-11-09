"""医院主数据测试：模板、名录导入、归并建议与手工合并。"""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config, hospital_master
from app.db import db, init_db


class HospitalMasterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch_db = patch.object(config, "DB_PATH", str(Path(self.tmp.name) / "test.db"))
        self.patch_db.start()
        init_db()
        with db() as conn:
            conn.execute(
                "INSERT INTO hospitals(name, norm_name, city_code, ownership, aliases, first_seen_at) "
                "VALUES (?,?,?,?,?,?)",
                ("隆回县人民医院", "隆回县人民医院", "430500", "公立", "[]", "2025-01-01"),
            )
            conn.execute(
                "INSERT INTO hospitals(name, norm_name, city_code, ownership, aliases, first_seen_at) "
                "VALUES (?,?,?,?,?,?)",
                ("洞口县人民医院", "洞口县人民医院", "430500", "公立", "[]", "2025-01-01"),
            )

    def tearDown(self):
        self.patch_db.stop()
        self.tmp.cleanup()

    def test_template_has_headers(self):
        data = hospital_master.template_csv().decode("utf-8-sig")
        self.assertIn("医院名称", data.splitlines()[0])
        self.assertIn("别名", data.splitlines()[0])

    def test_import_master_marks_and_merges_by_norm(self):
        csv_text = (
            "\ufeff医院名称,市州,等级,别名,来源链接\n"
            "隆回县人民医院,邵阳市,三级乙等,隆回人民医院,https://wjw.hunan.gov.cn/a\n"
            "邵阳市中心医院,邵阳市,三级甲等,,\n"
        )
        result = hospital_master.import_master(io.BytesIO(csv_text.encode("utf-8")), "master.csv", "tester")
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["updated"], 1)
        with db() as conn:
            master = conn.execute("SELECT * FROM hospitals WHERE norm_name='邵阳市中心医院'").fetchone()
            self.assertEqual(master["is_master"], 1)
            merged = conn.execute("SELECT * FROM hospitals WHERE norm_name='隆回县人民医院'").fetchone()
            self.assertEqual(merged["is_master"], 1)
            self.assertIn("隆回人民医院", merged["aliases"])

    def test_suggestions_prefer_exact_match(self):
        # 采集到的别名写法与名录主记录不同，应给出并入建议
        with db() as conn:
            conn.execute(
                "INSERT INTO hospitals(name, norm_name, city_code, ownership, aliases, first_seen_at) "
                "VALUES (?,?,?,?,?,?)",
                ("洞口人民医院", "洞口人民医院", "430500", "公立", "[]", "2025-01-01"),
            )
        csv_text = "\ufeff医院名称,市州\n洞口县人民医院,邵阳市\n"
        hospital_master.import_master(io.BytesIO(csv_text.encode("utf-8")), "m.csv", "tester")
        suggestions = hospital_master.suggestions()
        target = next(s for s in suggestions if s["name"] == "洞口人民医院")
        self.assertIsNotNone(target["suggestion"])
        self.assertEqual(target["suggestion"]["name"], "洞口县人民医院")
        # 已升级为主数据的记录不再出现在未匹配建议里
        self.assertFalse(any(s["name"] == "洞口县人民医院" for s in suggestions))

    def test_merge_moves_announcements(self):
        with db() as conn:
            conn.execute(
                "INSERT INTO announcements(url, title, hospital_id, publish_date, first_seen_at) "
                "VALUES (?,?,?,?,?)",
                ("https://example.com/x", "某医院信息系统中标公告", 1, "2025-03-01", "2025-03-02"),
            )
            conn.execute("UPDATE hospitals SET is_master=1 WHERE id=2")
        hospital_master.merge(1, 2, "tester")
        with db() as conn:
            ann = conn.execute("SELECT hospital_id FROM announcements WHERE url='https://example.com/x'").fetchone()
            self.assertEqual(ann["hospital_id"], 2)
            src = conn.execute("SELECT merged_into FROM hospitals WHERE id=1").fetchone()
            self.assertEqual(src["merged_into"], 2)
            audit = conn.execute("SELECT * FROM audit_logs WHERE action='hospital_merge_manual'").fetchone()
            self.assertIsNotNone(audit)


if __name__ == "__main__":
    unittest.main()
