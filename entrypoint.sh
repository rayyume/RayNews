#!/bin/bash
set -e

# ─── Crontab: write dynamically with runtime env vars ────
# Build proxy prefix for cron if env vars are set
PROXY_PREFIX=""
if [ -n "$HTTP_PROXY" ]; then
  PROXY_PREFIX="HTTP_PROXY=$HTTP_PROXY HTTPS_PROXY=$HTTPS_PROXY NO_PROXY=$NO_PROXY "
  echo "=== Proxy env injected into crontab ==="
fi

# Inject TZ if set
TZ_PREFIX=""
if [ -n "$TZ" ]; then
  TZ_PREFIX="TZ=$TZ "
  echo "=== TZ=$TZ injected into crontab ==="
fi

# Write crontab: fetch every 15 minutes
CRON_LINE="cd /app && ${PROXY_PREFIX}${TZ_PREFIX}python3 fetcher.py >> /var/log/fetcher.log 2>&1"
echo "*/15 * * * * $CRON_LINE" > /etc/cron.d/raynews
chmod 0644 /etc/cron.d/raynews
crontab /etc/cron.d/raynews

echo "=== Crontab installed ==="

# ─── Initial fetch ──────────────────────────────────────
echo "=== Running initial fetch ==="
cd /app && python fetcher.py

# ─── Start services ────────────────────────────────────
echo "=== Starting refresh server ==="
python3 /app/refresh_server.py &

echo "=== Starting nginx ==="
nginx -g 'daemon off;'
