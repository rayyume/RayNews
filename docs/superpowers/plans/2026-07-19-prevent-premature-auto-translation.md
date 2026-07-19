# Prevent Premature Auto-Translation UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the original article visible until a server translation is complete, while retaining the translating state for reader-initiated manual translations.

**Architecture:** Restrict `autoDisplaySummary()` to rendering completed cached AI results. Remove its fallback into `aiTranslate()`, which is the separate manual action and owns the translating placeholder. Verify the detail-page no-cache path with a Node-backed frontend contract test.

**Tech Stack:** Vanilla JavaScript, Node.js contract tests, pytest.

## Global Constraints

- Do not change server-side automatic translation, AI cache payloads, settings, polling, or API requests.
- A missing or pending cached translation must leave the original article body visible and must not call `aiTranslate`.
- A completed cached translation must continue to auto-display.
- The reader’s explicit “翻译” action must retain `⏳ 翻译中...` behaviour.

---

### Task 1: Stop automatic detail-page fallback into manual translation

**Files:**
- Modify: `frontend/index.html:4176-4280`
- Modify: `tests/test_ai_relay_frontend.py`

**Interfaces:**
- Consumes: `autoDisplaySummary(articleId)`, `/ai/result/<id>`, and `aiTranslate(articleId)`.
- Produces: a no-cache detail-page path that leaves the rendered original body untouched and never calls `aiTranslate`.
- Preserves: cached translation auto-display and the manual `aiTranslate()` workflow.

- [ ] **Step 1: Add a failing no-cache frontend contract test**

Extend `tests/test_ai_relay_frontend.py` with a test harness that extracts the
`autoDisplaySummary` function and provides minimal DOM and fetch shims. Add a test
whose `/ai/result/42` response is `{}`, whose `userAutoSettings` has
`auto_translate_content: true`, and whose `aiTranslate` increments a counter:

```python
def test_pending_auto_translation_keeps_original_body_and_does_not_call_manual_translate():
    _run_auto_display("""
const body = { dataset: {}, innerHTML: '<p>English original</p>', querySelectorAll: () => [] };
context.document.getElementById = id => id === 'articleBody' ? body : (id === 'articleWrap' ? { querySelector: () => null, prepend: () => {} } : null);
context.fetch = async () => ({ json: async () => ({}) });
context.userAutoSettings = { auto_translate_content: true };
let manualCalls = 0;
context.aiTranslate = async () => { manualCalls++; };
await context.autoDisplaySummary(42);
assert.equal(manualCalls, 0);
assert.equal(body.innerHTML, '<p>English original</p>');
""")
```

The helper must use Node `vm`, supply `authToken`, `isRestrictedUser`, `news`,
`proxyImages`, `sanitizeTranslatedHtml`, `syncArticleTitle`, and `esc` shims as
needed by the extracted function. It must not run the entire frontend bundle.

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```bash
/config/.local/bin/pytest -q tests/test_ai_relay_frontend.py::test_pending_auto_translation_keeps_original_body_and_does_not_call_manual_translate
```

Expected: FAIL because the current implementation calls `aiTranslate(42)`.

- [ ] **Step 3: Remove the incorrect automatic manual-translation fallback**

Delete the entire block at the end of `autoDisplaySummary()` beginning with:

```javascript
// Auto-trigger translation if setting enabled and title is all English
```

and ending at its matching closing brace immediately before `function showAIActions`.
Do not alter the preceding `data.translation` cache-rendering branches or
`aiTranslate()`.

- [ ] **Step 4: Run focused and full regression tests**

Run:

```bash
/config/.local/bin/pytest -q tests/test_ai_relay_frontend.py
/config/.local/bin/pytest -q
```

Expected: both commands pass; the full suite may emit only the existing
`datetime.utcnow()` deprecation warning.

- [ ] **Step 5: Commit the frontend behaviour fix**

```bash
git add frontend/index.html tests/test_ai_relay_frontend.py
git commit -m "fix(ui): keep original text while auto translation is pending"
```

## Plan Self-Review

- Spec coverage: the only implementation task removes the no-cache automatic manual translation trigger while preserving both completed-cache display and explicit manual translation.
- Placeholder scan: no incomplete markers or deferred implementation steps are present.
- Type consistency: the test calls `autoDisplaySummary(articleId)` and observes the existing `aiTranslate(articleId)` interface.
