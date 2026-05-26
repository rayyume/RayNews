<p align="center">
  <b>🇨🇳 中文</b> | <a href="README.en.md">🇺🇸 English</a>
</p>

# RayNews 📡

新闻聚合阅读器——从 Telegram 频道抓取新闻消息（通过 RSS-to-Telegram-Bot 推送），自动提取 Telegraph 全文，生成暗色模式新闻站。

![screenshot](assets/screenshot.jpg)

## 架构

```
新闻源 (RSS/网页/API)
       ↓
 RSS-to-Telegram-Bot ──推送──→ Telegram 频道 (t.me/s/your_channel)
                                      ↓
                        RayNews Fetcher ──定时抓取──→ news.json
                                      ↓
                                Nginx + 前端 Vue SPA
```

**数据流说明：**

1. **[RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)** —— 订阅 RSS 源，将新文章推送到你的 Telegram 频道
2. **Telegram 频道** —— 作为中间存储，RayNews 从此频道的公开页面（`t.me/s/频道名`）抓取消息
3. **RayNews Fetcher** —— Python 脚本，每 15 分钟增量抓取新消息，自动识别来源、提取 Telegraph 全文
4. **前端** —— 纯 Vue 3 SPA，暗色风格，支持来源筛选、文章详情、分享

> **注意：** RayNews 只负责**读取** Telegram 频道的数据，不做消息推送。推送需要用其他工具（如 [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)）或者手动在频道里发消息。

## 前置条件

- 一个 Telegram 公开频道
- （可选）[RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot) 或其他工具将新闻推送到该频道

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/rayyume/RayNews.git
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
- 数据 API: `http://<your-ip>:8090/news.json`

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TELEGRAM_CHANNEL` | `your_channel` | Telegram 频道名称（必填） |
| `TZ` | `Asia/Shanghai` | 容器时区，影响日志时间戳 |
| `DATA_DIR` | `/app/data` | 数据输出目录（容器内路径） |
| `HTTP_PROXY` | (空) | HTTP 代理（如需翻墙） |
| `HTTPS_PROXY` | (空) | HTTPS 代理 |
| `NO_PROXY` | `localhost,127.0.0.1` | 直连白名单 |

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
- [ ] **英文标题及文章自动翻译** — 自动将英文内容翻译为中文
- [ ] **关键词过滤** — 增加文章关键词过滤功能，不显示含有特定关键词的文章

### 长期

- [ ] **集成[RSStT](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)** — 用户无需额外部署机器人，开箱即用
- [ ] **自定义 AI API** — 接入自定义 AI API，支持文章摘要和每日综述
- [ ] **TTS 新闻阅读** — 文字转语音，听新闻
- [ ] **iOS 客户端** — 原生 iOS 应用

## 技术栈

- Python 3.12 (fetcher + refresh server)
- Nginx (静态服务)
- Vue 3 (前端，纯静态 SPA)
- BeautifulSoup (HTML 解析)
- Supervisor (进程管理)

## License

MIT
