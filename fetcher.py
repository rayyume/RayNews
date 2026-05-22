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

# ─── Config (overridable via environment variables) ──────
TELEGRAM_CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "your_channel")
TELEGRAM_LIST_URL = f"https://t.me/s/{TELEGRAM_CHANNEL}"
TELEGRAM_POST_URL = f"https://t.me/{TELEGRAM_CHANNEL}/{{id}}?embed=1&mode=tme"
OUTPUT_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
OUTPUT_FILE = OUTPUT_DIR / "news.json"
STATE_FILE = OUTPUT_DIR / "fetcher_state.json"
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
    """Save fetcher state."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


# ─── Source Detection ─────────────────────────────────────
def detect_source(content: str) -> str:
    """Extract and clean source name from content."""
    via_match = re.search(r"via\s+(.+?)(?:\s*<|$)", content, re.DOTALL)
    if via_match:
        raw = clean_html(via_match.group(1))
        raw = _clean_source_name(raw)
        if raw and len(raw) < 60:
            return raw

    tg = re.search(r't\.me/([a-zA-Z0-9_]+)', content)
    if tg:
        name = _clean_source_name(tg.group(1))
        if name:
            return f"@{name}"

    for match in re.finditer(r'<a[^>]*href=[\'"](https?://[^\'"]+)[\'"][^>]*>([^<]+)</a>', content):
        url, text = match.group(1), clean_html(match.group(2))
        text = _clean_source_name(text)
        if text and len(text) < 50:
            return text

    return "Telegram"


def _clean_source_name(name: str) -> str:
    """Strip verbose suffixes and extract meaningful source name."""
    name = name.strip("《》【】").strip()

    known_keywords = [
        "格隆汇", "联合早报", "cnBeta", "界面新闻", "少数派",
        "爱范儿", "金十数据", "TechCrunch", "36氪", "阮一峰",
        "MacRumors", "华尔街日报",
        "WBusiness商业",
    ]
    for kw in known_keywords:
        if kw in name:
            return kw

    suffixes = [
        " - Telegram Channel", " - Channel",
        " - 日排行", " - 即时", " - 国际", " - 要闻",
        " (author:", "全文版",
        " - Telegram", "Telegram",
    ]
    for s in suffixes:
        if s in name:
            name = name.split(s)[0]

    name = name.strip().rstrip(".")
    name = re.sub(r"\s*[-–]\s*(Channel|Group|Bot|频道|群)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^【.*?】\s*", "", name)

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
    except Exception:
        pass
    return {"iso": "", "time": "", "date": "", "timestamp": 0}


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

        return {
            "body_html": body_html,
            "images": images,
            "char_count": len(article.get_text()),
        }
    except Exception as e:
        log.error(f"  Telegraph fetch failed: {e}")
        return None


# ─── Main Pipeline ────────────────────────────────────────
def process_message(msg: dict, orig_msg_id: int) -> dict:
    """Process a single Telegram message into a news entry."""
    content = msg["html"]
    text = msg["text"]
    title = extract_title(text)
    telegraph_url = extract_telegraph_url(content)
    source = detect_source(content)
    thumb = msg["images"][0] if msg["images"] else ""
    time_info = parse_datetime(msg["datetime"])

    entry = {
        "id": orig_msg_id,  # Use stable Telegram message ID
        "title": title,
        "source": source,
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

        # Paginate for next page
        if is_first_run:
            # On first run: use OLDEST message ID as 'before' to avoid overlap
            # (Telegram before=X returns messages with ID < X)
            anchor = msgs[0]["id"]  # oldest on this page
            log.info(f"  Next page before={anchor} (oldest on current page)")
        else:
            anchor = get_anchor_id(msgs)
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
    messages = fetch_all_new_messages(state)

    if not messages:
        log.info("No new messages — keeping existing news.json")
        return

    # Process new messages with thread pool
    new_entries = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_message, msg, msg["id"]): msg for msg in messages}
        for future in as_completed(futures):
            try:
                new_entries.append(future.result())
            except Exception as e:
                log.error(f"Message processing failed: {e}")

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
        # Use a combination of title + timestamp as dedup key
        key = f"{item.get('title', '')}|{item.get('timestamp', 0)}"
        existing_ids.add(key)

    # Only add truly new entries
    for entry in new_entries:
        key = f"{entry.get('title', '')}|{entry.get('timestamp', 0)}"
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
    OUTPUT_FILE.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"Wrote {len(all_items)} entries to {OUTPUT_FILE} (added {len(new_entries)} new)")

    # Update state with latest message ID
    max_id = max(m["id"] for m in messages)
    state["last_seen_id"] = max(state.get("last_seen_id", 0), max_id)
    save_state(state)
    log.info(f"Updated state: last_seen_id = {state['last_seen_id']}")
    log.info("Fetch cycle complete")


if __name__ == "__main__":
    run()
