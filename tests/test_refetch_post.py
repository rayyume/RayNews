"""Behavioral contracts for fetcher.refetch_single_post() and its wiring into
refresh_server's /internal/refetch-post, used when a Telegram edit event arrives
(see docs/plans/telegram-serverless-webhook-plan.md, Phase 2)."""

import json
import sqlite3

import fetcher
import refresh_server


def _patch_fetcher_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(fetcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "OUTPUT_FILE", tmp_path / "news.json")
    monkeypatch.setattr(fetcher, "STATE_FILE", tmp_path / "fetcher_state.json")
    monkeypatch.setattr(fetcher, "DB_FILE", tmp_path / "news.db")
    monkeypatch.setattr(fetcher, "PROGRESS_FILE", tmp_path / "fetch_progress.json")


def _seed_article(db_file, article_id, title="old title"):
    conn = fetcher.init_db()
    fetcher.upsert_articles(conn, [{
        "id": article_id, "title": title, "source": "s", "feed_source": "s",
        "timestamp": article_id,
    }])
    conn.close()


def test_refetch_skips_message_not_in_articles_table(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)

    called = []
    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: called.append(1))

    result = fetcher.refetch_single_post(123)

    assert result is False
    assert called == []


def test_refetch_updates_existing_article(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    _seed_article(fetcher.DB_FILE, 42, title="old title")

    class FakeResp:
        text = "<html>fake</html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(
        fetcher, "parse_messages",
        lambda html: [{"id": 42, "html": "", "text": "new text", "images": [],
                        "videos": [], "datetime": "", "link_preview_title": "",
                        "link_preview_url": ""}],
    )
    monkeypatch.setattr(
        fetcher, "process_message",
        lambda msg, orig_id: {
            "id": orig_id, "title": "new title", "source": "s", "feed_source": "s",
            "timestamp": orig_id,
        },
    )

    result = fetcher.refetch_single_post(42)

    assert result is True
    conn = sqlite3.connect(fetcher.DB_FILE)
    row = conn.execute("SELECT title FROM articles WHERE id = 42").fetchone()
    conn.close()
    assert row[0] == "new title"


def test_refetch_does_not_revive_tombstoned_article(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    _seed_article(fetcher.DB_FILE, 7)

    conn = fetcher.init_db()
    conn.execute("INSERT INTO deleted_articles (article_id) VALUES (7)")
    conn.execute("DELETE FROM articles WHERE id = 7")
    conn.commit()
    conn.close()

    called = []
    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: called.append(1))

    result = fetcher.refetch_single_post(7)

    assert result is False
    assert called == []


def test_refetch_missing_from_embed_response_returns_false(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    _seed_article(fetcher.DB_FILE, 9)

    class FakeResp:
        text = "<html></html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(fetcher, "parse_messages", lambda html: [])

    result = fetcher.refetch_single_post(9)

    assert result is False


def test_main_refetch_post_arg_invokes_refetch_not_full_run(monkeypatch):
    calls = []
    monkeypatch.setattr(fetcher, "refetch_single_post", lambda mid: calls.append(mid) or True)
    monkeypatch.setattr(fetcher, "run", lambda: calls.append("run"))

    exit_code = fetcher.main(["--refetch-post", "123"])

    assert calls == [123]
    assert exit_code == 0


def test_main_without_refetch_post_arg_runs_normal_cycle(monkeypatch):
    calls = []
    monkeypatch.setattr(fetcher, "refetch_single_post", lambda mid: calls.append(mid) or True)
    monkeypatch.setattr(fetcher, "run", lambda: calls.append("run"))

    exit_code = fetcher.main([])

    assert calls == ["run"]
    assert exit_code == 0


def test_main_refetch_post_failure_returns_nonzero_exit(monkeypatch):
    monkeypatch.setattr(fetcher, "refetch_single_post", lambda mid: False)

    exit_code = fetcher.main(["--refetch-post", "123"])

    assert exit_code == 1


# ─── refresh_server.py: /internal/refetch-post wiring ──────────────────────


def test_run_refetch_post_returns_409_when_locked(monkeypatch):
    monkeypatch.setattr(refresh_server, "acquire_lock", lambda: False)

    body, status = refresh_server.run_refetch_post(42)

    assert status == 409
    assert json.loads(body)["status"] == "skipped"


def test_run_refetch_post_runs_subprocess_and_clears_cache(monkeypatch):
    monkeypatch.setattr(refresh_server, "acquire_lock", lambda: True)
    released = []
    monkeypatch.setattr(refresh_server, "release_lock", lambda: released.append(1))
    cleared = []
    monkeypatch.setattr(refresh_server, "clear_article_cache", lambda: cleared.append(1))

    class FakeResult:
        returncode = 0
        stdout = "ok"
        stderr = ""

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeResult()

    monkeypatch.setattr(refresh_server.subprocess, "run", fake_run)

    body, status = refresh_server.run_refetch_post(55)

    assert status == 200
    assert json.loads(body)["status"] == "ok"
    assert calls == [["python3", "/app/fetcher.py", "--refetch-post", "55"]]
    assert cleared == [1]
    assert released == [1]


def test_refetch_post_route_requires_valid_id(monkeypatch):
    calls = []
    monkeypatch.setattr(
        refresh_server, "send_text",
        lambda handler, body, status=200: calls.append((body, status)),
    )
    handler = refresh_server.Handler.__new__(refresh_server.Handler)
    handler.path = "/internal/refetch-post"

    refresh_server.Handler.do_POST(handler)

    assert calls[0][1] == 400


def test_refetch_post_route_dispatches_to_run_refetch_post(monkeypatch):
    calls = []
    monkeypatch.setattr(
        refresh_server, "run_refetch_post",
        lambda mid: (calls.append(mid) or json.dumps({"status": "ok"}).encode(), 200),
    )
    monkeypatch.setattr(
        refresh_server, "send_json",
        lambda handler, body, status=200: calls.append((body, status)),
    )
    handler = refresh_server.Handler.__new__(refresh_server.Handler)
    handler.path = "/internal/refetch-post?id=321"

    refresh_server.Handler.do_POST(handler)

    assert calls[0] == 321
