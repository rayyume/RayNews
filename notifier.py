"""RayNews Notifier — Send notifications via Resend API."""

import json
import requests

RESEND_API = "https://api.resend.com/emails"


def send_email(api_key: str, to_email: str, subject: str,
               html_body: str, from_name: str = "RayNews",
               from_email: str = "notifications@rayyu.me") -> dict:
    """Send an email via Resend API. Returns response dict."""
    resp = requests.post(
        RESEND_API,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
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
                             summary_text: str, article_count: int) -> dict:
    """Send a formatted daily summary email via Resend."""
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0a0c;color:#e8e8ed;padding:20px;max-width:600px;margin:0 auto}}
h1{{font-size:20px;font-weight:800;color:#6e8efb;margin-bottom:8px}}
.date{{color:#8b8b9e;font-size:13px;margin-bottom:20px}}
.summary{{font-size:15px;line-height:1.8;white-space:pre-wrap;color:#e8e8ed}}
.footer{{margin-top:24px;padding-top:16px;border-top:1px solid rgba(255,255,255,0.06);font-size:12px;color:#55556a}}
</style></head>
<body>
<h1>📰 RayNews 每日摘要</h1>
<p class="date">{article_count} 条新闻摘要</p>
<div class="summary">{summary_text}</div>
<p class="footer">由 RayNews 自动生成 · <a href="https://rayyu.me" style="color:#6e8efb">打开 RayNews</a></p>
</body>
</html>"""
    return send_email(api_key, to_email, f"RayNews 每日摘要 — {__import__('datetime').date.today()}", html)
