"""Current-state completeness, explicit review provenance, and rebuild parity."""

from __future__ import annotations

import json
from pathlib import Path

from cruxible_core.service.floor.floor import service_export_playbill_floor
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
    instance.floor_history_memo.clear()
    instance.floor_review_memo.clear()
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
