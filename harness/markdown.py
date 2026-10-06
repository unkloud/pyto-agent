"""Small, dependency-free Markdown subset for the browser transcript.

This parser returns a JSON-safe syntax tree, never HTML. The browser builds the tree with
``createElement`` and ``createTextNode``, so model text cannot become markup.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

MAX_MARKDOWN_CHARS = 12000
MAX_NESTING = 12

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
_QUOTE = re.compile(r"^ {0,3}> ?(.*)$")
_LIST = re.compile(r"^( {0,3})([-+*]|[0-9]{1,9}[.)])[ \t]+(.*)$")
_PUNCTUATION = set('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~')


def parse_markdown(text: str) -> List[list]:
    """Parse a small Markdown subset into safe, JSON-serializable nodes.

    Supported blocks are paragraphs, ATX headings, blockquotes, nested ordered and
    unordered lists, and fenced code. Inline formatting includes emphasis, strong
    emphasis, code spans, and links. Raw HTML and images are always ordinary text.
    """
    if not isinstance(text, str) or not text:
        return []
    source = text[:MAX_MARKDOWN_CHARS]
    try:
        return _parse_blocks(source, 0)
    except RecursionError:
        # Malformed/deeply nested input stays readable as text instead of failing a turn.
        return [["paragraph", [["text", source]]]]


def _line_records(source: str) -> List[Tuple[str, int, int]]:
    records = []
    offset = 0
    for raw in source.splitlines(keepends=True):
        if raw.endswith("\r\n"):
            content = raw[:-2]
        elif raw.endswith(("\r", "\n")):
            content = raw[:-1]
        else:
            content = raw
        end = offset + len(raw)
        records.append((content, offset, end))
        offset = end
    return records


def _parse_blocks(source: str, depth: int) -> List[list]:
    if depth > MAX_NESTING:
        return [["paragraph", [["text", source]]]] if source else []

    lines = _line_records(source)
    blocks: List[list] = []
    index = 0

    while index < len(lines):
        content, start, end = lines[index]
        if not content.strip(" \t"):
            index += 1
            continue

        fence = _FENCE_OPEN.match(content)
        if fence:
            marker = fence.group(1)
            marker_char = marker[0]
            marker_length = len(marker)
            content_start = end
            close_start = len(source)
            next_index = len(lines)
            for candidate_index in range(index + 1, len(lines)):
                close = _FENCE_CLOSE.match(lines[candidate_index][0])
                if close and close.group(1)[0] == marker_char and len(close.group(1)) >= marker_length:
                    close_start = lines[candidate_index][1]
                    next_index = candidate_index + 1
                    break
            # Slice the original source so indentation, blank lines, spaces, and line
            # endings inside fenced code are not normalized by the Markdown parser.
            blocks.append(["code_block", source[content_start:close_start]])
            index = next_index
            continue

        heading = _HEADING.match(content)
        if heading:
            body = heading.group(2) or ""
            body = re.sub(r"[ \t]+#+[ \t]*$", "", body)
            blocks.append(["heading", len(heading.group(1)), _parse_inline(body, 0)])
            index += 1
            continue

        if _QUOTE.match(content):
            quoted = []
            while index < len(lines):
                quote = _QUOTE.match(lines[index][0])
                if not quote:
                    break
                quoted.append(quote.group(1))
                index += 1
            blocks.append(["quote", _parse_blocks("\n".join(quoted), depth + 1)])
            continue

        list_match = _LIST.match(content)
        if list_match:
            block, index = _parse_list(lines, index, depth)
            blocks.append(block)
            continue

        paragraph = [content]
        index += 1
        while index < len(lines):
            next_content = lines[index][0]
            if (
                not next_content.strip(" \t")
                or _FENCE_OPEN.match(next_content)
                or _HEADING.match(next_content)
                or _QUOTE.match(next_content)
                or _LIST.match(next_content)
            ):
                break
            paragraph.append(next_content)
            index += 1
        blocks.append(["paragraph", _parse_inline("\n".join(paragraph), 0)])

    return blocks


def _leading_spaces(value: str) -> int:
    return len(value) - len(value.lstrip(" "))


def _parse_list(lines: List[Tuple[str, int, int]], index: int, depth: int) -> Tuple[list, int]:
    first = _LIST.match(lines[index][0])
    assert first is not None
    base_indent = len(first.group(1))
    marker = first.group(2)
    ordered = marker[0].isdigit()
    start = int(marker[:-1]) if ordered else 1
    items = []

    while index < len(lines):
        current = _LIST.match(lines[index][0])
        if not current:
            break
        current_indent = len(current.group(1))
        current_marker = current.group(2)
        if current_indent != base_indent or current_marker[0].isdigit() != ordered:
            break

        item_lines = [current.group(3)]
        index += 1
        while index < len(lines):
            content = lines[index][0]
            if not content.strip(" \t"):
                blank_end = index
                while blank_end < len(lines) and not lines[blank_end][0].strip(" \t"):
                    blank_end += 1
                following = _LIST.match(lines[blank_end][0]) if blank_end < len(lines) else None
                if (
                    following
                    and len(following.group(1)) == base_indent
                    and following.group(2)[0].isdigit() == ordered
                ):
                    index = blank_end
                    break
                if blank_end < len(lines) and _leading_spaces(lines[blank_end][0]) >= base_indent + 2:
                    item_lines.extend([""] * (blank_end - index))
                    index = blank_end
                    continue
                index = blank_end
                break

            following = _LIST.match(content)
            following_indent = len(following.group(1)) if following else _leading_spaces(content)
            if following and following_indent <= base_indent:
                break
            if following_indent >= base_indent + 2:
                item_lines.append(content[base_indent + 2 :])
                index += 1
                continue
            break

        items.append(_parse_blocks("\n".join(item_lines), depth + 1))

        if index < len(lines):
            next_item = _LIST.match(lines[index][0])
            if (
                not next_item
                or len(next_item.group(1)) != base_indent
                or next_item.group(2)[0].isdigit() != ordered
            ):
                break

    return ["list", "ol" if ordered else "ul", start, items], index


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _find_delimiter(text: str, delimiter: str, start: int) -> int:
    index = start
    while True:
        index = text.find(delimiter, index)
        if index < 0:
            return -1
        if not _is_escaped(text, index):
            # A one-character emphasis marker cannot close on half of a strong marker.
            if len(delimiter) == 1 and text.startswith(delimiter * 2, index):
                index += 2
                continue
            return index
        index += len(delimiter)


def _find_code_close(text: str, delimiter_length: int, start: int) -> int:
    index = start
    while index < len(text):
        if text[index] != "`":
            index += 1
            continue
        run_end = index
        while run_end < len(text) and text[run_end] == "`":
            run_end += 1
        if run_end - index == delimiter_length:
            return index
        index = run_end
    return -1


def _find_link_close(text: str, start: int) -> int:
    depth = 0
    index = start
    while index < len(text):
        if _is_escaped(text, index):
            index += 1
            continue
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            if depth == 0:
                return index
            depth -= 1
        index += 1
    return -1


def _link_destination(value: str) -> Optional[str]:
    value = value.strip()
    if value.startswith("<"):
        end = value.find(">")
        if end < 0 or value[end + 1 :].strip():
            return None
        value = value[1:end]
    else:
        # Titles are deliberately unsupported; destination text ends at whitespace.
        value = value.split(None, 1)[0] if value else ""
    return value.replace(r"\(", "(").replace(r"\)", ")").replace(r"\\", "\\")


def _safe_link(value: Optional[str]) -> Optional[str]:
    if not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme in ("http", "https"):
            if not parsed.hostname:
                return None
            # Accessing port validates malformed/out-of-range numeric ports.
            _ = parsed.port
            return value
        if scheme == "mailto" and parsed.path:
            return value
    except ValueError:
        return None
    return None


def _parse_link(text: str, start: int, depth: int) -> Optional[Tuple[int, list]]:
    label_end = _find_delimiter(text, "]", start + 1)
    if label_end < 0 or label_end + 1 >= len(text) or text[label_end + 1] != "(":
        return None
    link_end = _find_link_close(text, label_end + 2)
    if link_end < 0:
        return None
    label = text[start + 1 : label_end]
    destination = _link_destination(text[label_end + 2 : link_end])
    href = _safe_link(destination)
    return link_end + 1, ["link", href, _parse_inline(label, depth + 1)]


def _parse_inline(text: str, depth: int) -> List[list]:
    if depth > MAX_NESTING:
        return [["text", text]] if text else []

    nodes: List[list] = []
    buffer: List[str] = []

    def flush() -> None:
        if buffer:
            value = "".join(buffer)
            if nodes and nodes[-1][0] == "text":
                nodes[-1][1] += value
            else:
                nodes.append(["text", value])
            buffer[:] = []

    index = 0
    while index < len(text):
        char = text[index]

        if char == "\\" and index + 1 < len(text) and text[index + 1] in _PUNCTUATION:
            buffer.append(text[index + 1])
            index += 2
            continue

        if char == "`":
            run_end = index
            while run_end < len(text) and text[run_end] == "`":
                run_end += 1
            run_length = run_end - index
            close = _find_code_close(text, run_length, run_end)
            if close >= 0:
                flush()
                code = text[run_end:close].replace("\n", " ")
                nodes.append(["code", code])
                index = close + run_length
                continue
            buffer.append(text[index:run_end])
            index = run_end
            continue

        if char == "!" and text.startswith("![", index):
            label_end = _find_delimiter(text, "]", index + 2)
            if label_end >= 0 and label_end + 1 < len(text) and text[label_end + 1] == "(":
                image_end = _find_link_close(text, label_end + 2)
                if image_end >= 0:
                    # Images can cause external requests and are outside this renderer's
                    # subset, so keep their complete Markdown spelling as inert text.
                    buffer.append(text[index : image_end + 1])
                    index = image_end + 1
                    continue

        if char == "[" and not _is_escaped(text, index):
            link = _parse_link(text, index, depth)
            if link is not None:
                flush()
                nodes.append(link[1])
                index = link[0]
                continue

        matched = False
        for delimiter in ("***", "___", "**", "__", "*", "_"):
            if not text.startswith(delimiter, index) or _is_escaped(text, index):
                continue
            if delimiter.startswith("_") and index > 0 and text[index - 1].isalnum():
                continue
            close = _find_delimiter(text, delimiter, index + len(delimiter))
            if close <= index + len(delimiter):
                if len(delimiter) > 1:
                    buffer.append(delimiter)
                    index += len(delimiter)
                    matched = True
                    break
                continue
            if delimiter.endswith("_") and close + len(delimiter) < len(text) and text[close + len(delimiter)].isalnum():
                continue
            flush()
            children = _parse_inline(text[index + len(delimiter) : close], depth + 1)
            if len(delimiter) == 3:
                nodes.append(["strong", [["em", children]]])
            elif len(delimiter) == 2:
                nodes.append(["strong", children])
            else:
                nodes.append(["em", children])
            index = close + len(delimiter)
            matched = True
            break
        if matched:
            continue

        buffer.append(char)
        index += 1

    flush()
    return nodes
