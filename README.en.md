<p align="center">
  <a href="README.md">🇨🇳 中文</a> | <b>🇺🇸 English</b>
</p>

# RayNews 📡 🤖

<div align="center">

**AI-Powered Self-Hosted News Aggregator** — Curate what matters, let AI read the rest

[![GitHub Release](https://img.shields.io/github/v/release/rayyume/RayNews)](https://github.com/rayyume/RayNews/releases)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue)](https://github.com/rayyume/RayNews/pkgs/container/raynews)
[![License](https://img.shields.io/github/license/rayyume/RayNews)](LICENSE)

</div>

![screenshot](assets/screenshot.jpg)

## ✨ Highlights

| Feature | Description |
|---------|-------------|
| 🤖 **AI Powered** | One-click summarization, full article translation, auto-translate, AI daily digest |
| 🏠 **Self-Hosted** | Your data stays yours — no third-party cloud dependency |
| 🐳 **Lightweight** | Single container + SQLite, runs on Raspberry Pi / NAS / low-end VPS |
| 📱 **PWA Ready** | Add to home screen, offline cache, native app-like experience |
| 🔗 **Telegram Native** | Works with RSS-to-Telegram-Bot ecosystem, reuse your existing subscription |
| 🖥️ **Dual Arch** | Ships for both `linux/amd64` and `linux/arm64` |

## 🤖 AI Features

- **📝 Summarization** — One-click AI article summary with DB cache (no repeated API calls)
- **🌐 Full Translation** — Translate full articles to Chinese, toggle between original/translated
- **⚡ Auto-Translate** — English articles are automatically translated on open
- **📬 Daily Digest** — Scheduled AI-generated daily summary delivered via Markdown email
- **🔌 Custom API** — Bring your own API key (OpenAI / Claude / any compatible endpoint)

## 🏗️ Architecture

```
News Sources (RSS/Web/API)
       ↓
 RSS-to-Telegram-Bot ──push──→ Telegram Channel
                                      ↓
                           RayNews Fetcher ──poll(15min)──→ SQLite
                                      ↓
                           Flask API + Nginx ──→ SPA Frontend + PWA
```

## 🚀 Quick Start

### Prerequisites

- A Telegram **public** channel
- (Optional) [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot) to push news to your channel

### Deploy

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
      - TELEGRAM_CHANNEL=your_channel
      - RAYNEWS_SECRET=***
      - RESEND_API_KEY=***
      - HTTP_PROXY=http://proxy:port
    restart: unless-stopped
```

```bash
docker compose up -d
```

First run auto-fetches articles, then polls every 15 minutes.

### Access

- Frontend: `http://<your-ip>:8090`
- Manual refresh: `http://<your-ip>:8090/refresh`
- Health check: `http://<your-ip>:8090/auth/health`
- Scheduler status: `http://<your-ip>:8090/scheduler/status`

## ⚙️ Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_CHANNEL` | ✅ | Telegram public channel name |
| `RAYNEWS_SECRET` | ✅ | JWT signing key (set to persist tokens across restarts) |
| `RESEND_API_KEY` | ❌ | Resend email API key (for daily digest / test email) |
| `HTTP_PROXY` | ❌ | HTTP proxy (for Telegram access behind firewall) |
| `HTTPS_PROXY` | ❌ | HTTPS proxy |
| `NO_PROXY` | ❌ | Direct-connect bypass (default `localhost,127.0.0.1`) |
| `TZ` | ❌ | Timezone (default `Asia/Shanghai`) |

## 🗺️ Feature Roadmap

### ✅ Implemented

- [x] **AI Reading** — Summarization, full translation, auto-translate
- [x] **User System** — Register/login/JWT, multi-role, invitation codes
- [x] **AI Daily Digest** — Scheduled summary with Markdown email delivery
- [x] **Favorites** — Bookmark articles, dedicated favorites panel
- [x] **Category Filter** — Filter by source/category
- [x] **Telegram Fetcher** — Auto-incremental polling from Telegram channels
- [x] **WeChat Articles** — Auto-detect and extract WeChat public account articles
- [x] **Telegraph Articles** — Auto-extract full Telegraph content
- [x] **Image Proxy** — Hotlink bypass for external images
- [x] **PWA** — Service Worker caching, add to home screen
- [x] **Dark Theme** — Native dark-mode UI
- [x] **Pagination** — 30 per page, unlimited article support
- [x] **Server Cache** — In-memory article cache for instant re-open
- [x] **AI Result Cache** — Persistent DB cache for summaries & translations
- [x] **Admin Panel** — User management, role assignment, user deletion

### 🚧 Roadmap

- [ ] **Keyword Filter** — Hide articles matching keywords
- [ ] **TTS Reading** — AI-powered text-to-speech
- [ ] **iOS Client** — Native SwiftUI app
- [ ] **Built-in RSStT** — No separate RSS-to-Telegram-Bot deployment needed

## 🛠️ Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Vanilla HTML/CSS/JS (Single Page App) |
| Backend | Flask (Python 3.12) |
| Database | SQLite (WAL mode) |
| Proxy | Nginx (static + API reverse proxy) |
| AI | OpenAI / Claude / any compatible API |
| Notifications | Resend Email API |
| Container | Docker multi-arch (amd64 + arm64) |

## 📦 Images

```bash
# Production
ghcr.io/rayyume/raynews:latest
ghcr.io/rayyume/raynews:v3.0.0

# Beta
ghcr.io/rayyume/raynews:dev
```

## 📄 License

MIT
