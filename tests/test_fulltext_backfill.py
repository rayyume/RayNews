import time

import fetcher


def _patch_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(fetcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "DB_FILE", tmp_path / "news.db")


def _insert(conn, **kw):
    cols = {
        "id": kw["id"],
        "title": kw.get("title", "t"),
        "timestamp": kw["timestamp"],
        "has_full_content": kw.get("has_full_content", 0),
        "telegraph_url": kw.get("telegraph_url", ""),
        "body_html": kw.get("body_html", "excerpt"),
        "origin_source": kw.get("origin_source", "未分类"),
        "thumb": kw.get("thumb", ""),
        "summary": kw.get("summary", "short"),
    }
    conn.execute(
        "INSERT INTO articles (id, title, timestamp, has_full_content, telegraph_url, "
        "body_html, origin_source, thumb, summary) VALUES "
        "(:id, :title, :timestamp, :has_full_content, :telegraph_url, :body_html, "
        ":origin_source, :thumb, :summary)",
        cols,
    )
    conn.commit()


def test_backfill_upgrades_recent_downgraded_telegraph_article(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    now = int(time.time())
    _insert(conn, id=1, timestamp=now, telegraph_url="https://telegra.ph/x",
            has_full_content=0, body_html="short excerpt", summary="short",
            origin_source="未分类", thumb="")

    monkeypatch.setattr(fetcher, "fetch_telegraph", lambda url: {
        "body_html": "<article>full body</article>",
        "images": ["https://img/first.jpg"],
        "char_count": 9,
        "detected_source": "华尔街见闻",
    })

    upgraded = fetcher.backfill_missing_fulltext(conn)
    assert upgraded == 1

    row = conn.execute(
        "SELECT has_full_content, body_html, thumb, origin_source, summary "
        "FROM articles WHERE id = 1"
    ).fetchone()
    assert row[0] == 1
    assert row[1] == "<article>full body</article>"
    assert row[2] == "https://img/first.jpg"   # adopted first image (had no thumb)
    assert row[3] == "华尔街见闻"                # Telegraph-detected source applied
    assert row[4] == ""                         # excerpt summary cleared
    conn.close()


def test_backfill_skips_articles_beyond_the_recency_window(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    old = int(time.time()) - (fetcher.BACKFILL_MAX_AGE_DAYS + 1) * 86400
    _insert(conn, id=2, timestamp=old, telegraph_url="https://telegra.ph/old",
            has_full_content=0)

    called = []
    monkeypatch.setattr(fetcher, "fetch_telegraph",
                        lambda url: called.append(url) or None)

    assert fetcher.backfill_missing_fulltext(conn) == 0
    assert called == []   # a genuinely dead/old URL ages out — never retried
    conn.close()


def test_backfill_ignores_articles_without_telegraph_url_or_already_full(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    now = int(time.time())
    _insert(conn, id=3, timestamp=now, telegraph_url="", has_full_content=0)   # no url
    _insert(conn, id=4, timestamp=now, telegraph_url="https://telegra.ph/y",
            has_full_content=1)                                                # already full

    monkeypatch.setattr(fetcher, "fetch_telegraph",
                        lambda url: {"body_html": "x", "images": [], "char_count": 1})

    assert fetcher.backfill_missing_fulltext(conn) == 0
    conn.close()


def test_backfill_leaves_row_downgraded_when_fetch_still_fails(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    now = int(time.time())
    _insert(conn, id=5, timestamp=now, telegraph_url="https://telegra.ph/z",
            has_full_content=0, body_html="excerpt")

    monkeypatch.setattr(fetcher, "fetch_telegraph", lambda url: None)  # still failing

    assert fetcher.backfill_missing_fulltext(conn) == 0
    row = conn.execute(
        "SELECT has_full_content, body_html FROM articles WHERE id = 5"
    ).fetchone()
    assert row[0] == 0
    assert row[1] == "excerpt"   # untouched, will be retried again next cycle
    conn.close()


def test_run_still_backfills_when_there_are_no_new_messages(tmp_path, monkeypatch):
    # A no-new-messages cycle is exactly when a previously failed Telegraph fetch would
    # otherwise never get retried — backfill must still run so it doesn't age out of the
    # window and stay permanently downgraded.
    monkeypatch.setattr(fetcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "OUTPUT_FILE", tmp_path / "news.json")
    monkeypatch.setattr(fetcher, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(fetcher, "DB_FILE", tmp_path / "news.db")
    monkeypatch.setattr(fetcher, "PROGRESS_FILE", tmp_path / "progress.json")
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: ([], 0))

    calls = []
    monkeypatch.setattr(fetcher, "backfill_missing_fulltext", lambda conn: calls.append(True))

    fetcher.run()

    assert calls == [True]   # backfill ran on the empty cycle


def test_wechat_fetch_uses_the_longer_dedicated_timeout(monkeypatch):
    # WeChat has no backfill safety net, so its full-text fetch must keep the longer
    # timeout rather than the short Telegraph one.
    assert fetcher.WECHAT_FULLTEXT_TIMEOUT > fetcher.FULLTEXT_TIMEOUT

    seen = {}
    monkeypatch.setattr(fetcher, "safe_get",
                        lambda url, **kw: seen.update(timeout=kw.get("timeout")) or _raise())
    fetcher.fetch_wechat_article("https://mp.weixin.qq.com/s/abc")
    assert seen["timeout"] == fetcher.WECHAT_FULLTEXT_TIMEOUT


def _raise():
    raise RuntimeError("stop after capturing timeout")
