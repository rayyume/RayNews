# Task 1 Report: Safe Markdown notification emails

## Summary

Implemented safe server-side Markdown rendering for administrator broadcast emails while preserving the literal plain-text default used by ordinary/system notifications. The committed broadcast format now travels through the background fan-out into the email renderer. The daily-summary email implementation and template were not changed.

## TDD evidence

### RED — renderer contract

Command:

```bash
python3 -m pytest -q tests/test_notification_email_markdown.py
```

Result: **5 failed**. Every test failed with the expected `AttributeError` because `notifier.render_notification_email_body` did not exist yet. The tests covered supported Markdown structure, absolute proxied image URLs, dangerous markup/protocol removal, missing public URL behavior, plain escaping, and unknown-format plain fallback.

### GREEN — renderer contract

Command:

```bash
python3 -m pytest -q tests/test_notification_email_markdown.py
```

Result: **5 passed**.

### RED — broadcast format propagation

Command:

```bash
python3 -m pytest -q tests/test_notification_broadcast.py tests/test_email_delivery_failure_alert.py -k 'email or markdown or format'
```

Result: **2 failed, 14 passed, 9 deselected**. Markdown broadcasts reached the synchronous fan-out as `plain`, proving `_broadcast_notification_emails` discarded the committed format.

A direct helper integration test was then run before changing the helper:

```bash
python3 -m pytest -q tests/test_email_delivery_failure_alert.py -k markdown
```

Result: **1 failed, 10 deselected** with the expected `TypeError`: `_send_notification_email()` did not accept `fmt`.

### GREEN — focused renderer and propagation contracts

Command:

```bash
python3 -m pytest -q tests/test_notification_email_markdown.py tests/test_notification_broadcast.py tests/test_email_delivery_failure_alert.py -k 'email or markdown or format'
```

Result: **22 passed, 9 deselected**. Seven pre-existing `datetime.utcnow()` deprecation warnings remained.

## Regression and static verification

Command:

```bash
python3 -m pytest -q tests/test_notification_email_markdown.py tests/test_notification_broadcast.py tests/test_email_delivery_failure_alert.py tests/test_daily_summary_delivery.py tests/test_daily_summary_retry.py tests/test_share_suspension_notice_delivery.py
```

Result: **72 passed**, with 23 pre-existing `datetime.utcnow()` deprecation warnings.

Command:

```bash
python3 -m py_compile notifier.py web_server.py tests/test_notification_email_markdown.py
```

Result: exit 0, no output.

Command:

```bash
python3 -m pytest -q
```

Result: **666 passed**, with 59 pre-existing `datetime.utcnow()` deprecation warnings.

Command:

```bash
python3 -m compileall -q .
git diff --check
```

Result: exit 0, no output.

## Security decisions

- Markdown is parsed only for explicit `fmt="markdown"`; every other format, including omitted/unknown values, uses `html.escape()` and newline-to-`<br>` conversion.
- The rendered fragment is parsed with BeautifulSoup and reduced to a declared semantic tag whitelist.
- Script, style, iframe, form/control, embedded-object, SVG/MathML, metadata, and template elements are decomposed together with their contents.
- Unknown non-dangerous containers are unwrapped; their attributes cannot survive.
- Each allowed element keeps only explicitly declared attributes, removing event handlers and arbitrary style/data attributes.
- Links must be absolute HTTP(S) URLs without credentials or control characters. Safe links receive `target="_blank"` and `rel="noopener noreferrer"`; unsafe links retain only inert text.
- Images must use an absolute HTTP(S) source and require an absolute safe HTTP(S) `RAYNEWS_PUBLIC_URL`. Their original source is percent-encoded into the absolute `/img-cache?url=` contract. Invalid sources or missing/invalid public bases remove the image entirely.
- Email titles remain escaped, and existing idempotency keys, recipient resolution, best-effort failure alerts, return values, system notification defaults, and daily-summary flow remain unchanged.

## Changed files

- `notifier.py` — added the safe renderer and sanitizer.
- `web_server.py` — propagated `fmt`, used the renderer, and added notification-email Markdown CSS.
- `frontend/index.html` — updated the administrator email behavior hint.
- `tests/test_notification_email_markdown.py` — added renderer/security contracts.
- `tests/test_notification_broadcast.py` — added real-route synchronous fan-out format and replay contracts.
- `tests/test_email_delivery_failure_alert.py` — added default-plain and explicit-Markdown helper integration contracts.
- `.superpowers/sdd/2026-07-29-notification-email-markdown/task-1-report.md` — this report.

## Concerns

No task blocker. The only verification noise is the repository's existing `datetime.utcnow()` deprecation warnings.
