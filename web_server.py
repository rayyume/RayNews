"""RayNews Web Server — auth, favorites, AI, settings via Flask."""

import os
import sys

from flask import Flask, request, jsonify, g
from flask_cors import CORS

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (
    get_db, create_user, get_user, get_user_by_email, get_user_by_username,
    update_user, delete_user, list_users, count_users,
    verify_password,
    add_favorite, remove_favorite, get_favorites, is_favorited,
    get_ai_config, set_ai_config, get_user_settings, set_user_settings,
)
from auth import init_auth, create_token, require_auth, require_role
from ai_service import AIService

# ─── App Setup ────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)

# Secret key: from env or generate on first run
SECRET_KEY = os.environ.get("RAYNEWS_SECRET")
if not SECRET_KEY:
    import secrets
    SECRET_KEY = secrets.token_hex(32)
    print(f"[web] No RAYNEWS_SECRET set — using ephemeral key: {SECRET_KEY[:16]}...")

init_auth(SECRET_KEY)


# ─── Auth Routes ──────────────────────────────────────────────

@app.route("/auth/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    nickname = (data.get("nickname") or "").strip()

    if not email or not password:
        return jsonify({"error": "email and password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400

    # First user is admin, rest are user
    role = "admin" if count_users() == 0 else data.get("role", "user")

    user = create_user(email, password, nickname, role)
    if user is None:
        # Check if it was a duplicate email or duplicate username
        if nickname and get_user_by_username(nickname):
            return jsonify({"error": "username already taken"}), 409
        return jsonify({"error": "email already registered"}), 409

    token = create_token(user["id"], user["role"])
    return jsonify({"token": token, "user": user}), 201


@app.route("/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    login_val = (data.get("login") or data.get("email") or "").strip()
    password = data.get("password") or ""

    # Try email first (case-insensitive), then username (case-sensitive)
    user = get_user_by_email(login_val.lower())
    if not user:
        user = get_user_by_username(login_val)
    if not user or not verify_password(password, user["password"]):
        return jsonify({"error": "invalid email/username or password"}), 401

    token = create_token(user["id"], user["role"])
    return jsonify({
        "token": token,
        "user": {k: v for k, v in user.items() if k != "password"},
    })


@app.route("/auth/me", methods=["GET"])
@require_auth
def me():
    user = get_user(g.user_id)
    if not user:
        return jsonify({"error": "user not found"}), 404
    return jsonify(user)


@app.route("/auth/me", methods=["PUT"])
@require_auth
def update_me():
    data = request.get_json(silent=True) or {}
    allowed = {k: data[k] for k in ("nickname",) if k in data}
    user = update_user(g.user_id, **allowed)
    return jsonify(user) if user else (jsonify({"error": "not found"}), 404)


# ─── Avatar Upload ─────────────────────────────────────────

AVATARS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "avatars")
AVATAR_MAX_SIZE = 500 * 1024  # 500KB
ALLOWED_AVATAR_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


@app.route("/auth/me/avatar", methods=["PUT"])
@require_auth
def upload_avatar():
    """Upload avatar as base64 data URL. Saves to data/avatars/{user_id}.{ext}."""
    data = request.get_json(silent=True) or {}
    image_data = data.get("image", "")

    if not image_data:
        return jsonify({"error": "image required"}), 400

    # Parse data URL: data:image/{type};base64,{data}
    if not image_data.startswith("data:"):
        return jsonify({"error": "invalid image format"}), 400

    try:
        header, raw = image_data.split(",", 1)
        mime = header.split(";")[0].split(":", 1)[1]  # e.g. image/jpeg
        ext = ALLOWED_AVATAR_TYPES.get(mime)
        if not ext:
            return jsonify({"error": "unsupported image type (jpg/png/gif/webp only)"}), 400

        import base64
        raw_bytes = base64.b64decode(raw)

        if len(raw_bytes) > AVATAR_MAX_SIZE:
            return jsonify({"error": "image too large (max 500KB)"}), 400

        # Ensure avatars directory exists
        os.makedirs(AVATARS_DIR, exist_ok=True)

        # Build filename: user_id with proper extension
        old_paths = [
            os.path.join(AVATARS_DIR, f"{g.user_id}.{old_ext}")
            for old_ext in ALLOWED_AVATAR_TYPES.values()
        ]
        new_path = os.path.join(AVATARS_DIR, f"{g.user_id}.{ext}")

        # Remove old avatar files with different extension
        for p in old_paths:
            if p != new_path and os.path.exists(p):
                os.remove(p)

        # Write new avatar
        with open(new_path, "wb") as f:
            f.write(raw_bytes)

        avatar_url = f"/avatars/{g.user_id}.{ext}"
        update_user(g.user_id, avatar_url=avatar_url)

        return jsonify({"avatar_url": avatar_url}), 200

    except (ValueError, IndexError, base64.binascii.Error) as e:
        return jsonify({"error": "invalid image data"}), 400


# ─── Admin Routes ─────────────────────────────────────────────

@app.route("/auth/users", methods=["GET"])
@require_role("admin")
def admin_list_users():
    users = list_users()
    return jsonify({"users": users, "total": len(users)})


@app.route("/auth/users/<int:user_id>", methods=["DELETE"])
@require_role("admin")
def admin_delete_user(user_id):
    if user_id == g.user_id:
        return jsonify({"error": "cannot delete yourself"}), 400
    ok = delete_user(user_id)
    if ok:
        return jsonify({"ok": True}), 200
    return jsonify({"error": "not found"}), 404


@app.route("/auth/users/<int:user_id>/role", methods=["PUT"])
@require_role("admin")
def admin_set_role(user_id):
    data = request.get_json(silent=True) or {}
    new_role = data.get("role")
    if new_role not in ("preview", "user", "admin"):
        return jsonify({"error": "invalid role"}), 400
    if user_id == g.user_id:
        return jsonify({"error": "cannot change your own role"}), 400
    user = update_user(user_id, role=new_role)
    return jsonify(user) if user else (jsonify({"error": "not found"}), 404)


# ─── Favorites API ─────────────────────────────────────────

NEWS_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "news.db")


def _get_article_meta(article_id: int) -> dict | None:
    """Fetch article title/source/date/thumb from news.db by id."""
    if not os.path.exists(NEWS_DB):
        return None
    try:
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, title, source, date, time, thumb, has_full_content, timestamp FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


_news_conn = None


def _get_news_db():
    """Persistent connection to news.db for batch queries."""
    global _news_conn
    if _news_conn is None and os.path.exists(NEWS_DB):
        _news_conn = sqlite3.connect(NEWS_DB, check_same_thread=False)
        _news_conn.row_factory = sqlite3.Row
    return _news_conn


def _get_article_meta_batch(article_ids: list[int]) -> dict[int, dict]:
    """Fetch metadata for multiple articles at once."""
    if not article_ids:
        return {}
    conn = _get_news_db()
    if not conn:
        return {}
    try:
        placeholders = ",".join("?" * len(article_ids))
        rows = conn.execute(
            f"SELECT id, title, source, date, time, thumb, has_full_content, timestamp FROM articles WHERE id IN ({placeholders})",
            article_ids,
        ).fetchall()
        return {r["id"]: dict(r) for r in rows}
    except Exception:
        return {}


import sqlite3


@app.route("/favorites", methods=["GET"])
@require_auth
def list_favorites():
    """List user's favorites with article metadata."""
    favs = get_favorites(g.user_id)
    article_ids = [f["article_id"] for f in favs]
    articles = _get_article_meta_batch(article_ids)
    items = []
    for f in favs:
        meta = articles.get(f["article_id"])
        if meta:
            items.append({
                "article_id": f["article_id"],
                "created_at": f["created_at"],
                "title": meta["title"],
                "source": meta["source"],
                "date": meta["date"],
                "time": meta["time"],
                "thumb": meta["thumb"],
                "has_full_content": meta["has_full_content"],
            })
    return jsonify({"items": items, "total": len(items)})


@app.route("/favorites", methods=["POST"])
@require_auth
def add_favorite_route():
    data = request.get_json(silent=True) or {}
    article_id = data.get("article_id")
    if not article_id or not isinstance(article_id, int):
        return jsonify({"error": "article_id required (int)"}), 400
    fav = add_favorite(g.user_id, article_id)
    if fav is None:
        return jsonify({"error": "already favorited"}), 409
    return jsonify({"ok": True, "favorite": fav}), 201


@app.route("/favorites/<int:article_id>", methods=["DELETE"])
@require_auth
def remove_favorite_route(article_id):
    ok = remove_favorite(g.user_id, article_id)
    if ok:
        return jsonify({"ok": True}), 200
    return jsonify({"error": "not found"}), 404


@app.route("/favorites/<int:article_id>/status", methods=["GET"])
@require_auth
def favorite_status(article_id):
    return jsonify({"favorited": is_favorited(g.user_id, article_id)})


# ─── AI Routes ─────────────────────────────────────────────

@app.route("/ai/config", methods=["GET"])
@require_auth
def get_ai_config_route():
    config = get_ai_config(g.user_id)
    if not config:
        return jsonify({
            "provider": "openai",
            "endpoint": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "provider_type": "openai",
            "enabled": False,
            "api_key": "",
        })
    # Mask API key for security
    safe = dict(config)
    if safe.get("api_key"):
        k = safe["api_key"]
        safe["api_key"] = k[:6] + "****" + k[-4:] if len(k) > 10 else "****"
    return jsonify(safe)


@app.route("/ai/config", methods=["PUT"])
@require_auth
def set_ai_config_route():
    try:
        data = request.get_json(silent=True) or {}
        config = set_ai_config(g.user_id, **data)
        # Mask API key in response
        safe = dict(config) if config else {}
        if safe.get("api_key"):
            k = safe["api_key"]
            safe["api_key"] = k[:6] + "****" + k[-4:] if len(k) > 10 else "****"
        return jsonify(safe)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"server error: {str(e)}"}), 500


@app.route("/ai/summarize/<int:article_id>", methods=["POST"])
@require_auth
def ai_summarize(article_id):
    config = get_ai_config(g.user_id)
    if not config or not config.get("enabled"):
        return jsonify({"error": "AI not configured. Go to Settings → AI to set up."}), 400
    if not config.get("api_key"):
        return jsonify({"error": "API key not configured"}), 400

    # Fetch article content from news.db
    article = _fetch_article_body(article_id)
    if not article:
        return jsonify({"error": "article not found"}), 404

    try:
        svc = AIService(
            api_key=config["api_key"],
            endpoint=config["endpoint"],
            model=config["model"],
            provider_type=config.get("provider_type", "openai"),
        )
        summary = svc.summarize(
            article_text=article.get("body_html") or article.get("summary") or "",
            title=article.get("title", ""),
        )
        return jsonify({"summary": summary})
    except Exception as e:
        return jsonify({"error": f"AI request failed: {str(e)}"}), 502


@app.route("/ai/translate/<int:article_id>", methods=["POST"])
@require_auth
def ai_translate(article_id):
    config = get_ai_config(g.user_id)
    if not config or not config.get("enabled"):
        return jsonify({"error": "AI not configured. Go to Settings → AI to set up."}), 400
    if not config.get("api_key"):
        return jsonify({"error": "API key not configured"}), 400

    article = _fetch_article_body(article_id)
    if not article:
        return jsonify({"error": "article not found"}), 404

    try:
        svc = AIService(
            api_key=config["api_key"],
            endpoint=config["endpoint"],
            model=config["model"],
            provider_type=config.get("provider_type", "openai"),
        )
        translation = svc.translate(
            article_text=article.get("body_html") or article.get("summary") or "",
            title=article.get("title", ""),
        )
        return jsonify({"translation": translation})
    except Exception as e:
        return jsonify({"error": f"AI request failed: {str(e)}"}), 502


@app.route("/ai/test-connection", methods=["POST"])
@require_auth
def ai_test_connection():
    """Test the user's AI API configuration with a minimal prompt."""
    config = get_ai_config(g.user_id)
    if not config or not config.get("api_key"):
        return jsonify({"error": "AI not configured. Save API config first."}), 400
    try:
        svc = AIService(
            api_key=config["api_key"],
            endpoint=config["endpoint"],
            model=config["model"],
            provider_type=config.get("provider_type", "openai"),
        )
        response = svc.test_connection()
        return jsonify({"ok": True, "response": response})
    except Exception as e:
        return jsonify({"error": f"Connection test failed: {str(e)}"}), 502


@app.route("/ai/daily-summary", methods=["POST"])
@require_auth
def ai_daily_summary():
    config = get_ai_config(g.user_id)
    if not config or not config.get("enabled"):
        return jsonify({"error": "AI not configured. Go to Settings → AI to set up."}), 400
    if not config.get("api_key"):
        return jsonify({"error": "API key not configured"}), 400

    # Fetch top 20 articles from news.db
    articles = _fetch_recent_articles(20)
    if not articles:
        return jsonify({"error": "no articles available"}), 404

    try:
        svc = AIService(
            api_key=config["api_key"],
            endpoint=config["endpoint"],
            model=config["model"],
            provider_type=config.get("provider_type", "openai"),
        )
        summary = svc.daily_summary(articles)
        return jsonify({"summary": summary, "article_count": len(articles)})
    except Exception as e:
        return jsonify({"error": f"AI request failed: {str(e)}"}), 502


def _fetch_recent_articles(limit: int = 20) -> list[dict]:
    """Fetch most recent articles from news.db."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return []
    try:
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, source, date, time FROM articles ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _fetch_article_body(article_id: int) -> dict | None:
    """Fetch full article body from news.db."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return None
    try:
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, title, source, summary, body_html FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


# ─── Settings Routes ────────────────────────────────────────

@app.route("/settings", methods=["GET"])
@require_auth
def get_settings():
    settings = get_user_settings(g.user_id)
    if not settings:
        return jsonify({
            "auto_translate_title": False,
            "auto_translate_content": False,
            "daily_summary_enabled": False,
            "notification_config": {},
        })
    # Parse notification_config JSON
    safe = dict(settings)
    nc = safe.get("notification_config", "{}")
    if isinstance(nc, str):
        try:
            nc = json.loads(nc)
        except (json.JSONDecodeError, TypeError):
            nc = {}
    safe["notification_config"] = nc
    return jsonify(safe)


@app.route("/settings", methods=["PUT"])
@require_auth
def update_settings():
    data = request.get_json(silent=True) or {}
    # Normalize notification_config to JSON string for storage
    if "notification_config" in data:
        nc = data["notification_config"]
        data["notification_config"] = json.dumps(nc) if isinstance(nc, dict) else nc
    settings = set_user_settings(g.user_id, **data)
    if not settings:
        return jsonify({"error": "update failed"}), 400
    # Parse back
    safe = dict(settings)
    nc = safe.get("notification_config", "{}")
    if isinstance(nc, str):
        try:
            nc = json.loads(nc)
        except (json.JSONDecodeError, TypeError):
            nc = {}
    safe["notification_config"] = nc
    return jsonify(safe)


import json


@app.route("/settings/test-notification", methods=["POST"])
@require_auth
def test_notification():
    """Send a test email via Resend API using env var RESEND_API_KEY, always from news@rayyu.me."""
    settings = get_user_settings(g.user_id) or {}

    nc = settings.get("notification_config", "{}")
    if isinstance(nc, str):
        try:
            nc = json.loads(nc)
        except (json.JSONDecodeError, TypeError):
            nc = {}

    config = nc.get("resend", {})
    # Always use RESEND_API_KEY from environment
    api_key = os.environ.get("RESEND_API_KEY", "")
    to_email = config.get("to_email", "")

    if not api_key:
        return jsonify({"error": "RESEND_API_KEY not set in server environment. Contact admin."}), 400
    if not to_email:
        return jsonify({"error": "notification not configured. Set recipient email in Settings."}), 400

    try:
        from notifier import send_email
        result = send_email(api_key, to_email,
                            "RayNews 测试通知",
                            "<h2>✅ 配置成功</h2><p>这是一封来自 RayNews 的测试邮件，通知功能正常工作。</p>",
                            from_email="news@rayyu.me")
        return jsonify({"ok": True, "id": result.get("id", "")})
    except Exception as e:
        return jsonify({"error": f"send failed: {str(e)}"}), 502


# ─── Health (unused section divider) ────────────────────────

# ─── Preview-restricted Refresh ──────────────────────────

@app.route("/auth/refresh", methods=["POST", "GET"])
@require_role("user", "admin")
def protected_refresh():
    """Trigger fetcher refresh. Protected from preview users."""
    import requests as http_req
    try:
        resp = http_req.get("http://127.0.0.1:8081/refresh", timeout=30)
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ─── Health ───────────────────────────────────────────────────

@app.route("/auth/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


# ─── Main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", 8082))
    print(f"[web] RayNews Web Server listening on {port}")
    app.run(host="127.0.0.1", port=port, debug=False)
