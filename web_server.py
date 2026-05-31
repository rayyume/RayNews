"""RayNews Web Server — auth, favorites, AI, settings via Flask."""

import os
import sys

from flask import Flask, request, jsonify, g
from flask_cors import CORS

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (
    get_db, create_user, get_user, get_user_by_email,
    update_user, delete_user, list_users, count_users,
    verify_password,
    add_favorite, remove_favorite, get_favorites, is_favorited,
)
from auth import init_auth, create_token, require_auth, require_role

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
        return jsonify({"error": "email already registered"}), 409

    token = create_token(user["id"], user["role"])
    return jsonify({"token": token, "user": user}), 201


@app.route("/auth/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = get_user_by_email(email)
    if not user or not verify_password(password, user["password"]):
        return jsonify({"error": "invalid email or password"}), 401

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
    return jsonify({"ok": ok}), 200 if ok else (jsonify({"error": "not found"}), 404)


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
        _news_conn = sqlite3.connect(NEWS_DB)
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
    return jsonify({"ok": ok}), 200 if ok else (jsonify({"error": "not found"}), 404)


@app.route("/favorites/<int:article_id>/status", methods=["GET"])
@require_auth
def favorite_status(article_id):
    return jsonify({"favorited": is_favorited(g.user_id, article_id)})


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
