"""Persistence boundary tests for server-side AI endpoint safety."""

import socket

import pytest

import models
import web_server


@pytest.fixture
def ai_config_env(tmp_path):
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = tmp_path / "ai-endpoint-validation.db"
    models.get_db()
    user = models.create_user("user@example.com", "pw", "user")
    admin = models.create_user("admin@example.com", "pw", "admin", role="admin")
    client = web_server.app.test_client()
    try:
        yield client, user["id"], admin["id"]
    finally:
        models.close_db()
        models.DB_FILE = old_db_file


def _headers(user_id, role):
    return {"Authorization": f"Bearer {web_server.create_token(user_id, role)}"}


def _public_dns(*args, **kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))
    ]


def test_personal_config_rejects_private_endpoint_without_persisting(ai_config_env):
    client, user_id, _ = ai_config_env
    models.set_ai_config(user_id, endpoint="https://api.openai.com/v1", model="before")

    response = client.put(
        "/ai/config",
        headers=_headers(user_id, "user"),
        json={"endpoint": "http://127.0.0.1:11434/v1", "model": "after"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "AI endpoint must be a public HTTP(S) URL"}
    persisted = models.get_ai_config(user_id)
    assert persisted["endpoint"] == "https://api.openai.com/v1"
    assert persisted["model"] == "before"


def test_system_config_rejects_private_endpoint_without_persisting(ai_config_env):
    client, _, admin_id = ai_config_env
    models.set_system_ai_config(endpoint="https://api.openai.com/v1", model="before")

    response = client.put(
        "/admin/system-ai-config",
        headers=_headers(admin_id, "admin"),
        json={"endpoint": "http://127.0.0.1:11434/v1", "model": "after"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "AI endpoint must be a public HTTP(S) URL"}
    persisted = models.get_system_ai_config()
    assert persisted["endpoint"] == "https://api.openai.com/v1"
    assert persisted["model"] == "before"


def test_personal_config_accepts_public_hostname_endpoint(ai_config_env, monkeypatch):
    client, user_id, _ = ai_config_env
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)

    response = client.put(
        "/ai/config",
        headers=_headers(user_id, "user"),
        json={"endpoint": "https://provider.example/v1", "model": "public-model"},
    )

    assert response.status_code == 200
    assert models.get_ai_config(user_id)["endpoint"] == "https://provider.example/v1"


def test_system_config_accepts_public_hostname_endpoint(ai_config_env, monkeypatch):
    client, _, admin_id = ai_config_env
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)

    response = client.put(
        "/admin/system-ai-config",
        headers=_headers(admin_id, "admin"),
        json={"endpoint": "https://provider.example/v1", "model": "public-model"},
    )

    assert response.status_code == 200
    assert models.get_system_ai_config()["endpoint"] == "https://provider.example/v1"
