# 邮件投递失败管理员告警 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resend 邮件通道无法投递时，仅以站内通知向所有管理员告警一次，并在邮件通道成功后允许下一轮故障再次告警。

**Architecture:** 在 `web_server.py` 的统一通知邮件入口 `_send_notification_email()` 中记录缺少 Resend Key 和 Resend 发送异常。使用既有 app-state 原子标记抑制持续故障；告警通过专用的“仅站内”辅助函数写入管理员通知，绝不回流至邮件入口。一次成功的统一通知邮件清除标记。

**Tech Stack:** Python 3.12、Flask、SQLite、pytest、Resend HTTP API。

## Global Constraints

- 仅覆盖 `_send_notification_email()`，不修改直接调用 `notifier.send_email()` 的邀请、注册或历史清理流程。
- 告警类型固定为 `email_delivery_failed`，标题固定为 `邮件推送服务不可用`。
- 告警只能写入站内通知，不能调用 `_notify_user()`、`_notify_admins()` 或 `_send_notification_email()`。
- 故障原因应压缩为单行且最多 300 字符；不得包含 `RESEND_API_KEY`。
- 发送成功只解除故障抑制，不产生恢复通知。

---

### Task 1: 持久化邮件通道故障告警与仅站内管理员投递

**Files:**
- Modify: `web_server.py:1256-1347`
- Test: `tests/test_email_delivery_failure_alert.py`

**Interfaces:**
- Consumes: `claim_app_state_flag(key) -> bool`、`set_app_state(key, value) -> None`、`get_app_state(key) -> str | None`、`list_users() -> list[dict]`、`add_notification(user_id, ntype, title, body) -> int`
- Produces: `_note_email_delivery_failure(reason: str) -> None` and `_clear_email_delivery_failure_alert() -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_email_delivery_failure_alert.py` with an isolated app-state fake and an admin/user fixture. Assert that first failure writes a notification only for every admin and that a second failure while the flag is set writes none:

```python
import web_server


def test_first_delivery_failure_alerts_each_admin_once(monkeypatch):
    stored = {"email_delivery_failure_alerted": "0"}
    notices = []
    monkeypatch.setattr(web_server, "claim_app_state_flag", lambda key: (
        False if stored.get(key) == "1" else (stored.__setitem__(key, "1") or True)
    ))
    monkeypatch.setattr(web_server, "set_app_state", lambda key, value: stored.__setitem__(key, str(value)))
    monkeypatch.setattr(web_server, "list_users", lambda: [
        {"id": 1, "role": "admin"}, {"id": 2, "role": "user"}, {"id": 3, "role": "admin"},
    ])
    monkeypatch.setattr(web_server, "add_notification", lambda *args: notices.append(args) or 1)

    web_server._note_email_delivery_failure("Resend error: invalid_api_key")
    web_server._note_email_delivery_failure("Resend error: invalid_api_key")

    assert [notice[0] for notice in notices] == [1, 3]
    assert all(notice[1] == "email_delivery_failed" for notice in notices)
    assert all(notice[2] == "邮件推送服务不可用" for notice in notices)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest -q tests/test_email_delivery_failure_alert.py::test_first_delivery_failure_alerts_each_admin_once`

Expected: FAIL because `_note_email_delivery_failure` does not exist.

- [ ] **Step 3: Implement the minimal alert helpers**

Add constants near the existing notification helpers and implement direct in-app delivery:

```python
EMAIL_DELIVERY_FAILURE_ALERTED_STATE_KEY = "email_delivery_failure_alerted"
EMAIL_DELIVERY_FAILURE_TITLE = "邮件推送服务不可用"


def _note_email_delivery_failure(reason: str) -> None:
    safe_reason = re.sub(r"\s+", " ", str(reason or "邮件发送失败")).strip()[:300]
    try:
        if not claim_app_state_flag(EMAIL_DELIVERY_FAILURE_ALERTED_STATE_KEY):
            return
    except Exception:
        pass
    try:
        admins = [user for user in list_users() if user.get("role") == "admin"]
        for admin in admins:
            add_notification(
                admin["id"], "email_delivery_failed", EMAIL_DELIVERY_FAILURE_TITLE,
                f"邮件推送服务不可用。原因：{safe_reason}\n\n"
                "请检查 RESEND_API_KEY、Resend 账户状态和 RAYNEWS_FROM_EMAIL 配置。",
            )
    except Exception as exc:
        print(f"[notify] email delivery failure alert failed: {exc}")


def _clear_email_delivery_failure_alert() -> None:
    try:
        set_app_state(EMAIL_DELIVERY_FAILURE_ALERTED_STATE_KEY, "0")
    except Exception as exc:
        print(f"[notify] email delivery failure alert clear failed: {exc}")
```

Adjust the helper so an app-state claim exception still makes a best-effort direct alert, but does not raise. Do not call any email-capable notification helper.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest -q tests/test_email_delivery_failure_alert.py::test_first_delivery_failure_alerts_each_admin_once`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web_server.py tests/test_email_delivery_failure_alert.py
git commit -m "feat: alert admins when mail delivery fails"
```

### Task 2: Wire detection and recovery into unified notification emails

**Files:**
- Modify: `web_server.py:1256-1304`
- Modify: `tests/test_email_delivery_failure_alert.py`

**Interfaces:**
- Consumes: `_note_email_delivery_failure(reason: str) -> None` and `_clear_email_delivery_failure_alert() -> None`
- Produces: `_send_notification_email(...) -> bool` records no-key and Resend exceptions, and clears failure suppression after a successful `send_email()`.

- [ ] **Step 1: Write the failing tests**

Append tests that drive the real `_send_notification_email()` but stub outbound `send_email`. Verify missing key alerts admins, send exceptions alert admins, success clears the flag, and a later failure alerts again:

```python
def test_missing_resend_key_from_notification_email_alerts_admins(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    reasons = []
    monkeypatch.setattr(web_server, "_note_email_delivery_failure", reasons.append)

    assert web_server._send_notification_email(7, "标题", "正文") is False

    assert reasons == ["RESEND_API_KEY 未配置"]


def test_success_clears_suppression_and_later_failure_alerts_again(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setattr(web_server, "_notification_recipient", lambda user_id: "user@example.com")
    cleared = []
    failures = []
    monkeypatch.setattr(web_server, "_clear_email_delivery_failure_alert", lambda: cleared.append(True))
    monkeypatch.setattr(web_server, "_note_email_delivery_failure", failures.append)
    monkeypatch.setattr(web_server, "send_email", lambda *args, **kwargs: {"id": "sent"})

    assert web_server._send_notification_email(7, "标题", "正文") is True
    monkeypatch.setattr(web_server, "send_email", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("invalid api key")))

    assert web_server._send_notification_email(7, "标题", "正文") is False
    assert cleared == [True]
    assert failures == ["invalid api key"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest -q tests/test_email_delivery_failure_alert.py -k 'missing_resend or success_clears'`

Expected: FAIL because `_send_notification_email()` currently returns early without reporting a missing key and never clears the alert state after a successful send.

- [ ] **Step 3: Implement minimal wiring**

Change `_send_notification_email()` to report the missing-key case before returning and to clear suppression only after `send_email()` completes successfully:

```python
if not api_key:
    _note_email_delivery_failure("RESEND_API_KEY 未配置")
    return False
if not to_email:
    return False
...
    send_email(...)
    _clear_email_delivery_failure_alert()
    return True
except Exception as exc:
    _note_email_delivery_failure(str(exc))
    print(f"[notify] Failed to send notification email to user {user_id}: {exc}")
    return False
```

Leave missing-recipient behavior unchanged: it is not a Resend service failure. Ensure `send_email` remains imported from `notifier` at module level as it is today.

- [ ] **Step 4: Run focused tests to verify they pass**

Run: `python3 -m pytest -q tests/test_email_delivery_failure_alert.py tests/test_share_suspension_notice_delivery.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web_server.py tests/test_email_delivery_failure_alert.py
git commit -m "feat: detect notification email delivery outages"
```

### Task 3: Regression verification

**Files:**
- Verify: `tests/test_email_delivery_failure_alert.py`
- Verify: `tests/test_share_suspension_notice_delivery.py`
- Verify: `tests/test_system_ai_health_alert.py`

**Interfaces:**
- Consumes: the completed mail failure alert helpers and `_send_notification_email()`.
- Produces: verified behavior without regressions in personal-share or system-AI notification delivery.

- [ ] **Step 1: Run focused notification regression tests**

Run: `python3 -m pytest -q tests/test_email_delivery_failure_alert.py tests/test_share_suspension_notice_delivery.py tests/test_system_ai_health_alert.py`

Expected: PASS.

- [ ] **Step 2: Run the complete test suite and whitespace check**

Run: `python3 -m pytest -q && git diff --check`

Expected: all tests PASS and no whitespace errors.

- [ ] **Step 3: Confirm working tree state**

Run: `git status --short`

Expected: no uncommitted production or test changes; the implementation commits from Tasks 1 and 2 are present.
