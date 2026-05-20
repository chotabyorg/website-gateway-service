import html
import re


_FENCED_CODE_RE = re.compile(
    r"(^|\n)[ \t]*(```|~~~)[^\n]*\n(.*?)(?:\n[ \t]*\2[ \t]*(?=\n|$)|$)",
    re.DOTALL,
)
_SCRIPT_STYLE_RE = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DANGEROUS_EMPTY_TAG_RE = re.compile(
    r"<\s*(img|iframe|object|embed|svg|math|link|meta)\b[^>]*>",
    re.IGNORECASE,
)
_BLOCK_TAG_RE = re.compile(
    r"<\s*/?\s*(p|div|br|li|ul|ol|pre|blockquote|h[1-6]|tr|table)\b[^>]*>",
    re.IGNORECASE,
)
_BLOCK_BOUNDARY_RE = re.compile(
    r"<\s*/\s*(p|div|li|h[1-6]|tr)\s*>\s*<\s*\1\b[^>]*>",
    re.IGNORECASE,
)
_INLINE_TAG_RE = re.compile(
    r"<\s*/?\s*(strong|b|em|i|span|a|code|small|mark|sub|sup)\b[^>]*>",
    re.IGNORECASE,
)
_PAIRED_OPEN_TAG_RE = re.compile(
    r"<\s*([A-Za-z][A-Za-z0-9:-]*)\s*>(?=.*?</\s*\1\s*>)",
    re.IGNORECASE | re.DOTALL,
)
_CLOSING_TAG_RE = re.compile(r"</\s*[A-Za-z][A-Za-z0-9:-]*\s*>", re.IGNORECASE)
_ATTR_TAG_RE = re.compile(
    r"</?\s*[A-Za-z][A-Za-z0-9:-]*\s+[^<>]*>",
    re.IGNORECASE,
)
_SELF_CLOSING_TAG_RE = re.compile(
    r"<\s*[A-Za-z][A-Za-z0-9:-]*\s*/\s*>",
    re.IGNORECASE,
)
_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]+)\]\(([^)\s]+(?:\s+\"[^\"]*\")?)\)")
_AUTOLINK_RE = re.compile(r"<(https?://[^<>\s]+)>", re.IGNORECASE)
_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)", re.DOTALL)
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")
_TRAILING_SPACES_RE = re.compile(r"[ \t]+\n")


def format_plain_text_reply(raw_reply: object) -> str:
    """Convert raw Markdown/HTML-ish model output into readable widget text."""
    text = "" if raw_reply is None else str(raw_reply)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text, protected_segments = _extract_protected_segments(text)
    text = _format_non_code_text(text)
    text = _restore_protected_segments(text, protected_segments)
    return _normalize_spacing(text)


def _extract_protected_segments(text: str) -> tuple[str, list[str]]:
    protected_segments: list[str] = []

    def store(value: str) -> str:
        placeholder = f"CHOTAPROTECTEDTOKEN{len(protected_segments)}CHOTA"
        protected_segments.append(value)
        return placeholder

    def replace_fenced_code(match: re.Match[str]) -> str:
        leading_newline = match.group(1)
        code = match.group(3).strip("\n")
        return f"{leading_newline}{store(code)}"

    text = _FENCED_CODE_RE.sub(replace_fenced_code, text)
    text = _INLINE_CODE_RE.sub(lambda match: store(match.group(1)), text)
    text = _AUTOLINK_RE.sub(lambda match: store(match.group(1)), text)
    return text, protected_segments


def _restore_protected_segments(text: str, protected_segments: list[str]) -> str:
    for index, segment in enumerate(protected_segments):
        text = text.replace(f"CHOTAPROTECTEDTOKEN{index}CHOTA", segment)
    return text


def _format_non_code_text(text: str) -> str:
    text = _SCRIPT_STYLE_RE.sub("", text)
    text = _DANGEROUS_EMPTY_TAG_RE.sub("", text)
    text = _BLOCK_BOUNDARY_RE.sub("\n", text)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _INLINE_TAG_RE.sub("", text)
    text = _PAIRED_OPEN_TAG_RE.sub("", text)
    text = _ATTR_TAG_RE.sub("", text)
    text = _SELF_CLOSING_TAG_RE.sub("", text)
    text = _CLOSING_TAG_RE.sub("", text)
    text = _MARKDOWN_LINK_RE.sub(_replace_markdown_link, text)
    text = _BOLD_RE.sub(r"\2", text)
    text = _ITALIC_RE.sub(lambda match: match.group(1) or match.group(2) or "", text)
    return html.unescape(text)


def _replace_markdown_link(match: re.Match[str]) -> str:
    label = match.group(1)
    url = match.group(2).strip()
    if " " in url:
        url = url.split(" ", 1)[0]
    return f"{label} ({url})"


def _normalize_spacing(text: str) -> str:
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _TRAILING_SPACES_RE.sub("\n", text)
    text = _EXCESS_BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()
