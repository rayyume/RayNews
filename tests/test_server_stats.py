import os
import inspect
import sqlite3
import sys
import time
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import image_cache
import web_server


ARTICLES_DDL = (
    "CREATE TABLE articles ("
    "id INTEGER PRIMARY KEY, title TEXT DEFAULT '', source TEXT DEFAULT '', "
    "feed_source TEXT DEFAULT '', origin_source TEXT DEFAULT '', time TEXT DEFAULT '', "
    "date TEXT DEFAULT '', timestamp INTEGER DEFAULT 0, thumb TEXT DEFAULT '', "
    "has_full_content INTEGER DEFAULT 0, telegraph_url TEXT DEFAULT '', "
    "body_html TEXT DEFAULT '', summary TEXT DEFAULT '')"
)


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
    monkeypatch.setattr(web_server, "_news_conn", None)
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


def test_purge_date_parser_rejects_invalid_and_future_dates(monkeypatch):
    monkeypatch.setattr(web_server, "date", type("Today", (), {"today": staticmethod(lambda: date(2026, 7, 16))}))

    assert web_server._parse_purge_before_date("2026-07-16") == date(2026, 7, 16)
    for value in ("9999-99-99", "2026-02-30", "2026-07-17"):
        assert web_server._parse_purge_before_date(value) is None


def test_container_stats_does_not_sleep_while_serving_request():
    assert "time.sleep" not in inspect.getsource(web_server._container_resource_stats)
