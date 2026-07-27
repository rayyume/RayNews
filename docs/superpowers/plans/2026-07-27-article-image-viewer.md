# Unified Article Image Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every article cover, body, and translated image open the same full-screen viewer with reliable mobile pinch-to-zoom and pan.

**Architecture:** Replace per-image click listeners with one delegated listener on `#articleWrap`. Keep all viewer lifecycle and Pointer Events gesture state behind focused helpers so dynamic `innerHTML` replacements need no rebinding and every close path resets the transform.

**Tech Stack:** Vanilla HTML/CSS/JavaScript, Pointer Events, pytest source contracts, Node `vm` behavioral tests.

## Global Constraints

- Work on `dev`; line numbers refer to `dev@7fa5b56`.
- Do not enable inline article-page pinch zoom; zoom starts only after opening the full-screen viewer.
- Scale is clamped to `1..4`.
- Closing the viewer must restore the article scroll position and clear every active pointer/transform.
- Do not add a frontend dependency.
- Do not modify article sanitization, image proxying, or image recovery behavior.

---

## File Structure

- Modify: `frontend/index.html` — viewer markup/CSS, delegated click entry, gesture state machine, close/reset lifecycle.
- Create: `tests/test_article_image_viewer.py` — source contracts and Node behavioral tests for viewer math/lifecycle.
- Modify: `tests/test_access_and_ui_contracts.py` — one high-level rendered-markup contract.

### Task 1: Replace the lightbox shell and per-image bindings with one viewer entry

**Files:**
- Modify: `frontend/index.html:227-231`
- Modify: `frontend/index.html:636`
- Modify: `frontend/index.html:4240-4475`
- Modify: `frontend/index.html:7312-7327`
- Create: `tests/test_article_image_viewer.py`

**Interfaces:**
- Produces: `openImageViewer(image: HTMLImageElement): void`
- Produces: `closeLightbox(): void`
- Produces: delegated `click` listener on `#articleWrap`
- Consumes: existing `lockBodyScroll()`, `unlockBodyScroll()`, `recoverImageLoad()`

- [ ] **Step 1: Add the failing source-contract tests**

Create `tests/test_article_image_viewer.py`:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def between(start: str, end: str) -> str:
    left = HTML.index(start)
    right = HTML.index(end, left)
    return HTML[left:right]


def test_viewer_has_accessible_stage_close_button_and_single_delegated_entry():
    assert 'id="lbStage"' in HTML
    assert 'id="lbCloseBtn"' in HTML
    assert 'role="dialog"' in HTML
    assert 'aria-modal="true"' in HTML
    assert "function openImageViewer(image)" in HTML
    assert "articleWrap.addEventListener('click'" in HTML
    delegated = between(
        "articleWrap.addEventListener('click'",
        "\n});",
    )
    assert "closest('img')" in delegated
    assert "openImageViewer(image)" in delegated


def test_dynamic_translation_paths_do_not_bind_image_click_handlers():
    translate = between("async function aiTranslate(", "async function autoDisplaySummary(")
    auto_display = between("async function autoDisplaySummary(", "function showAIActions(")
    render_body = between("function renderArticleBody(", "function formatTime(")
    for block in (translate, auto_display, render_body):
        assert "querySelectorAll('img').forEach" not in block
        assert "img.addEventListener('click'" not in block


def test_viewer_no_longer_mutates_viewport_meta_for_zoom():
    close_block = between("function closeLightbox()", "function shareArticle()")
    assert "meta[name=viewport]" not in close_block
    assert "maximum-scale" not in close_block
```

- [ ] **Step 2: Run the new tests and verify the current implementation fails**

Run:

```bash
python3 -m pytest -q tests/test_article_image_viewer.py
```

Expected: FAIL because `lbStage`, `lbCloseBtn`, `openImageViewer`, and delegated entry do not exist, and repeated image listeners remain.

- [ ] **Step 3: Replace the lightbox markup and CSS**

Replace the current `.lb` rules and markup with:

```html
<div class="lb" id="lb" role="dialog" aria-modal="true"
     aria-label="图片全屏查看" aria-hidden="true">
  <button class="lb-close" id="lbCloseBtn" type="button" aria-label="关闭图片">✕</button>
  <div class="lb-stage" id="lbStage">
    <img src="" id="lbImg" alt="" draggable="false">
  </div>
</div>
```

```css
.lb{position:fixed;inset:0;z-index:600;background:rgba(0,0,0,.92);backdrop-filter:blur(10px);opacity:0;pointer-events:none;transition:opacity .25s;overflow:hidden}
.lb.open{opacity:1;pointer-events:auto}
.lb-stage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;overflow:hidden;touch-action:none;cursor:zoom-out}
.lb img{max-width:92vw;max-height:92vh;border-radius:8px;object-fit:contain;transform-origin:0 0;will-change:transform;user-select:none;-webkit-user-drag:none}
.lb-close{position:absolute;z-index:2;top:calc(12px + env(safe-area-inset-top,0px));right:14px;width:44px;height:44px;border:0;border-radius:50%;background:rgba(0,0,0,.55);color:#fff;font:600 20px/1 var(--font);cursor:pointer;touch-action:manipulation}
```

- [ ] **Step 4: Add the single open function and delegated listener**

Add near the existing lightbox functions:

```js
function openImageViewer(image) {
  if (!(image instanceof HTMLImageElement)) return;
  if (!image.currentSrc && !image.src) return;
  if (!image.complete || image.naturalWidth === 0) {
    recoverImageLoad(image);
    return;
  }
  resetLightboxGesture();
  const lb = document.getElementById('lb');
  const lbImg = document.getElementById('lbImg');
  lbImg.src = image.currentSrc || image.src;
  lbImg.alt = image.alt || '';
  lb.classList.add('open');
  lb.setAttribute('aria-hidden', 'false');
  lockBodyScroll();
}

const articleWrap = document.getElementById('articleWrap');
articleWrap.addEventListener('click', event => {
  const image = event.target.closest('img');
  if (!image || !articleWrap.contains(image)) return;
  event.preventDefault();
  event.stopPropagation();
  openImageViewer(image);
});
```

Remove every `querySelectorAll('img').forEach(...addEventListener('click'...))` block from:

- `aiTranslate()`
- its `applyTranslation()` helper
- `autoDisplaySummary()`
- `renderArticleBody()`

Do not remove lazy-loading/decoding assignments in `renderArticleBody()`; replace the listener loop with:

```js
bodyEl.querySelectorAll('img').forEach(img => {
  img.loading = img.loading || 'lazy';
  img.decoding = 'async';
});
```

- [ ] **Step 5: Run the source-contract tests**

Run:

```bash
python3 -m pytest -q tests/test_article_image_viewer.py
```

Expected: remaining failures mention missing `resetLightboxGesture()` or pointer listener end anchor; the delegated-entry and repeated-binding assertions pass.

- [ ] **Step 6: Commit the unified entry**

```bash
git add frontend/index.html tests/test_article_image_viewer.py
git commit -m "fix(ui): route all article images through one viewer"
```

### Task 2: Implement pinch, pan, clamping, and lifecycle reset

**Files:**
- Modify: `frontend/index.html:227-231`
- Modify: `frontend/index.html:7347-7356`
- Modify: `frontend/index.html:7478-7488`
- Modify: `tests/test_article_image_viewer.py`

**Interfaces:**
- Produces: `clampLightboxScale(value: number): number`
- Produces: `lightboxDistance(a: PointerPoint, b: PointerPoint): number`
- Produces: `clampLightboxOffset(x: number, y: number, scale: number): {x:number,y:number}`
- Produces: `resetLightboxGesture(): void`
- Produces: `applyLightboxTransform(): void`
- Consumes: `#lb`, `#lbStage`, `#lbImg`

- [ ] **Step 1: Add Node-backed failing tests for pure gesture helpers**

Append this helper and tests to `tests/test_article_image_viewer.py`:

```python
import json
import subprocess


def run_node(js: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", js],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


def test_gesture_helpers_clamp_scale_distance_and_offsets():
    helpers = between("function clampLightboxScale(", "function resetLightboxGesture(")
    run_node(
        """
import assert from 'node:assert/strict';
const context = globalThis;
context.document = {
  getElementById(id) {
    if (id === 'lbStage') return { clientWidth: 320, clientHeight: 640 };
    if (id === 'lbImg') return { offsetWidth: 300, offsetHeight: 200 };
    return null;
  }
};
"""
        + helpers
        + """
assert.equal(clampLightboxScale(.2), 1);
assert.equal(clampLightboxScale(2.5), 2.5);
assert.equal(clampLightboxScale(8), 4);
assert.equal(lightboxDistance({x:0,y:0}, {x:3,y:4}), 5);
assert.deepEqual(clampLightboxOffset(999, -999, 1), {x:0,y:0});
const limited = clampLightboxOffset(999, -999, 2);
assert.ok(limited.x <= 140 && limited.x >= -140);
assert.ok(limited.y <= 0 && limited.y >= -320);
"""
    )


def test_close_resets_transform_and_pointer_state_before_unlocking_scroll():
    close_block = between("function closeLightbox()", "function shareArticle()")
    assert "resetLightboxGesture();" in close_block
    assert close_block.index("resetLightboxGesture();") < close_block.index("unlockBodyScroll();")
    assert "aria-hidden', 'true'" in close_block
```

- [ ] **Step 2: Run tests and verify helper failures**

Run:

```bash
python3 -m pytest -q tests/test_article_image_viewer.py
```

Expected: FAIL because the pure helper functions and reset call do not exist.

- [ ] **Step 3: Implement the pure math and state**

Add:

```js
const lightboxGesture = {
  pointers: new Map(),
  scale: 1,
  x: 0,
  y: 0,
  startScale: 1,
  startDistance: 0,
  startCenter: null,
  dragStart: null,
  moved: false,
};

function clampLightboxScale(value) {
  return Math.min(4, Math.max(1, Number(value) || 1));
}

function lightboxDistance(a, b) {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

function clampLightboxOffset(x, y, scale) {
  const stage = document.getElementById('lbStage');
  const image = document.getElementById('lbImg');
  if (!stage || !image || scale <= 1) return { x: 0, y: 0 };
  const width = image.offsetWidth * scale;
  const height = image.offsetHeight * scale;
  const maxX = Math.max(0, (width - stage.clientWidth) / 2);
  const maxY = Math.max(0, (height - stage.clientHeight) / 2);
  return {
    x: Math.min(maxX, Math.max(-maxX, x)),
    y: Math.min(maxY, Math.max(-maxY, y)),
  };
}

function applyLightboxTransform() {
  const image = document.getElementById('lbImg');
  const offset = clampLightboxOffset(
    lightboxGesture.x,
    lightboxGesture.y,
    lightboxGesture.scale,
  );
  lightboxGesture.x = offset.x;
  lightboxGesture.y = offset.y;
  image.style.transform =
    `translate3d(${offset.x}px,${offset.y}px,0) scale(${lightboxGesture.scale})`;
}

function resetLightboxGesture() {
  lightboxGesture.pointers.clear();
  lightboxGesture.scale = 1;
  lightboxGesture.x = 0;
  lightboxGesture.y = 0;
  lightboxGesture.startScale = 1;
  lightboxGesture.startDistance = 0;
  lightboxGesture.startCenter = null;
  lightboxGesture.dragStart = null;
  lightboxGesture.moved = false;
  const image = document.getElementById('lbImg');
  if (image) image.style.transform = '';
}
```

- [ ] **Step 4: Implement Pointer Events**

Register listeners once on `lbStage`. Required behavior:

```js
function lightboxPoint(event) {
  return { x: event.clientX, y: event.clientY };
}

function lightboxPointerPair() {
  return Array.from(lightboxGesture.pointers.values()).slice(0, 2);
}

function onLightboxPointerDown(event) {
  if (!document.getElementById('lb').classList.contains('open')) return;
  event.currentTarget.setPointerCapture(event.pointerId);
  lightboxGesture.pointers.set(event.pointerId, lightboxPoint(event));
  const pair = lightboxPointerPair();
  if (pair.length === 2) {
    lightboxGesture.startDistance = lightboxDistance(pair[0], pair[1]);
    lightboxGesture.startScale = lightboxGesture.scale;
    lightboxGesture.startCenter = {
      x: (pair[0].x + pair[1].x) / 2,
      y: (pair[0].y + pair[1].y) / 2,
    };
    lightboxGesture.dragStart = null;
  } else if (pair.length === 1 && lightboxGesture.scale > 1) {
    lightboxGesture.dragStart = {
      pointer: pair[0],
      x: lightboxGesture.x,
      y: lightboxGesture.y,
    };
  }
}

function onLightboxPointerMove(event) {
  if (!lightboxGesture.pointers.has(event.pointerId)) return;
  lightboxGesture.pointers.set(event.pointerId, lightboxPoint(event));
  const pair = lightboxPointerPair();
  if (pair.length === 2 && lightboxGesture.startDistance > 0) {
    lightboxGesture.scale = clampLightboxScale(
      lightboxGesture.startScale
        * lightboxDistance(pair[0], pair[1])
        / lightboxGesture.startDistance,
    );
    lightboxGesture.moved = true;
    applyLightboxTransform();
    return;
  }
  if (pair.length === 1 && lightboxGesture.dragStart && lightboxGesture.scale > 1) {
    lightboxGesture.x =
      lightboxGesture.dragStart.x + pair[0].x - lightboxGesture.dragStart.pointer.x;
    lightboxGesture.y =
      lightboxGesture.dragStart.y + pair[0].y - lightboxGesture.dragStart.pointer.y;
    lightboxGesture.moved = true;
    applyLightboxTransform();
  }
}

function onLightboxPointerEnd(event) {
  lightboxGesture.pointers.delete(event.pointerId);
  if (lightboxGesture.scale <= 1) {
    lightboxGesture.scale = 1;
    lightboxGesture.x = 0;
    lightboxGesture.y = 0;
    applyLightboxTransform();
  }
  const pair = lightboxPointerPair();
  lightboxGesture.startDistance = 0;
  lightboxGesture.startCenter = null;
  lightboxGesture.dragStart = pair.length === 1 && lightboxGesture.scale > 1
    ? { pointer: pair[0], x: lightboxGesture.x, y: lightboxGesture.y }
    : null;
}

const lbStage = document.getElementById('lbStage');
lbStage.addEventListener('pointerdown', onLightboxPointerDown);
lbStage.addEventListener('pointermove', onLightboxPointerMove);
lbStage.addEventListener('pointerup', onLightboxPointerEnd);
lbStage.addEventListener('pointercancel', onLightboxPointerEnd);
```

- [ ] **Step 5: Make close behavior gesture-safe**

Replace `closeLightbox()` with:

```js
function closeLightbox() {
  const lb = document.getElementById('lb');
  lb.classList.remove('open');
  lb.setAttribute('aria-hidden', 'true');
  resetLightboxGesture();
  document.getElementById('lbImg').removeAttribute('src');
  unlockBodyScroll();
}
```

Wire close behavior:

```js
document.getElementById('lbCloseBtn').addEventListener('click', event => {
  event.stopPropagation();
  closeLightbox();
});
lbStage.addEventListener('click', event => {
  if (event.target !== event.currentTarget || lightboxGesture.moved) {
    lightboxGesture.moved = false;
    return;
  }
  closeLightbox();
});
```

Ensure `finishArticleClose()` calls `closeLightbox()` first when the viewer is open.

- [ ] **Step 6: Run viewer tests**

Run:

```bash
python3 -m pytest -q tests/test_article_image_viewer.py
```

Expected: PASS.

- [ ] **Step 7: Commit gesture support**

```bash
git add frontend/index.html tests/test_article_image_viewer.py
git commit -m "feat(ui): add pinch and pan to article image viewer"
```

### Task 3: Add regression contracts and complete verification

**Files:**
- Modify: `tests/test_access_and_ui_contracts.py:1138-1145`
- Modify: `tests/test_article_image_viewer.py`

**Interfaces:**
- Consumes: all viewer interfaces from Tasks 1–2
- Produces: final automated and manual evidence

- [ ] **Step 1: Add the high-level UI contract**

Append to `tests/test_access_and_ui_contracts.py`:

```python
def test_article_cover_body_and_translation_images_share_one_fullscreen_viewer():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert 'id="articleWrap"' in html
    assert 'id="lbStage"' in html
    assert "function openImageViewer(image)" in html
    assert "articleWrap.addEventListener('click'" in html
    assert html.count("img.addEventListener('click'") == 0
    assert ".lb-stage{" in html and "touch-action:none" in html
```

- [ ] **Step 2: Run the focused frontend tests**

Run:

```bash
python3 -m pytest -q \
  tests/test_article_image_viewer.py \
  tests/test_access_and_ui_contracts.py \
  tests/test_ai_relay_frontend.py
```

Expected: PASS.

- [ ] **Step 3: Run the full suite**

Run:

```bash
python3 -m pytest -q
```

Expected: PASS with no new warning category.

- [ ] **Step 4: Perform browser verification**

Use the Browser plugin if available; otherwise run Playwright and record that fallback. Verify:

1. Open an article whose `thumb` is not duplicated in `body_html`; click the cover.
2. Click the first and last body images.
3. Switch to translated content and click a translated image.
4. On an iOS/WebKit-sized touch context, pinch from 1× to at least 2×, pan, then close.
5. Reopen and confirm the image starts at 1× with zero offset.
6. Close while the article is scrolled and confirm its scroll position is unchanged.
7. Confirm a drag end does not close the viewer.

- [ ] **Step 5: Commit final contracts**

```bash
git add tests/test_access_and_ui_contracts.py tests/test_article_image_viewer.py
git commit -m "test(ui): cover unified article image interactions"
```

## Completion Gate

Do not mark this plan complete until focused tests, the full suite, and the three-platform interaction matrix pass. Record any unavailable physical-device check explicitly instead of claiming it was performed.
