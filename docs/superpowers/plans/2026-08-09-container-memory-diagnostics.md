# 容器内存诊断与有界治理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把接近 1GB 的容器总内存拆解为可归因指标，并消除 refresh 详情缓存、fetcher 输出和周期对象的无界驻留。

**Architecture:** 共享 `runtime_memory.py` 读取 cgroup 与 `/proc`。web 管理统计和周期采样负责容器级观测，refresh 内部 endpoint 提供其私有缓存指标。随后将详情缓存改成 byte-aware LRU，并将 fetcher 输出和周期数据改成流式/有界保留。

**Tech Stack:** Python 3.12、Linux cgroup v1/v2、`/proc`、pytest。

## Global Constraints

- 不引入 `psutil`、Prometheus 或常驻 `tracemalloc`。
- 不把 cgroup file cache 当作 Python 泄漏。
- 监控不得触发自动重启；本计划不增加 Compose 硬内存限制。
- `/internal/runtime-stats` 只能从 loopback 调用，不经 nginx 暴露。
- 详情缓存同时满足条目和字节上限；任何异常路径都保持 byte counter 准确。

---

### Task 1: 共享 cgroup 与进程内存采集器

**Files:**
- Create: `runtime_memory.py`
- Create: `tests/test_runtime_memory.py`
- Modify: `Dockerfile`（复制新模块）

**Interfaces:**
- `read_cgroup_memory(root="/sys/fs/cgroup") -> dict`
- `read_process_memory(proc_root="/proc") -> list[dict]`
- `runtime_memory_snapshot(...) -> dict`

- [ ] **Step 1: 写失败测试**

用 `tmp_path` 构造 cgroup v2：`memory.current=104857600`、`memory.max=max`、`memory.stat` 含 `anon 62914560/file 31457280/kernel 10485760`；断言字段按整数返回。再构造 v1 的 `memory.usage_in_bytes` 与 `memory.stat` fallback。构造 `/proc/10/status`（Name、VmRSS、Threads）和 `/proc/10/cmdline`，断言进程列表忽略消失/无权限 PID 而不是整体失败。

- [ ] **Step 2: 验证为红**

Run: `python3 -m pytest tests/test_runtime_memory.py -q`

Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现纯读取模块**

```python
def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _read_key_values(path: Path) -> dict[str, int]:
    result = {}
    try:
        for line in path.read_text().splitlines():
            key, value, *_ = line.split()
            result[key] = int(value)
    except (OSError, ValueError):
        return result
    return result
```

v2 输出 `current_bytes/max_bytes/anon_bytes/file_bytes/kernel_bytes/slab_bytes`；v1 使用 `total_rss/total_cache` 等可用字段。进程输出 `pid/name/cmdline/rss_bytes/threads`，按 RSS 降序。

- [ ] **Step 4: 验证并提交**

Run: `python3 -m pytest tests/test_runtime_memory.py -q`

```bash
git add runtime_memory.py tests/test_runtime_memory.py Dockerfile
git commit -m "feat: collect cgroup and process memory metrics"
```

---

### Task 2: 暴露 refresh 私有缓存指标并扩展管理统计

**Files:**
- Modify: `refresh_server.py`, `web_server.py`
- Modify: `tests/test_refresh_server_status.py`（若不存在则放入 `tests/test_review_bug_hardening.py`）
- Modify: `tests/test_server_stats.py`（若不存在则放入现有 server-stats 测试文件）

**Interfaces:**
- `refresh_runtime_stats() -> dict` 返回 `article_cache_items/article_cache_bytes/article_cache_inflight`。
- GET `/internal/runtime-stats`；非 loopback 请求返回 403。
- `admin_server_stats.container` 增加 `memory_breakdown` 与 `processes`，`application.refresh` 增加内部指标或 `{status:"unavailable"}`。

- [ ] **Step 1: 写失败测试**

直接向 `_article_cache` 放入两个 bytes 值，断言 helper 返回精确条数/字节；Handler 路由测试断言非 `127.0.0.1/::1` 拒绝。mock `runtime_memory_snapshot` 和 loopback requests，断言 admin JSON 保留现有字段并追加新字段；refresh 超时只返回 unavailable，不使 endpoint 500。

- [ ] **Step 2: 实现**

refresh helper 在 `_article_cache_lock` 内读取。web 使用 `requests.get("http://127.0.0.1:8081/internal/runtime-stats", timeout=1)`；不得在持有任何 SQLite lock 时请求。复用 Task 1 模块替换/扩展现有 `_container_resource_stats()`，维持旧 API 字段兼容。

- [ ] **Step 3: 验证并提交**

Run: `python3 -m pytest tests/test_runtime_memory.py tests/test_review_bug_hardening.py -q`

```bash
git add refresh_server.py web_server.py tests
git commit -m "feat: expose container memory breakdown and cache metrics"
```

---

### Task 3: 增加周期内存采样与阈值告警

**Files:**
- Modify: `web_server.py`, `.env.example`, `docker-compose.yml`
- Modify: `tests/test_runtime_memory.py`

**Interfaces:**
- `MEMORY_MONITOR_ENABLED` 默认 true；interval 最小 10 秒；warn 默认 768MB。
- `_memory_monitor_loop()` 每周期输出一行 `[memory] {compact-json}`。

- [ ] **Step 1: 写测试**

把 snapshot mock 为 current=800MB、anon=500MB、file=250MB，mock refresh stats 与 `time.sleep`；抽出 `_memory_sample_once()` 便于断言 payload 有 `warning=true`、top processes、cache bytes。低于阈值时 warning=false。采集异常返回带 error 的样本且 loop 不退出。

- [ ] **Step 2: 实现并在启动区启动一个 daemon thread**

每行只保留最多 5 个 RSS 最大进程，使用 `json.dumps(..., separators=(",", ":"), ensure_ascii=False)`。不得在每次样本启动新线程。

- [ ] **Step 3: 验证并提交**

Run: `python3 -m pytest tests/test_runtime_memory.py -q`

```bash
git add web_server.py .env.example docker-compose.yml tests/test_runtime_memory.py
git commit -m "feat: sample and warn on container memory usage"
```

---

### Task 4: 把 refresh 文章详情缓存改为 byte-aware LRU

**Files:**
- Modify: `refresh_server.py`, `.env.example`, `docker-compose.yml`
- Modify: `tests/test_news_search.py` 或新建 `tests/test_refresh_article_cache.py`

**Interfaces:**
- `ARTICLE_DETAIL_CACHE_MAX_ITEMS=256`、`ARTICLE_DETAIL_CACHE_MAX_MB=64`。
- `_get_cached_article(id) -> bytes | None`、`_store_cached_article(id, payload) -> bool`、`_evict_cached_article(id)`、`clear_article_cache()`。

- [ ] **Step 1: 写 LRU 测试**

设置 max_items=2/max_bytes=10：存 a=4、b=4，命中 a 后存 c=4，应驱逐 b；替换 a 时 counter 先扣旧值；清空归零；单条 11 bytes 不缓存；并发重复详情的 single-flight 行为保持。

- [ ] **Step 2: 实现**

使用 `OrderedDict[int, bytes]` 和 `_article_cache_bytes`。所有 helper 自己要求 caller 持 lock或统一在 helper 内加现有 RLock；选择一种并在 docstring 明确，禁止嵌套普通 Lock 死锁。title update/internal evict 都调用统一 helper，不能直接 `.pop()` 绕过计数。

- [ ] **Step 3: 验证并提交**

Run: `python3 -m pytest tests/test_refresh_article_cache.py tests/test_news_search.py -q`

```bash
git add refresh_server.py .env.example docker-compose.yml tests/test_refresh_article_cache.py
git commit -m "fix: bound refresh article detail cache by bytes and items"
```

---

### Task 5: 流式转发 fetcher 输出并有界保存尾部

**Files:**
- Modify: `refresh_server.py`
- Modify: `tests/test_review_bug_hardening.py`

**Interfaces:**
- `_run_fetcher_process(env, timeout) -> {returncode, stdout_tail, stderr_tail}`；tail 每路最多 50 行/16KB。

- [ ] **Step 1: 写子进程测试**

启动测试 Python 子进程输出 5000 行，断言父 helper 实时调用 sink、tail 只含末尾且长度受限；非零退出原样返回；timeout 杀死整个 process group 并返回 timeout 状态。

- [ ] **Step 2: 用 `Popen(start_new_session=True)` + 两个 reader thread/selector 实现**

禁止 `communicate()`/`capture_output=True`。reader 每行立即 `print(f"[fetcher:{stream}] {line}", flush=True)`，同时追加到有界 deque。timeout 使用 `os.killpg` 终止组，wait 后回收。

- [ ] **Step 3: 替换 `run_fetcher` 并验证**

Run: `python3 -m pytest tests/test_review_bug_hardening.py tests/test_streaming_refresh.py -q`

```bash
git add refresh_server.py tests/test_review_bug_hardening.py
git commit -m "fix: stream fetcher logs without unbounded capture"
```

---

### Task 6: 减少 fetcher 周期内完整正文重复引用

**Files:**
- Modify: `fetcher.py`
- Modify: `tests/test_streaming_refresh.py`

- [ ] **Step 1: 增加结构/行为测试**

生成 100 个带大 body 的假消息；mock upsert 记录批次。断言 futures 映射 value 仅为 id；成功批提交后 `stream_batch` 清空；mirror 输入来自 SQLite 最近记录而不是整个 `new_entries`；某批失败时仅该未提交批进入 fallback，已提交批不重复 upsert。

- [ ] **Step 2: 实现**

移除无界 `new_entries`。future 完成后立即删除显式映射引用；已提交 batch 只保留 inserted ids。新增 `_load_recent_articles_for_mirror(conn, limit=NEWS_JSON_MIRROR_LIMIT)`，周期末从 SQLite 生成 mirror。失败批独立收集并在主 streaming loop 后重试一次，provider/fetch 不重跑。

- [ ] **Step 3: 验证并提交**

Run: `python3 -m pytest tests/test_streaming_refresh.py tests/test_fulltext_backfill.py -q`

```bash
git add fetcher.py tests/test_streaming_refresh.py
git commit -m "fix: release fetch cycle payloads after streaming commit"
```

---

### Task 7: 24 小时验证与硬限制决策

- [ ] Run: `python3 -m pytest tests/test_runtime_memory.py tests/test_refresh_article_cache.py tests/test_streaming_refresh.py tests/test_review_bug_hardening.py -q`
- [ ] Run: `python3 -m pytest -q`
- [ ] 构建并运行 Compose，保存启动稳定后 10 分钟基线。
- [ ] 连续运行 24 小时/至少 8 次抓取，导出 `[memory]` 行为 CSV/JSON。
- [ ] 分别绘制 total、anon、file、web RSS、refresh RSS、fetcher RSS；记录每次 fetcher 结束后的回落。
- [ ] 只有 anon/RSS 仍单调增长且超出 1.5× 基线时，才新增后续 tracemalloc 计划；file 增长则按页缓存解释，不当作 Python 泄漏。
- [ ] 验证后单独决定是否设置 Compose `mem_limit`；不要在本提交中顺手加入。

## Definition of Done

- [ ] 1GB 总量可被 anon/file/kernel/进程 RSS 完整解释。
- [ ] 详情缓存、fetcher 输出 tail、周期 payload 均有明确上限。
- [ ] 24 小时记录满足验收或形成带证据的二阶段 profiling 任务。
