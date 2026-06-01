#!/bin/bash

# ─── Initial fetch (non-fatal — don't let this block startup) ──
echo "=== Running initial fetch ==="
cd /app && python fetcher.py || echo "[entrypoint] Initial fetch failed (non-fatal), continuing..."

# ─── Start services ────────────────────────────────────
echo "=== Starting refresh server ==="
python3 /app/refresh_server.py &

echo "=== Starting web server ==="
python3 /app/web_server.py &

echo "=== Starting nginx ==="
nginx -g 'daemon off;'
