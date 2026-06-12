#!/usr/bin/env python3
"""Tiny HTTP server: runs fetcher.py on GET /refresh, periodic auto-refresh, and serves SQLite-backed API."""
import http.server
import subprocess
import json
import sys
import logging
import threading
import urllib.parse
import os
import sqlite3
import re
import time
from pathlib import Path

from image_cache import (
    cache_image,
    enqueue_article_image_prefetch,
    fetch_remote_image,
    get_cached_image,
)
from news_schema import ensure_deleted_articles_table
from source_categories import (
    CATEGORY_NAMES, CATEGORY_ORDER, cleanup_stale_source_categories,
    ensure_article_source_columns, source_rows,
)

REFRESH_INTERVAL = 900  # 15 minutes
LOCK_FILE = "/tmp/raynews-fetcher.lock"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_FILE = DATA_DIR / "news.db"
NEWS_JSON_FILE = DATA_DIR / "news.json"
STATE_FILE = DATA_DIR / "fetcher_state.json"
LAST_FETCH_STATUS = {
    "status": "never",
    "returncode": None,
    "stdout": "",
    "stderr": "",
    "updated_at": None,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [refresh] %(message)s")
log = logging.getLogger("refresh")
_schema_lock = threading.Lock()
_schema_ready = False


def ensure_schema_once(conn: sqlite3.Connection) -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        ensure_article_source_columns(conn)
        ensure_article_title_columns(conn)
        ensure_deleted_articles_table(conn)
        conn.commit()
        _schema_ready = True


def ensure_article_title_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "original_title" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN original_title TEXT")
    if "title_updated_at" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN title_updated_at TEXT")
    if "title_source" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN title_source TEXT")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    ensure_schema_once(conn)
    return conn


# In-memory cache for article detail responses — invalidated on fetcher run
_article_cache: dict[int, bytes] = {}
_article_cache_lock = threading.Lock()


def clear_article_cache():
    with _article_cache_lock:
        _article_cache.clear()


def acquire_lock() -> bool:
    """Try to acquire a lock file atomically. Returns True if acquired."""
    try:
        os.makedirs(LOCK_FILE, exist_ok=False)
        return True
    except FileExistsError:
        return False


def release_lock():
    """Remove the lock file."""
    try:
        os.rmdir(LOCK_FILE)
    except OSError:
        pass


def run_fetcher():
    """Run fetcher.py and return the result dict + HTTP status code."""
    if not acquire_lock():
        log.warning("Fetcher already running — skipping")
        body = json.dumps({"status": "skipped", "error": "fetcher already running"}).encode()
        return body, 429
    existing_article_ids = article_id_snapshot()
    try:
        log.info("Triggering fetcher...")
        result = subprocess.run(
            ["python3", "/app/fetcher.py"],
            capture_output=True, text=True, timeout=120,
        )
        is_ok = result.returncode == 0
        body = json.dumps({
            "status": "ok" if is_ok else "error",
            "returncode": result.returncode,
            "stdout": result.stdout[-300:],
            "stderr": result.stderr[-300:],
        }).encode()
        LAST_FETCH_STATUS.update({
            "status": "ok" if is_ok else "error",
            "returncode": result.returncode,
            "stdout": result.stdout[-300:],
            "stderr": result.stderr[-300:],
            "updated_at": int(time.time()),
        })
        log.info(f"Fetcher done (exit={result.returncode})")
        if is_ok:
            clear_article_cache()
            try:
                conn = sqlite3.connect(str(DB_FILE))
                conn.row_factory = sqlite3.Row
                ensure_article_source_columns(conn)
                deleted = cleanup_stale_source_categories(conn)
                conn.commit()
                conn.close()
                if deleted:
                    log.info(f"Cleaned up {deleted} stale source(s)")
            except Exception as e:
                log.warning(f"Source cleanup failed: {e}")
            threading.Thread(
                target=enqueue_new_article_images,
                args=(existing_article_ids,),
                daemon=True,
            ).start()
        return body, 200 if is_ok else 500
    except subprocess.TimeoutExpired:
        LAST_FETCH_STATUS.update({
            "status": "error",
            "returncode": None,
            "stdout": "",
            "stderr": "timeout",
            "updated_at": int(time.time()),
        })
        body = json.dumps({"status": "error", "error": "timeout"}).encode()
        return body, 500
    except Exception as e:
        LAST_FETCH_STATUS.update({
            "status": "error",
            "returncode": None,
            "stdout": "",
            "stderr": str(e)[-300:],
            "updated_at": int(time.time()),
        })
        body = json.dumps({"status": "error", "error": str(e)}).encode()
        return body, 500
    finally:
        release_lock()


def article_id_snapshot() -> set[int]:
    """Return current article IDs so refresh can queue only newly inserted images."""
    conn = None
    try:
        if not DB_FILE.exists():
            return set()
        conn = sqlite3.connect(str(DB_FILE), timeout=30)
        rows = conn.execute("SELECT id FROM articles").fetchall()
        return {int(row[0]) for row in rows}
    except Exception as exc:
        log.warning(f"Article snapshot failed: {exc}")
        return set()
    finally:
        if conn:
            conn.close()


def enqueue_new_article_images(existing_article_ids: set[int]) -> None:
    """Queue image cache warmup for newly fetched articles without blocking refresh."""
    conn = None
    try:
        conn = sqlite3.connect(str(DB_FILE), timeout=30)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, thumb, body_html
            FROM articles
            ORDER BY timestamp DESC
            """
        ).fetchall()
        rows = [row for row in rows if int(row["id"]) not in existing_article_ids]
        queued = 0
        for row in rows:
            queued += enqueue_article_image_prefetch(row["id"], row["body_html"], row["thumb"])
        if queued:
            log.info(f"Queued {queued} image(s) for background cache warmup")
    except Exception as exc:
        log.warning(f"Image prefetch enqueue failed: {exc}")
    finally:
        if conn:
            conn.close()


def periodic_refresh():
    """Run fetcher periodically in the background."""
    body, _ = run_fetcher()
    threading.Timer(REFRESH_INTERVAL, periodic_refresh).start()


# ─── API Handlers ─────────────────────────────────────────

def _diagnostics(count: int | None = None) -> dict:
    channel = (os.environ.get("TELEGRAM_CHANNEL") or "").strip()
    exists = DB_FILE.exists()
    try:
        db_size = DB_FILE.stat().st_size if exists else 0
    except OSError:
        db_size = 0
    news_json = {"exists": NEWS_JSON_FILE.exists(), "size": 0, "count": None}
    if NEWS_JSON_FILE.exists():
        try:
            news_json["size"] = NEWS_JSON_FILE.stat().st_size
            data = json.loads(NEWS_JSON_FILE.read_text(encoding="utf-8"))
            news_json["count"] = data.get("count", len(data.get("items", [])))
        except Exception as e:
            news_json["error"] = str(e)
    state = {"exists": STATE_FILE.exists(), "last_seen_id": None}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state["last_seen_id"] = data.get("last_seen_id")
        except Exception as e:
            state["error"] = str(e)
    return {
        "data_dir": str(DATA_DIR),
        "db_path": str(DB_FILE),
        "db_exists": exists,
        "db_size": db_size,
        "article_count": count,
        "news_json": news_json,
        "fetcher_state": state,
        "telegram_channel_configured": bool(channel and channel != "your_channel"),
        "telegram_channel": channel if channel and channel != "your_channel" else "",
        "telegram_channel_default": not bool(channel and channel != "your_channel"),
        "last_fetch": dict(LAST_FETCH_STATUS),
    }


def api_meta() -> bytes:
    """GET /api/meta — total article count."""
    conn = None
    try:
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        return json.dumps({"count": count, "diagnostics": _diagnostics(count)}).encode()
    except Exception as e:
        return json.dumps({"error": str(e), "diagnostics": _diagnostics(None)}).encode()
    finally:
        if conn:
            conn.close()


_DISPLAY_ATTRIBUTION_RE = re.compile(
    r"(?is)(?:"
    r"\s*<p[^>]*>\s*(?:出处\s*[:：]\s*|via\s*)"
    r"(?:<a\b[^>]*>.*?</a>|[^<\r\n]{1,80})\s*</p>\s*|"
    r"(?:^|[\r\n])\s*(?:出处\s*[:：]\s*|via\s*)"
    r"(?:<a\b[^>]*>.*?</a>|[^\r\n]{1,80})\s*"
    r")$"
)


def _strip_display_attribution(value: str | None) -> str | None:
    if not value:
        return value
    cleaned = value
    while True:
        next_value = _DISPLAY_ATTRIBUTION_RE.sub("", cleaned).rstrip()
        if next_value == cleaned:
            return cleaned
        cleaned = next_value


def _clean_article_display_fields(item: dict) -> dict:
    for field in ("summary", "body_html"):
        if field in item:
            item[field] = _strip_display_attribution(item.get(field))
    return item


def api_news_list(params: dict) -> bytes:
    """GET /api/news — paginated or incremental list (no body_html)."""
    try:
        page = int(params.get("page", ["1"])[0])
        size = int(params.get("size", ["99999"])[0])
        size = min(max(size, 1), 99999)
        since = params.get("since", [None])[0]
        query = (params.get("q", [""])[0] or "").strip()
    except (ValueError, IndexError):
        return json.dumps({"error": "invalid params"}).encode()

    conn = None
    try:
        conn = get_db()
        if since:
            since_ts = int(since)
            rows = conn.execute(
                "SELECT id, title, COALESCE(NULLIF(feed_source, ''), source) AS source, "
                "       COALESCE(NULLIF(feed_source, ''), source) AS feed_source, origin_source, "
                "       time, date, timestamp, thumb, has_full_content, telegraph_url, summary "
                "FROM articles WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
                (since_ts, size),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM articles WHERE timestamp >= ?",
                (since_ts,),
            ).fetchone()[0]
        else:
            offset = (page - 1) * size
            base_select = (
                "SELECT id, title, COALESCE(NULLIF(feed_source, ''), source) AS source, "
                "       COALESCE(NULLIF(feed_source, ''), source) AS feed_source, origin_source, "
                "       time, date, timestamp, thumb, has_full_content, telegraph_url, summary "
                "FROM articles"
            )
            if query:
                pattern = f"%{query.lower()}%"
                where = (
                    " WHERE lower(title) LIKE ? "
                    "OR lower(COALESCE(NULLIF(feed_source, ''), source)) LIKE ? "
                    "OR lower(origin_source) LIKE ? "
                    "OR lower(summary) LIKE ?"
                )
                args = (pattern, pattern, pattern, pattern)
                rows = conn.execute(
                    f"{base_select}{where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (*args, size, offset),
                ).fetchall()
                total = conn.execute(
                    f"SELECT COUNT(*) FROM articles{where}",
                    args,
                ).fetchone()[0]
            else:
                rows = conn.execute(
                    f"{base_select} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                    (size, offset),
                ).fetchall()
                total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]

        items = [_clean_article_display_fields(dict(r)) for r in rows]
        return json.dumps({
            "items": items,
            "total": total,
            "page": page,
            "size": size,
            "diagnostics": _diagnostics(total) if total == 0 else None,
        }, ensure_ascii=False).encode()
    except Exception as e:
        return json.dumps({"error": str(e), "diagnostics": _diagnostics(None)}).encode()
    finally:
        if conn:
            conn.close()


def api_title_updates(params: dict) -> bytes:
    """GET /api/news/title-updates — lightweight title changes after cursor."""
    since = (params.get("since", [""])[0] or "").strip()
    since_ts = since
    since_id = 0
    if "|" in since:
        since_ts, since_id_text = since.rsplit("|", 1)
        try:
            since_id = int(since_id_text)
        except ValueError:
            since_id = 0
    conn = None
    try:
        conn = get_db()
        if not since:
            cursor = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')").fetchone()[0]
            return json.dumps({
                "items": [],
                "cursor": cursor,
            }, ensure_ascii=False).encode()
        rows = conn.execute(
            "SELECT id, title, title_updated_at, title_source "
            "FROM articles "
            "WHERE title_updated_at IS NOT NULL "
            "AND (title_updated_at > ? OR (title_updated_at = ? AND id > ?)) "
            "ORDER BY title_updated_at ASC, id ASC LIMIT 500",
            (since_ts, since_ts, since_id),
        ).fetchall()
        items = [dict(r) for r in rows]
        if items:
            with _article_cache_lock:
                for item in items:
                    _article_cache.pop(int(item["id"]), None)
        cursor = f"{items[-1]['title_updated_at']}|{items[-1]['id']}" if items else since
        return json.dumps({
            "items": items,
            "cursor": cursor,
        }, ensure_ascii=False).encode()
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()
    finally:
        if conn:
            conn.close()


def api_news_detail(article_id: int) -> bytes:
    """GET /api/news/<id> — single article with body_html (cached)."""
    conn = None
    try:
        conn = get_db()
        deleted = conn.execute(
            "SELECT 1 FROM deleted_articles WHERE article_id = ?",
            (article_id,),
        ).fetchone()
        if deleted:
            with _article_cache_lock:
                _article_cache.pop(article_id, None)
            return json.dumps({"error": "not found"}).encode()
        with _article_cache_lock:
            cached = _article_cache.get(article_id)
            if cached is not None:
                return cached
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if not row:
            return json.dumps({"error": "not found"}).encode()
        item = dict(row)
        item["feed_source"] = item.get("feed_source") or item.get("source") or ""
        item["origin_source"] = item.get("origin_source") or ""
        item["source"] = item["feed_source"]
        item = _clean_article_display_fields(item)
        result = json.dumps(item, ensure_ascii=False).encode()
        with _article_cache_lock:
            _article_cache[article_id] = result
        return result
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()
    finally:
        if conn:
            conn.close()


def api_sources() -> bytes:
    """GET /api/sources — source category metadata."""
    conn = None
    try:
        conn = get_db()
        rows = source_rows(conn)
        return json.dumps({
            "categories": CATEGORY_ORDER,
            "category_names": CATEGORY_NAMES,
            "sources": rows,
        }, ensure_ascii=False).encode()
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()
    finally:
        if conn:
            conn.close()


def send_json(handler, data: bytes, status=200):
    """Send a JSON response."""
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        log.warning("Client disconnected before response could be written")


def send_text(handler, text: str, status=200):
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(text.encode())))
        handler.end_headers()
        handler.wfile.write(text.encode())
    except (BrokenPipeError, ConnectionResetError):
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = urllib.parse.parse_qs(parsed.query)

        # ── API routes ──
        if path == "/api/meta":
            send_json(self, api_meta())
            return

        if path == "/api/news":
            send_json(self, api_news_list(params))
            return

        if path == "/api/news/title-updates":
            send_json(self, api_title_updates(params))
            return

        if path == "/api/sources":
            send_json(self, api_sources())
            return

        # /api/news/<id>
        m = re.match(r"^/api/news/(\d+)$", path)
        if m:
            send_json(self, api_news_detail(int(m.group(1))))
            return

        # ── Legacy routes ──
        if path == "/refresh":
            body, status = run_fetcher()
            send_json(self, body, status)
            return

        if path in ("/img-cache", "/img-proxy"):
            self._handle_img_cache(params)
            return

        send_text(self, "not found", 404)

    def _handle_img_cache(self, params):
        img_url = params.get("url", [None])[0]
        if not img_url:
            send_text(self, "Missing url parameter", 400)
            return

        parsed_url = urllib.parse.urlparse(img_url)
        if parsed_url.scheme not in ("http", "https"):
            send_text(self, "Invalid URL scheme", 400)
            return

        try:
            cached = get_cached_image(img_url)
            if not cached:
                cached = cache_image(img_url)
            if not cached:
                body, content_type = fetch_remote_image(img_url)
            else:
                path, content_type = cached
                body = path.read_bytes()

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=2592000")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            cached = get_cached_image(img_url)
            if cached:
                path, content_type = cached
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "public, max-age=2592000")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            log.warning(f"img-cache failed for {img_url[:80]}: {e}")
            send_text(self, f"Image cache error: {e}", 502)

    def log_message(self, fmt, *args):
        log.info(fmt % args)


class RayNewsThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    port = 8081
    diag = _diagnostics(None)
    log.info(
        "Startup diagnostics: data_dir=%s db_exists=%s db_size=%s telegram_configured=%s",
        diag["data_dir"],
        diag["db_exists"],
        diag["db_size"],
        diag["telegram_channel_configured"],
    )
    if diag["telegram_channel_default"]:
        log.warning("TELEGRAM_CHANNEL is not configured or still equals your_channel")
    # Start periodic refresh in background
    threading.Timer(REFRESH_INTERVAL, periodic_refresh).start()
    server = RayNewsThreadingHTTPServer(("127.0.0.1", port), Handler)
    log.info(f"Refresh + API server listening on {port} (auto-refresh every {REFRESH_INTERVAL}s)")
    server.serve_forever()
