"""Route contracts for the admin site-wide notification broadcast."""

import os
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import models
import web_server


@pytest.fixture
def env(monkeypatch):
    db_path = Path(__file__).resolve().parents[1] / f"tmp-broadcast-{uuid.uuid4().hex}.db"
    old_db_file, old_conn = models.DB_FILE, models._db
    models.DB_FILE = db_path
    models._db = None
    models.get_db()
    admin = models.create_user("admin@example.com", "pw", "admin", role="admin")
    u1 = models.create_user("u1@example.com", "pw", "u1")
    u2 = models.create_user("u2@example.com", "pw", "u2")
    # Emails are best-effort; never actually send during tests.
    monkeypatch.setattr(web_server, "_send_notification_email", lambda *a, **k: True)
    client = web_server.app.test_client()
    try:
        yield client, admin["id"], u1["id"], u2["id"]
    finally:
        models.DB_FILE, models._db = old_db_file, old_conn
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


def _headers(user_id, role):
    return {"Authorization": f"Bearer {web_server.create_token(user_id, role)}"}


def test_broadcast_reaches_every_user(env):
    client, admin_id, u1, u2 = env
    resp = client.post(
        "/admin/notifications/broadcast",
        json={"title": "公告", "body": "# 大标题\n正文", "format": "markdown"},
        headers=_headers(admin_id, "admin"),
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] and data["recipients"] == 3 and data["email"] is False
    for uid in (admin_id, u1, u2):
        items = models.list_notifications(uid)
        assert [i["title"] for i in items] == ["公告"]
        assert items[0]["format"] == "markdown"
        assert models.count_unread_notifications(uid) == 1


def test_broadcast_requires_admin(env):
    client, admin_id, u1, u2 = env
    resp = client.post(
        "/admin/notifications/broadcast",
        json={"title": "x", "body": "y"},
        headers=_headers(u1, "user"),
    )
    assert resp.status_code == 403
    assert models.count_unread_notifications(u1) == 0


def test_broadcast_validates_title_and_body(env):
    client, admin_id, u1, u2 = env
    for payload in ({"title": "", "body": "y"}, {"title": "x", "body": "   "}):
        resp = client.post(
            "/admin/notifications/broadcast",
            json=payload,
            headers=_headers(admin_id, "admin"),
        )
        assert resp.status_code == 400


def test_broadcast_unknown_format_falls_back_to_plain(env):
    client, admin_id, u1, u2 = env
    resp = client.post(
        "/admin/notifications/broadcast",
        json={"title": "t", "body": "b", "format": "bogus"},
        headers=_headers(admin_id, "admin"),
    )
    assert resp.status_code == 200
    assert models.list_notifications(u1)[0]["format"] == "plain"


def test_broadcast_rejects_non_string_title_or_body_instead_of_crashing(env):
    # Regression test: title/body used to go through `x or ""` before being
    # .strip()'d — a truthy non-string (e.g. a dict) slipped past that guard
    # and crashed with a 500 instead of a clean 400.
    client, admin_id, u1, u2 = env
    for payload in (
        {"title": {"unexpected": True}, "body": "y"},
        {"title": "x", "body": {"unexpected": True}},
        {"title": ["x"], "body": "y"},
    ):
        resp = client.post(
            "/admin/notifications/broadcast",
            json=payload,
            headers=_headers(admin_id, "admin"),
        )
        assert resp.status_code == 400, resp.get_data(as_text=True)
    assert models.count_unread_notifications(u1) == 0


def test_broadcast_rejects_non_bool_email_flag(env):
    # Regression test: bool("false") is True in Python — a string "false"
    # sent by a non-checkbox caller used to be silently truthy and could
    # trigger an unwanted mass email. Must be rejected, not coerced.
    client, admin_id, u1, u2 = env
    resp = client.post(
        "/admin/notifications/broadcast",
        json={"title": "t", "body": "b", "email": "false"},
        headers=_headers(admin_id, "admin"),
    )
    assert resp.status_code == 400
    assert models.count_unread_notifications(u1) == 0


def test_broadcast_rejects_non_string_format(env):
    client, admin_id, u1, u2 = env
    resp = client.post(
        "/admin/notifications/broadcast",
        json={"title": "t", "body": "b", "format": 123},
        headers=_headers(admin_id, "admin"),
    )
    assert resp.status_code == 400


def test_broadcast_rejects_non_object_top_level_json(env):
    # Regression test: `request.get_json(silent=True) or {}` doesn't catch a
    # syntactically valid JSON body that isn't an object — a top-level list,
    # number, or string is truthy, so it passed through to `.get()` and
    # crashed with a 500 (AttributeError: 'list'/'int'/'str' has no .get).
    client, admin_id, u1, u2 = env
    for top_level in ([1], 42, "unexpected"):
        resp = client.post(
            "/admin/notifications/broadcast",
            json=top_level,
            headers=_headers(admin_id, "admin"),
        )
        assert resp.status_code == 400, (top_level, resp.status_code, resp.get_data(as_text=True))
    assert models.count_unread_notifications(u1) == 0


def test_broadcast_retry_with_same_broadcast_id_is_not_duplicated(env):
    # Regression test: the server used to write notifications (and queue
    # emails) and only afterwards return a response — if that response was
    # lost in transit, an admin retry would fan out the exact same broadcast
    # a second time. Replaying the same broadcast_id must be a no-op that
    # just re-reports the original outcome.
    client, admin_id, u1, u2 = env
    payload = {
        "title": "公告",
        "body": "正文",
        "format": "markdown",
        "email": True,
        "broadcast_id": "bcast-fixed-1",
    }
    first = client.post("/admin/notifications/broadcast", json=payload, headers=_headers(admin_id, "admin"))
    assert first.status_code == 200
    first_data = first.get_json()
    assert first_data["ok"] and first_data["recipients"] == 3 and "replayed" not in first_data

    second = client.post("/admin/notifications/broadcast", json=payload, headers=_headers(admin_id, "admin"))
    assert second.status_code == 200
    second_data = second.get_json()
    assert second_data == {"ok": True, "recipients": 3, "email": True, "replayed": True}

    # Still exactly one notification per user, not two.
    for uid in (admin_id, u1, u2):
        items = models.list_notifications(uid)
        assert [i["title"] for i in items] == ["公告"]
        assert models.count_unread_notifications(uid) == 1


def test_broadcast_replay_ignores_a_different_payload_under_the_same_id(env):
    # A retry that races and somehow carries a mutated payload must not
    # overwrite/re-publish under the same id — the first committed content
    # wins, full stop.
    client, admin_id, u1, u2 = env
    first_payload = {"title": "原始标题", "body": "原始正文", "broadcast_id": "bcast-fixed-2"}
    client.post("/admin/notifications/broadcast", json=first_payload, headers=_headers(admin_id, "admin"))

    mutated_payload = {"title": "被篡改的标题", "body": "被篡改的正文", "broadcast_id": "bcast-fixed-2"}
    resp = client.post("/admin/notifications/broadcast", json=mutated_payload, headers=_headers(admin_id, "admin"))
    assert resp.status_code == 200
    assert resp.get_json()["replayed"] is True

    assert models.list_notifications(u1)[0]["title"] == "原始标题"


def test_broadcast_without_broadcast_id_still_publishes(env):
    # Backwards-compatible: an omitted broadcast_id just means no replay
    # protection for that call (server mints one internally), not a failure.
    client, admin_id, u1, u2 = env
    resp = client.post(
        "/admin/notifications/broadcast",
        json={"title": "t", "body": "b"},
        headers=_headers(admin_id, "admin"),
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] and data["recipients"] == 3 and "replayed" not in data


def test_atomic_model_broadcast_replays_its_committed_result(env):
    _, admin_id, u1, u2 = env
    first, result = models.publish_broadcast_atomically(
        [admin_id, u1, u2], "direct-1", "t", "b", "plain", True,
    )
    assert first is True
    assert result == {"recipients": 3, "email": True}

    replayed, replay_result = models.publish_broadcast_atomically(
        [admin_id, u1, u2], "direct-1", "changed", "changed", "markdown", False,
    )
    assert replayed is False
    assert replay_result == result
    row = models.get_broadcast_publication("direct-1")
    assert row["title"] == "t"
