"""Safe rendering contracts for notification email bodies."""

import notifier


def test_markdown_renderer_preserves_supported_layout_and_images(monkeypatch):
    monkeypatch.setenv("RAYNEWS_PUBLIC_URL", "https://news.example/")
    body = """#### 标题

**重点**

- 第一项
- 第二项

[链接](https://example.com/story)

```python
print("ok")
```

| 名称 | 数量 |
| --- | ---: |
| 新闻 | 2 |

![图](https://img.example/a.png)
"""

    rendered = notifier.render_notification_email_body(body, "markdown")

    assert "<h4>标题</h4>" in rendered
    assert "<strong>重点</strong>" in rendered
    assert "<ul>" in rendered and "<li>第一项</li>" in rendered
    assert 'href="https://example.com/story"' in rendered
    assert 'rel="noopener noreferrer"' in rendered
    assert 'target="_blank"' in rendered
    assert "<pre><code" in rendered and 'print("ok")' in rendered
    assert "<table>" in rendered and "<th>名称</th>" in rendered
    assert (
        'src="https://news.example/img-cache?url='
        'https%3A%2F%2Fimg.example%2Fa.png"'
    ) in rendered
    assert 'alt="图"' in rendered


def test_markdown_renderer_removes_dangerous_html_protocols_and_attributes(monkeypatch):
    monkeypatch.setenv("RAYNEWS_PUBLIC_URL", "https://news.example")
    body = """<script>alert(1)</script>
<style>body { display: none }</style>
<form action="https://evil.example"><input name="secret"></form>
<iframe src="https://evil.example"></iframe>
<p onclick="alert(2)">safe paragraph</p>
<a href="javascript:alert(3)" onmouseover="alert(4)">bad link</a>
<img src="javascript:alert(5)" onerror="alert(6)" alt="bad">
"""

    rendered = notifier.render_notification_email_body(body, "markdown")
    lowered = rendered.lower()

    for forbidden in (
        "<script",
        "alert(1)",
        "<style",
        "display: none",
        "<form",
        "<input",
        "<iframe",
        "onclick",
        "onmouseover",
        "onerror",
        "javascript:",
        "<img",
    ):
        assert forbidden not in lowered
    assert "<p>safe paragraph</p>" in rendered
    assert "bad link" in rendered


def test_markdown_renderer_drops_images_without_safe_absolute_public_url(monkeypatch):
    monkeypatch.delenv("RAYNEWS_PUBLIC_URL", raising=False)

    rendered = notifier.render_notification_email_body(
        "before ![private](https://img.example/a.png) after",
        "markdown",
    )

    assert "<img" not in rendered
    assert "img.example" not in rendered


def test_plain_renderer_escapes_html_and_converts_newlines():
    assert (
        notifier.render_notification_email_body("<b>x</b>\ny", "plain")
        == "&lt;b&gt;x&lt;/b&gt;<br>y"
    )


def test_unknown_format_remains_literal_plain_text():
    assert (
        notifier.render_notification_email_body("**not bold**", "unexpected")
        == "**not bold**"
    )
