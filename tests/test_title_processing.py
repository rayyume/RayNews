import json
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import refresh_server
import web_server


def temp_db_path():
    return ROOT / f"tmp-title-test-{uuid.uuid4().hex}.db"


class TitleProcessingTests(unittest.TestCase):
    def test_overlong_title_uses_chinese_character_budget(self):
        self.assertFalse(web_server._needs_title_summary("苹果发布 Vision Pro 新系统"))
        self.assertTrue(web_server._needs_title_summary("这是一个非常非常非常非常非常非常非常非常非常非常非常长的中文标题"))

    def test_clean_title_summary_rejects_explanatory_output(self):
        self.assertEqual(web_server._clean_title_summary("「苹果发布新系统」"), "苹果发布新系统")
        self.assertFalse(web_server._is_valid_title_summary("以下是简写后的标题：苹果发布新系统"))
        self.assertFalse(web_server._is_valid_title_summary("这是一个非常非常非常非常非常非常非常非常非常非常非常长的中文标题"))

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


if __name__ == "__main__":
    unittest.main()
