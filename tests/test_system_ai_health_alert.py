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
    jobs = ("自动翻译", "标题精简", "每日摘要")
    for job in jobs:                       # one failure each…
        _fail(1, job=job)
    _fail(max(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD - len(jobs), 0), job=jobs[-1])

    body = alerts[0]["body"]
    for job in jobs:
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


def test_the_evening_retry_chain_alone_reaches_the_threshold(news_db_free, monkeypatch, alerts):
    """A day with no pending article work still reports the outage.

    The article jobs only call the AI when they have something to process, so on
    a quiet day the daily-summary chain is the only caller: 21:00 plus three
    retries, four attempts. The threshold has to sit under that or the outage
    would be invisible until the 21:30 daily-summary alert.
    """
    assert web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD <= 4

    class BoomService:
        def __init__(self, **kwargs):
            pass

        def daily_summary(self, articles):
            raise RuntimeError("401 invalid api key")

    monkeypatch.setattr(web_server, "get_system_ai_config",
                        lambda: {"enabled": True, "api_key": "k", "endpoint": "e", "model": "m"})
    monkeypatch.setattr(web_server, "_fetch_articles_by_date",
                        lambda date_str, include_shared_summary=False: [{"id": 1, "title": "t"}])
    monkeypatch.setattr(web_server, "_dedup_articles", lambda articles: articles)
    monkeypatch.setattr(web_server, "AIService", BoomService)

    attempts = 1 + web_server.DAILY_SUMMARY_MAX_RETRIES
    for _ in range(attempts):
        web_server._generate_daily_summary_global("2026-07-10")

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2
    assert "每日摘要" in alerts[0]["body"]


# ─── A cleared/disabled config, without probing anything ───────────────


@pytest.fixture
def admin_with_auto_jobs(tmp_path, monkeypatch):
    """An admin with a background AI job switched on, on a real settings DB."""
    import uuid

    import models

    db_path = tmp_path / f"auto-config-{uuid.uuid4().hex}.db"
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = db_path
    try:
        models.get_db()
        admin = models.create_user("admin@example.com", "pw", "A", role="admin")["id"]
        models.set_user_settings(admin, auto_summary_enabled=1)
        monkeypatch.setattr(web_server, "get_db", models.get_db)
        yield admin
    finally:
        models.close_db()
        models.DB_FILE = old_db_file


def test_an_enabled_job_with_no_usable_system_ai_alerts(admin_with_auto_jobs, alerts, monkeypatch):
    # The jobs skip this state without calling the provider, so the
    # misconfiguration itself has to be what gets counted.
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": 0, "api_key": ""})

    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        assert web_server._system_auto_config("auto_summary_enabled") is None

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2
    assert "未配置或未启用" in alerts[0]["body"]
    assert "服务端 API 配置" in alerts[0]["body"]


def test_a_key_that_is_present_but_empty_counts_the_same(admin_with_auto_jobs, alerts, monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config",
                        lambda: {"enabled": 1, "api_key": "", "endpoint": "e", "model": "m"})

    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2


def test_no_enabled_job_means_no_alert_however_broken_the_config(tmp_path, alerts, monkeypatch):
    import uuid

    import models

    db_path = tmp_path / f"auto-config-off-{uuid.uuid4().hex}.db"
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = db_path
    try:
        models.get_db()
        models.create_user("admin@example.com", "pw", "A", role="admin")
        monkeypatch.setattr(web_server, "get_db", models.get_db)
        monkeypatch.setattr(web_server, "get_system_ai_config", lambda: None)

        for _ in range(10):
            assert web_server._system_auto_config("auto_summary_enabled") is None

        assert alerts == []
    finally:
        models.close_db()
        models.DB_FILE = old_db_file


def test_a_usable_config_is_returned_and_counts_nothing(admin_with_auto_jobs, alerts, monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {
        "enabled": 1, "api_key": "k", "endpoint": "e", "model": "m", "provider_type": "openai",
    })

    config = web_server._system_auto_config("auto_summary_enabled")

    assert config["api_key"] == "k"
    assert alerts == []


def test_fixing_the_config_sends_the_recovery_notice(admin_with_auto_jobs, alerts, monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": 0, "api_key": ""})
    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")
    alerts.clear()

    # The admin saves a working config and the next job call succeeds.
    web_server._note_system_ai_success()

    assert [a["type"] for a in alerts] == ["system_ai_recovered"] * 2


# ─── One outage, one alert — across restarts too ───────────────────────


def test_a_restart_mid_outage_does_not_re_alert(admin_with_auto_jobs, alerts, monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": 0, "api_key": ""})
    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")
    assert len(alerts) == 2

    # Restart: the in-memory streak is gone, the outage and the settings DB are not.
    web_server._system_ai_health.update(
        {"failures": 0, "alerted": False, "last_error": "", "jobs": []})
    for _ in range(3 * web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")

    assert len(alerts) == 2   # still the one alert from before the restart


def test_after_recovery_a_new_outage_alerts_again_across_a_restart(admin_with_auto_jobs, alerts,
                                                                   monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": 0, "api_key": ""})
    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")
    web_server._note_system_ai_success()          # fixed
    alerts.clear()

    web_server._system_ai_health.update(
        {"failures": 0, "alerted": False, "last_error": "", "jobs": []})   # restart
    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2


def test_the_recovery_notice_is_owed_even_if_the_alert_predates_the_restart(
        admin_with_auto_jobs, alerts, monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": 0, "api_key": ""})
    for _ in range(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._system_auto_config("auto_summary_enabled")
    alerts.clear()
    web_server._system_ai_health.update(
        {"failures": 0, "alerted": False, "last_error": "", "jobs": []})   # restart

    web_server._note_system_ai_success()

    assert [a["type"] for a in alerts] == ["system_ai_recovered"] * 2


# ─── Source classification runs on a different key ─────────────────────


def _classification_stubs(monkeypatch, classify_result=None, error=None):
    """Drive _classify_source_batch without a news DB or a real provider."""
    class FakeService:
        def __init__(self, **kwargs):
            pass

        def classify_source(self, source, titles, domains=None):
            if error:
                raise error
            return classify_result or {"category": "News", "label": source, "confidence": 0.9}

    monkeypatch.setattr(web_server, "_get_news_db", lambda: object())
    monkeypatch.setattr(web_server, "ensure_article_sources", lambda conn: None)
    monkeypatch.setattr(web_server, "source_rows",
                        lambda conn: [{"source": "财经早餐", "status": "pending"}])
    monkeypatch.setattr(web_server, "recent_titles_for_source",
                        lambda conn, source, limit=8: ["t1", "t2"])
    monkeypatch.setattr(web_server, "_extract_domains_for_source", lambda conn, source: [])
    monkeypatch.setattr(web_server, "update_source_category",
                        lambda conn, source, category, label, **kwargs: {"source": source})
    monkeypatch.setattr(web_server, "AIService", FakeService)


def test_a_working_personal_key_cannot_clear_a_system_ai_outage(alerts, monkeypatch):
    """The reported case: system API suspended, admin's own key still valid.

    Auto summary/translation/title were failing with 401 on the *system* key
    while source classification kept succeeding on the admin's *personal* key.
    Counting those successes as system-AI health reset the streak every minute,
    so the outage never reached the alert threshold.
    """
    _classification_stubs(monkeypatch)
    config = {"api_key": "personal", "endpoint": "e", "model": "m", "user_id": 1}

    web_server._note_system_ai_failure("自动翻译", "AI API HTTP 401: Invalid API key.")
    web_server._note_system_ai_failure("标题精简", "AI API HTTP 401: Invalid API key.")

    result = web_server._classify_source_batch(config, limit=10)
    assert result["processed"]           # the personal key worked, as in the log
    assert alerts == []                  # …and it must not have reset anything

    web_server._note_system_ai_failure("自动摘要", "AI API HTTP 401: Invalid API key.")

    assert [a["type"] for a in alerts] == ["system_ai_failed"] * 2
    assert "401" in alerts[0]["body"]


def test_a_failing_personal_key_does_not_blame_the_system_ai(alerts, monkeypatch):
    _classification_stubs(monkeypatch, error=RuntimeError("personal key 401"))
    config = {"api_key": "personal", "endpoint": "e", "model": "m", "user_id": 1}

    for _ in range(3 * web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD):
        web_server._classify_source_batch(config, limit=10)

    assert alerts == []
