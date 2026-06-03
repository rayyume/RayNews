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


def source_rows(conn: sqlite3.Connection) -> list[dict]:
    init_source_categories(conn)
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
) -> dict:
    if category not in CATEGORY_ORDER:
        raise ValueError("invalid category")
    if status not in VALID_STATUSES:
        raise ValueError("invalid status")
    label = clamp_weighted(label or local_short_source_name(source), 20)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
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
