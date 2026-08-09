import io
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from timestamp_filter import format_timestamped_line, main


ROOT = Path(__file__).resolve().parents[1]


def test_formats_iso_timestamp_with_aware_offset():
    now = datetime(2026, 8, 9, 20, 15, 32, tzinfo=timezone(timedelta(hours=8)))

    assert format_timestamped_line("web", "hello\n", now=now) == (
        "2026-08-09T20:15:32+08:00 [web] hello\n"
    )


def test_normalizes_naive_supplied_time_to_local_timezone():
    naive_now = datetime(2026, 8, 9, 20, 15, 32)
    expected_timestamp = naive_now.astimezone().isoformat(timespec="seconds")

    assert format_timestamped_line("web", "hello", now=naive_now) == (
        f"{expected_timestamp} [web] hello"
    )


def test_cli_prefixes_every_line_and_keeps_final_line_unterminated():
    result = subprocess.run(
        [sys.executable, "timestamp_filter.py", "web"],
        input="Traceback:\n  帧\nValueError: x",
        text=True,
        capture_output=True,
        cwd=ROOT,
        env={**os.environ, "TZ": "Asia/Shanghai"},
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout.endswith("ValueError: x")
    assert not result.stdout.endswith("\n")
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    assert all(
        re.match(
            r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+08:00 \[web\] ", line
        )
        for line in lines
    )
    assert lines[1].endswith("帧")


@pytest.mark.parametrize("service", ["", "web service", "web!", "x" * 33])
def test_cli_rejects_invalid_service_names(service):
    result = subprocess.run(
        [sys.executable, "timestamp_filter.py", service],
        input="hello\n",
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "service" in result.stderr.lower()


class BrokenPipeWriter(io.StringIO):
    def write(self, _text):
        raise BrokenPipeError


def test_main_silently_stops_on_broken_pipe():
    assert main(["web"], stdin=io.StringIO("hello\n"), stdout=BrokenPipeWriter()) == 0


def test_dockerfile_installs_timezone_data_and_copies_filter():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "DEBIAN_FRONTEND=noninteractive" in dockerfile
    assert "tzdata" in dockerfile
    assert "ENV TZ=Asia/Shanghai" in dockerfile
    assert "timestamp_filter.py" in dockerfile
