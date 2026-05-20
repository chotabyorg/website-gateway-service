from __future__ import annotations

import html
import re

PARAGRAPH_INDENT = "    "
LIST_INDENT = "  "
LIST_MARKER = "\u2022"
CODE_LABEL = "Код:"

_LIST_ITEM_RE = re.compile(r"^(\s*)([-+*]|\d+[.)])\s+(.*)$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_HTML_TAG_RE = re.compile(r"(?is)<[^>]+>")
_HTML_DANGEROUS_BLOCK_RE = re.compile(r"(?is)<(script|style).*?>.*?</\1>")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_AUTOLINK_RE = re.compile(r"<(https?://[^>]+)>")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_DOUBLE_MARK_RE = re.compile(r"(\*\*|__|~~)(.+?)\1")
_SINGLE_MARK_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)|(?<!_)_(?!\s)(.+?)(?<!\s)_(?!_)")
_LEFTOVER_MARK_RE = re.compile(r"(^|[\s([{\-])([*_~`]{1,3})(?=\S)|(?<=\S)([*_~`]{1,3})(?=[\s)\]}.,!?;:]|$)")


def format_agent_reply_for_website(raw_text: str) -> str:
    text = _strip_html_and_unescape(raw_text or "")
    if not text:
        return ""

    blocks = _parse_blocks(text)
    rendered = [_render_block(block_type, block_lines) for block_type, block_lines in blocks]
    result = "\n\n".join(chunk for chunk in rendered if chunk)
    return result.rstrip()


def _strip_html_and_unescape(text: str) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _HTML_DANGEROUS_BLOCK_RE.sub("", cleaned)
    cleaned = _HTML_TAG_RE.sub("", cleaned)
    cleaned = html.unescape(cleaned)
    return cleaned.strip()


def _parse_blocks(text: str) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = []
    paragraph_lines: list[str] = []
    list_lines: list[str] = []
    code_lines: list[str] = []
    in_code_block = False

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if paragraph_lines:
            blocks.append(("paragraph", paragraph_lines))
            paragraph_lines = []

    def flush_list() -> None:
        nonlocal list_lines
        if list_lines:
            blocks.append(("list", list_lines))
            list_lines = []

    def flush_code() -> None:
        nonlocal code_lines
        if code_lines:
            blocks.append(("code", code_lines))
            code_lines = []

    for line in text.split("\n"):
        trimmed = line.strip()

        if trimmed.startswith("```"):
            flush_paragraph()
            flush_list()
            if in_code_block:
                flush_code()
                in_code_block = False
            else:
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(line.rstrip())
            continue

        if not trimmed:
            flush_paragraph()
            flush_list()
            continue

        normalized_line = _normalize_text_line(line)
        if not normalized_line:
            flush_paragraph()
            flush_list()
            continue

        if _is_list_line(normalized_line):
            flush_paragraph()
            list_lines.append(normalized_line)
            continue

        flush_list()
        paragraph_lines.append(normalized_line)

    if in_code_block and code_lines:
        flush_code()
    else:
        flush_paragraph()
        flush_list()
    return blocks


def _normalize_text_line(line: str) -> str:
    normalized = line.rstrip()
    normalized = re.sub(r"^\s{0,3}(?:>\s*)+", "", normalized)
    heading_match = _HEADING_RE.match(normalized)
    if heading_match:
        normalized = heading_match.group(1)
    if _HR_RE.match(normalized):
        return ""
    normalized = _normalize_inline(normalized)
    normalized = normalized.rstrip()
    return normalized if _is_list_line(normalized) else normalized.strip()


def _normalize_inline(text: str) -> str:
    normalized = _IMAGE_RE.sub(lambda m: m.group(1).strip() or m.group(2).strip(), text)

    def _replace_link(match: re.Match[str]) -> str:
        title = match.group(1).strip()
        url = match.group(2).strip()
        return f"{title} ({url})" if url and title != url else title

    normalized = _LINK_RE.sub(_replace_link, normalized)
    normalized = _AUTOLINK_RE.sub(lambda m: m.group(1).strip(), normalized)
    normalized = _INLINE_CODE_RE.sub(lambda m: m.group(1), normalized)

    while True:
        collapsed = _DOUBLE_MARK_RE.sub(lambda m: m.group(2), normalized)
        if collapsed == normalized:
            break
        normalized = collapsed

    normalized = _SINGLE_MARK_RE.sub(lambda m: m.group(1) or m.group(2) or "", normalized)
    normalized = _LEFTOVER_MARK_RE.sub(lambda m: m.group(1) or "", normalized)
    return normalized


def _is_list_line(line: str) -> bool:
    return bool(_LIST_ITEM_RE.match(line))


def _render_block(block_type: str, lines: list[str]) -> str:
    if block_type == "code":
        body = "\n".join(f"{PARAGRAPH_INDENT}{line}" if line else "" for line in lines)
        return f"{CODE_LABEL}\n{body}" if body else CODE_LABEL

    if block_type == "list":
        rendered: list[str] = []
        for line in lines:
            match = _LIST_ITEM_RE.match(line)
            if not match:
                rendered.append(f"{PARAGRAPH_INDENT}{line.strip()}")
                continue
            leading, marker, content = match.groups()
            level = max(0, len(leading.expandtabs(4)) // 2)
            prefix = f"{LIST_INDENT * level}{_normalize_list_marker(marker)} "
            rendered.append(f"{prefix}{content.strip()}")
        return "\n".join(rendered).strip()

    paragraph = " ".join(part.strip() for part in lines if part.strip())
    return f"{PARAGRAPH_INDENT}{paragraph}" if paragraph else ""


def _normalize_list_marker(marker: str) -> str:
    return f"{marker[:-1]}." if marker.endswith(")") and marker[:-1].isdigit() else LIST_MARKER
