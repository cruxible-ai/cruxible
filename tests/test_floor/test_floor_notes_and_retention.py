"""One review-notes snapshot per render, and no floor published over a lost body.

A rationale revised after acceptance moves the warm floor exactly as it moves
a cold one, and a client floor installed before the revision is repaired
whole. A retained body or Capture envelope that is gone refuses a fresh
render instead of rendering something else for the same accepted inputs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.authoring.floor_apply import apply_floor_delta
from cruxible_client.authoring.workspace import sync_floor_directory
from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_client.contracts.proposal_models import ProposalAdmissionRecord
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.proposals.proposal_notes import admission_bytes
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.floor.floor_content import review_snapshot_oid
from cruxible_core.service.floor.floor_delta import (
    advance_floor_index,
    service_playbill_floor_delta,
)
from tests.core_support._write_support import KIND, report_evidence, seed_write_surface
from tests.test_floor.test_floor_index import _set, _write

WI1 = f"{KIND}/wi-1"
WI3 = f"{KIND}/wi-3"
RULING = "Rulings are text.\nEvery line greps on its own.\n"


def _revise_rationale(instance: PlaybillInstance, old: str, new: str, *, back: int = 0) -> None:
    """Rewrite the note of the change ``back`` generations before the head to read ``new``."""

    from cruxible_client.contracts.proposal_models import ProposalEvaluationRecord

    with instance.accepted_history_reader() as history:
        candidate = history.generation(history.sequence - back).candidate_digest
    notes = instance._ledger.read_tree(review_snapshot_oid(instance))
    for content in notes.values():
        lines = content.splitlines(keepends=True)
        evaluation = ProposalEvaluationRecord.model_validate_json(lines[1])
        if evaluation.candidate_digest == candidate:
            break
    else:  # pragma: no cover - the fixture always notes its head change
        raise AssertionError("the head change has no note")
    admission = ProposalAdmissionRecord.model_validate_json(lines[0])
    assert admission.rationale == old
    revised = admission_bytes(admission.model_copy(update={"rationale": new})) + lines[1]
    instance._ledger.write_proposal_note("evaluation", admission.candidate_commit_oid, revised)


def _cold(instance: PlaybillInstance) -> Any:
    instance.floor_current_memo.clear()
    return advance_floor_index(instance)


def _head(instance: PlaybillInstance) -> AcceptedCoordinate:
    return AcceptedCoordinate.from_internal(instance.accepted_coordinate())


def _tree(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_a_rationale_revised_after_acceptance_moves_warm_and_cold_alike(tmp_path: Path) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    _write(instance, _set(WI1, "status", "ready"))
    before = advance_floor_index(instance)
    _revise_rationale(instance, "The writer checked it.", "Later review text.")
    warm = advance_floor_index(instance)
    assert warm.generation == before.generation
    assert warm.notes_digest != before.notes_digest
    assert any(b"Later review text." in content for content, _ in warm.files.values())
    assert dict(warm.files) == dict(_cold(instance).files)
    assert warm.manifest() == _cold(instance).manifest()


def test_a_floor_installed_before_a_notes_revision_is_repaired_whole(tmp_path: Path) -> None:
    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    _write(instance, _set(WI1, "status", "ready"))
    floor = tmp_path / "floor"

    def fetch(generation: int | None, renderer: str | None) -> Any:
        return service_playbill_floor_delta(
            instance, head=_head(instance), base_generation=generation, base_renderer=renderer
        )

    sync_floor_directory(fetch, floor)
    installed = _tree(floor)
    _revise_rationale(instance, "The writer checked it.", "Later review text.")
    head = advance_floor_index(instance).generation
    # Same generation, other notes: the delta from the installed floor cannot
    # name its base, so the apply asks for the whole floor.
    assert apply_floor_delta(floor, fetch(head, fetch(None, None).renderer)).status == (
        "base_mismatch"
    )
    assert _tree(floor) == installed
    _delta, applied = sync_floor_directory(fetch, floor)
    assert applied.status == "applied"
    expected = tmp_path / "expected"
    apply_floor_delta(expected, fetch(None, None))
    assert _tree(floor) == _tree(expected)
    assert any(b"Later review text." in content for content in _tree(floor).values())


def test_a_lost_ruling_body_refuses_a_fresh_render_never_renders_it_otherwise(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    written = _write(instance, _set(WI1, "ruling", RULING))
    warm = advance_floor_index(instance)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        digest = projection.typed.source(
            f"Claim:{written.changes[0].claim}"
        ).statement.object.content_digest
    assert instance.body_store().erase(digest)
    # The kept index still serves the floor it rendered from the retained text.
    assert dict(advance_floor_index(instance).files) == dict(warm.files)
    with pytest.raises(ProjectionIntegrityError, match="not retained"):
        _cold(instance)
    instance.floor_current_memo.clear()
    with pytest.raises(ProjectionIntegrityError, match="not retained"):
        service_playbill_floor_delta(
            instance, head=_head(instance), base_generation=None, base_renderer=None
        )


def test_a_lost_capture_envelope_refuses_a_fresh_render_never_drops_its_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "instance").mkdir()
    instance, _owner = seed_write_surface(tmp_path / "instance")
    written = _write(
        instance, _set(WI1, "measured", 3, evidence=report_evidence(tmp_path / "ws", "Count: 3"))
    )
    warm = advance_floor_index(instance)
    assert b"repo.reports" in warm.files["sources/INDEX"][0]
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        claim = projection.typed.source(f"Claim:{written.changes[0].claim}")
    (capture,) = claim.backing.capture_digests
    assert instance.body_store().erase(capture)
    from cruxible_core.service.floor import floor_sources

    floor_sources._CACHE.clear()
    assert dict(advance_floor_index(instance).files) == dict(warm.files)
    with pytest.raises(ProjectionIntegrityError, match="not retained"):
        _cold(instance)
