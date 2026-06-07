import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import refresh_server


def _setup_news_db(tmp_path):
    db_path = tmp_path / "news.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            feed_source TEXT NOT NULL DEFAULT '',
            origin_source TEXT NOT NULL DEFAULT '',
            time TEXT DEFAULT '',
            date TEXT DEFAULT '',
            timestamp INTEGER NOT NULL DEFAULT 0,
            thumb TEXT DEFAULT '',
            has_full_content INTEGER DEFAULT 0,
            telegraph_url TEXT DEFAULT '',
            body_html TEXT DEFAULT '',
            summary TEXT DEFAULT ''
        )
    """)
    rows = [
        (1, "OpenAI releases model", "Tech Feed", "Tech Feed", "OpenAI", "10:00", "2026-06-07", 300, "", 1, "", "", "New reasoning model details"),
        (2, "Markets rally", "Biz Feed", "Biz Feed", "Reuters", "09:00", "2026-06-07", 200, "", 0, "", "", "Stocks rise after earnings"),
        (3, "Local weather", "City Feed", "City Feed", "", "08:00", "2026-06-07", 100, "", 0, "", "", "Sunny day"),
    ]
    conn.executemany(
        "INSERT INTO articles "
        "(id, title, source, feed_source, origin_source, time, date, timestamp, thumb, "
        "has_full_content, telegraph_url, body_html, summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()

    if refresh_server._db_conn is not None:
        refresh_server._db_conn.close()
    refresh_server._db_conn = None
    refresh_server.DB_FILE = db_path
    return db_path


def _api_news(params):
    return json.loads(refresh_server.api_news_list(params).decode("utf-8"))


def test_api_news_search_matches_title_source_origin_and_summary(tmp_path):
    _setup_news_db(tmp_path)

    assert [item["id"] for item in _api_news({"q": ["openai"]})["items"]] == [1]
    assert [item["id"] for item in _api_news({"q": ["biz feed"]})["items"]] == [2]
    assert [item["id"] for item in _api_news({"q": ["reuters"]})["items"]] == [2]
    assert [item["id"] for item in _api_news({"q": ["sunny"]})["items"]] == [3]


def test_api_news_search_keeps_empty_query_and_pagination_compatible(tmp_path):
    _setup_news_db(tmp_path)

    empty = _api_news({"q": ["   "], "page": ["1"], "size": ["2"]})
    assert empty["total"] == 3
    assert [item["id"] for item in empty["items"]] == [1, 2]

    paged = _api_news({"q": ["feed"], "page": ["2"], "size": ["1"]})
    assert paged["total"] == 3
    assert paged["page"] == 2
    assert paged["size"] == 1
    assert [item["id"] for item in paged["items"]] == [2]


def test_api_news_search_handles_special_characters(tmp_path):
    _setup_news_db(tmp_path)

    result = _api_news({"q": ["%'_"]})
    assert result["total"] == 0
    assert result["items"] == []
