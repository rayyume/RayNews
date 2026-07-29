# Global 21:00 Daily Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate exactly one server-owned daily summary after 21:00 Beijing time, expose it through ✨, and distribute the same Markdown to independently configured in-app and email channels.

**Architecture:** Persist one date-scoped run state in `NEWS_DB`, keep the existing `daily_summary_global` row as the sole content record, and split generation from delivery. A short atomic claim prevents duplicate generation; idempotent notification source keys and existing Resend keys prevent duplicate delivery.

**Tech Stack:** Flask, SQLite/WAL, Python background threads, existing `AIService`, vanilla JavaScript, Markdown sanitizer, pytest.

## Global Constraints

- Work on `dev`; line numbers refer to `dev@7fa5b56`.
- One Beijing date produces one global summary, regardless of user count.
- Use only `get_system_ai_config()`; never use a user's personal API.
- Ordinary users cannot generate, regenerate, or retry.
- Automatic failure ends automatic attempts for that date.
- Only administrators can retry, and only while the date-scoped status is `failed`.
- Failure notifications go only to administrators through in-app notification and email.
- In-app summary delivery defaults on for existing and new users; no historical backfill.
- Existing email preference/address remain unchanged; new users default email off.
- Missing Resend configuration or email subscribers must not block generation or in-app delivery.

---

## File Structure

- Modify: `models.py` — in-app preference migration, notification source-key idempotency, recipient selectors.
- Modify: `web_server.py` — global run state, generation error classification, scheduler, distribution, status/retry routes.
- Modify: `frontend/index.html` — independent settings, scheduled/running/completed/failed ✨ states, admin retry gate, notification article links.
- Modify: `tests/test_notifications.py` — source-key and bulk idempotency.
- Create: `tests/test_daily_summary_scheduling.py` — run-state, scheduler, one-generation, failure and retry tests.
- Modify: `tests/test_security_hardening.py` — replace force-resend assumptions with separated email-delivery idempotency.
- Modify: `tests/test_access_and_ui_contracts.py` — frontend copy/control contracts.
- Modify: `README.md`, `README.en.md` — document fixed 21:00 generation and independent channels.

### Task 1: Add independent in-app preference and idempotent notification keys

**Files:**
- Modify: `models.py:47-65`
- Modify: `models.py:94-105`
- Modify: `models.py:145-165`
- Modify: `models.py:518-558`
- Modify: `models.py:580-690`
- Modify: `tests/test_notifications.py`

**Interfaces:**
- Produces setting: `daily_summary_inapp_enabled: int` default `1`
- Extends notification: `source_key: str | None`
- Extends: `add_notification(..., source_key: str | None = None)`
- Produces: `add_notification_bulk_unique(user_ids, ntype, title, body, fmt, source_key) -> int`
- Produces: `get_daily_summary_inapp_user_ids() -> list[int]`

- [ ] **Step 1: Add failing preference migration tests**

Append to `tests/test_notifications.py`:

```python
def test_daily_summary_inapp_defaults_on_for_existing_and_new_settings_rows(self):
    existing_user = models.create_user("existing@example.com", "pw", "existing")["id"]
    models.set_user_settings(existing_user, theme_preference="dark")
    assert models.get_user_settings(existing_user)["daily_summary_inapp_enabled"] == 1

    new_user = models.create_user("new@example.com", "pw", "new")["id"]
    assert new_user in models.get_daily_summary_inapp_user_ids()


def test_daily_summary_inapp_recipient_selector_honors_opt_out(self):
    enabled = models.create_user("enabled@example.com", "pw", "enabled")["id"]
    disabled = models.create_user("disabled@example.com", "pw", "disabled")["id"]
    models.set_user_settings(enabled, daily_summary_inapp_enabled=1)
    models.set_user_settings(disabled, daily_summary_inapp_enabled=0)

    recipients = models.get_daily_summary_inapp_user_ids()

    assert enabled in recipients
    assert disabled not in recipients
```

- [ ] **Step 2: Add failing notification idempotency tests**

Append:

```python
def test_notification_source_key_is_user_scoped_and_idempotent():
    first = models.add_notification(
        self.user_a,
        "daily_summary",
        "2026-07-27 每日摘要",
        "# 摘要",
        fmt="markdown",
        source_key="daily-summary:2026-07-27",
    )
    second = models.add_notification(
        self.user_a,
        "daily_summary",
        "重复",
        "# 重复",
        fmt="markdown",
        source_key="daily-summary:2026-07-27",
    )
    other_user = models.add_notification(
        self.user_b,
        "daily_summary",
        "2026-07-27 每日摘要",
        "# 摘要",
        fmt="markdown",
        source_key="daily-summary:2026-07-27",
    )

    self.assertGreater(first, 0)
    self.assertEqual(second, 0)
    self.assertGreater(other_user, 0)
    self.assertEqual(len(models.list_notifications(self.user_a)), 1)
    self.assertEqual(
        models.list_notifications(self.user_a)[0]["source_key"],
        "daily-summary:2026-07-27",
    )


def test_bulk_unique_daily_summary_delivery_is_one_transaction_and_replay_safe():
    first = models.add_notification_bulk_unique(
        [self.user_a, self.user_b],
        "daily_summary",
        "2026-07-27 每日摘要",
        "# 摘要",
        "markdown",
        "daily-summary:2026-07-27",
    )
    replay = models.add_notification_bulk_unique(
        [self.user_a, self.user_b],
        "daily_summary",
        "重复",
        "# 重复",
        "markdown",
        "daily-summary:2026-07-27",
    )

    self.assertEqual(first, 2)
    self.assertEqual(replay, 0)
```

Place these methods inside `NotificationsModelTests`; use `self` rather than free functions.

- [ ] **Step 3: Run tests and verify missing schema/interfaces**

Run:

```bash
python3 -m pytest -q tests/test_notifications.py
```

Expected: FAIL because the new setting, selector, source key, and bulk helper do not exist.

- [ ] **Step 4: Add user-setting migration**

Add to the `user_settings` DDL:

```sql
daily_summary_inapp_enabled INTEGER NOT NULL DEFAULT 1,
```

Add migration:

```python
"ALTER TABLE user_settings ADD COLUMN daily_summary_inapp_enabled INTEGER NOT NULL DEFAULT 1",
```

Include it in `get_user_settings()` and `set_user_settings()` allowed fields.

Implement the recipient selector so users without a settings row inherit enabled:

```python
def get_daily_summary_inapp_user_ids() -> list[int]:
    db = get_db()
    rows = db.execute(
        "SELECT u.id AS user_id "
        "FROM users u "
        "LEFT JOIN user_settings s ON s.user_id = u.id "
        "WHERE COALESCE(s.daily_summary_inapp_enabled, 1) = 1 "
        "ORDER BY u.id"
    ).fetchall()
    return [int(row["user_id"]) for row in rows]
```

- [ ] **Step 5: Add notification source keys**

Add nullable `source_key TEXT` to the notification DDL and migration:

```python
"ALTER TABLE notifications ADD COLUMN source_key TEXT",
```

After migrations, always execute:

```python
db.execute(
    "CREATE UNIQUE INDEX IF NOT EXISTS "
    "idx_notifications_user_type_source "
    "ON notifications(user_id, type, source_key) "
    "WHERE source_key IS NOT NULL"
)
```

Extend `add_notification()`:

```python
def add_notification(
    user_id: int,
    ntype: str,
    title: str,
    body: str = "",
    fmt: str = "plain",
    source_key: str | None = None,
) -> int:
    ...
    cur = db.execute(
        "INSERT OR IGNORE INTO notifications "
        "(user_id, type, title, body, format, source_key, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, ntype, title, body, fmt, source_key, now),
    )
    db.commit()
    return int(cur.lastrowid or 0) if cur.rowcount else 0
```

Select `source_key` in `list_notifications()`.

- [ ] **Step 6: Implement bulk unique delivery**

```python
def add_notification_bulk_unique(
    user_ids: list[int],
    ntype: str,
    title: str,
    body: str,
    fmt: str,
    source_key: str,
) -> int:
    if not user_ids:
        return 0
    db = sqlite3.connect(str(DB_FILE), timeout=30)
    now = datetime.now().isoformat(timespec="seconds")
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("BEGIN IMMEDIATE")
        before = db.total_changes
        db.executemany(
            "INSERT OR IGNORE INTO notifications "
            "(user_id, type, title, body, format, source_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (uid, ntype, title, body, fmt, source_key, now)
                for uid in user_ids
            ],
        )
        inserted = db.total_changes - before
        db.commit()
        return inserted
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
```

- [ ] **Step 7: Run model tests**

Run:

```bash
python3 -m pytest -q tests/test_notifications.py tests/test_users_role_migration.py
```

Expected: PASS.

- [ ] **Step 8: Commit persistence primitives**

```bash
git add models.py tests/test_notifications.py
git commit -m "feat(notify): add daily summary preference and idempotent source keys"
```

### Task 2: Persist and atomically claim one global run per Beijing date

**Files:**
- Modify: `web_server.py:1272-1474`
- Create: `tests/test_daily_summary_scheduling.py`

**Interfaces:**
- Produces table: `daily_summary_runs`
- Produces: `_get_daily_summary_run(date_str: str) -> dict | None`
- Produces: `_claim_daily_summary_run(date_str: str, trigger: str) -> dict | None`
- Produces: `_complete_daily_summary_run(date_str: str) -> None`
- Produces: `_fail_daily_summary_run(date_str: str, code: str, detail: str) -> dict`
- Trigger values: `scheduled`, `admin_retry`

- [ ] **Step 1: Create test DB helpers and failing initial-claim test**

Create `tests/test_daily_summary_scheduling.py`:

```python
import datetime as dt
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

import models
import web_server

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def daily_env(monkeypatch):
    news_db = ROOT / f"tmp-daily-news-{uuid.uuid4().hex}.db"
    auth_db = ROOT / f"tmp-daily-auth-{uuid.uuid4().hex}.db"
    sqlite3.connect(news_db).close()
    old_news_db = web_server.NEWS_DB
    old_auth_db = models.DB_FILE
    models.close_db()
    web_server.NEWS_DB = str(news_db)
    models.DB_FILE = auth_db
    models.get_db()
    try:
        yield news_db
    finally:
        models.close_db()
        web_server.NEWS_DB = old_news_db
        models.DB_FILE = old_auth_db
        for path in (news_db, auth_db):
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(str(path) + suffix)
                except FileNotFoundError:
                    pass


def test_scheduled_claim_is_unique_and_persistent(daily_env):
    first = web_server._claim_daily_summary_run("2026-07-27", "scheduled")
    second = web_server._claim_daily_summary_run("2026-07-27", "scheduled")

    assert first["status"] == "running"
    assert first["attempt_count"] == 1
    assert first["trigger"] == "scheduled"
    assert second is None
    assert web_server._get_daily_summary_run("2026-07-27")["status"] == "running"
```

- [ ] **Step 2: Add failing failed/retry transition tests**

Append:

```python
def test_failed_run_blocks_scheduled_retry_but_allows_one_admin_retry(daily_env):
    web_server._claim_daily_summary_run("2026-07-27", "scheduled")
    failed = web_server._fail_daily_summary_run(
        "2026-07-27", "server_network_error", "connection timed out"
    )
    assert failed["status"] == "failed"
    assert web_server._claim_daily_summary_run("2026-07-27", "scheduled") is None

    retry = web_server._claim_daily_summary_run("2026-07-27", "admin_retry")
    duplicate = web_server._claim_daily_summary_run("2026-07-27", "admin_retry")
    assert retry["status"] == "running"
    assert retry["attempt_count"] == 2
    assert retry["trigger"] == "admin_retry"
    assert duplicate is None


def test_completed_run_can_never_be_reclaimed(daily_env):
    web_server._claim_daily_summary_run("2026-07-27", "scheduled")
    web_server._complete_daily_summary_run("2026-07-27")
    assert web_server._claim_daily_summary_run("2026-07-27", "scheduled") is None
    assert web_server._claim_daily_summary_run("2026-07-27", "admin_retry") is None
```

- [ ] **Step 3: Run tests and verify missing run-state functions**

Run:

```bash
python3 -m pytest -q tests/test_daily_summary_scheduling.py
```

Expected: FAIL because the run table and helpers do not exist.

- [ ] **Step 4: Create the run table**

Implement:

```python
def _init_daily_summary_runs_table() -> None:
    if not os.path.exists(NEWS_DB):
        return
    conn = sqlite3.connect(NEWS_DB, timeout=30)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary_runs (
                date          TEXT PRIMARY KEY,
                status        TEXT NOT NULL
                              CHECK(status IN ('running','completed','failed')),
                attempt_count INTEGER NOT NULL DEFAULT 1,
                trigger       TEXT NOT NULL
                              CHECK(trigger IN ('scheduled','admin_retry')),
                error_code    TEXT NOT NULL DEFAULT '',
                error_detail  TEXT NOT NULL DEFAULT '',
                started_at    TEXT NOT NULL,
                finished_at   TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()
```

Call it during `_start_background_jobs()`.

- [ ] **Step 5: Implement atomic claim**

Use a short-lived connection and `BEGIN IMMEDIATE`:

```python
def _claim_daily_summary_run(date_str: str, trigger: str) -> dict | None:
    if trigger not in {"scheduled", "admin_retry"}:
        raise ValueError("invalid daily summary trigger")
    _init_daily_summary_runs_table()
    conn = sqlite3.connect(NEWS_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    now = _beijing_now().isoformat(timespec="seconds")
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM daily_summary_runs WHERE date = ?",
            (date_str,),
        ).fetchone()
        if trigger == "scheduled":
            if row is not None:
                conn.rollback()
                return None
            conn.execute(
                "INSERT INTO daily_summary_runs "
                "(date,status,attempt_count,trigger,error_code,error_detail,started_at,finished_at) "
                "VALUES (?, 'running', 1, 'scheduled', '', '', ?, NULL)",
                (date_str, now),
            )
        else:
            if row is None or row["status"] != "failed":
                conn.rollback()
                return None
            updated = conn.execute(
                "UPDATE daily_summary_runs SET "
                "status='running', attempt_count=attempt_count+1, "
                "trigger='admin_retry', error_code='', error_detail='', "
                "started_at=?, finished_at=NULL "
                "WHERE date=? AND status='failed'",
                (now, date_str),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
        conn.commit()
        claimed = conn.execute(
            "SELECT * FROM daily_summary_runs WHERE date = ?",
            (date_str,),
        ).fetchone()
        return dict(claimed)
    finally:
        conn.close()
```

- [ ] **Step 6: Implement read, complete, and fail**

All writes use short-lived connections. Completion only updates a currently running row. Failure compresses whitespace, redacts `sk-*`, limits detail to 500 characters, and returns the updated row:

```python
def _safe_daily_summary_error_detail(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "generation failed")).strip()
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
    return text[:500]


def _get_daily_summary_run(date_str: str) -> dict | None:
    _init_daily_summary_runs_table()
    with sqlite3.connect(NEWS_DB, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM daily_summary_runs WHERE date = ?",
            (date_str,),
        ).fetchone()
    return dict(row) if row else None


def _complete_daily_summary_run(date_str: str) -> None:
    now = _beijing_now().isoformat(timespec="seconds")
    with sqlite3.connect(NEWS_DB, timeout=30) as conn:
        updated = conn.execute(
            "UPDATE daily_summary_runs SET status='completed', "
            "error_code='', error_detail='', finished_at=? "
            "WHERE date=? AND status='running'",
            (now, date_str),
        )
        if updated.rowcount != 1:
            raise RuntimeError("daily summary run is not running")


def _fail_daily_summary_run(date_str: str, code: str, detail: str) -> dict:
    now = _beijing_now().isoformat(timespec="seconds")
    safe_code = str(code or "generation_error")[:64]
    safe_detail = _safe_daily_summary_error_detail(detail)
    with sqlite3.connect(NEWS_DB, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        updated = conn.execute(
            "UPDATE daily_summary_runs SET status='failed', "
            "error_code=?, error_detail=?, finished_at=? "
            "WHERE date=? AND status='running'",
            (safe_code, safe_detail, now, date_str),
        )
        if updated.rowcount != 1:
            raise RuntimeError("daily summary run is not running")
        row = conn.execute(
            "SELECT * FROM daily_summary_runs WHERE date = ?",
            (date_str,),
        ).fetchone()
    return dict(row)
```

Use `_beijing_now().isoformat(timespec="seconds")` for timestamps.

- [ ] **Step 7: Run run-state tests**

Run:

```bash
python3 -m pytest -q tests/test_daily_summary_scheduling.py
```

Expected: PASS for claim/transition tests.

- [ ] **Step 8: Commit global run persistence**

```bash
git add web_server.py tests/test_daily_summary_scheduling.py
git commit -m "feat(summary): persist one daily generation run per date"
```

### Task 3: Make global generation raise classified, administrator-safe failures

**Files:**
- Modify: `web_server.py:1435-1474`
- Modify: `tests/test_daily_summary_scheduling.py`

**Interfaces:**
- Produces: `DailySummaryGenerationError(code: str, detail: str)`
- Produces: `_classify_daily_summary_exception(exc: Exception) -> tuple[str, str]`
- Extends: `_generate_daily_summary_global(date_str)` to return one result or raise classified error

- [ ] **Step 1: Add failing classifier tests**

Append:

```python
@pytest.mark.parametrize(
    ("exc", "expected"),
    (
        (RuntimeError("AI API HTTP 401: invalid api key"), "system_api_auth_or_quota"),
        (RuntimeError("AI API HTTP 429: insufficient quota"), "system_api_auth_or_quota"),
        (RuntimeError("AI API HTTP 404: model not found"), "system_api_model_error"),
        (RuntimeError("unknown model gpt-x"), "system_api_model_error"),
        (web_server.requests.exceptions.ConnectTimeout("timed out"), "server_network_error"),
        (web_server.requests.exceptions.ConnectionError("dns failed"), "server_network_error"),
        (RuntimeError("bad generated output"), "generation_error"),
    ),
)
def test_generation_failure_classifier(exc, expected):
    code, detail = web_server._classify_daily_summary_exception(exc)
    assert code == expected
    assert detail
    assert "sk-" not in detail
```

Add:

```python
def test_global_generation_rejects_missing_system_api(daily_env, monkeypatch):
    monkeypatch.setattr(web_server, "get_system_ai_config", lambda: {"enabled": 0, "api_key": ""})
    with pytest.raises(web_server.DailySummaryGenerationError) as caught:
        web_server._generate_daily_summary_global("2026-07-27")
    assert caught.value.code == "system_api_not_configured"
```

- [ ] **Step 2: Run classifier tests**

Run:

```bash
python3 -m pytest -q tests/test_daily_summary_scheduling.py -k "classifier or missing_system"
```

Expected: FAIL because typed generation failures do not exist and the current generator returns `None`.

- [ ] **Step 3: Implement the typed error and classifier**

```python
class DailySummaryGenerationError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = _safe_daily_summary_error_detail(detail)


def _classify_daily_summary_exception(exc: Exception) -> tuple[str, str]:
    detail = _safe_daily_summary_error_detail(str(exc))
    lower = detail.lower()
    if isinstance(exc, requests.exceptions.RequestException):
        return "server_network_error", detail
    if any(token in lower for token in (
        "http 401", "http 403", "http 429", "invalid api key",
        "unauthorized", "insufficient quota", "quota exceeded",
    )):
        return "system_api_auth_or_quota", detail
    if any(token in lower for token in (
        "model not found", "unknown model", "invalid model", "http 404",
    )):
        return "system_api_model_error", detail
    return "generation_error", detail
```

- [ ] **Step 4: Refactor the global generator**

Preserve the cache-first behavior. Replace silent `None` returns:

```python
if not sys_config or not sys_config.get("enabled") or not sys_config.get("api_key"):
    raise DailySummaryGenerationError(
        "system_api_not_configured",
        "管理员设置中的服务端 API 未配置或未启用",
    )
...
if not articles:
    raise DailySummaryGenerationError("no_articles", f"{date_str} 没有可用于摘要的文章")
```

Wrap only provider/generation work:

```python
try:
    result = svc.daily_summary(deduped)
except Exception as exc:
    code, detail = _classify_daily_summary_exception(exc)
    raise DailySummaryGenerationError(code, detail) from exc
```

After saving, read back the global cache so `updated_at` is present:

```python
saved = _get_daily_summary_global_cache(date_str)
if not saved:
    raise DailySummaryGenerationError("generation_error", "摘要保存后无法读取")
return saved
```

- [ ] **Step 5: Prove one generation is shared across users**

Append:

```python
def test_global_generation_cache_prevents_second_ai_generation(daily_env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        web_server,
        "get_system_ai_config",
        lambda: {
            "enabled": 1,
            "api_key": "key",
            "endpoint": "https://example.com/v1",
            "model": "model",
            "provider_type": "openai",
        },
    )
    monkeypatch.setattr(
        web_server,
        "_fetch_articles_by_date",
        lambda *args, **kwargs: [{"id": 1, "title": "t", "summary": "s"}],
    )
    monkeypatch.setattr(web_server, "_dedup_articles", lambda rows: rows)

    class FakeService:
        def __init__(self, **kwargs):
            pass
        def daily_summary(self, articles):
            calls.append(list(articles))
            return {"summary": "# 今日", "stats": {}}

    monkeypatch.setattr(web_server, "AIService", FakeService)

    first = web_server._generate_daily_summary_global("2026-07-27")
    second = web_server._generate_daily_summary_global("2026-07-27")

    assert first["summary"] == second["summary"] == "# 今日"
    assert len(calls) == 1
```

- [ ] **Step 6: Run generation tests**

Run:

```bash
python3 -m pytest -q tests/test_daily_summary.py tests/test_daily_summary_scheduling.py
```

Expected: PASS.

- [ ] **Step 7: Commit classified global generation**

```bash
git add web_server.py tests/test_daily_summary_scheduling.py
git commit -m "refactor(summary): classify global generation failures"
```

### Task 4: Schedule once, distribute success, and notify only administrators on failure

**Files:**
- Modify: `models.py` imports exposed to `web_server.py`
- Modify: `web_server.py:1020-1101`
- Modify: `web_server.py:1545-1706`
- Modify: `web_server.py:4711-4724`
- Modify: `tests/test_daily_summary_scheduling.py`
- Modify: `tests/test_security_hardening.py:140-285`

**Interfaces:**
- Produces: `_run_claimed_daily_summary(date_str: str) -> None`
- Produces: `_schedule_daily_summary_once() -> bool`
- Produces: `_distribute_daily_summary_inapp(date_str, result) -> int`
- Produces: `_distribute_daily_summary_email(date_str, result) -> dict`
- Consumes: run claim, global generator, unique notification helper, existing email send-state helpers

- [ ] **Step 1: Add failing schedule-time tests**

Append:

```python
def test_scheduler_does_nothing_before_2100(daily_env, monkeypatch):
    monkeypatch.setattr(
        web_server,
        "_beijing_now",
        lambda: dt.datetime(2026, 7, 27, 20, 59, tzinfo=dt.timezone(dt.timedelta(hours=8))),
    )
    assert web_server._schedule_daily_summary_once() is False
    assert web_server._get_daily_summary_run("2026-07-27") is None


def test_scheduler_claims_once_after_2100(daily_env, monkeypatch):
    monkeypatch.setattr(
        web_server,
        "_beijing_now",
        lambda: dt.datetime(2026, 7, 27, 21, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))),
    )
    started = []

    class ImmediateThread:
        def __init__(self, target, args=(), **kwargs):
            self.target, self.args = target, args
        def start(self):
            started.append(self.args)

    monkeypatch.setattr(web_server.threading, "Thread", ImmediateThread)
    assert web_server._schedule_daily_summary_once() is True
    assert web_server._schedule_daily_summary_once() is False
    assert started == [("2026-07-27",)]
```

- [ ] **Step 2: Add failing delivery tests**

Append:

```python
def test_inapp_delivery_uses_one_global_markdown_and_is_idempotent(daily_env):
    user_a = models.create_user("a@example.com", "pw", "a")["id"]
    user_b = models.create_user("b@example.com", "pw", "b")["id"]
    models.set_user_settings(user_b, daily_summary_inapp_enabled=0)
    result = {"summary": "# 今日摘要\n1. 新闻", "stats": {}, "article_count": 1}

    first = web_server._distribute_daily_summary_inapp("2026-07-27", result)
    replay = web_server._distribute_daily_summary_inapp("2026-07-27", result)

    assert first == 1
    assert replay == 0
    notice = models.list_notifications(user_a)[0]
    assert notice["format"] == "markdown"
    assert notice["body"] == result["summary"]
    assert models.list_notifications(user_b) == []


def test_no_resend_does_not_block_inapp_or_generation_success(daily_env, monkeypatch):
    admin = models.create_user("admin@example.com", "pw", "admin", role="admin")["id"]
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setattr(
        web_server,
        "_generate_daily_summary_global",
        lambda date_str: {
            "summary": "# 今日摘要",
            "stats": {},
            "article_count": 1,
            "updated_at": "2026-07-27 13:00:00",
        },
    )
    web_server._claim_daily_summary_run("2026-07-27", "scheduled")

    web_server._run_claimed_daily_summary("2026-07-27")

    assert web_server._get_daily_summary_run("2026-07-27")["status"] == "completed"
    assert models.list_notifications(admin)[0]["type"] == "daily_summary"
```

- [ ] **Step 3: Add failing administrator-only failure-notification test**

Append:

```python
def test_generation_failure_stops_and_notifies_only_admins(daily_env, monkeypatch):
    admin = models.create_user("admin@example.com", "pw", "admin", role="admin")["id"]
    user = models.create_user("user@example.com", "pw", "user")["id"]
    emails = []
    monkeypatch.setattr(
        web_server,
        "_generate_daily_summary_global",
        lambda date_str: (_ for _ in ()).throw(
            web_server.DailySummaryGenerationError(
                "server_network_error", "DNS connection failed"
            )
        ),
    )
    monkeypatch.setattr(
        web_server,
        "_send_notification_email",
        lambda user_id, *args, **kwargs: emails.append(user_id) or True,
    )
    monkeypatch.setattr(
        web_server,
        "_beijing_now",
        lambda: dt.datetime(
            2026, 7, 27, 21, 5,
            tzinfo=dt.timezone(dt.timedelta(hours=8)),
        ),
    )
    web_server._claim_daily_summary_run("2026-07-27", "scheduled")

    web_server._run_claimed_daily_summary("2026-07-27")

    run = web_server._get_daily_summary_run("2026-07-27")
    assert run["status"] == "failed"
    assert run["error_code"] == "server_network_error"
    assert [n["type"] for n in models.list_notifications(admin)] == ["daily_summary_failed"]
    assert models.list_notifications(user) == []
    assert emails == [admin]
    assert web_server._schedule_daily_summary_once() is False
```

- [ ] **Step 4: Run new tests and verify missing orchestration**

Run:

```bash
python3 -m pytest -q tests/test_daily_summary_scheduling.py -k "scheduler or delivery or generation_failure"
```

Expected: FAIL because schedule/run/distribution functions do not exist.

- [ ] **Step 5: Implement in-app delivery**

Import `get_daily_summary_inapp_user_ids` and `add_notification_bulk_unique`. Implement:

```python
def _distribute_daily_summary_inapp(date_str: str, result: dict) -> int:
    user_ids = get_daily_summary_inapp_user_ids()
    return add_notification_bulk_unique(
        user_ids,
        "daily_summary",
        f"{date_str} 每日摘要",
        result["summary"],
        "markdown",
        f"daily-summary:{date_str}",
    )
```

- [ ] **Step 6: Separate email delivery from generation**

Refactor the recipient/email loop from `_broadcast_daily_summary()` into:

```python
def _distribute_daily_summary_email(date_str: str, result: dict) -> dict:
```

Rules:

- if `RESEND_API_KEY` is missing, return `{"status": "skipped", "reason": "RESEND_API_KEY not set"}`;
- query only `daily_summary_enabled=1`;
- ignore rows without a recipient email;
- use `_get_daily_summary_sent_user_ids()` and `_record_daily_summary_send()`;
- use the existing Resend idempotency key `daily-<date>-<user>`;
- do not call `_generate_daily_summary_global()`.

Remove `force=True` resend semantics. A completed summary is never regenerated or force-resent by the new workflow.

- [ ] **Step 7: Implement failure notification**

Extend `_notify_user()` to accept optional `source_key` and email idempotency key:

```python
def _notify_user(
    user_id: int,
    ntype: str,
    title: str,
    body: str = "",
    *,
    source_key: str | None = None,
) -> None:
    try:
        add_notification(user_id, ntype, title, body, source_key=source_key)
    except Exception as exc:
        print(f"[notify] Failed to add in-app notification for user {user_id}: {exc}")
    _send_notification_email(
        user_id,
        title,
        body,
        idempotency_key=source_key,
    )
```

For each admin returned by `list_users()` with `role == "admin"`, send:

```python
source_key = f"daily-summary-failed:{date_str}:{attempt_count}"
title = f"{date_str} 每日摘要生成失败"
body = (
    f"失败分类：{error.code}\n\n"
    f"失败原因：{error.detail}\n\n"
    "系统不会自动重试。请在首页 ✨ 每日摘要弹窗中检查后手动重试。"
)
```

- [ ] **Step 8: Implement claimed-run execution**

```python
def _run_claimed_daily_summary(date_str: str) -> None:
    try:
        result = _generate_daily_summary_global(date_str)
        _complete_daily_summary_run(date_str)
    except DailySummaryGenerationError as error:
        failed = _fail_daily_summary_run(date_str, error.code, error.detail)
        _notify_daily_summary_failure_admins(date_str, failed)
        return
    except Exception as exc:
        code, detail = _classify_daily_summary_exception(exc)
        failed = _fail_daily_summary_run(date_str, code, detail)
        _notify_daily_summary_failure_admins(date_str, failed)
        return

    try:
        _distribute_daily_summary_inapp(date_str, result)
    except Exception as exc:
        print(f"[daily-summary] in-app delivery failed for {date_str}: {exc}")
    try:
        _distribute_daily_summary_email(date_str, result)
    except Exception as exc:
        print(f"[daily-summary] email delivery failed for {date_str}: {exc}")
```

Delivery failure never changes the completed generation state.

- [ ] **Step 9: Implement the scheduler**

```python
def _schedule_daily_summary_once() -> bool:
    now = _beijing_now()
    if (now.hour, now.minute) < (DAILY_SUMMARY_HOUR, DAILY_SUMMARY_MINUTE):
        return False
    date_str = now.strftime("%Y-%m-%d")
    if _get_daily_summary_global_cache(date_str):
        return False
    claimed = _claim_daily_summary_run(date_str, "scheduled")
    if not claimed:
        return False
    threading.Thread(
        target=_run_claimed_daily_summary,
        args=(date_str,),
        name=f"daily-summary-{date_str}",
        daemon=True,
    ).start()
    return True
```

Make `_daily_summary_loop()` call this every 60 seconds. Remove the ten-minute generation window; the run row now provides the once-per-date boundary.

- [ ] **Step 10: Update old email tests**

Change `tests/test_security_hardening.py` to call `_distribute_daily_summary_email(date_str, result)` directly. Preserve assertions for:

- successful per-user send state surviving restart;
- retrying only a previously failed email recipient;
- Resend idempotency keys.

Delete the old test that expects `force=True` to resend a completed summary; the new product explicitly prohibits successful regeneration/resend.

- [ ] **Step 11: Run scheduler, notification, and security tests**

Run:

```bash
python3 -m pytest -q \
  tests/test_daily_summary_scheduling.py \
  tests/test_notifications.py \
  tests/test_security_hardening.py
```

Expected: PASS.

- [ ] **Step 12: Commit scheduling and distribution**

```bash
git add models.py web_server.py tests/test_daily_summary_scheduling.py tests/test_security_hardening.py
git commit -m "feat(summary): schedule one global summary and distribute by channel"
```

### Task 5: Replace user generation APIs with global status and failed-only admin retry

**Files:**
- Modify: `web_server.py:1158-1270`
- Modify: `web_server.py:2993-3000`
- Modify: `tests/test_daily_summary_scheduling.py`

**Interfaces:**
- `GET /ai/daily-summary/today`
- `POST /ai/daily-summary/retry` (admin only)
- `POST /ai/daily-summary` returns 405 and never generates
- Removes user-level `_daily_summary_jobs` from active flow

- [ ] **Step 1: Add authenticated route helpers and failing state tests**

Append:

```python
def headers(user_id: int, role: str) -> dict:
    return {"Authorization": f"Bearer {web_server.create_token(user_id, role)}"}


def fixed_beijing(hour: int, minute: int = 0):
    return dt.datetime(
        2026, 7, 27, hour, minute,
        tzinfo=dt.timezone(dt.timedelta(hours=8)),
    )


def test_today_is_scheduled_before_2100(daily_env, monkeypatch):
    user = models.create_user("u@example.com", "pw", "u")
    monkeypatch.setattr(web_server, "_beijing_now", lambda: fixed_beijing(20, 59))
    response = web_server.app.test_client().get(
        "/ai/daily-summary/today",
        headers=headers(user["id"], "user"),
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "status": "scheduled",
        "date": "2026-07-27",
        "available_at": "21:00",
        "can_retry": False,
    }


def test_user_manual_generation_is_disabled(daily_env):
    user = models.create_user("u@example.com", "pw", "u")
    response = web_server.app.test_client().post(
        "/ai/daily-summary",
        headers=headers(user["id"], "user"),
    )
    assert response.status_code == 405
    assert response.get_json()["error"] == "manual daily summary generation is disabled"
```

- [ ] **Step 2: Add failing retry authorization/state tests**

Append:

```python
def test_only_admin_can_retry_and_only_failed_state_is_claimed(daily_env, monkeypatch):
    admin = models.create_user("a@example.com", "pw", "a", role="admin")
    user = models.create_user("u@example.com", "pw", "u")
    monkeypatch.setattr(web_server, "_beijing_now", lambda: fixed_beijing(21, 5))
    started = []
    monkeypatch.setattr(
        web_server,
        "_start_claimed_daily_summary_thread",
        lambda date_str: started.append(date_str),
    )
    web_server._claim_daily_summary_run("2026-07-27", "scheduled")
    web_server._fail_daily_summary_run("2026-07-27", "generation_error", "bad output")
    client = web_server.app.test_client()

    denied = client.post(
        "/ai/daily-summary/retry",
        headers=headers(user["id"], "user"),
    )
    accepted = client.post(
        "/ai/daily-summary/retry",
        headers=headers(admin["id"], "admin"),
    )
    duplicate = client.post(
        "/ai/daily-summary/retry",
        headers=headers(admin["id"], "admin"),
    )

    assert denied.status_code == 403
    assert accepted.status_code == 202
    assert accepted.get_json()["status"] == "running"
    assert duplicate.status_code == 409
    assert started == ["2026-07-27"]
```

Add a completed-state test asserting admin retry returns 409.

- [ ] **Step 3: Run route tests**

Run:

```bash
python3 -m pytest -q tests/test_daily_summary_scheduling.py -k "today_is or manual_generation or only_admin"
```

Expected: FAIL because old user generation routes and job model remain.

- [ ] **Step 4: Implement the global today response**

Order:

1. compute Beijing date;
2. if global cache exists, return `completed` with summary, article count, stats, and updated time;
3. if before 21:00, return `scheduled`;
4. inspect `daily_summary_runs`;
5. return `running` or `failed`.

For failed state:

```python
payload = {
    "status": "failed",
    "date": today_str,
    "can_retry": g.user_role == "admin",
}
if g.user_role == "admin":
    payload["error_code"] = run["error_code"]
    payload["error_detail"] = run["error_detail"]
    payload["attempt_count"] = run["attempt_count"]
return jsonify(payload)
```

Ordinary users never receive error details.

- [ ] **Step 5: Disable ordinary manual generation**

Keep an explicit authenticated compatibility response:

```python
@app.route("/ai/daily-summary", methods=["POST"])
@require_role("user", "admin")
def ai_daily_summary_manual_disabled():
    return jsonify({
        "error": "manual daily summary generation is disabled"
    }), 405
```

Delete or stop calling:

- `_daily_summary_jobs`;
- `_run_daily_summary_job`;
- user-level daily cache read/write;
- `/ai/daily-summary/<job_id>` polling route.

Do not drop legacy SQLite tables during this release.

- [ ] **Step 6: Implement failed-only retry**

```python
def _start_claimed_daily_summary_thread(date_str: str) -> None:
    threading.Thread(
        target=_run_claimed_daily_summary,
        args=(date_str,),
        name=f"daily-summary-retry-{date_str}",
        daemon=True,
    ).start()


@app.route("/ai/daily-summary/retry", methods=["POST"])
@require_role("admin")
def ai_daily_summary_retry():
    today_str = _today_str()
    claimed = _claim_daily_summary_run(today_str, "admin_retry")
    if not claimed:
        return jsonify({
            "error": "daily summary is not in failed state"
        }), 409
    _start_claimed_daily_summary_thread(today_str)
    return jsonify({
        "status": "running",
        "date": today_str,
        "attempt_count": claimed["attempt_count"],
    }), 202
```

Delete the `/ai/daily-summary/send` route and its frontend caller. The removed
endpoint must return Flask's normal `404`; no compatibility endpoint may
force-generate or force-resend a summary.

- [ ] **Step 7: Run route tests**

Run:

```bash
python3 -m pytest -q tests/test_daily_summary_scheduling.py
```

Expected: PASS.

- [ ] **Step 8: Commit global status and retry API**

```bash
git add web_server.py tests/test_daily_summary_scheduling.py
git commit -m "feat(summary): expose global status and failed-only admin retry"
```

### Task 6: Update settings and ✨ frontend

**Files:**
- Modify: `frontend/index.html:565`
- Modify: `frontend/index.html:964-978`
- Modify: `frontend/index.html:1008-1022`
- Modify: `frontend/index.html:3540-3570`
- Modify: `frontend/index.html:3718-4015`
- Modify: `frontend/index.html:4741-4775`
- Modify: `tests/test_access_and_ui_contracts.py:1138-1145`
- Modify: `tests/test_daily_summary_scheduling.py`

**Interfaces:**
- Settings IDs: `dailySummaryInappToggle`, `dailySummaryEmailToggle`
- Produces: `openDailySummaryPushSettings(): void`
- Produces: `retryDailySummary(): Promise<void>`
- Consumes global statuses: `scheduled`, `running`, `completed`, `failed`

- [ ] **Step 1: Add failing frontend contracts**

Append to `tests/test_access_and_ui_contracts.py`:

```python
def test_daily_summary_has_independent_inapp_and_email_settings():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="dailySummaryInappToggle"' in html
    assert 'id="dailySummaryEmailToggle"' in html
    assert "每日摘要站内推送" in html
    assert "每日摘要邮箱推送" in html
    assert "每日摘要推送设置" in html
    assert "function openDailySummaryPushSettings()" in html


def test_daily_summary_has_no_user_generate_or_regenerate_controls():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'onclick="generateDailySummary()"' not in html
    assert ">重新生成<" not in html
    assert "function generateDailySummary()" not in html
    assert "function triggerDailySummary()" not in html


def test_daily_summary_retry_is_admin_only_and_failed_gated():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function retryDailySummary()" in html
    assert "dailySummaryState.canRetry" in html
    assert "authUser?.role === 'admin'" in html
```

- [ ] **Step 2: Add failing settings-route tests**

Append to `tests/test_daily_summary_scheduling.py`:

```python
def test_settings_default_inapp_on_and_email_off(daily_env):
    user = models.create_user("u@example.com", "pw", "u")
    response = web_server.app.test_client().get(
        "/settings",
        headers=headers(user["id"], "user"),
    )
    data = response.get_json()
    assert data["daily_summary_inapp_enabled"] is True
    assert data["daily_summary_enabled"] is False


def test_email_summary_cannot_be_enabled_without_recipient(daily_env):
    user = models.create_user("u@example.com", "pw", "u")
    response = web_server.app.test_client().put(
        "/settings",
        json={
            "daily_summary_enabled": 1,
            "notification_config": {"resend": {"to_email": ""}},
        },
        headers=headers(user["id"], "user"),
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "请先填写每日摘要接收邮箱"
```

- [ ] **Step 3: Run frontend/settings tests**

Run:

```bash
python3 -m pytest -q \
  tests/test_access_and_ui_contracts.py \
  tests/test_daily_summary_scheduling.py -k "daily_summary or settings_default or email_summary"
```

Expected: FAIL because old controls/state remain and the new setting is not exposed.

- [ ] **Step 4: Update notification settings markup and save/load**

Rename the existing email toggle ID and label:

```html
<input type="checkbox" id="dailySummaryEmailToggle" ...>
<span>📧 每日摘要邮箱推送</span>
```

Add:

```html
<div class="ai-toggle" onclick="document.getElementById('dailySummaryInappToggle').click()">
  <input type="checkbox" id="dailySummaryInappToggle" onclick="event.stopPropagation()">
  <span>🔔 每日摘要站内推送</span>
</div>
<p class="ai-hint">默认开启。关闭后仍可通过首页右上角 ✨ 查看摘要。</p>
```

Load/save both fields. Before saving an enabled email toggle, require a trimmed `notifyToEmail` and show `请先填写每日摘要接收邮箱`.

In `GET /settings` default response set:

```python
"daily_summary_inapp_enabled": True,
"daily_summary_enabled": False,
```

In `PUT /settings`, validate the final merged notification config and email flag; do not rely only on fields present in the current request.

- [ ] **Step 5: Replace the daily summary state model**

Use:

```js
let dailySummaryState = {
  status: 'scheduled',
  result: null,
  errorCode: '',
  errorDetail: '',
  canRetry: false,
  pollTimer: null,
};
```

`syncDailySummaryToday()` fetches only `/ai/daily-summary/today`. If status is `running`, schedule the next sync in two seconds; otherwise clear polling. Remove job IDs and user-level job endpoints.

- [ ] **Step 6: Render the four global states**

`scheduled` body:

```html
<div class="daily-summary-empty">
  <p>每日摘要将在北京时间每天晚 9 点（21:00）生成显示</p>
  <button class="ai-save-btn" onclick="openDailySummaryPushSettings()">每日摘要推送设置</button>
</div>
```

`running`: show `今日每日摘要正在生成，请稍候。`

`completed`: render the existing safe Markdown, generation time, and article count; no generate/regenerate button.

`failed`:

- ordinary user: `今日每日摘要暂未生成，请稍后查看。`
- administrator: additionally render escaped `errorCode` and `errorDetail`, plus:

```js
const adminRetry = authUser?.role === 'admin'
  ? `<button class="ai-save-btn" onclick="retryDailySummary()"
       ${dailySummaryState.canRetry ? '' : 'disabled'}>重试生成</button>`
  : '';
```

The same admin button may be rendered disabled in scheduled/running/completed states. It is enabled only when `canRetry` is true.

- [ ] **Step 7: Implement settings jump and retry**

```js
function openDailySummaryPushSettings() {
  closeDailySummary();
  openSettings();
  switchSettingsTab('notify');
}

async function retryDailySummary() {
  if (authUser?.role !== 'admin' || !dailySummaryState.canRetry) return;
  setDailySummaryState({ canRetry: false, status: 'running' });
  try {
    const resp = await fetch('/ai/daily-summary/retry', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + authToken },
    });
    const data = await parseJsonResponse(resp);
    if (!resp.ok) throw new Error(data.error || '重试提交失败');
    showToast('📊 已提交每日摘要重试');
    scheduleDailySummarySync(800);
  } catch (error) {
    showToast('❌ ' + (error.message || '重试提交失败'));
    await syncDailySummaryToday();
  }
}
```

- [ ] **Step 8: Reuse article-link handling in notifications**

Add the notification stacking class beside `.overlay.over-daily-summary`:

```css
.overlay.over-daily-summary,
.overlay.over-notification{z-index:500}
```

Replace `handleDailySummaryLinkClick()` with:

```js
function handleInAppArticleLinkClick(event, sourceOverlay) {
  const link = event.target.closest('a');
  if (!link) return;
  const match = (link.getAttribute('href') || '')
    .match(/#\/article\/(\d{2}-\d{2}-\d{2})-(\d+)/);
  if (!match) return;
  event.preventDefault();
  const articleId = parseInt(match[2], 10);
  const overlay = document.getElementById('overlay');
  overlay.classList.add(
    sourceOverlay === 'notification'
      ? 'over-notification'
      : 'over-daily-summary'
  );
  syncArticleHistory(articleId, '20' + match[1]);
  openArticle(articleId);
}

function bindInAppArticleLinks() {
  const targets = [
    ['dailySummaryBody', 'daily-summary'],
    ['notifBody', 'notification'],
  ];
  targets.forEach(([id, source]) => {
    const element = document.getElementById(id);
    if (!element || element.dataset.articleLinksBound) return;
    element.addEventListener(
      'click',
      event => handleInAppArticleLinkClick(event, source)
    );
    element.dataset.articleLinksBound = '1';
  });
}
```

Call `bindInAppArticleLinks()` once from the existing authenticated UI
initialization after both overlay elements exist. In the article close/reset
path remove both stacking classes:

```js
document.getElementById('overlay').classList.remove(
  'over-daily-summary',
  'over-notification'
);
```

Do not change sanitizer rules; only matching internal article hashes are
intercepted.

- [ ] **Step 9: Run frontend and route tests**

Run:

```bash
python3 -m pytest -q \
  tests/test_access_and_ui_contracts.py \
  tests/test_daily_summary_scheduling.py \
  tests/test_notifications.py \
  tests/test_frontend_refresh_behavior.py
```

Expected: PASS.

- [ ] **Step 10: Commit the global summary UI**

```bash
git add frontend/index.html web_server.py tests/test_access_and_ui_contracts.py tests/test_daily_summary_scheduling.py
git commit -m "feat(ui): show scheduled global daily summary and admin retry"
```

### Task 7: Documentation, full verification, and performance checks

**Files:**
- Modify: `README.md`
- Modify: `README.en.md`

**Interfaces:**
- Consumes all previous tasks
- Produces user/operator documentation and final evidence

- [ ] **Step 1: Update documentation**

Document:

- one global summary generated after 21:00 Beijing time;
- independent in-app and email preferences;
- in-app default on, email default off for new users;
- ordinary users cannot trigger generation;
- administrators may retry only after a failed generation;
- missing Resend affects email only, not generation or in-app delivery.

Remove wording that implies per-user generation or a configurable/manual send time.

- [ ] **Step 2: Run focused suites**

Run:

```bash
python3 -m pytest -q \
  tests/test_daily_summary.py \
  tests/test_daily_summary_scheduling.py \
  tests/test_notifications.py \
  tests/test_security_hardening.py \
  tests/test_access_and_ui_contracts.py
```

Expected: PASS.

- [ ] **Step 3: Run the full suite**

Run:

```bash
python3 -m pytest -q
```

Expected: PASS.

- [ ] **Step 4: Verify time and role matrices**

Inject `_beijing_now()` for:

1. 20:59 — no run row; scheduled copy and settings jump.
2. 21:00 — one running claim and one background worker.
3. Process restart at 21:05 during `completed` — no second generation.
4. Process restart at 21:05 during `failed` — no automatic retry.
5. Ordinary user in failed state — generic copy, no retry control, no error detail.
6. Administrator in failed state — classified reason and enabled retry.
7. Administrator after accepted retry — disabled button while running.
8. Completed — same Markdown for ✨ and each delivered notification.

- [ ] **Step 5: Verify distribution matrix**

Create four users:

| User | In-app | Email | Expected |
|---|---:|---:|---|
| A | on | off | one in-app, no email |
| B | off | on with address | no in-app, one email |
| C | on | on with address | one in-app, one email |
| D | off | off | ✨ access only |

Replay success distribution and confirm no duplicate in-app notification or successful email.

- [ ] **Step 6: Verify request-path performance**

During generation and bulk in-app delivery:

- repeatedly request `/api/news`;
- repeatedly request `/auth/refresh/status`;
- confirm both remain responsive;
- confirm AI and email network calls occur without an open SQLite transaction;
- confirm in-app fan-out uses one transaction.

- [ ] **Step 7: Commit docs**

```bash
git add README.md README.en.md
git commit -m "docs: explain global daily summary delivery"
```

## Completion Gate

Do not mark complete if a user count can increase AI generation calls, if `RESEND_API_KEY` gates global generation, if a successful summary can be manually regenerated, or if any non-admin response contains a generation failure detail.
