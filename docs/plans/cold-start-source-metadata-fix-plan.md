# 冷启动来源元数据加载失败修复计划

> 状态：待开发
> 日期：2026-07-17
> 症状：部署后 PWA / 网页冷启动时，文章来源标签显示为原始 feed 名（不准确），左侧抽屉各分类下没有订阅源按钮；进入管理员菜单 → 订阅源管理，等所有订阅源列表加载出来后界面恢复正常。

## 一、根因分析（已确认）

两个症状同源：冷启动时 `loadSourceCategories()` 未能拿到真实的来源元数据，`rebuildCategoryMap()` 没有以服务端数据执行，导致 `sourceMeta = {}`、`sourceRows = []`。

- **标签不准确**：`sourceLabel()`（`frontend/index.html:1791`）在 `sourceMeta` 为空时退回显示原始 feed 名，别名合并、缩写标签、手工修正全部失效。
- **抽屉分类为空**：`renderFilters()`（`frontend/index.html:5509`）的来源按钮完全由 `sourceRows`（经 `rebuildSourceFilterGroups()`）构建；内置的 `CATEGORY_MAP` 种子（`frontend/index.html:958`）只提供分类名兜底，不产生按钮，因此只剩空的分类标题。

冷启动加载有两道防线，都容易失败：

1. **IndexedDB 缓存读取被限死 500ms**：`loadSourceCategories()` 里 `withCacheTimeout(readNewsCacheEntry('source-metadata'))`（`frontend/index.html:1745`），`withCacheTimeout` 默认 `timeoutMs = 500`（`frontend/index.html:5177`）。PWA 冷启动首次打开 IndexedDB 常超过 500ms（与 Service Worker 启动、首页文章请求竞争），超时即视为无缓存——即使缓存里有上次的完整元数据。
2. **网络请求 8 秒硬超时 + 服务端慢**：`loadSourceCategories` 用 AbortController 8s 超时（`networkTimeoutMs = 8000`，`frontend/index.html:1735`）。而服务端 `GET /sources`（`web_server.py:3143`）→ `source_rows()`（`source_categories.py:563`）**每次调用**都执行：
   - `ensure_article_sources()`（`source_categories.py:321`）：对 articles 全表按每个别名跑 UPDATE + DISTINCT 全表扫描 + INSERT 种子；
   - `cleanup_stale_source_categories()`（`source_categories.py:356`）：LEFT JOIN articles 全表分组统计 + 多个 DELETE。

   部署刚完成时抓取任务在全量刷新、持有 SQLite 写锁，该请求极易超 8 秒被 abort。`/api/sources`（`refresh_server.py:799`，游客路径）同样调用 `source_rows()`，问题相同。
3. **失败静默且不重试**：bootstrap 传 `quietNetworkError: true`（`frontend/index.html:6932`）；`apiFetch` 遇 401 返回 `{error:'auth_expired'}` 对象不抛错（`frontend/index.html:1261`），`data.sources` 缺失时静默保留降级状态，之后没有任何自动重试。
4. **修复路径不写缓存**：管理员标签页 `loadSourcesTab()`（`frontend/index.html:2255`）请求同一个 `/sources` 但**无超时**，慢也能等到结果并调用 `rebuildCategoryMap()` + `renderFilters()`，所以能"修好"；但它**不回写** IndexedDB 的 `source-metadata` 缓存（只有 `loadSourceCategories` 网络成功路径会写，`frontend/index.html:1767`），下次冷启动问题重现。

## 二、修复任务

按优先级排序，任务 1、2 是主修复，3～5 是加固。每个任务独立可提交。

### 任务 1（后端，收益最大）：让 `GET /sources` 变快——去掉每次请求的全表维护

**文件**：`source_categories.py`、`web_server.py`、`refresh_server.py`

1. 把 `source_rows()`（`source_categories.py:563`）中的 `ensure_article_sources(conn)` 和 `cleanup_stale_source_categories(conn)` 从每次调用中拆出：
   - 新增 `maintain_source_categories(conn)` 封装这两步；`source_rows()` 只做只读查询（保留 unlinked 来源的补充查询，它是只读的，可以留下）。
   - 兼容性检查：`source_rows()` 依赖 `init_source_categories` 建表。给 `source_rows()` 保留一个轻量兜底——查询前若 `source_categories` 表不存在则调用 `init_source_categories(conn)`（廉价的 `CREATE TABLE IF NOT EXISTS` 路径），避免全新部署首次请求报错。
2. 在正确的时机调用 `maintain_source_categories`：
   - 抓取周期结束后（`refresh_server.py` 中 refresh job 完成处，找 `start_refresh_job` 对应的执行完成回调/收尾处）；
   - `web_server.py` 的 `_auto_source_classification_loop`（`web_server.py:2638`）已有每 10 周期的 cleanup，把 ensure 合并进同一节流点；
   - 写路径（保存来源、合并别名、重新识别等修改了 articles/source_categories 的接口）保持原有调用不变——逐个检查 `web_server.py` 中现有 `ensure_article_sources` 调用点（`web_server.py:2471`、`web_server.py:3711`、`web_server.py:3942`、`web_server.py:4074`），只改只读的 `GET /sources` / `GET /api/sources` 路径。
3. 进程内节流保护：给 `maintain_source_categories` 加时间戳节流（如 60s 内最多执行一次，用模块级变量 + lock），防止并发触发。
4. **验收**：抓取刷新进行中（SQLite 有写锁竞争）时 `GET /sources` 响应时间 < 1s；刷新结束后新发现的来源仍会出现在 `/sources` 结果里（由收尾维护补齐）。

### 任务 2（前端）：冷启动缓存读取不再被 500ms 掐掉

**文件**：`frontend/index.html`

1. `loadSourceCategories()`（`frontend/index.html:1732`）读取 `source-metadata` 缓存时，把超时放宽为参数（如 `cacheTimeoutMs = 5000`），或干脆直接 `await readNewsCacheEntry(...)` 不加竞速——`readNewsCacheEntry` 自身 catch 后返回 null，不会挂死；注意不要影响其他调用 `withCacheTimeout` 的文章分页路径（保持它们 500ms 不变）。
2. 缓存命中后立即 `rebuildCategoryMap` + `renderFilters`（现有逻辑已做，确认顺序不回退）。
3. **验收**：DevTools 中模拟 IndexedDB 首次打开慢（或冷启动 + CPU 6x throttling），只要本地有缓存，抽屉和标签立即正确渲染，不出现空分类。

### 任务 3（前端）：网络失败后自动重试，成功路径统一回写缓存

**文件**：`frontend/index.html`

1. `loadSourceCategories()` 网络请求被 abort / 失败后，安排一次退避重试（如 10s 后重试一次、再 30s 一次，最多 2 次；页面 `document.hidden` 时跳过）。重试成功后走现有 `rebuildCategoryMap → renderFilters → writeNewsCacheEntry` 路径。注意用模块级 flag 防止多次 bootstrap 叠加重试。
2. `loadSourcesTab()`（`frontend/index.html:2255`）成功拿到 `data.sources` 后，同样 `writeNewsCacheEntry({ key: 'source-metadata', kind: 'metadata', updatedAt: Date.now(), data })`，与 `frontend/index.html:1767` 保持同一记录结构。可抽一个 `persistSourceMetadata(data)` helper 两处共用。
3. 401（`auth_expired`）时不要静默：若 `isRestrictedUser()` 为假但拿到 `auth_expired`，降级改走游客端点 `fetch('/api/sources')` 再试一次（该端点无鉴权，`refresh_server.py:868`）。
4. **验收**：断网加载页面 → 恢复网络后 30s 内抽屉自动恢复；在管理员订阅源页加载成功后，硬刷新（清 memory 不清 IndexedDB）冷启动直接显示正确标签。

### 任务 4（前端，可选加固）：8s 超时分级

`loadSourceCategories` 的 `networkTimeoutMs` 冷启动首轮保持 8s（保证首屏不被拖死），但重试轮次放宽到 20–30s，容忍部署后服务端仍在刷新周期内的慢响应。与任务 3 的重试实现合并做。

### 任务 5（回归确认）：来源深链路径

`bootstrapNews({ forceSourceMetadata })` 的 `?source=` 深链流程（`frontend/index.html:6902`）依赖 `onMetadataReady` 时序。任务 2/3 改动后确认：

- 缓存命中即 resolve `sourceMetadataPromise` 的行为不变（`frontend/index.html:1751`）；
- 深链失败重试 `retrySourceDeepLink()`（`frontend/index.html:6890`）仍能在元数据迟到后成功解析。

## 三、测试与验证

- **后端**：`tests/` 下补充 `source_rows` 只读化的单测（调用后不产生写事务；`maintain_source_categories` 幂等；节流生效）。运行现有测试套件确认无回归（先看 `tests/` 里已有的 source_categories 相关用例）。
- **前端手工验证脚本**（无自动化测试框架时按此清单过）：
  1. 清空站点数据 → 首次访问：抽屉分类下有来源按钮，卡片标签为缩写；
  2. 模拟 `GET /sources` 延迟 15s（代理或临时 sleep）：首屏用缓存渲染正确；无缓存时降级但 30s 内自动恢复；
  3. PWA 安装后杀进程冷启动 ×3：标签与抽屉每次都正确；
  4. 管理员 → 订阅源管理加载后，检查 IndexedDB `raynews-news-cache-v1/entries` 中 `source-metadata` 的 `updatedAt` 已更新。
- **部署场景复现**：部署新版本后立即（抓取周期进行中）冷启动访问，确认症状不再出现——这是原始 bug 的完整复现路径。

## 四、注意事项

- SQLite 并发：`maintain_source_categories` 的写事务要短，避免与抓取写入互相拖锁；沿用现有连接获取方式（`web_server.py` 的 `_get_news_db()` / `refresh_server.py` 的 `get_db()`）。
- `web_server.py`（8082, Flask）和 `refresh_server.py`（8081, http.server）是**两个进程**，进程内节流各自独立即可，不需要跨进程协调；维护动作幂等，重复执行只是浪费不是错误。
- 前端是单文件 `frontend/index.html`（约 7200 行），改动保持现有代码风格（无构建步骤、原生 JS）。
- Service Worker（`frontend/sw.js`）对 `/api/` 已是 network-first + 缓存兜底，本计划不需要动 SW；`/sources`（8082 路径）不经过 SW 的 API 分支，走"everything else: network-first"分支，同样不用动。
