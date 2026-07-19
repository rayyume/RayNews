"""AIService must turn a 200-but-empty completion (common on reasoning/thinking models
that exhaust max_tokens on hidden reasoning) into an actionable error, not a bare "" that
callers log as "empty" and retry forever."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai_service
from ai_service import AIService


class FakeResp:
    ok = True

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _svc(provider="openai"):
    return AIService("key", "https://opencode.ai/zen/go/v1", "m", provider_type=provider)


def _patch_post(monkeypatch, payload):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["body"] = json
        return FakeResp(payload)

    monkeypatch.setattr(ai_service.requests, "post", fake_post)
    return captured


def test_openai_empty_content_with_length_finish_raises_actionable_error(monkeypatch):
    _patch_post(monkeypatch, {
        "choices": [{"finish_reason": "length",
                     "message": {"content": "", "reasoning_content": "lots of thinking..."}}],
    })
    with pytest.raises(RuntimeError) as ei:
        _svc().chat([{"role": "user", "content": "hi"}], max_tokens=500)
    msg = str(ei.value)
    assert "空内容" in msg
    assert "max_tokens=500" in msg          # names the exhausted budget
    assert "AI_TITLE_MAX_TOKENS" in msg     # tells the operator how to fix it


def test_openai_normal_content_is_returned(monkeypatch):
    _patch_post(monkeypatch, {"choices": [{"message": {"content": "  hello  "}}]})
    assert _svc().chat([{"role": "user", "content": "hi"}]) == "  hello  "


def test_openai_null_content_does_not_crash_and_raises(monkeypatch):
    # content=None must not blow up later .strip() calls — it maps to the same error.
    _patch_post(monkeypatch, {"choices": [{"message": {"content": None}}]})
    with pytest.raises(RuntimeError):
        _svc().chat([{"role": "user", "content": "hi"}])


def test_claude_empty_text_raises(monkeypatch):
    _patch_post(monkeypatch, {"stop_reason": "max_tokens", "content": [{"type": "thinking", "thinking": "..."}]})
    with pytest.raises(RuntimeError) as ei:
        _svc("claude").chat([{"role": "user", "content": "hi"}], max_tokens=500)
    assert "空内容" in str(ei.value)


def test_claude_text_blocks_are_joined(monkeypatch):
    _patch_post(monkeypatch, {"content": [
        {"type": "thinking", "thinking": "ignore me"},
        {"type": "text", "text": "final answer"},
    ]})
    assert _svc("claude").chat([{"role": "user", "content": "hi"}]) == "final answer"


def test_title_calls_use_the_configurable_title_budget(monkeypatch):
    captured = _patch_post(monkeypatch, {"choices": [{"message": {"content": "短标题"}}]})
    _svc().summarize_title("Some very long original headline that must be shortened")
    assert captured["body"]["max_tokens"] == ai_service.TITLE_MAX_TOKENS
    assert ai_service.TITLE_MAX_TOKENS >= 1024
