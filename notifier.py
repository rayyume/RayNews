"""RayNews Notifier — Send notifications via Resend API."""

import json
import os
import markdown
import requests

RESEND_API = "https://api.resend.com/emails"


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
    data = resp.json()
    if resp.status_code not in (200, 201):
        error_msg = data.get("message") or data.get("error", str(resp.status_code))
        raise Exception(f"Resend error: {error_msg}")
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
