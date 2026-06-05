import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fetcher


def test_process_message_keeps_feed_source_separate_from_origin():
    original_fetch_telegraph = fetcher.fetch_telegraph
    try:
        fetcher.fetch_telegraph = lambda _url: {
            "body_html": "<article><p>Siri changes in iOS 27.</p></article>",
            "images": [],
            "char_count": 27,
            "detected_source": "MacRumors",
        }
        msg = {
            "id": 1,
            "datetime": "2026-06-05T00:00:00+00:00",
            "text": "Siri in iOS 27",
            "html": (
                '<a href="https://telegra.ph/Siri-in-iOS-27-06-02">Siri in iOS 27</a>'
                '<br>via <a href="https://t.me/techfeed">Tech Feed - Telegram Channel</a>'
            ),
            "images": [],
            "videos": [],
            "link_preview_url": "https://telegra.ph/Siri-in-iOS-27-06-02",
            "link_preview_title": "Siri in iOS 27",
        }

        entry = fetcher.process_message(msg, 1001)

        assert entry["source"] == "Tech Feed"
        assert entry["feed_source"] == "Tech Feed"
        assert entry["origin_source"] == "MacRumors"
    finally:
        fetcher.fetch_telegraph = original_fetch_telegraph


if __name__ == "__main__":
    test_process_message_keeps_feed_source_separate_from_origin()
    print("ok")
