"""Behavioral contracts for the webhook-triggered refresh rerun mechanism."""

import json
import threading
from collections import OrderedDict

import refresh_server


def reset_job(monkeypatch):
    monkeypatch.setattr(refresh_server, "REFRESH_JOB", {
        "job_id": "", "status": "idle", "trigger": "",
        "started_at": None, "finished_at": None,
        "new_count": 0, "error": "",
    })
    monkeypatch.setattr(refresh_server, "REFRESH_JOB_HISTORY", OrderedDict())
    monkeypatch.setattr(refresh_server, "REFRESH_RERUN_PENDING", False)


def wait_terminal():
    for _ in range(200):
        payload = json.loads(refresh_server.get_refresh_job_status())
        if payload["status"] != "running":
            return payload
        threading.Event().wait(0.01)
    raise AssertionError("refresh job did not finish")


def test_webhook_trigger_while_running_sets_rerun_pending(monkeypatch):
    reset_job(monkeypatch)
    release = threading.Event()

    def slow_fetcher():
        release.wait(2)
        return json.dumps({"status": "ok"}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", slow_fetcher)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())

    refresh_server.start_refresh_job("manual")
    body, status = refresh_server.start_refresh_job("webhook")

    assert status == 200
    assert refresh_server.REFRESH_RERUN_PENDING is True
    release.set()
    wait_terminal()


def test_manual_trigger_while_running_does_not_set_rerun_pending(monkeypatch):
    reset_job(monkeypatch)
    release = threading.Event()

    def slow_fetcher():
        release.wait(2)
        return json.dumps({"status": "ok"}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", slow_fetcher)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())

    refresh_server.start_refresh_job("manual")
    refresh_server.start_refresh_job("manual")

    assert refresh_server.REFRESH_RERUN_PENDING is False
    release.set()
    wait_terminal()


def test_pending_rerun_starts_one_more_job_after_completion(monkeypatch):
    reset_job(monkeypatch)
    release_first = threading.Event()
    fetch_calls = []

    def fetcher():
        fetch_calls.append(1)
        if len(fetch_calls) == 1:
            release_first.wait(2)
        return json.dumps({"status": "ok"}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", fetcher)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())

    refresh_server.start_refresh_job("manual")
    refresh_server.start_refresh_job("webhook")  # sets rerun pending
    release_first.set()

    # First job completes, rerun should fire automatically -> second job runs too.
    for _ in range(200):
        if len(fetch_calls) >= 2:
            break
        threading.Event().wait(0.01)
    else:
        raise AssertionError("rerun job never started")

    wait_terminal()
    assert refresh_server.REFRESH_RERUN_PENDING is False


def test_rerun_does_not_chain_indefinitely(monkeypatch):
    reset_job(monkeypatch)
    fetch_calls = []

    def fetcher():
        fetch_calls.append(1)
        return json.dumps({"status": "ok"}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", fetcher)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())

    refresh_server.start_refresh_job("webhook")
    wait_terminal()

    # No concurrent webhook arrived during the (instant) run, so no rerun should fire.
    assert len(fetch_calls) == 1
    assert refresh_server.REFRESH_RERUN_PENDING is False


def test_refresh_route_accepts_webhook_trigger(monkeypatch):
    calls = []
    monkeypatch.setattr(
        refresh_server,
        "start_refresh_job",
        lambda trigger: (calls.append(trigger) or json.dumps({"status": "running"}).encode(), 202),
    )
    monkeypatch.setattr(
        refresh_server,
        "send_json",
        lambda handler, body, status=200: calls.append((body, status)),
    )
    handler = refresh_server.Handler.__new__(refresh_server.Handler)
    handler.path = "/refresh?trigger=webhook"

    refresh_server.Handler.do_POST(handler)

    assert calls[0] == "webhook"


def test_refresh_route_rejects_unknown_trigger_as_manual(monkeypatch):
    calls = []
    monkeypatch.setattr(
        refresh_server,
        "start_refresh_job",
        lambda trigger: (calls.append(trigger) or json.dumps({"status": "running"}).encode(), 202),
    )
    monkeypatch.setattr(
        refresh_server,
        "send_json",
        lambda handler, body, status=200: calls.append((body, status)),
    )
    handler = refresh_server.Handler.__new__(refresh_server.Handler)
    handler.path = "/refresh?trigger=something-untrusted"

    refresh_server.Handler.do_POST(handler)

    assert calls[0] == "manual"


def test_refresh_interval_env_override(monkeypatch):
    monkeypatch.setenv("REFRESH_INTERVAL_SECONDS", "3600")
    assert refresh_server._resolve_refresh_interval() == 3600


def test_refresh_interval_env_floor(monkeypatch):
    monkeypatch.setenv("REFRESH_INTERVAL_SECONDS", "10")
    assert refresh_server._resolve_refresh_interval() == 300


def test_refresh_interval_env_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("REFRESH_INTERVAL_SECONDS", "not-a-number")
    assert refresh_server._resolve_refresh_interval() == 900


def test_refresh_interval_env_unset_defaults_to_900(monkeypatch):
    monkeypatch.delenv("REFRESH_INTERVAL_SECONDS", raising=False)
    assert refresh_server._resolve_refresh_interval() == 900
