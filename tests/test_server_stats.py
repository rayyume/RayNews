import os
import inspect
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import image_cache
import models
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
def access_log_env(tmp_path, monkeypatch):
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = tmp_path / "access-log.db"
    db = models.get_db()
    db.execute(
        "INSERT INTO users (email, password, nickname, role) VALUES (?, ?, ?, ?)",
        ("reader@example.com", "unused-test-hash", "reader", "user"),
    )
    db.commit()
    monkeypatch.setattr(models, "_last_access_log_prune_at", 0.0, raising=False)
    try:
        yield 1
    finally:
        models.close_db()
        models.DB_FILE = old_db_file


def test_record_access_window_call_is_read_only(access_log_env):
    user_id = access_log_env
    models.record_access(user_id)
    db = models.get_db()
    before = db.execute(
        "SELECT visit_count, last_seen_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    statements = []
    db.set_trace_callback(statements.append)

    models.record_access(user_id)

    after = db.execute(
        "SELECT visit_count, last_seen_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    db.set_trace_callback(None)
    assert tuple(after) == tuple(before)
    assert db.execute(
        "SELECT COUNT(*) FROM user_access_log WHERE user_id = ?",
        (user_id,),
    ).fetchone()[0] == 1
    writes = [sql for sql in statements if sql.lstrip().upper().startswith(("UPDATE", "INSERT"))]
    assert writes == []


def test_record_access_counts_again_after_window(access_log_env):
    user_id = access_log_env
    old_time = (datetime.utcnow() - timedelta(seconds=301)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    db = models.get_db()
    db.execute(
        "UPDATE users SET visit_count = 1, last_seen_at = ? WHERE id = ?",
        (old_time, user_id),
    )
    db.commit()

    models.record_access(user_id)

    row = db.execute(
        "SELECT visit_count, last_seen_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    assert row["visit_count"] == 2
    assert row["last_seen_at"] > old_time
    assert db.execute(
        "SELECT COUNT(*) FROM user_access_log WHERE user_id = ?",
        (user_id,),
    ).fetchone()[0] == 1


def test_concurrent_record_access_counts_only_once(access_log_env, monkeypatch):
    user_id = access_log_env
    old_time = (datetime.utcnow() - timedelta(seconds=301)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    db = models.get_db()
    db.execute(
        "UPDATE users SET visit_count = 0, last_seen_at = ? WHERE id = ?",
        (old_time, user_id),
    )
    db.commit()

    original_get_db = models.get_db
    barrier = threading.Barrier(2)

    class CursorBarrier:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchone(self):
            row = self._cursor.fetchone()
            barrier.wait(timeout=5)
            return row

    class ConnectionBarrier:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, params=()):
            cursor = self._connection.execute(sql, params)
            if sql.startswith("SELECT last_seen_at FROM users"):
                return CursorBarrier(cursor)
            return cursor

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(models, "get_db", lambda: ConnectionBarrier(original_get_db()))

    def record(_index):
        try:
            models.record_access(user_id)
        finally:
            models.close_db()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(record, range(2)))

    row = db.execute(
        "SELECT visit_count FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    assert row["visit_count"] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM user_access_log WHERE user_id = ?",
        (user_id,),
    ).fetchone()[0] == 1


def test_prune_access_log_removes_old_rows_and_throttles(access_log_env):
    user_id = access_log_env
    now = datetime.utcnow()
    db = models.get_db()
    db.executemany(
        "INSERT INTO user_access_log (user_id, accessed_at) VALUES (?, ?)",
        [
            (user_id, (now - timedelta(days=91)).strftime("%Y-%m-%d %H:%M:%S")),
            (user_id, now.strftime("%Y-%m-%d %H:%M:%S")),
        ],
    )
    db.commit()

    assert models.prune_access_log(now=now.timestamp()) == 1
    assert models.prune_access_log(now=now.timestamp() + 30) == 0
    rows = db.execute(
        "SELECT accessed_at FROM user_access_log ORDER BY accessed_at"
    ).fetchall()
    assert [row["accessed_at"] for row in rows] == [
        now.strftime("%Y-%m-%d %H:%M:%S")
    ]


def test_prune_access_log_failure_can_retry_immediately(access_log_env, monkeypatch):
    original_get_db = models.get_db
    failed = False

    class FailingConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, params=()):
            nonlocal failed
            if sql.startswith("DELETE FROM user_access_log") and not failed:
                failed = True
                raise RuntimeError("delete failed")
            return self._connection.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(models, "get_db", lambda: FailingConnection(original_get_db()))

    with pytest.raises(RuntimeError, match="delete failed"):
        models.prune_access_log(now=1_000_000)
    assert models.prune_access_log(now=1_000_000) == 0


def _make_cache(tmp_path, monkeypatch):
    cache_dir = tmp_path / "image_cache"
    monkeypatch.setattr(image_cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(image_cache, "DB_FILE", cache_dir / "cache.db")
    monkeypatch.setattr(image_cache, "IMAGE_CACHE_ENABLED", True)
    image_cache.init_cache()
    return cache_dir


def _insert_entry(cache_dir, url, *, pinned):
    url_hash = image_cache._url_hash(image_cache.normalize_image_url(url))
    rel = f"{url_hash[:2]}/{url_hash}.jpg"
    fpath = cache_dir / rel
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_bytes(b"x" * 10)
    conn = sqlite3.connect(cache_dir / "cache.db")
    now = int(time.time())
    conn.execute(
        "INSERT INTO image_cache_entries "
        "(url_hash, url, content_type, size_bytes, path, pinned, is_cover, created_at, accessed_at, hit_count) "
        "VALUES (?, ?, 'image/jpeg', 10, ?, ?, 0, ?, ?, 0)",
        (url_hash, image_cache.normalize_image_url(url), rel, 1 if pinned else 0, now, now),
    )
    conn.commit()
    conn.close()

    return url_hash, fpath


def test_evict_article_images_skips_pinned(tmp_path, monkeypatch):
    cache_dir = _make_cache(tmp_path, monkeypatch)
    keep_url = "https://example.com/keep.jpg"
    drop_url = "https://example.com/drop.jpg"
    keep_hash, keep_file = _insert_entry(cache_dir, keep_url, pinned=True)
    drop_hash, drop_file = _insert_entry(cache_dir, drop_url, pinned=False)

    body = f'<p><img src="{keep_url}"><img src="{drop_url}"></p>'
    deleted = image_cache.evict_article_images(body, thumb="")

    assert deleted == 1
    assert not drop_file.exists()          # unpinned image removed
    assert keep_file.exists()              # pinned (shared/favorited) image kept
    conn = sqlite3.connect(cache_dir / "cache.db")
    rows = {r[0] for r in conn.execute("SELECT url_hash FROM image_cache_entries")}
    conn.close()
    assert keep_hash in rows and drop_hash not in rows


def test_evict_article_images_keeps_image_referenced_by_another_article(tmp_path, monkeypatch):
    cache_dir = _make_cache(tmp_path, monkeypatch)
    url = "https://example.com/shared.jpg"
    url_hash, cached_file = _insert_entry(cache_dir, url, pinned=False)
    conn = sqlite3.connect(cache_dir / "cache.db")
    conn.executemany(
        "INSERT INTO image_cache_article_images (article_id, url_hash) VALUES (?, ?)",
        [(1, url_hash), (2, url_hash)],
    )
    conn.commit()
    conn.close()

    deleted = image_cache.evict_article_images(f'<img src="{url}">', article_id=1)

    assert deleted == 0
    assert cached_file.exists()


def test_unpin_article_images_accepts_a_batch_and_recomputes_once(tmp_path, monkeypatch):
    cache_dir = _make_cache(tmp_path, monkeypatch)
    url = "https://example.com/pinned.jpg"
    url_hash, _ = _insert_entry(cache_dir, url, pinned=True)
    conn = sqlite3.connect(cache_dir / "cache.db")
    conn.executemany(
        "INSERT INTO image_cache_article_images (article_id, url_hash) VALUES (?, ?)",
        [(1, url_hash), (2, url_hash)],
    )
    conn.commit()
    conn.close()

    image_cache.unpin_article_images([1, 2])
    conn = sqlite3.connect(cache_dir / "cache.db")
    assert conn.execute("SELECT COUNT(*) FROM image_cache_article_images").fetchone()[0] == 0
    assert conn.execute("SELECT pinned FROM image_cache_entries WHERE url_hash = ?", (url_hash,)).fetchone()[0] == 0
    conn.close()


def test_orphan_sweep_preserves_hashes_mapped_to_favorited_articles(tmp_path, monkeypatch):
    cache_dir = _make_cache(tmp_path, monkeypatch)
    url_hash, cached_file = _insert_entry(cache_dir, "https://example.com/favorite-only.jpg", pinned=True)
    conn = image_cache.open_cache_connection()
    try:
        conn.execute(
            "INSERT INTO image_cache_article_images (article_id, url_hash) VALUES (?, ?)",
            (99, url_hash),
        )
        conn.commit()
        deleted = image_cache.evict_unreferenced_images(set(), conn=conn)
    finally:
        conn.close()

    assert deleted == 0
    assert cached_file.exists()



def test_cache_stats_reports_count_and_size(tmp_path, monkeypatch):
    cache_dir = _make_cache(tmp_path, monkeypatch)
    _insert_entry(cache_dir, "https://example.com/a.jpg", pinned=False)
    _insert_entry(cache_dir, "https://example.com/b.jpg", pinned=True)
    stats = image_cache.cache_stats()
    assert stats["count"] == 2
    assert stats["used_bytes"] == 20
    assert stats["max_bytes"] == image_cache.MAX_CACHE_BYTES
    assert stats["enabled"] is True


def test_purge_dry_run_excludes_favorites(tmp_path, monkeypatch):
    db_path = tmp_path / f"news-{uuid.uuid4().hex}.db"
    conn = sqlite3.connect(db_path)
    conn.execute(ARTICLES_DDL)
    conn.executemany(
        "INSERT INTO articles (id, date, body_html, thumb) VALUES (?, ?, '', '')",
        [(1, "2026-07-01"), (2, "2026-07-10"), (3, "2026-08-01"), (4, "")],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    # Fresh per-thread connection store so _get_news_db() opens the patched NEWS_DB
    # rather than reusing a connection this thread opened for an earlier test.
    monkeypatch.setattr(web_server, "_news_conn_local", web_server.threading.local())
    # Article 2 is favorited by some user -> must be excluded.
    monkeypatch.setattr(web_server, "get_all_favorite_article_ids", lambda: [2])

    result = web_server._purge_articles_before("2026-07-15", dry_run=True)

    assert result["matched"] == 2           # ids 1 and 2 (id 3 is later, id 4 has no date)
    assert result["to_delete"] == 1         # only id 1 (2 excluded as favorite)
    assert result["favorites_excluded"] == 1
    assert result["deleted"] == 0
    # Dry run must not delete anything.
    check = sqlite3.connect(db_path)
    remaining = check.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    check.close()
    assert remaining == 4


def test_purge_rejects_bad_date_and_routes_registered():
    rules = {r.rule for r in web_server.app.url_map.iter_rules()}
    assert "/admin/server-stats" in rules
    assert "/admin/articles/purge" in rules
    # Endpoint validates the date format before touching the DB.
    assert web_server._PURGE_DATE_RE.match("2026-07-15")
    assert not web_server._PURGE_DATE_RE.match("2026/07/15")
    assert not web_server._PURGE_DATE_RE.match("bad")


def test_server_stats_reports_server_date_matching_purge_validation_today(monkeypatch):
    # The purge date picker's max must come from the same "today" the server itself
    # validates against (_parse_purge_before_date's date.today(), which respects the
    # process's TZ env var) — not the browser's own UTC/local date, which can disagree
    # with the server's around midnight in either timezone.
    import models

    monkeypatch.setattr(models, "get_user", lambda user_id: {"id": 202, "role": "admin"})
    monkeypatch.setattr(models, "record_access", lambda user_id: None)
    monkeypatch.setattr(web_server, "_path_size", lambda path: 0)
    monkeypatch.setattr(web_server, "_dir_size", lambda path: 0)
    monkeypatch.setattr(
        web_server, "cache_stats",
        lambda: {"enabled": True, "count": 0, "used_bytes": 0, "max_bytes": 0},
    )
    monkeypatch.setattr(
        web_server.shutil, "disk_usage",
        lambda path: type("Usage", (), {"total": 0, "used": 0, "free": 0})(),
    )
    monkeypatch.setattr(web_server, "_get_news_db", lambda: None)

    class FakeCursor:
        def fetchone(self):
            return (0,)

    class FakeConn:
        def execute(self, *args, **kwargs):
            return FakeCursor()

    monkeypatch.setattr(web_server, "get_db", lambda: FakeConn())
    monkeypatch.setattr(web_server, "count_users", lambda: 0)
    monkeypatch.setattr(web_server, "_container_resource_stats", lambda: {})
    monkeypatch.setattr(
        web_server, "date",
        type("Today", (), {"today": staticmethod(lambda: date(2026, 7, 16))}),
    )

    client = web_server.app.test_client()
    token = web_server.create_token(202, "admin")
    resp = client.get(
        "/admin/server-stats",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    assert resp.get_json()["server_date"] == "2026-07-16"


def test_purge_date_parser_rejects_invalid_and_future_dates(monkeypatch):
    monkeypatch.setattr(web_server, "date", type("Today", (), {"today": staticmethod(lambda: date(2026, 7, 16))}))

    assert web_server._parse_purge_before_date("2026-07-16") == date(2026, 7, 16)
    for value in ("9999-99-99", "2026-02-30", "2026-07-17"):
        assert web_server._parse_purge_before_date(value) is None


def test_container_stats_does_not_sleep_while_serving_request():
    assert "time.sleep" not in inspect.getsource(web_server._container_resource_stats)


def test_delete_article_ids_honours_cleanup_sources_when_nothing_matched(tmp_path, monkeypatch):
    # The batched purge passes cleanup_sources=False to skip the full-table stale
    # source scan per batch. A batch whose ids were all already deleted must not
    # sneak that scan back in through the early-return path.
    db_path = tmp_path / f"news-{uuid.uuid4().hex}.db"
    conn = sqlite3.connect(db_path)
    conn.execute(ARTICLES_DDL)
    conn.commit()
    conn.close()

    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    monkeypatch.setattr(web_server, "_news_conn_local", web_server.threading.local())
    monkeypatch.setattr(web_server, "_news_schema_ready_paths", set())

    calls = []
    monkeypatch.setattr(
        web_server, "cleanup_stale_source_categories",
        lambda conn: (calls.append(1), 0)[1],
    )

    skipped = web_server._delete_article_ids([999], cleanup_sources=False)
    assert skipped == {"deleted": 0, "deleted_sources": 0}
    assert calls == []

    swept = web_server._delete_article_ids([999], cleanup_sources=True)
    assert swept["deleted"] == 0
    assert calls == [1]


def test_purge_task_history_is_bounded_and_keeps_running_tasks(monkeypatch):
    # _purge_tasks used to grow for the life of the process.
    monkeypatch.setattr(web_server, "_purge_tasks", {})
    monkeypatch.setattr(web_server, "PURGE_TASK_HISTORY_LIMIT", 3)

    with web_server._purge_tasks_lock:
        web_server._purge_tasks["running"] = {"status": "running"}
        for i in range(6):
            web_server._purge_tasks[f"done-{i}"] = {
                "status": "completed", "finished_at": i,
            }
        web_server._trim_purge_tasks_locked()

    assert len(web_server._purge_tasks) == 3
    # A still-running purge must stay pollable no matter how many finished after it.
    assert "running" in web_server._purge_tasks
    # Oldest finished tasks are the ones evicted.
    assert "done-0" not in web_server._purge_tasks
    assert "done-5" in web_server._purge_tasks


def test_purge_worker_does_not_reuse_the_request_threads_connection(tmp_path, monkeypatch):
    # _get_news_db() hands out a thread-local connection. The purge worker runs in
    # its own thread, so it must ask for its own rather than closing over the
    # requesting thread's — driving one sqlite3 connection from two threads is the
    # exact hazard the per-thread design exists to remove.
    import inspect

    source = inspect.getsource(web_server._purge_articles_before)
    worker = source[source.index("def _run_purge()"):]
    assert "cleanup_stale_source_categories(purge_conn)" in worker
    assert "purge_conn = _get_news_db()" in worker
    # No use of the enclosing request-thread connection inside the worker.
    assert "cleanup_stale_source_categories(conn)" not in worker
