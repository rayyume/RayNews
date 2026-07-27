"""Regression coverage for durable shared-AI suspension state."""

import os
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
        ({"share_ai_results": 1, "share_suspended": 0, "share_last_check_ok": 1}, True),
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
    monkeypatch.setattr(
        web_server,
        "get_ai_config",
        lambda uid: {"base_url": "https://provider.example", "api_key": "key"},
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
