import sqlite3
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_schema import (
    enable_wal_mode,
    ensure_article_schema,
    ensure_article_source_columns,
    ensure_deleted_articles_table,
)
from source_categories import cleanup_stale_source_categories, init_source_categories
import refresh_server


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_deleted_articles_table_is_created_by_shared_helper():
    conn = sqlite3.connect(":memory:")

    ensure_deleted_articles_table(conn)

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(deleted_articles)").fetchall()
    }
    assert columns == {"article_id", "title", "source", "deleted_by", "deleted_at"}


def test_source_migration_does_not_mask_unrelated_operational_errors():
    """Only a rechecked duplicate-column race is tolerated by the migrator."""
    raw = sqlite3.connect(":memory:")
    raw.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, source TEXT NOT NULL DEFAULT '')")

    class BrokenAlterConnection:
        @property
        def in_transaction(self):
            return raw.in_transaction

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("ALTER TABLE articles ADD COLUMN feed_source"):
                raise sqlite3.OperationalError("database disk image is malformed")
            return raw.execute(sql, *args, **kwargs)

        def commit(self):
            return raw.commit()

        def rollback(self):
            return raw.rollback()

    try:
        ensure_article_source_columns(BrokenAlterConnection())
    except sqlite3.OperationalError as exc:
        assert "malformed" in str(exc)
    else:  # pragma: no cover - makes accidental broad exception handling obvious
        raise AssertionError("unrelated OperationalError was incorrectly swallowed")


def test_enable_wal_mode_does_not_mask_unrelated_operational_errors():
    class BrokenConnection:
        def execute(self, sql):
            assert sql == "PRAGMA journal_mode=WAL"
            raise sqlite3.OperationalError("disk I/O error")

    try:
        enable_wal_mode(BrokenConnection())
    except sqlite3.OperationalError as exc:
        assert "disk I/O" in str(exc)
    else:  # pragma: no cover - makes accidental broad retry handling obvious
        raise AssertionError("unrelated OperationalError was incorrectly swallowed")


def test_legacy_article_body_is_preserved_when_original_body_column_is_added():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE articles (id INTEGER PRIMARY KEY, source TEXT NOT NULL DEFAULT '', body_html TEXT)"
    )
    conn.execute(
        "INSERT INTO articles (id, source, body_html) VALUES (1, 'Feed', '<p>Original body</p>')"
    )

    ensure_article_schema(conn)

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()
    }
    row = conn.execute(
        "SELECT body_html, original_body_html FROM articles WHERE id = 1"
    ).fetchone()
    assert "original_body_html" in columns
    assert row == ("<p>Original body</p>", "<p>Original body</p>")


def test_unauthenticated_news_detail_never_serves_shared_translated_body(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "news.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            title TEXT,
            source TEXT NOT NULL DEFAULT '',
            body_html TEXT,
            original_body_html TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO articles (id, title, source, body_html, original_body_html) "
        "VALUES (1, 'Title', 'Feed', '<p>共享译文</p>', '<p>Original body</p>')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(refresh_server, "DB_FILE", db_path)
    monkeypatch.setattr(refresh_server, "_schema_ready", False)
    refresh_server._schema_ready_event.clear()
    refresh_server.clear_article_cache()

    item = json.loads(refresh_server.api_news_detail(1).decode("utf-8"))

    assert item["body_html"] == "<p>Original body</p>"
    assert "original_body_html" not in item


def test_empty_article_table_preserves_alias_to_same_user_private_source():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
            feed_source TEXT NOT NULL DEFAULT ''
        )
        """
    )
    init_source_categories(conn)
    conn.execute(
        "INSERT INTO user_source_categories "
        "(user_id, source, category, label, updated_at) "
        "VALUES (1, 'Old Feed', 'Tech', 'Old Feed', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO user_source_aliases "
        "(user_id, alias_source, target_source, created_at) "
        "VALUES (1, 'Old Feed Alias', 'Old Feed', datetime('now'))"
    )
    conn.commit()

    cleanup_stale_source_categories(conn)

    assert _table_count(conn, "user_source_categories") == 1
    assert _table_count(conn, "user_source_aliases") == 1


def test_nonempty_article_table_preserves_manual_user_source_metadata():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
            feed_source TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "INSERT INTO articles (id, source, feed_source) VALUES (1, 'Live Feed', 'Live Feed')"
    )
    init_source_categories(conn)
    for source, status in (
        ("Live Feed", "manual"),
        ("Stale Feed", "manual"),
        ("Stale Automatic Feed", "pending"),
    ):
        conn.execute(
            "INSERT INTO user_source_categories "
            "(user_id, source, category, label, status, updated_at) "
            "VALUES (1, ?, 'Tech', ?, ?, datetime('now'))",
            (source, source, status),
        )
    conn.commit()

    cleanup_stale_source_categories(conn)

    rows = conn.execute(
        "SELECT source FROM user_source_categories ORDER BY source"
    ).fetchall()
    assert rows == [("Live Feed",), ("Stale Feed",)]
