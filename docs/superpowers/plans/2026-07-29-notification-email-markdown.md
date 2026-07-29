# Notification Email Markdown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render administrator Markdown broadcasts as safe, styled HTML emails with proxied images while preserving plain-text and system-notification behavior.

**Architecture:** Add a focused Markdown-to-email-HTML renderer in `notifier.py`, using Python Markdown plus a BeautifulSoup whitelist and absolute RayNews image-cache rewriting. Thread the committed notification format through the broadcast email fan-out into `_send_notification_email(format="plain")`; update the admin hint and cover the complete path with regression tests.

**Tech Stack:** Python, Flask, Python-Markdown, BeautifulSoup, Resend, pytest, vanilla HTML.

## Global Constraints

- Markdown rendering applies only when notification format is exactly `markdown`; default is `plain`.
- Plain notifications and ordinary system notification emails retain escaped-text behavior.
- Permit h1-h6, paragraphs, emphasis, lists, blockquotes, code, fenced code, tables, safe HTTP(S) links, and HTTP(S) images.
- Remove unsafe HTML, event attributes, forms, frames, styles, and non-HTTP(S) link/image protocols.
- Rewrite email images to an absolute `RAYNEWS_PUBLIC_URL/img-cache?url=<encoded>` URL; remove images when no valid public base URL exists.
- Preserve broadcast persistence, replay/idempotency, recipient selection, and failure-alert behavior.
- Do not change the daily-summary email template or sending flow.

---

### Task 1: Safe Markdown rendering for notification email fan-out

**Files:**
- Modify: `notifier.py:1-130`
- Modify: `web_server.py:873-943,1354-1408`
- Modify: `frontend/index.html:815-824`
- Modify: `tests/test_notification_broadcast.py`
- Modify: `tests/test_email_delivery_failure_alert.py`
- Create: `tests/test_notification_email_markdown.py`

**Interfaces:**
- Produces: `render_notification_email_body(body: str, fmt: str = "plain") -> str`.
- Modifies: `_send_notification_email(user_id, title, body, idempotency_key=None, fmt="plain") -> bool`.
- Consumes: committed broadcast `fmt`, `RAYNEWS_PUBLIC_URL`, existing `send_email()` and `/img-cache` contract.

- [ ] **Step 1: Write renderer RED tests**

Create tests asserting Markdown h4, strong text, lists, links, fenced code, tables and image syntax produce HTML; images become an encoded absolute `/img-cache?url=` URL; `javascript:` links, raw scripts, event attributes, forms and unsafe images do not survive; `fmt="plain"` still escapes tags and converts newlines to `<br>`.

Use representative assertions:

```python
html = notifier.render_notification_email_body(
    "#### 标题\n\n**重点**\n\n![图](https://img.example/a.png)", "markdown"
)
assert "<h4>标题</h4>" in html
assert "<strong>重点</strong>" in html
assert "https://news.example/img-cache?url=https%3A%2F%2Fimg.example%2Fa.png" in html
assert "<script" not in unsafe_html.lower()
assert notifier.render_notification_email_body("<b>x</b>\ny", "plain") == "&lt;b&gt;x&lt;/b&gt;<br>y"
```

- [ ] **Step 2: Verify renderer tests RED**

Run: `python3 -m pytest -q tests/test_notification_email_markdown.py`

Expected: FAIL because `render_notification_email_body` does not exist.

- [ ] **Step 3: Implement minimal safe renderer and email CSS**

In `notifier.py`, add a whitelist sanitizer using `BeautifulSoup`. Parse `markdown.markdown(body, extensions=["fenced_code", "tables"])`, unwrap harmless unknown containers only when safe, remove dangerous element contents, retain only declared attributes, validate schemes with `urllib.parse.urlsplit`, add safe link attributes, and rewrite valid image sources through the absolute public `/img-cache` URL. Keep plain rendering in the same helper using `html.escape` plus newline-to-`<br>`.

Update the notification email template CSS for `h1` through `h6`, paragraphs, lists, links, blockquotes, code/pre, tables, and responsive images. Do not change `send_email()` or the daily-summary template.

- [ ] **Step 4: Write broadcast-format propagation RED tests**

Add tests that call the real broadcast route with email enabled while synchronously substituting the background thread and capturing `_send_notification_email`; assert Markdown passes `fmt="markdown"`, plain passes `fmt="plain"`, and a replay launches no second fan-out. Add helper tests that existing calls without `fmt` still render plain text.

- [ ] **Step 5: Verify propagation tests RED**

Run: `python3 -m pytest -q tests/test_notification_broadcast.py tests/test_email_delivery_failure_alert.py -k 'email or markdown or format'`

Expected: FAIL because `_broadcast_notification_emails` discards `fmt` and `_send_notification_email` has no format parameter.

- [ ] **Step 6: Thread format through the committed broadcast path**

Pass `fmt` from `admin_broadcast_notification()` to the background thread, then to `_broadcast_notification_emails()` and `_send_notification_email()`. Add `fmt="plain"` after the existing optional parameters to preserve callers. Replace the helper's inline escape/newline code with `render_notification_email_body(body, fmt)`; leave title escaping, idempotency key, delivery alerts, and return values unchanged.

Update the admin hint to state that Markdown email preserves text layout and images, while plain mode remains literal.

- [ ] **Step 7: Run GREEN and regression verification**

Run:

```bash
python3 -m pytest -q tests/test_notification_email_markdown.py tests/test_notification_broadcast.py tests/test_email_delivery_failure_alert.py tests/test_daily_summary_delivery.py tests/test_daily_summary_retry.py tests/test_share_suspension_notice_delivery.py
python3 -m py_compile notifier.py web_server.py tests/test_notification_email_markdown.py
python3 -m pytest -q
python3 -m compileall -q .
git diff --check
```

Expected: all tests and static checks pass; only pre-existing warnings may remain.

- [ ] **Step 8: Commit**

```bash
git add notifier.py web_server.py frontend/index.html tests/test_notification_broadcast.py tests/test_email_delivery_failure_alert.py tests/test_notification_email_markdown.py
git commit -m "fix: render markdown notification emails"
```
