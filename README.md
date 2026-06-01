<p align="center">
  <b>🇨🇳 中文</b> | <a href="README.en.md">🇺🇸 English</a>
</p>

# RayNews 📡 🤖

<div align="center">

**AI 驱动的自托管新闻聚合器** — 聚合你关心的内容，AI 帮你读

[![GitHub Release](https://img.shields.io/github/v/release/rayyume/RayNews)](https://github.com/rayyume/RayNews/releases)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue)](https://github.com/rayyume/RayNews/pkgs/container/raynews)
[![License](https://img.shields.io/github/license/rayyume/RayNews)](LICENSE)

</div>

![screenshot](assets/screenshot.jpg)

## ✨ 核心优势

| 优势 | 说明 |
|------|------|
| 🤖 **AI Powered** | 一键摘要、全文翻译、自动翻译、AI 每日摘要邮件推送 |
| 🏠 **自托管** | 数据全在你手里，无第三方服务依赖，隐私安全 |
| 🐳 **轻量部署** | 单容器 + SQLite，树莓派/NAS/低配 VPS 均可运行 |
| 📱 **PWA 支持** | 可添加到主屏幕，离线缓存，原生 App 体验 |
| 🔗 **Telegram 生态** | 对接 RSS-to-Telegram-Bot，复用现有订阅体系 |
| 🖥️ **双架构** | 同时支持 `linux/amd64` 和 `linux/arm64` |

## 🤖 AI 功能一览

- **📝 文章摘要** — 一键调用 AI 生成文章摘要，缓存至数据库不重复消耗
- **🌐 全文翻译** — 英译中翻译，保留原文格式，支持原文/译文一键切换
- **⚡ 自动翻译** — 打开英文文章自动显示中文，无需手动点击
- **📬 每日摘要** — 定时汇总今日新闻，AI 生成分类摘要，Markdown 邮件推送
- **🔌 自定义 API** — 支持 OpenAI / Claude / 任意兼容 API，用自己的 Key

## 🏗️ 架构

```
新闻源 (RSS/网页/API)
       ↓
 RSS-to-Telegram-Bot ──推送──→ Telegram 频道
                                      ↓
                            RayNews Fetcher ──定时抓取(15min)──→ SQLite
                                      ↓
                            Flask API + Nginx ──→ 前端 SPA + PWA
```

## 🚀 快速开始

### 前置条件

- 一个 Telegram **公开**频道
- （可选）[RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot) 将新闻推送到频道

### 部署

```yaml
services:
  raynews:
    image: ghcr.io/rayyume/raynews:latest
    container_name: raynews
    ports:
      - "8090:80"
    volumes:
      - ./data:/app/data
    environment:
      - TZ=Asia/Shanghai
      - TELEGRAM_CHANNEL=your_channel       # 你的 Telegram 频道名
      - RAYNEWS_SECRET=your_jwt_secret       # JWT 密钥（重要！）
      - RESEND_API_KEY=re_xxx                # 如需每日摘要邮件
      - HTTP_PROXY=http://proxy:port          # 如需代理
    restart: unless-stopped
```

```bash
docker compose up -d
```

首次启动自动抓取文章，之后每 15 分钟增量更新。

### 访问

- 前端：`http://<your-ip>:8090`
- 刷新：`http://<your-ip>:8090/refresh`
- 健康检查：`http://<your-ip>:8090/auth/health`
- 调度器状态：`http://<your-ip>:8090/scheduler/status`

## ⚙️ 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `TELEGRAM_CHANNEL` | ✅ | Telegram 公开频道名称 |
| `RAYNEWS_SECRET` | ✅ | JWT 签名密钥（容器重启后 token 不失效需固定此值） |
| `RESEND_API_KEY` | ❌ | Resend 邮件 API Key（用于每日摘要/测试邮件） |
| `HTTP_PROXY` | ❌ | HTTP 代理（如需翻墙访问 Telegram） |
| `HTTPS_PROXY` | ❌ | HTTPS 代理 |
| `NO_PROXY` | ❌ | 直连白名单（默认 `localhost,127.0.0.1`） |
| `TZ` | ❌ | 时区（默认 `Asia/Shanghai`） |

## 🗺️ 功能清单

### ✅ 已实现

- [x] **AI 智能阅读** — 文章摘要、全文翻译、自动翻译英文标题
- [x] **用户系统** — 注册/登录/JWT 认证、多角色、邀请码注册
- [x] **AI 每日摘要** — 定时生成今日新闻摘要，Markdown 邮件推送
- [x] **文章收藏** — 收藏/取消收藏、独立收藏面板
- [x] **分类筛选** — 按来源/分类筛选新闻
- [x] **Telegram 抓取** — 自动增量抓取 Telegram 频道消息
- [x] **微信公众号文章** — 自动识别并提取公众号文章全文
- [x] **Telegraph 全文** — 自动提取 Telegraph 文章内容
- [x] **图片代理** — 突破图片防盗链，自动代理所有外链图片
- [x] **PWA 支持** — Service Worker 离线缓存，可添加到主屏幕
- [x] **暗色主题** — 原生暗色风格 UI
- [x] **分页浏览** — 30 条/页，无限文章加载
- [x] **服务端缓存** — 文章详情内存缓存，重复打开秒开
- [x] **AI 结果缓存** — 摘要/翻译结果持久化，不重复消耗 API
- [x] **管理员面板** — 用户管理、角色分配、用户删除

### 🚧 计划中

- [ ] **关键词过滤** — 屏蔽含特定关键词的文章
- [ ] **TTS 朗读** — AI 语音朗读新闻
- [ ] **iOS 客户端** — 原生 SwiftUI 应用
- [ ] **集成 RSStT** — 开箱即用，无需额外部署 RSS-to-Telegram-Bot

## 🛠️ 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | 原生 HTML/CSS/JS（单页应用） |
| 后端 | Flask (Python 3.12) |
| 数据库 | SQLite（WAL 模式） |
| 代理 | Nginx（静态资源 + API 反代） |
| AI | OpenAI / Claude / 任意兼容 API |
| 通知 | Resend Email API |
| 容器 | Docker multi-arch（amd64 + arm64） |

## 📦 镜像

```bash
# 正式版
ghcr.io/rayyume/raynews:latest
ghcr.io/rayyume/raynews:v3.0.0

# 测试版
ghcr.io/rayyume/raynews:dev
```

## 📄 License

MIT
