import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from database import (
    CATEGORIES,
    add_record,
    delete_record,
    initialize_database,
    load_records,
    update_record,
)


class GrowthArchiveDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_file = Path(self.temp_dir.name) / "data" / "growth_archive.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_database_creates_four_related_tables(self):
        initialize_database(self.database_file)
        connection = sqlite3.connect(self.database_file)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            foreign_keys = connection.execute("PRAGMA foreign_key_list(records)").fetchall()
        finally:
            connection.close()
        self.assertEqual(tables, {"profiles", "categories", "records", "certificates"})
        self.assertEqual(len(foreign_keys), 2)

    def test_sql_insert_and_restart_persistence(self):
        records, _ = load_records(self.database_file)
        add_record(records, "Python 数据分析证书", "证书", "课程结业", date(2026, 9, 1), self.database_file)
        reloaded, warning = load_records(self.database_file)
        self.assertIsNone(warning)
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded.iloc[0]["title"], "Python 数据分析证书")

    def test_sql_update(self):
        records, _ = load_records(self.database_file)
        records = add_record(records, "原名称", "经历", "原备注", date(2026, 8, 1), self.database_file)
        record_id = int(records.iloc[0]["id"])
        update_record(
            records,
            record_id,
            "修改后的名称",
            "工作记录",
            "修改后的备注",
            date(2026, 9, 2),
            self.database_file,
        )
        reloaded, _ = load_records(self.database_file)
        row = reloaded.loc[reloaded["id"] == record_id].iloc[0]
        self.assertEqual(row["title"], "修改后的名称")
        self.assertEqual(row["category"], "工作记录")

    def test_sql_delete_cascades_certificate_row(self):
        records, _ = load_records(self.database_file)
        records = add_record(
            records,
            "获奖证书",
            "荣誉",
            "一等奖",
            date(2026, 9, 3),
            self.database_file,
            certificate_name="award.pdf",
            certificate_bytes=b"%PDF-1.4\n%%EOF",
        )
        record_id = int(records.iloc[0]["id"])
        certificate_path = self.database_file.parent / str(records.iloc[0]["certificate_file"])
        self.assertTrue(certificate_path.exists())
        delete_record(records, record_id, self.database_file)
        connection = sqlite3.connect(self.database_file)
        try:
            record_count = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            certificate_count = connection.execute("SELECT COUNT(*) FROM certificates").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(record_count, 0)
        self.assertEqual(certificate_count, 0)
        self.assertFalse(certificate_path.exists())

    def test_invalid_input_is_rejected(self):
        records, _ = load_records(self.database_file)
        with self.assertRaises(ValueError):
            add_record(records, "  ", "证书", "", date.today(), self.database_file)
        with self.assertRaises(ValueError):
            add_record(records, "测试", "无效类型", "", date.today(), self.database_file)

if __name__ == "__main__":
    unittest.main(verbosity=2)
