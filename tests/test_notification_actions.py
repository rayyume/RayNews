"""Authenticated action-route contracts for in-app notifications."""

import os
import uuid
from pathlib import Path

import pytest

import models
import web_server


@pytest.fixture
def env():
    db_path = Path(__file__).resolve().parents[1] / f"tmp-notification-actions-{uuid.uuid4().hex}.db"
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = db_path
    models.get_db()
    user_a = models.create_user("a@example.com", "pw", "A")
    user_b = models.create_user("b@example.com", "pw", "B")
    client = web_server.app.test_client()
    try:
        yield client, user_a["id"], user_b["id"]
    finally:
        models.close_db()
        models.DB_FILE = old_db_file
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


def headers(user_id):
    return {"Authorization": f"Bearer {web_server.create_token(user_id, 'user')}"}


def test_notification_actions_require_auth(env):
    client, _, _ = env
    assert client.post("/notifications/read-all").status_code == 401
    assert client.delete("/notifications/1").status_code == 401
    assert client.delete("/notifications").status_code == 401


def test_read_all_and_delete_routes_are_scoped_to_the_current_user(env):
    client, user_a, user_b = env
    a_unread = models.add_notification(user_a, "general", "A 未读", "")
    a_delete = models.add_notification(user_a, "general", "A 删除", "")
    b_id = models.add_notification(user_b, "general", "B", "")

    read_all = client.post("/notifications/read-all", headers=headers(user_a))
    assert read_all.get_json() == {"ok": True, "unread": 0}
    assert models.count_unread_notifications(user_b) == 1

    foreign = client.delete(f"/notifications/{b_id}", headers=headers(user_a))
    assert foreign.get_json() == {"ok": True, "unread": 0}
    assert models.list_notifications(user_b)[0]["id"] == b_id

    deleted = client.delete(f"/notifications/{a_delete}", headers=headers(user_a))
    assert deleted.get_json() == {"ok": True, "unread": 0}
    assert a_unread in [row["id"] for row in models.list_notifications(user_a)]


def test_delete_all_is_idempotent_and_leaves_other_users_notifications(env):
    client, user_a, user_b = env
    models.add_notification(user_a, "general", "A", "")
    b_id = models.add_notification(user_b, "general", "B", "")

    assert client.delete("/notifications", headers=headers(user_a)).get_json() == {"ok": True, "unread": 0}
    assert client.delete("/notifications", headers=headers(user_a)).get_json() == {"ok": True, "unread": 0}
    assert models.list_notifications(user_a) == []
    assert models.list_notifications(user_b)[0]["id"] == b_id
