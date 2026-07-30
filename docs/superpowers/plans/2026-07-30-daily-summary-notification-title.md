# Daily Summary Notification Title Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Change the daily-summary in-app notification title to exactly `RayNews每日摘要`.

**Architecture:** Keep the existing daily-summary delivery pipeline and replace only its title constant. Extend the existing delivery test to assert the persisted title while retaining all idempotency, Markdown, recipient, email, and failure behaviors.

**Tech Stack:** Python, unittest/pytest, SQLite models.

## Global Constraints

- The title must be exactly `RayNews每日摘要` with no date or additional whitespace.
- Change only the daily-summary in-app notification title.
- Do not change the daily-summary email subject/template, failure alerts, body, Markdown format, recipient rules, or broadcast ID.

---

### Task 1: Rename the in-app daily summary notification

**Files:**
- Modify: `web_server.py:2350`
- Modify: `tests/test_daily_summary_delivery.py:30-70`

**Interfaces:**
- Consumes: `_deliver_daily_summary_inapp(date_str: str, result: dict)`.
- Produces: notification rows whose `title` is exactly `RayNews每日摘要`.

- [ ] **Step 1: Write the failing assertion**

In `DailySummaryInAppDeliveryTests.test_delivers_to_every_user_including_those_without_a_settings_row`, add:

```python
self.assertEqual(items[0]["title"], "RayNews每日摘要")
```

- [ ] **Step 2: Verify RED**

Run:

```bash
python3 -m pytest -q tests/test_daily_summary_delivery.py::DailySummaryInAppDeliveryTests::test_delivers_to_every_user_including_those_without_a_settings_row
```

Expected: FAIL because the current title is `每日摘要已生成`.

- [ ] **Step 3: Implement the minimal change**

Set:

```python
DAILY_SUMMARY_NOTIFICATION_TITLE = "RayNews每日摘要"
```

Do not change `DAILY_SUMMARY_FAILURE_TITLE` or the email subject.

- [ ] **Step 4: Verify GREEN and regressions**

Run:

```bash
python3 -m pytest -q tests/test_daily_summary_delivery.py tests/test_daily_summary_retry.py tests/test_notifications.py
python3 -m py_compile web_server.py tests/test_daily_summary_delivery.py
git diff --check
```

Expected: all tests and static checks pass.

- [ ] **Step 5: Commit**

```bash
git add web_server.py tests/test_daily_summary_delivery.py
git commit -m "fix: rename daily summary notification"
```
