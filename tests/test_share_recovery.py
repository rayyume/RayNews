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
