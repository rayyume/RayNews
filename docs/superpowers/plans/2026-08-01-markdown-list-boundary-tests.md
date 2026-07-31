# Markdown List Boundary Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the intended boundaries of loose Markdown list parsing without changing renderer behavior.

**Architecture:** Extend the existing Node-backed test that extracts `renderMarkdownListBlocks` and `renderMarkdown` from `frontend/index.html`. The parser remains unchanged; assertions verify that its current list boundaries match the documented behavior.

**Tech Stack:** Python 3, pytest, Node.js, inline JavaScript assertions.

## Global Constraints

- Modify tests only; do not alter `frontend/index.html`.
- Preserve the existing normalized ordered-list semantics: source marker values are not emitted as HTML `value` attributes.
- Run the focused test and the complete pytest suite before committing.

---

### Task 1: Cover loose-list boundaries

**Files:**
- Modify: `tests/test_frontend_refresh_behavior.py:752-775`

**Interfaces:**
- Consumes: `run_node(source, body)` and `source_between(start, end)` test helpers.
- Consumes: `renderMarkdown(text: string): string` extracted from `frontend/index.html`.
- Produces: regression assertions for loose ordered-list boundaries.

- [ ] **Step 1: Add boundary assertions to the existing Markdown-list test**

Append these assertions after the existing `spacedOrdered` assertion:

```javascript
const orderedThenBullets = context.renderMarkdown('1. first\\n\\n2. second\\n\\n- bullet');
assert.equal(orderedThenBullets, '<ol><li>first</li><li>second</li></ol></p><p><ul><li>bullet</li></ul>');

const orderedThenParagraph = context.renderMarkdown('1. first\\n\\n2. second\\n\\nA paragraph');
assert.equal(orderedThenParagraph, '<ol><li>first</li><li>second</li></ol></p><p>A paragraph');

const skippedSourceNumber = context.renderMarkdown('1. first\\n\\n3. third');
assert.equal(skippedSourceNumber, '<ol><li>first</li><li>third</li></ol>');
```

- [ ] **Step 2: Run the focused regression test**

Run:

```bash
python3 -m pytest -q tests/test_frontend_refresh_behavior.py -k markdown_lists_keep_unordered_bullets_out_of_ordered_lists
```

Expected: one passing test. If expected HTML differs, update only the expected string to reflect the real generic paragraph-rendering behavior; do not change production code.

- [ ] **Step 3: Run the complete test suite**

Run:

```bash
python3 -m pytest -q
```

Expected: zero failures.

- [ ] **Step 4: Commit the regression coverage**

```bash
git add tests/test_frontend_refresh_behavior.py
git commit -m "test: cover markdown list boundaries"
```
