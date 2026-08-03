"""Network-boundary regressions for external full-text downloads."""

import pytest

import fetcher


class FakeResponse:
    def __init__(self, chunks, *, headers=None, status_error=None):
        self._chunks = chunks
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
        self.status_error = status_error
        self.closed = False
        self.chunk_sizes = []

    def raise_for_status(self):
        if self.status_error is not None:
            raise self.status_error

    def iter_content(self, chunk_size):
        self.chunk_sizes.append(chunk_size)
        yield from self._chunks

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    ("fetch_name", "url", "body", "timeout"),
    [
        (
            "fetch_telegraph",
            "https://telegra.ph/example-01-01",
            b"<article><p>Full Telegraph article body.</p></article>",
            fetcher.FULLTEXT_TIMEOUT,
        ),
        (
            "fetch_wechat_article",
            "https://mp.weixin.qq.com/s/example",
            b'<div id="js_content"><p>Full WeChat article body.</p></div>',
            fetcher.WECHAT_FULLTEXT_TIMEOUT,
        ),
    ],
)
def test_fulltext_fetch_uses_safe_streaming_get(
    monkeypatch,
    fetch_name,
    url,
    body,
    timeout,
):
    response = FakeResponse([body])
    calls = []

    def fake_safe_get(request_url, **kwargs):
        calls.append((request_url, kwargs))
        return response

    monkeypatch.setattr(fetcher, "safe_get", fake_safe_get, raising=False)
    monkeypatch.setattr(
        fetcher.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("full-text fetch bypassed safe_get"),
    )

    result = getattr(fetcher, fetch_name)(url)

    assert result is not None
    assert calls[0][0] == url
    assert calls[0][1]["stream"] is True
    assert calls[0][1]["timeout"] == timeout
    assert response.closed is True


def test_read_body_with_limit_rejects_oversized_stream():
    response = FakeResponse([b"a" * 8, b"b" * 8, b"c"])

    with pytest.raises(ValueError, match="fulltext body too large"):
        fetcher._read_body_with_limit(response, 16)


@pytest.mark.parametrize(
    ("fetch_name", "url"),
    [
        ("fetch_telegraph", "https://telegra.ph/example-01-01"),
        ("fetch_wechat_article", "https://mp.weixin.qq.com/s/example"),
    ],
)
def test_oversized_fulltext_returns_none_and_closes_response(
    monkeypatch,
    fetch_name,
    url,
):
    response = FakeResponse(
        [b"a" * (1024 * 1024), b"b" * (1024 * 1024), b"c"]
    )
    monkeypatch.setattr(fetcher, "safe_get", lambda *args, **kwargs: response, raising=False)

    assert getattr(fetcher, fetch_name)(url) is None
    assert response.closed is True


@pytest.mark.parametrize(
    ("fetch_name", "url"),
    [
        ("fetch_telegraph", "https://telegra.ph/example-01-01"),
        ("fetch_wechat_article", "https://mp.weixin.qq.com/s/example"),
    ],
)
def test_fulltext_http_error_closes_response(monkeypatch, fetch_name, url):
    response = FakeResponse([], status_error=RuntimeError("upstream failed"))
    monkeypatch.setattr(fetcher, "safe_get", lambda *args, **kwargs: response, raising=False)

    assert getattr(fetcher, fetch_name)(url) is None
    assert response.closed is True
