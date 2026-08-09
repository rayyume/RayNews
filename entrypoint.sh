#!/bin/bash

log() {
  printf '%s [entrypoint] %s\n' "$(date --iso-8601=seconds)" "$*"
}

log "=== Injecting custom HTML ==="
inject_output_file=""
if ! inject_output_file=$(mktemp /tmp/raynews-inject.XXXXXX 2>/dev/null); then
  log "ERROR: unable to allocate temporary output for custom HTML injection." >&2
else
  injector_status=0
  python3 - <<'PY' >"$inject_output_file" 2>&1 || injector_status=$?
import os
import sys
from pathlib import Path

path = Path(
    os.environ.get(
        "RAYNEWS_INJECT_HTML_PATH",
        "/usr/share/nginx/html/index.html",
    )
)
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
    print(f"Custom HTML injection failed: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
  if [ "$injector_status" -ne 0 ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      log "[inject] $line" >&2
    done < "$inject_output_file"
    log "ERROR: custom HTML injection failed (exit status $injector_status)." >&2
  fi
  if ! rm -f -- "$inject_output_file" 2>/dev/null; then
    log "WARNING: unable to remove custom HTML injection output file." >&2
  fi
fi

# Prepare the bind-mounted data directory before supervisor drops the Python
# services to the fixed application account. Never fall back to running them as
# root when the host mount cannot be repaired.
if ! install -d -o raynews -g raynews /app/data; then
  log "ERROR: unable to create or set ownership on /app/data for raynews." >&2
  exit 1
fi
if ! chown -R raynews:raynews /app/data; then
  log "ERROR: unable to grant raynews ownership of /app/data." >&2
  exit 1
fi
if ! /usr/sbin/runuser -u raynews -- /bin/sh -c '
  probe=$(/usr/bin/mktemp /app/data/.raynews-write-probe.XXXXXX) || exit 1
  /bin/rm -f -- "$probe"
'; then
  log "ERROR: /app/data is not writable by raynews after permission setup." >&2
  exit 1
fi

# ─── Configuration warning ─────────────────────────────
if [ -z "$TELEGRAM_CHANNEL_URL" ] && { [ -z "$TELEGRAM_CHANNEL" ] || [ "$TELEGRAM_CHANNEL" = "your_channel" ]; }; then
  log "WARNING: TELEGRAM_CHANNEL_URL/TELEGRAM_CHANNEL is not configured; fetcher will not read the intended Telegram source."
fi

# ─── Start services ────────────────────────────────────
exec supervisord -c /app/supervisord.conf
