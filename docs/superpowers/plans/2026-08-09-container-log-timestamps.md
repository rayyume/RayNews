# 容器日志统一时区时间戳 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 不依赖 `docker compose logs --timestamps`，容器所有业务日志行都以 Compose `TZ` 的 ISO 8601 时间和服务名开头。

**Architecture:** 通用 stdin filter 在 Supervisor 进程边界统一前缀；entrypoint 自己加前缀；nginx access/error 同样走 filter。镜像显式提供 tzdata，Compose 的 TZ 可覆盖。

**Tech Stack:** Python 标准库、Bash、Supervisor、Nginx、pytest。

## Global Constraints

- 输出格式固定：`YYYY-MM-DDTHH:MM:SS±HH:MM [service] message`。
- 每行立即 flush；traceback 每行独立加前缀。
- pipeline 任一进程失败必须使 Supervisor program 非零退出。
- 容器停止必须终止整个 pipeline/process group。
- nginx 不得同时保留 `$time_local` 造成双时间戳。

---

### Task 1: 实现并测试 timestamp filter

**Files:**
- Create: `timestamp_filter.py`
- Create: `tests/test_timestamp_filter.py`
- Modify: `Dockerfile`

**Interfaces:**
- `format_timestamped_line(service, line, now=None) -> str`
- CLI: `python3 -u /app/timestamp_filter.py SERVICE`

- [ ] **Step 1: 写失败测试**

```python
def test_formats_iso_timestamp_with_offset():
    now = datetime(2026, 8, 9, 20, 15, 32,
                   tzinfo=timezone(timedelta(hours=8)))
    assert format_timestamped_line("web", "hello\n", now=now) == (
        "2026-08-09T20:15:32+08:00 [web] hello\n"
    )


def test_cli_prefixes_every_traceback_line(tmp_path):
    result = subprocess.run(
        [sys.executable, "timestamp_filter.py", "web"],
        input="Traceback:\n  frame\nValueError: x",
        text=True, capture_output=True, cwd=ROOT,
        env={**os.environ, "TZ": "Asia/Shanghai"},
    )
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    assert all(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d \[web\] ", x) for x in lines)
```

- [ ] **Step 2: 实现**

使用 `datetime.now().astimezone()`，不要硬编码 `+08:00`。逐行读取 `sys.stdin`，保留消息是否以 newline 结束，写后 flush。CLI service 只允许 `[A-Za-z0-9_-]{1,32}`，非法返回 2。

- [ ] **Step 3: 安装时区数据并复制脚本**

Dockerfile apt 包增加 `tzdata`，设置 `ENV TZ=Asia/Shanghai` 作为镜像 fallback；Compose 仍可覆盖。

- [ ] **Step 4: 验证并提交**

Run: `python3 -m pytest tests/test_timestamp_filter.py -q`

```bash
git add timestamp_filter.py tests/test_timestamp_filter.py Dockerfile
git commit -m "feat: add timezone-aware container log prefixer"
```

---

### Task 2: Supervisor 包装 web/refresh/nginx 且保持退出语义

**Files:**
- Modify: `supervisord.conf`
- Modify: `tests/test_container_config.py`（若不存在则新建）

- [ ] **Step 1: 写配置测试**

解析文本断言三个 command 均包含 `/bin/bash -o pipefail -c` 和正确 service 参数；`stopasgroup/killasgroup=true` 保留；stdout/stderr 仍指向 fd 1/2。增加集成 shell 测试：左侧 `exit 7` 的 pipeline 最终 exit=7；filter 非法 service 最终非零。

- [ ] **Step 2: 修改命令**

```ini
command=/bin/bash -o pipefail -c 'python3 -u /app/web_server.py 2>&1 | python3 -u /app/timestamp_filter.py web'
```

refresh/nginx 分别替换命令和 service；nginx 使用 `nginx -g "daemon off;"`。不要把 `%` 格式串放进 Supervisor command，避免插值问题。

- [ ] **Step 3: 验证并提交**

Run: `python3 -m pytest tests/test_timestamp_filter.py tests/test_container_config.py -q`

```bash
git add supervisord.conf tests/test_container_config.py
git commit -m "feat: timestamp supervised service output"
```

---

### Task 3: 统一 nginx access/error 输出

**Files:**
- Modify: `nginx.conf`
- Modify: `tests/test_container_config.py`

- [ ] **Step 1: 写契约测试**

断言 conf.d 顶层定义 `log_format raynews`，格式含 remote address/request/status/body bytes/request time，但不含 `$time_local/$time_iso8601`；server 使用 `access_log /dev/stdout raynews`、`error_log /dev/stderr warn`。

- [ ] **Step 2: 实现**

```nginx
log_format raynews '$remote_addr "$request" $status $body_bytes_sent '
                   'rt=$request_time ua="$http_user_agent"';
```

外层 timestamp filter 提供唯一时间和 `[nginx]`。

- [ ] **Step 3: 验证语法并提交**

Run: `python3 -m pytest tests/test_container_config.py -q`

构建镜像后 Run: `nginx -t`

```bash
git add nginx.conf tests/test_container_config.py
git commit -m "feat: normalize nginx logs for timestamp prefixing"
```

---

### Task 4: entrypoint 和 Compose 时区可配置

**Files:**
- Modify: `entrypoint.sh`, `docker-compose.yml`, `.env.example`
- Modify: `tests/test_container_config.py`

- [ ] **Step 1: 写测试**

断言 Compose 使用 `TZ=${TZ:-Asia/Shanghai}`。执行仅包含 shell `log()` 的测试脚本，在 `TZ=UTC` 与 `TZ=Asia/Shanghai` 下断言偏移分别为 `+00:00`、`+08:00`（允许 UTC 表示为 `Z` 时先规范化）。

- [ ] **Step 2: 实现 shell log helper**

```bash
log() {
  printf '%s [entrypoint] %s\n' "$(date --iso-8601=seconds)" "$*"
}
```

把 entrypoint 自己的 `echo`/warning/error 改为 `log`；error 仍重定向 stderr。Python 注入脚本输出改为带 `[inject]` 的消息，外层调用失败时由 entrypoint log 错误。

- [ ] **Step 3: 验证并提交**

Run: `python3 -m pytest tests/test_container_config.py -q`

```bash
git add entrypoint.sh docker-compose.yml .env.example tests/test_container_config.py
git commit -m "feat: make container log timezone configurable"
```

---

### Task 5: 端到端容器验证

- [ ] Run: `python3 -m pytest tests/test_timestamp_filter.py tests/test_container_config.py -q`
- [ ] Run: `python3 -m pytest -q`
- [ ] Run: `docker compose build raynews && docker compose up -d raynews`
- [ ] 检查 `docker compose logs raynews`：entrypoint、web、refresh、fetcher、nginx access、Python traceback 样例均有时间和 service。
- [ ] 使用 `TZ=UTC docker compose up -d --force-recreate`，确认新日志偏移切换为 UTC；恢复原 TZ。
- [ ] `docker compose stop` 后确认容器内没有残留 child；主动让测试 program exit 7，确认 Supervisor 重启。

## Definition of Done

- [ ] 所有业务日志行都有 Compose TZ 的 ISO 8601 前缀。
- [ ] nginx 不重复时间戳。
- [ ] traceback 不存在无时间的续行。
- [ ] pipeline 正确传播退出并响应容器停止。
