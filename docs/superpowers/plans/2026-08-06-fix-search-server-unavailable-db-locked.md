# 全文搜索可用性与 news.db 锁竞争修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除搜索的“一次临时故障即永久显示完整搜索不可用”，并移除正常运行期间不必要的 schema 写锁及长事务。

**Architecture:** 四层防护按因果顺序实施：共享 schema migrator 对已就绪数据库走只读快路径；refresh_server 在启动 fetcher 前预热迁移且只有 articles 存在后才锁存；`ai_results` 初始化使用按 DB 路径、线程安全的进程锁存；fetcher 长事务增量提交。前端最后提供局部 controller 超时、受序列号保护的自动重试及手动重试。只重试幂等数据库操作，绝不重跑 AI/provider 调用。

**Tech Stack:** Python 3.12、SQLite WAL、原生 JavaScript、Node `vm`、pytest。

## Global Constraints

- `ensure_article_schema` 的真正迁移仍使用 `BEGIN IMMEDIATE`；只读快路径不得执行 DDL/DML。
- 快路径必须验证 `deleted_articles`、请求的列集合以及 `idx_feed_source`；不能只看 articles 是否存在。
- 所有进程锁存都按 `os.path.abspath(DB_PATH)` 区分，并且只在成功后置位。
- 不用重试包装“抓取 + AI 调用 + 保存”的整体任务；只允许重试无外部副作用的 SQLite 边界。
- 搜索 controller 必须是每次请求的局部变量；旧请求的 timer 不得 abort 新请求。
- 换词、关闭、清空搜索时取消 retry timer；过期请求不得覆盖结果。

---

### Task 1: 为共享 schema migrator 增加完整的只读快路径

**Files:**
- Modify: `news_schema.py`
- Modify: `tests/test_news_schema.py`

**Interfaces:**
- Produces: `_schema_already_current(conn, *, include_source_columns, include_title_columns) -> bool`。

- [ ] **Step 1: 写失败测试**

```python
def test_current_article_schema_skips_begin_and_ddl(tmp_path):
    conn = sqlite3.connect(tmp_path / "news.db")
    conn.execute("CREATE TABLE deleted_articles (article_id INTEGER PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE articles (id INTEGER PRIMARY KEY, body_html TEXT, "
        "original_body_html TEXT, feed_source TEXT NOT NULL DEFAULT '', "
        "origin_source TEXT NOT NULL DEFAULT '', original_title TEXT, "
        "title_updated_at TEXT, title_source TEXT)"
    )
    conn.execute("CREATE INDEX idx_feed_source ON articles(feed_source)")
    conn.commit()

    class Spy:
        def __init__(self, real): self.real, self.writes = real, []
        def __getattr__(self, name): return getattr(self.real, name)
        def execute(self, sql, *args):
            normalized = " ".join(sql.split()).upper()
            if normalized.startswith(("BEGIN", "CREATE", "ALTER", "UPDATE")):
                self.writes.append(normalized)
            return self.real.execute(sql, *args)

    spy = Spy(conn)
    ensure_article_schema(spy)
    assert spy.writes == []


def test_missing_feed_source_index_uses_migration_slow_path(tmp_path):
    conn = _current_schema_without_feed_source_index(tmp_path)
    ensure_article_schema(conn)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_feed_source'"
    ).fetchone()
```

- [ ] **Step 2: 验证为红**

Run: `python3 -m pytest tests/test_news_schema.py::test_current_article_schema_skips_begin_and_ddl tests/test_news_schema.py::test_missing_feed_source_index_uses_migration_slow_path -q`

Expected: 第一项 FAIL，因为当前总是 `BEGIN IMMEDIATE`。

- [ ] **Step 3: 实现只读快路径**

```python
def _schema_already_current(conn, *, include_source_columns, include_title_columns):
    deleted = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_articles'"
    ).fetchone()
    if not deleted:
        return False
    articles = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles'"
    ).fetchone()
    if not articles:
        return True
    columns = _article_columns(conn)
    if "body_html" in columns and "original_body_html" not in columns:
        return False
    if include_source_columns:
        if not {"feed_source", "origin_source"}.issubset(columns):
            return False
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_feed_source'"
        ).fetchone():
            return False
    if include_title_columns and not set(_TITLE_COLUMNS).issubset(columns):
        return False
    return True
```

仅在 `owns_transaction` 时、`BEGIN IMMEDIATE` 之前调用；caller-owned transaction 继续走既有慢路径语义。

- [ ] **Step 4: 验证并提交**

Run: `python3 -m pytest tests/test_news_schema.py tests/test_review_bug_hardening.py -q`

```bash
git add news_schema.py tests/test_news_schema.py
git commit -m "fix: skip schema write lock when news schema is current"
```

---

### Task 2: 启动预热并正确锁存 refresh_server schema

**Files:**
- Modify: `refresh_server.py`
- Modify: `tests/test_review_bug_hardening.py`

**Interfaces:**
- `ensure_schema_once(conn)` 只有检测到 articles 表后才设置 `_schema_ready_event`。
- Produces: `_warm_news_schema() -> bool`。

- [ ] **Step 1: 写失败测试**

```python
def test_schema_once_does_not_latch_before_articles_exists(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "news.db")
    monkeypatch.setattr(refresh_server, "_schema_ready", False)
    refresh_server._schema_ready_event.clear()
    refresh_server.ensure_schema_once(conn)
    assert refresh_server._schema_ready is False
    assert not refresh_server._schema_ready_event.is_set()


def test_warmup_runs_before_startup_fetch(monkeypatch):
    source = Path(refresh_server.__file__).read_text(encoding="utf-8")
    main = source[source.index('if __name__ == "__main__":'):]
    assert main.index("_warm_news_schema()") < main.index('start_refresh_job("startup")')
```

- [ ] **Step 2: 验证为红**

Run: `python3 -m pytest tests/test_review_bug_hardening.py::test_schema_once_does_not_latch_before_articles_exists tests/test_review_bug_hardening.py::test_warmup_runs_before_startup_fetch -q`

- [ ] **Step 3: 实现**

在迁移成功后查询 articles；只有存在时设置 bool/event。新增：

```python
def _warm_news_schema() -> bool:
    conn = None
    try:
        conn = get_db()
        return bool(_schema_ready_event.is_set() and _schema_ready)
    except Exception:
        log.exception("Schema warmup failed; migration remains lazy")
        return False
    finally:
        if conn is not None:
            conn.close()
```

在 `RayNewsThreadingHTTPServer` 创建后、`start_refresh_job("startup")` 前调用。不要增加可能叠加 30 秒 busy timeout 的外层 sleep 重试。

- [ ] **Step 4: 验证并提交**

Run: `python3 -m pytest tests/test_review_bug_hardening.py tests/test_news_search.py tests/test_news_schema.py -q`

```bash
git add refresh_server.py tests/test_review_bug_hardening.py
git commit -m "fix: warm news schema before startup refresh"
```

---

### Task 3: 对 ai_results 初始化使用路径化、线程安全锁存

**Files:**
- Modify: `web_server.py`
- Modify: `tests/test_news_db_thread_safety.py`

**Interfaces:**
- Produces: `_ai_results_schema_lock: RLock`、`_ai_results_schema_ready_paths: set[str]`、`_init_ai_results_table() -> bool`。

- [ ] **Step 1: 写失败测试**

```python
def test_init_ai_results_table_opens_one_connection_after_success(tmp_path, monkeypatch):
    db = tmp_path / "news.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(web_server, "NEWS_DB", str(db))
    monkeypatch.setattr(web_server, "_ai_results_schema_ready_paths", set())
    real = web_server._news_db_conn
    calls = []
    @contextmanager
    def counted():
        calls.append(1)
        with real() as conn:
            yield conn
    monkeypatch.setattr(web_server, "_news_db_conn", counted)
    assert web_server._init_ai_results_table() is True
    assert web_server._init_ai_results_table() is True
    assert len(calls) == 1


def test_init_ai_results_latch_is_scoped_by_database_path(tmp_path, monkeypatch):
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    sqlite3.connect(first).close(); sqlite3.connect(second).close()
    monkeypatch.setattr(web_server, "_ai_results_schema_ready_paths", set())
    monkeypatch.setattr(web_server, "NEWS_DB", str(first)); assert web_server._init_ai_results_table()
    monkeypatch.setattr(web_server, "NEWS_DB", str(second)); assert web_server._init_ai_results_table()
    assert len(web_server._ai_results_schema_ready_paths) == 2
```

- [ ] **Step 2: 实现锁存**

```python
_ai_results_schema_lock = threading.RLock()
_ai_results_schema_ready_paths: set[str] = set()

def _init_ai_results_table() -> bool:
    db_path = os.path.abspath(NEWS_DB)
    if not os.path.exists(db_path):
        return False
    with _ai_results_schema_lock:
        if db_path in _ai_results_schema_ready_paths:
            return True
        try:
            with _news_db_conn() as conn:
                # existing CREATE/PRAGMA/ALTER/commit body
            _ai_results_schema_ready_paths.add(db_path)
            return True
        except Exception as exc:
            print(f"[ai-results] schema initialization failed: {exc}")
            return False
```

只在 commit 成功且 context 退出后置位；不得保留裸 `except: pass`。

- [ ] **Step 3: 验证并提交**

Run: `python3 -m pytest tests/test_news_db_thread_safety.py tests/test_translation_updates.py -q`

```bash
git add web_server.py tests/test_news_db_thread_safety.py
git commit -m "fix: latch ai result schema initialization per database"
```

---

### Task 4: 缩短 fetcher 周期末写事务

**Files:**
- Modify: `source_categories.py`
- Modify: `fetcher.py`
- Modify: `tests/test_news_db_thread_safety.py`, `tests/test_fulltext_backfill.py`

- [ ] **Step 1: 写行为测试**

使用真实 WAL 数据库和第二连接：在每个别名更新/每条 fulltext backfill 完成后，从第二连接读取并断言该批已可见，而后续批尚未执行。不要使用源码字符串计数作为唯一测试。

- [ ] **Step 2: 实现提交边界**

- `ensure_article_sources`: `init_source_categories` 完成后，每个 alias UPDATE 后 commit；DISTINCT 插入全部完成后再 commit。
- `backfill_missing_fulltext`: 每条 article UPDATE（以及翻译缓存失效，见另一计划）作为同一小事务提交；不得在网络 future 等待期间保持未提交写事务。

- [ ] **Step 3: 验证并提交**

Run: `python3 -m pytest tests/test_news_db_thread_safety.py tests/test_fulltext_backfill.py tests/test_source_maintenance.py -q`

```bash
git add source_categories.py fetcher.py tests/test_news_db_thread_safety.py tests/test_fulltext_backfill.py
git commit -m "fix: shorten fetcher database write transactions"
```

---

### Task 5: 搜索使用局部超时、自动重试和手动恢复

**Files:**
- Modify: `frontend/index.html`
- Modify: `tests/test_ai_relay_frontend.py`, `tests/test_access_and_ui_contracts.py`

**Interfaces:**
- Produces: `cancelSearchRetry()`、`scheduleSearchRetry()`、`retryServerSearch()`。

- [ ] **Step 1: 写失败的 Node VM 测试**

测试 context 的 `getElementById` key 必须是不带 `#` 的 `articleSearchInput/searchStatus/searchResults`。覆盖：首次失败后二次成功；换词后旧 retry 不执行；耗尽后保留本地结果并出现手动重试按钮；旧请求 timer 不 abort 新 controller。

- [ ] **Step 2: 实现局部 controller**

```js
const controller = new AbortController();
if (searchRequestController) searchRequestController.abort();
searchRequestController = controller;
const timeoutTimer = setTimeout(() => controller.abort(), SEARCH_REQUEST_TIMEOUT_MS);
// fetch(..., {signal: controller.signal})
// finally:
clearTimeout(timeoutTimer);
if (searchRequestController === controller) searchRequestController = null;
```

新增 `cancelSearchRetry({resetCount = true} = {})`，并从 `closeSearch`、`clearArticleSearch`、`scheduleSearchRender` 调用。retry callback 同时校验 query 和创建时的 `searchRequestSeq`；成功后清零计数。耗尽时若存在 `localMatches`，重新渲染本地匹配并在末尾追加重试卡片，而不是用错误页覆盖结果。

- [ ] **Step 3: 验证并提交**

Run: `python3 -m pytest tests/test_ai_relay_frontend.py tests/test_access_and_ui_contracts.py -q`

```bash
git add frontend/index.html tests/test_ai_relay_frontend.py tests/test_access_and_ui_contracts.py
git commit -m "fix: make server search recover from transient failures"
```

---

### Task 6: 回归与部署验证

- [ ] Run: `python3 -m pytest tests/test_news_schema.py tests/test_review_bug_hardening.py tests/test_news_db_thread_safety.py tests/test_fulltext_backfill.py tests/test_ai_relay_frontend.py tests/test_access_and_ui_contracts.py -q`
- [ ] Run: `python3 -m pytest -q`
- [ ] 部署前后分别统计 `database is locked` 日志；记录相同观察窗口与抓取轮次。
- [ ] 冷启动搜索、抓取期间搜索、换词、清空、加载更多均手测通过。

## Definition of Done

- [ ] 正常 schema 检查不获取写锁。
- [ ] refresh_server 在 fetcher 前预热，且空 DB 不错误锁存。
- [ ] ai_results 初始化成功后同一路径只执行一次，失败可重试。
- [ ] fetcher 不在网络等待期间持有写事务。
- [ ] 搜索临时失败可自动/手动恢复且无跨请求 abort 竞态。
- [ ] 不存在包裹 AI/provider 调用的数据库重试。
