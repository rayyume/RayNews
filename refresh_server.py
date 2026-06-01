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
from pathlib import Path

import requests

REFRESH_INTERVAL = 900  # 15 minutes
LOCK_FILE = "/tmp/raynews-fetcher.lock"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_FILE = DATA_DIR / "news.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [refresh] %(message)s")
log = logging.getLogger("refresh")

# Persistent SQLite connection — avoid connect overhead per request
_db_conn = None


def get_db() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(str(DB_FILE))
        _db_conn.row_factory = sqlite3.Row
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")
    return _db_conn


# In-memory cache for article detail responses — invalidated on fetcher run
_article_cache: dict[int, bytes] = {}
# Track last daily summary send date per user (avoid double-send)
_last_summary_date: dict[int, str] = {}


def clear_article_cache():
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
        log.info(f"Fetcher done (exit={result.returncode})")
        if is_ok:
            clear_article_cache()
        return body, 200 if is_ok else 500
    except subprocess.TimeoutExpired:
        body = json.dumps({"status": "error", "error": "timeout"}).encode()
        return body, 500
    except Exception as e:
        body = json.dumps({"status": "error", "error": str(e)}).encode()
        return body, 500
    finally:
        release_lock()


def periodic_refresh():
    """Run fetcher periodically in the background."""
    body, _ = run_fetcher()
    threading.Timer(REFRESH_INTERVAL, periodic_refresh).start()


def check_daily_summary():
    """Check every 60s if any user has a daily summary due at this hour:minute.

    Runs in background, fires a separate thread per matched user to avoid
    blocking the check loop on slow AI calls.
    """
    import json as _json
    import datetime as _dt

    now = _dt.datetime.now()
    now_hhmm = now.strftime("%H:%M")
    today_str = now.strftime("%Y-%m-%d")

    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT user_id, notification_config, daily_summary_enabled "
            "FROM user_settings WHERE daily_summary_enabled = 1"
        ).fetchall()
    except Exception:
        threading.Timer(60, check_daily_summary).start()
        return

    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        threading.Timer(60, check_daily_summary).start()
        return

    for row in rows:
        settings = dict(row)
        uid = settings["user_id"]
        nc_raw = settings.get("notification_config", "{}")
        if isinstance(nc_raw, str):
            try:
                nc = _json.loads(nc_raw)
            except (_json.JSONDecodeError, TypeError):
                nc = {}
        else:
            nc = nc_raw

        resend_cfg = nc.get("resend", {})
        to_email = resend_cfg.get("to_email", "")
        scheduled_time = resend_cfg.get("daily_summary_time", "08:00")

        if not to_email:
            continue
        if scheduled_time != now_hhmm:
            continue
        # Already sent today?
        if _last_summary_date.get(uid) == today_str:
            continue

        _last_summary_date[uid] = today_str
        # Fire summary generation in a separate thread so the 60s loop is not blocked
        threading.Thread(
            target=_send_daily_summary_for_user,
            args=(uid, to_email, resend_api_key),
            daemon=True,
        ).start()

    threading.Timer(60, check_daily_summary).start()


def _send_daily_summary_for_user(uid: int, to_email: str, resend_api_key: str):
    """Generate and email a daily summary for a single user."""
    import json as _json

    log.info(f"Generating daily summary for user {uid} → {to_email}")

    # Load articles
    try:
        conn = get_db()
        articles = conn.execute(
            "SELECT id, title, source, date, time FROM articles "
            "ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        if not articles:
            log.warning(f"Daily summary for user {uid}: no articles")
            return
        article_list = [dict(r) for r in articles]
    except Exception as e:
        log.error(f"Daily summary for user {uid}: db error {e}")
        return

    # Load user's AI config
    try:
        conn = get_db()
        ai_row = conn.execute(
            "SELECT endpoint, model, api_key, provider_type, enabled "
            "FROM ai_config WHERE user_id = ?", (uid,)
        ).fetchone()
        if not ai_row or not ai_row["enabled"] or not ai_row["api_key"]:
            log.warning(f"Daily summary for user {uid}: no AI config")
            return
        ai_config = dict(ai_row)
    except Exception as e:
        log.error(f"Daily summary for user {uid}: ai config error {e}")
        return

    try:
        from ai_service import AIService
        svc = AIService(
            api_key=ai_config["api_key"],
            endpoint=ai_config["endpoint"],
            model=ai_config["model"],
            provider_type=ai_config.get("provider_type", "openai"),
        )
        summary = svc.daily_summary(article_list)

        from notifier import send_daily_summary_email
        result = send_daily_summary_email(
            resend_api_key, to_email, summary, len(article_list)
        )
        log.info(f"Daily summary sent to {to_email} — id={result.get('id', '?')}")
    except Exception as e:
        log.error(f"Daily summary for user {uid}: send failed {e}")


# ─── API Handlers ─────────────────────────────────────────

def api_meta() -> bytes:
    """GET /api/meta — total article count."""
    try:
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        return json.dumps({"count": count}).encode()
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()


def api_news_list(params: dict) -> bytes:
    """GET /api/news — paginated or incremental list (no body_html)."""
    try:
        page = int(params.get("page", ["1"])[0])
        size = int(params.get("size", ["30"])[0])
        size = min(max(size, 1), 2000)
        since = params.get("since", [None])[0]
    except (ValueError, IndexError):
        return json.dumps({"error": "invalid params"}).encode()

    try:
        conn = get_db()
        if since:
            since_ts = int(since)
            rows = conn.execute(
                "SELECT id, title, source, time, date, timestamp, thumb, has_full_content, telegraph_url, summary "
                "FROM articles WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
                (since_ts, size),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM articles WHERE timestamp >= ?",
                (since_ts,),
            ).fetchone()[0]
        else:
            offset = (page - 1) * size
            rows = conn.execute(
                "SELECT id, title, source, time, date, timestamp, thumb, has_full_content, telegraph_url, summary "
                "FROM articles ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (size, offset),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]

        items = [dict(r) for r in rows]
        return json.dumps({
            "items": items,
            "total": total,
            "page": page,
            "size": size,
        }, ensure_ascii=False).encode()
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()


def api_news_detail(article_id: int) -> bytes:
    """GET /api/news/<id> — single article with body_html (cached)."""
    cached = _article_cache.get(article_id)
    if cached is not None:
        return cached
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if not row:
            return json.dumps({"error": "not found"}).encode()
        result = json.dumps(dict(row), ensure_ascii=False).encode()
        _article_cache[article_id] = result
        return result
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()


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

        if path == "/img-proxy":
            self._handle_img_proxy(params)
            return

        send_text(self, "not found", 404)

    def _handle_img_proxy(self, params):
        img_url = params.get("url", [None])[0]
        if not img_url:
            send_text(self, "Missing url parameter", 400)
            return

        parsed_url = urllib.parse.urlparse(img_url)
        if parsed_url.scheme not in ("http", "https"):
            send_text(self, "Invalid URL scheme", 400)
            return

        try:
            proxy_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": f"{parsed_url.scheme}://{parsed_url.netloc}/",
            }
            resp = requests.get(img_url, headers=proxy_headers, timeout=15, stream=True)
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "application/octet-stream")
            body = resp.content

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            log.warning(f"img-proxy failed for {img_url[:80]}: {e}")
            send_text(self, f"Proxy error: {e}", 502)

    def log_message(self, fmt, *args):
        log.info(fmt % args)


if __name__ == "__main__":
    port = 8081
    # Start periodic refresh in background
    threading.Timer(REFRESH_INTERVAL, periodic_refresh).start()
    # Start daily summary checker (runs every 60s)
    threading.Timer(60, check_daily_summary).start()
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    log.info(f"Refresh + API server listening on {port} (auto-refresh every {REFRESH_INTERVAL}s)")
    server.serve_forever()
