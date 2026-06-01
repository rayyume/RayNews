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
        # Strip HTML tags for clean translation input
        import re
        plain_text = re.sub(r'<[^>]+>', '', article_text)
        plain_text = re.sub(r'\s*\n\s*', '\n', plain_text)
        plain_text = plain_text.strip()

        prompt = ("请将以下文章逐段翻译为中文。\n\n"
                  "格式要求（非常重要）：\n"
                  "1. 将文章按段落拆分，每段原文后面紧跟该段的中文翻译\n"
                  "2. 原文段和译文段之间用『||』分隔\n"
                  "3. 段与段之间用两个换行分隔\n"
                  "4. 输出纯文本，不要任何 HTML 标签（不要 <b> <u> <br/> 等）\n"
                  "5. 如果原文包含加粗或斜体，只保留纯文字含义\n\n"
                  "示例格式：\n"
                  "Today is Tuesday.||今天是星期二。\n\n"
                  "The weather is sunny.||天气晴朗。\n\n"
                  "Please output paragraph by paragraph exactly as shown above.")
        if title:
            prompt = f"文章标题：{title}\n\n{prompt}"
        messages = [
            {"role": "system",
             "content": "你是一个专业的翻译助手。逐段翻译，每段原文后紧跟该段译文，用『||』分隔。只输出纯文本，不要 HTML 标签。"},
            {"role": "user",
             "content": f"{prompt}\n\n文章内容：\n{plain_text[:4000]}"},
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

    # ─── Full HTML translation ────────────────────────────────

    def translate_full(self, html: str, target_lang: str = "zh-CN",
                       title: str = "") -> dict:
        """Translate full article HTML (and optionally title), preserving all tags.

        Returns {"title": translated_title, "html": translated_html}.
        If title is empty, translated_title will also be empty.
        """
        lang_name = "中文" if "zh" in target_lang else target_lang
        parts = []
        if title:
            parts.append(f"文章标题（请翻译并保持格式）：{title}")
        parts.append(f"文章正文 HTML（请翻译标签内文本，保持所有 HTML 结构不变）：\n{html}")
        user_content = "\n\n".join(parts)

        prompt = f"请将以下文章翻译为{lang_name}。\n\n"
        prompt += "要求：\n"
        if title:
            prompt += "0. 第一行输出翻译后的标题，不要任何标记或引号\n"
        prompt += "1. 只翻译标签内的文本内容，保持所有 HTML 标签不变\n"
        prompt += "2. 保持原文的段落、换行、超链接（<a>标签）、加粗（<b>）、斜体（<i>）等所有格式\n"
        prompt += "3. 不要添加、删除或修改任何 HTML 标签和属性\n"
        prompt += "4. 直接输出翻译后的 HTML，不要包含 ```html 代码块标记或任何额外说明文字\n\n"
        if title:
            prompt += "输出格式：\n第一行：翻译后的标题\n之后：翻译后的完整 HTML\n\n"

        messages = [
            {"role": "system",
             "content": f"你是一个专业的翻译助手。将文章翻译为{lang_name}，保持全部 HTML 标签和结构不变。"},
            {"role": "user",
             "content": prompt + user_content},
        ]
        result = self.chat(messages, max_tokens=8000, temperature=0.3)

        translated_title = ""
        translated_html = result
        if title:
            lines = result.split("\n", 1)
            translated_title = lines[0].strip()
            translated_html = lines[1] if len(lines) > 1 else ""

        return {"title": translated_title, "html": translated_html}

    # ─── Batch translation ────────────────────────────────────

    def translate_batch(self, segments: list[dict]) -> list[dict]:
        """Translate multiple text segments at once.
        
        segments: [{"id": 0, "text": "..."}, {"id": 1, "text": "..."}, ...]
        Returns: [{"id": 0, "text": "译文..."}, {"id": 1, "text": "译文..."}, ...]
        
        Inline HTML tags (<b>, <i>, <a>) are preserved using markdown equivalents
        so the AI can keep them in the translation.
        """
        import re

        # Build numbered prompt
        lines = []
        for seg in segments:
            text = seg["text"]
            # Convert inline HTML to markdown markers for AI preservation
            text = text.replace("<b>", "**").replace("</b>", "**")
            text = text.replace("<strong>", "**").replace("</strong>", "**")
            text = text.replace("<i>", "*").replace("</i>", "*")
            text = text.replace("<em>", "*").replace("</em>", "*")
            # Convert <a href="url">text</a> → [text](url)
            text = re.sub(r'<a\s+[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r'[\2](\1)', text)
            # Strip other tags (images, spans with no semantic value)
            text = re.sub(r'<[^>]+>', '', text)
            lines.append(f"[{seg['id']}] {text}")

        prompt = (
            "请将以下各段逐段翻译为中文。\n\n"
            "格式要求（非常重要）：\n"
            "1. 每段输出一行：原文编号 || 译文\n"
            "2. 保持原文中的 **加粗**、*斜体* 和 [链接文字](链接地址) 标记不变\n"
            "3. 不要添加任何额外说明文字\n\n"
            "示例：\n"
            "[0] This is **important** news. || 这是**重要**新闻。\n"
            "[1] Click [here](https://x.com) to visit. || 点击[这里](https://x.com)访问。\n\n"
            "待翻译段落：\n"
        )

        messages = [
            {"role": "system",
             "content": "你是一个专业的翻译助手。按编号逐段翻译，保留 **加粗** *斜体* 和 [链接文字](URL) 标记。"},
            {"role": "user",
             "content": prompt + "\n".join(lines)},
        ]
        result = self.chat(messages, max_tokens=4000)

        # Parse response: each line "[id] || translation"
        parsed = {}
        for line in result.split("\n"):
            line = line.strip()
            m = re.match(r'^\[(\d+)\]\s*\|\|\s*(.*)', line)
            if m:
                parsed[int(m.group(1))] = m.group(2).strip()

        # Build return array preserving original order
        output = []
        for seg in segments:
            trans = parsed.get(seg["id"], "")
            # Convert markdown markers back to HTML
            trans = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', trans)
            trans = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', trans)
            trans = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', trans)
            output.append({"id": seg["id"], "text": trans})
        return output

    # ─── Test connection ──────────────────────────────────────

    def test_connection(self) -> str:
        """Verify API connectivity with a lightweight models/list request (5s timeout).
        
        Instead of sending a chat completion (which is slow through proxy chains),
        we just list models — much faster and sufficient to verify API key + endpoint.
        """
        import requests as http_req
        base = self.endpoint.rstrip("/")
        if "/v1" not in base:
            base = f"{base}/v1"
        
        headers = {"Content-Type": "application/json"}
        if self.provider_type == "claude":
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        resp = http_req.get(f"{base}/models", headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        # Return first model name as confirmation
        models = data.get("data", []) if isinstance(data, dict) else data
        if models and isinstance(models, list) and len(models) > 0:
            name = models[0].get("id", models[0].get("name", "connected"))
            return f"✅ 连接成功 — 可用模型: {name}"
        return "✅ 连接成功 — 已获取模型列表"
