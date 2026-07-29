"""SSRF protections shared by image fetching and server-side AI requests."""

import socket

import pytest

import ai_service
import image_cache
import network_safety
from network_safety import UnsafeUrlError, assert_public_http_url, safe_get, safe_post


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "http://10.0.0.1/a",
        "http://172.16.0.1/a",
        "http://192.168.0.1/a",
        "http://169.254.169.254/a",
        "http://[::1]/a",
        "http://[fc00::1]/a",
        "http://[fe80::1]/a",
        "http://224.0.0.1/a",
        "http://[ff02::1]/a",
    ],
)
def test_rejects_direct_private_and_link_local_targets(url):
    with pytest.raises(UnsafeUrlError) as exc_info:
        assert_public_http_url(url)

    assert "127.0.0.1" not in str(exc_info.value)
    assert "169.254.169.254" not in str(exc_info.value)


def test_rejects_hostname_resolving_to_private_ip(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))
        ],
    )

    with pytest.raises(UnsafeUrlError):
        assert_public_http_url("https://model.example/v1")


def test_rejects_hostname_with_any_non_public_dns_answer(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1", 443, 0, 0)),
        ],
    )

    with pytest.raises(UnsafeUrlError):
        assert_public_http_url("https://mixed.example/v1")


def test_accepts_public_ip():
    url = "https://8.8.8.8/path?q=1"

    assert assert_public_http_url(url) == url


class _Response:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def close(self):
        self.closed = True


def _public_dns(*args, **kwargs):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    ]


def test_get_rejects_redirect_from_public_to_private_before_second_request(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    first = _Response(302, {"Location": "http://127.0.0.1/admin"})
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return first

    monkeypatch.setattr(network_safety, "_send_bound_request", fake_get)

    with pytest.raises(UnsafeUrlError):
        safe_get("https://public.example/image.jpg", timeout=15, stream=True)

    assert len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False
    assert first.closed


def test_post_rejects_redirect_from_public_to_private_before_second_request(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    first = _Response(307, {"Location": "http://[::1]/v1/messages"})
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return first

    monkeypatch.setattr(network_safety, "_send_bound_request", fake_post)

    with pytest.raises(UnsafeUrlError):
        safe_post(
            "https://public.example/v1/messages",
            headers={"Authorization": "Bearer secret"},
            json={"message": "hello"},
            timeout=(30, 300),
        )

    assert len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False
    assert first.closed


def test_get_follows_public_redirect_one_checked_hop_at_a_time(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    first = _Response(302, {"Location": "/final.jpg"})
    final = _Response(200, {"Content-Type": "image/jpeg"})
    responses = iter((first, final))
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr(network_safety, "_send_bound_request", fake_get)

    result = safe_get("https://public.example/start.jpg", timeout=15)

    assert result is final
    assert [call[0] for call in calls] == [
        "https://public.example/start.jpg",
        "https://public.example/final.jpg",
    ]
    assert all(call[1]["allow_redirects"] is False for call in calls)
    assert first.closed


def test_get_stops_after_bounded_redirect_limit(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    responses = []

    def fake_get(url, **kwargs):
        response = _Response(302, {"Location": "/again"})
        responses.append(response)
        return response

    monkeypatch.setattr(network_safety, "_send_bound_request", fake_get)

    with pytest.raises(UnsafeUrlError):
        safe_get("https://public.example/start", max_redirects=2)

    assert len(responses) == 3
    assert all(response.closed for response in responses)


def test_bound_connection_uses_validated_sockaddr_without_second_dns_lookup(monkeypatch):
    connected = []

    class FakeSocket:
        def settimeout(self, value):
            self.timeout = value

        def setsockopt(self, *args):
            pass

        def connect(self, sockaddr):
            connected.append(sockaddr)

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *args: FakeSocket())
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: pytest.fail("bound transport performed a second DNS lookup"),
    )
    address = (
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        "",
        ("93.184.216.34", 80),
    )

    connection = network_safety._BoundHTTPConnection(
        "rebind.example",
        80,
        resolved_addresses=(address,),
    )
    result = connection._new_conn()

    assert isinstance(result, FakeSocket)
    assert connected == [("93.184.216.34", 80)]
    assert connection.host == "rebind.example"


def test_https_bound_pool_keeps_origin_hostname_and_certificate_verification():
    address = (
        socket.AF_INET,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        "",
        ("93.184.216.34", 443),
    )
    adapter = network_safety._BoundAddressAdapter((address,))
    request = network_safety.requests.Request(
        "GET",
        "https://model.example/v1",
    ).prepare()

    pool = adapter.get_connection_with_tls_context(request, verify=True)
    connection = pool._new_conn()

    assert pool.host == "model.example"
    assert connection.host == "model.example"
    assert connection.server_hostname is None
    assert connection.cert_reqs == "CERT_REQUIRED"


def test_malformed_redirect_is_closed_and_raises_fixed_safe_error(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)
    first = _Response(302, {"Location": "http://[invalid"})

    def fake_request(self, method, url, **kwargs):
        return first

    monkeypatch.setattr(network_safety.requests.Session, "request", fake_request)

    with pytest.raises(UnsafeUrlError) as exc_info:
        safe_get("https://public.example/start")

    assert str(exc_info.value) == "URL target is not allowed"
    assert first.closed


def test_image_fetch_rejects_private_target_before_transport(monkeypatch):
    transport_called = False

    def fake_get(url, **kwargs):
        nonlocal transport_called
        transport_called = True
        response = _Response(200, {"Content-Type": "image/jpeg"})
        response.raise_for_status = lambda: None
        response.iter_content = lambda chunk_size: [b"jpeg"]
        return response

    monkeypatch.setattr(network_safety, "_send_bound_request", fake_get)

    with pytest.raises(UnsafeUrlError):
        image_cache.fetch_remote_image("http://127.0.0.1/secret.jpg")

    assert transport_called is False


@pytest.mark.parametrize("provider_type", ["openai", "claude"])
def test_ai_requests_reject_private_endpoint_before_transport(monkeypatch, provider_type):
    transport_called = False

    def fake_post(url, **kwargs):
        nonlocal transport_called
        transport_called = True
        response = _Response(200)
        response.ok = True
        if provider_type == "openai":
            response.json = lambda: {
                "choices": [{"message": {"content": "unsafe response"}}]
            }
        else:
            response.json = lambda: {
                "content": [{"type": "text", "text": "unsafe response"}]
            }
        return response

    monkeypatch.setattr(network_safety, "_send_bound_request", fake_post)
    service = ai_service.AIService(
        api_key="secret",
        endpoint="http://127.0.0.1:11434/v1",
        model="local-model",
        provider_type=provider_type,
    )

    with pytest.raises(UnsafeUrlError):
        service.chat([{"role": "user", "content": "hello"}])

    assert transport_called is False
