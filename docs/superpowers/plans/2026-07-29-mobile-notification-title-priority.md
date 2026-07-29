# 移动端通知标题优先 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在移动端通知列表隐藏日期，使标题获得更多横向空间，同时不改变桌面端或详情页的时间显示。

**Architecture:** 只增加一条窄屏 CSS 覆盖规则。通知 DOM、状态管理、操作按钮和服务端接口全部保持不变；前端静态测试验证移动端规则和详情页时间节点仍存在。

**Tech Stack:** 原生 CSS、HTML、pytest。

## Global Constraints

- 仅在 `@media(max-width:640px)` 中隐藏 `.notif-time`。
- 不改动桌面端 `.notif-time` 默认样式、通知详情时间或通知操作逻辑。
- 不新增依赖、接口、JavaScript 状态或数据库改动。

---

## File Structure

- `frontend/index.html`：通知列表的窄屏 CSS 覆盖规则。
- `tests/test_frontend_refresh_behavior.py`：移动端日期隐藏与详情时间保留的回归断言。

### Task 1: 移动端标题空间释放

**Files:**
- Modify: `frontend/index.html:555-565`（现有 `@media(max-width:640px)` 块）
- Modify: `tests/test_frontend_refresh_behavior.py`

**Interfaces:**
- Produces移动端 CSS：`@media(max-width:640px){.notif-time{display:none}}`。
- Preserves列表 DOM 的 `<span class="notif-time">` 与详情 DOM 的 `.notif-detail-time`。

- [ ] **Step 1: 写入失败回归测试**

在 `tests/test_frontend_refresh_behavior.py` 添加：

```python
def test_mobile_notification_list_hides_time_but_detail_keeps_it():
    mobile_css = HTML[HTML.index('@media(max-width:640px){'):HTML.index('</style>')]
    assert '.notif-time{display:none}' in mobile_css
    assert '<span class="notif-time">${esc(formatNotifTime(n.created_at))}</span>' in HTML
    assert '<div class="notif-detail-time">${esc(formatNotifTime(n.created_at))}</div>' in HTML
```

- [ ] **Step 2: 验证测试为红色**

Run:

```bash
python3 -m pytest -q tests/test_frontend_refresh_behavior.py::test_mobile_notification_list_hides_time_but_detail_keeps_it
```

Expected: FAIL，因为移动端样式中尚无 `.notif-time{display:none}`。

- [ ] **Step 3: 实现最小 CSS 覆盖**

在现有移动端媒体查询中加入：

```css
.notif-time{display:none}
```

不要修改 `.notif-time` 的默认规则、通知详情 CSS 或通知列表 HTML。

- [ ] **Step 4: 验证测试为绿色**

Run:

```bash
python3 -m pytest -q tests/test_frontend_refresh_behavior.py::test_mobile_notification_list_hides_time_but_detail_keeps_it
```

Expected: PASS。

- [ ] **Step 5: 执行通知前端回归测试**

Run:

```bash
python3 -m pytest -q tests/test_frontend_refresh_behavior.py -k notification
git diff --check
```

Expected: 所有通知相关测试通过，`git diff --check` 无输出。

- [ ] **Step 6: 提交实现**

```bash
git add frontend/index.html tests/test_frontend_refresh_behavior.py docs/superpowers/plans/2026-07-29-mobile-notification-title-priority.md
git commit -m "style: prioritize notification titles on mobile"
```
