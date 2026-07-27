"""Automatic translation must publish only after its gated cache commits."""

import web_server


def test_auto_translation_publishes_marker_after_cache_without_body_writeback(monkeypatch):
    calls = []

    class TranslationService:
        def __init__(self, **kwargs):
            pass

        def translate_full(self, *args, **kwargs):
            return {"title": "中文标题", "html": "<p>中文正文</p>"}

    monkeypatch.setattr(web_server, "AIService", TranslationService)
    monkeypatch.setattr(
        web_server,
        "_save_article_translation",
        lambda article_id, title=None, body_html=None: calls.append(
            ("article", article_id, title, body_html)
        ) or True,
    )
    monkeypatch.setattr(
        web_server,
        "_save_ai_result",
        lambda article_id, **kwargs: calls.append(("cache", article_id, kwargs["translation"])),
    )
    monkeypatch.setattr(
        web_server,
        "_publish_translation_update",
        lambda article_id: calls.append(("marker", article_id)),
    )

    assert web_server._translate_article_background(
        {
            "id": 42,
            "title": "English title",
            "body_html": "<p>English body</p>",
            "translate_content_needed": True,
            "translate_title_needed": True,
        },
        {"api_key": "key", "endpoint": "https://example.test", "model": "model"},
    )

    assert [call[0] for call in calls] == ["article", "cache", "marker"]
    assert calls[0][3] is None
