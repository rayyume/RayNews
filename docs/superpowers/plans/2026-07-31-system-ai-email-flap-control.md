# 服务端 AI 邮件防抖 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 防止短时间内服务端 AI 的失败/恢复抖动向管理员重复发送邮件，同时保留首次故障与稳定恢复告警。

**Architecture:** `web_server.py` 先把一次故障事件的恢复条件从“任意一次成功”改为“连续三次真实 provider 调用成功”。随后在 `app_state` 中持久化三态故障事件和最近通知时间，并通过 SQLite `BEGIN IMMEDIATE` 原子地在冷却期内创建静默事件。

**Tech Stack:** Python 3.12、Flask、SQLite、pytest。

## Global Constraints

- 默认连续失败阈值保持 `SYSTEM_AI_FAILURE_ALERT_THRESHOLD=3`。
- 默认连续成功恢复阈值为 `SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD=3`，最小值为 1。
- 默认通知冷却为 `SYSTEM_AI_ALERT_COOLDOWN_SECONDS=1800`，最小值为 0。
- 仅真实服务端 provider 调用可改变健康状态；缓存命中不能触发恢复。
- 通知未送达时必须释放事件，以便下一次失败重试。
- 管理员保存服务端 AI 配置时必须清除活跃事件和冷却状态。

---

### Task 1: 对恢复通知实施连续成功防抖

**Files:**
- Modify: `web_server.py:1535-1660`
- Modify: `tests/test_system_ai_health_alert.py`

**Interfaces:**
- Consumes: 现有 `_note_system_ai_failure(job, error)` 与 `_clear_system_ai_alert() -> bool`。
- Produces: `_note_system_ai_success()` 仅在连续成功达到阈值后发送恢复通知；`_note_system_ai_failure(...)` 清空成功计数。

- [ ] **Step 1: 写出会失败的测试**

Add this helper beside `_fail` and these tests after `test_recovery_is_announced_once_after_an_alert`:

```python
def _success(times):
    for _ in range(times):
        web_server._note_system_ai_success()


def test_system_ai_recovery_requires_consecutive_successes(alerts):
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()

    _success(web_server.SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD - 1)
    assert alerts == []

    web_server._note_system_ai_success()
    assert [item["type"] for item in alerts] == ["system_ai_recovered"] * 2


def test_failure_before_recovery_resets_the_success_streak(alerts):
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()

    _success(web_server.SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD - 1)
    web_server._note_system_ai_failure("自动摘要", "503")
    _success(web_server.SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD - 1)

    assert alerts == []
```

- [ ] **Step 2: 验证测试为红**

Run: `python3 -m pytest tests/test_system_ai_health_alert.py::test_system_ai_recovery_requires_consecutive_successes tests/test_system_ai_health_alert.py::test_failure_before_recovery_resets_the_success_streak -q`

Expected: the first test fails because the first success sends `system_ai_recovered`.

- [ ] **Step 3: 实现最小恢复防抖**

```python
SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD = max(
    1, int(os.environ.get("SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD", "3"))
)

_system_ai_health = {
    "failures": 0, "successes": 0, "alerted": False,
    "last_error": "", "jobs": [],
}

def _note_system_ai_success():
    # Increment successes only while an outage has been alerted; when the
    # threshold is reached, clear the persisted alert and send recovery once.

def _note_system_ai_failure(job, error):
    # Set _system_ai_health["successes"] = 0 before recording the failure.
```

Also reset `successes` in `_reset_system_ai_health()` and every in-memory health reset.

- [ ] **Step 4: 验证测试为绿**

Run: `python3 -m pytest tests/test_system_ai_health_alert.py -q`

Expected: all system-AI health tests pass; no recovery is emitted after one or two successful calls.

- [ ] **Step 5: 提交**

```bash
git add web_server.py tests/test_system_ai_health_alert.py
git commit -m "fix: debounce system AI recovery notices"
```

### Task 2: 对完整故障事件实施持久化冷却

**Files:**
- Modify: `models.py:909-945`
- Modify: `web_server.py:26-49,1535-1660`
- Modify: `tests/test_system_ai_health_alert.py`

**Interfaces:**
- Consumes: `app_state(key, value, updated_at)` 与 Task 1 的连续成功恢复行为。
- Produces: `claim_app_state_incident(key, last_notified_key, cooldown_seconds, now=None) -> str`，返回 `"notify"`、`"suppressed"` 或 `"active"`；`clear_app_state_incident(key) -> str` 返回清除前的状态。

- [ ] **Step 1: 写出会失败的测试**

```python
def test_cooldown_suppresses_a_second_system_ai_incident(alerts, monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    _success(web_server.SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD)
    alerts.clear()

    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    _success(web_server.SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD)

    assert alerts == []


def test_system_ai_alert_can_be_sent_after_cooldown(alerts, monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    _success(web_server.SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD)
    alerts.clear()

    clock["now"] += web_server.SYSTEM_AI_ALERT_COOLDOWN_SECONDS + 1
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)

    assert [item["type"] for item in alerts] == ["system_ai_failed"] * 2
```

Add a retry test which makes `_notify_admins` return `0` for the first incident creation, then `1` for the next failure; assert that the second attempt delivers despite the cooldown.

- [ ] **Step 2: 验证测试为红**

Run: `python3 -m pytest tests/test_system_ai_health_alert.py -q`

Expected: the suppression test fails because the recovered event is immediately eligible to alert again.

- [ ] **Step 3: 实现原子事件声明和冷却**

```python
def claim_app_state_incident(key, last_notified_key, cooldown_seconds, now=None):
    # BEGIN IMMEDIATE; return "active" if key is already "1" or "2".
    # Otherwise write "1" when the stored timestamp is outside cooldown, or
    # "2" when inside; commit before returning "notify"/"suppressed".

def clear_app_state_incident(key):
    # Atomically read the prior state, write "0", commit, and return it.
```

Import these helpers in `web_server.py`. Add `SYSTEM_AI_ALERT_LAST_NOTIFIED_STATE_KEY` and:

```python
SYSTEM_AI_ALERT_COOLDOWN_SECONDS = max(
    0, int(os.environ.get("SYSTEM_AI_ALERT_COOLDOWN_SECONDS", "1800"))
)
```

Call `claim_app_state_incident(...)` when the failure threshold is reached. State `"2"` is a live but silently suppressed event: it prevents repeated failures and closes without a recovery notice. Persist `time.time()` only after `_notify_admins(...)` reports at least one recipient; on a `0` recipient result, atomically clear the state to allow retry. On stable recovery, only prior state `"1"` sends recovery and records the new notification time. `_reset_system_ai_health()` must reset both the incident and timestamp keys.

- [ ] **Step 4: 验证测试为绿**

Run: `python3 -m pytest tests/test_system_ai_health_alert.py -q`

Expected: all system-AI health tests pass, including cooldown suppression, expiry, delivery retry, restart de-duplication, and cached-call boundaries.

- [ ] **Step 5: 运行完整回归**

Run: `python3 -m pytest -q`

Expected: all tests pass.

- [ ] **Step 6: 提交**

```bash
git add models.py web_server.py tests/test_system_ai_health_alert.py
git commit -m "fix: throttle flapping system AI alerts"
```
