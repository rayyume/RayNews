#!/usr/bin/env python3
"""Tiny HTTP server: runs fetcher.py on GET /refresh, plus periodic auto-refresh."""
import http.server
import subprocess
import json
import sys
import logging
import threading

REFRESH_INTERVAL = 900  # 15 minutes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [refresh] %(message)s")
log = logging.getLogger("refresh")


def run_fetcher():
    """Run fetcher.py and return the result dict."""
    try:
        log.info("Triggering fetcher...")
        result = subprocess.run(
            ["python3", "/app/fetcher.py"],
            capture_output=True, text=True, timeout=120,
        )
        body = json.dumps({
            "status": "ok",
            "stdout": result.stdout[-300:],
            "stderr": result.stderr[-300:],
        }).encode()
        log.info(f"Fetcher done (exit={result.returncode})")
        return body
    except subprocess.TimeoutExpired:
        return json.dumps({"status": "error", "error": "timeout"}).encode()
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)}).encode()


def periodic_refresh():
    """Run fetcher periodically in the background."""
    run_fetcher()
    threading.Timer(REFRESH_INTERVAL, periodic_refresh).start()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/refresh":
            body = run_fetcher()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                log.warning("Client disconnected before response could be written")
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
