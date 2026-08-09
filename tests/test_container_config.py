import configparser
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _supervisor_config():
    # Supervisor treats whitespace-prefixed ';' and '#' as inline comments.
    # Keep that behavior here so nginx's `daemon off;` argument is verified
    # after INI parsing, not only as raw source text.
    config = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    config.read(ROOT / "supervisord.conf", encoding="utf-8")
    return config


def test_supervised_service_commands_use_timestamp_filter_with_pipefail():
    config = _supervisor_config()
    expected_pipelines = {
        "program:refresh": (
            "python3 -u /app/refresh_server.py 2>&1 | "
            "python3 -u /app/timestamp_filter.py refresh"
        ),
        "program:web": (
            "python3 -u /app/web_server.py 2>&1 | "
            "python3 -u /app/timestamp_filter.py web"
        ),
        "program:nginx": (
            'nginx -g "daemon off;" 2>&1 | '
            "python3 -u /app/timestamp_filter.py nginx"
        ),
    }

    for section, pipeline in expected_pipelines.items():
        command = config[section]["command"]
        assert shlex.split(command) == [
            "/bin/bash",
            "-o",
            "pipefail",
            "-c",
            pipeline,
        ]


def test_supervised_service_process_groups_and_container_fds_are_preserved():
    config = _supervisor_config()

    for section in ("program:refresh", "program:web", "program:nginx"):
        program = config[section]
        assert program["stopasgroup"] == "true"
        assert program["killasgroup"] == "true"
        assert program["stdout_logfile"] == "/dev/fd/1"
        assert program["stdout_logfile_maxbytes"] == "0"
        assert program["stderr_logfile"] == "/dev/fd/2"
        assert program["stderr_logfile_maxbytes"] == "0"


def test_pipefail_propagates_a_left_hand_service_exit_code():
    result = subprocess.run(
        [
            "/bin/bash",
            "-o",
            "pipefail",
            "-c",
            "/bin/bash -c 'exit 7' 2>&1 | /bin/cat",
        ],
        check=False,
    )

    assert result.returncode == 7


def test_pipefail_propagates_invalid_timestamp_filter_service():
    filter_command = " ".join(
        shlex.quote(part)
        for part in (
            sys.executable,
            "-u",
            str(ROOT / "timestamp_filter.py"),
            "invalid service",
        )
    )
    result = subprocess.run(
        [
            "/bin/bash",
            "-o",
            "pipefail",
            "-c",
            f"printf 'hello\\n' | {filter_command}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0


def test_supervised_pipeline_can_be_terminated_as_a_process_group():
    process = subprocess.Popen(
        [
            "/bin/bash",
            "-o",
            "pipefail",
            "-c",
            "/bin/sleep 60 2>&1 | /bin/cat",
        ],
        start_new_session=True,
    )
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)

    assert process.returncode != 0
