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
echo "=== Running initial fetch ==="
cd /app && python fetcher.py || echo "[entrypoint] Initial fetch failed (non-fatal), continuing..."

# ─── Start services ────────────────────────────────────
echo "=== Starting refresh server ==="
python3 /app/refresh_server.py &

echo "=== Starting web server ==="
python3 /app/web_server.py &

echo "=== Starting nginx ==="
nginx -g 'daemon off;'
