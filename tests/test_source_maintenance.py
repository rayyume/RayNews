"""Tests for the read-only source_rows() / write-heavy maintain_source_categories()
split introduced to keep GET /sources fast during a fetch cycle (cold-start fix)."""

from pathlib import Path
import sqlite3

import source_categories as sc

ROOT = Path(__file__).resolve().parents[1]


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            title TEXT,
            source TEXT,
            feed_source TEXT,
            origin_source TEXT,
            timestamp INTEGER
        )
        """
    )
    return conn


def _add_article(conn, article_id, feed_source, ts=1):
    conn.execute(
        "INSERT INTO articles (id, title, source, feed_source, origin_source, timestamp) "
        "VALUES (?, ?, ?, ?, '', ?)",
        (article_id, f"t{article_id}", feed_source, feed_source, ts),
    )
    conn.commit()


def test_source_rows_does_not_write(monkeypatch):
    """source_rows() must stay read-only: no discovery, no cleanup on the read path."""
    conn = _make_conn()
    sc.init_source_categories(conn)
    _add_article(conn, 1, "全新来源")

    calls = []
    monkeypatch.setattr(sc, "ensure_article_sources",
                        lambda c: calls.append("ensure") or 0)
    monkeypatch.setattr(sc, "cleanup_stale_source_categories",
                        lambda c: calls.append("cleanup") or 0)

    rows = sc.source_rows(conn)

    assert calls == []  # neither bookkeeping pass ran
    # A brand-new source not yet in source_categories still surfaces via the unlinked query
    assert any(r["source"] == "全新来源" for r in rows)


def test_source_rows_bootstraps_tables_on_fresh_db():
    """On a fresh deployment the tables may not exist yet; source_rows must not crash."""
    conn = _make_conn()
    # Note: no init_source_categories() call here.
    rows = sc.source_rows(conn)
    assert isinstance(rows, list)
    # The table should now exist (cheap bootstrap ran).
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='source_categories'"
    ).fetchone()
    assert exists is not None


def test_maintain_discovers_and_cleans(monkeypatch):
    conn = _make_conn()
    sc.init_source_categories(conn)
    _add_article(conn, 1, "来源A")

    # Force bypasses the throttle so the pass runs immediately.
    result = sc.maintain_source_categories(conn, force=True)
    assert result["ran"] is True
    assert result["discovered"] >= 1
    row = conn.execute(
        "SELECT source FROM source_categories WHERE source = '来源A'"
    ).fetchone()
    assert row is not None


def test_maintain_is_throttled(monkeypatch):
    conn = _make_conn()
    sc.init_source_categories(conn)

    # Reset throttle clock so the first non-forced call is allowed.
    monkeypatch.setattr(sc, "_maintenance_last_run", 0.0)

    ensure_calls = []
    monkeypatch.setattr(sc, "ensure_article_sources",
                        lambda c: ensure_calls.append(1) or 0)
    monkeypatch.setattr(sc, "cleanup_stale_source_categories", lambda c: 0)

    first = sc.maintain_source_categories(conn)
    second = sc.maintain_source_categories(conn)

    assert first["ran"] is True
    assert second["ran"] is False  # throttled within the window
    assert len(ensure_calls) == 1


def test_refresh_server_lets_post_fetch_maintenance_be_throttled():
    # A manual/periodic refresh's post-fetch maintenance call no longer forces past
    # the throttle — back-to-back refreshes skip the two full-table scans
    # (source discovery, stale cleanup) when the last run was recent. New/stale
    # sources are still caught by whichever refresh lands after the throttle
    # window (60s) expires.
    source = (ROOT / "refresh_server.py").read_text(encoding="utf-8")
    assert "maintain_source_categories(conn, force=False)" in source
    assert "maintain_source_categories(conn, force=True)" not in source
