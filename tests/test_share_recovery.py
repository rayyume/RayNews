"""Regression coverage for durable shared-AI suspension state."""

import os
import threading
import uuid
from pathlib import Path

import pytest

import models
import web_server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def share_env():
    db_path = ROOT / f"tmp-share-recovery-{uuid.uuid4().hex}.db"
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = db_path
    models.get_db()
    user = models.create_user("share@example.com", "pw", "share-user")
    client = web_server.app.test_client()
    try:
        yield client, user["id"]
    finally:
        models.close_db()
        models.DB_FILE = old_db_file
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


def auth_headers(user_id: int, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {web_server.create_token(user_id, role)}"}


def test_share_suspended_defaults_false_and_round_trips(share_env):
    _, user_id = share_env
    settings = models.set_user_settings(
        user_id,
        share_ai_results=1,
        share_view_title=1,
        share_last_check_ok=1,
    )
    assert settings["share_suspended"] == 0

    settings = models.set_user_settings(user_id, share_suspended=1)
    assert settings["share_suspended"] == 1
    assert settings["share_ai_results"] == 1
    assert settings["share_view_title"] == 1


@pytest.mark.parametrize(
    ("settings", "expected"),
    (
        (None, False),
        ({}, False),
        ({
            "share_ai_results": 1,
            "share_suspended": 0,
            "share_last_check_ok": 1,
            "share_last_check_revision": 2,
            "share_current_config_revision": 2,
        }, True),
        ({
            "share_ai_results": 1,
            "share_suspended": 0,
            "share_last_check_ok": 1,
            "share_last_check_revision": None,
            "share_current_config_revision": 2,
        }, False),
        ({"share_ai_results": 1, "share_suspended": 1, "share_last_check_ok": 1}, False),
        ({"share_ai_results": 1, "share_suspended": 0, "share_last_check_ok": 0}, False),
        ({"share_ai_results": 0, "share_suspended": 0, "share_last_check_ok": 1}, False),
    ),
)
def test_is_share_active_requires_intent_health_and_no_suspension(settings, expected):
    assert web_server.is_share_active(settings) is expected


def test_suspension_hides_cached_summary_and_translation_without_clearing_preferences(
    share_env, monkeypatch
):
    client, user_id = share_env
    models.set_user_settings(
        user_id,
        share_ai_results=1,
        share_view_title=1,
        share_view_summary=1,
        share_view_translation=1,
        share_last_check_ok=0,
        share_suspended=1,
    )
    monkeypatch.setattr(
        web_server,
        "_get_ai_result",
        lambda article_id: {
            "summary": "shared summary",
            "summary_error": "old error",
            "summary_error_at": "2026-07-27T10:00:00",
            "translation": "shared translation",
            "updated_at": "2026-07-27T10:00:00",
        },
    )

    response = client.get("/ai/result/42", headers=auth_headers(user_id))

    assert response.status_code == 200
    assert response.get_json() == {"updated_at": "2026-07-27T10:00:00"}
    settings = models.get_user_settings(user_id)
    assert settings["share_ai_results"] == 1
    assert settings["share_view_title"] == 1
    assert settings["share_view_summary"] == 1
    assert settings["share_view_translation"] == 1
    assert settings["share_suspended"] == 1


def opted_in(user_id: int, *, suspended: int = 0):
    return models.set_user_settings(
        user_id,
        share_ai_results=1,
        share_view_title=1,
        share_view_translation=0,
        share_view_summary=1,
        share_suspended=suspended,
        share_last_check_ok=0 if suspended else 1,
    )


def test_failed_check_suspends_without_clearing_preferences(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    result = web_server._apply_share_connectivity_result(
        user_id, False, "AI API HTTP 401", "2026-07-27T10:00:00"
    )

    settings = models.get_user_settings(user_id)
    assert result == "suspended"
    assert settings["share_ai_results"] == 1
    assert settings["share_view_title"] == 1
    assert settings["share_view_translation"] == 0
    assert settings["share_view_summary"] == 1
    assert settings["share_suspended"] == 1
    assert settings["share_last_check_ok"] == 0
    assert len(notices) == 1
    assert notices[0][1] == "share_suspended"


def test_repeated_failure_updates_health_without_duplicate_notice(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    result = web_server._apply_share_connectivity_result(
        user_id, False, "still unavailable", "2026-07-27T11:00:00"
    )

    assert result == "unchanged"
    assert notices == []


def test_success_restores_exact_preferences_once(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    result = web_server._apply_share_connectivity_result(
        user_id, True, checked_at="2026-07-27T12:00:00"
    )

    settings = models.get_user_settings(user_id)
    assert result == "restored"
    assert settings["share_suspended"] == 0
    assert settings["share_view_title"] == 1
    assert settings["share_view_translation"] == 0
    assert settings["share_view_summary"] == 1
    assert settings["share_last_check_ok"] == 1
    assert len(notices) == 1
    assert notices[0][1] == "share_restored"

    assert web_server._apply_share_connectivity_result(
        user_id, True, checked_at="2026-07-27T12:01:00"
    ) == "unchanged"
    assert len(notices) == 1


def test_explicitly_disabled_user_is_never_auto_restored(share_env, monkeypatch):
    _, user_id = share_env
    models.set_user_settings(
        user_id,
        share_ai_results=0,
        share_suspended=0,
        share_last_check_ok=0,
    )
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    result = web_server._apply_share_connectivity_result(user_id, True)

    assert result == "not_opted_in"
    assert models.get_user_settings(user_id)["share_ai_results"] == 0
    assert notices == []


def test_share_error_is_bounded_and_redacts_api_key(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    secret = "sk-super-secret-provider-key"
    web_server._apply_share_connectivity_result(user_id, False, f"provider rejected {secret} " + "x" * 400)

    settings = models.get_user_settings(user_id)
    assert secret not in settings["share_last_check_error"]
    assert settings["share_last_check_error"] == settings["share_last_check_error"].strip()
    assert len(settings["share_last_check_error"]) <= 300
    assert secret not in notices[0][3]


def test_suspended_opted_in_user_remains_scheduled_for_revalidation(share_env):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    assert user_id in models.get_users_with_share_enabled()


def test_periodic_revalidation_restores_suspended_user(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    config = models.set_ai_config(user_id, api_key="key", enabled=1)
    monkeypatch.setattr(
        web_server,
        "get_ai_config",
        lambda uid: {
            "base_url": "https://provider.example",
            "api_key": "key",
            "revision": config["revision"],
        },
    )
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True}, 200),
    )
    notices = []
    monkeypatch.setattr(
        web_server,
        "_notify_user",
        lambda uid, *args, **kwargs: notices.append(uid),
    )
    monkeypatch.setattr(web_server.time, "sleep", lambda seconds: None)

    web_server._run_ai_share_revalidation_once()

    settings = models.get_user_settings(user_id)
    assert settings["share_suspended"] == 0
    assert web_server.is_share_active(settings) is True
    assert notices == [user_id]


def _run_concurrent_connectivity_results(user_id: int, *calls):
    start = threading.Barrier(len(calls))
    results = []
    errors = []
    lock = threading.Lock()

    def run(args):
        try:
            models.close_db()
            start.wait(timeout=5)
            result = web_server._apply_share_connectivity_result(user_id, *args)
            with lock:
                results.append(result)
        except Exception as exc:  # pragma: no cover - surfaced below
            with lock:
                errors.append(exc)
        finally:
            models.close_db()

    threads = [threading.Thread(target=run, args=(args,)) for args in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    return results


def test_concurrent_failures_suspend_once_and_send_one_notification(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    notices = []
    notices_lock = threading.Lock()
    original_get_settings = web_server.get_user_settings
    both_reads = threading.Barrier(2)
    reads_lock = threading.Lock()
    reads = 0

    def synchronize_initial_read(uid):
        nonlocal reads
        settings = original_get_settings(uid)
        with reads_lock:
            reads += 1
            synchronize = reads <= 2
        if synchronize:
            both_reads.wait(timeout=5)
        return settings

    def record_notice(*args, **kwargs):
        with notices_lock:
            notices.append(args)

    monkeypatch.setattr(web_server, "get_user_settings", synchronize_initial_read)
    monkeypatch.setattr(web_server, "_notify_user", record_notice)

    results = _run_concurrent_connectivity_results(
        user_id,
        (False, "AI API HTTP 401", "2026-07-27T13:00:00"),
        (False, "AI API HTTP 401", "2026-07-27T13:00:01"),
    )

    assert sorted(results) == ["suspended", "unchanged"]
    assert [notice[1] for notice in notices] == ["share_suspended"]
    assert models.get_user_settings(user_id)["share_suspended"] == 1


def test_concurrent_successes_restore_once_and_send_one_notification(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    notices = []
    notices_lock = threading.Lock()
    original_get_settings = web_server.get_user_settings
    both_reads = threading.Barrier(2)
    reads_lock = threading.Lock()
    reads = 0

    def synchronize_initial_read(uid):
        nonlocal reads
        settings = original_get_settings(uid)
        with reads_lock:
            reads += 1
            synchronize = reads <= 2
        if synchronize:
            both_reads.wait(timeout=5)
        return settings

    def record_notice(*args, **kwargs):
        with notices_lock:
            notices.append(args)

    monkeypatch.setattr(web_server, "get_user_settings", synchronize_initial_read)
    monkeypatch.setattr(web_server, "_notify_user", record_notice)

    results = _run_concurrent_connectivity_results(
        user_id,
        (True, "", "2026-07-27T14:00:00"),
        (True, "", "2026-07-27T14:00:01"),
    )

    assert sorted(results) == ["restored", "unchanged"]
    assert [notice[1] for notice in notices] == ["share_restored"]
    assert models.get_user_settings(user_id)["share_suspended"] == 0


def test_opt_out_winning_race_prevents_transition_notification(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    notices = []
    original_get_settings = web_server.get_user_settings
    read_complete = threading.Event()
    release_transition = threading.Event()

    def pause_after_read(uid):
        settings = original_get_settings(uid)
        read_complete.set()
        assert release_transition.wait(timeout=5)
        return settings

    monkeypatch.setattr(web_server, "get_user_settings", pause_after_read)
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))
    result = []

    def apply_failure():
        try:
            models.close_db()
            result.append(web_server._apply_share_connectivity_result(user_id, False, "AI API HTTP 401"))
        finally:
            models.close_db()

    worker = threading.Thread(target=apply_failure)
    worker.start()
    assert read_complete.wait(timeout=5)
    models.set_user_settings(user_id, share_ai_results=0)
    release_transition.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert result == ["not_opted_in"]
    assert notices == []
    settings = models.get_user_settings(user_id)
    assert settings["share_ai_results"] == 0
    assert settings["share_suspended"] == 0


def test_share_error_drops_provider_body_and_redacts_common_credentials(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))
    secrets = [
        "bearer-secret-value",
        "header-secret-value",
        "provider-api-key-value",
        "query-key-value",
        "form-token-value",
        "non-sk-provider-key-value",
    ]
    raw_error = (
        "AI API HTTP 401: provider response body: "
        "Bearer bearer-secret-value; Authorization: Bearer header-secret-value; "
        "api_key=provider-api-key-value&key=query-key-value&token=form-token-value; "
        "x-api-key: non-sk-provider-key-value"
    )

    web_server._apply_share_connectivity_result(user_id, False, raw_error)

    persisted = models.get_user_settings(user_id)["share_last_check_error"]
    notification_body = notices[0][3]
    assert persisted == "AI API HTTP 401"
    for secret in secrets:
        assert secret not in persisted
        assert secret not in notification_body
    assert "provider response body" not in persisted


def test_saving_new_api_tests_and_restores_suspended_share(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True, "response": "pong"}, 200),
    )
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    response = client.put(
        "/ai/config",
        json={
            "provider": "OpenAI",
            "api_key": "replacement-key",
            "endpoint": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "provider_type": "openai",
            "enabled": 1,
        },
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200
    assert response.get_json()["share_check"] == {
        "status": "restored",
        "restored": True,
    }
    assert models.get_user_settings(user_id)["share_suspended"] == 0
    assert [notice[1] for notice in notices] == ["share_restored"]


def test_failed_saved_config_remains_saved_but_share_stays_suspended(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"error": "AI API HTTP 401"}, 502),
    )

    response = client.put(
        "/ai/config",
        json={"api_key": "still-invalid", "enabled": 1},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["has_api_key"] is True
    assert data["share_check"]["status"] == "unchanged"
    assert data["share_check"]["restored"] is False
    assert "401" in data["share_check"]["error"]
    assert models.get_user_settings(user_id)["share_suspended"] == 1


def test_manual_connection_test_can_restore(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True, "response": "pong"}, 200),
    )
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: None)

    response = client.post("/ai/test-connection", headers=auth_headers(user_id))

    assert response.status_code == 200
    assert response.get_json()["share_check"]["restored"] is True
    assert models.get_user_settings(user_id)["share_suspended"] == 0


def test_failed_share_enable_keeps_saved_preferences_and_pauses_existing_opt_in(
    share_env, monkeypatch
):
    client, user_id = share_env
    opted_in(user_id)
    models.set_ai_config(user_id, api_key="key", enabled=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"error": "AI API HTTP 401"}, 502),
    )

    response = client.put(
        "/settings",
        json={"share_ai_results": 1, "share_view_translation": 1},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 400
    assert response.get_json()["share_check"] == {
        "ok": False,
        "status": "paused",
        "error": "AI API HTTP 401",
    }
    settings = models.get_user_settings(user_id)
    assert settings["share_ai_results"] == 1
    assert settings["share_view_title"] == 1
    assert settings["share_view_translation"] == 0
    assert settings["share_view_summary"] == 1
    assert settings["share_suspended"] == 1


def test_share_enable_restores_prior_suspension_and_returns_active_state(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    models.set_ai_config(user_id, api_key="key", enabled=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True, "response": "pong"}, 200),
    )
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    response = client.put(
        "/settings",
        json={"share_ai_results": 1, "share_view_translation": 1},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["share_active"] is True
    assert data["share_suspended"] == 0
    assert data["share_view_title"] == 1
    assert data["share_view_translation"] == 1
    assert data["share_view_summary"] == 1
    assert [notice[1] for notice in notices] == ["share_restored"]


def test_manual_success_after_explicit_opt_out_does_not_restore_sharing(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    models.set_ai_config(user_id, api_key="key", enabled=1)
    disabled = client.put(
        "/settings",
        json={"share_ai_results": 0},
        headers=auth_headers(user_id),
    )
    assert disabled.status_code == 200
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True, "response": "pong"}, 200),
    )

    response = client.post("/ai/test-connection", headers=auth_headers(user_id))

    assert response.status_code == 200
    assert "share_check" not in response.get_json()
    settings = models.get_user_settings(user_id)
    assert settings["share_ai_results"] == 0
    assert settings["share_suspended"] == 0
    assert all(settings[key] == 0 for key in (
        "share_view_title", "share_view_translation", "share_view_summary",
    ))


def test_manual_connection_failure_redacts_provider_error_in_every_response_field(
    share_env, monkeypatch
):
    client, user_id = share_env
    opted_in(user_id)
    raw_error = (
        "AI API HTTP 401: provider response body: Bearer bearer-secret-value; "
        "api_key=provider-api-key-value&token=form-token-value"
    )
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"error": raw_error}, 502),
    )

    response = client.post("/ai/test-connection", headers=auth_headers(user_id))

    assert response.status_code == 502
    data = response.get_json()
    assert data["error"] == "AI API HTTP 401"
    assert data["share_check"]["error"] == "AI API HTTP 401"
    serialized = str(data)
    for secret in ("bearer-secret-value", "provider-api-key-value", "form-token-value"):
        assert secret not in serialized
    assert "provider response body" not in serialized


def test_first_share_enable_failure_reports_not_opted_in_not_paused(share_env, monkeypatch):
    client, user_id = share_env
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"error": "AI API HTTP 401"}, 502),
    )

    response = client.put(
        "/settings",
        json={"share_ai_results": 1},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 400
    assert response.get_json()["share_check"] == {
        "ok": False,
        "status": "not_opted_in",
        "error": "AI API HTTP 401",
    }
    assert models.get_user_settings(user_id) is None


def _config_revision(user_id: int) -> int:
    config = models.get_ai_config(user_id)
    if not config:
        config = models.set_ai_config(user_id, api_key="initial-key", enabled=1)
    return config["revision"]


def test_old_slow_failure_cannot_override_new_fast_success(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    old_revision = _config_revision(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    models.set_ai_config(user_id, api_key="replacement-key")
    new_revision = _config_revision(user_id)
    assert new_revision > old_revision
    assert web_server._apply_share_connectivity_result(
        user_id, True, config_revision=new_revision
    ) == "unchanged"
    before = models.get_user_settings(user_id)

    assert web_server._apply_share_connectivity_result(
        user_id, False, "AI API HTTP 401", config_revision=old_revision
    ) == "stale"
    assert models.get_user_settings(user_id) == before
    assert notices == []


def test_old_slow_success_cannot_override_new_fast_failure(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    old_revision = _config_revision(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    models.set_ai_config(user_id, api_key="replacement-key")
    new_revision = _config_revision(user_id)
    assert web_server._apply_share_connectivity_result(
        user_id, False, "AI API HTTP 401", config_revision=new_revision
    ) == "unchanged"
    before = models.get_user_settings(user_id)

    assert web_server._apply_share_connectivity_result(
        user_id, True, config_revision=old_revision
    ) == "stale"
    assert models.get_user_settings(user_id) == before
    assert notices == []


def test_old_manual_probe_after_new_save_is_stale(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    old_revision = _config_revision(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    models.set_ai_config(user_id, api_key="replacement-key")
    before = models.get_user_settings(user_id)
    share_check = web_server._share_check_after_personal_api_test(
        user_id, {"error": "AI API HTTP 401"}, 502, old_revision
    )

    assert share_check == {
        "status": "stale",
        "restored": False,
        "error": "AI API HTTP 401",
    }
    assert models.get_user_settings(user_id) == before
    assert notices == []


def test_old_manual_probe_after_opt_out_does_not_change_health_or_notify(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    old_revision = _config_revision(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))
    models.set_user_settings(user_id, share_ai_results=0, share_suspended=0)
    before = models.get_user_settings(user_id)

    share_check = web_server._share_check_after_personal_api_test(
        user_id, {"ok": True}, 200, old_revision
    )

    assert share_check is None
    assert models.get_user_settings(user_id) == before
    assert notices == []


def _validated_opted_in(user_id: int, *, suspended: int = 0) -> int:
    opted_in(user_id, suspended=suspended)
    config = models.set_ai_config(user_id, api_key="validated-key", enabled=1)
    assert web_server._apply_share_connectivity_result(
        user_id, not suspended, config_revision=config["revision"]
    ) in {"unchanged", "restored"}
    return config["revision"]


def test_new_config_save_is_effectively_inactive_until_its_probe_finishes(share_env, monkeypatch):
    client, user_id = share_env
    old_revision = _validated_opted_in(user_id)
    settings = models.get_user_settings(user_id)
    assert settings["share_last_check_revision"] == old_revision
    assert web_server.is_share_active(settings) is True
    probe_started = threading.Event()
    allow_probe = threading.Event()
    probe_response = {}
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    def delayed_probe(config):
        if config["api_key"] == "new-key":
            probe_started.set()
            assert allow_probe.wait(timeout=5)
        return {"ok": True, "response": "pong"}, 200

    def save_new_config():
        try:
            worker_client = web_server.app.test_client()
            probe_response["response"] = worker_client.put(
                "/ai/config",
                json={"api_key": "new-key", "enabled": 1},
                headers=auth_headers(user_id),
            )
        finally:
            models.close_db()

    monkeypatch.setattr(
        web_server,
        "_get_ai_result",
        lambda article_id: {"summary": "shared", "updated_at": "now"},
    )
    monkeypatch.setattr(web_server, "_run_ai_connection_test", delayed_probe)

    worker = threading.Thread(target=save_new_config)
    worker.start()
    assert probe_started.wait(timeout=5)

    settings = models.get_user_settings(user_id)
    assert settings["share_suspended"] == 0
    assert settings["share_last_check_revision"] == old_revision
    assert settings["share_current_config_revision"] > old_revision
    assert web_server.is_share_active(settings) is False
    assert client.get("/settings", headers=auth_headers(user_id)).get_json()["share_active"] is False
    assert client.get("/ai/result/42", headers=auth_headers(user_id)).get_json() == {
        "updated_at": "now"
    }
    assert notices == []

    allow_probe.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert probe_response["response"].status_code == 200
    assert probe_response["response"].get_json()["share_check"]["status"] == "unchanged"
    assert web_server.is_share_active(models.get_user_settings(user_id)) is True
    assert notices == []


def test_new_config_success_validates_its_revision_without_fake_restore_notice(share_env, monkeypatch):
    _, user_id = share_env
    old_revision = _validated_opted_in(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))
    config = models.set_ai_config(user_id, api_key="new-key")

    assert web_server._apply_share_connectivity_result(
        user_id, True, config_revision=config["revision"]
    ) == "unchanged"

    settings = models.get_user_settings(user_id)
    assert settings["share_last_check_revision"] == config["revision"]
    assert settings["share_last_check_revision"] > old_revision
    assert web_server.is_share_active(settings) is True
    assert notices == []


def test_new_config_failure_records_its_revision_and_pauses(share_env, monkeypatch):
    _, user_id = share_env
    _validated_opted_in(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))
    config = models.set_ai_config(user_id, api_key="new-key")

    assert web_server._apply_share_connectivity_result(
        user_id, False, "AI API HTTP 401", config_revision=config["revision"]
    ) == "suspended"

    settings = models.get_user_settings(user_id)
    assert settings["share_suspended"] == 1
    assert settings["share_last_check_ok"] == 0
    assert settings["share_last_check_revision"] == config["revision"]
    assert web_server.is_share_active(settings) is False
    assert [notice[1] for notice in notices] == ["share_suspended"]


def test_stale_manual_and_periodic_probes_cannot_block_current_settings_validation(
    share_env, monkeypatch
):
    client, user_id = share_env
    old_revision = _validated_opted_in(user_id)
    old_config = models.get_ai_config(user_id)
    new_config = models.set_ai_config(user_id, api_key="new-key")
    before = models.get_user_settings(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    manual = web_server._share_check_after_personal_api_test(
        user_id, {"error": "AI API HTTP 401"}, 502, old_revision
    )
    monkeypatch.setattr(web_server, "get_ai_config", lambda uid: old_config)
    monkeypatch.setattr(web_server, "_run_ai_connection_test", lambda config: ({"ok": True}, 200))
    monkeypatch.setattr(web_server.time, "sleep", lambda seconds: None)
    web_server._run_ai_share_revalidation_once()

    assert manual["status"] == "stale"
    assert models.get_user_settings(user_id) == before
    assert notices == []

    monkeypatch.setattr(web_server, "get_ai_config", models.get_ai_config)
    response = client.put(
        "/settings",
        json={"share_ai_results": 1},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200
    assert models.get_user_settings(user_id)["share_last_check_revision"] == new_config["revision"]
    assert response.get_json()["share_active"] is True


def test_settings_enable_rejects_config_revision_changed_during_validation(
    share_env, monkeypatch
):
    client, user_id = share_env
    old_config = models.set_ai_config(user_id, api_key="old-key", enabled=1)
    models.set_user_settings(
        user_id,
        share_ai_results=0,
        share_view_title=0,
        share_view_translation=0,
        share_view_summary=0,
        share_suspended=0,
        share_last_check_ok=0,
    )
    probe_started = threading.Event()
    allow_probe = threading.Event()
    enable_response = {}
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    def delayed_probe(config):
        assert config["revision"] == old_config["revision"]
        probe_started.set()
        assert allow_probe.wait(timeout=5)
        return {"ok": True, "response": "pong"}, 200

    def enable_sharing():
        try:
            worker_client = web_server.app.test_client()
            enable_response["response"] = worker_client.put(
                "/settings",
                json={"share_ai_results": 1, "share_view_title": 1},
                headers=auth_headers(user_id),
            )
        finally:
            models.close_db()

    monkeypatch.setattr(web_server, "_run_ai_connection_test", delayed_probe)
    worker = threading.Thread(target=enable_sharing)
    worker.start()
    assert probe_started.wait(timeout=5)

    saved = client.put(
        "/ai/config",
        json={"api_key": "new-key", "enabled": 1},
        headers=auth_headers(user_id),
    )
    assert saved.status_code == 200
    assert "share_check" not in saved.get_json()
    assert models.get_ai_config(user_id)["revision"] > old_config["revision"]

    allow_probe.set()
    worker.join(timeout=10)
    assert not worker.is_alive()

    response = enable_response["response"]
    assert response.status_code == 409
    assert response.get_json()["share_check"] == {
        "ok": False,
        "status": "stale_validation",
        "error": "AI config changed during validation; retry enabling sharing",
    }
    settings = models.get_user_settings(user_id)
    assert settings["share_ai_results"] == 0
    assert settings["share_view_title"] == 0
    assert settings["share_suspended"] == 0
    assert web_server.is_share_active(settings) is False
    assert notices == []
