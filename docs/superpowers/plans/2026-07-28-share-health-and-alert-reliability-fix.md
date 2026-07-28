# Share Health and Alert Reliability Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent clients from forging shared-AI health state and make failed administrator alerts retryable.

**Architecture:** Filter server-owned fields at the HTTP boundary and split user-intent persistence from internal health-state persistence. Treat an alert as delivered only when at least one administrator receives an in-app notification; otherwise release the durable claim so a later failure can retry.

**Tech Stack:** Python 3.12, Flask, SQLite, pytest.

## Global Constraints

- Keep existing successful API response shapes compatible.
- Do not add database tables or migrations.
- Do not change failure thresholds, retry counts, or email policy.
- Use test-first red/green cycles for each behavior.

---

### Task 1: Protect Shared-AI Health State

**Files:**
- Modify: `models.py:661-809`
- Modify: `web_server.py:5183-5341`
- Test: `tests/test_share_recovery.py`

**Interfaces:**
- Produces: `set_share_health(user_id: int, **kwargs) -> dict`, an internal persistence function accepting only `share_suspended`, `share_last_check_at`, `share_last_check_ok`, and `share_last_check_error`.
- Changes: `set_user_settings(user_id: int, **kwargs) -> dict` and `set_user_settings_for_ai_config_revision(...)` accept user-intent/settings fields only.
- Consumes: `apply_share_connectivity_transition(...)` remains the revision-checked probe-result writer.

- [ ] **Step 1: Write the failing route regression test**

Add a test that creates a current-revision suspended user, submits forged health fields plus `theme_preference` to `PUT /settings`, and asserts the theme changes while suspension/check metadata do not and `share_active` remains false.

- [ ] **Step 2: Run the route regression test and verify RED**

Run:

```bash
python3 -m pytest -q tests/test_share_recovery.py::test_settings_cannot_forge_server_owned_share_health
```

Expected: FAIL because the response reports `share_active == True` or the persisted suspension/check result changes.

- [ ] **Step 3: Implement boundary filtering and internal persistence split**

In `update_settings()`, remove every server-owned derived/health key before validation:

```python
for key in (
    "share_suspended", "share_last_check_at", "share_last_check_ok",
    "share_last_check_error", "share_last_check_revision",
    "share_current_config_revision", "share_intent_revision", "share_active",
):
    data.pop(key, None)
```

Remove health keys from the two generic settings allowlists. Add `set_share_health()` for trusted callers, and use it when explicit opt-out must clear suspension. Keep probe results on `apply_share_connectivity_transition()`.

- [ ] **Step 4: Update white-box test setup to use the internal health writer**

Replace test-only setup calls that pass health keys through `set_user_settings()` with a user-intent write followed by `set_share_health()`. Do not change the assertions or route behavior under test.

- [ ] **Step 5: Run shared-state tests and verify GREEN**

Run:

```bash
python3 -m pytest -q tests/test_share_recovery.py tests/test_access_and_ui_contracts.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add models.py web_server.py tests/test_share_recovery.py tests/test_access_and_ui_contracts.py
git commit -m "fix: protect shared AI health state"
```

---

### Task 2: Retry Undelivered Administrator Alerts

**Files:**
- Modify: `web_server.py:1284-1441`
- Modify: `web_server.py:2059-2215`
- Test: `tests/test_system_ai_health_alert.py`
- Test: `tests/test_daily_summary_retry.py`

**Interfaces:**
- Changes: `_notify_user(...) -> bool` returns whether the in-app notification insert succeeded; email remains best-effort.
- Changes: `_notify_admins(...) -> int` counts successful in-app deliveries, not attempted administrators.
- Produces: `_release_system_ai_alert() -> None` and `_release_daily_summary_alert(date_str: str) -> None` clear unsuccessful claims for retry.

- [ ] **Step 1: Write failing system-AI alert retry tests**

Add a test where `_notify_admins` returns zero for the first threshold event and a positive count on the next failure. Assert the durable flag is cleared after the first attempt and notification is called again on the next failure.

- [ ] **Step 2: Run the system-AI retry test and verify RED**

Run:

```bash
python3 -m pytest -q tests/test_system_ai_health_alert.py::test_undelivered_system_ai_alert_is_retried
```

Expected: FAIL because `_system_ai_health["alerted"]` and the durable flag remain set.

- [ ] **Step 3: Implement reliable notification result reporting and system claim release**

Make `_notify_user()` return the in-app insert result while always attempting email. Count only true results in `_notify_admins()`. After a system-AI alert attempt returns zero, reset the in-memory `alerted` marker and durable state without resetting the failure streak.

- [ ] **Step 4: Run system-AI alert tests and verify GREEN**

Run:

```bash
python3 -m pytest -q tests/test_system_ai_health_alert.py
```

Expected: PASS.

- [ ] **Step 5: Write failing daily-summary alert retry test**

Add a test that drives the daily summary to `given_up`, makes the first `_notify_admins` call return zero, and asserts the database row returns to `alerted = 0` so `_claim_daily_summary_alert()` can succeed again.

- [ ] **Step 6: Run the daily-summary retry test and verify RED**

Run:

```bash
python3 -m pytest -q tests/test_daily_summary_retry.py::test_undelivered_daily_summary_alert_releases_the_claim
```

Expected: FAIL because `alerted` remains `1`.

- [ ] **Step 7: Implement daily-summary claim release**

Add a bounded SQLite update that changes `alerted` from `1` back to `0` for the date. Invoke it only when `_alert_admins_daily_summary_failure()` reports zero successful in-app deliveries.

- [ ] **Step 8: Run alert tests and verify GREEN**

Run:

```bash
python3 -m pytest -q tests/test_system_ai_health_alert.py tests/test_daily_summary_retry.py
```

Expected: PASS.

- [ ] **Step 9: Commit Task 2**

```bash
git add web_server.py tests/test_system_ai_health_alert.py tests/test_daily_summary_retry.py
git commit -m "fix: retry undelivered admin alerts"
```

---

### Task 3: Full Verification

**Files:**
- Verify all modified production and test files.

**Interfaces:**
- Consumes: all Task 1 and Task 2 behavior.
- Produces: verification evidence only.

- [ ] **Step 1: Run the complete pytest suite**

```bash
python3 -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Run syntax and whitespace checks**

```bash
python3 -m compileall -q ai_service.py fetcher.py models.py news_schema.py refresh_server.py source_categories.py web_server.py tests
python3 - <<'PY'
from bs4 import BeautifulSoup
from pathlib import Path
soup = BeautifulSoup(Path('frontend/index.html').read_text(), 'html.parser')
Path('/tmp/raynews-inline.js').write_text('\n'.join(
    script.string or script.get_text()
    for script in soup.find_all('script') if not script.get('src')
))
PY
node --check /tmp/raynews-inline.js
node --check frontend/sw.js
git diff --check HEAD~2..HEAD
```

Expected: exit code 0, no syntax or whitespace errors.

- [ ] **Step 3: Inspect final diff and repository status**

```bash
git diff --stat origin/main...HEAD
git status --short --branch
```

Expected: only intentional commits; clean working tree.
