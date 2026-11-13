"""Pure Markdown section addressing and edits; no filesystem or daemon access.

Each parse/edit visits only the document being edited, O(document bytes). Paths
are labels for authoring; exact section bytes, rather than paths, determine drift.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Annotated

from pydantic import AfterValidator, Field


def _heading_text(value: str) -> str:
    if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("heading text must be trimmed and contain no controls")
    return value


HeadingText = Annotated[str, Field(), AfterValidator(_heading_text)]
SectionPath = tuple[HeadingText, ...]

_HEADING = re.compile(rb"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
_FENCE = re.compile(rb"^ {0,3}(`{3,}|~{3,})(.*)$")


def section_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


class DocSectionError(ValueError):
    """A repairable section refusal, including locations for ambiguous labels."""

    def __init__(
        self, code: str, message: str, *, lines: tuple[int, ...] = (), repair: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = "cruxible.doc." + code
        self.lines = lines
        #: The one fix for this refusal, when the raise site knows it.
        self.repair = repair


@dataclass(frozen=True)
class DocSection:
    path: SectionPath
    level: int
    line: int
    start: int
    own_end: int
    end: int
    digest: str


@dataclass(frozen=True)
class SectionDocument:
    content: bytes
    sections: tuple[DocSection, ...]
    preamble: DocSection
    newline: bytes

    def section(self, path: SectionPath) -> DocSection:
        if not path:
            return self.preamble
        matches = tuple(section for section in self.sections if section.path == path)
        if len(matches) > 1:
            lines = tuple(section.line for section in matches)
            raise DocSectionError(
                "section_ambiguous", f"Section {path!r} occurs on lines {lines}.", lines=lines
            )
        if not matches:
            raise DocSectionError("section_not_found", f"Section {path!r} was not found.")
        return matches[0]

    def subtree(self, path: SectionPath) -> bytes:
        section = self.section(path)
        return self.content[section.start : section.end]

    def own_body(self, path: SectionPath) -> bytes:
        section = self.section(path)
        return self.content[section.start : section.own_end]


def parse_sections(content: bytes, *, file: str = "document.md") -> SectionDocument:
    """Parse ATX headings outside fences/frontmatter with exact UTF-8 byte extents."""

    if PurePath(file).suffix.lower() not in (".md", ".markdown"):
        raise DocSectionError("not_markdown", "Section edits require a .md or .markdown file.")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocSectionError("not_markdown", "Section edits require UTF-8 Markdown.") from exc
    lines = content.splitlines(keepends=True)
    newline = b"\r\n" if b"\r\n" in content else b"\n"
    frontmatter = bool(lines and lines[0].rstrip(b"\r\n") == b"---")
    frontmatter_end = 0
    fence: tuple[int, int] | None = None
    # Mutable records allow one stack pass for subtree ends, even for deep docs.
    records: list[tuple[SectionPath, int, int, int]] = []
    ends: list[int] = []
    stack: list[int] = []
    offset = 0
    for line_number, line in enumerate(lines, 1):
        raw = line.rstrip(b"\r\n")
        if frontmatter:
            frontmatter_end = offset + len(line)
            if line_number > 1 and raw in (b"---", b"..."):
                frontmatter = False
            offset += len(line)
            continue
        opening = _FENCE.fullmatch(raw)
        if fence is not None:
            if opening and opening[1][0] == fence[0] and len(opening[1]) >= fence[1]:
                if not opening[2].strip():
                    fence = None
        elif opening and (opening[1][0] != 96 or b"`" not in opening[2]):
            fence = (opening[1][0], len(opening[1]))
        else:
            heading = _HEADING.fullmatch(raw)
            if heading:
                title = (heading[2] or b"").strip(b" \t")
                title = re.sub(rb"(?:^|[ \t]+)#+[ \t]*$", b"", title).strip(b" \t")
                text = title.decode("utf-8").strip()
                try:
                    _heading_text(text)
                except ValueError as exc:
                    raise DocSectionError(
                        "section_invalid", str(exc), lines=(line_number,)
                    ) from exc
                level = len(heading[1])
                while stack and records[stack[-1]][1] >= level:
                    ends[stack.pop()] = offset
                parent = records[stack[-1]][0] if stack else ()
                records.append(((*parent, text), level, line_number, offset))
                ends.append(len(content))
                stack.append(len(records) - 1)
        offset += len(line)
    sections = tuple(
        DocSection(
            path=path,
            level=level,
            line=line,
            start=start,
            own_end=records[i + 1][3] if i + 1 < len(records) else len(content),
            end=ends[i],
            digest=section_digest(content[start : ends[i]]),
        )
        for i, (path, level, line, start) in enumerate(records)
    )
    preamble_end = records[0][3] if records else len(content)
    preamble = DocSection(
        (),
        0,
        1,
        frontmatter_end,
        preamble_end,
        preamble_end,
        section_digest(content[frontmatter_end:preamble_end]),
    )
    return SectionDocument(content, sections, preamble, newline)


@dataclass(frozen=True)
class SectionEdit:
    content: bytes
    section: SectionPath
    before_digest: str | None
    after_digest: str
    changed: bool


def _inserted(text: str, newline: bytes) -> bytes:
    raw = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    if raw and not raw.endswith(b"\n"):
        raw += b"\n"
    return raw.replace(b"\n", newline)


def _unique_new_paths(before: SectionDocument, after: SectionDocument) -> None:
    counts: dict[SectionPath, int] = {}
    for section in before.sections:
        counts[section.path] = counts.get(section.path, 0) + 1
    new_counts: dict[SectionPath, int] = {}
    for section in after.sections:
        new_counts[section.path] = new_counts.get(section.path, 0) + 1
        if new_counts[section.path] > max(1, counts.get(section.path, 0)):
            raise DocSectionError("section_exists", f"Section {section.path!r} already exists.")


def append_section(
    content: bytes | None, *, under: SectionPath = (), text: str, file: str = "document.md"
) -> SectionEdit:
    """Append one new child, or create a file with only the agent's text.

    A retry of an identical child is a no-op. File/catalog I/O belongs to the
    caller; ``None`` denotes a missing file, distinct from an empty existing one.
    """

    document = parse_sections(content or b"", file=file)
    parent = document.section(under)
    raw = _inserted(text, document.newline)
    inserted = parse_sections(raw, file=file)
    identical_document = not under and document.content == raw
    if not inserted.sections or (
        content is not None and not identical_document and inserted.sections[0].start != 0
    ):
        raise DocSectionError("section_invalid", "Append text must start with a heading.")
    if content is None and under:
        raise DocSectionError("section_not_found", "A new file requires a root append.")
    if content is None and inserted.preamble.end != inserted.preamble.start:
        raise DocSectionError("section_invalid", "Only frontmatter may precede the new heading.")
    first = inserted.sections[0]
    if under and first.level != parent.level + 1:
        wanted = parent.level + 1
        raise DocSectionError(
            "section_invalid",
            f"Under {parent.path[-1]!r} (level {parent.level}) the appended heading must be "
            f"level {wanted}; TEXT starts with {first.path[-1]!r} at level {first.level}.",
            repair=f"Start TEXT with {'#' * wanted} {first.path[-1]}",
        )
    siblings = [first, *(item for item in inserted.sections[1:] if item.level <= first.level)]
    if len(siblings) > 1:
        names = ", ".join(repr(item.path[-1]) for item in siblings[:5])
        more = f" (+{len(siblings) - 5})" if len(siblings) > 5 else ""
        raise DocSectionError(
            "section_invalid",
            f"An append adds one section; TEXT has {len(siblings)} headings at level "
            f"{first.level}"
            + ("" if all(item.level == first.level for item in siblings) else " or above")
            + f" ({names}{more}).",
            repair=(
                "Append each with its own op, or nest the later ones one level under "
                f"{first.path[-1]!r}"
            ),
        )
    if len({item.path for item in inserted.sections}) != len(inserted.sections):
        raise DocSectionError("section_exists", "Append text contains duplicate section paths.")
    if identical_document:
        current = document.section(first.path)
        return SectionEdit(document.content, current.path, current.digest, current.digest, False)
    parent_path = under
    if not under:
        parent_path = next(
            (item.path for item in reversed(document.sections) if item.level < first.level), ()
        )
    target = (*parent_path, first.path[0])
    existing = [item for item in document.sections if item.path == target]
    if existing:
        current = document.section(target)
        if document.subtree(target) == raw:
            return SectionEdit(document.content, target, current.digest, current.digest, False)
        raise DocSectionError("section_exists", f"Section {target!r} already exists.")
    position = parent.end if under else len(document.content)
    separator = (
        b"" if not position or document.content[:position].endswith(b"\n") else document.newline
    )
    result = document.content[:position] + separator + raw + document.content[position:]
    after = parse_sections(result, file=file)
    _unique_new_paths(document, after)
    return SectionEdit(result, target, None, after.section(target).digest, True)


def replace_section(
    content: bytes,
    *,
    section: SectionPath,
    text: str,
    expect: str | None = None,
    file: str = "document.md",
) -> SectionEdit:
    """Replace a subtree (including its heading), or the preamble after frontmatter."""

    document = parse_sections(content, file=file)
    raw = _inserted(text, document.newline)
    inserted = parse_sections(raw, file=file)
    try:
        current = document.section(section)
    except DocSectionError as exc:
        # A retry after a successful rename still names the original address.
        if exc.code != "cruxible.doc.section_not_found" or not section or not inserted.sections:
            raise
        renamed = (*section[:-1], inserted.sections[0].path[0])
        try:
            already = document.section(renamed)
        except DocSectionError:
            raise exc from None
        if document.subtree(renamed) != raw:
            raise exc
        return SectionEdit(content, renamed, already.digest, already.digest, False)
    if not section:
        if inserted.sections or inserted.preamble.start:
            raise DocSectionError("section_invalid", "Preamble text must contain no heading.")
        target: SectionPath = ()
    else:
        if (
            not inserted.sections
            or inserted.sections[0].start != 0
            or inserted.sections[0].level != current.level
            or any(item.level <= current.level for item in inserted.sections[1:])
        ):
            raise DocSectionError(
                "section_invalid", "Replace must contain one subtree at the target level."
            )
        target = (*section[:-1], inserted.sections[0].path[0])
    if content[current.start : current.end] == raw:
        return SectionEdit(content, target, current.digest, current.digest, False)
    if expect is not None and expect != current.digest:
        raise DocSectionError(
            "section_changed",
            f"Section {section!r} now has digest {current.digest}",
            repair=f"re-read the section, then retry with expect: {current.digest}",
        )
    result = content[: current.start] + raw + content[current.end :]
    after = parse_sections(result, file=file)
    _unique_new_paths(document, after)
    return SectionEdit(result, target, current.digest, after.section(target).digest, True)
