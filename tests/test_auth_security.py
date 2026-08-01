"""Security regressions for registration, login, and invite throttling."""

from concurrent.futures import ThreadPoolExecutor
import threading
import time

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


def _seed_admin(email="admin@example.com", nickname="admin"):
    db = models.get_db()
    cur = db.execute(
        "INSERT INTO users (email, password, nickname, role) VALUES (?, ?, ?, ?)",
        (email, "unused-test-hash", nickname, "admin"),
    )
    db.commit()
    return cur.lastrowid


def _register(
    client,
    *,
    email,
    nickname,
    invite_code="INVALID",
    remote_addr="198.51.100.40",
):
    return client.post(
        "/auth/register",
        json={
            "email": email,
            "password": "correct-password",
            "nickname": nickname,
            "invite_code": invite_code,
        },
        environ_base={"REMOTE_ADDR": remote_addr},
    )


def test_register_rate_limit_rejects_eleventh_attempt(auth_env):
    client = auth_env
    _seed_admin()

    for index in range(10):
        response = _register(
            client,
            email=f"failed-{index}@example.com",
            nickname=f"failed-{index}",
        )
        assert response.status_code == 400

    rejected = _register(
        client,
        email="failed-10@example.com",
        nickname="failed-10",
    )
    assert rejected.status_code == 429
    retry_after = rejected.get_json()["retry_after"]
    assert isinstance(retry_after, int)
    assert retry_after > 0
    assert rejected.headers["Retry-After"] == str(retry_after)


def test_invalid_invite_skips_bcrypt(auth_env, monkeypatch):
    _seed_admin()

    def unexpected_hash(_password):
        raise AssertionError("bcrypt must not run for an invalid invitation")

    monkeypatch.setattr(models, "hash_password", unexpected_hash)
    user, is_initial_admin = models.create_registered_user(
        "new@example.com",
        "correct-password",
        "new-reader",
        "INVALID",
    )

    assert user is None
    assert is_initial_admin is False


def test_duplicate_email_skips_bcrypt(auth_env, monkeypatch):
    _seed_admin(email="duplicate@example.com")

    def unexpected_hash(_password):
        raise AssertionError("bcrypt must not run for a duplicate email")

    monkeypatch.setattr(models, "hash_password", unexpected_hash)
    user, is_initial_admin = models.create_registered_user(
        "duplicate@example.com",
        "correct-password",
        "different-reader",
        "",
    )

    assert user is None
    assert is_initial_admin is False


def test_duplicate_nickname_skips_bcrypt(auth_env, monkeypatch):
    _seed_admin(nickname="duplicate-reader")

    def unexpected_hash(_password):
        raise AssertionError("bcrypt must not run for a duplicate nickname")

    monkeypatch.setattr(models, "hash_password", unexpected_hash)
    user, is_initial_admin = models.create_registered_user(
        "different@example.com",
        "correct-password",
        "duplicate-reader",
        "",
    )

    assert user is None
    assert is_initial_admin is False


def test_register_rate_limit_success_resets_attempts(auth_env, monkeypatch):
    client = auth_env
    client_ip = "198.51.100.41"
    _seed_admin()

    for index in range(9):
        assert _register(
            client,
            email=f"before-success-{index}@example.com",
            nickname=f"before-success-{index}",
            remote_addr=client_ip,
        ).status_code == 400

    attempt = models.get_db().execute(
        "SELECT failure_count FROM register_attempts WHERE client_ip = ?",
        (client_ip,),
    ).fetchone()
    assert attempt["failure_count"] == 9

    invite_code = models.create_invitation_code("success@example.com")
    monkeypatch.setattr(models, "hash_password", lambda _password: "test-hash")
    monkeypatch.setattr(web_server, "_send_registration_notice", lambda _user: False)
    successful = _register(
        client,
        email="success@example.com",
        nickname="successful-reader",
        invite_code=invite_code,
        remote_addr=client_ip,
    )
    assert successful.status_code == 201
    assert models.get_db().execute(
        "SELECT 1 FROM register_attempts WHERE client_ip = ?",
        (client_ip,),
    ).fetchone() is None

    after_reset = _register(
        client,
        email="after-reset@example.com",
        nickname="after-reset",
        remote_addr=client_ip,
    )
    assert after_reset.status_code == 400


def test_register_attempt_cleanup_removes_only_stale_rows(auth_env):
    now = time.time()
    db = models.get_db()
    db.executemany(
        "INSERT INTO register_attempts "
        "(client_ip, failure_count, locked_until, updated_at) "
        "VALUES (?, ?, ?, ?)",
        [
            ("198.51.100.42", 3, 0, now - 7200),
            ("198.51.100.43", 3, 0, now),
        ],
    )
    db.commit()

    allowed, retry_after = models.admit_register_attempt(
        "198.51.100.44",
        now=now,
    )

    assert allowed is True
    assert retry_after == 0
    rows = db.execute(
        "SELECT client_ip FROM register_attempts ORDER BY client_ip"
    ).fetchall()
    assert [row["client_ip"] for row in rows] == [
        "198.51.100.43",
        "198.51.100.44",
    ]


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


def test_parallel_login_failures_admit_only_five_password_verifications(
    auth_env,
    monkeypatch,
):
    _create_login_user()
    start = threading.Barrier(6)
    verification_calls = 0
    call_lock = threading.Lock()

    def slow_wrong_password(*args):
        nonlocal verification_calls
        with call_lock:
            verification_calls += 1
        # Keep every admitted request in password verification long enough for
        # all contenders to exercise the rate-limit admission boundary.
        time.sleep(0.15)
        return False

    monkeypatch.setattr(web_server, "verify_password", slow_wrong_password)

    def attempt(_index):
        client = web_server.app.test_client()
        try:
            start.wait(timeout=5)
            return _failed_login(client).status_code
        finally:
            models.close_db()

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(attempt, range(6)))

    assert sorted(statuses) == [401, 401, 401, 401, 401, 429]
    assert verification_calls == 5


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
    assert 0 < second.get_json()["retry_after"] <= 60
    assert second.headers["Retry-After"]
    assert len(sent) == 1


def test_failed_invite_delivery_does_not_consume_allowance(auth_env, monkeypatch):
    client = auth_env
    attempts = 0

    def flaky_send(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise notifier.EmailDeliveryRejected("mail provider rejected request")

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


def test_ambiguous_invite_delivery_preserves_code_and_idempotency_reservation(
    auth_env,
    monkeypatch,
):
    client = auth_env
    idempotency_keys = []

    def ambiguous_send(*args, **kwargs):
        idempotency_keys.append(kwargs.get("idempotency_key"))
        raise notifier.EmailDeliveryUncertain("response lost after upload")

    monkeypatch.setattr(notifier, "send_email", ambiguous_send)

    first = client.post(
        "/auth/request-invite",
        json={"email": "uncertain-reader@example.com"},
    )
    immediate_retry = client.post(
        "/auth/request-invite",
        json={"email": "uncertain-reader@example.com"},
    )

    assert first.status_code == 503
    assert immediate_retry.status_code == 429
    assert 0 < immediate_retry.get_json()["retry_after"] <= 60
    assert len(idempotency_keys) == 1
    assert idempotency_keys[0]
    assert models.count_pending_invitations() == 1
    persisted = models.get_db().execute(
        "SELECT reservation_token FROM invite_request_limits WHERE email = ?",
        ("uncertain-reader@example.com",),
    ).fetchone()
    assert persisted["reservation_token"] == idempotency_keys[0]

    old_code = models.get_db().execute(
        "SELECT code FROM invitation_codes "
        "WHERE email = ? AND used = 0",
        ("uncertain-reader@example.com",),
    ).fetchone()["code"]
    models.get_db().execute(
        "UPDATE invite_request_limits SET reserved_at = ? WHERE email = ?",
        (time.time() - 61, "uncertain-reader@example.com"),
    )
    models.get_db().commit()

    def delivered_on_new_application(*args, **kwargs):
        idempotency_keys.append(kwargs.get("idempotency_key"))

    monkeypatch.setattr(notifier, "send_email", delivered_on_new_application)
    after_one_minute = client.post(
        "/auth/request-invite",
        json={"email": "uncertain-reader@example.com"},
    )

    assert after_one_minute.status_code == 201
    assert idempotency_keys[1] and idempotency_keys[1] != idempotency_keys[0]
    codes = models.get_db().execute(
        "SELECT code, used FROM invitation_codes "
        "WHERE email = ? ORDER BY id",
        ("uncertain-reader@example.com",),
    ).fetchall()
    assert codes[0]["code"] == old_code and codes[0]["used"] == 1
    assert codes[1]["code"] != old_code and codes[1]["used"] == 0


def test_new_invite_after_one_minute_invalidates_previous_code(
    auth_env,
    monkeypatch,
):
    client = auth_env
    monkeypatch.setattr(notifier, "send_email", lambda *args, **kwargs: None)

    first = client.post(
        "/auth/request-invite",
        json={"email": "renew-reader@example.com"},
    )
    assert first.status_code == 201
    old_code = models.get_db().execute(
        "SELECT code FROM invitation_codes "
        "WHERE email = ? AND used = 0",
        ("renew-reader@example.com",),
    ).fetchone()["code"]
    models.get_db().execute(
        "UPDATE invite_request_limits SET last_success_at = ? WHERE email = ?",
        (time.time() - 61, "renew-reader@example.com"),
    )
    models.get_db().commit()

    renewed = client.post(
        "/auth/request-invite",
        json={"email": "renew-reader@example.com"},
    )

    assert renewed.status_code == 201
    rows = models.get_db().execute(
        "SELECT code, used FROM invitation_codes WHERE email = ? ORDER BY id",
        ("renew-reader@example.com",),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["code"] == old_code
    assert rows[0]["used"] == 1
    assert rows[1]["used"] == 0
    assert rows[1]["code"] != old_code


def test_invite_cooldown_expires_after_one_minute(auth_env):
    token, retry_after = models.claim_invite_request(
        "minute-reader@example.com",
        now=1_000,
    )
    assert token and retry_after == 0
    assert models.complete_invite_request(
        "minute-reader@example.com",
        token,
        succeeded=True,
        now=1_000,
    )

    blocked_token, retry_after = models.claim_invite_request(
        "minute-reader@example.com",
        now=1_059,
    )
    assert blocked_token is None
    assert retry_after == 1

    renewed_token, retry_after = models.claim_invite_request(
        "minute-reader@example.com",
        now=1_060,
    )
    assert renewed_token and renewed_token != token
    assert retry_after == 0


def test_notifier_marks_provider_rejection_as_definitive(monkeypatch):
    class RejectedResponse:
        status_code = 401

        @staticmethod
        def json():
            return {"message": "invalid API key"}

    monkeypatch.setattr(
        notifier.requests,
        "post",
        lambda *args, **kwargs: RejectedResponse(),
    )

    with pytest.raises(notifier.EmailDeliveryRejected):
        notifier.send_email("bad-key", "admin@example.com", "subject", "body")


def test_notifier_marks_lost_response_as_uncertain(monkeypatch):
    def timeout(*args, **kwargs):
        raise notifier.requests.Timeout("response timed out")

    monkeypatch.setattr(notifier.requests, "post", timeout)

    with pytest.raises(notifier.EmailDeliveryUncertain):
        notifier.send_email("key", "admin@example.com", "subject", "body")
