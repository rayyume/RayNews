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
    viewer = between("function closeLightbox()", "function shareArticle()")
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
const lbImage = new FakeImage('lbImg');
const trigger = new FakeImage('trigger');
lb.focusables = [closeButton];
const elements = { articleWrap, lb, lbCloseBtn: closeButton, lbImg: lbImage };
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
assert.equal(lbImage.style.transform, 'translate(0px, 0px) scale(1)', 'opening invokes the concrete gesture reset');
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
