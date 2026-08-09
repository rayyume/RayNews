# 文章详情前台恢复卡死修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复浏览器/PWA 长期后台后，翻译更新请求永久挂起并阻塞文章详情的问题，同时保证“2 秒放行”不会跨过一条已经完成的翻译更新。

**Architecture:** `pollTranslationUpdates()` 自身使用 8 秒 `AbortController` 超时，确保单飞 promise 必定结算。新增 `waitForTranslationBaseline()`：首篇详情最多等待基线 2 秒；若等待超时且游标仍为空，则把基线标记为 uncertain，迟到的成功基线必须调用既有 `recoverUncertainTranslationBaseline()` 失效详情缓存。文章正文请求在最多 2 秒后发起，但仍保留自身 8 秒网络超时。

**Tech Stack:** 原生 JavaScript、Node `vm` 契约测试、pytest。

## Global Constraints

- 只修改 `frontend/index.html` 与 `tests/test_ai_relay_frontend.py`。
- 不修改 `sw.js` 或后端 `/ai/translation-updates`。
- `translationUpdatePolling` 继续保证单飞；任何退出路径都必须清理 timer、controller 引用和单飞状态。
- 正常快速网络仍先建立基线，再请求 `/api/news/{id}`。
- 等待超时只能让正文请求继续，不能把迟到的翻译更新永久跨过。
- 验收口径是“详情请求在 2 秒边界后开始”，不是“正文一定在 2 秒内显示”。

---

### Task 1: 给翻译更新轮询增加可重试的硬超时

**Files:**
- Modify: `frontend/index.html`（翻译更新状态区、`pollTranslationUpdates`）
- Modify: `tests/test_ai_relay_frontend.py`（`_translation_update_block` 的 VM context）

**Interfaces:**
- Consumes: `translationUpdatePolling`, `translationUpdatePollPromise`, `translationUpdateCursor`。
- Produces: `TRANSLATION_UPDATES_POLL_TIMEOUT_MS = 8000`；超时后可再次调用的 `pollTranslationUpdates() -> Promise<void>`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_ai_relay_frontend.py` 增加：

```python
def test_translation_update_poll_times_out_and_can_retry():
    script = f"""
const assert = require('assert');
const vm = require('vm');
let calls = 0;
const context = {{
  console, authToken: 'tok', translationUpdateCursor: '',
  translationUpdateBaselineUncertain: false,
  translationUpdatePolling: false, translationUpdatePollPromise: null,
  articleBodyCache: {{}}, articleBodyPromises: {{}},
  articleBodyControllers: {{}}, articleBodyRequestGenerations: {{}},
  document: {{ getElementById: () => null }},
  setTimeout, clearTimeout, AbortController,
  TRANSLATION_UPDATES_POLL_TIMEOUT_MS: 30,
}};
context.fetch = (_url, options) => new Promise((_resolve, reject) => {{
  calls += 1;
  options.signal.addEventListener('abort', () => reject(new Error('aborted')));
}});
vm.createContext(context);
vm.runInContext({json.dumps(_translation_update_block())}, context);
(async () => {{
  await context.pollTranslationUpdates();
  assert.equal(context.translationUpdatePolling, false);
  assert.equal(context.translationUpdateBaselineUncertain, true);
  context.fetch = async () => ({{ok: true, json: async () => ({{items: [], cursor: 'c|0'}})}});
  await context.pollTranslationUpdates();
  assert.equal(calls, 1);
  assert.equal(context.translationUpdateCursor, 'c|0');
}})().catch(e => {{ console.error(e.stack || e); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0, result.stderr or result.stdout
```

- [ ] **Step 2: 验证测试为红**

Run: `python3 -m pytest tests/test_ai_relay_frontend.py::test_translation_update_poll_times_out_and_can_retry -q`

Expected: FAIL/timeout，因为当前 fetch 没有 signal 和超时。

- [ ] **Step 3: 实现最小超时**

在状态区加入：

```js
const TRANSLATION_UPDATES_POLL_TIMEOUT_MS = 8000;
const TRANSLATION_UPDATES_BASELINE_WAIT_MS = 2000;
```

在 `pollTranslationUpdates()` 中为本次调用创建局部 controller/timer：

```js
const pollController = new AbortController();
const pollTimer = setTimeout(
  () => pollController.abort(),
  TRANSLATION_UPDATES_POLL_TIMEOUT_MS,
);
```

把 `signal: pollController.signal` 传给 fetch，并在现有 `finally` 最前面执行 `clearTimeout(pollTimer)`。保留 catch 中建立基线失败时设置 `translationUpdateBaselineUncertain = true` 的逻辑。

- [ ] **Step 4: 补齐所有 VM context**

凡执行 `_translation_update_block()` 的 context 都显式提供 `setTimeout`、`clearTimeout`、`AbortController` 和两个超时常量；不要依赖 `undefined` 的 timer 延迟。

- [ ] **Step 5: 验证为绿并提交**

Run: `python3 -m pytest tests/test_ai_relay_frontend.py -q`

Expected: PASS。

```bash
git add frontend/index.html tests/test_ai_relay_frontend.py
git commit -m "fix: bound translation update polling"
```

---

### Task 2: 有界等待基线且保留迟到恢复语义

**Files:**
- Modify: `frontend/index.html`（新增 `waitForTranslationBaseline`、修改 `fetchArticleDetail`）
- Modify: `tests/test_ai_relay_frontend.py`

**Interfaces:**
- Produces: `waitForTranslationBaseline() -> Promise<void>`。
- Invariant: timer 胜出且游标仍为空时，`translationUpdateBaselineUncertain === true`；迟到的基线成功随后触发缓存恢复。

- [ ] **Step 1: 写两个失败测试**

```python
def test_article_detail_starts_after_bounded_baseline_wait():
    # VM 中令 pollTranslationUpdates 永不结算，wait 常量为 30ms；
    # fetch('/api/news/42') 记录时间并立即返回正文。
    # assert fetchArticleDetail(42) 成功，且详情 fetch 在 20~500ms 内发生。


def test_late_successful_baseline_invalidates_detail_cached_after_wait_timeout():
    # translation-updates fetch 返回一个由测试手动 resolve 的 deferred promise；
    # wait 常量为 20ms。先调用 fetchArticleDetail(42)，确认正文进入 articleBodyCache；
    # 再 resolve 基线为 {items: [], cursor: 'late|0'}。
    # await translationUpdatePollPromise 后断言：
    #   translationUpdateBaselineUncertain == false
    #   translationUpdateCursor == 'late|0'
    #   42 不再位于 articleBodyCache
```

测试必须使用真实 `_translation_update_block()` + `_article_detail_block()`；DOM 可返回关闭状态 overlay，因为 `applyTranslationUpdate()` 在检查 overlay 前已经调用 `invalidateArticleBody(id)`。

- [ ] **Step 2: 验证测试为红**

Run: `python3 -m pytest tests/test_ai_relay_frontend.py::test_article_detail_starts_after_bounded_baseline_wait tests/test_ai_relay_frontend.py::test_late_successful_baseline_invalidates_detail_cached_after_wait_timeout -q`

Expected: 第一个超时；第二个无法观察到 uncertain 恢复/缓存失效。

- [ ] **Step 3: 实现有界等待辅助**

```js
async function waitForTranslationBaseline() {
  if (!authToken || translationUpdateCursor) return;
  let timer = null;
  const timedOut = await Promise.race([
    Promise.resolve(pollTranslationUpdates()).then(() => false, () => false),
    new Promise(resolve => {
      timer = setTimeout(() => resolve(true), TRANSLATION_UPDATES_BASELINE_WAIT_MS);
    }),
  ]);
  if (timer) clearTimeout(timer);
  if (timedOut && !translationUpdateCursor) {
    // The detail request is about to cross an unestablished baseline. Mark it
    // uncertain so a late successful baseline invalidates any body cached now.
    translationUpdateBaselineUncertain = true;
  }
}
```

将 `fetchArticleDetail()` 的无界调用改为：

```js
if (authToken && !translationUpdateCursor) {
  await waitForTranslationBaseline();
}
```

- [ ] **Step 4: 验证正常路径顺序和迟到路径**

Run: `python3 -m pytest tests/test_ai_relay_frontend.py -q`

Expected: PASS；既有 `test_first_detail_waits_for_translation_cursor_baseline` 仍断言正常网络下先轮询、后详情。

- [ ] **Step 5: 提交**

```bash
git add frontend/index.html tests/test_ai_relay_frontend.py
git commit -m "fix: bound article detail baseline wait without losing updates"
```

---

### Task 3: 回归和人工验收

- [ ] Run: `python3 -m pytest tests/test_ai_relay_frontend.py tests/test_article_image_viewer.py tests/test_access_and_ui_contracts.py tests/test_frontend_refresh_behavior.py tests/test_ios_pwa_resume_recovery.py -q`
- [ ] Run: `python3 -m pytest -q`
- [ ] 手测：挂起 `/ai/translation-updates` 后打开文章，确认约 2 秒后 `/api/news/{id}` 已发起；翻译基线迟到成功后确认详情缓存被刷新。
- [ ] 手测：正常网络首次打开文章仍先建立游标，再请求详情。

## Definition of Done

- [ ] 翻译轮询在 8 秒内结算并可重试。
- [ ] 详情请求最多只被基线挡住 2 秒。
- [ ] 迟到成功基线不会跨过已完成翻译。
- [ ] 聚焦测试和全量测试通过。
