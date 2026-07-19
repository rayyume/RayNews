"""Contracts for authenticated translation-completion update notifications."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import models
import web_server


@pytest.fixture
def client(monkeypatch, tmp_path):
    db_path = tmp_path / "news.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))

    users = {
        101: {"id": 101, "role": "user"},
        303: {"id": 303, "role": "auditor"},
    }
    monkeypatch.setattr(models, "get_user", lambda user_id: users.get(user_id))
    monkeypatch.setattr(models, "record_access", lambda user_id: None)
    return web_server.app.test_client()


def _auth_headers(user_id=101, role="user"):
    return {"Authorization": f"Bearer {web_server.create_token(user_id, role)}"}


def test_translation_updates_publish_completed_translations_without_touching_summary_updates(client):
    web_server._init_ai_results_table()
    conn = sqlite3.connect(web_server.NEWS_DB)
    conn.execute("INSERT INTO ai_results (article_id) VALUES (42)")
    conn.commit()
    conn.close()

    web_server._save_ai_result(
        42,
        translation=json.dumps({"title": "中文", "html": "<p>译文</p>"}),
    )
    conn = sqlite3.connect(web_server.NEWS_DB)
    translation_updated_at = conn.execute(
        "SELECT translation_updated_at FROM ai_results WHERE article_id = 42"
    ).fetchone()[0]
    conn.close()
    assert translation_updated_at

    first = client.get(
        "/ai/translation-updates",
        query_string={"since": "2000-01-01 00:00:00|0"},
        headers=_auth_headers(),
    )
    assert first.status_code == 200
    assert first.get_json()["items"] == [{"id": 42}]
    cursor = first.get_json()["cursor"]
    assert cursor

    second = client.get(
        "/ai/translation-updates",
        query_string={"since": cursor},
        headers=_auth_headers(),
    )
    assert second.status_code == 200
    assert second.get_json()["items"] == []

    web_server._save_ai_result(42, summary="摘要")
    conn = sqlite3.connect(web_server.NEWS_DB)
    unchanged = conn.execute(
        "SELECT translation_updated_at FROM ai_results WHERE article_id = 42"
    ).fetchone()[0]
    conn.close()
    assert unchanged == translation_updated_at


def test_translation_updates_require_reader_authorization_and_reject_bad_cursors(client):
    unauthenticated = client.get("/ai/translation-updates")
    assert unauthenticated.status_code == 401

    forbidden = client.get(
        "/ai/translation-updates",
        headers=_auth_headers(303, "auditor"),
    )
    assert forbidden.status_code == 403

    for cursor in ("definitely-not-a-cursor|0", "2026-07-19 10:00:00|not-an-id"):
        malformed = client.get(
            "/ai/translation-updates",
            query_string={"since": cursor},
            headers=_auth_headers(),
        )
        assert malformed.status_code == 400
        assert malformed.get_json()["error"] == "invalid cursor"
