"""Shared derivation of the Telegram source channel/URLs from environment config.

Used by fetcher.py (scraping) and web_server.py (webhook source validation) so both
agree on which channel is authoritative without duplicating the parsing rule.
"""

import os
from urllib.parse import urlsplit


def resolve_telegram_urls() -> tuple[str, str, str]:
    """Derive (channel, list_url, post_url) from TELEGRAM_CHANNEL_URL if set,
    otherwise fall back to the legacy TELEGRAM_CHANNEL + hardcoded t.me domain.

    Using a full URL avoids hardcoding the t.me domain in code, so a domain
    change or mirror (e.g. telegram.me) can be handled purely via env var.
    """
    channel_url = os.environ.get("TELEGRAM_CHANNEL_URL", "").strip()
    if channel_url:
        parsed = urlsplit(channel_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        path_parts = [p for p in parsed.path.split("/") if p]
        channel = path_parts[-1] if path_parts else "your_channel"
        return channel, channel_url, f"{base}/{channel}/{{id}}?embed=1&mode=tme"

    channel = os.environ.get("TELEGRAM_CHANNEL", "your_channel")
    return channel, f"https://t.me/s/{channel}", f"https://t.me/{channel}/{{id}}?embed=1&mode=tme"
