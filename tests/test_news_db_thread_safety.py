import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import web_server


ARTICLES_DDL = (
    "CREATE TABLE articles ("
    "id INTEGER PRIMARY KEY, title TEXT DEFAULT '', source TEXT DEFAULT '', "
    "feed_source TEXT DEFAULT '', origin_source TEXT DEFAULT '', time TEXT DEFAULT '', "
    "date TEXT DEFAULT '', timestamp INTEGER DEFAULT 0, thumb TEXT DEFAULT '', "
    "has_full_content INTEGER DEFAULT 0, telegraph_url TEXT DEFAULT '', "
    "body_html TEXT DEFAULT '', summary TEXT DEFAULT '')"
)


def _make_db(tmp_path, monkeypatch):
    db = tmp_path / "news.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(ARTICLES_DDL)
    for i in range(1, 201):
        conn.execute(
            "INSERT INTO articles (id, title, timestamp) VALUES (?, ?, ?)",
            (i, f"t{i}", i),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db))
    return db


def test_get_news_db_is_per_thread_not_shared(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    conns = {}

    def grab(name):
        # Two calls in the same thread must reuse the one connection.
        c1 = web_server._get_news_db()
        c2 = web_server._get_news_db()
        assert c1 is c2
        conns[name] = c1

    ta = threading.Thread(target=grab, args=("a",))
    tb = threading.Thread(target=grab, args=("b",))
    ta.start(); tb.start(); ta.join(); tb.join()

    # Different threads must NOT share one connection object — that shared object is
    # exactly what let concurrent cursors/transactions corrupt each other.
    assert conns["a"] is not conns["b"]


def test_concurrent_reads_and_writes_do_not_corrupt_transactions(tmp_path, monkeypatch):
    _make_db(tmp_path, monkeypatch)
    errors = []
    barrier = threading.Barrier(6)

    def reader():
        try:
            barrier.wait()
            conn = web_server._get_news_db()
            for _ in range(50):
                rows = conn.execute(
                    "SELECT id FROM articles ORDER BY timestamp DESC LIMIT 25"
                ).fetchall()
                assert len(rows) == 25
        except Exception as e:  # a shared connection would raise here under load
            errors.append(repr(e))

    def writer(base):
        try:
            barrier.wait()
            conn = web_server._get_news_db()
            for i in range(50):
                conn.execute(
                    "UPDATE articles SET title = ? WHERE id = ?",
                    (f"w{base}-{i}", (i % 200) + 1),
                )
                conn.commit()
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads += [threading.Thread(target=writer, args=(b,)) for b in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors


def test_direct_title_maintenance_and_request_schema_migration_do_not_race(tmp_path, monkeypatch):
    """A maintenance writer and request reader must share migration safety.

    `_save_article_title_update` used to run its title-column PRAGMA/ALTER
    outside the request-only lock, so an old database could be touched by both
    paths at exactly the same time.
    """
    monkeypatch.setattr(web_server, "_invalidate_refresh_server_cache", lambda article_id: None)
    errors = []
    for attempt in range(20):
        db = tmp_path / f"news-{attempt}.db"
        conn = sqlite3.connect(db)
        conn.execute(ARTICLES_DDL)
        conn.execute("INSERT INTO articles (id, title) VALUES (1, 'original')")
        conn.commit()
        conn.close()
        monkeypatch.setattr(web_server, "NEWS_DB", str(db))
        monkeypatch.setattr(web_server, "_news_conn_local", threading.local())
        start = threading.Barrier(2)

        def maintain_title():
            try:
                start.wait()
                assert web_server._save_article_title_update(1, "translated")
            except Exception as exc:
                errors.append(repr(exc))

        def serve_request():
            try:
                start.wait()
                article = web_server._get_article_meta(1)
                assert article and article["id"] == 1
            except Exception as exc:
                errors.append(repr(exc))

        maintenance = threading.Thread(target=maintain_title)
        request = threading.Thread(target=serve_request)
        maintenance.start(); request.start()
        maintenance.join(); request.join()

    assert not errors, errors


def test_cold_schema_migration_is_safe_across_processes(tmp_path):
    """Two fresh processes upgrading one old database must both succeed."""
    db = tmp_path / "cold-news.db"
    conn = sqlite3.connect(db)
    conn.execute(ARTICLES_DDL)
    conn.execute("INSERT INTO articles (id, title) VALUES (1, 'original')")
    conn.commit()
    conn.close()
    script = """
import sqlite3
import sys
import web_server

web_server.NEWS_DB = sys.argv[1]
conn = sqlite3.connect(web_server.NEWS_DB, timeout=10)
try:
    web_server._ensure_news_schema(conn)
finally:
    conn.close()
"""
    commands = [
        [sys.executable, "-c", script, str(db)],
        [sys.executable, "-c", script, str(db)],
    ]
    processes = [subprocess.Popen(command, cwd=ROOT, stderr=subprocess.PIPE, text=True)
                 for command in commands]
    stderr = []
    for process in processes:
        _, err = process.communicate(timeout=20)
        stderr.append(err)
        assert process.returncode == 0, err

    conn = sqlite3.connect(db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    tombstone_columns = {row[1] for row in conn.execute("PRAGMA table_info(deleted_articles)")}
    conn.close()
    assert {"feed_source", "origin_source", "original_title", "title_updated_at", "title_source"} <= columns
    assert tombstone_columns == {"article_id", "title", "source", "deleted_by", "deleted_at"}
