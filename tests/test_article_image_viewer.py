from pathlib import Path
import subprocess

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


def test_viewer_runtime_opens_resets_traps_focus_and_restores_trigger():
    viewer = between("const lightboxGesture =", "function shareArticle()")
    runtime = r'''
const assert = require('node:assert/strict');

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}

class FakeElement {
  constructor(id, { tabIndex = 0, connected = true } = {}) {
    this.id = id;
    this.tabIndex = tabIndex;
    this.isConnected = connected;
    this.disabled = false;
    this.attrs = new Map();
    this.style = {};
    this.classList = new FakeClassList();
    this.listeners = {};
    this.focusables = [];
    this.focusCount = 0;
  }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  removeAttribute(name) { this.attrs.delete(name); }
  getAttribute(name) { return this.attrs.get(name) || null; }
  hasAttribute(name) { return this.attrs.has(name); }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  querySelectorAll() { return this.focusables; }
  getClientRects() { return this.isConnected ? [{}] : []; }
  focus() { this.focusCount += 1; document.activeElement = this; }
  contains(node) { return node === this || this.focusables.includes(node); }
}

class FakeImage extends FakeElement {
  constructor(id) {
    super(id);
    this.currentSrc = 'https://cdn.example/resolved.jpg';
    this.src = 'https://cdn.example/fallback.jpg';
    this.alt = '图片说明';
    this.complete = true;
    this.naturalWidth = 640;
  }
}

global.HTMLImageElement = FakeImage;
const articleWrap = new FakeElement('articleWrap');
const lb = new FakeElement('lb');
const closeButton = new FakeElement('lbCloseBtn');
const lbStage = new FakeElement('lbStage');
const lbImage = new FakeImage('lbImg');
const trigger = new FakeImage('trigger');
lb.focusables = [closeButton];
const elements = { articleWrap, lb, lbCloseBtn: closeButton, lbStage, lbImg: lbImage };
const documentListeners = {};
global.document = {
  body: { style: {} },
  activeElement: trigger,
  getElementById(id) { return elements[id]; },
  addEventListener(type, handler) { documentListeners[type] = handler; },
};

let lockCalls = 0;
let unlockCalls = 0;
global.lockBodyScroll = () => { lockCalls += 1; };
global.unlockBodyScroll = () => { unlockCalls += 1; };
global.recoverImageLoad = () => { throw new Error('loaded image should not recover'); };

eval(process.argv[1]);

openImageViewer(trigger);
assert.equal(lbImage.style.transform, '', 'opening clears any prior gesture transform');
assert.equal(lbImage.src, trigger.currentSrc);
assert.equal(lbImage.alt, trigger.alt);
assert.equal(lb.classList.contains('open'), true);
assert.equal(lb.getAttribute('aria-hidden'), 'false');
assert.equal(lockCalls, 1);
assert.equal(closeButton.focusCount, 1, 'focus enters the dialog close control');

const tabEvent = { key: 'Tab', shiftKey: false, prevented: false, preventDefault() { this.prevented = true; } };
assert.equal(typeof documentListeners.keydown, 'function', 'dialog installs a Tab trap');
documentListeners.keydown(tabEvent);
assert.equal(tabEvent.prevented, true);
assert.equal(document.activeElement, closeButton);

const shiftTabEvent = { key: 'Tab', shiftKey: true, prevented: false, preventDefault() { this.prevented = true; } };
documentListeners.keydown(shiftTabEvent);
assert.equal(shiftTabEvent.prevented, true);
assert.equal(document.activeElement, closeButton);

closeLightbox();
assert.equal(lb.classList.contains('open'), false);
assert.equal(lb.getAttribute('aria-hidden'), 'true');
assert.equal(unlockCalls, 1);
assert.equal(trigger.focusCount, 1, 'connected focusable trigger regains focus');
'''
    result = subprocess.run(
        ["node", "-e", runtime, viewer],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


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


def test_gesture_surface_uses_centered_transform_origin_without_browser_pinch_zoom():
    stage_css = between('.lb-stage{', '\n.lb img{')
    image_css = between('.lb img{', '\n.lb-close{')
    assert 'touch-action:none' in stage_css
    assert 'transform-origin:center' in image_css
