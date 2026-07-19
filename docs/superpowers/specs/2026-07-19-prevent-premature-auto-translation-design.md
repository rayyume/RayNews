# Prevent Premature Auto-Translation UI

## Goal

When an article has no completed server-side translation, keep its original text
visible. Show the translating state only for a translation explicitly initiated by
the reader.

## Root Cause

`autoDisplaySummary()` is a historical function name for the detail-page cached-AI
result loader. In addition to displaying cached summaries and translations, it
currently checks `auto_translate_content` and calls `aiTranslate(articleId)` when
no cached translation is available. `aiTranslate()` is the manual workflow and
immediately replaces the article body with `⏳ 翻译中...`.

## Design

- Keep `autoDisplaySummary()` responsible only for rendering completed cached AI
  results: cached summary and cached full-text translation.
- Remove its fallback that calls `aiTranslate(articleId)` based on
  `auto_translate_content`.
- Preserve `aiTranslate()` unchanged for the explicit “翻译” button; that manual
  action continues to display `⏳ 翻译中...` while it runs.
- Do not change server-side automatic translation, cache payloads, settings,
  polling, or API requests.

## Expected Behaviour

| Situation | Detail page behaviour |
| --- | --- |
| Server translation is pending or unavailable | Show the original article; do not start a browser AI request. |
| Server translation is cached and complete | Automatically display the cached translation. |
| Reader presses “翻译” | Start manual translation and show the translating state. |

## Tests

Add a frontend contract test that executes the no-cache path of
`autoDisplaySummary()` with `auto_translate_content` enabled and verifies that
`aiTranslate` is not called and the original body remains intact. Retain the
existing cached-translation behaviour tests.
