<p align="center">
  <b>🇨🇳 中文</b> | <a href="README.en.md">🇺🇸 English</a>
</p>

# RayNews 📡 🤖

RayNews 是一个以 Telegram 公开频道为数据入口的自托管新闻聚合阅读器。它可以增量抓取频道消息、提取 Telegraph 和微信公众号等文章全文，并提供 AI 摘要、翻译、每日总结、订阅源管理、收藏和图片持久缓存。

前端为响应式 PWA，支持跟随系统、明色和暗色三种主题。

**示例网站：** [https://news.rayyu.me](https://news.rayyu.me)

## 页面预览

<table>
  <tr>
    <th width="72%">桌面端</th>
    <th width="28%">移动端 PWA</th>
  </tr>
  <tr>
    <td align="center" valign="top">
      <img src="https://img.rayyu.me/file/1781248235309_PC.png" alt="RayNews 桌面端页面" width="100%">
    </td>
    <td align="center" valign="top">
      <img src="https://img.rayyu.me/file/1781248235931_PWA.jpg" alt="RayNews 移动端 PWA 页面" width="100%">
    </td>
  </tr>
</table>

## 主要功能

### 阅读与整理

- 从 Telegram 公开频道每 15 分钟增量抓取新闻，也支持手动刷新
- 提取 Telegraph、微信公众号及普通网页文章内容
- 按订阅源和四个固定分类筛选文章
- 搜索文章标题、来源和摘要
- 收藏文章并在多设备登录后同步
- 管理员可识别、分类、合并和删除订阅源及历史文章
- 删除记录写入 tombstone，避免文章在后续刷新时重新出现

### AI 能力

- 使用每个用户自己配置的 OpenAI-compatible 或 Claude/Anthropic API
- 手动或后台自动生成文章摘要
- 自动翻译英文标题和正文
- 自动将过长标题简写为适合首页展示的短标题
- 从当天数百篇文章中筛选重点新闻，生成分类每日总结
- AI 结果写入数据库，可在符合权限和功能开关的情况下复用
- 订阅源 AI 分类仅由管理员执行，并使用管理员自己的 AI API

### 图片缓存

- 文章图片统一经过服务端缓存，降低历史图片失效和防盗链影响
- 新文章抓取后后台预热封面和正文前几张图片
- 普通图片按缓存容量自动清理，默认上限为 `5120 MB`
- 收藏文章的全部图片会被标记为永久保护，不参与普通缓存清理
- 图片缓存保存在 `/app/data/image_cache`

### 用户与通知

- 第一个注册账号自动成为管理员
- 后续用户需要邀请码注册，初始角色为预览用户
- 管理员可以管理用户角色、订阅源及全局文章删除
- 支持通过 Resend 发送邀请码、注册成功通知和定时每日摘要邮件

## 架构

```text
新闻源 (RSS / 网页 / API)
          │
          ▼
RSS-to-Telegram-Bot ──推送──▶ Telegram 公开频道
                                      │
                                      ▼
                              RayNews Fetcher
                                      │
                     ┌────────────────┴────────────────┐
                     ▼                                 ▼
              news.db / news.json              图片缓存目录
                     │                                 │
                     └────────────────┬────────────────┘
                                      ▼
             Nginx ──▶ refresh_server.py + web_server.py
                                      │
                                      ▼
                              原生 JavaScript SPA
```

- `refresh_server.py`：多线程文章 API、刷新任务、文章详情和图片缓存
- `web_server.py`：登录、用户、AI、收藏、设置、邮件和订阅源管理
- `Nginx`：静态文件、SPA 路由和后端反向代理
- `SQLite`：文章、用户、AI 结果、设置、收藏及删除记录

> RayNews 只读取 Telegram 频道，不负责把 RSS 内容推送到频道。你可以使用 [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)、其他机器人或手工发消息。

## 快速开始

### 1. 前置条件

- Docker 和 Docker Compose
- 一个 Telegram 公开频道
- 可选：用于向频道推送新闻的 [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)
- 可选：AI API 和 Resend 邮件 API

### 2. 克隆项目

```bash
git clone https://github.com/rayyume/RayNews.git
cd RayNews
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

至少在 `.env` 中填写：

```dotenv
TELEGRAM_CHANNEL_URL=https://telegram.me/s/your_public_channel
RAYNEWS_PUBLIC_URL=https://news.example.com
```

`RAYNEWS_PUBLIC_URL` 是必填项，用于邮件页脚等对外站点链接。不要保留示例域名。

如需邮件功能，再配置：

```dotenv
RESEND_API_KEY=re_xxxxxxxxx
RAYNEWS_ADMIN_EMAIL=admin@example.com
RAYNEWS_FROM_EMAIL=news@example.com
```

### 4. 启动

```bash
docker compose up -d
```

访问 `http://<服务器地址>:8090`。容器首次启动会抓取一次数据，之后每 15 分钟自动刷新。

第一个成功注册的账号会成为管理员。管理员邮箱建议与 `RAYNEWS_ADMIN_EMAIL` 保持一致。

## 镜像与更新

仓库中的 `docker-compose.yml` 默认使用 `build: .` 构建当前代码。

- 稳定版：注释 `build: .`，启用 `image: ghcr.io/rayyume/raynews:latest`
- 开发测试版：使用 `image: ghcr.io/rayyume/raynews:dev`

更新预构建镜像：

```bash
docker compose pull
docker compose up -d
```

如果使用 Watchtower，请确认容器由 `ghcr.io/rayyume/raynews` 镜像创建，而不是 Compose 自动生成的本地镜像名。

## 数据持久化

Compose 默认挂载：

```yaml
volumes:
  - ./data:/app/data
```

`/app/data` 中包含文章数据库、用户和 AI 设置、登录密钥、头像、图片缓存及抓取状态。升级或重建容器时必须保留整个目录。

## 环境变量

### 必填与核心配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TELEGRAM_CHANNEL_URL` | 无 | 完整频道链接，如 `https://telegram.me/s/your_channel`；域名可换成镜像域名，推荐使用 |
| `TELEGRAM_CHANNEL` | `your_channel` | （旧配置方式）仅频道名，域名固定为 `t.me`；未设置 `TELEGRAM_CHANNEL_URL` 时生效 |
| `RAYNEWS_PUBLIC_URL` | 无 | 对外访问地址；Compose 中为必填，用于邮件页脚等场景 |
| `TZ` | `Asia/Shanghai` | 容器时区 |
| `DATA_DIR` | `/app/data` | 持久化数据目录 |
| `RAYNEWS_SECRET` | 自动生成 | JWT 签名密钥；未设置时保存到 `/app/data/raynews_secret` |
| `RAYNEWS_TOKEN_EXPIRY_SECONDS` | `2592000` | 登录 Token 有效期，单位秒 |

### 邮件配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RESEND_API_KEY` | 空 | Resend API Key，用于邀请码、注册通知、测试邮件和每日摘要 |
| `RAYNEWS_ADMIN_EMAIL` | 首个管理员邮箱 | 接收邀请码申请和新用户注册通知 |
| `RAYNEWS_FROM_EMAIL` | `onboarding@resend.dev` | 发件人；生产环境应使用 Resend 已验证域名 |

### AI 与后台任务

AI Endpoint、API Key、模型和供应商由用户在网页的“设置 → AI”中配置，不通过 Compose 共用。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AI_REQUEST_TIMEOUT_SECONDS` | `300` | AI 请求超时，单位秒 |
| `AUTO_SUMMARY_BATCH_LIMIT` | `20` | 每轮自动生成文章摘要的文章数 |
| `AUTO_SUMMARY_INTERVAL_SECONDS` | `30` | 自动摘要轮询间隔，单位秒 |
| `AUTO_TRANSLATION_BATCH_LIMIT` | `5` | 每轮后台翻译文章数 |
| `AUTO_TRANSLATION_INTERVAL_SECONDS` | `30` | 后台翻译轮询间隔，单位秒 |
| `AUTO_TITLE_PROCESS_BATCH_LIMIT` | `20` | 每轮标题翻译或简写数量 |
| `AUTO_TITLE_PROCESS_INTERVAL_SECONDS` | `10` | 标题后台处理间隔，单位秒 |
| `AUTO_TITLE_PROCESS_SCAN_LIMIT` | `1000` | 每轮标题任务最大扫描数 |
| `TITLE_SUMMARY_MAX_CHARS` | `30` | AI 短标题目标中文字符数 |
| `TITLE_SUMMARY_MAX_TOTAL_CHARS` | `40` | 短标题允许的加权总长度 |
| `AUTO_SOURCE_CLASSIFY_BATCH_LIMIT` | `20` | 每轮管理员 AI 分类的订阅源数量 |
| `AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS` | `120` | 订阅源分类轮询间隔，单位秒 |
| `TELEGRAM_EMBED_TIMEOUT_SECONDS` | `12` | 读取 Telegram 嵌入页面的超时 |

每日总结还有 `AI_DAILY_*` 高级调优变量。通常保留代码默认值即可；如需使用，请将变量显式加入 Compose 的 `environment`。

### 图片缓存

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `IMAGE_CACHE_ENABLED` | `true` | 启用服务端图片缓存 |
| `IMAGE_CACHE_MAX_MB` | `5120` | 普通图片缓存容量上限，单位 MB |
| `IMAGE_CACHE_MAX_FILE_MB` | `10` | 单张图片最大缓存大小，单位 MB |
| `IMAGE_CACHE_PREFETCH_BODY_LIMIT` | `3` | 新文章后台预热的正文图片数量 |
| `IMAGE_CACHE_PREFETCH_WORKERS` | `2` | 图片预热 worker 数量 |
| `IMAGE_CACHE_PREFETCH_QUEUE_SIZE` | `3000` | 图片预热队列容量 |

收藏文章图片不计入 `IMAGE_CACHE_MAX_MB` 的自动清理范围，因此实际目录占用可能超过该值。

### 页面与网络

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CUSTOM_HEAD_HTML` | 空 | 注入页面 `<head>` 的受信任 HTML，例如统计脚本 |
| `CUSTOM_FOOTER_HTML` | 空 | 替换主页底部版本号和 GitHub 链接前方内容的受信任 HTML；支持 `<script>`，固定保留版本号与 GitHub 链接 |
| `HTTP_PROXY` | 空 | HTTP 代理 |
| `HTTPS_PROXY` | 空 | HTTPS 代理 |
| `NO_PROXY` | `localhost,127.0.0.1` | 不使用代理的地址 |

## 权限说明

| 能力 | 未登录 | 预览用户 | 普通用户 | 管理员 |
|------|--------|----------|----------|--------|
| 阅读首页和文章 | ✓ | ✓ | ✓ | ✓ |
| 翻页、收藏、个人 AI 与通知设置 | — | 受限 | ✓ | ✓ |
| 订阅源识别、分类、合并 | — | — | — | ✓ |
| 删除订阅源和历史文章 | — | — | — | ✓ |
| 用户管理和角色调整 | — | — | — | ✓ |

订阅源标签和分类是全局设置，所有用户看到的结果以管理员维护的数据为准。

## 常用接口

- 健康检查：`GET /health`
- 手动刷新：普通用户或管理员登录后使用页面刷新按钮，也可调用受保护的 `GET/POST /auth/refresh`
- 文章列表：`GET /api/news`
- 图片缓存：`GET /img-cache?url=<encoded-url>`

## 路线图

### 短期

- [x] **订阅源分类** — 按来源/标签对文章进行分组和筛选
- [x] **微信公众号文章抓取** — 识别并提取微信公众号文章全文
- [x] **新闻收藏夹** — 文章详情页增加收藏功能，并新增收藏夹界面
- [x] **英文标题及文章自动翻译** — 自动将英文内容翻译为中文
- [x] **自定义 AI API** — 接入自定义 AI API，支持文章摘要和每日综述
- [ ] **自定义可见订阅源** — 支持用户自定义可见订阅源，建立订阅源市场
- [ ] **关注订阅源新文章通知** — 支持关注订阅源，抓取到新文章时推送通知（PWA 应用）

### 长期

- [ ] **集成 [RSStT](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)** — 用户无需额外部署 RSStT 项目，支持开箱即用
- [ ] **关键词过滤** — 增加文章关键词过滤功能，不显示含有特定关键词的文章
- [ ] **新闻 Podcast 生成** — 自动生成新闻 Podcast，听新闻
- [ ] **iOS 客户端** — 原生 iOS 应用

## 技术栈

- Python 3.12
- Flask + Python `ThreadingHTTPServer`
- Nginx
- SQLite
- 原生 HTML / CSS / JavaScript PWA
- BeautifulSoup

## License

MIT
