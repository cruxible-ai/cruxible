"""Writer-side halves of the backlinks read: naming a section, and its drift now.

The daemon never reads workspace files, so an adapter beside the workspace
(CLI, MCP, SDK) does two things for a backlinks read:

- ``section_source`` turns a workspace path the agent typed into the catalog
  name the daemon keys sections by (a catalog name passes through);
- ``observe_drift`` compares each section row's cited bytes with the section
  in the file now: ``unchanged`` or ``changed_since_cited`` (a section that is
  gone has changed). A row whose file is not catalogued here keeps the
  daemon's answer.

Cost: one catalog parse (cached by revision) and one parse per distinct file
on the page.
"""

from __future__ import annotations

from pathlib import Path

from cruxible_client._safe_files import read_regular_file
from cruxible_client.authoring.selectors import workspace_files
from cruxible_client.contracts.backlinks import BacklinksResult
from cruxible_client.contracts.doc_sections import (
    DocSectionError,
    SectionDocument,
    parse_sections,
    section_digest,
)


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

    if workspace is None or not any(link.file for link in result.links):
        return result
    try:
        catalog = workspace_files(workspace)
    except ValueError:
        return result
    documents: dict[str, SectionDocument | None] = {}
    links = []
    for link in result.links:
        if link.file is None or link.section is None or link.cited_digest is None:
            links.append(link)
            continue
        if link.file not in documents:
            try:
                path = catalog.path_for_file(link.file)
                documents[link.file] = parse_sections(read_regular_file(path), file=str(path))
            except (OSError, ValueError):
                documents[link.file] = None
        document = documents[link.file]
        if document is None:
            links.append(link)
            continue
        try:
            current = section_digest(document.own_body(link.section))
        except DocSectionError:
            current = None
        drift = "unchanged" if current == link.cited_digest else "changed_since_cited"
        links.append(link.model_copy(update={"drift": drift}))
    return result.model_copy(update={"links": tuple(links)})


__all__ = ["observe_drift", "section_source"]
