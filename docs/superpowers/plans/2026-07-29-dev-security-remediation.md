# Dev 分支安全问题修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 dev 分支的首管理员竞争、服务端 SSRF、错误与诊断泄露、认证/邀请码滥用和共享 AI 缓存未授权写入，同时保持已确认的正常使用路径。

**Architecture:** 在模型层添加原子账户创建和持久化认证/邀请码限流状态；在独立网络安全模块中集中执行公网 URL 与重定向校验，并由图片抓取与 AI 服务共同使用。Web 层收敛公开错误和诊断，AI 结果缓存增加发布授权及来源元数据。

**Tech Stack:** Python 3.12、Flask、SQLite、requests、BeautifulSoup、pytest。

## Global Constraints

- 仅允许公网 HTTP/HTTPS 图片与 AI Endpoint；禁止 loopback、private、link-local、multicast、reserved 和 unspecified IP，以及重定向到这些地址的目标。
- 不支持自建内网模型；公网模型和公网图片保持可用。
- 客户端错误不得暴露底层异常、文件路径、密钥或第三方响应体。
- `/api/news` 的首页冷启动最小诊断字段保持；`/api/meta` 不返回部署诊断。
- 登录锁定为同一可信客户端 IP + 规范化账号连续 5 次失败后 15 分钟；成功登录重置。
- 同一邮箱的邀请码成功申请进入 15 分钟冷却；发送失败不消耗额度。
- 只有 `is_share_active()` 为真者可写共享 AI 缓存；缓存记录作者、提供商、模型、生成时间。

---

### Task 1: 公网 URL 防护与图片抓取集成

**Files:**
- Create: `network_safety.py`
- Modify: `image_cache.py:111-160`
- Modify: `ai_service.py:181-305`
- Test: `tests/test_network_safety.py`

**Interfaces:**
- Produces: `assert_public_http_url(url: str) -> str`，以及安全的逐跳 GET/POST 请求辅助函数。
- Consumes: `requests`, `socket.getaddrinfo`, `ipaddress.ip_address`。

- [ ] **Step 1: Write failing URL safety tests**

Test direct loopback/private/link-local IPv4 and IPv6, private DNS resolution, a public IP, and a redirect from public to private. Mock DNS and the HTTP transport only at the network boundary.

```python
def test_rejects_private_and_link_local_targets(monkeypatch):
    for url in ("http://127.0.0.1/a", "http://10.0.0.1/a", "http://169.254.169.254/a", "http://[::1]/a"):
        with pytest.raises(UnsafeUrlError):
            assert_public_http_url(url)


def test_rejects_hostname_resolving_to_private_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("10.0.0.2", 0))])
    with pytest.raises(UnsafeUrlError):
        assert_public_http_url("https://model.example/v1")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python3 -m pytest -q tests/test_network_safety.py`

Expected: FAIL because the module and safety API do not exist.

- [ ] **Step 3: Implement the minimal shared safety module**

Implement URL syntax validation, `getaddrinfo` resolution, non-global IP rejection, manual redirect handling with a bounded limit, and safe request helpers. Disable implicit redirects. Ensure errors are safe client-facing `UnsafeUrlError` messages without echoing target internals.

- [ ] **Step 4: Integrate images and AI**

Replace direct `requests.get` in `image_cache.fetch_remote_image()` and direct `requests.post` in `AIService` with safety helpers. Preserve current timeouts, headers, image content-type checks, and public provider request formats.

- [ ] **Step 5: Verify GREEN and commit**

Run: `python3 -m pytest -q tests/test_network_safety.py tests/test_image_cache.py tests/test_ai_chat_relay.py`

Commit:
```bash
git add network_safety.py image_cache.py ai_service.py tests/test_network_safety.py
git commit -m "fix: block private network fetch targets"
```

### Task 2: 原子首管理员和认证/邀请码限流

**Files:**
- Modify: `models.py:230-335,1040-1118`
- Modify: `web_server.py:229-337`
- Test: `tests/test_auth_security.py`

**Interfaces:**
- Produces: `create_registered_user(...) -> tuple[dict | None, bool]` and persistent login/invite rate-limit helpers.
- Consumes: trusted proxy client address from `request.remote_addr` and `X-Real-IP` only for loopback proxy requests.

- [ ] **Step 1: Write failing auth security tests**

Cover atomic first registration, sixth login failure lockout, successful login reset, untrusted spoofed `X-Real-IP` rejection, invite cooldown after success, and failed email delivery not consuming the invite allowance.

```python
def test_concurrent_initial_registration_creates_only_one_admin(...):
    # launch two registration attempts against the same empty SQLite DB
    assert sorted(result["role"] for result in successful) == ["admin"]


def test_login_locks_after_five_failed_attempts(client, user):
    for _ in range(5):
        assert client.post("/auth/login", json={"login": user["email"], "password": "wrong"}).status_code == 401
    assert client.post("/auth/login", json={"login": user["email"], "password": "wrong"}).status_code == 429
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m pytest -q tests/test_auth_security.py`

Expected: FAIL because atomic registration and limit helpers do not exist.

- [ ] **Step 3: Implement atomic registration and rate-limit persistence**

Use a dedicated short-lived SQLite `BEGIN IMMEDIATE` transaction for initial registration. Add schema/migration-backed tables for login failures and invite request cooldowns. Reset failure state after successful authentication. Track invite cooldown only after `send_email()` succeeds; return generic 429 messages with retry time.

- [ ] **Step 4: Verify GREEN and commit**

Run: `python3 -m pytest -q tests/test_auth_security.py tests/test_access_and_ui_contracts.py`

Commit:
```bash
git add models.py web_server.py tests/test_auth_security.py
git commit -m "fix: harden registration and auth throttling"
```

### Task 3: 错误响应、公共元数据与共享缓存授权

**Files:**
- Modify: `web_server.py:145-156,1038-1065,3850-4160,4223-4250`
- Modify: `refresh_server.py:485-550`
- Modify: `frontend/index.html:4525-4685`
- Test: `tests/test_security_hardening.py`
- Test: `tests/test_refresh_jobs.py`

**Interfaces:**
- Consumes: `is_share_active(settings) -> bool` and authenticated caller settings/config.
- Produces: AI cache metadata fields for summary and translation author/provider/model/generated time.

- [ ] **Step 1: Write failing tests**

Add tests proving unhandled errors are generic, `/api/meta` excludes diagnostics, inactive sharing cannot post results, active sharing can post, and stored cache metadata is returned only through the intended internal model API or verified database rows.

```python
def test_unhandled_error_does_not_echo_exception(client):
    response = client.get("/route-that-raises")
    assert "secret detail" not in response.get_json()["error"]


def test_inactive_share_user_cannot_publish_shared_result(client, headers):
    response = client.post("/ai/result/42", headers=headers, json={"summary": "x"})
    assert response.status_code == 403
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python3 -m pytest -q tests/test_security_hardening.py tests/test_refresh_jobs.py -k 'error or meta or share'`

Expected: FAIL because the current handlers expose diagnostics and permit inactive publishers.

- [ ] **Step 3: Implement response hardening and cache provenance**

Return generic error payloads while logging exceptions. Reduce `/api/meta` to `{"count": count}`. Add nullable metadata columns through existing migration code, record publisher metadata on result save, and require active sharing on write. Change frontend publication calls to run only when `userAutoSettings.share_active` is true while retaining local display.

- [ ] **Step 4: Verify GREEN and commit**

Run: `python3 -m pytest -q tests/test_security_hardening.py tests/test_refresh_jobs.py tests/test_share_recovery.py tests/test_ai_relay_frontend.py`

Commit:
```bash
git add web_server.py refresh_server.py frontend/index.html tests/test_security_hardening.py tests/test_refresh_jobs.py
git commit -m "fix: restrict shared AI cache publication"
```

### Task 4: 全量验证

**Files:**
- Verify: all changed files and test suites.

- [ ] **Step 1: Run security-focused regression tests**

Run: `python3 -m pytest -q tests/test_network_safety.py tests/test_auth_security.py tests/test_security_hardening.py tests/test_image_cache.py tests/test_share_recovery.py`

Expected: PASS.

- [ ] **Step 2: Run full suite and static checks**

Run: `python3 -m compileall -q . && python3 -m pytest -q && git diff --check`

Expected: all tests PASS, no compile failures, no whitespace errors.

- [ ] **Step 3: Commit plan documentation and confirm clean tree**

```bash
git add docs/superpowers/plans/2026-07-29-dev-security-remediation.md
git commit -m "docs: plan dev security remediation"
git status --short
```
