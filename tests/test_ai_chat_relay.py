"""Contracts for the same-origin /ai/chat relay that backs CORS-blocked AI providers."""

import pytest

import models
import web_server


@pytest.fixture
def client(monkeypatch):
    users = {
        101: {"id": 101, "role": "user"},
        202: {"id": 202, "role": "admin"},
        303: {"id": 303, "role": "auditor"},
    }
    monkeypatch.setattr(models, "get_user", lambda user_id: users.get(user_id))
    monkeypatch.setattr(models, "record_access", lambda user_id: None)
    return web_server.app.test_client()


def _auth_headers(user_id, role):
    token = web_server.create_token(user_id, role)
    return {"Authorization": f"Bearer {token}"}


CONFIG = {
    "api_key": "sk-secret",
    "endpoint": "https://opencode.ai/zen/go/v1",
    "model": "opencode-go/deepseek-v4-pro",
    "provider_type": "openai",
    "enabled": 1,
}


class StubService:
    last = None

    def __init__(self, **kwargs):
        StubService.last = kwargs
        self.kwargs = kwargs

    def chat(self, messages, max_tokens=2000, temperature=0.3):
        StubService.call = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        return "relayed answer"


def _patch(monkeypatch, config=CONFIG, service=StubService):
    monkeypatch.setattr(web_server, "get_ai_config", lambda user_id: config)
    monkeypatch.setattr(web_server, "AIService", service)


def test_relay_requires_authentication(client):
    resp = client.post("/ai/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 401


def test_relay_forwards_messages_and_hides_api_key(client, monkeypatch):
    _patch(monkeypatch)
    resp = client.post(
        "/ai/chat",
        headers=_auth_headers(101, "user"),
        json={
            "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "ping"}],
            "max_tokens": 1234,
            "temperature": 0.1,
        },
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"content": "relayed answer"}
    # Server built the service from the caller's stored config (key never came from client).
    assert StubService.last["api_key"] == "sk-secret"
    assert StubService.call["max_tokens"] == 1234
    assert len(StubService.call["messages"]) == 2
    # The api_key must never be echoed back to the browser.
    assert "sk-secret" not in resp.get_data(as_text=True)


def test_relay_clamps_absurd_max_tokens(client, monkeypatch):
    _patch(monkeypatch)
    resp = client.post(
        "/ai/chat",
        headers=_auth_headers(101, "user"),
        json={"messages": [{"role": "user", "content": "x"}], "max_tokens": 10_000_000},
    )
    assert resp.status_code == 200
    assert StubService.call["max_tokens"] == 8000


def test_relay_rejects_missing_or_malformed_messages(client, monkeypatch):
    _patch(monkeypatch)
    for bad in ({}, {"messages": []}, {"messages": [{"role": "user"}]},
                {"messages": [{"content": "x"}]}, {"messages": "hi"}):
        resp = client.post("/ai/chat", headers=_auth_headers(101, "user"), json=bad)
        assert resp.status_code == 400, bad


def test_relay_requires_configured_and_enabled_ai(client, monkeypatch):
    monkeypatch.setattr(web_server, "get_ai_config", lambda user_id: None)
    resp = client.post("/ai/chat", headers=_auth_headers(101, "user"),
                       json={"messages": [{"role": "user", "content": "x"}]})
    assert resp.status_code == 400

    disabled = dict(CONFIG, enabled=0)
    monkeypatch.setattr(web_server, "get_ai_config", lambda user_id: disabled)
    resp = client.post("/ai/chat", headers=_auth_headers(101, "user"),
                       json={"messages": [{"role": "user", "content": "x"}]})
    assert resp.status_code == 400


def test_relay_maps_upstream_timeout_to_504(client, monkeypatch):
    class TimingOut:
        def __init__(self, **kwargs):
            pass

        def chat(self, *a, **k):
            raise TimeoutError("AI 服务响应超时")

    _patch(monkeypatch, service=TimingOut)
    resp = client.post("/ai/chat", headers=_auth_headers(101, "user"),
                       json={"messages": [{"role": "user", "content": "x"}]})
    assert resp.status_code == 504


def test_relay_empty_completion_is_502(client, monkeypatch):
    class Empty:
        def __init__(self, **kwargs):
            pass

        def chat(self, *a, **k):
            return "   "

    _patch(monkeypatch, service=Empty)
    resp = client.post("/ai/chat", headers=_auth_headers(101, "user"),
                       json={"messages": [{"role": "user", "content": "x"}]})
    assert resp.status_code == 502
