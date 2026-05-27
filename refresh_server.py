#!/usr/bin/env python3
"""Tiny HTTP server: runs fetcher.py on GET /refresh, plus periodic auto-refresh."""
import http.server
import subprocess
import json
import sys
import logging
import threading
import urllib.parse
import os
import tempfile

import requests

REFRESH_INTERVAL = 900  # 15 minutes

LOCK_FILE = "/tmp/raynews-fetcher.lock"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [refresh] %(message)s")
log = logging.getLogger("refresh")


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


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/refresh":
            body, status = run_fetcher()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                log.warning("Client disconnected before response could be written")

        elif parsed.path == "/img-proxy":
            params = urllib.parse.parse_qs(parsed.query)
            img_url = params.get("url", [None])[0]
            if not img_url:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Missing url parameter")
                return

            parsed_url = urllib.parse.urlparse(img_url)
            if parsed_url.scheme not in ("http", "https"):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid URL scheme")
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
                self.send_response(502)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(f"Proxy error: {e}".encode())

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        log.info(fmt % args)


if __name__ == "__main__":
    port = 8081
    # Start periodic refresh in background
    threading.Timer(REFRESH_INTERVAL, periodic_refresh).start()
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    log.info(f"Refresh server listening on {port} (auto-refresh every {REFRESH_INTERVAL}s)")
    server.serve_forever()
