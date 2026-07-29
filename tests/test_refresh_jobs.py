import json
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path

import pytest

import refresh_server


class _CountDb:
    def __init__(self, count):
        self.count = count
        self.closed = False

    def execute(self, sql):
        assert sql == "SELECT COUNT(*) FROM articles"
        return self

    def fetchone(self):
        return (self.count,)

    def close(self):
        self.closed = True


class _EmptyNewsDb:
    def execute(self, sql, args=()):
        self.sql = sql
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return (0,)

    def close(self):
        pass


def test_api_meta_public_payload_is_exactly_the_count(monkeypatch):
    db = _CountDb(3)
    monkeypatch.setattr(refresh_server, "get_db", lambda: db)

    payload = json.loads(refresh_server.api_meta())

    assert payload == {"count": 3}
    assert db.closed is True


def test_api_meta_failure_is_generic_and_detailed_only_in_logs(monkeypatch, caplog):
    def fail():
        raise sqlite3.OperationalError("secret path /app/data/news.db")

    monkeypatch.setattr(refresh_server, "get_db", fail)

    with caplog.at_level("ERROR"):
        payload = json.loads(refresh_server.api_meta())

    assert payload == {"error": "internal server error"}
    assert "secret path /app/data/news.db" in caplog.text


def test_api_news_failure_keeps_diagnostics_but_hides_exception_detail(
    monkeypatch, caplog
):
    def fail():
        raise sqlite3.OperationalError("secret path /app/data/news.db")

    monkeypatch.setattr(refresh_server, "get_db", fail)

    with caplog.at_level("ERROR"):
        payload = json.loads(refresh_server.api_news_list({}))

    assert payload["error"] == "internal server error"
    assert set(payload["diagnostics"]) == {"refresh_job", "global_article_count"}
    assert "secret path /app/data/news.db" not in json.dumps(payload)
    assert "secret path /app/data/news.db" in caplog.text


def test_empty_api_news_exposes_only_minimal_cold_start_diagnostics(monkeypatch):
    monkeypatch.setattr(refresh_server, "get_db", lambda: _EmptyNewsDb())

    payload = json.loads(refresh_server.api_news_list({}))

    assert payload["items"] == []
    assert payload["total"] == 0
    assert set(payload["diagnostics"]) == {"refresh_job", "global_article_count"}
    for private_key in (
        "data_dir",
        "db_path",
        "db_exists",
        "db_size",
        "news_json",
        "fetcher_state",
        "telegram_channel",
        "last_fetch",
    ):
        assert private_key not in payload["diagnostics"]


def reset_job(monkeypatch):
    monkeypatch.setattr(refresh_server, "REFRESH_JOB", {
        "job_id": "", "status": "idle", "trigger": "",
        "started_at": None, "finished_at": None,
        "new_count": 0, "new_ids": [], "error": "",
    })
    monkeypatch.setattr(refresh_server, "REFRESH_JOB_HISTORY", OrderedDict())


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


def test_start_and_status_return_while_baseline_snapshot_is_slow(monkeypatch):
    reset_job(monkeypatch)
    snapshot_entered = threading.Event()
    release_snapshot = threading.Event()
    start_returned = threading.Event()
    status_returned = threading.Event()
    snapshots = iter(({1, 2}, {1, 2, 3}))

    def slow_baseline_snapshot():
        snapshot = next(snapshots)
        if not snapshot_entered.is_set():
            snapshot_entered.set()
            release_snapshot.wait(2)
        return snapshot

    monkeypatch.setattr(refresh_server, "article_id_snapshot", slow_baseline_snapshot)
    monkeypatch.setattr(
        refresh_server,
        "run_fetcher",
        lambda: (json.dumps({"status": "ok"}).encode(), 200),
    )

    start_results = []

    def start_job():
        start_results.append(refresh_server.start_refresh_job("startup"))
        start_returned.set()

    starter = threading.Thread(target=start_job)
    starter.start()
    assert snapshot_entered.wait(1)

    status_responses = []
    monkeypatch.setattr(
        refresh_server,
        "send_json",
        lambda handler, body, status=200: status_responses.append((body, status)),
    )
    handler = refresh_server.Handler.__new__(refresh_server.Handler)
    handler.path = "/refresh/status"

    def request_status():
        refresh_server.Handler.do_GET(handler)
        status_returned.set()

    status_request = threading.Thread(target=request_status)
    status_request.start()

    start_was_immediate = start_returned.wait(0.5)
    status_was_immediate = status_returned.wait(0.5)
    release_snapshot.set()
    starter.join(1)
    status_request.join(1)

    assert (start_was_immediate, status_was_immediate) == (True, True)
    start_body, start_status = start_results[0]
    assert start_status == 202
    assert json.loads(start_body)["status"] == "running"
    assert json.loads(status_responses[0][0])["status"] == "running"
    payload = wait_terminal()
    assert payload["status"] == "completed"
    assert payload["new_count"] == 1


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


def test_refresh_job_reports_new_count_and_ids(monkeypatch):
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
    assert payload["new_ids"] == [3, 4]
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


def test_terminal_job_remains_queryable_after_next_job_starts(monkeypatch):
    reset_job(monkeypatch)
    release_second = threading.Event()
    fetch_count = 0

    def fetcher():
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 2:
            release_second.wait(2)
        return json.dumps({"status": "ok"}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", fetcher)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())

    first_body, _ = refresh_server.start_refresh_job("manual")
    first_id = json.loads(first_body)["job_id"]
    first_terminal = wait_terminal()
    second_body, second_status = refresh_server.start_refresh_job("manual")
    second_id = json.loads(second_body)["job_id"]

    first_lookup, first_status = refresh_server.get_refresh_job_status_response(first_id)
    current_lookup, current_status = refresh_server.get_refresh_job_status_response(second_id)

    assert second_status == 202
    assert json.loads(first_lookup) == first_terminal
    assert first_status == 200
    assert json.loads(current_lookup)["status"] == "running"
    assert current_status == 200
    release_second.set()
    assert wait_terminal()["status"] == "completed"


def test_terminal_history_is_bounded_and_unknown_ids_are_private(monkeypatch):
    reset_job(monkeypatch)
    monkeypatch.setattr(refresh_server, "REFRESH_JOB_HISTORY_LIMIT", 2)
    monkeypatch.setattr(refresh_server, "run_fetcher", lambda: (json.dumps({"status": "ok"}).encode(), 200))
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())
    completed_ids = []

    for _ in range(3):
        body, _ = refresh_server.start_refresh_job("manual")
        completed_ids.append(json.loads(body)["job_id"])
        assert wait_terminal()["status"] == "completed"

    expired_body, expired_status = refresh_server.get_refresh_job_status_response(completed_ids[0])
    retained_body, retained_status = refresh_server.get_refresh_job_status_response(completed_ids[-1])

    assert expired_status == 404
    assert json.loads(expired_body) == {
        "status": "not_found",
        "error": "refresh job not found",
    }
    assert completed_ids[0] not in expired_body.decode()
    assert retained_status == 200
    assert json.loads(retained_body)["job_id"] == completed_ids[-1]


def test_get_refresh_status_route_looks_up_requested_job_id(monkeypatch):
    calls = []
    response = json.dumps({"job_id": "job-a", "status": "completed"}).encode()
    monkeypatch.setattr(
        refresh_server,
        "get_refresh_job_status_response",
        lambda job_id=None: (calls.append(job_id) or response, 200),
    )
    monkeypatch.setattr(
        refresh_server,
        "send_json",
        lambda handler, body, status=200: calls.append((body, status)),
    )
    handler = refresh_server.Handler.__new__(refresh_server.Handler)
    handler.path = "/refresh/status?job_id=job-a"

    refresh_server.Handler.do_GET(handler)

    assert calls == ["job-a", (response, 200)]


def test_empty_news_diagnostics_expose_only_safe_startup_job_summary(monkeypatch):
    reset_job(monkeypatch)
    refresh_server.REFRESH_JOB.update({
        "job_id": "private-job-id",
        "status": "running",
        "trigger": "startup",
        "error": "/app/data/private.db",
    })

    diagnostics = refresh_server._diagnostics(0)

    assert diagnostics["refresh_job"] == {
        "status": "running",
        "trigger": "startup",
    }
    assert "private-job-id" not in json.dumps(diagnostics)
    assert "/app/data/private.db" not in json.dumps(diagnostics["refresh_job"])


def test_empty_filtered_result_diagnostics_include_global_article_count(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO articles (id) VALUES (?)", [(1,), (2,)])
    conn.commit()
    conn.close()
    monkeypatch.setattr(refresh_server, "DB_FILE", db_path)

    diagnostics = refresh_server._diagnostics(0)

    assert diagnostics["global_article_count"] == 2


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

    server_created = main.index("server = RayNewsThreadingHTTPServer")
    startup_scheduled = main.index('start_refresh_job("startup")')
    server_serving = main.index("server.serve_forever()")

    assert server_created < startup_scheduled < server_serving
