"""Automatic translation must publish only after its gated cache commits."""

import datetime as dt
import sqlite3

import pytest

import web_server


@pytest.fixture
def news_db(tmp_path, monkeypatch):
    db_path = tmp_path / "news.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
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
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    return db_path


def _insert_article(news_db, *, id, date, telegraph_url="", has_full_content=0,
                    title="English title", body_html="<p>English body</p>"):
    conn = sqlite3.connect(news_db)
    conn.execute(
        """
        INSERT INTO articles
            (id, title, source, feed_source, origin_source, summary, body_html,
             date, timestamp, has_full_content, telegraph_url)
        VALUES (?, ?, 'Source', 'Source', '', '', ?, ?, ?, ?, ?)
        """,
        (id, title, body_html, date, id, has_full_content, telegraph_url),
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
