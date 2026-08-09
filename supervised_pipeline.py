"""Run a supervised producer through the timestamp filter without orphaning it."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import TextIO


_POLL_INTERVAL_SECONDS = 0.02
_DEFAULT_TERM_GRACE_SECONDS = 1.0
_FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _signal_process(process: subprocess.Popen[bytes], signum: int) -> None:
    try:
        os.kill(process.pid, signum)
    except ProcessLookupError:
        pass


def _terminate_and_reap(
    process: subprocess.Popen[bytes], term_grace: float
) -> int:
    returncode = process.poll()
    if returncode is not None:
        return returncode

    _signal_process(process, signal.SIGTERM)
    try:
        return process.wait(timeout=term_grace)
    except subprocess.TimeoutExpired:
        _signal_process(process, signal.SIGKILL)
        return process.wait()


def _finish_with_forwarded_signal(signum: int | None, returncode: int) -> int:
    if signum is None:
        return returncode

    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
    return 128 + signum  # pragma: no cover - self-signal normally cannot return


def run_pipeline(
    service: str,
    producer_command: Sequence[str],
    *,
    filter_command: Sequence[str] | None = None,
    term_grace: float = _DEFAULT_TERM_GRACE_SECONDS,
) -> int:
    """Run *producer_command* through the service filter and return its status.

    The wrapper remains the Supervisor-visible process leader. Its children
    inherit its process group, while explicit signal forwarding and waiting
    keep them owned until they exit or Supervisor kills the whole group.
    """
    if not producer_command:
        raise ValueError("producer command must not be empty")
    if term_grace < 0:
        raise ValueError("term grace must not be negative")

    if filter_command is None:
        filter_command = [
            sys.executable,
            "-u",
            str(Path(__file__).with_name("timestamp_filter.py")),
        ]
    complete_filter_command = [*filter_command, service]

    producer: subprocess.Popen[bytes] | None = None
    filter_process: subprocess.Popen[bytes] | None = None
    received_signal: int | None = None
    previous_handlers: dict[int, signal.Handlers] = {}

    def forward_signal(signum: int, _frame: object) -> None:
        nonlocal received_signal
        if received_signal is None:
            received_signal = signum
        if producer is not None:
            _signal_process(producer, signum)
        if filter_process is not None:
            _signal_process(filter_process, signum)

    for signum in _FORWARDED_SIGNALS:
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, forward_signal)

    try:
        producer = subprocess.Popen(
            [str(part) for part in producer_command],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if received_signal is not None:
            _signal_process(producer, received_signal)

        assert producer.stdout is not None
        try:
            filter_process = subprocess.Popen(
                [str(part) for part in complete_filter_command],
                stdin=producer.stdout,
            )
        except BaseException:
            producer.stdout.close()
            _terminate_and_reap(producer, term_grace)
            raise
        producer.stdout.close()
        if received_signal is not None:
            _signal_process(filter_process, received_signal)

        while True:
            producer_returncode = producer.poll()
            filter_returncode = filter_process.poll()

            if producer_returncode is not None:
                if filter_returncode is None:
                    time.sleep(_POLL_INTERVAL_SECONDS)
                    continue
                result = (
                    producer_returncode
                    if producer_returncode != 0
                    else filter_returncode
                )
                return _finish_with_forwarded_signal(received_signal, result)

            if filter_returncode is not None:
                if received_signal is not None:
                    # During Supervisor shutdown, a peer exiting on the
                    # forwarded signal must not trigger our fail-fast grace
                    # timer. Keep owning any TERM-ignoring member so
                    # Supervisor can reach its process-group SIGKILL path.
                    time.sleep(_POLL_INTERVAL_SECONDS)
                    continue
                _terminate_and_reap(producer, term_grace)
                return _finish_with_forwarded_signal(
                    received_signal, filter_returncode
                )

            time.sleep(_POLL_INTERVAL_SECONDS)
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)


def main(
    argv: list[str] | None = None,
    *,
    filter_command: Sequence[str] | None = None,
    term_grace: float = _DEFAULT_TERM_GRACE_SECONDS,
    stderr: TextIO = sys.stderr,
) -> int:
    """Parse the wrapper CLI and return a process-compatible exit status."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--" or not args[0]:
        stderr.write(
            "usage: supervised_pipeline.py SERVICE -- PRODUCER [ARG ...]\n"
        )
        return 2

    service = args[0]
    producer_command = args[2:]
    try:
        return run_pipeline(
            service,
            producer_command,
            filter_command=filter_command,
            term_grace=term_grace,
        )
    except (OSError, ValueError) as exc:
        stderr.write(f"supervised_pipeline.py: unable to start pipeline: {exc}\n")
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
