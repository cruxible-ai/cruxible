"""PC-A1 generic projection registry coverage."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
)
from cruxible_client.contracts.candidates import SemanticCandidate, candidate_digest
from cruxible_client.contracts.canonical import manifest_root, semantic_diff
from cruxible_client.contracts.subjects import SubjectShell, render_subject
from cruxible_core.playbill.compiler import PC_A1_COMPILER
from cruxible_core.playbill.projection import ProvisionalProjectionCoordinate
from cruxible_core.playbill.projection_subjects import (
    compile_provisional_subject_projection,
)
from tests.test_playbill._projection_support import MemoryLedger, accepted_coordinate


def test_provisional_subject_projection_is_coordinate_labeled(tmp_path: Path) -> None:
    shell = SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-1"),
        subject_kind="project.work_item",
        subject_id="wi-1",
    )
    tree = {"subjects/project.work_item/wi-1.yaml": render_subject(shell)}
    repository = MemoryLedger(tmp_path / "repository", {})
    canonical = accepted_coordinate(repository).model_copy(update={"compiler": PC_A1_COMPILER})
    difference, scope = semantic_diff({}, tree)
    candidate = SemanticCandidate(
        parent_semantic_root=canonical.semantic_root,
        candidate_manifest_root=manifest_root(tree).tagged,
        semantic_diff_digest=difference.tagged,
        scope=scope,
        timestamp="2026-08-15T12:00:00.000000Z",
    )
    coordinate = ProvisionalProjectionCoordinate(
        canonical=canonical,
        candidate=candidate,
        candidate_digest=candidate_digest(candidate).tagged,
    )
    projection = compile_provisional_subject_projection(tree, coordinate=coordinate)

    view = projection.subject("Subject:project.work_item/wi-1")
    assert view is not None
    assert view.coordinate_kind == "provisional"
    assert view.coordinate == coordinate
    assert view.envelope.kind == "subject"
