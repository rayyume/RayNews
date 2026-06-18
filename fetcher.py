#!/usr/bin/env python3
"""
RayNews Fetcher
Fetches messages from Telegram channel (like BroadcastChannel), then
optionally fetches Telegraph full articles for messages that contain
telegra.ph links. Outputs news.json
"""

import json
import os
import re
import sqlite3
import logging
import html as html_mod
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from datetime import timezone as dt_timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from news_schema import ensure_deleted_articles_table
from source_categories import (
    ensure_article_sources, init_source_categories,
    ensure_article_source_columns,
    extract_domains_from_html, lookup_source_by_domain,
)

# ─── Config (overridable via environment variables) ──────
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "your_channel")
TELEGRAM_LIST_URL = f"https://t.me/s/{TELEGRAM_CHANNEL}"
TELEGRAM_POST_URL = f"https://t.me/{TELEGRAM_CHANNEL}/{{id}}?embed=1&mode=tme"
OUTPUT_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
OUTPUT_FILE = OUTPUT_DIR / "news.json"
STATE_FILE = OUTPUT_DIR / "fetcher_state.json"
DB_FILE = OUTPUT_DIR / "news.db"
MAX_WORKERS = 15
REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
HEADERS = {"User-Agent": USER_AGENT}
# Max historical pages to fetch on initial full sync
MAX_HISTORY_PAGES = 200
CST = dt_timezone(timedelta(hours=8))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fetcher")


# ─── SQLite ──────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    """Initialize SQLite DB and return connection."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            feed_source TEXT NOT NULL DEFAULT '',
            origin_source TEXT NOT NULL DEFAULT '',
            time TEXT DEFAULT '',
            date TEXT DEFAULT '',
            timestamp INTEGER NOT NULL DEFAULT 0,
            thumb TEXT DEFAULT '',
            has_full_content INTEGER DEFAULT 0,
            telegraph_url TEXT DEFAULT '',
            body_html TEXT DEFAULT '',
            summary TEXT DEFAULT ''
        )
    """)
    ensure_deleted_articles_table(conn)
    ensure_article_source_columns(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON articles(timestamp DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON articles(source)")
    init_source_categories(conn)
    conn.commit()
    return conn


def upsert_articles(conn: sqlite3.Connection, entries: list[dict]):
    """Batch insert or update articles into SQLite."""
    sql = """INSERT OR REPLACE INTO articles
        (id, title, source, feed_source, origin_source, time, date, timestamp, thumb,
         has_full_content, telegraph_url, body_html, summary)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    rows = []
    deleted_ids = {
        int(row[0])
        for row in conn.execute("SELECT article_id FROM deleted_articles").fetchall()
    }
    for e in entries:
        article_id = int(e.get("id", 0) or 0)
        if article_id in deleted_ids:
            continue
        rows.append((
            article_id,
            e.get("title", ""),
            e.get("source", ""),
            e.get("feed_source", e.get("source", "")),
            e.get("origin_source", ""),
            e.get("time", ""),
            e.get("date", ""),
            e.get("timestamp", 0),
            e.get("thumb", ""),
            1 if e.get("has_full_content") else 0,
            e.get("telegraph_url", ""),
            e.get("body_html", ""),
            e.get("summary", ""),
        ))
    conn.executemany(sql, rows)
    ensure_article_sources(conn)
    conn.commit()
    log.info(f"SQLite: upserted {len(rows)} articles"
             f" (total: {conn.execute('SELECT COUNT(*) FROM articles').fetchone()[0]})")


def migrate_news_json(conn: sqlite3.Connection):
    """Import existing news.json into SQLite if DB is empty."""
    count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    if count > 0:
        return  # already migrated
    if not OUTPUT_FILE.exists():
        return
    try:
        data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
        items = data.get("items", [])
        if items:
            upsert_articles(conn, items)
            log.info(f"Migrated {len(items)} articles from news.json to SQLite")
    except Exception as e:
        log.warning(f"Migration from news.json failed: {e}")


# ─── Helpers ──────────────────────────────────────────────
def clean_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    return " ".join(text.split()).strip()


def extract_title(text: str) -> str:
    """Extract first meaningful line as title."""
    # Try first line with content
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for line in lines:
        # Skip pure emoji or very short lines
        cleaned = re.sub(r'[\U0001F000-\U0010FFFF]', '', line).strip()
        if len(cleaned) >= 6:
            return line[:120]
    # Fallback: first 80 chars
    return text[:80]


# ─── Telegram Fetching ───────────────────────────────────
def fetch_telegram_page(before: str = "") -> str | None:
    """Fetch a Telegram channel page. Returns HTML or None on failure."""
    url = TELEGRAM_LIST_URL
    params = {"before": before} if before else {}
    try:
        log.info(f"Fetching Telegram: {url} before={before or '(latest)'}")
        resp = requests.get(url, headers=HEADERS, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        log.error(f"Telegram fetch failed: {e}")
        return None


def parse_messages(html: str) -> list[dict]:
    """Parse all messages from a Telegram channel page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    messages = []

    for wrap in soup.select(".tgme_widget_message_wrap"):
        msg = wrap.select_one(".tgme_widget_message")
        if not msg:
            continue

        # Message ID: data-post="your_channel/277214"
        data_post = msg.get("data-post", "")
        msg_id = data_post.replace(f"{TELEGRAM_CHANNEL}/", "").strip()
        if not msg_id or not msg_id.isdigit():
            continue

        # Skip service messages (join/leave/etc)
        classes = msg.get("class", "")
        if isinstance(classes, str) and "service_message" in classes:
            continue
        if isinstance(classes, (list, set, frozenset)) and "service_message" in classes:
            continue

        # Date/time
        time_el = msg.select_one(".tgme_widget_message_date time")
        datetime_str = time_el.get("datetime", "") if time_el else ""

        # Content HTML
        # Try direct text first, fallback to with reply
        has_reply = bool(msg.select_one(".js-message_reply_text"))
        text_selector = ".tgme_widget_message_text.js-message_text" if has_reply else ".tgme_widget_message_text"
        text_el = msg.select_one(text_selector)

        # Extract plain text
        content_text = ""
        content_html = ""
        if text_el:
            content_text = text_el.get_text("\n", strip=True)
            # Get inner HTML only, strip the outer wrapper element
            content_html = text_el.decode_contents()

        # Images
        images = []
        for photo in msg.select(".tgme_widget_message_photo_wrap"):
            style = photo.get("style", "")
            m = re.search(r'url\(["\']?([^"\'()]+)["\']?\)', style)
            if m:
                img_url = m.group(1)
                if img_url.startswith("//"):
                    img_url = "https:" + img_url
                images.append(img_url)

        # Videos
        videos = []
        for vwrap in msg.select(".tgme_widget_message_video_wrap"):
            video_html = str(vwrap)
            video_html = video_html.replace("preload muted autoplay loop playsinline", "controls preload metadata playsinline webkit-playsinline")
            videos.append(video_html)

        # Link preview
        link_preview = msg.select_one(".tgme_widget_message_link_preview")
        link_preview_url = ""
        link_preview_title = ""
        if link_preview:
            link_a = link_preview.get("href", "")
            if link_a:
                link_preview_url = link_a
            lp_title = link_preview.select_one(".link_preview_title")
            if lp_title:
                link_preview_title = lp_title.get_text(strip=True)

        if not content_text and not images:
            continue

        messages.append({
            "id": int(msg_id),
            "datetime": datetime_str,
            "text": content_text,
            "html": content_html,
            "images": images,
            "videos": videos,
            "link_preview_url": link_preview_url,
            "link_preview_title": link_preview_title,
        })

    # Return in chronological order (oldest first)
    messages.sort(key=lambda x: x["id"])
    return messages


def get_anchor_id(messages: list[dict]) -> int | None:
    """Get the newest (largest) message ID from a page, used for Telegram's before= parameter."""
    if not messages:
        return None
    return messages[-1]["id"]


# ─── State Management ────────────────────────────────────
def load_state() -> dict:
    """Load fetcher state (last_seen_id, etc.)."""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"State load failed: {e}")
    return {"last_seen_id": 0}


def save_state(state: dict):
    """Save fetcher state atomically (write to temp, then rename)."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ─── Source Detection ─────────────────────────────────────
def _extract_bottom_html(html: str, ratio: float = 0.15) -> str:
    """Return the bottom *ratio* portion of HTML content.

    Splits on block-level boundaries (<br>, </p>, </div>, <hr>, \n\n)
    and keeps only the last N chunks.  Source attribution links are
    almost always at the very end of an article; links in the top/middle
    are content references, not source indicators.
    """
    if not html:
        return ""
    # Split on common block boundaries
    chunks = re.split(r'(?:<br\s*/?>\s*)+|</p>|</div>|<hr[^>]*>|\n\s*\n', html, flags=re.IGNORECASE)
    chunks = [c.strip() for c in chunks if c.strip()]
    if not chunks:
        return html
    keep = max(1, int(len(chunks) * ratio))
    return "\n".join(chunks[-keep:])


def detect_source(content: str, extra_html: str = "") -> str:
    """Extract source from the bottom-most standalone via line.

    Tries in priority order:
      1. via attribution (link text or plain text after "via")
      2. domain from link_preview_url (always trustworthy — IS the article URL)
      3. domain from bottom ~15% of body links (source attributions are at the end)
      4. t.me/channel reference in content
      5. fallback: "未分类"
    """
    # 1) via attribution — already bottom-biased internally
    via_source = detect_source_from_attribution(content)
    if via_source:
        return via_source

    # 2) domain from link_preview_url — this IS the original article URL, no interference
    if extra_html:
        domains = extract_domains_from_html(extra_html)
        domain_match = lookup_source_by_domain(domains)
        if domain_match:
            source_name, _category = domain_match
            return source_name

    # 3) domain from bottom portion of body only
    #    Reference links in the middle of articles are excluded
    bottom = _extract_bottom_html(content, ratio=0.15)
    if bottom:
        domains = extract_domains_from_html(bottom)
        domain_match = lookup_source_by_domain(domains)
        if domain_match:
            source_name, _category = domain_match
            return source_name

    # 4) t.me/channel reference
    tg = re.search(r't\.me/([a-zA-Z0-9_]+)', content)
    if tg:
        name = _clean_source_name(tg.group(1))
        if name:
            return f"@{name}"

    # 5) fallback
    return "未分类"


def detect_feed_source(content: str, link_preview_title: str = "") -> str:
    """Extract the stable subscription/feed source for sidebar filtering.

    This intentionally avoids article-link domains and Telegraph metadata; those
    describe the original publisher and belong in origin_source.
    """
    via_source = detect_source_from_attribution(content)
    if via_source:
        return via_source

    tg = re.search(r't\.me/([a-zA-Z0-9_]+)', content)
    if tg:
        name = _clean_source_name(tg.group(1))
        if name:
            return f"@{name}"

    title = _clean_source_name(link_preview_title or "")
    if title and 1 < len(title) <= 30:
        return title

    channel = (TELEGRAM_CHANNEL or "").strip().lstrip("@")
    return f"@{channel}" if channel else "Unknown Feed"


def detect_source_from_attribution(content: str) -> str | None:
    """Extract source only from explicit bottom via attribution."""
    via_candidates = _extract_bottom_via_sources(content)
    if via_candidates:
        return via_candidates[0]

    # Fallback: body-inline attribution patterns common in Chinese news articles.
    # Examples: "—— 界面新闻", "▲ 财联社", "— BBC News"
    body_attr = _extract_body_attribution(content)
    return body_attr


def _extract_body_attribution(content: str) -> str | None:
    """Extract source from body-inline attribution like '—— SourceName' at article end."""
    plain = clean_html(content)
    if not plain:
        return None

    # Only look at the last 300 chars — in-body attributions are always at the end
    tail = plain[-300:] if len(plain) > 300 else plain
    # Match patterns: —— SourceName, ▲ SourceName, — SourceName, | Source
    m = re.search(r"(?:——|▲|—\s|\|\s)([\w一-鿿㐀-䶿·]+(?:[一-鿿\w]|\b))(?:\s|$)", tail)
    if not m:
        return None
    raw = _clean_source_name(m.group(1).strip())
    if raw and 1 < len(raw) <= 20:
        return raw
    return None


def _extract_bottom_via_sources(content: str) -> list[str]:
    """Return valid via sources from bottom to top, preferring link text.

    Only examines links that appear AFTER the last "via" keyword in each chunk,
    so that reference/body links elsewhere in the article cannot interfere.

    Two critical guards against misidentifying body text as source names:
    1. The "via" keyword must be in the bottom 500 chars of the content.
    2. The cleaned source name must be ≤ 40 chars (real source names are short).
    """
    # Quick guard: the attribution "via" is always near the end.
    # If the last "via" in the whole content is far from the bottom, it is body text.
    plain_all = clean_html(content)
    all_via = list(re.finditer(r"(?i)\bvia\b", plain_all))
    if not all_via or (len(plain_all) - all_via[-1].start()) > 500:
        return []

    chunks = re.split(r"<br\s*/?>", content, flags=re.IGNORECASE)
    candidates = []
    for chunk in reversed(chunks):
        line = clean_html(chunk)
        if not re.search(r"(?i)(^|\s)via\b", line):
            continue

        # Find the last "via" in the raw HTML — attribution lines are at the end
        via_matches = list(re.finditer(r"(?i)\bvia\b", chunk))
        if via_matches:
            # Only examine HTML after the last "via" (at most 150 chars)
            after_via_html = chunk[via_matches[-1].end():][:150]
            after_via_soup = BeautifulSoup(after_via_html, "html.parser")
            via_links = []
            for link in after_via_soup.find_all("a"):
                text = clean_html(link.get_text(" ", strip=True))
                href = link.get("href", "")
                if not text:
                    continue
                if href and "telegra.ph" in href:
                    continue
                via_links.append(text)
            if via_links:
                raw = _clean_source_name(via_links[0])
                if raw and len(raw) <= 30:
                    candidates.append(raw)
                continue

        # No via-adjacent link found — fall back to plain text after "via".
        # Extract from RAW HTML (before clean_html, which collapses <br> → space).
        # Split by <br> to isolate the via line from any article text that follows.
        via_match = re.search(r"(?i)\bvia\b", chunk)
        if via_match:
            after_via_html = chunk[via_match.end():]
            # Take only the first <br>-delimited segment after "via"
            first_segment = re.split(r"<br\s*/?>", after_via_html, maxsplit=1, flags=re.IGNORECASE)[0]
            raw = clean_html(first_segment).strip()
        else:
            raw = _text_after_last_via(line)
        if raw and len(raw) > 80:
            raw = raw[:80].rsplit(" ", 1)[0]
        raw = _clean_source_name(raw)
        if raw and len(raw) <= 30:
            candidates.append(raw)
    return candidates


def _text_after_last_via(line: str) -> str:
    matches = list(re.finditer(r"(?i)(^|\s)via\b", line))
    if not matches:
        return ""
    return line[matches[-1].end():].strip(" :：-—")


def _clean_source_name(name: str) -> str:
    """Strip verbose suffixes and extract meaningful source name."""
    name = name.strip("《》【】").strip()
    # Strip emoji (U+1F000–U+1FFFF)
    name = re.sub(r"[\U0001F000-\U0010FFFF]", "", name).strip()

    known_keywords = [
        "格隆汇", "联合早报", "cnBeta", "界面新闻", "少数派",
        "爱范儿", "金十数据", "TechCrunch", "36氪", "阮一峰",
        "MacRumors", "华尔街日报", "投资界", "包邮区",
        "XP Digital Lab", "凤凰网财经", "凤凰网科技",
        "WBusiness商业",
    ]
    for kw in known_keywords:
        if kw in name:
            return kw

    # Special cases for commonly merged channel display names
    if "科技圈" in name and "在花" in name:
        return "在花科技圈"

    suffixes = [
        " - Telegram Channel", " - Channel",
        " | Telegram Channel",
        " (Telegram Channel",
        " - 日排行", " - 即时", " - 国际", " - 要闻",
        " (author:", "全文版",
        " - Telegram", "Telegram",
        "频道",
    ]
    for s in suffixes:
        if s in name:
            name = name.split(s)[0]

    name = name.strip().rstrip(".:：")
    name = re.sub(r"\s*[-–]\s*(Channel|Group|Bot|频道|群)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^【.*?】\s*", "", name)
    name = name.strip().rstrip(".:：")

    if " - " in name:
        segments = [s.strip() for s in name.split(" - ") if s.strip()]
        if segments:
            name = segments[0]

    return name.strip()


# ─── Telegraph Extraction ────────────────────────────────
def extract_telegraph_url(content: str) -> str | None:
    match = re.search(r'https?://telegra\.ph/[^"\'<>\s]+', content)
    if match:
        return match.group(0).rstrip('"').rstrip("'")
    return None


def extract_wechat_url(content: str) -> str | None:
    """Extract mp.weixin.qq.com URL from article content."""
    match = re.search(r'https?://mp\.weixin\.qq\.com/[^"\'<>\s]+', content)
    if match:
        return match.group(0).rstrip('"').rstrip("'")
    return None


def extract_thumb(content: str) -> str:
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', content)
    if match:
        return match.group(1)
    return ""


def parse_datetime(dt_str: str) -> dict:
    """Parse ISO datetime string (from Telegram)."""
    try:
        if dt_str:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            dt_cst = dt.astimezone(CST)
            return {
                "iso": dt_cst.isoformat(),
                "time": dt_cst.strftime("%H:%M"),
                "date": dt_cst.strftime("%Y-%m-%d"),
                "timestamp": int(dt.timestamp()),
            }
    except Exception as exc:
        log.warning(f"Could not parse message datetime {dt_str!r}; using current Beijing time: {exc}")
    dt_cst = datetime.now(CST)
    return {
        "iso": dt_cst.isoformat(),
        "time": dt_cst.strftime("%H:%M"),
        "date": dt_cst.strftime("%Y-%m-%d"),
        "timestamp": int(dt_cst.timestamp()),
    }


def _legacy_news_item_key(item: dict) -> str:
    """Return a stable key for legacy news.json accumulation."""
    article_id = item.get("id")
    if article_id not in (None, ""):
        return f"id:{article_id}"
    telegraph_url = item.get("telegraph_url") or ""
    if telegraph_url:
        return f"url:{telegraph_url}"
    return "fallback:{source}:{date}:{title}".format(
        source=item.get("source", ""),
        date=item.get("date", ""),
        title=item.get("title", ""),
    )


# ─── Telegraph Fetching ──────────────────────────────────
def fetch_telegraph(url: str) -> dict | None:
    try:
        log.info(f"  Fetching Telegraph: {unquote(url)[:80]}...")
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        article = soup.find("article")
        if not article:
            return None

        # ── Extract source from Telegraph metadata BEFORE cleanup ──
        telegraph_source = ""

        # 1) <address> tag below title — often the original author/source name
        address = article.find("address")
        if address:
            addr_text = address.get_text(" ", strip=True)
            if addr_text and len(addr_text) < 60:
                # Try domain lookup first on any links in address
                addr_html = str(address)
                addr_domains = extract_domains_from_html(addr_html)
                domain_match = lookup_source_by_domain(addr_domains)
                if domain_match:
                    telegraph_source = domain_match[0]
                else:
                    telegraph_source = _clean_source_name(addr_text)
                log.info(f"  Telegraph source from <address>: {telegraph_source}")

        # 2) Attribution <a> links at the VERY BOTTOM of the article
        #    Only scan the last 5 <p> tags — source attributions are always at the end,
        #    links in the top/middle of articles are content references.
        if not telegraph_source:
            all_ps = article.find_all("p")
            bottom_ps = all_ps[-5:] if len(all_ps) > 5 else all_ps
            for p in bottom_ps:
                a_tag = p.find("a", href=True)
                if not a_tag:
                    continue
                href = a_tag.get("href", "")
                link_text = a_tag.get_text(strip=True)
                # Check if this looks like a source attribution link
                domains = extract_domains_from_html(f'<a href="{href}">link</a>')
                domain_match = lookup_source_by_domain(domains)
                if domain_match:
                    telegraph_source = domain_match[0]
                    log.info(f"  Telegraph source from bottom attribution link ({href[:60]}): {telegraph_source}")
                    break
                # Also try the link text itself
                if link_text and len(link_text) < 40:
                    cleaned = _clean_source_name(link_text)
                    if cleaned and len(cleaned) >= 2:
                        telegraph_source = cleaned
                        log.info(f"  Telegraph source from link text: {telegraph_source}")
                        break

        # ── Clean up non-content elements ──
        for p in article.find_all("p"):
            text = p.get_text(strip=True)
            if text.startswith("Generated by"):
                p.decompose()
            elif p.find("a", href=True):
                href = p.a["href"]
                if ("gelonghui.com" in href or "zaobao.com" in href) and len(text) < 30:
                    p.decompose()

        h1 = article.find("h1")
        if h1:
            h1.decompose()
        address = article.find("address")
        if address:
            address.decompose()

        # Fix relative URLs in iframe, img, a, video, source tags
        # Telegraph uses relative /embed/ paths that break on external sites
        base_tg = "https://telegra.ph"
        for tag in article.find_all(["iframe", "img", "a", "video", "source"]):
            for attr in ["src", "href"]:
                val = tag.get(attr, "")
                if val and val.startswith("/"):
                    tag[attr] = urljoin(base_tg, val)
        body_html = str(article)
        images = [img.get("src", "") for img in article.find_all("img") if img.get("src")]

        result = {
            "body_html": body_html,
            "images": images,
            "char_count": len(article.get_text()),
        }
        if telegraph_source:
            result["detected_source"] = telegraph_source
        return result
    except Exception as e:
        log.error(f"  Telegraph fetch failed: {e}")
        return None


# ─── WeChat Article Fetching ──────────────────────────────
def fetch_wechat_article(url: str) -> dict | None:
    """Fetch full content from a WeChat article (mp.weixin.qq.com)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        "Referer": "https://mp.weixin.qq.com/",
    }
    try:
        log.info(f"  Fetching WeChat: {url[:80]}...")
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Main content container in WeChat articles
        content_div = (
            soup.select_one("#js_content")
            or soup.select_one("#rich_media_content")
        )
        if not content_div:
            log.warning(f"  WeChat: no content div found")
            return None

        # Fix lazy-loaded images: data-src → src, make absolute
        for img in content_div.find_all("img"):
            data_src = img.get("data-src", "")
            src = img.get("src", "")
            if data_src:
                final_src = data_src
                if final_src.startswith("//"):
                    final_src = "https:" + final_src
                img["src"] = final_src
            elif src:
                if src.startswith("//"):
                    img["src"] = "https:" + src
                elif src.startswith("/"):
                    img["src"] = "https://mp.weixin.qq.com" + src

        # Clean up WeChat-specific styling that hides content
        if content_div.get("style"):
            style = content_div["style"]
            style = re.sub(r'visibility\s*:\s*hidden\s*;?\s*', '', style, flags=re.IGNORECASE)
            style = re.sub(r'opacity\s*:\s*0\s*;?\s*', '', style, flags=re.IGNORECASE)
            style = style.strip().rstrip(";")
            if style:
                content_div["style"] = style
            else:
                del content_div["style"]

        # Strip inline styles from all child elements so page CSS handles formatting
        for tag in content_div.find_all(True):
            if tag.name == "img":
                # Keep only width/height/display styles for images
                style = tag.get("style", "")
                if style:
                    keep = {}
                    for kv in style.split(";"):
                        kv = kv.strip()
                        if ":" in kv:
                            k, v = kv.split(":", 1)
                            k = k.strip().lower()
                            if k in ("width", "height", "display", "max-width"):
                                keep[k] = v.strip()
                    if keep:
                        tag["style"] = "; ".join(f"{k}: {v}" for k, v in keep.items())
                    else:
                        del tag["style"]
            else:
                tag.attrs.pop("style", None)

        body_html = str(content_div)
        images = [
            img.get("src", "") for img in content_div.find_all("img") if img.get("src")
        ]
        text_content = content_div.get_text(strip=True)

        log.info(f"  WeChat: {len(text_content)} chars, {len(images)} images")
        return {
            "body_html": body_html,
            "images": images,
            "char_count": len(text_content),
        }
    except Exception as e:
        log.error(f"  WeChat fetch failed: {e}")
        return None


# ─── Main Pipeline ────────────────────────────────────────
def process_message(msg: dict, orig_msg_id: int) -> dict:
    """Process a single Telegram message into a news entry."""
    content = msg["html"]
    text = msg["text"]
    title = extract_title(text)
    telegraph_url = extract_telegraph_url(content)
    feed_source = detect_feed_source(content, msg.get("link_preview_title", "") or "")
    # Pass link_preview_url as extra_html so domain detection can use the article URL
    link_preview_url = msg.get("link_preview_url", "") or ""
    origin_source = detect_source(content, extra_html=link_preview_url)
    thumb = msg["images"][0] if msg["images"] else ""
    time_info = parse_datetime(msg["datetime"])

    entry = {
        "id": orig_msg_id,  # Use stable Telegram message ID
        "title": title,
        "source": feed_source,
        "feed_source": feed_source,
        "origin_source": origin_source,
        "time": time_info.get("time", ""),
        "date": time_info.get("date", ""),
        "timestamp": time_info.get("timestamp", 0),
        "thumb": thumb,
        "has_full_content": False,
        "telegraph_url": telegraph_url or "",
        "body_html": "",
        "summary": "",
    }

    if telegraph_url:
        result = fetch_telegraph(telegraph_url)
        if result:
            entry["has_full_content"] = True
            entry["body_html"] = result["body_html"]
            entry["thumb"] = result["images"][0] if result["images"] and not thumb else thumb
            # Telegraph articles have no "via" line; use the source detected from
            # Telegraph metadata (<address>, attribution links) when the initial
            # detection is weak (plain "未分类" or just an @channel name).
            ts = result.get("detected_source", "")
            if ts:
                origin_source = ts
                entry["origin_source"] = origin_source
                log.info(f"  ✓ {title[:40]}... ({result['char_count']} chars, from Telegraph, origin → {origin_source})")
            else:
                log.info(f"  ✓ {title[:40]}... ({result['char_count']} chars, from Telegraph)")
        else:
            # Fallback to Telegram message content
            videos_html = "".join(msg.get("videos", []))
            entry["body_html"] = videos_html + content
            log.info(f"  ~ {title[:40]}... (Telegraph failed, using Telegram fallback)")
    else:
        videos_html = "".join(msg.get("videos", []))
        entry["body_html"] = videos_html + content
        plain = re.sub(r"<[^>]+>", " ", text).strip()
        entry["summary"] = plain[:250]
        log.info(f"  - {title[:40]}... (from Telegram)")

    # If body is too short (<64 chars) and original message has a WeChat URL,
    # try fetching full article content from mp.weixin.qq.com
    plain_body = re.sub(r"<[^>]+>", " ", entry["body_html"]).strip()
    if len(plain_body) < 64:
        wechat_url = extract_wechat_url(content)
        if wechat_url:
            log.info(f"  Short content ({len(plain_body)} chars), fetching WeChat: {wechat_url[:60]}...")
            wechat_result = fetch_wechat_article(wechat_url)
            if wechat_result:
                # Only append the "via <source>" link from the original content,
                # not the full original message (which repeats the title)
                via_matches = list(re.finditer(r'via\s*<a[^>]*>.*?</a>', content, re.DOTALL | re.IGNORECASE))
                via_suffix = via_matches[-1].group(0) if via_matches else ""
                entry["has_full_content"] = True
                entry["body_html"] = wechat_result["body_html"] + "\n" + via_suffix
                entry["thumb"] = wechat_result["images"][0] if wechat_result["images"] and not thumb else thumb
                log.info(f"  ✓ {title[:40]}... ({wechat_result['char_count']} chars, from WeChat)")
            else:
                log.warning(f"  ✗ WeChat fetch failed, keeping original content")

    return entry


def is_from_today(datetime_str: str) -> bool:
    """Check if a message datetime is from today (Beijing time)."""
    if not datetime_str:
        return False
    try:
        dt = datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))
        dt_cst = dt.astimezone(CST)
        now_cst = datetime.now(CST)
        return dt_cst.date() == now_cst.date()
    except Exception:
        return True  # If we can't parse, be inclusive


def fetch_all_new_messages(state: dict) -> list[dict]:
    """Fetch new messages since last seen ID. On first run, fetch all of today's messages."""
    last_id = state.get("last_seen_id", 0)
    all_msgs: list[dict] = []
    before = ""
    pages_fetched = 0

    log.info(f"Last seen message ID: {last_id}")
    is_first_run = last_id == 0

    if is_first_run:
        log.info("First run: fetching only today's messages (Beijing time)")
        pages_limit = MAX_HISTORY_PAGES
    else:
        pages_limit = MAX_HISTORY_PAGES

    while pages_fetched < pages_limit:
        html = fetch_telegram_page(before)
        if not html:
            break

        msgs = parse_messages(html)
        if not msgs:
            log.info("  No messages on this page")
            break

        pages_fetched += 1

        # Filter only new messages (id > last_id)
        new_msgs = [m for m in msgs if m["id"] > last_id]

        if is_first_run:
            # On first run: only keep unique messages from today (Beijing time)
            seen_ids = {m["id"] for m in all_msgs}
            today_msgs = [m for m in new_msgs if is_from_today(m["datetime"]) and m["id"] not in seen_ids]
            if today_msgs:
                log.info(f"  Page {pages_fetched}: {len(today_msgs)} unique today's msgs (IDs {today_msgs[0]['id']}~{today_msgs[-1]['id']})")
                all_msgs.extend(today_msgs)
            else:
                log.info(f"  Page {pages_fetched}: all already fetched (no new IDs) — stopping")
                break

            # If even the newest message on this page is from before today, we're past today
            if not is_from_today(msgs[-1]["datetime"]):
                log.info(f"  Newest message is from before today — stopping")
                break
        else:
            if new_msgs:
                log.info(f"  Page {pages_fetched}: {len(msgs)} messages, {len(new_msgs)} new (IDs {new_msgs[0]['id']}~{new_msgs[-1]['id']})")
                all_msgs.extend(new_msgs)
            else:
                log.info(f"  Page {pages_fetched}: {len(msgs)} messages, all already seen")
                if all(m["id"] < last_id for m in msgs):
                    log.info("  Caught up — stopping")
                    break
                existing_ids = {m["id"] for m in all_msgs}
                if all(m["id"] in existing_ids for m in msgs):
                    break

        # Paginate for next page — always use OLDEST message ID
        # Telegram before=X returns messages with ID < X, so using the oldest
        # message on the current page avoids overlap with the next page
        anchor = msgs[0]["id"]  # oldest on this page
        log.info(f"  Next page before={anchor} (oldest on current page)")
        if anchor is None or anchor <= 1:
            break
        before = str(anchor)

        if len(msgs) < 3:
            log.info("  Page has < 3 messages, likely end of channel")
            break

    log.info(f"Fetched {pages_fetched} pages, {len(all_msgs)} messages total")
    return all_msgs


def run():
    log.info("=" * 50)
    log.info("Starting fetch cycle")

    state = load_state()
    try:
        conn = init_db()
        migrate_news_json(conn)
        article_count = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        conn.close()
        if article_count == 0 and state.get("last_seen_id", 0) > 0:
            log.warning(
                "SQLite articles table is empty but fetcher_state has "
                f"last_seen_id={state.get('last_seen_id')}; forcing bootstrap fetch"
            )
            state["last_seen_id"] = 0
    except Exception as e:
        log.error(f"SQLite bootstrap check failed: {e}")

    messages = fetch_all_new_messages(state)

    if not messages:
        log.info("No new messages — keeping existing news.json")
        # Still ensure SQLite is initialized from existing data
        try:
            conn = init_db()
            migrate_news_json(conn)
            conn.close()
        except Exception as e:
            log.error(f"SQLite init failed: {e}")
        return

    # Process new messages with thread pool
    new_entries = []
    failed_count = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_message, msg, msg["id"]): msg for msg in messages}
        for future in as_completed(futures):
            try:
                new_entries.append(future.result())
            except Exception as e:
                failed_count += 1
                msg_id = futures[future].get("id", "?")
                log.error(f"Message processing failed (ID={msg_id}): {e}")

    # Merge with existing data (accumulate)
    existing_data = {"items": []}
    try:
        if OUTPUT_FILE.exists():
            existing_data = json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Could not read existing news.json: {e}")

    # Build seen_ids from existing items to avoid duplicates
    existing_ids = set()
    for item in existing_data.get("items", []):
        key = _legacy_news_item_key(item)
        existing_ids.add(key)

    # Only add truly new entries
    for entry in new_entries:
        key = _legacy_news_item_key(entry)
        if key not in existing_ids:
            existing_data["items"].append(entry)
            existing_ids.add(key)

    # Sort by timestamp descending
    all_items = existing_data["items"]
    all_items.sort(key=lambda x: x.get("timestamp", 0), reverse=True)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(all_items),
        "items": all_items,
    }
    # Atomic write: write to temp file, then rename
    tmp = OUTPUT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUTPUT_FILE)
    log.info(f"Wrote {len(all_items)} entries to {OUTPUT_FILE} (added {len(new_entries)} new)")

    # Update state with latest message ID
    # Only advance last_seen_id if ALL messages processed successfully,
    # otherwise failed messages would be permanently skipped on next run.
    if failed_count:
        log.warning(
            f"{failed_count} message(s) failed — keeping last_seen_id={state.get('last_seen_id')} "
            "so they can be retried on next fetch"
        )
    else:
        max_id = max(m["id"] for m in messages)
        state["last_seen_id"] = max(state.get("last_seen_id", 0), max_id)
        save_state(state)
        log.info(f"Updated state: last_seen_id = {state['last_seen_id']}")

    # ── SQLite sync ──
    try:
        conn = init_db()
        migrate_news_json(conn)
        upsert_articles(conn, new_entries)
        conn.close()
    except Exception as e:
        log.error(f"SQLite write failed: {e}")

    log.info("Fetch cycle complete")


if __name__ == "__main__":
    run()
