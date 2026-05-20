from services.text_formatter import format_plain_text_reply


def test_markdown_syntax_is_preserved_for_rich_rendering():
    raw = """## **Hello**

- First item
- [Docs](https://example.com/docs)
- Use `agent_id`
> Book by appointment
---

```html
<div>x</div>
```"""

    assert format_plain_text_reply(raw) == (
        "## **Hello**\n\n"
        "- First item\n"
        "- [Docs](https://example.com/docs)\n"
        "- Use `agent_id`\n"
        "> Book by appointment\n"
        "---\n\n"
        "```html\n"
        "<div>x</div>\n"
        "```"
    )


def test_angle_bracket_text_is_preserved_without_blind_stripping():
    raw = "2 < 3 and 5 > 4\n<https://example.com>\nUse <name>"

    assert format_plain_text_reply(raw) == "2 < 3 and 5 > 4\n<https://example.com>\nUse <name>"


def test_html_tags_are_sanitized_but_code_and_text_survive():
    raw = """```xml
<div>x</div>
```
<p>One</p><p>Two</p>
<strong>safe</strong>
<script>alert(1)</script>
<img src=x onerror=alert(1)>"""

    assert format_plain_text_reply(raw) == "```xml\n<div>x</div>\n```\n\nOne\nTwo\n\nsafe"


def test_unknown_html_tags_are_stripped_to_readable_text():
    raw = "<button onclick=alert(1)>Click</button>\n<unknown>tag</unknown>"

    assert format_plain_text_reply(raw) == "Click\ntag"


def test_html_stripping_keeps_literal_angle_brackets_and_code():
    raw = """2 < 3 and 5 > 4
Use <name>
<https://example.com>
`<tag>`
```html
<div>x</div>
```
<p>One</p><p>Two</p>"""

    assert format_plain_text_reply(raw) == (
        "2 < 3 and 5 > 4\n"
        "Use <name>\n"
        "<https://example.com>\n"
        "`<tag>`\n"
        "```html\n"
        "<div>x</div>\n"
        "```\n\n"
        "One\nTwo"
    )
