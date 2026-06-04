"""Shared source category metadata helpers for RayNews."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime


CATEGORY_ORDER = ["News", "Tech", "Biz", "Info"]
CATEGORY_NAMES = {
    "News": "政经新闻",
    "Tech": "科技动态",
    "Biz": "商业聚焦",
    "Info": "其他信息",
}

INITIAL_CATEGORY_MAP = {
    "News": ["竹新社", "风向旗参考快讯", "界面新闻", "即刻精选", "联合早报"],
    "Tech": [
        "凤凰网科技", "cnBeta", "知识分子", "逛逛GitHub", "开源日记",
        "科技圈🎗在花频道📮", "MacRumors", "少数派", "爱范儿", "Chiphell",
        "APPDO 数字生活指南", "XP Digital Lab", "RaysBlog", "Yummy 😋",
        "阮一峰", "小声逼逼",
    ],
    "Biz": [
        "金十数据", "格隆汇", "财经早餐", "凤凰网财经", "晚点", "投资界",
        "创业最前线", "WBusiness商业", "包邮区", "理想生活实验室",
    ],
    "Info": ["美卡指南", "蓝翼说", "酒店圈儿", "银探", "济南本地宝"],
}

VALID_STATUSES = {"pending", "classified", "manual", "failed"}
INITIAL_SOURCES = {
    source
    for sources in INITIAL_CATEGORY_MAP.values()
    for source in sources
}


def weighted_len(text: str) -> int:
    """ASCII counts as 1, non-ASCII counts as 2."""
    return sum(1 if ord(ch) < 128 else 2 for ch in text)


def clamp_weighted(text: str, limit: int = 20) -> str:
    result = []
    total = 0
    for ch in (text or "").strip():
        weight = 1 if ord(ch) < 128 else 2
        if total + weight > limit:
            break
        result.append(ch)
        total += weight
    return "".join(result).strip()


def local_short_source_name(source: str) -> str:
    """Deterministic fallback cleanup for verbose source names."""
    text = (source or "").strip()
    text = re.sub(r"\s*-\s*Telegram\s+Channel\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*\|\s*Telegram\s+Channel\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*\(\s*Telegram\s+Channel\s*\)\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*频道\s*$", "", text)
    text = re.sub(r"[\U0001F000-\U0010FFFF]", "", text).strip()
    text = re.sub(r"\s+", " ", text).strip()

    # Common Telegram display-name cleanup: keep the representative brand words.
    if "科技圈" in text and "在花" in text:
        return "在花科技圈"

    return clamp_weighted(text or source, 20)


def init_source_categories(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_categories (
            source TEXT PRIMARY KEY,
            category TEXT NOT NULL DEFAULT 'Info',
            label TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            confidence REAL,
            reason TEXT,
            sample_titles TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source_categories_status ON source_categories(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source_categories_category ON source_categories(category)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_aliases (
            alias_source  TEXT PRIMARY KEY,
            target_source TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_source_categories (
            user_id    INTEGER NOT NULL,
            source     TEXT NOT NULL,
            category   TEXT NOT NULL DEFAULT 'Info',
            label      TEXT NOT NULL DEFAULT '',
            status     TEXT NOT NULL DEFAULT 'manual',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, source)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_source_aliases (
            user_id       INTEGER NOT NULL,
            alias_source  TEXT NOT NULL,
            target_source TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, alias_source)
        )
    """)

    for category, sources in INITIAL_CATEGORY_MAP.items():
        for source in sources:
            conn.execute(
                """
                INSERT OR IGNORE INTO source_categories
                    (source, category, label, status, reason)
                VALUES (?, ?, ?, 'pending', 'seeded')
                """,
                (source, category, local_short_source_name(source)),
            )
    conn.commit()


def ensure_article_sources(conn: sqlite3.Connection) -> int:
    """Insert pending source records for every distinct article source."""
    init_source_categories(conn)
    aliases = conn.execute("SELECT alias_source, target_source FROM source_aliases").fetchall()
    for row in aliases:
        alias = row["alias_source"] if isinstance(row, sqlite3.Row) else row[0]
        target = row["target_source"] if isinstance(row, sqlite3.Row) else row[1]
        conn.execute("UPDATE articles SET source = ? WHERE source = ?", (target, alias))

    rows = conn.execute(
        "SELECT DISTINCT source FROM articles WHERE source IS NOT NULL AND TRIM(source) != ''"
    ).fetchall()
    inserted = 0
    for row in rows:
        source = row["source"] if isinstance(row, sqlite3.Row) else row[0]
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO source_categories
                (source, category, label, status, reason)
            VALUES (?, 'Info', ?, 'pending', 'discovered')
            """,
            (source, local_short_source_name(source)),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def cleanup_stale_source_categories(conn: sqlite3.Connection) -> int:
    """Remove discovered source rows that no longer have articles."""
    initial_sources = set(INITIAL_SOURCES)
    rows = conn.execute(
        """
        SELECT sc.source, sc.status, COUNT(a.id) AS article_count
        FROM source_categories sc
        LEFT JOIN articles a ON a.source = sc.source
        GROUP BY sc.source
        HAVING article_count = 0
        """
    ).fetchall()
    deleted = 0
    for row in rows:
        source = row["source"] if isinstance(row, sqlite3.Row) else row[0]
        status = row["status"] if isinstance(row, sqlite3.Row) else row[1]
        if source in initial_sources or status == "manual":
            continue
        alias_ref = conn.execute(
            """
            SELECT 1 FROM source_aliases
            WHERE alias_source = ? OR target_source = ?
            LIMIT 1
            """,
            (source, source),
        ).fetchone()
        if alias_ref:
            continue
        cur = conn.execute("DELETE FROM source_categories WHERE source = ?", (source,))
        deleted += cur.rowcount
    if deleted:
        conn.commit()
    return deleted


def find_merge_target(conn: sqlite3.Connection, source: str, label: str) -> str | None:
    """Find an existing source whose source name or label matches label."""
    label = (label or "").strip()
    if not label:
        return None
    rows = conn.execute(
        """
        SELECT sc.source, sc.status, COUNT(a.id) AS article_count
        FROM source_categories sc
        LEFT JOIN articles a ON a.source = sc.source
        WHERE sc.source != ?
          AND (sc.source = ? OR sc.label = ?)
        GROUP BY sc.source
        ORDER BY
          CASE sc.status WHEN 'manual' THEN 0 WHEN 'classified' THEN 1 ELSE 2 END,
          article_count DESC,
          sc.source COLLATE NOCASE
        LIMIT 1
        """,
        (source, label, label),
    ).fetchone()
    if not rows:
        return None
    return rows["source"] if isinstance(rows, sqlite3.Row) else rows[0]


def find_user_merge_target(conn: sqlite3.Connection, user_id: int, source: str, label: str) -> str | None:
    label = (label or "").strip()
    if not label:
        return None
    rows = effective_source_rows(conn, user_id)
    candidates = [
        row for row in rows
        if row.get("source") != source
        and not row.get("alias_target")
        and (row.get("source") == label or row.get("label") == label)
    ]
    candidates.sort(key=lambda row: (
        0 if row.get("status") == "manual" else 1,
        -(row.get("article_count") or 0),
        row.get("source") or "",
    ))
    return candidates[0]["source"] if candidates else None


def merge_source(conn: sqlite3.Connection, source: str, target_source: str,
                 user_id: int | None = None) -> dict:
    """Merge source into target_source. User merges are private to that user."""
    if source == target_source:
        raise ValueError("cannot merge a source into itself")
    target = conn.execute(
        "SELECT * FROM source_categories WHERE source = ?",
        (target_source,),
    ).fetchone()
    if not target:
        raise ValueError("target source not found")

    if user_id is not None:
        conn.execute(
            """
            INSERT INTO user_source_aliases (user_id, alias_source, target_source)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, alias_source) DO UPDATE SET
                target_source = excluded.target_source
            """,
            (user_id, source, target_source),
        )
        conn.commit()
        return dict(target)

    conn.execute("UPDATE articles SET source = ? WHERE source = ?", (target_source, source))
    conn.execute(
        """
        INSERT INTO source_aliases (alias_source, target_source)
        VALUES (?, ?)
        ON CONFLICT(alias_source) DO UPDATE SET
            target_source = excluded.target_source
        """,
        (source, target_source),
    )
    conn.execute("DELETE FROM source_categories WHERE source = ?", (source,))
    conn.commit()
    return dict(target)


def source_rows(conn: sqlite3.Connection) -> list[dict]:
    ensure_article_sources(conn)
    rows = conn.execute(
        """
        SELECT sc.source, sc.category, sc.label, sc.status, sc.confidence,
               sc.reason, sc.sample_titles, sc.updated_at,
               COUNT(a.id) AS article_count,
               MAX(a.timestamp) AS latest_timestamp
        FROM source_categories sc
        LEFT JOIN articles a ON a.source = sc.source
        GROUP BY sc.source
        ORDER BY article_count DESC, sc.source COLLATE NOCASE
        """
    ).fetchall()
    return [dict(row) for row in rows]


def effective_source_rows(conn: sqlite3.Connection, user_id: int | None = None) -> list[dict]:
    """Return shared source rows overlaid with a user's manual categories/aliases."""
    rows = source_rows(conn)
    if user_id is None:
        return rows

    by_source = {row["source"]: dict(row) for row in rows}
    overrides = conn.execute(
        "SELECT source, category, label, status, updated_at FROM user_source_categories WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    for row in overrides:
        source = row["source"]
        base = by_source.get(source, {
            "source": source,
            "article_count": 0,
            "latest_timestamp": None,
            "confidence": None,
            "reason": "",
            "sample_titles": None,
        })
        base.update({
            "category": row["category"],
            "label": row["label"],
            "status": row["status"],
            "updated_at": row["updated_at"],
            "user_override": True,
        })
        by_source[source] = base

    aliases = conn.execute(
        "SELECT alias_source, target_source FROM user_source_aliases WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    for row in aliases:
        alias = row["alias_source"]
        target = row["target_source"]
        target_row = by_source.get(target)
        alias_row = by_source.get(alias)
        if not target_row:
            continue
        if alias_row:
            target_row["article_count"] = (target_row.get("article_count") or 0) + (alias_row.get("article_count") or 0)
            by_source[alias] = {
                **alias_row,
                "category": target_row.get("category", "Info"),
                "label": target_row.get("label") or target,
                "status": "manual",
                "alias_target": target,
                "user_override": True,
            }
        target_row["has_aliases"] = True

    return sorted(by_source.values(), key=lambda row: (-(row.get("article_count") or 0), row.get("source") or ""))


def source_aliases_for_target(conn: sqlite3.Connection, user_id: int, target_source: str) -> list[str]:
    rows = conn.execute(
        "SELECT alias_source FROM user_source_aliases WHERE user_id = ? AND target_source = ?",
        (user_id, target_source),
    ).fetchall()
    return [row["alias_source"] if isinstance(row, sqlite3.Row) else row[0] for row in rows]


def recent_titles_for_source(conn: sqlite3.Connection, source: str, limit: int = 8) -> list[str]:
    rows = conn.execute(
        """
        SELECT title FROM articles
        WHERE source = ? AND title IS NOT NULL AND TRIM(title) != ''
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (source, limit),
    ).fetchall()
    return [row["title"] if isinstance(row, sqlite3.Row) else row[0] for row in rows]


def update_source_category(
    conn: sqlite3.Connection,
    source: str,
    category: str,
    label: str,
    status: str = "manual",
    confidence: float | None = None,
    reason: str | None = None,
    sample_titles: list[str] | None = None,
    user_id: int | None = None,
) -> dict:
    if category not in CATEGORY_ORDER:
        raise ValueError("invalid category")
    if status not in VALID_STATUSES:
        raise ValueError("invalid status")
    label = clamp_weighted(label or local_short_source_name(source), 20)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if user_id is not None:
        conn.execute(
            """
            INSERT INTO user_source_categories
                (user_id, source, category, label, status, updated_at)
            VALUES (?, ?, ?, ?, 'manual', ?)
            ON CONFLICT(user_id, source) DO UPDATE SET
                category = excluded.category,
                label = excluded.label,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (user_id, source, category, label, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT source, category, label, status, updated_at FROM user_source_categories WHERE user_id = ? AND source = ?",
            (user_id, source),
        ).fetchone()
        return dict(row)

    conn.execute(
        """
        INSERT INTO source_categories
            (source, category, label, status, confidence, reason, sample_titles, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            category = excluded.category,
            label = excluded.label,
            status = excluded.status,
            confidence = excluded.confidence,
            reason = excluded.reason,
            sample_titles = excluded.sample_titles,
            updated_at = excluded.updated_at
        """,
        (
            source,
            category,
            label,
            status,
            confidence,
            reason,
            json.dumps(sample_titles or [], ensure_ascii=False),
            now,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM source_categories WHERE source = ?", (source,)).fetchone()
    return dict(row)
