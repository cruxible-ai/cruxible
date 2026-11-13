"""Read file evidence on the writer's side, before a write reaches the daemon.

The daemon never reads workspace files. A ``FileEvidence`` (``PATH#ANCHOR``)
names a catalogued workspace file and text that occurs once in it; the CLI, the
SDK and the MCP adapter read the file here, select the anchor, and send what
they observed as the evidence's ``observation``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from cruxible_client.authoring.selectors import workspace_files
from cruxible_client.contracts.write import (
    AddChange,
    CaptureEvidence,
    Change,
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
    if isinstance(evidence, SectionEvidence):
        selection = workspace_files(workspace).select(evidence.file).section(*evidence.section)
        return evidence.model_copy(
            update={
                "observation": selection.observation(),
                "file": selection.source_id,
                "own_words": evidence.own_words or False,
            }
        )
    selection = workspace_files(workspace).select(evidence.path).anchor(evidence.anchor)
    return evidence.model_copy(update={"observation": selection.observation()})


def observe_changes(changes: Sequence[Change], *, workspace: Path) -> tuple[Change, ...]:
    """Every change with its file evidence observed from ``workspace``."""

    observed: list[Change] = []
    for change in changes:
        if isinstance(change, SetChange | AddChange | RebindChange) and isinstance(
            change.evidence, FileEvidence | SectionEvidence
        ):
            change = change.model_copy(
                update={"evidence": observe_evidence(change.evidence, workspace=workspace)}
            )
        observed.append(change)
    return tuple(observed)


def _catalogued_path(workspace: Path, file: str) -> str:
    """A workspace file by its path, or by its file-catalog name (as ``next`` names it)."""

    if Path(file).is_absolute() or (workspace / file).exists():
        return file
    try:
        return str(workspace_files(workspace).path_for_file(file))
    except (ValueError, OSError):
        return file


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
    located = _catalogued_path(workspace, path)
    if file is not None:
        return FileEvidence(file=f"{located}#{rest}")
    return SectionEvidence(file=located, section=tuple(item for item in rest.split("/") if item))


__all__ = ["observe_changes", "observe_evidence", "rebind_evidence"]
