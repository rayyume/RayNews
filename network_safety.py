"""Network-boundary helpers that only connect to public HTTP(S) targets."""

from __future__ import annotations

import socket
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import requests


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_DEFAULT_MAX_REDIRECTS = 5


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe for a server-side request."""


def _unsafe(message: str = "URL target is not allowed") -> UnsafeUrlError:
    return UnsafeUrlError(message)


def assert_public_http_url(url: str) -> str:
    """Validate that *url* is HTTP(S) and every resolved address is public."""
    if not isinstance(url, str):
        raise _unsafe("A valid public HTTP(S) URL is required")

    candidate = url.strip()
    if not candidate or any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        raise _unsafe("A valid public HTTP(S) URL is required")

    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        raise _unsafe("A valid public HTTP(S) URL is required") from None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise _unsafe("A valid public HTTP(S) URL is required")

    hostname = parsed.hostname
    if "%" in hostname:
        raise _unsafe()

    try:
        literal_address = ip_address(hostname)
    except ValueError:
        literal_address = None

    if literal_address is not None:
        if not literal_address.is_global or literal_address.is_multicast:
            raise _unsafe()
        return candidate

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
        addresses = socket.getaddrinfo(
            ascii_hostname,
            port or (443 if parsed.scheme.lower() == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except (OSError, UnicodeError, ValueError):
        raise _unsafe("URL target could not be safely resolved") from None

    if not addresses:
        raise _unsafe("URL target could not be safely resolved")

    try:
        resolved_addresses = [ip_address(address[4][0]) for address in addresses]
    except (IndexError, TypeError, ValueError):
        raise _unsafe("URL target could not be safely resolved") from None

    if any(not address.is_global or address.is_multicast for address in resolved_addresses):
        raise _unsafe()
    return candidate


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _without_sensitive_cross_origin_headers(
    headers: dict | None,
    source_url: str,
    target_url: str,
) -> dict | None:
    if not headers or _origin(source_url) == _origin(target_url):
        return headers
    sensitive = {"authorization", "cookie", "proxy-authorization", "x-api-key"}
    return {key: value for key, value in headers.items() if key.lower() not in sensitive}


def _without_body_headers(headers: dict | None) -> dict | None:
    if not headers:
        return headers
    body_headers = {"content-length", "content-type", "transfer-encoding"}
    return {key: value for key, value in headers.items() if key.lower() not in body_headers}


def _safe_request(
    method: str,
    url: str,
    *,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    **kwargs,
) -> requests.Response:
    """Send a GET or POST after validating every redirect hop."""
    if method not in {"GET", "POST"}:
        raise ValueError("safe requests support GET and POST only")
    if not isinstance(max_redirects, int) or max_redirects < 0:
        raise ValueError("max_redirects must be a non-negative integer")

    current_url = assert_public_http_url(url)
    current_method = method
    request_kwargs = dict(kwargs)
    request_kwargs["allow_redirects"] = False

    redirects_followed = 0
    while True:
        request_func = requests.get if current_method == "GET" else requests.post
        response = request_func(current_url, **request_kwargs)
        location = response.headers.get("Location") if response.status_code in _REDIRECT_STATUSES else None
        if not location:
            return response

        if redirects_followed >= max_redirects:
            response.close()
            raise _unsafe("Too many redirects")

        try:
            next_url = assert_public_http_url(urljoin(current_url, location))
        except UnsafeUrlError:
            response.close()
            raise
        next_headers = _without_sensitive_cross_origin_headers(
            request_kwargs.get("headers"),
            current_url,
            next_url,
        )

        if current_method == "POST" and response.status_code in {301, 302, 303}:
            current_method = "GET"
            for body_key in ("data", "json", "files"):
                request_kwargs.pop(body_key, None)
            next_headers = _without_body_headers(next_headers)

        if next_headers is not None:
            request_kwargs["headers"] = next_headers
        response.close()
        current_url = next_url
        redirects_followed += 1


def safe_get(
    url: str,
    *,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    **kwargs,
) -> requests.Response:
    """GET a public URL while validating every redirect target."""
    return _safe_request("GET", url, max_redirects=max_redirects, **kwargs)


def safe_post(
    url: str,
    *,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    **kwargs,
) -> requests.Response:
    """POST to a public URL while validating every redirect target."""
    return _safe_request("POST", url, max_redirects=max_redirects, **kwargs)
