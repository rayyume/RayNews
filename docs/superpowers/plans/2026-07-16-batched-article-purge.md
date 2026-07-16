# Batched Article Purge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely purge large article sets with progress and administrator email notifications while improving server-stat visualization.

**Architecture:** Replace per-article cache-maintenance threads with one bounded background purge worker and a task registry. The worker deletes 200 articles per transaction, maintains image-cache mappings through one retrying cache operation per batch, and sends a terminal email to the initiating administrator. The server-stat endpoint exposes ring-chart-ready CPU/memory/disk ratios; the frontend renders the ordered cards and sorted storage rows.

**Tech Stack:** Python 3.12, Flask, SQLite, Resend notifier, vanilla JavaScript/CSS, pytest + Node VM contract tests.

## Global Constraints

- Exclude favorited articles and preserve images referenced by retained articles.
- Never start one SQLite cache writer per article.
- Every terminal cleanup outcome attempts an email to the initiating administrator.
- Keep the HTTP purge endpoint non-blocking for a 10,000-article purge.

---

### Task 1: Batch image-cache maintenance

**Files:**
- Modify: `image_cache.py:315-335,473-527`
- Modify: `tests/test_server_stats.py`

**Interfaces:**
- Produces: `unpin_article_images(article_ids: int | list[int]) -> None`, accepting a batch.
- Produces: bounded SQLite retry helper used by purge cache writes.

- [ ] **Step 1: Write failing tests**

```python
def test_unpin_many_uses_one_connection_and_one_pinned_recompute(monkeypatch):
    # pass [1, 2, 3]; assert one DELETE ... IN (...) and one UPDATE occur
    ...

def test_unpin_retries_locked_database_then_commits(monkeypatch):
    # first execute raises OperationalError('database is locked'), second succeeds
    ...
```

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest -q tests/test_server_stats.py -k 'unpin_many or retries_locked'`
Expected: FAIL because batch unpin and retry do not exist.

- [ ] **Step 3: Implement**

```python
def unpin_article_images(article_ids: int | list[int]) -> None:
    ids = [article_ids] if isinstance(article_ids, int) else sorted(set(article_ids))
    # DELETE mappings with one IN clause, recompute pinned once, commit;
    # retry only SQLITE locked/busy failures with bounded exponential delays.
```

- [ ] **Step 4: Verify green**

Run: `python3 -m pytest -q tests/test_server_stats.py -k 'unpin_many or retries_locked'`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add image_cache.py tests/test_server_stats.py
git commit -m "fix: batch image cache unpin operations"
```

### Task 2: Background chunked purge and completion emails

**Files:**
- Modify: `web_server.py:3203-3250,3462-3545`
- Modify: `notifier.py`
- Modify: `tests/test_server_stats.py`

**Interfaces:**
- Produces: `start_article_purge(before_date: str, deleted_by: int) -> dict`.
- Produces: `get_article_purge_status(task_id: str) -> dict`.
- Produces: `send_purge_completion_email(to_email: str, result: dict) -> dict`.

- [ ] **Step 1: Write failing tests**

```python
def test_large_purge_uses_200_article_chunks_and_returns_running_task(): ...
def test_purge_terminal_success_sends_email_to_requesting_admin(): ...
def test_purge_terminal_failure_still_attempts_email(): ...
def test_purge_worker_does_not_spawn_one_unpin_thread_per_article(): ...
```

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest -q tests/test_server_stats.py -k 'purge and (chunks or email or thread)'`
Expected: FAIL because purge is synchronous with per-article cache threads and has no task/email state.

- [ ] **Step 3: Implement**

```python
PURGE_BATCH_SIZE = 200
# Snapshot non-favorite candidates; create task; daemon worker deletes each batch.
# On every terminal path: mark task state, call send_purge_completion_email,
# catch only email-delivery errors, and preserve task result.
```

- [ ] **Step 4: Verify green**

Run: `python3 -m pytest -q tests/test_server_stats.py -k 'purge'`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web_server.py notifier.py tests/test_server_stats.py
git commit -m "feat: run article purge in bounded background batches"
```

### Task 3: Ring-ready server stats and storage ordering

**Files:**
- Modify: `web_server.py:3339-3460`
- Modify: `frontend/index.html:359-362,2898-2941`
- Modify: `tests/test_server_stats.py`
- Modify: `tests/test_frontend_refresh_behavior.py`

**Interfaces:**
- Extends `container` JSON with non-null initial `cpu_percent` and `cpu_capacity_percent`.
- Frontend `ring(percent, primary, secondary, label)` renders one stat card.

- [ ] **Step 1: Write failing tests**

```python
def test_container_stats_returns_zero_cpu_before_second_sample(): ...
def test_server_stats_frontend_orders_cpu_memory_disk_data_and_sorts_storage(): ...
def test_storage_row_displays_percent_of_disk_total(): ...
```

- [ ] **Step 2: Verify red**

Run: `python3 -m pytest -q tests/test_server_stats.py tests/test_frontend_refresh_behavior.py -k 'container or storage or server_stats'`
Expected: FAIL because CPU can be null and the frontend uses unsorted rectangular boxes.

- [ ] **Step 3: Implement**

```js
const rows = [...].sort((a, b) => b.bytes - a.bytes);
// CPU, memory and disk cards use conic-gradient rings and show used / total below.
```

- [ ] **Step 4: Verify green**

Run: `python3 -m pytest -q tests/test_server_stats.py tests/test_frontend_refresh_behavior.py -k 'container or storage or server_stats'`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web_server.py frontend/index.html tests/test_server_stats.py tests/test_frontend_refresh_behavior.py
git commit -m "feat: improve server resource statistics"
```

### Task 4: Final integration verification

**Files:**
- Modify: only files required by test corrections.

- [ ] **Step 1: Run formatting/diff checks**

Run: `git diff --check`
Expected: no output and exit 0.

- [ ] **Step 2: Run complete suite**

Run: `python3 -m pytest -q`
Expected: all tests pass.

- [ ] **Step 3: Commit any final test-only correction**

```bash
git add -A && git commit -m "test: verify batched purge and resource stats"
```
