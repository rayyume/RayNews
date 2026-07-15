import json
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fetcher
import refresh_server


def test_parse_datetime_falls_back_to_current_beijing_time(monkeypatch):
    real_datetime = fetcher.datetime

    class FixedDatetime:
        @classmethod
        def fromisoformat(cls, value):
            raise ValueError(value)

        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 6, 18, 9, 30, tzinfo=tz)

    monkeypatch.setattr(fetcher, "datetime", FixedDatetime)

    parsed = fetcher.parse_datetime("not-an-iso-time")

    assert parsed["date"] == "2026-06-18"
    assert parsed["time"] == "09:30"
    assert parsed["timestamp"] > 0


def test_fetcher_tracks_failed_message_count_instead_of_boolean():
    source = (ROOT / "fetcher.py").read_text(encoding="utf-8")

    assert "failed_count = 0" in source
    assert "failed_count += 1" in source
    assert "if failed_count:" in source
    assert "{failed_count} message(s) failed" in source
    assert "any_failed = False" not in source
    assert "any_failed = True" not in source


def test_legacy_news_json_deduplicates_by_stable_article_id():
    source = (ROOT / "fetcher.py").read_text(encoding="utf-8")

    assert "def _legacy_news_item_key" in source
    assert "_legacy_news_item_key(item)" in source
    assert "_legacy_news_item_key(entry)" in source
    assert "title + timestamp" not in source
    assert "title|timestamp" not in source


def test_article_detail_sanitizes_dangerous_html_without_losing_images():
    db_path = ROOT / f"tmp-hardening-{uuid.uuid4().hex}.db"
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE articles (
                id INTEGER PRIMARY KEY,
                title TEXT,
                source TEXT,
                feed_source TEXT,
                origin_source TEXT,
                time TEXT,
                date TEXT,
                timestamp INTEGER,
                thumb TEXT,
                has_full_content INTEGER,
                telegraph_url TEXT,
                body_html TEXT,
                summary TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO articles "
            "(id, title, source, feed_source, origin_source, time, date, timestamp, thumb, "
            "has_full_content, telegraph_url, body_html, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "Unsafe",
                "Feed",
                "Feed",
                "",
                "09:00",
                "2026-06-18",
                1,
                "",
                1,
                "",
                '<p onclick="steal()">Hello <a href="javascript:steal()">bad</a>'
                '<img src="https://example.com/a.jpg" onerror="steal()">'
                '<script>steal()</script><iframe src="https://evil.example"></iframe></p>',
                "summary",
            ),
        )
        conn.commit()
        conn.close()

        refresh_server.DB_FILE = db_path
        refresh_server._schema_ready = False
        refresh_server.clear_article_cache()

        data = json.loads(refresh_server.api_news_detail(1).decode("utf-8"))
    finally:
        refresh_server.clear_article_cache()
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)

    assert "script" not in data["body_html"].lower()
    assert "iframe" not in data["body_html"].lower()
    assert "onclick" not in data["body_html"].lower()
    assert "onerror" not in data["body_html"].lower()
    assert "javascript:" not in data["body_html"].lower()
    assert 'src="https://example.com/a.jpg"' in data["body_html"]


def test_refresh_article_detail_cache_computes_once_per_article():
    source = (ROOT / "refresh_server.py").read_text(encoding="utf-8")

    assert "_article_cache_inflight" in source
    assert "event.wait()" in source
    assert "_article_cache_inflight[article_id]" in source


def test_news_db_connection_initialization_uses_lock():
    source = (ROOT / "web_server.py").read_text(encoding="utf-8")
    news_conn_pos = source.index("_news_conn = None")
    get_news_pos = source.index("def _get_news_db()")
    block = source[news_conn_pos:get_news_pos + 500]

    assert "_news_conn_lock = threading.Lock()" in block
    assert "with _news_conn_lock:" in block


def test_ai_result_save_uses_single_upsert_statement():
    source = (ROOT / "web_server.py").read_text(encoding="utf-8")
    start = source.index("def _save_ai_result")
    end = source.index("# ", start)
    block = source[start:end]

    assert "ON CONFLICT(article_id) DO UPDATE SET" in block
    assert "existing = _get_ai_result" not in block


def test_image_prefetch_cache_check_and_pending_mark_share_lock():
    source = (ROOT / "image_cache.py").read_text(encoding="utf-8")
    start = source.index("def enqueue_article_image_prefetch")
    end = source.index("def prune_cache", start)
    block = source[start:end]
    lock_pos = block.index("with _prefetch_lock:")
    cache_check_pos = block.index("get_cached_image(url)")
    pending_add_pos = block.index("_prefetch_pending.add(pending_key)")

    assert lock_pos < cache_check_pos < pending_add_pos


def test_service_worker_auth_requests_bypass_cache():
    sw = (ROOT / "frontend" / "sw.js").read_text(encoding="utf-8")

    assert "url.pathname.startsWith('/auth/')" in sw
    assert "return;" in sw[sw.index("url.pathname.startsWith('/auth/')"):sw.index("url.pathname.startsWith('/api/')")]


def test_api_fetch_returns_structured_auth_expired_result():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function apiFetch")
    end = html.index("async function parseJsonResponse", start)
    block = html[start:end]

    assert "return { error: 'auth_expired', status: 401 };" in block
    assert "return null;" not in block


def test_schema_ready_is_guarded_by_event_instead_of_unsynchronized_fast_path():
    source = (ROOT / "refresh_server.py").read_text(encoding="utf-8")
    start = source.index("def ensure_schema_once")
    end = source.index("def ensure_article_title_columns", start)
    block = source[start:end]

    assert "_schema_ready_event" in source
    assert "_schema_ready_event.is_set()" in block
    assert "_schema_ready =" not in block


def test_sanitize_strips_telegraph_spacer_brs_but_keeps_intext_breaks():
    html = (
        "<article>"
        "<p>First paragraph.</p>"
        "<br><p>Second paragraph.</p>"
        "<br><br><br><p>Third after triple spacer.</p>"
        "<figure><img src=\"https://x/i.jpg\"/><br>"
        "<figcaption>Caption line one<br>line two</figcaption></figure>"
        "</article>"
    )
    out = refresh_server._sanitize_article_html(html)

    # All spacer <br>s directly between blocks / in the figure are gone,
    # so paragraphs sit adjacent and rely on CSS margins for spacing.
    assert "</p><p>" in out
    assert "<br><p>" not in out
    assert "<img" in out and "<figcaption>" in out
    assert "<img/><figcaption>" in out.replace(' src="https://x/i.jpg"', "")
    # Genuine in-text line break inside the figcaption is preserved.
    assert "Caption line one<br/>line two" in out
