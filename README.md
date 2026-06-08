<p align="center">
  <b>🇨🇳 中文</b> | <a href="README.en.md">🇺🇸 English</a>
</p>

# RayNews 📡 🤖

新闻聚合阅读器——从 Telegram 频道抓取新闻消息（通过 RSS-to-Telegram-Bot 推送），自动提取 Telegraph 全文，生成暗色模式新闻站。

![screenshot](assets/screenshot.jpg)

## 🤖 AI 功能

- **📝 文章摘要** — 一键 AI 生成文章摘要，结果缓存至数据库，不重复消耗 API
- **🌐 全文翻译** — 英译中全文翻译，保留原文排版，支持原文/译文一键切换
- **⚡ 自动翻译** — 打开英文文章自动显示中文，无需手动点击
- **📬 每日摘要** — 定时汇总当日新闻，AI 生成分类摘要，Markdown 邮件推送
- **🔌 自定义 AI API** — 支持 OpenAI / Claude / 任意兼容接口，使用你自己的 Key

## 架构

```
新闻源 (RSS/网页/API)
       ↓
 RSS-to-Telegram-Bot ──推送──→ Telegram 频道
                                      ↓
                        RayNews Fetcher ──定时抓取──→ SQLite
                                      ↓
                          Flask API + Nginx ──→ 前端 SPA
```

**数据流说明：**

1. **[RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)** —— 订阅 RSS 源，将新文章推送到你的 Telegram 频道
2. **Telegram 频道** —— 作为中间存储，RayNews 从此频道的公开页面（`t.me/s/频道名`）抓取消息
3. **RayNews Fetcher** —— Python 脚本，每 15 分钟增量抓取新消息，自动识别来源、提取 Telegraph 全文
4. **后端** —— Flask API，提供文章列表、详情、AI 摘要/翻译、收藏、用户管理等接口
5. **前端** —— 纯原生 JS SPA，暗色风格，支持来源筛选、文章详情、AI 阅读、PWA

> **注意：** RayNews 只负责**读取** Telegram 频道的数据，不做消息推送。推送需要用其他工具（如 [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)）或者手动在频道里发消息。

## 前置条件

- 一个 Telegram 公开频道
- （可选）[RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot) 或其他工具将新闻推送到该频道

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/rayyume/RayNews-Reader.git
cd RayNews
```

### 2. 配置

```bash
# 复制环境配置模板
cp .env.example .env

# 编辑 .env，填入你的 Telegram 频道名称等配置
```

### 3. 启动

```bash
docker compose up -d
```

容器启动时自动抓取一次，之后每 15 分钟刷新。

### 4. 访问

- 前端: `http://<your-ip>:8090`
- 手动刷新 API: `http://<your-ip>:8090/refresh`
- 调度器状态: `http://<your-ip>:8090/scheduler/status`

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TELEGRAM_CHANNEL` | `your_channel` | Telegram 频道名称（必填） |
| `TZ` | `Asia/Shanghai` | 容器时区，影响日志时间戳 |
| `RAYNEWS_SECRET` | (自动保存到 `/app/data/raynews_secret`) | JWT 签名密钥；也可手动指定 |
| `RAYNEWS_TOKEN_EXPIRY_SECONDS` | `2592000` | 登录 token 有效期（秒），默认 30 天 |
| `RESEND_API_KEY` | (空) | Resend 邮件 API Key（用于每日摘要/测试邮件） |
| `RAYNEWS_ADMIN_EMAIL` | (第一个管理员邮箱) | 接收新用户邀请码申请的管理员邮箱 |
| `RAYNEWS_FROM_EMAIL` | `onboarding@resend.dev` | 邮件发件人地址；生产建议改成自己的已验证域名邮箱 |
| `HTTP_PROXY` | (空) | HTTP 代理（如需翻墙） |
| `HTTPS_PROXY` | (空) | HTTPS 代理 |
| `NO_PROXY` | `localhost,127.0.0.1` | 直连白名单 |
| `AI_REQUEST_TIMEOUT_SECONDS` | `300` | AI 请求超时时间（秒），每日摘要、文章摘要、翻译、订阅源分类共用 |
| `AUTO_SUMMARY_BATCH_LIMIT` | `20` | 后台自动生成文章摘要每轮处理的文章数 |
| `AUTO_SUMMARY_INTERVAL_SECONDS` | `30` | 后台自动生成文章摘要的轮询间隔（秒） |
| `AUTO_TRANSLATION_BATCH_LIMIT` | `5` | 后台自动翻译每轮处理的文章数 |
| `AUTO_TRANSLATION_INTERVAL_SECONDS` | `30` | 后台自动翻译的轮询间隔（秒） |
| `AUTO_SOURCE_CLASSIFY_BATCH_LIMIT` | `20` | 后台 AI 处理待分类订阅源每轮处理的来源数 |
| `AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS` | `120` | 后台 AI 处理待分类订阅源的轮询间隔（秒） |
| `IMAGE_CACHE_ENABLED` | `true` | 是否启用文章图片本地缓存 |
| `IMAGE_CACHE_MAX_MB` | `5120` | 普通图片缓存容量上限（MB）；收藏文章图片不参与清理 |
| `IMAGE_CACHE_MAX_FILE_MB` | `10` | 单张图片最大缓存大小（MB） |
| `IMAGE_CACHE_PREFETCH_BODY_LIMIT` | `3` | 抓取后后台预缓存每篇文章正文前几张图片 |
| `RAYNEWS_PUBLIC_URL` | (必填) | RayNews 对外访问地址，用于邮件链接等场景，例如 `https://news.example.com` |
| `CUSTOM_HEAD_HTML` | (空) | 注入到页面 `<head>` 的自定义 HTML，可用于访问统计脚本或 meta 标签 |

## 自定义构建

```bash
# 本地构建镜像
docker compose build

# 或直接使用 ghcr.io 预构建镜像
docker compose pull
```

## 路线图 🗺️

### 短期

- [x] **订阅源分类** — 按来源/标签对文章进行分组和筛选
- [x] **微信公众号文章抓取** — 识别并提取微信公众号文章全文
- [x] **新闻收藏夹** — 文章详情页增加收藏功能，并新增收藏夹界面
- [x] **英文标题及文章自动翻译** — 自动将英文内容翻译为中文
- [x] **自定义 AI API** — 接入自定义 AI API，支持文章摘要和每日综述
- [ ] **关键词过滤** — 增加文章关键词过滤功能，不显示含有特定关键词的文章

### 长期

- [ ] **集成 [RSStT](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)** — 用户无需额外部署 RSStT 项目，支持开箱即用
- [ ] **TTS 新闻阅读** — 文字转语音，听新闻
- [ ] **iOS 客户端** — 原生 iOS 应用

## 技术栈

- Python 3.12 (fetcher + refresh server + Flask API)
- Nginx (静态服务 + API 反代)
- 原生 HTML/CSS/JS (前端 SPA)
- SQLite (数据存储)
- BeautifulSoup (HTML 解析)

## License

MIT
