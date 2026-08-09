# 服务端 AI 告警防抖与去重 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一个系统 AI 故障只产生一次故障通知和一次稳定恢复通知；低调用量日期也能及时恢复；同根因的每日摘要失败不重复告警。

**Architecture:** app DB 保存三态 incident（0 无事件、1 已通知、2 冷却抑制）和最近通知时间。恢复首先支持连续真实 provider 成功计数；时间兜底使用一个 `BEGIN IMMEDIATE` helper 在同一事务中复核 incident、最近失败、最近成功和稳定窗口后关闭事件，避免检查/关闭竞态。每日摘要只在 incident=1 时抑制专项告警，并继续保留失败记录。

**Tech Stack:** Python 3.12、SQLite、pytest。

## Global Constraints

- 失败阈值 3；恢复成功阈值 3；通知冷却 1800 秒；稳定窗口 3600 秒。
- `SYSTEM_AI_RECOVERY_STABILITY_SECONDS=0` 明确禁用时间兜底。
- 缓存命中不计成功；只有真实 provider 返回成功才调用 `_note_system_ai_success()`。
- 通知零送达时释放 claim；保存新系统 AI 配置时清空 incident、cooldown 和两个时间戳。
- 时间兜底的读取、条件判断和 incident 完成必须在同一 SQLite 事务中。

---

### Task 1: 连续成功恢复和三态冷却

**Files:**
- Modify: `models.py`
- Modify: `web_server.py`
- Modify: `tests/test_system_ai_health_alert.py`

**Interfaces:**
- `claim_app_state_incident(...) -> "notify" | "suppressed" | "active"`
- `complete_app_state_incident(...) -> "0" | "1" | "2"`
- `_note_system_ai_success()` 连续三次后完成事件。

- [ ] **Step 1: 保留/补齐失败测试**

覆盖：1/2 次成功不恢复；失败重置成功计数；冷却内第二事件状态为 2 且无通知；冷却后可通知；零送达释放；重启后 incident 去重；缓存命中不计成功。

- [ ] **Step 2: 验证当前基线**

Run: `python3 -m pytest tests/test_system_ai_health_alert.py -q`

Expected: 当前 Task 1/2 已有实现时 PASS；若从早期提交执行则先红后按下述接口实现。

- [ ] **Step 3: 实现/核对状态机**

```python
SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD = max(1, int(os.environ.get(
    "SYSTEM_AI_RECOVERY_SUCCESS_THRESHOLD", "3"
)))
SYSTEM_AI_ALERT_COOLDOWN_SECONDS = max(0, int(os.environ.get(
    "SYSTEM_AI_ALERT_COOLDOWN_SECONDS", "1800"
)))
```

故障达到阈值后原子 claim；只有 `notify` 发送。`suppressed/active` 保持内存 alerted。连续成功达到阈值时原子 complete；prior=1 发恢复，prior=2 静默关闭。恢复完成应在通知之前提交 cooldown 时间。

- [ ] **Step 4: 验证并提交**

Run: `python3 -m pytest tests/test_system_ai_health_alert.py -q`

```bash
git add models.py web_server.py tests/test_system_ai_health_alert.py
git commit -m "fix: debounce system AI incidents with persistent cooldown"
```

---

### Task 2: 原子时间稳定恢复

**Files:**
- Modify: `models.py`
- Modify: `web_server.py`
- Modify: `tests/test_system_ai_health_alert.py`

**Interfaces:**
- Produces: `complete_app_state_incident_if_stable(key, last_notified_key, last_failure_key, last_success_key, stability_seconds, now=None) -> str`。
- 返回 `"0"` 表示条件不满足/无事件，`"1"` 或 `"2"` 表示原子关闭前状态。

- [ ] **Step 1: 写 models 事务测试**

```python
def test_stable_complete_is_disabled_when_window_is_zero(isolated_db):
    set_app_state("incident", "1")
    set_app_state("failed", "100")
    set_app_state("succeeded", "101")
    assert complete_app_state_incident_if_stable(
        "incident", "notified", "failed", "succeeded", 0, now=10_000
    ) == "0"
    assert get_app_state("incident") == "1"


def test_stable_complete_rechecks_timestamps_in_transaction(isolated_db):
    set_app_state("incident", "1")
    set_app_state("failed", "100")
    set_app_state("succeeded", "101")
    assert complete_app_state_incident_if_stable(
        "incident", "notified", "failed", "succeeded", 60, now=161
    ) == "1"
    assert get_app_state("incident") == "0"
    assert get_app_state("notified") == "161.0"
```

- [ ] **Step 2: 实现事务 helper**

helper 首先 `window=max(0,float(...))`，为 0 直接返回。否则 `BEGIN IMMEDIATE`，一次查询四个 key；仅当 state∈{1,2}、failure>0、success>failure、now-failure>=window 时写 state=0。prior=1 同事务更新 last_notified；不满足 rollback 并返回 0。异常 rollback 后抛出。

- [ ] **Step 3: 写 web_server 行为测试**

```python
def test_quiet_day_recovers_after_one_success_and_stability_window(alerts, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    alerts.clear()
    clock["now"] += 1
    web_server._note_system_ai_success()
    clock["now"] += web_server.SYSTEM_AI_RECOVERY_STABILITY_SECONDS + 1
    assert web_server._maybe_recover_stale_system_ai_incident() is True
    assert [x["type"] for x in alerts] == ["system_ai_recovered"] * 2


def test_failure_after_success_prevents_stable_recovery(alerts, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(web_server.time, "time", lambda: clock["now"])
    _fail(web_server.SYSTEM_AI_FAILURE_ALERT_THRESHOLD)
    clock["now"] += 1; web_server._note_system_ai_success()
    clock["now"] += 10; web_server._note_system_ai_failure("自动翻译", "503")
    clock["now"] += web_server.SYSTEM_AI_RECOVERY_STABILITY_SECONDS + 1
    assert web_server._maybe_recover_stale_system_ai_incident() is False
```

- [ ] **Step 4: 接入时间戳和调度**

新增两个 key。每次真实 failure/success 在 `_system_ai_health_lock` 序列化范围内持久化相应 epoch；failure 必须在任何 alerted 早退之前记录。`_maybe_recover_stale_system_ai_incident()` 只调用事务 helper；prior=1 发恢复，prior=2 静默，并同步清内存状态。`_daily_summary_loop` 每 tick 独立 try 调用一次。

- [ ] **Step 5: 验证并提交**

Run: `python3 -m pytest tests/test_system_ai_health_alert.py -q`

```bash
git add models.py web_server.py tests/test_system_ai_health_alert.py
git commit -m "fix: recover quiet system AI incidents atomically"
```

---

### Task 3: 每日摘要告警按已通知 incident 去重

**Files:**
- Modify: `web_server.py`
- Modify: `tests/test_system_ai_health_alert.py`, `tests/test_daily_summary_retry.py`

**Interfaces:**
- Produces: `_system_ai_incident_is_notified() -> bool`，仅 state==1。

- [ ] **Step 1: 增加隔离 fixture**

所有组合测试使用临时 models.DB_FILE，并在 teardown `models.close_db()`/恢复路径；不要共享真实当前日期的 `daily_summary_failures`。

- [ ] **Step 2: 写真实健康链测试**

```python
def test_same_daily_outage_sends_only_system_ai_alert(isolated_app_db, alerts, monkeypatch):
    def failing(_date):
        web_server._note_system_ai_failure("每日摘要", "401 invalid key")
        web_server._set_daily_summary_error("AI 生成失败")
        return None
    monkeypatch.setattr(web_server, "_generate_daily_summary_global", failing)
    for _ in range(1 + web_server.DAILY_SUMMARY_MAX_RETRIES):
        web_server._broadcast_daily_summary(force=False, bypass_window=True)
    types = [a["type"] for a in alerts]
    assert types.count("system_ai_failed") == 2
    assert "daily_summary_failed" not in types
    state = web_server._get_daily_summary_failure(web_server._today_str())
    assert state["given_up"] == 1


def test_daily_alert_remains_when_system_incident_was_cooldown_suppressed(
        isolated_app_db, alerts, monkeypatch):
    # 先创建并恢复 state=1 事件，再在 cooldown 内创建 state=2；
    # 使用 failing stub 跑完摘要尝试，断言 daily_summary_failed 发给两位 admin。
```

- [ ] **Step 3: 实现去重**

```python
def _system_ai_incident_is_notified() -> bool:
    try:
        return get_app_state(SYSTEM_AI_ALERTED_STATE_KEY) == "1"
    except Exception as exc:
        print(f"[daily-summary] system-AI state read failed: {exc}")
        return False
```

先 `_record_daily_summary_failure`。given_up 时：state=1 仅打印 suppressed，不 claim daily alert；其他状态保持原 claim/send/release。失败记录永远保留。

- [ ] **Step 4: 验证并提交**

Run: `python3 -m pytest tests/test_system_ai_health_alert.py tests/test_daily_summary_retry.py -q`

```bash
git add web_server.py tests/test_system_ai_health_alert.py tests/test_daily_summary_retry.py
git commit -m "fix: deduplicate daily summary and system AI alerts"
```

---

### Task 4: 完整回归

- [ ] Run: `python3 -m pytest tests/test_system_ai_health_alert.py tests/test_daily_summary_retry.py tests/test_notifications.py -q`
- [ ] Run: `python3 -m pytest -q`
- [ ] 用窗口 0、state 1、state 2、重启四种配置手测状态转换。

## Definition of Done

- [ ] 连续三次成功和原子时间稳定两条恢复路径最多发一次恢复。
- [ ] 窗口 0 永不触发时间恢复。
- [ ] failure/success 时间戳与关闭判断不存在读后竞态。
- [ ] 系统 AI 已通知时不再重复发每日摘要失败；state 0/2 仍有专项信号。
- [ ] 配置重置清理所有相关 app_state key。
