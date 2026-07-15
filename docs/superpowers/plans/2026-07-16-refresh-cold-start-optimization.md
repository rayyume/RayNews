# Refresh and Cold-Start Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make manual refresh non-blocking and internally consistent, while making container and browser cold starts render usable cached content before background synchronization.

**Architecture:** `refresh_server.py` owns one in-process refresh job used by startup, periodic, and manual triggers; `web_server.py` exposes short authenticated start/status calls. The frontend separates the hard-refresh button from the Logo, polls the job, suppresses competing prompts, and changes bootstrap to cache-first parallel revalidation. Container startup stops blocking on `fetcher.py` and lets the already-listening refresh server start the initial job.

**Tech Stack:** Python 3.12, Flask, `http.server.ThreadingHTTPServer`, SQLite, vanilla JavaScript, IndexedDB, Nginx, pytest.

## Global Constraints

- Do not add dynamic一级分类 support; fixed `News / Tech / Biz / Info` behavior remains unchanged.
- Do not change Telegram/Telegraph parsing, AI processing, or image-cache policy.
- Do not add a persistent task queue or new dependency.
- A manual refresh POST is never retried automatically.
- Existing content remains visible on refresh or revalidation failure.
- Logo returns to the homepage and performs an incremental check; it never starts a full scrape.

---

## File Structure

- Modify `refresh_server.py`: refresh job state machine, internal start/status routes, startup/periodic scheduling.
- Modify `web_server.py`: authenticated short proxy endpoints for job start and status.
- Modify `frontend/index.html`: manual refresh polling, Logo semantics, prompt coordination, cache-first parallel bootstrap, bounded cold-start retry.
- Modify `entrypoint.sh`: remove the synchronous pre-service fetch.
- Modify `nginx.conf`: explicit short `/auth/` proxy timeouts.
- Modify `tests/test_access_and_ui_contracts.py`: browser-code and startup-order contracts.
- Create `tests/test_refresh_jobs.py`: executable backend job-state tests.
- Modify `README.md` and `README.en.md`: document non-blocking refresh/startup behavior.

---

### Task 1: Add a Single Asynchronous Refresh Job Controller

**Files:**
- Create: `tests/test_refresh_jobs.py`
- Modify: `refresh_server.py:31-183,276-280,677-722,774-795`

**Interfaces:**
- Produces: `start_refresh_job(trigger: str = "manual") -> tuple[bytes, int]`.
- Produces: `get_refresh_job_status() -> bytes`.
- Produces: internal `POST /refresh` and `GET /refresh/status` JSON endpoints.
- State payload: `job_id`, `status`, `trigger`, `started_at`, `finished_at`, `new_count`, `error`.

- [ ] **Step 1: Write failing tests for immediate start and duplicate coalescing**

```python
# tests/test_refresh_jobs.py
import json
import threading

import refresh_server


def reset_job(monkeypatch):
    monkeypatch.setattr(refresh_server, "REFRESH_JOB", {
        "job_id": "", "status": "idle", "trigger": "",
        "started_at": None, "finished_at": None,
        "new_count": 0, "error": "",
    })


def test_start_refresh_job_returns_before_worker_finishes(monkeypatch):
    reset_job(monkeypatch)
    release = threading.Event()
    entered = threading.Event()

    def slow_fetcher():
        entered.set()
        release.wait(2)
        return json.dumps({"status": "ok"}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", slow_fetcher)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())

    body, status = refresh_server.start_refresh_job("manual")
    payload = json.loads(body)

    assert status == 202
    assert payload["status"] == "running"
    assert entered.wait(1)
    release.set()
    assert wait_terminal()["status"] == "completed"


def test_duplicate_start_returns_the_running_job(monkeypatch):
    reset_job(monkeypatch)
    release = threading.Event()

    def slow_fetcher():
        release.wait(2)
        return json.dumps({"status": "ok"}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", slow_fetcher)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())

    first, first_status = refresh_server.start_refresh_job("manual")
    second, second_status = refresh_server.start_refresh_job("manual")

    assert first_status == 202
    assert second_status == 200
    assert json.loads(first)["job_id"] == json.loads(second)["job_id"]
    release.set()
    assert wait_terminal()["status"] == "completed"
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m pytest -q tests/test_refresh_jobs.py`

Expected: FAIL because `REFRESH_JOB` and `start_refresh_job` do not exist.

- [ ] **Step 3: Implement locked job state and daemon worker**

Add a `REFRESH_JOB_LOCK`, the state dictionary, a public snapshot serializer, and a worker that calls the existing blocking `run_fetcher()` in a daemon thread. Capture article IDs before starting and after a successful run; set `new_count = len(after_ids - before_ids)`. Parse the existing JSON body and store only a compact error string. Never place `stdout`, `stderr`, DB paths, or full exception representations in the public state.

The minimal start shape is:

```python
def start_refresh_job(trigger="manual"):
    with REFRESH_JOB_LOCK:
        if REFRESH_JOB["status"] == "running":
            return _refresh_job_json_locked(), 200
        job_id = uuid.uuid4().hex
        before_ids = article_id_snapshot()
        REFRESH_JOB.update({
            "job_id": job_id,
            "status": "running",
            "trigger": trigger,
            "started_at": int(time.time()),
            "finished_at": None,
            "new_count": 0,
            "error": "",
        })
        body = _refresh_job_json_locked()
    threading.Thread(
        target=_run_refresh_job,
        args=(job_id, before_ids),
        name=f"refresh-job-{job_id[:8]}",
        daemon=True,
    ).start()
    return body, 202
```

- [ ] **Step 4: Add failing completion/failure tests**

```python
def wait_terminal():
    for _ in range(100):
        payload = json.loads(refresh_server.get_refresh_job_status())
        if payload["status"] != "running":
            return payload
        threading.Event().wait(0.01)
    raise AssertionError("refresh job did not finish")


def test_refresh_job_reports_new_count(monkeypatch):
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
    assert payload["finished_at"] is not None


def test_refresh_job_exposes_compact_failure(monkeypatch):
    reset_job(monkeypatch)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())
    monkeypatch.setattr(
        refresh_server,
        "run_fetcher",
        lambda: (json.dumps({"status": "error", "error": "timeout"}).encode(), 500),
    )
    refresh_server.start_refresh_job("manual")
    payload = wait_terminal()
    assert payload["status"] == "failed"
    assert payload["error"] == "timeout"
    assert "stdout" not in payload
    assert "stderr" not in payload
```

- [ ] **Step 5: Run completion tests and verify RED, then implement terminal transitions**

Run: `python3 -m pytest -q tests/test_refresh_jobs.py`

Expected before implementation: FAIL because terminal transitions/new-count calculation are missing. Implement `_run_refresh_job()` and rerun until all tests pass.

- [ ] **Step 6: Route manual, startup, and periodic triggers through the controller**

Add `Handler.do_POST()` for `/refresh`, change GET `/refresh` to compatibility start behavior, add GET `/refresh/status`, change `periodic_refresh()` to call `start_refresh_job("periodic")`, and schedule `start_refresh_job("startup")` only after the HTTP server object is created.

- [ ] **Step 7: Run backend tests and commit**

Run: `python3 -m pytest -q tests/test_refresh_jobs.py tests/test_review_bug_hardening.py tests/test_image_cache.py`

Expected: PASS.

```bash
git add refresh_server.py tests/test_refresh_jobs.py
git commit -m "feat: run refreshes as background jobs"
```

---

### Task 2: Expose Short Authenticated Start and Status Requests

**Files:**
- Modify: `tests/test_access_and_ui_contracts.py:363-383`
- Modify: `web_server.py:4120-4132`
- Modify: `nginx.conf:39-46`

**Interfaces:**
- Consumes: internal `POST http://127.0.0.1:8081/refresh` and `GET .../refresh/status`.
- Produces: authenticated `POST /auth/refresh` and `GET /auth/refresh/status`.

- [ ] **Step 1: Replace the old long-timeout contract with failing async-proxy contracts**

```python
def test_manual_refresh_proxies_short_start_and_status_requests():
    source = (ROOT / "web_server.py").read_text(encoding="utf-8")
    assert '@app.route("/auth/refresh", methods=["POST"])' in source
    assert 'http_req.post("http://127.0.0.1:8081/refresh", timeout=5)' in source
    assert '@app.route("/auth/refresh/status", methods=["GET"])' in source
    assert 'http_req.get("http://127.0.0.1:8081/refresh/status", timeout=5)' in source
    assert "timeout=150" not in source[source.index("def protected_refresh"):source.index("# ─── Health", source.index("def protected_refresh"))]


def test_auth_proxy_has_explicit_short_timeouts():
    nginx = (ROOT / "nginx.conf").read_text(encoding="utf-8")
    block = nginx[nginx.index("location /auth/"):nginx.index("location ^~ /avatars/")]
    assert "proxy_connect_timeout 5s;" in block
    assert "proxy_send_timeout 30s;" in block
    assert "proxy_read_timeout 30s;" in block
```

- [ ] **Step 2: Run the two tests and verify RED**

Run: `python3 -m pytest -q tests/test_access_and_ui_contracts.py -k 'manual_refresh_proxies or auth_proxy_has'`

Expected: FAIL on the old GET proxy with `timeout=150` and missing Nginx directives.

- [ ] **Step 3: Implement the two authenticated proxy routes**

Use `requests.post/get(..., timeout=5)`, parse JSON once, and preserve the internal status code. On internal connectivity failure return `502` with a concise error. Both routes retain `@require_role("user", "admin")`.

- [ ] **Step 4: Add explicit Nginx `/auth/` timeouts**

Add exactly:

```nginx
proxy_connect_timeout 5s;
proxy_send_timeout 30s;
proxy_read_timeout 30s;
```

- [ ] **Step 5: Run tests and commit**

Run: `python3 -m pytest -q tests/test_access_and_ui_contracts.py tests/test_security_hardening.py`

Expected: PASS.

```bash
git add web_server.py nginx.conf tests/test_access_and_ui_contracts.py
git commit -m "feat: expose authenticated refresh job status"
```

---

### Task 3: Unify the Frontend Manual Refresh State

**Files:**
- Modify: `tests/test_access_and_ui_contracts.py:188-206,325-383,544-565`
- Modify: `frontend/index.html:513-523,1289-1321,4196-4246,4489-4550,5415-5515,6228-6232`

**Interfaces:**
- Consumes: POST payload and status payload from Task 2.
- Produces: `requestRefreshStatus()`, `pollRefreshJob(jobId)`, and the revised `triggerRefresh()`.

- [ ] **Step 1: Write failing contracts for button/Logo separation and single POST**

```python
def test_logo_is_lightweight_and_header_button_starts_refresh_job():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'class="logo" onclick="refreshHomepage()"' in html
    assert 'id="refreshBtn" onclick="triggerRefresh()"' in html
    logo = html[html.index("async function refreshHomepage()"):html.index("async function scrollToTopAndCheckLatest")]
    assert "loadSince(cursor, { forceApply: true })" in logo
    assert "requestRefreshOnce" not in logo
    assert "triggerRefresh(" not in logo


def test_manual_refresh_posts_once_then_polls_status():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    block = html[html.index("async function triggerRefresh("):html.index("function setRefreshRunning", html.index("async function triggerRefresh("))]
    assert block.count("await requestRefreshOnce()") == 1
    assert "await pollRefreshJob(data.job_id)" in block
    assert "isTransientRefreshError" not in block
    assert "await delay(800)" not in block
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m pytest -q tests/test_access_and_ui_contracts.py -k 'logo_is_lightweight or posts_once'`

Expected: FAIL because both controls still call `refreshHomepage()` and POST retry remains.

- [ ] **Step 3: Implement job status polling and completion application**

Add:

```javascript
async function requestRefreshStatus() {
  const resp = await fetch('/auth/refresh/status', {
    headers: { 'Authorization': 'Bearer ' + authToken },
    cache: 'no-store',
  });
  return parseRefreshResponse(resp);
}

async function pollRefreshJob(jobId, timeoutMs = 135000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    await delay(1200);
    const status = await requestRefreshStatus();
    if (status.job_id !== jobId && status.status === 'running') continue;
    if (status.status === 'completed' || status.status === 'failed') return status;
  }
  throw new Error('刷新状态查询超时，请稍后查看最新文章');
}
```

Revise `triggerRefresh()` to start once, await the terminal status, perform one authoritative `loadNewsPage(1, { activeFilter: filter, useCache: false, forceNetwork: true, animate: true })` only when the user is still on page 1 without a blocking overlay, consume the active filter's pending items, then clear the progress state. If navigation changed, leave the current view intact and let normal incremental checking surface relevant items.

- [ ] **Step 4: Write a failing prompt-coordination contract**

```python
def test_manual_refresh_suppresses_competing_new_article_prompt():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    prompt = html[html.index("function showNewArticlesPrompt()"):html.index("function hideNewArticlesPrompt()")]
    assert "if (refreshInProgress) return;" in prompt
    trigger = html[html.index("async function triggerRefresh("):html.index("function setRefreshRunning")]
    assert "consumePendingNewArticles(activeFilter);" in trigger
    assert trigger.index("consumePendingNewArticles(activeFilter);") < trigger.index("setRefreshRunning(false);")
```

- [ ] **Step 5: Run RED, implement suppression, and keep navigation usable**

Run the named test and confirm failure. Add the refresh guard to `showNewArticlesPrompt()`. Update `setRefreshRunning()` so it disables only the refresh control; do not disable Logo, scroll-to-top, category controls, or article navigation while the background task runs.

- [ ] **Step 6: Implement the lightweight Logo flow**

Retain the existing smooth scroll/page-1 preparation, but remove `triggerRefresh()` and the post-scrape forced reload. After the homepage is visible, call `loadSince(latestKnownTimestamp || latestNewsTimestamp(), { forceApply: true })` once.

- [ ] **Step 7: Run frontend contracts and commit**

Run: `python3 -m pytest -q tests/test_access_and_ui_contracts.py tests/test_review_bug_hardening.py`

Expected: PASS.

```bash
git add frontend/index.html tests/test_access_and_ui_contracts.py
git commit -m "fix: make manual refresh non-blocking and consistent"
```

---

### Task 4: Make Browser Cold Start Cache-First and Parallel

**Files:**
- Modify: `tests/test_access_and_ui_contracts.py:527-541`
- Modify: `frontend/index.html:1480-1583,4335-4652,6155-6191`

**Interfaces:**
- Produces: cached metadata application before source-network completion.
- Produces: `loadNewsPage(..., networkRetries = 0)` with fresh controllers per retry.
- Produces: parallel `bootstrapNews()`.

- [ ] **Step 1: Write failing cold-start ordering contracts**

```python
def test_cold_start_renders_categories_immediately_and_runs_requests_in_parallel():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    boot = html[html.index("async function bootstrapNews()"):html.index("// Initial load")]
    assert boot.index("renderTopCatBar();") < boot.index("loadSourceCategories()")
    assert "const sourcePromise = loadSourceCategories();" in boot
    assert "const newsPromise = loadNewsPage(1, {" in boot
    assert "useCache: true," in boot
    assert "networkRetries: 1," in boot
    assert "await Promise.allSettled([sourcePromise, newsPromise, todayCountPromise]);" in boot
    assert "await loadSourceCategories();" not in boot


def test_cached_source_metadata_is_rendered_before_network_fetch():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    block = html[html.index("async function loadSourceCategories()"):html.index("function sourceLabel", html.index("async function loadSourceCategories()"))]
    assert block.index("rebuildCategoryMap(cached.data.sources);") < block.index("await apiFetch('/sources')")
    assert block.index("renderFilters();") < block.index("await apiFetch('/sources')")
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m pytest -q tests/test_access_and_ui_contracts.py -k 'cold_start_renders or cached_source_metadata'`

Expected: FAIL because bootstrap is serial and uses `useCache: false`.

- [ ] **Step 3: Render cached metadata immediately and revalidate in place**

After cached `rebuildCategoryMap()`, call `renderFilters()` before the network request. After fresh metadata arrives, rebuild and render again. Keep the built-in fixed map only as the no-cache fallback.

- [ ] **Step 4: Add a failing bounded-retry contract**

```python
def test_cold_start_retry_uses_a_fresh_abort_controller_and_preserves_cache():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    load = html[html.index("async function loadNewsPage("):html.index("function applyPageCalibrationWhenActive")]
    assert "networkRetries = 0" in load
    assert "for (let attempt = 0; attempt <= networkRetries; attempt++)" in load
    assert "pageRequestController = new AbortController();" in load
    assert "if (cacheApplied)" in load
    assert "renderColdStartError(message)" in load
```

- [ ] **Step 5: Run RED and implement one bounded cold-start retry**

Move controller/timeout creation inside a small attempt loop. Retry only when `networkRetries > 0`, the request still owns the current sequence, and no newer navigation superseded it. Use a new controller per attempt and a short `delay(600)` before the second attempt. If cached content was applied, keep it and show only a toast after the final failure; call `renderColdStartError()` only when there is no cache and no visible news.

- [ ] **Step 6: Rewrite bootstrap as parallel cache-first work**

Set filter/page synchronously, call `renderTopCatBar()`, then create `sourcePromise`, `newsPromise`, and `todayCountPromise` without awaiting between them. Pass `useCache: true`, `forceNetwork: true`, and `networkRetries: 1` to the article load. Finish with `Promise.allSettled`, render the final filter counts, sync URL, and reset mobile scroll.

- [ ] **Step 7: Run contracts and commit**

Run: `python3 -m pytest -q tests/test_access_and_ui_contracts.py`

Expected: PASS.

```bash
git add frontend/index.html tests/test_access_and_ui_contracts.py
git commit -m "perf: render cached news during cold start"
```

---

### Task 5: Serve Before the Initial Fetch

**Files:**
- Modify: `tests/test_access_and_ui_contracts.py`
- Modify: `entrypoint.sh:29-60`
- Modify: `refresh_server.py:774-795` only if Task 1 did not already complete startup scheduling.

**Interfaces:**
- Consumes: `start_refresh_job("startup")` from Task 1.
- Produces: container entrypoint that does not run `fetcher.py` synchronously before ports listen.

- [ ] **Step 1: Write a failing startup-order test**

```python
def test_container_does_not_block_web_startup_on_initial_fetch():
    entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
    start_services = entrypoint.index("=== Starting refresh server ===")
    assert "python fetcher.py" not in entrypoint[:start_services]
    refresh = (ROOT / "refresh_server.py").read_text(encoding="utf-8")
    main = refresh[refresh.index('if __name__ == "__main__":'):]
    assert 'start_refresh_job("startup")' in main
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m pytest -q tests/test_access_and_ui_contracts.py -k container_does_not_block`

Expected: FAIL because `entrypoint.sh` runs `python fetcher.py` before starting services.

- [ ] **Step 3: Remove the synchronous entrypoint fetch**

Delete only the blocking `=== Running initial fetch ===` command and its immediately following DB diagnostic block. Preserve the configuration warning, service launches, and foreground Nginx process. The refresh server's startup job now owns first-fetch behavior.

- [ ] **Step 4: Verify shell syntax and tests**

Run: `bash -n entrypoint.sh`

Expected: exit 0.

Run: `python3 -m pytest -q tests/test_access_and_ui_contracts.py tests/test_refresh_jobs.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add entrypoint.sh refresh_server.py tests/test_access_and_ui_contracts.py
git commit -m "perf: serve existing data before startup refresh"
```

---

### Task 6: Documentation and Full Verification

**Files:**
- Modify: `README.md:142-146,255`
- Modify: `README.en.md:142-146,255`

**Interfaces:**
- Documents the user-visible behavior delivered by Tasks 1-5.

- [ ] **Step 1: Update user documentation**

Document that startup serves persisted content immediately and refreshes in the background; manual refresh is authenticated, non-blocking, and reports completion/new-count; Logo is a lightweight return-to-latest action.

- [ ] **Step 2: Run focused verification**

```bash
python3 -m pytest -q \
  tests/test_refresh_jobs.py \
  tests/test_access_and_ui_contracts.py \
  tests/test_review_bug_hardening.py \
  tests/test_security_hardening.py \
  tests/test_image_cache.py
```

Expected: PASS with no new warnings.

- [ ] **Step 3: Run the full suite**

Run: `python3 -m pytest -q`

Expected: all tests pass.

- [ ] **Step 4: Perform an interface-level smoke test**

With a temporary data directory and a mocked/short-lived fetcher command, start `refresh_server.py` and `web_server.py`; verify:

```text
POST /auth/refresh                 -> 202 running in under 1 second
GET  /auth/refresh/status          -> running, then completed/failed
GET  /api/news?page=1&size=30      -> responds while refresh job runs
second POST /auth/refresh          -> same job_id, no second worker
```

If the full container cannot run because Docker or a deployment URL is unavailable, record that limitation rather than claiming rendered production validation.

- [ ] **Step 5: Inspect the final diff and commit docs**

Run: `git diff --check && git status --short && git log --oneline -8`

Expected: no whitespace errors and only intended files changed.

```bash
git add README.md README.en.md
git commit -m "docs: explain background refresh behavior"
```

- [ ] **Step 6: Request code review before integration**

Use `superpowers:requesting-code-review`, address any correctness findings, rerun the full suite, and only then report completion.
