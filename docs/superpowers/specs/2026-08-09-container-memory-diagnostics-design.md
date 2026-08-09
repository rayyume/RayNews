# 容器内存诊断与有界治理设计

## 目标

解释容器总内存接近 1GB 的构成，区分进程匿名内存、Linux 文件页缓存和短时 fetcher 峰值；在证据基础上消除无界驻留对象，并使正常负载下的内存回到可预测范围。

## 已确认现状

- 容器常驻 nginx、refresh_server、web_server；refresh_server 每 15 分钟启动一个短生命周期 fetcher。
- `docker stats` 展示容器 cgroup 总量，不能单独证明 Python 泄漏；图片缓存写盘可提高 cgroup file cache。
- refresh_server 的 `_article_cache: dict[int, bytes]` 无条目或字节上限，只在抓取成功后整体清空。
- fetcher 同一周期可能同时保留 messages、future/result、`new_entries`、stream batch 和 JSON mirror 数据。
- refresh_server 使用 `subprocess.run(capture_output=True)`，会把 fetcher 全部 stdout/stderr 保存在内存至进程结束。
- `_purge_tasks` 已有 16 条历史上限；图片预取 queue 已有 3000 上限，不能在没有证据时称为泄漏。
- `/admin/server-stats` 已报告 cgroup 总内存，但没有 anon/file 分解、逐进程 RSS 或应用缓存计数。

## 设计

### 观测

新增无第三方依赖的 `runtime_memory.py`：读取 cgroup v2/v1、`memory.stat` 和 `/proc`，返回 total/anon/file/kernel、容器内各 PID 的 RSS、线程数和命令名。web 管理统计扩展该数据；refresh_server 增加仅 loopback 使用的内部 runtime stats，暴露文章缓存条目/字节/inflight。web 每个采样周期把两者合并输出紧凑 JSON。

环境变量：

- `MEMORY_MONITOR_ENABLED=true`
- `MEMORY_MONITOR_INTERVAL_SECONDS=60`
- `MEMORY_WARN_MB=768`
- `ARTICLE_DETAIL_CACHE_MAX_ITEMS=256`
- `ARTICLE_DETAIL_CACHE_MAX_MB=64`

监控只告警，不触发 kill/restart。24 小时验证前不设置硬内存限制。

### 有界缓存

refresh 详情缓存改为 `OrderedDict` LRU，同时维护精确 `_article_cache_bytes`。命中移动到末尾；写入时扣除旧值、加入新值，并从最旧项开始驱逐到同时满足 item/byte 上限。单个响应大于 byte 上限时直接不缓存。所有变更都在现有 lock 内完成。

### fetcher 峰值

refresh_server 改用 `Popen` 逐行转发 fetcher 输出，仅以 `deque(maxlen=N)` 保留状态尾部，避免完整 capture。fetcher futures 映射只保留 message id；已完成 future 立即从显式引用表移除。成功流式写入的完整 entry 不再为 fallback 和 mirror 无界保留；mirror 在周期末从 SQLite 查询最近 2000 条构建。未提交 batch 是唯一需要保留的 fallback payload。

## 验收

- 连续 24 小时且至少 8 个抓取周期，每分钟有可比较采样。
- 能解释 cgroup 总内存中 anon/file/kernel 和各进程 RSS。
- refresh 文章缓存永远不超过配置的 item/byte 上限。
- fetcher 输出量不再线性增加 refresh_server RSS。
- 每次 fetcher 退出后匿名内存回落；空闲稳定 anon/RSS 不超过启动稳定基线的 1.5 倍。
- 768MB 仅告警；是否增加 Compose 硬限制由验证结果决定。
