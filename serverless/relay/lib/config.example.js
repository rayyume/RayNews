// Copy this file to config.js (gitignored) and fill in real values before
// `npx tgcloud push`. Do not commit config.js.

// RayNews webhook endpoint, e.g. "https://news.example.com/webhook/telegram"
export const RAYNEWS_WEBHOOK_URL = "";

// Must match the RayNews server's TELEGRAM_WEBHOOK_SECRET env var exactly.
export const WEBHOOK_TOKEN = "";

// Must match the channel name RayNews derives from TELEGRAM_CHANNEL_URL /
// TELEGRAM_CHANNEL (see telegram_source.py). In production this is "raysrss",
// derived from TELEGRAM_CHANNEL_URL=https://telegram.me/s/raysrss — independent
// of which Telegram domain (t.me vs telegram.me) is used to browse the channel.
export const CHANNEL_USERNAME = "";
