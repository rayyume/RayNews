# Force Non-Thinking AI Requests

## Goal

Prevent RayNews background and manual AI tasks from spending their output budget on
hidden reasoning, which can result in a successful HTTP response with empty visible
content. Keep the title-task token default at 1024 and do not add a settings control.

## Scope

- Apply one consistent non-thinking policy to every RayNews AI call path:
  background title processing, article translation, summaries, source
  classification, daily summaries, connection tests, and browser-direct personal
  AI requests.
- Apply the DeepSeek-specific request parameter only when the configured model is
  recognized as a DeepSeek model.
- Preserve compatibility with other OpenAI-compatible providers and Claude.

## Non-goals

- No server or personal-AI settings-page checkbox.
- No per-user or environment-variable opt-in to thinking mode.
- No increase to the default `AI_TITLE_MAX_TOKENS`; it remains 1024.
- No change to prompts, task batching, or provider credentials.

## Design

### Server request construction

`AIService` determines whether its configured model name contains `deepseek`
(case-insensitive). For an OpenAI-compatible request to such a model, it always
adds:

```json
"thinking": {"type": "disabled"}
```

The setting is not sent for other models or through the native Claude request path,
because it is a DeepSeek OpenAI-compatible extension and unrelated providers may
reject unknown request fields. There is no environment override that can re-enable
thinking.

All server-side features instantiate and use `AIService`, so this single request
construction point covers automatic and manual server-backed jobs.

### Browser-direct personal AI requests

The personal AI configuration already reaches browser-direct manual requests. The
frontend uses the same model-name rule when it constructs OpenAI-compatible request
bodies: DeepSeek model names always receive `thinking: {type: "disabled"}`;
non-DeepSeek and Claude request shapes remain unchanged. This keeps manual requests
consistent with the server policy without exposing an additional user setting.

### Error handling and observability

The existing empty-content error remains in place. It continues to report the
provider `finish_reason` and token budget when a provider returns no visible content.
After this change, recurring errors from a DeepSeek model indicate either a provider
that does not honor the non-thinking parameter or a separate provider/model issue,
not an intentionally enabled RayNews thinking mode.

## Data and compatibility

No database migration or API-schema change is required. Existing personal and system
AI configurations retain their current fields. Calls to OpenAI, Claude, Ollama, and
other non-DeepSeek-compatible models do not gain a new request field.

## Tests

1. Server OpenAI-compatible DeepSeek model request contains disabled thinking.
2. Server OpenAI-compatible non-DeepSeek model request does not contain thinking.
3. Browser direct OpenAI-compatible DeepSeek body contains disabled thinking.
4. Browser non-DeepSeek and Claude paths retain their existing request shapes.
5. Existing empty-content and title-budget tests continue to pass; the full suite
   remains green.

## Deployment and verification

Build and restart the container from the changed source. Confirm a representative
automatic title task and an automatic full-text translation complete with a visible
result. If the OpenCode gateway returns a validation error for `thinking`, retain the
error details and add provider-specific compatibility handling rather than silently
re-enabling reasoning.
