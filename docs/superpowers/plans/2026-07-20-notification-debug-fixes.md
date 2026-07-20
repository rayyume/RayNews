# Notification Debug Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Markdown notification rendering responsive and correct, make published notifications reliably visible, and show one consistent unread `new` indicator in every requested UI location.

**Architecture:** Replace the process-wide models SQLite connection with a connection owned by each thread, retaining the existing atomic broadcast connection. Treat notification fetch failures as failures instead of empty data and prohibit caching of user-specific notification responses. Use a block-aware Markdown list renderer and a shared notification Markdown layout class; reuse one amber tag style for the menu and unread list rows.

**Tech Stack:** Flask, SQLite, inline browser JavaScript/CSS, pytest, Node.js source-contract tests.

## Global Constraints

- Preserve the existing broadcast idempotency and post-commit email behavior.
- Do not add client-side Markdown dependencies.
- Keep plain notification bodies as escaped text with line breaks.
- Do not install browser-testing dependencies; use existing automated tests and perform browser QA only when a browser runtime is available.

---

### Task 1: Isolate model SQLite connections by thread

**Files:**
- Modify: `models.py`
- Modify: `tests/test_notifications.py`
- Modify: `tests/test_notification_broadcast.py`
- Modify: `tests/test_users_role_migration.py`

**Interfaces:**
- Produces: `get_db() -> sqlite3.Connection`, returning a reusable connection for the calling thread and database path only.
- Produces: `close_db() -> None`, closing the calling thread's model connection.

- [ ] Add a regression test that obtains `get_db()` in a worker thread and asserts its connection is not the main-thread connection.
- [ ] Run the test and confirm the process-wide `_db` implementation fails it.
- [ ] Replace the global connection with `threading.local()` state, a connection initialization lock, `busy_timeout=30000`, WAL, foreign keys, and per-path reconnection.
- [ ] Update temporary-database fixtures to call `close_db()` rather than mutating a process-global connection.
- [ ] Run notification and user-role tests.

### Task 2: Make notification delivery/loading observable and non-stale

**Files:**
- Modify: `web_server.py`
- Modify: `frontend/index.html`
- Modify: `tests/test_notification_broadcast.py`
- Modify: `tests/test_frontend_refresh_behavior.py`

**Interfaces:**
- `/notifications` returns a private, non-cacheable JSON response.
- `refreshNotifStatus()` resolves to `true` only after the current request successfully updates notification state.

- [ ] Add a route test that broadcasts as an admin, reads `/notifications` with the same token, and asserts the published row and `Cache-Control: private, no-store`.
- [ ] Add a frontend regression test showing that a notification request failure renders a retryable loading error rather than `暂无通知`.
- [ ] Add no-store response headers to the notifications route and request notifications with `cache: 'no-store'`.
- [ ] Track notification loading state (`idle`, `loading`, `ready`, `error`) so stale/error requests never overwrite a valid list or become an empty state.
- [ ] Refresh the publisher's notification state after a successful broadcast without turning a successful publish into a failed publish when the follow-up read fails.
- [ ] Run focused route and frontend tests.

### Task 3: Correct Markdown list blocks and contain notification Markdown

**Files:**
- Modify: `frontend/index.html`
- Modify: `tests/test_frontend_refresh_behavior.py`

**Interfaces:**
- `renderMarkdown(text)` emits `<ul>` only for unordered list blocks and `<ol>` only for ordered list blocks.
- `.notif-markdown` constrains every supported rendered element to its notification container.

- [ ] Add Node regression tests for consecutive `- xxx`/`-xxx` bullets, ordered items, and mixed blocks.
- [ ] Run them and confirm current list markup contains the invalid unordered/ordered nesting.
- [ ] Parse contiguous list lines as blocks before the generic line-break transformation; do not re-wrap generated list items.
- [ ] Apply `.notif-markdown` to the publish preview and notification detail, constraining long text, code, images, tables, and lists.
- [ ] Run frontend behavior tests and inline JavaScript syntax validation.

### Task 4: Complete all requested unread indicators

**Files:**
- Modify: `frontend/index.html`
- Modify: `tests/test_frontend_refresh_behavior.py`

**Interfaces:**
- `.notification-new-tag` is the shared amber source-label-like tag for unread menu and list UI.
- `updateNotifDot()` toggles the avatar dot and `new` menu tag from `notifUnread`.

- [ ] Add tests for avatar state, menu `new` state, unread-row `new` markup, and removal after marking all items read.
- [ ] Replace the menu's numeric badge with a right-aligned `new` tag.
- [ ] Add the same tag to unread list rows and preserve existing visual unread row treatment.
- [ ] Use source badge-equivalent pill geometry, typography, amber foreground, and translucent amber background.
- [ ] Run focused frontend behavior tests.

### Task 5: Verify and commit

**Files:**
- Modify: `docs/superpowers/plans/2026-07-20-notification-debug-fixes.md`

- [ ] Run focused notification tests.
- [ ] Run `python3 -m pytest -q`.
- [ ] Run `git diff --check`.
- [ ] Extract and run `node --check` on inline JavaScript.
- [ ] When a browser runtime is available, test desktop and mobile preview overflow, list type rendering, immediate publisher visibility, and all three unread indicators.
- [ ] Commit the implementation with a focused notification-fix message.
