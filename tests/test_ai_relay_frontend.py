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
