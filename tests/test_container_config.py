import configparser
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "supervised_pipeline.py"


def _supervisor_config():
    # Supervisor treats whitespace-prefixed ';' and '#' as inline comments.
    # Keep that behavior here so nginx's `daemon off;` argument is verified
    # after INI parsing, not only as raw source text.
    config = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    config.read(ROOT / "supervisord.conf", encoding="utf-8")
    return config


def _injected_wrapper_command(filter_command, producer_command, *, grace=0.1):
    driver = (
        "import json, sys; "
        "from supervised_pipeline import main; "
        "raise SystemExit(main(sys.argv[2:], "
        "filter_command=json.loads(sys.argv[1]), "
        f"term_grace={grace!r}))"
    )
    return [
        sys.executable,
        "-u",
        "-c",
        driver,
        json.dumps([str(part) for part in filter_command]),
        "test",
        "--",
        *(str(part) for part in producer_command),
    ]


def _wait_for_path(path, process, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise AssertionError(
                f"wrapper exited before {path.name} appeared: {process.returncode}"
            )
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


def _pid_is_running(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
    except FileNotFoundError:
        return False
    return state != "Z"


def _wait_until_not_running(pid, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return
        time.sleep(0.01)
    raise AssertionError(f"pid {pid} is still running")


def test_supervised_service_commands_exec_pipeline_wrapper_with_exact_argv():
    config = _supervisor_config()
    expected_producers = {
        "program:refresh": ["python3", "-u", "/app/refresh_server.py"],
        "program:web": ["python3", "-u", "/app/web_server.py"],
        "program:nginx": ["nginx", "-g", "daemon off;"],
    }

    for section, producer in expected_producers.items():
        service = section.removeprefix("program:")
        command = config[section]["command"]
        outer_argv = shlex.split(command)
        assert outer_argv[:4] == ["/bin/bash", "-o", "pipefail", "-c"]
        assert len(outer_argv) == 5
        assert shlex.split(outer_argv[4]) == [
            "exec",
            "python3",
            "-u",
            "/app/supervised_pipeline.py",
            service,
            "--",
            *producer,
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


def test_filter_failure_terminates_and_reaps_idle_term_ignoring_producer(tmp_path):
    producer_pid = tmp_path / "producer.pid"
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    failing_filter = tmp_path / "failing_filter.py"
    failing_filter.write_text(
        "import pathlib, sys, time\n"
        "pid_path = pathlib.Path(sys.argv[1])\n"
        "while not pid_path.exists(): time.sleep(0.01)\n"
        "raise SystemExit(23)\n",
        encoding="utf-8",
    )

    started = time.monotonic()
    result = subprocess.run(
        _injected_wrapper_command(
            [sys.executable, "-u", failing_filter, producer_pid],
            [sys.executable, "-u", producer, producer_pid],
        ),
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    pid = int(producer_pid.read_text(encoding="utf-8"))
    assert result.returncode == 23
    assert time.monotonic() - started < 2
    assert not _pid_is_running(pid)


def test_wrapper_stays_alive_with_term_ignoring_members_until_group_kill(tmp_path):
    producer_pid = tmp_path / "producer.pid"
    filter_pid = tmp_path / "filter.pid"
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    blocking_filter = tmp_path / "blocking_filter.py"
    blocking_filter.write_text(
        "import os, pathlib, signal, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "sys.stdin.buffer.read()\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        _injected_wrapper_command(
            [sys.executable, "-u", blocking_filter, filter_pid],
            [sys.executable, "-u", producer, producer_pid],
        ),
        cwd=ROOT,
        start_new_session=True,
    )

    producer_process_id = filter_process_id = None
    try:
        _wait_for_path(producer_pid, process)
        _wait_for_path(filter_pid, process)
        producer_process_id = int(producer_pid.read_text(encoding="utf-8"))
        filter_process_id = int(filter_pid.read_text(encoding="utf-8"))

        process.send_signal(signal.SIGTERM)
        time.sleep(0.2)

        assert process.poll() is None
        assert _pid_is_running(producer_process_id)
        assert _pid_is_running(filter_process_id)

        os.killpg(process.pid, signal.SIGKILL)
        assert process.wait(timeout=3) == -signal.SIGKILL
        _wait_until_not_running(producer_process_id)
        _wait_until_not_running(filter_process_id)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        for pid in (producer_process_id, filter_process_id):
            if pid is not None and _pid_is_running(pid):
                os.kill(pid, signal.SIGKILL)


def test_wrapper_does_not_kill_remaining_member_after_peer_exits_on_term(tmp_path):
    producer_pid = tmp_path / "producer.pid"
    filter_pid = tmp_path / "filter.pid"
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    cooperative_filter = tmp_path / "cooperative_filter.py"
    cooperative_filter.write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "sys.stdin.buffer.read()\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        _injected_wrapper_command(
            [sys.executable, "-u", cooperative_filter, filter_pid],
            [sys.executable, "-u", producer, producer_pid],
        ),
        cwd=ROOT,
        start_new_session=True,
    )

    producer_process_id = None
    try:
        _wait_for_path(producer_pid, process)
        _wait_for_path(filter_pid, process)
        producer_process_id = int(producer_pid.read_text(encoding="utf-8"))

        process.send_signal(signal.SIGTERM)
        time.sleep(0.3)

        assert process.poll() is None
        assert _pid_is_running(producer_process_id)

        os.killpg(process.pid, signal.SIGKILL)
        assert process.wait(timeout=3) == -signal.SIGKILL
        _wait_until_not_running(producer_process_id)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        if producer_process_id is not None and _pid_is_running(producer_process_id):
            os.kill(producer_process_id, signal.SIGKILL)


def test_producer_nonzero_exit_wins_after_stdout_and_stderr_are_drained():
    producer = (
        "import sys; "
        "print('stdout line', flush=True); "
        "print('stderr line', file=sys.stderr, flush=True); "
        "raise SystemExit(7)"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-u",
            WRAPPER,
            "web",
            "--",
            sys.executable,
            "-u",
            "-c",
            producer,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 7
    assert "[web] stdout line\n" in result.stdout
    assert "[web] stderr line\n" in result.stdout


def test_producer_receives_nginx_foreground_argument_as_one_argv_value():
    producer = "import json, sys; print(json.dumps(sys.argv[1:]), flush=True)"
    result = subprocess.run(
        [
            sys.executable,
            "-u",
            WRAPPER,
            "nginx",
            "--",
            sys.executable,
            "-u",
            "-c",
            producer,
            "-g",
            "daemon off;",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 0
    assert '[nginx] ["-g", "daemon off;"]\n' in result.stdout


def test_wrapper_returns_sigterm_after_reaping_cooperative_members(tmp_path):
    producer_pid = tmp_path / "producer.pid"
    producer = tmp_path / "producer.py"
    producer.write_text(
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-u",
            WRAPPER,
            "web",
            "--",
            sys.executable,
            "-u",
            producer,
            producer_pid,
        ],
        cwd=ROOT,
    )
    try:
        _wait_for_path(producer_pid, process)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=3) == -signal.SIGTERM
        _wait_until_not_running(int(producer_pid.read_text(encoding="utf-8")))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_container_image_copies_pipeline_wrapper():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "supervised_pipeline.py" in dockerfile
