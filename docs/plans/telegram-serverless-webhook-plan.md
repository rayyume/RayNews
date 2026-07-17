# Telegram Serverless 实时推送（Webhook 触发刷新）开发方案

> 适用版本：v5.0.3-beta.1 起 · 状态：待开发 · 分支：`serverless` · 2026-07

## 1. 背景与目标

### 1.1 现状

RayNews 目前对 Telegram 频道的信息获取是**纯拉取（Pull）**模式：

- `fetcher.py` 抓取 `t.me/s/<channel>` 公开网页 HTML（`fetch_telegram_page`，`fetcher.py:225`），
  BeautifulSoup 解析出消息列表，再按需抓取 Telegraph / 微信公众号全文；
- `refresh_server.py` 以固定 15 分钟间隔轮询触发（`REFRESH_INTERVAL = 900`，
  `refresh_server.py:33`；`periodic_refresh`，`refresh_server.py:423`）；
- 项目**没有任何 Bot API 的使用**。

痛点：
1. **时效性**：新消息最坏延迟 15 分钟才可见；
2. **无效负载**：每天约 96 次轮询绝大多数是空跑，且非官方网页抓取高频访问有被限流风险；
3. **编辑感知缺失**：增量抓取只取 `id > last_seen_id` 的消息
   （`fetch_all_new_messages`，`fetcher.py:931`），频道内**已入库消息被编辑后永远不会被更新**。

### 1.2 方案概述

利用 Telegram Serverless（<https://core.telegram.org/bots/serverless>，在 Telegram 官方
基础设施上按 update 类型触发 JS handler，支持出站 `fetch`）部署一个**极简转发 bot**：

- bot 被加为目标频道管理员后，频道每发/编辑一条消息，Telegram 实时触发
  `handlers/channel_post.js` / `handlers/edited_channel_post.js`；
- handler 把消息 payload POST 到 RayNews 新增的 webhook 端点（共享密钥鉴权）；
- RayNews 收到后立即触发一次**现有的**增量抓取管线（新消息），或单帖重抓（编辑）；
- 兜底轮询间隔放宽为可配置（建议 2 小时），继续承担补漏、历史回填职责。

**核心原则：serverless 侧只做"事件转发"，所有解析、入库、AI 逻辑全部留在现有 Python
管线中，webhook 不可用时系统无损退化为纯轮询模式。**

关键事实（去重基础）：Bot API 的 `channel_post.message_id` 与公开页
`t.me/<channel>/<id>` 的帖子编号是**同一个 ID**，即现有 `articles.id`
（`process_message` 中 `"id": orig_msg_id`，`fetcher.py:852`）。

### 1.3 明确不做（本方案边界）

- ❌ 不在 serverless 侧解析/清洗消息内容（没有 npm/BeautifulSoup，且会产生第二套解析逻辑）；
- ❌ 不在 serverless 侧下载任何文件字节（平台限制：只能复用 file_id，不支持二进制）；
- ❌ 不移除 `t.me/s` 抓取路径（历史回填、webhook 丢失补漏都依赖它）；
- ❌ 不做 inline search / `/daily` 伴侣 bot（另立方案，见 §8 展望）；
- ❌ Phase 1 不做"payload 直接入库的临时条目"（见 §8，收益需先验证 webhook→刷新的实际延迟）。

## 2. 总体架构

```
Telegram 频道发新帖
        │ (毫秒级 update 推送)
        ▼
Telegram Serverless: handlers/channel_post.js
        │ POST https://<raynews-domain>/webhook/telegram
        │ Header: X-RayNews-Webhook-Token: <secret>
        │ Body: { update 原始 JSON }
        ▼
nginx: location /webhook/ → 127.0.0.1:8082 (web_server.py, Flask)
        │ 校验 token + 校验 chat.username == TELEGRAM_CHANNEL + 去重/限频
        ▼
web_server.py: POST http://127.0.0.1:8081/refresh?trigger=webhook
        ▼
refresh_server.py: start_refresh_job("webhook") → 现有 fetcher 增量管线
```

编辑事件（`edited_channel_post`）走同一入口，但 RayNews 侧改为**单帖重抓**
（`t.me/<channel>/<id>?embed=1&mode=tme`，模板已存在：`TELEGRAM_POST_URL`，
`fetcher.py:48,51`），因为增量抓取的 `last_seen_id` 游标不会回看旧消息。

## 3. Phase 1 — RayNews 侧 webhook 接收与刷新触发

### 3.1 新增环境变量（`docker-compose.yml` + README 环境变量表）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TELEGRAM_WEBHOOK_SECRET` | 空 | 空 = webhook 功能整体禁用（路由返回 404）。建议 ≥32 字节随机串 |
| `REFRESH_INTERVAL_SECONDS` | `900` | 兜底轮询间隔。启用 webhook 后运维手动调大（建议 `7200`） |

`refresh_server.py:33` 的 `REFRESH_INTERVAL = 900` 改为读取
`REFRESH_INTERVAL_SECONDS`（沿用文件内既有的 `os.environ.get` + 默认值模式），
下限钳制 300，防止误配置打爆 t.me。

### 3.2 `refresh_server.py`：`/refresh` 支持 trigger 标注 + 合并期间的补跑

1. `do_POST` 的 `/refresh` 分支（`refresh_server.py:879`）解析 query 参数
   `trigger`，白名单 `{"manual", "webhook"}`，默认 `manual`，传给
   `start_refresh_job(trigger)`。`trigger` 已存在于 job 状态结构中，前端/日志无需改动。
2. **补跑机制（必须）**：`start_refresh_job`（`refresh_server.py:315`）目前在 job
   running 时直接返回当前 job。问题：频道连发多条消息时，第 2 条的 webhook 到达时刷新
   job 可能已越过列表页抓取阶段，该消息会漏到下一次兜底轮询。改法：
   - 新增模块级 `REFRESH_RERUN_PENDING = False`（由 `REFRESH_JOB_LOCK` 保护）；
   - `start_refresh_job` 在 running 且 `trigger == "webhook"` 时置
     `REFRESH_RERUN_PENDING = True` 后照旧返回当前 job；
   - `_run_refresh_job`（`refresh_server.py:280`）在 job 终态落定（`_remember_terminal_job_locked`
     之后）检查该标志：若为 True 则清零并调用 `start_refresh_job("webhook")` 再跑一轮。
     注意避免持锁调用（先在锁内读+清标志，出锁后再触发），防止死锁；
   - 补跑最多连锁一次即可自然收敛（第二轮开始前标志已清零，期间新 webhook 会重新置位）。

### 3.3 `web_server.py`：新增 `POST /webhook/telegram` 路由

放在 web_server（Flask，8082）而非 refresh_server：鉴权工具、`http_req` 内部转发模式
（参考 `protected_refresh`，`web_server.py:4256-4266`）都是现成的。

处理流程（按序短路）：

1. `TELEGRAM_WEBHOOK_SECRET` 为空 → 直接 `404`（对外表现为路由不存在）；
2. 读取 Header `X-RayNews-Webhook-Token`，用 `hmac.compare_digest` 与 secret 比较，
   失败 → `403`（不带任何提示信息）；
3. 解析 JSON body；取 `update = body`，`msg = update.get("channel_post") or
   update.get("edited_channel_post")`，两者皆空 → `200 {"status":"ignored"}`
   （其余 update 类型一律静默接受，方便 serverless 侧无脑转发）；
4. **来源校验**：`msg["chat"]["username"]`（小写比较）必须等于配置的频道名。
   频道名从环境变量解析：复用 `fetcher.py:35` `_resolve_telegram_urls` 的逻辑
   （建议把该函数挪到一个可共享的位置或在 web_server 里做同等解析，实现时二选一，
   不要复制粘贴两份规则）。不匹配 → `200 {"status":"ignored"}`（不给探测者信号）；
5. **限频**：进程内简单滑动窗口，每 60 秒最多接受 30 次有效请求，超出 → `429`；
6. 分派：
   - `channel_post` → 内部 `POST http://127.0.0.1:8081/refresh?trigger=webhook`
     （timeout 5s，参考 `web_server.py:4262`），返回
     `202 {"status":"accepted", "message_id": <id>}`；
   - `edited_channel_post` → Phase 2 的单帖重抓（Phase 1 先同样触发一次全量增量刷新
     并返回 202——虽然增量抓不到旧帖的编辑，但保证接口先行、行为无害；Phase 2 落地后替换）；
7. 全程不信任 payload 内容做任何入库——payload 只用于鉴别与路由。

nginx 配置（`nginx.conf`，参考 `location /auth/` 的写法，`nginx.conf:39`）：

```nginx
location /webhook/ {
    proxy_pass http://127.0.0.1:8082;
    client_max_body_size 64k;
}
```

### 3.4 日志

- webhook 每次有效触发记一行 INFO（含 message_id、update 类型）；
- 403/429/ignored 记 WARNING（限频聚合，避免刷屏）。

## 4. Phase 2 — 编辑事件的单帖重抓

新增能力：对指定 message_id 重抓单帖并更新入库。

1. `fetcher.py` 新增函数 `refetch_single_post(msg_id: int) -> bool`：
   - 请求 `TELEGRAM_POST_URL.format(id=msg_id)`（`?embed=1&mode=tme` 单帖嵌入页），
     沿用 `HEADERS` / `REQUEST_TIMEOUT`；
   - 用现有 `parse_messages`（`fetcher.py:240`）解析（embed 页与列表页的消息 DOM 结构
     一致性需在开发时用真实频道验证；若不一致，为 embed 页写最小适配，**复用**现有的
     字段提取函数而非新写解析器）；
   - 走 `process_message`（`fetcher.py:839`）产出 entry，`upsert_articles` 入库
     （`INSERT OR REPLACE`，`fetcher.py:129`，天然覆盖旧行；tombstone 已在其中过滤，
     `fetcher.py:134-141`——已删除文章的编辑不会复活，正确）；
   - 支持独立进程调用：加一个 `python fetcher.py --refetch-post <id>` 入口
     （argparse，勿影响现有无参调用行为）。
2. `refresh_server.py` 新增 `POST /internal/refetch-post?id=<msg_id>`：
   - 仅监听 127.0.0.1（现状即如此），参考 `/internal/cache-evict`（`refresh_server.py:894`）；
   - 以子进程方式运行 `fetcher.py --refetch-post <id>`（与 `run_fetcher`
     的子进程模式一致），并发保护：同一时刻只允许一个 refetch 子进程，忙时返回 409，
     web_server 收到 409 直接丢弃（编辑事件丢了无大碍，下次编辑或人工刷新可补）；
   - 完成后调用 `clear_article_cache()`（`refresh_server.py:113`）并参考
     `_invalidate_refresh_server_cache`（`web_server.py:1952`）的既有失效链路，
     确保详情缓存不陈旧。
3. `web_server.py` webhook 路由中 `edited_channel_post` 分支改为调用上述内部接口。
4. 边界：编辑消息若 `message_id` 不在 `articles` 表中（编辑的是入库范围之外的旧帖/
   非今日帖），`refetch_single_post` 入库前先查 `articles` 是否存在该 id，
   不存在则跳过（保持"今日增量"的数据范围语义，避免孤儿旧文混入）。

## 5. Phase 3 — Telegram Serverless 转发 bot

代码放本仓库 `serverless/relay/` 目录（纳入版本管理，独立于 Python 应用构建，
Dockerfile 无需感知）。

### 5.1 目录结构

```
serverless/relay/
├── schema.js              # 本方案不需要表，保持最小合法定义（空 schema）
├── handlers/
│   ├── channel_post.js
│   └── edited_channel_post.js
├── lib/
│   ├── forward.js         # 共享转发逻辑
│   ├── config.js          # 真实配置（gitignore）
│   └── config.example.js  # 模板：RAYNEWS_WEBHOOK_URL / WEBHOOK_TOKEN / CHANNEL_USERNAME
└── README.md              # 部署步骤（见 §5.3）
```

注意平台约束：无 npm、`handlers/` 不能有子目录、共享代码只能放 `lib/`。
文档未提供环境变量/密钥管理机制，故密钥走 `lib/config.js` + `.gitignore`
（在仓库根 `.gitignore` 加 `serverless/relay/lib/config.js`）。

### 5.2 handler 逻辑（两个 handler 都调用 `lib/forward.js`）

```js
// lib/forward.js 伪代码
export async function forwardUpdate(update, msg) {
  if (!msg || !msg.chat || (msg.chat.username || "").toLowerCase()
      !== CHANNEL_USERNAME.toLowerCase()) return;   // 边缘侧先过滤一道
  try {
    await fetch(RAYNEWS_WEBHOOK_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-RayNews-Webhook-Token": WEBHOOK_TOKEN,
      },
      body: JSON.stringify(update),
    });
  } catch (e) {
    console.log("forward failed: " + e);  // 平台会捕获 console 日志
  }
}
```

- **单次尝试、失败即放弃**：不做重试队列（serverless 侧无定时器保证，且兜底轮询
  本来就负责补漏），保持 handler 无状态、零 schema；
- 不实现其他 handler 文件（平台对未实现的 update 类型不触发，天然省调用量）。

### 5.3 部署步骤（写入 `serverless/relay/README.md`）

1. @BotFather 创建 bot（或复用），按官方文档开启 serverless/tgcloud 能力；
2. `cp lib/config.example.js lib/config.js` 填入真实值；
3. `npx tgcloud run` 本地联调（向测试频道发消息，观察 RayNews 日志收到 202）；
4. `npx tgcloud push` 部署；
5. 将 bot 加为目标频道**管理员**（无需任何管理权限项，仅需能接收频道消息）；
6. RayNews 侧配置 `TELEGRAM_WEBHOOK_SECRET`（与 `config.js` 一致）并重启容器；
7. 验证端到端后，把 `REFRESH_INTERVAL_SECONDS` 调到 `7200`。

## 6. 测试计划（`tests/`，沿用现有 pytest 风格）

新增 `tests/test_webhook.py`：

| 用例 | 断言 |
|---|---|
| secret 未配置 | POST /webhook/telegram → 404 |
| token 错误/缺失 | 403，响应体不泄露原因 |
| 合法 channel_post | 202，且向 8081 发出 `trigger=webhook` 的内部请求（mock `http_req.post`） |
| chat.username 不匹配 | 200 ignored，不触发内部请求 |
| 非 channel_post/edited 类型 update | 200 ignored |
| 限频 | 第 31 次（60s 窗口内）→ 429 |
| body 非 JSON / 超长 | 400 / 413，不崩 |

新增 `tests/test_refresh_rerun.py`：

- job running 时收到 webhook trigger → `REFRESH_RERUN_PENDING` 置位，job 结束后自动再跑一轮；
- 补跑不无限连锁（连续两轮后静止）。

Phase 2 补 `tests/test_refetch_post.py`：

- `--refetch-post` 对已存在 id：entry 覆盖更新；
- 对不存在 id：跳过不入库；
- 对 tombstone id：不复活（`upsert_articles` 已保证，用例固化行为）。

回归：现有 `tests/` 全绿；`TELEGRAM_WEBHOOK_SECRET` 未配置时整个系统行为与 dev 分支
完全一致（这是可回退性的验收线）。

## 7. 验收标准与发布

- [ ] Phase 1：向频道发一条测试消息，从 Telegram 发出到文章出现在 RayNews 列表 ≤ 60 秒
      （其中刷新管线自身耗时占大头；webhook 到达应在 5 秒内，看日志时间戳）；
- [ ] Phase 2：编辑一条已入库消息，≤ 60 秒内详情页内容更新；
- [ ] 关闭 serverless bot（移除管理员）后，系统按 `REFRESH_INTERVAL_SECONDS` 正常轮询，无错误日志；
- [ ] 兜底轮询调到 2 小时后运行 48 小时，对 t.me 的列表页请求量下降 ≥ 80%（看 fetcher 日志统计）；
- [ ] 文档：README（中英）环境变量表、`serverless/relay/README.md` 部署指引齐全。

发布节奏：Phase 1 + 3 一起构成最小可用闭环，先合入 `serverless` 分支自测（真实频道
+ 真实 tgcloud 部署验证后）再进 dev 走 beta；Phase 2 独立 PR 跟进。

## 8. 展望（本方案外，另立文档）

- **payload 临时条目**：webhook 收到后先用 payload 秒级插入标题级条目，刷新管线随后
  覆盖补全。等 Phase 1 上线后实测"webhook→文章可见"延迟，若 ≥30 秒再评估；
- **伴侣 bot**（inline 搜索、`/daily` 每日总结指令）：体验向扩展，依赖为 bot 开设带
  token 的只读查询 API，与本方案共用同一个 serverless 项目即可。

## 9. 风险与开放问题

| 风险 | 应对 |
|---|---|
| Serverless 计费未公布 | 转发 handler 用量极小；上线前查证官方条款，若收费超预期可随时下线（系统退化为轮询） |
| embed 单帖页 DOM 与列表页不一致 | Phase 2 开发首日先做真实页面结构验证，再决定解析适配量 |
| webhook 域名暴露 | token + 来源校验 + 限频 + 404 伪装；secret 泄露时轮换环境变量即可 |
| tgcloud 部署流程与文档描述有出入（功能较新） | Phase 3 首日先跑通官方 quickstart 再写转发逻辑 |
