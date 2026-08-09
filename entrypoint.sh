#!/bin/bash

log() {
  printf '%s [entrypoint] %s\n' "$(date --iso-8601=seconds)" "$*"
}

# Drain arbitrary command output while retaining only a small, line-oriented
# diagnostic sample. This prevents a failing setup command from filling /tmp
# or being expanded into an unbounded shell variable.
capture_setup_output() {
  python3 -c '
import sys

max_lines = 20
max_line_bytes = 2048
line = bytearray()
line_truncated = False
line_count = 0
lines_omitted = False
output = sys.stdout.buffer


def append(fragment):
    global line_truncated
    remaining = max_line_bytes - len(line)
    if remaining > 0:
        line.extend(fragment[:remaining])
    if len(fragment) > max(remaining, 0):
        line_truncated = True


def finish_line():
    global line, line_truncated, line_count, lines_omitted
    line_count += 1
    if line_count <= max_lines:
        output.write(line)
        if line_truncated:
            output.write(b"... [line truncated]")
        output.write(b"\n")
    else:
        lines_omitted = True
    line = bytearray()
    line_truncated = False


while True:
    chunk = sys.stdin.buffer.read(65536)
    if not chunk:
        break
    start = 0
    while True:
        newline = chunk.find(b"\n", start)
        if newline < 0:
            append(chunk[start:])
            break
        append(chunk[start:newline])
        finish_line()
        start = newline + 1

if line or line_truncated:
    finish_line()
if lines_omitted:
    output.write(b"diagnostic output truncated after 20 lines\n")
' 2>/dev/null
}

run_setup_command() {
  local setup_name="$1"
  shift
  local output_file=""
  local command_status=0
  local capture_status=0
  local final_status=0
  local line=""
  local -a pipeline_status

  if ! output_file=$(/usr/bin/mktemp /tmp/raynews-setup.XXXXXX 2>/dev/null); then
    log "ERROR: unable to allocate diagnostic output for setup command '$setup_name'." >&2
    return 1
  fi

  "$@" 2>&1 | capture_setup_output 2>/dev/null >"$output_file"
  pipeline_status=("${PIPESTATUS[@]}")
  command_status="${pipeline_status[0]}"
  capture_status="${pipeline_status[1]}"

  if [ "$command_status" -ne 0 ] || [ "$capture_status" -ne 0 ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      log "[setup:$setup_name] $line" >&2
    done < "$output_file"
  fi
  if [ "$capture_status" -ne 0 ]; then
    log "ERROR: unable to capture diagnostic output for setup command '$setup_name'." >&2
  fi
  if ! /bin/rm -f -- "$output_file" 2>/dev/null; then
    log "WARNING: unable to remove diagnostic output for setup command '$setup_name'." >&2
  fi

  final_status="$command_status"
  if [ "$final_status" -eq 0 ] && [ "$capture_status" -ne 0 ]; then
    final_status="$capture_status"
  fi
  return "$final_status"
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
if ! run_setup_command install install -d -o raynews -g raynews /app/data; then
  log "ERROR: unable to create or set ownership on /app/data for raynews." >&2
  exit 1
fi
if ! run_setup_command chown chown -R raynews:raynews /app/data; then
  log "ERROR: unable to grant raynews ownership of /app/data." >&2
  exit 1
fi
if ! run_setup_command runuser /usr/sbin/runuser -u raynews -- /bin/sh -c '
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
