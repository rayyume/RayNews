# Unified Manual Refresh Count Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Count every article surfaced by a manual refresh exactly once across the immediate database check and the current fetch job, then show that same total on the button and completion Toast.

**Architecture:** Extend the existing progress file/status payload with article IDs already available in the fetch pipeline. The browser owns one `Set<number>` per refresh flow and merges IDs from `loadSince`, running progress, and terminal status without adding requests or polling.

**Tech Stack:** Python, SQLite ID snapshots, JSON progress file, vanilla JavaScript, pytest, Node `vm`.

## Global Constraints

- Work on `dev`; line numbers refer to `dev@7fa5b56`.
- Count all sources and categories, independent of the active filter.
- Never compute the total by adding `immediate_count + new_count_so_far`.
- Do not add a network request or a refresh-status poll.
- Keep `new_count` and `new_count_so_far` for compatibility and diagnostics.
- Button and success Toast must use the same per-flow ID set.
- A cancelled/stale flow must never update a later flow's label or Toast.

---

## File Structure

- Modify: `fetcher.py` — write cumulative inserted article IDs to the existing atomic progress file.
- Modify: `refresh_server.py` — expose running and terminal ID lists.
- Modify: `frontend/index.html` — collect immediate and job IDs in one refresh-flow set.
- Modify: `tests/test_streaming_refresh.py` — progress/status ID contracts.
- Modify: `tests/test_refresh_jobs.py` — terminal ID-difference contract.
- Modify: `tests/test_frontend_refresh_behavior.py` — Set-based UI behavior and no-extra-request contract.

### Task 1: Add cumulative inserted IDs to fetch progress

**Files:**
- Modify: `fetcher.py:248-266`
- Modify: `fetcher.py:1152-1191`
- Modify: `tests/test_streaming_refresh.py:19-42`

**Interfaces:**
- Produces: `write_fetch_progress(inserted: int, total: int, inserted_ids: list[int] | None = None) -> None`
- Produces JSON: `inserted_ids: number[]`
- Consumes: existing `FETCH_JOB_ID`, `PROGRESS_FILE`

- [ ] **Step 1: Write failing progress-file tests**

Update the first test in `tests/test_streaming_refresh.py` and add normalization coverage:

```python
def test_write_fetch_progress_writes_atomic_json(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "FETCH_JOB_ID", "job-42")

    fetcher.write_fetch_progress(3, 10, [103, 101, 103, 102])

    payload = json.loads(fetcher.PROGRESS_FILE.read_text(encoding="utf-8"))
    assert payload["inserted"] == 3
    assert payload["inserted_ids"] == [101, 102, 103]
    assert payload["total_messages"] == 10
    assert payload["job_id"] == "job-42"
    assert payload["updated_at"] > 0
    assert not fetcher.PROGRESS_FILE.with_suffix(".json.tmp").exists()


def test_write_fetch_progress_drops_invalid_ids(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "FETCH_JOB_ID", "job-42")

    fetcher.write_fetch_progress(2, 2, [7, "8", 0, -1, None, "bad"])

    payload = json.loads(fetcher.PROGRESS_FILE.read_text(encoding="utf-8"))
    assert payload["inserted_ids"] == [7, 8]
```

- [ ] **Step 2: Run tests and verify the signature failure**

Run:

```bash
python3 -m pytest -q tests/test_streaming_refresh.py::test_write_fetch_progress_writes_atomic_json tests/test_streaming_refresh.py::test_write_fetch_progress_drops_invalid_ids
```

Expected: FAIL because `write_fetch_progress()` accepts only two arguments.

- [ ] **Step 3: Implement normalized cumulative IDs**

Change the function:

```python
def write_fetch_progress(
    inserted: int,
    total: int,
    inserted_ids: list[int] | None = None,
) -> None:
    normalized_ids = []
    for value in inserted_ids or []:
        try:
            article_id = int(value)
        except (TypeError, ValueError):
            continue
        if article_id > 0:
            normalized_ids.append(article_id)
    payload = {
        "pid": os.getpid(),
        "job_id": FETCH_JOB_ID,
        "inserted": inserted,
        "inserted_ids": sorted(set(normalized_ids)),
        "total_messages": total,
        "updated_at": int(time.time()),
    }
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PROGRESS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(PROGRESS_FILE)
    except Exception as e:
        log.warning(f"Could not write fetch progress: {e}")
```

- [ ] **Step 4: Accumulate IDs in the streaming loop**

Beside `inserted_total = 0`, add:

```python
inserted_ids: list[int] = []
```

Initialize progress with:

```python
write_fetch_progress(0, len(messages), [])
```

For each committed `stream_batch`, capture IDs before clearing it:

```python
committed_ids = [int(entry["id"]) for entry in stream_batch if int(entry.get("id", 0) or 0) > 0]
upsert_articles(stream_conn, stream_batch, sync_sources=False)
inserted_total += len(stream_batch)
inserted_ids.extend(committed_ids)
stream_batch = []
last_commit_at = time.monotonic()
write_fetch_progress(inserted_total, len(messages), inserted_ids)
```

Apply the same ordering to the trailing batch. Do not add IDs until `upsert_articles()` succeeds.

- [ ] **Step 5: Extend the streaming-ingest assertion**

In `test_run_streams_articles_into_sqlite_in_batches_before_cycle_completes`, add:

```python
assert progress["inserted_ids"] == [1, 2, 3, 4, 5, 6]
assert progress["inserted"] == len(progress["inserted_ids"])
```

- [ ] **Step 6: Run streaming tests**

Run:

```bash
python3 -m pytest -q tests/test_streaming_refresh.py
```

Expected: PASS.

- [ ] **Step 7: Commit progress IDs**

```bash
git add fetcher.py tests/test_streaming_refresh.py
git commit -m "feat(refresh): include article ids in fetch progress"
```

### Task 2: Expose running and terminal refresh IDs

**Files:**
- Modify: `refresh_server.py:43-56`
- Modify: `refresh_server.py:246-258`
- Modify: `refresh_server.py:290-323`
- Modify: `tests/test_streaming_refresh.py:222-307`
- Modify: `tests/test_refresh_jobs.py:200-217`

**Interfaces:**
- Produces running payload: `new_ids_so_far: number[]`
- Produces terminal payload: `new_ids: number[]`
- Consumes: `fetch_progress.json.inserted_ids`, `article_id_snapshot()`

- [ ] **Step 1: Write failing running-status tests**

Change `test_status_reports_new_count_so_far_while_running` progress payload and assertions:

```python
(tmp_path / "fetch_progress.json").write_text(
    json.dumps({
        "job_id": "job-1",
        "inserted": 3,
        "inserted_ids": [9, 7, 8, 8],
        "total_messages": 12,
        "updated_at": started_at + 1,
    }),
    encoding="utf-8",
)

payload = json.loads(refresh_server.get_refresh_job_status())

assert payload["new_count_so_far"] == 3
assert payload["new_ids_so_far"] == [7, 8, 9]
```

Add:

```python
def test_status_sanitizes_progress_ids(tmp_path, monkeypatch):
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    (tmp_path / "fetch_progress.json").write_text(
        json.dumps({
            "job_id": "job-1",
            "inserted": 2,
            "inserted_ids": [3, "4", 0, None, "bad"],
        }),
        encoding="utf-8",
    )
    payload = json.loads(refresh_server.get_refresh_job_status())
    assert payload["new_ids_so_far"] == [3, 4]
```

- [ ] **Step 2: Write the failing terminal-ID test**

Update `test_refresh_job_reports_new_count`:

```python
def test_refresh_job_reports_new_count_and_ids(monkeypatch):
    reset_job(monkeypatch)
    snapshots = iter(({1, 2}, {1, 2, 3, 4}))
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(
        refresh_server,
        "run_fetcher",
        lambda: (json.dumps({"status": "ok"}).encode(), 200),
    )
    refresh_server.start_refresh_job("manual")
    payload = wait_terminal()
    assert payload["status"] == "completed"
    assert payload["new_count"] == 2
    assert payload["new_ids"] == [3, 4]
```

- [ ] **Step 3: Run focused tests and verify missing fields**

Run:

```bash
python3 -m pytest -q \
  tests/test_streaming_refresh.py::test_status_reports_new_count_so_far_while_running \
  tests/test_streaming_refresh.py::test_status_sanitizes_progress_ids \
  tests/test_refresh_jobs.py::test_refresh_job_reports_new_count_and_ids
```

Expected: FAIL because the status serializers do not expose either ID field.

- [ ] **Step 4: Add a sanitizer and running payload**

In `refresh_server.py`:

```python
def _positive_article_ids(values) -> list[int]:
    result = set()
    for value in values or []:
        try:
            article_id = int(value)
        except (TypeError, ValueError):
            continue
        if article_id > 0:
            result.add(article_id)
    return sorted(result)
```

Inside `_refresh_job_json_locked()`:

```python
if progress and progress.get("job_id") and progress.get("job_id") == payload.get("job_id"):
    payload["new_count_so_far"] = progress.get("inserted", 0)
    payload["new_ids_so_far"] = _positive_article_ids(progress.get("inserted_ids"))
```

Do not expose IDs from a mismatched or missing `job_id`.

- [ ] **Step 5: Store the terminal ID difference**

In `_run_refresh_job()`:

```python
before_ids = article_id_snapshot()
...
after_ids = article_id_snapshot()
new_ids = sorted(after_ids - before_ids)
new_count = len(new_ids)
```

Initialize `new_ids = []` before the try and include it in the terminal update:

```python
REFRESH_JOB.update({
    "status": "completed" if completed else "failed",
    "finished_at": int(time.time()),
    "new_count": new_count,
    "new_ids": new_ids if completed else [],
    "error": error,
})
```

Add `"new_ids": []` to the initial `REFRESH_JOB` dictionary and every new job reset in `start_refresh_job()`. Update test reset fixtures likewise.

- [ ] **Step 6: Run refresh-server tests**

Run:

```bash
python3 -m pytest -q tests/test_streaming_refresh.py tests/test_refresh_jobs.py
```

Expected: PASS.

- [ ] **Step 7: Commit status IDs**

```bash
git add refresh_server.py tests/test_streaming_refresh.py tests/test_refresh_jobs.py
git commit -m "feat(refresh): expose running and terminal article ids"
```

### Task 3: Feed immediate discoveries into a per-flow ID Set

**Files:**
- Modify: `frontend/index.html:5100-5258`
- Modify: `frontend/index.html:6128-6187`
- Modify: `tests/test_frontend_refresh_behavior.py:790-1370`

**Interfaces:**
- Extends: `loadSince(timestamp, {forceApply?: boolean, manual?: boolean, onDiscovered?: (items: object[]) => void}): Promise<number>`
- Produces: `mergeRefreshArticleIds(target: Set<number>, values: unknown): number`
- Produces: `renderRefreshDiscoveredCount(ids: Set<number>): void`
- Consumes: `status.new_ids_so_far`, `status.new_ids`

- [ ] **Step 1: Add pure Set-merging tests**

Add to `tests/test_frontend_refresh_behavior.py`:

```python
def test_refresh_id_merge_normalizes_and_deduplicates():
    helpers = source_between(
        "function mergeRefreshArticleIds(",
        "async function triggerRefresh()",
    )
    run_node(
        helpers,
        """
const ids = new Set();
assert.equal(context.mergeRefreshArticleIds(ids, [2, '3', 2, 0, null, 'bad']), 2);
assert.deepEqual(Array.from(ids).sort((a, b) => a - b), [2, 3]);
assert.equal(context.mergeRefreshArticleIds(ids, [{id: 3}, {id: 4}]), 3);
assert.deepEqual(Array.from(ids).sort((a, b) => a - b), [2, 3, 4]);
""",
    )
```

- [ ] **Step 2: Add a failing `loadSince` discovery-callback test**

Copy the existing `test_manual_incremental_check_applies_immediately_despite_refresh_in_progress` setup and change the invocation/assertions:

```js
context.discovered = [];
const added = await context.loadSince(100, {
  manual: true,
  onDiscovered: items => context.discovered.push(...items.map(item => item.id)),
});
assert.equal(added, 1);
assert.deepEqual(context.discovered, [2]);
```

Also add a zero-result assertion that `onDiscovered` is not called.

- [ ] **Step 3: Run focused tests**

Run:

```bash
python3 -m pytest -q \
  tests/test_frontend_refresh_behavior.py::test_refresh_id_merge_normalizes_and_deduplicates \
  tests/test_frontend_refresh_behavior.py::test_manual_incremental_check_applies_immediately_despite_refresh_in_progress
```

Expected: FAIL because the merge helper and callback do not exist.

- [ ] **Step 4: Implement the merge helper**

Place immediately before `triggerRefresh()`:

```js
function mergeRefreshArticleIds(target, values) {
  if (!(target instanceof Set) || !Array.isArray(values)) return target instanceof Set ? target.size : 0;
  values.forEach(value => {
    const raw = value && typeof value === 'object' ? value.id : value;
    const id = Number(raw);
    if (Number.isInteger(id) && id > 0) target.add(id);
  });
  return target.size;
}

function renderRefreshDiscoveredCount(ids) {
  setRefreshProgressLabel(ids instanceof Set ? ids.size : 0);
}
```

- [ ] **Step 5: Extend `loadSince` without changing its return type**

Change the signature:

```js
async function loadSince(
  timestamp,
  { forceApply = false, manual = false, onDiscovered = null } = {},
) {
```

Immediately after `if (!added) return 0;`:

```js
if (typeof onDiscovered === 'function') onDiscovered(newItems.slice());
```

Do not call the callback for items rejected by `seenArticleIds` or the timestamp guard.

- [ ] **Step 6: Run focused tests**

Run:

```bash
python3 -m pytest -q tests/test_frontend_refresh_behavior.py -k "refresh_id_merge or incremental_check"
```

Expected: PASS.

- [ ] **Step 7: Commit the collector interface**

```bash
git add frontend/index.html tests/test_frontend_refresh_behavior.py
git commit -m "refactor(refresh): expose immediate article discoveries"
```

### Task 4: Make button and completion Toast use the same Set

**Files:**
- Modify: `frontend/index.html:5100-5231`
- Modify: `tests/test_frontend_refresh_behavior.py:790-1220`
- Modify: `tests/test_frontend_refresh_behavior.py:3300-3335`

**Interfaces:**
- Consumes: `loadSince(...onDiscovered)`, `new_ids_so_far`, `new_ids`
- Produces: one authoritative count for `setRefreshProgressLabel()` and success Toast

- [ ] **Step 1: Add the overlap regression test**

Add:

```python
def test_manual_refresh_unifies_immediate_progress_and_terminal_ids_without_double_counting():
    helpers = source_between(
        "function mergeRefreshArticleIds(",
        "async function triggerRefresh()",
    )
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 2, new_ids: [2, 3] };"
    )
    run_node(
        helpers + trigger,
        setup
        + """
context.window = { scrollY: 0 };
context.labels = [];
context.setRefreshProgressLabel = count => context.labels.push(count);
context.applyStreamedRefreshBatch = async () => true;
context.loadSince = async (timestamp, options) => {
  options.onDiscovered([{ id: 1 }, { id: 2 }]);
  return 2;
};
context.pollRefreshJob = async (jobId, timeout, signal, onProgress) => {
  onProgress({ new_count_so_far: 2, new_ids_so_far: [2, 3] });
  return { job_id: 'job-1', status: 'completed', new_count: 2, new_ids: [2, 3] };
};
await context.triggerRefresh();
assert.equal(context.labels.at(-1), 3);
assert.ok(context.toasts.includes('✅ 更新完成，新增 3 篇文章'));
""",
    )
```

- [ ] **Step 2: Add flow-isolation and filter-independence tests**

Add the same full Node setup pattern used in the overlap test:

```python
def test_refresh_discovery_count_is_global_not_filter_scoped():
    helpers = source_between(
        "function mergeRefreshArticleIds(",
        "async function triggerRefresh()",
    )
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1, new_ids: [12] };"
    )
    run_node(
        helpers + trigger,
        setup
        + """
context.filter = 'cat:Tech';
context.window = { scrollY: 0 };
context.setRefreshProgressLabel = count => context.lastLabel = count;
context.loadSince = async (timestamp, options) => {
  options.onDiscovered([{ id: 10, source: 'Tech' }, { id: 11, source: 'News' }]);
  return 2;
};
await context.triggerRefresh();
assert.equal(context.lastLabel, 3);
assert.ok(context.toasts.includes('✅ 更新完成，新增 3 篇文章'));
""",
    )
```

Extend the existing
`test_manual_refresh_flow_cancellation_aborts_poller_and_suppresses_stale_toast`
with these exact additions:

```js
context.labels = [];
context.setRefreshProgressLabel = count => context.labels.push(count);
let lateProgress;
context.pollRefreshJob = (jobId, timeout, signal, onProgress) => new Promise((resolve, reject) => {
  lateProgress = onProgress;
  markPollStarted();
  signal.addEventListener('abort', () => reject(
    Object.assign(new Error('cancelled'), { name: 'AbortError' })
  ));
});
const pending = context.triggerRefresh();
await pollStarted;
context.cancelRefreshFlow();
lateProgress({ new_count_so_far: 1, new_ids_so_far: [999] });
await pending;
assert.deepEqual(context.labels, []);
assert.deepEqual(context.toasts, ['🔄 正在后台抓取...']);
```

Include the `mergeRefreshArticleIds`/`renderRefreshDiscoveredCount` source block
before `trigger` in that test so the extracted function resolves its dependencies.

- [ ] **Step 3: Run the new tests and confirm old count behavior fails**

Run:

```bash
python3 -m pytest -q tests/test_frontend_refresh_behavior.py -k "unifies_immediate or discovery_count or cancellation"
```

Expected: FAIL because `triggerRefresh()` still labels and toasts from job counts only.

- [ ] **Step 4: Own one Set inside `triggerRefresh()`**

After `flowIsCurrent`, create:

```js
const discoveredArticleIds = new Set();
const mergeDiscovered = values => {
  if (!flowIsCurrent()) return discoveredArticleIds.size;
  const before = discoveredArticleIds.size;
  const size = mergeRefreshArticleIds(discoveredArticleIds, values);
  if (size !== before) renderRefreshDiscoveredCount(discoveredArticleIds);
  return size;
};
```

Start the immediate check with:

```js
const immediateCheck = loadSince(sinceCursor, {
  manual: true,
  onDiscovered: mergeDiscovered,
});
```

Inside `handleRefreshProgress(status)`:

```js
mergeDiscovered(status.new_ids_so_far || []);
```

Keep `new_count_so_far` only for deciding whether streamed rows may exist and whether `applyStreamedRefreshBatch()` should run. Remove its direct call to `setRefreshProgressLabel(soFar)`.

After terminal status arrives:

```js
mergeDiscovered(status.new_ids || []);
await immediateCheck;
```

Awaiting the already-started promise does not delay job startup; it only guarantees the final Toast includes immediate discoveries.

Replace the success Toast count:

```js
const count = discoveredArticleIds.size;
showToast(count > 0 ? `✅ 更新完成，新增 ${count} 篇文章` : '✅ 已是最新');
```

Do not derive this count from `pendingNewArticleCount`, the active filter, `new_count`, or `new_count_so_far`.

- [ ] **Step 5: Update old fixtures to provide exact IDs**

For existing `triggerRefresh()` tests that expect `新增 N 篇文章`, add `new_ids` matching `new_count`, for example:

```js
return { job_id: 'job-1', status: 'completed', new_count: 3, new_ids: [101, 102, 103] };
```

Keep tests that intentionally model an old/malformed status without `new_ids`; they must assert no guessed cumulative value.

- [ ] **Step 6: Prove request and poll counts did not increase**

Add:

```python
def test_unified_refresh_count_adds_no_fetch_or_status_poll():
    helpers = source_between(
        "function mergeRefreshArticleIds(",
        "async function triggerRefresh()",
    )
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 0, new_ids: [] };"
    )
    run_node(
        helpers + trigger,
        setup
        + """
context.startCalls = 0;
context.pollCalls = 0;
context.sinceCalls = 0;
context.requestRefreshOnce = async () => {
  context.startCalls++;
  return { job_id: 'job-1', status: 'running' };
};
context.pollRefreshJob = async () => {
  context.pollCalls++;
  return { job_id: 'job-1', status: 'completed', new_count: 0, new_ids: [] };
};
context.loadSince = async () => { context.sinceCalls++; return 0; };
await context.triggerRefresh();
assert.deepEqual(
  [context.startCalls, context.pollCalls, context.sinceCalls],
  [1, 1, 1],
);
""",
    )
```

- [ ] **Step 7: Run frontend refresh tests**

Run:

```bash
python3 -m pytest -q tests/test_frontend_refresh_behavior.py
```

Expected: PASS.

- [ ] **Step 8: Commit unified UI count**

```bash
git add frontend/index.html tests/test_frontend_refresh_behavior.py
git commit -m "fix(refresh): unify immediate and fetched article counts"
```

### Task 5: End-to-end verification

**Files:**
- No production files unless verification reveals a defect.

**Interfaces:**
- Consumes: all prior tasks
- Produces: performance and behavior evidence

- [ ] **Step 1: Run focused backend and frontend suites**

Run:

```bash
python3 -m pytest -q \
  tests/test_streaming_refresh.py \
  tests/test_refresh_jobs.py \
  tests/test_frontend_refresh_behavior.py \
  tests/test_refresh_auth_proxy.py
```

Expected: PASS.

- [ ] **Step 2: Run the full suite**

Run:

```bash
python3 -m pytest -q
```

Expected: PASS.

- [ ] **Step 3: Verify the two user scenarios**

Use the Browser plugin if available; otherwise Playwright:

1. Let a periodic refresh finish while the client stays on an older cursor.
2. Click refresh on page 1 and confirm existing articles appear quickly and enter `+N`.
3. Let the manual job stream at least one article also present in a later page response.
4. Confirm the duplicate ID changes neither the button nor final Toast.
5. Repeat on page 2 and under a category filter; confirm total count remains global while list/prompt remains filter-scoped.
6. Navigate away mid-refresh; confirm late progress cannot overwrite the new view or a later refresh.

- [ ] **Step 4: Record performance evidence**

In the implementation PR or execution notes, record:

- number of `/auth/refresh/status` requests before and after;
- number of `/api/news?since=` requests before and after;
- time from click to immediate articles;
- final job duration.

Acceptance: no new request or poll; click-to-immediate remains approximately one second under the same environment.

## Completion Gate

The task is incomplete if any success path still formats the completion Toast from `status.new_count`, or if the implementation adds a list fetch merely to reconcile the count.
