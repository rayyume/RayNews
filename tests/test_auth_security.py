"""Security regressions for registration, login, and invite throttling."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import models
import notifier
import web_server


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = tmp_path / "auth-security.db"
    models.get_db()
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("RAYNEWS_ADMIN_EMAIL", "admin@example.com")
    client = web_server.app.test_client()
    try:
        yield client
    finally:
        models.close_db()
        models.DB_FILE = old_db_file


def _create_login_user():
    user = models.create_user(
        "reader@example.com",
        "correct-password",
        "reader",
        role="user",
    )
    assert user is not None
    return user


def _failed_login(client, *, remote_addr="198.51.100.10", real_ip=None):
    headers = {"X-Real-IP": real_ip} if real_ip else {}
    return client.post(
        "/auth/login",
        json={"login": "reader@example.com", "password": "wrong-password"},
        headers=headers,
        environ_base={"REMOTE_ADDR": remote_addr},
    )


def test_concurrent_initial_registration_creates_only_one_admin(auth_env):
    barrier = threading.Barrier(2)

    def register(index):
        try:
            barrier.wait(timeout=5)
            return models.create_registered_user(
                f"first{index}@example.com",
                "correct-password",
                f"first{index}",
                "",
            )
        finally:
            models.close_db()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, (1, 2)))

    successful = [user for user, _is_initial_admin in results if user]
    assert [user["role"] for user in successful] == ["admin"]
    assert models.count_users() == 1


def test_login_locks_after_five_failed_attempts(auth_env):
    client = auth_env
    _create_login_user()

    for _ in range(5):
        assert _failed_login(client).status_code == 401

    locked = _failed_login(client)
    assert locked.status_code == 429
    assert locked.get_json()["retry_after"] > 0
    assert locked.headers["Retry-After"]


def test_successful_login_resets_failure_state(auth_env):
    client = auth_env
    _create_login_user()

    for _ in range(4):
        assert _failed_login(client).status_code == 401
    success = client.post(
        "/auth/login",
        json={"login": "reader@example.com", "password": "correct-password"},
        environ_base={"REMOTE_ADDR": "198.51.100.10"},
    )
    assert success.status_code == 200

    for _ in range(5):
        assert _failed_login(client).status_code == 401
    assert _failed_login(client).status_code == 429


def test_untrusted_peer_cannot_spoof_x_real_ip_to_evade_login_lock(auth_env):
    client = auth_env
    _create_login_user()

    for index in range(5):
        response = _failed_login(
            client,
            remote_addr="198.51.100.20",
            real_ip=f"203.0.113.{index + 1}",
        )
        assert response.status_code == 401

    locked = _failed_login(
        client,
        remote_addr="198.51.100.20",
        real_ip="203.0.113.99",
    )
    assert locked.status_code == 429


def test_loopback_proxy_x_real_ip_separates_login_limits(auth_env):
    client = auth_env
    _create_login_user()

    for _ in range(5):
        assert _failed_login(
            client,
            remote_addr="127.0.0.1",
            real_ip="198.51.100.30",
        ).status_code == 401

    other_client = _failed_login(
        client,
        remote_addr="127.0.0.1",
        real_ip="198.51.100.31",
    )
    assert other_client.status_code == 401
    assert _failed_login(
        client,
        remote_addr="127.0.0.1",
        real_ip="198.51.100.30",
    ).status_code == 429


def test_successful_invite_request_starts_email_cooldown(auth_env, monkeypatch):
    client = auth_env
    sent = []

    def fake_send(*args, **kwargs):
        sent.append((args, kwargs))

    monkeypatch.setattr(notifier, "send_email", fake_send)

    first = client.post(
        "/auth/request-invite",
        json={"email": "new-reader@example.com"},
    )
    second = client.post(
        "/auth/request-invite",
        json={"email": "NEW-READER@example.com"},
    )

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.get_json()["retry_after"] > 0
    assert second.headers["Retry-After"]
    assert len(sent) == 1


def test_failed_invite_delivery_does_not_consume_allowance(auth_env, monkeypatch):
    client = auth_env
    attempts = 0

    def flaky_send(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("mail provider unavailable")

    monkeypatch.setattr(notifier, "send_email", flaky_send)

    failed = client.post(
        "/auth/request-invite",
        json={"email": "retry-reader@example.com"},
    )
    retried = client.post(
        "/auth/request-invite",
        json={"email": "retry-reader@example.com"},
    )

    assert failed.status_code == 500
    assert retried.status_code == 201
    assert attempts == 2
    assert models.count_pending_invitations() == 1
