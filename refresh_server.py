#!/usr/bin/env python3
"""Tiny HTTP server: runs fetcher.py on GET /refresh, returns JSON."""
import http.server
import subprocess
import json
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [refresh] %(message)s")
log = logging.getLogger("refresh")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/refresh":
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
            except subprocess.TimeoutExpired:
                body = json.dumps({"status": "error", "error": "timeout"}).encode()
            except Exception as e:
                body = json.dumps({"status": "error", "error": str(e)}).encode()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        log.info(fmt % args)


if __name__ == "__main__":
    port = 8081
    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    log.info(f"Refresh server listening on {port}")
    server.serve_forever()
