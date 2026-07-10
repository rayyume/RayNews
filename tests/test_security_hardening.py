"""Regression tests for the security/correctness-review fixes:
1. Non-admin users can no longer rewrite the canonical (site-wide) article
   title via POST /ai/result/<id>.
2. Manually-translated article HTML is whitelist-sanitized in the browser
   before it's ever innerHTML-rendered, not just when it later enters the
   shared server-side cache.
3. Daily-summary send state is persisted per (date, user_id) instead of an
   in-memory set, so a mid-window restart can't cause a duplicate broadcast
   and a single recipient's transient failure gets retried.
4. refresh_server.py's in-memory article-detail cache is invalidated
   immediately when web_server.py updates an article's title/body, instead
   of only self-healing on the next ~15min fetcher cycle.
"""

import datetime as dt
import os
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import notifier
import refresh_server
import web_server


def test_ai_save_result_only_syncs_title_for_admin():
    src = (ROOT / "web_server.py").read_text(encoding="utf-8")
    start = src.index("def ai_save_result(article_id):")
    end = src.index("def _run_ai_connection_test", start)
    block = src[start:end]
    # Any logged-in user may still contribute to the shared summary/translation
    # cache...
    assert '_save_ai_result(article_id, **kwargs)' in block
    # ...but only an admin's submission is allowed to rewrite the article's
    # canonical, site-wide-visible title.
    assert 'if translation and g.user_role == "admin":' in block
    assert "_save_article_title_update(article_id, translated_title, \"translation\")" in block


def test_frontend_sanitizes_translated_html_before_first_render():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function sanitizeTranslatedHtml(html)" in html
    # Same whitelist as web_server.py's _sanitize_translated_html, kept in sync.
    for tag in ("'p'", "'a'", "'img'", "'table'"):
        assert tag in html
    assert "SANITIZE_ALLOWED_ATTRS" in html

    apply_start = html.index("const applyTranslation = (translatedHtmlRaw, translatedTitle) => {")
    apply_end = html.index("};", apply_start)
    apply_block = html[apply_start:apply_end]
    # Sanitize must run before proxyImages()/innerHTML — this is the function
    # every render path (cached, legacy-cached, and freshly browser-generated)
    # funnels through, so fixing it here covers all three.
    assert "proxyImages(sanitizeTranslatedHtml(translatedHtmlRaw))" in apply_block


def temp_db_path():
    return ROOT / f"tmp-cache-evict-test-{uuid.uuid4().hex}.db"


def test_cache_evict_endpoint_pops_only_the_given_article():
    refresh_server._article_cache[5] = b'{"stale": true}'
    refresh_server._article_cache[6] = b'{"other": true}'
    try:
        body, status = refresh_server.api_cache_evict({"id": ["5"]})
        assert status == 200
        assert 5 not in refresh_server._article_cache
        assert 6 in refresh_server._article_cache  # untouched
    finally:
        refresh_server._article_cache.pop(5, None)
        refresh_server._article_cache.pop(6, None)


def test_cache_evict_rejects_non_numeric_id():
    body, status = refresh_server.api_cache_evict({"id": ["not-a-number"]})
    assert status == 400


def test_title_update_invalidates_refresh_server_cache(monkeypatch):
    db_path = temp_db_path()
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        class FakeResp:
            pass
        return FakeResp()

    monkeypatch.setattr(web_server.requests, "get", fake_get)
    old_news_db = web_server.NEWS_DB
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO articles (id, title) VALUES (1, 'Old Title')")
        conn.commit()
        conn.close()

        web_server.NEWS_DB = str(db_path)
        changed = web_server._save_article_title_update(1, "New Title", "translation")
        assert changed is True
        assert len(calls) == 1
        assert calls[0][0] == "http://127.0.0.1:8081/internal/cache-evict"
        assert calls[0][1] == {"id": 1}
    finally:
        web_server.NEWS_DB = old_news_db
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


def test_body_translation_invalidates_refresh_server_cache(monkeypatch):
    db_path = temp_db_path()
    calls = []
    monkeypatch.setattr(
        web_server.requests, "get",
        lambda url, params=None, timeout=None: calls.append((url, params)),
    )
    old_news_db = web_server.NEWS_DB
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT, body_html TEXT)")
        conn.execute("INSERT INTO articles (id, title, body_html) VALUES (1, 'Title', 'old body')")
        conn.commit()
        conn.close()

        web_server.NEWS_DB = str(db_path)
        web_server._save_article_translation(1, body_html="<p>new body</p>")
        assert ("http://127.0.0.1:8081/internal/cache-evict", {"id": 1}) in calls
    finally:
        web_server.NEWS_DB = old_news_db
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


class FakeUserSettingsDb:
    """Stands in for models.get_db() — only the one query
    _broadcast_daily_summary issues against it."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, *args):
        assert "user_settings" in sql
        return self

    def fetchall(self):
        return self._rows


def _setup_daily_summary_test(monkeypatch, db_path, subscriber_rows, send_results):
    """send_results: dict to_email -> True (succeeds) | Exception (raises)."""
    sqlite3.connect(str(db_path)).close()  # the sends helpers no-op unless NEWS_DB exists
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setattr(web_server, "NEWS_DB", str(db_path))
    monkeypatch.setattr(web_server, "get_db", lambda: FakeUserSettingsDb(subscriber_rows))
    monkeypatch.setattr(
        web_server, "_generate_daily_summary_global",
        lambda date_str: {"summary": "today's news", "stats": {}},
    )
    # Beijing 21:00 — inside the default 10-minute send window.
    fixed_now = dt.datetime(2026, 7, 10, 21, 3, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    monkeypatch.setattr(web_server, "_beijing_now", lambda: fixed_now)

    sent_log = []

    def fake_send(api_key, to_email, summary, stats):
        sent_log.append(to_email)
        outcome = send_results.get(to_email, True)
        if outcome is not True:
            raise outcome
        return True

    monkeypatch.setattr(notifier, "send_daily_summary_email", fake_send)
    return sent_log


def test_daily_summary_persists_send_state_so_restart_does_not_duplicate(monkeypatch):
    db_path = ROOT / f"tmp-daily-summary-test-{uuid.uuid4().hex}.db"
    rows = [
        {"user_id": 1, "notification_config": '{"resend":{"to_email":"a@example.com"}}'},
        {"user_id": 2, "notification_config": '{"resend":{"to_email":"b@example.com"}}'},
    ]
    sent_log = _setup_daily_summary_test(monkeypatch, db_path, rows, {})
    try:
        # First run (e.g. the scheduler's first tick inside today's window).
        result1 = web_server._broadcast_daily_summary(force=False)
        assert result1["status"] == "ok"
        assert result1["sent"] == 2
        assert sorted(sent_log) == ["a@example.com", "b@example.com"]

        # Simulate a process restart: in the old code this reset the
        # in-memory "already sent today" set. The persisted table must still
        # remember both recipients were already sent, so a second tick in
        # the same window (as would happen right after a restart) must NOT
        # re-send.
        sent_log.clear()
        result2 = web_server._broadcast_daily_summary(force=False)
        assert result2["status"] == "skipped"
        assert result2["reason"] == "already sent today"
        assert sent_log == []
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


def test_daily_summary_retries_only_the_recipient_who_previously_failed(monkeypatch):
    db_path = ROOT / f"tmp-daily-summary-test-{uuid.uuid4().hex}.db"
    rows = [
        {"user_id": 1, "notification_config": '{"resend":{"to_email":"ok@example.com"}}'},
        {"user_id": 2, "notification_config": '{"resend":{"to_email":"bad@example.com"}}'},
    ]
    sent_log = _setup_daily_summary_test(
        monkeypatch, db_path, rows, {"bad@example.com": RuntimeError("smtp down")},
    )
    try:
        result1 = web_server._broadcast_daily_summary(force=False)
        assert result1["status"] == "ok"
        assert result1["sent"] == 1  # only ok@example.com succeeded
        assert sorted(sent_log) == ["bad@example.com", "ok@example.com"]

        # Fix the transient failure, then simulate the scheduler's next tick.
        sent_log.clear()
        monkeypatch.setattr(
            notifier, "send_daily_summary_email",
            lambda api_key, to_email, summary, stats: sent_log.append(to_email),
        )
        result2 = web_server._broadcast_daily_summary(force=False)
        assert result2["status"] == "ok"
        assert result2["sent"] == 1
        # Only the previously-failed recipient gets retried — the one that
        # already succeeded must not receive a duplicate email.
        assert sent_log == ["bad@example.com"]
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


def test_daily_summary_force_resends_regardless_of_persisted_history(monkeypatch):
    db_path = ROOT / f"tmp-daily-summary-test-{uuid.uuid4().hex}.db"
    rows = [{"user_id": 1, "notification_config": '{"resend":{"to_email":"a@example.com"}}'}]
    sent_log = _setup_daily_summary_test(monkeypatch, db_path, rows, {})
    try:
        web_server._broadcast_daily_summary(force=False)
        assert sent_log == ["a@example.com"]

        sent_log.clear()
        result = web_server._broadcast_daily_summary(force=True)
        assert result["status"] == "ok"
        assert result["sent"] == 1
        assert sent_log == ["a@example.com"]  # admin's manual re-send goes through
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass
