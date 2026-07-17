import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def test_review_hardening_frontend_contracts():
    assert HTML.count('data-admin-tab="server"') == 1
    assert "detail === 'refresh failed'" in HTML
    assert "global_article_count" in source_between(
        "function isStartupInitializationResponse(",
        "function renderColdStartInitializing",
    )
    assert "startStartupEmptyRevalidation" in source_between(
        "function applyNewsPage(",
        "async function fetchNewsPage",
    )
    assert "await delay(" in source_between("function retryArticleDetail(", "function usesMobileArticleNavigation")
    assert "retryArticleDetail" in source_between("function onReturnToForeground()", "document.addEventListener('visibilitychange'")


def test_logo_refresh_state_is_declared_before_click_handler_uses_it():
    assert "let logoRefreshInProgress = false;" in HTML


def test_ipad_pwa_trackpad_back_gesture_is_isolated_from_other_platforms():
    assert "function usesIpadPwaTrackpadNavigation()" in HTML
    gesture = source_between("function usesIpadPwaTrackpadNavigation()", "document.addEventListener('visibilitychange'")
    assert "navigator.platform === 'MacIntel'" in gesture
    assert "navigator.maxTouchPoints > 0" in gesture
    assert "display-mode: standalone" in gesture
    assert "overlay.addEventListener('wheel'" in HTML
    assert "closeArticleFromButton()" in HTML[HTML.index("overlay.addEventListener('wheel'"):]


def test_mobile_article_header_has_a_dedicated_double_tap_top_scroll_zone():
    assert 'id="articleTopScrollZone"' in HTML
    assert "function scrollArticleDetailToTop()" in HTML
    zone = source_between("const articleTopScrollZone", "document.getElementById('lb').addEventListener")
    assert "usesMobileArticleNavigation()" in zone
    assert "touchend" in zone
    assert "scrollArticleDetailToTop()" in zone


def test_service_worker_rethrows_network_errors_without_a_cached_response():
    source = (ROOT / "frontend" / "sw.js").read_text(encoding="utf-8")
    assert ".catch(error => { if (cached) return cached; throw error; })" in source


def source_between(start, end):
    return HTML[HTML.index(start):HTML.index(end, HTML.index(start))]


def run_node(source, body):
    script = f"""
const assert = require('assert');
const vm = require('vm');
const context = {{ console, URLSearchParams, AbortController }};
vm.createContext(context);
vm.runInContext({json.dumps(source)}, context);
(async () => {{
{body}
}})().catch(error => {{
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
}});
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_poll_refresh_job_rejects_terminal_status_for_another_job():
    poll = source_between("async function pollRefreshJob(", "function rebuildCategoryMap")
    run_node(
        poll,
        """
context.abortableDelay = async () => {};
context.requestRefreshStatus = async () => ({
  job_id: 'other-job', status: 'completed', new_count: 99,
});
context.Date = { now: () => 0 };
await assert.rejects(
  context.pollRefreshJob('expected-job', 1000),
  error => /取代|变更|失效/.test(error.message) && !error.message.includes('other-job'),
);
""",
    )


def test_status_request_uses_abort_signal_with_caller_bounded_timeout():
    request = source_between("async function requestRefreshStatus(", "async function pollRefreshJob(")
    run_node(
        request,
        """
const timers = [];
let fetchOptions;
context.authToken = 'token';
context.parseRefreshResponse = async () => ({ status: 'running' });
context.setTimeout = (callback, timeout) => { timers.push(timeout); return 7; };
context.clearTimeout = id => assert.equal(id, 7);
context.AbortController = class {
  constructor() { this.signal = { marker: 'status-signal' }; }
  abort() {}
};
context.fetch = async (url, options) => {
  assert.equal(url, '/auth/refresh/status?job_id=job-1');
  fetchOptions = options;
  return {};
};
await context.requestRefreshStatus('job-1', 37);
assert.deepEqual(timers, [37]);
assert.equal(fetchOptions.signal.marker, 'status-signal');
""",
    )


def test_poll_refresh_job_caps_each_status_request_to_remaining_deadline():
    poll = source_between("async function pollRefreshJob(", "function rebuildCategoryMap")
    run_node(
        poll,
        """
let now = 0;
let requestedTimeout = null;
context.Date = { now: () => now };
context.abortableDelay = async timeout => {
  assert.equal(timeout, 1000);
  now = 600;
};
context.requestRefreshStatus = async (jobId, timeout) => {
  assert.equal(jobId, 'job-1');
  requestedTimeout = timeout;
  return { job_id: 'job-1', status: 'completed', new_count: 2 };
};
const status = await context.pollRefreshJob('job-1', 1000);
assert.equal(status.new_count, 2);
assert.equal(requestedTimeout, 400);
""",
    )


def test_status_poll_targets_exact_job_and_links_fetch_to_flow_abort():
    request = source_between("async function requestRefreshStatus(", "async function pollRefreshJob(")
    run_node(
        request,
        """
const flow = new AbortController();
let fetchUrl = '';
let fetchSignal;
context.authToken = 'token';
context.parseRefreshResponse = async () => ({ job_id: 'job/a', status: 'running' });
context.fetch = (url, options) => {
  fetchUrl = url;
  fetchSignal = options.signal;
  return new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(
      Object.assign(new Error('cancelled'), { name: 'AbortError' })
    ));
  });
};
context.setTimeout = () => 9;
context.clearTimeout = () => {};
const pending = context.requestRefreshStatus('job/a', 5000, flow.signal);
await Promise.resolve();
assert.equal(fetchUrl, '/auth/refresh/status?job_id=job%2Fa');
assert.notEqual(fetchSignal, flow.signal);
flow.abort();
await assert.rejects(pending, error => error.name === 'AbortError');
""",
    )


def test_poll_refresh_job_passes_exact_job_id_to_every_status_request():
    poll = source_between("async function pollRefreshJob(", "function rebuildCategoryMap")
    run_node(
        poll,
        """
let now = 0;
const calls = [];
const flow = new AbortController();
context.Date = { now: () => now };
context.abortableDelay = async (timeout, signal) => {
  assert.equal(signal, flow.signal);
  now += timeout;
};
context.requestRefreshStatus = async (jobId, timeout, signal) => {
  calls.push([jobId, timeout, signal]);
  return { job_id: jobId, status: 'completed', new_count: 0 };
};
await context.pollRefreshJob('job-a', 5000, flow.signal);
assert.equal(calls.length, 1);
assert.equal(calls[0][0], 'job-a');
assert.equal(calls[0][2], flow.signal);
""",
    )


def trigger_context_setup(poll_body):
    return f"""
context.refreshInProgress = false;
context.refreshFlowGeneration = 0;
context.refreshFlowController = null;
context.authToken = 'token';
context.document = {{ hidden: false }};
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 3;
context.pageRequestSequence = 8;
context.pageRequestPendingSequence = 0;
context.pageNavigationPending = false;
context.cancelStartupEmptyRevalidation = () => {{}};
context.hideNewArticlesPrompt = () => {{}};
context.showToast = message => context.toasts.push(message);
context.toasts = [];
context.requestRefreshOnce = async () => ({{ job_id: 'job-1', status: 'running' }});
context.pollRefreshJob = async () => {{ {poll_body} }};
context.refreshErrorMessage = error => error.message || error.error || 'failed';
context.hasBlockingOverlayOpen = () => context.overlayOpen;
context.overlayOpen = false;
context.setRefreshRunning = running => {{
  context.refreshInProgress = running;
  context.runningStates.push(running);
}};
context.runningStates = [];
context.showNewArticlesPrompt = () => context.promptStates.push(context.refreshInProgress);
context.promptStates = [];
context.consumePendingNewArticles = () => context.consumed++;
context.consumed = 0;
context.loadCalls = 0;
"""


def test_manual_refresh_skips_apply_after_navigation_changed_then_returned():
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup(
        "context.pageNavigationSequence += 2; "
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.loadNewsPage = async () => { context.loadCalls++; return true; };
await context.triggerRefresh();
assert.equal(context.loadCalls, 0);
assert.equal(context.consumed, 0);
assert.deepEqual(context.runningStates, [true, false]);
assert.deepEqual(context.promptStates, [false]);
""",
    )


def test_manual_refresh_skips_apply_while_page_navigation_is_pending():
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup(
        "context.pageNavigationPending = true; "
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.loadNewsPage = async () => { context.loadCalls++; return true; };
await context.triggerRefresh();
assert.equal(context.loadCalls, 0);
assert.equal(context.consumed, 0);
assert.deepEqual(context.promptStates, [false]);
""",
    )


def test_manual_refresh_does_not_replace_a_list_request_already_in_flight():
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.filter = 'cat:Tech';
context.pageRequestPendingSequence = context.pageRequestSequence;
context.abortCalls = 0;
context.pageRequestController = { abort: () => context.abortCalls++ };
context.loadNewsPage = async () => {
  context.loadCalls++;
  context.pageRequestController.abort();
  context.filter = 'all';
  return true;
};
await context.triggerRefresh();
assert.equal(context.loadCalls, 0);
assert.equal(context.abortCalls, 0);
assert.equal(context.filter, 'cat:Tech');
assert.equal(context.pageRequestPendingSequence, 8);
assert.equal(context.consumed, 0);
assert.deepEqual(context.runningStates, [true, false]);
assert.deepEqual(context.promptStates, [false]);
""",
    )


def test_manual_refresh_application_guard_blocks_overlay_opened_mid_load():
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.loadNewsPage = async (page, options) => {
  context.loadCalls++;
  assert.equal(typeof options.applicationGuard, 'function');
  context.pageRequestSequence++;
  context.pageRequestPendingSequence = context.pageRequestSequence;
  context.overlayOpen = true;
  const applied = options.applicationGuard();
  context.pageRequestPendingSequence = 0;
  return applied;
};
await context.triggerRefresh();
assert.equal(context.loadCalls, 1);
assert.equal(context.consumed, 0);
assert.deepEqual(context.promptStates, [false]);
""",
    )


def test_manual_refresh_consumes_pending_only_after_confirmed_application():
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.applied = false;
context.consumePendingNewArticles = () => {
  assert.equal(context.applied, true);
  assert.equal(context.refreshInProgress, true);
  context.consumed++;
};
context.loadNewsPage = async (page, options) => {
  context.loadCalls++;
  context.pageRequestSequence++;
  context.pageRequestPendingSequence = context.pageRequestSequence;
  assert.equal(options.applicationGuard(), true);
  context.applied = true;
  context.pageRequestPendingSequence = 0;
  return true;
};
await context.triggerRefresh();
assert.equal(context.loadCalls, 1);
assert.equal(context.consumed, 1);
assert.deepEqual(context.runningStates, [true, false]);
assert.deepEqual(context.promptStates, [false]);
""",
    )


def test_manual_refresh_reports_streaming_progress_on_button_label():
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 3 };"
    )
    run_node(
        trigger,
        setup
        + """
context.window = { scrollY: 0 };
context.labels = [];
context.setRefreshProgressLabel = count => context.labels.push(count);
context.applyStreamedRefreshBatch = async () => { context.applyCalls++; };
context.applyCalls = 0;
context.pollRefreshJob = async (jobId, timeout, signal, onProgress) => {
  onProgress({ new_count_so_far: 2 });
  onProgress({ new_count_so_far: 2 }); // no increase — must not relabel or reapply
  return { job_id: 'job-1', status: 'completed', new_count: 3 };
};
await context.triggerRefresh();
assert.deepEqual(context.labels, [2]);
assert.equal(context.applyCalls, 1);
""",
    )


def test_manual_refresh_skips_streamed_apply_when_scrolled_away_from_top():
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.window = { scrollY: 200 };
context.labels = [];
context.setRefreshProgressLabel = count => context.labels.push(count);
context.applyStreamedRefreshBatch = async () => { context.applyCalls++; };
context.applyCalls = 0;
context.pollRefreshJob = async (jobId, timeout, signal, onProgress) => {
  onProgress({ new_count_so_far: 4 });
  return { job_id: 'job-1', status: 'completed', new_count: 4 };
};
await context.triggerRefresh();
assert.deepEqual(context.labels, [4]);
assert.equal(context.applyCalls, 0);
""",
    )


def test_manual_refresh_skips_streamed_apply_after_page_navigation_changed_view():
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.window = { scrollY: 0 };
context.applyStreamedRefreshBatch = async () => { context.applyCalls++; };
context.applyCalls = 0;
context.setRefreshProgressLabel = () => {};
context.pollRefreshJob = async (jobId, timeout, signal, onProgress) => {
  context.pageNavigationSequence += 1;
  onProgress({ new_count_so_far: 2 });
  return { job_id: 'job-1', status: 'completed', new_count: 2 };
};
await context.triggerRefresh();
assert.equal(context.applyCalls, 0);
""",
    )


def test_manual_refresh_streamed_apply_is_throttled_to_one_per_three_seconds():
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.window = { scrollY: 0 };
context.setRefreshProgressLabel = () => {};
let now = 10000; // start well past 0 so the first tick isn't starved by lastStreamedApplyAt=0
context.Date = { now: () => now };
context.applyStreamedRefreshBatch = async () => { context.applyCalls++; return true; };
context.applyCalls = 0;
// applyStreamedRefreshBatch is fire-and-forget (not awaited by handleRefreshProgress),
// so give its .finally() a couple of microtask turns to clear streamApplyInFlight
// between ticks, same as would happen across real pollRefreshJob's ~1.2s delay.
const flushMicrotasks = async () => { await Promise.resolve(); await Promise.resolve(); };
context.pollRefreshJob = async (jobId, timeout, signal, onProgress) => {
  onProgress({ new_count_so_far: 2 });
  await flushMicrotasks();
  now += 500; // well under the 3000ms throttle window
  onProgress({ new_count_so_far: 4 });
  await flushMicrotasks();
  now += 3000;
  onProgress({ new_count_so_far: 6 });
  await flushMicrotasks();
  return { job_id: 'job-1', status: 'completed', new_count: 6 };
};
await context.triggerRefresh();
assert.equal(context.applyCalls, 2);
""",
    )


def test_manual_refresh_flow_cancellation_aborts_poller_and_suppresses_stale_toast():
    helpers = source_between("function cancelRefreshFlow(", "function rebuildCategoryMap")
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    run_node(
        helpers + trigger,
        """
context.refreshInProgress = false;
context.refreshFlowGeneration = 0;
context.refreshFlowController = null;
context.authToken = 'token';
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageNavigationPending = false;
context.document = { hidden: false };
context.cancelStartupEmptyRevalidation = () => {};
context.hideNewArticlesPrompt = () => {};
context.promptCalls = 0;
context.showNewArticlesPrompt = () => context.promptCalls++;
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.refreshErrorMessage = error => error.message || 'failed';
context.hasBlockingOverlayOpen = () => false;
context.consumePendingNewArticles = () => {};
context.loadNewsPage = async () => true;
context.runningStates = [];
context.setRefreshRunning = running => {
  context.refreshInProgress = running;
  context.runningStates.push(running);
};
context.requestRefreshOnce = async signal => {
  assert.equal(signal, context.refreshFlowController.signal);
  return { job_id: 'job-a', status: 'running' };
};
let markPollStarted;
const pollStarted = new Promise(resolve => { markPollStarted = resolve; });
context.pollRefreshJob = (jobId, timeout, signal) => new Promise((resolve, reject) => {
  assert.equal(jobId, 'job-a');
  assert.equal(signal, context.refreshFlowController.signal);
  markPollStarted();
  signal.addEventListener('abort', () => reject(
    Object.assign(new Error('cancelled'), { name: 'AbortError' })
  ));
});
const pending = context.triggerRefresh();
await pollStarted;
context.cancelRefreshFlow();
await pending;
assert.deepEqual(context.runningStates, [true, false]);
assert.deepEqual(context.toasts, ['🔄 正在后台抓取...']);
assert.equal(context.promptCalls, 0);
assert.equal(context.refreshInProgress, false);
assert.equal(context.refreshFlowController, null);
""",
    )


def test_incremental_check_overlapping_manual_refresh_only_queues_pending_items():
    incremental = source_between("async function loadSince(", "function rebuildSourceFilterGroups")
    run_node(
        incremental,
        """
context.refreshInProgress = true;
context.INCREMENTAL_FETCH_LIMIT = 200;
context.seenArticleIds = new Set();
context.latestKnownTimestamp = 100;
context.pendingNewArticleCount = 0;
context.pendingNewItems = [];
context.contentEpoch = 0;
context.bumpContentEpoch = () => { context.epochBumps = (context.epochBumps || 0) + 1; };
context.fetch = async () => ({
  json: async () => ({ items: [{ id: 2, timestamp: 101, source: 's' }] }),
});
context.setTimeout = () => 1;
context.clearTimeout = () => {};
context.refreshTodayArticleCount = () => { context.countRefreshes++; };
context.countRefreshes = 0;
context.loadSourceCategories = async () => { context.sourceLoads++; };
context.sourceLoads = 0;
context.fetchNewsPage = async () => { context.pageFetches++; return { items: [] }; };
context.pageFetches = 0;
context.writeCachedNewsPage = async () => {};
context.pendingRelevantCount = () => 1;
context.showLatestAfterIdle = () => { context.applies++; };
context.showNewArticlesPrompt = () => { context.applies++; };
context.applyNewsPage = () => { context.applies++; };
context.consumePendingNewArticles = () => { context.consumes++; };
context.applies = 0;
context.consumes = 0;
context.filter = 'all';
context.currentPage = 1;
context.window = { scrollY: 0 };
context.hasBlockingOverlayOpen = () => false;
context.lastUserActivityAt = 0;
context.IDLE_LATEST_DELAY_MS = 1;
const added = await context.loadSince(100);
assert.equal(added, 1);
assert.equal(context.epochBumps, 1);
assert.equal(context.pendingNewArticleCount, 1);
assert.deepEqual(Array.from(context.pendingNewItems, item => item.id), [2]);
assert.equal(context.countRefreshes, 1);
assert.equal(context.sourceLoads, 0);
assert.equal(context.pageFetches, 0);
assert.equal(context.applies, 0);
assert.equal(context.consumes, 0);
""",
    )


# ─── contentEpoch: per-record stamping closes the TOCTOU window ───────────
#
# Code review (P1 x2) found that the original design — checking
# `epochAtStart === contentEpoch` immediately before an async cache write —
# left a race: a bump landing during that write's own await still let stale
# data reach the cache, and loadNewsPageRequest (the plain page-load path,
# not just pagination) never checked epoch at all. The fix stamps every
# stored record with the epoch that was live when the underlying fetch was
# *requested*, and rejects mismatched records at *read* time instead —
# closing the window regardless of when a bump lands, and covering every
# caller uniformly since the check lives in the shared read helpers.

def test_read_buffered_page_rejects_a_record_stamped_with_a_stale_epoch():
    buffer_fns = source_between("function rememberBufferedPage(", "async function prefetchNewsPage")
    run_node(
        buffer_fns,
        """
context.newsPageCacheKey = (page, activeFilter) => `page:${activeFilter}:${page}`;
context.PAGE_MEMORY_BUFFER_LIMIT = 10;
context.pageMemoryBuffer = new Map();
context.warmPageCoverImages = () => {};
context.contentEpoch = 1;
context.rememberBufferedPage(1, 'all', { items: [{ id: 1 }] });
context.contentEpoch = 2; // a bump landed after the record was stored
const stale = context.readBufferedPage(1, 'all');
assert.equal(stale, null);
// The stale entry must also be evicted, not just skipped, so it can't be
// served again after another read re-bumps back to the same epoch value.
assert.equal(context.pageMemoryBuffer.has('page:all:1'), false);
""",
    )


def test_read_buffered_page_accepts_a_record_stamped_with_the_live_epoch():
    buffer_fns = source_between("function rememberBufferedPage(", "async function prefetchNewsPage")
    run_node(
        buffer_fns,
        """
context.newsPageCacheKey = (page, activeFilter) => `page:${activeFilter}:${page}`;
context.PAGE_MEMORY_BUFFER_LIMIT = 10;
context.pageMemoryBuffer = new Map();
context.warmPageCoverImages = () => {};
context.contentEpoch = 3;
context.rememberBufferedPage(1, 'all', { items: [{ id: 1 }] });
const fresh = context.readBufferedPage(1, 'all');
assert.deepEqual(fresh, { items: [{ id: 1 }] });
""",
    )


def test_remember_buffered_page_stamps_the_epoch_passed_by_the_caller_not_the_live_one():
    buffer_fns = source_between("function rememberBufferedPage(", "async function prefetchNewsPage")
    run_node(
        buffer_fns,
        """
context.newsPageCacheKey = (page, activeFilter) => `page:${activeFilter}:${page}`;
context.PAGE_MEMORY_BUFFER_LIMIT = 10;
context.pageMemoryBuffer = new Map();
context.warmPageCoverImages = () => {};
context.contentEpoch = 9; // live epoch has already moved on by the time this lands
context.rememberBufferedPage(1, 'all', { items: [{ id: 1 }] }, 5); // stamped with the epoch at request time
const rejected = context.readBufferedPage(1, 'all');
assert.equal(rejected, null);
""",
    )


def test_read_cached_news_page_rejects_a_record_stamped_with_a_stale_epoch():
    cache_fns = source_between("async function readCachedNewsPage(", "async function cleanupNewsCache")
    run_node(
        cache_fns,
        """
const store = {};
context.newsPageCacheKey = (page, activeFilter) => `page:${activeFilter}:${page}`;
context.readNewsCacheEntry = async key => store[key] || null;
context.writeNewsCacheEntry = async record => { store[record.key] = record; };
context.NEWS_CACHE_MAX_AGE_MS = 999999999;
context.contentEpoch = 5;
await context.writeCachedNewsPage(1, 'all', { items: [{ id: 1 }] }); // stamps epoch=5 (default = live)
context.contentEpoch = 6; // a bump landed after the write completed
const result = await context.readCachedNewsPage(1, 'all');
assert.equal(result, null);
""",
    )


def test_write_cached_news_page_stamps_the_epoch_passed_by_the_caller_not_the_live_one():
    cache_fns = source_between("async function readCachedNewsPage(", "async function cleanupNewsCache")
    run_node(
        cache_fns,
        """
const store = {};
context.newsPageCacheKey = (page, activeFilter) => `page:${activeFilter}:${page}`;
context.readNewsCacheEntry = async key => store[key] || null;
context.writeNewsCacheEntry = async record => { store[record.key] = record; };
context.NEWS_CACHE_MAX_AGE_MS = 999999999;
context.contentEpoch = 9; // live epoch has already moved on by the time this write lands
await context.writeCachedNewsPage(1, 'all', { items: [{ id: 1 }] }, 5); // epoch at request time
assert.equal(store['page:all:1'].epoch, 5);
const rejected = await context.readCachedNewsPage(1, 'all');
assert.equal(rejected, null);
""",
    )


def test_read_cached_news_page_treats_a_pre_epoch_record_as_valid_at_epoch_zero():
    cache_fns = source_between("async function readCachedNewsPage(", "async function cleanupNewsCache")
    run_node(
        cache_fns,
        """
const store = {
  'page:all:1': { key: 'page:all:1', kind: 'page', updatedAt: Date.now(), data: { items: [{ id: 1 }] } },
};
context.newsPageCacheKey = (page, activeFilter) => `page:${activeFilter}:${page}`;
context.readNewsCacheEntry = async key => store[key] || null;
context.writeNewsCacheEntry = async record => { store[record.key] = record; };
context.NEWS_CACHE_MAX_AGE_MS = 999999999;
context.contentEpoch = 0; // fresh session, no bump has ever happened yet
const result = await context.readCachedNewsPage(1, 'all');
assert.deepEqual(result, { items: [{ id: 1 }] });
""",
    )


def test_load_news_page_stamps_cache_writes_with_the_epoch_captured_at_request_start():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 5;
context.pageRequestSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = async () => null;
context.writeCalls = [];
context.rememberCalls = [];
context.writeCachedNewsPage = async (page, activeFilter, data, epoch) => {
  context.writeCalls.push(epoch);
  context.contentEpoch = 9; // simulate a bump landing while this write is in flight
};
context.rememberBufferedPage = (page, activeFilter, data, epoch) => { context.rememberCalls.push(epoch); };
context.applyNewsPage = () => {};
context.renderColdStartSkeleton = () => {};
context.fetchNewsPage = async () => ({ items: [{ id: 1 }], total: 1 });
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 1;
context.document = { getElementById: () => ({ classList: { contains: () => false } }) };
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all',
  useCache: false,
  forceNetwork: true,
  userInitiated: true,
});
assert.equal(loaded, true);
// Must stamp with the epoch captured before the fetch (5), not the live value (9)
// a concurrent bump moved contentEpoch to while the write was in flight — otherwise
// stale data fetched under the old assumption would be marked fresh for the new epoch.
assert.deepEqual(context.writeCalls, [5]);
assert.deepEqual(context.rememberCalls, [5]);
""",
    )


def test_successful_empty_cold_start_revalidates_with_get_until_articles_arrive():
    startup = source_between(
        "function isStartupInitializationResponse(",
        "function renderSourceDeepLinkError",
    )
    run_node(
        startup,
        """
context.coldStartInitializationActive = false;
context.coldStartInitializationTimedOut = false;
context.startupCalibrationGeneration = 0;
context.startupCalibrationController = null;
context.startupCalibrationTimer = null;
context.filter = 'all';
context.currentPage = 1;
context.pageRequestSequence = 7;
context.document = { hidden: false };
context.renderList = () => context.renders.push(
  context.coldStartInitializationActive ? 'initializing' : 'settled'
);
context.renders = [];
context.applied = [];
context.applyNewsPage = data => context.applied.push(data.items.map(item => item.id));
context.scheduleAdjacentPagePrefetch = () => {};
context.fetchNewsPage = async (page, activeFilter, signal) => {
  context.fetchCalls.push({ page, activeFilter, signal });
  return { items: [{ id: 9 }], total: 1, page: 1 };
};
context.fetchCalls = [];
const scheduled = [];
let nextTimer = 0;
context.setTimeout = callback => {
  const timer = { id: ++nextTimer, callback };
  scheduled.push(timer);
  return timer.id;
};
context.clearTimeout = id => {
  const index = scheduled.findIndex(timer => timer.id === id);
  if (index >= 0) scheduled.splice(index, 1);
};
const initial = {
  items: [], total: 0, page: 1,
  diagnostics: { refresh_job: { status: 'running', trigger: 'startup' } },
};
const started = context.startStartupEmptyRevalidation(initial, {
  page: 1, activeFilter: 'all', requestSeq: 7, maxAttempts: 3, intervalMs: 5000,
});
assert.equal(started, true);
assert.deepEqual(context.renders, ['initializing']);
assert.equal(scheduled.length, 1);
await scheduled.shift().callback();
assert.equal(context.fetchCalls.length, 1);
assert.equal(context.fetchCalls[0].page, 1);
assert.equal(context.fetchCalls[0].activeFilter, 'all');
assert.ok(context.fetchCalls[0].signal);
assert.deepEqual(context.applied, [[9]]);
assert.equal(context.coldStartInitializationActive, false);
assert.equal(scheduled.length, 0);
""",
    )


def test_empty_cold_start_revalidation_stops_on_terminal_job_or_attempt_limit():
    startup = source_between(
        "function isStartupInitializationResponse(",
        "function renderSourceDeepLinkError",
    )
    run_node(
        startup,
        """
context.coldStartInitializationActive = false;
context.coldStartInitializationTimedOut = false;
context.startupCalibrationGeneration = 0;
context.startupCalibrationController = null;
context.startupCalibrationTimer = null;
context.filter = 'all';
context.currentPage = 1;
context.pageRequestSequence = 3;
context.document = { hidden: false };
context.renderList = () => context.renders++;
context.renders = 0;
context.applied = 0;
context.applyNewsPage = () => context.applied++;
context.scheduleAdjacentPagePrefetch = () => {};
const initial = {
  items: [], total: 0, page: 1,
  diagnostics: { refresh_job: { status: 'running', trigger: 'startup' } },
};
const timers = [];
let nextTimer = 0;
context.setTimeout = callback => {
  const timer = { id: ++nextTimer, callback };
  timers.push(timer);
  return timer.id;
};
context.clearTimeout = id => {
  const index = timers.findIndex(timer => timer.id === id);
  if (index >= 0) timers.splice(index, 1);
};
context.fetchNewsPage = async () => ({
  items: [], total: 0, page: 1,
  diagnostics: { refresh_job: { status: 'completed', trigger: 'startup' } },
});
context.startStartupEmptyRevalidation(initial, {
  page: 1, activeFilter: 'all', requestSeq: 3, maxAttempts: 2, intervalMs: 5000,
});
await timers.shift().callback();
assert.equal(context.applied, 1);
assert.equal(context.coldStartInitializationActive, false);
assert.equal(timers.length, 0);

context.fetchNewsPage = async () => initial;
context.startStartupEmptyRevalidation(initial, {
  page: 1, activeFilter: 'all', requestSeq: 3, maxAttempts: 1, intervalMs: 5000,
});
await timers.shift().callback();
assert.equal(context.coldStartInitializationActive, false);
assert.equal(context.coldStartInitializationTimedOut, true);
assert.equal(timers.length, 0);
""",
    )


def test_view_navigation_cancels_startup_revalidation_and_hidden_work_without_posting():
    startup = source_between(
        "function isStartupInitializationResponse(",
        "function renderSourceDeepLinkError",
    )
    run_node(
        startup,
        """
context.coldStartInitializationActive = false;
context.coldStartInitializationTimedOut = false;
context.startupCalibrationGeneration = 0;
context.startupCalibrationController = null;
context.startupCalibrationTimer = null;
context.filter = 'all';
context.currentPage = 1;
context.pageRequestSequence = 4;
context.document = { hidden: false };
context.renderList = () => {};
context.applyNewsPage = () => context.applied++;
context.applied = 0;
context.scheduleAdjacentPagePrefetch = () => {};
context.postCalls = 0;
context.requestRefreshOnce = () => { context.postCalls++; };
let resolveFetch;
context.fetchNewsPage = (page, activeFilter, signal) => new Promise((resolve, reject) => {
  resolveFetch = resolve;
  signal.addEventListener('abort', () => reject(
    Object.assign(new Error('cancelled'), { name: 'AbortError' })
  ));
});
const timers = [];
context.setTimeout = callback => { timers.push(callback); return timers.length; };
context.clearTimeout = () => {};
const initial = {
  items: [], total: 0, page: 1,
  diagnostics: { refresh_job: { status: 'running', trigger: 'startup' } },
};
context.startStartupEmptyRevalidation(initial, {
  page: 1, activeFilter: 'all', requestSeq: 4, maxAttempts: 3, intervalMs: 5000,
});
const inFlight = timers.shift()();
await Promise.resolve();
context.cancelStartupEmptyRevalidation();
await inFlight;
resolveFetch({ items: [{ id: 1 }], total: 1 });
await Promise.resolve();
assert.equal(context.applied, 0);
assert.equal(context.postCalls, 0);
assert.equal(context.coldStartInitializationActive, false);
""",
    )


def test_return_to_foreground_resumes_empty_startup_with_get_only():
    foreground = source_between(
        "function onReturnToForeground()",
        "document.addEventListener('visibilitychange', () =>",
    )
    run_node(
        foreground,
        """
context.Date = { now: () => 5000 };
context.lastForegroundSyncAt = 0;
context.pageVisibleSince = 0;
context.lastUserActivityAt = 0;
context.latestKnownTimestamp = 0;
context.latestNewsTimestamp = () => 0;
context.news = [];
context.filter = 'all';
context.currentPage = 1;
context.lastNewsDiagnostics = {
  refresh_job: { status: 'running', trigger: 'startup' },
};
context.loadSince = () => { context.incrementalCalls++; };
context.incrementalCalls = 0;
context.pollTitleUpdates = () => {};
context.loadCalls = [];
context.loadNewsPage = (page, options) => context.loadCalls.push([page, options]);
context.postCalls = 0;
context.requestRefreshOnce = () => { context.postCalls++; };
context.onReturnToForeground();
assert.equal(context.incrementalCalls, 0);
assert.equal(context.loadCalls.length, 1);
assert.equal(context.loadCalls[0][0], 1);
assert.equal(context.loadCalls[0][1].activeFilter, 'all');
assert.equal(context.loadCalls[0][1].forceNetwork, true);
assert.equal(context.postCalls, 0);
""",
    )


def test_load_news_page_checks_application_guard_before_mutating_visible_list():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestController = null;
context.news = [{ id: 1 }];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applyNewsPage = () => context.applied++;
context.applied = 0;
context.renderColdStartSkeleton = () => {};
context.fetchNewsPage = async () => ({ items: [{ id: 2 }], total: 1 });
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all',
  useCache: false,
  forceNetwork: true,
  applicationGuard: () => false,
});
assert.equal(loaded, false);
assert.equal(context.applied, 0);
""",
    )


def test_manual_refresh_completion_list_fetch_is_aborted_by_flow_signal():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [{ id: 1 }];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applyNewsPage = () => context.applied++;
context.applied = 0;
context.renderColdStartSkeleton = () => {};
let capturedSignal;
let resolveFetch;
context.fetchNewsPage = (page, activeFilter, signal) => {
  capturedSignal = signal;
  return new Promise(resolve => { resolveFetch = resolve; });
};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const flow = new AbortController();
const pending = context.loadNewsPage(1, {
  activeFilter: 'all', useCache: false, forceNetwork: true,
  externalSignal: flow.signal,
});
await Promise.resolve();
assert.ok(capturedSignal);
assert.equal(capturedSignal.aborted, false);
flow.abort();
await Promise.resolve();
assert.equal(capturedSignal.aborted, true);
resolveFetch({ items: [{ id: 2 }], total: 1 });
const loaded = await pending;
assert.equal(loaded, false);
assert.equal(context.applied, 0);
""",
    )


def test_startup_empty_network_response_keeps_nonempty_cached_articles_visible():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
const cached = { items: [{ id: 1 }], total: 1, page: 1 };
const initializing = {
  items: [], total: 0, page: 1,
  diagnostics: { refresh_job: { status: 'running', trigger: 'startup' } },
};
context.readCachedNewsPage = async () => cached;
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = data => {
  context.applied.push(data.items.map(item => item.id));
  context.news = data.items;
};
context.renderColdStartSkeleton = () => {};
context.fetchNewsPage = async () => initializing;
context.cacheWrites = 0;
context.writeCachedNewsPage = async () => { context.cacheWrites++; };
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
context.isStartupInitializationResponse = data => data === initializing;
context.startCalls = 0;
context.startStartupEmptyRevalidation = () => { context.startCalls++; };
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all', useCache: true, forceNetwork: true,
});
assert.equal(loaded, true);
assert.deepEqual(context.applied, [[1]]);
assert.deepEqual(Array.from(context.news, item => item.id), [1]);
assert.equal(context.startCalls, 1);
assert.equal(context.cacheWrites, 0);
""",
    )


def test_load_news_page_checks_application_guard_before_applying_cached_list():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestController = null;
context.news = [{ id: 1 }];
context.readCachedNewsPage = async () => ({ items: [{ id: 2 }], total: 1 });
context.rememberBufferedPage = () => {};
context.applyNewsPage = () => context.applied++;
context.applied = 0;
context.renderColdStartSkeleton = () => {};
context.fetchNewsPage = async () => ({ items: [{ id: 3 }], total: 1 });
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all',
  useCache: true,
  forceNetwork: true,
  applicationGuard: () => false,
});
assert.equal(loaded, false);
assert.equal(context.applied, 0);
""",
    )


def test_overlapping_list_request_finally_cannot_clear_newer_pending_marker():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [{ id: 1 }];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applyNewsPage = () => {};
context.renderColdStartSkeleton = () => {};
const resolvers = [];
context.fetchNewsPage = () => new Promise(resolve => resolvers.push(resolve));
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const first = context.loadNewsPage(1, { activeFilter: 'all', useCache: false });
await Promise.resolve();
assert.equal(context.pageRequestPendingSequence, 1);
const second = context.loadNewsPage(1, { activeFilter: 'cat:Tech', useCache: false });
await Promise.resolve();
assert.equal(context.pageRequestPendingSequence, 2);
resolvers[0]({ items: [{ id: 2 }], total: 1 });
await first;
assert.equal(context.pageRequestPendingSequence, 2);
resolvers[1]({ items: [{ id: 3 }], total: 1 });
await second;
assert.equal(context.pageRequestPendingSequence, 0);
""",
    )


def test_cached_source_metadata_renders_while_network_revalidation_is_pending():
    load_sources = source_between("async function loadSourceCategories(", "function sourceLabel")
    run_node(
        load_sources,
        """
const events = [];
let resolveNetwork;
context.readNewsCacheEntry = async () => ({ data: { sources: ['cached'] } });
context.withCacheTimeout = async value => value;
context.withPromiseTimeout = async value => value;
context.rebuildCategoryMap = sources => events.push(`rebuild:${sources[0]}`);
context.renderFilters = () => events.push('render');
context.isRestrictedUser = () => false;
context.apiFetch = () => {
  events.push('network');
  return new Promise(resolve => { resolveNetwork = resolve; });
};
context.writeNewsCacheEntry = () => {};
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loading = context.loadSourceCategories();
await new Promise(resolve => setImmediate(resolve));
assert.deepEqual(events, ['rebuild:cached', 'render', 'network']);
resolveNetwork({ sources: ['fresh'] });
await loading;
assert.deepEqual(events, [
  'rebuild:cached', 'render', 'network', 'rebuild:fresh', 'render',
]);
""",
    )


def test_cached_source_metadata_network_failure_uses_quiet_nonblocking_hint():
    load_sources = source_between("async function loadSourceCategories(", "function sourceLabel")
    run_node(
        load_sources,
        """
context.readNewsCacheEntry = async () => ({ data: { sources: ['cached'] } });
context.withCacheTimeout = async value => value;
context.rebuildCategoryMap = () => {};
context.renderFilters = () => {};
context.isRestrictedUser = () => false;
context.apiFetch = async () => { throw new Error('offline'); };
context.writeNewsCacheEntry = () => {};
context.hasLoadedNewsOnce = true;
context.document = { hidden: false };
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.setTimeout = () => 1;
context.clearTimeout = () => {};
await context.loadSourceCategories();
assert.deepEqual(context.toasts, ['来源信息暂未更新，已显示缓存内容']);
context.toasts = [];
await context.loadSourceCategories({ quietNetworkError: true });
assert.deepEqual(context.toasts, []);
context.apiFetch = async () => { throw Object.assign(new Error('cancelled'), { name: 'AbortError' }); };
await context.loadSourceCategories();
assert.deepEqual(context.toasts, []);
""",
    )


def test_bootstrap_starts_source_news_and_count_requests_without_serial_waits():
    bootstrap = source_between("async function bootstrapNews(", "// Initial load")
    run_node(
        bootstrap,
        """
const starts = [];
const resolvers = {};
const pending = name => {
  starts.push(name);
  return new Promise(resolve => { resolvers[name] = resolve; });
};
context.filter = 'all';
context.currentPage = 9;
context.location = { pathname: '/', search: '' };
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.listStateFromUrl = () => ({ activeFilter: 'cat:Tech' });
context.renderTopCatBar = () => starts.push('topbar');
context.loadSourceCategories = () => pending('sources');
context.loadNewsPage = (page, options) => {
  assert.equal(page, 1);
  assert.equal(options.activeFilter, 'cat:Tech');
  assert.equal(options.useCache, true);
  assert.equal(options.forceNetwork, true);
  assert.equal(options.networkRetries, 1);
  return pending('news');
};
context.refreshTodayArticleCount = () => pending('count');
context.renderFilters = () => starts.push('filters');
context.syncListUrl = () => starts.push('sync');
context.resetMobileColdStartScroll = () => starts.push('scroll');
const loading = context.bootstrapNews();
assert.deepEqual(starts, ['topbar', 'sources', 'news', 'count']);
assert.equal(context.filter, 'cat:Tech');
assert.equal(context.currentPage, 1);
resolvers.sources();
resolvers.news();
resolvers.count();
await loading;
assert.deepEqual(starts.slice(-3), ['filters', 'sync', 'scroll']);
""",
    )


def test_bootstrap_resolves_source_label_deep_link_after_metadata_hydration():
    list_state = source_between("function listStateFromUrl()", "async function restoreListStateFromUrl")
    bootstrap = source_between("async function bootstrapNews(", "// Initial load")
    run_node(
        list_state + bootstrap,
        """
const sourceKey = 'srcgrp:' + encodeURIComponent('财经早餐');
context.location = { pathname: '/', search: '?source=' + encodeURIComponent('财经早餐') + '&page=1' };
context.sourceFilterGroups = {};
context.CATEGORY_ORDER = ['News', 'Tech', 'Biz', 'Info'];
context.URLSearchParams = URLSearchParams;
context.filter = 'all';
context.currentPage = 9;
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.renderTopCatBar = () => {};
context.loadSourceCategories = async ({ onMetadataReady } = {}) => {
  await Promise.resolve();
  context.sourceFilterGroups[sourceKey] = {
    key: sourceKey,
    label: '财经早餐',
    sources: ['财经早餐'],
  };
  if (onMetadataReady) onMetadataReady();
};
context.requested = null;
context.loadNewsPage = async (page, options) => {
  context.requested = { page, activeFilter: options.activeFilter };
  return true;
};
context.refreshTodayArticleCount = async () => {};
context.renderFilters = () => {};
context.syncListUrl = () => {};
context.resetMobileColdStartScroll = () => {};
context.renderSourceDeepLinkError = () => {};
await context.bootstrapNews();
assert.deepEqual(context.requested, { page: 1, activeFilter: sourceKey });
assert.equal(context.filter, sourceKey);
assert.equal(context.currentPage, 1);
""",
    )


def test_source_deep_link_metadata_network_timeout_enters_dedicated_error_state():
    load_sources = source_between("async function loadSourceCategories(", "function sourceLabel")
    cache_timeout = source_between("async function withCacheTimeout(", "async function loadNewsPageRequest")
    bootstrap = source_between("async function bootstrapNews(", "// Initial load")
    run_node(
        load_sources + cache_timeout + bootstrap,
        """
const originalSearch = '?source=' + encodeURIComponent('财经早餐') + '&page=1';
context.location = { pathname: '/', search: originalSearch };
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.listStateFromUrl = () => ({ activeFilter: 'all', page: 1 });
context.readNewsCacheEntry = async () => null;
context.rebuildCategoryMap = () => {};
context.renderFilters = () => {};
context.isRestrictedUser = () => false;
context.apiFetch = (url, options = {}) => new Promise((resolve, reject) => {
  if (options.signal) {
    options.signal.addEventListener('abort', () => reject(
      Object.assign(new Error('timed out'), { name: 'AbortError' })
    ));
  }
});
context.writeNewsCacheEntry = () => {};
context.renderTopCatBar = () => {};
context.newsCalls = 0;
context.loadNewsPage = async () => { context.newsCalls++; return true; };
context.refreshTodayArticleCount = async () => {};
context.syncCalls = 0;
context.syncListUrl = () => context.syncCalls++;
context.resetMobileColdStartScroll = () => {};
context.sourceErrors = [];
context.renderSourceDeepLinkError = message => context.sourceErrors.push(message);
context.genericErrors = [];
context.renderColdStartError = message => context.genericErrors.push(message);
context.setTimeout = callback => setTimeout(callback, 5);
context.clearTimeout = clearTimeout;
const loading = context.bootstrapNews();
const outcome = await Promise.race([
  loading.then(() => 'done'),
  new Promise(resolve => setTimeout(() => resolve('hung'), 60)),
]);
assert.equal(outcome, 'done');
assert.equal(context.newsCalls, 0);
assert.equal(context.sourceErrors.length, 1);
assert.equal(context.genericErrors.length, 0);
assert.equal(context.location.search, originalSearch);
assert.equal(context.syncCalls, 0);
""",
    )


def test_each_fresh_source_timeout_aborts_its_fetch_and_clears_its_timer():
    load_sources = source_between("async function loadSourceCategories(", "function sourceLabel")
    cache_timeout = source_between("async function withCacheTimeout(", "async function loadNewsPageRequest")
    run_node(
        load_sources + cache_timeout,
        """
context.readNewsCacheEntry = async () => null;
context.rebuildCategoryMap = () => {};
context.renderFilters = () => {};
context.isRestrictedUser = () => false;
context.writeNewsCacheEntry = () => {};
context.controllers = [];
context.AbortController = class {
  constructor() {
    const listeners = [];
    this.signal = {
      aborted: false,
      addEventListener(type, listener) { if (type === 'abort') listeners.push(listener); },
    };
    this.listeners = listeners;
    context.controllers.push(this);
  }
  abort() {
    if (this.signal.aborted) return;
    this.signal.aborted = true;
    this.listeners.forEach(listener => listener());
  }
};
context.activeFetches = 0;
context.receivedSignals = [];
context.apiFetch = (url, options) => {
  context.receivedSignals.push(options && options.signal);
  context.activeFetches++;
  return new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => {
      context.activeFetches--;
      reject(Object.assign(new Error('timed out'), { name: 'AbortError' }));
    });
  });
};
let timerId = 0;
context.clearedTimers = [];
context.setTimeout = callback => {
  const id = ++timerId;
  setImmediate(callback);
  return id;
};
context.clearTimeout = id => context.clearedTimers.push(id);
await context.loadSourceCategories({ useCache: false, networkTimeoutMs: 5 });
await context.loadSourceCategories({ useCache: false, networkTimeoutMs: 5 });
assert.equal(context.controllers.length, 2);
assert.equal(context.receivedSignals.length, 2);
assert.equal(context.receivedSignals[0], context.controllers[0].signal);
assert.equal(context.receivedSignals[1], context.controllers[1].signal);
assert.deepEqual(context.controllers.map(controller => controller.signal.aborted), [true, true]);
assert.equal(context.activeFetches, 0);
assert.deepEqual(context.clearedTimers, [1, 2]);
""",
    )


def test_source_deep_link_wait_does_not_override_newer_user_navigation():
    list_state = source_between("function listStateFromUrl()", "async function restoreListStateFromUrl")
    bootstrap = source_between("async function bootstrapNews(", "// Initial load")
    run_node(
        list_state + bootstrap,
        """
const sourceKey = 'srcgrp:' + encodeURIComponent('财经早餐');
context.location = {
  pathname: '/',
  search: '?source=' + encodeURIComponent('财经早餐') + '&page=1',
};
context.sourceFilterGroups = {};
context.CATEGORY_ORDER = ['News', 'Tech', 'Biz', 'Info'];
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 4;
context.pageRequestSequence = 7;
context.renderTopCatBar = () => {};
let notifyMetadata;
let resolveSources;
context.loadSourceCategories = ({ onMetadataReady } = {}) => {
  notifyMetadata = onMetadataReady;
  return new Promise(resolve => { resolveSources = resolve; });
};
context.newsCalls = [];
context.loadNewsPage = async (page, options) => {
  context.newsCalls.push({ page, activeFilter: options.activeFilter });
  return true;
};
context.refreshTodayArticleCount = async () => {};
context.renderFilters = () => {};
context.syncListUrl = () => {};
context.resetMobileColdStartScroll = () => {};
context.renderSourceDeepLinkError = () => {};
const loading = context.bootstrapNews();
assert.equal(typeof notifyMetadata, 'function');
context.location.search = '?category=Tech&page=1';
context.filter = 'cat:Tech';
context.currentPage = 1;
context.pageNavigationSequence++;
context.pageRequestSequence++;
context.sourceFilterGroups[sourceKey] = {
  key: sourceKey, label: '财经早餐', sources: ['财经早餐'],
};
notifyMetadata();
resolveSources();
await loading;
assert.deepEqual(context.newsCalls, []);
assert.equal(context.filter, 'cat:Tech');
assert.equal(context.location.search, '?category=Tech&page=1');
""",
    )


def test_source_retry_lock_releases_after_hung_today_count_is_aborted():
    list_state = source_between("function listStateFromUrl()", "async function restoreListStateFromUrl")
    today_count = source_between("async function refreshTodayArticleCount(", "let dailySummaryState")
    cold_start = source_between("function resetMobileColdStartScroll()", "// Initial load")
    run_node(
        list_state + today_count + cold_start,
        """
const originalSearch = '?source=' + encodeURIComponent('财经早餐') + '&page=1';
context.location = { pathname: '/', search: originalSearch };
context.sourceFilterGroups = {};
context.CATEGORY_ORDER = ['News', 'Tech', 'Biz', 'Info'];
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.renderTopCatBar = () => {};
context.metadataCalls = 0;
context.loadSourceCategories = async ({ onMetadataReady } = {}) => {
  context.metadataCalls++;
  if (onMetadataReady) onMetadataReady();
};
context.loadNewsPage = async () => { throw new Error('must not load All'); };
context.renderFilters = () => {};
context.syncListUrl = () => {};
context.resetMobileColdStartScroll = () => {};
context.renderSourceDeepLinkError = () => {};
context.beijingDateString = () => '2026-07-16';
context.countTodayArticles = () => 0;
context.todayArticleCount = null;
context.countControllers = [];
context.AbortController = class {
  constructor() {
    const listeners = [];
    this.signal = {
      aborted: false,
      addEventListener(type, listener) { if (type === 'abort') listeners.push(listener); },
    };
    this.listeners = listeners;
    context.countControllers.push(this);
  }
  abort() {
    if (this.signal.aborted) return;
    this.signal.aborted = true;
    this.listeners.forEach(listener => listener());
  }
};
context.countFetchCalls = 0;
context.fetch = (url, options = {}) => {
  context.countFetchCalls++;
  if (context.countFetchCalls === 1) return Promise.resolve({ ok: false });
  return new Promise((resolve, reject) => {
    if (options.signal) {
      options.signal.addEventListener('abort', () => reject(
        Object.assign(new Error('timed out'), { name: 'AbortError' })
      ));
    }
  });
};
let nextTimer = 0;
const cancelledTimers = new Set();
context.setTimeout = callback => {
  const id = ++nextTimer;
  setImmediate(() => { if (!cancelledTimers.has(id)) callback(); });
  return id;
};
context.clearTimeout = id => cancelledTimers.add(id);
await context.bootstrapNews();
const first = context.retrySourceDeepLink();
const outcome = await Promise.race([
  first.then(value => ({ state: 'done', value })),
  new Promise(resolve => setTimeout(() => resolve({ state: 'hung' }), 60)),
]);
assert.deepEqual(outcome, { state: 'done', value: false });
assert.equal(context.countControllers.length, 2);
assert.equal(context.countControllers[0].signal.aborted, false);
assert.equal(context.countControllers[1].signal.aborted, true);
const second = await context.retrySourceDeepLink();
assert.equal(second, false);
assert.equal(context.metadataCalls, 3);
assert.equal(context.countControllers.length, 3);
assert.equal(context.countControllers[2].signal.aborted, true);
""",
    )


def test_source_deep_link_error_retry_refetches_metadata_and_reparses_original_url():
    list_state = source_between("function listStateFromUrl()", "async function restoreListStateFromUrl")
    cold_start = source_between("function resetMobileColdStartScroll()", "// Initial load")
    run_node(
        list_state + cold_start,
        """
const sourceKey = 'srcgrp:' + encodeURIComponent('财经早餐');
const originalSearch = '?source=' + encodeURIComponent('财经早餐') + '&page=1';
context.location = { pathname: '/', search: originalSearch };
context.sourceFilterGroups = {};
context.CATEGORY_ORDER = ['News', 'Tech', 'Biz', 'Info'];
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.renderTopCatBar = () => {};
context.metadataCalls = 0;
context.loadSourceCategories = async ({ onMetadataReady } = {}) => {
  context.metadataCalls++;
  if (context.metadataCalls === 2) {
    context.sourceFilterGroups[sourceKey] = {
      key: sourceKey, label: '财经早餐', sources: ['财经早餐'],
    };
  }
  if (onMetadataReady) onMetadataReady();
};
context.newsCalls = [];
context.loadNewsPage = async (page, options) => {
  context.newsCalls.push({ page, activeFilter: options.activeFilter });
  return true;
};
context.refreshTodayArticleCount = async () => {};
context.renderFilters = () => {};
context.syncListUrl = () => {};
context.resetMobileColdStartScroll = () => {};
context.sourceErrors = [];
context.renderSourceDeepLinkError = message => context.sourceErrors.push(message);
context.renderColdStartError = () => {};
context.loadDataCalls = 0;
context.loadData = () => context.loadDataCalls++;
await context.bootstrapNews();
assert.equal(context.sourceErrors.length, 1);
assert.equal(context.newsCalls.length, 0);
assert.equal(context.location.search, originalSearch);
assert.equal(typeof context.retrySourceDeepLink, 'function');
await context.retrySourceDeepLink();
assert.equal(context.metadataCalls, 2);
assert.deepEqual(context.newsCalls, [{ page: 1, activeFilter: sourceKey }]);
assert.equal(context.loadDataCalls, 0);
assert.equal(context.location.search, originalSearch);
""",
    )


def test_real_load_news_page_forwards_source_snapshot_to_fetch_query():
    params = source_between("function buildNewsPageParams(", "function listUrlForState")
    fetch_page = source_between("async function fetchNewsPage(", "function warmPageCoverImages")
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        params + fetch_page + load_page,
        """
const sourceKey = 'srcgrp:' + encodeURIComponent('财经早餐');
const sourceSnapshot = ['财经早餐', '财经早餐别名'];
context.PAGE_SIZE = 30;
context.sourceFilterGroups = {};
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applyNewsPage = () => {};
context.renderColdStartSkeleton = () => {};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 0;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.requestUrl = '';
context.fetch = async (url, options) => {
  context.requestUrl = url;
  assert.ok(options.signal);
  return {
    ok: true,
    json: async () => ({ items: [{ id: 1 }], total: 1 }),
  };
};
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loaded = await context.loadNewsPage(1, {
  activeFilter: sourceKey,
  useCache: false,
  forceNetwork: true,
  sourceSnapshot,
});
assert.equal(loaded, true);
const query = new URLSearchParams(context.requestUrl.split('?')[1]);
assert.deepEqual(query.getAll('source'), sourceSnapshot);
assert.equal(context.pageRequestPendingSequence, 0);
""",
    )


def test_source_deep_link_snapshots_sources_before_metadata_revalidation_removes_group():
    params = source_between("function buildNewsPageParams(", "function listUrlForState")
    list_state = source_between("function listStateFromUrl()", "async function restoreListStateFromUrl")
    bootstrap = source_between("async function bootstrapNews(", "// Initial load")
    run_node(
        params + list_state + bootstrap,
        """
const sourceKey = 'srcgrp:' + encodeURIComponent('财经早餐');
context.location = {
  pathname: '/',
  search: '?source=' + encodeURIComponent('财经早餐') + '&page=1',
};
context.sourceFilterGroups = {};
context.CATEGORY_ORDER = ['News', 'Tech', 'Biz', 'Info'];
context.PAGE_SIZE = 30;
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.renderTopCatBar = () => {};
context.loadSourceCategories = ({ onMetadataReady } = {}) => {
  context.sourceFilterGroups[sourceKey] = {
    key: sourceKey,
    label: '财经早餐',
    sources: ['财经早餐', '财经早餐别名'],
  };
  if (onMetadataReady) onMetadataReady();
  return new Promise(resolve => setImmediate(() => {
    context.sourceFilterGroups = {};
    resolve();
  }));
};
context.requestSources = null;
context.loadNewsPage = async (page, options) => {
  await new Promise(resolve => setImmediate(resolve));
  const built = context.buildNewsPageParams(page, options.activeFilter, options.sourceSnapshot);
  context.requestSources = built.getAll('source');
  return true;
};
context.refreshTodayArticleCount = async () => {};
context.renderFilters = () => {};
context.syncListUrl = () => {};
context.resetMobileColdStartScroll = () => {};
context.renderSourceDeepLinkError = () => {};
await context.bootstrapNews();
assert.deepEqual(context.requestSources, ['财经早餐', '财经早餐别名']);
""",
    )


def test_startup_empty_revalidation_times_out_hung_gets_and_reaches_bound():
    startup = source_between(
        "function isStartupInitializationResponse(",
        "function renderSourceDeepLinkError",
    )
    run_node(
        startup,
        """
context.coldStartInitializationActive = false;
context.coldStartInitializationTimedOut = false;
context.startupCalibrationGeneration = 0;
context.startupCalibrationController = null;
context.startupCalibrationTimer = null;
context.filter = 'all';
context.currentPage = 1;
context.pageRequestSequence = 12;
context.document = { hidden: false };
context.renderList = () => {};
context.applyNewsPage = () => { context.applied++; };
context.applied = 0;
context.scheduleAdjacentPagePrefetch = () => {};
context.fetchSignals = [];
context.fetchNewsPage = (page, activeFilter, signal) => {
  context.fetchSignals.push(signal);
  return new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(
      Object.assign(new Error('timed out'), { name: 'AbortError' })
    ));
  });
};
let nextTimer = 0;
context.timers = [];
context.cleared = [];
context.setTimeout = (callback, timeout) => {
  const timer = { id: ++nextTimer, callback, timeout };
  context.timers.push(timer);
  return timer.id;
};
context.clearTimeout = id => context.cleared.push(id);
const initial = {
  items: [], total: 0, page: 1,
  diagnostics: { refresh_job: { status: 'running', trigger: 'startup' } },
};
context.startStartupEmptyRevalidation(initial, {
  page: 1, activeFilter: 'all', requestSeq: 12,
  maxAttempts: 2, intervalMs: 5000, requestTimeoutMs: 12000,
});
const firstInterval = context.timers.shift();
assert.equal(firstInterval.timeout, 5000);
const firstPoll = firstInterval.callback();
await Promise.resolve();
assert.equal(context.fetchSignals.length, 1);
assert.equal(context.fetchSignals[0].aborted, false);
assert.equal(context.timers.length, 1);
const firstRequestTimeout = context.timers.shift();
assert.equal(firstRequestTimeout.timeout, 12000);
firstRequestTimeout.callback();
await firstPoll;
assert.equal(context.fetchSignals[0].aborted, true);
assert.ok(context.cleared.includes(firstRequestTimeout.id));
assert.equal(context.timers.length, 1);

const secondInterval = context.timers.shift();
assert.equal(secondInterval.timeout, 5000);
const secondPoll = secondInterval.callback();
await Promise.resolve();
assert.equal(context.fetchSignals.length, 2);
const secondRequestTimeout = context.timers.shift();
assert.equal(secondRequestTimeout.timeout, 12000);
secondRequestTimeout.callback();
await secondPoll;
assert.equal(context.fetchSignals[1].aborted, true);
assert.ok(context.cleared.includes(secondRequestTimeout.id));
assert.equal(context.coldStartInitializationActive, false);
assert.equal(context.coldStartInitializationTimedOut, true);
assert.equal(context.timers.length, 0);
assert.equal(context.applied, 0);
""",
    )


def test_cold_start_network_retry_uses_a_fresh_abort_controller():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = async () => ({ items: [{ id: 'cached' }], total: 1 });
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = (data, page, activeFilter, options) => context.applied.push({
  id: data.items[0].id,
  preserveDom: options && options.preserveDom,
});
context.renderColdStartSkeleton = () => {};
context.fetchSignals = [];
context.fetchNewsPage = async (page, activeFilter, signal) => {
  context.fetchSignals.push(signal);
  if (context.fetchSignals.length === 1) throw new Error('offline');
  return { items: [{ id: 'fresh' }], total: 1 };
};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.loading = [];
context.setPageLoading = state => context.loading.push(state);
context.renderColdStartError = () => { throw new Error('must preserve cache'); };
context.showToast = () => {};
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.delayCalls = [];
context.delay = async milliseconds => context.delayCalls.push(milliseconds);
context.setTimeout = () => 1;
context.clearTimeout = () => {};
let nextController = 0;
context.AbortController = class {
  constructor() { this.signal = { controller: ++nextController }; }
  abort() {}
};
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all',
  useCache: true,
  forceNetwork: true,
  userInitiated: true,
  networkRetries: 1,
});
assert.equal(loaded, true);
assert.deepEqual(context.applied, [
  { id: 'cached', preserveDom: undefined },
  { id: 'fresh', preserveDom: true },
]);
assert.equal(context.fetchSignals.length, 2);
assert.notEqual(context.fetchSignals[0], context.fetchSignals[1]);
assert.deepEqual(context.fetchSignals.map(signal => signal.controller), [1, 2]);
assert.deepEqual(context.delayCalls, [600]);
assert.equal(context.pageRequestPendingSequence, 0);
""",
    )


def test_hung_page_cache_read_cannot_block_cold_start_network():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = () => new Promise(() => {});
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = data => context.applied.push(data.items[0].id);
context.renderColdStartSkeleton = () => {};
context.fetchCalls = 0;
context.fetchNewsPage = async () => {
  context.fetchCalls++;
  return { items: [{ id: 'fresh' }], total: 1 };
};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 0;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = callback => setTimeout(callback, 5);
context.clearTimeout = clearTimeout;
const loading = context.loadNewsPage(1, {
  activeFilter: 'all', useCache: true, forceNetwork: true,
});
const outcome = await Promise.race([
  loading.then(value => ({ state: 'done', value })),
  new Promise(resolve => setTimeout(() => resolve({ state: 'hung' }), 40)),
]);
assert.deepEqual(outcome, { state: 'done', value: true });
assert.equal(context.fetchCalls, 1);
assert.deepEqual(context.applied, ['fresh']);
""",
    )


def test_hung_page_cache_write_cannot_block_fresh_network_application():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = data => context.applied.push(data.items[0].id);
context.renderColdStartSkeleton = () => {};
context.fetchNewsPage = async () => ({ items: [{ id: 'fresh' }], total: 1 });
context.writeCachedNewsPage = () => new Promise(() => {});
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 0;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = callback => setTimeout(callback, 5);
context.clearTimeout = clearTimeout;
const loading = context.loadNewsPage(1, {
  activeFilter: 'all', useCache: true, forceNetwork: true,
});
const outcome = await Promise.race([
  loading.then(value => ({ state: 'done', value })),
  new Promise(resolve => setTimeout(() => resolve({ state: 'hung' }), 40)),
]);
assert.deepEqual(outcome, { state: 'done', value: true });
assert.deepEqual(context.applied, ['fresh']);
""",
    )


def test_network_timeout_aborts_first_attempt_then_retries_with_fresh_controller():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = data => context.applied.push(data.items[0].id);
context.renderColdStartSkeleton = () => {};
context.controllers = [];
context.AbortController = class {
  constructor() {
    const listeners = [];
    this.signal = {
      aborted: false,
      addEventListener(type, listener) { if (type === 'abort') listeners.push(listener); },
    };
    this.listeners = listeners;
    context.controllers.push(this);
  }
  abort() {
    if (this.signal.aborted) return;
    this.signal.aborted = true;
    this.listeners.forEach(listener => listener());
  }
};
context.fetchCalls = 0;
context.fetchNewsPage = (page, activeFilter, signal) => {
  context.fetchCalls++;
  if (context.fetchCalls === 2) {
    return Promise.resolve({ items: [{ id: 'fresh' }], total: 1 });
  }
  return new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(
      Object.assign(new Error('timed out'), { name: 'AbortError' })
    ));
  });
};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 0;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.delay = async () => {};
const cancelled = new Set();
let nextTimer = 0;
let networkTimers = 0;
context.setTimeout = (callback, milliseconds) => {
  const id = ++nextTimer;
  if (milliseconds === 12000 && networkTimers++ === 0) {
    setImmediate(() => { if (!cancelled.has(id)) callback(); });
  }
  return id;
};
context.clearTimeout = id => cancelled.add(id);
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all', useCache: false, forceNetwork: true, networkRetries: 1,
});
assert.equal(loaded, true);
assert.equal(context.fetchCalls, 2);
assert.equal(context.controllers.length, 2);
assert.notEqual(context.controllers[0], context.controllers[1]);
assert.equal(context.controllers[0].signal.aborted, true);
assert.equal(context.controllers[1].signal.aborted, false);
assert.deepEqual(context.applied, ['fresh']);
""",
    )


def test_retry_stops_when_an_overlapping_request_supersedes_the_owner():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [{ id: 'visible' }];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = data => context.applied.push(data.items[0].id);
context.renderColdStartSkeleton = () => {};
context.fetches = [];
context.fetchNewsPage = async (page, activeFilter) => {
  context.fetches.push(activeFilter);
  if (activeFilter === 'all') throw new Error('offline');
  return { items: [{ id: 'tech' }], total: 1 };
};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.errors = [];
context.renderColdStartError = message => context.errors.push(message);
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
let releaseRetry;
context.delay = () => new Promise(resolve => { releaseRetry = resolve; });
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const first = context.loadNewsPage(1, {
  activeFilter: 'all', useCache: false, userInitiated: true, networkRetries: 1,
});
for (let turn = 0; turn < 5 && !releaseRetry; turn++) await Promise.resolve();
assert.equal(typeof releaseRetry, 'function');
const second = context.loadNewsPage(1, {
  activeFilter: 'cat:Tech', useCache: false, userInitiated: true,
});
assert.equal(await second, true);
releaseRetry();
assert.equal(await first, false);
assert.deepEqual(context.fetches, ['all', 'cat:Tech']);
assert.deepEqual(context.applied, ['tech']);
assert.deepEqual(context.errors, []);
assert.deepEqual(context.toasts, []);
assert.equal(context.pageRequestPendingSequence, 0);
""",
    )


def test_final_cold_start_failure_keeps_cached_articles_visible():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = async () => ({ items: [{ id: 'cached' }], total: 1 });
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = data => context.applied.push(data.items[0].id);
context.renderColdStartSkeleton = () => {};
context.fetchCalls = 0;
context.fetchNewsPage = async () => {
  context.fetchCalls++;
  throw Object.assign(new Error('timeout'), { name: 'AbortError' });
};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.errors = [];
context.renderColdStartError = message => context.errors.push(message);
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.delay = async () => {};
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all',
  useCache: true,
  forceNetwork: true,
  userInitiated: true,
  networkRetries: 1,
});
assert.equal(loaded, true);
assert.equal(context.fetchCalls, 2);
assert.deepEqual(context.applied, ['cached']);
assert.deepEqual(context.errors, []);
assert.deepEqual(context.toasts, ['连接超时，请稍后重试']);
""",
    )


def test_manual_refresh_failure_restores_prompt_only_after_running_state_clears():
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    setup = trigger_context_setup("throw Object.assign(new Error('timeout'), { name: 'AbortError' });")
    run_node(
        trigger,
        setup
        + """
context.loadNewsPage = async () => { throw new Error('must not load'); };
await context.triggerRefresh();
assert.deepEqual(context.runningStates, [true, false]);
assert.deepEqual(context.promptStates, [false]);
assert.equal(context.refreshInProgress, false);
""",
    )


def test_logo_is_keyboard_semantic_and_ignores_reentrant_activation():
    assert 'role="button"' in HTML[HTML.index('class="logo"'):HTML.index('</div>', HTML.index('class="logo"'))]
    assert 'tabindex="0"' in HTML[HTML.index('class="logo"'):HTML.index('</div>', HTML.index('class="logo"'))]
    assert "function handleLogoKeydown(event)" in HTML

    logo = source_between("async function refreshHomepage()", "async function scrollToTopAndCheckLatest")
    run_node(
        logo,
        """
context.logoRefreshInProgress = false;
context.cancelViewBoundRefreshWork = () => {};
context.programmaticScrollUntil = 0;
context.pendingLatestPage = null;
context.filter = 'all';
context.currentPage = 1;
context.applyNewsPage = () => {};
context.consumePendingNewArticles = () => {};
context.syncListUrl = () => {};
context.showToast = () => {};
context.stabilizePageTop = () => {};
context.latestKnownTimestamp = 10;
context.latestNewsTimestamp = () => 10;
context.loadSince = async () => {};
context.scrollPageToTop = async ({ onNearTop }) => { onNearTop(); return true; };
const resolvers = [];
context.prepareCalls = 0;
context.preparePageNavigation = () => {
  context.prepareCalls++;
  return new Promise(resolve => resolvers.push(resolve));
};
const first = context.refreshHomepage();
await Promise.resolve();
const second = context.refreshHomepage();
await Promise.resolve();
assert.equal(context.prepareCalls, 1);
resolvers.forEach(resolve => resolve({ items: [], total: 0 }));
await Promise.all([first, second]);
assert.equal(context.logoRefreshInProgress, false);
""",
    )


def test_logo_keyboard_handler_activates_only_enter_or_space():
    handler = source_between("function handleLogoKeydown(event)", "async function refreshHomepage()")
    run_node(
        handler,
        """
context.activations = 0;
context.refreshHomepage = () => context.activations++;
let prevented = 0;
const event = key => ({ key, preventDefault: () => prevented++ });
context.handleLogoKeydown(event('Escape'));
context.handleLogoKeydown(event('Enter'));
context.handleLogoKeydown(event(' '));
assert.equal(context.activations, 2);
assert.equal(prevented, 2);
""",
    )


def test_manual_refresh_cancels_pending_startup_calibration_before_updating():
    trigger = source_between("async function triggerRefresh()", "function setRefreshRunning")
    run_node(
        trigger,
        """
context.refreshInProgress = false;
context.refreshFlowGeneration = 0;
context.refreshFlowController = null;
context.authToken = 'token';
context.filter = 'all';
context.currentPage = 2;
context.pageNavigationSequence = 2;
context.pageRequestSequence = 4;
context.pageRequestPendingSequence = 0;
context.pageNavigationPending = false;
context.document = { hidden: false };
context.hideNewArticlesPrompt = () => {};
context.promptCalls = 0;
context.showNewArticlesPrompt = () => { context.promptCalls++; };
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.refreshErrorMessage = error => error.message || error.error || 'failed';
context.hasBlockingOverlayOpen = () => false;
context.consumePendingNewArticles = () => {};
context.loadNewsPage = async () => { throw new Error('page 2 must not calibrate'); };
context.runningStates = [];
context.setRefreshRunning = running => {
  context.refreshInProgress = running;
  context.runningStates.push(running);
};
const oldController = new AbortController();
let oldApplied = 0;
const oldGet = new Promise(resolve => {
  oldController.signal.addEventListener('abort', () => resolve('aborted'));
}).then(outcome => {
  if (outcome !== 'aborted') oldApplied++;
});
context.cancelCalls = 0;
context.cancelStartupEmptyRevalidation = () => {
  context.cancelCalls++;
  oldController.abort();
};
context.requestRefreshOnce = async () => ({ job_id: 'manual-job', status: 'running' });
let resolveManual;
let markManualPolling;
const manualPolling = new Promise(resolve => { markManualPolling = resolve; });
context.pollRefreshJob = () => new Promise(resolve => {
  resolveManual = resolve;
  markManualPolling();
});
const manual = context.triggerRefresh();
await manualPolling;
assert.equal(context.cancelCalls, 1);
assert.equal(oldController.signal.aborted, true);
await oldGet;
assert.equal(oldApplied, 0);
assert.equal(context.refreshInProgress, true);
assert.deepEqual(context.runningStates, [true]);
assert.deepEqual(context.toasts, ['🔄 正在后台抓取...']);
assert.equal(context.promptCalls, 0);
resolveManual({ job_id: 'manual-job', status: 'completed', new_count: 0 });
await manual;
assert.deepEqual(context.runningStates, [true, false]);
assert.equal(context.refreshInProgress, false);
assert.ok(context.toasts.includes('✅ 已是最新'));
""",
    )


def test_startup_empty_revalidation_has_wall_clock_deadline_for_hung_get():
    startup = source_between(
        "function isStartupInitializationResponse(",
        "function renderSourceDeepLinkError",
    )
    run_node(
        startup,
        """
let now = 0;
context.Date = { now: () => now };
context.coldStartInitializationActive = false;
context.coldStartInitializationTimedOut = false;
context.startupCalibrationGeneration = 0;
context.startupCalibrationController = null;
context.startupCalibrationTimer = null;
context.filter = 'all';
context.currentPage = 1;
context.pageRequestSequence = 1;
context.document = { hidden: false };
context.renderList = () => {};
context.applyNewsPage = () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.fetchNewsPage = (page, activeFilter, signal) => new Promise((resolve, reject) => {
  signal.addEventListener('abort', () => reject(
    Object.assign(new Error('timed out'), { name: 'AbortError' })
  ));
});
const timers = [];
let timerId = 0;
context.setTimeout = (callback, timeout) => {
  const timer = { id: ++timerId, timeout, callback: () => { now += timeout; return callback(); } };
  timers.push(timer);
  return timer.id;
};
context.clearTimeout = id => {
  const index = timers.findIndex(timer => timer.id === id);
  if (index >= 0) timers.splice(index, 1);
};
const initial = {
  items: [], total: 0,
  diagnostics: { refresh_job: { status: 'running', trigger: 'startup' } },
};
context.startStartupEmptyRevalidation(initial, {
  page: 1, activeFilter: 'all', requestSeq: 1,
  maxAttempts: 24, intervalMs: 5, requestTimeoutMs: 12, maxDurationMs: 10,
});
const interval = timers.shift();
assert.equal(interval.timeout, 5);
const poll = interval.callback();
await Promise.resolve();
const requestTimeout = timers.shift();
assert.equal(requestTimeout.timeout, 5);
requestTimeout.callback();
await poll;
assert.equal(now, 10);
assert.equal(context.coldStartInitializationActive, false);
assert.equal(context.coldStartInitializationTimedOut, true);
assert.equal(timers.length, 0);
""",
    )


# ─── Admin purge date picker ────────────────────────────────────────────

def _purge_picker_fns():
    return source_between("function normalizedPurgeDate(", "async function previewPurge(")


def test_open_purge_date_picker_prefills_from_text_field_and_caps_at_today():
    fns = _purge_picker_fns()
    run_node(
        fns,
        """
const elements = {
  purgeBeforeDate: { value: '2026/07/10' },
  purgeDatePicker: { value: '', max: '', style: {}, showPicker: () => { context.pickerShown = true; } },
};
context.document = { getElementById: id => elements[id] };
context.Date = Date; // use the real Date so toISOString().slice(0, 10) behaves normally
context.pickerShown = false;
context.openPurgeDatePicker();
assert.equal(elements.purgeDatePicker.value, '2026-07-10');
assert.equal(elements.purgeDatePicker.max, new Date().toISOString().slice(0, 10));
assert.equal(context.pickerShown, true);
""",
    )


def test_open_purge_date_picker_falls_back_to_focus_click_without_show_picker():
    fns = _purge_picker_fns()
    run_node(
        fns,
        """
const calls = [];
const elements = {
  purgeBeforeDate: { value: '' },
  purgeDatePicker: { value: '', max: '', style: {}, focus: () => calls.push('focus'), click: () => calls.push('click') },
};
context.document = { getElementById: id => elements[id] };
context.Date = Date;
context.openPurgeDatePicker();
assert.deepEqual(calls, ['focus', 'click']);
assert.equal(elements.purgeDatePicker.style.pointerEvents, 'auto');
""",
    )


def test_apply_purge_date_picker_fills_text_field_and_resets_confirm_state():
    fns = _purge_picker_fns()
    run_node(
        fns,
        """
const elements = {
  purgeBeforeDate: { value: '' },
  purgeDatePicker: { value: '2026-07-15', style: {} },
  purgeConfirmBtn: { disabled: false }, // simulate a stale "ready to delete" state
  purgeStatus: { textContent: '将删除 12 篇文章...' },
};
context.document = { getElementById: id => elements[id] };
context.applyPurgeDatePicker();
assert.equal(elements.purgeBeforeDate.value, '2026/07/15');
assert.equal(elements.purgeConfirmBtn.disabled, true);
assert.equal(elements.purgeStatus.textContent, '');
""",
    )


def test_apply_purge_date_picker_does_nothing_when_picker_has_no_value():
    fns = _purge_picker_fns()
    run_node(
        fns,
        """
const elements = {
  purgeBeforeDate: { value: '2026/01/01' },
  purgeDatePicker: { value: '', style: {} },
  purgeConfirmBtn: { disabled: false },
  purgeStatus: { textContent: 'old status' },
};
context.document = { getElementById: id => elements[id] };
context.applyPurgeDatePicker();
assert.equal(elements.purgeBeforeDate.value, '2026/01/01');
assert.equal(elements.purgeConfirmBtn.disabled, false);
assert.equal(elements.purgeStatus.textContent, 'old status');
""",
    )
