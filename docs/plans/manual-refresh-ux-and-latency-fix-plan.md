# 手动刷新异常与耗时优化修复方案

状态：待开发。本方案供开发 agent 直接执行，含根因分析、具体改动点（文件 + 行号，行号基于 dev@914dd6c）、验收标准与测试要求。

## 待修复的异常清单

1. 在非第一页浏览时点右上角"刷新"，会弹出报错 `load failed`；刷新进行中点"下一页"，按钮变灰长时间不可点（且翻页会把刷新流程静默取消）。→ 症状 1-A / 1-B
2. 刷新按钮运行态右上角的蓝点会遮挡按钮文字（现在运行态文字是"更新中"/"+N"），要求去掉蓝点、保留从左往右的潮汐（sweep）动画。→ 症状 2
3. 手动刷新经常要 10 秒以上才结束。→ 症状 3（主方案 P0.5：点击秒出已有增量）
4. 非第一页手动刷新出新文章时，页面无任何变化、也不弹"有 N 篇新文章"气泡，只有一条 toast。→ 症状 1-C
5. iOS PWA 从后台切前台（或被杀后重开）仍显示很久之前的旧文章。→ 症状 4

---

## 症状 2：蓝点遮挡文字（根因明确，改动最小，先做）

### 根因

`frontend/index.html:100`：

```css
.refresh-btn.refresh-running::after{content:'';position:absolute;right:7px;top:5px;width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 3px rgba(110,142,251,0.18)}
```

这个 `::after` 蓝点是早期设计（当时运行态只有潮汐动画、无文字变化）。现在 `setRefreshRunning()` / `setRefreshProgressLabel()`（index.html:4676、4691）会把按钮文字换成"更新中"或 `+N`，胶囊按钮本来就窄（移动端 `padding:3px 8px;font-size:10px`，index.html:298），右上角的点加上 3px 的 box-shadow 光晕直接压在文字上。

### 修复

- 删除 index.html:100 整条 `.refresh-btn.refresh-running::after` 规则。
- 保留 index.html:99 的 `::before` sweep 动画和 `@keyframes refreshSweep`（index.html:108），不动。
- 检查 `.refresh-btn.refresh-running span{position:relative;z-index:1}`（index.html:101）仍需保留（让文字浮在 sweep 渐变之上），不要顺手删掉。

### 测试

在 `tests/test_frontend_refresh_behavior.py` 加一条契约测试（该文件就是对 index.html 做字符串断言的模式，见文件头部）：

```python
def test_refresh_running_state_has_no_dot_overlay_but_keeps_sweep():
    assert '.refresh-btn.refresh-running::after' not in HTML
    assert '.refresh-btn.refresh-running::before' in HTML
    assert 'refreshSweep' in HTML
```

---

## 症状 1：非第一页刷新报 "load failed" + 翻页按钮变灰

### 根因分析（两个独立缺陷叠加）

**1a. `load failed` 是 Safari/WebKit 的原始网络错误文案被直接透传给了用户。**

- Safari（iOS PWA / macOS Safari）的 `fetch` 网络层失败抛 `TypeError: Load failed`（Chrome 是 `Failed to fetch`，Firefox 是 `NetworkError when attempting to fetch resource`）。
- `refreshErrorMessage()`（index.html:1316-1338）末尾有 `if (detail) return detail;`（index.html:1336）——任何没被前面模式匹配到的原始错误文案会原样透传，于是 toast 显示 `❌ Load failed`。
- 更关键的是 `isTransientRefreshError()`（index.html:1360-1363）的正则 `/network|failed to fetch|timeout|temporar|HTTP 5\d\d/i` **匹配不到 Safari 的 `Load failed`**，所以 Safari 上的瞬时网络失败不会走 `retryTransientRefreshRequest()` 的重试，一次抖动就让整个刷新流程报错——尽管服务端 job 其实还在正常跑。
- 为什么刷新期间容易触发：手动刷新会让后端进入高负载（fetcher 子进程 15 个线程并发抓全文 + SQLite 流式写入），前端在 10-30 秒内每 1.2s 轮询一次 `/auth/refresh/status`，任何一次 POST `/auth/refresh` 或轮询请求被丢/被 iOS 挂起网络就中断整个流程。非第一页与此没有已证实的因果（第一页时流式插入的视觉更新可能掩盖了部分失败），修复不区分页码。

**1b. 翻页按钮长时间灰掉：刷新期间导航被迫走纯网络请求、超时上限 12 秒。**

链路：

1. `goToPage()`（index.html:6083）进入即 `setPageNavigationPending(true)` → `renderPagination()` 把上下页按钮都 `disabled`（index.html:6052-6054），直到 `preparePageNavigation()` 返回才恢复。
2. 刷新期间流式批次落地会 `bumpContentEpoch()`（index.html:5036），清空 pageMemoryBuffer / prefetch / IndexedDB 页缓存；同时 `pendingRelevantCount() > 0` 使 `mustBeFresh = true`（index.html:5438）。
3. `mustBeFresh` 路径（index.html:5439-5446）完全跳过缓存，只等网络响应，AbortController 超时 12 秒（index.html:5415）。此刻后端正被抓取任务打满，`/api/news` 响应慢——按钮就灰 3~12 秒；超时后 toast"连接超时"，停留原页。
4. 额外问题：`goToPage()` 第一行 `cancelViewBoundRefreshWork()`（index.html:6091）会把刷新流程整个 abort 掉（`cancelRefreshFlow()`，index.html:1443），按钮立刻恢复"刷新"字样，但服务端 job 仍在跑——用户视角是"翻页把刷新弄没了"，且之后 job 完成也不再有任何提示。

### 修复

**A. 错误文案归一 + 把浏览器网络错误纳入瞬时重试（index.html）**

1. `refreshErrorMessage()`：在 `if (detail) return detail;` 之前加一条浏览器网络错误映射：

```js
if (/^(load failed|failed to fetch|networkerror)/i.test(detail)) {
  return '网络连接失败，请检查网络后重试';
}
```

2. `isTransientRefreshError()` 的正则扩为：`/network|failed to fetch|load failed|timeout|temporar|HTTP 5\d\d/i`。
3. `requestRefreshStatus()`（index.html:1387）目前只重试一次。改为在 `pollRefreshJob()` 的循环里对瞬时错误**不中断轮询**：单次状态查询失败（瞬时类）时记录连续失败次数，连续 ≥3 次才抛错，否则等下一轮继续。job 是服务端异步的，轮询丢一两拍不应该报"刷新失败"。

**B. 刷新与翻页解耦（index.html）**

1. `goToPage()` 不再调用 `cancelViewBoundRefreshWork()` 取消刷新流程。刷新 job 的 DOM 应用已有 `viewIsCurrent()` 守卫（index.html:4585-4593，含 `pageNavigationSequence` / `currentPage` / `filter` 比对），翻页后守卫自然失效，完成时只弹 toast + `showNewArticlesPrompt()`，不会错误改写 DOM。需要保留的取消场景（切 filter、登出等）逐一确认调用点后再决定是否保留 `cancelViewBoundRefreshWork()` 本身。
   - 验证点：翻页后刷新完成时走 index.html:4638 的 `refreshView.page === 1` 分支判断——非第一页本来就跳过 reload，行为安全。
2. `preparePageNavigation()` 的 `mustBeFresh` 路径加"降级预算"：网络请求与 2.5s 计时赛跑；超预算且存在 buffered/cached 快照（即便 epoch 过期，标注为临时）时先应用快照让用户翻过去，网络返回后用已有的 `applyPageCalibrationWhenActive()`（index.html:5393）校准。若无任何快照则维持现状（等网络/超时提示）。
3. 翻页按钮灰化时间随之缩短到 ≤2.5s；不改 disabled 机制本身（防连点仍需要）。

**C. 非第一页手动刷新出新文章时，弹出与后台自动刷新一致的"新文章"气泡（index.html）**

现状缺口：`triggerRefresh()` 完成路径的 `finally` 里已经调用 `showNewArticlesPrompt()`（index.html:4671），但气泡依赖的 `pendingNewItems` 队列只由周期性 `loadSince()` 填充（index.html:5520），手动刷新链路从未写入——所以非第一页刷新后，用户只看到一条 toast，列表不变、气泡不出现，新文章无从进入视野。

修复（复用现有机制，不新造状态）：

1. 在 `triggerRefresh()` 中，`pollRefreshJob()` 返回 `status.status === 'completed'` 且 `Number(status.new_count) > 0`、而第一页应用分支（index.html:4638 的 `refreshView.page === 1` 判断）**未执行**时，调用 `await loadSince(sinceCursor)`。
   - 此时 `refreshInProgress` 仍为 true，`loadSince()` 自动走 defer 路径（index.html:5526）：仅填充 `pendingNewItems`、`bumpContentEpoch()` 使旧页面快照失效、更新今日计数，不触碰当前 DOM——正是所需行为。
   - 随后 `finally` 里现成的 `setRefreshRunning(false)` + `showNewArticlesPrompt()` 会弹出"有 N 篇新文章，点击查看"气泡；用户点击走现有 `revealPendingLatest()`（滚回顶部 + 强制加载第一页），不点击则继续留在当前页。与后台自动刷新体验完全一致。
2. `sinceCursor` 在 `triggerRefresh()` 开头、发起 job 之前捕获：`latestKnownTimestamp || latestNewsTimestamp()`。注意在非第一页时 `latestNewsTimestamp()` 取的是当前页（较旧）的最新时间戳，会把"刷新前已存在于第一页、但用户没见过"的文章也计为新——可接受（对用户而言确实是没看过的内容），且 `loadSince` 的 `seenArticleIds` 去重与 `INCREMENTAL_FETCH_LIMIT` 上限兜底。气泡计数与 toast 的 `new_count` 可能不一致，以气泡自身的 `pendingRelevantCount()`（按当前分类过滤）为准，无需强行对齐。
   - 与 P0.5 叠加后：完成时这次 `loadSince` 应使用 `latestKnownTimestamp || sinceCursor`（取较新者）——P0.5 的立即增量检查若已消化部分文章并推进了 `latestKnownTimestamp`，完成时不必用旧 cursor 重拉；即便重拉，`seenArticleIds` 也保证不重复计数。
3. 调用需接受 `flowIsCurrent()` 守卫（翻页/切分类已使流程失效时跳过），失败静默（`loadSince` 内部已 catch 返回 0，不影响完成 toast）。

### 测试

- `tests/test_frontend_refresh_behavior.py` 增加契约断言：
  - `triggerRefresh` 源码段（`async function triggerRefresh()` 到 `function setRefreshRunning`）包含 `loadSince(`，且位于 `new_count` 判断之后；
  - `'load failed'` 出现在 `isTransientRefreshError` 的正则里；
  - `refreshErrorMessage` 源码段包含网络错误映射文案；
  - `goToPage(` 到 `waitForScrollTop` 之间不再包含 `cancelViewBoundRefreshWork()`（或按最终实现调整）；
  - `preparePageNavigation` 段包含降级预算逻辑标识（如 `NAVIGATION_FRESH_BUDGET_MS`）。
- 该文件已有 `run_node()` 基建可以用 Node vm 跑提取出的纯函数：把 `refreshErrorMessage` / `isTransientRefreshError` 提出来跑单测，覆盖 `TypeError: Load failed`、`Failed to fetch`、`HTTP 502`、`fetcher already running` 等输入。

---

## 症状 3：手动刷新 10 秒以上

### 关键路径耗时拆解（根因）

后端 job 模型本身是对的（`start_refresh_job()` 立即返回 job_id，refresh_server.py:318），慢在 job 内部与轮询节奏：

| 阶段 | 位置 | 量级 |
|---|---|---|
| 首次状态轮询前的固定等待 | index.html:1429（先 `abortableDelay(1200)` 再查） | +1.2s 固定 |
| 轮询粒度 | 同上，每轮 1.2s | 完成后平均 +0.6s 尾延迟 |
| 子进程冷启动 | refresh_server.py:146 `subprocess.run(["python3", "/app/fetcher.py"])` | 每次 0.5~1.5s（解释器 + requests/bs4 导入） |
| Telegram 分页抓取 | fetcher.py:954 循环，串行 `requests.get`（timeout 20s） | 每页 1~3s，至少 1 页 |
| 逐条全文抓取 | fetcher.py:1073，15 线程抓 Telegraph/微信，单条 timeout 20s | **有新文章时的主要成本**，长尾由最慢的一条决定 |
| news.json 全量重写 | fetcher.py:1103-1137：读全部历史 + 合并 + 排序 + `indent=2` 序列化含 body_html 的**全部累计条目** | 随历史无限增长，可达数秒 CPU+IO |
| 末尾重复 upsert + 全表 source 同步 | fetcher.py:1157-1164（流式已经插过一遍）`upsert_articles(..., sync_sources=True)` → `ensure_article_sources` 全表扫描 | 1~3s |
| 收尾 source 维护 | refresh_server.py:171 `maintain_source_categories(conn, force=True)` | ~1s |
| 全表 id 快照 ×3 | refresh_server.py:142、288、294（`SELECT id FROM articles` 全表） | <1s，顺手优化 |

结论：即使"无新消息"，固定成本（首轮询 1.2s + 子进程 1~1.5s + 一页 Telegram 1~3s + 收尾）就有 4~6 秒；有新文章时全文抓取和 news.json 重写把总时长推到 10~30 秒。

### 修复（按投入产出排序）

**P0.5 体验重构：点击后先秒出已有增量，抓取转后台（主方案，收益最大）**

前提事实：refresh_server 每 15 分钟自动抓一轮（refresh_server.py:33 `REFRESH_INTERVAL = 900`、:426 `periodic_refresh`），所以用户点"刷新"时 SQLite 里通常已有其未见过的新文章。让用户等一轮完整抓取才看到内容，是慢感的根源。改法（全部在 index.html 的 `triggerRefresh()`）：

1. 点击后**立即**执行一次 `loadSince(cursor, { manual: true })`（cursor 取法同 1-C）：DB 里已有的新文章 ≤1s 内呈现——第一页顶部直接应用，其余场景弹"新文章"气泡。多数点击的诉求在这一步就被满足。与发起 job 的 POST 并行执行，互不等待。
2. **`refreshInProgress` 全程保持 true 直到 job 终态，按钮运行态（"更新中"/`+N`/潮汐动画）也全程保留**——它就是"job 进行中"的指示器，且 `handleRefreshProgress` 的进度标签（index.html:4692 `if (!refreshInProgress) return;`）、1-C 完成时 `loadSince` 的 defer 路径、防重入守卫（index.html:4561）都依赖这个标志。**不要**为了"解锁按钮"提前清它。"不阻塞"的含义是：用户浏览、翻页（1-B）、气泡（下条）全程不受影响，而不是按钮提前恢复可点。
3. 为此需要两处配套改动（这是 P0.5 与现状的真正冲突点，缺一不可）：
   - `loadSince()` 新增 `manual: true` 选项：跳过 index.html:5526 的 `deferForRefresh || refreshInProgress` 早退，使第一页顶部场景能立即应用 DOM（其余守卫——`atLatestTop`、overlay 检查——照常生效）。不传该选项的调用（周期定时器、前台恢复）行为不变。
   - `showNewArticlesPrompt()` 移除 index.html:5170 的 `if (refreshInProgress) return;` 早退：否则 job 运行的 10~30 秒内气泡被压制，立即增量检查在非第一页发现的新文章要等到 job 结束才可见，P0.5 的意义就没了。`triggerRefresh` 开头已有 `hideNewArticlesPrompt()`（index.html:4595），移除守卫不会造成气泡与按钮状态互踩。
4. job 终态到达时维持现有收尾：第一页顶部静默应用/其余弹气泡（1-C），toast「✅ 更新完成，新增 N 篇」或错误提示。job 运行期间再次点击按钮 = 无操作（`refreshInProgress` 守卫）。
5. 感知延迟从"10~30s 等终态"降为"~1s 出增量"；后端瘦身（P2）仍做，把 job 实际时长压下来，使气泡/终态 toast 更快到达。
6. 与 1-B 的相互作用：立即增量检查发现新文章时会 `bumpContentEpoch()`，使刷新期间翻页更容易落入 `mustBeFresh` 纯网络路径——1-B 的降级预算因此成为 P0.5 的前置依赖（commit 顺序已保证）。

**P1 前端节奏（纯 index.html，收益 1.5~2.5s 且零风险）**

1. `pollRefreshJob()`：首轮立即查询（把循环改为"先查后等"或首轮 delay 300ms），后续轮询间隔降为 800ms。注意 1-A-3 的轮询容错也改同一个函数——两处改动都动 `pollRefreshJob()` 的循环体，实现时一并设计，避免 commit 2 与 commit 5 互相返工。
2. 完成后的 `loadNewsPage(1, { forceNetwork: true })`（index.html:4648）保持不变——正确性优先。

**P2 fetcher.py 关键路径瘦身（主要收益）**

1. **news.json 出关键路径**：SQLite 已是 source of truth（news.json 仅在 DB 为空时被 `migrate_news_json` 用作 bootstrap，fetcher.py:186-200）。改动：
   - 序列化去掉 `indent=2`（fetcher.py:1136）；
   - items 截断为最近 N 条（建议 2000，够 bootstrap 用）；
   - 整个 merge+write 移到 `save_state` 之后执行，失败仅 log 不影响 job 结果。
2. **删掉末尾重复 upsert**：fetcher.py:1157-1164 的 `upsert_articles(conn, new_entries)` 改为只跑一次 `ensure_article_sources(conn)`（流式循环已用 `sync_sources=False` 插完全部数据，见 fetcher.py:1088 注释）。
3. **全文抓取限时**：为 `process_message` 的 Telegraph/微信请求引入独立的 `FULLTEXT_TIMEOUT = 10`（fetcher.py:656、759），替代全局 20s。失败条目现有逻辑会保留 `last_seen_id` 下次重试（fetcher.py:1143-1147），语义不变。
4. refresh_server.py:171 的 `maintain_source_categories(conn, force=True)` 改为 `force=False`，吃 source_categories.py 里已有的节流（分类循环也会跑它，双保险存在）。

**P3 可选（收益中等、风险稍高，单独评估后再做）**

1. 子进程改为 refresh_server 内线程直调 `fetcher.run()`（省 1~1.5s 冷启动）。注意 fetcher 目前依赖 `subprocess.run(timeout=120)` 做硬超时，改线程后需自行实现超时与隔离，且 stdout/stderr 报告逻辑（refresh_server.py:151-156）要重写。**默认不做**，除非 P1+P2 后实测仍 >8s。
2. refresh_server.py 三处全表 id 快照合并/改 COUNT。

**P0 前置：先测量再动手**

开发 agent 动 P2 前，先在 `fetcher.run()` 里给各阶段加 `log.info` 计时（telegram 抓取 / 全文抓取 / news.json 写入 / 收尾 upsert），跑一次手动刷新，把基线时间贴进 PR 描述；改完再跑一次做对比。这一步不可跳过——上表的量级是代码推断，需要实测确认瓶颈排序。

### 测试

- `tests/test_source_maintenance.py` / `tests/test_frontend_refresh_behavior.py` 现有用例全绿。
- 新增断言：fetcher 源码不再含 `indent=2` 写 news.json；`pollRefreshJob` 段包含新轮询间隔常量。
- P0.5 契约断言：`triggerRefresh` 段包含 `manual: true` 的 `loadSince` 调用且位于 `requestRefreshOnce` 附近（并行发起）；`loadSince` 函数签名含 `manual` 选项且 defer 早退带 `!manual` 条件；`showNewArticlesPrompt` 段不再含 `if (refreshInProgress) return;`。
- 手工验收：无新消息时手动刷新 ≤5s 出"已是最新"；有新消息时按钮 `+N` 进度正常、完成 toast 正常。
- P0.5 手工验收：服务端已有未见新文章时（等一轮 15 分钟自动抓取后再点），第一页顶部点击刷新 ~1s 内新文章插入列表；非第一页点击刷新 ~1s 内弹出气泡，job 仍在后台跑、按钮保持"更新中/+N"直至终态。

---

## 症状 4：iOS PWA 从后台切前台（或重开）后仍显示很久之前的旧文章

### 根因分析

前台恢复的即时检查机制**存在**（`onReturnToForeground()`，index.html:7142，同时监听 `visibilitychange` 与 `focus`，恢复时立即 `loadSince(cursor)`，另有 60s 定时兜底，index.html:7115），但有三个静默失效点：

1. **单次尝试、失败即吞。** iOS 恢复 PWA 瞬间网络栈常未就绪（1~3s），`loadSince()` 的 catch 吞掉所有错误返回 0（index.html:5551-5553），无重试无提示；之后只能等 60s 定时器，且 iOS 对刚恢复页面的 timer 有节流。
2. **Service Worker 把网络失败伪装成"没有新文章"。** sw.js 对 `/api/news` 是 network-first、失败回退旧缓存响应（sw.js:91-105，缓存键仅剔除 `t` 参数，sw.js:17-27）。恢复瞬间网络失败 → SW 返回旧的 200 响应 → `loadSince` 正常返回 0，前端连"失败"都感知不到。
3. **PWA 被 iOS 杀掉后重开走冷启动而非 resume**：先渲染 IndexedDB 旧快照再网络校准，校准失败或被第 2 点的 SW 旧响应欺骗时，静默保留旧内容。用户描述的"显示上次打开时的文章"多为此路径。

### 修复

1. **SW 回退响应打标**（sw.js）：缓存回退分支返回前给 Response 加自定义头 `X-SW-Fallback: 1`（重建 Response 或 `new Response(cached.body, …)` 加 header）。API_CACHE 写入时原样存，仅回退路径打标。
2. **前端识别回退响应**（index.html）：`loadSince()` 与冷启动校准路径（`loadNewsPageRequest` 的网络分支）检查 `resp.headers.get('X-SW-Fallback')`——命中时视同网络失败进入重试，而不是当作最新数据。
3. **前台恢复加退避重试**：`onReturnToForeground()` 里的 `loadSince` 失败（含 SW 回退命中）后按 1.5s / 4s 各重试一次；重试期间页面再次隐藏则放弃（复用 `document.hidden` 检查）。冷启动校准同样加一轮短重试（`loadNewsPage` 已有 `networkRetries` 参数可用，index.html:5284）。
4. **可感知性**：冷启动校准最终失败时 toast「内容可能不是最新，下拉或点击刷新重试」，不再完全静默。

### 测试

- `tests/test_frontend_refresh_behavior.py`：断言 sw.js 回退分支含 `X-SW-Fallback`；断言 `loadSince` 源码段检查该头；断言 `onReturnToForeground` 段含重试逻辑标识。
- 手工验收（iOS PWA）：飞行模式下切后台→切前台→关飞行模式，数秒内应自动拉到新文章或弹气泡，而不是停留旧内容。

---

## 实施顺序与提交建议

1. commit 1：症状 2（CSS 一行删除 + 测试）。
2. commit 2：症状 1-A（错误映射 + 瞬时重试 + 轮询容错）。
3. commit 3：症状 1-B（翻页与刷新解耦 + mustBeFresh 降级预算）与 1-C（非第一页刷新后弹新文章气泡）。
4. commit 4：症状 3-P0.5（点击秒出增量 + 抓取转后台）。依赖 commit 3 的 1-C 气泡链路，须在其后。
5. commit 5：症状 3-P0/P1/P2（先提交测量日志与基线，再提交瘦身改动）。
6. commit 6：症状 4（SW 回退打标 + 前台恢复/冷启动重试）。

每个 commit 独立可回滚；1-B 与 3 涉及行为变化，需按仓库惯例跑 `tests/` 全量并手工过一遍：登录 → 翻到第 2 页 → 点刷新 → 刷新中翻页 → 等完成，覆盖 Safari（iOS PWA）与 Chrome。

## 风险与回滚点

- 1-B 移除 `cancelViewBoundRefreshWork()` 调用：若发现翻页后刷新完成错误改写列表 DOM，说明 `viewIsCurrent()` 守卫有遗漏，回滚该 commit 即可，其余不受影响。
- P2-1 截断 news.json：仅影响"DB 被清空后从 news.json bootstrap"的极端场景，截断到 2000 条足够；如担心，可在 bootstrap 日志里加告警。
- P2-3 缩短全文超时可能提高单条失败率，但失败条目会在下轮重试，不丢数据。
- P0.5：`refreshInProgress` 全程保持 true、服务端 `start_refresh_job()` 对 running 状态幂等返回同一 job（refresh_server.py:320-321），不会产生并发抓取。风险点集中在两处配套改动——`loadSince` 的 `manual` 选项若误改默认路径会影响周期检查/前台恢复（契约测试须覆盖"不传 manual 行为不变"）；`showNewArticlesPrompt` 移除 `refreshInProgress` 守卫后若气泡与流式 `+N` 应用出现竞争（第一页顶部同时满足两条路径），以 `applyStreamedRefreshBatch` 现有的 `consumePendingNewArticles`（index.html:4726）收敛为准。出现问题回滚该 commit 即可恢复阻塞式行为。
- 症状 4 的 SW 打标：只在回退分支加响应头，正常网络路径不变；旧版本 SW 与新版前端共存的过渡期（SW 更新有延迟）里，前端拿不到该头时行为等同现状，不会更糟。
