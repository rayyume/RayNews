# Personal AI Share Revalidation Failure Threshold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep sharing active after one scheduled personal-AI probe failure, suspend and notify after the second consecutive scheduled failure, while preserving immediate suspension for user-initiated failures.

**Architecture:** Persist the scheduled-probe streak and its config revision in `user_settings`. A transaction in `models.py` owns each scheduled failure edge, while the existing config-revision CAS continues to own manual failure and all success transitions. `web_server.py` selects the scheduled or immediate policy explicitly; the frontend only renders the persisted `1/2` warning.

**Tech Stack:** Python 3.12, Flask, SQLite, native browser JavaScript, pytest, Node-based frontend behavior tests.

## Global Constraints

- The scheduled background threshold is exactly two consecutive failed cycles and is not configurable.
- Saving/replacing a personal API, clicking “test connection”, and enabling/reconfirming sharing remain immediate-failure paths.
- Any successful current-revision probe clears the scheduled failure streak.
- Streaks are scoped to `ai_configs.revision`; stale probes cannot mutate health or notify.
- The first scheduled failure preserves `share_last_check_*`, `share_suspended=0`, and `is_share_active() == True`.
- Existing share intent and view toggles survive suspension.
- Do not delete historical `ai_results`.
- Provider errors must pass through `_compact_share_error()` before persistence, response, or notification.
- Preserve the unrelated working-tree changes in `source_categories.py` and `tests/test_source_maintenance.py`.

---

### Task 1: Persist and atomically claim scheduled failure edges

**Files:**
- Modify: `models.py:50-75`
- Modify: `models.py:195-240`
- Modify: `models.py:870-905`
- Modify: `models.py:995-1145`
- Test: `tests/test_share_recovery.py`

**Interfaces:**
- Produces: `record_share_revalidation_failure(user_id: int, config_revision: int, checked_at: str, error: str, threshold: int = 2) -> str`
- Produces: persisted fields `share_revalidation_failure_streak`, `share_revalidation_failure_revision`, `share_revalidation_last_failure_at`, and `share_revalidation_last_failure_error`
- Preserves: `apply_share_connectivity_transition(...) -> bool`, extended so every applied manual/success result clears the scheduled streak

- [ ] **Step 1: Write failing schema and persistence tests**

Add tests that use the real SQLite database:

```python
def test_share_revalidation_failure_fields_migrate_and_round_trip(share_env):
    _, user_id = share_env
    settings = models.get_user_settings(user_id)
    assert settings["share_revalidation_failure_streak"] == 0
    assert settings["share_revalidation_failure_revision"] is None
    assert settings["share_revalidation_last_failure_at"] is None
    assert settings["share_revalidation_last_failure_error"] is None


def test_first_scheduled_failure_is_durable_without_revoking_access(share_env):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)

    result = models.record_share_revalidation_failure(
        user_id,
        revision,
        "2026-07-30T10:00:00",
        "AI API HTTP 503",
    )

    assert result == "pending_failure"
    settings = models.get_user_settings(user_id)
    assert settings["share_revalidation_failure_streak"] == 1
    assert settings["share_revalidation_failure_revision"] == revision
    assert settings["share_suspended"] == 0
    assert settings["share_last_check_ok"] == 1
    assert web_server.is_share_active(settings) is True


def test_second_scheduled_failure_atomically_suspends(share_env):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    models.record_share_revalidation_failure(
        user_id, revision, "2026-07-30T10:00:00", "AI API HTTP 503"
    )

    result = models.record_share_revalidation_failure(
        user_id, revision, "2026-07-30T11:00:00", "AI API HTTP 503"
    )

    assert result == "suspended"
    settings = models.get_user_settings(user_id)
    assert settings["share_revalidation_failure_streak"] == 2
    assert settings["share_suspended"] == 1
    assert settings["share_last_check_ok"] == 0
    assert settings["share_last_check_revision"] == revision
    assert web_server.is_share_active(settings) is False
```

Also add cases asserting an opt-out returns `not_opted_in`, a mismatched revision returns `stale`, and a first failure for a new revision starts at one instead of inheriting the old revision's streak.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python3 -m pytest -q \
  tests/test_share_recovery.py::test_share_revalidation_failure_fields_migrate_and_round_trip \
  tests/test_share_recovery.py::test_first_scheduled_failure_is_durable_without_revoking_access \
  tests/test_share_recovery.py::test_second_scheduled_failure_atomically_suspends
```

Expected: failures for missing fields/function, not fixture or syntax errors.

- [ ] **Step 3: Add schema columns and settings serialization**

Add the four columns to both `SCHEMA_SQL` and the `_add_column_if_missing` migration tuple. Extend `get_user_settings()` to select them:

```python
"s.share_revalidation_failure_streak, "
"s.share_revalidation_failure_revision, "
"s.share_revalidation_last_failure_at, "
"s.share_revalidation_last_failure_error, "
```

Extend `set_share_health()`'s server-owned allowlist with the four names so opt-out and controlled recovery paths can reset them without accepting them from user payloads.

- [ ] **Step 4: Implement the atomic scheduled-failure claim**

Add `record_share_revalidation_failure()` next to `apply_share_connectivity_transition()`. Use `BEGIN IMMEDIATE`, read the joined current config revision and current persisted streak, and return only one of:

```python
{"pending_failure", "suspended", "unchanged", "not_opted_in", "stale"}
```

The transaction must:

```python
prior_streak = (
    int(row["share_revalidation_failure_streak"] or 0)
    if row["share_revalidation_failure_revision"] == config_revision
    else 0
)
next_streak = prior_streak + 1
```

For `next_streak < threshold`, update only the four revalidation fields. For the threshold edge, update those fields plus:

```sql
share_suspended = 1,
share_last_check_at = ?,
share_last_check_ok = 0,
share_last_check_error = ?,
share_last_check_revision = ?
```

If already suspended, return `unchanged` without creating another edge. Roll back on early `not_opted_in`/`stale`, commit successful updates, and roll back then re-raise on database errors.

- [ ] **Step 5: Clear scheduled failures on applied manual/success results**

Extend the `SET` clause in `apply_share_connectivity_transition()`:

```sql
share_revalidation_failure_streak = 0,
share_revalidation_failure_revision = NULL,
share_revalidation_last_failure_at = NULL,
share_revalidation_last_failure_error = NULL
```

Because the existing `WHERE` clause checks sharing intent, suspension state, and current config revision, a stale or opted-out probe still cannot clear the current streak.

- [ ] **Step 6: Run Task 1 tests and verify GREEN**

Run:

```bash
python3 -m pytest -q tests/test_share_recovery.py
```

Expected: all share-recovery tests pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add models.py tests/test_share_recovery.py
git commit -m "feat: persist share revalidation failure streaks"
```

---

### Task 2: Apply two-strike policy only to scheduled probes

**Files:**
- Modify: `web_server.py:1680-1815`
- Modify: `web_server.py:5450-5690`
- Test: `tests/test_share_recovery.py`
- Test: `tests/test_share_suspension_notice_delivery.py`

**Interfaces:**
- Consumes: `models.record_share_revalidation_failure(...)`
- Produces: `_apply_share_background_connectivity_result(user_id: int, ok: bool, error: str = "", checked_at: str | None = None, config_revision: int | None = None) -> str`
- Preserves: `_apply_share_connectivity_result(...)` as the immediate/manual path

- [ ] **Step 1: Write failing scheduled-policy tests**

Add an end-to-end periodic-loop test:

```python
def test_periodic_failure_needs_two_consecutive_cycles(share_env, monkeypatch):
    _, user_id = share_env
    revision = _validated_opted_in(user_id)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"error": "AI API HTTP 503"}, 502),
    )
    notices = []
    monkeypatch.setattr(
        web_server, "_notify_user", lambda *args, **kwargs: notices.append(args)
    )

    web_server._run_ai_share_revalidation_once()
    first = models.get_user_settings(user_id)
    assert first["share_revalidation_failure_streak"] == 1
    assert first["share_suspended"] == 0
    assert web_server.is_share_active(first) is True
    assert notices == []

    web_server._run_ai_share_revalidation_once()
    second = models.get_user_settings(user_id)
    assert second["share_revalidation_failure_streak"] == 2
    assert second["share_suspended"] == 1
    assert second["share_last_check_revision"] == revision
    assert [notice[1] for notice in notices] == ["share_suspended"]
```

Add tests for failure-success-failure resetting to `1/2`, manual failure after a pending scheduled failure suspending immediately, manual success clearing the pending state, and stale background results leaving current state untouched.

- [ ] **Step 2: Run the new scheduled-policy tests and verify RED**

Run the new named tests with `python3 -m pytest -q ...`.

Expected: the first periodic failure currently suspends and notifies, proving the regression.

- [ ] **Step 3: Extract one shared suspension notifier**

Extract the existing suspension `_notify_user(...)` body from `_apply_share_connectivity_result()`:

```python
def _notify_share_suspended(user_id: int, safe_error: str) -> None:
    _notify_user(
        user_id,
        "share_suspended",
        "共享 API 校验失败，共享已暂停",
        "系统对你配置的个人 AI API 做连通性校验时失败，共享访问已暂停；"
        "你的总开关和查看选项均已保留。\n\n"
        f"失败原因：{safe_error}\n\n"
        "请到 用户设置 → AI 更新配置。保存并校验成功后系统会自动恢复共享。",
    )
```

Keep manual behavior identical by calling this helper only when the existing immediate transition returns the real active-to-suspended edge.

- [ ] **Step 4: Implement the scheduled-policy wrapper**

Implement `_apply_share_background_connectivity_result()`:

```python
if ok:
    return _apply_share_connectivity_result(
        user_id,
        True,
        checked_at=checked_at,
        config_revision=config_revision,
    )

safe_error = _compact_share_error(error)
transition = record_share_revalidation_failure(
    user_id,
    config_revision,
    checked_at,
    safe_error,
    threshold=2,
)
if transition == "suspended":
    _notify_share_suspended(user_id, safe_error)
return transition
```

Normalize/validate `config_revision` and default `checked_at` exactly as the immediate helper does. Do not call the immediate failure helper for the first scheduled failure.

Update `_run_ai_share_revalidation_once()` to call this scheduled wrapper. Leave `/ai/test-connection`, `/ai/config`, and `/settings` on `_apply_share_connectivity_result()`.

- [ ] **Step 5: Reset pending state when the user explicitly opts out**

When `clear_share_suspension` is handled in `update_settings()`, call:

```python
set_share_health(
    g.user_id,
    share_suspended=0,
    share_revalidation_failure_streak=0,
    share_revalidation_failure_revision=None,
    share_revalidation_last_failure_at=None,
    share_revalidation_last_failure_error=None,
)
```

This prevents an old `1/2` warning from surviving an opt-out/re-enable cycle.

- [ ] **Step 6: Add delivery and concurrency coverage**

In `tests/test_share_suspension_notice_delivery.py`, drive the real periodic loop twice and assert no delivery after the first cycle and one in-app plus one email after the second.

In `tests/test_share_recovery.py`, run two simultaneous second failures after a persisted first failure. Assert:

```python
assert sorted(results) == ["suspended", "unchanged"]
assert [notice[1] for notice in notices] == ["share_suspended"]
```

Use the existing `_run_concurrent_connectivity_results` pattern and separate thread-local model connections.

- [ ] **Step 7: Run backend share tests and verify GREEN**

Run:

```bash
python3 -m pytest -q \
  tests/test_share_recovery.py \
  tests/test_share_suspension_notice_delivery.py \
  tests/test_ai_endpoint_validation.py
```

Expected: all selected tests pass with no new warnings.

- [ ] **Step 8: Commit Task 2**

```bash
git add web_server.py tests/test_share_recovery.py tests/test_share_suspension_notice_delivery.py
git commit -m "feat: require two scheduled AI share failures"
```

---

### Task 3: Expose the pending warning and document behavior

**Files:**
- Modify: `web_server.py:5450-5485`
- Modify: `frontend/index.html:3290-3360`
- Modify: `frontend/index.html:3705-3740`
- Modify: `README.md:55,232`
- Modify: `README.en.md:55`
- Test: `tests/test_ai_relay_frontend.py`
- Test: `tests/test_access_and_ui_contracts.py`

**Interfaces:**
- Consumes: safe settings fields `share_revalidation_failure_streak`, `share_revalidation_last_failure_at`, `share_revalidation_last_failure_error`
- Produces: reader-facing `1/2` warning while `share_active` remains true

- [ ] **Step 1: Write failing API/frontend contract tests**

Add a settings-response test asserting the three safe fields are returned but `share_revalidation_failure_revision` is removed.

Add a frontend source/Node test that supplies:

```javascript
{
  share_ai_results: true,
  share_active: true,
  share_suspended: false,
  share_revalidation_failure_streak: 1,
  share_revalidation_last_failure_at: '2026-07-30T10:00:00',
  share_revalidation_last_failure_error: 'AI API HTTP 503'
}
```

Then call `loadShareTab()` and assert `shareCheckStatus.textContent` contains `后台复核暂时失败（1/2）`, `共享仍在运行`, and the sanitized error.

- [ ] **Step 2: Run the new API/frontend tests and verify RED**

Run:

```bash
python3 -m pytest -q \
  tests/test_ai_relay_frontend.py \
  tests/test_access_and_ui_contracts.py
```

Expected: failures because the response and UI do not yet carry/render the streak.

- [ ] **Step 3: Serialize safe pending-failure fields**

In `_settings_response()`, keep streak/time/error and remove only:

```python
safe.pop("share_revalidation_failure_revision", None)
```

Set defaults for the three safe fields so accounts without a settings row return stable types.

- [ ] **Step 4: Store and render the warning in the frontend**

Extend `loadUserSettings()`:

```javascript
share_revalidation_failure_streak:
  Number(data.share_revalidation_failure_streak || 0),
share_revalidation_last_failure_at:
  data.share_revalidation_last_failure_at || null,
share_revalidation_last_failure_error:
  data.share_revalidation_last_failure_error || null,
```

In `loadShareTab()`, place the pending branch after `share_suspended` and before the normal last-check branches:

```javascript
} else if (Number(s.share_revalidation_failure_streak || 0) === 1) {
  statusEl.textContent =
    '⚠️ 后台复核暂时失败（1/2），共享仍在运行；'
    + '下次复核成功将自动清除，连续失败才会暂停。'
    + (s.share_revalidation_last_failure_error
      ? ' 原因：' + s.share_revalidation_last_failure_error
      : '');
  statusEl.style.color = '#e5a84d';
```

- [ ] **Step 5: Update README behavior**

Change the Chinese and English user-AI descriptions to say:

- scheduled revalidation defaults to hourly;
- one scheduled failure is tolerated;
- two consecutive scheduled failures suspend and notify;
- user-initiated validation failures suspend immediately;
- success clears the streak and restores a suspended share.

Keep the environment-variable table unchanged because the interval semantics do not change.

- [ ] **Step 6: Run Task 3 tests and verify GREEN**

Run:

```bash
python3 -m pytest -q \
  tests/test_ai_relay_frontend.py \
  tests/test_access_and_ui_contracts.py
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add web_server.py frontend/index.html README.md README.en.md \
  tests/test_ai_relay_frontend.py tests/test_access_and_ui_contracts.py
git commit -m "feat: show pending AI share revalidation failures"
```

---

### Task 4: Full regression and deployment verification

**Files:**
- Verify only; do not introduce unrelated refactors

**Interfaces:**
- Consumes: all behavior implemented by Tasks 1–3
- Produces: fresh verification evidence for completion

- [ ] **Step 1: Run formatting/diff checks**

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only intentional changes plus the pre-existing source-query work are present.

- [ ] **Step 2: Run the full test suite**

```bash
python3 -m pytest -q
```

Expected: zero failures. Existing `datetime.utcnow()` deprecation warnings may remain; no new warnings are acceptable.

- [ ] **Step 3: Review the state-machine diff**

```bash
git diff HEAD~3 -- models.py web_server.py frontend/index.html \
  tests/test_share_recovery.py tests/test_share_suspension_notice_delivery.py \
  tests/test_ai_relay_frontend.py tests/test_access_and_ui_contracts.py \
  README.md README.en.md
```

Verify explicitly:

- periodic loop calls only the scheduled wrapper;
- all three user-initiated paths still call the immediate helper;
- first scheduled failure cannot alter `share_last_check_ok`;
- threshold suspension commits before notification;
- stale revision and opt-out conditions precede every write;
- `/settings` never exposes a config revision or unsafe provider text.

- [ ] **Step 4: Run a deterministic two-cycle smoke check**

Using a temporary test database and monkeypatched `_run_ai_connection_test`, execute failure/failure, failure/success/failure, and manual-failure sequences. Confirm the observable states match the design without making real provider calls or sending real email.

- [ ] **Step 5: Record deployment checks in the completion report**

After rebuilding the container:

1. Confirm `PRAGMA table_info(user_settings)` contains all four new columns.
2. Force one scheduled failure and verify `/settings` reports streak `1`, active sharing, and no notification.
3. Force the second scheduled failure and verify suspension plus exactly one notification.
4. Restore the provider and verify the next scheduled/manual success clears the streak and restores sharing.

