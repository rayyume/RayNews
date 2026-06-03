"""RayNews AI Service — unified interface for OpenAI-compatible and Claude-compatible APIs."""

import os
import requests
import re
from collections import defaultdict
from typing import Optional


# ─── Token-aware text truncation ─────────────────────────────
# Conservative char→token estimate: 1 token ≈ 4 ASCII chars or ≈ 2 CJK chars
# We use 3 chars/token as a safe midpoint for mixed content.
_CHARS_PER_TOKEN = 3
_MAX_INPUT_CHARS = 12_000  # ≈ 4000 tokens — fits most models' input comfortably


def _token_aware_truncate(text: str, max_chars: int = _MAX_INPUT_CHARS) -> str:
    """Strip HTML and build a prioritized excerpt: title → opening → key paragraphs → ending.

    Preserves the most informative parts of an article within a token-aware budget.
    Returns clean plain text (no HTML).
    """
    # Strip HTML tags
    plain = re.sub(r'<[^>]+>', '\n', text)
    plain = re.sub(r'\s*\n\s*', '\n', plain)
    plain = plain.strip()

    if not plain:
        return ""

    # If already within budget, return as-is
    if len(plain) <= max_chars:
        return plain

    # Split into paragraphs (non-empty lines)
    paragraphs = [p.strip() for p in plain.split('\n') if p.strip()]
    if not paragraphs:
        return plain[:max_chars]

    budget = max_chars
    parts = []

    # 1) First paragraph (usually the lede / most important) — keep fully
    first = paragraphs[0]
    if len(first) < budget * 0.6:  # don't let a single paragraph eat >60%
        parts.append(first)
        budget -= len(first)
        para_start = 1
    else:
        # First paragraph is huge — take its truncated form
        parts.append(first[:budget // 2])
        budget -= len(parts[0])
        para_start = 1

    # 2) Last paragraph (conclusion) — keep if budget allows
    last = paragraphs[-1]
    if len(paragraphs) > 2 and len(last) + 100 <= budget:
        parts.append(last)
        budget -= len(last) + 2  # +2 for separator
        para_end = len(paragraphs) - 1
    else:
        para_end = len(paragraphs)

    # 3) Middle — scan for key paragraphs (bold indicators, caps-start, lists)
    for p in paragraphs[para_start:para_end]:
        if budget <= 100:
            break
        is_key = (
            p.startswith('**') or  # bold/highlighted
            p.startswith('•') or p.startswith('-') or p.startswith('* ') or  # lists
            p.startswith('#') or  # markdown headers
            (len(p) > 15 and p[0].isupper() and p[1].isalpha())  # likely a heading
        )
        if is_key and len(p) + 100 <= budget:
            parts.append(p)
            budget -= len(p) + 2
        elif not is_key and budget > 300:
            # Non-key paragraphs get a condensed allocation
            max_p = min(len(p), budget // 2)
            if max_p > 60:
                parts.append(p[:max_p] + ('…' if max_p < len(p) else ''))
                budget -= max_p + 2

    # 4) If we still have budget, add more middle content from top
    if budget > 200:
        for p in paragraphs[para_start:para_end]:
            if budget <= 50:
                break
            if p not in parts and len(p) + 50 <= budget:
                # Shorten aggressively
                take = min(len(p), budget)
                parts.append(p[:take] + ('…' if take < len(p) else ''))
                budget -= take + 2

    return '\n\n'.join(parts)


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
        self.request_timeout = int(os.environ.get("AI_REQUEST_TIMEOUT_SECONDS", "300"))

    # ─── OpenAI-compatible API call ──────────────────────────

    def _call_openai(self, messages: list, max_tokens: int = 2000,
                     temperature: float = 0.3) -> str:
        url = f"{self.endpoint}/chat/completions"
        # Handle both {endpoint}/v1/chat/completions and bare {endpoint}/chat/completions
        if "/v1" not in url and "/v1" not in self.endpoint:
            url = f"{self.endpoint}/v1/chat/completions"

        try:
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
                timeout=(30, self.request_timeout),
            )
        except requests.exceptions.ReadTimeout as e:
            raise TimeoutError(
                f"AI 服务响应超时（超过 {self.request_timeout} 秒），请稍后重试或减少日报文章数量"
            ) from e
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

        try:
            resp = requests.post(
                url,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=(30, self.request_timeout),
            )
        except requests.exceptions.ReadTimeout as e:
            raise TimeoutError(
                f"AI 服务响应超时（超过 {self.request_timeout} 秒），请稍后重试或减少日报文章数量"
            ) from e
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
        truncated = _token_aware_truncate(article_text)
        prompt = "请为以下文章生成一个简洁的中文摘要，200字以内。"
        if title:
            prompt = f"文章标题：{title}\n\n{prompt}"
        messages = [
            {"role": "system",
             "content": "你是一个专业的新闻摘要助手。请用简洁的语言概括文章核心内容。"},
            {"role": "user",
             "content": f"{prompt}\n\n文章内容：\n{truncated}"},
        ]
        return self.chat(messages)

    # ─── Translate (bilingual) ───────────────────────────────

    def translate(self, article_text: str, title: str = "") -> str:
        truncated = _token_aware_truncate(article_text)
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
             "content": f"{prompt}\n\n文章内容：\n{truncated}"},
        ]
        return self.chat(messages, max_tokens=4000)

    # ─── Daily summary (layered) ──────────────────────────────
    #
    # Strategy:
    #   1. For each article, use existing AI summary if available,
    #      otherwise take first ~500 chars of body content.
    #   2. Group articles by source → batch per source.
    #   3. Generate a per-source mini-summary (or pass rich list for final combiner).
    #   4. Final combiner: merge all source summaries into one daily summary.
    #
    # Returns {"summary": str, "stats": {...}}

    def daily_summary(self, articles: list[dict]) -> dict:
        MAX_SUMMARY_CHARS = 140
        TITLE_FALLBACK_CHARS = 80
        MAX_ARTICLES_PER_SOURCE = 100  # cap per source to avoid one dominating
        MAX_DAILY_CANDIDATES = 120
        MAX_CANDIDATES_PER_CATEGORY = 30
        def article_link(article: dict) -> str:
            date = article.get("date", "") or ""
            art_id = article.get("id", 0)
            if date and art_id:
                return f"https://news.rayyu.me/#/article/{date[2:]}-{art_id}"
            return ""

        def compact_text(text: str, limit: int) -> str:
            text = re.sub(r'<[^>]+>', ' ', text or "")
            text = " ".join(text.split()).strip()
            if len(text) <= limit:
                return text
            clipped = text[:limit].rstrip(" ，,、；;：:")
            punct_positions = [clipped.rfind(p) for p in "。！？.!?"]
            best = max(punct_positions)
            if best >= max(12, limit // 2):
                return clipped[:best + 1].strip()
            space_pos = clipped.rfind(" ")
            if space_pos >= max(12, limit // 2):
                return clipped[:space_pos].strip()
            return clipped.strip()

        def classify_article(title: str, text: str, source: str) -> str:
            haystack = f"{title} {text} {source}".lower()
            category_keywords = (
                ("科技动态", ("ai", "openai", "deepseek", "芯片", "半导体", "模型", "科技", "人工智能", "机器人", "苹果", "英伟达", "算力")),
                ("商业聚焦", ("公司", "财报", "营收", "利润", "融资", "上市", "并购", "裁员", "市场", "品牌", "电商", "商业", "投资")),
                ("政经新闻", ("央行", "美联储", "利率", "通胀", "gdp", "pmi", "就业", "财政", "关税", "政府", "政策", "总统", "选举", "经济")),
            )
            for category, keywords in category_keywords:
                if any(keyword in haystack for keyword in keywords):
                    return category
            return "其他信息"

        def format_article_entry(article: dict, index: int) -> str:
            link = article.get("url", "")
            text_label = "内容摘要" if article.get("has_summary") else "标题线索"
            parts = [
                f"{index}. 来源：{article['source']}",
                f"建议分类：{article['category']}",
                f"标题：{article['title']}",
                f"{text_label}：{article['text']}",
            ]
            if link:
                parts.append(f"链接：{link}")
            return "\n".join(parts)

        def fallback_daily_summary(items: list[dict]) -> str:
            lines = []
            categories = ("政经新闻", "科技动态", "商业聚焦", "其他信息")
            for category in categories:
                lines.append(f"## {category}")
                category_items = [e for e in items if e["category"] == category][:10]
                if not category_items:
                    category_items = [e for e in items if e["category"] != category][:3]
                for e in category_items:
                    title = compact_text(e.get("title", ""), 28)
                    text = compact_text(e.get("text", ""), 52)
                    if text and text[-1] not in "。！？.!?":
                        text = text.rstrip(" ，,、；;：:") + "。"
                    link = e.get("url", "")
                    lines.append(f"- **{title}：** {text} [🔗]({link})")
            return "\n".join(lines)

        def summary_looks_truncated(text: str) -> bool:
            stripped = (text or "").strip()
            if not stripped:
                return True
            if "…" in stripped or "..." in stripped:
                return True
            last_line = stripped.splitlines()[-1].strip()
            if stripped.count("**") % 2 != 0:
                return True
            if last_line.startswith("-") and "[🔗](" not in last_line:
                return True
            if re.search(r"\*\*[^*\n]*$", stripped):
                return True
            return False

        final_format_rules = (
            "请严格按以下四个大分类输出，标题必须完全一致：\n"
            "## 政经新闻\n"
            "## 科技动态\n"
            "## 商业聚焦\n"
            "## 其他信息\n\n"
            "每个分类输出 10-12 条；如果某分类素材不足，可以少于 10 条，但不要省略该分类。\n"
            "每条必须使用如下格式：\n"
            "1. **总结性短标题：** 一句完整摘要 [🔗](URL)\n"
            "每个分类下必须使用有序编号列表，不要使用 '-' 或 '*' 作为项目符号。\n"
            "总结性短标题必须根据该条新闻内容生成，例如“全球多项财经数据与事件：”，"
            "不要使用固定文案“单条摘要总结”。\n"
            "短标题加摘要正文合计不超过 70 个中文字符；链接不计入字数。\n"
            "每条摘要必须是完整句子，不能以省略号结尾，不能使用“…”或“...”截断内容。\n"
            "每条末尾必须附文章链接，格式为 [🔗](URL)，且 URL 必须使用输入中的 https://news.rayyu.me/#/article/xxx 链接。\n"
            "不得输出“（无相关新闻）”；只要输入列表中有文章，就必须归入最接近的分类并输出条目。\n"
            "不要输出总述、寒暄或额外说明；不要把链接集中放到末尾。"
        )

        # ── Build per-article excerpts ──
        excerpts = []
        for a in articles:
            art_id = a.get("id", 0)
            source = a.get("source", "?")
            title = a.get("title", "?")
            url = article_link(a)
            summary = a.get("summary", "") or ""
            body_html = a.get("body_html", "") or ""

            # Layer 1: existing AI summary
            text = ""
            if summary:
                text = compact_text(summary, MAX_SUMMARY_CHARS)
            # Layer 2: title only. Daily summary should not ingest article bodies.
            else:
                text = compact_text(title, TITLE_FALLBACK_CHARS)

            if not text:
                text = "(no content)"

            category = classify_article(title, text, source)

            excerpts.append({
                "id": art_id,
                "source": source,
                "title": title,
                "url": url,
                "text": text,
                "category": category,
                "has_summary": bool(summary),
            })

        # ── Group by source, cap per source ──
        by_source = defaultdict(list)
        for ex in excerpts:
            by_source[ex["source"]].append(ex)

        # Cap per source to avoid one source dominating
        capped = []
        for src, items in by_source.items():
            capped.extend(items[:MAX_ARTICLES_PER_SOURCE])
        articles_after_source_cap = len(capped)

        # Select a balanced candidate set for the AI. Processing every article in
        # a 500-item day is expensive and slow; the final digest only needs about
        # 40 high-signal items, so keep a wider but bounded candidate pool.
        categories = ("政经新闻", "科技动态", "商业聚焦", "其他信息")
        by_category = defaultdict(list)
        for ex in capped:
            by_category[ex["category"]].append(ex)
        for items in by_category.values():
            items.sort(key=lambda e: (not e["has_summary"]))

        selected = []
        selected_ids = set()
        for category in categories:
            for ex in by_category.get(category, [])[:MAX_CANDIDATES_PER_CATEGORY]:
                selected.append(ex)
                selected_ids.add(ex["id"])

        if len(selected) < MAX_DAILY_CANDIDATES:
            remaining = [ex for ex in capped if ex["id"] not in selected_ids]
            remaining.sort(key=lambda e: (not e["has_summary"]))
            selected.extend(remaining[:MAX_DAILY_CANDIDATES - len(selected)])

        capped = selected[:MAX_DAILY_CANDIDATES]
        articles_selected_for_ai = len(capped)

        total_articles_with_summary = sum(1 for e in capped if e["has_summary"])
        total_articles_without_summary = len(capped) - total_articles_with_summary

        # ── Final summary ──
        if not capped:
            return {"summary": "今日无新闻。", "stats": {
                "total_articles": len(articles),
                "total_batches": 0,
                "articles_with_summary": 0,
                "articles_without_summary": 0,
            }}

        articles_text = "\n\n".join(
            format_article_entry(e, i + 1)
            for i, e in enumerate(capped)
        )
        sys_msg = (
            "你是一个资深新闻编辑。请基于输入中的文章摘要或标题线索生成每日摘要。"
            "没有内容摘要的文章，只能根据标题线索做保守概括，不要编造标题之外的事实。\n"
            + final_format_rules
        )
        user_msg = f"以下是今日新闻候选列表，请生成每日摘要：\n\n{articles_text}"
        final_prompt = [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": user_msg},
        ]

        final_summary = self.chat(final_prompt, max_tokens=7000)
        if summary_looks_truncated(final_summary):
            continuation_prompt = final_prompt + [
                {"role": "assistant", "content": final_summary},
                {"role": "user", "content": "你的输出被截断了。请只从最后一条未完成的位置继续输出，保持相同 Markdown 格式，不要重复已经完成的条目。"},
            ]
            final_summary = final_summary.rstrip() + "\n" + self.chat(continuation_prompt, max_tokens=2500)
        if "[🔗](" not in final_summary:
            final_summary = fallback_daily_summary(capped)
        elif summary_looks_truncated(final_summary):
            final_summary = fallback_daily_summary(capped)

        return {
            "summary": final_summary,
            "stats": {
                "total_articles": len(articles),
                "articles_after_dedup": articles_after_source_cap,
                "articles_after_source_cap": articles_after_source_cap,
                "articles_selected_for_ai": articles_selected_for_ai,
                "total_batches": 1,
                "articles_with_summary": total_articles_with_summary,
                "articles_without_summary": total_articles_without_summary,
            },
        }

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
