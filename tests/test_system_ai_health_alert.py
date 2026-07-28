"""Admins are told when the system AI stops working — not just at 21:30.

Every background job (auto summary/translation/title, source classification)
and the daily summary share the one admin-configured system AI. A dead key used
to surface only as log lines plus, hours later, the daily-summary failure alert.
Consecutive failures across all of those jobs now raise one alert, and the next
success raises one recovery notice.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import web_server


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(web_server, "list_users", lambda: [
        {"id": 1, "role": "admin"}, {"id": 2, "role": "user"}, {"id": 3, "role": "admin"},
    ])
    monkeypatch.setattr(web_server, "_notify_user",
                        lambda user_id, ntype, title, body: sent.append(
                            {"user_id": user_id, "type": ntype, "title": title, "body": body}))
    web_server._reset_system_ai_health()
    yield sent
    web_server._reset_system_ai_health()


def _fail(times, job="自动摘要", error="401 invalid api key"):
    for _ in range(times):
        web_server._note_system_ai_failure(job, error)


def test_a_few_failures_stay_quiet(alerts):
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD - 1)
    assert alerts == []


def test_the_threshold_alerts_every_admin_once(alerts):
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)

    assert [a["user_id"] for a in alerts] == [1, 3]
    assert all(a["type"] == "system_ai_failed" for a in alerts)
    assert "401 invalid api key" in alerts[0]["body"]
    assert "自动摘要" in alerts[0]["body"]

    # A provider that keeps failing must not keep notifying.
    _fail(20)
    assert len(alerts) == 2


def test_the_alert_names_every_affected_job(alerts):
    _fail(2, job="自动翻译")
    _fail(2, job="标题精简")
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD, job="每日摘要")

    body = alerts[0]["body"]
    for job in ("自动翻译", "标题精简", "每日摘要"):
        assert job in body


def test_a_success_before_the_threshold_resets_the_streak(alerts):
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD - 1)
    web_server._note_system_ai_success()
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD - 1)

    assert alerts == []


def test_recovery_is_announced_once_after_an_alert(alerts):
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()

    web_server._note_system_ai_success()
    assert [a["type"] for a in alerts] == ["system_ai_recovered"] * 2

    # Nothing further to announce while it keeps working.
    alerts.clear()
    web_server._note_system_ai_success()
    assert alerts == []


def test_a_new_outage_after_a_recovery_alerts_again(alerts):
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    web_server._note_system_ai_success()
    alerts.clear()

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2


def test_saving_a_new_system_ai_config_clears_the_muted_flag(alerts):
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()

    # The admin swaps in another key; it is broken too and must alert again.
    web_server._reset_system_ai_health()
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2


def test_the_alert_never_raises_into_the_calling_job(alerts, monkeypatch):
    def boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(web_server, "list_users", boom)

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)  # must not raise


def test_generation_failure_feeds_the_streak(news_db_free, monkeypatch, alerts):
    class BoomService:
        def __init__(self, **kwargs):
            pass

        def daily_summary(self, articles):
            raise RuntimeError("502 upstream")

    monkeypatch.setattr(web_server, "get_system_ai_config",
                        lambda: {"enabled": True, "api_key": "k", "endpoint": "e", "model": "m"})
    monkeypatch.setattr(web_server, "_fetch_articles_by_date",
                        lambda date_str, include_shared_summary=False: [{"id": 1, "title": "t"}])
    monkeypatch.setattr(web_server, "_dedup_articles", lambda articles: articles)
    monkeypatch.setattr(web_server, "AIService", BoomService)

    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        assert web_server._generate_daily_summary_global("2026-07-10") is None

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2
    assert "502 upstream" in alerts[0]["body"]


@pytest.fixture
def news_db_free(monkeypatch, tmp_path):
    """Point NEWS_DB at a path with no database, so the cache helpers no-op."""
    monkeypatch.setattr(web_server, "NEWS_DB", str(tmp_path / "absent.db"))


def test_admin_connection_test_reports_recovery_without_waiting_for_a_job(alerts, monkeypatch):
    from flask import g

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()

    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": True})
    monkeypatch.setattr(web_server, "_run_ai_connection_test", lambda config: ({"ok": True}, 200))
    with web_server.app.test_request_context("/admin/system-ai-config/test", method="POST"):
        g.user_id = 1
        g.user_role = "admin"
        web_server.admin_system_ai_test_connection.__wrapped__()

    assert [a["type"] for a in alerts] == ["system_ai_recovered"] * 2


def test_a_failing_admin_connection_test_does_not_push_the_streak(alerts, monkeypatch):
    from flask import g

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD - 1)
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": True})
    monkeypatch.setattr(web_server, "_run_ai_connection_test",
                        lambda config: ({"error": "401"}, 400))
    with web_server.app.test_request_context("/admin/system-ai-config/test", method="POST"):
        g.user_id = 1
        g.user_role = "admin"
        web_server.admin_system_ai_test_connection.__wrapped__()

    assert alerts == []
