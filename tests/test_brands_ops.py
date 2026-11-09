"""品牌识别测试：包件拆分、集成商识别、规则匹配、待核与人工确认。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import brands_ops, config
from app.db import db, init_db


class BrandsOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch_db = patch.object(config, "DB_PATH", str(Path(self.tmp.name) / "test.db"))
        self.patch_db.start()
        init_db()
        with db() as conn:
            conn.execute(
                "INSERT INTO announcements(url, title, raw_text, publish_date, first_seen_at) VALUES (?,?,?,?,?)",
                (
                    "https://example.com/1",
                    "某县人民医院信息系统采购中标公告",
                    "包1：HIS系统，中标供应商：中国电信股份有限公司湖南分公司，金额：100万元。"
                    "包2：电子病历系统，中标供应商：湖南创星科技股份有限公司，金额：80万元。",
                    "2025-05-01",
                    "2025-05-02",
                ),
            )

    def tearDown(self):
        self.patch_db.stop()
        self.tmp.cleanup()

    def test_split_packages(self):
        packages = brands_ops.split_packages(
            "包1：HIS系统采购。包2：电子病历系统采购。包3：机房建设。"
        )
        self.assertEqual(len(packages), 3)
        self.assertIn("HIS", packages[0]["text"])

    def test_integrator_detection(self):
        self.assertTrue(brands_ops.is_integrator("中国电信股份有限公司湖南分公司"))
        self.assertTrue(brands_ops.is_integrator("长沙某某系统集成有限公司"))
        self.assertFalse(brands_ops.is_integrator("卫宁健康科技集团股份有限公司"))

    def test_rule_hit_on_supplier(self):
        brands_ops.add_rule("卫宁健康", "卫宁健康", actor="tester")
        brand, confidence, basis = brands_ops.match("湖南卫宁健康科技有限公司", "")
        self.assertEqual(brand, "卫宁健康")
        self.assertGreaterEqual(confidence, 0.9)
        self.assertIn("规则库", basis)

    def test_integrator_needs_evidence(self):
        brand, confidence, _ = brands_ops.match("中国电信股份有限公司湖南分公司", "本项目采购 HIS 系统")
        self.assertIsNone(brand)
        self.assertEqual(confidence, 0.0)
        brand2, confidence2, basis2 = brands_ops.match(
            "中国电信股份有限公司湖南分公司", "本项目 HIS 由卫宁健康提供服务"
        )
        self.assertEqual(brand2, "卫宁健康")
        self.assertLess(confidence2, 0.7)
        self.assertIn("集成商", basis2)

    def test_recompute_and_confirm(self):
        with db() as conn:
            conn.execute(
                "INSERT INTO awards(announcement_id, package_no, supplier_raw, brand_name, "
                "brand_confidence, review_status) VALUES (1, '1', '中国电信股份有限公司湖南分公司', "
                "'未识别', 0, 'pending')"
            )
        result = brands_ops.recompute_awards(actor="tester")
        self.assertGreaterEqual(result["updated"], 1)
        self.assertGreaterEqual(result["pending"], 1)
        with db() as conn:
            award = conn.execute("SELECT * FROM awards").fetchone()
        brands_ops.confirm_award(award["id"], "卫宁健康", actor="tester")
        with db() as conn:
            confirmed = conn.execute("SELECT * FROM awards WHERE id=?", (award["id"],)).fetchone()
            self.assertEqual(confirmed["review_status"], "confirmed")
            self.assertEqual(confirmed["brand_name"], "卫宁健康")
            self.assertEqual(confirmed["reviewed_by"], "tester")
        stats = brands_ops.unknown_ratio()
        self.assertEqual(stats["unknown"], 0)

    def test_rules_crud(self):
        row = brands_ops.add_rule("东华", "东华医为", actor="tester")
        updated = brands_ops.update_rule(row["id"], priority=5)
        self.assertEqual(updated["priority"], 5)
        brands_ops.delete_rule(row["id"], actor="tester")
        self.assertFalse(any(r["id"] == row["id"] for r in brands_ops.rules_list()))

    def test_agency_is_excluded_from_winners(self):
        with db() as conn:
            conn.execute(
                "INSERT INTO awards(announcement_id, package_no, supplier_raw, brand_name, "
                "brand_confidence, review_status, is_winner) VALUES (1, '1', '湖南省招标有限责任公司', "
                "'未识别', 0, 'pending', 1)"
            )
        brands_ops.recompute_awards(actor="tester")
        with db() as conn:
            row = conn.execute("SELECT * FROM awards WHERE supplier_raw='湖南省招标有限责任公司'").fetchone()
            self.assertEqual(row["is_winner"], 0)
        self.assertEqual(brands_ops.unknown_ratio()["excluded"], 1)

    def test_non_integrator_text_mention_is_ignored(self):
        # 普通供应商不能因为正文里提到某品牌就被判成该厂家
        brand, confidence, _ = brands_ops.match("湖南某某科技有限公司", "本项目采用卫宁健康系统")
        self.assertIsNone(brand)
        self.assertEqual(confidence, 0.0)


if __name__ == "__main__":
    unittest.main()
