#!/bin/bash
set -e

# Write proxy env to /etc/environment so cron inherits them
if [ -n "$HTTP_PROXY" ]; then
  > /etc/environment
  echo "HTTP_PROXY=$HTTP_PROXY" >> /etc/environment
  echo "HTTPS_PROXY=$HTTPS_PROXY" >> /etc/environment
  echo "NO_PROXY=$NO_PROXY" >> /etc/environment
  echo "=== Proxy env written to /etc/environment for cron ==="
fi

# Run fetcher once on startup
echo "=== Running initial fetch ==="
cd /app && python fetcher.py

# Start cron daemon
echo "=== Starting cron ==="
cron

# Start refresh server (on-demand fetcher trigger)
echo "=== Starting refresh server ==="
python3 /app/refresh_server.py &

# Start nginx in foreground
echo "=== Starting nginx ==="
nginx -g 'daemon off;'
