from services.text_formatter import format_agent_reply_for_website


def test_strips_html_and_markdown_to_plain_text():
    source = "<b>Привет</b> <script>alert(1)</script>мир с **жирным** текстом"

    result = format_agent_reply_for_website(source)

    assert result == "    Привет мир с жирным текстом"
    assert "<b>" not in result
    assert "**" not in result


def test_formats_nested_lists_readably():
    source = "- Первый\n  - Второй\n    - Третий\n1) Номер один\n2) Номер два"

    result = format_agent_reply_for_website(source)

    assert "\u2022 Первый" in result
    assert "  \u2022 Второй" in result
    assert "    \u2022 Третий" in result
    assert "1. Номер один" in result
    assert "2. Номер два" in result


def test_handles_unclosed_bold_and_keeps_readable_text():
    source = "Это **важно и срочно"

    result = format_agent_reply_for_website(source)

    assert result == "    Это важно и срочно"


def test_formats_code_block_and_unclosed_fence():
    source = "Текст до\n\n```python\nprint('ok')\nx = 1\n\nТекст после"

    result = format_agent_reply_for_website(source)

    assert "```" not in result
    assert "Код:" in result
    assert "    print('ok')" in result
    assert "    x = 1" in result
