#!/usr/bin/env python3
"""Tiny HTTP server: runs fetcher.py on GET /refresh, periodic auto-refresh, and serves SQLite-backed API."""
import http.server
import subprocess
import json
import sys
import logging
import threading
import urllib.parse
import os
import sqlite3
import re
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from image_cache import (
    cache_image,
    enqueue_article_image_prefetch,
    fetch_remote_image,
    get_cached_image,
)
from news_schema import ensure_deleted_articles_table
from source_categories import (
    CATEGORY_NAMES, CATEGORY_ORDER, ensure_article_source_columns,
    maintain_source_categories, source_rows,
)

REFRESH_INTERVAL = 900  # 15 minutes
LOCK_FILE = "/tmp/raynews-fetcher.lock"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_FILE = DATA_DIR / "news.db"
NEWS_JSON_FILE = DATA_DIR / "news.json"
STATE_FILE = DATA_DIR / "fetcher_state.json"
PROGRESS_FILE = DATA_DIR / "fetch_progress.json"
LAST_FETCH_STATUS = {
    "status": "never",
    "returncode": None,
    "stdout": "",
    "stderr": "",
    "updated_at": None,
}
REFRESH_JOB_LOCK = threading.Lock()
REFRESH_JOB = {
    "job_id": "",
    "status": "idle",
    "trigger": "",
    "started_at": None,
    "finished_at": None,
    "new_count": 0,
    "error": "",
}
REFRESH_JOB_HISTORY_LIMIT = 16
REFRESH_JOB_HISTORY = OrderedDict()
# Set by _run_refresh_job() right before calling run_fetcher(), so run_fetcher() can
# pass it to the fetcher.py subprocess as FETCH_JOB_ID — letting the progress file it
# writes be matched to a job by exact ID instead of a second-granularity timestamp
# heuristic. Safe as a bare global: REFRESH_JOB_LOCK already ensures only one fetch job
# runs (and thus one subprocess is spawned) at a time.
CURRENT_FETCH_JOB_ID = ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s [refresh] %(message)s")
log = logging.getLogger("refresh")
_schema_lock = threading.Lock()
_schema_ready = False
_schema_ready_event = threading.Event()


def ensure_schema_once(conn: sqlite3.Connection) -> None:
    global _schema_ready
    if _schema_ready_event.is_set() and _schema_ready:
        return
    with _schema_lock:
        if _schema_ready_event.is_set() and _schema_ready:
            return
        ensure_article_source_columns(conn)
        ensure_article_title_columns(conn)
        ensure_deleted_articles_table(conn)
        conn.commit()
        globals()["_schema_ready"] = True
        _schema_ready_event.set()


def ensure_article_title_columns(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "original_title" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN original_title TEXT")
    if "title_updated_at" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN title_updated_at TEXT")
    if "title_source" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN title_source TEXT")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    ensure_schema_once(conn)
    return conn


# In-memory cache for article detail responses — invalidated on fetcher run
_article_cache: dict[int, bytes] = {}
_article_cache_lock = threading.Lock()
_article_cache_inflight: dict[int, threading.Event] = {}


def clear_article_cache():
    with _article_cache_lock:
        _article_cache.clear()
        _article_cache_inflight.clear()


def acquire_lock() -> bool:
    """Try to acquire a lock file atomically. Returns True if acquired."""
    try:
        os.makedirs(LOCK_FILE, exist_ok=False)
        return True
    except FileExistsError:
        return False


def release_lock():
    """Remove the lock file."""
    try:
        os.rmdir(LOCK_FILE)
    except OSError:
        pass


def run_fetcher():
    """Run fetcher.py and return the result dict + HTTP status code."""
    if not acquire_lock():
        log.warning("Fetcher already running — skipping")
        body = json.dumps({"status": "skipped", "error": "fetcher already running"}).encode()
        return body, 429
    existing_article_ids = article_id_snapshot()
    try:
        log.info("Triggering fetcher...")
        env = {**os.environ, "FETCH_JOB_ID": CURRENT_FETCH_JOB_ID}
        result = subprocess.run(
            ["python3", "/app/fetcher.py"],
            capture_output=True, text=True, timeout=120, env=env,
        )
        is_ok = result.returncode == 0
        body = json.dumps({
            "status": "ok" if is_ok else "error",
            "returncode": result.returncode,
            "stdout": result.stdout[-300:],
            "stderr": result.stderr[-300:],
        }).encode()
        LAST_FETCH_STATUS.update({
            "status": "ok" if is_ok else "error",
            "returncode": result.returncode,
            "stdout": result.stdout[-300:],
            "stderr": result.stderr[-300:],
            "updated_at": int(time.time()),
        })
        log.info(f"Fetcher done (exit={result.returncode})")
        if is_ok:
            clear_article_cache()
            try:
                conn = sqlite3.connect(str(DB_FILE))
                conn.row_factory = sqlite3.Row
                ensure_article_source_columns(conn)
                # force=False: let maintain_source_categories()'s own
                # MAINTENANCE_THROTTLE_SECONDS throttle skip the two full-table
                # scans (source discovery, stale cleanup) when the last run was
                # recent — e.g. back-to-back manual refreshes. New/stale sources
                # are still caught within that window by whichever refresh (this
                # one, the next manual click, or the 15-minute periodic cycle)
                # lands after the throttle expires.
                result = maintain_source_categories(conn, force=False)
                conn.commit()
                conn.close()
                if result.get("discovered") or result.get("deleted"):
                    log.info(
                        f"Source maintenance: discovered {result.get('discovered', 0)}, "
                        f"cleaned up {result.get('deleted', 0)} stale source(s)"
                    )
            except Exception as e:
                log.warning(f"Source cleanup failed: {e}")
            threading.Thread(
                target=enqueue_new_article_images,
                args=(existing_article_ids,),
                daemon=True,
            ).start()
        return body, 200 if is_ok else 500
    except subprocess.TimeoutExpired:
        LAST_FETCH_STATUS.update({
            "status": "error",
            "returncode": None,
            "stdout": "",
            "stderr": "timeout",
            "updated_at": int(time.time()),
        })
        body = json.dumps({"status": "error", "error": "timeout"}).encode()
        return body, 500
    except Exception as e:
        LAST_FETCH_STATUS.update({
            "status": "error",
            "returncode": None,
            "stdout": "",
            "stderr": str(e)[-300:],
            "updated_at": int(time.time()),
        })
        body = json.dumps({"status": "error", "error": str(e)}).encode()
        return body, 500
    finally:
        release_lock()


def article_id_snapshot() -> set[int]:
    """Return current article IDs so refresh can queue only newly inserted images."""
    conn = None
    try:
        if not DB_FILE.exists():
            return set()
        conn = sqlite3.connect(str(DB_FILE), timeout=30)
        rows = conn.execute("SELECT id FROM articles").fetchall()
        return {int(row[0]) for row in rows}
    except Exception as exc:
        log.warning(f"Article snapshot failed: {exc}")
        return set()
    finally:
        if conn:
            conn.close()


def _read_fetch_progress() -> dict | None:
    """Read the streaming-ingest progress file fetcher.py writes during a fetch cycle.
    Returns None if missing/unreadable — the caller must still work without it."""
    try:
        if not PROGRESS_FILE.exists():
            return None
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _refresh_job_json_locked() -> bytes:
    payload = dict(REFRESH_JOB)
    if payload.get("status") == "running":
        progress = _read_fetch_progress()
        # Match by exact job_id (fetcher.py stamps it from FETCH_JOB_ID) rather than a
        # second-granularity timestamp comparison — two jobs finishing/starting within
        # the same wall-clock second could otherwise let a previous cycle's progress
        # file be mistaken for this job's progress.
        if progress and progress.get("job_id") and progress.get("job_id") == payload.get("job_id"):
            payload["new_count_so_far"] = progress.get("inserted", 0)
    return json.dumps(payload).encode()


def get_refresh_job_status() -> bytes:
    with REFRESH_JOB_LOCK:
        return _refresh_job_json_locked()


def _remember_terminal_job_locked() -> None:
    job_id = REFRESH_JOB.get("job_id") or ""
    if not job_id or REFRESH_JOB.get("status") not in ("completed", "failed"):
        return
    REFRESH_JOB_HISTORY[job_id] = dict(REFRESH_JOB)
    REFRESH_JOB_HISTORY.move_to_end(job_id)
    while len(REFRESH_JOB_HISTORY) > REFRESH_JOB_HISTORY_LIMIT:
        REFRESH_JOB_HISTORY.popitem(last=False)


def get_refresh_job_status_response(job_id: str | None = None) -> tuple[bytes, int]:
    """Return the current job, or a bounded terminal snapshot by exact ID."""
    with REFRESH_JOB_LOCK:
        if not job_id:
            return _refresh_job_json_locked(), 200
        if REFRESH_JOB.get("job_id") == job_id:
            return _refresh_job_json_locked(), 200
        terminal = REFRESH_JOB_HISTORY.get(job_id)
        if terminal is not None:
            return json.dumps(terminal).encode(), 200
        return json.dumps({
            "status": "not_found",
            "error": "refresh job not found",
        }).encode(), 404


def _run_refresh_job(job_id: str) -> None:
    global CURRENT_FETCH_JOB_ID
    new_count = 0
    error = ""
    try:
        before_ids = article_id_snapshot()
        CURRENT_FETCH_JOB_ID = job_id
        body, status = run_fetcher()
        payload = json.loads(body)
        completed = 200 <= status < 300 and payload.get("status") == "ok"
        if completed:
            new_count = len(article_id_snapshot() - before_ids)
        else:
            payload_error = payload.get("error")
            error = (
                payload_error
                if payload_error in ("timeout", "fetcher already running")
                else "refresh failed"
            )
    except Exception:
        completed = False
        error = "refresh failed"

    with REFRESH_JOB_LOCK:
        if REFRESH_JOB["job_id"] != job_id:
            return
        REFRESH_JOB.update({
            "status": "completed" if completed else "failed",
            "finished_at": int(time.time()),
            "new_count": new_count,
            "error": error,
        })
        _remember_terminal_job_locked()


def start_refresh_job(trigger: str = "manual") -> tuple[bytes, int]:
    with REFRESH_JOB_LOCK:
        if REFRESH_JOB["status"] == "running":
            return _refresh_job_json_locked(), 200
        job_id = uuid.uuid4().hex
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
    try:
        threading.Thread(
            target=_run_refresh_job,
            args=(job_id,),
            name=f"refresh-job-{job_id[:8]}",
            daemon=True,
        ).start()
    except Exception:
        with REFRESH_JOB_LOCK:
            if REFRESH_JOB["job_id"] == job_id and REFRESH_JOB["status"] == "running":
                REFRESH_JOB.update({
                    "status": "failed",
                    "finished_at": int(time.time()),
                    "error": "refresh failed",
                })
                _remember_terminal_job_locked()
            return _refresh_job_json_locked(), 500
    return body, 202


def enqueue_new_article_images(existing_article_ids: set[int]) -> None:
    """Queue image cache warmup for newly fetched articles without blocking refresh."""
    conn = None
    try:
        conn = sqlite3.connect(str(DB_FILE), timeout=30)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, thumb, body_html
            FROM articles
            ORDER BY timestamp DESC
            """
        ).fetchall()
        rows = [row for row in rows if int(row["id"]) not in existing_article_ids]
        queued = 0
        for row in rows:
            queued += enqueue_article_image_prefetch(row["id"], row["body_html"], row["thumb"])
        if queued:
            log.info(f"Queued {queued} image(s) for background cache warmup")
    except Exception as exc:
        log.warning(f"Image prefetch enqueue failed: {exc}")
    finally:
        if conn:
            conn.close()


def enqueue_today_wsrv_article_images() -> dict[str, int]:
    """Queue all images for today's articles containing wsrv URLs."""
    result = {"articles": 0, "queued": 0}
    if not DB_FILE.exists():
        log.info("Startup wsrv image scan skipped: news db not found")
        return result

    conn = None
    try:
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        conn = sqlite3.connect(str(DB_FILE), timeout=30)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, thumb, body_html
            FROM articles
            WHERE date = ?
              AND (
                LOWER(COALESCE(thumb, '')) LIKE '%wsrv.nl%'
                OR LOWER(COALESCE(body_html, '')) LIKE '%wsrv.nl%'
              )
            ORDER BY timestamp DESC
            """,
            (today,),
        ).fetchall()
        result["articles"] = len(rows)
        for row in rows:
            result["queued"] += enqueue_article_image_prefetch(
                row["id"],
                row["body_html"],
                row["thumb"],
                body_limit=None,
            )
        log.info(
            "Startup wsrv image scan: articles=%s queued=%s date=%s",
            result["articles"],
            result["queued"],
            today,
        )
    except Exception as exc:
        log.warning(f"Startup wsrv image scan failed: {exc}")
    finally:
        if conn:
            conn.close()
    return result


def periodic_refresh():
    """Run fetcher periodically in the background."""
    start_refresh_job("periodic")
    threading.Timer(REFRESH_INTERVAL, periodic_refresh).start()


# ─── API Handlers ─────────────────────────────────────────

def _diagnostics(count: int | None = None) -> dict:
    channel_url = (os.environ.get("TELEGRAM_CHANNEL_URL") or "").strip()
    channel = channel_url or (os.environ.get("TELEGRAM_CHANNEL") or "").strip()
    exists = DB_FILE.exists()
    try:
        db_size = DB_FILE.stat().st_size if exists else 0
    except OSError:
        db_size = 0
    news_json = {"exists": NEWS_JSON_FILE.exists(), "size": 0, "count": None}
    if NEWS_JSON_FILE.exists():
        try:
            news_json["size"] = NEWS_JSON_FILE.stat().st_size
            data = json.loads(NEWS_JSON_FILE.read_text(encoding="utf-8"))
            news_json["count"] = data.get("count", len(data.get("items", [])))
        except Exception as e:
            news_json["error"] = str(e)
    state = {"exists": STATE_FILE.exists(), "last_seen_id": None}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state["last_seen_id"] = data.get("last_seen_id")
        except Exception as e:
            state["error"] = str(e)
    with REFRESH_JOB_LOCK:
        refresh_job = {
            "status": REFRESH_JOB.get("status") or "idle",
            "trigger": REFRESH_JOB.get("trigger") or "",
        }
    global_article_count = None
    if exists:
        try:
            conn = sqlite3.connect(DB_FILE)
            try:
                global_article_count = int(conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
            finally:
                conn.close()
        except (sqlite3.Error, OSError, TypeError):
            pass
    return {
        "data_dir": str(DATA_DIR),
        "db_path": str(DB_FILE),
        "db_exists": exists,
        "db_size": db_size,
        "article_count": count,
        "global_article_count": global_article_count,
        "news_json": news_json,
        "fetcher_state": state,
        "telegram_channel_configured": bool(channel and channel != "your_channel"),
        "telegram_channel": channel if channel and channel != "your_channel" else "",
        "telegram_channel_default": not bool(channel and channel != "your_channel"),
        "last_fetch": dict(LAST_FETCH_STATUS),
        "refresh_job": refresh_job,
    }


def api_meta() -> bytes:
    """GET /api/meta — total article count."""
    conn = None
    try:
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        return json.dumps({"count": count, "diagnostics": _diagnostics(count)}).encode()
    except Exception as e:
        return json.dumps({"error": str(e), "diagnostics": _diagnostics(None)}).encode()
    finally:
        if conn:
            conn.close()


_DISPLAY_ATTRIBUTION_RE = re.compile(
    r"(?is)(?:"
    r"\s*<p[^>]*>\s*(?:出处\s*[:：]\s*|via\s*)"
    r"(?:<a\b[^>]*>.*?</a>|[^<\r\n]{1,80})\s*</p>\s*|"
    r"(?:^|[\r\n])\s*(?:出处\s*[:：]\s*|via\s*)"
    r"(?:<a\b[^>]*>.*?</a>|[^\r\n]{1,80})\s*"
    r")$"
)


def _strip_display_attribution(value: str | None) -> str | None:
    if not value:
        return value
    cleaned = value
    while True:
        next_value = _DISPLAY_ATTRIBUTION_RE.sub("", cleaned).rstrip()
        if next_value == cleaned:
            return cleaned
        cleaned = next_value


def _sanitize_article_html(value: str | None) -> str | None:
    if not value:
        return value
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup.find_all(["script", "style", "iframe", "object", "embed"]):
        tag.decompose()
    # Telegraph separates blocks with standalone <br> tags (often several in a
    # row) sitting directly between <p>/<figure> elements and between <img> and
    # <figcaption>. Combined with our paragraph/figure margins these produce
    # doubled or tripled gaps. Drop those spacer <br>s so spacing matches the
    # original Telegraph layout, keeping genuine in-text line breaks inside
    # <p>, <li>, headings, figcaption, etc.
    for br in soup.find_all("br"):
        parent = br.parent
        if parent is not None and parent.name in {"article", "figure"}:
            br.decompose()
    for tag in soup.find_all(True):
        for attr, attr_value in list(tag.attrs.items()):
            attr_l = attr.lower()
            if attr_l.startswith("on"):
                del tag.attrs[attr]
                continue
            values = attr_value if isinstance(attr_value, list) else [str(attr_value)]
            joined = " ".join(str(v).strip().lower() for v in values)
            if attr_l in {"href", "src", "xlink:href", "formaction"}:
                if joined.startswith(("javascript:", "data:text/html")):
                    del tag.attrs[attr]
    return str(soup)


def _clean_article_display_fields(item: dict) -> dict:
    for field in ("summary", "body_html"):
        if field in item:
            item[field] = _strip_display_attribution(item.get(field))
    if "body_html" in item:
        item["body_html"] = _sanitize_article_html(item.get("body_html"))
    return item


def api_news_list(params: dict) -> bytes:
    """GET /api/news — paginated or incremental list (no body_html)."""
    try:
        page = int(params.get("page", ["1"])[0])
        size = int(params.get("size", ["30"])[0])
        page = max(page, 1)
        size = min(max(size, 1), 200)
        since = params.get("since", [None])[0]
        query = (params.get("q", [""])[0] or "").strip()
        category = (params.get("category", [""])[0] or "").strip()
        article_date = (params.get("date", [""])[0] or "").strip()
        sources = [
            source.strip()
            for source in params.get("source", [])
            if source and source.strip()
        ]
    except (ValueError, IndexError):
        return json.dumps({"error": "invalid params"}).encode()

    conn = None
    try:
        conn = get_db()
        source_expr = "COALESCE(NULLIF(feed_source, ''), source)"
        clauses = []
        args = []
        if query:
            pattern = f"%{query.lower()}%"
            clauses.append(
                "(lower(title) LIKE ? "
                f"OR lower({source_expr}) LIKE ? "
                "OR lower(origin_source) LIKE ? "
                "OR lower(summary) LIKE ?)"
            )
            args.extend((pattern, pattern, pattern, pattern))
        if article_date:
            clauses.append("date = ?")
            args.append(article_date)
        if sources:
            placeholders = ",".join("?" for _ in sources)
            clauses.append(f"{source_expr} IN ({placeholders})")
            args.extend(sources)
        if category:
            if category not in CATEGORY_ORDER:
                return json.dumps({"error": "invalid category"}).encode()
            if category == "Info":
                clauses.append(
                    f"({source_expr} IN "
                    "(SELECT source FROM source_categories WHERE category = ?) "
                    f"OR {source_expr} NOT IN (SELECT source FROM source_categories))"
                )
            else:
                clauses.append(
                    f"{source_expr} IN "
                    "(SELECT source FROM source_categories WHERE category = ?)"
                )
            args.append(category)
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        if since:
            since_ts = int(since)
            since_where = f"{where_sql}{' AND' if where_sql else ' WHERE'} timestamp >= ?"
            since_args = (*args, since_ts)
            rows = conn.execute(
                "SELECT id, title, original_title, COALESCE(NULLIF(feed_source, ''), source) AS source, "
                "       COALESCE(NULLIF(feed_source, ''), source) AS feed_source, origin_source, "
                "       time, date, timestamp, thumb, has_full_content, telegraph_url, summary "
                f"FROM articles{since_where} ORDER BY timestamp DESC LIMIT ?",
                (*since_args, size),
            ).fetchall()
            total = conn.execute(
                f"SELECT COUNT(*) FROM articles{since_where}",
                since_args,
            ).fetchone()[0]
        else:
            offset = (page - 1) * size
            base_select = (
                "SELECT id, title, original_title, COALESCE(NULLIF(feed_source, ''), source) AS source, "
                "       COALESCE(NULLIF(feed_source, ''), source) AS feed_source, origin_source, "
                "       time, date, timestamp, thumb, has_full_content, telegraph_url, summary "
                "FROM articles"
            )
            rows = conn.execute(
                f"{base_select}{where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (*args, size, offset),
            ).fetchall()
            total = conn.execute(
                f"SELECT COUNT(*) FROM articles{where_sql}",
                args,
            ).fetchone()[0]

        items = [_clean_article_display_fields(dict(r)) for r in rows]
        return json.dumps({
            "items": items,
            "total": total,
            "page": page,
            "size": size,
            "diagnostics": _diagnostics(total) if total == 0 else None,
        }, ensure_ascii=False).encode()
    except Exception as e:
        return json.dumps({"error": str(e), "diagnostics": _diagnostics(None)}).encode()
    finally:
        if conn:
            conn.close()


def api_title_updates(params: dict) -> bytes:
    """GET /api/news/title-updates — lightweight title changes after cursor."""
    since = (params.get("since", [""])[0] or "").strip()
    since_ts = since
    since_id = 0
    if "|" in since:
        since_ts, since_id_text = since.rsplit("|", 1)
        try:
            since_id = int(since_id_text)
        except ValueError:
            since_id = 0
    conn = None
    try:
        conn = get_db()
        if not since:
            cursor = conn.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')").fetchone()[0]
            return json.dumps({
                "items": [],
                "cursor": cursor,
            }, ensure_ascii=False).encode()
        rows = conn.execute(
            "SELECT id, title, original_title, title_updated_at, title_source "
            "FROM articles "
            "WHERE title_updated_at IS NOT NULL "
            "AND (title_updated_at > ? OR (title_updated_at = ? AND id > ?)) "
            "ORDER BY title_updated_at ASC, id ASC LIMIT 500",
            (since_ts, since_ts, since_id),
        ).fetchall()
        items = [dict(r) for r in rows]
        if items:
            with _article_cache_lock:
                for item in items:
                    _article_cache.pop(int(item["id"]), None)
        cursor = f"{items[-1]['title_updated_at']}|{items[-1]['id']}" if items else since
        return json.dumps({
            "items": items,
            "cursor": cursor,
        }, ensure_ascii=False).encode()
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()
    finally:
        if conn:
            conn.close()


def api_cache_evict(params: dict) -> tuple[bytes, int]:
    """GET /internal/cache-evict?id=<article_id> — loopback-only, called by
    web_server.py right after it updates an article's title/body in news.db,
    so the next read doesn't return a stale cached response. Without this,
    staleness only self-heals on the next ~15min fetcher cycle (which clears
    the whole cache) or when a client happens to poll /api/news/title-updates
    (which only handles title changes, not body_html).
    """
    article_id = (params.get("id", [""])[0] or "").strip()
    if not article_id.isdigit():
        return json.dumps({"error": "invalid id"}).encode(), 400
    with _article_cache_lock:
        _article_cache.pop(int(article_id), None)
    return json.dumps({"ok": True}).encode(), 200


def _build_news_detail_response(article_id: int) -> bytes:
    """GET /api/news/<id> — single article with body_html (cached)."""
    conn = None
    try:
        conn = get_db()
        deleted = conn.execute(
            "SELECT 1 FROM deleted_articles WHERE article_id = ?",
            (article_id,),
        ).fetchone()
        if deleted:
            return json.dumps({"error": "not found"}).encode()
        row = conn.execute(
            "SELECT * FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if not row:
            return json.dumps({"error": "not found"}).encode()
        item = dict(row)
        item["feed_source"] = item.get("feed_source") or item.get("source") or ""
        item["origin_source"] = item.get("origin_source") or ""
        item["source"] = item["feed_source"]
        item = _clean_article_display_fields(item)
        result = json.dumps(item, ensure_ascii=False).encode()
        return result
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()
    finally:
        if conn:
            conn.close()


def api_news_detail(article_id: int) -> bytes:
    """GET /api/news/<id> - single article with body_html (cached)."""
    with _article_cache_lock:
        cached = _article_cache.get(article_id)
        if cached is not None:
            return cached
        event = _article_cache_inflight.get(article_id)
        if event is None:
            event = threading.Event()
            _article_cache_inflight[article_id] = event
            producer = True
        else:
            producer = False

    if not producer:
        event.wait()
        with _article_cache_lock:
            cached = _article_cache.get(article_id)
            if cached is not None:
                return cached
        return _build_news_detail_response(article_id)

    try:
        result = _build_news_detail_response(article_id)
        with _article_cache_lock:
            try:
                data = json.loads(result.decode("utf-8"))
            except Exception:
                data = {}
            if not data.get("error"):
                _article_cache[article_id] = result
            else:
                _article_cache.pop(article_id, None)
            _article_cache_inflight.pop(article_id, None)
            event.set()
        return result
    except Exception:
        with _article_cache_lock:
            _article_cache_inflight.pop(article_id, None)
            event.set()
        raise


def api_sources() -> bytes:
    """GET /api/sources — source category metadata."""
    conn = None
    try:
        conn = get_db()
        rows = source_rows(conn)
        return json.dumps({
            "categories": CATEGORY_ORDER,
            "category_names": CATEGORY_NAMES,
            "sources": rows,
        }, ensure_ascii=False).encode()
    except Exception as e:
        return json.dumps({"error": str(e)}).encode()
    finally:
        if conn:
            conn.close()


def send_json(handler, data: bytes, status=200):
    """Send a JSON response."""
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        log.warning("Client disconnected before response could be written")


def send_text(handler, text: str, status=200):
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Content-Length", str(len(text.encode())))
        handler.end_headers()
        handler.wfile.write(text.encode())
    except (BrokenPipeError, ConnectionResetError):
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path == "/refresh":
            body, status = start_refresh_job("manual")
            send_json(self, body, status)
            return
        send_text(self, "not found", 404)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = urllib.parse.parse_qs(parsed.query)

        # ── API routes ──
        if path == "/api/meta":
            send_json(self, api_meta())
            return

        if path == "/api/news":
            send_json(self, api_news_list(params))
            return

        if path == "/api/news/title-updates":
            send_json(self, api_title_updates(params))
            return

        if path == "/api/sources":
            send_json(self, api_sources())
            return

        # /api/news/<id>
        m = re.match(r"^/api/news/(\d+)$", path)
        if m:
            send_json(self, api_news_detail(int(m.group(1))))
            return

        # ── Legacy routes ──
        if path == "/refresh":
            body, status = start_refresh_job("manual")
            send_json(self, body, status)
            return

        if path == "/refresh/status":
            job_id = (params.get("job_id", [""])[0] or "").strip()
            if job_id:
                body, status = get_refresh_job_status_response(job_id)
                send_json(self, body, status)
            else:
                send_json(self, get_refresh_job_status())
            return

        # ── Internal (loopback-only via nginx routing; not exposed under /api/) ──
        if path == "/internal/cache-evict":
            body, status = api_cache_evict(params)
            send_json(self, body, status)
            return

        if path in ("/img-cache", "/img-proxy"):
            self._handle_img_cache(params)
            return

        send_text(self, "not found", 404)

    def _handle_img_cache(self, params):
        img_url = params.get("url", [None])[0]
        if not img_url:
            send_text(self, "Missing url parameter", 400)
            return

        parsed_url = urllib.parse.urlparse(img_url)
        if parsed_url.scheme not in ("http", "https"):
            send_text(self, "Invalid URL scheme", 400)
            return

        try:
            cached = get_cached_image(img_url)
            if not cached:
                cached = cache_image(img_url)
            if not cached:
                body, content_type = fetch_remote_image(img_url)
            else:
                path, content_type = cached
                body = path.read_bytes()

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=2592000")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            cached = get_cached_image(img_url)
            if cached:
                path, content_type = cached
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "public, max-age=2592000")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            log.warning(f"img-cache failed for {img_url[:80]}: {e}")
            send_text(self, f"Image cache error: {e}", 502)

    def log_message(self, fmt, *args):
        log.info(fmt % args)


class RayNewsThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    port = 8081
    diag = _diagnostics(None)
    log.info(
        "Startup diagnostics: data_dir=%s db_exists=%s db_size=%s telegram_configured=%s",
        diag["data_dir"],
        diag["db_exists"],
        diag["db_size"],
        diag["telegram_channel_configured"],
    )
    if diag["telegram_channel_default"]:
        log.warning("TELEGRAM_CHANNEL is not configured or still equals your_channel")
    server = RayNewsThreadingHTTPServer(("127.0.0.1", port), Handler)
    start_refresh_job("startup")
    # Start periodic refresh in background
    threading.Timer(REFRESH_INTERVAL, periodic_refresh).start()
    log.info(f"Refresh + API server listening on {port} (auto-refresh every {REFRESH_INTERVAL}s)")
    threading.Thread(
        target=enqueue_today_wsrv_article_images,
        name="startup-wsrv-image-scan",
        daemon=True,
    ).start()
    server.serve_forever()
