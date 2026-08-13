# Delete Source Group Final Fix Report

Date: 2026-08-13
Base reviewed: `6f18c47`
Implementation commit: `d4c5598` (`fix: make source group deletion fully atomic`)

## Scope and protected files

This wave changed only the source-group deletion implementation, tests, and plan:

- `web_server.py`
- `source_categories.py`
- `tests/test_source_deletion.py`
- `tests/test_source_maintenance.py`
- `docs/superpowers/plans/2026-08-13-delete-source-group.md`

The pre-existing dirty files `network_safety.py` and `tests/test_network_safety.py`
were neither edited nor staged by this work. `git diff --cached --name-only` before
commit listed exactly the five scoped files above.

## Findings: RED / GREEN evidence

### Important 1 — strict `ai_results` deletion for source-group route

**RED**

```text
python3 -m pytest -q \
  tests/test_source_deletion.py::test_delete_source_group_rolls_back_non_missing_ai_results_error

FAILED: expected HTTP 500, received HTTP 200
1 failed
```

The injected `sqlite3.OperationalError("locked")` from `DELETE FROM ai_results`
was swallowed by the shared helper.

**GREEN**

- Added `strict_ai_results` to the caller-owned deletion helper.
- The source-group route passes `strict_ai_results=True`.
- Strict mode checks `sqlite_master`; an absent optional table is skipped, but any
  delete error propagates into the route rollback.
- The ordinary `_delete_article_ids()` path retains its legacy best-effort behavior.
- Endpoint failure assertions cover articles, tombstones, AI rows, global/user
  categories, global/user aliases, favorites, cache unpin calls, and transaction state.

```text
python3 -m pytest -q \
  tests/test_source_deletion.py::test_delete_source_group_rolls_back_non_missing_ai_results_error \
  tests/test_source_deletion.py::test_delete_article_ids_ignores_ai_results_operational_error_and_commits

2 passed
```

A separate endpoint test proves a genuinely missing `ai_results` table is accepted.

### Important 2 — transitive shared-alias closure

**RED**

```text
python3 -m pytest -q \
  tests/test_source_deletion.py::test_delete_source_group_resolves_transitive_shared_aliases

FAILED: expected 4 deleted articles, received 3
```

The original route resolved only direct aliases.

**GREEN**

Added `_resolve_shared_source_alias_closure()` using a recursive SQLite CTE with
`UNION`. It traverses reverse shared-alias edges to a fixed point. `UNION` both
deduplicates and terminates cycles without an arbitrary depth limit or schema change.
The endpoint test includes `Ancestor -> Bridge -> Primary` plus a cycle back to
`Ancestor`, and verifies all group articles, AI rows, categories, and aliases are
removed while unrelated/unsupplied articles remain.

```text
1 passed (as part of the four-contract run below)
```

### Important 3 — one `BEGIN IMMEDIATE` snapshot

**RED**

```text
python3 -m pytest -q \
  tests/test_source_deletion.py::test_delete_source_group_resolves_aliases_and_selects_articles_in_immediate_transaction

FAILED: alias SELECT observed with conn.in_transaction == False
```

**GREEN**

Both source metadata and news schema bootstraps now complete before deletion. The
route then executes `BEGIN IMMEDIATE`, resolves the alias closure, selects article
IDs, deletes article/tombstone/AI state, deletes metadata, and commits. A real
SQLite trace callback records each statement with `conn.in_transaction`; the test
proves both alias resolution and article selection occur after `BEGIN IMMEDIATE`
and while the connection is in a transaction.

```text
python3 -m pytest -q \
  tests/test_source_deletion.py::test_delete_source_group_resolves_transitive_shared_aliases \
  tests/test_source_deletion.py::test_delete_source_group_resolves_aliases_and_selects_articles_in_immediate_transaction \
  tests/test_source_deletion.py::test_delete_source_group_rolls_back_non_missing_ai_results_error \
  tests/test_source_deletion.py::test_delete_article_ids_ignores_ai_results_operational_error_and_commits

4 passed
```

### Minor 1 — narrow `delete_source_metadata` contract

**RED**

```text
python3 -m pytest -q \
  tests/test_source_maintenance.py::test_delete_source_metadata_requires_bootstrap_before_caller_transaction

FAILED: DID NOT RAISE RuntimeError
```

Calling the helper inside a caller-owned transaction with missing tables could invoke
an initializer that commits.

**GREEN**

The docstring now states the bootstrap precondition. When a transaction already
exists, the helper checks that all four metadata tables exist and raises before any
DML if not. Standalone calls may still bootstrap and own commit/rollback.

```text
1 passed
```

### Minor 2 — failure after partial metadata deletion

A SQLite trigger aborts deletion of the `Primary` category, after alias deletes have
already executed. The endpoint returns 500 and the test verifies rollback restores
shared/user aliases, shared/user categories, articles, tombstones, and AI results;
favorites remain and cache unpin is not called.

```text
python3 -m pytest -q \
  tests/test_source_deletion.py::test_delete_source_group_rolls_back_after_alias_delete_when_category_delete_fails

1 passed
```

### Minor 3 — implementation plan examples

Updated the helper example to distinguish standalone versus caller-owned transaction
boundaries and explicit bootstrap preconditions. Updated the route example to show
pre-transaction bootstrap, `BEGIN IMMEDIATE`, in-transaction alias closure/article
selection, strict AI cleanup, outer rollback, and post-commit cross-database/cache
side effects.

## Verification

Baseline before changes:

```text
python3 -m pytest -q \
  tests/test_source_deletion.py tests/test_source_maintenance.py \
  tests/test_news_schema.py tests/test_access_and_ui_contracts.py \
  tests/test_server_stats.py tests/test_news_db_thread_safety.py

175 passed, 13 warnings in 22.34s
```

Fresh post-commit required six-file run:

```text
python3 -m pytest -q \
  tests/test_source_deletion.py tests/test_source_maintenance.py \
  tests/test_news_schema.py tests/test_access_and_ui_contracts.py \
  tests/test_server_stats.py tests/test_news_db_thread_safety.py

181 passed, 18 warnings in 13.49s
```

Warnings are existing `datetime.utcnow()` deprecation warnings in application and
test code; there are zero test failures.

Additional direct run:

```text
python3 -m pytest -q tests/test_source_deletion.py tests/test_source_maintenance.py
23 passed, 9 warnings in 6.33s
```

`git diff --check` returned no whitespace errors.

## Independent review and self-review

Independent reviewer result for `d4c5598` versus `6f18c47`:

> No Critical/Important/Minor findings. Merge ready.

Self-review checklist:

- Strict route skips only an absent AI table; real AI write errors roll back: yes.
- Ordinary article helper remains best-effort: yes.
- Shared alias closure is transitive, deduplicated, and cycle-safe: yes.
- Bootstrap/migration are pre-transaction: yes.
- Closure, selection, and all news writes share `BEGIN IMMEDIATE`: yes.
- Metadata helper cannot bootstrap/commit inside caller transaction: yes.
- Favorites/cache side effects occur only after commit: yes.
- Protected files neither staged nor committed: yes.

## Risk assessment

Residual risks are low:

- Recursive CTE work scales with the existing shared-alias graph. There is no new
  arbitrary depth limit; `UNION` prevents cycle repetition.
- `BEGIN IMMEDIATE` intentionally acquires the news DB write reservation before
  resolution, strengthening consistency at the cost of normal writer serialization.
  Existing busy-timeout behavior remains unchanged.
- The API error text remains the existing generic metadata-deletion message even
  when the actual failing news step is AI cleanup; rollback semantics are correct.
- The focused suite reports only existing deprecation warnings.

## Outcome

All requested Critical/Important/Minor findings are addressed in `d4c5598`.
