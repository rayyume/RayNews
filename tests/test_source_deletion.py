import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import models
import source_categories
import web_server


ARTICLES_DDL = (
    "CREATE TABLE articles ("
    "id INTEGER PRIMARY KEY, title TEXT DEFAULT '', source TEXT DEFAULT '', "
    "feed_source TEXT DEFAULT '', origin_source TEXT DEFAULT '', time TEXT DEFAULT '', "
    "date TEXT DEFAULT '', timestamp INTEGER DEFAULT 0, thumb TEXT DEFAULT '', "
    "has_full_content INTEGER DEFAULT 0, telegraph_url TEXT DEFAULT '', "
    "body_html TEXT DEFAULT '', summary TEXT DEFAULT '')"
)


@pytest.fixture
def source_deletion_env(tmp_path, monkeypatch):
    """Build isolated application/news databases for source-group deletion."""
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = tmp_path / "raynews.db"
    news_path = tmp_path / "news.db"
    news_conn = sqlite3.connect(news_path)
    news_conn.execute("PRAGMA journal_mode=WAL")
    news_conn.execute(ARTICLES_DDL)
    news_conn.execute("CREATE TABLE ai_results (article_id INTEGER PRIMARY KEY, summary TEXT)")
    source_categories.init_source_categories(news_conn)
    news_conn.executemany(
        "INSERT INTO articles (id, title, source, feed_source) VALUES (?, ?, ?, ?)",
        [
            (1, "Primary article", "Primary", "Primary"),
            (2, "Variant article", "Variant", "Variant"),
            (3, "Legacy article", "Legacy", "Legacy"),
            (4, "Unrelated article", "Unrelated", "Unrelated"),
        ],
    )
    news_conn.executemany(
        "INSERT INTO ai_results (article_id, summary) VALUES (?, ?)",
        [(article_id, f"summary {article_id}") for article_id in range(1, 5)],
    )
    news_conn.executemany(
        "INSERT INTO source_categories (source, category, label, status) "
        "VALUES (?, 'Tech', ?, 'manual')",
        [(source, source) for source in ("Primary", "Variant", "Unrelated")],
    )
    news_conn.executemany(
        "INSERT INTO user_source_categories "
        "(user_id, source, category, label, status) VALUES (1, ?, 'Biz', ?, 'manual')",
        [(source, source) for source in ("Primary", "Variant", "Unrelated")],
    )
    news_conn.executemany(
        "INSERT INTO source_aliases (alias_source, target_source) VALUES (?, ?)",
        [("Legacy", "Primary"), ("Variant", "External")],
    )
    news_conn.executemany(
        "INSERT INTO user_source_aliases "
        "(user_id, alias_source, target_source) VALUES (1, ?, ?)",
        [("Legacy", "Primary"), ("Variant", "External")],
    )
    news_conn.commit()

    app_conn = models.get_db()
    app_conn.execute(
        "INSERT INTO users (email, password, nickname, role) VALUES (?, ?, ?, ?)",
        ("admin@example.com", "unused-test-hash", "admin", "admin"),
    )
    app_conn.execute("INSERT INTO favorites (user_id, article_id) VALUES (1, 1)")
    app_conn.commit()

    monkeypatch.setattr(web_server, "NEWS_DB", str(news_path))
    monkeypatch.setattr(web_server, "_news_conn_local", threading.local())
    monkeypatch.setattr(web_server, "_news_schema_ready_paths", set())
    try:
        yield {
            "client": web_server.app.test_client(),
            "admin_headers": {
                "Authorization": f"Bearer {web_server.create_token(1, 'admin')}"
            },
            "news_conn": news_conn,
        }
    finally:
        server_conn = getattr(web_server._news_conn_local, "conn", None)
        if server_conn is not None:
            server_conn.close()
        web_server._news_conn_local = threading.local()
        news_conn.close()
        models.close_db()
        models.DB_FILE = old_db_file


def test_delete_source_group_removes_articles_tombstones_and_metadata(source_deletion_env):
    """Deleting a resolved group removes its articles and all connected metadata."""
    client = source_deletion_env["client"]
    admin_headers = source_deletion_env["admin_headers"]
    news_conn = source_deletion_env["news_conn"]

    response = client.delete(
        "/sources/articles",
        headers=admin_headers,
        json={"sources": ["Primary", "Variant"]},
    )

    assert response.status_code == 200
    assert response.get_json()["deleted"] == 3
    assert {
        row[0] for row in news_conn.execute("SELECT id FROM articles")
    } == {4}
    assert {
        row[0] for row in news_conn.execute("SELECT article_id FROM deleted_articles")
    } == {1, 2, 3}
    assert news_conn.execute(
        "SELECT 1 FROM source_categories WHERE source IN ('Primary', 'Variant')"
    ).fetchone() is None
    assert news_conn.execute(
        "SELECT 1 FROM user_source_categories WHERE source IN ('Primary', 'Variant')"
    ).fetchone() is None
    assert news_conn.execute(
        "SELECT 1 FROM source_aliases "
        "WHERE alias_source IN ('Legacy', 'Variant') OR target_source IN ('Primary', 'Variant')"
    ).fetchone() is None
    assert news_conn.execute(
        "SELECT 1 FROM user_source_aliases "
        "WHERE alias_source IN ('Legacy', 'Variant') OR target_source IN ('Primary', 'Variant')"
    ).fetchone() is None
    assert news_conn.execute(
        "SELECT 1 FROM source_categories WHERE source = 'Unrelated'"
    ).fetchone() is not None


def test_delete_source_group_removes_zero_article_source_metadata(source_deletion_env):
    """A manual source without articles still has all of its metadata purged."""
    client = source_deletion_env["client"]
    admin_headers = source_deletion_env["admin_headers"]
    news_conn = source_deletion_env["news_conn"]
    news_conn.execute(
        "INSERT INTO source_categories (source, category, label, status) "
        "VALUES ('Empty Feed', 'Info', 'Empty Feed', 'manual')"
    )
    news_conn.commit()

    response = client.delete(
        "/sources/articles",
        headers=admin_headers,
        json={"sources": ["Empty Feed"]},
    )

    assert response.status_code == 200
    assert response.get_json()["deleted"] == 0
    assert news_conn.execute(
        "SELECT 1 FROM source_categories WHERE source = 'Empty Feed'"
    ).fetchone() is None


def test_delete_source_group_reports_metadata_failure(source_deletion_env, monkeypatch):
    """A metadata failure rolls back news writes and defers external side effects."""
    client = source_deletion_env["client"]
    admin_headers = source_deletion_env["admin_headers"]
    news_conn = source_deletion_env["news_conn"]
    unpinned = []

    def fail_metadata_delete(_conn, _sources):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(web_server, "delete_source_metadata", fail_metadata_delete)
    monkeypatch.setattr(web_server, "unpin_article_images", lambda ids: unpinned.append(ids))

    response = client.delete(
        "/sources/articles",
        headers=admin_headers,
        json={"sources": ["Primary", "Variant"]},
    )

    assert response.status_code == 500
    assert response.get_json() == {"error": "failed to delete source metadata"}
    assert {row[0] for row in news_conn.execute("SELECT id FROM articles")} == {1, 2, 3, 4}
    assert news_conn.execute("SELECT 1 FROM deleted_articles").fetchone() is None
    assert {row[0] for row in news_conn.execute("SELECT article_id FROM ai_results")} == {1, 2, 3, 4}
    assert {row[0] for row in news_conn.execute("SELECT source FROM source_categories")} == {
        "Primary", "Variant", "Unrelated",
    }
    assert {row[0] for row in news_conn.execute("SELECT source FROM user_source_categories")} == {
        "Primary", "Variant", "Unrelated",
    }
    assert {
        tuple(row) for row in news_conn.execute(
            "SELECT alias_source, target_source FROM source_aliases"
        )
    } == {("Legacy", "Primary"), ("Variant", "External")}
    assert {
        tuple(row) for row in news_conn.execute(
            "SELECT alias_source, target_source FROM user_source_aliases"
        )
    } == {("Legacy", "Primary"), ("Variant", "External")}
    assert models.get_db().execute(
        "SELECT 1 FROM favorites WHERE user_id = 1 AND article_id = 1"
    ).fetchone() is not None
    assert unpinned == []
