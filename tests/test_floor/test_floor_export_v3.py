"""Current-state completeness, explicit review provenance, and rebuild parity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ProjectionIntegrityError, ProposalIntegrityError
from cruxible_client.contracts.proposal_models import ProposalAdmissionRecord
from cruxible_core.proposals.proposal_notes import admission_bytes
from cruxible_core.service.claims.claims import _claim_from_view, service_list_playbill_claims
from cruxible_core.service.floor.floor import service_export_playbill_floor
from cruxible_core.service.floor.floor_content import (
    current_content,
    review_context,
    review_snapshot_oid,
)
from tests.core_support._knowledge_loop_support import seed_claims


def test_floor_v3_rebuild_scopes_and_warm_reuse(tmp_path: Path, monkeypatch) -> None:
    instance, _ = seed_claims(tmp_path)
    files = service_export_playbill_floor(instance)
    manifest = json.loads(files["manifest.json"])
    assert manifest["format"] == "playbill-floor-export-v3"
    current = json.loads(files["current/project.work_item/wi-42.json"])
    assert current["claims"][0]["statement"]["object"]["value"] == "ready"
    snapshot = json.loads(files["provenance/snapshot.json"])
    assert snapshot["status"] == "available"
    changes = [
        json.loads(body) for path, body in files.items() if path.startswith("provenance/changes/")
    ]
    assert len(changes) == 2
    assert any(
        row["proposal_id"] and row["reported_actor"] == "owner"
        for change in changes
        for row in change["review_context"]
    )
    assert not any(path.startswith(("history/", "evidence/", "cas/")) for path in files)
    assert b"status: ready" not in b"".join(files.values())
    # The immutable note snapshot, not a moving ref, is a rebuild input.
    pinned = snapshot["evaluation_notes_oid"]
    instance.floor_export_memo.clear()
    instance.floor_structure_memo.clear()
    assert service_export_playbill_floor(instance, review_notes_oid=pinned) == files

    def no_tree(*args, **kwargs):
        raise AssertionError("warm export reconstructed accepted content")

    monkeypatch.setattr(instance, "tree_at", no_tree)
    assert service_export_playbill_floor(instance, review_notes_oid=pinned) == files
    # A changed review-context snapshot must not reconstruct accepted content.
    changed_context = service_export_playbill_floor(instance, review_notes_oid="absent")
    assert (
        changed_context["current/project.work_item/wi-42.json"]
        == files["current/project.work_item/wi-42.json"]
    )
    # Returned maps are caller-owned; mutation must not poison cached exports.
    files.pop("README.md")
    assert "README.md" in service_export_playbill_floor(instance, review_notes_oid=pinned)


def test_floor_v3_absent_review_snapshot_is_replayable(tmp_path: Path) -> None:
    instance, _ = seed_claims(tmp_path)
    files = service_export_playbill_floor(instance, review_notes_oid="absent")
    snapshot = json.loads(files["provenance/snapshot.json"])
    assert snapshot["evaluation_notes_oid"] is None
    assert snapshot["status"] == "unavailable"
    assert all(
        not json.loads(body)["review_context"]
        for path, body in files.items()
        if path.startswith("provenance/changes/")
    )
    instance.floor_export_memo.clear()
    assert service_export_playbill_floor(instance, review_notes_oid="absent") == files


def test_floor_provenance_reads_only_selected_latest_records(tmp_path: Path, monkeypatch) -> None:
    instance, _ = seed_claims(tmp_path)
    claims = tuple(_claim_from_view(view) for view in service_list_playbill_claims(instance).claims)
    coordinate = instance.accepted_coordinate()
    expected = current_content(instance, coordinate=coordinate, claims=claims[:1], notes_oid=None)
    reads = []
    original = instance.blob_at

    def selected_record(oid, path):
        reads.append((oid, path))
        assert path.startswith("changesets/")
        return original(oid, path)

    def no_history(*args, **kwargs):
        raise AssertionError("floor provenance scanned accepted history")

    monkeypatch.setattr(instance, "blob_at", selected_record)
    monkeypatch.setattr(instance, "accepted_history", no_history)
    monkeypatch.setattr(instance, "tree_at", no_history)
    monkeypatch.setattr(instance, "immutable_tree_at", no_history)
    assert (
        current_content(instance, coordinate=coordinate, claims=claims[:1], notes_oid=None)
        == expected
    )
    assert len(reads) == 1
    monkeypatch.setattr(instance, "blob_at", lambda _oid, _path: None)
    with pytest.raises(ProjectionIntegrityError, match="source record is unavailable"):
        current_content(instance, coordinate=coordinate, claims=claims[:1], notes_oid=None)


def test_floor_review_context_keeps_pinned_notes_and_verifies_cold_reads(
    tmp_path: Path, monkeypatch
) -> None:
    instance, _ = seed_claims(tmp_path)
    notes_oid = review_snapshot_oid(instance)
    assert notes_oid is not None
    expected = review_context(instance, notes_oid)
    notes = instance._ledger.read_tree(notes_oid)
    content = next(iter(notes.values()))
    lines = content.splitlines(keepends=True)
    admission = ProposalAdmissionRecord.model_validate_json(lines[0])
    revised = admission.model_copy(update={"rationale": "Later review text."})
    revised_pair = admission_bytes(revised) + lines[1]
    instance._ledger.write_proposal_note("evaluation", admission.candidate_commit_oid, revised_pair)
    assert review_snapshot_oid(instance) != notes_oid
    assert review_context(instance, notes_oid) == expected

    # An explicit cold read still verifies the exact retained bytes after a
    # prior successful request; current source-index rows cannot substitute.
    with monkeypatch.context() as patch:
        patch.setattr(instance._ledger, "read_tree", lambda _oid: {"a": lines[0]})
        with pytest.raises(ProposalIntegrityError, match="incomplete record pair"):
            review_context(instance, notes_oid)
        patch.setattr(
            instance._ledger,
            "read_tree",
            lambda _oid: {"a": lines[0] + lines[1], "b": revised_pair},
        )
        with pytest.raises(ProposalIntegrityError, match="aliases disagree"):
            review_context(instance, notes_oid)
        patch.setattr(instance._ledger, "read_tree", lambda _oid: {"a": b" " + lines[0] + lines[1]})
        with pytest.raises(ProposalIntegrityError, match="not a canonical proposal pair"):
            review_context(instance, notes_oid)

    def no_body(_oid):
        raise AssertionError("over-budget review snapshot was read")

    monkeypatch.setattr(instance._ledger, "read_tree", no_body)
    monkeypatch.setattr("cruxible_core.service.floor.floor_content.MAX_REVIEW_SNAPSHOT_BYTES", 0)
    assert review_context(instance, notes_oid) == ({}, "review_snapshot_budget_exceeded")
