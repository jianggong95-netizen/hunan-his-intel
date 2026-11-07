"""附件归档测试：类型识别、手工上传、哈希、审计、失效复检。"""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import FileStorage

from app import attachments, config
from app.db import db, init_db


class AttachmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.att_dir = Path(self.tmp.name) / "attachments"
        self.att_dir.mkdir()
        self.patch_db = patch.object(config, "DB_PATH", str(self.db_path))
        self.patch_att = patch.object(config, "ATTACHMENT_DIR", self.att_dir)
        self.patch_db.start()
        self.patch_att.start()
        init_db()
        with db() as conn:
            conn.execute(
                "INSERT INTO announcements(url, title, purchaser_norm, publish_date, first_seen_at, "
                "fetch_status) VALUES (?,?,?,?,?,?)",
                ("https://example.com/a.html", "某医院医院信息系统采购中标公告", "某医院", "2025-01-01", "2025-01-02", "ok"),
            )
        self.ann_id = 1

    def tearDown(self):
        self.patch_att.stop()
        self.patch_db.stop()
        self.tmp.cleanup()

    def test_classify(self):
        self.assertEqual(attachments.classify_attachment("采购需求及技术参数.pdf"), "参数文件")
        self.assertEqual(attachments.classify_attachment("招标文件.docx"), "招标文件")
        self.assertEqual(attachments.classify_attachment("中标结果公告.pdf"), "中标公告")
        self.assertEqual(attachments.classify_attachment("清单.xlsx"), "其他")

    def test_upload_records_hash_and_audit(self):
        raw = b"%PDF-1.4 demo"
        fs = FileStorage(stream=io.BytesIO(raw), filename="参数文件.pdf", content_type="application/pdf")
        row = attachments.save_upload(self.ann_id, fs, "参数文件", "tester", "127.0.0.1")
        self.assertEqual(row["kind"], "参数文件")
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["uploader"], "tester")
        self.assertEqual(row["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(row["local_path"].endswith(".pdf"))
        self.assertTrue((config.BASE_DIR / row["local_path"]).exists())
        with db() as conn:
            audit = conn.execute("SELECT * FROM audit_logs WHERE action='attachment_upload'").fetchone()
        self.assertIsNotNone(audit)

    def test_upload_rejects_bad_extension(self):
        fs = FileStorage(stream=io.BytesIO(b"x"), filename="bad.exe")
        with self.assertRaises(ValueError):
            attachments.save_upload(self.ann_id, fs, "其他", "tester")

    def test_check_local_file_valid(self):
        fs = FileStorage(stream=io.BytesIO(b"demo"), filename="需求.doc")
        row = attachments.save_upload(self.ann_id, fs, "参数文件", "tester")
        result = attachments.check_one(row["id"], actor="tester")
        self.assertTrue(result["valid"])

    def test_recheck_excludes_original_page(self):
        # 迁移回填的公告原文不应进入远端复检范围
        init_db()
        result = attachments.recheck_all(actor="test")
        self.assertEqual(result["checked"], 0)


if __name__ == "__main__":
    unittest.main()
