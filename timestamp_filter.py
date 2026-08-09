"""Prefix service output with a local, timezone-aware ISO-8601 timestamp."""

from __future__ import annotations

import re
import sys
from datetime import datetime
from typing import TextIO


_SERVICE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")


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
    try:
        for line in stdin:
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
