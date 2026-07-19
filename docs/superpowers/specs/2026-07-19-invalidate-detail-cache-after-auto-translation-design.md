# Invalidate Detail Cache After Automatic Translation

## Goal

Ensure a browser session stops reusing an English article-detail cache after the
server completes automatic full-text translation, without requiring the reader to
manually refresh the news list.

## Root Cause

The automatic translation worker writes translated HTML to `articles.body_html` and
evicts the refresh server's detail cache. The browser separately keeps
`articleBodyCache[articleId]` indefinitely. Its existing update poll reports title
changes only, so a content-only translation never invalidates that browser cache.

## Design

### Server-side update marker

Add `translation_updated_at` to `ai_results`. When the automatic translation worker
saves a completed translation cache payload, set it to SQLite's current timestamp.
Existing rows receive `NULL` and do not create a spurious update.

Expose a lightweight authenticated endpoint, `GET /ai/translation-updates?since=…`,
which returns article IDs whose `translation_updated_at` is newer than the cursor,
plus a cursor. It returns no translation text, so it does not bypass the existing
per-user shared-result visibility controls.

### Browser invalidation

Poll the endpoint on the existing 12-second update cadence. For every returned
article ID, delete `articleBodyCache[id]` and any in-flight cache entry. If that
article is currently open, re-fetch its detail and run the existing cached-result
display flow; otherwise the next open fetches the current server detail.

### Behaviour and boundaries

- The server still writes only completed translations; no partial text is exposed.
- The translation data remains protected by `share_view_translation` when the
detail page requests `/ai/result/<id>`.
- Title update handling, manual translation, article-list refresh, and the current
automatic-translation worker selection logic remain unchanged.

## Tests

1. Database migration and save path set `translation_updated_at` only for completed
translation payloads.
2. Translation-update endpoint returns ordered IDs/cursor and correctly resumes from
the supplied cursor.
3. Frontend contract test verifies an update evicts the named detail cache entry and
re-fetches a currently open article, while a closed article is only evicted.
