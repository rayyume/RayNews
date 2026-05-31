#!/bin/bash
set -e

# ─── Crontab: write dynamically with runtime env vars ────
# (Removed — refresh_server handles periodic fetching via internal timer)

# ─── Initial fetch ──────────────────────────────────────
echo "=== Running initial fetch ==="
cd /app && python fetcher.py

# ─── Start services ────────────────────────────────────
echo "=== Starting refresh server ==="
python3 /app/refresh_server.py &

echo "=== Starting web server ==="
python3 /app/web_server.py &

echo "=== Starting nginx ==="
nginx -g 'daemon off;'
