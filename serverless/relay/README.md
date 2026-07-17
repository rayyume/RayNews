# RayNews Telegram Serverless 转发 Bot

极简转发 bot：运行在 Telegram Serverless（<https://core.telegram.org/bots/serverless>）
上，收到目标频道的 `channel_post` / `edited_channel_post` 更新后，原样 POST 给 RayNews
的 `/webhook/telegram`。不做任何解析、不持久化任何状态——所有内容处理仍在
RayNews 服务器端的 `fetcher.py` 完成。

详见开发方案：`docs/plans/telegram-serverless-webhook-plan.md`。

## 部署步骤

1. 在 @BotFather 创建 bot（或复用现有 bot），按官方文档开启 serverless 能力。
2. 复制配置模板并填入真实值：

   ```bash
   cp lib/config.example.js lib/config.js
   ```

   - `RAYNEWS_WEBHOOK_URL`：RayNews 对外地址 + `/webhook/telegram`
   - `WEBHOOK_TOKEN`：与 RayNews 侧 `TELEGRAM_WEBHOOK_SECRET` 环境变量完全一致
   - `CHANNEL_USERNAME`：与 RayNews `TELEGRAM_CHANNEL_URL` 解析出的频道名一致
     （生产环境为 `raysrss`，来自 `TELEGRAM_CHANNEL_URL=https://telegram.me/s/raysrss`）

   `lib/config.js` 已加入 `.gitignore`，不会被提交。

3. 本地联调：

   ```bash
   npx tgcloud run
   ```

   向测试频道发一条消息，确认 RayNews 日志中出现该 webhook 请求。

4. 部署：

   ```bash
   npx tgcloud push
   ```

5. 将 bot 加为目标频道**管理员**（无需勾选任何管理权限，仅需能接收频道消息）。
6. 在 RayNews 部署环境中设置 `TELEGRAM_WEBHOOK_SECRET`（与 `config.js` 中的
   `WEBHOOK_TOKEN` 一致）并重启容器。
7. 端到端验证通过后，把 RayNews 的 `REFRESH_INTERVAL_SECONDS` 调大（建议 `7200`），
   兜底轮询作为补漏机制继续运行。

## 目录结构

```
relay/
├── schema.js                # 无状态，空 schema
├── handlers/
│   ├── channel_post.js
│   └── edited_channel_post.js
└── lib/
    ├── forward.js            # 共享转发逻辑
    ├── config.js             # 真实配置（本地，gitignore）
    └── config.example.js     # 配置模板
```
