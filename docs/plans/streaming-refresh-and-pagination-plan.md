# 流式刷新（方案 A）+ 翻页快照失效 综合优化开发方案

> 适用版本：v5.0.2 起 · 状态：待开发 · 2026-07

## 1. 背景与问题

两个现象共享同一个根源：**"内容已变更"这一事实，没有同步到所有消费内容快照的路径**。

- **现象一（翻页顶部突现）**：`goToPage()` 走"先渲染预取快照、后台网络校准"策略
  （`frontend/index.html:5758` → `preparePageNavigation:5111` → `applyPageCalibrationWhenActive:5097`）。
  后台刷新产生新文章后，第 2 页的预取快照已过期（第 1 页尾部文章被挤到第 2 页头部），
  而网络校准若晚于 340–640ms 的滚动动画返回，`reconcileVisibleArticles` 就会在动画结束后
  把差异文章插入列表顶端。`loadSince()`（`index.html:5172`）检测到新文章时，
  没有让 `pageMemoryBuffer` / IndexedDB 页缓存 / 预取 Promise 失效。
- **现象二（刷新按钮转圈≈10s 与文章可见解耦）**：刷新按钮绑定整个 fetcher 子进程生命周期
  （`refresh_server.py:252`），而 fetcher 目前"抓完所有列表页 → 抓完所有全文 →
  重写 news.json → **最后一次性** upsert SQLite"（`fetcher.py:966-1071`），
  文章入库发生在任务最后 1-2 秒；页面上更早出现的文章来自独立的增量通道
  （周期刷新 + `loadSince`）。

**决策**：刷新侧采用方案 A（fetcher 流式入库 + 进度上报），并与翻页快照失效机制一起设计，
因为流式入库会让"内容在用户浏览期间变更"发生得更频繁，若不先解决快照失效，会加剧现象一。

## 2. 总体设计

引入两个核心机制：

1. **前端内容纪元 `contentEpoch`**（会话级单调递增计数器）：任何"已知内容集合发生变化"的时刻
   （`loadSince` 检出新文章、流式刷新批次落地、刷新完成强制重载）都 `bumpContentEpoch()`，
   统一失效所有分页快照。翻页从此拿不到跨纪元的陈旧快照。
2. **服务端进度文件**：fetcher 每落一批文章就原子写入进度 JSON；
   `/refresh/status` 在任务 running 时合并进度返回 `new_count_so_far`，
   前端据此显示进度并（在安全条件下）提前应用新文章。

交付拆为三个独立可回滚的 PR，PR1 单独即可修复现象一：

| PR | 层 | 内容 | 依赖 |
|----|----|------|------|
| PR1 | 前端 | contentEpoch 快照失效 + 翻页保鲜 | 无 |
| PR2 | 服务端 | fetcher 流式入库 + 进度文件 + status 透出 | 无 |
| PR3 | 前端 | 刷新进度提示 + 流式提前应用 | PR1、PR2 |

---

## 3. PR1 — 翻页快照失效与保鲜（修复现象一）

### 3.1 contentEpoch 与失效动作

`frontend/index.html`，新增会话级状态：

```js
let contentEpoch = 0;

function bumpContentEpoch() {
  contentEpoch++;
  pageMemoryBuffer.clear();          // 内存页缓冲全部作废
  pagePrefetchPromises.clear();      // 在途预取结果不再被采纳（见 3.2 纪元守卫）
  clearCachedNewsPages();            // IndexedDB 页缓存清空（loadSince 随后会重写第 1 页）
}
```

调用点：

- `loadSince()`（`index.html:5196` 附近）：`added > 0` 时，在累积 `pendingNewItems` 之后立即调用；
- PR3 的流式批次应用点；
- 管理端删除文章等已知会改变分页的入口（如有）。

`clearCachedNewsPages()`：基于现有 IndexedDB 封装（`NEWS_CACHE_STORE`，`index.html:1579-1660`）
新增 store.clear() 的 Promise 封装，失败静默（与现有 `withCacheTimeout` 风格一致）。

### 3.2 纪元守卫：防止在途结果回填陈旧快照

`prefetchNewsPage()`（`index.html:4787`）与 `preparePageNavigation` 的 networkPromise
在发起时捕获 `const epochAtStart = contentEpoch`；写回 `rememberBufferedPage` /
`writeCachedNewsPage` 前校验 `epochAtStart === contentEpoch`，不一致则丢弃
（数据仍可返回给当前调用方使用，只是不落缓存）。

### 3.3 翻页保鲜：已知有新文章时不吃快照

`preparePageNavigation()`（`index.html:5111`）在读取 buffered/cached 之前增加判断：

```js
const mustBeFresh = pendingRelevantCount(activeFilter) > 0;
```

`mustBeFresh` 为 true 时跳过 buffered 与 IndexedDB 快照路径，直接 `await networkPromise`
返回最终数据。由于 `goToPage` 是先拿到数据再启动滚动动画（`index.html:5773-5783`），
滚动开始时内容已是最终版，`onNearTop` 一次渲染到位，事后校准成为 no-op——
用户只看到滚动动画。代价是点击后多一个网络往返（<500ms，发生在动画前，感知弱）。

### 3.4 兜底

保留 `applyPageCalibrationWhenActive` 现有逻辑不变：在 3.1–3.3 生效后，
它的 diff 正常情况下为空；万一仍有差异（例如翻页瞬间恰好有批次落地），
`reconcileVisibleArticles` 已有的 anchor 补偿（`index.html:4364-4370`）负责把视觉跳动最小化。

### 3.5 验收标准

- DevTools Slow 3G 节流下：先触发后台刷新产生新文章（或手工往 DB 插入），
  再点"下一页"——滚动动画结束后列表顶部**不出现**突现插入。
- 无新文章时翻页路径行为与性能与现状一致（快照直出）。

---

## 4. PR2 — fetcher 流式入库 + 进度上报（方案 A 服务端）

### 4.1 fetcher 流式入库（`fetcher.py`）

改造 `run()` 的处理循环（`fetcher.py:998-1010`）：

```python
STREAM_BATCH_SIZE = 5        # 每 5 篇一批
STREAM_BATCH_SECONDS = 2.0   # 或距上次提交超过 2s

conn = init_db()             # 复用现有 WAL + synchronous=NORMAL
conn.execute("PRAGMA busy_timeout=30000")
batch, inserted_total, last_commit = [], 0, time.monotonic()
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {executor.submit(process_message, msg, msg["id"]): msg for msg in messages}
    for future in as_completed(futures):
        try:
            entry = future.result()
            new_entries.append(entry)
            batch.append(entry)
        except Exception:
            failed_count += 1
            ...
        if batch and (len(batch) >= STREAM_BATCH_SIZE
                      or time.monotonic() - last_commit >= STREAM_BATCH_SECONDS):
            upsert_articles(conn, batch)        # 幂等 INSERT OR REPLACE，内部已过滤 deleted_articles
            inserted_total += len(batch)
            batch, last_commit = [], time.monotonic()
            write_fetch_progress(inserted_total, len(messages))
# 收尾：残余 batch 提交一次
```

要点：

- `upsert_articles` 幂等（`INSERT OR REPLACE`，`fetcher.py:108`），末尾对 `new_entries`
  的全量 upsert 可保留（自愈）或跳过已提交部分，二选一，推荐保留（代码改动最小）。
- `last_seen_id` 语义**不变**：仍然只在 `failed_count == 0` 时推进（`fetcher.py:1051-1060`），
  失败消息下轮重试时重新 upsert，无重复风险。
- news.json 仍在全部处理完后一次性重写（消费方 `/api/news` 读 DB，不受影响）。
- 新行为差异：任务中途失败时，已提交批次的文章保留在 DB（真实内容，可接受；现状是全部不可见）。

### 4.2 进度文件

```python
PROGRESS_FILE = OUTPUT_DIR / "fetch_progress.json"

def write_fetch_progress(inserted: int, total: int):
    payload = {"pid": os.getpid(), "inserted": inserted,
               "total_messages": total, "updated_at": int(time.time())}
    tmp = PROGRESS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload)); tmp.replace(PROGRESS_FILE)   # 原子替换
```

fetcher 启动时（进入 `run()`）先写 `inserted=0`，防止读到上一轮残留。

### 4.3 status 透出（`refresh_server.py`）

`get_refresh_job_status_response()`（`refresh_server.py:236`）在
`REFRESH_JOB["status"] == "running"` 时读取进度文件并合并：

```python
progress = _read_fetch_progress()
if progress and progress["updated_at"] >= REFRESH_JOB["started_at"]:
    payload["new_count_so_far"] = progress["inserted"]
```

守卫：`updated_at` 早于本任务 `started_at` 的进度视为陈旧，忽略（fetcher 崩溃/上轮残留）。
终态 `new_count` 仍以现有 ID 差集为准（`refresh_server.py:256-261`），进度值只是过程展示。

### 4.4 验收标准

- 单测（扩展 `tests/test_refresh_jobs.py`，新增 fetcher 批量测试）：
  - 批次提交后 DB 立即可查、`deleted_articles` 过滤仍生效；
  - 进度文件原子写入、格式正确；
  - status 在 running 时返回 `new_count_so_far`，陈旧进度被忽略；
  - 失败任务不推进 `last_seen_id`，重跑后文章齐全无重复。
- 打点：fetcher 日志输出各阶段耗时（列表页抓取 / 全文处理 / news.json / 收尾），
  用于验证首批文章可见时间从 8-15s 降至 3-6s。

---

## 5. PR3 — 刷新进度提示与流式提前应用（方案 A 前端）

### 5.1 按钮进度文案

`pollRefreshJob()`（`index.html:1408`）增加 `onProgress` 回调，把每次轮询的 status 交给
`triggerRefresh`；`new_count_so_far > 0` 时按钮文案改为 `已获取 N 篇`
（复用 `setRefreshRunning` 的 label 节点，不新增 DOM）。

### 5.2 流式提前应用（安全条件 + 节流）

在 `triggerRefresh` 的轮询回调中：

```
条件：refreshView.page === 1 且 window.scrollY <= 4 且 !hasBlockingOverlayOpen()
      且 viewIsCurrent(...)（复用现有守卫，index.html:4399）
节流：距上次应用 ≥ 3000ms 且 new_count_so_far 有增长
动作：fetchNewsPage(1, filter) → applyNewsPage(..., { animate:true, preserveDom:true })
      → bumpContentEpoch()（PR1）→ 更新 latestKnownTimestamp/seenArticleIds（复用 loadSince 的去重逻辑）
```

- 不满足条件时不动 DOM，只更新按钮文案；完成后由现有 `finally` 中的
  `showNewArticlesPrompt()`（`index.html:4454`）给出"有 N 篇新文章"提示条。
- 3s 节流 + `reconcileVisibleArticles` 的 FLIP 动画 + anchor 补偿，避免列表顶部高频抖动。
- `loadSince` 在 `refreshInProgress` 期间**继续保持挂起**（`index.html:5173,5201`），
  刷新期间由刷新流程独占应用权，避免双驱动竞争。
- 翻页会取消客户端刷新流（`goToPage → cancelViewBoundRefreshWork`，spinner 停止、
  服务端任务继续）——维持现状不改，流式落库的文章由后续 60s `loadSince` 通道接手。

### 5.3 完成路径

保持现有完成逻辑（`index.html:4421-4445`：强制 `loadNewsPage` + `consumePendingNewArticles`
+ ✅ toast）不变；toast 文案的 N 使用终态 `new_count`。

### 5.4 验收标准

- mock 一个慢全文源（人为 sleep 15s 的 telegraph 链接）：点击刷新后 3-6 秒内其余文章
  分批出现、按钮显示"已获取 N 篇"，转圈持续到任务结束；列表顶部插入平滑无跳动。
- 刷新期间用户滚动到页中/打开文章浮层：不自动插入，仅完成后出现提示条。
- 无新文章场景：行为与现状一致（转圈 → "已是最新"）。

---

## 6. 风险与边界情况

| 风险 | 应对 |
|------|------|
| 流式批次与翻页并发（刷新中点下一页） | PR1 的 `bumpContentEpoch` 使快照失效 + `mustBeFresh` 走网络；翻页同时取消刷新流（现状语义） |
| SQLite 写锁竞争（fetcher 批量提交 vs 服务端读） | 双方均已 WAL；fetcher 连接加 `busy_timeout=30000`；读方不被写阻塞 |
| 进度文件残留/崩溃 | `updated_at >= started_at` 守卫；fetcher 启动即清零 |
| 顶部多次插入的视觉抖动 | 3s 节流合批 + FLIP 动画 + anchor 补偿；不满足"页 1 顶部"条件绝不动 DOM |
| 任务失败但部分文章已可见 | 可接受（内容真实、幂等重试）；发布说明中注明行为变化 |
| IndexedDB 清空频率上升 | 仅在检出新文章时触发（约每 15 分钟一次 + 手动刷新期间），量级可忽略 |

## 7. 工作量估算与顺序

- PR1：前端 ~120-160 行改动（含测试契约），0.5-1 天。**先行合入，独立解决现象一。**
- PR2：Python ~100-140 行 + 单测，1 天。
- PR3：前端 ~120-180 行，0.5-1 天（依赖 PR1/PR2 合入后联调）。

联调验证：`build.sh` 本地起容器，按 §3.5 / §4.4 / §5.4 清单逐项走查；
fetcher 阶段耗时打点数据留存一周，确认首批可见延迟指标达成后关闭本方案。
