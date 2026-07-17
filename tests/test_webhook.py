"""Behavioral contracts for the Telegram serverless webhook receiver."""

import pytest

import web_server


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(web_server, "TELEGRAM_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setattr(web_server, "_WEBHOOK_CHANNEL", "raysrss")
    monkeypatch.setattr(web_server, "_webhook_rate_hits", [])
    return web_server.app.test_client()


def _headers(token="test-secret"):
    return {"X-RayNews-Webhook-Token": token, "Content-Type": "application/json"}


def _channel_post(message_id=42, username="raysrss"):
    return {
        "channel_post": {
            "message_id": message_id,
            "chat": {"username": username},
            "text": "hello",
        }
    }


def test_webhook_disabled_returns_404_when_secret_unset(monkeypatch):
    monkeypatch.setattr(web_server, "TELEGRAM_WEBHOOK_SECRET", "")
    client = web_server.app.test_client()

    response = client.post(
        "/webhook/telegram", json=_channel_post(), headers=_headers()
    )

    assert response.status_code == 404


def test_webhook_rejects_missing_token(client):
    response = client.post("/webhook/telegram", json=_channel_post())
    assert response.status_code == 403


def test_webhook_rejects_wrong_token(client):
    response = client.post(
        "/webhook/telegram", json=_channel_post(), headers=_headers("wrong-token")
    )
    assert response.status_code == 403


def test_webhook_rejects_invalid_json(client):
    response = client.post(
        "/webhook/telegram",
        data="not json",
        headers=_headers(),
    )
    assert response.status_code == 400


def test_webhook_ignores_non_channel_update(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        web_server.requests, "post", lambda *a, **k: calls.append((a, k))
    )

    response = client.post(
        "/webhook/telegram",
        json={"message": {"message_id": 1, "chat": {"username": "raysrss"}}},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "ignored"}
    assert calls == []


def test_webhook_ignores_wrong_channel(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        web_server.requests, "post", lambda *a, **k: calls.append((a, k))
    )

    response = client.post(
        "/webhook/telegram",
        json=_channel_post(username="someone_else"),
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "ignored"}
    assert calls == []


class StubResponse:
    status_code = 200

    def raise_for_status(self):
        pass


def test_webhook_accepts_valid_channel_post_and_triggers_refresh(client, monkeypatch):
    calls = []

    def fake_post(url, timeout=None, params=None):
        calls.append((url, timeout, params))
        return StubResponse()

    monkeypatch.setattr(web_server.requests, "post", fake_post)

    response = client.post(
        "/webhook/telegram", json=_channel_post(message_id=99), headers=_headers()
    )

    assert response.status_code == 202
    assert response.get_json() == {"status": "accepted", "message_id": 99}
    assert calls == [("http://127.0.0.1:8081/refresh?trigger=webhook", 5, None)]


def test_webhook_edited_post_calls_refetch_endpoint(client, monkeypatch):
    calls = []

    def fake_post(url, timeout=None, params=None):
        calls.append((url, timeout, params))
        return StubResponse()

    monkeypatch.setattr(web_server.requests, "post", fake_post)

    response = client.post(
        "/webhook/telegram",
        json={
            "edited_channel_post": {
                "message_id": 55,
                "chat": {"username": "raysrss"},
                "text": "edited",
            }
        },
        headers=_headers(),
    )

    assert response.status_code == 202
    assert calls == [
        ("http://127.0.0.1:8081/internal/refetch-post", 30, {"id": 55}),
    ]


def test_webhook_refetch_busy_is_treated_as_accepted(client, monkeypatch):
    class Busy(StubResponse):
        status_code = 409

        def raise_for_status(self):
            raise AssertionError("should not raise for 409")

    monkeypatch.setattr(web_server.requests, "post", lambda *a, **k: Busy())

    response = client.post(
        "/webhook/telegram",
        json={
            "edited_channel_post": {
                "message_id": 55,
                "chat": {"username": "raysrss"},
            }
        },
        headers=_headers(),
    )

    assert response.status_code == 202


def test_webhook_upstream_failure_returns_502(client, monkeypatch):
    def fail_post(*args, **kwargs):
        raise web_server.requests.RequestException("upstream down")

    monkeypatch.setattr(web_server.requests, "post", fail_post)

    response = client.post(
        "/webhook/telegram", json=_channel_post(), headers=_headers()
    )

    assert response.status_code == 502


def test_webhook_rate_limit_blocks_after_budget_exhausted(client, monkeypatch):
    monkeypatch.setattr(web_server.requests, "post", lambda *a, **k: StubResponse())

    for i in range(web_server._WEBHOOK_RATE_LIMIT):
        response = client.post(
            "/webhook/telegram", json=_channel_post(message_id=i), headers=_headers()
        )
        assert response.status_code == 202

    response = client.post(
        "/webhook/telegram", json=_channel_post(message_id=999), headers=_headers()
    )
    assert response.status_code == 429
