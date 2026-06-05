"""RayNews Web Server — auth, favorites, AI, settings via Flask."""

import os
import re
import sys
import json
import threading
import time
import uuid
import requests

from bs4 import BeautifulSoup
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
    create_invitation_code, validate_invitation_code, use_invitation_code,
)
from auth import init_auth, create_token, require_auth, require_role
from ai_service import AIService
from source_categories import (
    CATEGORY_NAMES, CATEGORY_ORDER, cleanup_stale_source_categories,
    clamp_weighted, ensure_article_source_columns, ensure_article_sources,
    effective_source_rows, find_user_merge_target, merge_source,
    recent_titles_for_source, source_aliases_for_target, source_rows,
    update_source_category, extract_domains_from_html,
)

AUTO_SUMMARY_BATCH_LIMIT = int(os.environ.get("AUTO_SUMMARY_BATCH_LIMIT", "20"))
AUTO_SUMMARY_INTERVAL_SECONDS = int(os.environ.get("AUTO_SUMMARY_INTERVAL_SECONDS", "30"))
AUTO_TRANSLATION_BATCH_LIMIT = int(os.environ.get("AUTO_TRANSLATION_BATCH_LIMIT", "5"))
AUTO_TRANSLATION_INTERVAL_SECONDS = int(os.environ.get("AUTO_TRANSLATION_INTERVAL_SECONDS", "30"))
AUTO_SOURCE_CLASSIFY_BATCH_LIMIT = int(os.environ.get("AUTO_SOURCE_CLASSIFY_BATCH_LIMIT", "50"))
AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS = int(os.environ.get("AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS", "60"))
TELEGRAM_EMBED_TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_EMBED_TIMEOUT_SECONDS", "12"))

# ─── App Setup ────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)


# ─── JSON error handler — prevent HTML responses on errors ─────
@app.errorhandler(Exception)
def handle_exception(e):
    """Return JSON for all unhandled exceptions instead of HTML."""
    import traceback
    traceback.print_exc()
    # If it's already a Flask HTTPException with a JSON response, pass through
    from flask import jsonify as _jsonify
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return _jsonify({"error": e.description or str(e)}), e.code or 500
    return _jsonify({"error": f"server error: {str(e)}"}), 500

DATA_DIR = os.environ.get("DATA_DIR", "/app/data")

# Secret key: from env or persisted under DATA_DIR.
SECRET_KEY = os.environ.get("RAYNEWS_SECRET")
if not SECRET_KEY:
    from pathlib import Path
    import secrets
    secret_file = Path(DATA_DIR) / "raynews_secret"
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        if secret_file.exists():
            SECRET_KEY = secret_file.read_text(encoding="utf-8").strip()
        if not SECRET_KEY:
            SECRET_KEY = secrets.token_hex(32)
            secret_file.write_text(SECRET_KEY, encoding="utf-8")
        print(f"[web] Using persisted RAYNEWS_SECRET from {secret_file}")
    except Exception as e:
        SECRET_KEY = secrets.token_hex(32)
        print(f"[web] Could not persist RAYNEWS_SECRET ({e}); using ephemeral key: {SECRET_KEY[:16]}...")

init_auth(SECRET_KEY)


# ─── Auth Routes ──────────────────────────────────────────────

@app.route("/auth/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    nickname = (data.get("nickname") or "").strip()
    invite_code = (data.get("invite_code") or "").strip().upper()

    if not email or not password:
        return jsonify({"error": "email and password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400

    # First user (admin) doesn't need invite code; all others do
    if count_users() > 0:
        if not invite_code:
            return jsonify({"error": "invitation code required. Go to Settings → Request Invite"}), 400
        if not validate_invitation_code(invite_code, email):
            return jsonify({"error": "invalid or expired invitation code"}), 400

    role = "admin" if count_users() == 0 else "preview"

    user = create_user(email, password, nickname, role)
    if user is None:
        if nickname and get_user_by_username(nickname):
            return jsonify({"error": "username already taken"}), 409
        return jsonify({"error": "email already registered"}), 409

    # Mark invite code as used
    if invite_code:
        use_invitation_code(invite_code)

    token = create_token(user["id"], user["role"])
    return jsonify({"token": token, "user": user}), 201


@app.route("/auth/request-invite", methods=["POST"])
def request_invite():
    """Generate an 8-char invitation code and email it to the admin."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400

    # Check if already registered
    if get_user_by_email(email):
        return jsonify({"error": "email already registered"}), 409

    # Generate code
    code = create_invitation_code(email)

    # Send email via Resend
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return jsonify({"error": "invitation code generated but email service not configured. Contact admin."}), 500

    try:
        from notifier import send_email
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:-apple-system,sans-serif;background:#0a0a0c;color:#e8e8ed;padding:20px;max-width:500px;margin:0 auto}}
h1{{color:#6e8efb;font-size:18px}}
.code{{font-size:32px;font-weight:800;letter-spacing:6px;text-align:center;padding:20px;background:#1a1a1f;border-radius:12px;margin:20px 0;color:#6e8efb}}
.footer{{font-size:12px;color:#55556a;margin-top:20px}}
</style></head><body>
<h1>🔑 新用户邀请请求</h1>
<p>邮箱：<strong>{email}</strong></p>
<p>邀请码：</p>
<div class="code">{code}</div>
<p>将此邀请码告知用户，用户在注册时输入即可完成注册。</p>
<p class="footer">RayNews · 邀请码在注册后自动失效</p>
</body></html>"""
        send_email(api_key, "mail@rayyu.me",
                   f"RayNews 新用户邀请 — {email}",
                   html, from_name="RayNews")
    except Exception as e:
        print(f"[web] Failed to send invite email: {e}")
        return jsonify({"error": f"邀请码已生成，但邮件发送失败：{e}"}), 500

    return jsonify({"ok": True, "message": "邀请码已发送至管理员邮箱，请等待审核"}), 201


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

AVATARS_DIR = os.path.join(DATA_DIR, "avatars")
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

        import time
        avatar_url = f"/avatars/{g.user_id}.{ext}?v={int(time.time())}"
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

NEWS_DB = os.path.join(DATA_DIR, "news.db")


def _get_article_meta(article_id: int) -> dict | None:
    """Fetch article title/source/date/thumb from news.db by id."""
    if not os.path.exists(NEWS_DB):
        return None
    try:
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        ensure_article_source_columns(conn)
        row = conn.execute(
            "SELECT id, title, COALESCE(NULLIF(feed_source, ''), source) AS source, "
            "       COALESCE(NULLIF(feed_source, ''), source) AS feed_source, origin_source, "
            "       date, time, thumb, has_full_content, timestamp "
            "FROM articles WHERE id = ?",
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
        ensure_article_source_columns(_news_conn)
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
            "SELECT id, title, COALESCE(NULLIF(feed_source, ''), source) AS source, "
            "       COALESCE(NULLIF(feed_source, ''), source) AS feed_source, origin_source, "
            f"      date, time, thumb, has_full_content, timestamp FROM articles WHERE id IN ({placeholders})",
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
                "feed_source": meta.get("feed_source", meta["source"]),
                "origin_source": meta.get("origin_source", ""),
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
            "has_api_key": False,
        })
    safe = dict(config)
    has_key = bool(safe.get("api_key"))
    safe["has_api_key"] = has_key
    safe.pop("api_key", None)  # never expose the key to frontend
    return jsonify(safe)


@app.route("/ai/config", methods=["PUT"])
@require_auth
def set_ai_config_route():
    try:
        data = request.get_json(silent=True) or {}
        # If api_key contains "****", it's the masked placeholder — preserve existing
        api_key = data.get("api_key", "")
        if "****" in api_key:
            existing = get_ai_config(g.user_id)
            if existing and existing.get("api_key"):
                data["api_key"] = existing["api_key"]
            else:
                data.pop("api_key", None)
        config = set_ai_config(g.user_id, **data)
        safe = dict(config) if config else {}
        has_key = bool(safe.get("api_key"))
        safe["has_api_key"] = has_key
        safe.pop("api_key", None)
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

    can_use_shared_summary = _user_auto_summary_enabled(g.user_id)

    # Check cache first
    cached = _get_ai_result(article_id) if can_use_shared_summary else None
    if cached and cached.get("summary"):
        return jsonify({"summary": cached["summary"], "cached": True})

    try:
        summary, cached = _generate_article_summary(
            article_id,
            config,
            use_shared_cache=can_use_shared_summary,
            save_shared_cache=can_use_shared_summary,
        )
        return jsonify({"summary": summary, "cached": cached})
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"AI request failed: {str(e)}"}), 502


def _user_auto_summary_enabled(user_id: int) -> bool:
    settings = get_user_settings(user_id) or {}
    return bool(settings.get("auto_summary_enabled"))


def _generate_article_summary(article_id: int, config: dict,
                              use_shared_cache: bool = True,
                              save_shared_cache: bool = True) -> tuple[str, bool]:
    """Generate and cache a single-article AI summary."""
    cached = _get_ai_result(article_id) if use_shared_cache else None
    if cached and cached.get("summary"):
        return cached["summary"], True

    article = _fetch_article_body(article_id)
    if not article:
        raise KeyError("article not found")

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
    if save_shared_cache:
        _save_ai_result(article_id, summary=summary)
    return summary, False


@app.route("/ai/translate/<int:article_id>", methods=["POST"])
@require_auth
def ai_translate(article_id):
    config = get_ai_config(g.user_id)
    if not config or not config.get("enabled"):
        return jsonify({"error": "AI not configured. Go to Settings → AI to set up."}), 400
    if not config.get("api_key"):
        return jsonify({"error": "API key not configured"}), 400

    # Check cache first
    cached = _get_ai_result(article_id)
    if cached and cached.get("translation"):
        return jsonify({"translation": cached["translation"], "cached": True})

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
        # Save to cache
        _save_ai_result(article_id, translation=translation)
        return jsonify({"translation": translation})
    except Exception as e:
        return jsonify({"error": f"AI request failed: {str(e)}"}), 502


@app.route("/ai/translate-full/<int:article_id>", methods=["POST"])
@require_auth
def ai_translate_full(article_id):
    """Translate the full article HTML (including title) — replaces old segment-based approach.

    Frontend sends {html: "...", title: "...", target_lang: "zh-CN"}.
    Backend caches the result by article_id for reuse.
    Returns {translated_html, translated_title} both with and without title.
    """
    config = get_ai_config(g.user_id)
    if not config or not config.get("enabled"):
        return jsonify({"error": "AI not configured. Go to Settings → AI to set up."}), 400
    if not config.get("api_key"):
        return jsonify({"error": "API key not configured"}), 400

    data = request.get_json(silent=True) or {}
    html = data.get("html", "")
    title = data.get("title", "")
    target_lang = data.get("target_lang", "zh-CN")
    if not html:
        return jsonify({"error": "html required"}), 400

    # Check cache first
    cached = _get_ai_result(article_id)
    if cached and cached.get("translation"):
        t = cached["translation"]
        # New format with title: JSON {"title": "...", "html": "..."}
        if t and t.strip().startswith("{"):
            try:
                cached_data = json.loads(t)
                return jsonify({
                    "translated_html": cached_data.get("html", ""),
                    "translated_title": cached_data.get("title", ""),
                    "cached": True,
                })
            except (json.JSONDecodeError, TypeError):
                pass  # stale cache, re-translate
        # Legacy format: plain HTML (starts with '<') — no title
        elif t and t.strip().startswith("<"):
            return jsonify({"translated_html": t, "translated_title": "", "cached": True})

    try:
        svc = AIService(
            api_key=config["api_key"],
            endpoint=config["endpoint"],
            model=config["model"],
            provider_type=config.get("provider_type", "openai"),
        )
        result = svc.translate_full(html, target_lang, title=title)
        translated_html = result["html"]
        translated_title = result["title"]
        # Save to cache as JSON
        cache_data = json.dumps({"title": translated_title, "html": translated_html})
        _save_ai_result(article_id, translation=cache_data)
        # Also update article title in news.db so home page shows translated title
        if translated_title:
            try:
                _ndb = _get_news_db()
                _ndb.execute("UPDATE articles SET title = ? WHERE id = ?", (translated_title, article_id))
                _ndb.commit()
            except Exception:
                pass  # non-fatal if title update fails
        return jsonify({"translated_html": translated_html, "translated_title": translated_title})
    except Exception as e:
        return jsonify({"error": f"AI request failed: {str(e)}"}), 502


@app.route("/ai/translate-batch/<int:article_id>", methods=["POST"])
@require_auth
def ai_translate_batch(article_id):
    """Translate article paragraphs in batch — frontend provides segments, we cache by article_id."""
    config = get_ai_config(g.user_id)
    if not config or not config.get("enabled"):
        return jsonify({"error": "AI not configured. Go to Settings → AI to set up."}), 400
    if not config.get("api_key"):
        return jsonify({"error": "API key not configured"}), 400

    data = request.get_json(silent=True) or {}
    segments = data.get("segments", [])
    if not segments or not isinstance(segments, list):
        return jsonify({"error": "segments array required"}), 400

    # Check cache first
    cached = _get_ai_result(article_id)
    if cached and cached.get("translation"):
        try:
            import json
            return jsonify({"translations": json.loads(cached["translation"]), "cached": True})
        except (json.JSONDecodeError, TypeError):
            pass  # stale cache, re-translate

    try:
        svc = AIService(
            api_key=config["api_key"],
            endpoint=config["endpoint"],
            model=config["model"],
            provider_type=config.get("provider_type", "openai"),
        )
        translations = svc.translate_batch(segments)
        # Cache as JSON string in the translation field
        import json
        _save_ai_result(article_id, translation=json.dumps(translations))
        return jsonify({"translations": translations})
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
        if not response or not response.strip():
            return jsonify({"error": "Connection test returned empty response"}), 502
        return jsonify({"ok": True, "response": response})
    except requests.exceptions.ConnectTimeout as e:
        return jsonify({"error": "连接 AI 服务超时。请检查 API 地址是否正确，或 Docker 容器是否配置了 HTTP_PROXY 环境变量"}), 502
    except requests.exceptions.ConnectionError as e:
        return jsonify({"error": f"无法连接 AI 服务（{type(e).__name__}）。请检查网络代理配置：Docker 容器需要设置 HTTP_PROXY/HTTPS_PROXY 环境变量"}), 502
    except requests.exceptions.Timeout as e:
        return jsonify({"error": f"AI 服务响应超时: {e}"}), 502
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

    _cleanup_daily_summary_jobs()
    with _daily_summary_jobs_lock:
        for job in _daily_summary_jobs.values():
            if job.get("user_id") == g.user_id and job.get("status") in ("queued", "running"):
                return jsonify({"job_id": job["job_id"], "status": job["status"]}), 202

        job_id = uuid.uuid4().hex
        _daily_summary_jobs[job_id] = {
            "job_id": job_id,
            "user_id": g.user_id,
            "date": _today_str(),
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "summary": "",
            "article_count": 0,
            "stats": {},
            "error": "",
        }

    threading.Thread(
        target=_run_daily_summary_job,
        args=(job_id, g.user_id, config),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "status": "queued"}), 202


@app.route("/ai/daily-summary/today", methods=["GET"])
@require_auth
def ai_daily_summary_today():
    today_str = _today_str()
    _cleanup_daily_summary_jobs()
    with _daily_summary_jobs_lock:
        for job in _daily_summary_jobs.values():
            if (
                job.get("user_id") == g.user_id
                and job.get("date") == today_str
                and job.get("status") in ("queued", "running")
            ):
                return jsonify({
                    "status": job["status"],
                    "job_id": job["job_id"],
                    "date": today_str,
                    "created_at": job.get("created_at"),
                    "updated_at": job.get("updated_at"),
                })

    cached = _get_daily_summary_cache(g.user_id, today_str)
    if cached:
        return jsonify({
            "status": "completed",
            "date": today_str,
            "summary": cached["summary"],
            "article_count": cached["article_count"],
            "stats": cached["stats"],
            "updated_at": cached["updated_at"],
        })
    return jsonify({"status": "idle", "date": today_str})


@app.route("/ai/daily-summary/<job_id>", methods=["GET"])
@require_auth
def ai_daily_summary_status(job_id):
    with _daily_summary_jobs_lock:
        job = _daily_summary_jobs.get(job_id)
        if not job or job.get("user_id") != g.user_id:
            return jsonify({"error": "job not found"}), 404
        safe = {k: v for k, v in job.items() if k != "user_id"}
    return jsonify(safe)


_daily_summary_jobs = {}
_daily_summary_jobs_lock = threading.Lock()


def _today_str() -> str:
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y-%m-%d")


def _init_daily_summary_cache_table():
    if not os.path.exists(NEWS_DB):
        return
    try:
        conn = sqlite3.connect(NEWS_DB)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary_cache (
                user_id       INTEGER NOT NULL,
                date          TEXT NOT NULL,
                summary       TEXT NOT NULL,
                article_count INTEGER NOT NULL DEFAULT 0,
                stats         TEXT NOT NULL DEFAULT '{}',
                updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, date)
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[daily-summary] cache table init failed: {e}")


def _get_daily_summary_cache(user_id: int, date_str: str) -> dict | None:
    if not os.path.exists(NEWS_DB):
        return None
    try:
        _init_daily_summary_cache_table()
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT summary, article_count, stats, updated_at "
            "FROM daily_summary_cache WHERE user_id = ? AND date = ?",
            (user_id, date_str),
        ).fetchone()
        conn.close()
        if not row:
            return None
        data = dict(row)
        try:
            data["stats"] = json.loads(data.get("stats") or "{}")
        except (json.JSONDecodeError, TypeError):
            data["stats"] = {}
        return data
    except Exception as e:
        print(f"[daily-summary] cache read failed: {e}")
        return None


def _save_daily_summary_cache(user_id: int, date_str: str, summary: str,
                              article_count: int, stats: dict):
    if not os.path.exists(NEWS_DB):
        return
    try:
        _init_daily_summary_cache_table()
        conn = sqlite3.connect(NEWS_DB)
        conn.execute(
            "INSERT INTO daily_summary_cache "
            "(user_id, date, summary, article_count, stats, updated_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(user_id, date) DO UPDATE SET "
            "summary = excluded.summary, "
            "article_count = excluded.article_count, "
            "stats = excluded.stats, "
            "updated_at = datetime('now')",
            (user_id, date_str, summary, article_count, json.dumps(stats or {}, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[daily-summary] cache write failed: {e}")


def _cleanup_daily_summary_jobs():
    cutoff = time.time() - 3600
    with _daily_summary_jobs_lock:
        old_ids = [
            job_id
            for job_id, job in _daily_summary_jobs.items()
            if job.get("updated_at", job.get("created_at", 0)) < cutoff
        ]
        for job_id in old_ids:
            _daily_summary_jobs.pop(job_id, None)


def _update_daily_summary_job(job_id, **updates):
    updates["updated_at"] = time.time()
    with _daily_summary_jobs_lock:
        if job_id in _daily_summary_jobs:
            _daily_summary_jobs[job_id].update(updates)


def _run_daily_summary_job(job_id, user_id, config):
    _update_daily_summary_job(job_id, status="running")
    try:
        today_str = _today_str()
        articles = _fetch_articles_by_date(
            today_str,
            include_shared_summary=_user_auto_summary_enabled(user_id),
        )
        if not articles:
            _update_daily_summary_job(job_id, status="failed", error="no articles today")
            return

        raw_article_count = len(articles)
        articles = _dedup_articles(articles)
        deduped_article_count = len(articles)
        svc = AIService(
            api_key=config["api_key"],
            endpoint=config["endpoint"],
            model=config["model"],
            provider_type=config.get("provider_type", "openai"),
        )
        result = svc.daily_summary(articles)
        result["stats"]["total_articles"] = raw_article_count
        result["stats"]["articles_after_dedup"] = deduped_article_count
        _save_daily_summary_cache(
            user_id,
            today_str,
            result["summary"],
            raw_article_count,
            result["stats"],
        )
        _update_daily_summary_job(
            job_id,
            status="completed",
            summary=result["summary"],
            article_count=raw_article_count,
            stats=result["stats"],
        )
    except Exception as e:
        _update_daily_summary_job(job_id, status="failed", error=f"AI request failed: {str(e)}")


_daily_summary_sent = set()  # {(user_id, date), ...}
_auto_summary_lock = threading.Lock()
_auto_translation_lock = threading.Lock()
_auto_source_classify_lock = threading.Lock()


def _send_daily_summaries():
    """Check all users' settings and send daily summary emails where due."""
    import json as _json
    from notifier import send_daily_summary_email
    from models import get_db as _get_settings_db
    import datetime as _dt

    now = _dt.datetime.now()
    now_hhmm = now.strftime("%H:%M")
    today_str = now.strftime("%Y-%m-%d")
    now_minutes = now.hour * 60 + now.minute  # minutes since midnight for window matching

    print(f"[scheduler] Checking at {now_hhmm} ({today_str})...")

    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        print("[scheduler] RESEND_API_KEY not set, skipping")
        return

    try:
        db = _get_settings_db()
        rows = db.execute(
            "SELECT user_id, notification_config, daily_summary_enabled, auto_summary_enabled "
            "FROM user_settings WHERE daily_summary_enabled = 1"
        ).fetchall()
    except Exception as e:
        print(f"[scheduler] DB error: {e}")
        return

    print(f"[scheduler] Found {len(rows)} user(s) with daily_summary_enabled=1")

    for row in rows:
        settings = dict(row)
        nc = settings.get("notification_config", "{}")
        if isinstance(nc, str):
            try:
                nc = _json.loads(nc)
            except (_json.JSONDecodeError, TypeError):
                nc = {}
        resend_cfg = nc.get("resend", {})
        to_email = resend_cfg.get("to_email", "")
        scheduled_time = resend_cfg.get("daily_summary_time", "08:00")

        if not to_email:
            print(f"[scheduler] User {settings['user_id']}: no to_email, skipping")
            continue

        # Use a 10-minute window: trigger if current time is within 10 min of scheduled time
        try:
            sh, sm = map(int, scheduled_time.split(":"))
            scheduled_minutes = sh * 60 + sm
        except (ValueError, AttributeError):
            print(f"[scheduler] User {settings['user_id']}: invalid time '{scheduled_time}', skipping")
            continue

        diff = now_minutes - scheduled_minutes
        if diff < 0 or diff >= 10:
            print(f"[scheduler] User {settings['user_id']}: scheduled={scheduled_time} now={now_hhmm} (diff={diff}m), out of window, skipping")
            continue

        uid = settings["user_id"]
        dedup_key = (uid, today_str)
        if dedup_key in _daily_summary_sent:
            print(f"[scheduler] User {uid}: already sent today, skipping")
            continue

        print(f"[scheduler] User {uid}: time matched ({scheduled_time}), fetching articles...")
        articles = _fetch_articles_by_date(
            today_str,
            include_shared_summary=bool(settings.get("auto_summary_enabled")),
        )
        if not articles:
            print(f"[scheduler] User {uid}: no articles for {today_str}")
            continue

        from ai_service import AIService
        ai_config = _get_ai_config_for_user(uid)
        if not ai_config or not ai_config.get("enabled") or not ai_config.get("api_key"):
            print(f"[scheduler] User {uid}: AI not configured, skipping daily summary")
            continue

        try:
            svc = AIService(
                api_key=ai_config["api_key"],
                endpoint=ai_config["endpoint"],
                model=ai_config["model"],
                provider_type=ai_config.get("provider_type", "openai"),
            )
            raw_article_count = len(articles)
            articles = _dedup_articles(articles)
            deduped_article_count = len(articles)
            result = svc.daily_summary(articles)
            summary = result["summary"]
            stats = result["stats"]
            stats["total_articles"] = raw_article_count
            stats["articles_after_dedup"] = deduped_article_count
            _save_daily_summary_cache(uid, today_str, summary, raw_article_count, stats)
            send_daily_summary_email(resend_api_key, to_email, summary, stats)
            _daily_summary_sent.add(dedup_key)
            print(f"[scheduler] Daily summary sent to {to_email} for {today_str}. "
                  f"Stats: {stats}")
        except Exception as e:
            print(f"[scheduler] Daily summary failed for user {uid}: {e}")


def _daily_summary_loop():
    """Background loop: check every 60 seconds."""
    import time as _time
    _time.sleep(15)  # initial delay to let app start fully
    while True:
        try:
            _send_daily_summaries()
        except Exception as e:
            print(f"[scheduler] Error in loop: {e}")
        _time.sleep(60)


def _get_auto_summary_users() -> list[dict]:
    """Users who opted in to background article summaries and have usable AI config."""
    try:
        db = get_db()
        rows = db.execute(
            "SELECT s.user_id, c.endpoint, c.model, c.api_key, c.provider_type, c.enabled "
            "FROM user_settings s "
            "JOIN ai_configs c ON c.user_id = s.user_id "
            "WHERE s.auto_summary_enabled = 1 "
            "AND c.enabled = 1 "
            "AND c.api_key != ''"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[auto-summary] settings DB error: {e}")
        return []


def _get_auto_translation_users() -> list[dict]:
    """Users who opted in to background translation and have usable AI config."""
    try:
        db = get_db()
        rows = db.execute(
            "SELECT s.user_id, s.auto_translate_title, s.auto_translate_content, "
            "c.endpoint, c.model, c.api_key, c.provider_type, c.enabled "
            "FROM user_settings s "
            "JOIN ai_configs c ON c.user_id = s.user_id "
            "WHERE (s.auto_translate_title = 1 OR s.auto_translate_content = 1) "
            "AND c.enabled = 1 "
            "AND c.api_key != ''"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[auto-translate] settings DB error: {e}")
        return []


def _has_latin(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text or ""))


def _plain_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return " ".join(text.split()).strip()


def _needs_translation(text: str) -> bool:
    text = _plain_text(text)
    if not text or not _has_latin(text):
        return False
    latin_count = len(re.findall(r"[A-Za-z]", text))
    cjk_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    return cjk_count == 0 or (latin_count >= 40 and latin_count > cjk_count * 2)


def _fetch_untranslated_articles(config: dict, limit: int = AUTO_TRANSLATION_BATCH_LIMIT) -> list[dict]:
    """Fetch recent today articles that need background title/body translation."""
    import datetime as _dt
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return []
    translate_title = bool(config.get("auto_translate_title"))
    translate_content = bool(config.get("auto_translate_content"))
    if not translate_title and not translate_content:
        return []
    try:
        _init_ai_results_table()
        today_str = _dt.datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        ensure_article_source_columns(conn)
        rows = conn.execute(
            "SELECT a.id, a.title, COALESCE(NULLIF(a.feed_source, ''), a.source) AS source, "
            "       a.origin_source, a.summary, a.body_html, r.translation "
            "FROM articles a "
            "LEFT JOIN ai_results r ON r.article_id = a.id "
            "WHERE a.date = ? "
            "ORDER BY a.timestamp ASC LIMIT ?",
            (today_str, max(limit * 8, 40)),
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"[auto-translate] fetch failed: {e}")
        return []

    selected = []
    for row in rows:
        article = dict(row)
        title_needed = translate_title and _needs_translation(article.get("title", ""))
        content_needed = (
            translate_content
            and bool(article.get("body_html"))
            and _needs_translation(article.get("body_html") or article.get("summary") or "")
        )
        if title_needed or content_needed:
            article["translate_title_needed"] = title_needed
            article["translate_content_needed"] = content_needed
            selected.append(article)
        if len(selected) >= limit:
            break
    return selected


def _save_article_translation(article_id: int, title: str | None = None,
                              body_html: str | None = None):
    if not os.path.exists(NEWS_DB):
        return
    sets = []
    vals = []
    if title:
        sets.append("title = ?")
        vals.append(title)
    if body_html:
        sets.append("body_html = ?")
        vals.append(body_html)
    if not sets:
        return
    vals.append(article_id)
    conn = sqlite3.connect(NEWS_DB, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"UPDATE articles SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
    finally:
        conn.close()


def _cached_full_translation(translation: str | None) -> tuple[str, str]:
    text = (translation or "").strip()
    if not text:
        return "", ""
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return data.get("title", "") or "", data.get("html", "") or ""
        except (json.JSONDecodeError, TypeError):
            return "", ""
    if text.startswith("<"):
        return "", text
    return "", ""


def _translate_article_background(article: dict, config: dict) -> bool:
    svc = AIService(
        api_key=config["api_key"],
        endpoint=config["endpoint"],
        model=config["model"],
        provider_type=config.get("provider_type", "openai"),
    )
    article_id = article["id"]
    translated_title = None
    translated_html = None
    cached_title = ""

    if article.get("translate_content_needed"):
        cached_title, cached_html = _cached_full_translation(article.get("translation"))
        if cached_html:
            translated_html = cached_html
            if article.get("translate_title_needed"):
                translated_title = cached_title or None
        else:
            title_for_translation = article.get("title", "") if article.get("translate_title_needed") else ""
            result = svc.translate_full(
                article.get("body_html") or "",
                "zh-CN",
                title=title_for_translation,
            )
            translated_html = result.get("html") or ""
            if article.get("translate_title_needed"):
                translated_title = result.get("title") or None
        if translated_html:
            cache_data = json.dumps({
                "title": translated_title if translated_title is not None else cached_title,
                "html": translated_html,
            }, ensure_ascii=False)
            _save_ai_result(article_id, translation=cache_data)
        if article.get("translate_title_needed") and not translated_title:
            translated_title = svc.translate_title(article.get("title", ""), "zh-CN")

    elif article.get("translate_title_needed"):
        translated_title = svc.translate_title(article.get("title", ""), "zh-CN")

    _save_article_translation(article_id, title=translated_title, body_html=translated_html)
    return bool(translated_title or translated_html)


def _run_auto_translation_once():
    """Translate article titles/bodies in small batches for opted-in users."""
    if not _auto_translation_lock.acquire(blocking=False):
        return
    try:
        users = _get_auto_translation_users()
        if not users:
            return
        for config in users:
            articles = _fetch_untranslated_articles(config, AUTO_TRANSLATION_BATCH_LIMIT)
            if not articles:
                continue
            print(f"[auto-translate] User {config['user_id']}: translating {len(articles)} article(s)")
            for article in articles:
                try:
                    if _translate_article_background(article, config):
                        print(f"[auto-translate] Translated article {article['id']}: {article.get('title', '')[:50]}")
                except Exception as e:
                    print(f"[auto-translate] Article {article.get('id')}: failed: {e}")
    finally:
        _auto_translation_lock.release()


def _auto_translation_loop():
    """Background loop for opt-in automatic title/content translation."""
    import time as _time
    _time.sleep(60)
    while True:
        try:
            _run_auto_translation_once()
        except Exception as e:
            print(f"[auto-translate] Error in loop: {e}")
        _time.sleep(AUTO_TRANSLATION_INTERVAL_SECONDS)


def _get_source_classification_config() -> dict | None:
    """Return the first user AI config available for fallback callers."""
    try:
        db = get_db()
        row = db.execute(
            "SELECT c.user_id, c.endpoint, c.model, c.api_key, c.provider_type, c.enabled "
            "FROM ai_configs c "
            "WHERE c.enabled = 1 "
            "AND c.api_key != '' "
            "ORDER BY c.user_id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[source-classify] settings DB error: {e}")
        return None


def _get_source_classification_users() -> list[dict]:
    """Users whose own AI config can classify sources for their view."""
    try:
        db = get_db()
        rows = db.execute(
            "SELECT user_id, endpoint, model, api_key, provider_type, enabled "
            "FROM ai_configs "
            "WHERE enabled = 1 "
            "AND api_key != '' "
            "ORDER BY user_id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[source-classify] settings DB error: {e}")
        return []


def _classify_source_batch(config: dict, limit: int = AUTO_SOURCE_CLASSIFY_BATCH_LIMIT,
                           force: bool = False) -> dict:
    conn = _get_news_db()
    if not conn:
        return {"processed": [], "failed": [], "remaining": 0}
    user_id = config.get("user_id")
    ensure_article_sources(conn)
    rows = effective_source_rows(conn, user_id) if user_id else source_rows(conn)
    candidates = [
        row for row in rows
        if row.get("source")
        and not row.get("alias_target")
        and not row.get("user_override")
        and row.get("status") != "manual"
        and (
            force
            or row.get("status") in ("pending", "failed")
            or (
                row.get("status") == "classified"
                and (row.get("confidence") or 0) < 0.6
            )
        )
    ][:limit]

    svc = AIService(
        api_key=config["api_key"],
        endpoint=config["endpoint"],
        model=config["model"],
        provider_type=config.get("provider_type", "openai"),
    )
    processed = []
    failed = []
    for row in candidates:
        source = row["source"]
        titles = recent_titles_for_source(conn, source, limit=8)
        # Extract domains from recent article bodies for stronger AI signal
        domains = _extract_domains_for_source(conn, source)
        try:
            result = svc.classify_source(source, titles, domains=domains)
            saved = update_source_category(
                conn,
                source,
                result["category"],
                result["label"],
                status="classified",
                confidence=result.get("confidence"),
                reason=result.get("reason") or "ai classified",
                sample_titles=titles,
            )
            processed.append(saved)
        except Exception as e:
            try:
                update_source_category(
                    conn,
                    source,
                    row.get("category") or "Info",
                    row.get("label") or source,
                    status="failed",
                    reason=str(e)[:300],
                    sample_titles=titles,
                )
            except Exception:
                pass
            failed.append({"source": source, "error": str(e)})

    remaining = [
        row for row in (effective_source_rows(conn, user_id) if user_id else source_rows(conn))
        if not row.get("alias_target")
        and not row.get("user_override")
        and row.get("status") in ("pending", "failed")
        and row.get("status") != "manual"
    ]
    return {
        "processed": processed,
        "failed": failed,
        "remaining": len(remaining),
    }


def _extract_domains_for_source(conn, source: str) -> list[str]:
    """Extract unique domain names from recent articles of a given source."""
    try:
        rows = conn.execute(
            "SELECT body_html, telegraph_url FROM articles "
            "WHERE COALESCE(NULLIF(feed_source, ''), source) = ? "
            "AND (body_html != '' OR telegraph_url != '') "
            "ORDER BY timestamp DESC LIMIT 5",
            (source,),
        ).fetchall()
    except Exception:
        return []
    all_domains = []
    seen = set()
    for row in rows:
        html = (row["body_html"] or "") + " " + (row["telegraph_url"] or "")
        for domain in extract_domains_from_html(html):
            if domain not in seen:
                seen.add(domain)
                all_domains.append(domain)
    return all_domains[:10]


_source_classify_jobs = {}
_source_classify_jobs_lock = threading.Lock()


def _update_source_classify_job(job_id: str, **updates):
    updates["updated_at"] = time.time()
    with _source_classify_jobs_lock:
        if job_id in _source_classify_jobs:
            _source_classify_jobs[job_id].update(updates)


def _run_source_classify_job(job_id: str, user_id: int, config: dict, force: bool):
    _update_source_classify_job(job_id, status="running")
    processed_total = 0
    failed_total = 0
    remaining = 0
    try:
        config = dict(config)
        config["user_id"] = user_id
        for _ in range(200):
            result = _classify_source_batch(config, limit=50, force=force)
            processed = len(result.get("processed") or [])
            failed = len(result.get("failed") or [])
            remaining = int(result.get("remaining") or 0)
            processed_total += processed
            failed_total += failed
            _update_source_classify_job(
                job_id,
                processed=processed_total,
                failed=failed_total,
                remaining=remaining,
            )
            if remaining <= 0:
                break
            if processed == 0:
                break
            # With force=True, one pass over non-manual sources is enough.
            if force:
                break
        _update_source_classify_job(
            job_id,
            status="completed",
            processed=processed_total,
            failed=failed_total,
            remaining=remaining,
        )
    except Exception as exc:
        _update_source_classify_job(job_id, status="failed", error=str(exc))


def _run_auto_source_classification_once():
    if not _auto_source_classify_lock.acquire(blocking=False):
        return
    try:
        configs = _get_source_classification_users()
        if not configs:
            return
        for config in configs:
            result = _classify_source_batch(config, AUTO_SOURCE_CLASSIFY_BATCH_LIMIT)
            if result["processed"] or result["failed"]:
                print(
                    f"[source-classify] user={config['user_id']} processed="
                    f"{len(result['processed'])} failed={len(result['failed'])} "
                    f"remaining={result['remaining']}"
                )
            if result["remaining"] == 0:
                break
    finally:
        _auto_source_classify_lock.release()


def _auto_source_classification_loop():
    import time as _time
    _time.sleep(90)
    cleanup_counter = 0
    while True:
        try:
            _run_auto_source_classification_once()
        except Exception as e:
            print(f"[source-classify] Error in loop: {e}")
        # Periodically clean up sources with 0 articles (every 10 cycles)
        cleanup_counter += 1
        if cleanup_counter % 10 == 0:
            try:
                conn = _get_news_db()
                if conn:
                    deleted = cleanup_stale_source_categories(conn)
                    conn.commit()
                    if deleted:
                        print(f"[source-cleanup] removed {deleted} stale source(s)")
            except Exception as e:
                print(f"[source-cleanup] Error: {e}")
        _time.sleep(AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS)


def _run_auto_summary_once():
    """Fill cached summaries in small batches so daily summaries can reuse them."""
    if not _auto_summary_lock.acquire(blocking=False):
        return
    try:
        users = _get_auto_summary_users()
        if not users:
            return

        for config in users:
            articles = _fetch_unsummarized_articles(AUTO_SUMMARY_BATCH_LIMIT)
            if not articles:
                continue
            print(f"[auto-summary] User {config['user_id']}: summarizing {len(articles)} article(s)")
            for article in articles:
                try:
                    summary, cached = _generate_article_summary(article["id"], config)
                    if not cached:
                        print(f"[auto-summary] Cached summary for article {article['id']}: {article.get('title', '')[:50]}")
                except Exception as e:
                    _save_ai_result(article["id"], summary_error=str(e))
                    print(f"[auto-summary] Article {article.get('id')}: failed: {e}")
    finally:
        _auto_summary_lock.release()


def _auto_summary_loop():
    """Background loop for opt-in automatic article summaries."""
    import time as _time
    _time.sleep(45)
    while True:
        try:
            _run_auto_summary_once()
        except Exception as e:
            print(f"[auto-summary] Error in loop: {e}")
        _time.sleep(AUTO_SUMMARY_INTERVAL_SECONDS)


@app.route("/ai/daily-summary/send", methods=["POST"])
@require_role("admin")
def ai_daily_summary_send():
    """Manually trigger daily summary delivery for administrators."""
    _send_daily_summaries()
    return jsonify({"status": "ok", "checked_at": __import__("datetime").datetime.now().strftime("%H:%M")})


def _get_ai_config_for_user(user_id: int) -> dict | None:
    """Fetch a user's AI config for programmatic use."""
    from models import get_db as _db
    try:
        db = _db()
        row = db.execute(
            "SELECT endpoint, model, api_key, provider_type, enabled "
            "FROM ai_configs WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _fetch_recent_articles(limit: int = 20) -> list[dict]:
    """Fetch most recent articles from news.db."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return []
    try:
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        ensure_article_source_columns(conn)
        rows = conn.execute(
            "SELECT id, title, COALESCE(NULLIF(feed_source, ''), source) AS source, "
            "       COALESCE(NULLIF(feed_source, ''), source) AS feed_source, origin_source, "
            "       date, time FROM articles ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ─── Dedup helper ──────────────────────────────────────────


def _dedup_articles(articles: list[dict]) -> list[dict]:
    """Remove duplicate/similar articles. Prefers articles with body content.

    Heuristic: same normalized title → same event → keep first with body.
    """
    best_by_title = {}
    untitled = []
    for a in articles:
        title = (a.get("title") or "").strip().lower()
        # Normalize whitespace and common quote characters.
        normalized = re.sub("[\\s\"'“”‘’「」『』]+", " ", title).strip()
        if not normalized:
            untitled.append(a)
            continue
        existing = best_by_title.get(normalized)
        if existing is None or (a.get("body_html") and not existing.get("body_html")):
            best_by_title[normalized] = a
    return untitled + list(best_by_title.values())


def _fetch_articles_by_date(date_str: str, include_shared_summary: bool = True) -> list[dict]:
    """Fetch articles for a date, preferring cached AI summaries when available."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return []
    try:
        _init_ai_results_table()
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        ensure_article_source_columns(conn)
        summary_expr = (
            "COALESCE(NULLIF(r.summary, ''), a.summary)"
            if include_shared_summary else "a.summary"
        )
        rows = conn.execute(
            "SELECT a.id, a.title, COALESCE(NULLIF(a.feed_source, ''), a.source) AS source, "
            "a.origin_source, a.date, a.time, a.body_html, "
            f"{summary_expr} AS summary, "
            "a.telegraph_url "
            "FROM articles a "
            "LEFT JOIN ai_results r ON r.article_id = a.id "
            "WHERE a.date = ? ORDER BY a.timestamp ASC",
            (date_str,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _fetch_unsummarized_articles(limit: int = AUTO_SUMMARY_BATCH_LIMIT) -> list[dict]:
    """Fetch recent today articles without cached AI summaries."""
    import datetime as _dt
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return []
    try:
        _init_ai_results_table()
        today_str = _dt.datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        ensure_article_source_columns(conn)
        rows = conn.execute(
            "SELECT a.id, a.title, COALESCE(NULLIF(a.feed_source, ''), a.source) AS source, "
            "a.origin_source, a.summary, a.body_html "
            "FROM articles a "
            "LEFT JOIN ai_results r ON r.article_id = a.id "
            "WHERE a.date = ? "
            "AND (r.summary IS NULL OR r.summary = '') "
            "AND (r.summary_error_at IS NULL OR datetime(r.summary_error_at, '+6 hours') < datetime('now')) "
            "AND (a.body_html != '' OR a.summary != '') "
            "ORDER BY a.timestamp ASC LIMIT ?",
            (today_str, limit),
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
        ensure_article_source_columns(conn)
        row = conn.execute(
            "SELECT id, title, COALESCE(NULLIF(feed_source, ''), source) AS source, "
            "origin_source, summary, body_html FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


# ─── AI Results Cache (prevent duplicate generation) ────────


def _init_ai_results_table():
    """Create ai_results table in news.db if not exists."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return
    try:
        conn = sqlite3.connect(NEWS_DB)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_results (
                article_id   INTEGER PRIMARY KEY,
                summary      TEXT,
                translation  TEXT,
                summary_error TEXT,
                summary_error_at TEXT,
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(ai_results)").fetchall()
        }
        if "summary_error" not in cols:
            conn.execute("ALTER TABLE ai_results ADD COLUMN summary_error TEXT")
        if "summary_error_at" not in cols:
            conn.execute("ALTER TABLE ai_results ADD COLUMN summary_error_at TEXT")
        conn.commit()
        conn.close()
    except Exception:
        pass


def _get_ai_result(article_id: int) -> dict | None:
    """Get cached AI result (summary/translation) for an article."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return None
    try:
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        _init_ai_results_table()
        row = conn.execute(
            "SELECT summary, translation, summary_error, summary_error_at "
            "FROM ai_results WHERE article_id = ?",
            (article_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _save_ai_result(article_id: int, summary: str | None = None,
                    translation: str | None = None,
                    summary_error: str | None = None):
    """Save or update AI result for an article."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return
    try:
        conn = sqlite3.connect(NEWS_DB)
        existing = _get_ai_result(article_id)
        if existing:
            sets = []
            vals = []
            if summary is not None:
                sets.append("summary = ?")
                vals.append(summary)
                sets.append("summary_error = NULL")
                sets.append("summary_error_at = NULL")
            if translation is not None:
                sets.append("translation = ?")
                vals.append(translation)
            if summary_error is not None:
                sets.append("summary_error = ?")
                vals.append(summary_error[:500])
                sets.append("summary_error_at = datetime('now')")
            if sets:
                sets.append("updated_at = datetime('now')")
                vals.append(article_id)
                conn.execute(
                    f"UPDATE ai_results SET {', '.join(sets)} WHERE article_id = ?",
                    vals,
                )
        else:
            conn.execute(
                "INSERT INTO ai_results "
                "(article_id, summary, translation, summary_error, summary_error_at) "
                "VALUES (?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END)",
                (article_id, summary, translation, summary_error[:500] if summary_error else None, summary_error),
            )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ─── AI Result Cache (read-only) ──────────────────────────


@app.route("/ai/result/<int:article_id>", methods=["GET"])
@require_auth
def ai_get_result(article_id):
    """Return cached AI result (summary/translation) without generating."""
    cached = _get_ai_result(article_id)
    if cached and not _user_auto_summary_enabled(g.user_id):
        cached = dict(cached)
        cached.pop("summary", None)
    return jsonify(cached or {})


# ─── Source Categories ─────────────────────────────────────

@app.route("/sources", methods=["GET"])
@require_auth
def list_sources():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    return jsonify({
        "categories": CATEGORY_ORDER,
        "category_names": CATEGORY_NAMES,
        "sources": effective_source_rows(conn, g.user_id),
    })


@app.route("/sources/articles", methods=["GET"])
@require_auth
def list_source_articles():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    sources_json = (request.args.get("sources") or "").strip()
    if sources_json:
        try:
            base_sources = [
                str(item).strip()
                for item in json.loads(sources_json)
                if str(item).strip()
            ][:100]
        except (json.JSONDecodeError, TypeError, ValueError):
            return jsonify({"error": "invalid sources"}), 400
    else:
        source = (request.args.get("source") or "").strip()
        base_sources = [source] if source else []
    if not base_sources:
        return jsonify({"error": "source required"}), 400
    try:
        limit = min(max(int(request.args.get("limit", "80")), 1), 200)
    except ValueError:
        limit = 80

    sources = []
    for source in base_sources:
        sources.append(source)
        sources.extend(source_aliases_for_target(conn, g.user_id, source))
    sources = list(dict.fromkeys(sources))
    placeholders = ",".join("?" * len(sources))
    rows = conn.execute(
        "SELECT id, title, COALESCE(NULLIF(feed_source, ''), source) AS source, "
        "       COALESCE(NULLIF(feed_source, ''), source) AS feed_source, origin_source, "
        "       date, time, timestamp, thumb, has_full_content "
        f"FROM articles WHERE COALESCE(NULLIF(feed_source, ''), source) IN ({placeholders}) "
        "ORDER BY timestamp DESC LIMIT ?",
        (*sources, limit),
    ).fetchall()
    return jsonify({
        "source": base_sources[0],
        "sources": sources,
        "items": [dict(row) for row in rows],
    })


@app.route("/sources", methods=["PUT"])
@require_auth
def save_source():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    data = request.get_json(silent=True) or {}
    source = (data.get("source") or "").strip()
    category = (data.get("category") or "").strip()
    label = (data.get("label") or "").strip()
    if not source:
        return jsonify({"error": "source required"}), 400
    if category not in CATEGORY_ORDER:
        return jsonify({"error": "invalid category"}), 400
    if not label:
        return jsonify({"error": "label required"}), 400
    label = clamp_weighted(label, 20)
    try:
        target_source = find_user_merge_target(conn, g.user_id, source, label)
        if target_source:
            target = merge_source(conn, source, target_source, user_id=g.user_id)
            return jsonify({
                **target,
                "merged": True,
                "merged_from": source,
                "target_source": target_source,
            })
        row = update_source_category(
            conn, source, category, label, status="manual", reason="user edited",
            user_id=g.user_id,
        )
        return jsonify(row)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/sources/classify", methods=["POST"])
@require_auth
def classify_sources():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    config = get_ai_config(g.user_id)
    if not config or not config.get("enabled") or not config.get("api_key"):
        return jsonify({"error": "请先在AI菜单中设置API"}), 400

    data = request.get_json(silent=True) or {}
    limit = min(max(int(data.get("limit", 50) or 50), 1), 100)
    force = bool(data.get("force"))
    config = dict(config)
    config["user_id"] = g.user_id
    return jsonify(_classify_source_batch(config, limit, force))


@app.route("/sources/reinitialize", methods=["POST"])
@require_role("admin")
def reinitialize_sources():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    data = request.get_json(silent=True) or {}
    preserve_manual = data.get("preserve_manual", True) is not False
    conn.execute("DELETE FROM source_categories")
    conn.execute("DELETE FROM source_aliases")
    if not preserve_manual:
        conn.execute("DELETE FROM user_source_categories")
        conn.execute("DELETE FROM user_source_aliases")
    ensure_article_sources(conn)
    cleanup_stale_source_categories(conn)
    conn.commit()
    count = conn.execute("SELECT COUNT(*) AS c FROM source_categories").fetchone()["c"]
    return jsonify({"ok": True, "sources": count, "preserve_manual": preserve_manual})


@app.route("/sources/classify-job", methods=["POST"])
@require_auth
def classify_sources_job():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    config = get_ai_config(g.user_id)
    if not config or not config.get("enabled") or not config.get("api_key"):
        return jsonify({"error": "请先在AI菜单中设置API"}), 400

    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    with _source_classify_jobs_lock:
        for job in _source_classify_jobs.values():
            if job.get("user_id") == g.user_id and job.get("status") in ("queued", "running"):
                return jsonify({"job_id": job["job_id"], "status": job["status"]}), 202
        job_id = uuid.uuid4().hex
        _source_classify_jobs[job_id] = {
            "job_id": job_id,
            "user_id": g.user_id,
            "status": "queued",
            "force": force,
            "created_at": time.time(),
            "updated_at": time.time(),
            "processed": 0,
            "failed": 0,
            "remaining": 0,
            "error": "",
        }
    threading.Thread(
        target=_run_source_classify_job,
        args=(job_id, g.user_id, config, force),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "status": "queued"}), 202


@app.route("/sources/classify-job/<job_id>", methods=["GET"])
@require_auth
def classify_sources_job_status(job_id):
    with _source_classify_jobs_lock:
        job = _source_classify_jobs.get(job_id)
        if not job or job.get("user_id") != g.user_id:
            return jsonify({"error": "job not found"}), 404
        safe = {k: v for k, v in job.items() if k != "user_id"}
    return jsonify(safe)


def _fetch_telegram_message_content(article_id: int) -> str:
    """Fetch the original Telegram message body for historical source repair."""
    channel = (os.environ.get("TELEGRAM_CHANNEL") or "").strip().lstrip("@")
    if not channel or channel == "your_channel":
        return ""
    url = f"https://t.me/{channel}/{article_id}?embed=1&mode=tme"
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36"
                )
            },
            timeout=TELEGRAM_EMBED_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"telegram source lookup failed for {article_id}: {exc}")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    text_el = soup.select_one(".tgme_widget_message_text.js-message_text")
    if not text_el:
        text_el = soup.select_one(".tgme_widget_message_text")
    return text_el.decode_contents() if text_el else ""


_source_redetect_jobs = {}
_source_redetect_jobs_lock = threading.Lock()


def _parse_source_redetect_options(data: dict) -> tuple[int, int, bool]:
    try:
        limit = min(max(int(data.get("limit", 2000) or 2000), 1), 10000)
    except (TypeError, ValueError):
        limit = 2000
    try:
        network_limit = min(max(int(data.get("network_limit", 1000) or 1000), 0), limit)
    except (TypeError, ValueError):
        network_limit = min(1000, limit)
    force_telegram = bool(data.get("force_telegram"))
    return limit, network_limit, force_telegram


def _cleanup_source_redetect_jobs():
    cutoff = time.time() - 7200
    with _source_redetect_jobs_lock:
        old_ids = [
            job_id for job_id, job in _source_redetect_jobs.items()
            if job.get("updated_at", job.get("created_at", 0)) < cutoff
        ]
        for job_id in old_ids:
            _source_redetect_jobs.pop(job_id, None)


def _update_source_redetect_job(job_id: str, **updates):
    updates["updated_at"] = time.time()
    with _source_redetect_jobs_lock:
        if job_id in _source_redetect_jobs:
            _source_redetect_jobs[job_id].update(updates)


def _redetect_article_sources_work(limit: int, network_limit: int, force_telegram: bool,
                                   job_id: str | None = None) -> dict:
    if not os.path.exists(NEWS_DB):
        raise FileNotFoundError("news db not found")
    from fetcher import detect_feed_source, detect_source, detect_source_from_attribution

    conn = sqlite3.connect(NEWS_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    ensure_article_source_columns(conn)
    rows = conn.execute(
        """
        SELECT id, title, source, feed_source, origin_source, body_html, summary, telegraph_url
        FROM articles
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    changed = []
    skipped = 0
    telegram_checked = 0
    telegram_hits = 0
    checked = 0
    def update_progress():
        if job_id and (checked % 20 == 0 or checked == len(rows)):
            _update_source_redetect_job(
                job_id,
                checked=checked,
                updated=len(changed),
                skipped=skipped,
                telegram_checked=telegram_checked,
                telegram_hits=telegram_hits,
            )

    for row in rows:
        checked += 1
        detected_feed = None
        detected_origin = None
        fetched_telegram = False
        telegram_content = ""
        is_telegraph = bool(row["telegraph_url"])
        should_fetch_telegram = (
            telegram_checked < network_limit
            and (force_telegram or is_telegraph)
        )
        if should_fetch_telegram:
            fetched_telegram = True
            telegram_checked += 1
            telegram_content = _fetch_telegram_message_content(row["id"])
            if telegram_content:
                detected_feed = detect_feed_source(telegram_content)
                detected_origin = detect_source(telegram_content)
                if detected_feed or detected_origin:
                    telegram_hits += 1

        content = "\n".join([
            row["body_html"] or "",
            row["summary"] or "",
            row["title"] or "",
        ])
        if not detected_feed and (telegram_content or not is_telegraph):
            detected_feed = detect_feed_source(telegram_content or content)
        detected_origin = detected_origin or detect_source_from_attribution(content)
        # Telegraph body is mirrored article content; arbitrary links in it are not source attribution.
        if not detected_origin and not is_telegraph:
            detected_origin = detect_source(content)
        if (not detected_feed or not detected_origin) and not fetched_telegram and telegram_checked < network_limit:
            telegram_checked += 1
            telegram_content = _fetch_telegram_message_content(row["id"])
            if telegram_content:
                detected_feed = detected_feed or detect_feed_source(telegram_content)
                detected_origin = detected_origin or detect_source(telegram_content)
                if detected_feed or detected_origin:
                    telegram_hits += 1
        if not detected_feed and not detected_origin:
            skipped += 1
            update_progress()
            continue
        current_feed = (row["feed_source"] or row["source"] or "").strip()
        current_origin = (row["origin_source"] or "").strip()
        next_feed = detected_feed or current_feed
        next_origin = detected_origin or current_origin
        if next_feed == current_feed and next_origin == current_origin:
            skipped += 1
            update_progress()
            continue
        conn.execute(
            "UPDATE articles SET source = ?, feed_source = ?, origin_source = ? WHERE id = ?",
            (next_feed, next_feed, next_origin, row["id"]),
        )
        changed.append({
            "id": row["id"],
            "title": row["title"],
            "from": current_feed,
            "to": next_feed,
            "origin_from": current_origin,
            "origin_to": next_origin,
        })
        update_progress()
    ensure_article_sources(conn)
    deleted_sources = cleanup_stale_source_categories(conn)
    conn.commit()
    conn.close()
    return {
        "checked": len(rows),
        "updated": len(changed),
        "skipped": skipped,
        "telegram_checked": telegram_checked,
        "telegram_hits": telegram_hits,
        "deleted_sources": deleted_sources,
        "changes": changed[:100],
    }


def _run_source_redetect_job(job_id: str, limit: int, network_limit: int, force_telegram: bool):
    _update_source_redetect_job(job_id, status="running")
    try:
        result = _redetect_article_sources_work(limit, network_limit, force_telegram, job_id)
        _update_source_redetect_job(job_id, status="completed", **result)
    except Exception as exc:
        _update_source_redetect_job(job_id, status="failed", error=str(exc))


@app.route("/sources/redetect", methods=["POST"])
@require_role("admin")
def redetect_article_sources():
    if not os.path.exists(NEWS_DB):
        return jsonify({"error": "news db not found"}), 404
    data = request.get_json(silent=True) or {}
    limit, network_limit, force_telegram = _parse_source_redetect_options(data)
    _cleanup_source_redetect_jobs()
    with _source_redetect_jobs_lock:
        for job in _source_redetect_jobs.values():
            if job.get("user_id") == g.user_id and job.get("status") in ("queued", "running"):
                return jsonify({"job_id": job["job_id"], "status": job["status"]}), 202
        job_id = uuid.uuid4().hex
        _source_redetect_jobs[job_id] = {
            "job_id": job_id,
            "user_id": g.user_id,
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "limit": limit,
            "network_limit": network_limit,
            "checked": 0,
            "updated": 0,
            "skipped": 0,
            "telegram_checked": 0,
            "telegram_hits": 0,
            "deleted_sources": 0,
            "error": "",
        }
    threading.Thread(
        target=_run_source_redetect_job,
        args=(job_id, limit, network_limit, force_telegram),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "status": "queued"}), 202


@app.route("/sources/redetect/<job_id>", methods=["GET"])
@require_role("admin")
def source_redetect_status(job_id):
    with _source_redetect_jobs_lock:
        job = _source_redetect_jobs.get(job_id)
        if not job or job.get("user_id") != g.user_id:
            return jsonify({"error": "job not found"}), 404
        safe = {k: v for k, v in job.items() if k != "user_id"}
    return jsonify(safe)


@app.route("/sources/redetect-single", methods=["POST"])
@require_auth
def redetect_single_source():
    """Re-detect source for all articles matching a given source name."""
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    data = request.get_json(silent=True) or {}
    source = (data.get("source") or "").strip()
    if not source:
        return jsonify({"error": "source required"}), 400

    from fetcher import detect_feed_source, detect_source, detect_source_from_attribution

    rows = conn.execute(
        "SELECT id, title, source, feed_source, origin_source, body_html, summary, telegraph_url "
        "FROM articles "
        "WHERE COALESCE(NULLIF(feed_source, ''), source) = ? "
        "ORDER BY timestamp DESC",
        (source,),
    ).fetchall()

    changed = []
    for row in rows:
        # Join with <br> so via line stays isolated from title/summary in chunk splitting
        content = "<br>".join([
            row["body_html"] or "",
            row["summary"] or "",
            row["title"] or "",
        ])
        detected_feed = None if row["telegraph_url"] else detect_feed_source(content)
        detected_origin = detect_source_from_attribution(content)
        if not detected_origin and not row["telegraph_url"]:
            detected_origin = detect_source(content)
        # If still no result, try fetching the original Telegram message
        # (body_html may be Telegraph content without via lines)
        if not detected_feed or not detected_origin:
            tg_content = _fetch_telegram_message_content(row["id"])
            if tg_content:
                detected_feed = detected_feed or detect_feed_source(tg_content)
                detected_origin = detected_origin or detect_source(tg_content)
        current_feed = (row["feed_source"] or row["source"] or "").strip()
        current_origin = (row["origin_source"] or "").strip()
        next_feed = detected_feed or current_feed
        next_origin = detected_origin or current_origin
        if next_feed == current_feed and next_origin == current_origin:
            continue
        conn.execute(
            "UPDATE articles SET source = ?, feed_source = ?, origin_source = ? WHERE id = ?",
            (next_feed, next_feed, next_origin, row["id"]),
        )
        changed.append({
            "id": row["id"],
            "title": row["title"],
            "from": current_feed,
            "to": next_feed,
            "origin_from": current_origin,
            "origin_to": next_origin,
        })

    ensure_article_sources(conn)
    cleanup_stale_source_categories(conn)
    conn.commit()
    return jsonify({"ok": True, "checked": len(rows), "updated": len(changed), "changes": changed[:50]})


# ─── Settings Routes ────────────────────────────────────────

@app.route("/settings", methods=["GET"])
@require_auth
def get_settings():
    settings = get_user_settings(g.user_id)
    if not settings:
        return jsonify({
            "auto_translate_title": False,
            "auto_translate_content": False,
            "auto_summary_enabled": False,
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
    needs_ai_config = any(
        _is_enabled_value(data.get(key))
        for key in ("auto_summary_enabled", "auto_translate_title", "auto_translate_content")
    )
    if needs_ai_config:
        config = get_ai_config(g.user_id)
        if not config or not config.get("enabled") or not config.get("api_key"):
            return jsonify({
                "error": "请先在AI菜单中设置API"
            }), 400
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
    if _is_enabled_value(data.get("auto_summary_enabled")):
        threading.Thread(target=_run_auto_summary_once, daemon=True).start()
    if _is_enabled_value(data.get("auto_translate_title")) or _is_enabled_value(data.get("auto_translate_content")):
        threading.Thread(target=_run_auto_translation_once, daemon=True).start()
    return jsonify(safe)


def _is_enabled_value(value) -> bool:
    return value is True or value == 1 or str(value).strip().lower() in {"1", "true", "yes", "on"}


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


@app.route("/scheduler/status", methods=["GET"])
def scheduler_status():
    """Return scheduler status for debugging."""
    import datetime as _dt
    now = _dt.datetime.now()
    return jsonify({
        "running": True,
        "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "today": now.strftime("%Y-%m-%d"),
        "timezone_hint": str(_dt.datetime.now().astimezone().tzinfo),
        "sent_today": list(_daily_summary_sent),
    })


# ─── Main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    _init_ai_results_table()
    _init_daily_summary_cache_table()
    import threading as _th
    _th.Thread(target=_daily_summary_loop, daemon=True).start()
    _th.Thread(target=_auto_summary_loop, daemon=True).start()
    _th.Thread(target=_auto_translation_loop, daemon=True).start()
    _th.Thread(target=_auto_source_classification_loop, daemon=True).start()
    print("[scheduler] Daily summary background thread started")
    print("[auto-summary] Background summary thread started")
    print("[auto-translate] Background translation thread started")
    print("[source-classify] Background source classification thread started")
    port = int(os.environ.get("WEB_PORT", 8082))
    print(f"[web] RayNews Web Server listening on {port}")
    app.run(host="127.0.0.1", port=port, debug=False)
