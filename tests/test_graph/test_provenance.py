"""Tests for typed relationship provenance helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.provenance import (
    CLONE_ORIGIN_UPSTREAM_SNAPSHOT,
    RelationshipProvenance,
    backfill_provenance_on_touch,
    dump_provenance,
    load_provenance,
    make_provenance,
    provenance_group_id,
    relabel_provenance_for_clone,
    stamp_provenance_modified,
)
from cruxible_core.graph.types import (
    EntityInstance,
    RelationshipInstance,
    RelationshipMetadata,
)


def test_make_provenance_returns_model_and_dump_returns_dict() -> None:
    provenance = make_provenance("workflow_apply", "workflow:canonical-fitment")

    assert isinstance(provenance, RelationshipProvenance)
    dumped = dump_provenance(provenance)
    assert dumped["source"] == "workflow_apply"
    assert dumped["source_ref"] == "workflow:canonical-fitment"
    assert isinstance(dumped["created_at"], str)


def test_load_provenance_accepts_partial_dict() -> None:
    provenance = load_provenance({"source_ref": "group:GRP-test"})

    assert provenance is not None
    assert provenance.source_ref == "group:GRP-test"
    assert provenance_group_id(provenance) == "GRP-test"
    assert dump_provenance(provenance) == {"source_ref": "group:GRP-test"}


def test_extra_fields_survive_stamp_and_dump() -> None:
    provenance = load_provenance(
        {
            "source": "group_resolve",
            "source_ref": "group:GRP-test",
            "created_at": "2026-01-01T00:00:00+00:00",
            "calibration_id": "catalog_match_v3",
        }
    )

    assert provenance is not None
    stamped = stamp_provenance_modified(provenance, "feedback:approve")
    dumped = dump_provenance(stamped)

    assert dumped["source"] == "group_resolve"
    assert dumped["source_ref"] == "group:GRP-test"
    assert dumped["calibration_id"] == "catalog_match_v3"
    assert dumped["created_at"] == "2026-01-01T00:00:00+00:00"
    assert dumped["last_modified_by"] == "feedback:approve"
    assert isinstance(dumped["last_modified_at"], str)


def test_load_provenance_rejects_non_dict_values() -> None:
    assert load_provenance(None) is None
    assert load_provenance("not provenance") is None


def _actor_context() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="user-1",
        org_id="org-1",
        operation_id="op-1",
        timestamp=datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC),
    )


def test_make_provenance_stamps_write_correlation_fields() -> None:
    provenance = make_provenance(
        "group_resolve",
        "group:GRP-test",
        receipt_id="RCP-abc123",
        resolution_id="RES-xyz789",
        actor_context=_actor_context(),
    )

    dumped = dump_provenance(provenance)
    assert dumped["receipt_id"] == "RCP-abc123"
    assert dumped["resolution_id"] == "RES-xyz789"
    assert dumped["created_actor_context"]["actor_id"] == "user-1"
    assert dumped["created_actor_context"]["org_id"] == "org-1"
    assert "last_modified_actor_context" not in dumped


def test_correlation_fields_default_null_and_stay_out_of_dump() -> None:
    provenance = make_provenance("batch_direct_write", "batch_direct_write")

    assert provenance.receipt_id is None
    assert provenance.resolution_id is None
    assert provenance.created_actor_context is None
    dumped = dump_provenance(provenance)
    assert "receipt_id" not in dumped
    assert "resolution_id" not in dumped
    assert "created_actor_context" not in dumped


def test_stamp_modified_preserves_creation_correlation_fields() -> None:
    provenance = make_provenance(
        "batch_direct_write",
        "batch_direct_write",
        receipt_id="RCP-create",
    )

    stamped = stamp_provenance_modified(
        provenance,
        "agent",
        actor_context=_actor_context(),
    )

    assert stamped.receipt_id == "RCP-create"
    assert stamped.created_actor_context is None
    assert stamped.last_modified_actor_context is not None
    assert stamped.last_modified_actor_context.actor_id == "user-1"


def test_backfill_on_touch_stamps_existing_provenance() -> None:
    existing = make_provenance("ingest", "fitments", receipt_id="RCP-create")

    result = backfill_provenance_on_touch(
        existing, "cli_add", "add_relationship", "cli_add"
    )

    # Existing provenance is stamped, not replaced: creation fields survive.
    assert result.source == "ingest"
    assert result.source_ref == "fitments"
    assert result.receipt_id == "RCP-create"
    assert result.last_modified_by == "cli_add"
    assert result.last_modified_at is not None


def test_backfill_on_touch_creates_provenance_when_null() -> None:
    result = backfill_provenance_on_touch(
        None,
        "human",
        "feedback:approve",
        "feedback:approve",
        actor_context=_actor_context(),
    )

    assert result.source == "human"
    assert result.source_ref == "feedback:approve"
    assert result.last_modified_by == "feedback:approve"
    assert result.last_modified_at is not None
    assert result.last_modified_actor_context is not None
    assert result.last_modified_actor_context.actor_id == "user-1"
    # No creation correlation is fabricated for a backfilled edge.
    assert result.created_at is None
    assert result.created_actor_context is None


def test_relabel_for_clone_clears_correlation_and_stamps_origin() -> None:
    provenance = make_provenance(
        "workflow_apply",
        "workflow:canonical-fitment",
        receipt_id="RCP-source",
        resolution_id="RES-source",
    )

    relabeled = relabel_provenance_for_clone(provenance)

    assert relabeled is not None
    # Dangling write-time correlation is cleared.
    assert relabeled.receipt_id is None
    assert relabeled.resolution_id is None
    # Clone origin is stamped, preserving the cleared receipt for traceability.
    assert relabeled.clone_origin == CLONE_ORIGIN_UPSTREAM_SNAPSHOT
    dumped = dump_provenance(relabeled)
    assert dumped["clone_origin"] == "upstream-snapshot"
    assert dumped["cloned_receipt_id"] == "RCP-source"
    assert "receipt_id" not in dumped
    assert "resolution_id" not in dumped
    # Authoring history is preserved.
    assert relabeled.source == "workflow_apply"
    assert relabeled.source_ref == "workflow:canonical-fitment"


def test_relabel_for_clone_is_noop_for_clean_provenance() -> None:
    # Legacy/null-receipt edges (and already-relabeled clone edges) are untouched.
    legacy = load_provenance({"source": "workflow_apply", "source_ref": "workflow:apply"})
    assert legacy is not None
    assert relabel_provenance_for_clone(legacy) is legacy
    assert relabel_provenance_for_clone(None) is None


def test_relabel_for_clone_supports_custom_origin() -> None:
    provenance = make_provenance("group_resolve", "group:GRP-1", receipt_id="RCP-1")

    relabeled = relabel_provenance_for_clone(provenance, origin="custom-origin")

    assert relabeled is not None
    assert relabeled.clone_origin == "custom-origin"


def _fits_edge(receipt_id: str | None) -> RelationshipInstance:
    provenance = (
        RelationshipProvenance(source="workflow_apply", receipt_id=receipt_id)
        if receipt_id is not None
        else RelationshipProvenance(source="workflow_apply")
    )
    return RelationshipInstance(
        relationship_type="fits",
        from_type="Part",
        from_id="BP-1",
        to_type="Vehicle",
        to_id="V-1",
        metadata=RelationshipMetadata(provenance=provenance),
    )


def test_graph_relabel_clone_receipts_clears_only_dangling_edges() -> None:
    graph = EntityGraph()
    graph.add_entity(EntityInstance(entity_type="Part", entity_id="BP-1"))
    graph.add_entity(EntityInstance(entity_type="Vehicle", entity_id="V-1"))
    graph.add_relationship(_fits_edge("RCP-dangling"))
    graph.add_relationship(_fits_edge(None))  # legacy null-receipt edge

    relabeled = graph.relabel_clone_receipts()

    assert relabeled == 1  # only the receipt-bearing edge is touched
    receipt_ids = []
    clone_origins = []
    for rel in graph.iter_relationships():
        assert rel.metadata.provenance is not None
        receipt_ids.append(rel.metadata.provenance.receipt_id)
        clone_origins.append(rel.metadata.provenance.clone_origin)
    # No edge retains a receipt_id; the relabeled edge records clone origin.
    assert receipt_ids == [None, None]
    assert CLONE_ORIGIN_UPSTREAM_SNAPSHOT in clone_origins
    # Re-running is a no-op (idempotent): nothing left to relabel.
    assert graph.relabel_clone_receipts() == 0


def test_historical_provenance_loads_with_null_correlation_fields() -> None:
    provenance = load_provenance(
        {
            "source": "workflow_apply",
            "source_ref": "workflow:canonical-fitment:apply",
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )

    assert provenance is not None
    assert provenance.receipt_id is None
    assert provenance.resolution_id is None
    assert provenance.created_actor_context is None
    assert provenance.last_modified_actor_context is None
