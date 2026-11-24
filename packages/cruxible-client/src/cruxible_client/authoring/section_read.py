"""Read one Markdown section of a catalogued workspace file: ``cruxible section``.

The read behind a section locator (``FILE#Heading/Path``, ``FILE#`` for the
preamble) that ``get``, ``backlinks`` and floor cards print. It runs beside the
workspace -- the daemon never reads workspace files -- and returns the section's
own body (its heading line and prose, up to its first subsection), the digest a
citation or a note records for exactly those bytes, and the subsections' names,
so a long file is read one section at a time.

Cost: one catalog parse (cached by revision) and one parse of the one file.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client._safe_files import read_regular_file
from cruxible_client.authoring.sdk_types import SourceSelectionError
from cruxible_client.authoring.selectors import workspace_files
from cruxible_client.contracts.doc_sections import (
    DocSectionError,
    SectionPath,
    parse_sections,
    section_digest,
)

#: Nearest heading paths a refusal names.
_NEAREST = 5
#: Top-level headings a refusal lists when nothing is near.
_TOP_LEVEL = 10


class SectionRead(BaseModel):
    """One section's own body, as read from the workspace now."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str = Field(description="The catalogued file (files.yaml name).")
    path: str = Field(description="Where the catalog locates it, workspace-relative when inside.")
    section: SectionPath = Field(description="The heading path; empty for the preamble.")
    digest: str = Field(description="sha256 of the section's own body, as a citation records it.")
    text: str = Field(description="The section's own body: its heading line and prose.")
    subsections: tuple[str, ...] = Field(
        default=(), description="Its direct subsections' headings, each readable as FILE#Path/Name."
    )


class SectionReadError(ValueError):
    """A section read that cannot answer, with its code, nearest names and one repair."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        candidates: tuple[str, ...] = (),
        repair: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidates = candidates
        self.repair = repair


def parse_section_locator(locator: str) -> tuple[str, SectionPath]:
    """``FILE#Heading/Path`` as (file, heading path); ``FILE#`` names the preamble."""

    file, mark, rest = locator.partition("#")
    if not file or not mark:
        raise SectionReadError(
            "cruxible.doc.locator_invalid",
            f"{locator!r} is not a section locator",
            repair="Pass FILE#Heading/Path (FILE# for the preamble), as get and backlinks print it.",
        )
    return file, tuple(item.strip() for item in rest.split("/") if item.strip())


def read_section(workspace: Path, locator: str) -> SectionRead:
    """The section a locator names, read from the catalogued file as it is now."""

    file, heading = parse_section_locator(locator)
    try:
        catalog = workspace_files(workspace)
        name = catalog.file_name(file)
        located = catalog.path_for_file(name)
    except SourceSelectionError as exc:
        raise SectionReadError(
            "cruxible.doc.file_not_catalogued",
            str(exc).removeprefix(f"{exc.code}: "),
            repair="Name a file files.yaml catalogues, by its path or name; "
            "`cruxible files add PATH` catalogues one.",
        ) from None
    except ValueError as exc:
        raise SectionReadError(
            "cruxible.doc.catalog_unreadable",
            str(exc),
            repair="Run it from the workspace, or pass --workspace-root.",
        ) from None
    root = catalog.workspace
    shown = located.relative_to(root).as_posix() if located.is_relative_to(root) else str(located)
    try:
        content = read_regular_file(located)
    except OSError as exc:
        raise SectionReadError(
            "cruxible.doc.file_missing",
            f"{name} is catalogued at {shown}, which cannot be read: {exc.strerror or exc}",
            repair="Restore the file, or point its catalog entry at where it is now.",
        ) from None
    try:
        document = parse_sections(content, file=str(located))
    except DocSectionError as exc:
        raise SectionReadError(exc.code, str(exc), repair=exc.repair) from None
    try:
        found = document.section(heading)
    except DocSectionError as exc:
        if exc.code != "cruxible.doc.section_not_found":
            raise SectionReadError(exc.code, str(exc), repair=exc.repair) from None
        paths = ["/".join(item.path) for item in document.sections]
        wanted = "/".join(heading)
        nearest = tuple(difflib.get_close_matches(wanted, paths, n=_NEAREST, cutoff=0.4))
        top = [item.path[0] for item in document.sections if len(item.path) == 1]
        listed = "; ".join(top[:_TOP_LEVEL]) + (" (and more)" if len(top) > _TOP_LEVEL else "")
        raise SectionReadError(
            exc.code,
            f"{shown} has no section {wanted!r}",
            candidates=tuple(f"{name}#{item}" for item in nearest),
            repair="Name the section by its full heading path, joined with /"
            + (f"; top-level headings: {listed}" if listed else "; the file has no headings")
            + ".",
        ) from None
    body = content[found.start : found.own_end]
    depth = len(heading)
    children = tuple(
        item.path[-1]
        for item in document.sections
        if len(item.path) == depth + 1 and item.path[:depth] == heading
    )
    return SectionRead(
        file=name,
        path=shown,
        section=heading,
        digest=section_digest(body),
        text=body.decode("utf-8"),
        subsections=children,
    )


__all__ = ["SectionRead", "SectionReadError", "parse_section_locator", "read_section"]
