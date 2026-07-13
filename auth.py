"""JWT authentication utilities and Flask decorators for RayNews."""

import jwt
import time
import functools
import os
from flask import request, jsonify, g

SECRET_KEY = None  # set on init


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


JWT_EXPIRY = _int_env("RAYNEWS_TOKEN_EXPIRY_SECONDS", 30 * 24 * 3600)


def init_auth(secret_key: str):
    global SECRET_KEY
    SECRET_KEY = secret_key


# ─── Token ────────────────────────────────────────────────────

def create_token(user_id: int, role: str) -> str:
    payload = {
        "user_id": user_id,
        "role": role,
        "exp": int(time.time()) + JWT_EXPIRY,
        "iat": int(time.time()),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ─── Decorators ───────────────────────────────────────────────

def require_auth(f):
    """Require a valid JWT token. Sets g.user_id and g.user_role from DB."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "missing token"}), 401
        payload = decode_token(auth[7:])
        if payload is None:
            return jsonify({"error": "invalid or expired token"}), 401
        g.user_id = payload["user_id"]
        # Verify current role from database (don't trust stale JWT role)
        from models import get_user, record_access
        user = get_user(g.user_id)
        if not user:
            return jsonify({"error": "user not found"}), 401
        g.user_role = user["role"]
        record_access(g.user_id)
        return f(*args, **kwargs)

    return wrapper


def require_role(*roles: str):
    """Require the user to have one of the given roles."""

    def decorator(f):
        @functools.wraps(f)
        @require_auth
        def wrapper(*args, **kwargs):
            if g.user_role not in roles:
                return jsonify({"error": "insufficient permissions"}), 403
            return f(*args, **kwargs)

        return wrapper

    return decorator
