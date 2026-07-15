#!/bin/bash

echo "=== Injecting custom HTML ==="
python3 - <<'PY'
import os
from pathlib import Path

path = Path("/usr/share/nginx/html/index.html")
placeholder = "<!-- {{CUSTOM_HEAD_HTML}} -->"
custom_head = os.environ.get("CUSTOM_HEAD_HTML", "").replace("\\n", "\n")
footer_start = "<!-- {{CUSTOM_FOOTER_HTML_START}} -->"
footer_end = "<!-- {{CUSTOM_FOOTER_HTML_END}} -->"
custom_footer = os.environ.get("CUSTOM_FOOTER_HTML", "").replace("\\n", "\n")

try:
    html = path.read_text(encoding="utf-8")
    html = html.replace(placeholder, custom_head)
    if custom_footer and footer_start in html and footer_end in html:
        before, rest = html.split(footer_start, 1)
        _, after = rest.split(footer_end, 1)
        html = before + custom_footer + after
    else:
        html = html.replace(footer_start, "").replace(footer_end, "")
    path.write_text(html, encoding="utf-8")
except Exception as exc:
    print(f"[entrypoint] Custom HTML injection failed: {exc}")
PY

# ─── Configuration warning ─────────────────────────────
if [ -z "$TELEGRAM_CHANNEL_URL" ] && { [ -z "$TELEGRAM_CHANNEL" ] || [ "$TELEGRAM_CHANNEL" = "your_channel" ]; }; then
  echo "[entrypoint] WARNING: TELEGRAM_CHANNEL_URL/TELEGRAM_CHANNEL is not configured; fetcher will not read the intended Telegram source."
fi

# ─── Start services ────────────────────────────────────
echo "=== Starting refresh server ==="
python3 /app/refresh_server.py &

echo "=== Starting web server ==="
python3 /app/web_server.py &

echo "=== Starting nginx ==="
nginx -g 'daemon off;'
