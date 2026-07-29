"""Network-boundary helpers that only connect to public HTTP(S) targets."""

from __future__ import annotations

import socket
import sys
import os
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3 import ProxyManager
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_DEFAULT_MAX_REDIRECTS = 5


@dataclass(frozen=True)
class _TrustedProxy:
    """A public, environment-configured proxy trusted to reach public targets."""

    url: str
    resolved_addresses: tuple[tuple, ...]


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe for a server-side request."""


def _unsafe(message: str = "URL target is not allowed") -> UnsafeUrlError:
    return UnsafeUrlError(message)


def _resolve_public_http_url(url: str) -> tuple[str, tuple[tuple, ...]]:
    """Return a safe URL and the exact public socket addresses it resolved to."""
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
        family = socket.AF_INET6 if literal_address.version == 6 else socket.AF_INET
        sockaddr = (
            (str(literal_address), port or (443 if parsed.scheme.lower() == "https" else 80), 0, 0)
            if family == socket.AF_INET6
            else (str(literal_address), port or (443 if parsed.scheme.lower() == "https" else 80))
        )
        return candidate, (
            (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr),
        )

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
        target_port = port or (443 if parsed.scheme.lower() == "https" else 80)
        addresses = socket.getaddrinfo(
            ascii_hostname,
            target_port,
            type=socket.SOCK_STREAM,
        )
    except (OSError, UnicodeError, ValueError):
        raise _unsafe("URL target could not be safely resolved") from None

    if not addresses:
        raise _unsafe("URL target could not be safely resolved")

    normalized_addresses = []
    try:
        for family, socktype, proto, canonname, sockaddr in addresses:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                raise ValueError
            resolved = ip_address(sockaddr[0])
            if not resolved.is_global or resolved.is_multicast:
                raise UnsafeUrlError
            normalized_sockaddr = (
                (str(resolved), target_port, sockaddr[2], sockaddr[3])
                if family == socket.AF_INET6
                else (str(resolved), target_port)
            )
            normalized_addresses.append(
                (
                    family,
                    socktype or socket.SOCK_STREAM,
                    proto or socket.IPPROTO_TCP,
                    canonname,
                    normalized_sockaddr,
                )
            )
    except UnsafeUrlError:
        raise _unsafe() from None
    except (IndexError, TypeError, ValueError):
        raise _unsafe("URL target could not be safely resolved") from None

    if not normalized_addresses:
        raise _unsafe("URL target could not be safely resolved")
    return candidate, tuple(normalized_addresses)


def assert_public_http_url(url: str) -> str:
    """Validate that *url* is HTTP(S) and every resolved address is public."""
    candidate, _ = _resolve_public_http_url(url)
    return candidate


def _trusted_environment_proxy(url: str) -> _TrustedProxy | None:
    """Return a public proxy explicitly configured for this URL's scheme.

    The proxy is an intentional trust boundary: HTTP forwarding/CONNECT makes
    the proxy, not this process, perform the final destination connection.
    Destination URLs are still validated before this boundary is crossed.
    """
    scheme = urlsplit(url).scheme.lower()
    environment_name = "HTTPS_PROXY" if scheme == "https" else "HTTP_PROXY"
    configured_proxy = os.environ.get(environment_name, "").strip()
    if not configured_proxy:
        return None
    proxy_url, proxy_addresses = _resolve_public_http_url(configured_proxy)
    return _TrustedProxy(proxy_url, proxy_addresses)


class _BoundConnectionMixin:
    """Connect urllib3 to prevalidated sockaddrs without resolving again."""

    def __init__(self, *args, resolved_addresses: tuple[tuple, ...], **kwargs):
        self._resolved_addresses = resolved_addresses
        super().__init__(*args, **kwargs)

    def _new_conn(self) -> socket.socket:
        last_error: OSError | None = None
        for family, socktype, proto, _canonname, sockaddr in self._resolved_addresses:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(self.timeout)
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sockaddr)
                sys.audit("http.client.connect", self, self.host, self.port)
                return sock
            except TimeoutError as exc:
                if sock is not None:
                    sock.close()
                raise ConnectTimeoutError(
                    self,
                    f"Connection to public target timed out. (connect timeout={self.timeout})",
                ) from exc
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()

        raise NewConnectionError(
            self,
            "Failed to establish a connection to the validated public target",
        ) from last_error


class _BoundHTTPConnection(_BoundConnectionMixin, HTTPConnection):
    pass


class _BoundHTTPSConnection(_BoundConnectionMixin, HTTPSConnection):
    pass


class _BoundHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _BoundHTTPConnection


class _BoundHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _BoundHTTPSConnection


class _BoundAddressAdapter(HTTPAdapter):
    """Requests adapter whose pools retain the origin but dial only validated IPs."""

    def __init__(
        self,
        resolved_addresses: tuple[tuple, ...],
        *,
        trusted_proxy: _TrustedProxy | None = None,
    ):
        self._resolved_addresses = resolved_addresses
        self._trusted_proxy = trusted_proxy
        self._proxy_manager = (
            ProxyManager(trusted_proxy.url) if trusted_proxy is not None else None
        )
        super().__init__()

    def _pool_for_url(self, url: str, pool_kwargs: dict | None = None):
        parsed = urlsplit(url)
        # Keep the original hostname on the pool/connection: urllib3 uses it
        # for the HTTP Host header and for HTTPS SNI/certificate matching.
        # Only _new_conn's socket destination is replaced with validated IPs.
        if self._trusted_proxy is not None and parsed.scheme == "http":
            proxy = self._proxy_manager.proxy
            return _BoundHTTPConnectionPool(
                proxy.host,
                proxy.port,
                resolved_addresses=self._trusted_proxy.resolved_addresses,
            )
        if parsed.scheme == "https":
            proxy_kwargs = {}
            connection_addresses = self._resolved_addresses
            if self._trusted_proxy is not None:
                proxy_kwargs = {
                    "_proxy": self._proxy_manager.proxy,
                    "_proxy_headers": self._proxy_manager.proxy_headers,
                    "_proxy_config": self._proxy_manager.proxy_config,
                }
                connection_addresses = self._trusted_proxy.resolved_addresses
            return _BoundHTTPSConnectionPool(
                parsed.hostname,
                parsed.port,
                resolved_addresses=connection_addresses,
                **proxy_kwargs,
                **(pool_kwargs or {}),
            )
        return _BoundHTTPConnectionPool(
            parsed.hostname,
            parsed.port,
            resolved_addresses=self._resolved_addresses,
        )

    def get_connection_with_tls_context(
        self,
        request,
        verify,
        proxies=None,
        cert=None,
    ):
        if bool(proxies) != bool(self._trusted_proxy):
            raise _unsafe("Proxy routing is not allowed for safe requests")
        _host_params, pool_kwargs = self.build_connection_pool_key_attributes(
            request,
            verify,
            cert,
        )
        return self._pool_for_url(request.url, pool_kwargs)

    def get_connection(self, url, proxies=None):
        """Compatibility with Requests versions before get_connection_with_tls_context."""
        if bool(proxies) != bool(self._trusted_proxy):
            raise _unsafe("Proxy routing is not allowed for safe requests")
        return self._pool_for_url(url)


def _send_bound_request(
    url: str,
    *,
    method: str,
    resolved_addresses: tuple[tuple, ...],
    trusted_proxy: _TrustedProxy | None = None,
    **kwargs,
) -> requests.Response:
    """Send one hop through an adapter bound to the validated socket addresses."""
    if "proxies" in kwargs:
        raise _unsafe("Explicit proxy routing is not allowed for safe requests")
    kwargs["allow_redirects"] = False
    proxies = (
        {"http": trusted_proxy.url, "https": trusted_proxy.url}
        if trusted_proxy is not None
        else {}
    )
    with requests.Session() as session:
        session.trust_env = False
        adapter = _BoundAddressAdapter(resolved_addresses, trusted_proxy=trusted_proxy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session.request(method, url, proxies=proxies, **kwargs)


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
    if "proxies" in kwargs:
        raise _unsafe("Explicit proxy routing is not allowed for safe requests")

    current_url, resolved_addresses = _resolve_public_http_url(url)
    current_method = method
    request_kwargs = dict(kwargs)
    request_kwargs["allow_redirects"] = False

    redirects_followed = 0
    while True:
        trusted_proxy = _trusted_environment_proxy(current_url)
        response = _send_bound_request(
            current_url,
            method=current_method,
            resolved_addresses=resolved_addresses,
            trusted_proxy=trusted_proxy,
            **request_kwargs,
        )
        location = response.headers.get("Location") if response.status_code in _REDIRECT_STATUSES else None
        if not location:
            return response

        if redirects_followed >= max_redirects:
            response.close()
            raise _unsafe("Too many redirects")

        try:
            next_url, next_addresses = _resolve_public_http_url(
                urljoin(current_url, location)
            )
        except (UnsafeUrlError, TypeError, ValueError):
            response.close()
            raise _unsafe() from None
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
        resolved_addresses = next_addresses
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
