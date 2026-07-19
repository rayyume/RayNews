"""Frontend contract: aiChat() tries the direct call first and only falls back to the
same-origin /ai/chat relay when the provider blocks the browser-direct call (CORS)."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def _ai_chat_block():
    start = HTML.index("function isDeepSeekModel(")
    end = HTML.index("async function aiSummarize(")
    return HTML[start:end]


def _auto_display_summary_block():
    start = HTML.index("async function autoDisplaySummary(")
    end = HTML.index("function showAIActions(", start)
    return HTML[start:end]


def _translation_update_block():
    start = HTML.index("function applyTranslationUpdate(")
    end = HTML.index("function articleItemHtml(", start)
    return HTML[start:end]


def _run_translation_update(body):
    # Evaluate only translation-update handling with the article cache and overlay
    # dependencies supplied explicitly, keeping this a browser contract test.
    script = f"""
const assert = require('assert');
const vm = require('vm');
const overlay = {{
  dataset: {{}},
  classList: {{ contains: () => false }},
}};
const articleWrap = {{ id: 'articleWrap' }};
const context = {{
  console,
  authToken: 'tok',
  articleBodyCache: {{}},
  articleBodyPromises: {{}},
  document: {{
    getElementById: id => id === 'overlay' ? overlay : (id === 'articleWrap' ? articleWrap : null),
  }},
  fetchArticleDetail: async () => {{ throw new Error('fetchArticleDetail not stubbed'); }},
  renderArticleBody: () => {{ throw new Error('renderArticleBody not stubbed'); }},
  autoDisplaySummary: () => {{ throw new Error('autoDisplaySummary not stubbed'); }},
  _overlay: overlay,
  _articleWrap: articleWrap,
}};
vm.createContext(context);
vm.runInContext({json.dumps(_translation_update_block())}, context);
(async () => {{
{body}
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def _run(body):
    # Minimal browser shims: an in-memory localStorage and a controllable fetch.
    script = f"""
const assert = require('assert');
const vm = require('vm');
const store = {{}};
const context = {{
  console,
  authToken: 'tok',
  localStorage: {{
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => {{ store[k] = String(v); }},
  }},
  URL,
  _store: store,
}};
context.fetch = async () => {{ throw new Error('fetch not stubbed'); }};
vm.createContext(context);
vm.runInContext({json.dumps(_ai_chat_block())}, context);
(async () => {{
{body}
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def _run_auto_display(body):
    # Evaluate only the cache-display function with the smallest browser surface it
    # needs, so this stays a contract test rather than loading the frontend bundle.
    script = f"""
const assert = require('assert');
const vm = require('vm');
const context = {{
  console,
  authToken: 'tok',
  isRestrictedUser: () => false,
  news: [],
  proxyImages: html => html,
  sanitizeTranslatedHtml: html => html,
  syncArticleTitle: () => {{}},
  esc: value => String(value),
  document: {{
    getElementById: () => null,
    querySelector: () => null,
    createElement: () => ({{ style: {{}}, remove: () => {{}} }}),
  }},
}};
context.fetch = async () => {{ throw new Error('fetch not stubbed'); }};
vm.createContext(context);
vm.runInContext({json.dumps(_auto_display_summary_block())}, context);
(async () => {{
{body}
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_cors_friendly_endpoint_uses_direct_and_never_relays():
    _run("""
let relayed = false;
context.fetch = async () => { relayed = true; return { ok: true, json: async () => ({ content: 'x' }) }; };
context.callOpenAiChat = async () => 'direct-answer';
context.callClaudeChat = async () => { throw new Error('wrong path'); };

const out = await context.aiChat(
  { provider_type: 'openai', endpoint: 'https://api.deepseek.com/v1' },
  [{ role: 'user', content: 'hi' }], 100, 0.3);

assert.equal(out, 'direct-answer');
assert.equal(relayed, false);
assert.equal(context._store.aiRelayOrigins, undefined);  // origin not marked
""")


def test_browser_direct_deepseek_request_disables_thinking():
    _run("""
const calls = [];
context.fetch = async (url, opts) => {
  calls.push({ url, body: JSON.parse(opts.body) });
  return { ok: true, json: async () => ({ choices: [{ message: { content: 'ok' } }] }) };
};
const deepseekCfg = { endpoint: 'https://opencode.ai/zen/go/v1', api_key: 'key', model: 'DeepSeek-V4-Flash' };
assert.equal(await context.callOpenAiChat(deepseekCfg, [{ role: 'user', content: 'hi' }], 1024, 0.3), 'ok');
assert.deepEqual(calls[0].body.thinking, { type: 'disabled' });

calls.length = 0;
const openAiCfg = { endpoint: 'https://api.openai.com/v1', api_key: 'key', model: 'gpt-4o-mini' };
await context.callOpenAiChat(openAiCfg, [{ role: 'user', content: 'hi' }], 1024, 0.3);
assert.equal('thinking' in calls[0].body, false);
""")


def test_cors_blocked_endpoint_falls_back_to_relay_and_remembers_origin():
    _run("""
const relayCalls = [];
context.fetch = async (url, opts) => {
  relayCalls.push({ url, body: JSON.parse(opts.body), auth: opts.headers.Authorization });
  return { ok: true, json: async () => ({ content: 'relayed' }) };
};
let directCalls = 0;
context.callOpenAiChat = async () => { directCalls++; throw new TypeError('Load failed'); };
context.callClaudeChat = async () => { throw new Error('unused'); };

const cfg = { provider_type: 'openai', endpoint: 'https://opencode.ai/zen/go/v1' };
const out1 = await context.aiChat(cfg, [{ role: 'user', content: 'hi' }], 8000, 0.3);
assert.equal(out1, 'relayed');
assert.equal(directCalls, 1);                 // tried direct once
assert.equal(relayCalls.length, 1);
assert.equal(relayCalls[0].url, '/ai/chat');
assert.equal(relayCalls[0].auth, 'Bearer tok');
assert.equal(relayCalls[0].body.max_tokens, 8000);
// Origin remembered so the next call skips the doomed direct attempt.
assert.deepEqual(JSON.parse(context._store.aiRelayOrigins), ['https://opencode.ai']);

const out2 = await context.aiChat(cfg, [{ role: 'user', content: 'again' }], 8000, 0.3);
assert.equal(out2, 'relayed');
assert.equal(directCalls, 1);                 // did NOT try direct a second time
assert.equal(relayCalls.length, 2);
""")


def test_real_api_error_is_not_rerouted_through_relay():
    _run("""
let relayed = false;
context.fetch = async () => { relayed = true; return { ok: true, json: async () => ({ content: 'x' }) }; };
// A 4xx/5xx from the provider surfaces as a plain Error, not a TypeError — must propagate.
context.callOpenAiChat = async () => { throw new Error('AI API HTTP 500: boom'); };

await assert.rejects(
  context.aiChat({ provider_type: 'openai', endpoint: 'https://api.deepseek.com/v1' },
                 [{ role: 'user', content: 'hi' }], 100, 0.3),
  err => /AI API HTTP 500/.test(err.message));
assert.equal(relayed, false);                 // did not silently reroute a real error
assert.equal(context._store.aiRelayOrigins, undefined);
""")


def test_network_drop_after_a_prior_direct_success_is_not_relayed():
    # A CORS-friendly endpoint proven to work must not have a later network drop
    # (which may already have reached and billed the provider) silently retried through
    # the relay — that would double-charge.
    _run("""
let relayed = false;
context.fetch = async () => { relayed = true; return { ok: true, json: async () => ({ content: 'x' }) }; };
let directCalls = 0;
context.callOpenAiChat = async () => {
  directCalls++;
  if (directCalls === 1) return 'ok';           // first call succeeds → origin proven CORS-friendly
  throw new TypeError('Load failed');           // second: a genuine network drop
};
const cfg = { provider_type: 'openai', endpoint: 'https://api.deepseek.com/v1' };

assert.equal(await context.aiChat(cfg, [{ role: 'user', content: 'a' }], 100, 0.3), 'ok');
assert.deepEqual(JSON.parse(context._store.aiDirectOkOrigins), ['https://api.deepseek.com']);

await assert.rejects(
  context.aiChat(cfg, [{ role: 'user', content: 'b' }], 100, 0.3),
  err => err instanceof TypeError);
assert.equal(relayed, false);                   // did NOT reroute → no duplicate charge
assert.equal(context._store.aiRelayOrigins, undefined);
""")


def test_relay_verdict_not_persisted_when_the_relay_itself_fails():
    # A one-off outage where BOTH direct and relay fail must not permanently pin a
    # (possibly CORS-friendly) endpoint to the relay path.
    _run("""
context.fetch = async () => { throw new TypeError('Load failed'); };  // relay also down
context.callOpenAiChat = async () => { throw new TypeError('Load failed'); };
const cfg = { provider_type: 'openai', endpoint: 'https://api.deepseek.com/v1' };

await assert.rejects(context.aiChat(cfg, [{ role: 'user', content: 'x' }], 100, 0.3));
assert.equal(context._store.aiRelayOrigins, undefined);  // not persisted on relay failure
""")


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


def test_translation_update_invalidates_closed_cache_and_refreshes_open_article():
    _run_translation_update("""
context.articleBodyCache[41] = { body_html: '<p>stale</p>' };
context.articleBodyPromises[41] = Promise.resolve({ body_html: '<p>stale</p>' });
let detailCalls = 0;
context.fetchArticleDetail = async () => { detailCalls++; return { body_html: '<p>译文</p>' }; };
context.applyTranslationUpdate({ id: 41 });
assert.equal(context.articleBodyCache[41], undefined);
assert.equal(context.articleBodyPromises[41], undefined);
assert.equal(detailCalls, 0);

context._overlay.dataset.articleId = '42';
context._overlay.classList.contains = name => name === 'open';
context.articleBodyCache[42] = { body_html: '<p>stale</p>' };
let rendered = null;
let summaryCalls = 0;
context.fetchArticleDetail = async id => {
  detailCalls++;
  assert.equal(id, 42);
  return { body_html: '<p>译文</p>' };
};
context.renderArticleBody = (wrap, data, id) => { rendered = { wrap, data, id }; };
context.autoDisplaySummary = id => { summaryCalls++; assert.equal(id, 42); };
context.applyTranslationUpdate({ id: 42 });
await Promise.resolve();
await Promise.resolve();
assert.equal(detailCalls, 1);
assert.equal(context.articleBodyCache[42], undefined);
assert.deepEqual(rendered, { wrap: context._articleWrap, data: { body_html: '<p>译文</p>' }, id: 42 });
assert.equal(summaryCalls, 1);
""")
