# Force Non-Thinking AI Requests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Force all RayNews uses of DeepSeek models into non-thinking mode while keeping the title token default at 1024 and leaving no user-facing toggle.

**Architecture:** Centralize the server policy in `AIService._call_openai`, which is used by background jobs, server-backed manual jobs, and the AI relay. Mirror the same model-name guard in the browser's direct OpenAI-compatible call path; browser calls routed through `/ai/chat` are covered by the server. Never send the DeepSeek-only parameter to Claude or models whose names do not contain `deepseek`.

**Tech Stack:** Python 3, Flask, `requests`, pytest, vanilla JavaScript, Node.js contract tests.

## Global Constraints

- Keep `AI_TITLE_MAX_TOKENS` default at exactly `1024`.
- Do not add a database field, environment switch, API field, or settings-page control for thinking mode.
- For case-insensitive model names containing `deepseek`, OpenAI-compatible requests must include `"thinking": {"type": "disabled"}`.
- Do not attach `thinking` to non-DeepSeek OpenAI-compatible requests or native Claude requests.
- Preserve the existing empty-content error reporting.

---

### Task 1: Enforce non-thinking mode in server AI requests

**Files:**
- Modify: `ai_service.py:164-246`
- Modify: `tests/test_ai_empty_content.py:15-85`

**Interfaces:**
- Consumes: `AIService(model: str, provider_type: str)` and the existing OpenAI-compatible request body.
- Produces: `AIService._call_openai()` request bodies that include `thinking={"type": "disabled"}` only for model names containing `deepseek`.
- Preserves: `_call_claude()` request body with no DeepSeek extension and `TITLE_MAX_TOKENS == 1024` by default.

- [ ] **Step 1: Write the failing server request-body tests**

Add the following tests after `test_openai_normal_content_is_returned` in `tests/test_ai_empty_content.py`:

```python
def test_deepseek_openai_request_forces_non_thinking(monkeypatch):
    captured = _patch_post(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    AIService("key", "https://opencode.ai/zen/go/v1", "deepseek-v4-flash").chat(
        [{"role": "user", "content": "hi"}]
    )
    assert captured["body"]["thinking"] == {"type": "disabled"}


def test_non_deepseek_openai_request_does_not_send_thinking(monkeypatch):
    captured = _patch_post(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    AIService("key", "https://api.openai.com/v1", "gpt-4o-mini").chat(
        [{"role": "user", "content": "hi"}]
    )
    assert "thinking" not in captured["body"]
```

- [ ] **Step 2: Run the new tests and verify the DeepSeek assertion fails**

Run:

```bash
/config/.local/bin/pytest -q tests/test_ai_empty_content.py::test_deepseek_openai_request_forces_non_thinking tests/test_ai_empty_content.py::test_non_deepseek_openai_request_does_not_send_thinking
```

Expected: the DeepSeek test fails with a missing `thinking` key before the production change; the non-DeepSeek test passes.

- [ ] **Step 3: Implement the fixed server policy**

In `ai_service.py`, retain the endpoint-independent model detector:

```python
def _is_deepseek_model(self) -> bool:
    return "deepseek" in (self.model or "").lower()
```

Replace the environment-controlled `_wants_thinking_disabled` method with no method. In `_call_openai`, construct the request body and add the parameter unconditionally for recognized DeepSeek models:

```python
body = {
    "model": self.model,
    "messages": messages,
    "max_tokens": max_tokens,
    "temperature": temperature,
}
if self._is_deepseek_model():
    body["thinking"] = {"type": "disabled"}
```

Pass `json=body` to `requests.post`. Do not modify `_call_claude`, `_empty_ai_content_error`, or `TITLE_MAX_TOKENS`.

- [ ] **Step 4: Run server AI regression tests**

Run:

```bash
/config/.local/bin/pytest -q tests/test_ai_empty_content.py tests/test_title_processing.py tests/test_refresh_jobs.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit the server behavior and tests**

```bash
git add ai_service.py tests/test_ai_empty_content.py
git commit -m "fix(ai): force DeepSeek requests to non-thinking mode"
```

### Task 2: Match browser-direct personal AI requests to server behavior

**Files:**
- Modify: `frontend/index.html:3858-3890`
- Modify: `tests/test_ai_relay_frontend.py:1-55`

**Interfaces:**
- Consumes: `config.model` and `callOpenAiChat(config, messages, maxTokens, temperature)`.
- Produces: browser-direct DeepSeek OpenAI-compatible POST bodies with `thinking: {type: "disabled"}`.
- Preserves: the relay payload schema; relayed calls obtain the policy from server-side `AIService`.

- [ ] **Step 1: Write failing browser request-body contract tests**

Change `_ai_chat_block()` in `tests/test_ai_relay_frontend.py` so it extracts from the new helper declaration through the existing `async function aiSummarize(` boundary:

```python
def _ai_chat_block():
    start = HTML.index("function isDeepSeekModel(")
    end = HTML.index("async function aiSummarize(")
    return HTML[start:end]
```

Add this test after the existing direct-call test:

```python
def test_browser_direct_deepseek_request_disables_thinking():
    _run("""
const calls = [];
context.fetch = async (url, opts) => {
  calls.push({ url, body: JSON.parse(opts.body) });
  return { ok: true, json: async () => ({ choices: [{ message: { content: 'ok' } }] }) };
};
const cfg = { endpoint: 'https://opencode.ai/zen/go/v1', api_key: 'key', model: 'DeepSeek-V4-Flash' };
assert.equal(await context.callOpenAiChat(cfg, [{ role: 'user', content: 'hi' }], 1024, 0.3), 'ok');
assert.deepEqual(calls[0].body.thinking, { type: 'disabled' });
""")
```

Add a matching non-DeepSeek assertion in the same test function or a separate test:

```python
const cfg = { endpoint: 'https://api.openai.com/v1', api_key: 'key', model: 'gpt-4o-mini' };
await context.callOpenAiChat(cfg, [{ role: 'user', content: 'hi' }], 1024, 0.3);
assert.equal('thinking' in calls[0].body, false);
```

- [ ] **Step 2: Run the browser contract test and verify it fails**

Run:

```bash
/config/.local/bin/pytest -q tests/test_ai_relay_frontend.py::test_browser_direct_deepseek_request_disables_thinking
```

Expected: FAIL because `isDeepSeekModel` does not exist and the direct request body lacks `thinking`.

- [ ] **Step 3: Implement browser request construction**

Immediately before `callOpenAiChat` in `frontend/index.html`, add:

```javascript
function isDeepSeekModel(model) {
  return (model || '').toLowerCase().includes('deepseek');
}
```

Inside `callOpenAiChat`, replace the inline serialized object with a body variable:

```javascript
const body = { model: config.model, messages, max_tokens: maxTokens, temperature };
if (isDeepSeekModel(config.model)) body.thinking = { type: 'disabled' };
```

Then send `body: JSON.stringify(body)`. Do not alter `callClaudeChat`, `callAiRelay`, CORS fallback behavior, or settings UI.

- [ ] **Step 4: Run browser and complete regression tests**

Run:

```bash
/config/.local/bin/pytest -q tests/test_ai_relay_frontend.py tests/test_ai_chat_relay.py tests/test_ai_empty_content.py
/config/.local/bin/pytest -q
```

Expected: both commands pass; the full suite may emit the pre-existing `datetime.utcnow()` deprecation warning only.

- [ ] **Step 5: Commit the browser behavior and tests**

```bash
git add frontend/index.html tests/test_ai_relay_frontend.py
git commit -m "fix(ai): disable thinking in direct DeepSeek chats"
```

### Task 3: Verify production build behavior

**Files:**
- No source changes.

**Interfaces:**
- Consumes: built `raynews` service and an existing DeepSeek configuration.
- Produces: verified non-thinking background and manual DeepSeek requests.

- [ ] **Step 1: Build and restart from the committed source**

Run:

```bash
docker compose up -d --build
docker compose ps
```

Expected: `raynews` is running.

- [ ] **Step 2: Verify an automatic task and a personal manual request**

Use the application to trigger one automatic title/translation job and one manual summary or translation with `deepseek-v4-flash`. Inspect logs:

```bash
docker compose logs --since=10m raynews | grep -E '\[auto-title\]|\[auto-translate\]|空内容|finish_reason'
```

Expected: completed tasks have `Updated article` or `Translated article` entries and no new empty-content error for the tested requests.

- [ ] **Step 3: Record any gateway validation response before adding compatibility changes**

If OpenCode rejects the `thinking` request field, retain the exact `AI API HTTP 4xx` message from logs and stop. Do not re-enable thinking or increase token defaults; use the provider response as evidence for a separate compatibility design.

## Plan Self-Review

- Spec coverage: Task 1 covers all server `AIService` call paths, including automatic processing, server-backed manual calls, daily summaries, and relay requests. Task 2 covers browser-direct personal requests. Task 3 covers deployment verification. The fixed 1024 title default and no-toggle/no-database constraints are explicit global constraints.
- Placeholder scan: no incomplete markers or deferred implementation steps are present.
- Type consistency: the Python model guard is `AIService._is_deepseek_model`; the JavaScript guard is `isDeepSeekModel(model)`. Both use case-insensitive model-name matching and write the identical JSON object.
