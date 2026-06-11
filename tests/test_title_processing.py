import json
import os
import sqlite3
import sys
import unittest
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import refresh_server
import web_server

SHORT_CHINESE_TITLE = "\u82f9\u679c\u53d1\u5e03 Vision Pro \u65b0\u7cfb\u7edf"
LONG_CHINESE_TITLE = "\u8fd9\u662f\u4e00\u4e2a" + ("\u975e\u5e38" * 12) + "\u957f\u7684\u4e2d\u6587\u6807\u9898"
SHORTENED_CHINESE_TITLE = "\u82f9\u679c\u53d1\u5e03\u65b0\u7cfb\u7edf"


def temp_db_path():
    return ROOT / f"tmp-title-test-{uuid.uuid4().hex}.db"


class TitleProcessingTests(unittest.TestCase):
    def test_overlong_title_uses_chinese_character_budget(self):
        self.assertFalse(web_server._needs_title_summary(SHORT_CHINESE_TITLE))
        self.assertTrue(web_server._needs_title_summary(LONG_CHINESE_TITLE))

    def test_clean_title_summary_rejects_explanatory_output(self):
        self.assertEqual(web_server._clean_title_summary(f"\u300c{SHORTENED_CHINESE_TITLE}\u300d"), SHORTENED_CHINESE_TITLE)
        self.assertEqual(web_server._clean_title_summary(f"\u4ee5\u4e0b\u662f\u7b80\u5199\u540e\u7684\u6807\u9898\uff1a{SHORTENED_CHINESE_TITLE}"), SHORTENED_CHINESE_TITLE)
        self.assertTrue(web_server._is_valid_title_summary(f"\u4ee5\u4e0b\u662f\u7b80\u5199\u540e\u7684\u6807\u9898\uff1a{SHORTENED_CHINESE_TITLE}"))
        self.assertFalse(web_server._is_valid_title_summary(LONG_CHINESE_TITLE))

    def test_repair_title_summary_compacts_long_ai_output(self):
        raw = (
            "macOS 27 \u8d77\u82f9\u679c\u79fb\u9664\u4e86 AFP \u534f\u8bae\uff0c"
            "\u8001\u6b3e Time Capsule \u4e0d\u80fd\u518d\u7ed9 Time Machine "
            "\u505a\u65e0\u7ebf\u5907\u4efd\u4e86\u3002Time Machine \u4e4b\u540e"
            "\u53ea\u8ba4 SMBv2/SMBv3\u3002"
        )
        repaired = web_server._repair_title_summary(raw)
        self.assertTrue(web_server._is_valid_title_summary(repaired))
        self.assertNotIn("…", repaired)

    def test_save_article_title_update_preserves_original_title(self):
        db_path = temp_db_path()
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT)")
            conn.execute("INSERT INTO articles (id, title) VALUES (1, 'Original Long Title')")
            conn.commit()
            conn.close()

            old_news_db = web_server.NEWS_DB
            try:
                web_server.NEWS_DB = str(db_path)
                web_server._save_article_title_update(1, "Short Title", "title_summary")
                web_server._save_article_title_update(1, "Shorter Title", "title_summary")
            finally:
                web_server.NEWS_DB = old_news_db

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT title, original_title, title_updated_at, title_source FROM articles WHERE id = 1"
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], "Shorter Title")
            self.assertEqual(row[1], "Original Long Title")
            self.assertIsNotNone(row[2])
            self.assertEqual(row[3], "title_summary")
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(db_path) + suffix)
                except FileNotFoundError:
                    pass

    def test_title_updates_endpoint_returns_cursor_changes(self):
        db_path = temp_db_path()
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE articles ("
                "id INTEGER PRIMARY KEY, title TEXT, title_updated_at TEXT, title_source TEXT, "
                "source TEXT, feed_source TEXT, origin_source TEXT)"
            )
            conn.execute(
                "INSERT INTO articles (id, title, title_updated_at, title_source) VALUES "
                "(1, 'Old', '2026-06-11 08:00:00', 'title_summary'), "
                "(2, 'New', '2026-06-11 08:05:00', 'translation'), "
                "(3, 'Same Tick', '2026-06-11 08:05:00', 'title_summary')"
            )
            conn.commit()
            conn.close()

            old_db = refresh_server.DB_FILE
            old_schema_ready = refresh_server._schema_ready
            try:
                refresh_server.DB_FILE = db_path
                refresh_server._schema_ready = False
                body = refresh_server.api_title_updates({"since": ["2026-06-11 08:05:00|2"]})
            finally:
                refresh_server.DB_FILE = old_db
                refresh_server._schema_ready = old_schema_ready

            data = json.loads(body.decode("utf-8"))
            self.assertEqual([item["id"] for item in data["items"]], [3])
            self.assertEqual(data["cursor"], "2026-06-11 08:05:00|3")
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(db_path) + suffix)
                except FileNotFoundError:
                    pass

    def test_title_process_scans_more_than_recent_200_today_articles(self):
        db_path = temp_db_path()
        old_news_db = web_server.NEWS_DB
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE articles ("
                "id INTEGER PRIMARY KEY, title TEXT, date TEXT, timestamp INTEGER, "
                "source TEXT, feed_source TEXT, origin_source TEXT)"
            )
            long_title = LONG_CHINESE_TITLE
            rows = []
            for i in range(1, 251):
                title = long_title if i == 1 else f"普通标题 {i}"
                rows.append((i, title, today, 1000000 + i, "source", "", ""))
            conn.executemany(
                "INSERT INTO articles (id, title, date, timestamp, source, feed_source, origin_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
            conn.close()

            web_server.NEWS_DB = str(db_path)
            candidates = web_server._fetch_title_process_articles(
                {"auto_title_summary_enabled": 1, "auto_translate_title": 0},
                limit=20,
            )
            self.assertIn(1, [item["id"] for item in candidates])
        finally:
            web_server.NEWS_DB = old_news_db
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(db_path) + suffix)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
