"""Deterministic exact-byte Markdown spans for Claim source mappings."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Literal

from markdown_it import MarkdownIt
from markdown_it.token import Token
from pydantic import BaseModel, ConfigDict, field_validator

from cruxible_client.contracts.canonical import CasDigest, canonical_bytes

MARKDOWN_SPANS_V1 = "playbill-markdown-spans-v1"
_FRONT_MATTER_DELIMITER = "---"
_PREVIEW_CHARS = 240


class _StrictMarkdownSpanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarkdownSpanV1(_StrictMarkdownSpanModel):
    """One semantic Markdown block bound to exact source bytes."""

    tag: Literal["playbill-markdown-span-v1"] = "playbill-markdown-span-v1"
    span_id: str
    parser_version: Literal["playbill-markdown-spans-v1"] = "playbill-markdown-spans-v1"
    source_content_digest: str
    span_content_digest: str
    heading_path: tuple[str, ...] = ()
    block_selector: str
    block_type: Literal[
        "front_matter",
        "section",
        "paragraph",
        "list",
        "list_item",
        "code_fence",
        "blockquote",
        "table",
    ]
    start_byte: int
    end_byte: int
    line_start: int
    line_end: int
    preview: str | None = None
    label: str | None = None

    @field_validator("source_content_digest", "span_content_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        CasDigest.from_tagged(value)
        return value


def parse_markdown_spans(content: bytes) -> tuple[MarkdownSpanV1, ...]:
    """Parse UTF-8 Markdown while retaining exact offsets into the original bytes."""

    text = content.decode("utf-8")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized_lines = normalized.splitlines()
    original_lines = content.splitlines(keepends=True)
    line_offsets = [0]
    for line in original_lines:
        line_offsets.append(line_offsets[-1] + len(line))
    source_digest = CasDigest(hashlib.sha256(content).hexdigest()).tagged
    tokens = _markdown().parse(normalized)
    raw_spans: list[tuple[tuple[str, ...], str, str, int, int, str | None]] = []

    front_matter_end = _front_matter_end_line(normalized_lines)
    if front_matter_end is not None:
        raw_spans.append(((), "front_matter", "front_matter", 1, front_matter_end, "front matter"))

    heading_events = _heading_events(tokens, front_matter_end=front_matter_end)
    heading_stack: list[tuple[int, str]] = []
    for index, (level, title, start_line) in enumerate(heading_events):
        heading_stack = [item for item in heading_stack if item[0] < level]
        heading_stack.append((level, title))
        section_end = len(normalized_lines)
        for next_level, _next_title, next_start in heading_events[index + 1 :]:
            if next_level <= level:
                section_end = next_start - 1
                break
        raw_spans.append(
            (
                tuple(value for _heading_level, value in heading_stack),
                "section",
                "section",
                start_line,
                max(start_line, section_end),
                title,
            )
        )

    block_counts: Counter[tuple[str, ...]] = Counter()
    heading_path_by_line = _heading_path_by_line(normalized_lines, heading_events)
    for token in tokens:
        block_type = _block_type(token)
        if block_type is None or token.map is None:
            continue
        line_start = int(token.map[0]) + 1
        line_end = int(token.map[1])
        if front_matter_end is not None and line_end <= front_matter_end:
            continue
        heading_path = heading_path_by_line.get(line_start, ())
        count_key = (*heading_path, block_type)
        block_counts[count_key] += 1
        selector = _block_selector(block_type, block_counts[count_key])
        raw_spans.append(
            (
                heading_path,
                selector,
                block_type,
                line_start,
                line_end,
                heading_path[-1] if heading_path else None,
            )
        )

    result = tuple(
        _span(
            content=content,
            source_digest=source_digest,
            line_offsets=line_offsets,
            heading_path=heading_path,
            block_selector=block_selector,
            block_type=block_type,
            line_start=line_start,
            line_end=line_end,
            label=label,
        )
        for heading_path, block_selector, block_type, line_start, line_end, label in raw_spans
    )
    return tuple(
        sorted(
            result,
            key=lambda item: (item.start_byte, item.end_byte, item.block_selector, item.span_id),
        )
    )


def _markdown() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"sourceMap": True})
    parser.enable("table")
    return parser


def _front_matter_end_line(lines: list[str]) -> int | None:
    if not lines or lines[0].strip() != _FRONT_MATTER_DELIMITER:
        return None
    for index, line in enumerate(lines[1:], start=2):
        if line.strip() == _FRONT_MATTER_DELIMITER:
            return index
    return None


def _heading_events(
    tokens: list[Token],
    *,
    front_matter_end: int | None,
) -> list[tuple[int, str, int]]:
    events: list[tuple[int, str, int]] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.map is None:
            continue
        line_start = int(token.map[0]) + 1
        if front_matter_end is not None and line_start <= front_matter_end:
            continue
        title = ""
        if index + 1 < len(tokens) and tokens[index + 1].type == "inline":
            title = tokens[index + 1].content.strip()
        level = int(token.tag.removeprefix("h") or "1")
        events.append((level, title, line_start))
    return events


def _heading_path_by_line(
    lines: list[str],
    heading_events: list[tuple[int, str, int]],
) -> dict[int, tuple[str, ...]]:
    result: dict[int, tuple[str, ...]] = {}
    stack: list[tuple[int, str]] = []
    events_by_line = {line: (level, title) for level, title, line in heading_events}
    for line_number in range(1, len(lines) + 1):
        event = events_by_line.get(line_number)
        if event is not None:
            level, title = event
            stack = [item for item in stack if item[0] < level]
            stack.append((level, title))
        result[line_number] = tuple(value for _level, value in stack)
    return result


def _block_type(token: Token) -> str | None:
    return {
        "paragraph_open": "paragraph",
        "bullet_list_open": "list",
        "ordered_list_open": "list",
        "list_item_open": "list_item",
        "fence": "code_fence",
        "code_block": "code_fence",
        "blockquote_open": "blockquote",
        "table_open": "table",
    }.get(token.type)


def _block_selector(block_type: str, count: int) -> str:
    if block_type in {"list_item", "code_fence"}:
        return f"{block_type}:{count}"
    return f"{block_type}:{count}"


def _span(
    *,
    content: bytes,
    source_digest: str,
    line_offsets: list[int],
    heading_path: tuple[str, ...],
    block_selector: str,
    block_type: str,
    line_start: int,
    line_end: int,
    label: str | None,
) -> MarkdownSpanV1:
    start_byte = line_offsets[max(line_start - 1, 0)]
    end_byte = line_offsets[min(line_end, len(line_offsets) - 1)]
    body = content[start_byte:end_byte]
    span_digest = CasDigest(hashlib.sha256(body).hexdigest()).tagged
    identity = canonical_bytes(
        {
            "block_selector": block_selector,
            "end_byte": end_byte,
            "heading_path": list(heading_path),
            "parser_version": MARKDOWN_SPANS_V1,
            "source_content_digest": source_digest,
            "start_byte": start_byte,
        }
    )
    span_id = f"mdspan_{hashlib.sha256(identity).hexdigest()[:16]}"
    compact = re.sub(r"\s+", " ", body.decode("utf-8")).strip()
    preview = compact if len(compact) <= _PREVIEW_CHARS else compact[:239].rstrip() + "..."
    return MarkdownSpanV1(
        span_id=span_id,
        source_content_digest=source_digest,
        span_content_digest=span_digest,
        heading_path=heading_path,
        block_selector=block_selector,
        block_type=block_type,  # type: ignore[arg-type]
        start_byte=start_byte,
        end_byte=end_byte,
        line_start=line_start,
        line_end=line_end,
        preview=preview or None,
        label=label,
    )


__all__ = ["MARKDOWN_SPANS_V1", "MarkdownSpanV1", "parse_markdown_spans"]
