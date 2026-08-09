"""Automatic translation must publish only after its gated cache commits."""

import datetime as dt
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest

import web_server


_ARTICLES_SCHEMA = """
    CREATE TABLE articles (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        feed_source TEXT NOT NULL DEFAULT '',
        origin_source TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        body_html TEXT NOT NULL DEFAULT '',
        date TEXT NOT NULL DEFAULT '',
        timestamp INTEGER NOT NULL DEFAULT 0,
        has_full_content INTEGER,
        telegraph_url TEXT
    )
"""


def _create_news_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(_ARTICLES_SCHEMA)
    conn.commit()
    conn.close()


@pytest.fixture
def news_db(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    _create_news_db(db_path)
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    return db_path


def _insert_article(
    news_db,
    *,
    id,
    date,
    telegraph_url="",
    has_full_content=0,
    title="English title",
    body_html="<p>English body</p>",
    timestamp=None,
):
    conn = sqlite3.connect(news_db)
    conn.execute(
        """
        INSERT INTO articles
            (id, title, source, feed_source, origin_source, summary, body_html,
             date, timestamp, has_full_content, telegraph_url)
        VALUES (?, ?, 'Source', 'Source', '', '', ?, ?, ?, ?, ?)
        """,
        (
            id,
            title,
            body_html,
            date,
            id if timestamp is None else timestamp,
            has_full_content,
            telegraph_url,
        ),
    )
    conn.commit()
    conn.close()


def _insert_translations(news_db, translations):
    assert web_server._init_ai_results_table()
    conn = sqlite3.connect(news_db)
    conn.executemany(
        "INSERT INTO ai_results (article_id, translation) VALUES (?, ?)",
        translations,
    )
    conn.commit()
    conn.close()


@pytest.mark.parametrize(("article", "expected"), [
    ({"telegraph_url": "https://telegra.ph/x", "has_full_content": 0}, True),
    ({"telegraph_url": "https://telegra.ph/x", "has_full_content": False}, True),
    ({"telegraph_url": "https://telegra.ph/x", "has_full_content": None}, True),
    ({"telegraph_url": "https://telegra.ph/x", "has_full_content": 1}, False),
    ({"telegraph_url": "", "has_full_content": 0}, False),
    ({"has_full_content": None}, False),
])
def test_translation_pending_fulltext(article, expected):
    assert web_server._translation_pending_fulltext(article) is expected


def test_fetch_untranslated_skips_pending_telegraph_content_but_keeps_title(news_db):
    today = dt.datetime.now().strftime("%Y-%m-%d")
    _insert_article(
        news_db, id=1, date=today, telegraph_url="https://telegra.ph/pending",
        has_full_content=0,
    )
    _insert_article(
        news_db, id=2, date=today, telegraph_url="https://telegra.ph/complete",
        has_full_content=1,
    )
    _insert_article(news_db, id=3, date=today, has_full_content=0)

    content_only = web_server._fetch_untranslated_articles(
        {"auto_translate_title": False, "auto_translate_content": True}, limit=10
    )
    assert [row["id"] for row in content_only] == [3, 2]

    candidates = web_server._fetch_untranslated_articles(
        {"auto_translate_title": True, "auto_translate_content": True}, limit=10
    )
    pending = next(row for row in candidates if row["id"] == 1)
    assert pending["translate_title_needed"] is True
    assert pending["translate_content_needed"] is False


def test_translation_looks_truncated_is_conservative():
    source = "<p>" + ("English source sentence. " * 30) + "</p>"

    assert web_server._translation_looks_truncated(
        {"has_full_content": 1}, "<p>短译文</p>", source
    ) is True
    assert web_server._translation_looks_truncated(
        {"has_full_content": 0}, "<p>短译文</p>", source
    ) is False
    assert web_server._translation_looks_truncated(
        {"has_full_content": 1}, "<p>" + ("完整译文" * 80) + "</p>", source
    ) is False
    assert web_server._translation_looks_truncated(
        {"has_full_content": 1}, "<p>短</p>", "<p>short source</p>"
    ) is False


def test_stale_translation_scan_reaches_old_articles_across_pages(news_db):
    source = "<p>" + ("Historical English source sentence. " * 30) + "</p>"
    complete_translation = "<p>" + ("完整译文" * 100) + "</p>"
    for article_id in range(1, 62):
        _insert_article(
            news_db,
            id=article_id,
            date="2024-01-01",
            has_full_content=1,
            body_html=source,
        )
    _insert_translations(
        news_db,
        [(1, "<p>短译文</p>")]
        + [(article_id, complete_translation) for article_id in range(2, 62)],
    )

    pages = [web_server._fetch_stale_translation_articles(limit=25) for _ in range(3)]

    assert [[row["id"] for row in page] for page in pages] == [[], [], [1]]
    candidate = pages[-1][0]
    assert candidate["translation_stale"] is True
    assert candidate["translate_content_needed"] is True
    assert candidate["translate_title_needed"] is True
    assert web_server._translation_repair_cursor is None


def test_repair_cursor_resets_after_end_of_history_and_finds_new_old_candidate(news_db):
    source = "<p>" + ("Complete English source sentence. " * 30) + "</p>"
    _insert_article(
        news_db,
        id=10,
        date="2023-12-31",
        timestamp=10,
        has_full_content=1,
        body_html=source,
    )
    _insert_translations(news_db, [(10, "<p>" + ("完整译文" * 100) + "</p>")])

    assert web_server._fetch_stale_translation_articles(limit=1) == []
    assert web_server._translation_repair_cursor == (10, 10)
    assert web_server._fetch_stale_translation_articles(limit=1) == []
    assert web_server._translation_repair_cursor is None

    _insert_article(
        news_db,
        id=1,
        date="2020-01-01",
        timestamp=1,
        has_full_content=1,
        title="中文标题",
        body_html=source,
    )
    _insert_translations(news_db, [(1, "<p>短</p>")])

    candidates = web_server._fetch_stale_translation_articles(limit=2)
    assert [row["id"] for row in candidates] == [1]
    assert candidates[0]["translate_title_needed"] is False


def test_repair_cursor_uses_id_as_tiebreaker_for_equal_timestamps(news_db):
    source = "<p>" + ("English source sentence. " * 30) + "</p>"
    for article_id in range(1, 6):
        _insert_article(
            news_db,
            id=article_id,
            date="2022-01-01",
            timestamp=100,
            has_full_content=1,
            body_html=source,
        )
    _insert_translations(
        news_db,
        [(1, "<p>短</p>")]
        + [
            (article_id, "<p>" + ("完整译文" * 100) + "</p>")
            for article_id in range(2, 6)
        ],
    )

    pages = [web_server._fetch_stale_translation_articles(limit=2) for _ in range(3)]

    assert [[row["id"] for row in page] for page in pages] == [[], [], [1]]
    assert web_server._translation_repair_cursor is None


def test_stale_scan_page_contains_only_full_articles_with_nonempty_translations(news_db):
    source = "<p>" + ("English source sentence. " * 30) + "</p>"
    for article_id, has_full_content in ((1, 1), (2, 1), (3, 1), (4, 0)):
        _insert_article(
            news_db,
            id=article_id,
            date="2020-01-01",
            timestamp=article_id,
            has_full_content=has_full_content,
            body_html=source,
        )
    _insert_translations(
        news_db,
        [(1, "<p>短</p>"), (3, "   "), (4, "<p>短</p>")],
    )

    candidates = web_server._fetch_stale_translation_articles(limit=1)

    assert [row["id"] for row in candidates] == [1]


def test_repair_cursor_is_isolated_when_news_database_changes(
    news_db, tmp_path, monkeypatch
):
    source = "<p>" + ("English source sentence. " * 30) + "</p>"
    _insert_article(
        news_db,
        id=1,
        date="2024-01-01",
        timestamp=50,
        has_full_content=1,
        body_html=source,
    )
    _insert_translations(news_db, [(1, "<p>" + ("完整译文" * 100) + "</p>")])
    assert web_server._fetch_stale_translation_articles(limit=1) == []
    assert web_server._translation_repair_cursor == (50, 1)

    second_db = tmp_path / "second-news.db"
    _create_news_db(second_db)
    monkeypatch.setattr(web_server, "NEWS_DB", str(second_db))
    _insert_article(
        second_db,
        id=9,
        date="2021-01-01",
        timestamp=100,
        has_full_content=1,
        body_html=source,
    )
    _insert_translations(second_db, [(9, "<p>短</p>")])

    assert [
        row["id"] for row in web_server._fetch_stale_translation_articles(limit=1)
    ] == [9]


def test_concurrent_stale_scans_claim_distinct_keyset_pages(news_db, monkeypatch):
    source = "<p>" + ("English source sentence. " * 30) + "</p>"
    for article_id in (1, 2):
        _insert_article(
            news_db,
            id=article_id,
            date="2022-01-01",
            timestamp=article_id,
            has_full_content=1,
            body_html=source,
        )
    _insert_translations(news_db, [(1, "<p>短</p>"), (2, "<p>短</p>")])

    original_news_db_conn = web_server._news_db_conn
    first_connection_started = threading.Event()
    connection_count_lock = threading.Lock()
    connection_count = 0

    @contextmanager
    def delayed_first_connection():
        nonlocal connection_count
        with connection_count_lock:
            connection_count += 1
            is_first = connection_count == 1
        if is_first:
            first_connection_started.set()
            time.sleep(0.1)
        with original_news_db_conn() as conn:
            yield conn

    monkeypatch.setattr(web_server, "_news_db_conn", delayed_first_connection)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(web_server._fetch_stale_translation_articles, 1)
        assert first_connection_started.wait(timeout=1)
        second = pool.submit(web_server._fetch_stale_translation_articles, 1)
        claimed = first.result(timeout=2) + second.result(timeout=2)

    assert sorted(row["id"] for row in claimed) == [1, 2]


def test_auto_translation_publishes_marker_after_cache_without_body_writeback(monkeypatch):
    calls = []

    class TranslationService:
        def __init__(self, **kwargs):
            pass

        def translate_full(self, *args, **kwargs):
            return {"title": "中文标题", "html": "<p>中文正文</p>"}

    monkeypatch.setattr(web_server, "AIService", TranslationService)
    monkeypatch.setattr(
        web_server,
        "_save_article_translation",
        lambda article_id, title=None, body_html=None: calls.append(
            ("article", article_id, title, body_html)
        ) or True,
    )
    monkeypatch.setattr(
        web_server,
        "_save_ai_result",
        lambda article_id, **kwargs: calls.append(("cache", article_id, kwargs["translation"])),
    )
    monkeypatch.setattr(
        web_server,
        "_publish_translation_update",
        lambda article_id: calls.append(("marker", article_id)),
    )

    assert web_server._translate_article_background(
        {
            "id": 42,
            "title": "English title",
            "body_html": "<p>English body</p>",
            "translate_content_needed": True,
            "translate_title_needed": True,
        },
        {"api_key": "key", "endpoint": "https://example.test", "model": "model"},
    )

    assert [call[0] for call in calls] == ["article", "cache", "marker"]
    assert calls[0][3] is None
