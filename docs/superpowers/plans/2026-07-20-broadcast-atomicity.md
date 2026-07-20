# Broadcast Atomicity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure a failed site-wide notification broadcast leaves no poisoned idempotency claim and can be retried with the same `broadcast_id`.

**Architecture:** A model-layer transaction writes the broadcast claim, all in-app notification rows, and the completed broadcast result together. On any fan-out failure it rolls back all of them. The route starts its best-effort email thread only after the transaction commits.

**Tech Stack:** Python 3.12, SQLite, Flask, pytest, Resend.

## Global Constraints

- Keep email best-effort and start it only after the in-app transaction commits.
- Preserve `broadcast-{broadcast_id}-{user_id}` as the Resend idempotency key.
- Do not add an outbox or dependency.
- Use a red/green test cycle.

---

### Task 1: Regression test for atomic rollback and retry

**Files:**
- Modify: `tests/test_notifications.py`
- Test: `NotificationsModelTests.test_atomic_broadcast_rolls_back_claim_and_notifications_on_fanout_failure`

**Interfaces:**
- Consumes: `models.publish_broadcast_atomically()` and `models.get_broadcast_publication()`.
- Produces: Regression coverage proving a real SQLite foreign-key failure leaves no claim or notification rows.

- [ ] **Step 1: Write the failing test**

Add `import sqlite3` beside the existing standard-library imports in `tests/test_notifications.py`, then add:

```python
def test_atomic_broadcast_rolls_back_claim_and_notifications_on_fanout_failure(self):
    broadcast_id = "rollback-retry-1"
    with self.assertRaises(sqlite3.IntegrityError):
        models.publish_broadcast_atomically(
            [self.user_a, 999999], broadcast_id, "公告", "正文", "plain", False,
        )
    self.assertIsNone(models.get_broadcast_publication(broadcast_id))
    self.assertEqual(models.list_notifications(self.user_a), [])

    is_new, result = models.publish_broadcast_atomically(
        [self.user_a, self.user_b], broadcast_id, "公告", "正文", "plain", False,
    )
    self.assertTrue(is_new)
    self.assertEqual(result, {"recipients": 2, "email": False})
    self.assertEqual([row["title"] for row in models.list_notifications(self.user_a)], ["公告"])
```

- [ ] **Step 2: Verify RED**

Run `python3 -m pytest tests/test_notification_broadcast.py::test_broadcast_retry_after_fanout_failure_reuses_same_id -q`.

Expected: FAIL with `AttributeError` because `publish_broadcast_atomically` does not exist yet.

---

### Task 2: Add an atomic persistence API

**Files:**
- Modify: `models.py:531-652`
- Test: `tests/test_notifications.py::NotificationsModelTests::test_atomic_broadcast_rolls_back_claim_and_notifications_on_fanout_failure`

**Interfaces:**
- Produces: `publish_broadcast_atomically(user_ids, broadcast_id, title, body, fmt, email) -> tuple[bool, dict]`.
- `True` means this call committed a new broadcast. The result has `recipients` and `email`.

- [ ] **Step 1: Implement the transaction**

```python
def publish_broadcast_atomically(user_ids, broadcast_id, title, body, fmt, email):
    db = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        db.execute("BEGIN")
        claimed = db.execute(
            "INSERT OR IGNORE INTO broadcast_publications "
            "(broadcast_id, title, recipients, email, created_at) VALUES (?, '', 0, 0, ?)",
            (broadcast_id, now),
        ).rowcount > 0
        if not claimed:
            row = db.execute(
                "SELECT recipients, email FROM broadcast_publications WHERE broadcast_id = ?",
                (broadcast_id,),
            ).fetchone()
            db.rollback()
            return False, {"recipients": int(row["recipients"]), "email": bool(row["email"])}
        db.executemany(
            "INSERT INTO notifications (user_id, type, title, body, format, created_at) "
            "VALUES (?, 'admin_broadcast', ?, ?, ?, ?)",
            [(uid, title, body, fmt, now) for uid in user_ids],
        )
        db.execute(
            "UPDATE broadcast_publications SET title = ?, recipients = ?, email = ? "
            "WHERE broadcast_id = ?",
            (title, len(user_ids), int(email), broadcast_id),
        )
        db.commit()
        return True, {"recipients": len(user_ids), "email": bool(email)}
    except Exception:
        db.rollback()
        raise
```

Remove the obsolete split claim/finalize calls when no callers remain.

- [ ] **Step 2: Verify GREEN**

Run `python3 -m pytest tests/test_notifications.py::NotificationsModelTests::test_atomic_broadcast_rolls_back_claim_and_notifications_on_fanout_failure -q`.

Expected: PASS; the invalid foreign key rolls back claim and notifications, then the valid retry succeeds.

- [ ] **Step 3: Run focused tests**

Run `python3 -m pytest tests/test_notifications.py tests/test_notification_broadcast.py -q`.

Expected: PASS.

---

### Task 3: Route after the atomic commit

**Files:**
- Modify: `web_server.py:691-785`
- Test: `tests/test_notification_broadcast.py`

**Interfaces:**
- Consumes: `publish_broadcast_atomically(...)`.
- Produces: Existing response shape; `replayed: true` only after an earlier completed transaction.

- [ ] **Step 1: Replace claim/finalize calls**

```python
user_ids = [u["id"] for u in list_users()]
is_new, result = publish_broadcast_atomically(
    user_ids, broadcast_id, title, body, fmt, do_email,
)
if is_new and do_email and user_ids:
    threading.Thread(
        target=_broadcast_notification_emails,
        args=(broadcast_id, user_ids, title, body),
        daemon=True,
    ).start()
response = {"ok": True, **result}
if not is_new:
    response["replayed"] = True
return jsonify(response)
```

Import `publish_broadcast_atomically`; remove unused split claim/finalize imports.

- [ ] **Step 2: Verify route tests**

Run `python3 -m pytest tests/test_notification_broadcast.py -q`.

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add models.py web_server.py tests/test_notification_broadcast.py
git commit -m "fix: make notification broadcasts atomic"
```

---

### Task 4: Full verification

**Files:** Verify all touched files.

- [ ] **Step 1: Run all tests**

```bash
python3 -m pytest -q
```

Expected: exit code 0.

- [ ] **Step 2: Run hygiene and frontend syntax checks**

```bash
git diff --check
python3 - <<'PY'
from bs4 import BeautifulSoup
from pathlib import Path
soup = BeautifulSoup(Path('frontend/index.html').read_text(), 'html.parser')
for i, tag in enumerate(soup.find_all('script')):
    if not tag.get('src'):
        Path(f'/tmp/raynews-broadcast-{i}.js').write_text(tag.string or tag.get_text())
PY
for file in /tmp/raynews-broadcast-*.js; do node --check "$file"; done
```

Expected: exit code 0.
