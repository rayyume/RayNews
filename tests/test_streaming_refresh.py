import json
import sqlite3
import time

import fetcher
import refresh_server


# ─── fetcher.py: streaming ingest + progress file ──────────────────────────

def _patch_fetcher_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(fetcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "OUTPUT_FILE", tmp_path / "news.json")
    monkeypatch.setattr(fetcher, "STATE_FILE", tmp_path / "fetcher_state.json")
    monkeypatch.setattr(fetcher, "DB_FILE", tmp_path / "news.db")
    monkeypatch.setattr(fetcher, "PROGRESS_FILE", tmp_path / "fetch_progress.json")


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


def test_run_streams_articles_into_sqlite_in_batches_before_cycle_completes(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 2)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)  # size-triggered only

    messages = [{"id": i} for i in range(1, 7)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, max((m["id"] for m in messages), default=0)))
    monkeypatch.setattr(
        fetcher, "process_message",
        lambda msg, orig_id: {
            "id": orig_id, "title": f"t{orig_id}", "source": "s",
            "feed_source": "s", "timestamp": orig_id,
        },
    )

    batch_sizes = []
    sync_sources_flags = []
    original_upsert = fetcher.upsert_articles

    def tracking_upsert(conn, entries, sync_sources=True):
        batch_sizes.append(len(entries))
        sync_sources_flags.append(sync_sources)
        return original_upsert(conn, entries, sync_sources=sync_sources)

    monkeypatch.setattr(fetcher, "upsert_articles", tracking_upsert)

    ensure_sources_calls = []
    original_ensure_sources = fetcher.ensure_article_sources

    def tracking_ensure_sources(conn):
        ensure_sources_calls.append(True)
        return original_ensure_sources(conn)

    monkeypatch.setattr(fetcher, "ensure_article_sources", tracking_ensure_sources)

    fetcher.run()

    # More than one upsert call proves articles were committed incrementally during
    # processing, not just once at the very end.
    assert len(batch_sizes) >= 2
    assert sum(batch_sizes) == 6
    # Every streamed batch skips the expensive full-table source sync — since every
    # entry made it in via streaming, run() doesn't fall back to a trailing
    # full-pass upsert_articles(..., sync_sources=True); it runs the one
    # full-table source sync directly (ensure_article_sources) instead.
    assert sync_sources_flags == [False] * len(sync_sources_flags)
    assert ensure_sources_calls == [True]

    conn = sqlite3.connect(fetcher.DB_FILE)
    count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    assert count == 6

    progress = json.loads(fetcher.PROGRESS_FILE.read_text(encoding="utf-8"))
    assert progress["inserted"] == 6
    assert progress["inserted_ids"] == [1, 2, 3, 4, 5, 6]
    assert progress["inserted"] == len(progress["inserted_ids"])
    assert progress["total_messages"] == 6


def test_news_json_mirror_is_truncated_and_unindented(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "NEWS_JSON_MIRROR_LIMIT", 3)

    messages = [{"id": i} for i in range(1, 6)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, max(m["id"] for m in messages)))
    monkeypatch.setattr(
        fetcher, "process_message",
        lambda msg, orig_id: {
            "id": orig_id, "title": f"t{orig_id}", "source": "s",
            "feed_source": "s", "timestamp": orig_id,
        },
    )

    fetcher.run()

    raw = fetcher.OUTPUT_FILE.read_text(encoding="utf-8")
    assert "\n" not in raw  # no pretty-printing indent — this file is machine-read only
    data = json.loads(raw)
    assert len(data["items"]) == 3
    # Keeps the most recent (highest timestamp) entries, not an arbitrary slice.
    assert sorted(item["id"] for item in data["items"]) == [3, 4, 5]


def test_news_json_mirror_failure_does_not_fail_the_cycle(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)

    messages = [{"id": 1}]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, 1))
    monkeypatch.setattr(
        fetcher, "process_message",
        lambda msg, orig_id: {
            "id": orig_id, "title": "t1", "source": "s",
            "feed_source": "s", "timestamp": orig_id,
        },
    )

    def boom(entries):
        raise RuntimeError("disk full")

    monkeypatch.setattr(fetcher, "write_news_json_mirror", boom)

    fetcher.run()  # must not raise despite the news.json mirror blowing up

    conn = sqlite3.connect(fetcher.DB_FILE)
    count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    assert count == 1  # SQLite already has the article regardless of the mirror


def test_upsert_articles_skips_source_sync_when_disabled(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()

    sync_calls = []
    monkeypatch.setattr(fetcher, "ensure_article_sources", lambda c: sync_calls.append(c) or 0)

    fetcher.upsert_articles(conn, [
        {"id": 1, "title": "t1", "source": "s", "feed_source": "s", "timestamp": 1},
    ], sync_sources=False)
    assert sync_calls == []

    fetcher.upsert_articles(conn, [
        {"id": 2, "title": "t2", "source": "s", "feed_source": "s", "timestamp": 2},
    ])  # default stays True
    assert sync_calls == [conn]

    count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    assert count == 2


def test_streaming_ingest_still_respects_deleted_articles(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 2)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)

    # Pre-seed the DB (and its deleted_articles table) before the cycle runs.
    conn = fetcher.init_db()
    conn.execute("INSERT INTO deleted_articles (article_id) VALUES (?)", (3,))
    conn.commit()
    conn.close()

    # Five rows with batch size two exercises both the size-triggered commits and
    # the trailing partial-batch branch.
    messages = [{"id": i} for i in range(1, 6)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, max((m["id"] for m in messages), default=0)))
    monkeypatch.setattr(
        fetcher, "process_message",
        lambda msg, orig_id: {
            "id": orig_id, "title": f"t{orig_id}", "source": "s",
            "feed_source": "s", "timestamp": orig_id,
        },
    )

    fetcher.run()

    conn = sqlite3.connect(fetcher.DB_FILE)
    ids = {row[0] for row in conn.execute("SELECT id FROM articles").fetchall()}
    conn.close()
    assert ids == {1, 2, 4, 5}

    # Running progress is a discovery contract, not an attempted-upsert log.
    # A tombstoned row must therefore never enter the browser's authoritative Set.
    progress = json.loads(fetcher.PROGRESS_FILE.read_text(encoding="utf-8"))
    assert progress["inserted"] == 4
    assert progress["inserted_ids"] == [1, 2, 4, 5]


def test_run_keeps_last_seen_id_unchanged_when_a_message_fails(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 2)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)
    fetcher.save_state({"last_seen_id": 0})

    messages = [{"id": i} for i in range(1, 4)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: (messages, max((m["id"] for m in messages), default=0)))

    def flaky_process_message(msg, orig_id):
        if orig_id == 2:
            raise ValueError("boom")
        return {
            "id": orig_id, "title": f"t{orig_id}", "source": "s",
            "feed_source": "s", "timestamp": orig_id,
        }

    monkeypatch.setattr(fetcher, "process_message", flaky_process_message)

    fetcher.run()

    state = fetcher.load_state()
    assert state["last_seen_id"] == 0

    conn = sqlite3.connect(fetcher.DB_FILE)
    ids = {row[0] for row in conn.execute("SELECT id FROM articles").fetchall()}
    conn.close()
    assert ids == {1, 3}


# ─── refresh_server.py: new_count_so_far exposure ──────────────────────────

def _reset_running_job(monkeypatch, started_at):
    monkeypatch.setattr(refresh_server, "REFRESH_JOB", {
        "job_id": "job-1", "status": "running", "trigger": "manual",
        "started_at": started_at, "finished_at": None,
        "new_count": 0, "new_ids": [], "error": "",
    })


def test_status_reports_new_count_so_far_while_running(tmp_path, monkeypatch):
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
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


def test_running_status_excludes_existing_ids_retried_after_failed_cycle(tmp_path, monkeypatch):
    """A failed cycle keeps its cursor, so its successful rows are retried next job."""
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")

    # IDs 1 and 3 were committed by the previous failed cycle; only 4 is new in
    # this job. Replaying 1 and 3 through INSERT OR REPLACE must not advertise
    # them as new running discoveries.
    snapshots = iter(({1, 3}, {1, 3, 4}))
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: next(snapshots))
    running_payload = {}

    def fake_run_fetcher():
        refresh_server.PROGRESS_FILE.write_text(
            json.dumps({
                "job_id": "job-1",
                "inserted": 3,
                "inserted_ids": [1, 3, 4],
                "total_messages": 3,
                "updated_at": started_at + 1,
            }),
            encoding="utf-8",
        )
        running_payload.update(json.loads(refresh_server.get_refresh_job_status()))
        return json.dumps({"status": "ok"}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", fake_run_fetcher)

    refresh_server._run_refresh_job("job-1")

    assert running_payload["new_count_so_far"] == 1
    assert running_payload["new_ids_so_far"] == [4]
    terminal = json.loads(refresh_server.get_refresh_job_status())
    assert terminal["new_count"] == 1
    assert terminal["new_ids"] == [4]


def test_status_ignores_progress_file_from_a_different_job_id(tmp_path, monkeypatch):
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    # A previous job's progress file, left over from a job that finished — or started —
    # within the same wall-clock second as this one. A timestamp-only comparison
    # (updated_at >= started_at) would have wrongly accepted this; only the job_id
    # mismatch should reject it.
    (tmp_path / "fetch_progress.json").write_text(
        json.dumps({"job_id": "job-0", "inserted": 99, "total_messages": 99, "updated_at": started_at}),
        encoding="utf-8",
    )

    payload = json.loads(refresh_server.get_refresh_job_status())

    assert "new_count_so_far" not in payload


def test_status_ignores_progress_file_with_no_job_id(tmp_path, monkeypatch):
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    # A progress file written by a fetcher.py invocation that never received
    # FETCH_JOB_ID (e.g. run standalone) must not be attributed to any job.
    (tmp_path / "fetch_progress.json").write_text(
        json.dumps({"job_id": "", "inserted": 5, "total_messages": 5, "updated_at": started_at}),
        encoding="utf-8",
    )

    payload = json.loads(refresh_server.get_refresh_job_status())

    assert "new_count_so_far" not in payload


def test_status_omits_new_count_so_far_when_job_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh_server, "REFRESH_JOB", {
        "job_id": "job-1", "status": "completed", "trigger": "manual",
        "started_at": int(time.time()) - 10, "finished_at": int(time.time()),
        "new_count": 3, "error": "",
    })
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    (tmp_path / "fetch_progress.json").write_text(
        json.dumps({"job_id": "job-1", "inserted": 3, "total_messages": 3, "updated_at": int(time.time())}),
        encoding="utf-8",
    )

    payload = json.loads(refresh_server.get_refresh_job_status())

    assert "new_count_so_far" not in payload


def test_status_handles_missing_progress_file(monkeypatch, tmp_path):
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")

    payload = json.loads(refresh_server.get_refresh_job_status())

    assert "new_count_so_far" not in payload
    assert payload["status"] == "running"


def test_run_fetcher_passes_current_job_id_to_fetcher_subprocess_env(monkeypatch):
    monkeypatch.setattr(refresh_server, "CURRENT_FETCH_JOB_ID", "job-xyz")
    monkeypatch.setattr(refresh_server, "acquire_lock", lambda: True)
    monkeypatch.setattr(refresh_server, "release_lock", lambda: None)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())
    monkeypatch.setattr(refresh_server, "clear_article_cache", lambda: None)

    captured_env = {}

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(*args, **kwargs):
        captured_env.update(kwargs.get("env") or {})
        return FakeResult()

    monkeypatch.setattr(refresh_server.subprocess, "run", fake_run)
    monkeypatch.setattr(refresh_server.threading, "Thread", lambda **kwargs: type(
        "T", (), {"start": lambda self: None},
    )())

    refresh_server.run_fetcher()

    assert captured_env.get("FETCH_JOB_ID") == "job-xyz"


def test_run_refresh_job_sets_current_fetch_job_id_before_running(monkeypatch):
    monkeypatch.setattr(refresh_server, "REFRESH_JOB", {
        "job_id": "job-abc", "status": "running", "trigger": "manual",
        "started_at": int(time.time()), "finished_at": None,
        "new_count": 0, "error": "",
    })
    monkeypatch.setattr(refresh_server, "CURRENT_FETCH_JOB_ID", "")
    seen_job_id = {}

    def fake_run_fetcher():
        seen_job_id["value"] = refresh_server.CURRENT_FETCH_JOB_ID
        return json.dumps({"status": "ok"}).encode(), 200

    monkeypatch.setattr(refresh_server, "run_fetcher", fake_run_fetcher)
    monkeypatch.setattr(refresh_server, "article_id_snapshot", lambda: set())

    refresh_server._run_refresh_job("job-abc")

    assert seen_job_id["value"] == "job-abc"
    assert refresh_server.CURRENT_FETCH_JOB_ID == "job-abc"
