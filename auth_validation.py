"""Validation helpers shared by RayNews authentication routes."""

from __future__ import annotations

import re


_EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+"
    r"[A-Z]{2,63}$",
    re.IGNORECASE,
)


def is_valid_email(value: str) -> bool:
    """Return whether value is a practical Internet email address."""
    email = (value or "").strip()
    if len(email) > 254 or ".." in email:
        return False
    local, separator, _domain = email.rpartition("@")
    if not separator or not local or len(local) > 64:
        return False
    if local.startswith(".") or local.endswith("."):
        return False
    return bool(_EMAIL_RE.fullmatch(email))
