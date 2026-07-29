# 通知操作 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为当前用户提供单条和批量的通知已读、删除操作，并让慢网络下的列表、详情和未读角标保持一致。

**Architecture:** 模型层提供用户范围内的批量已读、单条删除和全部删除函数，Flask 路由只将当前 `g.user_id` 传入这些函数。前端在通知状态块中维护待删除 ID 与批量删除标记，先乐观更新 UI，再异步调用 API；通知 GET 刷新会过滤待删除条目并合并待已读状态。

**Tech Stack:** Python 3、SQLite、Flask、原生 JavaScript/CSS、pytest、Node `vm` 前端行为测试、Playwright。

## Global Constraints

- 所有通知写操作必须使用 `require_role("user", "admin")`，只能影响 `g.user_id` 对应的记录。
- `DELETE /notifications/<nid>` 和 `DELETE /notifications` 必须幂等；未知 ID、重复删除或其他用户 ID 不泄露存在性。
- “全部删除”必须在前端 `confirm()` 返回真时才请求；单条删除不弹确认。
- 未读通知在列表中按钮顺序固定为“已读”“删除”，不再渲染“查看”。
- 删除按钮为红色实底色；详情底部同样使用红色删除按钮。
- 不新增数据库表、字段、第三方依赖、回收站或撤销功能。

---

## File Structure

- `models.py`：通知的用户范围 SQL 写操作。
- `web_server.py`：认证后的通知 action 路由和 JSON 响应。
- `frontend/index.html`：通知面板结构、CSS、乐观状态合并和事件处理。
- `tests/test_notifications.py`：模型层隔离、计数和幂等回归测试。
- `tests/test_notification_actions.py`：Flask action 路由的认证、用户隔离和响应契约测试。
- `tests/test_frontend_refresh_behavior.py`：通知 DOM、乐观删除、批量已读和刷新竞争的 Node 回归测试。

## Task 1: 用户范围通知写模型

**Files:**
- Modify: `models.py:969-990`
- Modify: `tests/test_notifications.py`

**Interfaces:**
- Produces `mark_all_notifications_read(user_id: int) -> int`，返回从未读改为已读的行数。
- Produces `delete_notification(user_id: int, notification_id: int) -> bool`，仅在删除该用户现存记录时返回 `True`。
- Produces `delete_all_notifications(user_id: int) -> int`，返回该用户被删除的记录数。

- [ ] **Step 1: 写入模型失败测试**

在 `NotificationsModelTests` 中加入以下测试：

```python
def test_mark_all_read_only_updates_the_owner_unread_rows(self):
    a_unread = models.add_notification(self.user_a, "general", "A 未读", "")
    a_read = models.add_notification(self.user_a, "general", "A 已读", "")
    b_unread = models.add_notification(self.user_b, "general", "B 未读", "")
    models.mark_notification_read(self.user_a, a_read)

    assert models.mark_all_notifications_read(self.user_a) == 1
    assert models.count_unread_notifications(self.user_a) == 0
    assert models.count_unread_notifications(self.user_b) == 1
    assert models.list_notifications(self.user_b)[0]["id"] == b_unread
    assert any(row["id"] == a_unread and row["read_at"] for row in models.list_notifications(self.user_a))


def test_delete_notification_is_scoped_and_idempotent(self):
    a_id = models.add_notification(self.user_a, "general", "A", "")
    b_id = models.add_notification(self.user_b, "general", "B", "")

    assert models.delete_notification(self.user_b, a_id) is False
    assert models.count_unread_notifications(self.user_a) == 1
    assert models.delete_notification(self.user_a, a_id) is True
    assert models.delete_notification(self.user_a, a_id) is False
    assert [row["id"] for row in models.list_notifications(self.user_b)] == [b_id]


def test_delete_all_notifications_only_removes_the_owner_rows(self):
    models.add_notification(self.user_a, "general", "A1", "")
    models.add_notification(self.user_a, "general", "A2", "")
    b_id = models.add_notification(self.user_b, "general", "B", "")

    assert models.delete_all_notifications(self.user_a) == 2
    assert models.delete_all_notifications(self.user_a) == 0
    assert models.list_notifications(self.user_a) == []
    assert [row["id"] for row in models.list_notifications(self.user_b)] == [b_id]
```

- [ ] **Step 2: 验证模型测试为红色**

Run:

```bash
python3 -m pytest -q tests/test_notifications.py
```

Expected: FAIL，报出 `models` 缺少 `mark_all_notifications_read`、`delete_notification` 或 `delete_all_notifications`。

- [ ] **Step 3: 实现最小模型 API**

在 `mark_notification_read()` 后定义：

```python
def mark_all_notifications_read(user_id: int) -> int:
    db = get_db()
    now = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
        (now, user_id),
    )
    db.commit()
    return cur.rowcount


def delete_notification(user_id: int, notification_id: int) -> bool:
    db = get_db()
    cur = db.execute(
        "DELETE FROM notifications WHERE id = ? AND user_id = ?",
        (notification_id, user_id),
    )
    db.commit()
    return cur.rowcount > 0


def delete_all_notifications(user_id: int) -> int:
    db = get_db()
    cur = db.execute("DELETE FROM notifications WHERE user_id = ?", (user_id,))
    db.commit()
    return cur.rowcount
```

- [ ] **Step 4: 验证模型测试为绿色**

Run:

```bash
python3 -m pytest -q tests/test_notifications.py
```

Expected: PASS。

- [ ] **Step 5: 提交模型实现**

```bash
git add models.py tests/test_notifications.py
git commit -m "feat: add notification action models"
```

## Task 2: 认证后的通知 action 路由

**Files:**
- Modify: `web_server.py:40-45, 779-786`
- Create: `tests/test_notification_actions.py`

**Interfaces:**
- Consumes Task 1 的 `mark_all_notifications_read`、`delete_notification`、`delete_all_notifications`。
- Produces `POST /notifications/read-all -> {"ok": true, "unread": 0}`。
- Produces `DELETE /notifications/<int:nid> -> {"ok": true, "unread": int}`。
- Produces `DELETE /notifications -> {"ok": true, "unread": 0}`。

- [ ] **Step 1: 写入路由失败测试**

创建 `tests/test_notification_actions.py`，使用临时 `models.DB_FILE`、`web_server.app.test_client()` 和 `web_server.create_token()` 建立两个普通用户。测试以下契约：

```python
def test_notification_actions_require_auth(client):
    assert client.post("/notifications/read-all").status_code == 401
    assert client.delete("/notifications/1").status_code == 401
    assert client.delete("/notifications").status_code == 401


def test_read_all_and_delete_routes_are_scoped_to_the_current_user(client, user_a, user_b):
    a_unread = models.add_notification(user_a, "general", "A 未读", "")
    a_delete = models.add_notification(user_a, "general", "A 删除", "")
    b_id = models.add_notification(user_b, "general", "B", "")

    read_all = client.post("/notifications/read-all", headers=headers(user_a))
    assert read_all.get_json() == {"ok": True, "unread": 0}
    assert models.count_unread_notifications(user_b) == 1

    foreign = client.delete(f"/notifications/{b_id}", headers=headers(user_a))
    assert foreign.get_json()["ok"] is True
    assert models.list_notifications(user_b)[0]["id"] == b_id

    deleted = client.delete(f"/notifications/{a_delete}", headers=headers(user_a))
    assert deleted.get_json() == {"ok": True, "unread": 0}
    assert a_unread in [row["id"] for row in models.list_notifications(user_a)]


def test_delete_all_is_idempotent_and_leaves_other_users_notifications(client, user_a, user_b):
    models.add_notification(user_a, "general", "A", "")
    b_id = models.add_notification(user_b, "general", "B", "")

    assert client.delete("/notifications", headers=headers(user_a)).get_json() == {"ok": True, "unread": 0}
    assert client.delete("/notifications", headers=headers(user_a)).get_json() == {"ok": True, "unread": 0}
    assert models.list_notifications(user_a) == []
    assert models.list_notifications(user_b)[0]["id"] == b_id
```

- [ ] **Step 2: 验证路由测试为红色**

Run:

```bash
python3 -m pytest -q tests/test_notification_actions.py
```

Expected: FAIL，路由返回 404 或缺少导入的模型函数。

- [ ] **Step 3: 实现和导入路由**

在 `web_server.py` 的 models 导入列表中加入三个 Task 1 函数；在现有单条已读路由后加入：

```python
@app.route("/notifications/read-all", methods=["POST"])
@require_role("user", "admin")
def mark_all_notifications_read_route():
    mark_all_notifications_read(g.user_id)
    return jsonify({"ok": True, "unread": count_unread_notifications(g.user_id)})


@app.route("/notifications/<int:nid>", methods=["DELETE"])
@require_role("user", "admin")
def delete_notification_route(nid):
    delete_notification(g.user_id, nid)
    return jsonify({"ok": True, "unread": count_unread_notifications(g.user_id)})


@app.route("/notifications", methods=["DELETE"])
@require_role("user", "admin")
def delete_all_notifications_route():
    delete_all_notifications(g.user_id)
    return jsonify({"ok": True, "unread": 0})
```

- [ ] **Step 4: 验证路由测试为绿色**

Run:

```bash
python3 -m pytest -q tests/test_notification_actions.py tests/test_notification_broadcast.py
```

Expected: PASS。

- [ ] **Step 5: 提交路由实现**

```bash
git add web_server.py tests/test_notification_actions.py
git commit -m "feat: add notification action routes"
```

## Task 3: 通知列表和详情操作 UI

**Files:**
- Modify: `frontend/index.html:427-451, 880-898, 4866-5135`
- Modify: `tests/test_frontend_refresh_behavior.py`

**Interfaces:**
- Consumes Task 2 的三个 HTTP 路由和现有 `POST /notifications/<id>/read`。
- Produces `markAllNotifRead() -> Promise<boolean>`、`deleteNotif(id: number, fromDetail?: boolean) -> Promise<boolean>`、`deleteAllNotifications() -> Promise<boolean>`。
- Produces `notifPendingDeletes: Map<number, object>` 和 `notifDeleteAllPending: boolean`，供 `refreshNotifStatus()` 在接受 GET 快照前合并。

- [ ] **Step 1: 写入前端失败测试**

在 `tests/test_frontend_refresh_behavior.py` 的通知测试区域添加一个测试源辅助函数：

```python
def notification_source_with_state_helpers():
    source = "function esc(value) { return String(value); }\n" + source_between(
        "// ═══ In-App Notifications", "// ═══ Admin Panel"
    )
    return source + """
globalThis.__setNotifTestState = (items, unread) => {
  notifItems = items;
  notifUnread = unread;
  notifLoadState = 'ready';
};
globalThis.__getNotifTestState = () => ({
  items: notifItems.map(item => ({ ...item })),
  unread: notifUnread,
});
"""
```

随后添加以下测试；每个测试使用真实通知状态块，只为 DOM 和网络边界提供最小替身：

```python
def test_notification_list_uses_read_then_red_delete_without_view_button():
    source = notification_source_with_state_helpers()
    run_node(source, """
const body = { innerHTML: '' };
const menuBadge = { style: {}, textContent: '' };
context.document = {
  querySelector: () => ({ classList: { toggle: () => {} } }),
  getElementById: id => id === 'notifBody' ? body : menuBadge,
};
context.__setNotifTestState([
  { id: 7, title: '公告', created_at: '2026-07-29T00:00:00', read_at: null },
], 1);
context.renderNotifList();
assert.match(body.innerHTML, /markNotifRead\(7\)/);
assert.match(body.innerHTML, /deleteNotif\(7\)/);
assert.ok(body.innerHTML.indexOf('markNotifRead(7)') < body.innerHTML.indexOf('deleteNotif(7)'));
assert.doesNotMatch(body.innerHTML, />查看</);
assert.match(body.innerHTML, /notif-delete-btn/);
""")
    assert 'id="notifListActions"' in HTML
    assert 'onclick="markAllNotifRead()"' in HTML
    assert 'onclick="deleteAllNotifications()"' in HTML
    assert '.notif-delete-btn{background:#ef4444' in HTML


def test_notification_detail_renders_read_and_delete_actions_for_an_unread_item():
    source = notification_source_with_state_helpers()
    run_node(source, """
const body = { innerHTML: '' };
const elements = {
  notifBody: body,
  notifBackBtn: { style: {} },
  notifPanelTitle: { textContent: '' },
  notifListActions: { style: {} },
  notifMenuBadge: { style: {}, textContent: '' },
};
context.document = {
  querySelector: () => ({ classList: { toggle: () => {} } }),
  getElementById: id => elements[id],
};
context.__setNotifTestState([
  { id: 7, title: '公告', body: '正文', format: 'plain', created_at: '2026-07-29T00:00:00', read_at: null },
], 1);
context.showNotifDetail(7);
assert.match(body.innerHTML, /markNotifRead\(7, true\)/);
assert.match(body.innerHTML, /deleteNotif\(7, true\)/);
assert.match(body.innerHTML, /notif-delete-primary/);
""")


def test_delete_all_notifications_requires_confirmation_before_requesting():
    source = notification_source_with_state_helpers()
    run_node(source, """
const body = { innerHTML: '' };
const menuBadge = { style: {}, textContent: '' };
context.document = {
  querySelector: () => ({ classList: { toggle: () => {} } }),
  getElementById: id => id === 'notifBody' ? body : menuBadge,
};
context.authToken = 'token';
context.confirm = () => false;
context.apiFetch = () => { throw new Error('request must not be sent'); };
context.__setNotifTestState([
  { id: 7, title: '公告', created_at: '2026-07-29T00:00:00', read_at: null },
], 1);
assert.equal(await context.deleteAllNotifications(), false);
assert.equal(context.__getNotifTestState().items[0].id, 7);
assert.equal(context.__getNotifTestState().unread, 1);
""")


def test_optimistic_notification_delete_filters_a_stale_refresh_and_rolls_back_on_error():
    source = notification_source_with_state_helpers()
    run_node(source, """
const body = { innerHTML: '' };
const menuBadge = { style: {}, textContent: '' };
context.document = {
  querySelector: () => ({ classList: { toggle: () => {} } }),
  getElementById: id => id === 'notifBody' ? body : menuBadge,
};
context.authToken = 'token';
context.showToast = () => {};
let rejectDelete;
context.apiFetch = url => {
  if (url === '/notifications/7') return new Promise((resolve, reject) => { rejectDelete = reject; });
  return Promise.resolve({
    items: [{ id: 7, title: '公告', created_at: '2026-07-29T00:00:00', read_at: null }],
    unread: 1,
  });
};
context.__setNotifTestState([
  { id: 7, title: '公告', created_at: '2026-07-29T00:00:00', read_at: null },
], 1);
const request = context.deleteNotif(7);
assert.deepEqual(context.__getNotifTestState().items, []);
assert.equal(context.__getNotifTestState().unread, 0);
await context.refreshNotifStatus();
assert.deepEqual(context.__getNotifTestState().items, []);
rejectDelete(new Error('offline'));
assert.equal(await request, false);
assert.equal(context.__getNotifTestState().items[0].id, 7);
assert.equal(context.__getNotifTestState().unread, 1);
""")
```

- [ ] **Step 2: 验证前端测试为红色**

Run:

```bash
python3 -m pytest -q tests/test_frontend_refresh_behavior.py -k notification
```

Expected: FAIL，因为当前 HTML 仍含“查看”按钮，且缺少批量/删除函数和危险样式。

- [ ] **Step 3: 实现通知操作结构、样式和状态**

在通知面板 `.fav-header` 的标题区域后加入只在列表视图显示的操作组：

```html
<div class="notif-list-actions" id="notifListActions">
  <button class="notif-btn" onclick="markAllNotifRead()">全部已读</button>
  <button class="notif-btn notif-delete-btn" onclick="deleteAllNotifications()">全部删除</button>
</div>
```

补充 CSS：

```css
.notif-list-actions{display:flex;align-items:center;gap:6px;margin-left:auto;margin-right:8px}
.notif-delete-btn{background:#ef4444;border-color:#ef4444;color:#fff}
.notif-delete-btn:hover{background:#dc2626;border-color:#dc2626;color:#fff}
.notif-detail-footer{gap:10px}
.notif-delete-primary{background:#ef4444;border:1px solid #ef4444;color:#fff;padding:9px 32px;border-radius:10px;font:inherit;font-size:13px;font-weight:700;cursor:pointer;transition:opacity .15s}
.notif-delete-primary:hover{opacity:.85}
```

在通知状态块中：

1. 声明 `let notifPendingDeletes = new Map();` 与 `let notifDeleteAllPending = false;`；在 `resetNotifState()` 清空它们。
2. 在 `refreshNotifStatus()` 接受响应前，若 `notifDeleteAllPending` 为真，保留本地空列表；否则过滤 `notifPendingDeletes` 中的 ID，再调用 `reconcilePendingNotifReads()`。
3. `renderNotifList()` 删除“查看”按钮，未读行按顺序渲染：

```html
<button class="notif-btn read-btn" onclick="event.stopPropagation();markNotifRead(${n.id})">已读</button>
<button class="notif-btn notif-delete-btn" onclick="event.stopPropagation();deleteNotif(${n.id})">删除</button>
```

已读行仅渲染第二个按钮。
4. `showNotifList()` 显示 `#notifListActions`；`showNotifDetail()` 隐藏它，并为未读通知渲染“已读”与“删除”，已读通知只渲染“删除”。
5. `deleteNotif()` 记录被删除条目和索引、将其从 `notifItems` 移除、按需递减未读数并立即重绘；请求 `DELETE /notifications/<id>`。成功后丢弃待删除记录并触发权威刷新；失败时按原索引恢复条目和未读数。
6. `markAllNotifRead()` 先将所有本地未读行写入已有 `notifPendingReads` 语义并将 `notifUnread` 置零，随后请求 `POST /notifications/read-all`；失败时恢复调用前的 `read_at` 快照和未读数。
7. `deleteAllNotifications()` 先执行 `if (!confirm('确定删除全部通知吗？')) return Promise.resolve(false);`；确认后保存完整列表和未读数、设置 `notifDeleteAllPending = true`、清空列表，请求 `DELETE /notifications`；失败时恢复快照，成功后清除 pending 标记并刷新。

- [ ] **Step 4: 验证前端测试为绿色**

Run:

```bash
python3 -m pytest -q tests/test_frontend_refresh_behavior.py -k notification
```

Expected: PASS。

- [ ] **Step 5: 提交前端实现**

```bash
git add frontend/index.html tests/test_frontend_refresh_behavior.py
git commit -m "feat: add notification read and delete actions"
```

## Task 4: 集成验证与界面 QA

**Files:**
- Modify: none
- Test: `tests/test_notifications.py`, `tests/test_notification_actions.py`, `tests/test_notification_broadcast.py`, `tests/test_frontend_refresh_behavior.py`

**Interfaces:**
- Consumes Task 1–3 的模型函数、Flask routes 和浏览器 UI。
- Produces可审查的全量测试输出和本地 Playwright 交互证据。

- [ ] **Step 1: 运行通知相关集成测试**

Run:

```bash
python3 -m pytest -q \
  tests/test_notifications.py \
  tests/test_notification_actions.py \
  tests/test_notification_broadcast.py \
  tests/test_frontend_refresh_behavior.py
```

Expected: PASS。

- [ ] **Step 2: 用慢响应执行 Playwright 验证**

启动临时 Flask QA 服务，创建一个有未读和已读通知的用户，并让 `POST /notifications/read-all` 与 `DELETE /notifications*` 延迟 1.5 秒。用 Playwright 验证：

```text
登录 -> 我的通知 -> 单条删除 -> 条目立即消失 -> 关闭并重开仍不存在
登录 -> 我的通知 -> 全部已读 -> 所有 new 标签立即消失
登录 -> 我的通知 -> 全部删除 -> 取消确认时不发请求 -> 确认后列表立即为空
登录 -> 我的通知 -> 打开详情 -> 删除 -> 返回空列表或剩余列表
```

捕获通知面板截图，并检查控制台中没有通知相关错误。

- [ ] **Step 3: 运行完整回归套件**

Run:

```bash
git diff --check && python3 -m pytest -q
```

Expected: `git diff --check` 无输出，pytest 退出码 0。

- [ ] **Step 4: 提交验证后的最终变更**

若前述任务的文件均已按任务提交，此步骤只确认工作树除预先存在的改动外没有未提交的通知 action 文件：

```bash
git status --short
git log --oneline -4
```

Expected: `models.py`、`web_server.py`、`frontend/index.html` 和三个通知测试文件的 action 改动均已在对应提交中。
