# Shared AI Pause and Auto-Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve a user's sharing choices when their personal AI API fails, suspend shared-result access safely, and automatically restore the exact choices after connectivity returns.

**Architecture:** Treat existing sharing switches as durable user intent and add one runtime suspension flag. Funnel scheduled checks, saved-config checks, and manual connection tests through one transition function that owns state changes and one-shot pause/recovery notifications.

**Tech Stack:** Flask, SQLite migrations, Python service helpers, vanilla JavaScript settings UI, pytest Flask client/model tests.

## Global Constraints

- Work on `dev`; line numbers refer to `dev@7fa5b56`.
- API failure must not clear `share_ai_results` or any `share_view_*` preference.
- Suspended users must have no effective access to shared title, translation, or summary results.
- Pause and recovery each notify once per state transition through in-app notification and email.
- Saving a new API, manual API testing, and the six-hour background check all use the same transition function.
- A user who explicitly turns sharing off must never be auto-restored.
- Never expose API keys, provider response bodies, stack traces, or internal paths in settings or notifications.

---

## File Structure

- Modify: `models.py` — `share_suspended` migration, selectors, allowed settings.
- Modify: `web_server.py` — effective-access helper, connectivity transition, route integration, notification copy.
- Modify: `frontend/index.html` — paused UI and effective title gate.
- Create: `tests/test_share_recovery.py` — model, transition, route, notification, and access regression tests.
- Modify: `tests/test_ai_relay_frontend.py` — shared-result UI gate compatibility if existing fixtures require the new field.

### Task 1: Add durable suspension state and an effective-access helper

**Files:**
- Modify: `models.py:47-65`
- Modify: `models.py:145-163`
- Modify: `models.py:518-558`
- Modify: `web_server.py:3490-3506`
- Create: `tests/test_share_recovery.py`

**Interfaces:**
- Produces setting: `share_suspended: int`
- Produces: `is_share_active(settings: dict | None) -> bool`
- Extends: `get_user_settings()`, `set_user_settings()`
- Consumes: existing `share_ai_results`, `share_last_check_ok`

- [ ] **Step 1: Create a temporary-database fixture and failing migration test**

Create `tests/test_share_recovery.py`:

```python
import os
import uuid
from pathlib import Path

import pytest

import models
import web_server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def share_env(monkeypatch):
    db_path = ROOT / f"tmp-share-recovery-{uuid.uuid4().hex}.db"
    old_db_file = models.DB_FILE
    models.close_db()
    models.DB_FILE = db_path
    models.get_db()
    user = models.create_user("share@example.com", "pw", "share-user")
    client = web_server.app.test_client()
    try:
        yield client, user["id"]
    finally:
        models.close_db()
        models.DB_FILE = old_db_file
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(str(db_path) + suffix)
            except FileNotFoundError:
                pass


def auth_headers(user_id: int, role: str = "user") -> dict:
    return {"Authorization": f"Bearer {web_server.create_token(user_id, role)}"}


def test_share_suspended_defaults_false_and_round_trips(share_env):
    _, user_id = share_env
    settings = models.set_user_settings(
        user_id,
        share_ai_results=1,
        share_view_title=1,
        share_last_check_ok=1,
    )
    assert settings["share_suspended"] == 0

    settings = models.set_user_settings(user_id, share_suspended=1)
    assert settings["share_suspended"] == 1
    assert settings["share_ai_results"] == 1
    assert settings["share_view_title"] == 1
```

- [ ] **Step 2: Add failing effective-access tests**

Append:

```python
@pytest.mark.parametrize(
    ("settings", "expected"),
    (
        (None, False),
        ({}, False),
        ({"share_ai_results": 1, "share_suspended": 0, "share_last_check_ok": 1}, True),
        ({"share_ai_results": 1, "share_suspended": 1, "share_last_check_ok": 1}, False),
        ({"share_ai_results": 1, "share_suspended": 0, "share_last_check_ok": 0}, False),
        ({"share_ai_results": 0, "share_suspended": 0, "share_last_check_ok": 1}, False),
    ),
)
def test_is_share_active_requires_intent_health_and_no_suspension(settings, expected):
    assert web_server.is_share_active(settings) is expected
```

- [ ] **Step 3: Run tests and verify schema/helper failures**

Run:

```bash
python3 -m pytest -q tests/test_share_recovery.py
```

Expected: FAIL because `share_suspended` and `is_share_active()` do not exist.

- [ ] **Step 4: Add the schema and migration**

Add to the `user_settings` DDL:

```sql
share_suspended         INTEGER NOT NULL DEFAULT 0,
```

Add to the migration tuple:

```python
"ALTER TABLE user_settings ADD COLUMN share_suspended INTEGER NOT NULL DEFAULT 0",
```

Include `share_suspended` in:

- `get_user_settings()` SELECT;
- `set_user_settings()` allowed fields;
- any default settings dictionary returned by `GET /settings`.

- [ ] **Step 5: Add the central effective-access helper**

Near the connectivity helpers in `web_server.py`:

```python
def is_share_active(settings: dict | None) -> bool:
    settings = settings or {}
    return (
        _is_enabled_value(settings.get("share_ai_results"))
        and not _is_enabled_value(settings.get("share_suspended"))
        and _is_enabled_value(settings.get("share_last_check_ok"))
    )
```

Move `_is_enabled_value()` above this helper or keep Python runtime ordering valid by ensuring `is_share_active()` is not called before module initialization completes.

- [ ] **Step 6: Gate shared summary/translation results**

Change `ai_get_result()`:

```python
settings = get_user_settings(g.user_id) or {}
share_active = is_share_active(settings)
if not share_active or not settings.get("share_view_summary"):
    cached.pop("summary", None)
    cached.pop("summary_error", None)
    cached.pop("summary_error_at", None)
if not share_active or not settings.get("share_view_translation"):
    cached.pop("translation", None)
```

- [ ] **Step 7: Run the new tests**

Run:

```bash
python3 -m pytest -q tests/test_share_recovery.py
```

Expected: PASS for migration and helper cases.

- [ ] **Step 8: Commit the state model**

```bash
git add models.py web_server.py tests/test_share_recovery.py
git commit -m "refactor(share): separate user intent from suspension"
```

### Task 2: Centralize pause/recovery transitions and notifications

**Files:**
- Modify: `web_server.py:1020-1153`
- Modify: `tests/test_share_recovery.py`

**Interfaces:**
- Produces: `_apply_share_connectivity_result(user_id: int, ok: bool, error: str = "", checked_at: str | None = None) -> str`
- Return values: `"suspended"`, `"restored"`, `"unchanged"`, `"not_opted_in"`
- Consumes: `get_user_settings()`, `set_user_settings()`, `_notify_user()`

- [ ] **Step 1: Add failing transition tests**

Append:

```python
def opted_in(user_id: int, *, suspended: int = 0):
    return models.set_user_settings(
        user_id,
        share_ai_results=1,
        share_view_title=1,
        share_view_translation=0,
        share_view_summary=1,
        share_suspended=suspended,
        share_last_check_ok=0 if suspended else 1,
    )


def test_failed_check_suspends_without_clearing_preferences(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    result = web_server._apply_share_connectivity_result(
        user_id, False, "AI API HTTP 401", "2026-07-27T10:00:00"
    )

    settings = models.get_user_settings(user_id)
    assert result == "suspended"
    assert settings["share_ai_results"] == 1
    assert settings["share_view_title"] == 1
    assert settings["share_view_translation"] == 0
    assert settings["share_view_summary"] == 1
    assert settings["share_suspended"] == 1
    assert settings["share_last_check_ok"] == 0
    assert len(notices) == 1
    assert notices[0][1] == "share_suspended"


def test_repeated_failure_updates_health_without_duplicate_notice(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    result = web_server._apply_share_connectivity_result(
        user_id, False, "still unavailable", "2026-07-27T11:00:00"
    )

    assert result == "unchanged"
    assert notices == []


def test_success_restores_exact_preferences_once(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    result = web_server._apply_share_connectivity_result(
        user_id, True, checked_at="2026-07-27T12:00:00"
    )

    settings = models.get_user_settings(user_id)
    assert result == "restored"
    assert settings["share_suspended"] == 0
    assert settings["share_view_title"] == 1
    assert settings["share_view_translation"] == 0
    assert settings["share_view_summary"] == 1
    assert settings["share_last_check_ok"] == 1
    assert len(notices) == 1
    assert notices[0][1] == "share_restored"

    assert web_server._apply_share_connectivity_result(
        user_id, True, checked_at="2026-07-27T12:01:00"
    ) == "unchanged"
    assert len(notices) == 1


def test_explicitly_disabled_user_is_never_auto_restored(share_env, monkeypatch):
    _, user_id = share_env
    models.set_user_settings(
        user_id,
        share_ai_results=0,
        share_suspended=0,
        share_last_check_ok=0,
    )
    notices = []
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: notices.append(args))

    result = web_server._apply_share_connectivity_result(user_id, True)

    assert result == "not_opted_in"
    assert models.get_user_settings(user_id)["share_ai_results"] == 0
    assert notices == []
```

- [ ] **Step 2: Run transition tests and verify missing helper**

Run:

```bash
python3 -m pytest -q tests/test_share_recovery.py -k "failed_check or repeated_failure or success_restores or explicitly_disabled"
```

Expected: FAIL because `_apply_share_connectivity_result()` does not exist.

- [ ] **Step 3: Implement the transition function**

Add:

```python
def _apply_share_connectivity_result(
    user_id: int,
    ok: bool,
    error: str = "",
    checked_at: str | None = None,
) -> str:
    import datetime as _dt

    settings = get_user_settings(user_id) or {}
    if not _is_enabled_value(settings.get("share_ai_results")):
        return "not_opted_in"

    checked_at = checked_at or _dt.datetime.now().isoformat(timespec="seconds")
    was_suspended = _is_enabled_value(settings.get("share_suspended"))
    if ok:
        set_user_settings(
            user_id,
            share_suspended=0,
            share_last_check_at=checked_at,
            share_last_check_ok=1,
            share_last_check_error=None,
        )
        if not was_suspended:
            return "unchanged"
        _notify_user(
            user_id,
            "share_restored",
            "共享 API 已恢复，共享状态已自动恢复",
            "系统已确认你的个人 AI API 恢复连通。\n\n"
            "「共享 AI 结果」及你此前选择的查看选项已自动恢复，无需手动重新开启。",
        )
        return "restored"

    safe_error = _compact_share_error(error)
    set_user_settings(
        user_id,
        share_suspended=1,
        share_last_check_at=checked_at,
        share_last_check_ok=0,
        share_last_check_error=safe_error,
    )
    if was_suspended:
        return "unchanged"
    _notify_user(
        user_id,
        "share_suspended",
        "共享 API 校验失败，共享已暂停",
        "系统对你配置的个人 AI API 做连通性校验时失败，共享访问已暂停；"
        "你的总开关和查看选项均已保留。\n\n"
        f"失败原因：{safe_error}\n\n"
        "请到 用户设置 → AI 更新配置。保存并校验成功后系统会自动恢复共享。",
    )
    return "suspended"
```

Add a bounded sanitizer:

```python
def _compact_share_error(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "connection test failed")).strip()
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
    return text[:300]
```

- [ ] **Step 4: Replace the periodic loop's destructive update**

In `_run_ai_share_revalidation_once()` replace both success/failure `set_user_settings()` branches and the old revoked notification with:

```python
body, status = _run_ai_connection_test(config)
_apply_share_connectivity_result(
    user_id,
    status == 200,
    body.get("error", "") if status != 200 else "",
)
```

Keep the existing 0.5-second spacing. Update the docstring to say opted-in suspended users continue to be checked.

- [ ] **Step 5: Prove suspended users remain in the revalidation set**

Append:

```python
def test_suspended_opted_in_user_remains_scheduled_for_revalidation(share_env):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    assert user_id in models.get_users_with_share_enabled()


def test_periodic_revalidation_restores_suspended_user(share_env, monkeypatch):
    _, user_id = share_env
    opted_in(user_id, suspended=1)
    monkeypatch.setattr(
        web_server,
        "get_ai_config",
        lambda uid: {"base_url": "https://provider.example", "api_key": "key"},
    )
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True}, 200),
    )
    notices = []
    monkeypatch.setattr(
        web_server,
        "_notify_user",
        lambda uid, *args, **kwargs: notices.append(uid),
    )
    monkeypatch.setattr(web_server.time, "sleep", lambda seconds: None)

    web_server._run_ai_share_revalidation_once()

    settings = models.get_user_settings(user_id)
    assert settings["share_suspended"] == 0
    assert web_server.is_share_active(settings) is True
    assert notices == [user_id]
```

- [ ] **Step 6: Run transition and notification tests**

Run:

```bash
python3 -m pytest -q tests/test_share_recovery.py tests/test_notifications.py
```

Expected: PASS.

- [ ] **Step 7: Commit the state machine**

```bash
git add web_server.py tests/test_share_recovery.py
git commit -m "feat(share): pause and restore sharing on connectivity transitions"
```

### Task 3: Restore from saved config and manual connection tests

**Files:**
- Modify: `web_server.py:782-825`
- Modify: `web_server.py:926-960`
- Modify: `web_server.py:4525-4563`
- Modify: `tests/test_share_recovery.py`

**Interfaces:**
- Extends `PUT /ai/config` response: `share_check: {status, restored, error?}`
- Extends `POST /ai/test-connection` response with same `share_check`
- Consumes: `_apply_share_connectivity_result()`

- [ ] **Step 1: Add route tests for saved-config recovery**

Append:

```python
def test_saving_new_api_tests_and_restores_suspended_share(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True, "response": "pong"}, 200),
    )
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: None)

    response = client.put(
        "/ai/config",
        json={
            "provider": "OpenAI",
            "api_key": "replacement-key",
            "endpoint": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "provider_type": "openai",
            "enabled": 1,
        },
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200
    assert response.get_json()["share_check"] == {
        "status": "restored",
        "restored": True,
    }
    assert models.get_user_settings(user_id)["share_suspended"] == 0


def test_failed_saved_config_remains_saved_but_share_stays_suspended(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"error": "AI API HTTP 401"}, 502),
    )

    response = client.put(
        "/ai/config",
        json={"api_key": "still-invalid", "enabled": 1},
        headers=auth_headers(user_id),
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["has_api_key"] is True
    assert data["share_check"]["status"] == "unchanged"
    assert data["share_check"]["restored"] is False
    assert "401" in data["share_check"]["error"]
    assert models.get_user_settings(user_id)["share_suspended"] == 1


def test_manual_connection_test_can_restore(share_env, monkeypatch):
    client, user_id = share_env
    opted_in(user_id, suspended=1)
    monkeypatch.setattr(
        web_server,
        "_run_ai_connection_test",
        lambda config: ({"ok": True, "response": "pong"}, 200),
    )
    monkeypatch.setattr(web_server, "_notify_user", lambda *args, **kwargs: None)

    response = client.post("/ai/test-connection", headers=auth_headers(user_id))

    assert response.status_code == 200
    assert response.get_json()["share_check"]["restored"] is True
    assert models.get_user_settings(user_id)["share_suspended"] == 0
```

- [ ] **Step 2: Run route tests and verify missing response state**

Run:

```bash
python3 -m pytest -q tests/test_share_recovery.py -k "saving_new_api or failed_saved_config or manual_connection"
```

Expected: FAIL because config/test routes do not run the share transition or return `share_check`.

- [ ] **Step 3: Add one route helper**

Add:

```python
def _share_check_after_personal_api_test(user_id: int, body: dict, status: int) -> dict | None:
    settings = get_user_settings(user_id) or {}
    if not _is_enabled_value(settings.get("share_ai_results")):
        return None
    error = body.get("error", "") if status != 200 else ""
    transition = _apply_share_connectivity_result(user_id, status == 200, error)
    result = {
        "status": transition,
        "restored": transition == "restored",
    }
    if status != 200:
        result["error"] = _compact_share_error(error)
    return result
```

- [ ] **Step 4: Integrate saved-config testing**

After `set_ai_config()` succeeds in `set_ai_config_route()`:

```python
settings = get_user_settings(g.user_id) or {}
share_check = None
if _is_enabled_value(settings.get("share_ai_results")):
    test_body, test_status = _run_ai_connection_test(config)
    share_check = _share_check_after_personal_api_test(
        g.user_id, test_body, test_status
    )
...
if share_check is not None:
    safe["share_check"] = share_check
```

The route remains HTTP 200 when saving succeeds but connectivity fails; the saved invalid config is needed so the user can inspect/correct it. The nested `share_check.error` communicates that sharing remains paused.

- [ ] **Step 5: Integrate manual testing**

In `ai_test_connection()`:

```python
body, status = _run_ai_connection_test(get_ai_config(g.user_id))
share_check = _share_check_after_personal_api_test(g.user_id, body, status)
if share_check is not None:
    body = {**body, "share_check": share_check}
return jsonify(body), status
```

- [ ] **Step 6: Make sharing settings use the transition helper**

When enabling `share_ai_results` in `update_settings()`, use this order so the
transition helper can still observe whether the prior state was suspended:

```python
share_check_ok = False
if "share_ai_results" in data and _is_enabled_value(data["share_ai_results"]):
    config = get_ai_config(g.user_id)
    body, status = _run_ai_connection_test(config)
    if status != 200:
        _apply_share_connectivity_result(
            g.user_id, False, body.get("error", "")
        )
        return jsonify({
            "error": "personal AI API connection test failed",
            "share_check": {
                "ok": False,
                "status": "paused",
                "error": _compact_share_error(body.get("error", "")),
            },
        }), 400

    # Persist intent and exact sub-toggle choices first. Do not write
    # share_suspended/share_last_check_* from request data.
    for key in (
        "share_suspended",
        "share_last_check_ok",
        "share_last_check_at",
        "share_last_check_error",
    ):
        data.pop(key, None)
    share_check_ok = True

settings = set_user_settings(g.user_id, **data)
if share_check_ok:
    _apply_share_connectivity_result(g.user_id, True)
    settings = get_user_settings(g.user_id)
settings["share_active"] = is_share_active(settings)
return jsonify(settings)
```

This sequence covers both first-time enablement and restoration of a suspended
opted-in user. A failed live test leaves the saved intent/sub-toggle values
unchanged and transitions an already opted-in user to paused.

When disabling:

```python
data["share_ai_results"] = 0
data["share_suspended"] = 0
for key in share_sub_keys:
    data[key] = 0
```

- [ ] **Step 7: Run route and settings tests**

Run:

```bash
python3 -m pytest -q tests/test_share_recovery.py
```

Expected: PASS.

- [ ] **Step 8: Commit recovery routes**

```bash
git add web_server.py tests/test_share_recovery.py
git commit -m "feat(share): recover sharing after personal api validation"
```

### Task 4: Render paused state and enforce the frontend title gate

**Files:**
- Modify: `web_server.py:4470-4500`
- Modify: `frontend/index.html:939-962`
- Modify: `frontend/index.html:3208-3268`
- Modify: `frontend/index.html:3578-3600`
- Modify: `frontend/index.html:7498-7505`
- Modify: `tests/test_share_recovery.py`
- Modify: `tests/test_ai_relay_frontend.py`

**Interfaces:**
- `GET /settings` produces `share_suspended`, `share_active`
- Frontend `userAutoSettings` stores both
- `displayTitle()` consumes `share_active && share_view_title`

- [ ] **Step 1: Add settings-response and frontend source tests**

Append to `tests/test_share_recovery.py`:

```python
def test_settings_returns_intent_suspension_and_effective_state(share_env):
    client, user_id = share_env
    opted_in(user_id, suspended=1)

    response = client.get("/settings", headers=auth_headers(user_id))

    assert response.status_code == 200
    data = response.get_json()
    assert data["share_ai_results"] == 1
    assert data["share_suspended"] == 1
    assert data["share_active"] is False


def test_frontend_keeps_paused_preferences_visible_but_disabled():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    load_start = html.index("async function loadShareTab()")
    load_end = html.index("async function saveShareConfig()", load_start)
    block = html[load_start:load_end]
    assert "share_suspended" in html
    assert "share_active" in html
    assert "共享已暂停" in block
    assert "el.disabled = !masterOn || suspended" in html
    title_start = html.index("function displayTitle(")
    title_end = html.index("\n}", title_start)
    assert "share_active" in html[title_start:title_end]
```

- [ ] **Step 2: Run tests and verify missing effective state**

Run:

```bash
python3 -m pytest -q tests/test_share_recovery.py -k "settings_returns or frontend_keeps"
```

Expected: FAIL because settings and frontend do not expose/render suspension.

- [ ] **Step 3: Add a safe settings serializer**

Before the settings routes, add:

```python
def _settings_response(settings: dict | None) -> dict:
    safe = dict(settings or {})
    safe.setdefault("share_ai_results", 0)
    safe.setdefault("share_view_title", 0)
    safe.setdefault("share_view_translation", 0)
    safe.setdefault("share_view_summary", 0)
    safe.setdefault("share_suspended", 0)
    safe["share_active"] = is_share_active(safe)
    nc = safe.get("notification_config", "{}")
    if isinstance(nc, str):
        try:
            nc = json.loads(nc)
        except (json.JSONDecodeError, TypeError):
            nc = {}
    safe["notification_config"] = nc
    return safe
```

Use it in both `GET /settings` and the successful `PUT /settings` response so the two routes cannot disagree.

- [ ] **Step 4: Update frontend settings state**

In `loadUserSettings()` add:

```js
share_suspended: !!data.share_suspended,
share_active: !!data.share_active,
```

Change `updateShareSubToggleState()`:

```js
function updateShareSubToggleState() {
  const masterOn = document.getElementById('shareAiResults').checked;
  const suspended = !!userAutoSettings?.share_suspended;
  ['shareViewTitle', 'shareViewTranslation', 'shareViewSummary'].forEach(id => {
    const el = document.getElementById(id);
    el.disabled = !masterOn || suspended;
    if (!masterOn) el.checked = false;
  });
  const hint = document.getElementById('shareSubHint');
  hint.textContent = !masterOn
    ? '需先开启共享 AI 结果'
    : (suspended
      ? 'API 校验失败，共享已暂停。更新 API 并校验成功后会自动恢复，无需重新开启。'
      : '');
  hint.style.display = hint.textContent ? '' : 'none';
}
```

In `loadShareTab()`, keep the database values checked and render:

```js
if (s.share_suspended) {
  statusEl.textContent =
    '⏸ API 校验失败，共享已暂停。原有选项已保留，API 恢复后会自动重新生效。'
    + (s.share_last_check_error ? ' 原因：' + s.share_last_check_error : '');
  statusEl.style.color = '#e5a84d';
}
```

- [ ] **Step 5: Update config-save feedback**

In `saveAIConfig()`:

```js
if (data.share_check?.restored) {
  showSettingsStatus('✅ AI 配置已保存，共享状态已自动恢复', 'ok');
  await loadUserSettings();
} else if (data.share_check?.error) {
  showSettingsStatus(
    '⚠️ AI 配置已保存，但连接校验失败，共享仍处于暂停状态：'
      + data.share_check.error,
    'err',
  );
} else {
  showSettingsStatus('✅ AI 配置已保存');
}
```

Do the same for `testAIConnection()` success when `share_check.restored` is true.

- [ ] **Step 6: Enforce effective shared-title access**

Change:

```js
if (userAutoSettings?.share_view_title)
```

to:

```js
if (userAutoSettings?.share_active && userAutoSettings?.share_view_title)
```

Keep the original-title fallback unchanged.

- [ ] **Step 7: Run focused frontend/backend tests**

Run:

```bash
python3 -m pytest -q \
  tests/test_share_recovery.py \
  tests/test_ai_relay_frontend.py \
  tests/test_notifications.py
```

Expected: PASS.

- [ ] **Step 8: Commit the paused UI**

```bash
git add frontend/index.html web_server.py tests/test_share_recovery.py tests/test_ai_relay_frontend.py
git commit -m "feat(ui): show paused sharing and automatic recovery"
```

### Task 5: Complete regression and transition verification

**Files:**
- No production files unless a failing test proves a defect.

**Interfaces:**
- Consumes all prior tasks
- Produces final safety and notification evidence

- [ ] **Step 1: Run security and notification suites**

Run:

```bash
python3 -m pytest -q \
  tests/test_share_recovery.py \
  tests/test_notifications.py \
  tests/test_security_hardening.py \
  tests/test_ai_relay_frontend.py \
  tests/test_access_and_ui_contracts.py
```

Expected: PASS.

- [ ] **Step 2: Run the full suite**

Run:

```bash
python3 -m pytest -q
```

Expected: PASS.

- [ ] **Step 3: Manually verify the state matrix**

Use a temporary user:

1. Enable sharing with title and summary on, translation off.
2. Make the personal API return an authentication failure and run revalidation.
3. Confirm the master remains on, the exact sub-switch values remain visible, and all three controls are disabled.
4. Confirm shared title/summary/translation endpoints return no gated fields.
5. Confirm one pause notification and one email attempt.
6. Run the same failed check again; confirm no duplicate notification.
7. Save a working API; confirm immediate recovery, exact preferences, one recovery notification, and one email attempt.
8. Suspend again, explicitly switch sharing off, then restore the API; confirm no automatic re-enable.

- [ ] **Step 4: Record connectivity performance evidence**

Confirm no connection test occurs during homepage or article-list requests. Confirm the six-hour loop sleeps 0.5 seconds between users and holds no database transaction while waiting on the provider.

## Completion Gate

Do not mark complete if any code path treats `share_view_*` alone as sufficient access, or if a failed check still writes zero into the user's intent fields.
