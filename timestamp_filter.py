"""Prefix service output with a local, timezone-aware ISO-8601 timestamp."""

from __future__ import annotations

import re
import sys
from datetime import datetime
from typing import TextIO


_SERVICE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")

_MAX_LINE_CHARS = 8192

_TRUNCATE_MARKER = " ... [line truncated]"

# A leading ISO-8601 timestamp (compact ``..T..+08:00`` or logging-default
# ``.. ..,789``) optionally followed by a `` [tag]`` service token, so a
# self-decorated prefix can be stripped instead of duplicated.
_LEADING_PREFIX_RE = re.compile(
    r"\A"
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d+)?"
    r"(?:[+-]\d{2}:?\d{2}|Z)?"
    r"(?:[ \t]+\[[A-Za-z0-9_-]{1,32}\])?"
    r"[ \t]*"
)


def format_timestamped_line(service: str, line: str, now: datetime | None = None) -> str:
    """Return *line* prefixed with a local ISO-8601 timestamp and service name.

    A supplied aware ``now`` keeps its own offset, which makes formatting
    deterministic for callers. A supplied naive ``now`` is interpreted in the
    process's local timezone, matching ``datetime.astimezone`` semantics.
    """
    if now is None:
        timestamp = datetime.now().astimezone()
    elif now.tzinfo is None or now.utcoffset() is None:
        timestamp = now.astimezone()
    else:
        timestamp = now

    return f"{timestamp.isoformat(timespec='seconds')} [{service}] {line}"


def _strip_existing_prefix(line: str) -> str:
    """Drop a leading self-decorated timestamp (and optional ``[tag]``) if present."""
    return _LEADING_PREFIX_RE.sub("", line, count=1)


def _truncate_line(line: str) -> str:
    """Cap *line* to ``_MAX_LINE_CHARS`` chars, preserving its trailing newline."""
    if line.endswith("\n"):
        body, newline = line[:-1], "\n"
    else:
        body, newline = line, ""
    if len(body) > _MAX_LINE_CHARS:
        body = body[:_MAX_LINE_CHARS] + _TRUNCATE_MARKER
    return body + newline


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Run the streaming filter and return a process-compatible exit status."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or _SERVICE_RE.fullmatch(args[0]) is None:
        stderr.write(
            "usage: timestamp_filter.py SERVICE\n"
            "error: SERVICE must match [A-Za-z0-9_-]{1,32}\n"
        )
        return 2

    service = args[0]
    # stdin may be binary-backed; never let one bad byte kill the long-running
    # filter. Test streams may not support reconfigure.
    try:
        stdin.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    try:
        for line in stdin:
            line = _strip_existing_prefix(line)
            line = _truncate_line(line)
            stdout.write(format_timestamped_line(service, line))
            stdout.flush()
    except BrokenPipeError:
        try:
            stdout.close()
        except BrokenPipeError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())