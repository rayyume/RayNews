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


def get_db() -> sqlite3.Connection:
    """Open a read-only-ish connection to the SQLite DB."""
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # allow concurrent read while writer active
    return conn


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


# ─── API Handlers ─────────────────────────────────────────

def api_meta() -> bytes:
    """GET /api/meta — total article count."""
    try:
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        conn.close()
        return json.dumps({"count": count}).encode()
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()


def api_news_list(params: dict) -> bytes:
    """GET /api/news — paginated or incremental list (no body_html)."""
    try:
        page = int(params.get("page", ["1"])[0])
        size = int(params.get("size", ["30"])[0])
        size = min(max(size, 1), 100)
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
        conn.close()

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
    """GET /api/news/<id> — single article with body_html."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        conn.close()
        if not row:
            return json.dumps({"error": "not found"}).encode()
        return json.dumps(dict(row), ensure_ascii=False).encode()
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
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    log.info(f"Refresh + API server listening on {port} (auto-refresh every {REFRESH_INTERVAL}s)")
    server.serve_forever()
