"""RayNews AI Service — unified interface for OpenAI-compatible and Claude-compatible APIs."""

import requests


class AIService:
    """Unified AI service supporting both OpenAI format and Claude format APIs.

    Use cases: DeepSeek (OpenAI), MiniMax (both), Anthropic Claude (native),
    OpenAI (native), Ollama (OpenAI).
    """

    def __init__(self, api_key: str, endpoint: str, model: str,
                 provider_type: str = "openai"):
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.provider_type = provider_type  # 'openai' or 'claude'

    # ─── OpenAI-compatible API call ──────────────────────────

    def _call_openai(self, messages: list, max_tokens: int = 2000,
                     temperature: float = 0.3) -> str:
        url = f"{self.endpoint}/chat/completions"
        # Handle both {endpoint}/v1/chat/completions and bare {endpoint}/chat/completions
        if "/v1" not in url and "/v1" not in self.endpoint:
            url = f"{self.endpoint}/v1/chat/completions"

        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    # ─── Claude-compatible API call ──────────────────────────

    def _call_claude(self, messages: list, max_tokens: int = 2000,
                     temperature: float = 0.3) -> str:
        url = f"{self.endpoint}/messages"
        # Handle both {endpoint}/v1/messages and bare {endpoint}/messages
        if "/v1" not in url and "/v1" not in self.endpoint:
            url = f"{self.endpoint}/v1/messages"

        # Convert OpenAI-style messages to Anthropic format
        system = ""
        claude_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                claude_messages.append({
                    "role": "assistant" if msg["role"] == "assistant" else "user",
                    "content": msg["content"],
                })

        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": claude_messages,
            "temperature": temperature,
        }
        if system:
            body["system"] = system

        resp = requests.post(
            url,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]

    # ─── Unified chat ────────────────────────────────────────

    def chat(self, messages: list, max_tokens: int = 2000,
             temperature: float = 0.3) -> str:
        if self.provider_type == "claude":
            return self._call_claude(messages, max_tokens, temperature)
        return self._call_openai(messages, max_tokens, temperature)

    # ─── Summarize ───────────────────────────────────────────

    def summarize(self, article_text: str, title: str = "") -> str:
        prompt = "请为以下文章生成一个简洁的中文摘要，200字以内。"
        if title:
            prompt = f"文章标题：{title}\n\n{prompt}"
        messages = [
            {"role": "system",
             "content": "你是一个专业的新闻摘要助手。请用简洁的语言概括文章核心内容。"},
            {"role": "user",
             "content": f"{prompt}\n\n文章内容：\n{article_text[:4000]}"},
        ]
        return self.chat(messages)

    # ─── Translate (bilingual) ───────────────────────────────

    def translate(self, article_text: str, title: str = "") -> str:
        prompt = ("请将以下文章翻译为中文，保留原文段落格式。"
                  "输出格式：先显示原文，空一行后显示中文翻译。")
        if title:
            prompt = f"文章标题：{title}\n\n{prompt}"
        messages = [
            {"role": "system",
             "content": "你是一个专业的翻译助手。请准确翻译文章内容，保持专业术语的准确性。"},
            {"role": "user",
             "content": f"{prompt}\n\n文章内容：\n{article_text[:4000]}"},
        ]
        return self.chat(messages, max_tokens=4000)

    # ─── Daily summary ───────────────────────────────────────

    def daily_summary(self, articles: list[dict]) -> str:
        articles_text = "\n\n".join([
            f"{i+1}. [{a.get('source', '?')}] {a.get('title', '?')}"
            for i, a in enumerate(articles[:20])
        ])
        messages = [
            {"role": "system",
             "content": "你是一个新闻编辑助手。请为以下今日新闻生成一份简洁的每日摘要，"
                        "按主题分类，每条新闻用一句话概括。"},
            {"role": "user",
             "content": f"以下是今日新闻列表，请生成摘要：\n\n{articles_text}"},
        ]
        return self.chat(messages, max_tokens=2000)

    # ─── Test connection ──────────────────────────────────────

    def test_connection(self) -> str:
        """Send a minimal prompt to verify API connectivity."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Reply with exactly one word: ok"},
        ]
        return self.chat(messages, max_tokens=10, temperature=0)
