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

    fetcher.write_fetch_progress(3, 10)

    payload = json.loads(fetcher.PROGRESS_FILE.read_text(encoding="utf-8"))
    assert payload["inserted"] == 3
    assert payload["total_messages"] == 10
    assert payload["updated_at"] > 0
    assert not fetcher.PROGRESS_FILE.with_suffix(".json.tmp").exists()


def test_run_streams_articles_into_sqlite_in_batches_before_cycle_completes(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 2)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)  # size-triggered only

    messages = [{"id": i} for i in range(1, 7)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: messages)
    monkeypatch.setattr(
        fetcher, "process_message",
        lambda msg, orig_id: {
            "id": orig_id, "title": f"t{orig_id}", "source": "s",
            "feed_source": "s", "timestamp": orig_id,
        },
    )

    batch_sizes = []
    original_upsert = fetcher.upsert_articles

    def tracking_upsert(conn, entries):
        batch_sizes.append(len(entries))
        return original_upsert(conn, entries)

    monkeypatch.setattr(fetcher, "upsert_articles", tracking_upsert)

    fetcher.run()

    # More than one upsert call proves articles were committed incrementally during
    # processing, not just once at the very end (the trailing entry is the final
    # self-healing full-pass upsert that already existed before this change).
    assert len(batch_sizes) >= 2
    assert sum(batch_sizes[:-1]) == 6
    assert batch_sizes[-1] == 6

    conn = sqlite3.connect(fetcher.DB_FILE)
    count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    conn.close()
    assert count == 6

    progress = json.loads(fetcher.PROGRESS_FILE.read_text(encoding="utf-8"))
    assert progress["inserted"] == 6
    assert progress["total_messages"] == 6


def test_streaming_ingest_still_respects_deleted_articles(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 2)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)

    # Pre-seed the DB (and its deleted_articles table) before the cycle runs.
    conn = fetcher.init_db()
    conn.execute("INSERT INTO deleted_articles (article_id) VALUES (?)", (3,))
    conn.commit()
    conn.close()

    messages = [{"id": i} for i in range(1, 5)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: messages)
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
    assert ids == {1, 2, 4}


def test_run_keeps_last_seen_id_unchanged_when_a_message_fails(tmp_path, monkeypatch):
    _patch_fetcher_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SIZE", 2)
    monkeypatch.setattr(fetcher, "STREAM_BATCH_SECONDS", 100.0)
    fetcher.save_state({"last_seen_id": 0})

    messages = [{"id": i} for i in range(1, 4)]
    monkeypatch.setattr(fetcher, "fetch_all_new_messages", lambda state: messages)

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
        "new_count": 0, "error": "",
    })


def test_status_reports_new_count_so_far_while_running(tmp_path, monkeypatch):
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    (tmp_path / "fetch_progress.json").write_text(
        json.dumps({"inserted": 7, "total_messages": 12, "updated_at": started_at + 1}),
        encoding="utf-8",
    )

    payload = json.loads(refresh_server.get_refresh_job_status())

    assert payload["new_count_so_far"] == 7


def test_status_ignores_progress_file_older_than_current_job(tmp_path, monkeypatch):
    started_at = int(time.time())
    _reset_running_job(monkeypatch, started_at)
    monkeypatch.setattr(refresh_server, "PROGRESS_FILE", tmp_path / "fetch_progress.json")
    # Stale progress left over from a previous cycle/crash, predating this job.
    (tmp_path / "fetch_progress.json").write_text(
        json.dumps({"inserted": 99, "total_messages": 99, "updated_at": started_at - 100}),
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
        json.dumps({"inserted": 3, "total_messages": 3, "updated_at": int(time.time())}),
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
