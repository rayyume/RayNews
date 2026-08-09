import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import refresh_server


@pytest.fixture(autouse=True)
def _reset_article_cache(monkeypatch):
    refresh_server.clear_article_cache()
    monkeypatch.setattr(refresh_server, "_article_cache_bytes", 0, raising=False)
    yield
    refresh_server.clear_article_cache()


@pytest.mark.parametrize(
    ("items", "megabytes"),
    [
        ("not-a-number", "64"),
        ("256", "not-a-number"),
        ("-1", "64"),
        ("256", "-1"),
        ("1.5", "64"),
        ("256", "1.5"),
    ],
)
def test_article_cache_config_falls_back_for_invalid_limits(monkeypatch, items, megabytes):
    monkeypatch.setenv("ARTICLE_DETAIL_CACHE_MAX_ITEMS", items)
    monkeypatch.setenv("ARTICLE_DETAIL_CACHE_MAX_MB", megabytes)

    assert refresh_server._article_cache_config_from_env() == {
        "max_items": 256,
        "max_mb": 64,
    }


def test_article_cache_config_accepts_zero_and_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("ARTICLE_DETAIL_CACHE_MAX_ITEMS", " 0 ")
    monkeypatch.setenv("ARTICLE_DETAIL_CACHE_MAX_MB", " 0 ")

    assert refresh_server._article_cache_config_from_env() == {
        "max_items": 0,
        "max_mb": 0,
    }


def test_cache_hit_updates_lru_recency_for_item_eviction(monkeypatch):
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_ITEMS", 2)
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_BYTES", 10)

    assert refresh_server._store_cached_article(1, b"aaaa") is True
    assert refresh_server._store_cached_article(2, b"bbbb") is True
    assert refresh_server._get_cached_article(1) == b"aaaa"
    assert refresh_server._store_cached_article(3, b"cccc") is True

    assert refresh_server._get_cached_article(2) is None
    assert refresh_server._get_cached_article(1) == b"aaaa"
    assert refresh_server._get_cached_article(3) == b"cccc"
    assert refresh_server.refresh_runtime_stats() == {
        "article_cache_items": 2,
        "article_cache_bytes": 8,
        "article_cache_inflight": 0,
    }


def test_cache_evicts_oldest_entry_to_meet_byte_limit(monkeypatch):
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_ITEMS", 10)
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_BYTES", 7)

    assert refresh_server._store_cached_article(1, b"aaaa") is True
    assert refresh_server._store_cached_article(2, b"bbbb") is True

    assert refresh_server._get_cached_article(1) is None
    assert refresh_server._get_cached_article(2) == b"bbbb"
    assert refresh_server.refresh_runtime_stats()["article_cache_bytes"] == 4


def test_replacing_entry_subtracts_old_payload_before_enforcing_limits(monkeypatch):
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_ITEMS", 2)
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_BYTES", 10)
    refresh_server._store_cached_article(1, b"aaaa")
    refresh_server._store_cached_article(2, b"bbbb")

    assert refresh_server._store_cached_article(1, b"aaaaaa") is True

    assert refresh_server._get_cached_article(1) == b"aaaaaa"
    assert refresh_server._get_cached_article(2) == b"bbbb"
    assert refresh_server.refresh_runtime_stats()["article_cache_bytes"] == 10


def test_oversize_replacement_is_rejected_and_removes_old_entry(monkeypatch):
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_ITEMS", 10)
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_BYTES", 10)
    refresh_server._store_cached_article(1, b"old!")

    assert refresh_server._store_cached_article(1, b"x" * 11) is False

    assert refresh_server._get_cached_article(1) is None
    assert refresh_server.refresh_runtime_stats()["article_cache_bytes"] == 0


@pytest.mark.parametrize(
    ("max_items", "max_bytes"),
    [(0, 10), (10, 0)],
)
def test_zero_limit_disables_article_cache(monkeypatch, max_items, max_bytes):
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_ITEMS", max_items)
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_BYTES", max_bytes)

    assert refresh_server._store_cached_article(1, b"data") is False
    assert refresh_server._get_cached_article(1) is None
    assert refresh_server.refresh_runtime_stats()["article_cache_bytes"] == 0


def test_clear_resets_entries_bytes_and_inflight(monkeypatch):
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_ITEMS", 10)
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_BYTES", 10)
    refresh_server._store_cached_article(1, b"aaaa")
    refresh_server._store_cached_article(2, b"bbbb")
    with refresh_server._article_cache_lock:
        refresh_server._article_cache_inflight[3] = refresh_server.threading.Event()

    refresh_server.clear_article_cache()

    assert refresh_server.refresh_runtime_stats() == {
        "article_cache_items": 0,
        "article_cache_bytes": 0,
        "article_cache_inflight": 0,
    }


def test_title_updates_evict_only_changed_articles_and_update_byte_stats(monkeypatch):
    class Rows:
        def fetchall(self):
            return [
                {
                    "id": 7,
                    "title": "New title",
                    "original_title": "Old title",
                    "title_updated_at": "2026-08-09 12:00:00",
                    "title_source": "translation",
                }
            ]

    class Connection:
        def execute(self, _sql, _params):
            return Rows()

        def close(self):
            pass

    monkeypatch.setattr(refresh_server, "get_db", Connection)
    refresh_server._store_cached_article(7, b"stale!")
    refresh_server._store_cached_article(8, b"fresh")

    body = refresh_server.api_title_updates({"since": ["2026-08-09 11:00:00|6"]})

    assert json.loads(body)["items"][0]["id"] == 7
    assert refresh_server._get_cached_article(7) is None
    assert refresh_server._get_cached_article(8) == b"fresh"
    assert refresh_server.refresh_runtime_stats()["article_cache_bytes"] == 5


def test_concurrent_article_detail_requests_keep_single_flight_and_byte_stats(monkeypatch):
    started = threading.Event()
    waiter_joined = threading.Event()
    release = threading.Event()
    calls = []
    payload = b'{"id": 42}'

    class TrackingInflight(dict):
        def get(self, key, default=None):
            event = super().get(key, default)
            if event is not None:
                waiter_joined.set()
            return event

    def build(article_id):
        calls.append(article_id)
        started.set()
        assert release.wait(timeout=2)
        return payload

    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_ITEMS", 10)
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_BYTES", 100)
    monkeypatch.setattr(refresh_server, "_article_cache_inflight", TrackingInflight())
    monkeypatch.setattr(refresh_server, "_build_news_detail_response", build)

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(refresh_server.api_news_detail, 42)
        assert started.wait(timeout=2)
        second = workers.submit(refresh_server.api_news_detail, 42)
        assert waiter_joined.wait(timeout=2)
        release.set()
        assert first.result(timeout=2) == payload
        assert second.result(timeout=2) == payload

    assert calls == [42]
    assert refresh_server.refresh_runtime_stats() == {
        "article_cache_items": 1,
        "article_cache_bytes": 10,
        "article_cache_inflight": 0,
    }


@pytest.mark.parametrize(
    ("max_items", "max_bytes"),
    [(10, 5), (0, 100)],
)
def test_concurrent_uncacheable_article_detail_requests_still_share_one_result(
    monkeypatch, max_items, max_bytes
):
    started = threading.Event()
    waiter_joined = threading.Event()
    release = threading.Event()
    calls = []
    payload = b'{"id": 42}'

    class TrackingInflight(dict):
        def get(self, key, default=None):
            event = super().get(key, default)
            if event is not None:
                waiter_joined.set()
            return event

    def build(article_id):
        calls.append(article_id)
        started.set()
        assert release.wait(timeout=2)
        return payload

    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_ITEMS", max_items)
    monkeypatch.setattr(refresh_server, "ARTICLE_DETAIL_CACHE_MAX_BYTES", max_bytes)
    monkeypatch.setattr(refresh_server, "_article_cache_inflight", TrackingInflight())
    monkeypatch.setattr(refresh_server, "_build_news_detail_response", build)

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(refresh_server.api_news_detail, 42)
        assert started.wait(timeout=2)
        second = workers.submit(refresh_server.api_news_detail, 42)
        assert waiter_joined.wait(timeout=2)
        release.set()
        assert first.result(timeout=2) == payload
        assert second.result(timeout=2) == payload

    assert calls == [42]
    assert refresh_server.refresh_runtime_stats() == {
        "article_cache_items": 0,
        "article_cache_bytes": 0,
        "article_cache_inflight": 0,
    }
