"""RayNews data models — user auth, favorites, AI configs, settings."""

import sqlite3
import json
import bcrypt
from pathlib import Path
import os

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
                        CHECK(role IN ('preview', 'user', 'admin')),
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
    auto_summary_enabled    INTEGER NOT NULL DEFAULT 0,
    daily_summary_enabled   INTEGER NOT NULL DEFAULT 0,
    notification_config     TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS invitation_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    email       TEXT    NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);"""

# ─── Connection ───────────────────────────────────────────────

_db = None


def get_db() -> sqlite3.Connection:
    global _db
    if _db is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(str(DB_FILE), check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA foreign_keys=ON")
        _db.executescript(SCHEMA_SQL)
        # Migration: add provider_type column if it doesn't exist (pre-v3 schema)
        try:
            _db.execute("ALTER TABLE ai_configs ADD COLUMN provider_type TEXT NOT NULL DEFAULT 'openai'")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            _db.execute("ALTER TABLE user_settings ADD COLUMN auto_summary_enabled INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        _db.commit()
    return _db


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
        "SELECT id, email, nickname, role, avatar_url, created_at FROM users WHERE id = ?",
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
        "SELECT id, email, nickname, role, avatar_url, created_at FROM users ORDER BY id"
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


# ─── User Settings ─────────────────────────────────────────


def get_user_settings(user_id: int) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT id, auto_translate_title, auto_translate_content, "
        "auto_summary_enabled, daily_summary_enabled, notification_config "
        "FROM user_settings WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return dict(row) if row else None


def set_user_settings(user_id: int, **kwargs) -> dict:
    """Upsert settings."""
    allowed = {"auto_translate_title", "auto_translate_content",
               "auto_summary_enabled", "daily_summary_enabled",
               "notification_config"}
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
