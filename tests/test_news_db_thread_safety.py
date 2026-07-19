import sqlite3
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
