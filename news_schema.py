"""Shared SQLite schema helpers for RayNews news data."""

from __future__ import annotations

import sqlite3


def ensure_deleted_articles_table(conn: sqlite3.Connection) -> None:
    """Create the article deletion tombstone table when needed."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deleted_articles (
            article_id   INTEGER PRIMARY KEY,
            title        TEXT NOT NULL DEFAULT '',
            source       TEXT NOT NULL DEFAULT '',
            deleted_by   INTEGER,
            deleted_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
