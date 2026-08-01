# RayNews 安全加固与可靠性修复实施方案

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不破坏现有成功响应结构的前提下，修复代码审查确认的安全、资源生命周期、数据库可靠性与容器部署问题。

**Architecture:** 修复按安全边界分层：入口限流与请求上限、出站网络与内容验证、错误脱敏、SQLite 原子操作、反向代理策略、进程守护与权限隔离。T1–T22 标识保持不变以便追踪审查结论，但实际执行顺序以“权威执行顺序”为准。

**Tech Stack:** Python 3.12、Flask、SQLite、requests、BeautifulSoup、nginx、Docker Compose、supervisord、pytest。

## Global Constraints

- 当前基线已经实测：`python3 -m pytest tests/ -q` 为 **707 passed**；新增测试后要求收集数不少于 707 且零失败。
- 每个任务先写失败测试，确认失败原因正确，再实现最小修复；定向测试和全量测试通过后才能提交。
- 每个任务独立提交；只暂存该任务列出的实现、测试和文档文件，不得顺带提交工作区已有改动。
- 固定行号仅供定位；函数、路由、表名和 nginx 配置块才是修改边界。
- 不删除或改名既有公开 API 的成功响应字段；新增字段和新路由允许。
- 不复制未经验证的示例密钥、域名或依赖版本到生产配置。
- `datetime.utcnow()` 弃用告警不在本计划范围内。
- 每个任务结束必须运行：

```bash
python3 -m pytest tests/ -q
```

## 权威执行顺序

```text
入口与出站安全: T1 → T2 → T3 → T4 → T5
资源与图片边界: T21 → T12 → T15 → T22
数据库与业务可靠性: T6 → T7 → T8 → T9 → T10 → T11 → T13 → T14
认证与代理: T16 → T17
容器与依赖: T18 → T19 → T20
```

T21 必须早于 T12；T15 必须早于 T22；T18 必须早于 T19。

---

### T1：注册接口限流、bcrypt 预检与限流表清理

**Files:**
- Modify: `models.py` — `SCHEMA_SQL`、注册限流函数、`create_registered_user`
- Modify: `web_server.py` — `register`
- Test: `tests/test_auth_security.py`

**Interfaces:**
- Produces: `admit_register_attempt(client_ip: str, *, now: float | None = None) -> tuple[bool, int]`
- Produces: `reset_register_attempts(client_ip: str) -> None`
- Preserves: `create_registered_user(email: str, password: str, nickname: str, invite_code: str = "") -> tuple[dict | None, bool]`

**Problem:** `/auth/register` 可无限触发 bcrypt；当前 `create_registered_user` 在邀请码、重复邮箱和昵称判断前计算密码哈希。

- [ ] **Step 1: 写失败测试**

在 `tests/test_auth_security.py` 增加：

增加 `test_register_rate_limit_rejects_eleventh_attempt`、`test_register_invalid_invite_skips_bcrypt`、`test_register_duplicate_email_skips_bcrypt`、`test_register_duplicate_nickname_skips_bcrypt`、`test_register_success_resets_rate_limit`、`test_register_attempt_cleanup_removes_expired_rows`。限流测试先创建初始管理员，再连续提交 11 个字段合法但邀请码无效的请求，并断言前 10 个不是 429、第 11 个为 429 且 `Retry-After` 为正整数；三个预检测试把 `models.hash_password` 替换成一旦调用就抛出 `AssertionError` 的桩；成功重置测试先累计 9 次失败，再使用与注册邮箱匹配的有效邀请码成功注册，随后一次请求不得返回 429；清理测试预置一条两小时前的记录和一条当前记录，触发 admission 后只保留当前窗口内的行。

- [ ] **Step 2: 运行失败测试**

```bash
python3 -m pytest tests/test_auth_security.py -q -k 'register_rate_limit or skips_bcrypt or register_attempt_cleanup'
```

Expected: 当前无限流且重复项仍触发 bcrypt，因此失败。

- [ ] **Step 3: 建表并实现原子限流**

在 `SCHEMA_SQL` 增加：

```sql
CREATE TABLE IF NOT EXISTS register_attempts (
    client_ip     TEXT PRIMARY KEY,
    failure_count INTEGER NOT NULL DEFAULT 0,
    locked_until  REAL NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL
);
```

定义 15 分钟窗口、10 次允许尝试。`admit_register_attempt` 使用 `BEGIN IMMEDIATE`，先删除 `updated_at < current - 1800` 的过期行，再原子读取并更新当前 IP；第 10 次允许执行但建立锁，第 11 次返回 `(False, retry_after)`。成功注册调用 `reset_register_attempts`。

- [ ] **Step 4: 将 bcrypt 移到全部廉价预检之后**

短连接流程必须为：初始化数据库 → 检查重复邮箱 → 检查重复昵称 → 非首用户检查邀请码 → 计算 bcrypt → `BEGIN IMMEDIATE` → 重新检查首用户、重复项和邀请码 → INSERT/消费邀请码。事务内复检和 UNIQUE 约束仍是并发正确性的最终边界。

- [ ] **Step 5: 接入路由并验证**

字段格式校验之后调用 `admit_register_attempt(_trusted_client_ip())`；成功注册、签发 token 之前清除该 IP 的计数。运行：

```bash
python3 -m pytest tests/test_auth_security.py -q
python3 -m pytest tests/ -q
```

- [ ] **Step 6: 提交**

```bash
git add models.py web_server.py tests/test_auth_security.py
git commit -m "fix: harden registration admission"
```

---

### T2：全文抓取使用安全网络层和有界流式读取

**Files:**
- Modify: `fetcher.py` — `fetch_telegraph`、`fetch_wechat_article`
- Test: `tests/test_fetcher_network_safety.py`

**Interfaces:**
- Consumes: `network_safety.safe_get`
- Produces: `_read_body_with_limit(resp, limit: int) -> str`

**Problem:** 两个全文抓取函数直接使用 `requests.get`；仅在下载后检查长度无法阻止大响应占用内存。

- [ ] **Step 1: 写失败测试**

新增测试，确认两个抓取函数只调用 `fetcher.safe_get` 且参数包含 `stream=True`；假响应累计超过 2 MiB 时 `_read_body_with_limit` 抛 `ValueError`，外层抓取函数保持现有契约并返回 `None`；所有已取得的响应都调用 `close()`。

- [ ] **Step 2: 运行失败测试**

```bash
python3 -m pytest tests/test_fetcher_network_safety.py -q
```

- [ ] **Step 3: 实现有界读取**

增加：

```python
FULLTEXT_BODY_LIMIT = 2 * 1024 * 1024


def _read_body_with_limit(resp, limit: int) -> str:
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise ValueError("fulltext body too large")
        chunks.append(chunk)
    encoding = requests.utils.get_encoding_from_headers(resp.headers) or "utf-8"
    return b"".join(chunks).decode(encoding, errors="replace")
```

两个函数都使用：

```python
resp = None
try:
    resp = safe_get(url, headers=headers, timeout=timeout, stream=True)
    resp.raise_for_status()
    body = _read_body_with_limit(resp, FULLTEXT_BODY_LIMIT)
finally:
    if resp is not None:
        resp.close()
```

读取 `body` 后保留各函数现有 BeautifulSoup 解析和返回结构；外层继续记录 `<type> fetch failed` 并返回 `None`。

- [ ] **Step 4: 验证并提交**

```bash
python3 -m pytest tests/test_fetcher_network_safety.py tests/test_network_safety.py tests/test_fulltext_backfill.py -q
python3 -m pytest tests/ -q
git add fetcher.py tests/test_fetcher_network_safety.py
git commit -m "fix: bound safe fulltext downloads"
```

---

### T3：每日摘要邮件复用 HTML 白名单消毒

**Files:**
- Modify: `notifier.py` — `send_daily_summary_email`
- Test: `tests/test_notification_email_markdown.py`
- Test: `tests/test_daily_summary_delivery.py`

**Problem:** 每日摘要直接嵌入 Python-Markdown 输出，原始 HTML 未经过现有白名单清洗。

- [ ] **Step 1: 写完整发送路径的失败测试**

mock `send_email` 捕获 HTML；输入包含 `<script>`、事件属性、`javascript:` 链接和正常标题/列表/加粗，断言危险内容删除且 Markdown 布局保留。

- [ ] **Step 2: 运行失败测试**

```bash
python3 -m pytest tests/test_notification_email_markdown.py -q
```

- [ ] **Step 3: 使用现有消毒器**

将摘要渲染替换为：

```python
summary_html = render_notification_email_body(summary_text, fmt="markdown")
```

模板、统计字段和发送参数不变。

- [ ] **Step 4: 验证并提交**

```bash
python3 -m pytest tests/test_notification_email_markdown.py tests/test_daily_summary.py tests/test_daily_summary_delivery.py tests/test_daily_summary_retry.py -q
python3 -m pytest tests/ -q
git add notifier.py tests/test_notification_email_markdown.py tests/test_daily_summary_delivery.py
git commit -m "fix: sanitize daily summary email html"
```

---

### T4：AI 错误全链路脱敏

**Files:**
- Modify: `ai_service.py` — `_format_api_error` 和脱敏辅助函数
- Modify: `web_server.py` — 健康状态、后台日志和持久化错误调用点
- Test: `tests/test_system_ai_health_alert.py`
- Test: `tests/test_ai_endpoint_validation.py`

**Problem:** provider 错误体可能进入异常、日志、站内通知和 `ai_results` 错误字段；只匹配 `sk-` 或 `key=` 不能覆盖直接回显的真实 key。

- [ ] **Step 1: 写失败测试**

覆盖三类输入：Authorization/Bearer、`api_key=<secret>`、无标签但与 `AIService.api_key` 完全相同的随机字符串。断言 `_format_api_error`、`_system_ai_health["last_error"]`、后台日志捕获值和 `_save_ai_result` 错误参数均不含真实凭据。

- [ ] **Step 2: 运行失败测试**

```bash
python3 -m pytest tests/test_system_ai_health_alert.py tests/test_ai_endpoint_validation.py -q -k 'redact or secret or api_error'
```

- [ ] **Step 3: 在错误源头替换已知 key**

在 `ai_service.py` 实现：

```python
def _redact_api_error(value: str, *known_secrets: str) -> str:
    text = " ".join(str(value or "").split())
    for secret in known_secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(
        r"(?i)\b(?:proxy-)?authorization\s*:\s*(?:bearer\s+)?[^\s,;]+",
        "[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [redacted]",
        text,
    )
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(
        r"(?i)(?:api[_-]?key|x-api-key|access[_-]?token|token|secret|password|key)"
        r"\s*(?:=|:)\s*(?:[\"']?)[^\s,;&}\]\"']+",
        "[redacted]",
        text,
    )
    return text
```

`AIService._format_api_error` 在截断和返回前调用 `_redact_api_error(detail, self.api_key)`。这里的精确替换是主要安全边界，通用正则仅为补充。

- [ ] **Step 4: 收口 web 层输出**

`_note_system_ai_failure`、`_compact_share_error` 统一调用 web 层 `_redact_secrets`。后台任务打印或写入 `summary_error`、`title_summary_error` 前也调用该函数；不得把同一个原始 `str(e)` 同时传给安全状态和不安全日志。

- [ ] **Step 5: 验证并提交**

```bash
python3 -m pytest tests/test_system_ai_health_alert.py tests/test_ai_endpoint_validation.py tests/test_share_recovery.py -q
python3 -m pytest tests/ -q
git add ai_service.py web_server.py tests/test_system_ai_health_alert.py tests/test_ai_endpoint_validation.py
git commit -m "fix: redact ai errors end to end"
```

---

### T5：AI 结果请求体、类型与长度校验

**Files:**
- Modify: `web_server.py` — Flask 配置、`ai_save_result`
- Test: `tests/test_access_and_ui_contracts.py`

**Problem:** 字段类型错误会触发 500；解析 JSON 后才检查字符串长度无法阻止超大请求体。

- [ ] **Step 1: 写失败测试**

测试非字符串 summary/translation 返回 400、200001 字符返回 400、超过 2 MiB 的请求返回 413、巨大无关字段也返回 413、正常字符串仍成功。

- [ ] **Step 2: 运行失败测试**

```bash
python3 -m pytest tests/test_access_and_ui_contracts.py -q -k 'ai_result and (type or size or body)'
```

- [ ] **Step 3: 增加解析前硬上限和字段校验**

设置：

```python
MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
AI_RESULT_MAX_BODY_BYTES = 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BODY_BYTES
```

`ai_save_result` 在 `request.get_json` 前拒绝已知 `Content-Length > AI_RESULT_MAX_BODY_BYTES`；解析后严格校验两个字段为 `str | None` 且分别不超过 200000 字符，再执行现有判空和消毒逻辑。Flask 全局上限负责无 Content-Length 或其他路由的最终硬边界。

- [ ] **Step 4: 验证并提交**

```bash
python3 -m pytest tests/test_access_and_ui_contracts.py tests/test_ai_relay_frontend.py -q
python3 -m pytest tests/ -q
git add web_server.py tests/test_access_and_ui_contracts.py
git commit -m "fix: bound shared ai result requests"
```

---

### T6：原子访问计数与访问日志保留期

**Files:**
- Modify: `models.py` — `record_access`、`prune_access_log`
- Modify: `web_server.py` — 低频清理调用
- Test: `tests/test_server_stats.py`

**Problem:** 每个认证 GET 都写 `last_seen_at`；读后再写的节流在并发请求下会重复计数；访问日志无限增长。

- [ ] **Step 1: 写失败测试**

新增窗口内顺序调用、窗口后调用、两个线程同时调用、清理新旧日志、清理失败后可立即重试五类测试。

- [ ] **Step 2: 运行失败测试**

```bash
python3 -m pytest tests/test_server_stats.py -q
```

- [ ] **Step 3: 用廉价预读和条件 UPDATE 实现原子节流**

先 SELECT `last_seen_at`；用户不存在或仍在 5 分钟窗口内时直接返回，不执行 DML。只有预读显示窗口已过时才计算 `now_str` 和 cutoff 并执行：

```sql
UPDATE users
SET visit_count = visit_count + 1, last_seen_at = ?
WHERE id = ?
  AND (
      last_seen_at IS NULL OR last_seen_at = '' OR last_seen_at < ?
  )
```

仅当 cursor `rowcount == 1` 时，在同一事务插入 `user_access_log` 并提交；若并发请求抢先更新导致 `rowcount == 0`，调用 `db.rollback()` 结束本次 DML 事务后返回。这样窗口内常规请求保持纯读，并发越过窗口时仍只有一个请求计数。

- [ ] **Step 4: 实现可靠清理节流**

定义模块级 `_access_log_prune_lock = threading.Lock()` 和成功时间戳。锁内检查间隔；执行 DELETE、读取该 cursor 的 `rowcount`、commit 成功后才更新时间戳。删除条件使用 UTC 字符串：

```sql
DELETE FROM user_access_log
WHERE accessed_at < strftime('%Y-%m-%d %H:%M:%S', ?, 'unixepoch')
```

在 `_daily_summary_loop` 每次 tick 调用 `prune_access_log()`；函数自身保证每小时最多实际 DELETE 一次。

- [ ] **Step 5: 验证并提交**

```bash
python3 -m pytest tests/test_server_stats.py tests/test_auth_security.py -q
python3 -m pytest tests/ -q
git add models.py web_server.py tests/test_server_stats.py
git commit -m "fix: atomically throttle access logging"
```

---

### T7：一次性 news.db 连接异常路径关闭

**Files:**
- Modify: `web_server.py` — `_news_db_conn` 和一次性连接调用点
- Test: `tests/test_news_db_thread_safety.py`

**Problem:** 多个一次性连接仅在成功路径关闭；但 `_get_news_db` 的连接是刻意保存在 thread-local 中的持久连接，不能放进自动关闭上下文。

- [ ] **Step 1: 写失败测试**

用可记录 `close()` 的假连接让 `_get_article_meta`、daily-summary helper、AI result helper 在查询异常时退出，断言连接已关闭。另加连续两次 `_get_news_db()` 查询测试，断言缓存连接没有被提前关闭。

- [ ] **Step 2: 运行失败测试**

```bash
python3 -m pytest tests/test_news_db_thread_safety.py -q
```

- [ ] **Step 3: 增加上下文管理器并只迁移一次性调用点**

```python
@contextmanager
def _news_db_conn():
    conn = _news_db_connect()
    try:
        yield conn
    finally:
        conn.close()
```

迁移这些函数：`_get_article_meta`、`_init_daily_summary_global_table`、`_get_daily_summary_global_cache`、`_save_daily_summary_global_cache`、`_init_daily_summary_sends_table`、`_get_daily_summary_sent_user_ids`、`_record_daily_summary_send`、`_init_daily_summary_failures_table`、`_get_daily_summary_failure`、`_record_daily_summary_failure`、`_clear_daily_summary_failure`、`_claim_daily_summary_alert`、`_release_daily_summary_alert`、`_fetch_untranslated_articles`、`_fetch_recent_articles`、`_fetch_articles_by_date`、`_fetch_unsummarized_articles`、`_fetch_article_body`、`_init_ai_results_table`、`_get_ai_result`。

已有 `conn = None` + `finally` 的 `_save_ai_result`、`_publish_translation_update`、`ai_translation_updates` 可以保留原样。**不得迁移 `_get_news_db` 内的 `_news_db_connect()`。**

- [ ] **Step 4: 静态与运行验证**

```bash
python3 -m pytest tests/test_news_db_thread_safety.py -q
python3 - <<'PY'
import ast
tree = ast.parse(open('web_server.py', encoding='utf-8').read())
print('parsed', bool(tree))
PY
python3 -m pytest tests/ -q
git add web_server.py tests/test_news_db_thread_safety.py
git commit -m "fix: close one-shot news database connections"
```

---

### T8：调度状态端点管理员鉴权和可达别名

**Files:**
- Modify: `web_server.py` — `scheduler_status`
- Test: `tests/test_auth_security.py`

**Problem:** 内部调试端点暴露用户 ID 且无鉴权；当前 nginx 又没有代理原路径，因此管理员无法通过正常入口使用。

- [ ] **Step 1: 写失败测试**

分别请求 `/scheduler/status` 和 `/admin/scheduler/status`：未认证 401、普通用户 403、管理员 200，两个成功响应结构一致。

- [ ] **Step 2: 实现鉴权别名**

在现有 `scheduler_status` 函数上依次添加 `@app.route("/scheduler/status", methods=["GET"])`、`@app.route("/admin/scheduler/status", methods=["GET"])` 和 `@require_role("admin")`，函数体保持现有响应字段不变。已有 nginx `/admin/` location 会代理新别名；保留旧路径避免破坏直接调用。

- [ ] **Step 3: 验证并提交**

```bash
python3 -m pytest tests/test_auth_security.py -q -k scheduler
python3 -m pytest tests/ -q
git add web_server.py tests/test_auth_security.py
git commit -m "fix: protect scheduler status"
```

---

### T9：通知收件邮箱输入校验

**Files:**
- Modify: `web_server.py` — 设置保存、测试通知、每日收件人收集
- Test: `tests/test_notification_email_required.py`

**Problem:** 非法邮箱字符串可持久化并进入发送路径。本任务只保证格式正确，不声称阻止用户配置任意合法第三方邮箱。

- [ ] **Step 1: 写失败测试**

覆盖开启邮件推送时非法地址、测试通知非法地址、历史非法配置在每日投递时被跳过、合法地址正常四种路径。

- [ ] **Step 2: 实现三层校验**

设置保存和测试通知使用 `is_valid_email(to_email)` 返回 400；每日收件人收集仅接受非空且格式有效的地址。关闭邮件推送时允许用户清除或暂存空地址。

- [ ] **Step 3: 验证并提交**

```bash
python3 -m pytest tests/test_notification_email_required.py tests/test_daily_summary_delivery.py -q
python3 -m pytest tests/ -q
git add web_server.py tests/test_notification_email_required.py
git commit -m "fix: validate notification recipients"
```

---

### T10：保留人工/AI 分类和显式别名

**Files:**
- Modify: `source_categories.py` — `cleanup_stale_source_categories`
- Test: `tests/test_source_maintenance.py`

**Problem:** 当前清理会删除零文章的 manual/classified 分类及显式 alias；alias 表没有 status 字段，不能按 pending 处理。

- [ ] **Step 1: 写失败测试**

构造 global manual/classified/pending/failed 分类和 global/user aliases。断言只删除零文章的 pending/failed 分类；manual/classified 和指向仍存在分类的显式 alias 保留；目标分类确实不存在的悬空 alias 删除。

- [ ] **Step 2: 实现明确删除规则**

分类删除条件为：

```sql
DELETE FROM source_categories
WHERE source = ? AND status IN ('pending', 'failed')
```

`user_source_categories` 仅删除不在 live sources 且 `status IN ('pending', 'failed')` 的行。`source_aliases` 和 `user_source_aliases` 不再按 `live_sources` 删除，只删除：

```sql
DELETE FROM source_aliases
WHERE target_source NOT IN (SELECT source FROM source_categories);

DELETE FROM user_source_aliases
WHERE target_source NOT IN (SELECT source FROM source_categories);
```

- [ ] **Step 3: 验证并提交**

```bash
python3 -m pytest tests/test_source_maintenance.py tests/test_source_split.py -q
python3 -m pytest tests/ -q
git add source_categories.py tests/test_source_maintenance.py
git commit -m "fix: preserve explicit source metadata"
```

---

### T11：直接解析 link preview URL 的来源域名

**Files:**
- Modify: `source_categories.py` — URL 域名规范化辅助函数
- Modify: `fetcher.py` — `detect_source`、`process_message`
- Test: `tests/test_detect_source_link_preview.py`

**Problem:** `process_message` 把裸 URL 传给只识别 `href=` 的 HTML 正则；把不可信 URL 拼成 HTML 又会引入属性注入和来源伪造。

- [ ] **Step 1: 写失败测试**

测试合法 ifeng URL、大小写 host、带端口 URL、非 HTTP scheme、包含引号和伪造 `href` 片段的 URL。只有实际 URL hostname 可参与 `KNOWN_DOMAINS` 匹配。

- [ ] **Step 2: 提取 URL 解析接口**

在 `source_categories.py` 增加：

```python
def extract_domain_from_url(value: str) -> str | None:
    try:
        parsed = urlsplit((value or "").strip())
        host = (parsed.hostname or "").lower()
    except (TypeError, ValueError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return None
    return _root_domain(host)
```

把现有 `extract_domains_from_html` 的 www、多级 TLD、排除列表逻辑抽成 `_root_domain` 复用。为保持内部调用兼容，签名改为 `detect_source(content: str, extra_html: str = "", *, extra_url: str = "") -> str`：`extra_url` 直接调用新接口，`extra_html` 仍只处理真正的 HTML；`process_message` 使用 `extra_url=link_preview_url`，不构造 HTML。

- [ ] **Step 3: 验证并提交**

```bash
python3 -m pytest tests/test_detect_source_link_preview.py tests/test_source_split.py -q
python3 -m pytest tests/ -q
git add source_categories.py fetcher.py tests/test_detect_source_link_preview.py
git commit -m "fix: parse preview source urls directly"
```

---

### T12：按真实魔术字节识别图片并拒绝非图片

**Files:**
- Create: `image_validation.py`
- Modify: `image_cache.py` — `fetch_remote_image`
- Modify: `refresh_server.py` — 图片响应头
- Modify: `Dockerfile` — COPY 新模块
- Test: `tests/test_image_cache.py`
- Test: `tests/test_network_safety.py`

**Prerequisite:** T21 已完成，所有 streamed response 均可靠关闭。

**Problem:** 远端声明的 Content-Type 可与真实内容不一致；已知为 HTML 的 body 不得以图片 MIME 和 HTTP 200 原样透传。

- [ ] **Step 1: 写失败测试**

覆盖 JPEG、PNG、GIF、WebP；声明 JPEG 实际 PNG 时按 PNG 返回并缓存；声明图片实际 HTML 时 `fetch_remote_image` 最终失败、无文件和数据库记录；`/img-cache` 返回 502 而不是 200；所有正常图片响应含 `nosniff`。

- [ ] **Step 2: 建立共享图片类型接口**

`image_validation.py` 提供：

```python
def detect_image_content_type(data: bytes) -> str | None:
    header = bytes(data[:16])
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None
```

- [ ] **Step 3: 以真实类型作为唯一缓存类型**

删除“读取 body 前要求声明 Content-Type 属于 `ALLOWED_IMAGE_TYPES`”的判断。`fetch_remote_image` 读取完整且有界的 body 后调用 detector；返回 `None` 时记录候选失败并尝试下一个安全候选，所有候选失败后抛最后异常。detector 返回类型时忽略缺失或错误声明，以真实类型决定扩展名和响应 MIME。删除 `MismatchedImageContent` 透传设计。

- [ ] **Step 4: 所有图片成功响应添加 nosniff**

`refresh_server.Handler._handle_img_cache` 的正常缓存、直接抓取和异常后缓存兜底三个 200 分支都调用：

```python
self.send_header("X-Content-Type-Options", "nosniff")
```

- [ ] **Step 5: 验证并提交**

```bash
python3 -m pytest tests/test_image_cache.py tests/test_network_safety.py -q
python3 -m pytest tests/ -q
git add image_validation.py image_cache.py refresh_server.py Dockerfile tests/test_image_cache.py tests/test_network_safety.py
git commit -m "fix: validate cached image content"
```

---

### T13：非破坏性 article upsert

**Files:**
- Modify: `fetcher.py` — `upsert_articles`
- Test: `tests/test_fulltext_backfill.py`

**Problem:** `INSERT OR REPLACE` 会删除再插入整行，覆盖 `original_body_html` 并重置未列出的迁移字段；空的重复抓取还可能抹掉已有全文。

- [ ] **Step 1: 写失败测试**

先插入完整文章 A 和额外标题迁移字段，再用同 ID 的空正文/新元数据 B upsert。断言 original、已有全文、`has_full_content=1`、已有非空 summary 和未列出的标题字段保留，明确提供的新非空正文可以更新 `body_html`。

- [ ] **Step 2: 改用 ON CONFLICT UPDATE**

使用 `INSERT INTO articles (id, title, source, feed_source, origin_source, time, date, timestamp, thumb, has_full_content, telegraph_url, body_html, original_body_html, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE`。元数据采用 `excluded`；以下内容字段非破坏更新：

```sql
has_full_content = CASE
    WHEN articles.has_full_content = 1 OR excluded.has_full_content = 1 THEN 1 ELSE 0 END,
telegraph_url = CASE WHEN excluded.telegraph_url != ''
    THEN excluded.telegraph_url ELSE articles.telegraph_url END,
body_html = CASE WHEN excluded.body_html != ''
    THEN excluded.body_html ELSE articles.body_html END,
original_body_html = CASE
    WHEN articles.original_body_html IS NULL OR articles.original_body_html = ''
    THEN excluded.original_body_html ELSE articles.original_body_html END,
summary = CASE WHEN excluded.summary != ''
    THEN excluded.summary ELSE articles.summary END
```

不得在 UPDATE 列表中写未由 fetcher 拥有的迁移字段。

- [ ] **Step 3: 验证并提交**

```bash
python3 -m pytest tests/test_fulltext_backfill.py tests/test_fetch_today_only.py tests/test_news_schema.py -q
python3 -m pytest tests/ -q
git add fetcher.py tests/test_fulltext_backfill.py
git commit -m "fix: preserve article state during upsert"
```

---

### T14：图片预热只查询新增文章并复用刷新快照

**Files:**
- Modify: `refresh_server.py` — `run_fetcher`、`_run_refresh_job`、`enqueue_new_article_images`
- Test: `tests/test_refresh_jobs.py`
- Test: `tests/test_streaming_refresh.py`

**Problem:** 当前预热读取全表正文后在 Python 过滤；外层刷新任务已做 before/after 快照，不能再无视它们增加到四次扫描。

- [ ] **Step 1: 写失败测试**

记录 SQLite 查询，断言预热只有分批的 `WHERE id IN` 参数化查询，每批不超过 500；一次刷新总共只执行一组 before/after ID 快照；响应新增 `new_ids` 不删除既有字段。

- [ ] **Step 2: 让 run_fetcher 接受可复用 baseline**

```python
def run_fetcher(existing_article_ids: set[int] | None = None):
    baseline = (
        set(existing_article_ids)
        if existing_article_ids is not None
        else article_id_snapshot()
    )
```

该片段替换 `run_fetcher` 当前无参签名和函数开头的 `existing_article_ids = article_id_snapshot()`；其余抓取逻辑接在 `baseline` 赋值之后。抓取和维护成功后计算一次 `after_ids` 和 `new_ids`，再构造最终响应 JSON，把 `new_ids` 加入既有 `status`、`returncode`、`stdout`、`stderr` 字段并传给后台预热线程。`_run_refresh_job` 调用 `run_fetcher(before_ids)`，从 payload 读取 `new_ids`，不再执行自己的第二次 after snapshot；失败响应的既有结构不变。

- [ ] **Step 3: 分批查询新增行**

`enqueue_new_article_images(new_article_ids: list[int])` 对排序去重后的正整数每 500 个一批生成相同数量的 `?` 占位符并执行 `WHERE id IN (<generated placeholders>)`，不再查询全表正文；`<generated placeholders>` 是运行时由 `",".join("?" for _ in batch)` 生成的 SQL 片段，不是待填写文本。

- [ ] **Step 4: 验证并提交**

```bash
python3 -m pytest tests/test_refresh_jobs.py tests/test_streaming_refresh.py -q
python3 -m pytest tests/ -q
git add refresh_server.py tests/test_refresh_jobs.py tests/test_streaming_refresh.py
git commit -m "fix: scope image warmup to new articles"
```

---

### T15：同源 CORS、nginx 安全头与请求体上限

**Files:**
- Create: `nginx-security-headers.conf`
- Modify: `nginx.conf`
- Modify: `Dockerfile`
- Modify: `web_server.py` — 删除全局 Flask-CORS
- Test: `tests/test_security_hardening.py`

**Prerequisite:** T5 已定义应用请求体上限；T12 已为 Python 图片响应设置 nosniff。

**Problem:** nginx 和 Flask 同时生成宽松 CORS；上游 Flask-CORS 会绕过 nginx allowlist；nginx `add_header` 在子 location 重新定义时不继承 server 头。

- [ ] **Step 1: 写失败测试和配置断言**

Flask test client 带恶意 Origin 请求 `/auth/health`，断言无 `Access-Control-Allow-Origin`。静态检查确保不存在 `CORS(app)`、通配 CORS 或未锚定 Origin map。容器集成测试覆盖 `/`、`/auth/health`、`/api/news`、`/img-cache` 的三个安全头。

- [ ] **Step 2: 选择单一同源策略**

RayNews 前端和 API 均由同一 nginx origin 提供，因此删除：

```python
from flask_cors import CORS
CORS(app)
```

删除 nginx 中全部 `Access-Control-Allow-*`。不反射任何 Origin；非浏览器客户端不受 CORS 影响。未来若产品明确支持跨源客户端，另开设计任务，不在此处硬编码示例域名。

- [ ] **Step 3: 用 include 解决 header 继承**

创建：

```nginx
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options SAMEORIGIN always;
add_header Referrer-Policy strict-origin-when-cross-origin always;
```

在 server 层 include；所有自身含 `add_header` 的 location 也 include 同一文件。Dockerfile 将文件 COPY 到 `/etc/nginx/snippets/raynews-security-headers.conf`。server 层增加 `client_max_body_size 2m;`。

- [ ] **Step 4: 构建和实机验证**

```bash
docker build -t raynews-security-plan .
docker run --rm raynews-security-plan nginx -t
```

启动容器后分别对静态和代理路由执行：

```bash
curl -sSI -H 'Origin: https://evil.example' http://127.0.0.1:8090/
curl -sSI -H 'Origin: https://evil.example' http://127.0.0.1:8090/auth/health
```

两者均不得出现 ACAO，且必须出现三项安全头。浏览器回归登录、新闻列表、详情、收藏和设置保存。

- [ ] **Step 5: 验证并提交**

```bash
python3 -m pytest tests/test_security_hardening.py tests/test_auth_security.py -q
python3 -m pytest tests/ -q
git add nginx-security-headers.conf nginx.conf Dockerfile web_server.py tests/test_security_hardening.py
git commit -m "fix: enforce same-origin proxy policy"
```

---

### T16：可配置可信代理网段，默认行为保持不变

**Files:**
- Modify: `web_server.py` — `_trusted_client_ip`
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Test: `tests/test_auth_security.py`

**Problem:** 当前部署通过 loopback nginx 工作正常；受支持的外部反代部署需要显式配置，不能默认信任 Docker 私网或任意 forwarded header。

- [ ] **Step 1: 写失败测试**

覆盖默认仅 loopback、显式 `192.168.0.0/24`、不可信 peer 伪造 X-Real-IP、非法网段 fail closed、合法 IPv6 网段。

- [ ] **Step 2: 每次请求按环境解析显式配置**

实现 `_trusted_proxy_networks()`，使用：

```python
raw = os.environ.get("TRUSTED_PROXY_PREFIXES") or "127.0.0.1/32,::1/128"
```

逐项调用 `ipaddress.ip_network(prefix, strict=False)`；非法项跳过且不扩大信任。仅当 `request.remote_addr` 属于其中一个网段时才解析单值 `X-Real-IP`。不使用 `X-Forwarded-For` 链。

- [ ] **Step 3: 文档和 compose 透传**

`.env.example` 说明只有直接代理地址段可以配置；compose 增加：

```yaml
- TRUSTED_PROXY_PREFIXES=${TRUSTED_PROXY_PREFIXES:-}
```

空值仍回退到 loopback 默认值。

- [ ] **Step 4: 验证并提交**

```bash
python3 -m pytest tests/test_auth_security.py -q -k trusted_client_ip
python3 -m pytest tests/ -q
git add web_server.py .env.example docker-compose.yml tests/test_auth_security.py
git commit -m "fix: configure trusted auth proxies"
```

---

### T17：JWT 版本化和真实吊销入口

**Files:**
- Modify: `models.py` — schema、读取和轮换
- Modify: `auth.py` — 签发与校验
- Modify: `web_server.py` — 签发点、角色变更、管理员吊销路由
- Test: `tests/test_auth_security.py`

**Problem:** 当前角色降级已通过每请求读取 DB role 生效，但不存在主动吊销仍有效 JWT 的入口。只增加未调用的 rotate helper 不构成修复。

- [ ] **Step 1: 写失败测试**

覆盖旧 token 无 `ver` 与版本 0 兼容、管理员吊销后旧 token 401、新登录 token 正常、角色变更自动轮换、普通用户不能吊销其他用户。

- [ ] **Step 2: schema 和 token payload**

users 增加 `token_version INTEGER NOT NULL DEFAULT 0`，既写入 fresh schema，也通过 `_add_column_if_missing` 迁移。`get_user`、注册返回查询和 `list_users` 包含该列。`create_token` 写入整数 `ver`；`require_auth` 在 `record_access` 前比较 DB 版本和 payload 默认 0。

- [ ] **Step 3: 建立实际轮换路径**

实现返回 bool 的：

```python
def rotate_token_version(user_id: int) -> bool:
    db = get_db()
    cur = db.execute(
        "UPDATE users SET token_version = token_version + 1 WHERE id = ?",
        (user_id,),
    )
    db.commit()
    return cur.rowcount == 1
```

新增 `POST /auth/users/<int:user_id>/revoke-tokens`，仅 admin 可调用，成功返回 `{"ok": true}`，不存在返回 404。`admin_set_role` 成功修改角色后也轮换版本。register/login 签发当前版本。

- [ ] **Step 4: 验证并提交**

```bash
python3 -m pytest tests/test_auth_security.py -q -k 'token_version or revoke_tokens or role'
python3 -m pytest tests/ -q
git add models.py auth.py web_server.py tests/test_auth_security.py
git commit -m "fix: add jwt token revocation"
```

---

### T18：supervisord 进程守护和容器日志

**Files:**
- Create: `supervisord.conf`
- Modify: `Dockerfile`
- Modify: `entrypoint.sh`
- Modify: `nginx.conf`

**Problem:** Python 服务用裸 `&` 启动，无重启守护；简单加入 supervisor 若不转发日志会破坏 `docker logs`。

- [ ] **Step 1: 创建可独立运行的 supervisor 配置**

配置 `nodaemon=true`、`pidfile=/run/supervisord.pid`。refresh、web、nginx 三个 program 均设置 `autorestart=true`、`startsecs=3`、`startretries=5`、`stopasgroup=true`、`killasgroup=true`，并包含：

```ini
stdout_logfile=/dev/fd/1
stdout_logfile_maxbytes=0
stderr_logfile=/dev/fd/2
stderr_logfile_maxbytes=0
```

nginx command 为 `nginx -g "daemon off;"`。本任务 supervisor 仍以 root 启动；T19 再为 Python program 设置 `user=raynews`。`nginx.conf` 在 server 块增加 `access_log /dev/stdout;` 和 `error_log /dev/stderr warn;`，使 nginx 请求和错误也进入容器日志。

- [ ] **Step 2: Dockerfile 和 entrypoint 接入**

安装 `supervisor`，COPY 配置到 `/app/supervisord.conf`。entrypoint 保留配置注入和警告，删除三个手工启动命令，最后执行：

```bash
exec supervisord -c /app/supervisord.conf
```

- [ ] **Step 3: 容器验证**

构建并启动后记录 `docker logs`；杀死 web 和 refresh 子进程，确认 PID 改变且服务恢复。杀死 nginx 后同样确认恢复。检查 SIGTERM 能在 compose stop 超时内退出。

- [ ] **Step 4: 测试并提交**

```bash
docker build -t raynews-supervisor .
docker run -d --name raynews-supervisor-check raynews-supervisor
sleep 5
docker inspect -f '{{.State.Running}}' raynews-supervisor-check
docker logs raynews-supervisor-check
docker rm -f raynews-supervisor-check
python3 -m pytest tests/ -q
git add supervisord.conf Dockerfile entrypoint.sh nginx.conf
git commit -m "fix: supervise container services"
```

---

### T19：Python 子进程非 root 和挂载目录权限

**Files:**
- Modify: `Dockerfile`
- Modify: `supervisord.conf`
- Modify: `entrypoint.sh`
- Modify: `docker-compose.yml`

**Prerequisite:** T18 已创建 supervisor 配置。

**Problem:** Python 服务当前以 root 运行；镜像构建时 chown 不能保证宿主机 bind mount 在运行时可写。不得同时设置 Dockerfile `USER raynews` 再执行 `su raynews`。

- [ ] **Step 1: 创建固定应用用户但保留 root entrypoint**

Dockerfile 使用以下命令创建系统用户并准备目录，但**不添加 `USER raynews`**：

```dockerfile
RUN groupadd --system raynews && \
    useradd --system --gid raynews --create-home \
      --home-dir /home/raynews --shell /usr/sbin/nologin raynews && \
    mkdir -p /app/data /var/log/nginx /run/nginx && \
    chown -R raynews:raynews /app/data
```

entrypoint 需要 root 完成静态 HTML 注入、挂载目录检查和子进程降权。

- [ ] **Step 2: supervisor 只降权 Python program**

在 refresh/web program 增加：

```ini
user=raynews
environment=HOME="/home/raynews",USER="raynews"
```

nginx master 继续由 supervisor root 启动，沿用发行版自身 worker 降权配置；无需改到高端口，也不改 compose 的 `8090:80`。

- [ ] **Step 3: entrypoint 验证并授权 bind mount**

启动 supervisor 前执行：

```bash
install -d -o raynews -g raynews /app/data
chown -R raynews:raynews /app/data
test -w /app/data
```

若授权失败，entrypoint 以非零状态退出并输出明确错误，不允许以 root Python 回退。compose 增加以下 healthcheck，检查 nginx、web 和 refresh 三个进程面：

```yaml
healthcheck:
  test:
    - CMD
    - python3
    - -c
    - >-
      import urllib.request;
      [urllib.request.urlopen(url, timeout=2).read() for url in
      ('http://127.0.0.1/health',
      'http://127.0.0.1:8082/auth/health',
      'http://127.0.0.1:8081/refresh/status')]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 30s
```

- [ ] **Step 4: 集成验收**

使用真实 `./data` bind mount 启动；`ps -o user,pid,cmd` 必须显示两个 Python 进程属于 raynews，nginx 正常监听 80；执行注册、刷新和图片缓存，确认三个数据库/缓存目录可写。再次重启容器确认权限保持。

- [ ] **Step 5: 测试并提交**

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose exec raynews ps -o user,pid,cmd
python3 -m pytest tests/ -q
git add Dockerfile supervisord.conf entrypoint.sh docker-compose.yml
git commit -m "fix: drop python service privileges"
```

---

### T20：完整可复现 Python 依赖锁

**Files:**
- Create: `requirements.in`
- Modify: `requirements.txt`
- Modify: `Dockerfile`

**Problem:** 顶层 `>=` 会随时间解析不同版本；只把七个顶层依赖改成 `==` 仍不能锁定传递依赖。

- [ ] **Step 1: 创建顶层输入文件**

把 T15 完成后仍使用的六个直接依赖及其允许范围移到 `requirements.in`；不再使用的 `flask-cors` 不列入输入文件。`requirements.txt` 成为机器生成的完整锁文件，不手工维护。

- [ ] **Step 2: 在目标基础镜像环境生成带 hash 的锁**

从仓库根目录运行固定 pip-tools 版本：

```bash
docker run --rm -v "$PWD:/src" -w /src python:3.12-slim sh -c '
  python -m pip install --no-cache-dir pip-tools==7.5.1 &&
  python -m piptools compile --generate-hashes --resolver=backtracking \
    --output-file=requirements.txt requirements.in
'
```

锁文件必须包含所有传递依赖和 hash，并记录生成命令头。

- [ ] **Step 3: 构建时强制 hash**

Dockerfile 安装命令改为：

```dockerfile
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
```

- [ ] **Step 4: 干净构建和测试**

```bash
docker build --no-cache -t raynews-locked .
docker run --rm raynews-locked python3 -m pip check
python3 -m pytest tests/ -q
```

- [ ] **Step 5: 提交**

```bash
git add requirements.in requirements.txt Dockerfile
git commit -m "chore: lock python dependencies"
```

---

### T21：图片缓存按路径初始化和 streamed response 关闭

**Files:**
- Modify: `image_cache.py` — `init_cache`、`fetch_remote_image`
- Test: `tests/test_image_cache.py`
- Test: `tests/test_network_safety.py`

**Problem:** 每个连接重复执行 PRAGMA/DDL；异常和候选重试路径未保证关闭响应；单个布尔初始化标志无法应对测试重定向或缓存数据库被删除后的恢复。

- [ ] **Step 1: 写失败测试**

覆盖同一路径并发初始化仅执行一次、切换 DB_FILE 后重新初始化、删除 cache.db 后恢复、HTTP 状态异常/类型异常/过大/空 body/UnsafeUrlError 后 close、`safe_get` 在返回前抛异常时不引用未赋值变量。

- [ ] **Step 2: 按路径守卫初始化**

定义：

```python
_cache_init_lock = threading.Lock()
_initialized_cache_paths: set[str] = set()
```

锁内以 `str(DB_FILE.resolve())` 为 key；仅当 key 已记录且 DB 文件仍存在时短路。DDL 和 commit 全部成功后才加入 set；失败不得加入。

- [ ] **Step 3: 用 finally 关闭每个候选响应**

候选循环每轮：

```python
resp = None
try:
    resp = safe_get(candidate, headers=headers, timeout=15, stream=True)
    resp.raise_for_status()
    content_type = (
        (resp.headers.get("Content-Type") or "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError(f"unsupported image type: {content_type or 'unknown'}")
    chunks: list[bytes] = []
    total = 0
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise ValueError("image too large")
        chunks.append(chunk)
    body = b"".join(chunks)
    if not body:
        raise ValueError("empty image")
    return body, content_type
except UnsafeUrlError:
    raise
except Exception as exc:
    last_error = exc
finally:
    if resp is not None:
        resp.close()
```

成功返回也必须经过 finally；不得在 except 中无条件调用未定义的 `resp.close()`。

- [ ] **Step 4: 验证并提交**

```bash
python3 -m pytest tests/test_image_cache.py tests/test_network_safety.py -q
python3 -m pytest tests/ -q
git add image_cache.py tests/test_image_cache.py tests/test_network_safety.py
git commit -m "fix: harden image cache lifecycle"
```

---

### T22：头像真实类型校验

**Files:**
- Modify: `web_server.py` — `upload_avatar`
- Test: `tests/test_access_and_ui_contracts.py`

**Prerequisite:** T12 已提供 `image_validation.detect_image_content_type`；T15 已让 `/avatars/` 响应继承 nosniff。

**Problem:** 上传逻辑只信任 data URL 声明；旧方案只截 8 字节却读取 WebP 的 8:12 区间，会拒绝所有 WebP。

- [ ] **Step 1: 写失败测试**

分别上传合法 JPEG/PNG/GIF/WebP、HTML 伪装 PNG、PNG 声明 JPEG、损坏 base64。合法类型成功，声明与真实类型不一致及损坏输入均返回 400；保存扩展名与真实类型一致。

- [ ] **Step 2: 严格解码并比较真实类型**

使用：

```python
raw_bytes = base64.b64decode(raw, validate=True)
actual_mime = detect_image_content_type(raw_bytes)
if actual_mime is None or actual_mime != mime:
    return jsonify({"error": "image content does not match declared type"}), 400
ext = ALLOWED_AVATAR_TYPES[actual_mime]
```

大小限制仍按解码后的 `raw_bytes` 判断。不要新写第二份 magic-byte 逻辑。

- [ ] **Step 3: 验证 nginx 和应用响应**

```bash
python3 -m pytest tests/test_access_and_ui_contracts.py -q -k avatar
curl -sSI http://127.0.0.1:8090/avatars/VALID_TEST_FILE | grep -i x-content-type-options
python3 -m pytest tests/ -q
```

- [ ] **Step 4: 提交**

```bash
git add web_server.py tests/test_access_and_ui_contracts.py
git commit -m "fix: validate uploaded avatar content"
```

---

## 明确非目标

1. 共享 AI 缓存的事实真实性、来源证明、管理员撤销与灰度策略。
2. 将通知收件人强制限制为账号邮箱；T9 只做格式和防御性过滤。
3. 完整 CSP；`CUSTOM_HEAD_HTML` 的第三方脚本需求需单独设计 nonce/hash 策略。
4. `datetime.utcnow()` 技术债。
5. wsrv.nl 隐私策略、多实例分布式锁、抓取进度文件协议重构。
6. 跨源浏览器客户端支持；T15 明确采用同源产品边界。

## 最终验收

- [ ] 按权威顺序确认 T1–T22 每项恰好一个提交，且提交只包含对应任务文件。
- [ ] 运行全量测试并确认不少于 707 项、零失败：

```bash
python3 -m pytest tests/ -q
```

- [ ] 构建并启动最终容器：

```bash
docker compose build --no-cache
docker compose up -d
docker compose ps
```

- [ ] 确认三个服务健康、Python 进程非 root、bind mount 可写、子进程可自动拉起。
- [ ] 对静态和代理路由验证安全头、无宽松 CORS、超大请求返回 413。
- [ ] 用真实浏览器完成注册/登录、新闻列表、详情、收藏、设置、管理员状态和头像上传回归。
- [ ] 检查工作区和提交序列：

```bash
git status --short
git log --oneline -25
```
