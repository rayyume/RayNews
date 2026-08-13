# Top Category Auto-Scroll Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and execute the steps in order.

**Goal:** Make fixed top-category activation return the homepage list to the top without showing a new category at the old scroll offset.

**Architecture:** Add an opt-in navigation mode to `selectFilter()`. The top bar enables it; the sidebar does not. Reuse `preparePageNavigation()`, `scrollPageToTop()`, `applyPageDuringScroll()`, navigation sequence guards, and the existing URL/pending/prefetch lifecycle.

**Tech Stack:** Vanilla JavaScript and pytest-driven Node runtime contracts.

## Task 1: Runtime contracts and implementation

**Files:**
- Modify: `frontend/index.html`
- Modify: `tests/test_frontend_refresh_behavior.py`

- [ ] Add runtime tests for same-category/no-request, parallel prepare/scroll with near-top application, failure restoration, and stale-transition rejection.
- [ ] Run each new test and confirm it fails because auto-scroll navigation is absent.
- [ ] Pass `{ scrollToTop: true }` from the top category bar.
- [ ] Refactor the existing selection-state DOM updates into a helper used when the target list is applied.
- [ ] For page-1 reactivation, scroll only.
- [ ] For target changes, prepare page 1 and scroll concurrently; apply only when data and near-top readiness are both true.
- [ ] On failure, retain the prior list state and restore the previous scroll offset.
- [ ] Run frontend runtime/contracts, then the full suite.
- [ ] Commit the implementation and tests.
