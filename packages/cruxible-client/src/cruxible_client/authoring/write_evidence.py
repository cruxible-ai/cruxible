"""Read file evidence on the writer's side, before a write reaches the daemon.

The daemon never reads workspace files. A ``FileEvidence`` (``PATH#ANCHOR``)
names a catalogued workspace file and text that occurs once in it; the CLI, the
SDK and the MCP adapter read the file here, select the anchor, and send what
they observed as the evidence's ``observation``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from cruxible_client.authoring.selectors import catalogued_path, workspace_files
from cruxible_client.contracts.errors import WriteRefusalError
from cruxible_client.contracts.workspace_layout import workspace_path
from cruxible_client.contracts.write import (
    AddChange,
    CaptureEvidence,
    Change,
    CreateChange,
    Evidence,
    FileEvidence,
    RebindChange,
    SectionEvidence,
    SetChange,
)


def observe_evidence(evidence: Evidence | None, *, workspace: Path) -> Evidence | None:
    """Fill a file evidence's observation from the workspace; other evidence passes through."""

    if not isinstance(evidence, FileEvidence | SectionEvidence) or evidence.observation is not None:
        return evidence
    named = evidence.file if isinstance(evidence, SectionEvidence) else evidence.path
    # A path or a catalog name: one resolver for every citation.
    cited = catalogued_path(workspace, named)
    path = Path(cited) if Path(cited).is_absolute() else workspace / cited
    catalogs = (workspace_path(workspace, "files.yaml"), workspace / "files.yaml")
    catalogued = any(item.is_file() for item in catalogs)
    if path.is_file() and (
        not catalogued or not workspace_files(workspace).entries_for_path(path.resolve())
    ):
        # Re-binds read a file already cited; a write citing a new file catalogues it.
        raise WriteRefusalError(
            "cruxible.doc.file_uncatalogued",
            f"{cited} is not in the workspace's file catalog",
            repair_line=f"Catalogue it first: cruxible files add {cited}",
        )
    files = workspace_files(workspace)
    if isinstance(evidence, SectionEvidence):
        selection = files.select(cited).section(*evidence.section)
        return evidence.model_copy(
            update={
                "observation": selection.observation(),
                "file": selection.source_id,
                "own_words": evidence.own_words or False,
            }
        )
    selection = files.select(cited).anchor(evidence.anchor)
    return evidence.model_copy(update={"observation": selection.observation()})


def observe_changes(changes: Sequence[Change], *, workspace: Path) -> tuple[Change, ...]:
    """Every change with its file evidence observed from ``workspace``."""

    observed: list[Change] = []
    for change in changes:
        if isinstance(change, SetChange | AddChange | CreateChange | RebindChange) and isinstance(
            change.evidence, FileEvidence | SectionEvidence
        ):
            change = change.model_copy(
                update={"evidence": observe_evidence(change.evidence, workspace=workspace)}
            )
        observed.append(change)
    return tuple(observed)


def rebind_evidence(
    *,
    file: str | None = None,
    section: str | None = None,
    capture: str | None = None,
    workspace: Path,
) -> FileEvidence | SectionEvidence | CaptureEvidence:
    """What a re-bind cites now: ``FILE#ANCHOR`` (file), ``FILE#Heading/Path`` (section),
    or a fresh Capture (a digest or ``CAP-`` handle) under the replaced citation's contract.

    FILE is a catalogued file's path or its catalog name; ``FILE#`` names a
    file's preamble. A heading that itself holds ``/`` is cited through a
    ``write`` change instead, whose section is a heading list.
    """

    if sum(item is not None for item in (file, section, capture)) != 1:
        raise ValueError(
            "pass one of file (FILE#ANCHOR), section (FILE#Heading/Path) or capture (CAP-...)"
        )
    if capture is not None:
        return CaptureEvidence(capture=capture)
    spec = file if file is not None else section
    assert spec is not None
    path, mark, rest = spec.partition("#")
    if not path or not mark or (file is not None and not rest):
        raise ValueError(
            "file takes FILE#ANCHOR"
            if file is not None
            else "section takes FILE#Heading/Path (FILE# for the preamble)"
        )
    located = catalogued_path(workspace, path)
    if file is not None:
        return FileEvidence(file=f"{located}#{rest}")
    return SectionEvidence(file=located, section=tuple(item for item in rest.split("/") if item))


__all__ = ["observe_changes", "observe_evidence", "rebind_evidence"]
