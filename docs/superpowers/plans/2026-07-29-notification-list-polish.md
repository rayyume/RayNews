# Notification List Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display compact browser-local notification dates and replace each unread row's `new` tag with an interactive unread/read status button.

**Architecture:** Keep notification timestamps and read state unchanged. Add a pure browser-local formatter in the existing notification script; render an unread-only button that delegates to the existing `markNotifRead(id)` optimistic update flow. CSS changes make the button amber with `未读` at rest and green with `已读` on hover/focus, while retaining the date on mobile.

**Tech Stack:** Vanilla JavaScript, CSS, existing Python/Node frontend contract tests (`pytest`).

## Global Constraints

- Use the browser's local `Date` interpretation for relative-day labels.
- Do not modify notification API responses, data schema, read/delete endpoints, or detail-page behavior.
- Preserve `markNotifRead()` concurrency protection and rollback behavior.
- Remove `new` only from notification list rows; retain the menu unread badge and avatar dot.
- Do not hide `.notif-time` on narrow viewports.

---

### Task 1: Compact timestamps and unread-state action

**Files:**
- Modify: `frontend/index.html:428-439,564,4884-5070`
- Modify: `tests/test_frontend_refresh_behavior.py:784-829`

**Interfaces:**
- Consumes: notification objects with `created_at`, `read_at`, `id`, and the existing `markNotifRead(id)` function.
- Produces: `formatNotifListTime(iso, now = new Date()) -> string` and unread row markup containing `.notif-unread-action`.

- [ ] **Step 1: Write failing frontend contract tests**

Add one Node-backed test for browser-local timestamp labels and one markup/CSS contract test:

```python
def test_notification_list_uses_compact_browser_local_dates_and_keeps_time_on_mobile():
    notification_source = source_between(
        "function formatNotifTime(",
        "function renderNotifList()",
    )
    run_node(
        notification_source,
        """
const now = new Date(2026, 6, 29, 12, 0);
assert.equal(context.formatNotifListTime('2026-07-29T09:05:00', now), '今天 09:05');
assert.equal(context.formatNotifListTime('2026-07-28T09:05:00', now), '昨天 09:05');
assert.equal(context.formatNotifListTime('2026-07-27T09:05:00', now), '前天 09:05');
assert.equal(context.formatNotifListTime('2026-07-26T09:05:00', now), '7-26 09:05');
""",
    )
    mobile_css = HTML[HTML.index('@media(max-width:640px){'):HTML.index('</style>')]
    assert '.notif-time{display:none}' not in mobile_css


def test_unread_notification_uses_hoverable_status_action_not_new_tag():
    assert 'class="notif-unread-action"' in HTML
    assert 'markNotifRead(${n.id})' in HTML
    assert '<span class="notification-new-tag">new</span>' not in source_between(
        "function renderNotifList()",
        "function retryNotifications()",
    )
    assert '.notif-unread-action::before{content:\'未读\'' in HTML
    assert '.notif-unread-action:hover::before,.notif-unread-action:focus-visible::before{content:\'已读\'' in HTML
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
python3 -m pytest -q tests/test_frontend_refresh_behavior.py -k 'notification_list_uses_compact or unread_notification_uses_hoverable'
```

Expected: FAIL because `formatNotifListTime` and `.notif-unread-action` do not exist; the mobile rule still hides time and list markup still renders the `new` tag.

- [ ] **Step 3: Implement the minimal list formatter and row markup**

In `frontend/index.html`, add a local-calendar formatter adjacent to `formatNotifTime`:

```javascript
function formatNotifListTime(iso, now = new Date()) {
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return formatNotifTime(iso);
  const day = new Date(value.getFullYear(), value.getMonth(), value.getDate());
  const currentDay = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diffDays = Math.round((currentDay - day) / 86400000);
  const hhmm = `${String(value.getHours()).padStart(2, '0')}:${String(value.getMinutes()).padStart(2, '0')}`;
  if (diffDays === 0) return `今天 ${hhmm}`;
  if (diffDays === 1) return `昨天 ${hhmm}`;
  if (diffDays === 2) return `前天 ${hhmm}`;
  return `${value.getMonth() + 1}-${value.getDate()} ${hhmm}`;
}
```

Render `formatNotifListTime(n.created_at)` for `.notif-time`. For unread rows, replace the list-only `notification-new-tag` and the old read button with one button:

```html
<button class="notif-btn notif-unread-action" aria-label="标记为已读"
  onclick="event.stopPropagation();markNotifRead(${n.id})"></button>
```

Keep the existing delete button, `markNotifRead()` invocation, and all read/delete behavior unchanged.

- [ ] **Step 4: Implement the visual states and mobile date visibility**

Replace the old `.read-btn` hover styling with `.notif-unread-action` rules using the same amber translucent background/text as `.notification-new-tag`; use `::before` for the rest/hover/focus labels so the visual copy changes without changing the action semantics:

```css
.notif-unread-action{background:rgba(251,191,36,0.12);border-color:rgba(251,191,36,0.28);color:#fbbf24}
.notif-unread-action::before{content:'未读'}
.notif-unread-action:hover,.notif-unread-action:focus-visible{background:rgba(34,197,94,0.12);border-color:#22c55e;color:#22c55e}
.notif-unread-action:hover::before,.notif-unread-action:focus-visible::before{content:'已读'}
```

Remove the mobile `.notif-time{display:none}` declaration. Preserve the existing title `flex:1; min-width:0` truncation and button `flex-shrink:0` behavior so titles use remaining width.

- [ ] **Step 5: Run GREEN and relevant regression tests**

Run:

```bash
python3 -m pytest -q tests/test_frontend_refresh_behavior.py tests/test_notification_actions.py tests/test_notifications.py
python3 -m py_compile tests/test_frontend_refresh_behavior.py
```

Expected: PASS. Verify that existing optimistic read tests continue to cover click behavior and that notification detail timestamps still use `formatNotifTime()`.

- [ ] **Step 6: Commit**

```bash
git add frontend/index.html tests/test_frontend_refresh_behavior.py
git commit -m "fix: polish notification list read state"
```
