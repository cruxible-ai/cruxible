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
    Change,
    Evidence,
    FileEvidence,
    SectionEvidence,
    SetChange,
)


def observe_evidence(evidence: Evidence | None, *, workspace: Path) -> Evidence | None:
    """Fill a file evidence's observation from the workspace; other evidence passes through."""

    if not isinstance(evidence, FileEvidence | SectionEvidence) or evidence.observation is not None:
        return evidence
    if isinstance(evidence, SectionEvidence):
        selection = workspace_files(workspace).select(evidence.file).section(*evidence.section)
        return evidence.model_copy(update={"observation": selection.observation(), "file": selection.source_id, "own_words": evidence.own_words or False})
    selection = workspace_files(workspace).select(evidence.path).anchor(evidence.anchor)
    return evidence.model_copy(update={"observation": selection.observation()})


def observe_changes(changes: Sequence[Change], *, workspace: Path) -> tuple[Change, ...]:
    """Every change with its file evidence observed from ``workspace``."""

    observed: list[Change] = []
    for change in changes:
        if isinstance(change, SetChange | AddChange) and isinstance(change.evidence, FileEvidence | SectionEvidence):
            change = change.model_copy(
                update={"evidence": observe_evidence(change.evidence, workspace=workspace)}
            )
        observed.append(change)
    return tuple(observed)


__all__ = ["observe_changes", "observe_evidence"]
