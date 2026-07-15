import json
import threading
from pathlib import Path

import pytest

import refresh_server


def reset_job(monkeypatch):
    monkeypatch.setattr(refresh_server, "REFRESH_JOB", {
        "job_id": "", "status": "idle", "trigger": "",
        "started_at": None, "finished_at": None,
        "new_count": 0, "error": "",
    })


def wait_terminal():
    for _ in range(100):
        payload = json.loads(refresh_server.get_refresh_job_status())
        if payload["status"] != "running":
            return payload
        threading.Event().wait(0.01)
    raise AssertionError("refresh job did not finish")


def test_start_refresh_job_returns_before_worker_finishes(monkeypatch):
    reset_job(monkeypatch)
    release = threading.Event()
    entered = threading.Event()

    def slow_fetcher():
        entered.set()
        release.wait(2)
        return json.dumps({"status": "ok"}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", slow_fetcher)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())

    body, status = refresh_server.start_refresh_job("manual")
    payload = json.loads(body)

    assert status == 202
    assert payload["status"] == "running"
    assert entered.wait(1)
    release.set()
    assert wait_terminal()["status"] == "completed"


def test_duplicate_start_returns_the_running_job(monkeypatch):
    reset_job(monkeypatch)
    release = threading.Event()

    def slow_fetcher():
        release.wait(2)
        return json.dumps({"status": "ok"}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", slow_fetcher)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())

    first, first_status = refresh_server.start_refresh_job("manual")
    second, second_status = refresh_server.start_refresh_job("manual")

    assert first_status == 202
    assert second_status == 200
    assert json.loads(first)["job_id"] == json.loads(second)["job_id"]
    release.set()
    assert wait_terminal()["status"] == "completed"


def test_concurrent_starts_coalesce_to_one_job(monkeypatch):
    reset_job(monkeypatch)
    callers_ready = threading.Barrier(3)
    release_worker = threading.Event()
    results = []
    results_lock = threading.Lock()

    def slow_fetcher():
        release_worker.wait(2)
        return json.dumps({"status": "ok"}).encode(), 200

    def start_job():
        callers_ready.wait()
        result = refresh_server.start_refresh_job("manual")
        with results_lock:
            results.append(result)

    monkeypatch.setattr(refresh_server, "run_fetcher", slow_fetcher)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())
    callers = [threading.Thread(target=start_job) for _ in range(2)]
    for caller in callers:
        caller.start()

    callers_ready.wait()
    for caller in callers:
        caller.join(1)
    release_worker.set()

    assert all(not caller.is_alive() for caller in callers)
    assert sorted(status for _, status in results) == [200, 202]
    assert len({json.loads(body)["job_id"] for body, _ in results}) == 1
    assert wait_terminal()["status"] == "completed"


@pytest.mark.parametrize("failure_point", ["construct", "start"])
def test_thread_launch_failure_transitions_job_to_failed(monkeypatch, failure_point):
    reset_job(monkeypatch)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())

    class FailingThread:
        def __init__(self, **kwargs):
            if failure_point == "construct":
                raise RuntimeError("internal path: /app/data/news.db")

        def start(self):
            raise RuntimeError("internal path: /app/data/news.db")

    monkeypatch.setattr(refresh_server.threading, "Thread", FailingThread)

    body, status = refresh_server.start_refresh_job("manual")
    payload = json.loads(body)
    current = json.loads(refresh_server.get_refresh_job_status())

    assert status == 500
    assert payload == current
    assert payload["job_id"]
    assert payload["status"] == "failed"
    assert payload["finished_at"] is not None
    assert payload["error"] == "refresh failed"
    assert "/app/data" not in json.dumps(payload)


def test_refresh_job_reports_new_count(monkeypatch):
    reset_job(monkeypatch)
    snapshots = iter(({1, 2}, {1, 2, 3, 4}))
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(
        refresh_server,
        "run_fetcher",
        lambda: (json.dumps({"status": "ok"}).encode(), 200),
    )
    refresh_server.start_refresh_job("manual")
    payload = wait_terminal()
    assert payload["status"] == "completed"
    assert payload["new_count"] == 2
    assert payload["finished_at"] is not None


def test_refresh_job_exposes_compact_failure(monkeypatch):
    reset_job(monkeypatch)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())
    monkeypatch.setattr(
        refresh_server,
        "run_fetcher",
        lambda: (json.dumps({"status": "error", "error": "timeout"}).encode(), 500),
    )
    refresh_server.start_refresh_job("manual")
    payload = wait_terminal()
    assert payload["status"] == "failed"
    assert payload["error"] == "timeout"
    assert "stdout" not in payload
    assert "stderr" not in payload


def test_refresh_job_does_not_expose_internal_error_details(monkeypatch):
    reset_job(monkeypatch)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())
    monkeypatch.setattr(
        refresh_server,
        "run_fetcher",
        lambda: (
            json.dumps({
                "status": "error",
                "error": "unable to open /app/data/news.db",
                "stdout": "fetch log",
                "stderr": "traceback",
            }).encode(),
            500,
        ),
    )

    refresh_server.start_refresh_job("manual")
    payload = wait_terminal()

    assert payload["status"] == "failed"
    assert payload["error"] == "refresh failed"
    assert "/app/data" not in json.dumps(payload)
    assert "stdout" not in payload
    assert "stderr" not in payload


def test_post_refresh_starts_manual_job(monkeypatch):
    calls = []
    response = json.dumps({"status": "running"}).encode()
    monkeypatch.setattr(
        refresh_server,
        "start_refresh_job",
        lambda trigger: (calls.append(trigger) or response, 202),
    )
    monkeypatch.setattr(
        refresh_server,
        "send_json",
        lambda handler, body, status=200: calls.append((body, status)),
    )
    handler = refresh_server.Handler.__new__(refresh_server.Handler)
    handler.path = "/refresh"

    refresh_server.Handler.do_POST(handler)

    assert calls == ["manual", (response, 202)]


def test_get_refresh_keeps_compatibility_start_behavior(monkeypatch):
    calls = []
    response = json.dumps({"status": "running"}).encode()
    monkeypatch.setattr(
        refresh_server,
        "start_refresh_job",
        lambda trigger: (calls.append(trigger) or response, 202),
    )
    monkeypatch.setattr(
        refresh_server,
        "run_fetcher",
        lambda: (json.dumps({"status": "legacy"}).encode(), 200),
    )
    monkeypatch.setattr(
        refresh_server,
        "send_json",
        lambda handler, body, status=200: calls.append((body, status)),
    )
    handler = refresh_server.Handler.__new__(refresh_server.Handler)
    handler.path = "/refresh"

    refresh_server.Handler.do_GET(handler)

    assert calls == ["manual", (response, 202)]


def test_get_refresh_status_returns_job_snapshot(monkeypatch):
    calls = []
    response = json.dumps({"status": "idle"}).encode()
    monkeypatch.setattr(refresh_server, "get_refresh_job_status", lambda: response)
    monkeypatch.setattr(
        refresh_server,
        "send_json",
        lambda handler, body, status=200: calls.append((body, status)),
    )
    monkeypatch.setattr(
        refresh_server,
        "send_text",
        lambda handler, body, status=200: calls.append((body, status)),
    )
    handler = refresh_server.Handler.__new__(refresh_server.Handler)
    handler.path = "/refresh/status"

    refresh_server.Handler.do_GET(handler)

    assert calls == [(response, 200)]


def test_periodic_refresh_starts_periodic_job_and_reschedules(monkeypatch):
    calls = []

    class FakeTimer:
        def __init__(self, interval, callback):
            calls.append((interval, callback))

        def start(self):
            calls.append("timer-started")

    monkeypatch.setattr(refresh_server, "start_refresh_job", calls.append)
    monkeypatch.setattr(refresh_server, "run_fetcher", lambda: calls.append("legacy"))
    monkeypatch.setattr(refresh_server.threading, "Timer", FakeTimer)

    refresh_server.periodic_refresh()

    assert calls == [
        "periodic",
        (refresh_server.REFRESH_INTERVAL, refresh_server.periodic_refresh),
        "timer-started",
    ]


def test_startup_refresh_is_scheduled_after_server_creation():
    source = Path(refresh_server.__file__).read_text(encoding="utf-8")
    main = source[source.index('if __name__ == "__main__":'):]

    assert main.index("server = RayNewsThreadingHTTPServer") < main.index(
        'start_refresh_job("startup")'
    )
