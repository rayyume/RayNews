# 防止摘要正文产生截断自动翻译 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegraph 全文未抓到时不生成正文翻译；全文补抓后原子清除基于摘要的旧缓存；后台以有界、可续页的历史扫描修复已存在的截断译文。

**Architecture:** 今日新文章走 prevention gate。`backfill_missing_fulltext()` 在同一个小事务内升级正文并使旧 translation 失效。历史修复不复用仅扫描今天的 `_fetch_untranslated_articles()`，而使用独立 keyset cursor 分页扫描所有已完整文章；正常今日任务优先，剩余 batch 配额用于历史修复。截断判断保持保守，只有来源足够长、含拉丁字符且译文/原文纯文本长度比低于 0.30 时重译。

**Tech Stack:** Python、SQLite、pytest。

## Global Constraints

- `articles.body_html` 与 `original_body_html` 永远保存原文；译文只写 `ai_results.translation`。
- 非 Telegraph 文章不能被 pending-fulltext gate 阻塞。
- 清除缓存不得吞掉 `database is locked`、损坏等错误；仅“表不存在”是正常 no-op。
- 历史扫描不得局限于今天，也不得每轮永远只看同一批最新非候选文章。
- 已中文化标题不重新翻译；重建正文缓存时保留已有缓存 title，若缓存已被 backfill 清除则使用 articles.title。

---

### Task 1: 阻止未完成 Telegraph 正文进入自动翻译

**Files:**
- Modify: `web_server.py`
- Modify: `tests/test_auto_translation_completion.py`

**Interfaces:**
- Produces: `_translation_pending_fulltext(article: dict) -> bool`。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.parametrize(("article", "expected"), [
    ({"telegraph_url": "https://telegra.ph/x", "has_full_content": 0}, True),
    ({"telegraph_url": "https://telegra.ph/x", "has_full_content": 1}, False),
    ({"telegraph_url": "", "has_full_content": 0}, False),
])
def test_translation_pending_fulltext(article, expected):
    assert web_server._translation_pending_fulltext(article) is expected


def test_fetch_untranslated_skips_pending_telegraph_excerpt(news_db, monkeypatch):
    today = dt.datetime.now().strftime("%Y-%m-%d")
    _insert_article(news_db, id=1, date=today, telegraph_url="https://telegra.ph/a",
                    has_full_content=0, body_html="<p>English excerpt</p>")
    _insert_article(news_db, id=2, date=today, telegraph_url="https://telegra.ph/b",
                    has_full_content=1, body_html="<p>English complete body</p>")
    rows = web_server._fetch_untranslated_articles(
        {"auto_translate_title": False, "auto_translate_content": True}, limit=10
    )
    assert [row["id"] for row in rows] == [2]
```

- [ ] **Step 2: 验证为红**

Run: `python3 -m pytest tests/test_auto_translation_completion.py::test_translation_pending_fulltext tests/test_auto_translation_completion.py::test_fetch_untranslated_skips_pending_telegraph_excerpt -q`

- [ ] **Step 3: 实现**

```python
def _translation_pending_fulltext(article: dict) -> bool:
    return bool(article.get("telegraph_url")) and not bool(article.get("has_full_content"))
```

给今日候选 SELECT 增加 `a.has_full_content, a.telegraph_url`，并在 `content_needed` 中加入 `not _translation_pending_fulltext(article)`。

- [ ] **Step 4: 验证并提交**

Run: `python3 -m pytest tests/test_auto_translation_completion.py -q`

```bash
git add web_server.py tests/test_auto_translation_completion.py
git commit -m "fix: wait for telegraph full text before translation"
```

---

### Task 2: 全文补抓与旧翻译失效保持同一事务

**Files:**
- Modify: `fetcher.py`
- Modify: `tests/test_fulltext_backfill.py`

**Interfaces:**
- Produces: `_invalidate_stale_translation(conn, article_id) -> bool`。

- [ ] **Step 1: 写失败测试**

```python
def test_backfill_invalidates_excerpt_translation(tmp_path, monkeypatch):
    _patch_paths(monkeypatch, tmp_path)
    conn = fetcher.init_db()
    _insert(conn, id=1, timestamp=int(time.time()), telegraph_url="https://telegra.ph/x",
            has_full_content=0, body_html="excerpt")
    conn.execute("CREATE TABLE ai_results (article_id INTEGER PRIMARY KEY, translation TEXT, translation_updated_at TEXT)")
    conn.execute("INSERT INTO ai_results VALUES (1, '<p>短译文</p>', '2026-08-01')")
    conn.commit()
    monkeypatch.setattr(fetcher, "fetch_telegraph", lambda _url: {
        "body_html": "<p>complete English body</p>", "images": [], "detected_source": "src"
    })
    assert fetcher.backfill_missing_fulltext(conn) == 1
    row = conn.execute("SELECT translation, translation_updated_at FROM ai_results WHERE article_id=1").fetchone()
    assert tuple(row) == (None, None)


def test_translation_invalidation_is_noop_only_when_table_is_absent(tmp_path):
    conn = sqlite3.connect(tmp_path / "news.db")
    assert fetcher._invalidate_stale_translation(conn, 1) is False
```

- [ ] **Step 2: 实现显式表/列检测**

```python
def _invalidate_stale_translation(conn, article_id: int) -> bool:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_results'"
    ).fetchone()
    if not table:
        return False
    columns = {row[1] for row in conn.execute("PRAGMA table_info(ai_results)")}
    assignments = ["translation = NULL"]
    if "translation_updated_at" in columns:
        assignments.append("translation_updated_at = NULL")
    cur = conn.execute(
        f"UPDATE ai_results SET {', '.join(assignments)} "
        "WHERE article_id = ? AND translation IS NOT NULL",
        (article_id,),
    )
    return cur.rowcount > 0
```

在 article UPDATE 后调用 helper，再执行一次 `conn.commit()`；两项修改属于同一事务。不要捕获通用 `sqlite3.Error`。

- [ ] **Step 3: 验证并提交**

Run: `python3 -m pytest tests/test_fulltext_backfill.py -q`

```bash
git add fetcher.py tests/test_fulltext_backfill.py
git commit -m "fix: invalidate excerpt translation when full text arrives"
```

---

### Task 3: 实现独立、可续页的历史截断译文扫描

**Files:**
- Modify: `web_server.py`
- Modify: `tests/test_auto_translation_completion.py`

**Interfaces:**
- Produces:
  - `_translation_looks_truncated(article, cached_html, source_html) -> bool`
  - `_fetch_stale_translation_articles(limit: int) -> list[dict]`
  - `_translation_repair_cursor: tuple[int, int] | None`，按 `(timestamp, id)` 降序 keyset 分页。

- [ ] **Step 1: 写 heuristic 测试**

```python
def test_translation_looks_truncated_is_conservative():
    source = "<p>" + ("English source sentence. " * 30) + "</p>"
    assert web_server._translation_looks_truncated(
        {"has_full_content": 1}, "<p>短译文</p>", source
    ) is True
    assert web_server._translation_looks_truncated(
        {"has_full_content": 0}, "<p>短译文</p>", source
    ) is False
    assert web_server._translation_looks_truncated(
        {"has_full_content": 1}, "<p>" + ("完整译文" * 80) + "</p>", source
    ) is False
    assert web_server._translation_looks_truncated(
        {"has_full_content": 1}, "<p>短</p>", "<p>short source</p>"
    ) is False
```

- [ ] **Step 2: 写跨日期和翻页测试**

```python
def test_stale_translation_scan_reaches_old_articles_across_pages(news_db, monkeypatch):
    # 插入 60 条非截断的较新历史文章，以及 1 条更旧的截断文章；page size=25。
    # 连续调用 _fetch_stale_translation_articles 三次，合并返回 id，断言旧文章出现。
    # 不得把日期限制为 datetime.now()。


def test_repair_cursor_resets_after_end_of_history(news_db, monkeypatch):
    # 扫描至最后一页后断言 cursor 为 None；随后新增旧候选并再次调用，能够被发现。
```

- [ ] **Step 3: 实现 heuristic**

```python
def _translation_looks_truncated(article, cached_html, source_html):
    if not bool(article.get("has_full_content")) or not cached_html:
        return False
    source = _plain_text(source_html or "")
    translated = _plain_text(cached_html)
    return len(source) >= 400 and _has_latin(source) and len(translated) < len(source) * 0.30
```

- [ ] **Step 4: 实现 keyset 扫描**

每页查询所有日期的 `has_full_content=1` 且 translation 非空行，排序必须是 `ORDER BY a.timestamp DESC, a.id DESC`。下一页条件：

```sql
AND (a.timestamp < ? OR (a.timestamp = ? AND a.id < ?))
```

每次无论是否发现 stale，都把 cursor 推进到本页最后一行；少于 page size 时重置为 `None`。返回行携带 `translation_stale=True`、`translate_content_needed=True`，以及已有的 `translate_title_needed`。

- [ ] **Step 5: 验证并提交**

Run: `python3 -m pytest tests/test_auto_translation_completion.py -q`

```bash
git add web_server.py tests/test_auto_translation_completion.py
git commit -m "fix: scan historical articles for truncated translations"
```

---

### Task 4: 用剩余 batch 配额重建历史译文

**Files:**
- Modify: `web_server.py`
- Modify: `tests/test_auto_translation_completion.py`

**Interfaces:**
- `_run_auto_translation_once` 先处理今日任务，再用 `limit - len(today)` 获取历史修复。
- `_translate_article_background` 在 `translation_stale=True` 时绕过 cached_html。

- [ ] **Step 1: 写失败测试**

```python
def test_stale_translation_bypasses_cache(monkeypatch):
    translated = []
    class Service:
        def __init__(self, **_kwargs): pass
        def translate_full(self, html, *_args, **_kwargs):
            translated.append(html)
            return {"html": "<p>完整译文</p>", "title": ""}
    monkeypatch.setattr(web_server, "AIService", Service)
    monkeypatch.setattr(web_server, "_save_article_translation", lambda *_a, **_k: False)
    saved = []
    monkeypatch.setattr(web_server, "_save_ai_result", lambda article_id, **kw: saved.append(kw["translation"]) or True)
    monkeypatch.setattr(web_server, "_publish_translation_update", lambda _id: None)
    article = {"id": 9, "title": "中文标题", "body_html": "<p>long English body</p>",
               "translation": json.dumps({"title": "旧标题", "html": "<p>短译文</p>"}),
               "translation_stale": True, "translate_content_needed": True,
               "translate_title_needed": False}
    assert web_server._translate_article_background(article, _system_config())
    assert translated == ["<p>long English body</p>"]
    assert "完整译文" in saved[0]
```

- [ ] **Step 2: 实现 stale bypass**

仅在 `cached_html and not article.get("translation_stale")` 时复用缓存。stale 重译且无需翻译标题时，缓存 JSON 的 title 使用 `cached_title`，不得调用 `translate_title`。

- [ ] **Step 3: 接入批处理并验证**

今日任务达到 batch limit 时不扫描历史；否则获取剩余数量并追加，按 id 去重。

Run: `python3 -m pytest tests/test_auto_translation_completion.py tests/test_fulltext_backfill.py tests/test_translation_updates.py tests/test_title_processing.py -q`

- [ ] **Step 4: 提交**

```bash
git add web_server.py tests/test_auto_translation_completion.py
git commit -m "fix: regenerate stale full article translations"
```

---

### Task 5: 完整回归

- [ ] Run: `python3 -m pytest tests/test_auto_translation_completion.py tests/test_fulltext_backfill.py tests/test_translation_updates.py tests/test_title_processing.py tests/test_news_db_thread_safety.py tests/test_news_schema.py -q`
- [ ] Run: `python3 -m pytest -q`
- [ ] 在测试/副本数据库确认目标旧文章进入 repair 队列，完整译文写入后不再被 heuristic 选中。

## Definition of Done

- [ ] 未完成 Telegraph 正文不生成正文翻译。
- [ ] 补抓全文与清除旧译文原子提交。
- [ ] 历史扫描跨日期、可翻页、可循环，不会永远停在最新一页。
- [ ] 正常今日文章优先，历史修复只用剩余配额。
- [ ] 有效缓存和中文标题不被重建。
