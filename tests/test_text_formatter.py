from services.text_formatter import format_plain_text_reply


def test_markdown_reply_becomes_readable_plain_text():
    raw = """**Hello**

- First item
- [Docs](https://example.com/docs)
- Use `agent_id`

```html
<div>x</div>
```"""

    assert format_plain_text_reply(raw) == (
        "Hello\n\n"
        "- First item\n"
        "- Docs (https://example.com/docs)\n"
        "- Use agent_id\n\n"
        "<div>x</div>"
    )


def test_angle_bracket_text_is_preserved_without_blind_stripping():
    raw = "2 < 3 and 5 > 4\n<https://example.com>\nUse <name>"

    assert format_plain_text_reply(raw) == "2 < 3 and 5 > 4\nhttps://example.com\nUse <name>"


def test_html_tags_are_sanitized_but_code_and_text_survive():
    raw = """```xml
<div>x</div>
```
<p>One</p><p>Two</p>
<strong>safe</strong>
<script>alert(1)</script>
<img src=x onerror=alert(1)>"""

    assert format_plain_text_reply(raw) == "<div>x</div>\n\nOne\nTwo\n\nsafe"
