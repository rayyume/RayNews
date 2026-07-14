"""RayNews Web Server — auth, favorites, AI, settings via Flask."""

import os
import re
import sys
import json
import sqlite3
import threading
import time
import calendar
import uuid
import requests
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, g
from flask_cors import CORS

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import (
    get_db, create_user, get_user, get_user_by_email, get_user_by_username,
    update_user, delete_user, list_users, get_first_admin_email, count_users,
    count_active_users_since,
    verify_password,
    add_favorite, remove_favorite, get_favorites, get_all_favorite_article_ids, is_favorited,
    count_article_favorites,
    get_ai_config, set_ai_config, get_user_settings, set_user_settings,
    get_users_with_share_enabled,
    get_system_ai_config, set_system_ai_config,
    create_invitation_code, validate_invitation_code, use_invitation_code,
    list_pending_invitations, delete_invitation_code, delete_invitation_code_by_code,
)
from auth import init_auth, create_token, require_auth, require_role
from auth_validation import is_valid_email
from ai_service import AIService
from image_cache import enqueue_article_image_prefetch, unpin_article_images
from news_schema import ensure_deleted_articles_table
from source_categories import (
    CATEGORY_NAMES, CATEGORY_ORDER, cleanup_stale_source_categories,
    clamp_weighted, ensure_article_source_columns, ensure_article_sources,
    find_merge_target, merge_source, promote_user_source_settings,
    recent_titles_for_source, source_aliases_for_target, source_rows,
    update_source_category, extract_domains_from_html,
)

AUTO_SUMMARY_BATCH_LIMIT = int(os.environ.get("AUTO_SUMMARY_BATCH_LIMIT", "20"))
AUTO_SUMMARY_INTERVAL_SECONDS = int(os.environ.get("AUTO_SUMMARY_INTERVAL_SECONDS", "30"))
AUTO_TRANSLATION_BATCH_LIMIT = int(os.environ.get("AUTO_TRANSLATION_BATCH_LIMIT", "5"))
AUTO_TRANSLATION_INTERVAL_SECONDS = int(os.environ.get("AUTO_TRANSLATION_INTERVAL_SECONDS", "30"))
AUTO_TITLE_PROCESS_BATCH_LIMIT = int(os.environ.get("AUTO_TITLE_PROCESS_BATCH_LIMIT", "20"))
AUTO_TITLE_PROCESS_INTERVAL_SECONDS = int(os.environ.get("AUTO_TITLE_PROCESS_INTERVAL_SECONDS", "10"))
AUTO_TITLE_PROCESS_SCAN_LIMIT = int(os.environ.get("AUTO_TITLE_PROCESS_SCAN_LIMIT", "1000"))
AUTO_SOURCE_CLASSIFY_BATCH_LIMIT = int(os.environ.get("AUTO_SOURCE_CLASSIFY_BATCH_LIMIT", "50"))
AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS = int(os.environ.get("AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS", "60"))
AI_SHARE_REVALIDATION_INTERVAL_SECONDS = int(
    os.environ.get("AI_SHARE_REVALIDATION_INTERVAL_HOURS", "6")
) * 3600
TELEGRAM_EMBED_TIMEOUT_SECONDS = int(os.environ.get("TELEGRAM_EMBED_TIMEOUT_SECONDS", "12"))
# Daily summary is now server-generated once and broadcast to every subscribed
# user — the send time is a fixed ops-level setting, not user-configurable.
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "21"))
DAILY_SUMMARY_MINUTE = int(os.environ.get("DAILY_SUMMARY_MINUTE", "0"))
DAILY_SUMMARY_WINDOW_MINUTES = int(os.environ.get("DAILY_SUMMARY_WINDOW_MINUTES", "10"))
TITLE_SUMMARY_MAX_CHARS = int(os.environ.get("TITLE_SUMMARY_MAX_CHARS", "30"))
TITLE_SUMMARY_MAX_WEIGHT = TITLE_SUMMARY_MAX_CHARS * 2
# The list UI already CSS-clamps titles to 2 lines with an ellipsis
# (frontend .item-title: -webkit-line-clamp:2), so there's no display reason
# to hard-reject a summary just for running over TITLE_SUMMARY_MAX_CHARS —
# doing so only forced AI/regex gymnastics (dropped details, brittle
# punctuation-splitting fallbacks) to hit an arbitrary target. This backstop
# instead only catches genuinely broken output (e.g. the AI ignoring the
# "shorten" instruction and returning most of the article).
TITLE_SUMMARY_MAX_WEIGHT_HARD = TITLE_SUMMARY_MAX_WEIGHT * 3
# Aspirational lower bound communicated to the LLM in the shortening prompt.
TITLE_SUMMARY_PROMPT_MIN_CHARS = int(os.environ.get("TITLE_SUMMARY_PROMPT_MIN_CHARS", "18"))
# Hard floor enforced by the validator. Kept well below the prompt's target so
# legitimately short titles aren't rejected; the real defense against
# attribution-only junk (e.g. "据FT报道") is _shares_title_signal below.
TITLE_SUMMARY_MIN_CHARS = int(os.environ.get("TITLE_SUMMARY_MIN_CHARS", "6"))
TITLE_SUMMARY_MIN_WEIGHT = TITLE_SUMMARY_MIN_CHARS * 2
TITLE_SUMMARY_MAX_TOTAL_CHARS = int(os.environ.get("TITLE_SUMMARY_MAX_TOTAL_CHARS", "40"))
# Only *trigger* the extra AI shortening pass once a title runs meaningfully
# past the target length — not the moment it crosses it by a character or two.
# The target (TITLE_SUMMARY_MAX_CHARS) is still what the prompt asks the model
# to aim for; this ratio just avoids spending an AI call to trim a 31-char
# title down to 30 (the list UI CSS-clamps to 2 lines anyway).
TITLE_SUMMARY_TRIGGER_RATIO = float(os.environ.get("TITLE_SUMMARY_TRIGGER_RATIO", "1.3"))
TITLE_SUMMARY_TRIGGER_CJK = int(TITLE_SUMMARY_MAX_CHARS * TITLE_SUMMARY_TRIGGER_RATIO)
TITLE_SUMMARY_TRIGGER_TOTAL = int(TITLE_SUMMARY_MAX_TOTAL_CHARS * TITLE_SUMMARY_TRIGGER_RATIO)
# When a title is BOTH foreign and over-long, translate+shorten it in a single
# AI call rather than translate → notice it's still long → summarize (two calls,
# title visibly changes twice). Set to "0" to fall back to the two-step path.
TITLE_MERGE_TRANSLATE_CONDENSE = os.environ.get("TITLE_MERGE_TRANSLATE_CONDENSE", "1") == "1"

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


def _admin_email_address() -> str:
    return (os.environ.get("RAYNEWS_ADMIN_EMAIL") or get_first_admin_email() or "").strip()


def _send_registration_notice(user: dict) -> bool:
    """Notify the admin after a user successfully registers. Never blocks registration."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    admin_email = _admin_email_address()
    if not api_key or not admin_email:
        return False
    from_email = os.environ.get("RAYNEWS_FROM_EMAIL") or "onboarding@resend.dev"
    nickname = user.get("nickname") or "未设置"
    try:
        from notifier import send_email
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0a0a0c;color:#e8e8ed;padding:20px;max-width:560px;margin:0 auto}}
h1{{color:#6e8efb;font-size:18px}}
.box{{background:#111114;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:16px;margin:16px 0}}
.row{{margin:8px 0;color:#c9c9d4}}
.label{{display:inline-block;width:80px;color:#8b8b9e}}
.footer{{font-size:12px;color:#55556a;margin-top:20px}}
</style></head><body>
<h1>RayNews 新用户已注册</h1>
<div class="box">
  <div class="row"><span class="label">邮箱</span>{user.get("email", "")}</div>
  <div class="row"><span class="label">昵称</span>{nickname}</div>
  <div class="row"><span class="label">角色</span>{user.get("role", "")}</div>
  <div class="row"><span class="label">时间</span>{user.get("created_at", "")}</div>
</div>
<p class="footer">此邮件仅用于管理员获知注册结果，不包含密码、验证码或令牌。</p>
</body></html>"""
        send_email(
            api_key,
            admin_email,
            f"RayNews 新用户注册 — {user.get('email', '')}",
            html,
            from_name="RayNews",
            from_email=from_email,
        )
        return True
    except Exception as exc:
        print(f"[web] Failed to send registration notice: {exc}")
        return False


# ─── Auth Routes ──────────────────────────────────────────────

@app.route("/auth/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    nickname = (data.get("nickname") or "").strip()
    invite_code = (data.get("invite_code") or "").strip().upper()

    if not email or not password or not nickname:
        return jsonify({"error": "email, username and password required"}), 400
    if not is_valid_email(email):
        return jsonify({"error": "invalid email format"}), 400
    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400

    # First user (admin) doesn't need invite code; all others do
    if count_users() > 0:
        if not invite_code:
            return jsonify({"error": "invitation code required. Go to Settings → Request Invite"}), 400
        if not validate_invitation_code(invite_code, email):
            return jsonify({"error": "invalid or expired invitation code"}), 400

    # Invite code already gates who can register at all, so a successful
    # registration is the approval step — no separate preview/pending state.
    role = "admin" if count_users() == 0 else "user"

    user = create_user(email, password, nickname, role)
    if user is None:
        if nickname and get_user_by_username(nickname):
            return jsonify({"error": "username already taken"}), 409
        return jsonify({"error": "email already registered"}), 409

    # Mark invite code as used
    if invite_code:
        use_invitation_code(invite_code)

    admin_notified = _send_registration_notice(user) if role != "admin" else False

    token = create_token(user["id"], user["role"])
    return jsonify({"token": token, "user": user, "admin_notified": admin_notified}), 201


@app.route("/auth/request-invite", methods=["POST"])
def request_invite():
    """Generate an 8-char invitation code and email it to the admin."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    if not is_valid_email(email):
        return jsonify({"error": "invalid email format"}), 400

    # Check if already registered
    if get_user_by_email(email):
        return jsonify({"error": "email already registered"}), 409

    # Verify the email service is actually usable *before* persisting a code,
    # so a config problem never leaves a valid-but-undelivered invitation
    # sitting in the pending-review list.
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return jsonify({"error": "email service not configured. Contact admin."}), 500
    admin_email = (os.environ.get("RAYNEWS_ADMIN_EMAIL") or get_first_admin_email() or "").strip()
    if not admin_email:
        return jsonify({"error": "admin email is not configured."}), 500
    from_email = os.environ.get("RAYNEWS_FROM_EMAIL") or "onboarding@resend.dev"

    # Generate code
    code = create_invitation_code(email)

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
        send_email(api_key, admin_email,
                   f"RayNews 新用户邀请 — {email}",
                   html, from_name="RayNews", from_email=from_email)
    except Exception as e:
        print(f"[web] Failed to send invite email: {e}")
        delete_invitation_code_by_code(code)
        return jsonify({"error": f"邮件发送失败，请稍后重试：{e}"}), 500

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
@require_role("user", "admin")
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
@require_role("user", "admin")
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
    return jsonify({
        "users": users,
        "total": len(users),
        "active_7d": count_active_users_since(7),
    })


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
    if new_role not in ("user", "admin"):
        return jsonify({"error": "invalid role"}), 400
    if user_id == g.user_id:
        return jsonify({"error": "cannot change your own role"}), 400
    user = update_user(user_id, role=new_role)
    return jsonify(user) if user else (jsonify({"error": "not found"}), 404)


@app.route("/auth/pending-invitations", methods=["GET"])
@require_role("admin")
def admin_list_pending_invitations():
    """Invite-code requests that haven't been used to complete registration yet."""
    pending = list_pending_invitations()
    return jsonify({"invitations": pending, "total": len(pending)})


@app.route("/auth/pending-invitations/<int:invitation_id>", methods=["DELETE"])
@require_role("admin")
def admin_revoke_pending_invitation(invitation_id):
    ok = delete_invitation_code(invitation_id)
    if ok:
        return jsonify({"ok": True}), 200
    return jsonify({"error": "not found"}), 404


@app.route("/admin/system-ai-config", methods=["GET"])
@require_role("admin")
def admin_get_system_ai_config():
    config = get_system_ai_config()
    safe = dict(config)
    safe["has_api_key"] = bool(safe.get("api_key"))
    safe.pop("api_key", None)
    return jsonify(safe)


@app.route("/admin/system-ai-config", methods=["PUT"])
@require_role("admin")
def admin_set_system_ai_config():
    try:
        data = request.get_json(silent=True) or {}
        api_key = data.get("api_key", "")
        if "****" in api_key:
            existing = get_system_ai_config()
            if existing.get("api_key"):
                data["api_key"] = existing["api_key"]
            else:
                data.pop("api_key", None)
        config = set_system_ai_config(**data)
        safe = dict(config)
        safe["has_api_key"] = bool(safe.get("api_key"))
        safe.pop("api_key", None)
        return jsonify(safe)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"server error: {str(e)}"}), 500


# ─── Favorites API ─────────────────────────────────────────

NEWS_DB = os.path.join(DATA_DIR, "news.db")


def _ensure_news_schema(conn: sqlite3.Connection) -> None:
    ensure_article_source_columns(conn)
    ensure_deleted_articles_table(conn)
    conn.commit()


def _get_article_meta(article_id: int) -> dict | None:
    """Fetch article title/source/date/thumb from news.db by id."""
    if not os.path.exists(NEWS_DB):
        return None
    try:
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        _ensure_news_schema(conn)
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
_news_conn_lock = threading.Lock()


def _get_news_db():
    """Persistent connection to news.db for batch queries."""
    global _news_conn
    if _news_conn is None and os.path.exists(NEWS_DB):
        with _news_conn_lock:
            if _news_conn is None and os.path.exists(NEWS_DB):
                _news_conn = sqlite3.connect(NEWS_DB, check_same_thread=False)
                _news_conn.row_factory = sqlite3.Row
                _ensure_news_schema(_news_conn)
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


def _fetch_article_images(article_id: int) -> dict | None:
    conn = _get_news_db()
    if not conn:
        return None
    try:
        row = conn.execute(
            "SELECT id, thumb, body_html FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def _pin_favorite_article_images(article_id: int):
    article = _fetch_article_images(article_id)
    if not article:
        return
    enqueue_article_image_prefetch(
        article_id,
        article.get("body_html"),
        article.get("thumb"),
        pinned=True,
    )


def _pin_favorite_articles_images(article_ids: list[int]):
    for article_id in article_ids:
        _pin_favorite_article_images(article_id)


def _pin_existing_favorite_images_on_startup():
    try:
        article_ids = get_all_favorite_article_ids()
        if not article_ids:
            return
        print(f"[image-cache] Pinning images for {len(article_ids)} existing favorite article(s)")
        _pin_favorite_articles_images(article_ids)
        print("[image-cache] Existing favorite image pinning finished")
    except Exception as exc:
        print(f"[image-cache] Existing favorite image pinning failed: {exc}")


import sqlite3


@app.route("/favorites", methods=["GET"])
@require_role("user", "admin")
def list_favorites():
    """List user's favorites with article metadata."""
    favs = get_favorites(g.user_id)
    article_ids = [f["article_id"] for f in favs]
    if article_ids:
        threading.Thread(target=_pin_favorite_articles_images, args=(article_ids,), daemon=True).start()
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
@require_role("user", "admin")
def add_favorite_route():
    data = request.get_json(silent=True) or {}
    article_id = data.get("article_id")
    if not article_id or not isinstance(article_id, int):
        return jsonify({"error": "article_id required (int)"}), 400
    fav = add_favorite(g.user_id, article_id)
    if fav is None:
        return jsonify({"error": "already favorited"}), 409
    threading.Thread(target=_pin_favorite_article_images, args=(article_id,), daemon=True).start()
    return jsonify({"ok": True, "favorite": fav}), 201


@app.route("/favorites/<int:article_id>", methods=["DELETE"])
@require_role("user", "admin")
def remove_favorite_route(article_id):
    ok = remove_favorite(g.user_id, article_id)
    if ok:
        if count_article_favorites(article_id) == 0:
            threading.Thread(target=unpin_article_images, args=(article_id,), daemon=True).start()
        return jsonify({"ok": True}), 200
    return jsonify({"error": "not found"}), 404


@app.route("/favorites/<int:article_id>/status", methods=["GET"])
@require_role("user", "admin")
def favorite_status(article_id):
    return jsonify({"favorited": is_favorited(g.user_id, article_id)})


# ─── AI Routes ─────────────────────────────────────────────

@app.route("/ai/config", methods=["GET"])
@require_role("user", "admin")
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
@require_role("user", "admin")
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


@app.route("/ai/config/client", methods=["GET"])
@require_role("user", "admin")
def get_ai_config_client_route():
    """Return the calling user's own AI config, including the plaintext api_key.

    Used by the browser to call the LLM provider directly for manual
    summarize/translate when the server has no shared cached result yet.
    Only ever returns the requesting user's own config (g.user_id).
    """
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
    return jsonify(dict(config))


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


@app.route("/ai/result/<int:article_id>", methods=["POST"])
@require_role("user", "admin")
def ai_save_result(article_id):
    """Save a browser-generated summary/translation into the shared cache.

    Manual summarize/translate now runs in the user's own browser against
    their own AI config (see GET /ai/config/client) instead of a server-side
    proxy call. Once the browser has a result, it POSTs it here so every
    other user can reuse it via GET /ai/result/<id> without regenerating.
    Body: {"summary": "..."} or {"translation": "..."} (translation is the
    same JSON-string-encoded {"title", "html"} shape used elsewhere).
    """
    article = _fetch_article_body(article_id)
    if not article:
        return jsonify({"error": "article not found"}), 404

    data = request.get_json(silent=True) or {}
    summary = data.get("summary")
    translation = data.get("translation")
    if not summary and not translation:
        return jsonify({"error": "summary or translation required"}), 400

    kwargs = {}
    if summary:
        kwargs["summary"] = summary
    if translation:
        kwargs["translation"] = translation
    _save_ai_result(article_id, **kwargs)

    # Keep the article title in news.db in sync so the home page list shows
    # the translated title too (mirrors the old server-side translate-full behavior).
    # Restricted to admins: this endpoint accepts any logged-in user's
    # self-reported translation JSON, and the home page title is shown to
    # every visitor — a regular user must not be able to rewrite it by simply
    # submitting a crafted payload. Non-admin submissions still populate the
    # shared summary/translation cache above; only the title-sync side effect
    # is gated. (Non-admin titles still get translated via the validated
    # admin-driven auto-title-process pipeline in _process_article_title.)
    if translation and g.user_role == "admin":
        try:
            translated_title = json.loads(translation).get("title") if translation.strip().startswith("{") else ""
        except (json.JSONDecodeError, TypeError):
            translated_title = ""
        if translated_title:
            _save_article_title_update(article_id, translated_title, "translation")

    return jsonify({"ok": True})


def _run_ai_connection_test(config: dict | None) -> tuple[dict, int]:
    """Shared connectivity test used by both the personal and system AI configs."""
    if not config or not config.get("api_key"):
        return {"error": "AI not configured. Save API config first."}, 400
    try:
        svc = AIService(
            api_key=config["api_key"],
            endpoint=config["endpoint"],
            model=config["model"],
            provider_type=config.get("provider_type", "openai"),
        )
        response = svc.test_connection()
        if not response or not response.strip():
            return {"error": "Connection test returned empty response"}, 502
        return {"ok": True, "response": response}, 200
    except requests.exceptions.ConnectTimeout:
        return {"error": "连接 AI 服务超时。请检查 API 地址是否正确，或 Docker 容器是否配置了 HTTP_PROXY 环境变量"}, 502
    except requests.exceptions.ConnectionError as e:
        return {"error": f"无法连接 AI 服务（{type(e).__name__}）。请检查网络代理配置：Docker 容器需要设置 HTTP_PROXY/HTTPS_PROXY 环境变量"}, 502
    except requests.exceptions.Timeout as e:
        return {"error": f"AI 服务响应超时: {e}"}, 502
    except Exception as e:
        return {"error": f"Connection test failed: {str(e)}"}, 502


@app.route("/ai/test-connection", methods=["POST"])
@require_role("user", "admin")
def ai_test_connection():
    """Test the user's own AI API configuration with a minimal prompt."""
    body, status = _run_ai_connection_test(get_ai_config(g.user_id))
    return jsonify(body), status


@app.route("/admin/system-ai-config/test", methods=["POST"])
@require_role("admin")
def admin_system_ai_test_connection():
    """Test the admin-configured system AI (drives background auto summary/translate)."""
    body, status = _run_ai_connection_test(get_system_ai_config())
    return jsonify(body), status


def _run_ai_share_revalidation_once():
    """Re-verify every opted-in user's own AI connectivity.

    A user's "共享 AI 结果" access is only granted after a live connectivity
    test at save time (see update_settings); this loop periodically re-tests
    it so a key that later expires/runs out of credit doesn't leave shared
    content permanently accessible. On failure, the master switch and all
    three view sub-toggles are cascaded off — same invariant enforced by
    update_settings (a sub-toggle must never be on while the master is off).
    """
    import datetime as _dt
    for user_id in get_users_with_share_enabled():
        config = get_ai_config(user_id)
        body, status = _run_ai_connection_test(config)
        now_str = _dt.datetime.now().isoformat(timespec="seconds")
        if status == 200:
            set_user_settings(
                user_id,
                share_last_check_at=now_str, share_last_check_ok=1, share_last_check_error=None,
            )
        else:
            set_user_settings(
                user_id,
                share_ai_results=0, share_view_title=0, share_view_translation=0, share_view_summary=0,
                share_last_check_at=now_str, share_last_check_ok=0,
                share_last_check_error=body.get("error", "connection test failed"),
            )
        time.sleep(0.5)  # spread requests out instead of bursting every provider at once


def _ai_share_revalidation_loop():
    time.sleep(60)
    while True:
        try:
            _run_ai_share_revalidation_once()
        except Exception as e:
            print(f"[share-revalidation] Error in loop: {e}")
        time.sleep(AI_SHARE_REVALIDATION_INTERVAL_SECONDS)


@app.route("/ai/daily-summary", methods=["POST"])
@require_role("user", "admin")
def ai_daily_summary():
    today_str = _today_str()

    # Reuse today's shared summary if it already exists (from the fixed
    # 21:00 broadcast or another user's request) instead of spending this
    # user's own key on a duplicate AI call.
    shared = _get_daily_summary_global_cache(today_str)
    if shared:
        _cleanup_daily_summary_jobs()
        job_id = uuid.uuid4().hex
        with _daily_summary_jobs_lock:
            _daily_summary_jobs[job_id] = {
                "job_id": job_id,
                "user_id": g.user_id,
                "date": today_str,
                "status": "completed",
                "created_at": time.time(),
                "updated_at": time.time(),
                "summary": shared["summary"],
                "article_count": shared["article_count"],
                "stats": shared["stats"],
                "error": "",
            }
        return jsonify({"job_id": job_id, "status": "completed"}), 202

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
            "date": today_str,
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
@require_role("user", "admin")
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

    shared = _get_daily_summary_global_cache(today_str)
    if shared:
        return jsonify({
            "status": "completed",
            "date": today_str,
            "summary": shared["summary"],
            "article_count": shared["article_count"],
            "stats": shared["stats"],
            "updated_at": shared["updated_at"],
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
@require_role("user", "admin")
def ai_daily_summary_status(job_id):
    with _daily_summary_jobs_lock:
        job = _daily_summary_jobs.get(job_id)
        if not job or job.get("user_id") != g.user_id:
            return jsonify({"error": "job not found"}), 404
        safe = {k: v for k, v in job.items() if k != "user_id"}
    return jsonify(safe)


_daily_summary_jobs = {}
_daily_summary_jobs_lock = threading.Lock()


def _beijing_now():
    """Explicit UTC+8 'now' — independent of the container's TZ env var
    (unlike SQLite's own datetime('now'), which is always UTC)."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))


def _today_str() -> str:
    return _beijing_now().strftime("%Y-%m-%d")


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


# ─── Shared (server-wide) daily summary ───────────────────────
# One summary per day, generated with the admin-configured system AI and
# broadcast by email to every user with daily_summary_enabled — replaces the
# old per-user generation so N subscribers no longer cost N AI calls.


def _init_daily_summary_global_table():
    if not os.path.exists(NEWS_DB):
        return
    try:
        conn = sqlite3.connect(NEWS_DB)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary_global (
                date          TEXT PRIMARY KEY,
                summary       TEXT NOT NULL,
                article_count INTEGER NOT NULL DEFAULT 0,
                stats         TEXT NOT NULL DEFAULT '{}',
                updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[daily-summary] global cache table init failed: {e}")


def _get_daily_summary_global_cache(date_str: str) -> dict | None:
    if not os.path.exists(NEWS_DB):
        return None
    try:
        _init_daily_summary_global_table()
        conn = sqlite3.connect(NEWS_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT summary, article_count, stats, updated_at "
            "FROM daily_summary_global WHERE date = ?",
            (date_str,),
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
        print(f"[daily-summary] global cache read failed: {e}")
        return None


def _save_daily_summary_global_cache(date_str: str, summary: str,
                                     article_count: int, stats: dict):
    if not os.path.exists(NEWS_DB):
        return
    try:
        _init_daily_summary_global_table()
        conn = sqlite3.connect(NEWS_DB)
        conn.execute(
            "INSERT INTO daily_summary_global "
            "(date, summary, article_count, stats, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(date) DO UPDATE SET "
            "summary = excluded.summary, "
            "article_count = excluded.article_count, "
            "stats = excluded.stats, "
            "updated_at = datetime('now')",
            (date_str, summary, article_count, json.dumps(stats or {}, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[daily-summary] global cache write failed: {e}")


def _generate_daily_summary_global(date_str: str) -> dict | None:
    """Return (generating if needed) the one shared daily summary for date_str,
    using the admin-configured system AI. Returns None if it can't be produced
    yet (no system AI configured, or no articles for that date)."""
    cached = _get_daily_summary_global_cache(date_str)
    if cached:
        return cached

    sys_config = get_system_ai_config()
    if not sys_config or not sys_config.get("enabled") or not sys_config.get("api_key"):
        print("[daily-summary] system AI not configured (管理员设置→服务端API), cannot generate")
        return None

    articles = _fetch_articles_by_date(date_str, include_shared_summary=True)
    if not articles:
        print(f"[daily-summary] no articles for {date_str}")
        return None

    try:
        svc = AIService(
            api_key=sys_config["api_key"],
            endpoint=sys_config["endpoint"],
            model=sys_config["model"],
            provider_type=sys_config.get("provider_type", "openai"),
        )
        raw_article_count = len(articles)
        deduped = _dedup_articles(articles)
        result = svc.daily_summary(deduped)
        result["stats"]["total_articles"] = raw_article_count
        result["stats"]["articles_after_dedup"] = len(deduped)
        _save_daily_summary_global_cache(date_str, result["summary"], raw_article_count, result["stats"])
        return {
            "summary": result["summary"],
            "article_count": raw_article_count,
            "stats": result["stats"],
        }
    except Exception as e:
        print(f"[daily-summary] global generation failed: {e}")
        return None


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
            include_shared_summary=True,
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


_auto_summary_lock = threading.Lock()
_auto_translation_lock = threading.Lock()
_auto_title_process_lock = threading.Lock()
_auto_source_classify_lock = threading.Lock()
_legacy_admin_source_settings_lock = threading.Lock()
_legacy_admin_source_settings_promoted = False


def _init_daily_summary_sends_table():
    if not os.path.exists(NEWS_DB):
        return
    try:
        conn = sqlite3.connect(NEWS_DB)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary_sends (
                date       TEXT NOT NULL,
                user_id    INTEGER NOT NULL,
                email      TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'sent',
                sent_at    TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (date, user_id)
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[daily-summary] sends table init failed: {e}")


def _get_daily_summary_sent_user_ids(date_str: str) -> set[int]:
    """user_ids already *successfully* sent this date's summary — failed
    attempts are excluded on purpose so the next run retries them."""
    if not os.path.exists(NEWS_DB):
        return set()
    try:
        _init_daily_summary_sends_table()
        conn = sqlite3.connect(NEWS_DB)
        rows = conn.execute(
            "SELECT user_id FROM daily_summary_sends WHERE date = ? AND status = 'sent'",
            (date_str,),
        ).fetchall()
        conn.close()
        return {int(r[0]) for r in rows}
    except Exception as e:
        print(f"[daily-summary] sends read failed: {e}")
        return set()


def _record_daily_summary_send(date_str: str, user_id: int, email: str, status: str):
    if not os.path.exists(NEWS_DB):
        return
    try:
        _init_daily_summary_sends_table()
        conn = sqlite3.connect(NEWS_DB)
        conn.execute(
            "INSERT INTO daily_summary_sends (date, user_id, email, status, sent_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(date, user_id) DO UPDATE SET "
            "email = excluded.email, status = excluded.status, sent_at = excluded.sent_at",
            (date_str, user_id, email, status),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[daily-summary] sends write failed: {e}")


def _broadcast_daily_summary(force: bool = False) -> dict:
    """Generate (once) and email the one shared daily summary to every user
    with daily_summary_enabled. Runs automatically at DAILY_SUMMARY_HOUR:MINUTE
    Beijing time; `force=True` (admin manual trigger) bypasses the time
    window and resends to every subscriber regardless of history.

    Send state is persisted in daily_summary_sends (date, user_id) rather than
    kept in memory: an in-memory "already sent today" set is lost on every
    restart, which could cause a duplicate broadcast if the process restarts
    inside the same day's send window. Persisting per-recipient status also
    lets a transient per-recipient failure (e.g. one bad email) get retried
    on the next scheduler tick instead of being silently skipped for the rest
    of the day.
    """
    import json as _json
    from notifier import send_daily_summary_email

    now = _beijing_now()
    today_str = now.strftime("%Y-%m-%d")

    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend_api_key:
        print("[scheduler] RESEND_API_KEY not set, skipping")
        return {"status": "skipped", "reason": "RESEND_API_KEY not set"}

    if not force:
        target_minutes = DAILY_SUMMARY_HOUR * 60 + DAILY_SUMMARY_MINUTE
        now_minutes = now.hour * 60 + now.minute
        diff = now_minutes - target_minutes
        if diff < 0 or diff >= DAILY_SUMMARY_WINDOW_MINUTES:
            return {"status": "skipped", "reason": "outside daily send window"}

    try:
        db = get_db()
        rows = db.execute(
            "SELECT user_id, notification_config FROM user_settings WHERE daily_summary_enabled = 1"
        ).fetchall()
    except Exception as e:
        print(f"[scheduler] DB error: {e}")
        return {"status": "error", "reason": str(e)}

    recipients = {}  # user_id -> to_email
    for row in rows:
        settings = dict(row)
        nc = settings.get("notification_config", "{}")
        if isinstance(nc, str):
            try:
                nc = _json.loads(nc)
            except (_json.JSONDecodeError, TypeError):
                nc = {}
        to_email = (nc.get("resend") or {}).get("to_email", "")
        if to_email:
            recipients[int(settings["user_id"])] = to_email

    print(f"[scheduler] Daily summary broadcast for {today_str}: {len(recipients)} subscriber(s)")
    if not recipients:
        return {"status": "skipped", "reason": "no subscribers"}

    already_sent = set() if force else _get_daily_summary_sent_user_ids(today_str)
    pending = {uid: email for uid, email in recipients.items() if uid not in already_sent}
    if not pending:
        return {"status": "skipped", "reason": "already sent today"}

    result = _generate_daily_summary_global(today_str)
    if not result:
        return {"status": "error", "reason": "generation failed (check 管理员设置→服务端API, or no articles yet)"}

    sent = 0
    for user_id, to_email in pending.items():
        try:
            # Idempotency key = one send per (date, user). If a prior tick's send
            # actually reached Resend but its response never got back to us (so it
            # was recorded "failed" and retried this tick), Resend replays the
            # original instead of delivering a duplicate.
            send_daily_summary_email(
                resend_api_key, to_email, result["summary"], result["stats"],
                idempotency_key=f"daily-{today_str}-{user_id}",
            )
            _record_daily_summary_send(today_str, user_id, to_email, "sent")
            sent += 1
        except Exception as e:
            print(f"[scheduler] send to user {user_id} ({to_email}) failed: {e}")
            _record_daily_summary_send(today_str, user_id, to_email, "failed")

    print(f"[scheduler] Daily summary broadcast for {today_str}: sent to {sent}/{len(pending)} pending "
          f"({len(recipients)} total subscriber(s))")
    return {"status": "ok", "sent": sent, "pending": len(pending), "subscribers": len(recipients), "date": today_str}


def _send_daily_summaries():
    _broadcast_daily_summary(force=False)


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


def _system_auto_config(*flag_columns: str) -> dict | None:
    """Build a single synthetic config for background auto jobs.

    Auto-summary/auto-translate/auto-title-process are admin-only: the flags
    still live on the triggering admin's user_settings row (so the admin UI
    stays simple), but the actual AI credentials always come from the
    admin-managed system_ai_config, never from any individual user's own key.
    Returns None unless some admin has one of the given flags on AND the
    system AI config is enabled with a key.
    """
    try:
        db = get_db()
        cond = " OR ".join(f"s.{col} = 1" for col in flag_columns)
        row = db.execute(
            "SELECT s.user_id, s.auto_translate_title, s.auto_translate_content, "
            "s.auto_title_summary_enabled, s.auto_summary_enabled "
            "FROM user_settings s JOIN users u ON u.id = s.user_id "
            f"WHERE u.role = 'admin' AND ({cond}) LIMIT 1"
        ).fetchone()
        if not row:
            return None
        sys_config = get_system_ai_config()
        if not sys_config or not sys_config.get("enabled") or not sys_config.get("api_key"):
            return None
        config = dict(row)
        config.update({
            "endpoint": sys_config["endpoint"],
            "model": sys_config["model"],
            "api_key": sys_config["api_key"],
            "provider_type": sys_config.get("provider_type", "openai"),
            "enabled": sys_config.get("enabled", 1),
        })
        return config
    except Exception as e:
        print(f"[auto-service] system config lookup error: {e}")
        return None


def _get_auto_summary_users() -> list[dict]:
    """Admin-enabled background article summarization, using the system AI config."""
    config = _system_auto_config("auto_summary_enabled")
    return [config] if config else []


def _get_auto_translation_users() -> list[dict]:
    """Admin-enabled background translation, using the system AI config."""
    config = _system_auto_config("auto_translate_title", "auto_translate_content")
    return [config] if config else []


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


def _title_weight(title: str) -> int:
    """Count Chinese-like characters as 2 and ASCII characters as 1."""
    return sum(1 if ord(ch) < 128 else 2 for ch in (title or "").strip())


TITLE_ERROR_BACKOFF_HOURS = float(os.environ.get("TITLE_ERROR_BACKOFF_HOURS", "6"))


def _within_error_backoff(error_at: str | None, hours: float = TITLE_ERROR_BACKOFF_HOURS) -> bool:
    """True if a recorded failure timestamp is recent enough that we should
    skip retrying for now. Shared by the title-translation and title-summary
    paths so a title that just failed isn't hammered every 10s at low temp.

    The stored timestamp comes from SQLite datetime('now'), which is UTC, so
    it's parsed with calendar.timegm (UTC), not time.mktime (local) — the
    latter silently skews the window by the host's UTC offset."""
    if not error_at:
        return False
    try:
        err_ts = calendar.timegm(time.strptime(error_at, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return False
    return (time.time() - err_ts) < hours * 3600


def _title_cjk_count(title: str) -> int:
    return len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", title or ""))


def _title_total_chars(title: str) -> int:
    return len(re.sub(r"\s+", "", title or ""))


def _needs_title_summary(title: str) -> bool:
    return (
        _title_cjk_count(title) > TITLE_SUMMARY_TRIGGER_CJK
        or _title_total_chars(title) > TITLE_SUMMARY_TRIGGER_TOTAL
    )


def _strip_outer_title_quotes(text: str) -> str:
    pairs = (
        ("\"", "\""), ("'", "'"),
        (chr(0x201c), chr(0x201d)), (chr(0x2018), chr(0x2019)),
        (chr(0x300c), chr(0x300d)), (chr(0x300e), chr(0x300f)),
    )
    changed = True
    while changed and len(text) >= 2:
        changed = False
        for left, right in pairs:
            if text.startswith(left) and text.endswith(right):
                text = text[len(left):-len(right)].strip()
                changed = True
                break
    return text


def _remove_title_summary_prefix(text: str) -> str:
    prefixes = (
        "\u4ee5\u4e0b\u662f\u7b80\u5199\u540e\u7684\u6807\u9898",
        "\u4ee5\u4e0b\u662f\u538b\u7f29\u540e\u7684\u6807\u9898",
        "\u4ee5\u4e0b\u662f\u4f18\u5316\u540e\u7684\u6807\u9898",
        "\u6211\u4e3a\u4f60\u7b80\u5199\u540e\u7684\u6807\u9898",
        "\u6211\u4e3a\u4f60\u538b\u7f29\u540e\u7684\u6807\u9898",
        "\u6211\u4e3a\u4f60\u4f18\u5316\u540e\u7684\u6807\u9898",
        "\u7b80\u5199\u540e\u7684\u6807\u9898",
        "\u538b\u7f29\u540e\u7684\u6807\u9898",
        "\u4f18\u5316\u540e\u7684\u6807\u9898",
        "\u7b80\u5199\u6807\u9898",
        "\u77ed\u6807\u9898",
        "\u603b\u7ed3\u6807\u9898",
        "\u6807\u9898",
    )
    stripped = text.lstrip()
    for prefix in prefixes:
        if stripped.lower().startswith(prefix.lower()):
            rest = stripped[len(prefix):].lstrip()
            if rest.startswith((":", chr(0xff1a))):
                return rest[1:].lstrip()
    return text


def _normalize_cjk_quotes(text: str) -> str:
    """Convert ASCII straight quotes and Unicode curly quotes to Chinese corner brackets 「」 in CJK context."""
    # Paired double quotes — ASCII and Unicode curly
    text = re.sub(r'"([^"]+)"', r'「\1」', text)
    text = re.sub('\u201c([^\u201d]+)\u201d', r'「\1」', text)
    # Paired single quotes — ASCII and Unicode curly (only in CJK context)
    text = re.sub(
        r"'([^']+)'",
        lambda m: f'「{m.group(1)}」'
        if re.search(r'[\u4e00-\u9fff]', m.group(1))
        else m.group(0),
        text,
    )
    text = re.sub(
        '\u2018([^\u2019]+)\u2019',
        lambda m: f'「{m.group(1)}」'
        if re.search(r'[\u4e00-\u9fff]', m.group(1))
        else m.group(0),
        text,
    )
    return text


def _clean_title_summary(title: str | None) -> str:
    text = " ".join((title or "").strip().split())
    text = _remove_title_summary_prefix(text)
    text = re.sub(r"^\s*(?:[-*]\s+|\d{1,2}[.\u3001]\s+)", "", text)
    quote_chars = " \t\r\n" + "".join(chr(cp) for cp in (0x201c, 0x201d, 0x2018, 0x2019, 0x300c, 0x300d, 0x300e, 0x300f))
    # Only strip quote chars when both ends have them (prevent asymmetric tearing:
    # e.g. 飞行员「患有焦虑症」 should not become 飞行员「患有焦虑症)
    if text and text[0] in quote_chars and text[-1] in quote_chars:
        text = text.strip(quote_chars)
    text = _strip_outer_title_quotes(text).strip()
    text = _normalize_cjk_quotes(text)
    return text


_AI_REFUSAL_MARKERS = (
    "\u4ee5\u4e0b\u662f", "\u65e0\u6cd5", "\u4e0d\u80fd",
    "\u8bf7\u63d0\u4f9b", "\u8bf7\u8865\u5145", "as an ai", "i cannot", "i'm unable",
)


def _is_valid_title_summary(title: str | None) -> bool:
    text = _clean_title_summary(title)
    if not text:
        return False
    lowered = text.lower()
    bad_markers = _AI_REFUSAL_MARKERS + ("\u7b80\u5199\u540e\u7684",)
    if any(marker in lowered for marker in bad_markers):
        return False
    if chr(0x2026) in text or "..." in text:
        return False
    if text.startswith("{"):
        return False
    weight = _title_weight(text)
    return TITLE_SUMMARY_MIN_WEIGHT <= weight <= TITLE_SUMMARY_MAX_WEIGHT_HARD


def _parse_title_summary_result(raw: str | None) -> dict:
    text = (raw or "").strip()
    if not text:
        return {"title": "", "valid": False, "reason": "empty AI title summary"}
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match:
        try:
            data = json.loads(match.group(0))
            return {
                "title": data.get("title") or "",
                "valid": bool(data.get("valid")),
                "reason": data.get("reason") or "",
            }
        except (json.JSONDecodeError, TypeError):
            pass
    return {"title": text, "valid": True, "reason": "legacy plain title summary"}


def _balanced_title_punctuation(title: str) -> bool:
    pairs = (
        # ASCII/西文
        ("(", ")"), ("[", "]"), ("{", "}"),
        # 全角 / CJK
        ("\uff08", "\uff09"),   # （）
        ("\uff3b", "\uff3d"),   # ［］
        ("\uff5b", "\uff5d"),   # ｛｝
        # 中文专用
        ("\u300a", "\u300b"),   # 《》
        ("\u300c", "\u300d"),   # 「」
        ("\u300e", "\u300f"),   # 『』
        ("\u3010", "\u3011"),   # 【】
        ("\u3014", "\u3015"),   # 〔〕
        ("\u3016", "\u3017"),   # 〖〗
        ("\u3008", "\u3009"),   # 〈〉
        # 弯引号
        ("\u201c", "\u201d"),   # ""
        ("\u2018", "\u2019"),   # ''
    )
    for left, right in pairs:
        if title.count(left) != title.count(right):
            return False
        if title.find(right) != -1 and (title.find(left) == -1 or title.find(right) < title.find(left)):
            return False
    return True


def _looks_like_code_only_title(title: str) -> bool:
    compact = re.sub(r"[\s\-_.:\u3001\uff1a/]+", "", title or "")
    if not compact:
        return True
    if re.fullmatch(r"[\dA-Za-z]+", compact):
        digits = len(re.findall(r"\d", compact))
        letters = len(re.findall(r"[A-Za-z]", compact))
        return digits >= 3 or (digits > 0 and letters <= 4)
    if re.fullmatch(r"[\dA-Za-z.\-_/]+", title or ""):
        return True
    return False


# Attribution/sourcing words and wire-service names. These are excluded when
# checking whether a shortened title still carries the original's subject \u2014
# a title that only echoes "who reported it" (e.g. "\u636eFT\u62a5\u9053") without any of
# the actual subject/action tokens must not be treated as on-topic.
TITLE_ATTRIBUTION_STOPWORDS = {
    "\u62a5\u9053", "\u636e\u62a5\u9053", "\u636e\u6089", "\u6d88\u606f", "\u6d88\u606f\u4eba\u58eb", "\u63f4\u5f15", "\u77e5\u60c5\u4eba\u58eb", "\u62a5\u9053\u79f0", "\u62a5\u9053\u8bf4",
    "reuters", "bloomberg", "afp", "ap", "cnn", "bbc", "wsj", "ft",
    "\u8def\u900f", "\u5f6d\u535a", "\u7f8e\u8054\u793e", "\u6cd5\u65b0\u793e", "\u534e\u5c14\u8857\u65e5\u62a5", "\u91d1\u878d\u65f6\u62a5", "\u7ebd\u7ea6\u65f6\u62a5", "nyt",
    "sources", "reports", "reported", "reporting", "said", "according", "says",
}


def _title_keyword_tokens(title: str) -> set[str]:
    text = _clean_title_summary(title)
    cjk_tokens = set(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}", text))
    latin_tokens = {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9.+#-]{1,}", text)}
    return cjk_tokens | latin_tokens


def _shares_title_signal(candidate: str, original_title: str) -> bool:
    original_tokens = _title_keyword_tokens(original_title) - TITLE_ATTRIBUTION_STOPWORDS
    if not original_tokens:
        return True
    candidate_tokens = _title_keyword_tokens(candidate) - TITLE_ATTRIBUTION_STOPWORDS
    if not candidate_tokens:
        return False
    if candidate_tokens & original_tokens:
        return True
    candidate_text = _clean_title_summary(candidate).lower()
    original_text = _clean_title_summary(original_title).lower()
    for token in original_tokens:
        if token.lower() in candidate_text:
            return True
    for token in candidate_tokens:
        if token.lower() in original_text:
            return True
    return False


def _validate_ai_title_summary_result(result: dict | str | None, original_title: str = "") -> dict:
    """Hard-reject only when the output structurally can't be a title summary
    at all (AI says so itself, or it doesn't fit the length/format contract
    the whole "shorten the title" feature exists to deliver). Everything else
    below (code-only text, unbalanced punctuation, missing keyword/number
    overlap with the original) is a probabilistic clue, not proof the result
    is wrong — a false positive on any one of them used to discard an
    otherwise-fine title forever, since the same input reliably re-triggers
    the same rule on every retry. Those are logged but kept instead: a
    slightly-off title beats no title. (_repair_title_summary, upstream,
    checks bracket/quote balance itself before picking a fallback clause, so
    a mid-bracket punctuation warning here should be rare in practice.)
    """
    data = _parse_title_summary_result(result) if isinstance(result, str) or result is None else dict(result)
    title = _repair_title_summary(data.get("title") or "")
    reason = data.get("reason") or ""
    if data.get("valid") is False:
        return {"title": title, "valid": False, "reason": reason or "AI marked title summary invalid"}
    if not _is_valid_title_summary(title):
        return {"title": title, "valid": False, "reason": "invalid title summary length or format"}
    warnings = []
    if _looks_like_code_only_title(title):
        warnings.append("code-only or numeric title")
    if not _balanced_title_punctuation(title):
        warnings.append("unbalanced punctuation")
    if original_title:
        # _shares_title_signal is a literal-keyword-overlap check that only
        # makes sense when both titles are in the same language (same-language
        # shortening). When the "summary" step is actually also translating
        # (original_title is still untranslated English), fall back to the
        # cross-language-safe numeric check instead — see _numbers_match.
        if _needs_translation(original_title):
            if not _numbers_match(original_title, title):
                warnings.append("missing numbers from original title")
        elif not _shares_title_signal(title, original_title):
            warnings.append("missing original title signal")
    if warnings:
        print(f"[auto-title] title summary kept despite soft warning(s) "
              f"({'; '.join(warnings)}): {title[:120]!r}")
    return {"title": title, "valid": True, "reason": reason}


def _title_numeric_values(text: str) -> set[float]:
    """Numbers in a title, including Chinese 万/亿-scaled numerals (e.g. "1万"
    -> 10000.0), normalized to their numeric value. Unlike words, a number's
    *value* should survive a correct translation even though its literal
    digits/formatting often don't (English "10,000" is routinely rendered as
    Chinese "1万"), so this is a safer cross-language sanity check than
    literal-token overlap (see _shares_title_signal).
    """
    text = text or ""
    values: set[float] = set()
    for m in re.finditer(r"\d[\d,]*\.?\d*", text):
        try:
            values.add(float(m.group(0).replace(",", "")))
        except ValueError:
            pass
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([万亿])", text):
        try:
            values.add(float(m.group(1)) * (10000 if m.group(2) == "万" else 100000000))
        except ValueError:
            pass
    return values


def _numbers_match(original_title: str, candidate: str) -> bool:
    original_values = _title_numeric_values(original_title)
    if not original_values:
        return True
    return bool(original_values & _title_numeric_values(candidate))


def _validate_title_translation(translated: str | None, original_title: str) -> dict:
    """Sanity-check a translated title before it's saved as the site-wide title.

    Hard-rejects only truly unusable output: empty, an AI refusal/explanation
    instead of a translation, or an unparsed JSON payload — cases where
    there's no plausible title to fall back on at all. Everything else below
    (ellipsis/truncation look, code-only text, unbalanced punctuation, being
    shorter than expected, missing numbers from the original — see
    _numbers_match) is a probabilistic heuristic, not proof of a bad
    translation, and discarding on those used to permanently strand titles
    that legitimately tripped one rule or another on every retry (the same
    input reliably re-triggers the same rule, so it never self-heals). Those
    are logged as a warning but the translation is kept: a slightly-off
    translation is still more useful to readers than none at all.
    """
    title = _clean_title_summary(translated)
    if not title:
        return {"title": title, "valid": False, "reason": "empty translation"}
    if any(marker in title.lower() for marker in _AI_REFUSAL_MARKERS):
        return {"title": title, "valid": False, "reason": "AI refusal or explanation, not a translation"}
    if title.startswith("{"):
        return {"title": title, "valid": False, "reason": "unparsed JSON payload"}
    warnings = []
    if chr(0x2026) in title or "..." in title:
        warnings.append("looks truncated (ellipsis)")
    if _looks_like_code_only_title(title):
        warnings.append("code-only or numeric title")
    if not _balanced_title_punctuation(title):
        warnings.append("unbalanced punctuation")
    if _title_weight(title) < TITLE_SUMMARY_MIN_WEIGHT:
        warnings.append("shorter than expected")
    if not _numbers_match(original_title, title):
        warnings.append("missing numbers from original title")
    if warnings:
        print(f"[auto-title] translation kept despite soft warning(s) "
              f"({'; '.join(warnings)}): {title[:120]!r}")
    return {"title": title, "valid": True, "reason": ""}


def _clip_title_by_weight(title: str, max_weight: int = TITLE_SUMMARY_MAX_WEIGHT) -> str:
    title = _clean_title_summary(title)
    if _title_weight(title) <= max_weight:
        return title
    out = []
    weight = 0
    for ch in title:
        ch_weight = 1 if ord(ch) < 128 else 2
        if weight + ch_weight > max_weight:
            break
        out.append(ch)
        weight += ch_weight
    clipped = "".join(out).rstrip(" ，,、：:；;。.!！?？-_/")
    if " " in clipped and re.search(r"[A-Za-z0-9]$", clipped):
        clipped = clipped.rsplit(" ", 1)[0].rstrip(" ，,、：:；;。.!！?？-_/") or clipped
    return clipped


def _repair_title_summary(title: str | None) -> str:
    """Turn a slightly non-compliant AI title into a valid short title when safe.

    The sentence/clause splits below are punctuation-based and don't know
    about bracket/quote pairs — a naturally-written title can have a comma or
    colon nested inside a 「」/《》 quote (e.g. "「甲：从乙到丙」是丁"), and
    slicing there leaves one candidate with a dangling open bracket. Such a
    candidate can still be short enough to pass _is_valid_title_summary's
    length check, so it must also pass _balanced_title_punctuation before
    being accepted — otherwise a fragment like "「甲" gets picked as the
    "repaired" title instead of a genuinely complete clause.
    """
    text = _clean_title_summary(title)
    if not text:
        return ""
    text = text.replace("……", " ").replace("...", " ").replace("…", " ")
    text = " ".join(text.split())
    if _is_valid_title_summary(text) and _balanced_title_punctuation(text):
        return text

    sentence_parts = [p.strip() for p in re.split(r"[。！？!?；;]\s*", text) if p.strip()]
    for part in sentence_parts:
        part = _clean_title_summary(part)
        if _is_valid_title_summary(part) and _balanced_title_punctuation(part):
            return part

    clause_source = sentence_parts[0] if sentence_parts else text
    clause_parts = [p.strip() for p in re.split(r"[，,、：:]\s*", clause_source) if p.strip()]
    for part in clause_parts:
        part = _clean_title_summary(part)
        if _is_valid_title_summary(part) and _balanced_title_punctuation(part):
            return part

    return _clip_title_by_weight(clause_source)


def _ensure_article_title_columns(conn) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "original_title" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN original_title TEXT")
    if "title_updated_at" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN title_updated_at TEXT")
    if "title_source" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN title_source TEXT")


def _invalidate_refresh_server_cache(article_id: int) -> None:
    """Best-effort: tell refresh_server.py to drop its in-memory cached
    /api/news/<id> response for this article, so the next read reflects the
    title/body update we just wrote to news.db instead of stale cached JSON.
    refresh_server is a separate process with no other way to learn about
    writes made here; without this, staleness only self-heals on the next
    ~15min fetcher cycle (which clears the whole cache) or when some client
    happens to poll /api/news/title-updates (which only handles title
    changes, not body_html).
    """
    try:
        requests.get(
            "http://127.0.0.1:8081/internal/cache-evict",
            params={"id": article_id},
            timeout=3,
        )
    except requests.exceptions.RequestException:
        pass  # non-fatal — cache will self-heal on the next fetcher cycle


def _save_article_title_update(article_id: int, title: str | None,
                               title_source: str = "ai") -> bool:
    title = _clean_title_summary(_sanitize_plain_text(title or "", max_len=300))
    if not title or not os.path.exists(NEWS_DB):
        return False
    conn = sqlite3.connect(NEWS_DB, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_article_title_columns(conn)
        row = conn.execute("SELECT title FROM articles WHERE id = ?", (article_id,)).fetchone()
        if not row:
            return False
        current_title = row[0] or ""
        if current_title == title:
            return False
        conn.execute(
            "UPDATE articles SET "
            "original_title = CASE WHEN original_title IS NULL OR original_title = '' THEN title ELSE original_title END, "
            "title = ?, title_updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now'), title_source = ? "
            "WHERE id = ?",
            (title, title_source, article_id),
        )
        conn.commit()
        _invalidate_refresh_server_cache(article_id)
        return True
    finally:
        conn.close()


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
        _save_article_title_update(article_id, title, "translation")
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
        # _save_article_title_update() above already invalidates on a title
        # change; body_html has no such path, so cover it here too.
        _invalidate_refresh_server_cache(article_id)
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


def _get_auto_title_process_users() -> list[dict]:
    """Admin-enabled title translation/shortening, using the system AI config."""
    config = _system_auto_config("auto_translate_title", "auto_title_summary_enabled")
    return [config] if config else []


def _fetch_title_process_articles(config: dict, limit: int = AUTO_TITLE_PROCESS_BATCH_LIMIT) -> list[dict]:
    """Fetch today's articles needing title translation or AI title shortening."""
    import datetime as _dt
    if not os.path.exists(NEWS_DB):
        return []
    translate_title = bool(config.get("auto_translate_title"))
    summarize_title = bool(config.get("auto_title_summary_enabled"))
    if not translate_title and not summarize_title:
        return []
    conn = None
    try:
        _init_ai_results_table()
        today_str = _dt.datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(NEWS_DB, timeout=30)
        conn.row_factory = sqlite3.Row
        ensure_article_source_columns(conn)
        _ensure_article_title_columns(conn)
        conn.commit()
        rows = conn.execute(
            "SELECT a.id, a.title, a.original_title, a.title_source, a.title_updated_at, "
            "       r.title_summary, r.title_summary_error, r.title_summary_error_at, "
            "       r.title_translation_error_at "
            "FROM articles a "
            "LEFT JOIN ai_results r ON r.article_id = a.id "
            "WHERE a.date = ? "
            "ORDER BY a.timestamp DESC LIMIT ?",
            (today_str, max(limit * 10, AUTO_TITLE_PROCESS_SCAN_LIMIT)),
        ).fetchall()
    except Exception as e:
        print(f"[auto-title] fetch failed: {e}")
        return []
    finally:
        if conn:
            conn.close()

    selected = []
    for row in rows:
        article = dict(row)
        title = article.get("title") or ""
        cached_summary = _clean_title_summary(article.get("title_summary"))
        translate_needed = translate_title and _needs_translation(title)
        summary_needed = summarize_title and (
            (cached_summary and title != cached_summary)
            or (not cached_summary and _needs_title_summary(title))
        )
        if not translate_needed and not summary_needed:
            continue
        # Back off recently-failed translations so a title that reliably fails
        # validation isn't re-attempted every 10s at low temperature.
        if translate_needed and _within_error_backoff(article.get("title_translation_error_at")):
            translate_needed = False
        previous_error = (article.get("title_summary_error") or "").lower()
        retryable_invalid_output = "invalid title summary" in previous_error
        if (summary_needed and not cached_summary and not retryable_invalid_output
                and _within_error_backoff(article.get("title_summary_error_at"))):
            summary_needed = False
        if translate_needed or summary_needed:
            article["translate_title_needed"] = translate_needed
            article["title_summary_needed"] = summary_needed
            selected.append(article)
        if len(selected) >= limit:
            break
    return selected


def _title_service(config: dict) -> AIService:
    return AIService(
        api_key=config["api_key"],
        endpoint=config["endpoint"],
        model=config["model"],
        provider_type=config.get("provider_type", "openai"),
    )


# On a rejected result we re-ask once with the failure reason as feedback and
# a higher temperature — a low-temp model tends to re-emit the same bad output
# verbatim, so a plain retry is wasted. One corrective retry is enough; beyond
# that the article backs off (error timestamp) instead of hammering every 10s.
TITLE_RETRY_TEMPERATURE = float(os.environ.get("TITLE_RETRY_TEMPERATURE", "0.5"))


def _translate_title_with_retry(svc: "AIService", title: str) -> dict:
    """Translate `title`, and if validation rejects the output, re-ask once
    with the failure reason as feedback. Returns the _validate_title_translation
    dict of whichever attempt succeeded, else the last failure."""
    raw = svc.translate_title(title, "zh-CN")
    result = _validate_title_translation(raw, title)
    if result["valid"]:
        return result
    raw2 = svc.translate_title(
        title, "zh-CN",
        feedback=result.get("reason") or "输出不合规",
        temperature=TITLE_RETRY_TEMPERATURE,
    )
    return _validate_title_translation(raw2, title)


def _summarize_title_with_retry(svc: "AIService", title: str) -> dict:
    """Shorten `title`, re-asking once with feedback if the first result is
    rejected. Returns the _validate_ai_title_summary_result dict."""
    raw = svc.summarize_title(title, TITLE_SUMMARY_MAX_CHARS, TITLE_SUMMARY_PROMPT_MIN_CHARS)
    result = _validate_ai_title_summary_result(_parse_title_summary_result(raw), original_title=title)
    if result["valid"]:
        return result
    raw2 = svc.summarize_title(
        title, TITLE_SUMMARY_MAX_CHARS, TITLE_SUMMARY_PROMPT_MIN_CHARS,
        feedback=result.get("reason") or "输出不合规",
        temperature=TITLE_RETRY_TEMPERATURE,
    )
    return _validate_ai_title_summary_result(_parse_title_summary_result(raw2), original_title=title)


def _translate_condense_with_retry(svc: "AIService", title: str) -> dict:
    """One-shot translate+shorten with one corrective retry. Validated as a
    title summary (the output is both translated and shortened), against the
    still-foreign original."""
    raw = svc.translate_and_condense_title(
        title, "zh-CN", TITLE_SUMMARY_MAX_CHARS, TITLE_SUMMARY_PROMPT_MIN_CHARS)
    result = _validate_ai_title_summary_result(_parse_title_summary_result(raw), original_title=title)
    if result["valid"]:
        return result
    raw2 = svc.translate_and_condense_title(
        title, "zh-CN", TITLE_SUMMARY_MAX_CHARS, TITLE_SUMMARY_PROMPT_MIN_CHARS,
        feedback=result.get("reason") or "输出不合规",
        temperature=TITLE_RETRY_TEMPERATURE,
    )
    return _validate_ai_title_summary_result(_parse_title_summary_result(raw2), original_title=title)


def _process_article_title(article: dict, config: dict) -> bool:
    article_id = article["id"]
    title = article.get("title") or ""
    changed = False
    svc = None
    summarize_enabled = bool(config.get("auto_title_summary_enabled"))

    if article.get("translate_title_needed") and _needs_translation(title):
        svc = _title_service(config)

        # Merged path: the title is BOTH foreign and over-long, so translate
        # and shorten it in a single call — one AI round-trip, and the title
        # only changes once on screen instead of translate-then-shorten.
        if (TITLE_MERGE_TRANSLATE_CONDENSE and summarize_enabled
                and _needs_title_summary(title)):
            result = _translate_condense_with_retry(svc, title)
            if result["valid"]:
                new_title = result["title"]
                if _save_article_title_update(article_id, new_title, "title_summary"):
                    changed = True
                _save_ai_result(
                    article_id,
                    title_summary=new_title,
                    title_summary_provider=config.get("provider_type") or "openai",
                    title_summary_model=config.get("model") or "",
                    title_summary_by_user_id=config.get("user_id"),
                    clear_title_translation_error=True,
                )
            else:
                print(f"[auto-title] Article {article_id}: discarded invalid "
                      f"translate+condense ({result['reason']}): {result['title'][:120]!r}")
                _save_ai_result(
                    article_id,
                    title_translation_error=f"{result['reason']}: {result['title'][:120]}",
                )
            return changed

        result = _translate_title_with_retry(svc, title)
        if result["valid"]:
            translated = result["title"]
            if _save_article_title_update(article_id, translated, "translation"):
                changed = True
            title = translated
            _save_ai_result(article_id, clear_title_translation_error=True)
        else:
            print(f"[auto-title] Article {article_id}: discarded invalid title translation "
                  f"({result['reason']}): {result['title'][:120]!r}")
            _save_ai_result(
                article_id,
                title_translation_error=f"{result['reason']}: {result['title'][:120]}",
            )

    if not summarize_enabled:
        return changed
    if not _needs_title_summary(title):
        return changed

    cached = _get_ai_result(article_id) or {}
    cached_summary = cached.get("title_summary")
    if cached_summary:
        cached_result = _validate_ai_title_summary_result(
            {"title": cached_summary, "valid": True, "reason": "cached title summary"},
            original_title=title,
        )
        if cached_result["valid"]:
            return _save_article_title_update(article_id, cached_result["title"], "title_summary") or changed
        _save_ai_result(article_id, title_summary_error=f"invalid cached title summary: {cached_result['reason']}")
        return changed

    try:
        svc = svc or _title_service(config)
        result = _summarize_title_with_retry(svc, title)
        if not result["valid"]:
            snippet = _clean_title_summary(result.get("title") or "")[:120] or "<empty>"
            reason = result.get("reason") or "invalid title summary"
            raise ValueError(f"AI returned invalid title summary: {reason}: {snippet}")
        short_title = result["title"]
        _save_ai_result(
            article_id,
            title_summary=short_title,
            title_summary_provider=config.get("provider_type") or "openai",
            title_summary_model=config.get("model") or "",
            title_summary_by_user_id=config.get("user_id"),
        )
        if _save_article_title_update(article_id, short_title, "title_summary"):
            changed = True
    except Exception as e:
        _save_ai_result(article_id, title_summary_error=str(e))
        raise
    return changed

def _run_auto_title_process_once():
    """Translate and shorten today's titles for opted-in users."""
    if not _auto_title_process_lock.acquire(blocking=False):
        return
    try:
        users = _get_auto_title_process_users()
        if not users:
            return
        for config in users:
            articles = _fetch_title_process_articles(config, AUTO_TITLE_PROCESS_BATCH_LIMIT)
            if not articles:
                continue
            print(f"[auto-title] User {config['user_id']}: processing {len(articles)} title(s)")
            for article in articles:
                try:
                    if _process_article_title(article, config):
                        print(f"[auto-title] Updated article {article['id']}: {article.get('title', '')[:50]}")
                except Exception as e:
                    print(f"[auto-title] Article {article.get('id')}: failed: {e}")
    finally:
        _auto_title_process_lock.release()


def _auto_title_process_loop():
    """Background loop for fast title translation/shortening."""
    import time as _time
    _time.sleep(20)
    while True:
        try:
            _run_auto_title_process_once()
        except Exception as e:
            print(f"[auto-title] Error in loop: {e}")
        _time.sleep(AUTO_TITLE_PROCESS_INTERVAL_SECONDS)


def _get_source_classification_config() -> dict | None:
    """Return the first enabled administrator AI config."""
    try:
        db = get_db()
        row = db.execute(
            "SELECT c.user_id, c.endpoint, c.model, c.api_key, c.provider_type, c.enabled "
            "FROM ai_configs c JOIN users u ON u.id = c.user_id "
            "WHERE c.enabled = 1 "
            "AND c.api_key != '' "
            "AND u.role = 'admin' "
            "ORDER BY c.user_id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[source-classify] settings DB error: {e}")
        return None


def _get_source_classification_users() -> list[dict]:
    """Return one administrator AI config for shared source classification."""
    try:
        db = get_db()
        rows = db.execute(
            "SELECT c.user_id, c.endpoint, c.model, c.api_key, c.provider_type, c.enabled "
            "FROM ai_configs c JOIN users u ON u.id = c.user_id "
            "WHERE c.enabled = 1 "
            "AND c.api_key != '' "
            "AND u.role = 'admin' "
            "ORDER BY c.user_id ASC LIMIT 1"
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
    ensure_article_sources(conn)
    rows = source_rows(conn)
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
        row for row in source_rows(conn)
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
    """Manually force-send today's shared daily summary now (bypasses the fixed send window)."""
    result = _broadcast_daily_summary(force=True)
    return jsonify(result)


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
                title_summary TEXT,
                title_summary_error TEXT,
                title_summary_error_at TEXT,
                title_translation_error TEXT,
                title_translation_error_at TEXT,
                title_summary_provider TEXT,
                title_summary_model TEXT,
                title_summary_by_user_id INTEGER,
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
        if "title_summary" not in cols:
            conn.execute("ALTER TABLE ai_results ADD COLUMN title_summary TEXT")
        if "title_summary_error" not in cols:
            conn.execute("ALTER TABLE ai_results ADD COLUMN title_summary_error TEXT")
        if "title_summary_error_at" not in cols:
            conn.execute("ALTER TABLE ai_results ADD COLUMN title_summary_error_at TEXT")
        if "title_translation_error" not in cols:
            conn.execute("ALTER TABLE ai_results ADD COLUMN title_translation_error TEXT")
        if "title_translation_error_at" not in cols:
            conn.execute("ALTER TABLE ai_results ADD COLUMN title_translation_error_at TEXT")
        if "title_summary_provider" not in cols:
            conn.execute("ALTER TABLE ai_results ADD COLUMN title_summary_provider TEXT")
        if "title_summary_model" not in cols:
            conn.execute("ALTER TABLE ai_results ADD COLUMN title_summary_model TEXT")
        if "title_summary_by_user_id" not in cols:
            conn.execute("ALTER TABLE ai_results ADD COLUMN title_summary_by_user_id INTEGER")
        if "updated_at" not in cols:
            conn.execute("ALTER TABLE ai_results ADD COLUMN updated_at TEXT")
            conn.execute("UPDATE ai_results SET updated_at = datetime('now') WHERE updated_at IS NULL")
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
            "SELECT summary, translation, summary_error, summary_error_at, "
            "title_summary, title_summary_error, title_summary_error_at, "
            "title_summary_provider, title_summary_model, title_summary_by_user_id "
            "FROM ai_results WHERE article_id = ?",
            (article_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


_SANITIZE_ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "s", "a", "ul", "ol", "li",
    "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "dd", "dt", "dl",
    "figure", "figcaption", "img", "span", "div", "table", "thead", "tbody",
    "tr", "td", "th", "code", "pre", "sub", "sup", "hr",
}
_SANITIZE_ALLOWED_ATTRS = {"a": {"href"}, "img": {"src", "alt"}}


def _sanitize_translated_html(html: str) -> str:
    """Whitelist-sanitize AI-translated article HTML before it enters the
    shared cache.

    Manual translate/summarize results come from whichever user happened to
    trigger them (browser-generated with their own API key), and even
    admin-driven auto-translation results come from an LLM reading
    attacker-influenced article text — never trust returned HTML as safe to
    innerHTML-render for every other user without stripping scripts, event
    handlers, and javascript:/data: URLs first.
    """
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(True):
        if tag.name not in _SANITIZE_ALLOWED_TAGS:
            tag.unwrap()
            continue
        allowed_attrs = _SANITIZE_ALLOWED_ATTRS.get(tag.name, set())
        for attr in list(tag.attrs.keys()):
            if attr not in allowed_attrs:
                del tag[attr]
        if tag.name == "a" and tag.get("href"):
            href = tag["href"].strip()
            if not re.match(r"^https?://|^mailto:", href, re.I):
                del tag["href"]
            else:
                tag["rel"] = "noopener noreferrer"
                tag["target"] = "_blank"
        if tag.name == "img" and tag.get("src"):
            src = tag["src"].strip()
            # Allow absolute http(s) URLs and our own same-origin image proxy
            # endpoints. The manual-translate flow feeds already-proxied body
            # HTML (img src="/img-cache?url=...") through here, so an https-only
            # rule silently strips *every* image from the translated result.
            # These relative URLs only ever load an image via our own cache; the
            # real risk this guards against is javascript:/data: srcs.
            if not re.match(r"^https?://|^/img-(?:cache|proxy)\?", src, re.I):
                tag.decompose()
    return str(soup)


def _sanitize_plain_text(text: str, max_len: int = 4000) -> str:
    """Strip any HTML markup for values that must always be plain text."""
    if not text:
        return text
    cleaned = BeautifulSoup(text, "html.parser").get_text()
    return cleaned.strip()[:max_len]


def _sanitize_translation_payload(translation: str) -> str:
    stripped = translation.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return _sanitize_translated_html(translation)
        data["html"] = _sanitize_translated_html(data.get("html") or "")
        data["title"] = _sanitize_plain_text(data.get("title") or "", max_len=300)
        return json.dumps(data, ensure_ascii=False)
    if stripped.startswith("["):
        # Legacy per-segment array shape ([{"id":0,"text":"<b>..</b>"}, ...]) —
        # no live route writes this anymore, but sanitize each item's HTML
        # defensively in case old data ever gets re-saved through here.
        try:
            items = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return _sanitize_translated_html(translation)
        if not isinstance(items, list):
            return _sanitize_translated_html(translation)
        for item in items:
            if isinstance(item, dict) and "text" in item:
                item["text"] = _sanitize_translated_html(item.get("text") or "")
        return json.dumps(items, ensure_ascii=False)
    return _sanitize_translated_html(translation)


def _save_ai_result(article_id: int, summary: str | None = None,
                    translation: str | None = None,
                    summary_error: str | None = None,
                    title_summary: str | None = None,
                    title_summary_error: str | None = None,
                    title_summary_provider: str | None = None,
                    title_summary_model: str | None = None,
                    title_summary_by_user_id: int | None = None,
                    title_translation_error: str | None = None,
                    clear_title_translation_error: bool = False):
    """Save or update AI result for an article."""
    import sqlite3
    if summary is not None:
        summary = _sanitize_plain_text(summary)
    if translation is not None:
        translation = _sanitize_translation_payload(translation)
    if not os.path.exists(NEWS_DB):
        return
    conn = None
    try:
        conn = sqlite3.connect(NEWS_DB)
        _init_ai_results_table()
        conn.execute(
            """
            INSERT INTO ai_results
            (article_id, summary, translation, summary_error, summary_error_at,
             title_summary, title_summary_error, title_summary_error_at,
             title_translation_error, title_translation_error_at,
             title_summary_provider, title_summary_model, title_summary_by_user_id)
            VALUES (?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END,
                    ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END,
                    ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END,
                    ?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
                summary = COALESCE(excluded.summary, summary),
                translation = COALESCE(excluded.translation, translation),
                summary_error = CASE
                    WHEN excluded.summary IS NOT NULL THEN NULL
                    WHEN excluded.summary_error IS NOT NULL THEN excluded.summary_error
                    ELSE summary_error
                END,
                summary_error_at = CASE
                    WHEN excluded.summary IS NOT NULL THEN NULL
                    WHEN excluded.summary_error IS NOT NULL THEN datetime('now')
                    ELSE summary_error_at
                END,
                title_summary = COALESCE(excluded.title_summary, title_summary),
                title_summary_error = CASE
                    WHEN excluded.title_summary IS NOT NULL THEN NULL
                    WHEN excluded.title_summary_error IS NOT NULL THEN excluded.title_summary_error
                    ELSE title_summary_error
                END,
                title_summary_error_at = CASE
                    WHEN excluded.title_summary IS NOT NULL THEN NULL
                    WHEN excluded.title_summary_error IS NOT NULL THEN datetime('now')
                    ELSE title_summary_error_at
                END,
                title_translation_error = CASE
                    WHEN ? = 1 THEN NULL
                    WHEN excluded.title_translation_error IS NOT NULL THEN excluded.title_translation_error
                    ELSE title_translation_error
                END,
                title_translation_error_at = CASE
                    WHEN ? = 1 THEN NULL
                    WHEN excluded.title_translation_error IS NOT NULL THEN datetime('now')
                    ELSE title_translation_error_at
                END,
                title_summary_provider = COALESCE(excluded.title_summary_provider, title_summary_provider),
                title_summary_model = COALESCE(excluded.title_summary_model, title_summary_model),
                title_summary_by_user_id = COALESCE(excluded.title_summary_by_user_id, title_summary_by_user_id),
                updated_at = datetime('now')
            """,
            (
                article_id,
                summary,
                translation,
                summary_error[:500] if summary_error else None,
                summary_error,
                title_summary,
                title_summary_error[:500] if title_summary_error else None,
                title_summary_error,
                title_translation_error[:500] if title_translation_error else None,
                title_translation_error,
                title_summary_provider,
                title_summary_model,
                title_summary_by_user_id,
                1 if clear_title_translation_error else 0,
                1 if clear_title_translation_error else 0,
            ),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        if conn:
            conn.close()


# ─── AI Result Cache (read-only) ──────────────────────────


@app.route("/ai/result/<int:article_id>", methods=["GET"])
@require_role("user", "admin")
def ai_get_result(article_id):
    """Return cached AI result (summary/translation) without generating.

    Shared cache, but each field is only returned to viewers who opted into
    seeing it (Settings → AI → 共享) — see share_view_summary/share_view_translation.
    """
    cached = dict(_get_ai_result(article_id) or {})
    settings = get_user_settings(g.user_id) or {}
    if not settings.get("share_view_summary"):
        cached.pop("summary", None)
        cached.pop("summary_error", None)
        cached.pop("summary_error_at", None)
    if not settings.get("share_view_translation"):
        cached.pop("translation", None)
    return jsonify(cached)


# ─── Source Categories ─────────────────────────────────────

def _promote_legacy_admin_source_settings(conn: sqlite3.Connection) -> None:
    """Migrate the first administrator's old private source overrides once."""
    global _legacy_admin_source_settings_promoted
    if _legacy_admin_source_settings_promoted:
        return
    try:
        with _legacy_admin_source_settings_lock:
            if _legacy_admin_source_settings_promoted:
                return
            admin = get_db().execute(
                "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
            ).fetchone()
            if not admin:
                return
            promote_user_source_settings(conn, int(admin["id"]))
            _legacy_admin_source_settings_promoted = True
    except Exception as exc:
        print(f"[sources] Failed to promote legacy administrator settings: {exc}")


@app.route("/sources", methods=["GET"])
@require_role("user", "admin")
def list_sources():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    _promote_legacy_admin_source_settings(conn)
    return jsonify({
        "categories": CATEGORY_ORDER,
        "category_names": CATEGORY_NAMES,
        "sources": source_rows(conn),
    })


@app.route("/sources/articles", methods=["GET"])
@require_role("user", "admin")
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
        sources.extend(source_aliases_for_target(conn, source))
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


def _delete_article_ids(article_ids: list[int], deleted_by: int | None = None) -> dict:
    ids = sorted({int(article_id) for article_id in article_ids if int(article_id) > 0})
    if not ids:
        return {"deleted": 0, "deleted_sources": 0}
    conn = _get_news_db()
    if not conn:
        raise FileNotFoundError("news db not found")
    _ensure_news_schema(conn)
    placeholders = ",".join("?" * len(ids))
    existing = conn.execute(
        f"SELECT id, title, COALESCE(NULLIF(feed_source, ''), source) AS source FROM articles WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    if not existing:
        return {"deleted": 0, "deleted_sources": cleanup_stale_source_categories(conn)}
    for row in existing:
        conn.execute(
            """
            INSERT INTO deleted_articles (article_id, title, source, deleted_by, deleted_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(article_id) DO UPDATE SET
                title = excluded.title,
                source = excluded.source,
                deleted_by = excluded.deleted_by,
                deleted_at = excluded.deleted_at
            """,
            (row["id"], row["title"] or "", row["source"] or "", deleted_by),
        )
    existing_ids = [int(row["id"]) for row in existing]
    existing_placeholders = ",".join("?" * len(existing_ids))
    cur = conn.execute(f"DELETE FROM articles WHERE id IN ({existing_placeholders})", existing_ids)
    try:
        conn.execute(f"DELETE FROM ai_results WHERE article_id IN ({existing_placeholders})", existing_ids)
    except sqlite3.OperationalError:
        pass
    conn.commit()
    deleted_sources = cleanup_stale_source_categories(conn)

    # Favorites live in raynews.db; remove global references to deleted articles.
    app_db = get_db()
    app_db.execute(f"DELETE FROM favorites WHERE article_id IN ({existing_placeholders})", existing_ids)
    app_db.commit()
    for article_id in existing_ids:
        threading.Thread(target=unpin_article_images, args=(article_id,), daemon=True).start()
    return {"deleted": cur.rowcount, "deleted_sources": deleted_sources}


@app.route("/articles", methods=["DELETE"])
@require_role("admin")
def delete_articles():
    data = request.get_json(silent=True) or {}
    raw_ids = data.get("ids") or data.get("article_ids") or []
    if not isinstance(raw_ids, list):
        return jsonify({"error": "ids must be a list"}), 400
    try:
        result = _delete_article_ids([int(item) for item in raw_ids], deleted_by=g.user_id)
    except FileNotFoundError:
        return jsonify({"error": "news db not found"}), 404
    except (TypeError, ValueError):
        return jsonify({"error": "invalid article id"}), 400
    return jsonify({"ok": True, **result})


@app.route("/articles/<int:article_id>", methods=["DELETE"])
@require_role("admin")
def delete_article(article_id):
    try:
        result = _delete_article_ids([article_id], deleted_by=g.user_id)
    except FileNotFoundError:
        return jsonify({"error": "news db not found"}), 404
    return jsonify({"ok": True, **result})


@app.route("/sources/articles", methods=["DELETE"])
@require_role("admin")
def delete_source_articles():
    conn = _get_news_db()
    if not conn:
        return jsonify({"error": "news db not found"}), 404
    data = request.get_json(silent=True) or {}
    base_sources = []
    if isinstance(data.get("sources"), list):
        base_sources = [str(item).strip() for item in data.get("sources") if str(item).strip()]
    else:
        source = (data.get("source") or "").strip()
        if source:
            base_sources = [source]
    base_sources = base_sources[:100]
    if not base_sources:
        return jsonify({"error": "source required"}), 400
    sources = []
    for source in base_sources:
        sources.append(source)
        sources.extend(source_aliases_for_target(conn, source))
    sources = list(dict.fromkeys(sources))
    placeholders = ",".join("?" * len(sources))
    rows = conn.execute(
        f"SELECT id FROM articles WHERE COALESCE(NULLIF(feed_source, ''), source) IN ({placeholders})",
        sources,
    ).fetchall()
    result = _delete_article_ids([int(row["id"]) for row in rows], deleted_by=g.user_id)
    return jsonify({"ok": True, "sources": sources, **result})


@app.route("/sources", methods=["PUT"])
@require_role("admin")
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
        target_source = find_merge_target(conn, source, label)
        if target_source:
            target = merge_source(conn, source, target_source)
            return jsonify({
                **target,
                "merged": True,
                "merged_from": source,
                "target_source": target_source,
            })
        row = update_source_category(
            conn, source, category, label, status="manual", reason="user edited",
        )
        return jsonify(row)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/sources/classify", methods=["POST"])
@require_role("admin")
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
    if preserve_manual:
        conn.execute("DELETE FROM source_categories WHERE status != 'manual'")
    else:
        conn.execute("DELETE FROM source_categories")
        conn.execute("DELETE FROM source_aliases")
        conn.execute("DELETE FROM user_source_categories")
        conn.execute("DELETE FROM user_source_aliases")
    ensure_article_sources(conn)
    cleanup_stale_source_categories(conn)
    conn.commit()
    count = conn.execute("SELECT COUNT(*) AS c FROM source_categories").fetchone()["c"]
    return jsonify({"ok": True, "sources": count, "preserve_manual": preserve_manual})


@app.route("/sources/classify-job", methods=["POST"])
@require_role("admin")
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
@require_role("admin")
def classify_sources_job_status(job_id):
    with _source_classify_jobs_lock:
        job = _source_classify_jobs.get(job_id)
        if not job or job.get("user_id") != g.user_id:
            return jsonify({"error": "job not found"}), 404
        safe = {k: v for k, v in job.items() if k != "user_id"}
    return jsonify(safe)


def _telegram_channel_and_base() -> tuple[str, str]:
    """Resolve (channel, base_url) from TELEGRAM_CHANNEL_URL if set,
    otherwise fall back to legacy TELEGRAM_CHANNEL + hardcoded t.me domain."""
    channel_url = (os.environ.get("TELEGRAM_CHANNEL_URL") or "").strip()
    if channel_url:
        parsed = urlsplit(channel_url)
        path_parts = [p for p in parsed.path.split("/") if p]
        channel = path_parts[-1] if path_parts else ""
        return channel, f"{parsed.scheme}://{parsed.netloc}"
    channel = (os.environ.get("TELEGRAM_CHANNEL") or "").strip().lstrip("@")
    return channel, "https://t.me"


def _fetch_telegram_message_content(article_id: int) -> str:
    """Fetch the original Telegram message body for historical source repair."""
    channel, base_url = _telegram_channel_and_base()
    if not channel or channel == "your_channel":
        return ""
    url = f"{base_url}/{channel}/{article_id}?embed=1&mode=tme"
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
@require_role("admin")
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
@require_role("user", "admin")
def get_settings():
    settings = get_user_settings(g.user_id)
    if not settings:
        return jsonify({
            "auto_translate_title": False,
            "auto_translate_content": False,
            "auto_title_summary_enabled": False,
            "auto_summary_enabled": False,
            "daily_summary_enabled": False,
            "theme_preference": "system",
            "notification_config": {},
            "share_ai_results": False,
            "share_view_title": False,
            "share_view_translation": False,
            "share_view_summary": False,
            "share_last_check_at": None,
            "share_last_check_ok": None,
            "share_last_check_error": None,
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
@require_role("user", "admin")
def update_settings():
    data = request.get_json(silent=True) or {}
    # Auto-summary/auto-translate are admin-only; silently drop these fields
    # for non-admin requests instead of erroring, so the rest of the payload
    # (theme, notifications, daily summary) still saves normally.
    if g.user_role != "admin":
        for key in (
            "auto_summary_enabled",
            "auto_translate_title",
            "auto_translate_content",
            "auto_title_summary_enabled",
        ):
            data.pop(key, None)
    # Normalize notification_config to JSON string for storage
    if "notification_config" in data:
        nc = data["notification_config"]
        data["notification_config"] = json.dumps(nc) if isinstance(nc, dict) else nc
    if "theme_preference" in data:
        theme = str(data.get("theme_preference") or "system").strip().lower()
        data["theme_preference"] = theme if theme in {"system", "light", "dark"} else "system"

    # Sharing ("共享AI结果"): the master switch requires the user's own AI
    # config to pass a live connectivity test at save time — this isn't the
    # admin-only system AI, so every role (including plain "user") goes
    # through this check. Sub-toggles (title/translation/summary) may only be
    # turned on once the master switch is verified on; turning the master
    # switch off cascades off all sub-toggles regardless of what the request
    # body says, so we never persist a state where a sub-toggle is on while
    # the master is off.
    share_sub_keys = ("share_view_title", "share_view_translation", "share_view_summary")
    if "share_ai_results" in data:
        import datetime as _dt
        if _is_enabled_value(data.get("share_ai_results")):
            user_ai_config = get_ai_config(g.user_id)
            if not user_ai_config or not user_ai_config.get("enabled") or not user_ai_config.get("api_key"):
                return jsonify({"error": "请先在AI设置中配置并启用有效的API"}), 400
            test_body, test_status = _run_ai_connection_test(user_ai_config)
            now_str = _dt.datetime.now().isoformat(timespec="seconds")
            if test_status != 200:
                set_user_settings(
                    g.user_id,
                    share_last_check_at=now_str, share_last_check_ok=0,
                    share_last_check_error=test_body.get("error", "connection test failed"),
                )
                return jsonify({"error": test_body.get("error", "AI 连通性测试失败")}), 400
            data["share_ai_results"] = 1
            data["share_last_check_at"] = now_str
            data["share_last_check_ok"] = 1
            data["share_last_check_error"] = None
        else:
            data["share_ai_results"] = 0
            for key in share_sub_keys:
                data[key] = 0
    resulting_share_master = _is_enabled_value(
        data["share_ai_results"] if "share_ai_results" in data
        else (get_user_settings(g.user_id) or {}).get("share_ai_results")
    )
    if not resulting_share_master and any(_is_enabled_value(data.get(key)) for key in share_sub_keys):
        return jsonify({"error": "请先开启并通过校验“共享AI结果”"}), 400

    needs_ai_config = any(
        _is_enabled_value(data.get(key))
        for key in (
            "auto_summary_enabled",
            "auto_translate_title",
            "auto_translate_content",
            "auto_title_summary_enabled",
        )
    )
    if needs_ai_config:
        config = get_system_ai_config()
        if not config or not config.get("enabled") or not config.get("api_key"):
            return jsonify({
                "error": "请先在管理员设置→服务端API中配置系统AI"
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
    if _is_enabled_value(data.get("auto_title_summary_enabled")) or _is_enabled_value(data.get("auto_translate_title")):
        threading.Thread(target=_run_auto_title_process_once, daemon=True).start()
    return jsonify(safe)


def _is_enabled_value(value) -> bool:
    return value is True or value == 1 or str(value).strip().lower() in {"1", "true", "yes", "on"}


@app.route("/settings/test-notification", methods=["POST"])
@require_role("user", "admin")
def test_notification():
    """Send a test email via Resend API using configured server email settings."""
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
        from_email = os.environ.get("RAYNEWS_FROM_EMAIL") or "onboarding@resend.dev"
        result = send_email(api_key, to_email,
                            "RayNews 测试通知",
                            "<h2>✅ 配置成功</h2><p>这是一封来自 RayNews 的测试邮件，通知功能正常工作。</p>",
                            from_email=from_email)
        return jsonify({"ok": True, "id": result.get("id", "")})
    except Exception as e:
        return jsonify({"error": f"send failed: {str(e)}"}), 502


# ─── Health (unused section divider) ────────────────────────

# ─── Authenticated Refresh ────────────────────────────────

@app.route("/auth/refresh", methods=["POST", "GET"])
@require_role("user", "admin")
def protected_refresh():
    """Trigger fetcher refresh. Requires an authenticated user or admin."""
    import requests as http_req
    try:
        resp = http_req.get("http://127.0.0.1:8081/refresh", timeout=150)
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
    beijing_now = _beijing_now()
    today_str = beijing_now.strftime("%Y-%m-%d")
    today_sent = sorted(_get_daily_summary_sent_user_ids(today_str))
    return jsonify({
        "running": True,
        "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "beijing_time": beijing_now.strftime("%Y-%m-%d %H:%M:%S"),
        "today": today_str,
        "timezone_hint": str(_dt.datetime.now().astimezone().tzinfo),
        "daily_summary_send_time": f"{DAILY_SUMMARY_HOUR:02d}:{DAILY_SUMMARY_MINUTE:02d}",
        "daily_summary_sent_user_ids_today": today_sent,
    })


# ─── Main ─────────────────────────────────────────────────────

if __name__ == "__main__":
    _init_ai_results_table()
    _init_daily_summary_cache_table()
    _init_daily_summary_global_table()
    _init_daily_summary_sends_table()
    import threading as _th
    _th.Thread(target=_daily_summary_loop, daemon=True).start()
    _th.Thread(target=_auto_summary_loop, daemon=True).start()
    _th.Thread(target=_auto_translation_loop, daemon=True).start()
    _th.Thread(target=_auto_title_process_loop, daemon=True).start()
    _th.Thread(target=_auto_source_classification_loop, daemon=True).start()
    _th.Thread(target=_ai_share_revalidation_loop, daemon=True).start()
    _th.Thread(target=_pin_existing_favorite_images_on_startup, daemon=True).start()
    print("[scheduler] Daily summary background thread started")
    print("[auto-summary] Background summary thread started")
    print("[auto-translate] Background translation thread started")
    print("[auto-title] Background title processing thread started")
    print("[source-classify] Background source classification thread started")
    print("[image-cache] Existing favorite image pinning thread started")
    port = int(os.environ.get("WEB_PORT", 8082))
    print(f"[web] RayNews Web Server listening on {port}")
    app.run(host="127.0.0.1", port=port, debug=False)
