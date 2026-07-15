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
context.pageRequestSequence = 0;
context.listStateFromUrl = () => ({ activeFilter: 'all', page: 1 });
context.readNewsCacheEntry = async () => null;
context.rebuildCategoryMap = () => {};
context.renderFilters = () => {};
context.isRestrictedUser = () => false;
context.apiFetch = () => new Promise(() => {});
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


def test_cold_start_network_retry_uses_a_fresh_abort_controller():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
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
