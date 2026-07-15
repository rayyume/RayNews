"""Behavioral contracts for the authenticated refresh-service proxies."""

import json

import pytest
import requests

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


class StubResponse:
    def __init__(self, payload, status_code):
        self.payload = payload
        self.status_code = status_code
        self.json_calls = 0

    def json(self):
        self.json_calls += 1
        return self.payload


class FailingJsonResponse:
    status_code = 200

    def __init__(self, failure):
        self.failure = failure
        self.json_calls = 0

    def json(self):
        self.json_calls += 1
        raise self.failure


@pytest.mark.parametrize(
    ("method", "path"),
    (("post", "/auth/refresh"), ("get", "/auth/refresh/status")),
)
def test_refresh_proxies_require_authentication(client, monkeypatch, method, path):
    monkeypatch.setattr(
        web_server.requests,
        method,
        lambda *args, **kwargs: pytest.fail("unauthenticated request reached upstream"),
    )

    response = getattr(client, method)(path)

    assert response.status_code == 401
    assert response.get_json() == {"error": "missing token"}


@pytest.mark.parametrize(
    ("method", "path"),
    (("get", "/auth/refresh"), ("post", "/auth/refresh/status")),
)
def test_refresh_proxies_reject_wrong_methods(client, method, path):
    response = getattr(client, method)(
        path,
        headers=_auth_headers(101, "user"),
    )

    assert response.status_code == 405


def test_refresh_proxies_reject_authenticated_unsupported_roles(client, monkeypatch):
    monkeypatch.setattr(
        web_server.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("unauthorized role reached upstream"),
    )

    response = client.post(
        "/auth/refresh",
        headers=_auth_headers(303, "auditor"),
    )

    assert response.status_code == 403
    assert response.get_json() == {"error": "insufficient permissions"}


@pytest.mark.parametrize(
    ("client_method", "path", "request_method", "upstream_url", "user_id", "role", "payload", "status_code"),
    (
        (
            "post",
            "/auth/refresh",
            "post",
            "http://127.0.0.1:8081/refresh",
            101,
            "user",
            {"status": "started", "job_id": "job-1"},
            202,
        ),
        (
            "get",
            "/auth/refresh/status",
            "get",
            "http://127.0.0.1:8081/refresh/status",
            202,
            "admin",
            {"status": "running", "job_id": "job-1"},
            200,
        ),
        (
            "post",
            "/auth/refresh",
            "post",
            "http://127.0.0.1:8081/refresh",
            202,
            "admin",
            {"status": "started", "job_id": "admin-job"},
            202,
        ),
        (
            "get",
            "/auth/refresh/status",
            "get",
            "http://127.0.0.1:8081/refresh/status",
            101,
            "user",
            {"status": "failed", "error": "worker launch failed"},
            503,
        ),
    ),
)
def test_refresh_proxies_preserve_upstream_payload_status_and_decode_once(
    client,
    monkeypatch,
    client_method,
    path,
    request_method,
    upstream_url,
    user_id,
    role,
    payload,
    status_code,
):
    upstream = StubResponse(payload, status_code)
    calls = []

    def request_stub(url, timeout):
        calls.append((url, timeout))
        return upstream

    monkeypatch.setattr(web_server.requests, request_method, request_stub)

    response = getattr(client, client_method)(
        path,
        headers=_auth_headers(user_id, role),
    )

    assert response.status_code == status_code
    assert response.get_json() == payload
    assert upstream.json_calls == 1
    assert calls == [(upstream_url, 5)]


def test_refresh_proxy_connectivity_failure_returns_static_private_502(
    client,
    monkeypatch,
):
    def fail_request(*args, **kwargs):
        raise requests.RequestException("private connection details")

    monkeypatch.setattr(web_server.requests, "post", fail_request)

    response = client.post(
        "/auth/refresh",
        headers=_auth_headers(101, "user"),
    )

    assert response.status_code == 502
    assert response.get_json() == {"error": "refresh service unavailable"}
    assert "private" not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "failure",
    (
        ValueError("private decoder details"),
        json.JSONDecodeError("private JSON document", "x", 0),
    ),
)
def test_refresh_proxy_json_decode_failure_returns_static_private_502(
    client,
    monkeypatch,
    failure,
):
    upstream = FailingJsonResponse(failure)
    monkeypatch.setattr(web_server.requests, "get", lambda *args, **kwargs: upstream)

    response = client.get(
        "/auth/refresh/status",
        headers=_auth_headers(101, "user"),
    )

    assert response.status_code == 502
    assert response.get_json() == {"error": "refresh service unavailable"}
    assert "private" not in response.get_data(as_text=True)
    assert upstream.json_calls == 1


def test_refresh_start_unexpected_error_returns_static_private_500(client, monkeypatch):
    def fail_unexpectedly(*args, **kwargs):
        raise RuntimeError("private programming failure")

    monkeypatch.setattr(web_server.requests, "post", fail_unexpectedly)

    response = client.post(
        "/auth/refresh",
        headers=_auth_headers(101, "user"),
    )

    assert response.status_code == 500
    assert response.get_json() == {"error": "internal server error"}
    assert "private" not in response.get_data(as_text=True)


def test_refresh_status_serialization_error_returns_static_private_500(client, monkeypatch):
    upstream = StubResponse({"not_json": {"private", "values"}}, 200)
    monkeypatch.setattr(web_server.requests, "get", lambda *args, **kwargs: upstream)

    response = client.get(
        "/auth/refresh/status",
        headers=_auth_headers(202, "admin"),
    )

    assert response.status_code == 500
    assert response.get_json() == {"error": "internal server error"}
    assert "private" not in response.get_data(as_text=True)
    assert upstream.json_calls == 1


def test_refresh_status_proxy_forwards_job_id_query_without_leaking_it_on_failure(
    client,
    monkeypatch,
):
    upstream = StubResponse({"job_id": "job-a", "status": "completed"}, 200)
    calls = []

    def request_stub(url, timeout, params=None):
        calls.append((url, timeout, params))
        return upstream

    monkeypatch.setattr(web_server.requests, "get", request_stub)
    response = client.get(
        "/auth/refresh/status?job_id=job-a",
        headers=_auth_headers(101, "user"),
    )

    assert response.status_code == 200
    assert response.get_json() == {"job_id": "job-a", "status": "completed"}
    assert calls == [
        ("http://127.0.0.1:8081/refresh/status", 5, {"job_id": "job-a"}),
    ]

    def fail_request(*args, **kwargs):
        raise requests.RequestException("job-a private upstream detail")

    monkeypatch.setattr(web_server.requests, "get", fail_request)
    failed = client.get(
        "/auth/refresh/status?job_id=job-a",
        headers=_auth_headers(101, "user"),
    )
    assert failed.status_code == 502
    assert failed.get_json() == {"error": "refresh service unavailable"}
    assert "job-a" not in failed.get_data(as_text=True)
