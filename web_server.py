"""RayNews Web Server — auth, favorites, AI, settings via Flask."""

import os
import sys
import requests

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

    # Check cache first
    cached = _get_ai_result(article_id)
    if cached and cached.get("summary"):
        return jsonify({"summary": cached["summary"], "cached": True})

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
        # Save to cache
        _save_ai_result(article_id, summary=summary)
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

    # Fetch ALL articles from today
    import datetime as _dt
    today_str = _dt.datetime.now().strftime("%Y-%m-%d")
    articles = _fetch_articles_by_date(today_str)
    if not articles:
        return jsonify({"error": "no articles today"}), 404

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


_daily_summary_sent = set()  # {(user_id, date), ...}


def _send_daily_summaries():
    """Check all users' settings and send daily summary emails where due."""
    import json as _json
    from notifier import send_daily_summary_email
    from models import get_db as _get_settings_db
    import datetime as _dt

    now = _dt.datetime.now()
    now_hhmm = now.strftime("%H:%M")
    today_str = now.strftime("%Y-%m-%d")

    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        return

    try:
        db = _get_settings_db()
        rows = db.execute(
            "SELECT user_id, notification_config, daily_summary_enabled "
            "FROM user_settings WHERE daily_summary_enabled = 1"
        ).fetchall()
    except Exception:
        return

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

        if not to_email or scheduled_time != now_hhmm:
            continue

        uid = settings["user_id"]
        dedup_key = (uid, today_str)
        if dedup_key in _daily_summary_sent:
            continue

        articles = _fetch_articles_by_date(today_str)
        if not articles:
            continue

        from ai_service import AIService
        ai_config = _get_ai_config_for_user(uid)
        if not ai_config or not ai_config.get("enabled") or not ai_config.get("api_key"):
            continue

        try:
            svc = AIService(
                api_key=ai_config["api_key"],
                endpoint=ai_config["endpoint"],
                model=ai_config["model"],
                provider_type=ai_config.get("provider_type", "openai"),
            )
            summary = svc.daily_summary(articles)
            send_daily_summary_email(resend_api_key, to_email, summary, len(articles))
            _daily_summary_sent.add(dedup_key)
            print(f"[scheduler] Daily summary sent to {to_email} for {today_str}")
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


@app.route("/ai/daily-summary/send", methods=["POST"])
def ai_daily_summary_send():
    """Scheduled daily summary delivery. Also triggered by internal scheduler."""
    # Verify cron secret if set (optional — scheduler bypasses this)
    cron_secret = os.environ.get("CRON_SECRET", "")
    if cron_secret:
        provided = request.headers.get("X-Cron-Secret", "")
        if provided != cron_secret:
            return jsonify({"error": "unauthorized"}), 401

    _send_daily_summaries()
    return jsonify({"status": "ok", "checked_at": __import__("datetime").datetime.now().strftime("%H:%M")})


def _get_ai_config_for_user(user_id: int) -> dict | None:
    """Fetch a user's AI config for programmatic use."""
    from models import get_db as _db
    try:
        db = _db()
        row = db.execute(
            "SELECT endpoint, model, api_key, provider_type, enabled "
            "FROM ai_config WHERE user_id = ?", (user_id,)
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
        rows = conn.execute(
            "SELECT id, title, source, date, time FROM articles ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _fetch_articles_by_date(date_str: str) -> list[dict]:
    """Fetch all articles for a given date string (YYYY-MM-DD)."""
    import sqlite3
    if not os.path.exists(NEWS_DB):
        return []
    try:
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, source, date, time FROM articles WHERE date = ? ORDER BY timestamp ASC",
            (date_str,),
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
                updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
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
        row = conn.execute(
            "SELECT summary, translation FROM ai_results WHERE article_id = ?",
            (article_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _save_ai_result(article_id: int, summary: str | None = None,
                    translation: str | None = None):
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
            if translation is not None:
                sets.append("translation = ?")
                vals.append(translation)
            if sets:
                sets.append("updated_at = datetime('now')")
                vals.append(article_id)
                conn.execute(
                    f"UPDATE ai_results SET {', '.join(sets)} WHERE article_id = ?",
                    vals,
                )
        else:
            conn.execute(
                "INSERT INTO ai_results (article_id, summary, translation) VALUES (?, ?, ?)",
                (article_id, summary, translation),
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
    return jsonify(cached or {})


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
    _init_ai_results_table()
    import threading as _th
    _th.Thread(target=_daily_summary_loop, daemon=True).start()
    print("[scheduler] Daily summary background thread started")
    port = int(os.environ.get("WEB_PORT", 8082))
    print(f"[web] RayNews Web Server listening on {port}")
    app.run(host="127.0.0.1", port=port, debug=False)
