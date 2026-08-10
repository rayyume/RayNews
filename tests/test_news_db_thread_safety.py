import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


class _CommitObserver:
    """Delegate to a real SQLite connection and inspect only durable commits."""

    def __init__(self, connection, observer):
        self._connection = connection
        self._observer = observer

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def commit(self):
        self._connection.commit()
        self._observer()


def _make_source_maintenance_db(tmp_path):
    db = tmp_path / "source-maintenance.db"
    writer = sqlite3.connect(db)
    writer.row_factory = sqlite3.Row
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute(ARTICLES_DDL)
    source_categories.init_source_categories(writer)
    return db, writer


def test_ensure_article_sources_publishes_alias_normalization_atomically(tmp_path):
    """The alias normalization phase is one transaction: no partial namespace."""
    db, writer = _make_source_maintenance_db(tmp_path)
    writer.executemany(
        "INSERT INTO articles (id, source, feed_source) VALUES (?, ?, ?)",
        [(1, "alias-a", "alias-a"), (2, "alias-b", "alias-b")],
    )
    writer.executemany(
        "INSERT INTO source_aliases (alias_source, target_source) VALUES (?, ?)",
        [("alias-a", "target-a"), ("alias-b", "target-b")],
    )
    writer.commit()

    reader = sqlite3.connect(db)
    snapshots = []

    def observe_commit():
        normalized = reader.execute(
            "SELECT COUNT(*) FROM articles WHERE source LIKE 'target-%'"
        ).fetchone()[0]
        discovered = reader.execute(
            "SELECT COUNT(*) FROM source_categories WHERE source LIKE 'target-%'"
        ).fetchone()[0]
        snapshots.append((normalized, discovered))

    try:
        source_categories.ensure_article_sources(_CommitObserver(writer, observe_commit))
    finally:
        reader.close()
        writer.close()

    # init_source_categories commits first (nothing discovered yet), then the alias
    # UPDATEs and the DISTINCT discovery inserts publish together in one commit.
    assert snapshots == [(0, 0), (2, 2)]


def test_ensure_article_sources_rolls_back_all_aliases_on_mid_loop_failure(tmp_path):
    db, writer = _make_source_maintenance_db(tmp_path)
    writer.executemany(
        "INSERT INTO articles (id, source, feed_source) VALUES (?, ?, ?)",
        [(1, "alias-a", "alias-a"), (2, "broken", "broken")],
    )
    writer.executemany(
        "INSERT INTO source_aliases (alias_source, target_source) VALUES (?, ?)",
        [("alias-a", "target-a"), ("broken", "target")],
    )
    writer.execute(
        """CREATE TRIGGER reject_broken_alias
           BEFORE UPDATE ON articles WHEN OLD.source = 'broken'
           BEGIN SELECT RAISE(ABORT, 'forced alias failure'); END"""
    )
    writer.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced alias failure"):
        source_categories.ensure_article_sources(writer)

    reader = sqlite3.connect(db, timeout=0.1)
    try:
        assert not writer.in_transaction
        reader.execute("BEGIN IMMEDIATE")
        reader.rollback()
        # The alias phase is atomic: the alias that normalized before the failure
        # is rolled back too, so the namespace is not observed half-normalized.
        rolled_back = reader.execute(
            "SELECT COUNT(*) FROM articles WHERE source = 'target-a'"
        ).fetchone()[0]
        assert rolled_back == 0
    finally:
        reader.close()
        writer.close()


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


def _run_web_cold_start_race(tmp_path, startup_script: str, *, fetcher_compatible: bool = True):
    """Run a web migrator and a second service from the same cold-start gate."""
    db = tmp_path / "startup-race.db"
    conn = sqlite3.connect(db)
    if fetcher_compatible:
        conn.execute(ARTICLES_DDL)
    else:
        conn.execute(
            "CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT, source TEXT NOT NULL DEFAULT '')"
        )
    conn.executemany(
        "INSERT INTO articles (id, title, source) VALUES (?, ?, 'Feed')",
        [(number, f"article-{number}") for number in range(1, 50001)],
    )
    conn.commit()
    conn.close()
    ready_web = tmp_path / "web-ready"
    ready_other = tmp_path / "other-ready"
    start = tmp_path / "start"
    migration_lock = tmp_path / "migration-lock"
    web_script = """
import sqlite3
import sys
import time
from pathlib import Path
import web_server

db, ready, start, migration_lock = sys.argv[1:]
web_server.NEWS_DB = db
Path(ready).touch()
while not Path(start).exists():
    time.sleep(0.01)
conn = sqlite3.connect(db, timeout=10)
try:
    conn.execute("BEGIN IMMEDIATE")
    Path(migration_lock).touch()
    web_server._ensure_news_schema(conn)
    conn.commit()
finally:
    conn.close()
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", web_script, str(db), str(ready_web), str(start), str(migration_lock)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ),
        subprocess.Popen(
            [sys.executable, "-c", startup_script, str(db), str(ready_other), str(start), str(migration_lock)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ),
    ]
    try:
        deadline = time.monotonic() + 20
        while not (ready_web.exists() and ready_other.exists()):
            assert time.monotonic() < deadline, "cold-start workers did not become ready"
            time.sleep(0.01)
        start.touch()
        outputs = []
        for process in processes:
            try:
                outputs.append(process.communicate(timeout=20))
            except subprocess.TimeoutExpired:
                process.terminate()
                process.communicate(timeout=5)
                raise AssertionError("cold-start worker timed out")
        assert all(process.returncode == 0 for process in processes), outputs
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    conn = sqlite3.connect(db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    conn.close()
    assert {"feed_source", "origin_source", "original_title", "title_updated_at", "title_source"} <= columns


def test_web_migration_and_fetcher_cold_start_do_not_race_on_wal(tmp_path):
    _run_web_cold_start_race(
        tmp_path,
        """
import sys
import time
from pathlib import Path
import fetcher

db, ready, start, migration_lock = sys.argv[1:]
fetcher.DB_FILE = Path(db)
fetcher.OUTPUT_DIR = Path(db).parent
Path(ready).touch()
while not Path(start).exists():
    time.sleep(0.01)
while not Path(migration_lock).exists():
    time.sleep(0.01)
conn = fetcher.init_db()
conn.close()
""",
    )


def test_web_migration_and_refresh_cold_start_do_not_race_on_wal(tmp_path):
    _run_web_cold_start_race(
        tmp_path,
        """
import sys
import time
from pathlib import Path
import refresh_server

db, ready, start, migration_lock = sys.argv[1:]
refresh_server.DB_FILE = Path(db)
Path(ready).touch()
while not Path(start).exists():
    time.sleep(0.01)
while not Path(migration_lock).exists():
    time.sleep(0.01)
conn = refresh_server.get_db()
conn.close()
        """,
        fetcher_compatible=False,
    )


def _reset_news_schema_latch(monkeypatch):
    """Give a test its own migration latch and thread-local connection store."""
    monkeypatch.setattr(web_server, "_news_schema_ready_paths", set())
    monkeypatch.setattr(web_server, "_news_conn_local", threading.local())


def test_news_schema_migration_runs_once_per_process_not_per_connection(tmp_path, monkeypatch):
    # ensure_article_schema() takes SQLite's exclusive write lock (BEGIN IMMEDIATE)
    # before it can read PRAGMA table_info. Werkzeug hands every request a brand new
    # thread, so re-running the migration for each thread-local connection made every
    # single request contend for the write lock with the fetcher's streaming commits.
    _make_db(tmp_path, monkeypatch)
    _reset_news_schema_latch(monkeypatch)

    calls = []
    real = web_server.ensure_article_schema
    monkeypatch.setattr(
        web_server, "ensure_article_schema",
        lambda conn, *a, **kw: (calls.append(1), real(conn, *a, **kw))[1],
    )

    def touch():
        web_server._get_news_db()

    threads = [threading.Thread(target=touch) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1, f"migration ran {len(calls)} times across 6 request threads"


def test_news_schema_migration_reruns_when_news_db_path_changes(tmp_path, monkeypatch):
    # The latch is keyed by path, not a bare bool: a second database still has to be
    # migrated even though the first one already was.
    _reset_news_schema_latch(monkeypatch)
    _make_db(tmp_path, monkeypatch)
    web_server._get_news_db()

    second = tmp_path / "second"
    second.mkdir()
    _make_db(second, monkeypatch)
    conn = web_server._get_news_db()

    # A stale connection to the previous file must not be handed back.
    assert conn is not None
    columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    assert "original_body_html" in columns


def test_news_schema_latch_waits_for_articles_table_to_exist(tmp_path, monkeypatch):
    # news.db can exist before the fetcher has created `articles`. Latching on that
    # empty pass would permanently skip the real column migration.
    _reset_news_schema_latch(monkeypatch)
    db = tmp_path / "news.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db))

    conn = web_server._news_db_connect()
    try:
        web_server._ensure_news_schema(conn)
        assert str(db.resolve()) not in web_server._news_schema_ready_paths
        conn.execute(ARTICLES_DDL)
        conn.commit()
        web_server._ensure_news_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
        assert "original_body_html" in columns
    finally:
        conn.close()


def test_news_db_connections_carry_a_busy_timeout(tmp_path, monkeypatch):
    # The fetcher commits a streaming batch every couple of seconds and holds the
    # write lock while it does. sqlite3's 5s default surfaces as a hard
    # "database is locked" mid-cycle; every other writer in the tree uses 30s.
    _make_db(tmp_path, monkeypatch)
    conn = web_server._news_db_connect()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        conn.close()


class _TrackedNewsConnection:
    def __init__(self, *, explode=False):
        self.closed = False
        self.explode = explode
        self.queries = 0
        self.row_factory = None

    def execute(self, *_args, **_kwargs):
        self.queries += 1
        if self.explode:
            raise sqlite3.OperationalError("forced query failure")
        return self

    def fetchone(self):
        return (1,)

    def close(self):
        self.closed = True


def _install_one_shot_connection(tmp_path, monkeypatch):
    db = tmp_path / "news.db"
    db.touch()
    connection = _TrackedNewsConnection(explode=True)
    monkeypatch.setattr(web_server, "NEWS_DB", str(db))
    monkeypatch.setattr(web_server, "_news_db_connect", lambda: connection)
    return connection


def test_get_article_meta_closes_one_shot_connection_when_query_fails(tmp_path, monkeypatch):
    connection = _install_one_shot_connection(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "_ensure_news_schema", lambda _conn: None)

    assert web_server._get_article_meta(1) is None

    assert connection.queries == 1
    assert connection.closed


def test_daily_summary_helper_closes_one_shot_connection_when_query_fails(tmp_path, monkeypatch):
    connection = _install_one_shot_connection(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "_init_daily_summary_global_table", lambda: None)

    assert web_server._get_daily_summary_global_cache("2026-08-01") is None

    assert connection.queries == 1
    assert connection.closed


def test_ai_result_helper_closes_one_shot_connection_when_query_fails(tmp_path, monkeypatch):
    connection = _install_one_shot_connection(tmp_path, monkeypatch)
    monkeypatch.setattr(web_server, "_init_ai_results_table", lambda: None)

    assert web_server._get_ai_result(1) is None

    assert connection.queries == 1
    assert connection.closed


def test_get_news_db_keeps_thread_local_connection_open_between_queries(tmp_path, monkeypatch):
    db = tmp_path / "news.db"
    db.touch()
    connection = _TrackedNewsConnection()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db))
    monkeypatch.setattr(web_server, "_news_conn_local", threading.local())
    monkeypatch.setattr(web_server, "_news_db_connect", lambda: connection)
    monkeypatch.setattr(web_server, "_ensure_news_schema", lambda _conn: None)

    first = web_server._get_news_db()
    first.execute("SELECT 1")
    second = web_server._get_news_db()
    second.execute("SELECT 1")

    assert first is second is connection
    assert connection.queries == 2
    assert not connection.closed


def _reset_ai_results_schema_latch(monkeypatch):
    """Give a test an empty per-process ai_results schema latch."""
    monkeypatch.setattr(web_server, "_ai_results_schema_ready_paths", set(), raising=False)


def test_init_ai_results_table_opens_one_connection_after_success(tmp_path, monkeypatch):
    """A warmed database must not repeat ai_results DDL on later callers."""
    db = tmp_path / "news.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db))
    _reset_ai_results_schema_latch(monkeypatch)
    real = web_server._news_db_conn
    calls = []

    @contextmanager
    def counted():
        calls.append(1)
        with real() as conn:
            yield conn

    monkeypatch.setattr(web_server, "_news_db_conn", counted)

    assert web_server._init_ai_results_table() is True
    assert web_server._init_ai_results_table() is True
    assert len(calls) == 1


def test_init_ai_results_latch_is_scoped_by_database_path(tmp_path, monkeypatch):
    """Initializing one database must not skip another database's table setup."""
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    sqlite3.connect(first).close()
    sqlite3.connect(second).close()
    _reset_ai_results_schema_latch(monkeypatch)

    monkeypatch.setattr(web_server, "NEWS_DB", str(first))
    assert web_server._init_ai_results_table() is True
    monkeypatch.setattr(web_server, "NEWS_DB", str(second))
    assert web_server._init_ai_results_table() is True

    assert {str(first.resolve()), str(second.resolve())} == web_server._ai_results_schema_ready_paths


def test_init_ai_results_table_single_flights_concurrent_first_use(tmp_path, monkeypatch):
    """Simultaneous first callers share one initialization connection."""
    db = tmp_path / "news.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db))
    _reset_ai_results_schema_latch(monkeypatch)
    real = web_server._news_db_conn
    calls = []
    start = threading.Barrier(2)
    results = []

    @contextmanager
    def slow_counted():
        calls.append(1)
        # Keep the first initializer inside its connection long enough that a
        # missing process lock would allow the other caller to open its own.
        time.sleep(0.05)
        with real() as conn:
            yield conn

    monkeypatch.setattr(web_server, "_news_db_conn", slow_counted)

    def initialize():
        start.wait()
        results.append(web_server._init_ai_results_table())

    first = threading.Thread(target=initialize)
    second = threading.Thread(target=initialize)
    first.start(); second.start()
    first.join(); second.join()

    assert results == [True, True]
    assert len(calls) == 1


def test_init_ai_results_table_does_not_latch_a_failed_initialization(tmp_path, monkeypatch, caplog):
    """A transient schema error returns false and leaves the path retryable."""
    db = tmp_path / "news.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db))
    _reset_ai_results_schema_latch(monkeypatch)
    real = web_server._news_db_conn
    attempts = 0

    @contextmanager
    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("forced initialization failure")
        with real() as conn:
            yield conn

    monkeypatch.setattr(web_server, "_news_db_conn", fail_once)

    assert web_server._init_ai_results_table() is False
    assert str(db.resolve()) not in web_server._ai_results_schema_ready_paths
    assert "schema initialization failed" in caplog.text
    assert web_server._init_ai_results_table() is True
    assert str(db.resolve()) in web_server._ai_results_schema_ready_paths


def test_init_ai_results_table_does_not_latch_when_commit_fails(tmp_path, monkeypatch):
    """A failed commit cannot make a partially initialized schema ready."""
    db = tmp_path / "news.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db))
    _reset_ai_results_schema_latch(monkeypatch)
    real = web_server._news_db_conn

    class CommitFailingConnection:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def commit(self):
            raise sqlite3.OperationalError("forced commit failure")

    @contextmanager
    def commit_fails():
        with real() as conn:
            yield CommitFailingConnection(conn)

    monkeypatch.setattr(web_server, "_news_db_conn", commit_fails)

    assert web_server._init_ai_results_table() is False
    assert str(db.resolve()) not in web_server._ai_results_schema_ready_paths


def test_init_ai_results_table_returns_false_for_a_missing_database(tmp_path, monkeypatch):
    """A nonexistent news database cannot be considered schema-ready."""
    missing = tmp_path / "missing.db"
    monkeypatch.setattr(web_server, "NEWS_DB", str(missing))
    _reset_ai_results_schema_latch(monkeypatch)

    assert web_server._init_ai_results_table() is False
    assert web_server._ai_results_schema_ready_paths == set()
