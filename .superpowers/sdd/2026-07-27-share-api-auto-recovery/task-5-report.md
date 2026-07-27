# Task 5 report — complete regression and transition verification

## Result

A full-suite regression was found and fixed before completion. Concurrent first
access to an older `news.db` could run the PRAGMA/ALTER migrations in parallel;
the second connection then raised `OperationalError: duplicate column name`
(`original_title` / `title_updated_at`). `web_server._ensure_news_schema` is
now protected by a process-local re-entrant lock, covering all callers
(including article metadata and maintenance routes). The same lock guards
per-thread connection initialization.

- Code commit: `0c2259a fix(db): serialize concurrent news schema upgrades`
- No sharing intent, access, or notification behavior was changed by this fix.

## RED → GREEN evidence

- Initial full-suite run: `255 passed, 1 failed` at
  `tests/test_news_db_thread_safety.py::test_concurrent_reads_and_writes_do_not_corrupt_transactions`.
- Reproduction: a second isolated run failed with the same duplicate-column
  race.
- Green: the concurrent regression test passed 10 consecutive isolated runs.
- All suite batches below passed against the fixed code.

## Required security/notification suites

The requested combined command exceeded the execution harness window, so its
five mutually exclusive files were rerun individually after the fix:

| Suite | Result |
| --- | --- |
| `test_share_recovery.py` | 40 passed (fresh later batch: 40 included in 46) |
| `test_notifications.py` | 10 passed |
| `test_security_hardening.py` | 9 passed |
| `test_ai_relay_frontend.py` | 15 passed |
| `test_access_and_ui_contracts.py` | 75 passed |

Total: **149 passed** (one pre-existing deprecation warning from
`source_categories.py`).

## Full suite

A single `python3 -m pytest -q` invocation was attempted twice; the harness
ended it around 26 seconds without an exit result (the second had progressed
past 48%). Per the task brief, the collected suite was then run in disjoint
file batches. All **446 collected tests** passed:

| Batch | Result |
| --- | --- |
| access/UI, AI chat/empty/frontend | 105 passed |
| image viewer, translation completion, daily summary, fetch, frontend refresh, fulltext, image cache | 149 passed |
| news DB/schema/search, notification broadcast | 22 passed |
| notifications, refresh auth/jobs, review hardening, security, server stats | 78 passed |
| share recovery, source maintenance/split | 46 passed |
| streaming refresh, title processing, translation updates, user role migration | 46 passed |

## Manual transition matrix (isolated temporary user/database)

An API-level manual exercise passed:

1. Enabled sharing with title+summary on and translation off; a 401 pause kept
   all three intent values (`1, 1, 0`), reported `share_active: false`, and
   removed cached summary/translation from `/ai/result`.
2. The pause produced exactly one in-app `share_suspended` notification and one
   email attempt; repeating the failed result produced neither duplicate.
3. Saving a working API immediately restored the exact intent, produced one
   `share_restored` notification, and one email attempt.
4. A later pause followed by explicit master opt-out remained opt-out after a
   successful manual connection test; that test returned no `share_check`.

Evidence output: `EMAIL_ATTEMPTS=3` for pause, restore, second pause; notification
sequence was `share_suspended`, `share_restored`, `share_suspended`.

Effective gates are covered by `test_share_recovery.py` and the Node-driven
frontend contract tests: inactive/paused sharing hides summary and translation,
and `displayTitle` requires both `share_active` and `share_view_title` (including
favorites/source-history original-title fallbacks). No Playwright project or
configuration exists in this repository; the available executable frontend
matrix is the existing Node test harness, which passed.

## Connectivity/cadence evidence

An isolated two-user periodic revalidation exercise recorded provider calls
while `models.get_db().in_transaction == False` for both users and observed
sleep calls exactly `[0.5, 0.5]`. Static call-site inspection found personal
connection probes only in config save, manual test, admin system test, periodic
loop, and settings validation—none in the news/homepage list handlers.

## Completion gate review

- `is_share_active` requires master intent, current matching config revision,
  successful health, and no suspension; no `share_view_*` switch alone grants
  access.
- Failure transitions update only health/suspension fields; they do not zero
  the user’s intent fields.
- Stale-revision and concurrent transition cases are present in the passing
  share-recovery suite.

## Concerns

- Existing deprecation warnings remain for `datetime.utcnow()` in `models.py`
  and `source_categories.py`; they are unrelated to this task.
- The environment’s single-command execution window prevents observing a full
  suite exit, hence the documented mutually exclusive batch evidence.
