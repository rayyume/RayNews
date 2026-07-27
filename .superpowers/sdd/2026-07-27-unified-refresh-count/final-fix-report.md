# Unified Refresh Count — Final Fix Report

## Status

READY for final review. Both Important findings from `final-review.md` are fixed with regression coverage.

## Strict TDD evidence

### RED

The first focused run against the unchanged production code failed in all three required paths:

- deleted article: progress reported `inserted == 4` instead of `3`;
- failed-cycle retry: running status reported `new_count_so_far == 3` instead of `1`;
- delayed flow A → cancel → flow B: B's final label was empty instead of `1`, proving A polluted `seenArticleIds` and suppressed B.

Additional lifecycle tests were written and observed failing before their implementations:

- cancellation during source-metadata cache await still rebuilt/rendered stale and fresh categories;
- cancellation during today's-count JSON await did not abort the combined request signal;
- a cancelled source-metadata retry still performed network/category/UI mutations;
- a cancelled idle-latest cache write still changed `currentPage` and applied UI.

### GREEN

Each regression was rerun after its minimal implementation and passed. The deleted-ID test uses five inputs with batch size two, covering both size-triggered commits and the trailing partial batch.

## Implementation

### Exact running IDs

- `upsert_articles()` now returns only positive IDs that were actually persisted after deleted-article filtering, using the existing upsert transaction/path and no added query.
- Streaming progress accumulates those returned IDs only after a successful commit; `inserted` matches the cumulative unique persisted-ID list. Atomic replacement and trailing-batch ordering are unchanged.
- The refresh server stores the already-existing pre-job `article_id_snapshot()` as an internal baseline and subtracts it from matching-job running progress. This excludes `INSERT OR REPLACE` rows from failed-cycle retries without a status-time DB scan.
- Old progress payloads without `inserted_ids` retain their diagnostic numeric `new_count_so_far`; no IDs are guessed. Internal baseline state is removed from current and historical JSON payloads.
- Browser regressions prove attempted/deleted/retry numeric counts cannot inflate the authoritative Set, button label, or completion Toast.

### Flow-bound immediate `loadSince()`

- The manual immediate call and completion fallback both receive the current flow's signal and generation/lifecycle guard.
- The local 8-second timeout controller is linked to the external flow signal and listeners/timers are removed in `finally`.
- `loadSince()` checks lifecycle state after every await and before discovery/global/pending/epoch/category/page/UI mutation.
- Source metadata loading/retry, today's count refresh, and idle latest application accept the same signal/guard, recheck after their awaits, and suppress late category/page/UI/global mutation.
- `triggerRefresh()` rechecks the flow immediately after awaiting the immediate request.
- The delayed A → cancel → B regression proves A leaves `seenArticleIds`, timestamp, pending state, epoch, categories/pages, label, and Toast untouched, while B counts and Toasts the shared ID exactly once.

## No-extra-work audit

- No client request, refresh-status poll, or `loadSince()` call was added.
- No database query or status-time scan was added.
- Existing pre/post job snapshots, progress file, start request, immediate request, and poll loop are reused.
- The existing request-cardinality regression remains green (one start / one poll / one immediate check).

## Verification

Focused refresh suites, split to keep feedback bounded:

- `tests/test_streaming_refresh.py tests/test_refresh_jobs.py`: **36 passed**
- `tests/test_refresh_auth_proxy.py`: **15 passed**
- `tests/test_frontend_refresh_behavior.py`: **114 passed**
- Focused total: **165 passed**

The full suite was exhaustively covered with mutually exclusive test-file batches:

- focused files above: **165 passed**
- `test_access_and_ui_contracts.py`: **75 passed**
- AI/article/daily/fetch batch: **49 passed**
- fulltext/image/news/notification batch: **44 passed**
- hardening/security/stats/source/title/translation/users batch: **69 passed**
- Full union: **402 passed**, **0 failed**

Other checks:

- `git diff --check`: PASS
- Added-request/query diff audit: PASS

## Self-review

- Running IDs are job-isolated, positive, normalized, cumulative, and exact against both tombstones and the pre-job baseline.
- Count and ID publication remain post-commit and atomic; partial trailing batches use the same path.
- Terminal `new_ids` remains the authoritative post-minus-pre snapshot and Set union still deduplicates overlap.
- Cancellation guards cover the initial fetch/JSON, source cache/network/retry, page fetch/cache, idle scroll/apply, today's count, and all final UI/pending/global mutations.
- No success path formats the Toast from numeric job counts.

## Concerns

No blocking concerns. The full run emits the existing 20 `datetime.utcnow()` deprecation warnings; they are unrelated to this fix.
