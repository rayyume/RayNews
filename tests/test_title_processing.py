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
ORIGINAL_RESTAURANT_TITLE = "\u300a\u718a\u5bb6\u9910\u9986\u300b\u8ba9\u6700\u540e\u51e0\u79d2\u7269\u5c3d\u5176\u7528"
SHORT_RESTAURANT_TITLE = "\u300a\u718a\u5bb6\u9910\u9986\u300b\u5229\u7528\u6700\u540e\u51e0\u79d2\u521b\u9020\u7b11\u70b9"


def temp_db_path():
    return ROOT / f"tmp-title-test-{uuid.uuid4().hex}.db"


class TitleProcessingTests(unittest.TestCase):
    def test_overlong_title_uses_chinese_character_budget(self):
        self.assertFalse(web_server._needs_title_summary(SHORT_CHINESE_TITLE))
        self.assertTrue(web_server._needs_title_summary(LONG_CHINESE_TITLE))
        self.assertFalse(web_server._needs_title_summary(
            "\u6ce1\u6ce1\u739b\u7279\u56de\u5e94\u4f9d\u8d56\u5355\u4e00\u7206\u6b3e\u62c5\u5fe7 "
            "\u79f0\u7f8e\u56fd\u975eLabubu\u591a\u5143\u5316\u9500\u552e\u989d\u5df2\u536050%"
        ))
        self.assertFalse(web_server._needs_title_summary(
            "OpenAI Codex CLI 支持 GPT-5 Code 模型"
        ))

    def test_clean_title_summary_rejects_explanatory_output(self):
        self.assertEqual(web_server._clean_title_summary(f"\u300c{SHORTENED_CHINESE_TITLE}\u300d"), SHORTENED_CHINESE_TITLE)
        self.assertEqual(web_server._clean_title_summary(f"\u4ee5\u4e0b\u662f\u7b80\u5199\u540e\u7684\u6807\u9898\uff1a{SHORTENED_CHINESE_TITLE}"), SHORTENED_CHINESE_TITLE)
        self.assertTrue(web_server._is_valid_title_summary(f"\u4ee5\u4e0b\u662f\u7b80\u5199\u540e\u7684\u6807\u9898\uff1a{SHORTENED_CHINESE_TITLE}"))
        self.assertFalse(web_server._is_valid_title_summary(LONG_CHINESE_TITLE))

        self.assertEqual(web_server._clean_title_summary(ORIGINAL_RESTAURANT_TITLE), ORIGINAL_RESTAURANT_TITLE)

    def test_ai_title_summary_rejects_numeric_and_broken_titles(self):
        for value in ("3998", "500"):
            result = web_server._validate_ai_title_summary_result(
                {"title": value, "valid": True, "reason": "AI thinks it is short"},
                original_title="\u683c\u9686\u6c47\u62a5\u9053\u516c\u53f8\u80a1\u4ef7\u5f02\u52a8",
            )
            self.assertFalse(result["valid"], value)

        broken = web_server._validate_ai_title_summary_result(
            {"title": "\u718a\u5bb6\u9910\u9986\u300b\u8ba9\u6700\u540e\u51e0\u79d2\u7269\u5c3d\u5176\u7528", "valid": True},
            original_title=ORIGINAL_RESTAURANT_TITLE,
        )
        self.assertFalse(broken["valid"])

        valid = web_server._validate_ai_title_summary_result(
            {"title": SHORT_RESTAURANT_TITLE, "valid": True},
            original_title=ORIGINAL_RESTAURANT_TITLE,
        )
        self.assertTrue(valid["valid"])
        self.assertEqual(valid["title"], SHORT_RESTAURANT_TITLE)

    def test_ai_title_summary_rejects_ai_self_invalid(self):
        result = web_server._validate_ai_title_summary_result(
            {"title": "\u82f9\u679c\u53d1\u5e03\u65b0\u7cfb\u7edf", "valid": False, "reason": "\u4fe1\u606f\u4e0d\u8db3"},
            original_title="\u82f9\u679c\u53d1\u5e03 Vision Pro \u65b0\u7cfb\u7edf",
        )
        self.assertFalse(result["valid"])
        self.assertIn("\u4fe1\u606f\u4e0d\u8db3", result["reason"])

    def test_parse_title_summary_json_result(self):
        parsed = web_server._parse_title_summary_result(
            '{"title":"\u82f9\u679c\u53d1\u5e03\u65b0\u7cfb\u7edf","valid":true,"reason":"\u4fdd\u7559\u4e3b\u4f53\u548c\u4e8b\u4ef6"}'
        )
        self.assertEqual(parsed["title"], "\u82f9\u679c\u53d1\u5e03\u65b0\u7cfb\u7edf")
        self.assertTrue(parsed["valid"])

        legacy = web_server._parse_title_summary_result("\u82f9\u679c\u53d1\u5e03\u65b0\u7cfb\u7edf")
        self.assertEqual(legacy["title"], "\u82f9\u679c\u53d1\u5e03\u65b0\u7cfb\u7edf")
        self.assertTrue(legacy["valid"])

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

    def test_empty_ai_title_summary_can_fallback_to_original_title(self):
        original = (
            "macOS 27 \u8d77\u82f9\u679c\u79fb\u9664\u4e86 AFP \u534f\u8bae\uff0c"
            "\u8001\u6b3e Time Capsule \u4e0d\u80fd\u518d\u7ed9 Time Machine "
            "\u505a\u65e0\u7ebf\u5907\u4efd\u4e86"
        )
        repaired = web_server._repair_title_summary("") or web_server._repair_title_summary(original)
        self.assertTrue(web_server._is_valid_title_summary(repaired))

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

    def test_cached_numeric_title_summary_is_not_written_back(self):
        db_path = temp_db_path()
        old_news_db = web_server.NEWS_DB
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE articles ("
                "id INTEGER PRIMARY KEY, title TEXT, date TEXT, timestamp INTEGER, "
                "source TEXT, feed_source TEXT, origin_source TEXT)"
            )
            conn.execute(
                "CREATE TABLE ai_results ("
                "article_id INTEGER PRIMARY KEY, summary TEXT, translation TEXT, "
                "title_summary TEXT, title_summary_error TEXT, title_summary_error_at TEXT)"
            )
            original = "\u683c\u9686\u6c47\u957f\u6807\u9898" * 8
            conn.execute(
                "INSERT INTO articles (id, title, date, timestamp, source, feed_source, origin_source) "
                "VALUES (1, ?, '2026-06-11', 1, ?, '', '')",
                (original, "\u683c\u9686\u6c47"),
            )
            conn.execute("INSERT INTO ai_results (article_id, title_summary) VALUES (1, '3998')")
            conn.commit()
            conn.close()

            web_server.NEWS_DB = str(db_path)
            changed = web_server._process_article_title(
                {"id": 1, "title": original, "title_summary_needed": True},
                {"auto_title_summary_enabled": 1, "provider_type": "openai", "model": "fake"},
            )

            conn = sqlite3.connect(db_path)
            title, error = conn.execute(
                "SELECT a.title, r.title_summary_error "
                "FROM articles a LEFT JOIN ai_results r ON r.article_id = a.id WHERE a.id = 1"
            ).fetchone()
            conn.close()
            self.assertFalse(changed)
            self.assertNotEqual(title, "3998")
            self.assertIn("invalid cached title summary", error)
        finally:
            web_server.NEWS_DB = old_news_db
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(db_path) + suffix)
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    unittest.main()
