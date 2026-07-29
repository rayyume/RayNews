"""RayNews Notifier — Send notifications via Resend API."""

import html
import json
import os
import markdown
import requests
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup

RESEND_API = "https://api.resend.com/emails"

_NOTIFICATION_EMAIL_TAGS = {
    "a", "blockquote", "br", "code", "em", "h1", "h2", "h3", "h4", "h5",
    "h6", "hr", "img", "li", "ol", "p", "pre", "strong", "table", "tbody",
    "td", "th", "thead", "tr", "ul",
}
_NOTIFICATION_EMAIL_ATTRIBUTES = {
    "a": {"href", "title"},
    "code": {"class"},
    "img": {"alt", "src", "title"},
    "td": {"align"},
    "th": {"align"},
}
_NOTIFICATION_EMAIL_DANGEROUS_TAGS = {
    "base", "button", "embed", "form", "iframe", "input", "link", "math",
    "meta", "noscript", "object", "script", "select", "style", "svg",
    "template", "textarea",
}


def _safe_absolute_http_url(value: str) -> str | None:
    """Return a stripped absolute HTTP(S) URL, or None for unsafe input."""
    value = (value or "").strip()
    if not value or any(char in value for char in "\r\n\t"):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None and not 0 < port <= 65535
    ):
        return None
    return value


def render_notification_email_body(body: str, fmt: str = "plain") -> str:
    """Render a notification body for email, sanitizing Markdown as HTML."""
    body = body or ""
    if fmt != "markdown":
        return html.escape(body).replace("\n", "<br>")

    rendered = markdown.markdown(
        body,
        extensions=["fenced_code", "tables"],
    )
    soup = BeautifulSoup(rendered, "html.parser")

    for tag in list(soup.find_all(_NOTIFICATION_EMAIL_DANGEROUS_TAGS)):
        tag.decompose()

    public_url = _safe_absolute_http_url(
        os.environ.get("RAYNEWS_PUBLIC_URL", "").rstrip("/")
    )
    for tag in list(soup.find_all(True)):
        if tag.name not in _NOTIFICATION_EMAIL_TAGS:
            tag.unwrap()
            continue

        allowed_attributes = _NOTIFICATION_EMAIL_ATTRIBUTES.get(tag.name, set())
        tag.attrs = {
            name: value
            for name, value in tag.attrs.items()
            if name in allowed_attributes
        }

        if tag.name == "a":
            href = _safe_absolute_http_url(tag.get("href", ""))
            if href:
                tag["href"] = href
                tag["target"] = "_blank"
                tag["rel"] = "noopener noreferrer"
            else:
                tag.attrs.pop("href", None)
        elif tag.name == "img":
            src = _safe_absolute_http_url(tag.get("src", ""))
            if not src or not public_url:
                tag.decompose()
                continue
            tag["src"] = f"{public_url}/img-cache?url={quote(src, safe='')}"

    return str(soup)


class EmailDeliveryError(Exception):
    """Base class for delivery errors with a known certainty category."""


class EmailDeliveryRejected(EmailDeliveryError):
    """The provider definitively rejected the request before accepting it."""


class EmailDeliveryUncertain(EmailDeliveryError):
    """The request may have been accepted but its final response was lost."""


def send_email(api_key: str, to_email: str, subject: str,
               html_body: str, from_name: str = "RayNews",
               from_email: str | None = None,
               idempotency_key: str | None = None) -> dict:
    """Send an email via Resend API. Returns response dict.

    `idempotency_key`, when provided, is passed to Resend so that a retry of a
    send whose response we never saw (network timeout, non-JSON gateway error)
    replays the original send instead of delivering a second copy. Resend keys
    are honoured for 24h — long enough to cover the daily-summary send window.
    """
    from_email = (from_email or os.environ.get("RAYNEWS_FROM_EMAIL") or "onboarding@resend.dev").strip()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        resp = requests.post(
            RESEND_API,
            headers=headers,
            json={
                "from": f"{from_name} <{from_email}>",
                "to": [to_email],
                "subject": subject,
                "html": html_body,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise EmailDeliveryUncertain(
            "email delivery status is uncertain"
        ) from exc

    try:
        data = resp.json()
    except ValueError as exc:
        if 400 <= resp.status_code < 500:
            raise EmailDeliveryRejected(
                f"Resend error: {resp.status_code}"
            ) from exc
        raise EmailDeliveryUncertain(
            "email delivery status is uncertain"
        ) from exc
    if resp.status_code not in (200, 201):
        error_msg = data.get("message") or data.get("error", str(resp.status_code))
        error_type = (
            EmailDeliveryRejected
            if 400 <= resp.status_code < 500
            else EmailDeliveryUncertain
        )
        raise error_type(f"Resend error: {error_msg}")
    return data


def send_daily_summary_email(api_key: str, to_email: str,
                             summary_text: str, stats: dict,
                             idempotency_key: str | None = None) -> dict:
    """Send a formatted daily summary email via Resend.
    Converts Markdown summary_text to HTML before embedding.
    stats is a dict with keys: total_articles, articles_after_dedup,
    articles_selected_for_ai, selected_articles_with_summary.
    """
    # Convert markdown to HTML with fenced code blocks and tables
    summary_html = markdown.markdown(
        summary_text,
        extensions=["fenced_code", "tables"],
    )
    total = stats.get("total_articles", 0)
    deduped = stats.get("articles_after_dedup", 0)
    selected = stats.get("articles_selected_for_ai", deduped)
    with_summary = stats.get("selected_articles_with_summary", stats.get("articles_with_summary", 0))
    public_url = os.environ.get("RAYNEWS_PUBLIC_URL", "").rstrip("/")
    footer_link = f'<a href="{public_url}">打开 RayNews</a>' if public_url else "RayNews"
    subtitle = f"{total} 篇原始 · {deduped} 篇去重 · 入选 {selected} 篇 · 入选中 {with_summary} 篇已有摘要"
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0c;color:#e8e8ed;padding:20px;max-width:600px;margin:0 auto}}
h1{{font-size:20px;font-weight:800;color:#6e8efb;margin-bottom:8px}}
h2{{font-size:17px;font-weight:700;color:#6e8efb;margin:20px 0 10px}}
h3{{font-size:15px;font-weight:700;color:#9aaafb;margin:16px 0 8px}}
p{{font-size:15px;line-height:1.8;margin:8px 0;color:#e8e8ed}}
ul,ol{{padding-left:20px;margin:8px 0}}
li{{font-size:15px;line-height:1.8;color:#e8e8ed;margin:4px 0}}
strong{{color:#ffffff;font-weight:700}}
code{{background:rgba(255,255,255,0.08);padding:2px 6px;border-radius:4px;font-size:13px;color:#f0c674}}
pre{{background:rgba(0,0,0,0.3);padding:12px 16px;border-radius:8px;overflow-x:auto;font-size:13px;line-height:1.5;color:#c5c8c6}}
blockquote{{border-left:3px solid #6e8efb;margin:10px 0;padding:8px 16px;color:#8b8b9e;background:rgba(110,142,251,0.06);border-radius:0 6px 6px 0}}
a{{color:#6e8efb;text-decoration:none}}
hr{{border:none;border-top:1px solid rgba(255,255,255,0.08);margin:16px 0}}
.date{{color:#8b8b9e;font-size:13px;margin-bottom:20px}}
.footer{{margin-top:24px;padding-top:16px;border-top:1px solid rgba(255,255,255,0.06);font-size:12px;color:#55556a}}
</style></head>
<body>
<h1>📰 RayNews 每日摘要</h1>
<p class="date">{subtitle}</p>
<div class="summary">
{summary_html}
</div>
<p class="footer">由 RayNews 自动生成 · {footer_link}</p>
</body>
</html>"""
    return send_email(
        api_key, to_email,
        f"RayNews 每日摘要 — {__import__('datetime').date.today()}",
        html,
        idempotency_key=idempotency_key,
    )
