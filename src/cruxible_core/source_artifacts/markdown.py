"""Deterministic Markdown chunk parsing for local source evidence."""

from __future__ import annotations

import hashlib
import re
from collections import Counter

from markdown_it import MarkdownIt
from markdown_it.token import Token

from cruxible_core.primitives import canonical_json
from cruxible_core.source_artifacts.types import (
    MARKDOWN_CHUNKS_V1,
    SourceArtifactChunk,
)

_PREVIEW_CHARS = 240
_FRONT_MATTER_DELIMITER = "---"


def parse_markdown_chunks(
    *,
    source_artifact_id: str,
    content: bytes,
    parser_version: str = MARKDOWN_CHUNKS_V1,
) -> list[SourceArtifactChunk]:
    """Parse Markdown bytes into deterministic source evidence chunks."""
    text = content.decode("utf-8")
    normalized = _normalize_line_endings(text)
    lines = normalized.splitlines()
    tokens = _markdown().parse(normalized)
    chunks: list[SourceArtifactChunk] = []

    front_matter_end = _front_matter_end_line(lines)
    if front_matter_end is not None:
        chunks.append(
            _chunk(
                source_artifact_id=source_artifact_id,
                parser_version=parser_version,
                heading_path=[],
                block_selector="front_matter",
                block_type="front_matter",
                line_start=1,
                line_end=front_matter_end,
                lines=lines,
                label="front matter",
            )
        )

    heading_stack: list[tuple[int, str]] = []
    heading_events = _heading_events(tokens, front_matter_end=front_matter_end)
    for index, event in enumerate(heading_events):
        level, title, start_line = event
        heading_stack = [
            (existing_level, value)
            for existing_level, value in heading_stack
            if existing_level < level
        ]
        heading_stack.append((level, title))
        section_end = len(lines)
        for next_level, _next_title, next_start_line in heading_events[index + 1 :]:
            if next_level <= level:
                section_end = next_start_line - 1
                break
        chunks.append(
            _chunk(
                source_artifact_id=source_artifact_id,
                parser_version=parser_version,
                heading_path=[value for _level, value in heading_stack],
                block_selector="section",
                block_type="section",
                line_start=start_line,
                line_end=max(start_line, section_end),
                lines=lines,
                label=title,
            )
        )

    block_counts: Counter[tuple[str, ...]] = Counter()
    heading_path_by_line = _heading_path_by_line(lines, heading_events)
    for token in tokens:
        block_type = _block_type(token)
        if block_type is None or token.map is None:
            continue
        line_start = int(token.map[0]) + 1
        line_end = int(token.map[1])
        if front_matter_end is not None and line_end <= front_matter_end:
            continue
        heading_path = heading_path_by_line.get(line_start, [])
        count_key = (*heading_path, block_type)
        block_counts[count_key] += 1
        selector = _block_selector(block_type, block_counts[count_key])
        chunks.append(
            _chunk(
                source_artifact_id=source_artifact_id,
                parser_version=parser_version,
                heading_path=heading_path,
                block_selector=selector,
                block_type=block_type,
                line_start=line_start,
                line_end=line_end,
                lines=lines,
                label=heading_path[-1] if heading_path else None,
            )
        )

    chunks.sort(key=lambda item: (item.line_start, item.block_selector, item.chunk_id))
    return chunks


def _markdown() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"sourceMap": True})
    parser.enable("table")
    return parser


def _normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


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
) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {}
    stack: list[tuple[int, str]] = []
    events_by_line = {line: (level, title) for level, title, line in heading_events}
    for line_number in range(1, len(lines) + 1):
        event = events_by_line.get(line_number)
        if event is not None:
            level, title = event
            stack = [
                (existing_level, value) for existing_level, value in stack if existing_level < level
            ]
            stack.append((level, title))
        result[line_number] = [value for _level, value in stack]
    return result


def _block_type(token: Token) -> str | None:
    if token.type == "paragraph_open":
        return "paragraph"
    if token.type in ("bullet_list_open", "ordered_list_open"):
        return "list"
    if token.type == "list_item_open":
        return "list_item"
    if token.type in ("fence", "code_block"):
        return "code_fence"
    if token.type == "blockquote_open":
        return "blockquote"
    if token.type == "table_open":
        return "table"
    return None


def _block_selector(block_type: str, count: int) -> str:
    if block_type == "list_item":
        return f"list_item:{count}"
    if block_type == "code_fence":
        return f"code_fence:{count}"
    return f"{block_type}:{count}"


def _chunk(
    *,
    source_artifact_id: str,
    parser_version: str,
    heading_path: list[str],
    block_selector: str,
    block_type: str,
    line_start: int,
    line_end: int,
    lines: list[str],
    label: str | None,
) -> SourceArtifactChunk:
    body = "\n".join(lines[max(line_start - 1, 0) : max(line_end, line_start)])
    content_hash = _sha256_text(body)
    identity = {
        "source_artifact_id": source_artifact_id,
        "parser_version": parser_version,
        "heading_path": heading_path,
        "block_selector": block_selector,
    }
    chunk_digest = hashlib.sha256(canonical_json(identity).encode()).hexdigest()[:16]
    return SourceArtifactChunk(
        chunk_id=f"mdchunk_{chunk_digest}",
        heading_path=heading_path,
        block_selector=block_selector,
        block_type=block_type,
        content_hash=content_hash,
        line_start=line_start,
        line_end=line_end,
        preview=_preview(body),
        label=label,
    )


def _preview(value: str) -> str | None:
    compact = re.sub(r"\s+", " ", value).strip()
    if not compact:
        return None
    if len(compact) <= _PREVIEW_CHARS:
        return compact
    return f"{compact[: _PREVIEW_CHARS - 1].rstrip()}..."


def _sha256_text(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"
