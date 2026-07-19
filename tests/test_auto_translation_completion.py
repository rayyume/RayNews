"""Automatic translation must publish its update only after article text commits."""

import web_server


def test_auto_translation_publishes_marker_after_translated_body_commit(monkeypatch):
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
        ),
    )
    monkeypatch.setattr(
        web_server,
        "_save_ai_result",
        lambda article_id, **kwargs: calls.append(("marker", article_id, kwargs["translation"])),
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

    assert [call[0] for call in calls] == ["article", "marker"]
    assert calls[0][3] == "<p>中文正文</p>"
