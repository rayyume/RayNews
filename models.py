"""RayNews data models — user auth, favorites, AI configs, settings."""

import sqlite3
import json
import bcrypt
from datetime import datetime
from pathlib import Path
import os
import threading

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_FILE = DATA_DIR / "raynews.db"

# ─── Schema ───────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT    NOT NULL UNIQUE,
    password    TEXT    NOT NULL,
    nickname    TEXT    NOT NULL DEFAULT '',
    role        TEXT    NOT NULL DEFAULT 'user'
                        CHECK(role IN ('user', 'admin')),
    avatar_url  TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS favorites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    article_id  INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, article_id)
);

CREATE TABLE IF NOT EXISTS ai_configs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    provider    TEXT    NOT NULL DEFAULT 'openai',
    api_key     TEXT    NOT NULL DEFAULT '',
    endpoint    TEXT    NOT NULL DEFAULT 'https://api.openai.com/v1',
    model       TEXT    NOT NULL DEFAULT 'gpt-4o-mini',
    provider_type TEXT NOT NULL DEFAULT 'openai'
                    CHECK(provider_type IN ('openai', 'claude')),
    enabled     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_settings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                 INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    auto_translate_title    INTEGER NOT NULL DEFAULT 0,
    auto_translate_content  INTEGER NOT NULL DEFAULT 0,
    auto_title_summary_enabled INTEGER NOT NULL DEFAULT 0,
    auto_summary_enabled    INTEGER NOT NULL DEFAULT 0,
    daily_summary_enabled   INTEGER NOT NULL DEFAULT 0,
    theme_preference        TEXT    NOT NULL DEFAULT 'system',
    notification_config     TEXT    NOT NULL DEFAULT '{}',
    share_ai_results        INTEGER NOT NULL DEFAULT 0,
    share_view_title        INTEGER NOT NULL DEFAULT 0,
    share_view_translation  INTEGER NOT NULL DEFAULT 0,
    share_view_summary      INTEGER NOT NULL DEFAULT 0,
    share_last_check_at     TEXT,
    share_last_check_ok     INTEGER,
    share_last_check_error  TEXT
);

CREATE TABLE IF NOT EXISTS user_access_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    accessed_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_access_log_user_time ON user_access_log(user_id, accessed_at);

CREATE TABLE IF NOT EXISTS invitation_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    email       TEXT    NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS system_ai_config (
    id          INTEGER PRIMARY KEY CHECK(id = 1),
    provider    TEXT    NOT NULL DEFAULT 'openai',
    api_key     TEXT    NOT NULL DEFAULT '',
    endpoint    TEXT    NOT NULL DEFAULT 'https://api.openai.com/v1',
    model       TEXT    NOT NULL DEFAULT 'gpt-4o-mini',
    provider_type TEXT NOT NULL DEFAULT 'openai'
                    CHECK(provider_type IN ('openai', 'claude')),
    enabled     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type        TEXT    NOT NULL DEFAULT 'general',
    title       TEXT    NOT NULL,
    body        TEXT    NOT NULL DEFAULT '',
    format      TEXT    NOT NULL DEFAULT 'plain',
    created_at  TEXT    NOT NULL,
    read_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_notifications_user_time ON notifications(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS broadcast_publications (
    broadcast_id TEXT    PRIMARY KEY,
    title        TEXT    NOT NULL DEFAULT '',
    recipients   INTEGER NOT NULL DEFAULT 0,
    email        INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);"""

# ─── Connection ───────────────────────────────────────────────

_db_local = threading.local()
_db_init_lock = threading.Lock()
_initialized_db_paths: set[str] = set()


def _initialize_db(db: sqlite3.Connection) -> None:
    """Create and migrate the model database on a newly opened connection."""
    db.executescript(SCHEMA_SQL)
    # Migration: add provider_type column if it doesn't exist (pre-v3 schema)
    try:
        db.execute("ALTER TABLE ai_configs ADD COLUMN provider_type TEXT NOT NULL DEFAULT 'openai'")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        db.execute("ALTER TABLE user_settings ADD COLUMN auto_summary_enabled INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        db.execute("ALTER TABLE user_settings ADD COLUMN auto_title_summary_enabled INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        db.execute("ALTER TABLE user_settings ADD COLUMN theme_preference TEXT NOT NULL DEFAULT 'system'")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        db.execute("ALTER TABLE users ADD COLUMN visit_count INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        db.execute("ALTER TABLE users ADD COLUMN last_seen_at TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # column already exists
    for _col_sql in (
        "ALTER TABLE user_settings ADD COLUMN share_ai_results INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE user_settings ADD COLUMN share_view_title INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE user_settings ADD COLUMN share_view_translation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE user_settings ADD COLUMN share_view_summary INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE user_settings ADD COLUMN share_last_check_at TEXT",
        "ALTER TABLE user_settings ADD COLUMN share_last_check_ok INTEGER",
        "ALTER TABLE user_settings ADD COLUMN share_last_check_error TEXT",
        # notifications gained a body format ('plain'|'markdown') after the
        # table already shipped, so existing DBs need the column backfilled.
        "ALTER TABLE notifications ADD COLUMN format TEXT NOT NULL DEFAULT 'plain'",
    ):
        try:
            db.execute(_col_sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    # Registration now requires a username, stored in the nickname column.
    # Backfill existing accounts that predate this requirement: keep their
    # nickname if they set one, otherwise fall back to their email.
    db.execute("UPDATE users SET nickname = email WHERE nickname = ''")
    # Auto-summary/auto-translate are now admin-only; zero out any
    # leftover opt-in flags on non-admin accounts from before this change.
    db.execute(
        "UPDATE user_settings SET auto_translate_title = 0, auto_translate_content = 0, "
        "auto_title_summary_enabled = 0, auto_summary_enabled = 0 "
        "WHERE user_id NOT IN (SELECT id FROM users WHERE role = 'admin')"
    )
    # The "preview" role has been retired: registration now grants "user"
    # directly, and "who's pending" is tracked via unused invitation_codes
    # instead. Promote any leftover preview accounts from before this change.
    # (The role CHECK constraint above only drops 'preview' for brand new
    # databases — existing databases keep their original, more permissive
    # constraint rather than paying for a table-rebuild migration just to
    # tighten it; the application layer already never assigns 'preview'.)
    db.execute("UPDATE users SET role = 'user' WHERE role = 'preview'")
    db.commit()


def close_db() -> None:
    """Close the model connection owned by the calling thread, if any."""
    db = getattr(_db_local, "connection", None)
    if db is not None:
        db.close()
    _db_local.connection = None
    _db_local.path = None


def get_db() -> sqlite3.Connection:
    """Return a SQLite connection owned by the calling thread.

    sqlite3 connections may not safely execute statements or share transaction
    state across Flask request threads. Keeping one connection per thread lets
    WAL serialize writers at SQLite's transaction boundary instead of letting
    one request commit or corrupt another request's work.
    """
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db_path = str(DB_FILE.resolve())
    db = getattr(_db_local, "connection", None)
    if db is not None and getattr(_db_local, "path", None) == db_path:
        return db
    if db is not None:
        close_db()

    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        # Foreign-key enforcement and busy_timeout are per connection.
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        # The schema/migrations perform writes. Run them once per database
        # path in this process so independent request connections cannot race
        # them or pay migration cost on every request thread.
        with _db_init_lock:
            if db_path not in _initialized_db_paths:
                db.execute("PRAGMA journal_mode=WAL")
                _initialize_db(db)
                _initialized_db_paths.add(db_path)
    except Exception:
        db.close()
        raise
    _db_local.connection = db
    _db_local.path = db_path
    return db


# ─── User helpers ─────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_user(email: str, password: str, nickname: str = "",
                role: str = "user") -> dict | None:
    db = get_db()
    # Check username uniqueness if provided
    if nickname:
        existing = db.execute(
            "SELECT 1 FROM users WHERE nickname = ? AND nickname != ''",
            (nickname,),
        ).fetchone()
        if existing:
            return None  # duplicate username
    try:
        cur = db.execute(
            "INSERT INTO users (email, password, nickname, role) VALUES (?, ?, ?, ?)",
            (email, hash_password(password), nickname, role),
        )
        db.commit()
        return get_user(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None  # duplicate email


def get_user(user_id: int) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT id, email, nickname, role, avatar_url, created_at, "
        "visit_count, last_seen_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    """Look up a user by nickname/username."""
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE nickname = ?", (username,)).fetchone()
    return dict(row) if row else None


def update_user(user_id: int, **kwargs) -> dict | None:
    allowed = {"nickname", "avatar_url", "role"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_user(user_id)
    # Check nickname uniqueness if updating nickname
    if "nickname" in updates:
        nickname = updates["nickname"]
        if nickname:
            existing = get_user_by_username(nickname)
            if existing and existing["id"] != user_id:
                return None  # nickname taken by another user
    sets = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [user_id]
    db = get_db()
    db.execute(f"UPDATE users SET {sets} WHERE id = ?", vals)
    db.commit()
    return get_user(user_id)


def delete_user(user_id: int) -> bool:
    db = get_db()
    cur = db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return cur.rowcount > 0


def list_users() -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT id, email, nickname, role, avatar_url, created_at, "
        "visit_count, last_seen_at FROM users ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_first_admin_email() -> str:
    db = get_db()
    row = db.execute(
        "SELECT email FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
    ).fetchone()
    return row["email"] if row else ""


def count_users() -> int:
    db = get_db()
    return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]


# ─── Access Stats ──────────────────────────────────────────

_ACCESS_THROTTLE_SECONDS = 300  # collapse bursts of requests into one "visit"


def record_access(user_id: int) -> None:
    """Bump a user's visit counter/last-seen timestamp, throttled per session.

    Called on every authenticated request, so repeat calls within the
    throttle window only refresh last_seen_at without inflating visit_count
    or the detail log.
    """
    db = get_db()
    row = db.execute("SELECT last_seen_at FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return
    now = datetime.utcnow()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    should_count = True
    last_seen = row["last_seen_at"]
    if last_seen:
        try:
            last_dt = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S")
            if (now - last_dt).total_seconds() < _ACCESS_THROTTLE_SECONDS:
                should_count = False
        except ValueError:
            pass
    if should_count:
        db.execute(
            "UPDATE users SET visit_count = visit_count + 1, last_seen_at = ? WHERE id = ?",
            (now_str, user_id),
        )
        db.execute("INSERT INTO user_access_log (user_id, accessed_at) VALUES (?, ?)", (user_id, now_str))
    else:
        db.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (now_str, user_id))
    db.commit()


def count_active_users_since(days: int) -> int:
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) FROM users WHERE last_seen_at >= datetime('now', ?)",
        (f"-{int(days)} days",),
    ).fetchone()
    return int(row[0]) if row else 0


# ─── Favorites ─────────────────────────────────────────────


def add_favorite(user_id: int, article_id: int) -> dict | None:
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO favorites (user_id, article_id) VALUES (?, ?)",
            (user_id, article_id),
        )
        db.commit()
        row = db.execute(
            "SELECT id, user_id, article_id, created_at FROM favorites WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.IntegrityError:
        return None  # already favorited


def remove_favorite(user_id: int, article_id: int) -> bool:
    db = get_db()
    cur = db.execute(
        "DELETE FROM favorites WHERE user_id = ? AND article_id = ?",
        (user_id, article_id),
    )
    db.commit()
    return cur.rowcount > 0


def get_favorites(user_id: int) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT article_id, created_at FROM favorites WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_favorite_article_ids() -> list[int]:
    db = get_db()
    rows = db.execute(
        "SELECT DISTINCT article_id FROM favorites ORDER BY article_id"
    ).fetchall()
    return [int(r["article_id"]) for r in rows]


def is_favorited(user_id: int, article_id: int) -> bool:
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM favorites WHERE user_id = ? AND article_id = ?",
        (user_id, article_id),
    ).fetchone()
    return row is not None


def count_article_favorites(article_id: int) -> int:
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) AS count FROM favorites WHERE article_id = ?",
        (article_id,),
    ).fetchone()
    return int(row["count"] if row else 0)


# ─── AI Config ─────────────────────────────────────────────


def get_ai_config(user_id: int) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT id, provider, api_key, endpoint, model, provider_type, enabled "
        "FROM ai_configs WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def set_ai_config(user_id: int, **kwargs) -> dict:
    """Upsert AI config. Allowed keys: provider, api_key, endpoint, model, provider_type, enabled."""
    allowed = {"provider", "api_key", "endpoint", "model", "provider_type", "enabled"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_ai_config(user_id) or {}
    db = get_db()
    existing = get_ai_config(user_id)
    if existing:
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [user_id]
        db.execute(f"UPDATE ai_configs SET {sets} WHERE user_id = ?", vals)
    else:
        keys = ", ".join(updates.keys())
        placeholders = ", ".join("?" for _ in updates)
        vals = list(updates.values())
        db.execute(
            f"INSERT INTO ai_configs (user_id, {keys}) VALUES (?, {placeholders})",
            [user_id] + vals,
        )
    db.commit()
    return get_ai_config(user_id)


# ─── System AI Config (admin-managed, drives background jobs) ──


def get_system_ai_config() -> dict:
    db = get_db()
    row = db.execute(
        "SELECT provider, api_key, endpoint, model, provider_type, enabled "
        "FROM system_ai_config WHERE id = 1"
    ).fetchone()
    if row:
        return dict(row)
    return {
        "provider": "openai", "api_key": "", "endpoint": "https://api.openai.com/v1",
        "model": "gpt-4o-mini", "provider_type": "openai", "enabled": 0,
    }


def set_system_ai_config(**kwargs) -> dict:
    """Upsert the singleton system AI config. Allowed keys: provider, api_key, endpoint, model, provider_type, enabled."""
    allowed = {"provider", "api_key", "endpoint", "model", "provider_type", "enabled"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_system_ai_config()
    db = get_db()
    db.execute("INSERT OR IGNORE INTO system_ai_config (id) VALUES (1)")
    sets = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values())
    db.execute(f"UPDATE system_ai_config SET {sets} WHERE id = 1", vals)
    db.commit()
    return get_system_ai_config()


# ─── User Settings ─────────────────────────────────────────


def get_user_settings(user_id: int) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT id, auto_translate_title, auto_translate_content, "
        "auto_title_summary_enabled, auto_summary_enabled, "
        "daily_summary_enabled, theme_preference, notification_config, "
        "share_ai_results, share_view_title, share_view_translation, share_view_summary, "
        "share_last_check_at, share_last_check_ok, share_last_check_error "
        "FROM user_settings WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def get_users_with_share_enabled() -> list[int]:
    """User ids that currently have the AI-result sharing master switch on."""
    db = get_db()
    rows = db.execute(
        "SELECT user_id FROM user_settings WHERE share_ai_results = 1"
    ).fetchall()
    return [int(r["user_id"]) for r in rows]


def set_user_settings(user_id: int, **kwargs) -> dict:
    """Upsert settings."""
    allowed = {"auto_translate_title", "auto_translate_content",
               "auto_title_summary_enabled", "auto_summary_enabled", "daily_summary_enabled",
               "theme_preference", "notification_config",
               "share_ai_results", "share_view_title", "share_view_translation", "share_view_summary",
               "share_last_check_at", "share_last_check_ok", "share_last_check_error"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_user_settings(user_id) or {}
    db = get_db()
    existing = get_user_settings(user_id)
    if existing:
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [user_id]
        db.execute(f"UPDATE user_settings SET {sets} WHERE user_id = ?", vals)
    else:
        keys = ", ".join(updates.keys())
        placeholders = ", ".join("?" for _ in updates)
        vals = list(updates.values())
        db.execute(
            f"INSERT INTO user_settings (user_id, {keys}) VALUES (?, {placeholders})",
            [user_id] + vals,
        )
    db.commit()
    return get_user_settings(user_id)


# ─── In-App Notifications ──────────────────────────────────


def add_notification(user_id: int, ntype: str, title: str, body: str = "",
                     fmt: str = "plain") -> int:
    """Insert an in-app notification for a user. Returns the new row id.

    fmt is 'plain' (client renders newline→<br>) or 'markdown' (client runs it
    through renderMarkdown + sanitizer). The raw body text is stored as-is;
    every client render path escapes/sanitizes, so markup here is inert.
    """
    db = get_db()
    # Local time, same convention as user_settings.share_last_check_at.
    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        "INSERT INTO notifications (user_id, type, title, body, format, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, ntype, title, body, fmt, now),
    )
    db.commit()
    return cur.lastrowid


def add_notification_bulk(user_ids: list[int], ntype: str, title: str,
                          body: str = "", fmt: str = "plain") -> int:
    """Insert the same notification for many users in one transaction (used by
    the admin site-wide broadcast). Returns the number of rows inserted."""
    if not user_ids:
        return 0
    db = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    rows = [(uid, ntype, title, body, fmt, now) for uid in user_ids]
    db.executemany(
        "INSERT INTO notifications (user_id, type, title, body, format, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    db.commit()
    return len(rows)


def publish_broadcast_atomically(
    user_ids: list[int], broadcast_id: str, title: str, body: str,
    fmt: str, email: bool,
) -> tuple[bool, dict]:
    """Commit a site-wide in-app broadcast as one transaction.

    Returns ``(True, result)`` when this call publishes a new broadcast, or
    ``(False, result)`` when ``broadcast_id`` was already committed. A fan-out
    failure rolls back both the claim row and all notification rows, so a retry
    with the same id can safely try again.
    """
    # This transaction must not use get_db(): that function returns the
    # process-wide connection shared by every Flask request. A commit/rollback
    # from another thread on that same connection would split or undo this
    # transaction. A short-lived connection gives this unit its own transaction
    # boundary; WAL + busy_timeout let concurrent writers serialize normally.
    db = sqlite3.connect(str(DB_FILE), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    now = datetime.now().isoformat(timespec="seconds")
    try:
        db.execute("BEGIN IMMEDIATE")
        claimed = db.execute(
            "INSERT OR IGNORE INTO broadcast_publications "
            "(broadcast_id, title, recipients, email, created_at) VALUES (?, '', 0, 0, ?)",
            (broadcast_id, now),
        ).rowcount > 0
        if not claimed:
            row = db.execute(
                "SELECT recipients, email FROM broadcast_publications WHERE broadcast_id = ?",
                (broadcast_id,),
            ).fetchone()
            db.rollback()
            return False, {
                "recipients": int(row["recipients"]),
                "email": bool(row["email"]),
            }

        db.executemany(
            "INSERT INTO notifications (user_id, type, title, body, format, created_at) "
            "VALUES (?, 'admin_broadcast', ?, ?, ?, ?)",
            [(uid, title, body, fmt, now) for uid in user_ids],
        )
        db.execute(
            "UPDATE broadcast_publications SET title = ?, recipients = ?, email = ? "
            "WHERE broadcast_id = ?",
            (title, len(user_ids), int(email), broadcast_id),
        )
        db.commit()
        return True, {"recipients": len(user_ids), "email": bool(email)}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def list_notifications(user_id: int, limit: int = 100) -> list[dict]:
    """Newest first, but unread rows always sort ahead of read ones.

    count_unread_notifications() below counts the whole table, not just this
    page — without the unread-first ordering, a user who accumulates more
    than `limit` notifications could have an old unread row pushed off the
    end by newer *read* ones, leaving it permanently uncounted-but-invisible
    (the badge would never clear). Unread-first guarantees every unread row
    is visible as long as the unread count itself stays under `limit`.
    """
    db = get_db()
    rows = db.execute(
        "SELECT id, type, title, body, format, created_at, read_at "
        "FROM notifications WHERE user_id = ? "
        "ORDER BY (read_at IS NULL) DESC, id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def count_unread_notifications(user_id: int) -> int:
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL",
        (user_id,),
    ).fetchone()
    return int(row[0])


def mark_notification_read(user_id: int, notification_id: int) -> bool:
    """Mark one notification read. User-scoped; idempotent (already-read rows
    are untouched and return False)."""
    db = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        "UPDATE notifications SET read_at = ? "
        "WHERE id = ? AND user_id = ? AND read_at IS NULL",
        (now, notification_id, user_id),
    )
    db.commit()
    return cur.rowcount > 0


def get_broadcast_publication(broadcast_id: str) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT broadcast_id, title, recipients, email, created_at "
        "FROM broadcast_publications WHERE broadcast_id = ?",
        (broadcast_id,),
    ).fetchone()
    return dict(row) if row else None


# ─── Invitation Codes ──────────────────────────────────────

import secrets


def create_invitation_code(email: str) -> str:
    """Generate an 8-char alphanumeric code and store it."""
    db = get_db()
    for attempt in range(10):
        code = secrets.token_hex(4).upper()  # 8 hex chars
        try:
            db.execute(
                "INSERT INTO invitation_codes (code, email) VALUES (?, ?)",
                (code, email),
            )
            db.commit()
            return code
        except sqlite3.IntegrityError:
            continue  # code collision, retry
    raise Exception("failed to generate unique invitation code")


def validate_invitation_code(code: str, email: str) -> bool:
    """Check if code is valid and tied to this email, not yet used."""
    db = get_db()
    row = db.execute(
        "SELECT 1 FROM invitation_codes WHERE code = ? AND email = ? AND used = 0",
        (code, email),
    ).fetchone()
    return row is not None


def use_invitation_code(code: str) -> bool:
    """Mark code as used. Returns True if successful."""
    db = get_db()
    cur = db.execute(
        "UPDATE invitation_codes SET used = 1 WHERE code = ? AND used = 0",
        (code,),
    )
    db.commit()
    return cur.rowcount > 0


def list_pending_invitations() -> list[dict]:
    """Invite-code requests that haven't been used to complete registration yet."""
    db = get_db()
    rows = db.execute(
        "SELECT id, code, email, created_at FROM invitation_codes "
        "WHERE used = 0 ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def count_pending_invitations() -> int:
    db = get_db()
    return db.execute(
        "SELECT COUNT(*) FROM invitation_codes WHERE used = 0"
    ).fetchone()[0]


def delete_invitation_code(invitation_id: int) -> bool:
    """Revoke a pending (unused) invitation request."""
    db = get_db()
    cur = db.execute(
        "DELETE FROM invitation_codes WHERE id = ? AND used = 0",
        (invitation_id,),
    )
    db.commit()
    return cur.rowcount > 0


def delete_invitation_code_by_code(code: str) -> bool:
    """Roll back a just-created code (e.g. the notification email failed to send)."""
    db = get_db()
    cur = db.execute(
        "DELETE FROM invitation_codes WHERE code = ? AND used = 0",
        (code,),
    )
    db.commit()
    return cur.rowcount > 0
