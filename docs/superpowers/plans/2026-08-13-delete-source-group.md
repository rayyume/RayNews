# Delete Source Group Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the administrator source-delete action remove the complete visible source-label group, its articles, and all retained source metadata while allowing later new articles to rediscover the source from first-seen defaults.

**Architecture:** Keep `DELETE /sources/articles` as the client contract. Add a focused source-metadata purge helper in `source_categories.py`, invoke it after tombstoned article deletion with automatic stale cleanup disabled, and make known-source seeding conditional on a matching article. The frontend continues to submit every source variant in the visible label group and updates its copy to describe source-plus-article deletion.

**Tech Stack:** Python 3, Flask, SQLite, vanilla JavaScript, pytest.

## Global Constraints

- The deletion unit is the complete source label row visible to the administrator.
- Delete all submitted source variants, backend-resolved aliases, their articles, global/user metadata, and alias relationships.
- Preserve article tombstones so historical Telegram message IDs cannot return.
- Do not add a source blocklist or source-level tombstone.
- Known-source presets apply only when a matching article exists; unknown rediscovered sources start as `Info` / `pending`.
- A zero-article source row must still be deletable.
- Keep administrator authorization and the existing `DELETE /sources/articles` route.
- Do not perform unrelated refactoring.

---

## File structure

- Modify `source_categories.py`: condition preset seeding on live article sources and expose targeted metadata deletion.
- Modify `web_server.py`: compose article deletion and source-metadata deletion in the existing endpoint.
- Modify `frontend/index.html`: remove the zero-count early return and clarify destructive-action copy.
- Modify `tests/test_source_maintenance.py`: unit coverage for live-only preset seeding and metadata purge/rediscovery.
- Create `tests/test_source_deletion.py`: authenticated endpoint coverage for grouped deletion, tombstones, zero-article deletion, and error responses.
- Modify `tests/test_access_and_ui_contracts.py`: frontend request/copy regression contract.

### Task 1: Source metadata purge and live-only preset seeding

**Files:**
- Modify: `source_categories.py:271-367`
- Test: `tests/test_source_maintenance.py`

**Interfaces:**
- Produces: `delete_source_metadata(conn: sqlite3.Connection, sources: list[str]) -> int`
- Preserves: `init_source_categories(conn) -> None` and `ensure_article_sources(conn) -> int`
- Consumes: effective source expression `COALESCE(NULLIF(feed_source, ''), source)`

- [ ] **Step 1: Write failing tests for live-only preset seeding**

Append tests that initialize an empty article table, assert that the known source `少数派` is not seeded without an article, then insert a new article and assert first-seen defaults are created:

```python
def test_known_source_preset_is_seeded_only_when_an_article_exists():
    conn = _make_conn()

    sc.init_source_categories(conn)
    assert conn.execute(
        "SELECT 1 FROM source_categories WHERE source = '少数派'"
    ).fetchone() is None

    _add_article(conn, 101, "少数派")
    sc.init_source_categories(conn)
    row = conn.execute(
        "SELECT category, label, status, reason "
        "FROM source_categories WHERE source = '少数派'"
    ).fetchone()

    assert tuple(row) == ("Tech", "少数派", "pending", "seeded")
```

- [ ] **Step 2: Run the preset test and verify RED**

Run:

```bash
pytest -q tests/test_source_maintenance.py::test_known_source_preset_is_seeded_only_when_an_article_exists
```

Expected: FAIL because `init_source_categories()` currently inserts `少数派` even when `articles` is empty.

- [ ] **Step 3: Implement live-only preset seeding**

In `init_source_categories()`, inspect whether `articles` exists, collect its distinct effective source names, and execute the existing `INITIAL_CATEGORY_MAP` insert only for names in that set:

```python
article_table = conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'articles'"
).fetchone()
article_sources = set()
if article_table:
    article_sources = {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT COALESCE(NULLIF(feed_source, ''), source) "
            "FROM articles "
            "WHERE COALESCE(NULLIF(feed_source, ''), source) IS NOT NULL "
            "AND TRIM(COALESCE(NULLIF(feed_source, ''), source)) != ''"
        ).fetchall()
    }

for category, sources in INITIAL_CATEGORY_MAP.items():
    for source in sources:
        if source not in article_sources:
            continue
        conn.execute(
            """
            INSERT OR IGNORE INTO source_categories
                (source, category, label, status, reason)
            VALUES (?, ?, ?, 'pending', 'seeded')
            """,
            (source, category, local_short_source_name(source)),
        )
```

- [ ] **Step 4: Run the preset test and verify GREEN**

Run:

```bash
pytest -q tests/test_source_maintenance.py::test_known_source_preset_is_seeded_only_when_an_article_exists
```

Expected: PASS.

- [ ] **Step 5: Write failing tests for targeted metadata deletion and rediscovery**

Add a test that creates global/user categories and aliases for `Primary`, `Variant`, and unrelated sources. Call the wished-for helper with `["Primary", "Variant"]`, assert all category rows for those names and all aliases connected as alias or target are gone, and assert unrelated rows remain. Add a rediscovery assertion for an unknown source:

```python
def test_delete_source_metadata_removes_group_and_all_connected_aliases():
    conn = _make_conn()
    sc.init_source_categories(conn)
    conn.executemany(
        "INSERT INTO source_categories (source, category, label, status) "
        "VALUES (?, 'Tech', 'Group', 'manual')",
        [("Primary",), ("Variant",), ("Unrelated",)],
    )
    conn.executemany(
        "INSERT INTO user_source_categories "
        "(user_id, source, category, label, status) VALUES (7, ?, 'Biz', 'Custom', 'manual')",
        [("Primary",), ("Variant",), ("Unrelated",)],
    )
    conn.executemany(
        "INSERT INTO source_aliases (alias_source, target_source) VALUES (?, ?)",
        [
            ("Legacy", "Primary"),
            ("Variant", "External"),
            ("Unrelated Alias", "Unrelated"),
        ],
    )
    conn.executemany(
        "INSERT INTO user_source_aliases (user_id, alias_source, target_source) VALUES (7, ?, ?)",
        [
            ("User Legacy", "Primary"),
            ("Variant", "User External"),
            ("User Unrelated", "Unrelated"),
        ],
    )
    conn.commit()

    deleted = sc.delete_source_metadata(conn, ["Primary", "Variant"])

    assert deleted == 8
    assert {
        row[0] for row in conn.execute("SELECT source FROM source_categories")
    } == {"Unrelated"}
    assert {
        row[0] for row in conn.execute("SELECT source FROM user_source_categories")
    } == {"Unrelated"}
    assert {
        row[0] for row in conn.execute("SELECT alias_source FROM source_aliases")
    } == {"Unrelated Alias"}
    assert {
        row[0] for row in conn.execute("SELECT alias_source FROM user_source_aliases")
    } == {"User Unrelated"}


def test_deleted_unknown_source_is_rediscovered_without_old_settings():
    conn = _make_conn()
    sc.init_source_categories(conn)
    conn.execute(
        "INSERT INTO source_categories (source, category, label, status) "
        "VALUES ('Fresh Feed', 'Biz', 'Old Custom Label', 'manual')"
    )
    conn.commit()

    sc.delete_source_metadata(conn, ["Fresh Feed"])
    _add_article(conn, 202, "Fresh Feed")
    sc.ensure_article_sources(conn)

    row = conn.execute(
        "SELECT category, label, status, reason "
        "FROM source_categories WHERE source = 'Fresh Feed'"
    ).fetchone()
    assert tuple(row) == ("Info", "Fresh Feed", "pending", "discovered")
```

- [ ] **Step 6: Run the metadata tests and verify RED**

Run:

```bash
pytest -q \
  tests/test_source_maintenance.py::test_delete_source_metadata_removes_group_and_all_connected_aliases \
  tests/test_source_maintenance.py::test_deleted_unknown_source_is_rediscovered_without_old_settings
```

Expected: FAIL with `AttributeError` because `delete_source_metadata` does not exist.

- [ ] **Step 7: Implement targeted metadata deletion**

Add this focused helper after `cleanup_stale_source_categories()`:

```python
def delete_source_metadata(conn: sqlite3.Connection, sources: list[str]) -> int:
    """Delete category and alias metadata connected to a source-label group."""
    normalized = list(dict.fromkeys(
        str(source).strip() for source in sources if str(source).strip()
    ))
    if not normalized:
        return 0
    _ensure_source_tables(conn)
    placeholders = ",".join("?" * len(normalized))
    deleted = 0
    try:
        deleted += conn.execute(
            f"DELETE FROM source_aliases "
            f"WHERE alias_source IN ({placeholders}) OR target_source IN ({placeholders})",
            (*normalized, *normalized),
        ).rowcount
        deleted += conn.execute(
            f"DELETE FROM user_source_aliases "
            f"WHERE alias_source IN ({placeholders}) OR target_source IN ({placeholders})",
            (*normalized, *normalized),
        ).rowcount
        deleted += conn.execute(
            f"DELETE FROM source_categories WHERE source IN ({placeholders})",
            normalized,
        ).rowcount
        deleted += conn.execute(
            f"DELETE FROM user_source_categories WHERE source IN ({placeholders})",
            normalized,
        ).rowcount
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
```

- [ ] **Step 8: Run all source-maintenance tests**

Run:

```bash
pytest -q tests/test_source_maintenance.py tests/test_news_schema.py
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

```bash
git add source_categories.py tests/test_source_maintenance.py
git commit -m "feat: purge deleted source metadata"
```

### Task 2: Compose grouped article and metadata deletion in the API

**Files:**
- Modify: `web_server.py:65-72, 5378-5406`
- Create: `tests/test_source_deletion.py`

**Interfaces:**
- Consumes: `delete_source_metadata(conn, sources) -> int` from Task 1
- Preserves: `DELETE /sources/articles` request body `{"sources": string[]}`
- Produces response: `{"ok": true, "sources": string[], "deleted": int, "deleted_sources": int}`

- [ ] **Step 1: Create the endpoint test fixture and failing grouped-deletion test**

Create `tests/test_source_deletion.py` with a temporary application database, a temporary news database, an administrator token, and full source tables. Seed articles for `Primary`, `Variant`, a backend-resolved alias `Legacy`, and `Unrelated`. Seed category/user-category rows and aliases connected to the group, then call:

```python
response = client.delete(
    "/sources/articles",
    headers=admin_headers,
    json={"sources": ["Primary", "Variant"]},
)
```

Assert:

```python
assert response.status_code == 200
assert response.get_json()["deleted"] == 3
assert {
    row[0] for row in news_conn.execute("SELECT id FROM articles")
} == {4}
assert {
    row[0] for row in news_conn.execute("SELECT article_id FROM deleted_articles")
} == {1, 2, 3}
assert news_conn.execute(
    "SELECT 1 FROM source_categories WHERE source IN ('Primary', 'Variant')"
).fetchone() is None
assert news_conn.execute(
    "SELECT 1 FROM user_source_categories WHERE source IN ('Primary', 'Variant')"
).fetchone() is None
assert news_conn.execute(
    "SELECT 1 FROM source_aliases "
    "WHERE alias_source IN ('Legacy', 'Variant') OR target_source IN ('Primary', 'Variant')"
).fetchone() is None
assert news_conn.execute(
    "SELECT 1 FROM user_source_aliases "
    "WHERE alias_source IN ('Legacy', 'Variant') OR target_source IN ('Primary', 'Variant')"
).fetchone() is None
assert news_conn.execute(
    "SELECT 1 FROM source_categories WHERE source = 'Unrelated'"
).fetchone() is not None
```

The fixture must restore `models.DB_FILE`, close `models` connections, reset `web_server._news_conn_local`, and close SQLite connections in `finally`.

- [ ] **Step 2: Run the grouped endpoint test and verify RED**

Run:

```bash
pytest -q tests/test_source_deletion.py::test_delete_source_group_removes_articles_tombstones_and_metadata
```

Expected: FAIL because the endpoint preserves manual/classified source metadata and connected aliases.

- [ ] **Step 3: Update the endpoint to purge metadata explicitly**

Import `delete_source_metadata` from `source_categories`. Change the endpoint tail to disable broad stale cleanup and purge only the resolved group:

```python
result = _delete_article_ids(
    [int(row["id"]) for row in rows],
    deleted_by=g.user_id,
    cleanup_sources=False,
)
try:
    result["deleted_sources"] = delete_source_metadata(conn, sources)
except sqlite3.DatabaseError as exc:
    print(f"[sources] Failed to delete source metadata: {exc}")
    return jsonify({"error": "failed to delete source metadata"}), 500
return jsonify({"ok": True, "sources": sources, **result})
```

- [ ] **Step 4: Run the grouped endpoint test and verify GREEN**

Run:

```bash
pytest -q tests/test_source_deletion.py::test_delete_source_group_removes_articles_tombstones_and_metadata
```

Expected: PASS.

- [ ] **Step 5: Write and run a zero-article endpoint test**

Add a manual `Empty Feed` category with no article, call the same endpoint with `{"sources": ["Empty Feed"]}`, and assert status 200, `deleted == 0`, and no remaining category row.

Run:

```bash
pytest -q tests/test_source_deletion.py::test_delete_source_group_removes_zero_article_source_metadata
```

Expected: PASS after Step 3; this guards the backend behavior needed by the frontend zero-count change.

- [ ] **Step 6: Write and run a metadata failure response test**

Monkeypatch `web_server.delete_source_metadata` to raise `sqlite3.OperationalError("locked")`, call the endpoint for a zero-article source, and assert:

```python
assert response.status_code == 500
assert response.get_json() == {"error": "failed to delete source metadata"}
```

Run:

```bash
pytest -q tests/test_source_deletion.py::test_delete_source_group_reports_metadata_failure
```

Expected: PASS after Step 3.

- [ ] **Step 7: Run API and adjacent deletion tests**

Run:

```bash
pytest -q tests/test_source_deletion.py tests/test_server_stats.py tests/test_news_db_thread_safety.py
```

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

```bash
git add web_server.py tests/test_source_deletion.py
git commit -m "feat: delete complete source groups"
```

### Task 3: Make the frontend action accurately delete a source group

**Files:**
- Modify: `frontend/index.html:2736-2738, 3016-3039`
- Test: `tests/test_access_and_ui_contracts.py`

**Interfaces:**
- Consumes: current grouped row fields `label`, `source`, `article_count`, `sources`, and `alias_target`
- Preserves: request body `JSON.stringify({ sources })`
- Produces: confirmation and success copy that says the source and its articles were deleted

- [ ] **Step 1: Write a failing frontend contract test**

Add:

```python
def test_source_delete_removes_the_group_even_when_it_has_no_articles():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    start = html.index("async function deleteSourceArticles(idx)")
    end = html.index("async function reinitializeSourceLabels()", start)
    block = html[start:end]

    assert "if (!count) return;" not in block
    assert "body: JSON.stringify({ sources })" in block
    assert "删除订阅源" in block
    assert "及其全部文章" in html
```

- [ ] **Step 2: Run the frontend contract and verify RED**

Run:

```bash
pytest -q tests/test_access_and_ui_contracts.py::test_source_delete_removes_the_group_even_when_it_has_no_articles
```

Expected: FAIL because the current handler returns immediately for `article_count === 0` and the copy only promises article deletion.

- [ ] **Step 3: Update tooltip, confirmation, and success copy**

Change the button title to `删除该订阅源及其全部文章`. Remove `if (!count) return;`. Preserve typed confirmation for 20 or more articles and use a normal confirmation otherwise:

```javascript
const confirmText = count >= 20
  ? prompt(`将删除订阅源「${label}」及其 ${count} 篇文章。请输入订阅源名称“${label}”确认。`)
  : (confirm(count
      ? `确定删除订阅源「${label}」及其 ${count} 篇文章？删除后所有用户都不可见。`
      : `确定删除订阅源「${label}」？`) ? label : '');
```

Keep the complete-group request:

```javascript
const sources = row.sources && row.sources.length
  ? row.sources
  : [row.alias_target || row.source];
```

Change the success message to distinguish zero and nonzero article counts while always saying the source was deleted:

```javascript
showSettingsStatus(
  data.deleted
    ? `已删除订阅源「${label}」及其 ${data.deleted} 篇文章`
    : `已删除订阅源「${label}」`,
  'ok',
);
```

- [ ] **Step 4: Run frontend contracts**

Run:

```bash
pytest -q tests/test_access_and_ui_contracts.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 3**

```bash
git add frontend/index.html tests/test_access_and_ui_contracts.py
git commit -m "fix: clarify source group deletion"
```

### Task 4: Final regression verification

**Files:**
- Verify only; modify production files only if a failing regression proves a scoped fix is necessary.

**Interfaces:**
- Verifies all interfaces and constraints from Tasks 1-3.

- [ ] **Step 1: Run focused source and deletion suites**

```bash
pytest -q \
  tests/test_source_deletion.py \
  tests/test_source_maintenance.py \
  tests/test_news_schema.py \
  tests/test_access_and_ui_contracts.py \
  tests/test_server_stats.py \
  tests/test_news_db_thread_safety.py
```

Expected: PASS with no warnings or errors.

- [ ] **Step 2: Run the full test suite**

```bash
pytest -q
```

Expected: PASS. If the complete suite cannot finish because of an environmental dependency, record the exact failing command and output and do not claim complete verification.

- [ ] **Step 3: Check diff quality and repository state**

```bash
git diff --check
git status --short --branch
git log -5 --oneline --decorate
```

Expected: no whitespace errors; only intentional commits on `dev`; working tree clean.

- [ ] **Step 4: Review final behavior against the design**

Confirm from tests and diff that:

- a grouped label deletes primary, variants, and aliases;
- a zero-article label can be deleted;
- global/user categories and connected aliases are removed;
- old article tombstones remain;
- known presets do not reappear without articles;
- later articles recreate known or unknown sources from first-seen defaults; and
- the frontend reload path removes the deleted label from both source lists.

No additional commit is required when the working tree is already clean.
