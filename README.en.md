<p align="center">
  <a href="README.md">🇨🇳 中文</a> | <b>🇺🇸 English</b>
</p>

# RayNews 📡 🤖

A news aggregator that fetches messages from a Telegram channel (powered by [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)), automatically extracts Telegraph full articles, and serves a dark-mode news site.

![screenshot](assets/screenshot.jpg)

## 🤖 AI Features

- **📝 Summarization** — One-click AI summary, cached in DB, no repeated API calls
- **🌐 Translation** — Full article English-to-Chinese translation with original formatting preserved, toggle between original/translated
- **⚡ Auto-Translate** — English articles auto-translate on open
- **📬 Daily Digest** — Scheduled AI-generated daily summary via Markdown email
- **🔌 Custom AI API** — OpenAI / Claude / any compatible API, your own key

## Architecture

```
News Sources (RSS/Web/API)
          ↓
 RSS-to-Telegram-Bot ──push──→ Telegram Channel
                                      ↓
                        RayNews Fetcher ──poll──→ SQLite
                                      ↓
                          Flask API + Nginx ──→ Frontend SPA
```

**Data flow:**

1. **[RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)** — subscribes to RSS feeds and pushes new articles to your Telegram channel
2. **Telegram Channel** — acts as intermediate storage; RayNews fetches messages from the channel's public page (`t.me/s/channel_name`)
3. **RayNews Fetcher** — Python script, incrementally fetches new messages every 15 minutes, auto-detects sources, extracts Telegraph full text
4. **Backend** — Flask API for articles, AI, favorites, user management
5. **Frontend** — Vanilla JS SPA, dark theme, PWA, source filtering, AI-assisted reading

> **Note:** RayNews only **reads** from Telegram. You'll need another tool (like [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)) or manual posting to push content into the channel.

## Prerequisites

- A public Telegram channel
- (Optional) [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot) or other tools to push news to your channel

## Quick Start

### 1. Clone

```bash
git clone https://github.com/rayyume/RayNews-Reader.git
cd RayNews
```

### 2. Configure

```bash
# Copy environment template
cp .env.example .env

# Edit .env with your Telegram channel name and other settings
```

### 3. Start

```bash
docker compose up -d
```

The fetcher runs once on startup, then every 15 minutes.

### 4. Access

- Frontend: `http://<your-ip>:8090`
- Manual refresh: `http://<your-ip>:8090/refresh`
- Scheduler status: `http://<your-ip>:8090/scheduler/status`

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_CHANNEL` | `your_channel` | Telegram channel name (required) |
| `TZ` | `Asia/Shanghai` | Container timezone |
| `RAYNEWS_SECRET` | (saved to `/app/data/raynews_secret`) | JWT signing key; can also be set manually |
| `RAYNEWS_TOKEN_EXPIRY_SECONDS` | `2592000` | Login token lifetime in seconds, defaults to 30 days |
| `RESEND_API_KEY` | (empty) | Resend email API key (for daily digest / test email) |
| `HTTP_PROXY` | (empty) | HTTP proxy |
| `HTTPS_PROXY` | (empty) | HTTPS proxy |
| `NO_PROXY` | `localhost,127.0.0.1` | Direct connection whitelist |
| `AI_REQUEST_TIMEOUT_SECONDS` | `300` | AI request timeout in seconds, shared by daily digest, article summaries, translation, and source classification |
| `AUTO_SUMMARY_BATCH_LIMIT` | `20` | Number of articles processed per background auto-summary batch |
| `AUTO_SUMMARY_INTERVAL_SECONDS` | `30` | Background auto-summary polling interval in seconds |
| `AUTO_TRANSLATION_BATCH_LIMIT` | `5` | Number of articles processed per background auto-translation batch |
| `AUTO_TRANSLATION_INTERVAL_SECONDS` | `30` | Background auto-translation polling interval in seconds |
| `AUTO_SOURCE_CLASSIFY_BATCH_LIMIT` | `20` | Number of pending sources processed per background AI classification batch |
| `AUTO_SOURCE_CLASSIFY_INTERVAL_SECONDS` | `120` | Background AI source classification polling interval in seconds |
| `RAYNEWS_PUBLIC_URL` | `https://news.rayyu.me` | Public RayNews URL used for email links and similar outbound links |
| `CUSTOM_HEAD_HTML` | (empty) | Custom HTML injected into the page `<head>`, useful for analytics scripts or meta tags |

## Custom Build

```bash
# Build locally
docker compose build

# Or pull pre-built image from ghcr.io
docker compose pull
```

## Roadmap 🗺️

### Short-term

- [x] **Source Categorization** — Group and filter articles by source/tags
- [x] **WeChat Official Account Articles** — Identify and extract full-text content from WeChat public accounts
- [x] **Favorites** — Article bookmarking with dedicated panel
- [x] **Auto-translate** — Automatically translate English titles and articles
- [x] **Custom AI API** — Bring your own AI API for summaries and daily digest
- [ ] **Keyword Filter** — Hide articles containing specific words

### Long-term

- [ ] **Integrate [RSStT](https://github.com/Rongronggg9/RSS-to-Telegram-Bot)** — No extra deployment needed, OOTB support
- [ ] **TTS News Reading** — Text-to-speech for listening to news
- [ ] **iOS App** — Native iOS client

## Tech Stack

- Python 3.12 (fetcher + refresh server + Flask API)
- Nginx (static serving + API reverse proxy)
- Vanilla HTML/CSS/JS (frontend SPA)
- SQLite (data storage)
- BeautifulSoup (HTML parsing)

## License

MIT
