import html
import re


_FENCED_CODE_RE = re.compile(
    r"(^|\n)[ \t]*(```|~~~)([^\n]*)\n(.*?)(?:\n[ \t]*\2[ \t]*(?=\n|$)|$)",
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
_AUTOLINK_RE = re.compile(r"<(https?://[^<>\s]+)>", re.IGNORECASE)
_INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")
_TRAILING_SPACES_RE = re.compile(r"[ \t]+\n")


def format_plain_text_reply(raw_reply: object) -> str:
    """Convert raw model output into safe Markdown-ish widget text."""
    text = "" if raw_reply is None else str(raw_reply)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text, protected_segments = _extract_protected_segments(text)
    text = _format_non_code_text(text)
    text = _normalize_spacing(text)
    text = _restore_protected_segments(text, protected_segments)
    return text.strip()


def _extract_protected_segments(text: str) -> tuple[str, list[str]]:
    protected_segments: list[str] = []

    def store(value: str) -> str:
        placeholder = f"CHOTAPROTECTEDTOKEN{len(protected_segments)}CHOTA"
        protected_segments.append(value)
        return placeholder

    def replace_fenced_code(match: re.Match[str]) -> str:
        leading_newline = match.group(1)
        fence = match.group(2)
        info = match.group(3).rstrip()
        code = match.group(4).strip("\n")
        return f"{leading_newline}{store(f'{fence}{info}\n{code}\n{fence}')}"

    text = _FENCED_CODE_RE.sub(replace_fenced_code, text)
    text = _INLINE_CODE_RE.sub(lambda match: store(f"`{match.group(1)}`"), text)
    text = _AUTOLINK_RE.sub(lambda match: store(match.group(0)), text)
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
    return html.unescape(text)


def _normalize_spacing(text: str) -> str:
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _TRAILING_SPACES_RE.sub("\n", text)
    text = _EXCESS_BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()
