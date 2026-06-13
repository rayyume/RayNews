import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from auth_validation import is_valid_email
from source_categories import (
    init_source_categories,
    promote_user_source_settings,
    source_rows,
)


def _source_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            feed_source TEXT NOT NULL DEFAULT '',
            timestamp INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    init_source_categories(conn)
    return conn


def test_email_validation_rejects_non_email_values():
    for value in ("", "abc", "name@", "@example.com", "a..b@example.com", "a@invalid"):
        assert not is_valid_email(value), value
    assert is_valid_email("reader+news@example.com")


def test_nginx_proxies_article_delete_routes():
    config = (ROOT / "nginx.conf").read_text(encoding="utf-8")
    assert "location /articles" in config
    section = config.split("location /articles", 1)[1].split("}", 1)[0]
    assert "proxy_pass http://127.0.0.1:8082" in section


def test_admin_source_overrides_promote_to_shared_settings():
    conn = _source_db()
    conn.executemany(
        "INSERT INTO articles (id, title, source, feed_source, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (1, "Main", "Original Feed", "Original Feed", 2),
            (2, "Alias", "Old Alias", "Old Alias", 1),
        ],
    )
    conn.execute(
        "INSERT OR IGNORE INTO source_categories "
        "(source, category, label, status, reason) "
        "VALUES ('Original Feed', 'Info', 'Original Feed', 'pending', 'test')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO source_categories "
        "(source, category, label, status, reason) "
        "VALUES ('Old Alias', 'Info', 'Old Alias', 'pending', 'test')"
    )
    conn.execute(
        "INSERT INTO user_source_categories "
        "(user_id, source, category, label, status, updated_at) "
        "VALUES (1, 'Original Feed', 'Tech', 'Shared Tech', 'manual', datetime('now'))"
    )
    conn.execute(
        "INSERT INTO user_source_aliases "
        "(user_id, alias_source, target_source, created_at) "
        "VALUES (1, 'Old Alias', 'Original Feed', datetime('now'))"
    )
    conn.commit()

    promoted = promote_user_source_settings(conn, 1)

    rows = {row["source"]: row for row in source_rows(conn)}
    assert promoted == {"categories": 1, "aliases": 1}
    assert rows["Original Feed"]["category"] == "Tech"
    assert rows["Original Feed"]["label"] == "Shared Tech"
    assert conn.execute(
        "SELECT feed_source FROM articles WHERE id = 2"
    ).fetchone()[0] == "Original Feed"
    assert conn.execute(
        "SELECT COUNT(*) FROM user_source_categories WHERE user_id = 1"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM user_source_aliases WHERE user_id = 1"
    ).fetchone()[0] == 0


def test_source_history_has_own_status_layer_and_theme_safe_article_actions():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="sourceArticlesStatus"' in html
    assert "function showSourceArticlesStatus" in html
    assert "background:#161a2a" not in html
    assert 'class="article-ai-btn ai-summarize-btn"' in html


def test_source_mutation_routes_are_admin_only_and_shared():
    server = (ROOT / "web_server.py").read_text(encoding="utf-8")
    for route in (
        '"/sources", methods=["PUT"]',
        '"/sources/classify", methods=["POST"]',
        '"/sources/classify-job", methods=["POST"]',
        '"/sources/redetect-single", methods=["POST"]',
    ):
        route_pos = server.index(route)
        decorator_block = server[route_pos:route_pos + 180]
        assert '@require_role("admin")' in decorator_block, route
    assert '"sources": source_rows(conn)' in server
    assert "update_source_category(\n            conn, source, category, label" in server
    assert "user_id=g.user_id" not in server[server.index("def save_source"):server.index("def classify_sources")]


def test_article_navigation_splits_mobile_and_desktop_history_modes():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function usesMobileArticleNavigation()" in html
    assert "(hover: none) and (pointer: coarse)" in html
    assert "(display-mode: standalone)" in html
    assert "navigator.maxTouchPoints > 0" in html
    sync_start = html.index("function syncArticleHistory(id, date)")
    sync_end = html.index("function openArticle(id)", sync_start)
    sync_block = html[sync_start:sync_end]
    assert "if (usesMobileArticleNavigation())" in sync_block
    assert "history.replaceState({ raynewsMobileArticle: true }" in sync_block
    assert "history.pushState({ raynewsArticle: true }" in html
    assert "history.replaceState({ raynewsHome: true }" in html
    assert "function closeArticle(fromHistoryNavigation = false, forceInAppNavigation = false)" in html
    close_start = html.index("function closeArticle(fromHistoryNavigation = false, forceInAppNavigation = false)")
    close_end = html.index("// Handle hash-based article links", close_start)
    close_block = html[close_start:close_end]
    assert "const mobileNavigation = forceInAppNavigation || usesMobileArticleNavigation();" in close_block
    assert "!mobileNavigation" in close_block
    assert "history.state.raynewsArticle" in close_block
    assert "history.back();" in close_block
    assert "history.replaceState({ raynewsHome: true }" in close_block
    assert "if (!overlay.classList.contains('open')) return;" in html
    assert "closeArticle(true)" in html


def test_mobile_edge_swipe_claims_navigation_at_touch_start():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    swipe_marker = html.index("let sx = 0, sy = 0, swiping = false")
    swipe_start = html.rindex("(function() {", 0, swipe_marker)
    swipe_end = html.index("</script>", swipe_marker)
    swipe_block = html[swipe_start:swipe_end]

    assert "if (!usesMobileArticleNavigation()) return;" in swipe_block
    assert "edgeCandidate = sx < 50;" in swipe_block
    assert "if (edgeCandidate) e.preventDefault();" in swipe_block
    assert "overlay.addEventListener('touchcancel'" in swipe_block
    assert "closeArticle();" in swipe_block


def test_mobile_back_button_is_excluded_from_edge_swipe_and_handles_touch_directly():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    swipe_marker = html.index("let sx = 0, sy = 0, swiping = false")
    swipe_start = html.rindex("(function() {", 0, swipe_marker)
    swipe_end = html.index("</script>", swipe_marker)
    swipe_block = html[swipe_start:swipe_end]
    assert "if (e.target.closest('#backBtn')) return;" in swipe_block
    assert "backBtn.addEventListener('touchstart'" in html
    assert "backBtn.addEventListener('touchend'" in html
    assert "backBtn.addEventListener('touchcancel'" in html
    assert "articleBackTouchStart = null;" in html
    assert "e.stopPropagation();" in html


def test_article_back_and_refresh_do_not_replace_the_whole_list():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    close_start = html.index("function closeArticle(fromHistoryNavigation = false, forceInAppNavigation = false)")
    close_end = html.index("// Handle hash-based article links", close_start)
    close_block = html[close_start:close_end]
    refresh_start = html.index("async function triggerRefresh(")
    refresh_end = html.index("function setRefreshRunning", refresh_start)
    refresh_block = html[refresh_start:refresh_end]

    assert "function reconcileVisibleArticles()" in html
    assert "function flushPendingListUpdate()" in html
    assert "flushPendingListUpdate();" in close_block
    assert "renderList();" not in close_block
    assert "const refreshCursor = latestKnownTimestamp || latestNewsTimestamp();" in refresh_block
    assert "await loadSince(refreshCursor, { forceApply: true });" in refresh_block
    assert "const listResp = await fetch('/api/news?size='" not in refresh_block


def test_blocking_list_loading_is_only_used_when_no_articles_are_available():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    load_start = html.index("async function loadNewsPage(")
    load_end = html.index("function renderFilters()", load_start)
    load_block = html[load_start:load_end]

    assert "if (!cacheApplied && !news.length) renderColdStartSkeleton();" in load_block
    assert "if (!cacheApplied && !news.length) renderColdStartError(message);" in load_block
    assert "list.innerHTML =" not in load_block


def test_news_list_uses_paged_cache_first_loading_without_full_history_requests():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "const INITIAL_LOAD_SIZE = 99999" not in html
    assert "size=99999" not in html
    assert "const PAGE_SIZE = 30;" in html
    assert "function openNewsCache()" in html
    assert "async function readCachedNewsPage" in html
    assert "async function writeCachedNewsPage" in html
    assert "async function loadNewsPage(" in html
    assert "await readCachedNewsPage" in html
    assert "pageRequestController.abort()" in html
    assert "if (data.error) throw new Error(data.error);" in html
    load_start = html.index("async function loadNewsPage(")
    load_end = html.index("function renderFilters()", load_start)
    assert "list.innerHTML =" not in html[load_start:load_end]


def test_idle_refresh_only_returns_to_latest_after_five_minutes_and_new_articles():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "const IDLE_LATEST_DELAY_MS = 5 * 60 * 1000;" in html
    assert "function markUserActivity()" in html
    assert "function hasBlockingOverlayOpen()" in html
    assert "async function showLatestAfterIdle()" in html
    idle_start = html.index("async function showLatestAfterIdle()")
    idle_end = html.index("function scheduleAdjacentPagePrefetch", idle_start)
    idle_block = html[idle_start:idle_end]
    assert "pendingNewArticleCount" in idle_block
    assert "hasBlockingOverlayOpen()" in idle_block
    assert "window.scrollTo({ top: 0, behavior: 'smooth' });" in idle_block
    assert "currentPage = 1;" in idle_block
    assert "filter = 'all';" in idle_block
    assert "document.addEventListener('visibilitychange'" in html
    assert "loadDataWithRetry();" not in html


def test_service_worker_normalizes_api_cache_keys():
    sw = (ROOT / "frontend" / "sw.js").read_text(encoding="utf-8")
    assert "function normalizedApiRequest(request)" in sw
    assert "url.searchParams.delete('t');" in sw
    assert "cache.match(cacheRequest)" in sw
    assert "cache.put(cacheRequest, network.clone())" in sw


def test_search_uses_server_side_pagination_without_limiting_database_scope():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "let searchPage = 1;" in html
    assert "let searchTotal = 0;" in html
    assert "async function fetchServerSearchResults(query, page = 1" in html
    assert "async function loadMoreSearchResults()" in html
    assert "params.set('q', query);" in html
    assert "params.set('page', String(page));" in html
    assert "params.set('size', String(PAGE_SIZE));" in html
    assert "加载更多" in html


def test_paged_list_mutations_keep_server_total_and_new_article_prompt():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    delete_start = html.index("async function deleteArticlesByIds(ids)")
    delete_end = html.index("async function deleteSourceArticle", delete_start)
    delete_block = html[delete_start:delete_end]
    since_start = html.index("async function loadSince(")
    since_end = html.index("function renderFilters()", since_start)
    since_block = html[since_start:since_end]
    assert "currentTotal = Math.max(0, currentTotal - deleted);" in delete_block
    assert "document.getElementById('count').textContent = currentTotal + ' 条新闻';" in delete_block
    assert "有 ' + pendingNewArticleCount + ' 篇新文章" in since_block


def test_filter_switch_rolls_back_when_target_page_cannot_load():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function selectFilter(value)")
    end = html.index("function filteredNews()", start)
    block = html[start:end]
    assert "const previousFilter = filter;" in block
    assert "const previousPage = currentPage;" in block
    assert "const loaded = await loadNewsPage" in block
    assert "filter = previousFilter;" in block
    assert "currentPage = previousPage;" in block


def test_cached_page_background_calibration_reuses_existing_cards():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    apply_start = html.index("function applyNewsPage(")
    apply_end = html.index("async function fetchNewsPage", apply_start)
    apply_block = html[apply_start:apply_end]
    load_start = html.index("async function loadNewsPage(")
    load_end = html.index("// Full snapshot compatibility wrapper", load_start)
    load_block = html[load_start:load_end]
    assert "preserveDom = false" in apply_block
    assert "if (preserveDom" in apply_block
    assert "reconcileVisibleArticles();" in apply_block
    assert "preserveDom: cacheApplied" in load_block
    assert "const showPageProgress = !cacheApplied && userInitiated;" in load_block


def test_all_home_refresh_controls_use_one_shared_first_page_flow():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'class="logo" onclick="refreshHomepage()"' in html
    assert 'id="refreshBtn" onclick="refreshHomepage()"' in html
    assert 'id="scrollTopBtn" onclick="refreshHomepage()"' in html
    start = html.index("async function refreshHomepage()")
    end = html.index("function articleDetailErrorText", start)
    block = html[start:end]
    assert "await scrollPageToTop();" in block
    assert "await showHomepageAfterScroll();" in block
    assert "triggerRefresh();" in block
    assert "async function showHomepageAfterScroll()" in html
    assert "function waitForScrollTop(" in html


def test_pagination_scrolls_immediately_and_switches_only_after_reaching_top():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function goToPage(page)")
    end = html.index("function waitForScrollTop(", start)
    block = html[start:end]
    assert "const pagePromise = preparePageNavigation(page, filter);" in block
    assert "await scrollPageToTop();" in block
    assert "const data = await pagePromise;" in block
    assert block.index("await scrollPageToTop()") < block.index("applyNewsPage(data")
    assert block.index("applyNewsPage(data") < block.index("stabilizePageTop();")
    assert "async function preparePageNavigation(" in html


def test_mobile_pull_to_refresh_is_not_registered():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="pullIndicator"' not in html
    assert "Mobile: pull-to-refresh on homepage" not in html
    assert "function resetPullIndicator()" not in html


def test_mobile_cold_start_resets_scroll_before_and_after_bootstrap():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function resetMobileColdStartScroll()" in html
    start = html.index("async function bootstrapNews()")
    end = html.index("// Initial load", start)
    block = html[start:end]
    assert "resetMobileColdStartScroll();" in block
    initial_start = html.index("// Initial load")
    initial_end = html.index("// Restore sidebar preference", initial_start)
    initial_block = html[initial_start:initial_end]
    assert initial_block.index("resetMobileColdStartScroll();") < initial_block.index("bootstrapNews();")
    assert "history.scrollRestoration = 'manual';" in html
    assert "pageshow" not in initial_block


def test_desktop_scroll_to_top_uses_duration_controlled_animation():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("function scrollPageToTop(")
    end = html.index("async function showHomepageAfterScroll()", start)
    block = html[start:end]
    assert "requestAnimationFrame(step)" in block
    assert "Math.min(700, Math.max(480" in block
    assert "usesMobileArticleNavigation()" in block
    assert "prefers-reduced-motion" in block


def test_article_history_keeps_only_one_desktop_article_entry():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("function syncArticleHistory(id, date)")
    end = html.index("function openArticle(id)", start)
    block = html[start:end]
    assert "history.state && history.state.raynewsArticle" in block
    assert "history.replaceState({ raynewsArticle: true }, '', articleHash);" in block
    assert "history.pushState({ raynewsArticle: true }, '', articleHash);" in block


def test_article_back_button_does_not_pass_click_event_as_history_state():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "function closeArticleFromButton()" in html
    assert "closeArticle(false, navigator.maxTouchPoints > 0);" in html
    assert "addEventListener('click', closeArticleFromButton)" in html
    assert "addEventListener('click', closeArticle);" not in html


def test_legacy_admin_source_promotion_is_guarded_after_success():
    server = (ROOT / "web_server.py").read_text(encoding="utf-8")
    assert "_legacy_admin_source_settings_promoted = False" in server
    assert "global _legacy_admin_source_settings_promoted" in server
    assert "if _legacy_admin_source_settings_promoted:" in server


def test_sources_settings_tab_is_admin_only():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="sourcesSettingsTab"' in html
    assert "sourcesSettingsTab.style.display = authUser.role === 'admin' ? '' : 'none';" in html
    assert "if (tab === 'sources' && (!authUser || authUser.role !== 'admin')) tab = 'account';" in html
    assert "if (authUser && authUser.role === 'admin') loadSourcesTab();" in html


def test_header_avatar_search_and_summary_controls_are_aligned():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert ".header-right{display:flex;align-items:center;gap:10px;" in html
    assert ".daily-summary-btn{position:relative;width:32px;height:32px;" in html
    assert 'class="icon-btn daily-summary-btn"' in html
    assert ".header-right .header-avatar .user-avatar{width:30px!important;height:30px!important}" in html
