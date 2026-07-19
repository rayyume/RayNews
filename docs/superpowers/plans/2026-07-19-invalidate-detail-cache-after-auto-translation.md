# Invalidate Detail Cache After Automatic Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Invalidate a browser's cached article detail when automatic full-text translation completes, so reopening an article displays current translated content without a manual news refresh.

**Architecture:** Add a nullable translation-completion timestamp to the existing `ai_results` cache and expose only changed article IDs through an authenticated cursor endpoint. The frontend polls that endpoint alongside existing title updates, clears its per-article detail cache, and re-fetches an article if it is currently open.

**Tech Stack:** Python 3, Flask, SQLite, vanilla JavaScript, pytest, Node.js contract tests.

## Global Constraints

- Do not expose translated HTML through the update endpoint or bypass `share_view_translation`.
- Only completed translation saves advance the update marker; summary/title/error writes must not.
- Existing translation writeback, title updates, manual translation, settings, and article-list refresh behavior remain unchanged.
- Closed articles are cache-invalidated only; an open updated article is re-fetched and passed through the existing `autoDisplaySummary` flow.

---

### Task 1: Persist and publish translation-completion updates

**Files:**
- Modify: `web_server.py:2898-3160`
- Modify: `tests/test_ai_empty_content.py` or create `tests/test_translation_updates.py`

**Interfaces:**
- Produces: `ai_results.translation_updated_at TEXT` and authenticated `GET /ai/translation-updates?since=<cursor>`.
- Consumes: `_save_ai_result(article_id, translation=...)` and `@require_role("user", "admin")`.
- Returns: `{"items":[{"id": 42}], "cursor":"<timestamp>|42"}`.

- [ ] **Step 1: Write failing persistence and endpoint tests**

Create `tests/test_translation_updates.py` using the project's Flask test-client fixture pattern. Seed an `ai_results` row, call `_save_ai_result(42, translation=json.dumps({"title":"中文","html":"<p>译文</p>"}))`, and assert its `translation_updated_at` is non-empty. Call the update route as an authenticated user and assert the response has item `{"id": 42}` and a non-empty cursor. Then call the same route with that cursor and assert `items == []`.

Also assert `_save_ai_result(42, summary="摘要")` does not change the previously read `translation_updated_at`.

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
/config/.local/bin/pytest -q tests/test_translation_updates.py
```

Expected: FAIL because the table, save path, and route do not yet provide `translation_updated_at` updates.

- [ ] **Step 3: Implement the marker, migration, and cursor endpoint**

In `_init_ai_results_table`, add `translation_updated_at TEXT` to the create-table SQL and migration guard:

```python
if "translation_updated_at" not in cols:
    conn.execute("ALTER TABLE ai_results ADD COLUMN translation_updated_at TEXT")
```

Update `_save_ai_result` so an incoming non-`None` `translation` writes the current SQLite timestamp in both insert and conflict-update branches; an omitted translation preserves the existing timestamp:

```sql
translation_updated_at = CASE
    WHEN excluded.translation IS NOT NULL THEN strftime('%Y-%m-%d %H:%M:%f', 'now')
    ELSE translation_updated_at
END
```

Add `GET /ai/translation-updates` near `ai_get_result`. Parse `since` as the existing `timestamp|id` cursor format, query `ai_results` where `translation_updated_at IS NOT NULL` and is newer than the cursor, order by timestamp then article ID, cap at 500, and return article IDs plus the final cursor. Initialize an empty cursor by returning the current SQLite timestamp and no items. Decorate the route with `@require_role("user", "admin")`; never query or return `translation`.

- [ ] **Step 4: Run server regression tests**

Run:

```bash
/config/.local/bin/pytest -q tests/test_translation_updates.py tests/test_ai_empty_content.py tests/test_refresh_jobs.py
```

Expected: all pass.

- [ ] **Step 5: Commit server update publication**

```bash
git add web_server.py tests/test_translation_updates.py
git commit -m "fix(ai): publish completed translation updates"
```

### Task 2: Invalidate and refresh browser detail caches on translation updates

**Files:**
- Modify: `frontend/index.html:1010-1040, 5980-6040, 7350-7390`
- Modify: `tests/test_ai_relay_frontend.py`

**Interfaces:**
- Consumes: `GET /ai/translation-updates`, `articleBodyCache`, `fetchArticleDetail(id)`, `renderArticleBody(wrap, data, id)`, and `autoDisplaySummary(id)`.
- Produces: `pollTranslationUpdates()` and `applyTranslationUpdate(item)`.

- [ ] **Step 1: Write failing browser contract tests**

Add an extraction helper for the translation-update functions and a Node `vm` test with two cached article objects. For a closed updated ID, assert `articleBodyCache[id]` is deleted and `fetchArticleDetail` is not called. For an open updated ID, provide an overlay whose `dataset.articleId` matches the ID, stub `fetchArticleDetail` to return `{body_html: "<p>译文</p>"}`, and assert it is called once, `renderArticleBody` receives that result, and `autoDisplaySummary(id)` is called once.

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```bash
/config/.local/bin/pytest -q tests/test_ai_relay_frontend.py -k translation_update
```

Expected: FAIL because no translation-update cache invalidation functions exist.

- [ ] **Step 3: Implement polling and cache invalidation**

Add state beside the title-update state:

```javascript
let translationUpdateCursor = '';
let translationUpdatePolling = false;
```

Implement `applyTranslationUpdate(item)` to delete `articleBodyCache[item.id]` and `articleBodyPromises[item.id]`. If the open overlay is not displaying that ID, stop. Otherwise call `fetchArticleDetail(item.id)`, confirm the overlay still targets that ID, then call:

```javascript
renderArticleBody(document.getElementById('articleWrap'), data, item.id);
autoDisplaySummary(item.id);
```

Implement `pollTranslationUpdates()` with an in-flight guard, authenticated fetch of `/ai/translation-updates?since=…`, cursor update, and iteration over returned valid numeric IDs. Errors must be ignored like existing title polling so an optional update check never disrupts reading.

Invoke both `pollTitleUpdates()` and `pollTranslationUpdates()` from the existing 12-second interval and foreground-resume path. Do not change the existing title polling function or the 60-second article-list refresh.

- [ ] **Step 4: Run frontend and full regression tests**

Run:

```bash
/config/.local/bin/pytest -q tests/test_ai_relay_frontend.py
/config/.local/bin/pytest -q
```

Expected: all tests pass; the full suite may emit only the pre-existing `datetime.utcnow()` warning.

- [ ] **Step 5: Commit browser cache invalidation**

```bash
git add frontend/index.html tests/test_ai_relay_frontend.py
git commit -m "fix(ui): refresh article cache after automatic translation"
```

## Plan Self-Review

- Spec coverage: Task 1 supplies a non-sensitive, completed-translation update signal; Task 2 consumes it to invalidate closed details and refresh an open detail.
- Placeholder scan: no incomplete markers or deferred implementation steps are present.
- Type consistency: the server exposes `{id}` update items and the browser consumes `item.id` as the detail-cache key.
