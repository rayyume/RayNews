"""Shared source category metadata helpers for RayNews."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

from news_schema import ensure_article_source_columns as _ensure_article_source_columns


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


def ensure_article_source_columns(conn: sqlite3.Connection) -> None:
    """Upgrade split source fields through the shared migration protocol."""
    _ensure_article_source_columns(conn)

# ─── Domain → Source mapping (used by fetcher + AI classification) ──
# Format: "domain" → ("source_display_name", "default_category")
# When detect_source() finds a domain in article content, it uses this
# mapping to assign a source name without waiting for AI classification.
KNOWN_DOMAINS: dict[str, tuple[str, str]] = {
    # ── News 政经新闻 ──
    "zaobao.com": ("联合早报", "News"),
    "jiemian.com": ("界面新闻", "News"),
    "ifeng.com": ("凤凰网", "News"),
    "thepaper.cn": ("澎湃新闻", "News"),
    "bbc.com": ("BBC", "News"),
    "bbc.co.uk": ("BBC", "News"),
    "reuters.com": ("路透社", "News"),
    "wsj.com": ("华尔街日报", "News"),
    "ft.com": ("金融时报", "News"),
    "nytimes.com": ("纽约时报", "News"),
    "bloomberg.com": ("彭博社", "News"),
    "cnn.com": ("CNN", "News"),
    "theguardian.com": ("卫报", "News"),
    "scmp.com": ("南华早报", "News"),
    "dw.com": ("德国之声", "News"),
    "rfi.fr": ("法国国际广播", "News"),
    "nikkei.com": ("日经新闻", "News"),
    "yicai.com": ("第一财经", "News"),
    "apnews.com": ("美联社", "News"),
    "aljazeera.com": ("半岛电视台", "News"),
    "france24.com": ("France 24", "News"),
    "huanqiu.com": ("环球网", "News"),
    "guancha.cn": ("观察者网", "News"),
    "cna.com.tw": ("中央社", "News"),
    "ltn.com.tw": ("自由时报", "News"),
    "udn.com": ("联合报", "News"),
    "straitstimes.com": ("海峡时报", "News"),
    "rthk.hk": ("香港电台", "News"),

    # ── Tech 科技动态 ──
    "cnbeta.com": ("cnBeta", "Tech"),
    "cnbeta.com.tw": ("cnBeta", "Tech"),
    "sspai.com": ("少数派", "Tech"),
    "ifanr.com": ("爱范儿", "Tech"),
    "36kr.com": ("36氪", "Tech"),
    "chiphell.com": ("Chiphell", "Tech"),
    "macrumors.com": ("MacRumors", "Tech"),
    "techcrunch.com": ("TechCrunch", "Tech"),
    "theverge.com": ("The Verge", "Tech"),
    "arstechnica.com": ("Ars Technica", "Tech"),
    "wired.com": ("Wired", "Tech"),
    "github.com": ("GitHub", "Tech"),
    "ruanyifeng.com": ("阮一峰", "Tech"),
    "zhihu.com": ("知乎", "Tech"),
    "ithome.com": ("IT之家", "Tech"),
    "solidot.org": ("Solidot", "Tech"),
    "producthunt.com": ("Product Hunt", "Tech"),
    "xiaohongshu.com": ("小红书", "Tech"),
    "huggingface.co": ("HuggingFace", "Tech"),
    "openai.com": ("OpenAI", "Tech"),
    "anthropic.com": ("Anthropic", "Tech"),
    "9to5mac.com": ("9to5Mac", "Tech"),
    "oschina.net": ("开源中国", "Tech"),
    "v2ex.com": ("V2EX", "Tech"),
    "nodeseek.com": ("NodeSeek", "Tech"),
    "hackernews.com": ("Hacker News", "Tech"),
    "infoq.cn": ("InfoQ", "Tech"),
    "geekpark.net": ("极客公园", "Tech"),
    "pingwest.com": ("品玩", "Tech"),
    "sohu.com": ("搜狐", "Tech"),

    # ── Biz 商业聚焦 ──
    "gelonghui.com": ("格隆汇", "Biz"),
    "jin10.com": ("金十数据", "Biz"),
    "pedaily.cn": ("投资界", "Biz"),
    "wallstreetcn.com": ("华尔街见闻", "Biz"),
    "latepost.com": ("晚点", "Biz"),
    "cls.cn": ("财联社", "Biz"),
    "eastmoney.com": ("东方财富", "Biz"),
    "sina.com.cn": ("新浪财经", "Biz"),
    "fortunechina.com": ("财富中文网", "Biz"),
    "hbr.org": ("哈佛商业评论", "Biz"),
    "caixin.com": ("财新", "Biz"),
    "fortune.com": ("财富", "Biz"),
    "fastcompany.com": ("Fast Company", "Biz"),
    "cnbc.com": ("CNBC", "Biz"),
    "economist.com": ("经济学人", "Biz"),
    "businessinsider.com": ("商业内幕", "Biz"),
    "forbes.com": ("福布斯", "Biz"),
    "barrons.com": ("巴伦周刊", "Biz"),
    "stcn.com": ("证券时报", "Biz"),
    "21jingji.com": ("21世纪经济报道", "Biz"),
    "nbd.com.cn": ("每日经济新闻", "Biz"),
    "10jqka.com.cn": ("同花顺", "Biz"),
    "ce.cn": ("中国经济网", "Biz"),
    "cnstock.com": ("上海证券报", "Biz"),
    "cs.com.cn": ("中证网", "Biz"),

    # ── Info 其他信息 ──
    "uscreditcardguide.com": ("美卡指南", "Info"),
    "travelafterwork.com": ("酒店圈儿", "Info"),

    # ── 微信公众号 (域名为 mp.weixin.qq.com, 但会根据文章内容进一步识别) ──
    # 不在 KNOWN_DOMAINS 中注册 weixin 域名，因为不同公众号是不同的来源
}

# Domains to exclude from extraction (platform/aggregator domains)
_DOMAIN_EXCLUDE = {
    "telegra.ph", "t.me", "telegram.me", "telegram.org",
    "mp.weixin.qq.com", "weixin.qq.com",
    "x.com", "twitter.com", "facebook.com", "fb.com",
    "instagram.com", "youtube.com", "youtu.be",
    "reddit.com", "redd.it",
    "google.com", "bing.com", "baidu.com",
    "amazon.com", "apple.com",
    "news.rayyu.me", "localhost", "127.0.0.1",
}


def _root_domain(host: str) -> str | None:
    """Normalize a hostname to its matchable root domain."""
    host = host.lower().strip()
    # Strip leading "www." or "wwwN." patterns
    host = re.sub(r'^www\d*\.', '', host)
    # Extract root domain (last two parts for known multi-part TLDs)
    parts = host.split(".")
    if len(parts) >= 2:
        # Handle com.cn / co.uk / com.tw etc.
        if parts[-2] in ("com", "co", "org", "net", "gov", "edu", "ac") and len(parts) >= 3:
            root = ".".join(parts[-3:])
        else:
            root = ".".join(parts[-2:])
    else:
        root = host
    return None if root in _DOMAIN_EXCLUDE else root


def extract_domain_from_url(value: str) -> str | None:
    """Return a matchable root domain from an HTTP(S) URL."""
    try:
        parsed = urlsplit((value or "").strip())
        host = (parsed.hostname or "").lower()
    except (TypeError, ValueError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return None
    return _root_domain(host)


def extract_domains_from_html(html: str) -> list[str]:
    """Extract unique root domains from all href links in HTML.

    Strips subdomains (www.zaobao.com → zaobao.com), excludes
    platform/aggregator domains, and returns unique results in
    discovery order.
    """
    if not html:
        return []
    urls = re.findall(r'href=["\']?https?://([^/"\'<>\s]+)', html or "")
    seen = set()
    domains = []
    for host in urls:
        root = _root_domain(host)
        if not root:
            continue
        if root not in seen:
            seen.add(root)
            domains.append(root)
    return domains


def lookup_source_by_domain(domains: list[str]) -> tuple[str, str] | None:
    """Look up (source_name, category) from a list of domains.

    Returns the first match found, or None if no domain is known.
    """
    for domain in domains:
        if domain in KNOWN_DOMAINS:
            return KNOWN_DOMAINS[domain]
    return None


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

    known_sources = [
        "包邮区", "金十数据", "投资界", "XP Digital Lab", "凤凰网财经",
        "凤凰网科技", "财经早餐", "界面新闻", "联合早报", "MacRumors",
    ]
    for name in known_sources:
        if name in text:
            return name

    # Common Telegram display-name cleanup: keep the representative brand words.
    if "科技圈" in text and "在花" in text:
        return "在花科技圈"

    return clamp_weighted(text or source, 20)


def init_source_categories(conn: sqlite3.Connection) -> None:
    ensure_article_source_columns(conn)
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
    try:
        init_source_categories(conn)
        aliases = conn.execute(
            "SELECT alias_source, target_source FROM source_aliases"
        ).fetchall()
        for row in aliases:
            alias = row["alias_source"] if isinstance(row, sqlite3.Row) else row[0]
            target = row["target_source"] if isinstance(row, sqlite3.Row) else row[1]
            conn.execute(
                "UPDATE articles SET feed_source = ?, source = ? "
                "WHERE feed_source = ? OR (TRIM(feed_source) = '' AND source = ?) OR source = ?",
                (target, target, alias, alias, alias),
            )

        rows = conn.execute(
            "SELECT DISTINCT COALESCE(NULLIF(feed_source, ''), source) AS source "
            "FROM articles "
            "WHERE COALESCE(NULLIF(feed_source, ''), source) IS NOT NULL "
            "  AND TRIM(COALESCE(NULLIF(feed_source, ''), source)) != ''"
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
    except Exception:
        conn.rollback()
        raise


def _ensure_source_tables(conn: sqlite3.Connection) -> None:
    """Create the source tables if a fresh deployment hasn't seeded them yet.

    Cheap no-op once they exist, so it's safe on the read path — unlike the full
    init_source_categories(), which also seeds rows and commits.
    """
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'source_categories'"
    ).fetchone()
    if not table:
        init_source_categories(conn)


MAINTENANCE_THROTTLE_SECONDS = 60
_maintenance_lock = threading.Lock()
_maintenance_last_run = 0.0


def maintain_source_categories(conn: sqlite3.Connection, force: bool = False) -> dict:
    """Run the write-heavy source bookkeeping: discover new sources, drop stale ones.

    Both steps scan the whole articles table, so they must stay off the read path of
    GET /sources — that request has to stay fast even while a fetch cycle holds the
    write lock. Call this after a fetch cycle instead. Throttled to one pass per
    MAINTENANCE_THROTTLE_SECONDS per process; pass force=True to bypass (write paths
    that just changed articles need their changes reflected immediately).
    """
    global _maintenance_last_run
    if not force:
        with _maintenance_lock:
            if time.monotonic() - _maintenance_last_run < MAINTENANCE_THROTTLE_SECONDS:
                return {"ran": False, "discovered": 0, "deleted": 0}
    discovered = ensure_article_sources(conn)
    deleted = cleanup_stale_source_categories(conn)
    with _maintenance_lock:
        _maintenance_last_run = time.monotonic()
    return {"ran": True, "discovered": discovered, "deleted": deleted}


def cleanup_stale_source_categories(conn: sqlite3.Connection) -> int:
    """Remove discovered source rows that no longer have articles."""
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'source_categories'"
    ).fetchone()
    if not table:
        init_source_categories(conn)
    rows = conn.execute(
        """
        SELECT sc.source, sc.status, COUNT(a.id) AS article_count
        FROM source_categories sc
        LEFT JOIN articles a ON COALESCE(NULLIF(a.feed_source, ''), a.source) = sc.source
        GROUP BY sc.source
        HAVING article_count = 0
        """
    ).fetchall()
    deleted = 0
    for row in rows:
        source = row["source"] if isinstance(row, sqlite3.Row) else row[0]
        cur = conn.execute(
            """
            DELETE FROM source_categories
            WHERE source = ? AND status IN ('pending', 'failed')
            """,
            (source,),
        )
        deleted += cur.rowcount

    deleted += conn.execute(
        """
        DELETE FROM source_aliases
        WHERE target_source NOT IN (SELECT source FROM source_categories)
        """
    ).rowcount
    deleted += conn.execute(
        """
        DELETE FROM user_source_categories
        WHERE source NOT IN (
            SELECT DISTINCT COALESCE(NULLIF(feed_source, ''), source)
            FROM articles
            WHERE COALESCE(NULLIF(feed_source, ''), source) IS NOT NULL
              AND TRIM(COALESCE(NULLIF(feed_source, ''), source)) != ''
        )
          AND status IN ('pending', 'failed')
        """
    ).rowcount
    deleted += conn.execute(
        """
        DELETE FROM user_source_aliases
        WHERE NOT EXISTS (
            SELECT 1 FROM source_categories
            WHERE source_categories.source = user_source_aliases.target_source
        )
          AND NOT EXISTS (
            SELECT 1 FROM user_source_categories
            WHERE user_source_categories.user_id = user_source_aliases.user_id
              AND user_source_categories.source = user_source_aliases.target_source
        )
        """
    ).rowcount
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
        LEFT JOIN articles a ON COALESCE(NULLIF(a.feed_source, ''), a.source) = sc.source
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


def promote_user_source_settings(conn: sqlite3.Connection, user_id: int) -> dict:
    """Promote legacy per-user source settings into shared administrator settings."""
    category_rows = conn.execute(
        """
        SELECT source, category, label
        FROM user_source_categories
        WHERE user_id = ?
        ORDER BY updated_at ASC
        """,
        (user_id,),
    ).fetchall()
    alias_rows = conn.execute(
        """
        SELECT alias_source, target_source
        FROM user_source_aliases
        WHERE user_id = ?
        ORDER BY created_at ASC
        """,
        (user_id,),
    ).fetchall()

    promoted_categories = 0
    for row in category_rows:
        source = row["source"] if isinstance(row, sqlite3.Row) else row[0]
        category = row["category"] if isinstance(row, sqlite3.Row) else row[1]
        label = row["label"] if isinstance(row, sqlite3.Row) else row[2]
        update_source_category(
            conn,
            source,
            category,
            label,
            status="manual",
            reason="administrator edited",
        )
        promoted_categories += 1

    promoted_aliases = 0
    for row in alias_rows:
        alias = row["alias_source"] if isinstance(row, sqlite3.Row) else row[0]
        target = row["target_source"] if isinstance(row, sqlite3.Row) else row[1]
        target_exists = conn.execute(
            "SELECT 1 FROM source_categories WHERE source = ?",
            (target,),
        ).fetchone()
        if alias != target and target_exists:
            merge_source(conn, alias, target)
            promoted_aliases += 1

    conn.execute("DELETE FROM user_source_categories WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM user_source_aliases WHERE user_id = ?", (user_id,))
    conn.commit()
    return {"categories": promoted_categories, "aliases": promoted_aliases}


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

    conn.execute(
        "UPDATE articles SET feed_source = ?, source = ? "
        "WHERE COALESCE(NULLIF(feed_source, ''), source) = ? OR source = ?",
        (target_source, target_source, source, source),
    )
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
    """Read-only snapshot of source metadata.

    Deliberately does no bookkeeping: the full-table discover/cleanup passes live in
    maintain_source_categories() and run after a fetch cycle. Keeping them here made
    every GET /sources scan the articles table, which timed out the frontend's cold
    start whenever a fetch cycle held the write lock. Sources discovered since the
    last maintenance pass still show up via the `unlinked` query below.
    """
    _ensure_source_tables(conn)
    rows = conn.execute(
        """
        WITH source_counts AS (
            SELECT feed_source AS source,
                   COUNT(*) AS article_count,
                   MAX(timestamp) AS latest_timestamp
            FROM articles
            WHERE feed_source IS NOT NULL AND feed_source != ''
            GROUP BY feed_source

            UNION ALL

            SELECT source,
                   COUNT(*) AS article_count,
                   MAX(timestamp) AS latest_timestamp
            FROM articles
            WHERE (feed_source IS NULL OR feed_source = '')
              AND source IS NOT NULL
              AND TRIM(source) != ''
            GROUP BY source
        ),
        source_stats AS (
            SELECT source,
                   SUM(article_count) AS article_count,
                   MAX(latest_timestamp) AS latest_timestamp
            FROM source_counts
            WHERE source IS NOT NULL AND TRIM(source) != ''
            GROUP BY source
        )
        SELECT sc.source, sc.category, sc.label, sc.status, sc.confidence,
               sc.reason, sc.sample_titles, sc.updated_at,
               COALESCE(stats.article_count, 0) AS article_count,
               stats.latest_timestamp,
               0 AS is_unlinked
        FROM source_categories sc
        LEFT JOIN source_stats stats ON stats.source = sc.source

        UNION ALL

        SELECT stats.source, 'Info', NULL, 'pending', NULL,
               'unlinked', NULL, NULL,
               stats.article_count, stats.latest_timestamp,
               1 AS is_unlinked
        FROM source_stats stats
        LEFT JOIN source_categories sc ON sc.source = stats.source
        WHERE sc.source IS NULL
        """
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        is_unlinked = bool(item.pop("is_unlinked"))
        if is_unlinked:
            item["label"] = local_short_source_name(item["source"])
        result.append(item)
    result.sort(key=lambda r: (-(r.get("article_count") or 0), (r.get("source") or "").lower()))
    return result


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


def source_aliases_for_target(
    conn: sqlite3.Connection,
    target_source: str,
    user_id: int | None = None,
) -> list[str]:
    if user_id is None:
        rows = conn.execute(
            "SELECT alias_source FROM source_aliases WHERE target_source = ?",
            (target_source,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT alias_source FROM user_source_aliases WHERE user_id = ? AND target_source = ?",
            (user_id, target_source),
        ).fetchall()
    return [row["alias_source"] if isinstance(row, sqlite3.Row) else row[0] for row in rows]


def recent_titles_for_source(conn: sqlite3.Connection, source: str, limit: int = 8) -> list[str]:
    rows = conn.execute(
        """
        SELECT title FROM articles
        WHERE COALESCE(NULLIF(feed_source, ''), source) = ?
          AND title IS NOT NULL AND TRIM(title) != ''
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
