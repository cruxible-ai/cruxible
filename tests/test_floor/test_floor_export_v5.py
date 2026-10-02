"""Current-state completeness, explicit review provenance, and rebuild parity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_client.contracts.proposal_models import ProposalAdmissionRecord
from cruxible_client.contracts.write import PlaybillWriteRequestV1
from cruxible_core.proposals.proposal_notes import admission_bytes
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.floor.floor import service_export_playbill_floor
from cruxible_core.service.floor.floor_content import (
    change_rationales,
    review_snapshot_oid,
)
from tests.core_support._knowledge_loop_support import seed_claims
from tests.core_support._write_support import KIND, caller, seed_write_surface


def test_floor_v5_rebuild_scopes_and_warm_reuse(tmp_path: Path, monkeypatch) -> None:
    instance, _ = seed_claims(tmp_path)
    files = service_export_playbill_floor(instance)
    manifest = json.loads(files["manifest.json"])
    assert manifest["format"] == "playbill-floor-export-v5"
    assert manifest["generation"] == len(instance.accepted_history()) - 1
    assert b"\nstatus: ready  # CLM-" in files["current/project.work_item/wi-42.yaml"]
    # No source content and no provenance mirror: the ledger clone is the audit path.
    assert not any(path.startswith(("provenance/", "documents/")) for path in files)
    changes = [json.loads(body) for path, body in files.items() if path.startswith("changes/")]
    assert len(changes) == 2
    assert all(change["actor"] == "owner" for change in changes)
    # No boilerplate and no digests in the grep path.
    assert all(
        set(change) == {"sequence", "timestamp", "actor", "rationale", "changed"}
        for change in changes
    )
    assert not any(
        b"sha256:" in body for path, body in files.items() if path.startswith("changes/")
    )
    assert not any(path.startswith(("history/", "evidence/", "cas/")) for path in files)
    # The evidence body is the bytes "status: ready". current/ shows the Claim's
    # value "ready" under its field "status", which spells the same; nothing
    # else may carry those bytes.
    assert not any(
        b"status: ready" in body for path, body in files.items() if not path.startswith("current/")
    )
    # The immutable note snapshot, not a moving ref, is a rebuild input.
    pinned = review_snapshot_oid(instance)
    assert pinned is not None
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
        changed_context["current/project.work_item/wi-42.yaml"]
        == files["current/project.work_item/wi-42.yaml"]
    )
    # Returned maps are caller-owned; mutation must not poison cached exports.
    files.pop("README.md")
    assert "README.md" in service_export_playbill_floor(instance, review_notes_oid=pinned)


def test_floor_v5_absent_review_snapshot_is_replayable(tmp_path: Path) -> None:
    instance, _ = seed_claims(tmp_path)
    files = service_export_playbill_floor(instance, review_notes_oid="absent")
    assert all(
        not json.loads(body)["rationale"]
        for path, body in files.items()
        if path.startswith("changes/")
    )
    instance.floor_export_memo.clear()
    assert service_export_playbill_floor(instance, review_notes_oid="absent") == files


def test_floor_changes_reverify_no_change_set_record(tmp_path: Path, monkeypatch) -> None:
    instance, _ = seed_claims(tmp_path)
    expected = service_export_playbill_floor(instance, review_notes_oid="absent")
    instance.floor_current_memo.clear()

    def no_records(*args, **kwargs):
        raise AssertionError("floor changes re-verified a change-set record")

    def no_history(*args, **kwargs):
        raise AssertionError("floor changes scanned accepted history")

    # A change's members come from the history index and its time from the
    # replayed generation, never from a re-verified record.
    monkeypatch.setattr(instance, "retained_record_reader", no_records)
    monkeypatch.setattr(instance, "tree_at", no_history)
    monkeypatch.setattr(instance, "immutable_tree_at", no_history)
    assert service_export_playbill_floor(instance, review_notes_oid="absent") == expected
    changes = [json.loads(body) for path, body in expected.items() if path.startswith("changes/")]
    assert changes and all(change["changed"] for change in changes)


def test_change_rationale_reads_one_pinned_note_by_path(tmp_path: Path, monkeypatch) -> None:
    instance, _ = seed_write_surface(tmp_path)
    request = PlaybillWriteRequestV1.model_validate(
        {
            "because": "The writer checked it.",
            "changes": [
                {"op": "set", "subject": f"{KIND}/wi-1", "field": "status", "value": "ready"}
            ],
        }
    )
    assert service_playbill_write(instance, request=request, caller=caller()).status == "accepted"
    notes_oid = review_snapshot_oid(instance)
    assert notes_oid is not None
    with instance.accepted_history_reader() as history:
        generations = [history.generation(item) for item in range(1, history.sequence + 1)]
    expected = change_rationales(instance, generations, notes_oid)
    assert expected[generations[-1].sequence] == ("The writer checked it.",)
    files = service_export_playbill_floor(instance)
    (change,) = (
        json.loads(body)
        for path, body in files.items()
        if path.startswith("changes/") and b"checked" in body
    )
    assert change["rationale"] == ["The writer checked it."]
    notes = instance._ledger.read_tree(notes_oid)
    content = next(body for body in notes.values() if b"The writer checked it." in body)
    lines = content.splitlines(keepends=True)
    admission = ProposalAdmissionRecord.model_validate_json(lines[0])
    revised = admission.model_copy(update={"rationale": "Later review text."})
    revised_pair = admission_bytes(revised) + lines[1]
    instance._ledger.write_proposal_note("evaluation", admission.candidate_commit_oid, revised_pair)
    assert review_snapshot_oid(instance) != notes_oid
    # The pinned notes commit, not the moving ref, answers.
    assert change_rationales(instance, generations, notes_oid) == expected

    # The notes tree is never read whole: only each accepted candidate's note.
    def no_tree(_oid):
        raise AssertionError("floor read the whole notes tree")

    monkeypatch.setattr(instance._ledger, "read_tree", no_tree)
    assert change_rationales(instance, generations, notes_oid) == expected
    with monkeypatch.context() as patch:
        patch.setattr(
            instance, "read_proposal_notes", lambda pairs, **_: {pair: lines[0] for pair in pairs}
        )
        with pytest.raises(ProposalIntegrityError, match="incomplete record pair"):
            change_rationales(instance, generations, notes_oid)
        patch.setattr(
            instance,
            "read_proposal_notes",
            lambda pairs, **_: {pair: b" " + lines[0] + lines[1] for pair in pairs},
        )
        with pytest.raises(ProposalIntegrityError, match="not a canonical proposal pair"):
            change_rationales(instance, generations, notes_oid)
