import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


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
context.delay = async () => {};
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
  assert.equal(url, '/auth/refresh/status');
  fetchOptions = options;
  return {};
};
await context.requestRefreshStatus(37);
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
context.delay = async timeout => {
  assert.equal(timeout, 1000);
  now = 600;
};
context.requestRefreshStatus = async timeout => {
  requestedTimeout = timeout;
  return { job_id: 'job-1', status: 'completed', new_count: 2 };
};
const status = await context.pollRefreshJob('job-1', 1000);
assert.equal(status.new_count, 2);
assert.equal(requestedTimeout, 400);
""",
    )


def trigger_context_setup(poll_body):
    return f"""
context.refreshInProgress = false;
context.authToken = 'token';
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 3;
context.pageRequestSequence = 8;
context.pageRequestPendingSequence = 0;
context.pageNavigationPending = false;
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


def test_load_news_page_checks_application_guard_before_mutating_visible_list():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
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


def test_load_news_page_checks_application_guard_before_applying_cached_list():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
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
