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


def test_article_navigation_uses_browser_history_for_back():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "history.pushState({ raynewsArticle: true }" in html
    assert "history.replaceState({ raynewsHome: true }" in html
    assert "function closeArticle(fromHistoryNavigation = false)" in html
    assert "history.state.raynewsArticle" in html
    assert "if (!overlay.classList.contains('open')) return;" in html
    assert "closeArticle(true)" in html


def test_legacy_admin_source_promotion_is_guarded_after_success():
    server = (ROOT / "web_server.py").read_text(encoding="utf-8")
    assert "_legacy_admin_source_settings_promoted = False" in server
    assert "global _legacy_admin_source_settings_promoted" in server
    assert "if _legacy_admin_source_settings_promoted:" in server
