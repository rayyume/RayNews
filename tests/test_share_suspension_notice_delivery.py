"""A share suspension must reach the user on both channels, not just in-app.

The existing share-recovery tests stub _notify_user wholesale, so they prove the
transition asks for a notice but say nothing about what actually gets delivered.
These drive the real _notify_user: an in-app row AND an email, with only the
outbound HTTP call faked.
"""

import json
import os
import uuid
from pathlib import Path

import pytest

import models
import notifier
import web_server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def share_user(monkeypatch):
    db_path = ROOT / f"tmp-share-notice-{uuid.uuid4().hex}.db"
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = db_path
    models.get_db()
    user = models.create_user("share@example.com", "pw", "share-user")
    # A real personal AI config: is_share_active() compares the revision the last
    # check ran against with the config's current one, so a user with no config
    # row can never read as active regardless of the check result.
    config = models.set_ai_config(
        user["id"], api_key="sk-test", endpoint="https://api.example.com/v1",
        model="gpt-x", provider_type="openai", enabled=1,
    )
    models.set_user_settings(
        user["id"],
        share_ai_results=1,
        share_view_title=1,
        share_view_summary=1,
        share_suspended=0,
        share_last_check_ok=1,
        share_last_check_revision=config["revision"],
    )
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    try:
        yield user["id"]
    finally:
        models.close_db()
        models.DB_FILE = old_db_file
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


@pytest.fixture
def sent_emails(monkeypatch):
    """Capture at the Resend boundary so the real notifier code still runs."""
    sent = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "email-1"}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append({"to": json["to"], "subject": json["subject"], "html": json["html"]})
        return FakeResponse()

    monkeypatch.setattr(notifier.requests, "post", fake_post)
    return sent


def _suspend(user_id, error="AI API HTTP 401 invalid api key"):
    return web_server._apply_share_connectivity_result(
        user_id, False, error, "2026-07-27T10:00:00"
    )


def test_a_failed_check_suspends_sharing_and_delivers_both_channels(share_user, sent_emails):
    assert _suspend(share_user) == "suspended"

    settings = models.get_user_settings(share_user)
    assert settings["share_suspended"] == 1
    assert settings["share_ai_results"] == 1        # intent preserved
    assert web_server.is_share_active(settings) is False

    items = models.list_notifications(share_user)
    assert [i["type"] for i in items] == ["share_suspended"]
    assert "共享" in items[0]["title"]
    assert "用户设置 → AI" in items[0]["body"]
    assert models.count_unread_notifications(share_user) == 1

    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == ["share@example.com"]   # falls back to the account address
    assert "共享" in sent_emails[0]["subject"]


def test_the_notification_address_wins_over_the_account_address(share_user, sent_emails):
    models.set_user_settings(
        share_user,
        notification_config=json.dumps({"resend": {"to_email": "inbox@example.com"}}),
    )

    _suspend(share_user)

    assert sent_emails[0]["to"] == ["inbox@example.com"]


def test_recovery_suspends_nothing_and_tells_the_user_on_both_channels(share_user, sent_emails):
    _suspend(share_user)
    sent_emails.clear()

    result = web_server._apply_share_connectivity_result(
        share_user, True, "", "2026-07-27T16:00:00"
    )

    assert result == "restored"
    settings = models.get_user_settings(share_user)
    assert settings["share_suspended"] == 0
    assert web_server.is_share_active(settings) is True
    assert [i["type"] for i in models.list_notifications(share_user)][0] == "share_restored"
    assert len(sent_emails) == 1


def test_a_still_failing_check_does_not_re_notify(share_user, sent_emails):
    _suspend(share_user)
    _suspend(share_user, "AI API HTTP 401 invalid api key")

    assert len(models.list_notifications(share_user)) == 1
    assert len(sent_emails) == 1


def test_a_mail_failure_still_leaves_the_in_app_notice(share_user, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("resend down")

    monkeypatch.setattr(notifier.requests, "post", boom)

    assert _suspend(share_user) == "suspended"
    assert models.get_user_settings(share_user)["share_suspended"] == 1
    assert [i["type"] for i in models.list_notifications(share_user)] == ["share_suspended"]


def test_no_resend_key_configured_still_suspends_and_notifies_in_app(share_user, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    assert _suspend(share_user) == "suspended"
    assert models.get_user_settings(share_user)["share_suspended"] == 1
    assert [i["type"] for i in models.list_notifications(share_user)] == ["share_suspended"]


def test_the_revalidation_loop_reaches_a_suspended_user_and_restores_it(share_user, sent_emails,
                                                                       monkeypatch):
    # The whole point of keeping suspended users scheduled: a key that starts
    # working again must restore sharing without the user doing anything.
    _suspend(share_user)
    sent_emails.clear()

    monkeypatch.setattr(web_server, "_run_ai_connection_test", lambda config: ({"ok": True}, 200))
    web_server._run_ai_share_revalidation_once()

    settings = models.get_user_settings(share_user)
    assert settings["share_suspended"] == 0
    assert [i["type"] for i in models.list_notifications(share_user)][0] == "share_restored"
    assert len(sent_emails) == 1


def test_revalidation_interval_defaults_to_hourly_and_never_goes_hot(monkeypatch):
    # The loop is the only detector of a bad personal key when every AI job runs
    # on the system config, so the interval is the detection delay.
    monkeypatch.delenv("AI_SHARE_REVALIDATION_INTERVAL_HOURS", raising=False)
    assert web_server._share_revalidation_interval_seconds() == 3600

    monkeypatch.setenv("AI_SHARE_REVALIDATION_INTERVAL_HOURS", "0.5")
    assert web_server._share_revalidation_interval_seconds() == 1800

    for bad in ("0", "-4", "", "abc"):
        monkeypatch.setenv("AI_SHARE_REVALIDATION_INTERVAL_HOURS", bad)
        assert web_server._share_revalidation_interval_seconds() >= 300
