"""Writer-side halves of the backlinks read: naming a section, and its drift now.

The daemon never reads workspace files, so an adapter beside the workspace
(CLI, MCP, SDK) does two things for a backlinks read:

- ``section_source`` turns a workspace path the agent typed into the catalog
  name the daemon keys sections by (a catalog name passes through);
- ``observe_drift`` compares each section row's cited bytes with the section
  in the file now: ``unchanged`` or ``changed_since_cited`` (a section that is
  gone has changed). A row whose file is not catalogued here keeps the
  daemon's answer. ``observe_get_drift`` does the same for ``get`` with
  ``detail="backlinks"``.

Cost: one catalog parse (cached by revision) and one parse per distinct file
on the page.
"""

from __future__ import annotations

from pathlib import Path

from cruxible_client._safe_files import read_regular_file
from cruxible_client.authoring.selectors import workspace_files
from cruxible_client.contracts.backlinks import Backlink, BacklinksResult
from cruxible_client.contracts.doc_sections import (
    DocSectionError,
    SectionDocument,
    parse_sections,
    section_digest,
)
from cruxible_client.contracts.get_reads import GetResult


def section_source(workspace: Path | None, file: str) -> str:
    """The catalog name of ``file``: itself when catalogued by name, else by its path."""

    if workspace is None:
        return file
    try:
        catalog = workspace_files(workspace)
    except ValueError:
        return file
    if file in {entry.name for entry in catalog.file_entries}:
        return file
    path = Path(file).expanduser()
    matches = catalog.entries_for_path(path if path.is_absolute() else catalog.workspace / path)
    return matches[0].name if len(matches) == 1 else file


def observe_drift(result: BacklinksResult, workspace: Path | None) -> BacklinksResult:
    """Each section row's drift against the workspace file as it is now."""

    links = _observed(result.links, workspace)
    return result if links is None else result.model_copy(update={"links": links})


def observe_get_drift(result: GetResult, workspace: Path | None) -> GetResult:
    """``get`` with ``detail="backlinks"``: the same drift the backlinks read shows,
    so one note reads one status whichever read asked."""

    page = result.backlinks
    if page is None:
        return result
    links = _observed(page.links, workspace)
    if links is None:
        return result
    return result.model_copy(update={"backlinks": page.model_copy(update={"links": links})})


def _observed(links: tuple[Backlink, ...], workspace: Path | None) -> tuple[Backlink, ...] | None:
    """The rows with each section row's drift now; None when none can be checked here."""

    if workspace is None or not any(link.file for link in links):
        return None
    try:
        catalog = workspace_files(workspace)
    except ValueError:
        return None
    documents: dict[str, SectionDocument | None] = {}
    observed = []
    for link in links:
        if link.file is None or link.section is None or link.cited_digest is None:
            observed.append(link)
            continue
        if link.file not in documents:
            try:
                path = catalog.path_for_file(link.file)
                documents[link.file] = parse_sections(read_regular_file(path), file=str(path))
            except (OSError, ValueError):
                documents[link.file] = None
        document = documents[link.file]
        if document is None:
            observed.append(link)
            continue
        try:
            current = section_digest(document.own_body(link.section))
        except DocSectionError:
            current = None
        drift = "unchanged" if current == link.cited_digest else "changed_since_cited"
        observed.append(link.model_copy(update={"drift": drift}))
    return tuple(observed)


__all__ = ["observe_drift", "observe_get_drift", "section_source"]
