#!/bin/bash

echo "=== Injecting custom head HTML ==="
python3 - <<'PY'
import os
from pathlib import Path

path = Path("/usr/share/nginx/html/index.html")
placeholder = "<!-- {{CUSTOM_HEAD_HTML}} -->"
custom_head = os.environ.get("CUSTOM_HEAD_HTML", "").replace("\\n", "\n")

try:
    html = path.read_text(encoding="utf-8")
    html = html.replace(placeholder, custom_head)
    path.write_text(html, encoding="utf-8")
except Exception as exc:
    print(f"[entrypoint] Custom head injection failed: {exc}")
PY

# ─── Initial fetch (non-fatal — don't let this block startup) ──
if [ -z "$TELEGRAM_CHANNEL" ] || [ "$TELEGRAM_CHANNEL" = "your_channel" ]; then
  echo "[entrypoint] WARNING: TELEGRAM_CHANNEL is not configured; fetcher will not read the intended Telegram source."
fi

echo "=== Running initial fetch ==="
cd /app && python fetcher.py || echo "[entrypoint] Initial fetch failed (non-fatal), continuing..."

python3 - <<'PY'
import os
import sqlite3

p = os.path.join(os.environ.get("DATA_DIR", "/app/data"), "news.db")
try:
    exists = os.path.exists(p)
    size = os.path.getsize(p) if exists else 0
    count = None
    if exists:
        conn = sqlite3.connect(p)
        count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        conn.close()
    print(f"[entrypoint] news.db exists={exists} size={size} article_count={count}")
except Exception as exc:
    print(f"[entrypoint] news.db check failed: {exc}")
PY

# ─── Start services ────────────────────────────────────
echo "=== Starting refresh server ==="
python3 /app/refresh_server.py &

echo "=== Starting web server ==="
python3 /app/web_server.py &

echo "=== Starting nginx ==="
nginx -g 'daemon off;'
